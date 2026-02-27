package com.playmonumenta.exceptionreporter;

import java.util.List;
import org.apache.logging.log4j.LogManager;
import org.apache.logging.log4j.Logger;
import org.bukkit.command.Command;
import org.bukkit.command.CommandSender;

class TestExceptionCommand extends Command {
	private static final Logger LOGGER = LogManager.getLogger(TestExceptionCommand.class);

	TestExceptionCommand() {
		super("excepttest",
			"Send a synthetic test exception to the ingest service",
			"/excepttest",
			List.of());
		setPermission("monumenta.excepttest");
	}

	@Override
	public boolean execute(CommandSender sender, String label, String[] args) {
		if (!testPermission(sender)) {
			return true;
		}

		SyntheticTestException e = new SyntheticTestException(
			"Synthetic test exception from /excepttest command");
		if (ExceptionReporterPlugin.verbose) {
			LOGGER.info("[verbose] /excepttest throwing synthetic exception through appender pipeline");
		}
		LOGGER.error("Synthetic test exception from /excepttest command", e);
		sender.sendMessage("Test exception dispatched through exception pipeline");
		return true;
	}
}
