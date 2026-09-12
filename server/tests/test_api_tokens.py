# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Byron Marohn
"""
Tests for the network API bearer token feature (see NETWORK_API.md's "Attribution"
section): Tracker.create_api_token / verify_api_token / revoke_api_tokens, expiry
sweeping via run_expiry, and the bot's pure `_resolve_token_ttl` clamping helper.
"""

import sys
import os
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest
from tracker.config import TrackerConfig
from tracker.api import Tracker
from bot import _format_api_token_created_message, _resolve_token_ttl


@pytest.fixture
def fresh_api():
    return Tracker(TrackerConfig(db_path=':memory:'))


# ===========================================================================
# create_api_token / verify_api_token
# ===========================================================================

def test_create_returns_token_and_expiry(fresh_api):
    token, expires_at = fresh_api.create_api_token("111", ttl_hours=24)
    assert isinstance(token, str) and len(token) > 20
    assert isinstance(expires_at, int)


def test_create_expiry_matches_ttl(fresh_api):
    before = int(time.time())
    _token, expires_at = fresh_api.create_api_token("111", ttl_hours=1)
    assert before + 3600 <= expires_at <= before + 3600 + 5  # small margin for test runtime


def test_verify_returns_the_bound_discord_id(fresh_api):
    token, _ = fresh_api.create_api_token("111222333", ttl_hours=24)
    assert fresh_api.verify_api_token(token) == "111222333"


def test_verify_unknown_token_returns_none(fresh_api):
    assert fresh_api.verify_api_token("never-minted") is None


def test_verify_expired_token_returns_none(fresh_api):
    token, _ = fresh_api.create_api_token("111", ttl_hours=-1)
    assert fresh_api.verify_api_token(token) is None


def test_two_tokens_for_the_same_user_are_distinct_and_both_valid(fresh_api):
    token1, _ = fresh_api.create_api_token("111", ttl_hours=24)
    token2, _ = fresh_api.create_api_token("111", ttl_hours=24)
    assert token1 != token2
    assert fresh_api.verify_api_token(token1) == "111"
    assert fresh_api.verify_api_token(token2) == "111"


def test_only_the_hash_is_recoverable_not_the_raw_token(fresh_api):
    """Sanity check on the storage contract: the raw token must not appear verbatim
    anywhere a plain substring search of the underlying connection would find it."""
    token, _ = fresh_api.create_api_token("111", ttl_hours=24)
    # pylint: disable=protected-access
    row = fresh_api._conn.execute("SELECT token_hash FROM api_tokens").fetchone()
    assert row["token_hash"] != token


# ===========================================================================
# revoke_api_tokens
# ===========================================================================

def test_revoke_invalidates_all_of_a_users_tokens(fresh_api):
    token1, _ = fresh_api.create_api_token("111", ttl_hours=24)
    token2, _ = fresh_api.create_api_token("111", ttl_hours=24)
    count = fresh_api.revoke_api_tokens("111")
    assert count == 2
    assert fresh_api.verify_api_token(token1) is None
    assert fresh_api.verify_api_token(token2) is None


def test_revoke_does_not_touch_another_users_tokens(fresh_api):
    mine, _ = fresh_api.create_api_token("111", ttl_hours=24)
    theirs, _ = fresh_api.create_api_token("222", ttl_hours=24)
    fresh_api.revoke_api_tokens("111")
    assert fresh_api.verify_api_token(mine) is None
    assert fresh_api.verify_api_token(theirs) == "222"


def test_revoke_with_no_tokens_returns_zero(fresh_api):
    assert fresh_api.revoke_api_tokens("no-such-user") == 0


# ===========================================================================
# Per-user token cap
# ===========================================================================

def test_create_raises_once_the_cap_is_reached(fresh_api):
    for _ in range(20):
        fresh_api.create_api_token("111", ttl_hours=24)
    with pytest.raises(ValueError):
        fresh_api.create_api_token("111", ttl_hours=24)


def test_cap_is_per_user(fresh_api):
    for _ in range(20):
        fresh_api.create_api_token("111", ttl_hours=24)
    # A different user is unaffected by "111" being at the cap.
    token, _ = fresh_api.create_api_token("222", ttl_hours=24)
    assert fresh_api.verify_api_token(token) == "222"


def test_revoking_frees_up_the_cap(fresh_api):
    for _ in range(20):
        fresh_api.create_api_token("111", ttl_hours=24)
    fresh_api.revoke_api_tokens("111")
    token, _ = fresh_api.create_api_token("111", ttl_hours=24)
    assert fresh_api.verify_api_token(token) == "111"


# ===========================================================================
# Expiry sweeping
# ===========================================================================

def test_run_expiry_sweeps_expired_tokens(fresh_api):
    fresh_api.create_api_token("111", ttl_hours=-1)
    fresh_api.create_api_token("222", ttl_hours=24)
    result = fresh_api.run_expiry()
    assert result["api_tokens"] == 1
    # pylint: disable=protected-access
    remaining = fresh_api._conn.execute("SELECT discord_id FROM api_tokens").fetchall()
    assert [r["discord_id"] for r in remaining] == ["222"]


# ===========================================================================
# bot._resolve_token_ttl (pure clamping helper)
# ===========================================================================

def test_resolve_token_ttl_uses_default_when_not_requested():
    assert _resolve_token_ttl(None, default=24, maximum=168) == 24


def test_resolve_token_ttl_honors_a_request_within_range():
    assert _resolve_token_ttl(48, default=24, maximum=168) == 48


def test_resolve_token_ttl_clamps_above_the_maximum():
    assert _resolve_token_ttl(9999, default=24, maximum=168) == 168


def test_resolve_token_ttl_clamps_below_one():
    assert _resolve_token_ttl(0, default=24, maximum=168) == 1
    assert _resolve_token_ttl(-5, default=24, maximum=168) == 1


def test_resolve_token_ttl_never_exceeds_a_misconfigured_sub_one_maximum():
    """A nonsense API_TOKEN_MAX_TTL_HOURS=0 must not grant a longer-than-configured
    token by falling through to the >=1 floor."""
    assert _resolve_token_ttl(None, default=24, maximum=0) == 1
    assert _resolve_token_ttl(100, default=24, maximum=0) == 1


# ===========================================================================
# bot._format_api_token_created_message (pure message formatting)
# ===========================================================================

def test_format_message_includes_the_real_token_once_in_a_code_span_inside_the_spoiler():
    msg = _format_api_token_created_message("abcDEF123-_xyz", 1700000000, "")
    assert "||`abcDEF123-_xyz`||" in msg
    assert msg.count("abcDEF123-_xyz") == 1


def test_format_message_second_mention_is_a_literal_placeholder_not_the_real_token():
    """Guards against a future "fix" that turns the deliberate `{{token}}` double-brace
    into a real interpolation, which would leak the secret a second time."""
    msg = _format_api_token_created_message("the-real-secret", 1700000000, "")
    assert "Bearer {token}" in msg
    assert msg.count("the-real-secret") == 1


def test_format_message_uses_the_slash_command_prefix_in_the_revoke_hint():
    msg = _format_api_token_created_message("tok", 1700000000, "ex_play_")
    assert "/ex_play_api-token revoke" in msg
