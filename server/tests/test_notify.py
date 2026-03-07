# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Byron Marohn
"""
Tests for the notify subscription feature.

Covers:
- Tracker.add_notify_subscription / list / remove / get_all
- Tracker.get_active_fingerprints
- Subscription cap (100 per user)
- AUTOINCREMENT id monotonicity (never reused after deletion)
- _matches_notify: regex matching against exception class, message, and trace
- format_notify_dm: header format, multi-rule listing, length constraint
"""

import sys
import os
from datetime import timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest
from tracker.config import TrackerConfig
from tracker.api import Tracker
from tracker.ingest import parse_event
from bot import _matches_notify, format_notify_dm
from tests.fixtures import EXAMPLE_EVENT, EXAMPLE_EVENT_2


@pytest.fixture
def fresh_api():
    return Tracker(TrackerConfig(db_path=':memory:'))


# ===========================================================================
# add_notify_subscription
# ===========================================================================

def test_add_subscription_returns_int(fresh_api):
    sub_id = fresh_api.add_notify_subscription("user1", "NullPointer")
    assert isinstance(sub_id, int)
    assert sub_id > 0


def test_subscription_ids_increment(fresh_api):
    id1 = fresh_api.add_notify_subscription("user1", "foo")
    id2 = fresh_api.add_notify_subscription("user1", "bar")
    assert id2 > id1


def test_subscription_ids_never_reused(fresh_api):
    id1 = fresh_api.add_notify_subscription("user1", "foo")
    fresh_api.remove_notify_subscription("user1", id1)
    id2 = fresh_api.add_notify_subscription("user1", "bar")
    assert id2 > id1


# ===========================================================================
# list_notify_subscriptions
# ===========================================================================

def test_list_subscriptions_empty(fresh_api):
    assert fresh_api.list_notify_subscriptions("user1") == []


def test_list_subscriptions_returns_added(fresh_api):
    sub_id = fresh_api.add_notify_subscription("user1", "NullPointer")
    subs = fresh_api.list_notify_subscriptions("user1")
    assert len(subs) == 1
    assert subs[0][0] == sub_id
    assert subs[0][1] == "NullPointer"
    assert subs[0][2].tzinfo == timezone.utc


def test_list_subscriptions_ordered_by_id(fresh_api):
    id1 = fresh_api.add_notify_subscription("user1", "aaa")
    id2 = fresh_api.add_notify_subscription("user1", "bbb")
    id3 = fresh_api.add_notify_subscription("user1", "ccc")
    subs = fresh_api.list_notify_subscriptions("user1")
    ids = [s[0] for s in subs]
    assert ids == [id1, id2, id3]


def test_list_subscriptions_per_user_isolation(fresh_api):
    fresh_api.add_notify_subscription("user1", "PatternA")
    fresh_api.add_notify_subscription("user2", "PatternB")
    user1_subs = fresh_api.list_notify_subscriptions("user1")
    user2_subs = fresh_api.list_notify_subscriptions("user2")
    assert len(user1_subs) == 1
    assert user1_subs[0][1] == "PatternA"
    assert len(user2_subs) == 1
    assert user2_subs[0][1] == "PatternB"


# ===========================================================================
# remove_notify_subscription
# ===========================================================================

def test_remove_subscription_success(fresh_api):
    sub_id = fresh_api.add_notify_subscription("user1", "foo")
    ok = fresh_api.remove_notify_subscription("user1", sub_id)
    assert ok is True
    assert fresh_api.list_notify_subscriptions("user1") == []


def test_remove_subscription_not_found(fresh_api):
    ok = fresh_api.remove_notify_subscription("user1", 9999)
    assert ok is False


def test_remove_subscription_wrong_user(fresh_api):
    sub_id = fresh_api.add_notify_subscription("user1", "foo")
    ok = fresh_api.remove_notify_subscription("user2", sub_id)
    assert ok is False
    # Original still present
    assert len(fresh_api.list_notify_subscriptions("user1")) == 1


# ===========================================================================
# subscription cap
# ===========================================================================

def test_subscription_limit_100th_succeeds(fresh_api):
    for i in range(99):
        fresh_api.add_notify_subscription("user1", f"pattern{i}")
    # 100th should succeed
    sub_id = fresh_api.add_notify_subscription("user1", "pattern99")
    assert sub_id > 0


def test_subscription_limit_101st_raises(fresh_api):
    for i in range(100):
        fresh_api.add_notify_subscription("user1", f"pattern{i}")
    with pytest.raises(ValueError, match="100"):
        fresh_api.add_notify_subscription("user1", "one_too_many")


def test_subscription_limit_per_user_independent(fresh_api):
    for i in range(100):
        fresh_api.add_notify_subscription("user1", f"pattern{i}")
    # user2 should still be able to add
    sub_id = fresh_api.add_notify_subscription("user2", "new_pattern")
    assert sub_id > 0


# ===========================================================================
# get_all_notify_subscriptions
# ===========================================================================

def test_get_all_subscriptions_empty(fresh_api):
    assert fresh_api.get_all_notify_subscriptions() == []


def test_get_all_subscriptions_returns_all_users(fresh_api):
    id1 = fresh_api.add_notify_subscription("user1", "foo")
    id2 = fresh_api.add_notify_subscription("user2", "bar")
    all_subs = fresh_api.get_all_notify_subscriptions()
    assert len(all_subs) == 2
    ids = {s[0] for s in all_subs}
    assert id1 in ids
    assert id2 in ids


def test_get_all_subscriptions_tuple_shape(fresh_api):
    sub_id = fresh_api.add_notify_subscription("user1", "test")
    all_subs = fresh_api.get_all_notify_subscriptions()
    assert len(all_subs) == 1
    s_id, s_user, s_pattern = all_subs[0]
    assert s_id == sub_id
    assert s_user == "user1"
    assert s_pattern == "test"


# ===========================================================================
# get_active_fingerprints
# ===========================================================================

def test_get_active_fingerprints_empty(fresh_api):
    assert fresh_api.get_active_fingerprints() == []


def test_get_active_fingerprints_returns_active(fresh_api):
    fp, _ = fresh_api.ingest_event(parse_event(EXAMPLE_EVENT))
    fps = fresh_api.get_active_fingerprints()
    assert fp in fps


def test_get_active_fingerprints_excludes_muted(fresh_api):
    fp, _ = fresh_api.ingest_event(parse_event(EXAMPLE_EVENT))
    fresh_api.mute_group(fp)
    assert fresh_api.get_active_fingerprints() == []


def test_get_active_fingerprints_excludes_resolved(fresh_api):
    fp, _ = fresh_api.ingest_event(parse_event(EXAMPLE_EVENT))
    fresh_api.resolve_group(fp)
    assert fresh_api.get_active_fingerprints() == []


def test_get_active_fingerprints_multiple(fresh_api):
    fp1, _ = fresh_api.ingest_event(parse_event(EXAMPLE_EVENT))
    fp2, _ = fresh_api.ingest_event(parse_event(EXAMPLE_EVENT_2))
    fps = fresh_api.get_active_fingerprints()
    assert fp1 in fps
    assert fp2 in fps


# ===========================================================================
# _matches_notify
# ===========================================================================

def _make_details_for_match():
    """Build a minimal GroupDetails sufficient for _matches_notify tests."""
    from datetime import datetime
    from tracker.api import FrameSummary, GroupDetails
    now = datetime(2024, 1, 1, tzinfo=timezone.utc)
    frames = [
        FrameSummary(
            class_name="com.playmonumenta.plugins.bosses.BossManager",
            method="processEntity",
            file="BossManager.java",
            line=100,
        ),
        FrameSummary(
            class_name="com.playmonumenta.plugins.items.ItemHandler",
            method="onInteract",
            file="ItemHandler.java",
            line=42,
        ),
    ]
    return GroupDetails(
        fingerprint="abcd1234" + "0" * 56,
        exception_class="java.lang.NullPointerException",
        message_template="Cannot invoke method on null object",
        status="active",
        first_seen=now,
        last_seen=now,
        total_count=5,
        logger="com.playmonumenta.plugins.bosses.BossManager",
        canonical_frames=frames[:1],
        canonical_trace=frames,
        servers_affected=["srv-1"],
        server_counts_24h={"srv-1": 5},
        hourly_timeline=[],
    )


def test_matches_notify_exception_class():
    details = _make_details_for_match()
    assert _matches_notify("NullPointerException", details) is True


def test_matches_notify_message_template():
    details = _make_details_for_match()
    assert _matches_notify("null object", details) is True


def test_matches_notify_trace_classname():
    details = _make_details_for_match()
    assert _matches_notify("BossManager", details) is True


def test_matches_notify_trace_file():
    details = _make_details_for_match()
    assert _matches_notify("ItemHandler.java", details) is True


def test_matches_notify_no_match():
    details = _make_details_for_match()
    assert _matches_notify("ParticleManager", details) is False


def test_matches_notify_case_sensitive():
    details = _make_details_for_match()
    # Exact case matches
    assert _matches_notify("NullPointerException", details) is True
    assert _matches_notify("BossManager", details) is True
    # Wrong case does not match
    assert _matches_notify("nullpointerexception", details) is False
    assert _matches_notify("bossmanager", details) is False


def test_matches_notify_regex_pattern():
    details = _make_details_for_match()
    # Regex anchors and alternation
    assert _matches_notify(r"Null|IllegalArgument", details) is True
    assert _matches_notify(r"^java\.lang\.", details) is True
    assert _matches_notify(r"^org\.spigot", details) is False


# ===========================================================================
# format_notify_dm
# ===========================================================================

def _make_details_for_dm():
    from datetime import datetime
    from tracker.api import FrameSummary, GroupDetails
    now = datetime(2024, 1, 1, tzinfo=timezone.utc)
    frames = [
        FrameSummary(class_name="com.example.Foo", method="bar", file="Foo.java", line=i)
        for i in range(3)
    ]
    return GroupDetails(
        fingerprint="abcd1234" + "0" * 56,
        exception_class="java.lang.Exception",
        message_template="test error",
        status="active",
        first_seen=now,
        last_seen=now,
        total_count=1,
        logger="com.example.Foo",
        canonical_frames=frames,
        canonical_trace=frames,
        servers_affected=["srv-1"],
        server_counts_24h={"srv-1": 1},
        hourly_timeline=[],
    )


def test_format_notify_dm_header_single_rule():
    msg = format_notify_dm(_make_details_for_dm(), [(7, "NullPointer")])
    assert msg.startswith("Matched notify rule(s): #7 (`NullPointer`)\n")


def test_format_notify_dm_header_multiple_rules():
    msg = format_notify_dm(_make_details_for_dm(), [(3, "foo"), (12, "bar")])
    assert "#3 (`foo`)" in msg
    assert "#12 (`bar`)" in msg


def test_format_notify_dm_contains_fingerprint():
    msg = format_notify_dm(_make_details_for_dm(), [(1, "test")])
    assert "abcd1234" in msg


def test_format_notify_dm_within_limit():
    msg = format_notify_dm(_make_details_for_dm(), [(1, "test")])
    assert len(msg) <= 2000


def test_format_notify_dm_within_limit_long_header():
    """A very long rule list must still keep the total message under 2000 chars."""
    from datetime import datetime
    from tracker.api import FrameSummary, GroupDetails
    now = datetime(2024, 1, 1, tzinfo=timezone.utc)
    # Many long frame lines to stress the truncation path.
    frames = [
        FrameSummary(
            class_name="com.example.VeryLongClassNameThatTakesUpLotsOfSpace",
            method="veryLongMethodNameThatAlsoConsumesSpace",
            file="VeryLongFileName.java",
            line=i,
        )
        for i in range(50)
    ]
    details = GroupDetails(
        fingerprint="abcd1234" + "0" * 56,
        exception_class="java.lang.Exception",
        message_template="error",
        status="active",
        first_seen=now,
        last_seen=now,
        total_count=1,
        logger="com.example.Foo",
        canonical_frames=frames[:3],
        canonical_trace=frames,
        servers_affected=["srv-1"],
        server_counts_24h={},
        hourly_timeline=[],
    )
    # Build a large set of matching rules to produce a long header.
    rules = [(i, f"pattern_{i}_with_extra_text") for i in range(20)]
    msg = format_notify_dm(details, rules)
    assert len(msg) <= 2000
