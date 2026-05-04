# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Byron Marohn
"""
Tests for synthetic MemoryLeakException events produced by heap-logger.

These events use JVM slash-notation class names (e.g. com/playmonumenta/...) in frames
because heaptool outputs raw internal class names.  APP_PACKAGES is configured with dot
notation (com.playmonumenta), so no frames match and extract_app_frames falls back to the
first three frames for fingerprinting.
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest
from tracker.config import TrackerConfig
from tracker.api import Tracker
from tracker.ingest import parse_event
from tests.fixtures import HEAP_LEAK_CRAFT_PLAYER


@pytest.fixture
def fresh_api():
    return Tracker(TrackerConfig(db_path=':memory:'))


# ===========================================================================
# Basic ingestion
# ===========================================================================

def test_heap_leak_ingest_creates_group(fresh_api):
    fp, is_new = fresh_api.ingest_event(parse_event(HEAP_LEAK_CRAFT_PLAYER))
    assert is_new
    assert len(fp) == 64
    details = fresh_api.get_group_details(fp)
    assert details.exception_class == 'com.playmonumenta.memoryleak.MemoryLeakException'
    assert details.status == 'active'


def test_heap_leak_message_template_normalizes_numbers(fresh_api):
    # All numbers are replaced with <N>, including the version in the JVM class path and
    # the instance count.  The slash class name structure is otherwise preserved verbatim.
    fp, _ = fresh_api.ingest_event(parse_event(HEAP_LEAK_CRAFT_PLAYER))
    details = fresh_api.get_group_details(fp)
    assert details.message_template == (
        'Leaked: org/bukkit/craftbukkit/v<N>_<N>_R<N>/entity/CraftPlayer x <N>'
    )


# ===========================================================================
# Grouping
# ===========================================================================

def test_heap_leak_same_pattern_different_servers_group_together(fresh_api):
    event_a = {**HEAP_LEAK_CRAFT_PLAYER, 'server_id': 'survival-0'}
    event_b = {**HEAP_LEAK_CRAFT_PLAYER, 'server_id': 'ring-0'}
    fp_a, _ = fresh_api.ingest_event(parse_event(event_a))
    fp_b, _ = fresh_api.ingest_event(parse_event(event_b))
    assert fp_a == fp_b
    details = fresh_api.get_group_details(fp_a)
    assert details.total_count == 2
    assert sorted(details.servers_affected) == ['ring-0', 'survival-0']


def test_heap_leak_different_leaked_class_different_group(fresh_api):
    other_leak = {
        **HEAP_LEAK_CRAFT_PLAYER,
        'exception': {
            **HEAP_LEAK_CRAFT_PLAYER['exception'],
            'message': 'Leaked: com/playmonumenta/plugins/SomeOtherManager x 50',
            'frames': [
                {'class_name': 'com/playmonumenta/plugins/SomeOtherManager',
                 'method': '<ref>', 'file': None, 'line': -1, 'location': None},
            ],
        },
    }
    fp_craft_player, _ = fresh_api.ingest_event(parse_event(HEAP_LEAK_CRAFT_PLAYER))
    fp_other, _ = fresh_api.ingest_event(parse_event(other_leak))
    assert fp_craft_player != fp_other


# ===========================================================================
# Frame matching: slash notation vs dot notation
# ===========================================================================

def test_heap_leak_slash_frames_not_matched_as_app_frames(fresh_api):
    # APP_PACKAGES defaults to ['com.playmonumenta'].  Frames use slash notation
    # (com/playmonumenta/...) so none match, and extract_app_frames falls back to
    # the first three frames of the chain.
    fp, _ = fresh_api.ingest_event(parse_event(HEAP_LEAK_CRAFT_PLAYER))
    details = fresh_api.get_group_details(fp)
    assert len(details.canonical_frames) == 3
    assert details.canonical_frames[0].class_name == (
        'org/bukkit/craftbukkit/v1_20_R3/entity/CraftPlayer'
    )
    assert details.canonical_frames[1].class_name == 'com/playmonumenta/plugins/SomeManager$1'
    assert details.canonical_frames[2].class_name == (
        'org/bukkit/craftbukkit/v1_20_R3/scheduler/CraftTask'
    )
    # None of the canonical frames start with the dot-notation app prefix.
    assert not any(
        f.class_name.startswith('com.playmonumenta') for f in details.canonical_frames
    )


# ===========================================================================
# Count normalization
# ===========================================================================

def test_heap_leak_large_count_normalized_groups_different_counts(fresh_api):
    # All numbers are replaced by <N>, so two dumps of the same bug with
    # different counts map to the same fingerprint.
    event_10k = {
        **HEAP_LEAK_CRAFT_PLAYER,
        'server_id': 'survival-0',
        'exception': {
            **HEAP_LEAK_CRAFT_PLAYER['exception'],
            'message': 'Leaked: org/bukkit/craftbukkit/v1_20_R3/entity/CraftPlayer x 10000',
        },
    }
    event_20k = {
        **HEAP_LEAK_CRAFT_PLAYER,
        'server_id': 'ring-0',
        'exception': {
            **HEAP_LEAK_CRAFT_PLAYER['exception'],
            'message': 'Leaked: org/bukkit/craftbukkit/v1_20_R3/entity/CraftPlayer x 20000',
        },
    }
    fp_10k, _ = fresh_api.ingest_event(parse_event(event_10k))
    fp_20k, _ = fresh_api.ingest_event(parse_event(event_20k))
    assert fp_10k == fp_20k
    details = fresh_api.get_group_details(fp_10k)
    assert '<N>' in details.message_template


def test_heap_leak_small_count_normalized_groups_together(fresh_api):
    # All numbers are normalized to <N>, so dumps of the same bug with different
    # instance counts (small or large) always map to the same fingerprint.
    event_a = {
        **HEAP_LEAK_CRAFT_PLAYER,
        'exception': {
            **HEAP_LEAK_CRAFT_PLAYER['exception'],
            'message': 'Leaked: org/bukkit/craftbukkit/v1_20_R3/entity/CraftPlayer x 173',
        },
    }
    event_b = {
        **HEAP_LEAK_CRAFT_PLAYER,
        'exception': {
            **HEAP_LEAK_CRAFT_PLAYER['exception'],
            'message': 'Leaked: org/bukkit/craftbukkit/v1_20_R3/entity/CraftPlayer x 174',
        },
    }
    fp_a, _ = fresh_api.ingest_event(parse_event(event_a))
    fp_b, _ = fresh_api.ingest_event(parse_event(event_b))
    assert fp_a == fp_b
