# Exception Reporting Protocol

This document defines the JSON message format sent from the Minecraft plugin to the Python ingest server.

## Transport

- **Method:** `HTTP POST`
- **Endpoint:** Configured via `EXCEPTLOG_INGEST_URL` environment variable on the server (e.g. `http://exception-tracker.internal/ingest`)
- **Content-Type:** `application/json`
- **Authentication:** None. The ingest server listens on plain HTTP with no auth. Security is provided entirely by the Kubernetes network boundary - the service is not exposed outside the cluster. This avoids per-request TLS/crypto overhead and removes secret management from the plugin.
- **Fire-and-forget:** The plugin does not retry on failure and does not block the server thread. Failures are silently dropped after logging a single warning.
- **Batching:** Single events per POST. No batching in v1.

## Message Schema

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

### Top-Level Fields

| Field | Type | Description |
|---|---|---|
| `schema_version` | integer | Always `1` for this version. Used by the server to handle future format changes. |
| `server_id` | string | Identifies the originating server. Read from `EXCEPTLOG_SERVER_NAME` env var at plugin startup. Falls back to hostname if env var is absent. |
| `timestamp_ms` | integer | Unix timestamp in milliseconds (UTC) when the log event was emitted. From `LogEvent.getTimeMillis()`. |
| `level` | string | Log level string. Will always be `"ERROR"` in practice since the plugin only captures ERROR-level events, but included for completeness. |
| `logger` | string | The Log4j2 logger name that emitted the event. Typically the fully-qualified plugin class name, e.g. `com.playmonumenta.plugins.Plugin`. |
| `thread` | string | Name of the thread that logged the event, e.g. `"Server thread"`. From `LogEvent.getThreadName()`. |
| `message` | string | The human-readable log message that accompanied the exception, e.g. `"Failed to load boss!"`. This is the message passed to `logger.error(...)`, not the exception message. May be empty string if no message was provided. |
| `exception` | object | The captured exception. Always present (events without a throwable are filtered by the plugin before sending). |

### `exception` Object

| Field | Type | Description |
|---|---|---|
| `class_name` | string | Fully-qualified exception class name, e.g. `"java.lang.NullPointerException"` or `"com.playmonumenta.plugins.SomeCustomException"`. |
| `message` | string \| null | The exception's own message (`e.getMessage()`). Null if no message was set. May contain variable content (player names, coordinates, etc.) - the server normalizes this for fingerprinting. |
| `frames` | array | Ordered list of stack frames, from closest to throw site (index 0) to oldest caller. See Frame Object below. Includes all frames; the server filters for application frames during fingerprinting. |
| `cause` | object \| null | The chained cause exception, if any (`e.getCause()`). Same structure as `exception`. Cause chains are captured up to a depth of 5 to prevent unbounded nesting. |

### Frame Object

| Field | Type | Description |
|---|---|---|
| `class_name` | string | Fully-qualified class name, e.g. `"com.playmonumenta.plugins.bosses.bosses.GenericTargetBoss"`. |
| `method` | string | Method name, e.g. `"<init>"`, `"processEntity"`. |
| `file` | string \| null | Source file name, e.g. `"GenericTargetBoss.java"`. Null when compiled without debug info. |
| `line` | integer | Source line number. `-1` if unknown (e.g. native methods, or compiled without debug info). |
| `location` | string \| null | JAR file or module the class was loaded from, e.g. `"Monumenta.jar"`, `"paper-1.20.4.jar"`. Derived from `StackTraceElement.toString()` - the portion in brackets. Null when not available (`"?"` in raw output is normalized to null). |

## Plugin Implementation Notes

### Log4j2 Appender vs. JUL Handler

**Use a custom Log4j2 Appender attached programmatically at runtime.** This is preferred over a JUL handler because:
- Log4j2 `LogEvent` is the primary logging event in Paper - JUL events are bridged to Log4j2, introducing extra overhead.
- `LogEvent.getThrown()` gives direct Throwable access without going through JUL's `LogRecord`.
- More precise filtering is possible at the Log4j2 level.

**Appender attachment pattern:**
```java
LoggerContext context = (LoggerContext) LogManager.getContext(false);
Configuration config = context.getConfiguration();
ExceptionReporterAppender appender = new ExceptionReporterAppender(/* config */);
appender.start();
config.addAppender(appender);
config.getRootLogger().addAppender(appender, Level.ERROR, null);
context.updateLoggers();
```

Remove the appender on plugin disable:
```java
config.getRootLogger().removeAppender("ExceptionReporter");
appender.stop();
context.updateLoggers();
```

### Event Filtering

The appender should skip events that:
- Have no throwable (`logEvent.getThrown() == null`)
- Are below ERROR level (enforced by the level filter on `addAppender`)

### HTTP Client

Use Java's built-in `java.net.http.HttpClient` (available since Java 11, which Paper 1.20.4 requires). No external HTTP library needed. The POST should be made on a separate thread (or virtual thread) to avoid blocking the server thread.

### Frame Extraction

```java
Throwable t = logEvent.getThrown();
StackTraceElement[] elements = t.getStackTrace();
for (StackTraceElement ste : elements) {
    String location = parseLocation(ste.toString()); // extract "Monumenta.jar" from "...~[Monumenta.jar:?]"
    frames.add(new Frame(ste.getClassName(), ste.getMethodName(),
                         ste.getFileName(), ste.getLineNumber(), location));
}
```

## Synthetic Exceptions from heap-logger

The heap-logger microservice (`heap-logger/`) uses this same protocol to report memory
leak patterns detected via heap dump analysis. It POSTs synthetic exceptions directly
to the exception-logger's `POST /ingest` endpoint, bypassing the Java plugin entirely.

These synthetic events use fixed, stable field values so that the fingerprinting algorithm
groups the same leak pattern consistently across servers and over time:

| Field | Value |
|---|---|
| `level` | `ERROR` |
| `logger` | `com.playmonumenta.memoryleak.HeapAnalyzer` |
| `thread` | `heap-worker` |
| `message` | `Memory leak detected in heap dump` |
| `exception.class_name` | `com.playmonumenta.memoryleak.MemoryLeakException` |
| `exception.message` | `Leaked: <first class in retention chain> x <instance count>` |
| `exception.frames` | One frame per step in the retention chain. `class_name` is the class at that step; `method` is the field name holding the reference, or `<ref>` if unknown. `file`, `line`, and `location` are always `null`, `-1`, and `null`. |
| `exception.cause` | Always `null` |

The `exception.message` field is the fingerprint key. The first class name in the
retention chain is the leaked object type (e.g. `org/bukkit/craftbukkit/.../CraftPlayer`);
using it verbatim ensures that the same leak on different servers produces the same
fingerprint and is merged into one exception group.

One POST is sent per leak pattern. A single heap dump analysis may produce multiple
patterns, each reported as a separate ingest event.

## Concrete Example

Based on the real exception from the project specification:

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
