package com.playmonumenta.exceptionreporter;

import java.util.List;
import org.bukkit.command.Command;
import org.bukkit.command.CommandSender;

class ExceptionVerboseCommand extends Command {

	ExceptionVerboseCommand() {
		super("exceptverbose",
			"Toggle verbose exception-logging output",
			"/exceptverbose",
			List.of());
		setPermission("monumenta.exceptverbose");
	}

	@Override
	public boolean execute(CommandSender sender, String label, String[] args) {
		if (!testPermission(sender)) {
			return true;
		}

		ExceptionReporterPlugin.verbose = !ExceptionReporterPlugin.verbose;
		sender.sendMessage("exception-logging verbose=" + ExceptionReporterPlugin.verbose);
		return true;
	}
}
