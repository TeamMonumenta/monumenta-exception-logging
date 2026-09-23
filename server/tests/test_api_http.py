# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Byron Marohn
"""
Tests for the network API (see NETWORK_API.md): the /api/* HTTP routes and the
JSON serialization helpers that back them.
"""

import asyncio
import sqlite3
import sys
import os
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest
from tracker.config import TrackerConfig
from tracker.api import FixAttemptStatus, FrameSummary, GroupDetails, GroupSummary, \
    OccurrenceSummary, Tracker
from tracker.ingest import parse_event
from server import (
    _details_to_json, _fix_attempt_to_json, _occurrence_to_json, _summary_to_json, create_app,
)
from tests.fixtures import EXAMPLE_EVENT, EXAMPLE_EVENT_2


@pytest.fixture
def api():
    return Tracker(TrackerConfig(db_path=':memory:'))


@pytest.fixture
def fp(api):
    fingerprint, _ = api.ingest_event(parse_event(EXAMPLE_EVENT))
    return fingerprint


def _run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


class _FakeBot:
    """Minimal stand-in for ExceptionBot, recording the calls the API dispatches."""

    def __init__(self):
        self.edited: list[tuple[str, str]] = []
        self.working_reactions: list[str] = []

    async def edit_exception_message(self, fingerprint: str, message_id: str) -> None:
        self.edited.append((fingerprint, message_id))

    async def add_fix_working_reaction(self, fingerprint: str) -> None:
        self.working_reactions.append(fingerprint)

    async def resolve_display_name(self, discord_id: str) -> str:
        return f"TestUser#{discord_id}"


def _bearer(tracker: Tracker, discord_id: str = '123456789012345678') -> dict[str, str]:
    """Mint a fresh token for discord_id and return it as an Authorization header dict."""
    token, _ = tracker.create_api_token(discord_id, ttl_hours=24)
    return {'Authorization': f'Bearer {token}'}


# ===========================================================================
# JSON serialization helpers (§8.1)
# ===========================================================================

def _make_summary(**overrides) -> GroupSummary:
    now = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    defaults = {
        "fingerprint": "abcd1234" + "0" * 56,
        "exception_class": "java.lang.Exception",
        "message_template": "oops",
        "status": "active",
        "first_seen": now,
        "last_seen": now,
        "total_count": 5,
        "recent_count": 2,
        "server_counts": {"srv-1": 2},
    }
    defaults.update(overrides)
    return GroupSummary(**defaults)


def _make_details(**overrides) -> GroupDetails:
    now = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    frame = FrameSummary(class_name="com.example.Foo", method="bar", file="Foo.java", line=1)
    unknown_frame = FrameSummary(class_name="com.example.Baz", method="qux", file=None, line=-1)
    defaults = {
        "fingerprint": "abcd1234" + "0" * 56,
        "exception_class": "java.lang.Exception",
        "message_template": "oops",
        "status": "active",
        "first_seen": now,
        "last_seen": now,
        "total_count": 5,
        "logger": "com.example.Foo",
        "canonical_frames": [frame],
        "canonical_trace": [frame, unknown_frame],
        "servers_affected": ["srv-1"],
        "server_counts_24h": {"srv-1": 5},
        "hourly_timeline": [(now, 3)],
        "latest_message": "raw message",
        "muted_by": None,
        "muted_at": None,
        "resolved_by": None,
        "resolved_at": None,
    }
    defaults.update(overrides)
    return GroupDetails(**defaults)


def test_summary_to_json_timestamps_are_epoch_ints():
    g = _make_summary()
    data = _summary_to_json(g)
    assert data['first_seen'] == int(g.first_seen.timestamp())
    assert isinstance(data['first_seen'], int)
    assert data['last_seen'] == int(g.last_seen.timestamp())


def test_summary_to_json_all_fields_present():
    g = _make_summary()
    data = _summary_to_json(g)
    assert data['fingerprint'] == g.fingerprint
    assert data['status'] == 'active'
    assert data['recent_count'] == 2
    assert data['server_counts'] == {'srv-1': 2}


def test_details_to_json_frame_shape():
    d = _make_details()
    data = _details_to_json(d)
    assert data['canonical_frames'] == [
        {'class_name': 'com.example.Foo', 'method': 'bar', 'file': 'Foo.java', 'line': 1}
    ]
    assert data['canonical_trace'][1] == {
        'class_name': 'com.example.Baz', 'method': 'qux', 'file': None, 'line': -1
    }


def test_details_to_json_hourly_timeline_is_pair_arrays():
    d = _make_details()
    data = _details_to_json(d)
    assert data['hourly_timeline'] == [[int(d.hourly_timeline[0][0].timestamp()), 3]]


def test_details_to_json_optional_fields_emitted_as_null():
    d = _make_details(muted_by=None, muted_at=None, resolved_by=None, resolved_at=None)
    data = _details_to_json(d)
    assert data['muted_by'] is None
    assert data['muted_at'] is None
    assert data['resolved_by'] is None
    assert data['resolved_at'] is None
    assert 'muted_by' in data  # present, not omitted


def test_details_to_json_muted_at_epoch_int_when_set():
    now = datetime(2024, 6, 1, tzinfo=timezone.utc)
    d = _make_details(muted_by="Alice", muted_at=now)
    data = _details_to_json(d)
    assert data['muted_by'] == "Alice"
    assert data['muted_at'] == int(now.timestamp())
    assert isinstance(data['muted_at'], int)


def test_occurrence_to_json():
    now = datetime(2024, 1, 1, tzinfo=timezone.utc)
    o = OccurrenceSummary(timestamp=now, server="build", message="boom")
    data = _occurrence_to_json(o)
    assert data == {'timestamp': int(now.timestamp()), 'server': 'build',
                    'message': 'boom', 'log_message': ''}


def test_fix_attempt_to_json_pending():
    now = datetime(2024, 1, 1, tzinfo=timezone.utc)
    s = FixAttemptStatus(
        job_id="j1", fingerprint="f" * 64, status="pending", message=None, summary=None,
        pr_url=None, queued_at=now, started_at=None, completed_at=None,
    )
    data = _fix_attempt_to_json(s)
    assert data['status'] == 'pending'
    assert data['started_at'] is None
    assert data['completed_at'] is None
    assert data['queued_at'] == int(now.timestamp())


def test_fix_attempt_to_json_completed():
    now = datetime(2024, 1, 1, tzinfo=timezone.utc)
    s = FixAttemptStatus(
        job_id="j1", fingerprint="f" * 64, status="success", message="fixed",
        summary="did stuff", pr_url="https://github.com/x/y/pull/1",
        queued_at=now, started_at=now, completed_at=now,
    )
    data = _fix_attempt_to_json(s)
    assert data['pr_url'] == "https://github.com/x/y/pull/1"
    assert data['started_at'] == int(now.timestamp())
    assert data['completed_at'] == int(now.timestamp())


# ===========================================================================
# GET /api/health
# ===========================================================================

def test_health_ok():
    async def _inner():
        tracker = Tracker(TrackerConfig(db_path=':memory:'))
        app = create_app(tracker)
        async with app.test_client() as client:
            resp = await client.get('/api/health')
            assert resp.status_code == 200
            assert await resp.get_json() == {'ok': True}
    _run(_inner())


# ===========================================================================
# GET /api/groups
# ===========================================================================

def test_list_groups_empty():
    async def _inner():
        tracker = Tracker(TrackerConfig(db_path=':memory:'))
        app = create_app(tracker)
        async with app.test_client() as client:
            resp = await client.get('/api/groups')
            assert resp.status_code == 200
            data = await resp.get_json()
            assert data == {'groups': [], 'total': 0}
    _run(_inner())


def test_list_groups_returns_total_and_groups():
    async def _inner():
        tracker = Tracker(TrackerConfig(db_path=':memory:'))
        tracker.ingest_event(parse_event(EXAMPLE_EVENT))
        tracker.ingest_event(parse_event(EXAMPLE_EVENT_2))
        app = create_app(tracker)
        async with app.test_client() as client:
            resp = await client.get('/api/groups')
            data = await resp.get_json()
            assert data['total'] == 2
            assert len(data['groups']) == 2
    _run(_inner())


def test_list_groups_filters_by_status():
    async def _inner():
        tracker = Tracker(TrackerConfig(db_path=':memory:'))
        fp1, _ = tracker.ingest_event(parse_event(EXAMPLE_EVENT))
        tracker.ingest_event(parse_event(EXAMPLE_EVENT_2))
        tracker.mute_group(fp1)
        app = create_app(tracker)
        async with app.test_client() as client:
            resp = await client.get('/api/groups?status=muted')
            data = await resp.get_json()
            assert data['total'] == 1
            assert data['groups'][0]['fingerprint'] == fp1
    _run(_inner())


def test_list_groups_invalid_status_400():
    async def _inner():
        tracker = Tracker(TrackerConfig(db_path=':memory:'))
        app = create_app(tracker)
        async with app.test_client() as client:
            resp = await client.get('/api/groups?status=bogus')
            assert resp.status_code == 400
    _run(_inner())


def test_list_groups_invalid_sort_400():
    async def _inner():
        tracker = Tracker(TrackerConfig(db_path=':memory:'))
        app = create_app(tracker)
        async with app.test_client() as client:
            resp = await client.get('/api/groups?sort=bogus')
            assert resp.status_code == 400
    _run(_inner())


def test_list_groups_non_integer_limit_400():
    async def _inner():
        tracker = Tracker(TrackerConfig(db_path=':memory:'))
        app = create_app(tracker)
        async with app.test_client() as client:
            resp = await client.get('/api/groups?limit=abc')
            assert resp.status_code == 400
    _run(_inner())


def test_list_groups_out_of_range_limit_clamped_not_rejected():
    async def _inner():
        tracker = Tracker(TrackerConfig(db_path=':memory:'))
        tracker.ingest_event(parse_event(EXAMPLE_EVENT))
        app = create_app(tracker)
        async with app.test_client() as client:
            resp = await client.get('/api/groups?limit=999999')
            assert resp.status_code == 200
    _run(_inner())


def test_list_groups_search_filter():
    async def _inner():
        tracker = Tracker(TrackerConfig(db_path=':memory:'))
        fp1, _ = tracker.ingest_event(parse_event(EXAMPLE_EVENT))
        tracker.ingest_event(parse_event(EXAMPLE_EVENT_2))
        app = create_app(tracker)
        async with app.test_client() as client:
            resp = await client.get('/api/groups?search=lang.exception')
            data = await resp.get_json()
            assert data['total'] == 1
            assert data['groups'][0]['fingerprint'] == fp1
    _run(_inner())


# ===========================================================================
# GET /api/groups/<id>
# ===========================================================================

def test_get_group_by_full_fingerprint():
    async def _inner():
        tracker = Tracker(TrackerConfig(db_path=':memory:'))
        fp1, _ = tracker.ingest_event(parse_event(EXAMPLE_EVENT))
        app = create_app(tracker)
        async with app.test_client() as client:
            resp = await client.get(f'/api/groups/{fp1}')
            assert resp.status_code == 200
            data = await resp.get_json()
            assert data['fingerprint'] == fp1
    _run(_inner())


def test_get_group_by_short_id_case_insensitive():
    async def _inner():
        tracker = Tracker(TrackerConfig(db_path=':memory:'))
        fp1, _ = tracker.ingest_event(parse_event(EXAMPLE_EVENT))
        app = create_app(tracker)
        async with app.test_client() as client:
            resp = await client.get(f'/api/groups/{fp1[:8].upper()}')
            assert resp.status_code == 200
            data = await resp.get_json()
            assert data['fingerprint'] == fp1
    _run(_inner())


def test_get_group_unknown_404():
    async def _inner():
        tracker = Tracker(TrackerConfig(db_path=':memory:'))
        app = create_app(tracker)
        async with app.test_client() as client:
            resp = await client.get('/api/groups/deadbeef')
            assert resp.status_code == 404
    _run(_inner())


def test_get_group_unknown_full_fingerprint_404():
    async def _inner():
        tracker = Tracker(TrackerConfig(db_path=':memory:'))
        app = create_app(tracker)
        async with app.test_client() as client:
            resp = await client.get(f'/api/groups/{"0" * 64}')
            assert resp.status_code == 404
    _run(_inner())


# ===========================================================================
# GET /api/groups/<id>/occurrences
# ===========================================================================

def test_get_occurrences():
    async def _inner():
        tracker = Tracker(TrackerConfig(db_path=':memory:'))
        fp1, _ = tracker.ingest_event(parse_event(EXAMPLE_EVENT))
        app = create_app(tracker)
        async with app.test_client() as client:
            resp = await client.get(f'/api/groups/{fp1}/occurrences')
            assert resp.status_code == 200
            data = await resp.get_json()
            assert len(data['occurrences']) == 1
            assert data['occurrences'][0]['server'] == 'survival-0'
    _run(_inner())


def test_get_occurrences_unknown_group_404():
    async def _inner():
        tracker = Tracker(TrackerConfig(db_path=':memory:'))
        app = create_app(tracker)
        async with app.test_client() as client:
            resp = await client.get('/api/groups/deadbeef/occurrences')
            assert resp.status_code == 404
    _run(_inner())


# ===========================================================================
# GET /api/groups/<id>/fix-attempts and /api/fix-attempts/<job_id>
# ===========================================================================

def test_get_group_fix_attempts():
    async def _inner():
        tracker = Tracker(TrackerConfig(db_path=':memory:'))
        fp1, _ = tracker.ingest_event(parse_event(EXAMPLE_EVENT))
        tracker.queue_fix_attempt(fp1, "msg", "123456789012345678")
        app = create_app(tracker)
        async with app.test_client() as client:
            resp = await client.get(f'/api/groups/{fp1}/fix-attempts')
            assert resp.status_code == 200
            data = await resp.get_json()
            assert len(data['fix_attempts']) == 1
    _run(_inner())


def test_get_group_fix_attempts_unknown_group_404():
    async def _inner():
        tracker = Tracker(TrackerConfig(db_path=':memory:'))
        app = create_app(tracker)
        async with app.test_client() as client:
            resp = await client.get('/api/groups/deadbeef/fix-attempts')
            assert resp.status_code == 404
    _run(_inner())


def test_get_single_fix_attempt():
    async def _inner():
        tracker = Tracker(TrackerConfig(db_path=':memory:'))
        fp1, _ = tracker.ingest_event(parse_event(EXAMPLE_EVENT))
        job_id = tracker.queue_fix_attempt(fp1, "msg", "123456789012345678")
        app = create_app(tracker)
        async with app.test_client() as client:
            resp = await client.get(f'/api/fix-attempts/{job_id}')
            assert resp.status_code == 200
            data = await resp.get_json()
            assert data['job_id'] == job_id
    _run(_inner())


def test_get_single_fix_attempt_unknown_404():
    async def _inner():
        tracker = Tracker(TrackerConfig(db_path=':memory:'))
        app = create_app(tracker)
        async with app.test_client() as client:
            resp = await client.get('/api/fix-attempts/no-such-job')
            assert resp.status_code == 404
    _run(_inner())


# ===========================================================================
# GET /api/servers
# ===========================================================================

def test_list_servers():
    async def _inner():
        tracker = Tracker(TrackerConfig(db_path=':memory:'))
        tracker.ingest_event(parse_event(EXAMPLE_EVENT))
        app = create_app(tracker)
        async with app.test_client() as client:
            resp = await client.get('/api/servers')
            assert resp.status_code == 200
            data = await resp.get_json()
            assert data == {'servers': ['survival-0']}
    _run(_inner())


# ===========================================================================
# POST /api/groups/<id>/mute|unmute|resolve
# ===========================================================================

def test_mute_group():
    """With Discord disabled (bot=None), attribution falls back to the raw discord_id."""
    async def _inner():
        tracker = Tracker(TrackerConfig(db_path=':memory:'))
        fp1, _ = tracker.ingest_event(parse_event(EXAMPLE_EVENT))
        app = create_app(tracker)
        async with app.test_client() as client:
            resp = await client.post(
                f'/api/groups/{fp1}/mute', headers=_bearer(tracker)
            )
            assert resp.status_code == 200
            data = await resp.get_json()
            assert data['status'] == 'muted'
            assert data['muted_by'] == '123456789012345678'
    _run(_inner())


def test_mute_group_attributed_with_resolved_display_name():
    """With Discord enabled, attribution uses the resolved display name."""
    async def _inner():
        tracker = Tracker(TrackerConfig(db_path=':memory:'))
        fp1, _ = tracker.ingest_event(parse_event(EXAMPLE_EVENT))
        fake_bot = _FakeBot()
        app = create_app(tracker, bot=fake_bot)  # type: ignore[arg-type]
        async with app.test_client() as client:
            resp = await client.post(
                f'/api/groups/{fp1}/mute', headers=_bearer(tracker)
            )
            assert resp.status_code == 200
            data = await resp.get_json()
            assert data['muted_by'] == 'TestUser#123456789012345678'
    _run(_inner())


def test_unmute_group_also_clears_resolve_attribution():
    async def _inner():
        tracker = Tracker(TrackerConfig(db_path=':memory:'))
        fp1, _ = tracker.ingest_event(parse_event(EXAMPLE_EVENT))
        tracker.resolve_group(fp1, actor="Alice")
        app = create_app(tracker)
        async with app.test_client() as client:
            resp = await client.post(
                f'/api/groups/{fp1}/unmute', headers=_bearer(tracker)
            )
            assert resp.status_code == 200
            data = await resp.get_json()
            assert data['status'] == 'active'
            assert data['resolved_by'] is None
    _run(_inner())


def test_resolve_group():
    async def _inner():
        tracker = Tracker(TrackerConfig(db_path=':memory:'))
        fp1, _ = tracker.ingest_event(parse_event(EXAMPLE_EVENT))
        app = create_app(tracker)
        async with app.test_client() as client:
            resp = await client.post(
                f'/api/groups/{fp1}/resolve', headers=_bearer(tracker)
            )
            assert resp.status_code == 200
            data = await resp.get_json()
            assert data['status'] == 'resolved'
            assert data['resolved_by'] == '123456789012345678'
    _run(_inner())


def test_mute_unknown_group_404():
    async def _inner():
        tracker = Tracker(TrackerConfig(db_path=':memory:'))
        app = create_app(tracker)
        async with app.test_client() as client:
            resp = await client.post(
                '/api/groups/deadbeef/mute', headers=_bearer(tracker)
            )
            assert resp.status_code == 404
    _run(_inner())


def test_mute_missing_token_401():
    async def _inner():
        tracker = Tracker(TrackerConfig(db_path=':memory:'))
        fp1, _ = tracker.ingest_event(parse_event(EXAMPLE_EVENT))
        app = create_app(tracker)
        async with app.test_client() as client:
            resp = await client.post(f'/api/groups/{fp1}/mute')
            assert resp.status_code == 401
            assert 'Bearer' in (await resp.get_json())['error']
    _run(_inner())


@pytest.mark.parametrize("bad_header", [
    "NotBearer sometoken",  # wrong scheme
    "Bearer",               # scheme with no token at all
    "Bearer ",              # scheme with an empty token
    "Bearer garbage-token-that-was-never-minted",
])
def test_mute_invalid_token_401(bad_header):
    async def _inner():
        tracker = Tracker(TrackerConfig(db_path=':memory:'))
        fp1, _ = tracker.ingest_event(parse_event(EXAMPLE_EVENT))
        app = create_app(tracker)
        async with app.test_client() as client:
            resp = await client.post(
                f'/api/groups/{fp1}/mute', headers={'Authorization': bad_header}
            )
            assert resp.status_code == 401
    _run(_inner())


def test_mute_expired_token_401():
    async def _inner():
        tracker = Tracker(TrackerConfig(db_path=':memory:'))
        fp1, _ = tracker.ingest_event(parse_event(EXAMPLE_EVENT))
        token, _ = tracker.create_api_token('123456789012345678', ttl_hours=-1)
        app = create_app(tracker)
        async with app.test_client() as client:
            resp = await client.post(
                f'/api/groups/{fp1}/mute', headers={'Authorization': f'Bearer {token}'}
            )
            assert resp.status_code == 401
    _run(_inner())


def test_mute_revoked_token_401():
    async def _inner():
        tracker = Tracker(TrackerConfig(db_path=':memory:'))
        fp1, _ = tracker.ingest_event(parse_event(EXAMPLE_EVENT))
        token, _ = tracker.create_api_token('123456789012345678', ttl_hours=24)
        tracker.revoke_api_tokens('123456789012345678')
        app = create_app(tracker)
        async with app.test_client() as client:
            resp = await client.post(
                f'/api/groups/{fp1}/mute', headers={'Authorization': f'Bearer {token}'}
            )
            assert resp.status_code == 401
    _run(_inner())


@pytest.mark.parametrize("scheme", ["Bearer", "bearer", "BEARER", "BeArEr"])
def test_mute_accepts_any_case_of_the_bearer_scheme(scheme):
    """RFC 7235 auth-schemes are case-insensitive; a client or proxy that normalizes
    it must not be rejected with a misleading 'missing' error."""
    async def _inner():
        tracker = Tracker(TrackerConfig(db_path=':memory:'))
        fp1, _ = tracker.ingest_event(parse_event(EXAMPLE_EVENT))
        token, _ = tracker.create_api_token('123456789012345678', ttl_hours=24)
        app = create_app(tracker)
        async with app.test_client() as client:
            resp = await client.post(
                f'/api/groups/{fp1}/mute', headers={'Authorization': f'{scheme} {token}'}
            )
            assert resp.status_code == 200
    _run(_inner())


def test_mute_tolerates_extra_whitespace_after_the_scheme():
    """A stray extra space between 'Bearer' and the token must not be treated as
    part of the token — that would blame the token for a spacing quirk."""
    async def _inner():
        tracker = Tracker(TrackerConfig(db_path=':memory:'))
        fp1, _ = tracker.ingest_event(parse_event(EXAMPLE_EVENT))
        token, _ = tracker.create_api_token('123456789012345678', ttl_hours=24)
        app = create_app(tracker)
        async with app.test_client() as client:
            resp = await client.post(
                f'/api/groups/{fp1}/mute', headers={'Authorization': f'Bearer  {token}'}
            )
            assert resp.status_code == 200
    _run(_inner())


def test_mute_401_carries_www_authenticate_header():
    async def _inner():
        tracker = Tracker(TrackerConfig(db_path=':memory:'))
        fp1, _ = tracker.ingest_event(parse_event(EXAMPLE_EVENT))
        app = create_app(tracker)
        async with app.test_client() as client:
            resp = await client.post(f'/api/groups/{fp1}/mute')
            assert resp.status_code == 401
            assert resp.headers.get('WWW-Authenticate') == 'Bearer'
    _run(_inner())


def test_mute_dispatches_discord_edit_when_bot_present():
    async def _inner():
        tracker = Tracker(TrackerConfig(db_path=':memory:'))
        fp1, _ = tracker.ingest_event(parse_event(EXAMPLE_EVENT))
        tracker.set_discord_message_id(fp1, "111111111111111111")
        fake_bot = _FakeBot()
        app = create_app(tracker, bot=fake_bot)  # type: ignore[arg-type]
        async with app.test_client() as client:
            resp = await client.post(
                f'/api/groups/{fp1}/mute', headers=_bearer(tracker)
            )
            assert resp.status_code == 200
            await asyncio.sleep(0.05)
            assert fake_bot.edited == [(fp1, "111111111111111111")]
    _run(_inner())


def test_mute_no_discord_dispatch_without_tracked_message():
    async def _inner():
        tracker = Tracker(TrackerConfig(db_path=':memory:'))
        fp1, _ = tracker.ingest_event(parse_event(EXAMPLE_EVENT))
        fake_bot = _FakeBot()
        app = create_app(tracker, bot=fake_bot)  # type: ignore[arg-type]
        async with app.test_client() as client:
            resp = await client.post(
                f'/api/groups/{fp1}/mute', headers=_bearer(tracker)
            )
            assert resp.status_code == 200
            await asyncio.sleep(0.05)
            assert not fake_bot.edited
    _run(_inner())


def test_mute_works_with_discord_entirely_disabled():
    """The API must keep working with bot=None, exactly like /ingest does."""
    async def _inner():
        tracker = Tracker(TrackerConfig(db_path=':memory:'))
        fp1, _ = tracker.ingest_event(parse_event(EXAMPLE_EVENT))
        app = create_app(tracker, bot=None)
        async with app.test_client() as client:
            resp = await client.post(
                f'/api/groups/{fp1}/mute', headers=_bearer(tracker)
            )
            assert resp.status_code == 200
    _run(_inner())


# ===========================================================================
# POST /api/groups/<id>/fix — status-code precedence
# ===========================================================================

@pytest.fixture
def prompt_path(tmp_path):
    path = tmp_path / "fix_exception_prompt.md"
    path.write_text("Fix {exception_class}", encoding="utf-8")
    return str(path)


def test_fix_unknown_group_404_before_other_checks():
    async def _inner():
        tracker = Tracker(TrackerConfig(db_path=':memory:'))
        app = create_app(tracker, chisel_public_url=None)  # not configured either
        async with app.test_client() as client:
            resp = await client.post(
                '/api/groups/deadbeef/fix', headers=_bearer(tracker)
            )
            assert resp.status_code == 404
    _run(_inner())


def test_fix_missing_token_401():
    async def _inner():
        tracker = Tracker(TrackerConfig(db_path=':memory:'))
        fp1, _ = tracker.ingest_event(parse_event(EXAMPLE_EVENT))
        app = create_app(tracker, chisel_public_url="https://example.com")
        async with app.test_client() as client:
            resp = await client.post(f'/api/groups/{fp1}/fix')
            assert resp.status_code == 401
    _run(_inner())


def test_fix_not_configured_503():
    async def _inner():
        tracker = Tracker(TrackerConfig(db_path=':memory:'))
        fp1, _ = tracker.ingest_event(parse_event(EXAMPLE_EVENT))
        app = create_app(tracker, chisel_public_url=None)
        async with app.test_client() as client:
            resp = await client.post(
                f'/api/groups/{fp1}/fix', headers=_bearer(tracker)
            )
            assert resp.status_code == 503
    _run(_inner())


def test_fix_not_in_allowed_users_403(prompt_path):
    async def _inner():
        tracker = Tracker(TrackerConfig(db_path=':memory:'))
        fp1, _ = tracker.ingest_event(parse_event(EXAMPLE_EVENT))
        app = create_app(
            tracker, chisel_public_url="https://example.com",
            chisel_allowed_users=["999999999999999999"],
            chisel_fix_prompt_path=prompt_path,
        )
        async with app.test_client() as client:
            resp = await client.post(
                f'/api/groups/{fp1}/fix', headers=_bearer(tracker)
            )
            assert resp.status_code == 403
    _run(_inner())


def test_fix_in_allowed_users_200(prompt_path):
    """The allow-list must be checked against the token's verified discord_id, not
    against anything else — this is the positive case for test_fix_not_in_allowed_users_403
    above, proving a match (not just a mismatch) is actually recognized."""
    async def _inner():
        tracker = Tracker(TrackerConfig(db_path=':memory:'))
        fp1, _ = tracker.ingest_event(parse_event(EXAMPLE_EVENT))
        app = create_app(
            tracker, chisel_public_url="https://example.com",
            chisel_allowed_users=["123456789012345678"],
            chisel_fix_prompt_path=prompt_path,
        )
        async with app.test_client() as client:
            resp = await client.post(
                f'/api/groups/{fp1}/fix',
                headers=_bearer(tracker, discord_id='123456789012345678'),
            )
            assert resp.status_code == 200
    _run(_inner())


def test_fix_empty_allowed_users_means_no_restriction(prompt_path):
    async def _inner():
        tracker = Tracker(TrackerConfig(db_path=':memory:'))
        fp1, _ = tracker.ingest_event(parse_event(EXAMPLE_EVENT))
        app = create_app(
            tracker, chisel_public_url="https://example.com",
            chisel_allowed_users=[], chisel_fix_prompt_path=prompt_path,
        )
        async with app.test_client() as client:
            resp = await client.post(
                f'/api/groups/{fp1}/fix', headers=_bearer(tracker)
            )
            assert resp.status_code == 200
    _run(_inner())


def test_fix_already_active_409(prompt_path):
    async def _inner():
        tracker = Tracker(TrackerConfig(db_path=':memory:'))
        fp1, _ = tracker.ingest_event(parse_event(EXAMPLE_EVENT))
        app = create_app(
            tracker, chisel_public_url="https://example.com", chisel_fix_prompt_path=prompt_path,
        )
        async with app.test_client() as client:
            first = await client.post(
                f'/api/groups/{fp1}/fix', headers=_bearer(tracker)
            )
            assert first.status_code == 200
            second = await client.post(
                f'/api/groups/{fp1}/fix', headers=_bearer(tracker)
            )
            assert second.status_code == 409
    _run(_inner())


def test_fix_template_unreadable_500():
    async def _inner():
        tracker = Tracker(TrackerConfig(db_path=':memory:'))
        fp1, _ = tracker.ingest_event(parse_event(EXAMPLE_EVENT))
        app = create_app(
            tracker, chisel_public_url="https://example.com",
            chisel_fix_prompt_path="/no/such/path.md",
        )
        async with app.test_client() as client:
            resp = await client.post(
                f'/api/groups/{fp1}/fix', headers=_bearer(tracker)
            )
            assert resp.status_code == 500
    _run(_inner())


def test_fix_queued_returns_job_id(prompt_path):
    async def _inner():
        tracker = Tracker(TrackerConfig(db_path=':memory:'))
        fp1, _ = tracker.ingest_event(parse_event(EXAMPLE_EVENT))
        app = create_app(
            tracker, chisel_public_url="https://example.com", chisel_fix_prompt_path=prompt_path,
        )
        async with app.test_client() as client:
            resp = await client.post(
                f'/api/groups/{fp1}/fix', headers=_bearer(tracker)
            )
            assert resp.status_code == 200
            data = await resp.get_json()
            assert 'job_id' in data
            status = tracker.get_fix_attempt(data['job_id'])
            assert status is not None
    _run(_inner())


def test_fix_dispatches_working_reaction_when_bot_present(prompt_path):
    """An API-queued fix must show the same in-flight marker the :wrench: path adds,
    or the outcome emoji later appears with nothing having preceded it."""
    async def _inner():
        tracker = Tracker(TrackerConfig(db_path=':memory:'))
        fp1, _ = tracker.ingest_event(parse_event(EXAMPLE_EVENT))
        fake_bot = _FakeBot()
        app = create_app(
            tracker, bot=fake_bot,  # type: ignore[arg-type]
            chisel_public_url="https://example.com", chisel_fix_prompt_path=prompt_path,
        )
        async with app.test_client() as client:
            resp = await client.post(
                f'/api/groups/{fp1}/fix', headers=_bearer(tracker)
            )
            assert resp.status_code == 200
            await asyncio.sleep(0.05)
            assert fake_bot.working_reactions == [fp1]
    _run(_inner())


def test_fix_rejected_does_not_dispatch_working_reaction(prompt_path):
    """A 409 must not leave a working marker on the message."""
    async def _inner():
        tracker = Tracker(TrackerConfig(db_path=':memory:'))
        fp1, _ = tracker.ingest_event(parse_event(EXAMPLE_EVENT))
        tracker.queue_fix_attempt(fp1, "already running", "123456789012345678")
        fake_bot = _FakeBot()
        app = create_app(
            tracker, bot=fake_bot,  # type: ignore[arg-type]
            chisel_public_url="https://example.com", chisel_fix_prompt_path=prompt_path,
        )
        async with app.test_client() as client:
            resp = await client.post(
                f'/api/groups/{fp1}/fix', headers=_bearer(tracker)
            )
            assert resp.status_code == 409
            await asyncio.sleep(0.05)
            assert not fake_bot.working_reactions
    _run(_inner())


# ===========================================================================
# Case-insensitive ID resolution (§8.2)
# ===========================================================================

def test_get_group_by_uppercase_full_fingerprint():
    """Uppercase must work for the full fingerprint too, not just the short ID —
    otherwise an ID pasted from a log 404s in one form and resolves in the other."""
    async def _inner():
        tracker = Tracker(TrackerConfig(db_path=':memory:'))
        fp1, _ = tracker.ingest_event(parse_event(EXAMPLE_EVENT))
        app = create_app(tracker)
        async with app.test_client() as client:
            resp = await client.get(f'/api/groups/{fp1.upper()}')
            assert resp.status_code == 200
            data = await resp.get_json()
            assert data['fingerprint'] == fp1
    _run(_inner())


def test_mutate_by_uppercase_short_id():
    async def _inner():
        tracker = Tracker(TrackerConfig(db_path=':memory:'))
        fp1, _ = tracker.ingest_event(parse_event(EXAMPLE_EVENT))
        app = create_app(tracker)
        async with app.test_client() as client:
            resp = await client.post(
                f'/api/groups/{fp1[:8].upper()}/mute', headers=_bearer(tracker)
            )
            assert resp.status_code == 200
            assert (await resp.get_json())['status'] == 'muted'
    _run(_inner())


# ===========================================================================
# JSON error responses for /api/* (a typo'd path must not return HTML)
# ===========================================================================

def test_unknown_api_path_returns_json_error():
    async def _inner():
        tracker = Tracker(TrackerConfig(db_path=':memory:'))
        app = create_app(tracker)
        async with app.test_client() as client:
            resp = await client.get('/api/nope')
            assert resp.status_code == 404
            assert (await resp.get_json())['error'] == 'no such endpoint'
    _run(_inner())


def test_wrong_method_on_api_path_returns_json_error():
    async def _inner():
        tracker = Tracker(TrackerConfig(db_path=':memory:'))
        app = create_app(tracker)
        async with app.test_client() as client:
            resp = await client.post('/api/servers')
            assert resp.status_code == 405
            assert 'error' in (await resp.get_json())
    _run(_inner())


def test_non_api_404_keeps_default_html_response():
    """Only /api/* switches to JSON errors; /ingest and friends are untouched."""
    async def _inner():
        tracker = Tracker(TrackerConfig(db_path=':memory:'))
        app = create_app(tracker)
        async with app.test_client() as client:
            resp = await client.get('/definitely-not-a-route')
            assert resp.status_code == 404
            assert b'<!doctype html>' in (await resp.get_data()).lower()
    _run(_inner())


def test_unhandled_error_on_api_path_returns_json_500():
    async def _inner():
        tracker = Tracker(TrackerConfig(db_path=':memory:'))

        def _boom() -> list[str]:
            raise sqlite3.OperationalError("database is locked")

        tracker.get_distinct_servers = _boom  # type: ignore[method-assign]
        app = create_app(tracker)
        async with app.test_client() as client:
            resp = await client.get('/api/servers')
            assert resp.status_code == 500
            assert (await resp.get_json())['error'] == 'internal server error'
    _run(_inner())


# ===========================================================================
# No /api/purge route
# ===========================================================================

def test_no_purge_route():
    async def _inner():
        tracker = Tracker(TrackerConfig(db_path=':memory:'))
        app = create_app(tracker)
        async with app.test_client() as client:
            resp = await client.post('/api/purge', headers=_bearer(tracker))
            assert resp.status_code == 404
    _run(_inner())


# ---------------------------------------------------------------------------
# Log-event context in the group-details JSON
# ---------------------------------------------------------------------------

def test_details_to_json_log_context_defaults():
    """A group carrying no log context still emits every documented key."""
    data = _details_to_json(_make_details())
    assert data['level'] == ''
    assert data['log_message_template'] == ''
    assert data['cause_chain'] == []
    assert data['latest_log_message'] is None


def test_details_to_json_cause_chain():
    from tracker.api import CauseSummary
    inner = FrameSummary(class_name="com.example.Deep", method="run", file="Deep.java", line=7)
    d = _make_details(
        level='WARN',
        log_message_template='Task #<N> for Monumenta generated an exception',
        latest_log_message='Task #42 for Monumenta generated an exception',
        cause_chain=[
            CauseSummary(class_name='java.lang.IllegalArgumentException',
                         message='World unloaded', frames=[inner]),
            CauseSummary(class_name='java.lang.NullPointerException', message='', frames=[]),
        ],
    )
    data = _details_to_json(d)
    assert data['level'] == 'WARN'
    assert data['log_message_template'] == 'Task #<N> for Monumenta generated an exception'
    assert data['latest_log_message'] == 'Task #42 for Monumenta generated an exception'
    assert data['cause_chain'] == [
        {
            'class_name': 'java.lang.IllegalArgumentException',
            'message': 'World unloaded',
            'frames': [{'class_name': 'com.example.Deep', 'method': 'run',
                        'file': 'Deep.java', 'line': 7}],
        },
        {'class_name': 'java.lang.NullPointerException', 'message': '', 'frames': []},
    ]
