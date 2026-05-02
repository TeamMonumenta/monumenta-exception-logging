# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Byron Marohn
"""Shared test fixtures and helpers."""

import time

# Use current time so events fall within windowed query results.
# The exception shape mirrors the concrete example in PROTOCOL.md.
_NOW_MS = int(time.time() * 1000)

# ---------------------------------------------------------------------------
# Real production exceptions extracted from server logs
# ---------------------------------------------------------------------------

# From blue_latest.log — NullPointerException with a Java 17+ "helpful NPE"
# message containing double-quoted class and field names.
REAL_NPE_ALLAY = {
    'schema_version': 1,
    'server_id': 'blue',
    'timestamp_ms': _NOW_MS,
    'level': 'WARN',
    'logger': 'com.playmonumenta.plugins.abilities.cleric.seraph.KeeperVirtue',
    'thread': 'Server thread',
    'message': 'Task #736602 for Monumenta v11.55.0 generated an exception',
    'exception': {
        'class_name': 'java.lang.NullPointerException',
        'message': (
            'Cannot invoke "org.bukkit.entity.Allay.teleport(org.bukkit.Location)"'
            ' because "this.this$0.mBoss" is null'
        ),
        'frames': [
            {
                'class_name': 'com.playmonumenta.plugins.abilities.cleric.seraph.KeeperVirtue$1',
                'method': 'run',
                'file': 'KeeperVirtue.java',
                'line': 338,
                'location': 'Monumenta.jar',
            },
            {
                'class_name': 'org.bukkit.craftbukkit.v1_20_R3.scheduler.CraftTask',
                'method': 'run',
                'file': 'CraftTask.java',
                'line': 101,
                'location': 'paper-1.20.4.jar',
            },
            {
                'class_name': 'org.bukkit.craftbukkit.v1_20_R3.scheduler.CraftScheduler',
                'method': 'mainThreadHeartbeat',
                'file': 'CraftScheduler.java',
                'line': 482,
                'location': 'paper-1.20.4.jar',
            },
            {
                'class_name': 'net.minecraft.server.MinecraftServer',
                'method': 'tickChildren',
                'file': 'MinecraftServer.java',
                'line': 1646,
                'location': None,
            },
            {
                'class_name': 'net.minecraft.server.dedicated.DedicatedServer',
                'method': 'tickChildren',
                'file': 'DedicatedServer.java',
                'line': 447,
                'location': None,
            },
            {
                'class_name': 'net.minecraft.server.MinecraftServer',
                'method': 'tickServer',
                'file': 'MinecraftServer.java',
                'line': 1525,
                'location': None,
            },
            {
                'class_name': 'net.minecraft.server.MinecraftServer',
                'method': 'runServer',
                'file': 'MinecraftServer.java',
                'line': 1226,
                'location': None,
            },
            {
                'class_name': 'net.minecraft.server.MinecraftServer',
                'method': 'lambda$spin$0',
                'file': 'MinecraftServer.java',
                'line': 319,
                'location': None,
            },
            {
                'class_name': 'java.lang.Thread',
                'method': 'run',
                'file': 'Thread.java',
                'line': 1583,
                'location': None,
            },
        ],
        'cause': None,
    },
}

# From ring_latest.log — IllegalStateException thrown on an async thread that
# tried to call a main-thread-only API (play sound).  The com.playmonumenta
# frames are NOT at the top of the stack; they appear after spigot frames.
REAL_ILLEGAL_STATE_ASYNC_SOUND = {
    'schema_version': 1,
    'server_id': 'ring',
    'timestamp_ms': _NOW_MS,
    'level': 'ERROR',
    'logger': 'com.playmonumenta.plugins.particle.ParticleManager',
    'thread': 'PartialParticle Thread',
    'message': 'Error executing particle task',
    'exception': {
        'class_name': 'java.lang.IllegalStateException',
        'message': 'Asynchronous play sound!',
        'frames': [
            {
                'class_name': 'org.spigotmc.AsyncCatcher',
                'method': 'catchOp',
                'file': 'AsyncCatcher.java',
                'line': 15,
                'location': 'paper-1.20.4.jar',
            },
            {
                'class_name': 'org.bukkit.craftbukkit.v1_20_R3.CraftWorld',
                'method': 'playSound',
                'file': 'CraftWorld.java',
                'line': 1918,
                'location': 'paper-1.20.4.jar',
            },
            {
                'class_name': 'org.bukkit.craftbukkit.v1_20_R3.CraftWorld',
                'method': 'playSound',
                'file': 'CraftWorld.java',
                'line': 1908,
                'location': 'paper-1.20.4.jar',
            },
            {
                'class_name': 'com.playmonumenta.plugins.hunts.bosses.spells.SpellMagmaticConvergence$2',
                'method': 'lambda$run$3',
                'file': 'SpellMagmaticConvergence.java',
                'line': 151,
                'location': 'Monumenta.jar',
            },
            {
                'class_name': 'com.playmonumenta.plugins.particle.PPParametric',
                'method': 'doSpawn',
                'file': 'PPParametric.java',
                'line': 72,
                'location': 'Monumenta.jar',
            },
            {
                'class_name': 'com.playmonumenta.plugins.particle.AbstractPartialParticle',
                'method': 'spawnForPlayerInternal',
                'file': 'AbstractPartialParticle.java',
                'line': 558,
                'location': 'Monumenta.jar',
            },
            {
                'class_name': 'com.playmonumenta.plugins.particle.AbstractPartialParticle',
                'method': 'lambda$spawnForPlayersInternal$0',
                'file': 'AbstractPartialParticle.java',
                'line': 527,
                'location': 'Monumenta.jar',
            },
            {
                'class_name': 'java.util.ArrayList',
                'method': 'forEach',
                'file': 'ArrayList.java',
                'line': 1596,
                'location': None,
            },
            {
                'class_name': 'com.playmonumenta.plugins.particle.AbstractPartialParticle',
                'method': 'lambda$spawnForPlayersInternal$1',
                'file': 'AbstractPartialParticle.java',
                'line': 526,
                'location': 'Monumenta.jar',
            },
            {
                'class_name': 'com.playmonumenta.plugins.particle.ParticleManager',
                'method': 'lambda$runOffMainThread$0',
                'file': 'ParticleManager.java',
                'line': 53,
                'location': 'Monumenta.jar',
            },
            {
                'class_name': 'com.playmonumenta.plugins.particle.ParticleManager$WrappedRunnable',
                'method': 'run',
                'file': 'ParticleManager.java',
                'line': 66,
                'location': 'Monumenta.jar',
            },
            {
                'class_name': 'java.util.concurrent.Executors$RunnableAdapter',
                'method': 'call',
                'file': 'Executors.java',
                'line': 572,
                'location': None,
            },
            {
                'class_name': 'java.util.concurrent.FutureTask',
                'method': 'run',
                'file': 'FutureTask.java',
                'line': 317,
                'location': None,
            },
            {
                'class_name': 'java.util.concurrent.ScheduledThreadPoolExecutor$ScheduledFutureTask',
                'method': 'run',
                'file': 'ScheduledThreadPoolExecutor.java',
                'line': 304,
                'location': None,
            },
            {
                'class_name': 'java.util.concurrent.ThreadPoolExecutor',
                'method': 'runWorker',
                'file': 'ThreadPoolExecutor.java',
                'line': 1144,
                'location': None,
            },
            {
                'class_name': 'java.util.concurrent.ThreadPoolExecutor$Worker',
                'method': 'run',
                'file': 'ThreadPoolExecutor.java',
                'line': 642,
                'location': None,
            },
            {
                'class_name': 'java.lang.Thread',
                'method': 'run',
                'file': 'Thread.java',
                'line': 1583,
                'location': None,
            },
        ],
        'cause': None,
    },
}

# From isles_latest.log — IllegalArgumentException thrown in a scheduled task.
REAL_ILLEGAL_ARG_HITBOX = {
    'schema_version': 1,
    'server_id': 'isles',
    'timestamp_ms': _NOW_MS,
    'level': 'WARN',
    'logger': 'com.playmonumenta.plugins.bosses.spells.frostgiant.SpellGreatswordSlam',
    'thread': 'Server thread',
    'message': 'Task #2748835 for Monumenta v11.55.0 generated an exception',
    'exception': {
        'class_name': 'java.lang.IllegalArgumentException',
        'message': 'Tried to find the union of an empty list of hitboxes!',
        'frames': [
            {
                'class_name': 'com.playmonumenta.plugins.utils.Hitbox',
                'method': 'unionOf',
                'file': 'Hitbox.java',
                'line': 379,
                'location': 'Monumenta.jar',
            },
            {
                'class_name': 'com.playmonumenta.plugins.utils.Hitbox',
                'method': 'unionOfAABB',
                'file': 'Hitbox.java',
                'line': 389,
                'location': 'Monumenta.jar',
            },
            {
                'class_name': 'com.playmonumenta.plugins.bosses.spells.frostgiant.SpellGreatswordSlam$2$1',
                'method': 'run',
                'file': 'SpellGreatswordSlam.java',
                'line': 223,
                'location': 'Monumenta.jar',
            },
            {
                'class_name': 'org.bukkit.craftbukkit.v1_20_R3.scheduler.CraftTask',
                'method': 'run',
                'file': 'CraftTask.java',
                'line': 101,
                'location': 'paper-1.20.4.jar',
            },
            {
                'class_name': 'org.bukkit.craftbukkit.v1_20_R3.scheduler.CraftScheduler',
                'method': 'mainThreadHeartbeat',
                'file': 'CraftScheduler.java',
                'line': 482,
                'location': 'paper-1.20.4.jar',
            },
            {
                'class_name': 'net.minecraft.server.MinecraftServer',
                'method': 'tickChildren',
                'file': 'MinecraftServer.java',
                'line': 1646,
                'location': None,
            },
            {
                'class_name': 'net.minecraft.server.dedicated.DedicatedServer',
                'method': 'tickChildren',
                'file': 'DedicatedServer.java',
                'line': 447,
                'location': None,
            },
            {
                'class_name': 'net.minecraft.server.MinecraftServer',
                'method': 'tickServer',
                'file': 'MinecraftServer.java',
                'line': 1525,
                'location': None,
            },
            {
                'class_name': 'net.minecraft.server.MinecraftServer',
                'method': 'runServer',
                'file': 'MinecraftServer.java',
                'line': 1226,
                'location': None,
            },
            {
                'class_name': 'net.minecraft.server.MinecraftServer',
                'method': 'lambda$spin$0',
                'file': 'MinecraftServer.java',
                'line': 319,
                'location': None,
            },
            {
                'class_name': 'java.lang.Thread',
                'method': 'run',
                'file': 'Thread.java',
                'line': 1583,
                'location': None,
            },
        ],
        'cause': None,
    },
}

# From isles_latest.log — ConcurrentModificationException from TAB plugin.
# The com.playmonumenta frames appear deep in the stack (positions 11–14),
# after JDK and third-party TAB plugin frames.  Message is null.
REAL_CME_TAB = {
    'schema_version': 1,
    'server_id': 'isles',
    'timestamp_ms': _NOW_MS,
    'level': 'WARN',
    'logger': 'com.playmonumenta.plugins.integrations.TABIntegration',
    'thread': 'Craft Scheduler Thread - 1549 - Monumenta',
    'message': 'Plugin Monumenta v11.55.0 generated an exception while executing task 2751738',
    'exception': {
        'class_name': 'java.util.ConcurrentModificationException',
        'message': None,
        'frames': [
            {
                'class_name': 'java.util.WeakHashMap$HashIterator',
                'method': 'nextEntry',
                'file': 'WeakHashMap.java',
                'line': 815,
                'location': None,
            },
            {
                'class_name': 'java.util.WeakHashMap$KeyIterator',
                'method': 'next',
                'file': 'WeakHashMap.java',
                'line': 848,
                'location': None,
            },
            {
                'class_name': 'java.util.Collection',
                'method': 'removeIf',
                'file': 'Collection.java',
                'line': 583,
                'location': None,
            },
            {
                'class_name': 'java.util.Collections$SetFromMap',
                'method': 'removeIf',
                'file': 'Collections.java',
                'line': 5949,
                'location': None,
            },
            {
                'class_name': 'me.neznamy.tab.shared.features.PlaceholderManagerImpl',
                'method': 'lambda$removeUsedPlaceholder$9',
                'file': 'PlaceholderManagerImpl.java',
                'line': 312,
                'location': 'TAB.jar',
            },
            {
                'class_name': 'java.util.concurrent.ConcurrentHashMap',
                'method': 'computeIfPresent',
                'file': 'ConcurrentHashMap.java',
                'line': 1828,
                'location': None,
            },
            {
                'class_name': 'me.neznamy.tab.shared.features.PlaceholderManagerImpl',
                'method': 'removeUsedPlaceholder',
                'file': 'PlaceholderManagerImpl.java',
                'line': 310,
                'location': 'TAB.jar',
            },
            {
                'class_name': 'me.neznamy.tab.shared.features.PlaceholderManagerImpl',
                'method': 'removeUsedPlaceholder',
                'file': 'PlaceholderManagerImpl.java',
                'line': 322,
                'location': 'TAB.jar',
            },
            {
                'class_name': 'me.neznamy.tab.shared.features.layout.LayoutView',
                'method': 'destroy',
                'file': 'LayoutView.java',
                'line': 111,
                'location': 'TAB.jar',
            },
            {
                'class_name': 'me.neznamy.tab.shared.features.layout.LayoutManagerImpl',
                'method': 'refresh',
                'file': 'LayoutManagerImpl.java',
                'line': 132,
                'location': 'TAB.jar',
            },
            {
                'class_name': 'me.neznamy.tab.shared.features.layout.LayoutManagerImpl',
                'method': 'sendLayout',
                'file': 'LayoutManagerImpl.java',
                'line': 208,
                'location': 'TAB.jar',
            },
            {
                'class_name': 'com.playmonumenta.plugins.integrations.TABIntegration',
                'method': 'refreshOnlinePlayer',
                'file': 'TABIntegration.java',
                'line': 316,
                'location': 'Monumenta.jar',
            },
            {
                'class_name': 'com.playmonumenta.plugins.integrations.TABIntegration',
                'method': 'refresh',
                'file': 'TABIntegration.java',
                'line': 363,
                'location': 'Monumenta.jar',
            },
            {
                'class_name': 'com.playmonumenta.plugins.integrations.TABIntegration',
                'method': 'onRefreshRequest',
                'file': 'TABIntegration.java',
                'line': 338,
                'location': 'Monumenta.jar',
            },
            {
                'class_name': 'com.playmonumenta.plugins.integrations.TABIntegration',
                'method': 'lambda$onRefreshRequest$7',
                'file': 'TABIntegration.java',
                'line': 345,
                'location': 'Monumenta.jar',
            },
            {
                'class_name': 'org.bukkit.craftbukkit.v1_20_R3.scheduler.CraftTask',
                'method': 'run',
                'file': 'CraftTask.java',
                'line': 101,
                'location': 'paper-1.20.4.jar',
            },
            {
                'class_name': 'org.bukkit.craftbukkit.v1_20_R3.scheduler.CraftAsyncTask',
                'method': 'run',
                'file': 'CraftAsyncTask.java',
                'line': 57,
                'location': 'paper-1.20.4.jar',
            },
            {
                'class_name': 'java.util.concurrent.ThreadPoolExecutor',
                'method': 'runWorker',
                'file': 'ThreadPoolExecutor.java',
                'line': 1144,
                'location': None,
            },
            {
                'class_name': 'java.util.concurrent.ThreadPoolExecutor$Worker',
                'method': 'run',
                'file': 'ThreadPoolExecutor.java',
                'line': 642,
                'location': None,
            },
            {
                'class_name': 'java.lang.Thread',
                'method': 'run',
                'file': 'Thread.java',
                'line': 1583,
                'location': None,
            },
        ],
        'cause': None,
    },
}

# Synthetic MemoryLeakException produced by heap-logger from the heaptool
# Class names use JVM slash notation because heaptool outputs raw internal class names.
HEAP_LEAK_CRAFT_PLAYER = {
    'schema_version': 1,
    'server_id': 'survival-0',
    'timestamp_ms': _NOW_MS,
    'level': 'ERROR',
    'logger': 'com.playmonumenta.memoryleak.HeapAnalyzer',
    'thread': 'heap-worker',
    'message': 'Memory leak detected in heap dump',
    'exception': {
        'class_name': 'com.playmonumenta.memoryleak.MemoryLeakException',
        'message': 'Leaked: org/bukkit/craftbukkit/v1_20_R3/entity/CraftPlayer x 173',
        'frames': [
            {'class_name': 'org/bukkit/craftbukkit/v1_20_R3/entity/CraftPlayer',
             'method': '<ref>', 'file': None, 'line': -1, 'location': None},
            {'class_name': 'com/playmonumenta/plugins/SomeManager$1',
             'method': 'val$player', 'file': None, 'line': -1, 'location': None},
            {'class_name': 'org/bukkit/craftbukkit/v1_20_R3/scheduler/CraftTask',
             'method': 'rTask', 'file': None, 'line': -1, 'location': None},
            {'class_name': '[Ljava/lang/Object;[]',
             'method': '<ref>', 'file': None, 'line': -1, 'location': None},
            {'class_name': 'java/util/PriorityQueue',
             'method': 'queue', 'file': None, 'line': -1, 'location': None},
            {'class_name': 'org/bukkit/craftbukkit/v1_20_R3/scheduler/CraftScheduler',
             'method': 'pending', 'file': None, 'line': -1, 'location': None},
        ],
        'cause': None,
    },
}

EXAMPLE_EVENT = {
    'schema_version': 1,
    'server_id': 'survival-0',
    'timestamp_ms': _NOW_MS,
    'level': 'ERROR',
    'logger': 'com.playmonumenta.plugins.Plugin',
    'thread': 'Server thread',
    'message': 'Failed to load boss!',
    'exception': {
        'class_name': 'java.lang.Exception',
        'message': (
            "boss_generictarget only works on mobs! "
            "Entity name='Souls Unleashed', tags=[boss_projectile]"
        ),
        'frames': [
            {
                'class_name': 'com.playmonumenta.plugins.bosses.bosses.GenericTargetBoss',
                'method': '<init>',
                'file': 'GenericTargetBoss.java',
                'line': 34,
                'location': 'Monumenta.jar',
            },
            {
                'class_name': 'com.playmonumenta.plugins.bosses.BossManager',
                'method': 'processEntity',
                'file': 'BossManager.java',
                'line': 1369,
                'location': 'Monumenta.jar',
            },
            {
                'class_name': 'com.playmonumenta.plugins.bosses.BossManager',
                'method': 'creatureSpawnEvent',
                'file': 'BossManager.java',
                'line': 565,
                'location': 'Monumenta.jar',
            },
            {
                'class_name': 'com.destroystokyo.paper.event.executor.asm.generated.GeneratedEventExecutor472',
                'method': 'execute',
                'file': None,
                'line': -1,
                'location': None,
            },
            {
                'class_name': 'java.lang.Thread',
                'method': 'run',
                'file': 'Thread.java',
                'line': 1583,
                'location': None,
            },
        ],
        'cause': None,
    },
}

# A second distinct exception for multi-group tests
EXAMPLE_EVENT_2 = {
    'schema_version': 1,
    'server_id': 'dungeon-0',
    'timestamp_ms': _NOW_MS,
    'level': 'ERROR',
    'logger': 'com.playmonumenta.plugins.Plugin',
    'thread': 'Server thread',
    'message': 'NullPointerException in item handler',
    'exception': {
        'class_name': 'java.lang.NullPointerException',
        'message': None,
        'frames': [
            {
                'class_name': 'com.playmonumenta.plugins.items.ItemHandler',
                'method': 'onInteract',
                'file': 'ItemHandler.java',
                'line': 42,
                'location': 'Monumenta.jar',
            },
            {
                'class_name': 'java.lang.Thread',
                'method': 'run',
                'file': 'Thread.java',
                'line': 1583,
                'location': None,
            },
        ],
        'cause': None,
    },
}
