# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Byron Marohn
"""
Tests for Phase 4: Discord bot integration additions.

Covers:
- ingest_event returning (fingerprint, is_new) tuple
- mute_group / resolve_group attribution fields
- set_discord_message_id / get_all_discord_messages
- GroupDetails new fields
- run_expiry returning discord_message_ids
- get_fingerprint_by_short_id
- format_exception_message (message formatting)
"""

import sys
import os
from datetime import timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest
from tracker.config import TrackerConfig
from tracker.api import GroupDetails, Tracker
from tracker.ingest import parse_event
from bot import format_exception_message, _build_frames_block
from tests.fixtures import EXAMPLE_EVENT, EXAMPLE_EVENT_2


@pytest.fixture
def fresh_api():
    return Tracker(TrackerConfig(db_path=':memory:'))


# ===========================================================================
# ingest_event return value
# ===========================================================================

def test_ingest_new_group_returns_true(fresh_api):
    fp, is_new = fresh_api.ingest_event(parse_event(EXAMPLE_EVENT))
    assert len(fp) == 64
    assert is_new is True


def test_ingest_repeat_returns_false(fresh_api):
    fp1, _ = fresh_api.ingest_event(parse_event(EXAMPLE_EVENT))
    fp2, is_new = fresh_api.ingest_event(parse_event(EXAMPLE_EVENT))
    assert fp1 == fp2
    assert is_new is False


def test_ingest_different_group_each_new(fresh_api):
    _, new1 = fresh_api.ingest_event(parse_event(EXAMPLE_EVENT))
    _, new2 = fresh_api.ingest_event(parse_event(EXAMPLE_EVENT_2))
    assert new1 is True
    assert new2 is True


# ===========================================================================
# mute_group / resolve_group attribution
# ===========================================================================

def test_mute_group_writes_actor(fresh_api):
    fp, _ = fresh_api.ingest_event(parse_event(EXAMPLE_EVENT))
    fresh_api.mute_group(fp, actor="TestUser")
    details = fresh_api.get_group_details(fp)
    assert details.muted_by == "TestUser"
    assert details.muted_at is not None
    assert details.muted_at.tzinfo == timezone.utc


def test_mute_group_default_actor(fresh_api):
    fp, _ = fresh_api.ingest_event(parse_event(EXAMPLE_EVENT))
    fresh_api.mute_group(fp)
    details = fresh_api.get_group_details(fp)
    assert details.muted_by == "unknown"
    assert details.muted_at is not None


def test_resolve_group_writes_actor(fresh_api):
    fp, _ = fresh_api.ingest_event(parse_event(EXAMPLE_EVENT))
    fresh_api.resolve_group(fp, actor="DevOps")
    details = fresh_api.get_group_details(fp)
    assert details.resolved_by == "DevOps"
    assert details.resolved_at is not None
    assert details.resolved_at.tzinfo == timezone.utc


def test_resolve_group_default_actor(fresh_api):
    fp, _ = fresh_api.ingest_event(parse_event(EXAMPLE_EVENT))
    fresh_api.resolve_group(fp)
    details = fresh_api.get_group_details(fp)
    assert details.resolved_by == "unknown"
    assert details.resolved_at is not None


def test_fresh_group_has_no_attribution(fresh_api):
    fp, _ = fresh_api.ingest_event(parse_event(EXAMPLE_EVENT))
    details = fresh_api.get_group_details(fp)
    assert details.muted_by is None
    assert details.muted_at is None
    assert details.resolved_by is None
    assert details.resolved_at is None


# ===========================================================================
# set_discord_message_id / get_all_discord_messages
# ===========================================================================

def test_set_discord_message_id(fresh_api):
    fp, _ = fresh_api.ingest_event(parse_event(EXAMPLE_EVENT))
    fresh_api.set_discord_message_id(fp, "123456789")
    pairs = fresh_api.get_all_discord_messages()
    assert (fp, "123456789") in pairs


def test_get_all_discord_messages_excludes_null(fresh_api):
    fp1, _ = fresh_api.ingest_event(parse_event(EXAMPLE_EVENT))
    fp2, _ = fresh_api.ingest_event(parse_event(EXAMPLE_EVENT_2))
    fresh_api.set_discord_message_id(fp1, "111")
    # fp2 has no message ID
    pairs = fresh_api.get_all_discord_messages()
    fps = [p[0] for p in pairs]
    assert fp1 in fps
    assert fp2 not in fps


def test_set_discord_message_id_none_clears(fresh_api):
    fp, _ = fresh_api.ingest_event(parse_event(EXAMPLE_EVENT))
    fresh_api.set_discord_message_id(fp, "999")
    fresh_api.set_discord_message_id(fp, None)
    pairs = fresh_api.get_all_discord_messages()
    assert all(p[0] != fp for p in pairs)


def test_get_all_discord_messages_multiple(fresh_api):
    fp1, _ = fresh_api.ingest_event(parse_event(EXAMPLE_EVENT))
    fp2, _ = fresh_api.ingest_event(parse_event(EXAMPLE_EVENT_2))
    fresh_api.set_discord_message_id(fp1, "aaa")
    fresh_api.set_discord_message_id(fp2, "bbb")
    pairs = fresh_api.get_all_discord_messages()
    assert len(pairs) == 2
    fps = {p[0] for p in pairs}
    assert fp1 in fps
    assert fp2 in fps


# ===========================================================================
# run_expiry returns discord_message_ids
# ===========================================================================

def test_run_expiry_returns_discord_message_ids_key(fresh_api):
    fresh_api.ingest_event(parse_event(EXAMPLE_EVENT))
    result = fresh_api.run_expiry()
    assert 'discord_message_ids' in result
    assert isinstance(result['discord_message_ids'], list)


def test_run_expiry_includes_ids_for_expired_groups(fresh_api):
    fp, _ = fresh_api.ingest_event(parse_event(EXAMPLE_EVENT))
    fresh_api.set_discord_message_id(fp, "msg-42")
    conn = fresh_api._conn  # pylint: disable=protected-access
    conn.execute("UPDATE error_groups SET last_seen = 0")
    conn.execute("UPDATE occurrences SET timestamp = 0")
    conn.execute("UPDATE server_hour_counts SET hour_bucket = 0")
    conn.commit()
    result = fresh_api.run_expiry()
    assert "msg-42" in result['discord_message_ids']


def test_run_expiry_discord_ids_empty_when_none_tracked(fresh_api):
    fresh_api.ingest_event(parse_event(EXAMPLE_EVENT))
    conn = fresh_api._conn  # pylint: disable=protected-access
    conn.execute("UPDATE error_groups SET last_seen = 0")
    conn.execute("UPDATE occurrences SET timestamp = 0")
    conn.execute("UPDATE server_hour_counts SET hour_bucket = 0")
    conn.commit()
    result = fresh_api.run_expiry()
    assert result['discord_message_ids'] == []


def test_run_expiry_only_includes_ids_for_expired_groups(fresh_api):
    # One group expires, one does not
    fp1, _ = fresh_api.ingest_event(parse_event(EXAMPLE_EVENT))
    fp2, _ = fresh_api.ingest_event(parse_event(EXAMPLE_EVENT_2))
    fresh_api.set_discord_message_id(fp1, "expire-id")
    fresh_api.set_discord_message_id(fp2, "keep-id")
    conn = fresh_api._conn  # pylint: disable=protected-access
    conn.execute("UPDATE error_groups SET last_seen = 0 WHERE fingerprint = ?", (fp1,))
    conn.execute("UPDATE occurrences SET timestamp = 0 WHERE group_id = "
                 "(SELECT id FROM error_groups WHERE fingerprint = ?)", (fp1,))
    conn.execute("UPDATE server_hour_counts SET hour_bucket = 0 WHERE group_id = "
                 "(SELECT id FROM error_groups WHERE fingerprint = ?)", (fp1,))
    conn.commit()
    result = fresh_api.run_expiry()
    assert "expire-id" in result['discord_message_ids']
    assert "keep-id" not in result['discord_message_ids']


# ===========================================================================
# get_fingerprint_by_discord_message_id
# ===========================================================================

def test_get_fingerprint_by_discord_message_id_found(fresh_api):
    fp, _ = fresh_api.ingest_event(parse_event(EXAMPLE_EVENT))
    fresh_api.set_discord_message_id(fp, "123456789012345678")
    result = fresh_api.get_fingerprint_by_discord_message_id("123456789012345678")
    assert result == fp


def test_get_fingerprint_by_discord_message_id_not_found(fresh_api):
    result = fresh_api.get_fingerprint_by_discord_message_id("999999999999999999")
    assert result is None


def test_get_fingerprint_by_discord_message_id_cleared(fresh_api):
    fp, _ = fresh_api.ingest_event(parse_event(EXAMPLE_EVENT))
    fresh_api.set_discord_message_id(fp, "555")
    fresh_api.set_discord_message_id(fp, None)
    result = fresh_api.get_fingerprint_by_discord_message_id("555")
    assert result is None


# ===========================================================================
# get_fingerprint_by_short_id
# ===========================================================================

def test_get_fingerprint_by_short_id_found(fresh_api):
    fp, _ = fresh_api.ingest_event(parse_event(EXAMPLE_EVENT))
    short = fp[:8]
    result = fresh_api.get_fingerprint_by_short_id(short)
    assert result == fp


def test_get_fingerprint_by_short_id_not_found(fresh_api):
    result = fresh_api.get_fingerprint_by_short_id("00000000")
    assert result is None


# ===========================================================================
# format_exception_message
# ===========================================================================

def _make_details(status="active", muted_by=None, muted_at=None,
                  resolved_by=None, resolved_at=None, trace_count=3) -> GroupDetails:
    from datetime import datetime
    from tracker.api import FrameSummary
    now = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    frames = [
        FrameSummary(class_name="com.example.Foo", method="bar", file="Foo.java", line=i)
        for i in range(trace_count)
    ]
    return GroupDetails(
        fingerprint="abcd1234" + "0" * 56,
        exception_class="java.lang.Exception",
        message_template="test error",
        status=status,
        first_seen=now,
        last_seen=now,
        total_count=42,
        logger="com.example.Foo",
        canonical_frames=frames[:3],
        canonical_trace=frames,
        servers_affected=["srv-1", "srv-2"],
        server_counts_24h={"srv-1": 10, "srv-2": 5},
        hourly_timeline=[],
        muted_by=muted_by,
        muted_at=muted_at,
        resolved_by=resolved_by,
        resolved_at=resolved_at,
    )


def test_format_message_contains_fingerprint():
    details = _make_details()
    msg = format_exception_message(details)
    assert "abcd1234" in msg


def test_format_message_within_limit():
    details = _make_details(trace_count=50)
    msg = format_exception_message(details)
    assert len(msg) <= 2000


def test_format_message_active_no_wrapping():
    details = _make_details(status="active")
    msg = format_exception_message(details)
    assert "||" not in msg
    assert "~~" not in msg


def test_format_message_muted_has_spoiler():
    from datetime import datetime
    muted_at = datetime(2024, 1, 2, 0, 0, tzinfo=timezone.utc)
    details = _make_details(status="muted", muted_by="Alice", muted_at=muted_at)
    msg = format_exception_message(details)
    assert "||" in msg
    assert "Alice" in msg
    assert "Muted on:" in msg


def test_format_message_resolved_has_strikethrough():
    from datetime import datetime
    resolved_at = datetime(2024, 1, 3, 0, 0, tzinfo=timezone.utc)
    details = _make_details(status="resolved", resolved_by="Bob", resolved_at=resolved_at)
    msg = format_exception_message(details)
    assert "~~" in msg
    assert "Bob" in msg
    assert "Resolved on:" in msg


def test_format_message_truncation_stays_under_limit():
    # 200 frames — must truncate to stay under 2000
    details = _make_details(trace_count=200)
    msg = format_exception_message(details)
    assert len(msg) <= 2000
    assert "more frames" in msg


def test_format_message_servers_listed():
    details = _make_details()
    msg = format_exception_message(details)
    assert "srv-1" in msg
    assert "srv-2" in msg


def test_format_message_count_present():
    details = _make_details()
    msg = format_exception_message(details)
    assert "Count: 42" in msg


# ===========================================================================
# _build_frames_block
# ===========================================================================

def test_build_frames_block_fits_exactly():
    lines = ["  at Foo.bar(Foo.java:1)", "  at Baz.qux(Baz.java:2)"]
    result = _build_frames_block(lines, 1000)
    assert result == "\n".join(lines)


def test_build_frames_block_truncates():
    lines = ["x" * 100 for _ in range(20)]
    result = _build_frames_block(lines, 200)
    assert len(result) <= 200
    assert "more frames" in result


def test_build_frames_block_empty():
    assert _build_frames_block([], 1000) == ""


# ===========================================================================
# has_activity flag
# ===========================================================================

def _get_has_activity(api: Tracker, fingerprint: str) -> int:
    row = api._conn.execute(  # pylint: disable=protected-access
        "SELECT has_activity FROM error_groups WHERE fingerprint = ?", (fingerprint,)
    ).fetchone()
    assert row is not None
    return row['has_activity']


def test_new_group_has_activity_false(fresh_api):
    fp, _ = fresh_api.ingest_event(parse_event(EXAMPLE_EVENT))
    assert _get_has_activity(fresh_api, fp) == 0


def test_repeat_ingest_sets_has_activity(fresh_api):
    fp, _ = fresh_api.ingest_event(parse_event(EXAMPLE_EVENT))
    fresh_api.ingest_event(parse_event(EXAMPLE_EVENT))
    assert _get_has_activity(fresh_api, fp) == 1


def test_get_active_discord_messages_requires_activity_flag(fresh_api):
    fp, _ = fresh_api.ingest_event(parse_event(EXAMPLE_EVENT))
    fresh_api.set_discord_message_id(fp, "msg-1")
    # No repeat ingest yet — has_activity is still 0
    assert fresh_api.get_active_discord_messages() == []


def test_get_active_discord_messages_returns_after_repeat_ingest(fresh_api):
    fp, _ = fresh_api.ingest_event(parse_event(EXAMPLE_EVENT))
    fresh_api.set_discord_message_id(fp, "msg-1")
    fresh_api.ingest_event(parse_event(EXAMPLE_EVENT))  # sets has_activity=1
    pairs = fresh_api.get_active_discord_messages()
    assert (fp, "msg-1") in pairs


def test_get_active_discord_messages_requires_message_id(fresh_api):
    fresh_api.ingest_event(parse_event(EXAMPLE_EVENT))
    # has_activity=1 but no discord_message_id
    fresh_api.ingest_event(parse_event(EXAMPLE_EVENT))
    assert fresh_api.get_active_discord_messages() == []


def test_clear_has_activity_resets_flag(fresh_api):
    fp, _ = fresh_api.ingest_event(parse_event(EXAMPLE_EVENT))
    fresh_api.ingest_event(parse_event(EXAMPLE_EVENT))
    assert _get_has_activity(fresh_api, fp) == 1
    fresh_api.clear_has_activity(fp)
    assert _get_has_activity(fresh_api, fp) == 0


def test_active_messages_filtered_by_flag(fresh_api):
    fp1, _ = fresh_api.ingest_event(parse_event(EXAMPLE_EVENT))
    fp2, _ = fresh_api.ingest_event(parse_event(EXAMPLE_EVENT_2))
    fresh_api.set_discord_message_id(fp1, "aaa")
    fresh_api.set_discord_message_id(fp2, "bbb")
    # Only fp2 gets a repeat ingest
    fresh_api.ingest_event(parse_event(EXAMPLE_EVENT_2))
    pairs = fresh_api.get_active_discord_messages()
    fps = [p[0] for p in pairs]
    assert fp2 in fps
    assert fp1 not in fps
