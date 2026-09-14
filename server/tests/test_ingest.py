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


# ===========================================================================
# Log-event context: level, accompanying log message, cause chain
#
# All three arrive in the wire payload (PROTOCOL.md) and were validated then
# discarded before this was added, which is what made a scheduler exception's
# real cause invisible in the database even though it was being sent.
# ===========================================================================

# A cause-swallowing wrapper of the shape Paper's scheduler produces: the wrapper's
# own frames are scheduler machinery, and the real bug is only in the cause.
_WRAPPED_EVENT = {
    'schema_version': 1,
    'server_id': 'valley-2',
    'timestamp_ms': 1789339168000,
    'level': 'WARN',
    'logger': 'Monumenta',
    'thread': 'Server thread',
    'message': 'Task #9498967 for Monumenta v11.84.2 generated an exception',
    'exception': {
        'class_name': 'com.destroystokyo.paper.exception.ServerSchedulerException',
        'message': 'Task #9498967 for Monumenta v11.84.2 generated an exception',
        'frames': [
            {'class_name': 'org.bukkit.craftbukkit.v1_20_R3.scheduler.CraftScheduler',
             'method': 'mainThreadHeartbeat', 'file': 'CraftScheduler.java',
             'line': 497, 'location': None},
        ],
        'cause': {
            'class_name': 'java.lang.IllegalArgumentException',
            'message': 'World unloaded',
            'frames': [
                {'class_name': 'com.playmonumenta.plugins.depths.DepthsUtils$1',
                 'method': 'run', 'file': 'DepthsUtils.java', 'line': 258,
                 'location': 'Monumenta.jar'},
            ],
            'cause': {
                'class_name': 'java.lang.NullPointerException',
                'message': None,
                'frames': [],
                'cause': None,
            },
        },
    },
}


def test_level_is_persisted(fresh_api):
    fp, _ = fresh_api.ingest_event(parse_event(_WRAPPED_EVENT))
    assert fresh_api.get_group_details(fp).level == 'WARN'


def test_log_message_is_persisted_normalized_and_raw(fresh_api):
    fp, _ = fresh_api.ingest_event(parse_event(_WRAPPED_EVENT))
    details = fresh_api.get_group_details(fp)
    # Normalized on the group, raw on the occurrence - mirroring how the
    # exception's own message is stored. Asserted on the task id rather than the
    # whole string so this does not also pin how versions normalize.
    assert details.log_message_template.startswith('Task #<N> for Monumenta')
    assert '9498967' not in details.log_message_template
    assert details.latest_log_message == \
        'Task #9498967 for Monumenta v11.84.2 generated an exception'
    assert fresh_api.get_recent_occurrences(fp)[0].log_message == \
        'Task #9498967 for Monumenta v11.84.2 generated an exception'


def test_cause_chain_is_persisted_outermost_first(fresh_api):
    fp, _ = fresh_api.ingest_event(parse_event(_WRAPPED_EVENT))
    chain = fresh_api.get_group_details(fp).cause_chain
    # The wrapper itself is not in the chain - only what it wrapped.
    assert [c.class_name for c in chain] == [
        'java.lang.IllegalArgumentException', 'java.lang.NullPointerException',
    ]
    assert chain[0].message == 'World unloaded'
    assert chain[0].frames[0].class_name == 'com.playmonumenta.plugins.depths.DepthsUtils$1'
    assert chain[0].frames[0].line == 258
    # A null message normalizes to empty rather than None, matching message_template.
    assert chain[1].message == ''


def test_cause_chain_is_empty_when_there_is_no_cause(fresh_api):
    fp, _ = fresh_api.ingest_event(parse_event(REAL_NPE_ALLAY))
    assert fresh_api.get_group_details(fp).cause_chain == []


def test_log_context_does_not_affect_the_fingerprint(fresh_api):
    """level/log message/cause are context, not identity.

    Two events identical except for these fields must stay one group, or every
    scheduler task id would create a new one.
    """
    import copy  # pylint: disable=import-outside-toplevel
    other = copy.deepcopy(_WRAPPED_EVENT)
    other['level'] = 'ERROR'
    other['message'] = 'Task #11111 for Monumenta v11.84.2 generated an exception'
    other['exception']['cause'] = None
    fp_a, new_a = fresh_api.ingest_event(parse_event(_WRAPPED_EVENT))
    fp_b, new_b = fresh_api.ingest_event(parse_event(other))
    assert fp_a == fp_b
    assert new_a is True and new_b is False


def test_group_context_is_captured_from_the_first_occurrence_only(fresh_api):
    """Matches how logger and canonical_trace already behave."""
    import copy  # pylint: disable=import-outside-toplevel
    later = copy.deepcopy(_WRAPPED_EVENT)
    later['level'] = 'ERROR'
    later['exception']['cause'] = None
    fp, _ = fresh_api.ingest_event(parse_event(_WRAPPED_EVENT))
    fresh_api.ingest_event(parse_event(later))
    details = fresh_api.get_group_details(fp)
    assert details.level == 'WARN'
    assert len(details.cause_chain) == 2


def _create_pre_migration_db(db_path: str) -> None:
    """Create the two tables in their shape before the log-context columns existed.

    Written out rather than derived from the current schema, so the test keeps
    describing the old deployment even as the current schema moves on.
    _create_tables uses CREATE TABLE IF NOT EXISTS, so it leaves these alone and
    fills in the rest. has_activity is omitted too, exercising the oldest
    migration entry as well as the new ones.
    """
    import sqlite3  # pylint: disable=import-outside-toplevel

    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE error_groups (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            fingerprint        TEXT NOT NULL UNIQUE,
            exception_class    TEXT NOT NULL,
            message_template   TEXT NOT NULL,
            canonical_frames   TEXT NOT NULL,
            canonical_trace    TEXT NOT NULL,
            logger             TEXT NOT NULL,
            first_seen         INTEGER NOT NULL,
            last_seen          INTEGER NOT NULL,
            total_count        INTEGER NOT NULL DEFAULT 0,
            status             TEXT NOT NULL DEFAULT 'active'
                               CHECK (status IN ('active', 'muted', 'resolved')),
            discord_message_id TEXT,
            muted_by           TEXT,
            muted_at           INTEGER,
            resolved_by        TEXT,
            resolved_at        INTEGER
        );
        CREATE TABLE occurrences (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id   INTEGER NOT NULL REFERENCES error_groups(id) ON DELETE CASCADE,
            server     TEXT NOT NULL,
            timestamp  INTEGER NOT NULL,
            message    TEXT NOT NULL
        );
    """)
    conn.commit()
    conn.close()


def test_migration_adds_columns_to_a_pre_existing_database(tmp_path):
    """An existing deployment's DB predates these columns and cannot be backfilled.

    Opening it must add them with defaults rather than failing, and ingest must
    work afterwards.
    """
    db_path = str(tmp_path / 'old.db')
    _create_pre_migration_db(db_path)

    api = Tracker(TrackerConfig(db_path=db_path))
    fp, is_new = api.ingest_event(parse_event(_WRAPPED_EVENT))
    assert is_new is True
    details = api.get_group_details(fp)
    assert details.level == 'WARN'
    assert len(details.cause_chain) == 2
    api.close()

    # Reopening an already-migrated DB is a no-op and must not raise. Ingesting
    # again exercises the UPDATE path, and so the migrated has_activity column.
    api2 = Tracker(TrackerConfig(db_path=db_path))
    _, is_new_again = api2.ingest_event(parse_event(_WRAPPED_EVENT))
    assert is_new_again is False
    assert api2.get_group_details(fp).total_count == 2
    api2.close()


def test_migration_leaves_pre_existing_rows_readable(tmp_path):
    """A row written before the columns existed must read back through the API.

    The data was discarded at ingest time, so it cannot be backfilled with
    anything real - it must come back empty rather than NULL-crashing callers.
    """
    import sqlite3  # pylint: disable=import-outside-toplevel

    db_path = str(tmp_path / 'rows.db')
    _create_pre_migration_db(db_path)

    # A group and occurrence written by the old code, with no knowledge of the
    # new columns.
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO error_groups (fingerprint, exception_class, message_template, "
        "canonical_frames, canonical_trace, logger, first_seen, last_seen, total_count, "
        "status) VALUES (?, 'java.lang.RuntimeException', 'old', '[]', '[]', 'Monumenta', "
        "1700000000, 1700000000, 1, 'active')",
        ('f' * 64,)
    )
    conn.execute("INSERT INTO occurrences (group_id, server, timestamp, message) "
                 "VALUES (1, 'valley', 1700000000, 'old raw')")
    conn.commit()
    conn.close()

    api = Tracker(TrackerConfig(db_path=db_path))
    details = api.get_group_details('f' * 64)
    assert details.level == ''
    assert details.log_message_template == ''
    assert not details.cause_chain
    assert details.latest_log_message == ''
    assert api.get_recent_occurrences('f' * 64)[0].log_message == ''
    # And the pre-existing row still takes updates, exercising has_activity too.
    api.ingest_event(parse_event(REAL_NPE_ALLAY))
    api.close()


def test_cause_chain_depth_is_capped(fresh_api):
    """/ingest is unauthenticated, so the plugin's depth budget is not trusted."""
    import copy  # pylint: disable=import-outside-toplevel
    from tracker.ingest import MAX_CAUSE_DEPTH  # pylint: disable=import-outside-toplevel

    deep = copy.deepcopy(_WRAPPED_EVENT)
    node = deep['exception']
    for i in range(MAX_CAUSE_DEPTH + 10):
        node['cause'] = {'class_name': f'java.lang.Nested{i}', 'message': None,
                         'frames': [], 'cause': None}
        node = node['cause']
    fp, _ = fresh_api.ingest_event(parse_event(deep))
    assert len(fresh_api.get_group_details(fp).cause_chain) == MAX_CAUSE_DEPTH


def test_cause_chain_frames_are_capped(fresh_api):
    import copy  # pylint: disable=import-outside-toplevel
    from tracker.ingest import MAX_CAUSE_FRAMES  # pylint: disable=import-outside-toplevel

    wide = copy.deepcopy(_WRAPPED_EVENT)
    wide['exception']['cause']['frames'] = [
        {'class_name': f'com.example.C{i}', 'method': 'run', 'file': None,
         'line': -1, 'location': None}
        for i in range(MAX_CAUSE_FRAMES * 3)
    ]
    fp, _ = fresh_api.ingest_event(parse_event(wide))
    assert len(fresh_api.get_group_details(fp).cause_chain[0].frames) == MAX_CAUSE_FRAMES
