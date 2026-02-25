package com.playmonumenta.exceptionreporter;

import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.logging.Logger;
import org.apache.logging.log4j.core.Appender;
import org.apache.logging.log4j.core.Core;
import org.apache.logging.log4j.core.LogEvent;
import org.apache.logging.log4j.core.appender.AbstractAppender;
import org.apache.logging.log4j.core.config.Property;
import org.apache.logging.log4j.core.config.plugins.Plugin;

@Plugin(name = "MonumentaExceptionReporter", category = Core.CATEGORY_NAME, elementType = Appender.ELEMENT_TYPE)
public class ExceptionAppender extends AbstractAppender {
	private static final int MAX_EVENTS_PER_SECOND = 20;
	private static final int MAX_CAUSE_DEPTH = 5;

	private final AtomicInteger mEventsThisSecond = new AtomicInteger(0);
	private final String mServerId;
	private final HttpSender mSender;
	private final Logger mPluginLogger;
	private volatile Thread mRateLimitThread;

	protected ExceptionAppender(String serverId, HttpSender sender, Logger pluginLogger) {
		super("MonumentaExceptionReporter", null, null, true, Property.EMPTY_ARRAY);
		mServerId = serverId;
		mSender = sender;
		mPluginLogger = pluginLogger;
	}

	@Override
	public void start() {
		super.start();
		mRateLimitThread = new Thread(() -> {
			while (!Thread.currentThread().isInterrupted()) {
				try {
					Thread.sleep(1000);
				} catch (InterruptedException e) {
					Thread.currentThread().interrupt();
					break;
				}
				mEventsThisSecond.set(0);
			}
		}, "ExceptionReporter-RateLimitReset");
		mRateLimitThread.setDaemon(true);
		mRateLimitThread.start();
	}

	@Override
	public void stop() {
		Thread t = mRateLimitThread;
		if (t != null) {
			t.interrupt();
			mRateLimitThread = null;
		}
		super.stop();
	}

	@Override
	public void append(LogEvent event) {
		Throwable thrown = event.getThrown();
		if (thrown == null) {
			return;
		}
		if (mEventsThisSecond.getAndIncrement() >= MAX_EVENTS_PER_SECOND) {
			return;
		}
		mSender.send(buildPayload(event, thrown));
	}

	private EventPayload buildPayload(LogEvent event, Throwable thrown) {
		EventPayload payload = new EventPayload();
		payload.serverId = mServerId;
		payload.timestampMs = event.getTimeMillis();
		payload.level = event.getLevel().name();
		payload.logger = event.getLoggerName();
		payload.thread = event.getThreadName();
		payload.message = event.getMessage().getFormattedMessage();
		payload.exception = buildExceptionData(thrown, MAX_CAUSE_DEPTH);
		return payload;
	}

	private static EventPayload.ExceptionData buildExceptionData(Throwable t, int depthRemaining) {
		EventPayload.ExceptionData data = new EventPayload.ExceptionData();
		data.className = t.getClass().getName();
		data.message = t.getMessage();

		StackTraceElement[] elements = t.getStackTrace();
		List<EventPayload.FrameData> frames = new ArrayList<>(elements.length);
		for (StackTraceElement ste : elements) {
			EventPayload.FrameData frame = new EventPayload.FrameData();
			frame.className = ste.getClassName();
			frame.method = ste.getMethodName();
			frame.file = ste.getFileName();
			frame.line = ste.getLineNumber();
			frame.location = extractLocation(ste);
			frames.add(frame);
		}
		data.frames = frames;

		Throwable cause = t.getCause();
		if (cause != null && depthRemaining > 1) {
			data.cause = buildExceptionData(cause, depthRemaining - 1);
		}
		return data;
	}

	// Extracts the JAR name from the bracket suffix Log4j2 or the JVM appends to
	// StackTraceElement.toString(), e.g. "~[Monumenta.jar:?]" → "Monumenta.jar".
	// Returns null when the location is unavailable ("?") or absent.
	private static String extractLocation(StackTraceElement ste) {
		String str = ste.toString();
		int bracketStart = str.lastIndexOf('[');
		if (bracketStart == -1) {
			return null;
		}
		int bracketEnd = str.indexOf(']', bracketStart);
		if (bracketEnd == -1) {
			return null;
		}
		String content = str.substring(bracketStart + 1, bracketEnd);
		int colonIdx = content.indexOf(':');
		String jar = colonIdx >= 0 ? content.substring(0, colonIdx) : content;
		return (jar.isEmpty() || jar.equals("?")) ? null : jar;
	}
}
