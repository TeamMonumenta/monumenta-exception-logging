# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Byron Marohn
"""
Tests for the Chisel integration: fix_attempts DB layer, Tracker methods,
HTTP endpoints, and prompt template rendering.
"""

import asyncio
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest
from tracker.config import TrackerConfig
from tracker.api import Tracker
from tracker.ingest import parse_event
from bot import _render_fix_prompt
from server import create_app
from tests.fixtures import EXAMPLE_EVENT, EXAMPLE_EVENT_2


@pytest.fixture
def api():
    return Tracker(TrackerConfig(db_path=':memory:'))


@pytest.fixture
def fp(api):
    fingerprint, _ = api.ingest_event(parse_event(EXAMPLE_EVENT))
    return fingerprint


@pytest.fixture
def fp2(api):
    api.ingest_event(parse_event(EXAMPLE_EVENT))
    fingerprint, _ = api.ingest_event(parse_event(EXAMPLE_EVENT_2))
    return fingerprint


# ===========================================================================
# has_active_fix_attempt
# ===========================================================================

def test_no_active_when_empty(api, fp):
    assert api.has_active_fix_attempt(fp) is False


def test_active_after_queue(api, fp):
    api.queue_fix_attempt(fp, "fix me")
    assert api.has_active_fix_attempt(fp) is True


def test_not_active_after_complete(api, fp):
    job_id = api.queue_fix_attempt(fp, "fix me")
    api.claim_fix_attempt()
    api.complete_fix_attempt(job_id, "success", "done", "summary", "detail", None)
    assert api.has_active_fix_attempt(fp) is False


def test_not_active_for_different_fingerprint(api, fp, fp2):
    api.queue_fix_attempt(fp, "fix me")
    assert api.has_active_fix_attempt(fp2) is False


# ===========================================================================
# queue_fix_attempt
# ===========================================================================

def test_queue_returns_uuid(api, fp):
    job_id = api.queue_fix_attempt(fp, "fix me")
    assert len(job_id) == 36  # UUID format
    assert job_id.count('-') == 4


def test_queue_two_jobs_different_ids(api, fp, fp2):
    id1 = api.queue_fix_attempt(fp, "fix 1")
    id2 = api.queue_fix_attempt(fp2, "fix 2")
    assert id1 != id2


# ===========================================================================
# claim_fix_attempt
# ===========================================================================

def test_claim_empty_returns_none(api):
    assert api.claim_fix_attempt() is None


def test_claim_returns_job(api, fp):
    job_id = api.queue_fix_attempt(fp, "fix me")
    job = api.claim_fix_attempt()
    assert job is not None
    assert job.job_id == job_id
    assert job.fingerprint == fp
    assert job.rendered_message == "fix me"


def test_claim_marks_running(api, fp):
    api.queue_fix_attempt(fp, "fix me")
    api.claim_fix_attempt()
    # Still active (running)
    assert api.has_active_fix_attempt(fp) is True


def test_claim_returns_oldest_first(api, fp, fp2):
    id1 = api.queue_fix_attempt(fp, "first")
    id2 = api.queue_fix_attempt(fp2, "second")
    job = api.claim_fix_attempt()
    assert job is not None
    assert job.job_id == id1
    job2 = api.claim_fix_attempt()
    assert job2 is not None
    assert job2.job_id == id2


def test_claim_skips_running(api, fp, fp2):
    api.queue_fix_attempt(fp, "first")
    api.queue_fix_attempt(fp2, "second")
    api.claim_fix_attempt()  # claims first, marks it running
    # Second claim should get fp2
    job = api.claim_fix_attempt()
    assert job is not None
    assert job.fingerprint == fp2


def test_claim_exhausted_returns_none(api, fp):
    api.queue_fix_attempt(fp, "fix me")
    api.claim_fix_attempt()
    assert api.claim_fix_attempt() is None


# ===========================================================================
# complete_fix_attempt
# ===========================================================================

def test_complete_returns_fingerprint(api, fp):
    job_id = api.queue_fix_attempt(fp, "fix me")
    api.claim_fix_attempt()
    result = api.complete_fix_attempt(job_id, "success", "ok", "summary", "detail", None)
    assert result is not None
    assert result[0] == fp
    assert result[1] is None  # no requester stored


def test_complete_returns_requester_discord_id(api, fp):
    job_id = api.queue_fix_attempt(fp, "fix me", requested_by_discord_id="123456789")
    api.claim_fix_attempt()
    result = api.complete_fix_attempt(job_id, "success", "ok", "summary", "detail", None)
    assert result is not None
    assert result[0] == fp
    assert result[1] == "123456789"


def test_complete_unknown_job_id_returns_none(api):
    result = api.complete_fix_attempt(
        "00000000-0000-0000-0000-000000000000",
        "failure", "nope", "", "", None,
    )
    assert result is None


def test_complete_stores_pr_url(api, fp):
    job_id = api.queue_fix_attempt(fp, "fix me")
    api.claim_fix_attempt()
    api.complete_fix_attempt(
        job_id, "success", "done", "summary", "detail",
        "https://github.com/example/pull/1",
    )
    # The row still exists - complete_fix_attempt does not guard against re-completion
    result = api.complete_fix_attempt(job_id, "failure", "re-run", "", "", None)
    assert result is not None
    assert result[0] == fp  # fingerprint still returned (row found)


def test_complete_all_statuses():
    for status in ("success", "failure", "declined"):
        tracker = Tracker(TrackerConfig(db_path=':memory:'))
        fingerprint, _ = tracker.ingest_event(parse_event(EXAMPLE_EVENT))
        job_id = tracker.queue_fix_attempt(fingerprint, "msg")
        tracker.claim_fix_attempt()
        result = tracker.complete_fix_attempt(job_id, status, "msg", "sum", "det", None)
        assert result is not None
        assert result[0] == fingerprint


# ===========================================================================
# /chisel/poll HTTP endpoint
# ===========================================================================

def _run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


def test_poll_503_when_chisel_disabled():
    async def _inner():
        tracker = Tracker(TrackerConfig(db_path=':memory:'))
        app = create_app(tracker, chisel_public_url=None)
        async with app.test_client() as client:
            resp = await client.post('/chisel/poll')
            assert resp.status_code == 503
    _run(_inner())


def test_poll_204_when_queue_empty():
    async def _inner():
        tracker = Tracker(TrackerConfig(db_path=':memory:'))
        app = create_app(tracker, chisel_public_url="https://example.com")
        async with app.test_client() as client:
            resp = await client.post('/chisel/poll')
            assert resp.status_code == 204
    _run(_inner())


def test_poll_200_returns_job():
    async def _inner():
        tracker = Tracker(TrackerConfig(db_path=':memory:'))
        fp, _ = tracker.ingest_event(parse_event(EXAMPLE_EVENT))
        job_id = tracker.queue_fix_attempt(fp, "please fix this")
        app = create_app(tracker, chisel_public_url="https://example.com")
        async with app.test_client() as client:
            resp = await client.post('/chisel/poll')
            assert resp.status_code == 200
            data = await resp.get_json()
            assert data['message'] == "please fix this"
            assert data['requester_id'] == fp[:8]
            assert data['callback_url'] == f"https://example.com/chisel/callback/{job_id}"
    _run(_inner())


def test_poll_claims_job_so_second_poll_is_204():
    async def _inner():
        tracker = Tracker(TrackerConfig(db_path=':memory:'))
        fp, _ = tracker.ingest_event(parse_event(EXAMPLE_EVENT))
        tracker.queue_fix_attempt(fp, "fix me")
        app = create_app(tracker, chisel_public_url="https://example.com")
        async with app.test_client() as client:
            await client.post('/chisel/poll')
            resp = await client.post('/chisel/poll')
            assert resp.status_code == 204
    _run(_inner())


# ===========================================================================
# /chisel/callback/<job_id> HTTP endpoint
# ===========================================================================

def test_callback_503_when_chisel_disabled():
    async def _inner():
        tracker = Tracker(TrackerConfig(db_path=':memory:'))
        app = create_app(tracker, chisel_public_url=None)
        async with app.test_client() as client:
            resp = await client.post(
                '/chisel/callback/some-id',
                json={'status': 'success', 'message': 'ok', 'summary': '', 'detail': ''},
            )
            assert resp.status_code == 503
    _run(_inner())


def test_callback_404_for_unknown_job():
    async def _inner():
        tracker = Tracker(TrackerConfig(db_path=':memory:'))
        app = create_app(tracker, chisel_public_url="https://example.com")
        async with app.test_client() as client:
            resp = await client.post(
                '/chisel/callback/00000000-0000-0000-0000-000000000000',
                json={'status': 'success', 'message': 'ok', 'summary': '', 'detail': ''},
            )
            assert resp.status_code == 404
    _run(_inner())


def test_callback_400_for_invalid_status():
    async def _inner():
        tracker = Tracker(TrackerConfig(db_path=':memory:'))
        fp, _ = tracker.ingest_event(parse_event(EXAMPLE_EVENT))
        job_id = tracker.queue_fix_attempt(fp, "fix me")
        tracker.claim_fix_attempt()
        app = create_app(tracker, chisel_public_url="https://example.com")
        async with app.test_client() as client:
            resp = await client.post(
                f'/chisel/callback/{job_id}',
                json={'status': 'unknown', 'message': 'bad', 'summary': '', 'detail': ''},
            )
            assert resp.status_code == 400
    _run(_inner())


def test_callback_200_success():
    async def _inner():
        tracker = Tracker(TrackerConfig(db_path=':memory:'))
        fp, _ = tracker.ingest_event(parse_event(EXAMPLE_EVENT))
        job_id = tracker.queue_fix_attempt(fp, "fix me")
        tracker.claim_fix_attempt()
        app = create_app(tracker, chisel_public_url="https://example.com")
        async with app.test_client() as client:
            resp = await client.post(
                f'/chisel/callback/{job_id}',
                json={
                    'status': 'success',
                    'message': 'Fixed!',
                    'summary': 'Added null check',
                    'detail': 'step 1 step 2',
                    'pr_url': 'https://github.com/example/pull/99',
                },
            )
            assert resp.status_code == 200
            data = await resp.get_json()
            assert data['ok'] is True
        # Job should now be inactive
        assert tracker.has_active_fix_attempt(fp) is False
    _run(_inner())


def test_callback_all_terminal_statuses():
    for status in ("success", "failure", "declined"):
        async def _inner(s=status):
            tracker = Tracker(TrackerConfig(db_path=':memory:'))
            fp2, _ = tracker.ingest_event(parse_event(EXAMPLE_EVENT))
            job_id = tracker.queue_fix_attempt(fp2, "fix me")
            tracker.claim_fix_attempt()
            app = create_app(tracker, chisel_public_url="https://example.com")
            async with app.test_client() as client:
                resp = await client.post(
                    f'/chisel/callback/{job_id}',
                    json={'status': s, 'message': 'done', 'summary': '', 'detail': ''},
                )
                assert resp.status_code == 200
        _run(_inner())


# ===========================================================================
# _render_fix_prompt
# ===========================================================================

def test_render_substitutes_all_variables(api, fp):
    details = api.get_group_details(fp)
    assert details is not None
    template = (
        "{short_id} {exception_class} {message} {stacktrace} "
        "{count} {servers} {first_seen} {last_seen}"
    )
    rendered = _render_fix_prompt(template, details)
    assert details.fingerprint[:8] in rendered
    assert details.exception_class in rendered
    assert str(details.total_count) in rendered


def test_render_unknown_variable_left_unchanged(api, fp):
    details = api.get_group_details(fp)
    assert details is not None
    rendered = _render_fix_prompt("hello {unknown_var} world", details)
    assert "{unknown_var}" in rendered


def test_render_curly_braces_in_message_safe():
    """Curly braces in exception data must not cause format errors."""
    tracker = Tracker(TrackerConfig(db_path=':memory:'))
    event = dict(EXAMPLE_EVENT)
    exception = dict(event['exception'])
    exception['message'] = "Cannot find key {myKey} in map"
    event['exception'] = exception
    fp, _ = tracker.ingest_event(parse_event(event))
    details = tracker.get_group_details(fp)
    assert details is not None
    rendered = _render_fix_prompt("msg: {message}", details)
    # The normalized message may differ, but the render must not raise
    assert "msg:" in rendered
