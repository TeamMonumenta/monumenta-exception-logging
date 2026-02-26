package com.playmonumenta.exceptionreporter;

import java.net.InetAddress;
import java.net.UnknownHostException;
import org.apache.logging.log4j.Level;
import org.apache.logging.log4j.LogManager;
import org.apache.logging.log4j.core.LoggerContext;
import org.apache.logging.log4j.core.config.Configuration;
import org.bukkit.plugin.java.JavaPlugin;

public class ExceptionReporterPlugin extends JavaPlugin {
	private ExceptionAppender mAppender;
	private HttpSender mSender;

	@Override
	public void onEnable() {
		String ingestUrl = System.getenv("EXCEPTLOG_INGEST_URL");
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

		String serverName = System.getenv("EXCEPTLOG_SERVER_NAME");
		if (serverName == null || serverName.isBlank()) {
			try {
				serverName = InetAddress.getLocalHost().getHostName();
			} catch (UnknownHostException e) {
				serverName = "unknown";
			}
		}

		mSender = new HttpSender(ingestUrl, getLogger());
		mAppender = new ExceptionAppender(serverName, mSender, getLogger());
		mAppender.start();

		LoggerContext ctx = (LoggerContext) LogManager.getContext(false);
		Configuration cfg = ctx.getConfiguration();
		cfg.addAppender(mAppender);
		cfg.getRootLogger().addAppender(mAppender, Level.ERROR, null);
		ctx.updateLoggers();

		getLogger().info("Started — server=" + serverName + " url=" + ingestUrl);
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
