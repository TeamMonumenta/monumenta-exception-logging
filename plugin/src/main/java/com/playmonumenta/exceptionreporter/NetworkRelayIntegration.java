// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (C) 2026 Byron Marohn

package com.playmonumenta.exceptionreporter;

import com.playmonumenta.networkrelay.shardhealth.LowMemoryEvent;
import org.bukkit.event.EventHandler;
import org.bukkit.event.EventPriority;
import org.bukkit.event.Listener;

/**
 * Bukkit listener that triggers an automatic heap dump on LowMemoryEvent.
 * Only instantiated and registered when HEAPLOG_AUTO_DUMP is set and
 * MonumentaNetworkRelay is present on the classpath.
 */
class NetworkRelayIntegration implements Listener {
	private final HeapDumpIntegration mHeapDumpIntegration;

	NetworkRelayIntegration(HeapDumpIntegration heapDumpIntegration) {
		mHeapDumpIntegration = heapDumpIntegration;
	}

	@EventHandler(ignoreCancelled = false, priority = EventPriority.HIGH)
	public void onLowMemory(LowMemoryEvent event) {
		mHeapDumpIntegration.triggerDump();
	}
}
