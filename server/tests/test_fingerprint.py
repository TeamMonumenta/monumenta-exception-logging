# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Byron Marohn
"""
Unit tests for tracker/fingerprint.py.

All functions are pure/stateless so no DB setup is needed.
Real exception messages and frame data are drawn from production server logs.
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from tracker.fingerprint import normalize_message, extract_app_frames, compute_fingerprint

_APP_PACKAGES = ['com.playmonumenta']

# ---------------------------------------------------------------------------
# Real messages from production logs
# ---------------------------------------------------------------------------

# From ring_latest.log — Java 17+ "helpful NPE" with class names in double quotes.
_NPE_HELPFUL_MSG = (
    'Cannot invoke "org.bukkit.entity.Allay.teleport(org.bukkit.Location)"'
    ' because "this.this$0.mBoss" is null'
)

# From forum_latest.log — boss loading error with a long tag list containing
# nested brackets, single-quoted entity name, and numeric parameters.
_BOSS_ENTITY_MSG = (
    "boss_generictarget only works on mobs! Entity name='Lunar Omen',"
    " tags=[boss_facing[prefertarget=true,target=[PLAYER,30.0,limit=(1,CLOSER),"
    "filters=[NOT_STEALTHED],tags=[]]],boss_onhit[sound=[(ITEM_TOTEM_USE,1,0.8),"
    "(BLOCK_RESPAWN_ANCHOR_CHARGE,2,0.4)]],boss_limited_lifespan,"
    "boss_lacerate[slashcolorinner=#bf52ff,slashcolorouter=#4fe9f7]]"
)

# ---------------------------------------------------------------------------
# Real frame lists from production logs
# ---------------------------------------------------------------------------

# ring_latest.log — IllegalStateException: Asynchronous play sound!
# com.playmonumenta frames appear at index 3 onward (after 3 spigot frames).
_ASYNC_SOUND_FRAMES = [
    {'class_name': 'org.spigotmc.AsyncCatcher', 'method': 'catchOp'},
    {'class_name': 'org.bukkit.craftbukkit.v1_20_R3.CraftWorld', 'method': 'playSound'},
    {'class_name': 'org.bukkit.craftbukkit.v1_20_R3.CraftWorld', 'method': 'playSound'},
    {'class_name': 'com.playmonumenta.plugins.hunts.bosses.spells.SpellMagmaticConvergence$2',
     'method': 'lambda$run$3'},
    {'class_name': 'com.playmonumenta.plugins.particle.PPParametric', 'method': 'doSpawn'},
    {'class_name': 'com.playmonumenta.plugins.particle.AbstractPartialParticle',
     'method': 'spawnForPlayerInternal'},
    {'class_name': 'com.playmonumenta.plugins.particle.AbstractPartialParticle',
     'method': 'lambda$spawnForPlayersInternal$0'},
    {'class_name': 'java.util.ArrayList', 'method': 'forEach'},
    {'class_name': 'com.playmonumenta.plugins.particle.AbstractPartialParticle',
     'method': 'lambda$spawnForPlayersInternal$1'},
    {'class_name': 'com.playmonumenta.plugins.particle.ParticleManager',
     'method': 'lambda$runOffMainThread$0'},
    {'class_name': 'com.playmonumenta.plugins.particle.ParticleManager$WrappedRunnable',
     'method': 'run'},
    {'class_name': 'java.util.concurrent.ThreadPoolExecutor', 'method': 'runWorker'},
    {'class_name': 'java.lang.Thread', 'method': 'run'},
]

# isles_latest.log — ConcurrentModificationException from TABIntegration.
# Only the com.playmonumenta frames are in _APP_PACKAGES; TAB and JDK frames are not.
_CME_TAB_FRAMES = [
    {'class_name': 'java.util.WeakHashMap$HashIterator', 'method': 'nextEntry'},
    {'class_name': 'java.util.WeakHashMap$KeyIterator', 'method': 'next'},
    {'class_name': 'java.util.Collection', 'method': 'removeIf'},
    {'class_name': 'java.util.Collections$SetFromMap', 'method': 'removeIf'},
    {'class_name': 'me.neznamy.tab.shared.features.PlaceholderManagerImpl',
     'method': 'lambda$removeUsedPlaceholder$9'},
    {'class_name': 'me.neznamy.tab.shared.features.layout.LayoutManagerImpl',
     'method': 'sendLayout'},
    {'class_name': 'com.playmonumenta.plugins.integrations.TABIntegration',
     'method': 'refreshOnlinePlayer'},
    {'class_name': 'com.playmonumenta.plugins.integrations.TABIntegration', 'method': 'refresh'},
    {'class_name': 'com.playmonumenta.plugins.integrations.TABIntegration',
     'method': 'onRefreshRequest'},
    {'class_name': 'java.lang.Thread', 'method': 'run'},
]

# Stack with no com.playmonumenta frames at all.
_NO_APP_FRAMES = [
    {'class_name': 'java.util.WeakHashMap$HashIterator', 'method': 'nextEntry'},
    {'class_name': 'java.util.WeakHashMap$KeyIterator', 'method': 'next'},
    {'class_name': 'java.util.Collection', 'method': 'removeIf'},
    {'class_name': 'java.lang.Thread', 'method': 'run'},
]


# ===========================================================================
# normalize_message
# ===========================================================================

class TestNormalizeMessage:
    def test_uuid_replaced(self):
        msg = 'Player 550e8400-e29b-41d4-a716-446655440000 disconnected'
        result = normalize_message(msg)
        assert '<uuid>' in result
        assert '550e8400' not in result

    def test_uuid_case_insensitive(self):
        msg = 'Entity 550E8400-E29B-41D4-A716-446655440000 logged in'
        result = normalize_message(msg)
        assert '<uuid>' in result

    def test_ip_replaced(self):
        msg = 'Connection from 192.168.1.100 refused'
        result = normalize_message(msg)
        assert '<ip>' in result
        assert '192.168.1.100' not in result

    def test_long_number_replaced(self):
        # Task IDs, entity IDs, etc. — 4+ digit numbers common in error messages.
        msg = 'Task #2748835 for Plugin generated an exception'
        result = normalize_message(msg)
        assert '<N>' in result
        assert '2748835' not in result

    def test_short_numbers_not_replaced(self):
        # 1–3 digit numbers should remain as-is.
        msg = 'Error at slot 3 with 12 items and 999 stacks'
        result = normalize_message(msg)
        assert '3' in result
        assert '12' in result
        assert '999' in result
        assert '<N>' not in result

    def test_single_quoted_string_replaced(self):
        msg = "Entity name='Lunar Omen', something else"
        result = normalize_message(msg)
        assert '<str>' in result
        assert 'Lunar Omen' not in result

    def test_double_quoted_class_name_replaced(self):
        # Java 17+ NPE messages embed class/field names in double quotes.
        result = normalize_message(_NPE_HELPFUL_MSG)
        assert 'org.bukkit.entity.Allay.teleport' not in result
        assert 'this.this$0.mBoss' not in result
        assert '<str>' in result
        # Plain text portions survive.
        assert 'Cannot invoke' in result
        assert 'because' in result
        assert 'is null' in result

    def test_bracket_data_replaced(self):
        msg = 'tags=[boss_projectile] active'
        result = normalize_message(msg)
        assert '<data>' in result
        assert 'boss_projectile' not in result

    def test_no_change_to_plain_message(self):
        # Message with none of the trigger patterns — should be unchanged.
        msg = 'Tried to find the union of an empty list of hitboxes!'
        assert normalize_message(msg) == msg

    def test_different_entity_names_produce_same_normalized_form(self):
        # Two boss loading errors that differ only in the entity name should
        # normalize to the same string and thus fingerprint to the same group.
        msg1 = "boss_generictarget only works on mobs! Entity name='Lunar Omen', tags=[boss_projectile]"
        msg2 = "boss_generictarget only works on mobs! Entity name='Solar Guardian', tags=[boss_projectile]"
        assert normalize_message(msg1) == normalize_message(msg2)

    def test_boss_entity_message_normalizes_entity_name(self):
        result = normalize_message(_BOSS_ENTITY_MSG)
        assert 'Lunar Omen' not in result
        assert '<str>' in result
        assert 'boss_generictarget only works on mobs!' in result

    def test_empty_string(self):
        assert normalize_message('') == ''

    def test_idempotent(self):
        # Normalizing an already-normalized string should be a no-op.
        first = normalize_message(_NPE_HELPFUL_MSG)
        second = normalize_message(first)
        assert first == second

    def test_bare_uuid_replaced(self):
        msg = ('Failed to read from https://sessionserver.mojang.com/session/minecraft/profile/'
               '3601df3d96f54dc1b10b8a4ebcefd210?unsigned=false due to Read timed out')
        result = normalize_message(msg)
        assert '<uuid>' in result
        assert '3601df3d96f54dc1b10b8a4ebcefd210' not in result

    def test_bare_uuid_requires_word_boundary(self):
        # 33-char hex — _BARE_UUID_RE must not fire (no word boundary after char 32).
        # The whole 33-char token is caught by _LONG_TOKEN_RE instead.
        msg = 'token=3601df3d96f54dc1b10b8a4ebcefd210x end'
        result = normalize_message(msg)
        assert '<uuid>' not in result
        assert '<id>' in result

    def test_long_opaque_token_replaced(self):
        token = '20260317T210746Z-r1db49788ddzpxgmhC1YTOzpg800000001fg000000006ta5'
        msg = f'<span>{token}</span>'
        result = normalize_message(msg)
        assert '<id>' in result
        assert token not in result

    def test_short_alphanumeric_not_replaced(self):
        # 20-char token — under the 32-char threshold, must not be replaced.
        msg = 'error code ABC12345678901234567890 end'
        result = normalize_message(msg)
        assert 'ABC12345678901234567890' in result
        assert '<id>' not in result

    def test_bad1_pair_same_fingerprint(self):
        # Two Mojang auth timeouts differing only in the unhyphenated player UUID.
        msg1 = ('Failed to read from https://sessionserver.mojang.com/session/minecraft/profile/'
                '3601df3d96f54dc1b10b8a4ebcefd210?unsigned=false due to Read timed out')
        msg2 = ('Failed to read from https://sessionserver.mojang.com/session/minecraft/profile/'
                'ab7cb32502bc45048c6a45ca7f170dad?unsigned=false due to Read timed out')
        assert normalize_message(msg1) == normalize_message(msg2)

    def test_world_distance_normalized(self):
        msg = 'Cannot measure distance between plot3769 and plot4763'
        result = normalize_message(msg)
        assert 'plot3769' not in result
        assert 'plot4763' not in result
        assert '<world1>' in result
        assert '<world2>' in result
        assert result == 'Cannot measure distance between <world1> and <world2>'

    def test_world_distance_different_worlds_same_normalized(self):
        msg1 = 'Cannot measure distance between plot3769 and plot4763'
        msg2 = 'Cannot measure distance between ring101 and plot9999'
        assert normalize_message(msg1) == normalize_message(msg2)

    def test_bad2_pair_same_fingerprint(self):
        # Two CDN/WAF block pages differing only in the opaque correlation ID.
        msg1 = 'request blocked, ref: 20260317T210746Z-r1db49788ddzpxgmhC1YTOzpg800000001fg000000006ta5'
        msg2 = 'request blocked, ref: 20260317T210746Z-r1db49788dd97n9rhC1YTOp5cs00000001tg00000000fh40'
        assert normalize_message(msg1) == normalize_message(msg2)

    def test_particle_count_normalized(self):
        msg = 'PartialParticle (Type: DUST_COLOR_TRANSITION, Count: 1, ...) error'
        result = normalize_message(msg)
        assert 'Count: <N>' in result
        assert 'Count: 1' not in result

    def test_particle_count_large_number_normalized(self):
        msg = 'PartialParticle (Type: DUST_COLOR_TRANSITION, Count: 12345, ...) error'
        result = normalize_message(msg)
        assert 'Count: <N>' in result
        assert '12345' not in result

    def test_location_block_normalized(self):
        msg = ('PartialParticle (Type: DUST_COLOR_TRANSITION, Count: 1, '
               'Location: "Location{world=CraftWorld{name=quests},x=123456.789,y=37.5678,z=-456789.123,pitch=47.1234,yaw=80.5678}") '
               'has the wrong data type!')
        result = normalize_message(msg)
        assert 'Location{<location>}' in result
        assert 'quests' not in result
        assert 'x=' not in result

    def test_particle_pair_same_fingerprint(self):
        # Two PartialParticle errors differing only in coordinates/count should normalize identically.
        msg1 = ('PartialParticle (Type: DUST_COLOR_TRANSITION, Count: 1, '
                'Location: "Location{world=CraftWorld{name=quests},x=12345.6789,y=37.5678,z=-45678.9012,pitch=47.1234,yaw=80.5678}") '
                'has the wrong data type! (Requires: DustTransition, Got: null)')
        msg2 = ('PartialParticle (Type: DUST_COLOR_TRANSITION, Count: 1, '
                'Location: "Location{world=CraftWorld{name=quests},x=12345.6789,y=37.5678,z=-45678.9012,pitch=-11.2345,yaw=184.5678}") '
                'has the wrong data type! (Requires: DustTransition, Got: null)')
        assert normalize_message(msg1) == normalize_message(msg2)

    def test_location_block_nested_braces_handled(self):
        # The Location block contains a nested brace (CraftWorld{name=...}) — must not truncate early.
        msg = 'err Location{world=CraftWorld{name=foo},x=1.0,y=2.0} done'
        result = normalize_message(msg)
        assert 'Location{<location>}' in result
        assert 'foo' not in result
        assert 'done' in result

    def test_contraction_apostrophe_not_treated_as_quote(self):
        # "Can't" — the apostrophe after 'n' is a contraction, not a quote delimiter.
        # It must not consume text up to the next single-quote.
        msg = "Can't unload world 'plot6771' because there are still players in it (HackJoinWorldFix check)"
        result = normalize_message(msg)
        assert "Can't" in result
        assert 'plot6771' not in result
        assert '<str>' in result

    def test_world_unload_different_plots_same_normalized(self):
        # Two "Can't unload world" messages differing only in the plot number must
        # normalize identically so they land in the same exception group.
        msg1 = "Can't unload world 'plot6771' because there are still players in it (HackJoinWorldFix check)"
        msg2 = "Can't unload world 'plot1979' because there are still players in it (HackJoinWorldFix check)"
        assert normalize_message(msg1) == normalize_message(msg2)


# ===========================================================================
# extract_app_frames
# ===========================================================================

class TestExtractAppFrames:
    def test_skips_non_app_frames_at_top(self):
        # The first three frames in _ASYNC_SOUND_FRAMES are spigot/bukkit, not
        # com.playmonumenta.  They must be skipped.
        result = extract_app_frames(_ASYNC_SOUND_FRAMES, _APP_PACKAGES, 3)
        assert all(f['class_name'].startswith('com.playmonumenta') for f in result)
        assert result[0]['class_name'] == (
            'com.playmonumenta.plugins.hunts.bosses.spells.SpellMagmaticConvergence$2'
        )

    def test_count_respected(self):
        result = extract_app_frames(_ASYNC_SOUND_FRAMES, _APP_PACKAGES, 3)
        assert len(result) == 3

    def test_count_respected_when_fewer_app_frames_exist(self):
        frames = [
            {'class_name': 'com.playmonumenta.plugins.Foo', 'method': 'bar'},
            {'class_name': 'java.lang.Thread', 'method': 'run'},
        ]
        result = extract_app_frames(frames, _APP_PACKAGES, 3)
        assert len(result) == 1
        assert result[0]['class_name'] == 'com.playmonumenta.plugins.Foo'

    def test_app_frames_mid_stack_are_found(self):
        # CME from TAB: com.playmonumenta frames are at position 6+ in the stack.
        result = extract_app_frames(_CME_TAB_FRAMES, _APP_PACKAGES, 3)
        assert all(f['class_name'].startswith('com.playmonumenta') for f in result)
        assert result[0]['class_name'] == (
            'com.playmonumenta.plugins.integrations.TABIntegration'
        )
        assert result[0]['method'] == 'refreshOnlinePlayer'

    def test_falls_back_to_first_n_when_no_app_frames(self):
        result = extract_app_frames(_NO_APP_FRAMES, _APP_PACKAGES, 3)
        assert len(result) == 3
        assert result[0]['class_name'] == 'java.util.WeakHashMap$HashIterator'

    def test_empty_frames_returns_empty(self):
        assert extract_app_frames([], _APP_PACKAGES, 3) == []

    def test_multiple_app_packages(self):
        frames = [
            {'class_name': 'com.example.Foo', 'method': 'bar'},
            {'class_name': 'com.playmonumenta.Plugin', 'method': 'handle'},
            {'class_name': 'java.lang.Thread', 'method': 'run'},
        ]
        result = extract_app_frames(frames, ['com.playmonumenta', 'com.example'], 3)
        assert len(result) == 2
        assert result[0]['class_name'] == 'com.example.Foo'


# ===========================================================================
# compute_fingerprint
# ===========================================================================

class TestComputeFingerprint:
    def test_returns_64_char_sha256_hex(self):
        fp = compute_fingerprint('java.lang.Exception', 'some error', [])
        assert len(fp) == 64
        assert all(c in '0123456789abcdef' for c in fp)

    def test_stability(self):
        frames = [{'class_name': 'com.playmonumenta.Foo', 'method': 'bar'}]
        fp1 = compute_fingerprint('java.lang.Exception', 'same error', frames)
        fp2 = compute_fingerprint('java.lang.Exception', 'same error', frames)
        assert fp1 == fp2

    def test_different_class_different_fingerprint(self):
        frames = [{'class_name': 'com.playmonumenta.Foo', 'method': 'bar'}]
        fp1 = compute_fingerprint('java.lang.NullPointerException', 'msg', frames)
        fp2 = compute_fingerprint('java.lang.IllegalArgumentException', 'msg', frames)
        assert fp1 != fp2

    def test_different_message_different_fingerprint(self):
        frames = [{'class_name': 'com.playmonumenta.Foo', 'method': 'bar'}]
        fp1 = compute_fingerprint('java.lang.Exception', 'error A', frames)
        fp2 = compute_fingerprint('java.lang.Exception', 'error B', frames)
        assert fp1 != fp2

    def test_different_class_name_in_frames_different_fingerprint(self):
        fp1 = compute_fingerprint('java.lang.Exception', 'msg',
                                   [{'class_name': 'com.playmonumenta.Foo', 'method': 'bar'}])
        fp2 = compute_fingerprint('java.lang.Exception', 'msg',
                                   [{'class_name': 'com.playmonumenta.Baz', 'method': 'bar'}])
        assert fp1 != fp2

    def test_different_method_in_frames_different_fingerprint(self):
        fp1 = compute_fingerprint('java.lang.Exception', 'msg',
                                   [{'class_name': 'com.playmonumenta.Foo', 'method': 'bar'}])
        fp2 = compute_fingerprint('java.lang.Exception', 'msg',
                                   [{'class_name': 'com.playmonumenta.Foo', 'method': 'qux'}])
        assert fp1 != fp2

    def test_line_number_does_not_affect_fingerprint(self):
        # A minor code edit that shifts line numbers must not create a new group.
        frames_v1 = [{'class_name': 'com.playmonumenta.Foo', 'method': 'bar', 'line': 42}]
        frames_v2 = [{'class_name': 'com.playmonumenta.Foo', 'method': 'bar', 'line': 99}]
        fp1 = compute_fingerprint('java.lang.Exception', 'same msg', frames_v1)
        fp2 = compute_fingerprint('java.lang.Exception', 'same msg', frames_v2)
        assert fp1 == fp2

    def test_empty_frames_is_stable(self):
        fp1 = compute_fingerprint('java.lang.Exception', 'msg', [])
        fp2 = compute_fingerprint('java.lang.Exception', 'msg', [])
        assert fp1 == fp2

    def test_real_npe_fingerprint_stable(self):
        # Verify that the real NPE message normalizes and fingerprints consistently.
        normalized = normalize_message(_NPE_HELPFUL_MSG)
        top_frames = extract_app_frames(
            [
                {'class_name': 'com.playmonumenta.plugins.abilities.cleric.seraph.KeeperVirtue$1',
                 'method': 'run'},
                {'class_name': 'org.bukkit.craftbukkit.v1_20_R3.scheduler.CraftTask',
                 'method': 'run'},
                {'class_name': 'java.lang.Thread', 'method': 'run'},
            ],
            _APP_PACKAGES, 3
        )
        fp1 = compute_fingerprint('java.lang.NullPointerException', normalized, top_frames)
        fp2 = compute_fingerprint('java.lang.NullPointerException', normalized, top_frames)
        assert fp1 == fp2
        assert len(fp1) == 64

    def test_real_illegal_state_fingerprint_uses_app_frames(self):
        # extract_app_frames skips spigot frames; fingerprint must reflect that.
        normalized = normalize_message('Asynchronous play sound!')
        top_frames_full = extract_app_frames(_ASYNC_SOUND_FRAMES, _APP_PACKAGES, 3)
        top_frames_first3 = _ASYNC_SOUND_FRAMES[:3]  # spigot frames only

        fp_app = compute_fingerprint('java.lang.IllegalStateException', normalized, top_frames_full)
        fp_noapp = compute_fingerprint('java.lang.IllegalStateException', normalized,
                                       top_frames_first3)
        # The two fingerprints differ because their frame sets differ.
        assert fp_app != fp_noapp
