package com.playmonumenta.exceptionreporter;

import com.google.gson.FieldNamingPolicy;
import com.google.gson.Gson;
import com.google.gson.GsonBuilder;
import java.util.List;
import org.jetbrains.annotations.Nullable;

class EventPayload {
	private static final Gson GSON = new GsonBuilder()
		.setFieldNamingPolicy(FieldNamingPolicy.LOWER_CASE_WITH_UNDERSCORES)
		.create();

	final int schemaVersion = 1;
	final String serverId;
	final long timestampMs;
	final String level;
	final String logger;
	final String thread;
	final String message;
	final ExceptionData exception;

	EventPayload(String serverId, long timestampMs, String level, String logger, String thread, String message, ExceptionData exception) {
		this.serverId = serverId;
		this.timestampMs = timestampMs;
		this.level = level;
		this.logger = logger;
		this.thread = thread;
		this.message = message;
		this.exception = exception;
	}

	String toJson() {
		return GSON.toJson(this);
	}

	static class ExceptionData {
		final String className;
		final @Nullable String message;
		final List<FrameData> frames;
		final @Nullable ExceptionData cause;

		ExceptionData(String className, @Nullable String message, List<FrameData> frames, @Nullable ExceptionData cause) {
			this.className = className;
			this.message = message;
			this.frames = frames;
			this.cause = cause;
		}
	}

	static class FrameData {
		final String className;
		final String method;
		final @Nullable String file;
		final int line;
		final @Nullable String location;

		FrameData(String className, String method, @Nullable String file, int line, @Nullable String location) {
			this.className = className;
			this.method = method;
			this.file = file;
			this.line = line;
			this.location = location;
		}
	}
}
