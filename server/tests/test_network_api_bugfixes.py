# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Byron Marohn
"""
Tests for the two pre-existing Discord-side bugs fixed as part of the network API work:

- _fmt_details_lines previously showed "Muted by"/"Resolved by" with no status guard,
  so an unmuted group with stale muted_by (or a resolved-then-muted group with stale
  resolved_by) displayed contradictory attribution alongside its real status.
- has_activity was never set by mute/unmute/resolve, and was cleared unconditionally by
  _refresh_loop even when the Discord edit failed, so a missed edit was never retried.
"""

import asyncio
import sys
import os
from datetime import datetime, timezone
from unittest.mock import AsyncMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest
import discord
from tracker.config import TrackerConfig
from tracker.api import FrameSummary, GroupDetails, Tracker
from tracker.ingest import parse_event
from bot import ExceptionBot, _MAX_EDIT_FAILURES, _fmt_details_lines
from tests.fixtures import EXAMPLE_EVENT


@pytest.fixture
def fresh_api():
    return Tracker(TrackerConfig(db_path=':memory:'))


@pytest.fixture
def bot(fresh_api):
    return ExceptionBot(fresh_api, channel_id=123, refresh_period=300)


def _get_has_activity(api: Tracker, fingerprint: str) -> int:
    row = api._conn.execute(  # pylint: disable=protected-access
        "SELECT has_activity FROM error_groups WHERE fingerprint = ?", (fingerprint,)
    ).fetchone()
    assert row is not None
    return row['has_activity']


def _make_details(status: str, muted_by=None, muted_at=None,
                   resolved_by=None, resolved_at=None) -> GroupDetails:
    now = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    frame = FrameSummary(class_name="com.example.Foo", method="bar", file="Foo.java", line=1)
    return GroupDetails(
        fingerprint="abcd1234" + "0" * 56,
        exception_class="java.lang.Exception",
        message_template="test error",
        status=status,
        first_seen=now,
        last_seen=now,
        total_count=1,
        logger="com.example.Foo",
        canonical_frames=[frame],
        canonical_trace=[frame],
        servers_affected=["srv-1"],
        server_counts_24h={"srv-1": 1},
        hourly_timeline=[],
        muted_by=muted_by,
        muted_at=muted_at,
        resolved_by=resolved_by,
        resolved_at=resolved_at,
    )


# ===========================================================================
# §5.1 Fix 1 — _fmt_details_lines status guard
# ===========================================================================

def test_details_lines_no_muted_line_when_active_with_stale_muted_by():
    """Pre-existing-row case: status='active' but muted_by wasn't cleared by an old
    unmute_group() call (the bug fixed in §5.1 Fix 2 for new mutations, but stale rows
    from before the fix still need the display-side guard)."""
    details = _make_details(
        status="active", muted_by="Alice",
        muted_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )
    lines = _fmt_details_lines(details)
    assert not any("Muted by" in line for line in lines)
    assert any("Status: active" in line for line in lines)


def test_details_lines_no_resolved_line_when_muted_with_stale_resolved_by():
    """resolve -> mute path: resolved_by is never cleared by mute_group, so a group
    that's now 'muted' must not still display 'Resolved by'."""
    details = _make_details(
        status="muted", resolved_by="Bob",
        resolved_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )
    lines = _fmt_details_lines(details)
    assert not any("Resolved by" in line for line in lines)


def test_details_lines_shows_muted_line_when_actually_muted():
    details = _make_details(
        status="muted", muted_by="Alice",
        muted_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )
    lines = _fmt_details_lines(details)
    assert any("Muted by Alice" in line for line in lines)


def test_details_lines_shows_resolved_line_when_actually_resolved():
    details = _make_details(
        status="resolved", resolved_by="Bob",
        resolved_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )
    lines = _fmt_details_lines(details)
    assert any("Resolved by Bob" in line for line in lines)


# ===========================================================================
# §5.2 — has_activity set on mutation, cleared only on a successful Discord edit
# ===========================================================================

def test_mute_makes_group_appear_in_active_discord_messages(fresh_api):
    fp, _ = fresh_api.ingest_event(parse_event(EXAMPLE_EVENT))
    fresh_api.set_discord_message_id(fp, "111111111111111111")
    fresh_api.clear_has_activity(fp)
    fresh_api.mute_group(fp, actor="Alice")
    pairs = fresh_api.get_active_discord_messages()
    assert (fp, "111111111111111111") in pairs


def test_edit_exception_message_clears_flag_on_success(fresh_api, bot):
    fp, _ = fresh_api.ingest_event(parse_event(EXAMPLE_EVENT))
    fresh_api.set_discord_message_id(fp, "111111111111111111")
    fresh_api.mute_group(fp, actor="Alice")
    assert _get_has_activity(fresh_api, fp) == 1

    fake_message = AsyncMock()
    fake_channel = AsyncMock()
    fake_channel.fetch_message = AsyncMock(return_value=fake_message)
    bot._get_channel = AsyncMock(return_value=fake_channel)  # pylint: disable=protected-access

    asyncio.run(bot.edit_exception_message(fp, "111111111111111111"))

    fake_message.edit.assert_awaited_once()
    assert _get_has_activity(fresh_api, fp) == 0


def test_edit_exception_message_survives_failed_edit(fresh_api, bot):
    """The retry Fix 1 schedules must actually be retryable: a failed edit must leave
    has_activity set so the next refresh tick tries again."""
    fp, _ = fresh_api.ingest_event(parse_event(EXAMPLE_EVENT))
    fresh_api.set_discord_message_id(fp, "111111111111111111")
    fresh_api.mute_group(fp, actor="Alice")
    assert _get_has_activity(fresh_api, fp) == 1

    fake_message = AsyncMock()
    fake_message.edit = AsyncMock(side_effect=discord.DiscordException("boom"))
    fake_channel = AsyncMock()
    fake_channel.fetch_message = AsyncMock(return_value=fake_message)
    bot._get_channel = AsyncMock(return_value=fake_channel)  # pylint: disable=protected-access

    asyncio.run(bot.edit_exception_message(fp, "111111111111111111"))

    assert _get_has_activity(fresh_api, fp) == 1


def test_edit_exception_message_not_found_does_not_touch_flag(fresh_api, bot):
    """discord.NotFound clears the tracked message ID and should NOT clear
    has_activity — the group is picked up by _backfill_missing_messages instead."""
    fp, _ = fresh_api.ingest_event(parse_event(EXAMPLE_EVENT))
    fresh_api.set_discord_message_id(fp, "111111111111111111")
    fresh_api.mute_group(fp, actor="Alice")
    assert _get_has_activity(fresh_api, fp) == 1

    fake_channel = AsyncMock()
    fake_channel.fetch_message = AsyncMock(
        side_effect=discord.NotFound.__new__(discord.NotFound)
    )
    bot._get_channel = AsyncMock(return_value=fake_channel)  # pylint: disable=protected-access

    asyncio.run(bot.edit_exception_message(fp, "111111111111111111"))

    assert _get_has_activity(fresh_api, fp) == 1
    assert fresh_api.get_discord_message_id(fp) is None


def test_edit_failures_stop_retrying_after_the_ceiling(fresh_api, bot):
    """Retrying a failed edit is the point of the flag, but a durable failure must not
    be retried on every tick forever. After _MAX_EDIT_FAILURES the flag is cleared so
    the loop stops; new activity on the group re-arms it."""
    fp, _ = fresh_api.ingest_event(parse_event(EXAMPLE_EVENT))
    fresh_api.set_discord_message_id(fp, "111111111111111111")
    fresh_api.mute_group(fp, actor="Alice")

    fake_message = AsyncMock()
    fake_message.edit = AsyncMock(side_effect=discord.DiscordException("boom"))
    fake_channel = AsyncMock()
    fake_channel.fetch_message = AsyncMock(return_value=fake_message)
    bot._get_channel = AsyncMock(return_value=fake_channel)  # pylint: disable=protected-access

    for attempt in range(1, _MAX_EDIT_FAILURES):
        asyncio.run(bot.edit_exception_message(fp, "111111111111111111"))
        assert _get_has_activity(fresh_api, fp) == 1, f"gave up after only {attempt} failure(s)"

    asyncio.run(bot.edit_exception_message(fp, "111111111111111111"))
    assert _get_has_activity(fresh_api, fp) == 0

    # New activity re-arms the retry from scratch.
    fresh_api.mute_group(fp, actor="Alice")
    assert _get_has_activity(fresh_api, fp) == 1


def test_edit_failure_counter_resets_after_a_success(fresh_api, bot):
    """The ceiling counts *consecutive* failures — an intermittent failure must not
    accumulate toward giving up."""
    fp, _ = fresh_api.ingest_event(parse_event(EXAMPLE_EVENT))
    fresh_api.set_discord_message_id(fp, "111111111111111111")
    fresh_api.mute_group(fp, actor="Alice")

    fake_message = AsyncMock()
    fake_channel = AsyncMock()
    fake_channel.fetch_message = AsyncMock(return_value=fake_message)
    bot._get_channel = AsyncMock(return_value=fake_channel)  # pylint: disable=protected-access

    fake_message.edit = AsyncMock(side_effect=discord.DiscordException("boom"))
    asyncio.run(bot.edit_exception_message(fp, "111111111111111111"))
    fake_message.edit = AsyncMock()
    asyncio.run(bot.edit_exception_message(fp, "111111111111111111"))
    assert bot._edit_failures == {}  # pylint: disable=protected-access


# ===========================================================================
# API-triggered fix requests get the same in-flight marker as the :wrench: path
# ===========================================================================

def test_add_fix_working_reaction_adds_the_configured_emoji(fresh_api, bot):
    fp, _ = fresh_api.ingest_event(parse_event(EXAMPLE_EVENT))
    fresh_api.set_discord_message_id(fp, "111111111111111111")

    fake_message = AsyncMock()
    fake_channel = AsyncMock()
    fake_channel.fetch_message = AsyncMock(return_value=fake_message)
    bot._get_channel = AsyncMock(return_value=fake_channel)  # pylint: disable=protected-access

    asyncio.run(bot.add_fix_working_reaction(fp))

    fake_message.add_reaction.assert_awaited_once_with(
        bot._reaction_fix_working  # pylint: disable=protected-access
    )


def test_add_fix_working_reaction_noop_without_tracked_message(fresh_api, bot):
    fp, _ = fresh_api.ingest_event(parse_event(EXAMPLE_EVENT))
    fake_channel = AsyncMock()
    bot._get_channel = AsyncMock(return_value=fake_channel)  # pylint: disable=protected-access

    asyncio.run(bot.add_fix_working_reaction(fp))

    fake_channel.fetch_message.assert_not_awaited()
