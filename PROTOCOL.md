# Exception Reporting Protocol

This document defines the JSON message format sent to the Python ingest server's
`POST /ingest` endpoint. The Java plugin is the main producer and the heap-logger service
is a second one (see "Synthetic exceptions from heap-logger" below); the server treats
any sender that can reach the endpoint and POST this shape the same way.

## Transport

- **Method:** `HTTP POST`, `Content-Type: application/json`, one event per request.
  There is no batching.
- **Endpoint:** the full URL of the server's `/ingest` route, read from the
  `EXCEPTLOG_INGEST_URL` environment variable of the Minecraft server process the plugin
  runs in (e.g. `http://exception-tracker.internal/ingest`). If it is unset or not a
  valid URI, the plugin logs a warning and reports nothing.
- **Responses:** `204 No Content` on success. A body that fails validation against the
  schema below gets `400` with `{"error": [...]}` listing the validation errors; a body
  that is not JSON at all gets a plain `400`. The plugin logs a warning for any status
  other than 204 and drops the event.
- **Authentication:** none. The ingest server listens on plain HTTP with no auth.
  Security is provided entirely by the Kubernetes network boundary - the service is not
  exposed outside the cluster. This avoids per-request TLS/crypto overhead and removes
  secret management from the plugin. Because the endpoint is unauthenticated, the server
  enforces its own bounds on what it stores rather than trusting the sender (see
  "Server-side limits" below).
- **Fire-and-forget:** the plugin POSTs from a single daemon thread, never from the
  server thread, and does not retry. A failed send logs one warning and the event is gone.
- **Rate limit:** at most 20 events per second per server process. The counter resets
  every second; events over the limit are dropped silently.

## Message schema

```json
{
  "schema_version": 1,
  "server_id": "string",
  "timestamp_ms": 0,
  "level": "string",
  "logger": "string",
  "thread": "string",
  "message": "string",
  "exception": {
    "class_name": "string",
    "message": "string | null",
    "frames": [
      {
        "class_name": "string",
        "method": "string",
        "file": "string | null",
        "line": 0,
        "location": "string | null"
      }
    ],
    "cause": "{ ...same exception shape... } | null"
  }
}
```

### Top-level fields

| Field | Type | Description |
|---|---|---|
| `schema_version` | integer | Always `1` for this version. Used by the server to handle future format changes. Not stored as a column; it survives inside `occurrences.raw_event`. |
| `server_id` | string | Identifies the originating server. Read from `EXCEPTLOG_SERVER_NAME` at plugin startup; falls back to the hostname, and to `"unknown"` if that cannot be resolved. |
| `timestamp_ms` | integer | Unix timestamp in milliseconds (UTC) when the log event was emitted, from `LogEvent.getTimeMillis()`. The server stores it in seconds, clamped to no more than 5 minutes ahead of its own clock (see `occurrences.timestamp` in SCHEMA.md). |
| `level` | string | Log level of the originating event, e.g. `"WARN"` or `"ERROR"`. The plugin reports everything at or above `EXCEPTLOG_MIN_LEVEL` (default `INFO`) - see "Reporting threshold" in the README. |
| `logger` | string | The Log4j2 logger name that emitted the event. Typically the fully-qualified plugin class name, e.g. `com.playmonumenta.plugins.Plugin`. |
| `thread` | string | Name of the thread that logged the event, e.g. `"Server thread"`, from `LogEvent.getThreadName()`. It distinguishes a main-thread failure from an async one. |
| `message` | string | The human-readable log message that accompanied the exception, e.g. `"Failed to load boss!"` - what was passed to `logger.error(...)`, not the exception's own message. May be an empty string. It often carries context the exception's message does not: Paper's scheduler puts the task id and owning plugin here. |
| `exception` | object | The captured exception. Always present; events without a throwable are filtered out by the plugin before sending. |

### `exception` object

| Field | Type | Description |
|---|---|---|
| `class_name` | string | Fully-qualified exception class name, e.g. `"java.lang.NullPointerException"` or `"com.playmonumenta.plugins.SomeCustomException"`. |
| `message` | string \| null | The exception's own message (`e.getMessage()`). Null if no message was set. May contain variable content (player names, coordinates, etc.); the server normalizes it for fingerprinting. |
| `frames` | array | Ordered list of stack frames, from closest to throw site (index 0) to oldest caller. Every frame of the throwable is sent; the server selects application frames from them when fingerprinting. |
| `cause` | object \| null | The chained cause (`e.getCause()`), in the same shape, recursively. The plugin's depth budget is 5 including the outermost throwable, so a well-behaved payload carries at most 4 causes. |

### Frame object

| Field | Type | Description |
|---|---|---|
| `class_name` | string | Fully-qualified class name, e.g. `"com.playmonumenta.plugins.bosses.bosses.GenericTargetBoss"`. |
| `method` | string | Method name, e.g. `"<init>"`, `"processEntity"`. |
| `file` | string \| null | Source file name, e.g. `"GenericTargetBoss.java"`. Null when compiled without debug info. |
| `line` | integer | Source line number. `-1` if unknown (native methods, or compiled without debug info). |
| `location` | string \| null | JAR file or module the class was loaded from, e.g. `"Monumenta.jar"`, `"paper-1.20.4.jar"`. Derived from the bracketed suffix of `StackTraceElement.toString()`; null when unavailable (`"?"` in the raw output becomes null). Stored on every frame: it is what separates our own code from a third-party plugin's in an otherwise identical-looking trace. |

### Server-side limits

`/ingest` is unauthenticated, so the sender's own budgets are a convention, not a
guarantee. The server applies its own, in `tracker/ingest.py`:

| Limit | Value | Applies to |
|---|---|---|
| `MAX_CAUSE_DEPTH` | 4 | Causes stored in `error_groups.cause_chain`; deeper links are dropped. |
| `MAX_CAUSE_FRAMES` | 200 | Frames per stored cause. The outermost exception's own `frames` are not truncated. |
| `RAW_EVENT_CAP` / `RAW_EVENT_MAX_BYTES` | 50 per group/server/hour, 64 KiB each | Retention of the compressed payload in `occurrences.raw_event`. |

Unknown fields are ignored by validation, and are therefore absent from the stored
`raw_event` too - it holds the validated payload, not the received bytes.

## Plugin implementation

### Log4j2 appender, not a JUL handler

The plugin attaches a custom Log4j2 `Appender` programmatically at runtime, rather than a
JUL handler, because:

- Log4j2 `LogEvent` is the primary logging event in Paper; JUL events are bridged into
  Log4j2, so a JUL handler sees them later and with extra overhead.
- `LogEvent.getThrown()` gives direct `Throwable` access without going through JUL's
  `LogRecord`.
- More precise filtering is possible at the Log4j2 level.

### Appender attachment

The appender is attached to the core root `Logger` directly, **not** via
`LogManager.getContext(false)`. Called from a plugin classloader, that can return a
*child* context, whose appenders never see server-level events.

```java
ExceptionAppender appender = new ExceptionAppender(serverId, sender, minLevel);
appender.start();
((org.apache.logging.log4j.core.Logger) LogManager.getRootLogger()).addAppender(appender);
```

This overload registers the appender with **no level threshold** - it delegates to
`LoggerConfig.addAppender(appender, null, null)` - so the appender receives everything
reaching the root `LoggerConfig` and must filter by level itself. The
`addAppender(appender, Level.ERROR, null)` overload is not usable here: it lives on
`LoggerConfig`, reached through the context this pattern deliberately avoids.

On plugin disable the appender is removed and stopped:

```java
((org.apache.logging.log4j.core.Logger) LogManager.getRootLogger()).removeAppender(appender);
appender.stop();
```

### Event filtering

`append()` skips events that:

- have no throwable (`logEvent.getThrown() == null`), or
- are below `EXCEPTLOG_MIN_LEVEL` (default `INFO`).

Both checks happen before the rate-limit counter is incremented, so filtered events do not
consume budget a reportable event in the same second needs.

### HTTP client

Java's built-in `java.net.http.HttpClient` (Java 11+, which Paper 1.20.4 requires) - no
external HTTP library. Requests are submitted to a single-threaded daemon executor, so
the server thread never blocks on a POST, and events are sent in the order they occurred.

### Frame extraction

```java
Throwable t = logEvent.getThrown();
StackTraceElement[] elements = t.getStackTrace();
for (StackTraceElement ste : elements) {
    String location = parseLocation(ste.toString()); // extract "Monumenta.jar" from "...~[Monumenta.jar:?]"
    frames.add(new Frame(ste.getClassName(), ste.getMethodName(),
                         ste.getFileName(), ste.getLineNumber(), location));
}
```

## Synthetic exceptions from heap-logger

The heap-logger microservice (`heap-logger/`) uses this same protocol to report memory
leak patterns detected via heap dump analysis. It POSTs synthetic exceptions directly to
the exception-logger's `POST /ingest`, bypassing the Java plugin entirely, so
`EXCEPTLOG_MIN_LEVEL` and the plugin's rate limit do not apply to them.

The field values are fixed so that the same leak pattern fingerprints identically across
servers and over time:

| Field | Value |
|---|---|
| `level` | `ERROR` |
| `logger` | `com.playmonumenta.memoryleak.HeapAnalyzer` |
| `thread` | `heap-worker` |
| `message` | `Memory leak detected in heap dump` |
| `exception.class_name` | `com.playmonumenta.memoryleak.MemoryLeakException` |
| `exception.message` | `Leaked: <first class in retention chain> x <instance count>`, where the separator is a U+00D7 multiplication sign, not the letter `x` |
| `exception.frames` | One frame per step in the retention chain. `class_name` is the class at that step; `method` is the field name holding the reference, or `<ref>` if unknown. `file`, `line` and `location` are always `null`, `-1` and `null`. |
| `exception.cause` | Always `null` |

What makes these group usefully is that the leaked object type appears verbatim as the
first class name in `exception.message` and as the first retention-chain frame, while the
instance count normalizes to `<N>` - so the same leak reported from two servers with
different counts lands in one group. The hashed frames are the first three chain entries
that match `APP_PACKAGES`, or, for a chain of purely third-party classes, the first three
entries via `extract_app_frames`'s fallback.

One POST is sent per leak pattern. A single heap dump analysis may produce several
patterns, each reported as a separate ingest event.

## Concrete example

```json
{
  "schema_version": 1,
  "server_id": "survival-0",
  "timestamp_ms": 1705298892000,
  "level": "ERROR",
  "logger": "com.playmonumenta.plugins.Plugin",
  "thread": "Server thread",
  "message": "Failed to load boss!",
  "exception": {
    "class_name": "java.lang.Exception",
    "message": "boss_generictarget only works on mobs! Entity name='Souls Unleashed', tags=[boss_projectile[soundlaunch=[],...",
    "frames": [
      {
        "class_name": "com.playmonumenta.plugins.bosses.bosses.GenericTargetBoss",
        "method": "<init>",
        "file": "GenericTargetBoss.java",
        "line": 34,
        "location": "Monumenta.jar"
      },
      {
        "class_name": "com.playmonumenta.plugins.bosses.BossManager",
        "method": "processEntity",
        "file": "BossManager.java",
        "line": 1369,
        "location": "Monumenta.jar"
      },
      {
        "class_name": "com.playmonumenta.plugins.bosses.BossManager",
        "method": "creatureSpawnEvent",
        "file": "BossManager.java",
        "line": 565,
        "location": "Monumenta.jar"
      },
      {
        "class_name": "com.destroystokyo.paper.event.executor.asm.generated.GeneratedEventExecutor472",
        "method": "execute",
        "file": null,
        "line": -1,
        "location": null
      },
      {
        "class_name": "java.lang.Thread",
        "method": "run",
        "file": "Thread.java",
        "line": 1583,
        "location": null
      }
    ],
    "cause": null
  }
}
```

The first three frames are in `com.playmonumenta`, so those are the ones the
fingerprint hashes, and the quoted entity name normalizes away before hashing. See
[SCHEMA.md](SCHEMA.md) for how the event is fingerprinted and stored, and
[README.md](README.md) for the plugin's configuration.
