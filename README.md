# Monumenta Exception Logger

Custom exception tracker used by the the Monumenta Minecraft network.
Aggregates and fingerprints exceptions from all servers into a central SQLite
database, with Discord integration for alerts and triage.

Minimal requirements make this relatively simple to deploy in any setup.

## Components

### Java Plugin (`plugin/`)

A lightweight Paper plugin that attaches a custom Log4j2 appender to each server process. On every
ERROR-level log event with a throwable, the appender:

- Extracts the exception class, message, and full stack trace
- Serializes them as JSON ([PROTOCOL.md](PROTOCOL.md))
- POSTs to the ingest server asynchronously (fire-and-forget, 20 events/sec rate limit)

The appender is attached programmatically at plugin startup via `LoggerContext` and removed on
disable. It uses Java's built-in `java.net.http.HttpClient` — no external HTTP library is needed.
Gson (available on Paper's classpath) handles JSON serialization.

The plugin is configured via environment variables for simplicity in a docker/kubernetes environment:

| Variable | Description |
|---|---|
| `EXCEPTLOG_INGEST_URL` | Full URL of the Python server's `POST /ingest` endpoint |
| `EXCEPTLOG_SERVER_NAME` | Server identity included in every event; falls back to hostname |
| `EXCEPTLOG_VERBOSE` | Set to any non-empty value other than `false` to enable verbose logging (logs every exception queued and each successful POST) |

### In-game commands

| Command | Permission | Description |
|---|---|---|
| `/excepttest` | `monumenta.excepttest` | Sends a synthetic exception to the ingest service with a pseudorandom class/method/line so every invocation creates a new exception group. Useful for verifying the full pipeline end-to-end. |
| `/exceptverbose` | `monumenta.exceptverbose` | Toggles verbose logging at runtime. Equivalent to setting `EXCEPTLOG_VERBOSE` at startup but can be flipped without a restart. Reports the new state to the sender. |

### Python Server (`server/`)

Receives events, fingerprints and groups them, stores in SQLite (WAL mode), and exposes a query/
mutation API consumed by the embedded Discord bot.

**Packages:**

| File | Role |
|---|---|
| `tracker/config.py` | `TrackerConfig` dataclass + `from_env()` loader |
| `tracker/db.py` | SQLite init, schema, expiry task |
| `tracker/fingerprint.py` | Message normalization + SHA-256 fingerprinting |
| `tracker/ingest.py` | Pydantic validation + ingest pipeline |
| `tracker/api.py` | `Tracker` class: all query and mutation methods |
| `server.py` | Quart HTTP app (`POST /ingest`) + async entry point |
| `bot.py` | Discord bot (slash commands, channel message management) |

The server is configured via environment variables:

| Variable | Description |
|---|---|
| `DB_PATH` | Path to SQLite database (default: `tracker.db`) |
| `APP_PACKAGES` | Comma-separated package prefixes for fingerprinting (default: `com.playmonumenta`) |
| `EXPIRY_DAYS` | Number of days to retain exception groups and occurrences before purging (default: `14`) |
| `PORT` | HTTP port (default: `8080`) |
| `VERBOSE` | Log a formatted entry for every ingest submission (default: `true`; set to `false` to disable) |
| `DISCORD_TOKEN` | Discord bot token; if unset, the bot is disabled |
| `DISCORD_CHANNEL` | Discord channel ID (integer) |
| `DISCORD_REFRESH_PERIOD_SECONDS` | Refresh loop interval in seconds (default: `300`) |
| `SLASH_COMMAND_PREFIX` | Prefix prepended to all slash command names (default: empty). Use to run multiple bots in one Discord — e.g. `ex_play_` makes `/new` become `/ex_play_new`. |

## Architecture

### Fingerprinting

Each exception is fingerprinted by hashing three components: exception class + normalized message +
top 3 application stack frames (`class.method` only, no line numbers). Line numbers are excluded
so minor code edits that shift lines don't create new groups.

The normalization step replaces UUIDs, IPs, long numbers, quoted strings, and bracket data with
tokens (`<uuid>`, `<ip>`, `<N>`, `<str>`, `<data>`) so the same logical bug groups together even
when the exception message contains variable runtime content.

See [SCHEMA.md](SCHEMA.md) for the full fingerprinting algorithm and schema.

### Status model

Groups have three statuses: `active`, `muted`, `resolved`. **Status is never changed by ingest** —
active, muted, and resolved groups all receive count and `last_seen` updates on reoccurrence. Status
is only changed by explicit slash commands (`/mute`, `/unmute`, `/resolve`). Resolved groups age
out naturally after the retention window expires (see `EXPIRY_DAYS`).

### Discord integration

When a new exception group is first observed, the bot posts a message to the configured channel
with fingerprint, timestamps, affected servers, count, and stack trace (truncated to Discord's 2000-
char limit). A background refresh loop (default 300s) re-edits all tracked messages with fresh
data. When expiry removes a group, its Discord message is deleted.

Groups are identified in slash commands by their **short ID**: the first 8 hex characters of the
fingerprint. Muted groups are displayed as spoilers (`||..||`); resolved groups as strikethrough
(`~~..~~`).

**Slash commands (all ephemeral):**

Command names are prefixed by `SLASH_COMMAND_PREFIX` (default: empty, so names are as shown).

| Command | Args | Description |
|---|---|---|
| `/top` | `[window_hours=24]` | Top 20 active groups by recent count |
| `/new` | `[hours=24]` | Groups first seen in the last N hours |
| `/search` | `query` | Search by exception class, message text, or stack frame (e.g. `ParticleManager.java`) |
| `/server` | `name` | Top groups for a specific server |
| `/muted` | — | List muted groups |
| `/resolved` | — | List resolved groups |
| `/details` | `short_id` | Full details with stack trace and timeline |
| `/mute` | `short_id` | Mute a group |
| `/unmute` | `short_id` | Unmute a group |
| `/resolve` | `short_id` | Mark a group resolved |
| `/notify add` | `pattern` | Add a personal notification rule (Python regex) |
| `/notify list` | — | List your notification rules with their IDs |
| `/notify remove` | `id` | Remove a notification rule by ID |
| `/notify test` | `id` | Test a rule against all active groups (sends up to 5 DMs) |

**Personal notifications:**

Users can subscribe to be DMed whenever a new exception group is first observed. Each subscription
is a Python regex (case-sensitive) matched against the exception class, normalized message, and
stack trace. When a new group matches one or more of your rules, you receive a single DM listing
every matched rule ID and pattern, followed by the full exception message.

Rule IDs are stable integers that never change or get reused after deletion, so an ID seen in a DM
always refers to the same rule (or no longer exists if you removed it). There is a maximum of 100
rules per user.

`/notify test` scans all active (non-muted, non-resolved) groups and sends a DM for each match,
capped at 5 to avoid inbox flooding. Use it to verify a new pattern before relying on it.

**Reaction shortcuts:**

Reacting to an exception group message provides a faster alternative to slash commands for common
triage actions:

| Reaction | Effect |
|---|---|
| Add `:no_entry:` | Mute the group (equivalent to `/mute`) |
| Add `:white_check_mark:` | Resolve the group (equivalent to `/resolve`) |
| Remove `:no_entry:` or `:white_check_mark:` | Unmute the group (equivalent to `/unmute`) |
| Add `:question:` | Receive a DM with full group details (equivalent to `/details`) |

Removing either mute or resolve reaction always unmutes, regardless of whether other reactions of
that type remain — making it easy to unmute an issue someone else muted. For `:question:`, the bot
attempts to remove the reaction after sending the DM; this requires the **Manage Messages**
permission and is skipped with a warning logged if not granted.

### Async model

Quart (async Flask) and discord.py share a single asyncio event loop. SQLite calls use the
synchronous `sqlite3` module; at the expected write volume (a few thousand events/hour), individual
writes complete fast enough not to block the event loop meaningfully.

### Security

No authentication. Plain HTTP only. You must ensure that the server is properly firewalled. 

## Development

### Python server

```bash
cd server

# Create .venv and install runtime + dev dependencies
make venv

# Run all checks (pylint → pyright → pytest); stops at first failure
make test

# Individual targets
make lint       # pylint
make typecheck  # pyright (strict)
make pytest     # pytest

# Run server (Discord disabled if DISCORD_TOKEN is unset)
python server.py
```

Runtime dependencies are in `server/requirements.txt`; dev/test dependencies (pytest, pylint,
pyright) are in `server/requirements-dev.txt`. The `make venv` target creates `.venv` inside
`server/` and installs both. It re-runs automatically if either requirements file changes.

### Java plugin

```bash
# Build
cd plugin && ./gradlew clean build
# Output: plugin/build/libs/MonumentaExceptionReporter-*.jar
```

## Reference

- [PROTOCOL.md](PROTOCOL.md) — JSON wire format (plugin → server)
- [SCHEMA.md](SCHEMA.md) — SQLite schema and fingerprinting algorithm
