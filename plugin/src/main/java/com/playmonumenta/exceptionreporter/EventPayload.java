package com.playmonumenta.exceptionreporter;

import com.google.gson.FieldNamingPolicy;
import com.google.gson.Gson;
import com.google.gson.GsonBuilder;
import java.util.List;

class EventPayload {
	private static final Gson GSON = new GsonBuilder()
		.setFieldNamingPolicy(FieldNamingPolicy.LOWER_CASE_WITH_UNDERSCORES)
		.create();

	final int schemaVersion = 1;
	String serverId;
	long timestampMs;
	String level;
	String logger;
	String thread;
	String message;
	ExceptionData exception;

	String toJson() {
		return GSON.toJson(this);
	}

	static class ExceptionData {
		String className;
		String message;
		List<FrameData> frames;
		ExceptionData cause;
	}

	static class FrameData {
		String className;
		String method;
		String file;
		int line;
		String location;
	}
}
