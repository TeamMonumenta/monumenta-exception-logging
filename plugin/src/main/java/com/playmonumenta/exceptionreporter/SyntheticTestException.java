// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (C) 2026 Byron Marohn

package com.playmonumenta.exceptionreporter;

/** Marker exception thrown by {@code /excepttest} to exercise the real appender pipeline. */
class SyntheticTestException extends RuntimeException {
	SyntheticTestException(String message) {
		super(message);
	}
}
