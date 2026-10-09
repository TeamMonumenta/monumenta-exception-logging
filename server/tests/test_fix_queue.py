# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Byron Marohn
"""
Tests for fix-queue visibility and cancellation.

Covers the fix_attempts CHECK rebuild for the 'cancelled' status, Tracker.list_fix_attempts
and cancel_fix_attempt, GET /api/fix-attempts, POST /api/fix-attempts/<job_id>/cancel,
and exctl fix-list / fix-cancel.
"""

import asyncio
import os
import sqlite3
import sys
from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest
from bot import ExceptionBot
from cli import exctl
from tracker.api import CancelFixOutcome, Tracker
from tracker.config import TrackerConfig
from tracker.ingest import parse_event
from server import create_app
from tests.fixtures import EXAMPLE_EVENT, EXAMPLE_EVENT_2

HANDLERS = exctl._HANDLERS  # pylint: disable=protected-access

_REQUESTER = "111111111111111111"
_OTHER_USER = "222222222222222222"


@pytest.fixture
def api():
    return Tracker(TrackerConfig(db_path=':memory:'))


def _run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


def _bearer(tracker: Tracker, discord_id: str = _REQUESTER) -> dict[str, str]:
    token, _ = tracker.create_api_token(discord_id, ttl_hours=24)
    return {'Authorization': f'Bearer {token}'}


class _FakeBot:
    def __init__(self):
        self.completed: list[tuple[str, str, str, Optional[str]]] = []

    async def on_fix_attempt_completed(  # pylint: disable=too-many-arguments,too-many-positional-arguments
        self, fingerprint: str, status: str, message: str, summary: str,
        pr_url: Optional[str] = None, requester_discord_id: Optional[str] = None,
    ) -> None:
        del summary, pr_url
        self.completed.append((fingerprint, status, message, requester_discord_id))

    async def resolve_display_name(self, discord_id: str) -> str:
        return f"User{discord_id[:3]}"


# ===========================================================================
# Schema: rebuilding an old fix_attempts table
# ===========================================================================

_OLD_FIX_ATTEMPTS = """
    CREATE TABLE fix_attempts (
        id                       INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id                   TEXT NOT NULL UNIQUE,
        fingerprint              TEXT NOT NULL,
        status                   TEXT NOT NULL DEFAULT 'pending'
                                 CHECK (status IN ('pending', 'running', 'declined', 'success', 'failure')),
        rendered_message         TEXT NOT NULL,
        requested_by_discord_id  TEXT,
        message                  TEXT,
        summary                  TEXT,
        detail                   TEXT,
        pr_url                   TEXT,
        queued_at                INTEGER NOT NULL,
        started_at               INTEGER,
        completed_at             INTEGER
    );
    INSERT INTO fix_attempts (job_id, fingerprint, status, rendered_message,
                              requested_by_discord_id, message, summary, detail, pr_url,
                              queued_at, started_at, completed_at)
    VALUES ('old-job', 'f' || hex(zeroblob(32)), 'success', 'prompt', '42', 'done',
            'narrative', 'log', 'https://pr/1', 1000, 1500, 2000);
    INSERT INTO fix_attempts (job_id, fingerprint, rendered_message, queued_at)
    VALUES ('deleted-job', 'x', 'p', 1100);
    DELETE FROM fix_attempts WHERE job_id = 'deleted-job';
"""


def test_old_fix_attempts_table_is_rebuilt_to_allow_cancelled(tmp_path):
    db_path = str(tmp_path / 'old.db')
    conn = sqlite3.connect(db_path)
    conn.executescript(_OLD_FIX_ATTEMPTS)
    conn.close()

    tracker = Tracker(TrackerConfig(db_path=db_path))
    old = tracker.get_fix_attempt('old-job')
    assert old is not None
    assert old.status == 'success' and old.message == 'done'
    assert old.summary == 'narrative' and old.pr_url == 'https://pr/1'
    assert int(old.queued_at.timestamp()) == 1000
    assert old.started_at is not None and int(old.started_at.timestamp()) == 1500

    fp, _ = tracker.ingest_event(parse_event(EXAMPLE_EVENT))
    job_id = tracker.queue_fix_attempt(fp, "prompt", _REQUESTER)
    assert tracker.cancel_fix_attempt(job_id).outcome == CancelFixOutcome.CANCELLED
    tracker.close()

    conn = sqlite3.connect(db_path)
    indexes = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'fix_attempts'")}
    detail, requester = conn.execute(
        "SELECT detail, requested_by_discord_id FROM fix_attempts "
        "WHERE job_id = 'old-job'").fetchone()
    # The deleted row had id 2; the AUTOINCREMENT high-water mark must survive the
    # rebuild so it isn't handed out again.
    new_id = conn.execute(
        "SELECT id FROM fix_attempts WHERE job_id = ?", (job_id,)).fetchone()[0]
    sequences = conn.execute(
        "SELECT name, seq FROM sqlite_sequence WHERE name LIKE 'fix_attempts%'").fetchall()
    leftovers = conn.execute(
        "SELECT name FROM sqlite_master WHERE name = 'fix_attempts_new'").fetchall()
    conn.close()
    assert {'idx_fix_attempts_fingerprint', 'idx_fix_attempts_status'} <= indexes
    assert detail == 'log' and requester == '42'
    assert new_id == 3
    assert sequences == [('fix_attempts', 3)]
    assert not leftovers

    # Reopening the rebuilt database is a no-op.
    Tracker(TrackerConfig(db_path=db_path)).close()


def test_fresh_database_is_not_rebuilt(tmp_path):
    db_path = str(tmp_path / 'new.db')
    Tracker(TrackerConfig(db_path=db_path)).close()
    conn = sqlite3.connect(db_path)
    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'fix_attempts'").fetchone()[0]
    conn.close()
    assert "'cancelled'" in sql


# ===========================================================================
# Tracker
# ===========================================================================

def test_list_fix_attempts_filters_by_status_newest_first(api):
    fp1, _ = api.ingest_event(parse_event(EXAMPLE_EVENT))
    fp2, _ = api.ingest_event(parse_event(EXAMPLE_EVENT_2))
    done = api.queue_fix_attempt(fp1, "p", _REQUESTER)
    api.complete_fix_attempt(done, "failure", "nope", "", "", None)
    running = api.queue_fix_attempt(fp1, "p", _REQUESTER)
    claimed = api.claim_fix_attempt()
    assert claimed is not None and claimed.job_id == running
    pending = api.queue_fix_attempt(fp2, "p", _REQUESTER)

    queue = api.list_fix_attempts(['pending', 'running'])
    assert [a.job_id for a in queue] == [pending, running]
    assert [a.job_id for a in api.list_fix_attempts()] == [pending, running, done]
    assert [a.job_id for a in api.list_fix_attempts(limit=1)] == [pending]


def test_list_fix_attempts_rejects_unknown_status(api):
    with pytest.raises(ValueError):
        api.list_fix_attempts(['pending', 'bogus'])


def test_cancel_pending_attempt_frees_the_group(api):
    fp, _ = api.ingest_event(parse_event(EXAMPLE_EVENT))
    job_id = api.queue_fix_attempt(fp, "p", _REQUESTER)
    result = api.cancel_fix_attempt(job_id, actor="Alice")
    assert result.outcome == CancelFixOutcome.CANCELLED
    assert result.requested_by_discord_id == _REQUESTER
    status = api.get_fix_attempt(job_id)
    assert status is not None
    assert status.status == 'cancelled'
    assert status.message == 'Cancelled by Alice'
    assert status.completed_at is not None
    assert not api.has_active_fix_attempt(fp)
    # Chisel never sees it.
    assert api.claim_fix_attempt() is None


def test_cancel_running_attempt_is_refused(api):
    fp, _ = api.ingest_event(parse_event(EXAMPLE_EVENT))
    job_id = api.queue_fix_attempt(fp, "p", _REQUESTER)
    api.claim_fix_attempt()
    result = api.cancel_fix_attempt(job_id)
    assert result.outcome == CancelFixOutcome.NOT_PENDING
    assert result.status == 'running'
    status = api.get_fix_attempt(job_id)
    assert status is not None and status.status == 'running'


def test_cancel_twice_reports_not_pending(api):
    fp, _ = api.ingest_event(parse_event(EXAMPLE_EVENT))
    job_id = api.queue_fix_attempt(fp, "p", _REQUESTER)
    assert api.cancel_fix_attempt(job_id).outcome == CancelFixOutcome.CANCELLED
    again = api.cancel_fix_attempt(job_id)
    assert again.outcome == CancelFixOutcome.NOT_PENDING
    assert again.status == 'cancelled'


def test_completion_does_not_overwrite_a_cancel(api):
    fp, _ = api.ingest_event(parse_event(EXAMPLE_EVENT))
    job_id = api.queue_fix_attempt(fp, "p", _REQUESTER)
    api.cancel_fix_attempt(job_id, actor="Alice")
    assert api.complete_fix_attempt(job_id, "success", "m", "s", "d", "https://pr") is None
    status = api.get_fix_attempt(job_id)
    assert status is not None
    assert status.status == 'cancelled' and status.pr_url is None


def test_cancel_unknown_attempt(api):
    assert api.cancel_fix_attempt('nope').outcome == CancelFixOutcome.NOT_FOUND


def test_cancelled_attempt_is_not_timed_out(api):
    fp, _ = api.ingest_event(parse_event(EXAMPLE_EVENT))
    job_id = api.queue_fix_attempt(fp, "p", _REQUESTER)
    api.cancel_fix_attempt(job_id)
    assert api.timeout_stale_fix_attempts(timeout_seconds=-1) == []
    status = api.get_fix_attempt(job_id)
    assert status is not None and status.status == 'cancelled'


# ===========================================================================
# HTTP
# ===========================================================================

def test_api_list_fix_attempts_status_filter():
    async def _inner():
        tracker = Tracker(TrackerConfig(db_path=':memory:'))
        fp, _ = tracker.ingest_event(parse_event(EXAMPLE_EVENT))
        done = tracker.queue_fix_attempt(fp, "p", _REQUESTER)
        tracker.complete_fix_attempt(done, "success", "ok", "", "", "https://pr")
        pending = tracker.queue_fix_attempt(fp, "p", _REQUESTER)
        app = create_app(tracker)
        async with app.test_client() as client:
            resp = await client.get('/api/fix-attempts?status=pending,running')
            data = await resp.get_json()
            assert [a['job_id'] for a in data['fix_attempts']] == [pending]
            assert data['fix_attempts'][0]['fingerprint'] == fp

            for query in ('', '?status=all'):
                resp = await client.get(f'/api/fix-attempts{query}')
                data = await resp.get_json()
                assert [a['job_id'] for a in data['fix_attempts']] == [pending, done]

            resp = await client.get('/api/fix-attempts?status=bogus')
            assert resp.status_code == 400
            resp = await client.get('/api/fix-attempts?limit=x')
            assert resp.status_code == 400
    _run(_inner())


def test_api_cancel_fix_attempt_success_dispatches_to_bot():
    async def _inner():
        tracker = Tracker(TrackerConfig(db_path=':memory:'))
        fp, _ = tracker.ingest_event(parse_event(EXAMPLE_EVENT))
        job_id = tracker.queue_fix_attempt(fp, "p", _REQUESTER)
        fake_bot = _FakeBot()
        app = create_app(tracker, bot=fake_bot)  # type: ignore[arg-type]
        async with app.test_client() as client:
            resp = await client.post(f'/api/fix-attempts/{job_id}/cancel',
                                     headers=_bearer(tracker, _OTHER_USER))
            assert resp.status_code == 200
            data = await resp.get_json()
            assert data['status'] == 'cancelled'
            assert data['message'] == 'Cancelled by User222'
            await asyncio.sleep(0.05)
            # Someone else cancelled it, so the requester is told.
            assert fake_bot.completed == [(fp, 'cancelled', 'Cancelled by User222', _REQUESTER)]
    _run(_inner())


def test_api_cancel_own_fix_attempt_skips_the_dm():
    async def _inner():
        tracker = Tracker(TrackerConfig(db_path=':memory:'))
        fp, _ = tracker.ingest_event(parse_event(EXAMPLE_EVENT))
        job_id = tracker.queue_fix_attempt(fp, "p", _REQUESTER)
        fake_bot = _FakeBot()
        app = create_app(tracker, bot=fake_bot)  # type: ignore[arg-type]
        async with app.test_client() as client:
            resp = await client.post(f'/api/fix-attempts/{job_id}/cancel',
                                     headers=_bearer(tracker, _REQUESTER))
            assert resp.status_code == 200
            await asyncio.sleep(0.05)
            assert [c[3] for c in fake_bot.completed] == [None]
    _run(_inner())


def test_api_cancel_fix_attempt_error_statuses():
    async def _inner():
        tracker = Tracker(TrackerConfig(db_path=':memory:'))
        fp, _ = tracker.ingest_event(parse_event(EXAMPLE_EVENT))
        running = tracker.queue_fix_attempt(fp, "p", _REQUESTER)
        tracker.claim_fix_attempt()
        pending = tracker.queue_fix_attempt(fp, "p", _REQUESTER)
        app = create_app(tracker, chisel_allowed_users=[_REQUESTER])
        async with app.test_client() as client:
            # Unknown job is 404 even without a token.
            resp = await client.post('/api/fix-attempts/nope/cancel')
            assert resp.status_code == 404
            resp = await client.post(f'/api/fix-attempts/{pending}/cancel')
            assert resp.status_code == 401
            resp = await client.post(f'/api/fix-attempts/{pending}/cancel',
                                     headers=_bearer(tracker, _OTHER_USER))
            assert resp.status_code == 403
            resp = await client.post(f'/api/fix-attempts/{running}/cancel',
                                     headers=_bearer(tracker))
            assert resp.status_code == 409
            assert 'running' in (await resp.get_json())['error']
        status = tracker.get_fix_attempt(pending)
        assert status is not None and status.status == 'pending'
    _run(_inner())


def test_chisel_callback_for_cancelled_job_is_rejected():
    async def _inner():
        tracker = Tracker(TrackerConfig(db_path=':memory:'))
        fp, _ = tracker.ingest_event(parse_event(EXAMPLE_EVENT))
        job_id = tracker.queue_fix_attempt(fp, "p", _REQUESTER)
        tracker.cancel_fix_attempt(job_id)
        fake_bot = _FakeBot()
        app = create_app(tracker, bot=fake_bot,  # type: ignore[arg-type]
                         chisel_public_url="https://example.com")
        async with app.test_client() as client:
            resp = await client.post(f'/chisel/callback/{job_id}',
                                     json={'status': 'success', 'pr_url': 'https://pr'})
            assert resp.status_code == 409
            await asyncio.sleep(0.05)
            assert not fake_bot.completed
    _run(_inner())


def test_bot_cancel_removes_working_reaction_without_an_outcome(api, monkeypatch):
    fp, _ = api.ingest_event(parse_event(EXAMPLE_EVENT))
    api.set_discord_message_id(fp, "333333333333333333")
    bot = ExceptionBot(api, channel_id=123, refresh_period=300)
    me = MagicMock()
    monkeypatch.setattr(ExceptionBot, 'user', property(lambda self: me))
    fake_message = AsyncMock()
    fake_channel = AsyncMock()
    fake_channel.fetch_message = AsyncMock(return_value=fake_message)
    bot._get_channel = AsyncMock(return_value=fake_channel)  # pylint: disable=protected-access
    requester = AsyncMock()
    bot.fetch_user = AsyncMock(return_value=requester)  # type: ignore[method-assign]

    asyncio.run(bot.on_fix_attempt_completed(
        fp, 'cancelled', 'Cancelled by Alice', '', None, _REQUESTER))

    fake_message.remove_reaction.assert_awaited_once()
    fake_message.add_reaction.assert_not_awaited()
    sent = requester.send.await_args.args[0]
    assert 'was **cancelled** before Chisel picked it up' in sent
    assert 'Cancelled by Alice' in sent


# ===========================================================================
# CLI
# ===========================================================================

def _capture_request(response: Any):
    calls: list[tuple[str, str, Optional[str]]] = []

    def _fake(base_url, method, path, timeout, body=None, token=None):
        # pylint: disable=unused-argument
        calls.append((method, path, token))
        return response

    return _fake, calls


def test_fix_list_defaults_to_the_live_queue(monkeypatch, capsys):
    response = {'fix_attempts': [{
        'job_id': 'j1', 'fingerprint': 'abcd1234' + '0' * 56, 'status': 'running',
        'queued_at': 1700000000, 'started_at': 1700000060, 'completed_at': None,
        'message': None, 'summary': None, 'pr_url': None,
    }]}
    fake, calls = _capture_request(response)
    monkeypatch.setattr(exctl, 'request', fake)
    args = exctl.build_parser().parse_args(['fix-list'])
    assert HANDLERS['fix-list'](args, "http://x", 30) == 0
    assert calls == [('GET', '/api/fix-attempts?status=pending%2Crunning', None)]
    out = capsys.readouterr().out
    assert out.startswith('j1 [running] group abcd1234 queued ')
    assert ' ago), started ' in out


def test_fmt_fix_attempt_line_age_only_for_live_jobs():
    base = {'job_id': 'j', 'fingerprint': 'f' * 64, 'queued_at': 1000,
            'started_at': None, 'message': None}
    assert '(47m ago)' in exctl.fmt_fix_attempt_line(
        {**base, 'status': 'pending'}, now=1000 + 47 * 60 + 5)
    assert '(1h05m ago)' in exctl.fmt_fix_attempt_line(
        {**base, 'status': 'running'}, now=1000 + 65 * 60)
    assert 'ago' not in exctl.fmt_fix_attempt_line(
        {**base, 'status': 'failure'}, now=99999)


def test_fix_list_passes_status_and_limit(monkeypatch, capsys):
    fake, calls = _capture_request({'fix_attempts': []})
    monkeypatch.setattr(exctl, 'request', fake)
    args = exctl.build_parser().parse_args(
        ['fix-list', '--status', 'all', '--limit', '5'])
    assert HANDLERS['fix-list'](args, "http://x", 30) == 0
    assert calls[0][1] == '/api/fix-attempts?status=all&limit=5'
    assert 'No fix attempts (all).' in capsys.readouterr().out


def test_fix_list_rejects_a_bad_status(capsys):
    with pytest.raises(SystemExit):
        exctl.build_parser().parse_args(['fix-list', '--status', 'pending,bogus'])
    assert "invalid status 'bogus'" in capsys.readouterr().err


def test_fix_cancel_requires_a_token(monkeypatch, capsys):
    monkeypatch.delenv('EXCTL_API_TOKEN', raising=False)
    args = exctl.build_parser().parse_args(['fix-cancel', 'j1'])
    assert HANDLERS['fix-cancel'](args, "http://x", 30) == 1
    assert '$EXCTL_API_TOKEN' in capsys.readouterr().err


def test_fix_cancel_posts_with_token(monkeypatch, capsys):
    fake, calls = _capture_request(
        {'job_id': 'j/1', 'fingerprint': 'abcd1234' + '0' * 56, 'status': 'cancelled'})
    monkeypatch.setattr(exctl, 'request', fake)
    monkeypatch.setenv('EXCTL_API_TOKEN', 'tok')
    args = exctl.build_parser().parse_args(['fix-cancel', 'j/1'])
    assert HANDLERS['fix-cancel'](args, "http://x", 30) == 0
    assert calls == [('POST', '/api/fix-attempts/j%2F1/cancel', 'tok')]
    assert 'Cancelled job j/1 for group abcd1234' in capsys.readouterr().out
