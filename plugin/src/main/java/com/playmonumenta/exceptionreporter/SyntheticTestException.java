package com.playmonumenta.exceptionreporter;

/** Marker exception thrown by {@code /excepttest} to exercise the real appender pipeline. */
class SyntheticTestException extends RuntimeException {
	SyntheticTestException(String message) {
		super(message);
	}
}
