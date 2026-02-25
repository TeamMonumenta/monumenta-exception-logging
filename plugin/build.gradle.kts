import net.minecrell.pluginyml.bukkit.BukkitPluginDescription

plugins {
	id("com.playmonumenta.gradle-config") version "3+"
}

dependencies {
	compileOnly(libs.gson)
	compileOnly(libs.log4jCore)
}

monumenta {
	name("MonumentaExceptionReporter")
	id("MonumentaExceptionReporter")
	paper(
		"com.playmonumenta.exceptionreporter.ExceptionReporterPlugin",
		BukkitPluginDescription.PluginLoadOrder.STARTUP,
		"1.20.4",
		apiJarVersion = "1.20.4-R0.1-SNAPSHOT"
	)
}
