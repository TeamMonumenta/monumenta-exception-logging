// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (C) 2026 Byron Marohn

package com.playmonumenta.exceptionreporter;

import com.google.gson.Gson;
import java.io.IOException;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.logging.Logger;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import org.apache.logging.log4j.LogManager;
import org.apache.logging.log4j.core.LogEvent;
import org.apache.logging.log4j.core.appender.AbstractAppender;
import org.apache.logging.log4j.core.config.Property;
import org.bukkit.Bukkit;

class HeapDumpIntegration {
	// Matches spark's confirmed heap dump completion line.
	// Spark logs: "[⚡] Heap dump written to: plugins/spark/heap-2026-04-30_23.26.24.hprof"
	private static final Pattern HEAP_DUMP_WRITTEN = Pattern.compile("Heap dump written to: (\\S+\\.hprof)");

	// Literal substring that must appear in every message HEAP_DUMP_WRITTEN can match.
	// Checked before the regex so the expensive pattern is never run on the vast majority
	// of log messages. String.contains uses vectorized (SIMD) search on modern JVMs.
	private static final String HEAP_DUMP_FAST_PATH = "Heap dump written";

	// Used only for safe JSON string escaping of arbitrary path/URL values.
	private static final Gson GSON = new Gson();

	// 30 minutes in ticks. Heap dumps on large (32 GB+) heaps can be slow.
	private static final long COMMAND_TIMEOUT_TICKS = 30 * 60 * 20L;

	private final String mServerId;
	private final String mHeaplogUrl;
	private final String mExceptlogUrl;
	private final Logger mLogger;
	private final org.bukkit.plugin.Plugin mPlugin;
	private final HttpClient mHttpClient;
	private final SparkHeapWatcher mWatcher;

	// True from when triggerDump() dispatches "spark heapdump" until the watcher sees
	// the completion line (or the 30-minute timeout fires). Prevents triggerDump() from
	// issuing a second command while one is already in flight.
	private final AtomicBoolean mDumpInProgress = new AtomicBoolean(false);

	// True while the watcher is running the HTTP notification. Prevents concurrent
	// processing if two completion messages somehow arrive in quick succession.
	private final AtomicBoolean mNotifyInFlight = new AtomicBoolean(false);

	HeapDumpIntegration(String serverId, String heaplogUrl, String exceptlogUrl, Logger logger,
		org.bukkit.plugin.Plugin plugin) {
		mServerId = serverId;
		mHeaplogUrl = heaplogUrl;
		mExceptlogUrl = exceptlogUrl;
		mLogger = logger;
		mPlugin = plugin;
		mHttpClient = HttpClient.newHttpClient();

		// Attach permanently so the watcher catches both LowMemoryEvent-triggered dumps
		// and manually issued `/spark heapdump` commands.
		mWatcher = new SparkHeapWatcher();
		mWatcher.start();
		((org.apache.logging.log4j.core.Logger) LogManager.getRootLogger()).addAppender(mWatcher);
	}

	/** Remove the log watcher. Called from ExceptionReporterPlugin.onDisable(). */
	void shutdown() {
		((org.apache.logging.log4j.core.Logger) LogManager.getRootLogger()).removeAppender(mWatcher);
		mWatcher.stop();
	}

	/**
	 * Dispatch a spark heapdump command. Called from {@link NetworkRelayIntegration} when
	 * auto-dump is enabled and a LowMemoryEvent fires. Must be called on the main thread.
	 */
	void triggerDump() {
		if (!mDumpInProgress.compareAndSet(false, true)) {
			mLogger.warning("[HeapDump] Already in progress — skipping trigger");
			return;
		}

		mLogger.info("[HeapDump] LowMemoryEvent received — triggering spark heapdump");

		// Reset the in-flight guard if spark never logs a completion line (e.g. crash).
		Bukkit.getScheduler().runTaskLaterAsynchronously(mPlugin, () -> {
			if (mDumpInProgress.compareAndSet(true, false)) {
				mLogger.warning("[HeapDump] Command timed out — spark never logged an .hprof path");
			}
		}, COMMAND_TIMEOUT_TICKS);

		Bukkit.getServer().dispatchCommand(Bukkit.getServer().getConsoleSender(), "spark heapdump");
	}

	private void onHeapDumpReady(String path) {
		mLogger.info("[HeapDump] Spark complete, path=" + path + " — notifying heap-logger at " + mHeaplogUrl);

		// Build JSON manually so we avoid a naming-convention conflict between the
		// project's m-prefix field style and Gson's snake_case serialization.
		String json = "{\"heapdump_path\":" + GSON.toJson(path)
			+ ",\"exception_logger_url\":" + GSON.toJson(mExceptlogUrl)
			+ ",\"server_id\":" + GSON.toJson(mServerId) + "}";

		Thread sender = new Thread(() -> {
			try {
				HttpRequest request = HttpRequest.newBuilder()
					.uri(URI.create(mHeaplogUrl))
					.header("Content-Type", "application/json")
					.POST(HttpRequest.BodyPublishers.ofString(json))
					.build();
				HttpResponse<Void> response = mHttpClient.send(request, HttpResponse.BodyHandlers.discarding());
				if (response.statusCode() == 202) {
					mLogger.info("[HeapDump] heap-logger accepted the request (HTTP 202)");
				} else {
					mLogger.warning("[HeapDump] heap-logger returned unexpected HTTP " + response.statusCode());
				}
			} catch (InterruptedException e) {
				Thread.currentThread().interrupt();
			} catch (IOException e) {
				mLogger.warning("[HeapDump] Failed to notify heap-logger: " + e.getMessage());
			} finally {
				mNotifyInFlight.set(false);
			}
		}, "ExceptionReporter-HeapNotify");
		sender.setDaemon(true);
		sender.start();
	}

	// Lightweight appender permanently attached to the root logger. Processes heap dump
	// completion messages whether triggered by LowMemoryEvent or by a manual command.
	// Optimized for the hot path: the vast majority of log messages are rejected by a
	// single String.contains check before the regex is ever evaluated.
	private class SparkHeapWatcher extends AbstractAppender {
		SparkHeapWatcher() {
			super("MonumentaSparkHeapWatcher", null, null, true, Property.EMPTY_ARRAY);
		}

		@Override
		public void append(LogEvent event) {
			// Fast path: avoids the regex for the overwhelming majority of messages.
			// String.indexOf (used by contains) is SIMD-accelerated on modern JVMs and
			// orders of magnitude cheaper than Matcher.find().
			String msg = event.getMessage().getFormattedMessage();
			if (!msg.contains(HEAP_DUMP_FAST_PATH)) {
				return;
			}
			Matcher m = HEAP_DUMP_WRITTEN.matcher(msg);
			if (!m.find()) {
				return;
			}
			String path = m.group(1);
			// Prevent concurrent processing if two completion messages arrive at once.
			if (!mNotifyInFlight.compareAndSet(false, true)) {
				mLogger.warning("[HeapDump] Concurrent completion detected — ignoring duplicate");
				return;
			}
			// Release the LowMemory dispatch guard so future events can trigger again.
			mDumpInProgress.set(false);
			onHeapDumpReady(path);
		}
	}
}
