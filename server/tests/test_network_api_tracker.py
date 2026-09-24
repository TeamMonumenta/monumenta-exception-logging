# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Byron Marohn
"""
Tests for the Tracker-layer additions backing the network API (see NETWORK_API.md).

Covers:
- unmute_group clearing mute/resolve attribution + has_activity on all three mutations
- list_groups / count_groups filters, sort, clamping, and validation
- get_recent_occurrences / get_distinct_servers / get_discord_message_id
"""

import copy
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest
from tracker.config import TrackerConfig
from tracker.api import Tracker
from tracker.ingest import parse_event
from tests.fixtures import EXAMPLE_EVENT, EXAMPLE_EVENT_2


@pytest.fixture
def fresh_api():
    return Tracker(TrackerConfig(db_path=':memory:'))


def _event_from(base: dict, server_id: str) -> dict:
    ev = copy.deepcopy(base)
    ev['server_id'] = server_id
    return ev


def _get_has_activity(api: Tracker, fingerprint: str) -> int:
    row = api._conn.execute(  # pylint: disable=protected-access
        "SELECT has_activity FROM error_groups WHERE fingerprint = ?", (fingerprint,)
    ).fetchone()
    assert row is not None
    return row['has_activity']


# ===========================================================================
# get_fix_attempt / get_fix_attempts_for_group
# ===========================================================================

def test_get_fix_attempt_unknown_job_id(fresh_api):
    assert fresh_api.get_fix_attempt("no-such-job") is None


def test_get_fix_attempt_reflects_pending_state(fresh_api):
    fp, _ = fresh_api.ingest_event(parse_event(EXAMPLE_EVENT))
    job_id = fresh_api.queue_fix_attempt(fp, "prompt text", "123456789012345678")
    status = fresh_api.get_fix_attempt(job_id)
    assert status.job_id == job_id
    assert status.fingerprint == fp
    assert status.status == "pending"
    assert status.message is None
    assert status.pr_url is None
    assert status.started_at is None
    assert status.completed_at is None
    assert status.queued_at is not None


def test_get_fix_attempt_reflects_completed_state(fresh_api):
    fp, _ = fresh_api.ingest_event(parse_event(EXAMPLE_EVENT))
    job_id = fresh_api.queue_fix_attempt(fp, "prompt text", "123456789012345678")
    fresh_api.complete_fix_attempt(
        job_id, status="success", message="fixed", summary="did stuff",
        detail="detailed log", pr_url="https://github.com/example/repo/pull/1",
    )
    status = fresh_api.get_fix_attempt(job_id)
    assert status.status == "success"
    assert status.message == "fixed"
    assert status.pr_url == "https://github.com/example/repo/pull/1"
    assert status.completed_at is not None


def test_get_fix_attempts_for_group_newest_first(fresh_api):
    fp, _ = fresh_api.ingest_event(parse_event(EXAMPLE_EVENT))
    job1 = fresh_api.queue_fix_attempt(fp, "first", "1")
    fresh_api.complete_fix_attempt(job1, "failure", "no", "", "", None)
    job2 = fresh_api.queue_fix_attempt(fp, "second", "1")
    attempts = fresh_api.get_fix_attempts_for_group(fp)
    assert [a.job_id for a in attempts] == [job2, job1]


def test_get_fix_attempts_for_group_empty_for_unknown_fingerprint(fresh_api):
    assert fresh_api.get_fix_attempts_for_group("0" * 64) == []


def test_get_fix_attempts_for_group_respects_limit(fresh_api):
    fp, _ = fresh_api.ingest_event(parse_event(EXAMPLE_EVENT))
    for _ in range(5):
        fresh_api.queue_fix_attempt(fp, "msg", "1")
    attempts = fresh_api.get_fix_attempts_for_group(fp, limit=2)
    assert len(attempts) == 2


# ===========================================================================
# unmute_group attribution clearing + has_activity on mutations
# ===========================================================================

def test_unmute_clears_mute_attribution(fresh_api):
    fp, _ = fresh_api.ingest_event(parse_event(EXAMPLE_EVENT))
    fresh_api.mute_group(fp, actor="Alice")
    fresh_api.unmute_group(fp, actor="Bob")
    details = fresh_api.get_group_details(fp)
    assert details.status == "active"
    assert details.muted_by is None
    assert details.muted_at is None


def test_unmute_clears_resolve_attribution_too(fresh_api):
    """README.md: removing the resolve reaction 'always unmutes' - unmute_group
    already un-resolves, so it must clear resolved_by/resolved_at as well."""
    fp, _ = fresh_api.ingest_event(parse_event(EXAMPLE_EVENT))
    fresh_api.resolve_group(fp, actor="Alice")
    fresh_api.unmute_group(fp, actor="Bob")
    details = fresh_api.get_group_details(fp)
    assert details.status == "active"
    assert details.resolved_by is None
    assert details.resolved_at is None


def test_unmute_group_default_actor(fresh_api):
    fp, _ = fresh_api.ingest_event(parse_event(EXAMPLE_EVENT))
    fresh_api.mute_group(fp)
    assert fresh_api.unmute_group(fp) is True


def test_unmute_group_unknown_fingerprint_returns_false(fresh_api):
    assert fresh_api.unmute_group("0" * 64) is False


def test_mute_group_sets_has_activity(fresh_api):
    fp, _ = fresh_api.ingest_event(parse_event(EXAMPLE_EVENT))
    fresh_api.clear_has_activity(fp)
    assert _get_has_activity(fresh_api, fp) == 0
    fresh_api.mute_group(fp, actor="Alice")
    assert _get_has_activity(fresh_api, fp) == 1


def test_resolve_group_sets_has_activity(fresh_api):
    fp, _ = fresh_api.ingest_event(parse_event(EXAMPLE_EVENT))
    fresh_api.clear_has_activity(fp)
    fresh_api.resolve_group(fp, actor="Alice")
    assert _get_has_activity(fresh_api, fp) == 1


def test_unmute_group_sets_has_activity(fresh_api):
    fp, _ = fresh_api.ingest_event(parse_event(EXAMPLE_EVENT))
    fresh_api.mute_group(fp, actor="Alice")
    fresh_api.clear_has_activity(fp)
    fresh_api.unmute_group(fp, actor="Bob")
    assert _get_has_activity(fresh_api, fp) == 1


# ===========================================================================
# list_groups / count_groups
# ===========================================================================

def test_list_groups_no_filters_returns_all(fresh_api):
    fp1, _ = fresh_api.ingest_event(parse_event(EXAMPLE_EVENT))
    fp2, _ = fresh_api.ingest_event(parse_event(EXAMPLE_EVENT_2))
    groups = fresh_api.list_groups()
    fps = {g.fingerprint for g in groups}
    assert fps == {fp1, fp2}


def test_count_groups_no_filters_matches_list(fresh_api):
    fresh_api.ingest_event(parse_event(EXAMPLE_EVENT))
    fresh_api.ingest_event(parse_event(EXAMPLE_EVENT_2))
    assert fresh_api.count_groups() == 2
    assert len(fresh_api.list_groups()) == 2


def test_list_groups_filters_by_status(fresh_api):
    fp1, _ = fresh_api.ingest_event(parse_event(EXAMPLE_EVENT))
    fp2, _ = fresh_api.ingest_event(parse_event(EXAMPLE_EVENT_2))
    fresh_api.mute_group(fp1)
    active = fresh_api.list_groups(status="active")
    muted = fresh_api.list_groups(status="muted")
    assert [g.fingerprint for g in active] == [fp2]
    assert [g.fingerprint for g in muted] == [fp1]
    assert fresh_api.count_groups(status="active") == 1
    assert fresh_api.count_groups(status="muted") == 1


def test_list_groups_invalid_status_raises(fresh_api):
    with pytest.raises(ValueError):
        fresh_api.list_groups(status="bogus")
    with pytest.raises(ValueError):
        fresh_api.count_groups(status="bogus")


def test_list_groups_invalid_sort_raises(fresh_api):
    with pytest.raises(ValueError):
        fresh_api.list_groups(sort="bogus")


def test_list_groups_filters_by_server_regardless_of_window(fresh_api):
    """server filter matches if the server EVER contributed, not just within window_hours."""
    fp, _ = fresh_api.ingest_event(parse_event(_event_from(EXAMPLE_EVENT, 'build')))
    conn = fresh_api._conn  # pylint: disable=protected-access
    conn.execute("UPDATE server_hour_counts SET hour_bucket = 0")
    conn.commit()
    groups = fresh_api.list_groups(server='build', window_hours=1)
    assert [g.fingerprint for g in groups] == [fp]
    assert fresh_api.count_groups(server='build') == 1


def test_list_groups_server_filter_excludes_non_matching(fresh_api):
    fresh_api.ingest_event(parse_event(_event_from(EXAMPLE_EVENT, 'build')))
    groups = fresh_api.list_groups(server='does-not-exist')
    assert groups == []
    assert fresh_api.count_groups(server='does-not-exist') == 0


def test_list_groups_search_matches_exception_class(fresh_api):
    fp, _ = fresh_api.ingest_event(parse_event(EXAMPLE_EVENT))
    groups = fresh_api.list_groups(search="lang.exception")
    assert [g.fingerprint for g in groups] == [fp]


def test_list_groups_new_within_hours_reproduces_new_command(fresh_api):
    fp, _ = fresh_api.ingest_event(parse_event(EXAMPLE_EVENT))
    conn = fresh_api._conn  # pylint: disable=protected-access
    conn.execute("UPDATE error_groups SET first_seen = first_seen - 1000000")
    conn.commit()
    recent = fresh_api.list_groups(new_within_hours=24)
    assert fp not in [g.fingerprint for g in recent]
    everything = fresh_api.list_groups(new_within_hours=None)
    assert fp in [g.fingerprint for g in everything]


def test_list_groups_limit_hard_capped_at_500(fresh_api):
    fresh_api.ingest_event(parse_event(EXAMPLE_EVENT))
    # Requesting an absurd limit must not raise or bypass the cap.
    groups = fresh_api.list_groups(limit=100000)
    assert len(groups) <= 500


def test_list_groups_offset_clamped_non_negative(fresh_api):
    fresh_api.ingest_event(parse_event(EXAMPLE_EVENT))
    groups = fresh_api.list_groups(offset=-5)
    assert len(groups) == 1


def test_list_groups_window_hours_clamped_to_retention(fresh_api):
    fp, _ = fresh_api.ingest_event(parse_event(EXAMPLE_EVENT))
    # window_hours far beyond expiry_days*24 must be clamped, not error, and must not
    # crash on a negative cutoff computation.
    groups = fresh_api.list_groups(window_hours=10**9, sort="recent")
    assert fp in [g.fingerprint for g in groups]


def test_list_groups_sort_last_seen_orders_desc(fresh_api):
    fp1, _ = fresh_api.ingest_event(parse_event(EXAMPLE_EVENT))
    fp2, _ = fresh_api.ingest_event(parse_event(EXAMPLE_EVENT_2))
    conn = fresh_api._conn  # pylint: disable=protected-access
    conn.execute("UPDATE error_groups SET last_seen = 100 WHERE fingerprint = ?", (fp1,))
    conn.execute("UPDATE error_groups SET last_seen = 200 WHERE fingerprint = ?", (fp2,))
    conn.commit()
    groups = fresh_api.list_groups(sort="last_seen")
    assert [g.fingerprint for g in groups] == [fp2, fp1]


def test_list_groups_sort_recent_orders_by_windowed_count(fresh_api):
    fp1, _ = fresh_api.ingest_event(parse_event(EXAMPLE_EVENT))
    fp2, _ = fresh_api.ingest_event(parse_event(EXAMPLE_EVENT_2))
    for _ in range(3):
        fresh_api.ingest_event(parse_event(EXAMPLE_EVENT_2))
    groups = fresh_api.list_groups(sort="recent")
    assert groups[0].fingerprint == fp2
    assert fp1 in [g.fingerprint for g in groups]


def test_list_groups_recent_count_and_server_counts_populated(fresh_api):
    fp, _ = fresh_api.ingest_event(parse_event(_event_from(EXAMPLE_EVENT, 'build')))
    groups = fresh_api.list_groups()
    g = next(g for g in groups if g.fingerprint == fp)
    assert g.recent_count == 1
    assert g.server_counts == {'build': 1}


def test_list_groups_filter_clause_shared_with_count(fresh_api):
    """count_groups must never disagree with len(list_groups(...)) for the same filters
    (aside from limit/offset truncation), since they share one clause builder."""
    fp1, _ = fresh_api.ingest_event(parse_event(EXAMPLE_EVENT))
    fresh_api.ingest_event(parse_event(EXAMPLE_EVENT_2))
    fresh_api.mute_group(fp1)
    for status in ("active", "muted", "resolved", None):
        assert fresh_api.count_groups(status=status) == len(
            fresh_api.list_groups(status=status, limit=500)
        )


# ===========================================================================
# get_recent_occurrences / get_distinct_servers / get_discord_message_id
# ===========================================================================

def test_get_recent_occurrences_newest_first(fresh_api):
    fp, _ = fresh_api.ingest_event(parse_event(EXAMPLE_EVENT))
    fresh_api.ingest_event(parse_event(EXAMPLE_EVENT))
    occurrences = fresh_api.get_recent_occurrences(fp)
    assert len(occurrences) == 2
    assert occurrences[0].timestamp >= occurrences[1].timestamp


def test_get_recent_occurrences_unknown_fingerprint(fresh_api):
    assert fresh_api.get_recent_occurrences("0" * 64) == []


def test_get_recent_occurrences_respects_limit(fresh_api):
    fp, _ = fresh_api.ingest_event(parse_event(EXAMPLE_EVENT))
    for _ in range(4):
        fresh_api.ingest_event(parse_event(EXAMPLE_EVENT))
    occurrences = fresh_api.get_recent_occurrences(fp, limit=2)
    assert len(occurrences) == 2


def test_get_distinct_servers(fresh_api):
    fresh_api.ingest_event(parse_event(_event_from(EXAMPLE_EVENT, 'alpha')))
    fresh_api.ingest_event(parse_event(_event_from(EXAMPLE_EVENT_2, 'beta')))
    assert fresh_api.get_distinct_servers() == ['alpha', 'beta']


def test_get_distinct_servers_empty(fresh_api):
    assert fresh_api.get_distinct_servers() == []


def test_get_discord_message_id_found(fresh_api):
    fp, _ = fresh_api.ingest_event(parse_event(EXAMPLE_EVENT))
    fresh_api.set_discord_message_id(fp, "42")
    assert fresh_api.get_discord_message_id(fp) == "42"


def test_get_discord_message_id_none_when_unset(fresh_api):
    fp, _ = fresh_api.ingest_event(parse_event(EXAMPLE_EVENT))
    assert fresh_api.get_discord_message_id(fp) is None


def test_get_discord_message_id_unknown_fingerprint(fresh_api):
    assert fresh_api.get_discord_message_id("0" * 64) is None


def test_group_exists(fresh_api):
    fp, _ = fresh_api.ingest_event(parse_event(EXAMPLE_EVENT))
    assert fresh_api.group_exists(fp) is True
    assert fresh_api.group_exists("0" * 64) is False


# ===========================================================================
# Paging stability and status='all'
# ===========================================================================

def test_list_groups_paging_is_stable_when_sort_keys_tie(fresh_api):
    """Groups ingested together share a last_seen, and most groups share a recent_count
    of 0. Without a tiebreaker the planner picks the order of tied rows, and successive
    --offset pages can repeat or drop rows. Every page must be disjoint and complete."""
    events = [
        _event_from(EXAMPLE_EVENT, 'alpha'),
        _event_from(EXAMPLE_EVENT_2, 'beta'),
        _event_from(EXAMPLE_EVENT, 'gamma'),
        _event_from(EXAMPLE_EVENT_2, 'delta'),
    ]
    for ev in events:
        fresh_api.ingest_event(parse_event(ev))
    total = fresh_api.count_groups()
    assert total >= 2  # the fixtures fingerprint to at least two distinct groups

    for sort in ('last_seen', 'first_seen', 'total_count', 'recent'):
        seen: list[str] = []
        for offset in range(0, total, 1):
            page = fresh_api.list_groups(sort=sort, limit=1, offset=offset)
            seen.extend(g.fingerprint for g in page)
        assert len(seen) == total, f"{sort}: paged {len(seen)} rows, expected {total}"
        assert len(set(seen)) == total, f"{sort}: paging repeated a row"


def test_list_groups_status_all_means_no_filter(fresh_api):
    """'all' is accepted as an explicit synonym for None rather than rejected; it is
    the obvious thing for an API caller to try."""
    fp1, _ = fresh_api.ingest_event(parse_event(EXAMPLE_EVENT))
    fresh_api.ingest_event(parse_event(EXAMPLE_EVENT_2))
    fresh_api.mute_group(fp1)
    assert len(fresh_api.list_groups(status='all')) == len(fresh_api.list_groups())
    assert fresh_api.count_groups(status='all') == fresh_api.count_groups()


def test_get_fix_attempts_for_group_limit_is_clamped(fresh_api):
    """An API-supplied limit must not be able to ask for an unbounded result set."""
    fp, _ = fresh_api.ingest_event(parse_event(EXAMPLE_EVENT))
    fresh_api.queue_fix_attempt(fp, "msg", "123456789012345678")
    assert len(fresh_api.get_fix_attempts_for_group(fp, limit=10_000)) == 1
    assert fresh_api.get_fix_attempts_for_group(fp, limit=0) == []


# ===========================================================================
# Indexes
# ===========================================================================

def test_occurrences_server_index_exists(fresh_api):
    """/api/servers and the list_groups `server` filter both scan occurrences.server.
    Unindexed that is a full scan of the largest table, measured at ~2.1s per call at
    1M rows, blocking the shared event loop. The index is not optional."""
    rows = fresh_api._conn.execute(  # pylint: disable=protected-access
        "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'occurrences'"
    ).fetchall()
    assert 'idx_occurrences_server' in {row['name'] for row in rows}


def test_server_filter_uses_the_index(fresh_api):
    """Guards the query shape as well as the index: a rewrite that wraps the column in
    a function (LOWER(server) = ?) would silently fall back to a scan."""
    fresh_api.ingest_event(parse_event(_event_from(EXAMPLE_EVENT, 'alpha')))
    plan = fresh_api._conn.execute(  # pylint: disable=protected-access
        "EXPLAIN QUERY PLAN SELECT DISTINCT group_id FROM occurrences WHERE server = ?",
        ('alpha',)
    ).fetchall()
    assert any('idx_occurrences_server' in str(row['detail']) for row in plan), plan
