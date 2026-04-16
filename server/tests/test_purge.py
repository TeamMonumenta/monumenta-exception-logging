# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Byron Marohn
"""Tests for the Tracker purge methods."""

import copy
import sys
import os
import time

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


# ===========================================================================
# purge_server
# ===========================================================================

def test_purge_server_removes_exclusive_group(fresh_api):
    fp, _ = fresh_api.ingest_event(parse_event(_event_from(EXAMPLE_EVENT, 'build')))
    n, msg_ids = fresh_api.purge_server('build')
    assert n == 1
    assert msg_ids == []
    assert fresh_api.get_group_details(fp) is None


def test_purge_server_spares_mixed_group(fresh_api):
    # fp1: only on build
    fp1, _ = fresh_api.ingest_event(parse_event(_event_from(EXAMPLE_EVENT, 'build')))
    # fp2: on build AND play — should survive
    fp2, _ = fresh_api.ingest_event(parse_event(_event_from(EXAMPLE_EVENT_2, 'build')))
    fresh_api.ingest_event(parse_event(_event_from(EXAMPLE_EVENT_2, 'play')))

    n, _ = fresh_api.purge_server('build')
    assert n == 1
    assert fresh_api.get_group_details(fp1) is None
    assert fresh_api.get_group_details(fp2) is not None


def test_purge_server_returns_discord_message_id(fresh_api):
    fp, _ = fresh_api.ingest_event(parse_event(_event_from(EXAMPLE_EVENT, 'build')))
    fresh_api.set_discord_message_id(fp, 'msg-build-99')
    _, msg_ids = fresh_api.purge_server('build')
    assert 'msg-build-99' in msg_ids


def test_purge_server_no_match_returns_zero(fresh_api):
    fresh_api.ingest_event(parse_event(_event_from(EXAMPLE_EVENT, 'play')))
    n, msg_ids = fresh_api.purge_server('build')
    assert n == 0
    assert msg_ids == []


def test_purge_server_cascades_occurrences(fresh_api):
    fp, _ = fresh_api.ingest_event(parse_event(_event_from(EXAMPLE_EVENT, 'build')))
    # Ingest a second occurrence to ensure there is more than one occurrence row
    fresh_api.ingest_event(parse_event(_event_from(EXAMPLE_EVENT, 'build')))
    fresh_api.purge_server('build')
    conn = fresh_api._conn  # pylint: disable=protected-access
    count = conn.execute(
        "SELECT COUNT(*) FROM occurrences WHERE group_id IN "
        "(SELECT id FROM error_groups WHERE fingerprint = ?)", (fp,)
    ).fetchone()[0]
    # The group is gone; occurrence rows should be cascade-deleted too
    assert count == 0


# ===========================================================================
# purge_by_status
# ===========================================================================

def test_purge_by_status_resolved_removes_resolved(fresh_api):
    fp, _ = fresh_api.ingest_event(parse_event(EXAMPLE_EVENT))
    fresh_api.resolve_group(fp)
    n, _ = fresh_api.purge_by_status('resolved')
    assert n == 1
    assert fresh_api.get_group_details(fp) is None


def test_purge_by_status_resolved_spares_active(fresh_api):
    fp_active, _ = fresh_api.ingest_event(parse_event(EXAMPLE_EVENT))
    fp_resolved, _ = fresh_api.ingest_event(parse_event(EXAMPLE_EVENT_2))
    fresh_api.resolve_group(fp_resolved)
    fresh_api.purge_by_status('resolved')
    assert fresh_api.get_group_details(fp_active) is not None


def test_purge_by_status_muted_removes_muted(fresh_api):
    fp, _ = fresh_api.ingest_event(parse_event(EXAMPLE_EVENT))
    fresh_api.mute_group(fp)
    n, _ = fresh_api.purge_by_status('muted')
    assert n == 1
    assert fresh_api.get_group_details(fp) is None


def test_purge_by_status_returns_discord_message_id(fresh_api):
    fp, _ = fresh_api.ingest_event(parse_event(EXAMPLE_EVENT))
    fresh_api.resolve_group(fp)
    fresh_api.set_discord_message_id(fp, 'msg-resolved-77')
    _, msg_ids = fresh_api.purge_by_status('resolved')
    assert 'msg-resolved-77' in msg_ids


def test_purge_by_status_no_match_returns_zero(fresh_api):
    fresh_api.ingest_event(parse_event(EXAMPLE_EVENT))
    n, msg_ids = fresh_api.purge_by_status('resolved')
    assert n == 0
    assert msg_ids == []


# ===========================================================================
# purge_older_than
# ===========================================================================

def test_purge_older_than_removes_stale_group(fresh_api):
    fp, _ = fresh_api.ingest_event(parse_event(EXAMPLE_EVENT))
    conn = fresh_api._conn  # pylint: disable=protected-access
    old_ts = int(time.time()) - 10 * 86400
    conn.execute("UPDATE error_groups SET last_seen = ?, first_seen = ?", (old_ts, old_ts))
    conn.execute("UPDATE occurrences SET timestamp = ?", (old_ts,))
    conn.execute("UPDATE server_hour_counts SET hour_bucket = ?", (old_ts,))
    conn.commit()

    n, _ = fresh_api.purge_older_than(days=7)
    assert n == 1
    assert fresh_api.get_group_details(fp) is None


def test_purge_older_than_spares_recent_group(fresh_api):
    fp, _ = fresh_api.ingest_event(parse_event(EXAMPLE_EVENT))
    n, _ = fresh_api.purge_older_than(days=7)
    assert n == 0
    assert fresh_api.get_group_details(fp) is not None


def test_purge_older_than_returns_discord_message_id(fresh_api):
    fp, _ = fresh_api.ingest_event(parse_event(EXAMPLE_EVENT))
    fresh_api.set_discord_message_id(fp, 'msg-old-55')
    conn = fresh_api._conn  # pylint: disable=protected-access
    old_ts = int(time.time()) - 10 * 86400
    conn.execute("UPDATE error_groups SET last_seen = ?, first_seen = ?", (old_ts, old_ts))
    conn.execute("UPDATE occurrences SET timestamp = ?", (old_ts,))
    conn.execute("UPDATE server_hour_counts SET hour_bucket = ?", (old_ts,))
    conn.commit()

    _, msg_ids = fresh_api.purge_older_than(days=7)
    assert 'msg-old-55' in msg_ids


def test_purge_older_than_cleans_up_orphan_occurrences(fresh_api):
    """Old occurrence rows in a still-active group are also pruned."""
    fp, _ = fresh_api.ingest_event(parse_event(EXAMPLE_EVENT))
    conn = fresh_api._conn  # pylint: disable=protected-access
    # Age the occurrence row but keep the group itself recent
    old_ts = int(time.time()) - 10 * 86400
    conn.execute("UPDATE occurrences SET timestamp = ?", (old_ts,))
    conn.execute("UPDATE server_hour_counts SET hour_bucket = ?", (old_ts,))
    conn.commit()

    fresh_api.purge_older_than(days=7)

    # Group still exists (last_seen was not backdated)
    assert fresh_api.get_group_details(fp) is not None
    # But the old occurrence row is gone
    occ_count = conn.execute("SELECT COUNT(*) FROM occurrences").fetchone()[0]
    assert occ_count == 0
