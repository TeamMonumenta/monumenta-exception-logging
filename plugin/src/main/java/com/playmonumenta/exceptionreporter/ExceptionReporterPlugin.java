// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (C) 2026 Byron Marohn

package com.playmonumenta.exceptionreporter;

import java.lang.reflect.Field;
import java.net.InetAddress;
import java.net.UnknownHostException;
import org.apache.logging.log4j.LogManager;
import org.bukkit.Bukkit;
import org.bukkit.command.CommandMap;
import org.bukkit.plugin.java.JavaPlugin;
import org.jetbrains.annotations.Nullable;

public class ExceptionReporterPlugin extends JavaPlugin {
	public static volatile boolean verbose = false;

	private @Nullable ExceptionAppender mAppender;
	private @Nullable HttpSender mSender;
	private @Nullable HeapDumpIntegration mHeapDumpIntegration;

	@Override
	public void onEnable() {
		String ingestUrl = System.getenv("EXCEPTLOG_INGEST_URL");
		String rawServerName = System.getenv("EXCEPTLOG_SERVER_NAME");
		String rawVerbose = System.getenv("EXCEPTLOG_VERBOSE");
		String heaplogUrl = System.getenv("HEAPLOG_INGEST_URL");
		String rawAutoHeapDump = System.getenv("HEAPLOG_AUTO_DUMP");
		boolean autoHeapDump = rawAutoHeapDump != null && !rawAutoHeapDump.isBlank() && !rawAutoHeapDump.equalsIgnoreCase("false");

		verbose = rawVerbose != null && !rawVerbose.isBlank() && !rawVerbose.equalsIgnoreCase("false");

		getLogger().info("  EXCEPTLOG_INGEST_URL=" + (ingestUrl != null ? ingestUrl : "(not set)"));
		getLogger().info("  EXCEPTLOG_SERVER_NAME=" + (rawServerName != null ? rawServerName : "(not set, will use hostname)"));
		getLogger().info("  EXCEPTLOG_VERBOSE=" + (rawVerbose != null ? rawVerbose : "(not set)") + " → verbose=" + verbose);
		getLogger().info("  HEAPLOG_INGEST_URL=" + (heaplogUrl != null ? heaplogUrl : "(not set)"));
		getLogger().info("  HEAPLOG_AUTO_DUMP=" + (rawAutoHeapDump != null ? rawAutoHeapDump : "(not set)") + " → autoHeapDump=" + autoHeapDump);

		String serverName = rawServerName;
		if (serverName == null || serverName.isBlank()) {
			try {
				serverName = InetAddress.getLocalHost().getHostName();
			} catch (UnknownHostException e) {
				serverName = "unknown";
			}
		}

		if (ingestUrl == null || ingestUrl.isBlank()) {
			getLogger().warning("EXCEPTLOG_INGEST_URL not set — exception reporting disabled.");
		} else {
			try {
				new java.net.URI(ingestUrl);
			} catch (java.net.URISyntaxException e) {
				getLogger().severe("EXCEPTLOG_INGEST_URL is not a valid URI: " + e.getMessage());
				ingestUrl = null;
			}
		}

		if (ingestUrl != null) {
			mSender = new HttpSender(ingestUrl, getLogger());
			mAppender = new ExceptionAppender(serverName, mSender);
			mAppender.start();

			// Attach directly to the core root Logger so the appender receives events from
			// Paper's own logging context regardless of which classloader is calling.
			// LogManager.getContext(false) can return a child context when called from a
			// plugin classloader, causing the appender to miss server-level ERROR events.
			((org.apache.logging.log4j.core.Logger) LogManager.getRootLogger()).addAppender(mAppender);
		}

		if (heaplogUrl != null && !heaplogUrl.isBlank() && ingestUrl != null) {
			HeapDumpIntegration heapDump = new HeapDumpIntegration(serverName, heaplogUrl, ingestUrl, getLogger(), this);
			mHeapDumpIntegration = heapDump;
			getLogger().info("  HeapDump integration active (log watcher enabled)");
			if (autoHeapDump) {
				tryRegisterAutoTrigger(heapDump);
			} else {
				getLogger().info("  HEAPLOG_AUTO_DUMP not set — auto-dump on LowMemoryEvent disabled");
			}
		} else if (heaplogUrl != null && !heaplogUrl.isBlank()) {
			getLogger().warning("HEAPLOG_INGEST_URL is set but EXCEPTLOG_INGEST_URL is not — heap dump integration disabled.");
		}

		try {
			Field commandMapField = Bukkit.getServer().getClass().getDeclaredField("commandMap");
			commandMapField.setAccessible(true);
			CommandMap commandMap = (CommandMap) commandMapField.get(Bukkit.getServer());
			commandMap.register("excepttest", new TestExceptionCommand());
			commandMap.register("exceptverbose", new ExceptionVerboseCommand());
		} catch (NoSuchFieldException | IllegalAccessException e) {
			getLogger().warning("Failed to register commands: " + e.getMessage());
		}

		getLogger().info("  Started: server=" + serverName);
	}

	private void tryRegisterAutoTrigger(HeapDumpIntegration heapDump) {
		try {
			if (Bukkit.getPluginManager().getPlugin("MonumentaNetworkRelay") != null) {
				Bukkit.getPluginManager().registerEvents(new NetworkRelayIntegration(heapDump), this);
				getLogger().info("  Auto-dump on LowMemoryEvent enabled (MonumentaNetworkRelay present)");
			} else {
				getLogger().warning("  HEAPLOG_AUTO_DUMP set but MonumentaNetworkRelay not found — auto-dump disabled");
			}
		} catch (NoClassDefFoundError e) {
			getLogger().warning("  HEAPLOG_AUTO_DUMP set but MonumentaNetworkRelay classes unavailable — auto-dump disabled");
		}
	}

	@Override
	public void onDisable() {
		if (mHeapDumpIntegration != null) {
			mHeapDumpIntegration.shutdown();
			mHeapDumpIntegration = null;
		}
		if (mAppender != null) {
			((org.apache.logging.log4j.core.Logger) LogManager.getRootLogger()).removeAppender(mAppender);
			mAppender.stop();
			mAppender = null;
		}
		if (mSender != null) {
			mSender.shutdown();
			mSender = null;
		}
	}
}
