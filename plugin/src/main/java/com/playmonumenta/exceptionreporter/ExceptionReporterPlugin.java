package com.playmonumenta.exceptionreporter;

import java.lang.reflect.Field;
import java.net.InetAddress;
import java.net.UnknownHostException;
import org.apache.logging.log4j.Level;
import org.apache.logging.log4j.LogManager;
import org.apache.logging.log4j.core.LoggerContext;
import org.apache.logging.log4j.core.config.Configuration;
import org.bukkit.Bukkit;
import org.bukkit.command.CommandMap;
import org.bukkit.plugin.java.JavaPlugin;
import org.jetbrains.annotations.Nullable;

public class ExceptionReporterPlugin extends JavaPlugin {
	private @Nullable ExceptionAppender mAppender;
	private @Nullable HttpSender mSender;

	@Override
	public void onEnable() {
		String ingestUrl = System.getenv("EXCEPTLOG_INGEST_URL");
		String rawServerName = System.getenv("EXCEPTLOG_SERVER_NAME");

		getLogger().info("  EXCEPTLOG_INGEST_URL=" + (ingestUrl != null ? ingestUrl : "(not set)"));
		getLogger().info("  EXCEPTLOG_SERVER_NAME=" + (rawServerName != null ? rawServerName : "(not set, will use hostname)"));

		if (ingestUrl == null || ingestUrl.isBlank()) {
			getLogger().severe("EXCEPTLOG_INGEST_URL env var not set — exception reporting disabled.");
			return;
		}
		try {
			new java.net.URI(ingestUrl);
		} catch (java.net.URISyntaxException e) {
			getLogger().severe("EXCEPTLOG_INGEST_URL is not a valid URI: " + e.getMessage());
			return;
		}

		String serverName = rawServerName;
		if (serverName == null || serverName.isBlank()) {
			try {
				serverName = InetAddress.getLocalHost().getHostName();
			} catch (UnknownHostException e) {
				serverName = "unknown";
			}
		}

		mSender = new HttpSender(ingestUrl, getLogger());
		mAppender = new ExceptionAppender(serverName, mSender);
		mAppender.start();

		LoggerContext ctx = (LoggerContext) LogManager.getContext(false);
		Configuration cfg = ctx.getConfiguration();
		cfg.addAppender(mAppender);
		cfg.getRootLogger().addAppender(mAppender, Level.ERROR, null);
		ctx.updateLoggers();

		try {
			Field commandMapField = Bukkit.getServer().getClass().getDeclaredField("commandMap");
			commandMapField.setAccessible(true);
			CommandMap commandMap = (CommandMap) commandMapField.get(Bukkit.getServer());
			commandMap.register("excepttest", new TestExceptionCommand(serverName, mSender));
		} catch (NoSuchFieldException | IllegalAccessException e) {
			getLogger().warning("Failed to register /excepttest command: " + e.getMessage());
		}

		getLogger().info("  Started — server=" + serverName + " url=" + ingestUrl);
	}

	@Override
	public void onDisable() {
		if (mAppender != null) {
			LoggerContext ctx = (LoggerContext) LogManager.getContext(false);
			Configuration cfg = ctx.getConfiguration();
			cfg.getRootLogger().removeAppender("MonumentaExceptionReporter");
			ctx.updateLoggers();
			mAppender.stop();
			mAppender = null;
		}
		if (mSender != null) {
			mSender.shutdown();
			mSender = null;
		}
	}
}
