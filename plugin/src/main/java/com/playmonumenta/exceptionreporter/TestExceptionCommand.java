package com.playmonumenta.exceptionreporter;

import java.util.List;
import java.util.concurrent.ThreadLocalRandom;
import java.util.logging.Logger;
import org.bukkit.command.Command;
import org.bukkit.command.CommandSender;

class TestExceptionCommand extends Command {
	private static final String[] FAKE_METHODS = {
		"handlePlayerAction", "processInventoryClick", "updateEntityAI",
		"scheduleTick", "broadcastPacket", "validateChunkData",
		"applyBlockUpdate", "resolveEntityTarget", "flushPendingEvents"
	};

	private final String mServerId;
	private final HttpSender mSender;
	private final Logger mLogger;

	TestExceptionCommand(String serverId, HttpSender sender, Logger logger) {
		super("excepttest",
			"Send a synthetic test exception to the ingest service",
			"/excepttest",
			List.of());
		setPermission("monumenta.excepttest");
		mServerId = serverId;
		mSender = sender;
		mLogger = logger;
	}

	@Override
	public boolean execute(CommandSender sender, String label, String[] args) {
		if (!testPermission(sender)) {
			return true;
		}

		ThreadLocalRandom rng = ThreadLocalRandom.current();
		int randomId = rng.nextInt(100_000, 999_999);
		String method = FAKE_METHODS[rng.nextInt(FAKE_METHODS.length)];
		int line = rng.nextInt(50, 800);

		String exceptionClass = "com.playmonumenta.test.SyntheticException" + randomId;
		String throwerClass = "com.playmonumenta.test.TestExceptionThrower" + randomId;
		String exceptionMessage = "Test exception #" + randomId + " triggered via /excepttest";

		EventPayload.FrameData frame = new EventPayload.FrameData(
			throwerClass,
			method,
			"TestExceptionThrower.java",
			line,
			null
		);

		EventPayload.ExceptionData exceptionData = new EventPayload.ExceptionData(
			exceptionClass,
			exceptionMessage,
			List.of(frame),
			null
		);

		EventPayload payload = new EventPayload(
			mServerId,
			System.currentTimeMillis(),
			"ERROR",
			throwerClass,
			Thread.currentThread().getName(),
			"Synthetic test exception from /excepttest command",
			exceptionData
		);

		if (ExceptionReporterPlugin.verbose) {
			mLogger.info("[verbose] /excepttest dispatching synthetic exception: "
				+ exceptionClass + "." + method + ":" + line);
		}
		mSender.send(payload);
		sender.sendMessage("Test exception sent to ingest (class=" + exceptionClass + ", method=" + method + ", line=" + line + ")");
		return true;
	}
}
