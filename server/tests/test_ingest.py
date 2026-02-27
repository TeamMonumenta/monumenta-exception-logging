# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Byron Marohn
"""
Unit tests for the ingest pipeline and grouping behaviour.

Tests use real production exception data from server logs to validate that
fingerprinting, normalization, and grouping work correctly end-to-end.
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest
from tracker.config import TrackerConfig
from tracker.api import Tracker
from tracker.ingest import parse_event
from tests.fixtures import (
    EXAMPLE_EVENT,
    REAL_NPE_ALLAY,
    REAL_ILLEGAL_STATE_ASYNC_SOUND,
    REAL_ILLEGAL_ARG_HITBOX,
    REAL_CME_TAB,
)


@pytest.fixture
def fresh_api():
    return Tracker(TrackerConfig(db_path=':memory:'))


# ===========================================================================
# Ingest of real production exceptions
# ===========================================================================

def test_ingest_npe_allay_creates_group(fresh_api):
    fp, _ = fresh_api.ingest_event(parse_event(REAL_NPE_ALLAY))
    assert len(fp) == 64
    details = fresh_api.get_group_details(fp)
    assert details.exception_class == 'java.lang.NullPointerException'
    assert details.status == 'active'


def test_ingest_npe_allay_normalizes_quoted_class_names(fresh_api):
    # Java 17+ NPE messages embed class names in double quotes; they must be
    # replaced by <str> tokens so different class paths don't split the group.
    fp, _ = fresh_api.ingest_event(parse_event(REAL_NPE_ALLAY))
    details = fresh_api.get_group_details(fp)
    assert 'org.bukkit.entity.Allay' not in details.message_template
    assert 'this.this$0.mBoss' not in details.message_template
    assert '<str>' in details.message_template


def test_ingest_illegal_state_async_sound(fresh_api):
    fp, _ = fresh_api.ingest_event(parse_event(REAL_ILLEGAL_STATE_ASYNC_SOUND))
    details = fresh_api.get_group_details(fp)
    assert details.exception_class == 'java.lang.IllegalStateException'
    # Plain message with no normalizable tokens is stored verbatim.
    assert details.message_template == 'Asynchronous play sound!'


def test_ingest_async_sound_canonical_frames_are_app_frames(fresh_api):
    # The first frames in the stack are spigot/bukkit (not app frames).
    # canonical_frames must contain only the com.playmonumenta frames.
    fp, _ = fresh_api.ingest_event(parse_event(REAL_ILLEGAL_STATE_ASYNC_SOUND))
    details = fresh_api.get_group_details(fp)
    assert all(f.class_name.startswith('com.playmonumenta') for f in details.canonical_frames)
    assert details.canonical_frames[0].class_name == (
        'com.playmonumenta.plugins.hunts.bosses.spells.SpellMagmaticConvergence$2'
    )


def test_ingest_illegal_arg_hitbox(fresh_api):
    fp, _ = fresh_api.ingest_event(parse_event(REAL_ILLEGAL_ARG_HITBOX))
    details = fresh_api.get_group_details(fp)
    assert details.exception_class == 'java.lang.IllegalArgumentException'
    assert 'empty list of hitboxes' in details.message_template
    assert details.canonical_frames[0].class_name == 'com.playmonumenta.plugins.utils.Hitbox'
    assert details.canonical_frames[0].method == 'unionOf'


def test_ingest_cme_null_message(fresh_api):
    # ConcurrentModificationException has no message; ingest must handle None gracefully.
    fp, _ = fresh_api.ingest_event(parse_event(REAL_CME_TAB))
    details = fresh_api.get_group_details(fp)
    assert details.exception_class == 'java.util.ConcurrentModificationException'
    assert details.message_template == ''


def test_ingest_cme_canonical_frames_skip_third_party(fresh_api):
    # com.playmonumenta frames are deep in the CME stack (after TAB plugin frames).
    # extract_app_frames must find them even though they are not at the top.
    fp, _ = fresh_api.ingest_event(parse_event(REAL_CME_TAB))
    details = fresh_api.get_group_details(fp)
    assert all(f.class_name.startswith('com.playmonumenta') for f in details.canonical_frames)
    assert details.canonical_frames[0].class_name == (
        'com.playmonumenta.plugins.integrations.TABIntegration'
    )
    assert details.canonical_frames[0].method == 'refreshOnlinePlayer'


# ===========================================================================
# Grouping behaviour
# ===========================================================================

def test_different_uuids_in_message_same_group(fresh_api):
    """Two NPEs that differ only in a UUID in their message must map to the same group."""
    frame = {'class_name': 'com.playmonumenta.plugins.Foo', 'method': 'bar',
             'file': 'Foo.java', 'line': 10, 'location': 'Monumenta.jar'}
    event_a = {**EXAMPLE_EVENT, 'exception': {
        **EXAMPLE_EVENT['exception'],
        'class_name': 'java.lang.NullPointerException',
        'message': 'Cannot read entity 550e8400-e29b-41d4-a716-446655440001',
        'frames': [frame],
    }}
    event_b = {**EXAMPLE_EVENT, 'exception': {
        **EXAMPLE_EVENT['exception'],
        'class_name': 'java.lang.NullPointerException',
        'message': 'Cannot read entity 550e8400-e29b-41d4-a716-446655440002',
        'frames': [frame],
    }}
    fp_a, _ = fresh_api.ingest_event(parse_event(event_a))
    fp_b, _ = fresh_api.ingest_event(parse_event(event_b))
    assert fp_a == fp_b
    details = fresh_api.get_group_details(fp_a)
    assert details.total_count == 2


def test_line_number_change_same_group(fresh_api):
    """After a minor code edit that shifts line numbers, the same exception maps to the same group."""
    base_frames = REAL_ILLEGAL_ARG_HITBOX['exception']['frames']
    event_v2 = {**REAL_ILLEGAL_ARG_HITBOX, 'exception': {
        **REAL_ILLEGAL_ARG_HITBOX['exception'],
        'frames': [{**f, 'line': f.get('line', -1) + 5} for f in base_frames],
    }}
    fp1, _ = fresh_api.ingest_event(parse_event(REAL_ILLEGAL_ARG_HITBOX))
    fp2, _ = fresh_api.ingest_event(parse_event(event_v2))
    assert fp1 == fp2


def test_null_message_events_group_together(fresh_api):
    """Two CMEs with null message from the same code path must share a fingerprint."""
    event_a = {**REAL_CME_TAB, 'server_id': 'isles'}
    event_b = {**REAL_CME_TAB, 'server_id': 'isles-2'}
    fp_a, _ = fresh_api.ingest_event(parse_event(event_a))
    fp_b, _ = fresh_api.ingest_event(parse_event(event_b))
    assert fp_a == fp_b


def test_four_real_exceptions_produce_four_distinct_fingerprints(fresh_api):
    fp1, _ = fresh_api.ingest_event(parse_event(REAL_NPE_ALLAY))
    fp2, _ = fresh_api.ingest_event(parse_event(REAL_ILLEGAL_STATE_ASYNC_SOUND))
    fp3, _ = fresh_api.ingest_event(parse_event(REAL_ILLEGAL_ARG_HITBOX))
    fp4, _ = fresh_api.ingest_event(parse_event(REAL_CME_TAB))
    assert len({fp1, fp2, fp3, fp4}) == 4


def test_no_app_frames_falls_back_to_first_n(fresh_api):
    """Exception with no com.playmonumenta frames uses the first 3 frames for fingerprinting."""
    no_app_event = {**EXAMPLE_EVENT, 'exception': {
        **EXAMPLE_EVENT['exception'],
        'class_name': 'java.lang.Exception',
        'message': 'Fallback test',
        'frames': [
            {'class_name': 'java.lang.Thread', 'method': 'run',
             'file': 'Thread.java', 'line': 1583, 'location': None},
            {'class_name': 'java.util.concurrent.FutureTask', 'method': 'run',
             'file': 'FutureTask.java', 'line': 317, 'location': None},
            {'class_name': 'java.util.concurrent.ThreadPoolExecutor', 'method': 'runWorker',
             'file': 'ThreadPoolExecutor.java', 'line': 1144, 'location': None},
            {'class_name': 'java.util.concurrent.ThreadPoolExecutor$Worker', 'method': 'run',
             'file': 'ThreadPoolExecutor.java', 'line': 642, 'location': None},
        ],
    }}
    fp, _ = fresh_api.ingest_event(parse_event(no_app_event))
    details = fresh_api.get_group_details(fp)
    assert len(details.canonical_frames) == 3
    assert details.canonical_frames[0].class_name == 'java.lang.Thread'


def test_same_exception_multiple_servers_all_show_in_servers_affected(fresh_api):
    """Occurrences from different servers must all appear in servers_affected."""
    for server in ('ring', 'ring-2', 'ring-5'):
        fresh_api.ingest_event(parse_event({
            **REAL_ILLEGAL_STATE_ASYNC_SOUND, 'server_id': server
        }))
    fp, _ = fresh_api.ingest_event(parse_event(REAL_ILLEGAL_STATE_ASYNC_SOUND))
    details = fresh_api.get_group_details(fp)
    assert sorted(details.servers_affected) == ['ring', 'ring-2', 'ring-5']


def test_servers_affected_excludes_stale_servers(fresh_api):
    """servers_affected must not include servers whose only occurrences are older
    than the expiry window, even before run_expiry() is called."""
    # Ingest one event backdated far beyond the retention window.
    old_event = {**REAL_NPE_ALLAY, 'server_id': 'ancient-server', 'timestamp_ms': 1000}
    fresh_api.ingest_event(parse_event(old_event))
    # Ingest a recent event on a different server.
    recent_event = {**REAL_NPE_ALLAY, 'server_id': 'recent-server'}
    fp, _ = fresh_api.ingest_event(parse_event(recent_event))
    details = fresh_api.get_group_details(fp)
    assert 'recent-server' in details.servers_affected
    assert 'ancient-server' not in details.servers_affected


# ===========================================================================
# Cause chain handling
# ===========================================================================

def test_cause_chain_is_parsed():
    cause = {
        'class_name': 'java.io.IOException',
        'message': 'disk full',
        'frames': [{'class_name': 'java.io.FileOutputStream', 'method': 'write',
                    'file': 'FileOutputStream.java', 'line': 100, 'location': None}],
        'cause': None,
    }
    event = {**EXAMPLE_EVENT, 'exception': {**EXAMPLE_EVENT['exception'], 'cause': cause}}
    parsed = parse_event(event)
    assert parsed.exception.cause is not None
    assert parsed.exception.cause.class_name == 'java.io.IOException'


def test_different_cause_chains_same_top_level_same_group(fresh_api):
    """The cause chain must not influence the fingerprint — only the top-level
    exception class, message, and frames matter."""
    cause_a = {
        'class_name': 'java.io.IOException',
        'message': 'network error',
        'frames': [{'class_name': 'java.io.InputStream', 'method': 'read',
                    'file': 'InputStream.java', 'line': 10, 'location': None}],
        'cause': None,
    }
    cause_b = {
        'class_name': 'java.sql.SQLException',
        'message': 'query failed',
        'frames': [{'class_name': 'java.sql.Connection', 'method': 'prepareStatement',
                    'file': 'Connection.java', 'line': 55, 'location': None}],
        'cause': None,
    }
    event_a = {**EXAMPLE_EVENT, 'exception': {**EXAMPLE_EVENT['exception'], 'cause': cause_a}}
    event_b = {**EXAMPLE_EVENT, 'exception': {**EXAMPLE_EVENT['exception'], 'cause': cause_b}}
    fp_a, _ = fresh_api.ingest_event(parse_event(event_a))
    fp_b, _ = fresh_api.ingest_event(parse_event(event_b))
    assert fp_a == fp_b


# ===========================================================================
# canonical_trace completeness
# ===========================================================================

def test_canonical_trace_contains_all_frames(fresh_api):
    """canonical_trace must include the full stack, not just the fingerprint frames."""
    fp, _ = fresh_api.ingest_event(parse_event(REAL_ILLEGAL_STATE_ASYNC_SOUND))
    details = fresh_api.get_group_details(fp)
    expected_count = len(REAL_ILLEGAL_STATE_ASYNC_SOUND['exception']['frames'])
    assert len(details.canonical_trace) == expected_count


def test_canonical_frames_limited_to_config_count(fresh_api):
    """canonical_frames must be at most fingerprint_frame_count (default 3)."""
    fp, _ = fresh_api.ingest_event(parse_event(REAL_ILLEGAL_STATE_ASYNC_SOUND))
    details = fresh_api.get_group_details(fp)
    assert len(details.canonical_frames) <= 3
