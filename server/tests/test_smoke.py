"""
Smoke tests: end-to-end ingest + query against an in-memory SQLite DB.
These cover the full pipeline and serve as a quick sanity check.
More focused unit tests live in test_fingerprint.py and test_ingest.py.
"""

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
    """Return a fresh Tracker backed by an in-memory DB for each test."""
    return Tracker(TrackerConfig(db_path=':memory:'))


def test_ingest_creates_group(fresh_api):
    event = parse_event(EXAMPLE_EVENT)
    fp, _ = fresh_api.ingest_event(event)
    assert len(fp) == 64  # SHA-256 hex


def test_same_exception_same_fingerprint(fresh_api):
    e1 = parse_event(EXAMPLE_EVENT)
    e2 = parse_event({**EXAMPLE_EVENT, 'server_id': 'survival-1'})
    fp1, _ = fresh_api.ingest_event(e1)
    fp2, _ = fresh_api.ingest_event(e2)
    assert fp1 == fp2


def test_different_exceptions_different_fingerprints(fresh_api):
    fp1, _ = fresh_api.ingest_event(parse_event(EXAMPLE_EVENT))
    fp2, _ = fresh_api.ingest_event(parse_event(EXAMPLE_EVENT_2))
    assert fp1 != fp2


def test_reoccurrence_increments_count(fresh_api):
    event = parse_event(EXAMPLE_EVENT)
    fp, _ = fresh_api.ingest_event(event)
    fresh_api.ingest_event(event)
    details = fresh_api.get_group_details(fp)
    assert details.total_count == 2


def test_reoccurrence_does_not_change_muted_status(fresh_api):
    event = parse_event(EXAMPLE_EVENT)
    fp, _ = fresh_api.ingest_event(event)
    fresh_api.mute_group(fp)
    fresh_api.ingest_event(event)
    details = fresh_api.get_group_details(fp)
    assert details.status == 'muted'
    assert details.total_count == 2


def test_resolved_group_stays_resolved_on_reoccurrence(fresh_api):
    event = parse_event(EXAMPLE_EVENT)
    fp, _ = fresh_api.ingest_event(event)
    fresh_api.resolve_group(fp)
    fresh_api.ingest_event(event)
    details = fresh_api.get_group_details(fp)
    assert details.status == 'resolved'    # count and last_seen still update
    assert details.total_count == 2


def test_get_top_active_groups(fresh_api):
    fresh_api.ingest_event(parse_event(EXAMPLE_EVENT))
    fresh_api.ingest_event(parse_event(EXAMPLE_EVENT))
    fresh_api.ingest_event(parse_event(EXAMPLE_EVENT_2))
    top = fresh_api.get_top_active_groups(limit=10, window_hours=24 * 365)
    assert len(top) == 2
    assert top[0].recent_count >= top[1].recent_count


def test_get_top_active_excludes_muted(fresh_api):
    event = parse_event(EXAMPLE_EVENT)
    fp, _ = fresh_api.ingest_event(event)
    fresh_api.mute_group(fp)
    top = fresh_api.get_top_active_groups(limit=10, window_hours=24 * 365)
    assert all(g.fingerprint != fp for g in top)


def test_get_group_details_fields(fresh_api):
    e1 = parse_event(EXAMPLE_EVENT)
    e2 = parse_event({**EXAMPLE_EVENT, 'server_id': 'survival-1'})
    fp, _ = fresh_api.ingest_event(e1)
    fresh_api.ingest_event(e2)
    d = fresh_api.get_group_details(fp)
    assert d.fingerprint == fp
    assert d.exception_class == 'java.lang.Exception'
    assert d.status == 'active'
    assert sorted(d.servers_affected) == ['survival-0', 'survival-1']
    assert len(d.canonical_frames) <= 3
    assert all(f.class_name.startswith('com.playmonumenta') for f in d.canonical_frames)
    assert len(d.canonical_trace) == len(EXAMPLE_EVENT['exception']['frames'])


def test_get_group_details_missing(fresh_api):
    assert fresh_api.get_group_details('deadbeef' * 8) is None


def test_mute_unmute(fresh_api):
    fp, _ = fresh_api.ingest_event(parse_event(EXAMPLE_EVENT))
    assert fresh_api.mute_group(fp) is True
    assert fresh_api.get_group_details(fp).status == 'muted'
    assert fresh_api.unmute_group(fp) is True
    assert fresh_api.get_group_details(fp).status == 'active'


def test_mute_returns_false_for_unknown(fresh_api):
    assert fresh_api.mute_group('00' * 32) is False


def test_resolve(fresh_api):
    fp, _ = fresh_api.ingest_event(parse_event(EXAMPLE_EVENT))
    assert fresh_api.resolve_group(fp) is True
    assert fresh_api.get_group_details(fp).status == 'resolved'


def test_server_counts(fresh_api):
    fresh_api.ingest_event(parse_event(EXAMPLE_EVENT))
    fresh_api.ingest_event(parse_event({**EXAMPLE_EVENT, 'server_id': 'survival-1'}))
    fresh_api.ingest_event(parse_event({**EXAMPLE_EVENT, 'server_id': 'survival-1'}))
    top = fresh_api.get_top_active_groups(limit=5, window_hours=24 * 365)
    counts = top[0].server_counts
    assert counts.get('survival-0') == 1
    assert counts.get('survival-1') == 2


def test_search_groups(fresh_api):
    fresh_api.ingest_event(parse_event(EXAMPLE_EVENT))
    fresh_api.ingest_event(parse_event(EXAMPLE_EVENT_2))
    results = fresh_api.search_groups('generictarget')
    assert len(results) == 1
    assert results[0].exception_class == 'java.lang.Exception'


def test_get_groups_for_server(fresh_api):
    fresh_api.ingest_event(parse_event(EXAMPLE_EVENT))
    fresh_api.ingest_event(parse_event(EXAMPLE_EVENT_2))
    results = fresh_api.get_groups_for_server('dungeon-0', window_hours=24 * 365)
    assert len(results) == 1
    assert results[0].exception_class == 'java.lang.NullPointerException'


def test_get_new_groups(fresh_api):
    fresh_api.ingest_event(parse_event(EXAMPLE_EVENT))
    fresh_api.ingest_event(parse_event(EXAMPLE_EVENT_2))
    # Both events have current timestamps — they appear as new groups
    results = fresh_api.get_new_groups(hours=24)
    assert len(results) == 2
    # A second ingest of the same event must not create a duplicate group
    fresh_api.ingest_event(parse_event(EXAMPLE_EVENT))
    results = fresh_api.get_new_groups(hours=24)
    assert len(results) == 2

def test_get_new_groups_excludes_old(fresh_api):
    """Groups whose first_seen is backdated beyond the window are excluded."""
    fresh_api.ingest_event(parse_event(EXAMPLE_EVENT))
    # Backdate first_seen to well outside any reasonable window
    fresh_api._conn.execute("UPDATE error_groups SET first_seen = 0")  # pylint: disable=protected-access
    fresh_api._conn.commit()  # pylint: disable=protected-access
    results = fresh_api.get_new_groups(hours=24)
    assert len(results) == 0


def test_run_expiry_removes_old_data(fresh_api):
    fresh_api.ingest_event(parse_event(EXAMPLE_EVENT))
    # Manually backdate last_seen to trigger expiry
    conn = fresh_api._conn  # pylint: disable=protected-access
    conn.execute("UPDATE error_groups SET last_seen = 0, first_seen = 0")
    conn.execute("UPDATE occurrences SET timestamp = 0")
    conn.execute("UPDATE server_hour_counts SET hour_bucket = 0")
    conn.commit()
    result = fresh_api.run_expiry()
    assert result['error_groups'] == 1
    assert result['occurrences'] == 1
    assert result['server_hour_counts'] == 1


def test_get_muted_groups(fresh_api):
    fp1, _ = fresh_api.ingest_event(parse_event(EXAMPLE_EVENT))
    fp2, _ = fresh_api.ingest_event(parse_event(EXAMPLE_EVENT_2))
    fresh_api.mute_group(fp1)
    muted = fresh_api.get_muted_groups()
    assert len(muted) == 1
    assert muted[0].fingerprint == fp1
    # fp2 is still active, should not appear
    assert all(g.fingerprint != fp2 for g in muted)


def test_get_resolved_groups(fresh_api):
    fp1, _ = fresh_api.ingest_event(parse_event(EXAMPLE_EVENT))
    fp2, _ = fresh_api.ingest_event(parse_event(EXAMPLE_EVENT_2))
    fresh_api.resolve_group(fp1)
    resolved = fresh_api.get_resolved_groups()
    assert len(resolved) == 1
    assert resolved[0].fingerprint == fp1
    assert all(g.fingerprint != fp2 for g in resolved)
