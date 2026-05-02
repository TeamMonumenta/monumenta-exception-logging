// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (C) 2026 Byron Marohn

package com.playmonumenta.exceptionreporter;

import java.io.IOException;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.RejectedExecutionException;
import java.util.concurrent.TimeUnit;
import java.util.logging.Logger;

class HttpSender {
	private final String mIngestUrl;
	private final Logger mLogger;
	private final HttpClient mHttpClient;
	private final ExecutorService mExecutor;

	HttpSender(String ingestUrl, Logger logger) {
		mIngestUrl = ingestUrl;
		mLogger = logger;
		mExecutor = Executors.newSingleThreadExecutor(r -> {
			Thread t = new Thread(r, "ExceptionReporter-HttpSender");
			t.setDaemon(true);
			return t;
		});
		mHttpClient = HttpClient.newHttpClient();
	}

	void send(EventPayload payload) {
		String json = payload.toJson();
		try {
			mExecutor.submit(() -> {
				try {
					HttpRequest request = HttpRequest.newBuilder()
						.uri(URI.create(mIngestUrl))
						.header("Content-Type", "application/json")
						.POST(HttpRequest.BodyPublishers.ofString(json))
						.build();
					HttpResponse<Void> response = mHttpClient.send(request, HttpResponse.BodyHandlers.discarding());
					if (response.statusCode() != 204) {
						mLogger.warning("Ingest returned HTTP " + response.statusCode());
					} else if (ExceptionReporterPlugin.verbose) {
						mLogger.info("[verbose] exception event POST'd — HTTP 204");
					}
				} catch (InterruptedException e) {
					Thread.currentThread().interrupt();
				} catch (IOException e) {
					mLogger.warning("Failed to send exception event: " + e.getMessage());
				}
			});
		} catch (RejectedExecutionException ignored) {
			// Shutting down; drop the event
		}
	}

	void shutdown() {
		mExecutor.shutdown();
		try {
			if (!mExecutor.awaitTermination(3, TimeUnit.SECONDS)) {
				mExecutor.shutdownNow();
			}
		} catch (InterruptedException e) {
			mExecutor.shutdownNow();
			Thread.currentThread().interrupt();
		}
	}
}
