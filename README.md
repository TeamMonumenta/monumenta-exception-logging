# Monumenta Exception Logger

Custom exception tracker used by the Monumenta Minecraft network.
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
disable. It uses Java's built-in `java.net.http.HttpClient` (no external HTTP library needed).
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
| `SLASH_COMMAND_PREFIX` | Prefix prepended to all slash command names (default: empty). Use to run multiple bots in one Discord - e.g. `ex_play_` makes `/new` become `/ex_play_new`. |
| `CHISEL_PUBLIC_URL` | Base URL of this server's public endpoint (e.g. `https://exceptions.example.com`). When set, enables the Chisel integration: the `/chisel/poll` and `/chisel/callback/*` endpoints become active and the 🔧 reaction handler is enabled. |
| `CHISEL_FIX_PROMPT_PATH` | Path to the `fix_exception_prompt.md` template rendered when a fix is requested (default: `fix_exception_prompt.md`). |
| `REACTION_FIX_REQUEST` | Emoji that triggers a Chisel fix request (default: 🔧). |
| `REACTION_FIX_WORKING` | Emoji shown while a fix is in progress (default: 🔄). |
| `REACTION_FIX_SUCCESS` | Emoji shown when Chisel opens a PR (default: 🟢). |
| `REACTION_FIX_FAILURE` | Emoji shown when Chisel fails (default: 🔴). |
| `REACTION_FIX_DECLINED` | Emoji shown when Chisel declines the task (default: 🟡). |

## Architecture

### Fingerprinting

Each exception is fingerprinted by hashing three components: exception class + normalized message +
top 3 application stack frames (`class.method` only, no line numbers). Line numbers are excluded
so minor code edits that shift lines don't create new groups.

The normalization step replaces variable runtime content with stable tokens so the same logical
bug always groups together:

| Pattern | Token | Examples |
|---|---|---|
| Hyphenated UUID | `<uuid>` | `550e8400-e29b-41d4-a716-446655440000` |
| Bare (unhyphenated) UUID | `<uuid>` | `3601df3d96f54dc1b10b8a4ebcefd210` (Mojang auth URLs) |
| IP address | `<ip>` | `192.168.1.100` |
| Long number (>= 4 digits) | `<N>` | coordinates, entity IDs, task IDs |
| Quoted string (single or double) | `<str>` | entity names, class names in NPE messages |
| Bracket data | `<data>` | boss tag lists, NBT |
| Long opaque token (>= 32 `[A-Za-z0-9_-]` chars) | `<id>` | CDN/WAF request IDs, auth tokens, hashes |
| World names after "measure distance between ... and ..." | `<world1>`, `<world2>` | `plot3769`, `ringinstance101` |

At startup the server automatically re-fingerprints all existing groups using the current
normalization rules. Groups whose fingerprint changes are updated in place; groups that become
identical after re-normalization are merged (counts and occurrence records are combined, and any
orphaned Discord messages for the removed duplicate are deleted by the bot's next refresh tick).
The migration is logged only when something changed, so normal restarts are quiet.

See [SCHEMA.md](SCHEMA.md) for the full fingerprinting algorithm and schema.

### Status model

Groups have three statuses: `active`, `muted`, `resolved`. **Status is never changed by ingest** -
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
| `/muted` | - | List muted groups |
| `/resolved` | - | List resolved groups |
| `/details` | `short_id` | Full details with stack trace and timeline |
| `/mute` | `short_id` | Mute a group |
| `/unmute` | `short_id` | Unmute a group |
| `/resolve` | `short_id` | Mark a group resolved |
| `/notify add` | `pattern` | Add a personal notification rule (Python regex) |
| `/notify list` | - | List your notification rules with their IDs |
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
| Add `:wrench:` | Submit a Chisel fix request (requires `CHISEL_PUBLIC_URL` to be set) |

Removing either mute or resolve reaction always unmutes, regardless of whether other reactions of
that type remain - making it easy to unmute an issue someone else muted. For `:question:`, the bot
attempts to remove the reaction after sending the DM; this requires the **Manage Messages**
permission and is skipped with a warning logged if not granted.

### Chisel integration (automated fix requests)

When `CHISEL_PUBLIC_URL` is set, the server exposes two additional endpoints consumed by the
[Chisel](https://github.com/Combustible/discord-autopatch-chisel) service. When configured
and triggered on a specific exception via the discord channel, this integration will attempt
to automatically fix that exception and open a pull request.

| Endpoint | Description |
|---|---|
| `POST /chisel/poll` | Chisel polls this to claim the next pending fix job. Returns 200 with `{message, requester_id, callback_url}` or 204 if the queue is empty. Authentication is handled at the Kubernetes ingress layer - see the deployment docs. |
| `POST /chisel/callback/<job_id>` | Chisel POSTs the job result here on completion. Updates the fix attempt record, swaps the Discord reaction to the outcome emoji, and DMs the user who requested the fix. |

**Callback request body** (POSTed by Chisel to `/chisel/callback/<job_id>`):

```json
{
  "status": "success" | "failure" | "declined",
  "message": "Short human-readable status (<= 200 chars)",
  "summary": "Full agent narrative: what was examined, what changed or why not",
  "detail": "Step-by-step execution log: every file examined, search run, decision made",
  "pr_url": "https://github.com/..."
}
```

`pr_url` is only present when `status = "success"`. All other fields are always present.
`detail` is stored in the `fix_attempts` table but not included in the DM to the requester.

The fix request workflow:

1. A developer adds `:wrench:` to an exception group's Discord message
2. The bot renders `fix_exception_prompt.md` with exception data, queues a fix attempt in the
   `fix_attempts` table (recording the requester's Discord user ID), removes `:wrench:`, and
   adds `:arrows_counterclockwise:`. The wrench reaction is always removed regardless of outcome
   so it cannot linger on messages across bot restarts.
3. Chisel polls, claims the job, and attempts to create a pull request fixing the exception
4. On completion, Chisel POSTs the result; the bot swaps `:arrows_counterclockwise:` to the
   outcome emoji (🟢 success / 🔴 failure / 🟡 declined) and DMs the requester with the
   status, message, summary, and PR URL if applicable

If a fix attempt is already pending or running for a group, a second `:wrench:` reaction is
silently ignored (the wrench is still removed). Fix attempt history is stored in the
`fix_attempts` table for future `/fix-history` commands.

The `fix_exception_prompt.md` template supports these variables:

| Variable | Value |
|---|---|
| `{short_id}` | 8-character fingerprint prefix |
| `{exception_class}` | Fully qualified exception class |
| `{message}` | Normalized exception message (variable content replaced with tokens) |
| `{raw_message}` | Raw exception message from the most recent occurrence (un-normalized; falls back to `{message}` if no occurrences are retained) |
| `{stacktrace}` | Full canonical stack trace |
| `{count}` | Total occurrence count |
| `{servers}` | Comma-separated list of servers affected |
| `{first_seen}` | ISO timestamp of first occurrence |
| `{last_seen}` | ISO timestamp of most recent occurrence |

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

# Run all checks (pylint -> pyright -> pytest); stops at first failure
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

- [PROTOCOL.md](PROTOCOL.md) - JSON wire format (plugin -> server)
- [SCHEMA.md](SCHEMA.md) - SQLite schema and fingerprinting algorithm
