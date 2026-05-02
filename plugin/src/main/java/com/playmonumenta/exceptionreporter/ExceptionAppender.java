// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (C) 2026 Byron Marohn

package com.playmonumenta.exceptionreporter;

import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.ThreadLocalRandom;
import java.util.concurrent.atomic.AtomicInteger;
import org.apache.logging.log4j.LogManager;
import org.apache.logging.log4j.Logger;
import org.apache.logging.log4j.core.Appender;
import org.apache.logging.log4j.core.Core;
import org.apache.logging.log4j.core.LogEvent;
import org.apache.logging.log4j.core.appender.AbstractAppender;
import org.apache.logging.log4j.core.config.Property;
import org.apache.logging.log4j.core.config.plugins.Plugin;
import org.jetbrains.annotations.Nullable;

@Plugin(name = "MonumentaExceptionReporter", category = Core.CATEGORY_NAME, elementType = Appender.ELEMENT_TYPE)
public class ExceptionAppender extends AbstractAppender {
	private static final Logger LOGGER = LogManager.getLogger(ExceptionAppender.class);
	private static final int MAX_EVENTS_PER_SECOND = 20;
	private static final int MAX_CAUSE_DEPTH = 5;

	private final AtomicInteger mEventsThisSecond = new AtomicInteger(0);
	private final String mServerId;
	private final HttpSender mSender;
	private volatile @Nullable Thread mRateLimitThread;

	protected ExceptionAppender(String serverId, HttpSender sender) {
		super("MonumentaExceptionReporter", null, null, true, Property.EMPTY_ARRAY);
		mServerId = serverId;
		mSender = sender;
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
		if (ExceptionReporterPlugin.verbose) {
			LOGGER.info("[verbose] queuing exception: {} : {}", thrown.getClass().getName(), thrown.getMessage());
		}
		mSender.send(buildPayload(event, thrown));
	}

	private EventPayload buildPayload(LogEvent event, Throwable thrown) {
		return new EventPayload(
			mServerId,
			event.getTimeMillis(),
			event.getLevel().name(),
			event.getLoggerName(),
			event.getThreadName(),
			event.getMessage().getFormattedMessage(),
			buildExceptionData(thrown, MAX_CAUSE_DEPTH)
		);
	}

	private static EventPayload.ExceptionData buildExceptionData(Throwable t, int depthRemaining) {
		// /excepttest throws a SyntheticTestException so it flows through the real pipeline.
		// Patch the exception class name and top frame here so every invocation produces a
		// unique fingerprint and looks like a distinct exception to the ingest service.
		boolean isSynthetic = t instanceof SyntheticTestException;
		int syntheticId = isSynthetic ? ThreadLocalRandom.current().nextInt(100_000, 1_000_000) : -1;

		StackTraceElement[] elements = t.getStackTrace();
		List<EventPayload.FrameData> frames = new ArrayList<>(elements.length);
		for (int i = 0; i < elements.length; i++) {
			StackTraceElement ste = elements[i];
			String frameClass = (isSynthetic && i == 0)
				? "com.playmonumenta.test.TestExceptionThrower" + syntheticId
				: ste.getClassName();
			int frameLine = (isSynthetic && i == 0)
				? ThreadLocalRandom.current().nextInt(50, 10_000)
				: ste.getLineNumber();
			frames.add(new EventPayload.FrameData(
				frameClass,
				ste.getMethodName(),
				ste.getFileName(),
				frameLine,
				extractLocation(ste)
			));
		}

		String exceptionClassName = isSynthetic
			? "com.playmonumenta.test.SyntheticException" + syntheticId
			: t.getClass().getName();

		@Nullable EventPayload.ExceptionData cause = null;
		if (t.getCause() != null && depthRemaining > 1) {
			cause = buildExceptionData(t.getCause(), depthRemaining - 1);
		}

		return new EventPayload.ExceptionData(
			exceptionClassName,
			t.getMessage(),
			frames,
			cause
		);
	}

	// Extracts the JAR name from the bracket suffix Log4j2 or the JVM appends to
	// StackTraceElement.toString(), e.g. "~[Monumenta.jar:?]" → "Monumenta.jar".
	// Returns null when the location is unavailable ("?") or absent.
	private static @Nullable String extractLocation(StackTraceElement ste) {
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
