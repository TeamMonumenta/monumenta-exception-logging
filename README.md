# Monumenta Exception Logger

Custom exception and memory-leak tracker used by the Monumenta Minecraft network.
Aggregates and fingerprints exceptions and heap-dump leak patterns from all servers
into a central SQLite database, with Discord integration for alerts and triage.

Minimal requirements make this relatively simple to deploy in any setup.

This repository also contains [pr-bot](pr-bot/README.md), a separate Discord bot that
tracks GitHub pull request state. It shares no code or data with the exception tracker
and is documented independently.

## Components

### Java Plugin (`plugin/`)

A lightweight Paper plugin that attaches a custom Log4j2 appender to each server process. On every
log event at or above `EXCEPTLOG_MIN_LEVEL` (default `INFO`) that carries a throwable, the appender:

- Extracts the exception class, message, full stack trace, and chained causes
- Serializes them as JSON ([PROTOCOL.md](PROTOCOL.md))
- POSTs to the ingest server asynchronously (fire-and-forget, 20 events/sec rate limit)

The appender is attached at plugin startup to the core root `Logger`
(`LogManager.getRootLogger()`) and removed on disable. It is deliberately not attached through
`LogManager.getContext(false)`, which from a plugin classloader can return a *child* context whose
appenders never see server-level events. That attachment registers no level threshold, so the
appender filters by level itself. It uses Java's built-in `java.net.http.HttpClient` (no external
HTTP library needed); Gson (available on Paper's classpath) handles JSON serialization.

The plugin is configured via environment variables for simplicity in a docker/kubernetes environment:

| Variable | Description |
|---|---|
| `EXCEPTLOG_INGEST_URL` | Full URL of the Python server's `POST /ingest` endpoint. If unset or not a valid URI, exception reporting is disabled. |
| `EXCEPTLOG_SERVER_NAME` | Server identity included in every event; falls back to the hostname, then to `unknown` |
| `EXCEPTLOG_VERBOSE` | Set to any non-empty value other than `false` to enable verbose logging (logs every exception queued and each successful POST) |
| `EXCEPTLOG_MIN_LEVEL` | Minimum Log4j2 level to report (default: `INFO`). Events below this are dropped before the rate limiter sees them. **Do not set this to `ERROR`** without reading "Reporting threshold" below - it silently discards most of what this tracker usefully reports. An unrecognized name falls back to `INFO`; `OFF` is rejected with a warning (unset `EXCEPTLOG_INGEST_URL` to disable reporting instead). Values below `INFO` (`DEBUG`, `TRACE`, `ALL`) have no additional effect, since Paper's root logger is already capped at `INFO`. |
| `HEAPLOG_INGEST_URL` | URL of the heap-logger's `POST /ingest` endpoint (e.g. `http://heap-logger.<ns>.svc.cluster.local:8081/ingest`). If unset, heap dump reporting is disabled; so is it if `EXCEPTLOG_INGEST_URL` is unset, since the heap-logger is told where to report its findings. When set, the always-on log watcher reports heap dumps whenever `/spark heapdump` is run (manually or triggered by another plugin). spark must be present. |
| `HEAPLOG_AUTO_DUMP` | Set to any non-empty value other than `false` to automatically trigger a heap dump via `spark heapdump` when a `LowMemoryEvent` is received from MonumentaNetworkRelay. Default: off (log watcher still runs; only manual dumps are reported). Requires `HEAPLOG_INGEST_URL` to be set and MonumentaNetworkRelay to be present. |

#### Reporting threshold

`EXCEPTLOG_MIN_LEVEL` defaults to `INFO`, and **must not be raised to `ERROR`**.

Paper's `CraftScheduler.mainThreadHeartbeat` (sync tasks) and `CraftAsyncTask.run` (async) both
catch whatever a scheduled task threw and log **the original exception** at `Level.WARNING`,
through the owning plugin's logger. Mojang's authlib logs session-service failures at `warn` as
well. Between them, that is the majority of what this tracker reports - including its single
largest group. An `ERROR`-only threshold drops all of it, and does so silently: nothing fails,
the data just stops arriving.

`INFO` is as low as the threshold can usefully go: the appender is attached with
`Logger.addAppender(Appender)`, which registers it with no level threshold, so it sees whatever
reaches the root `LoggerConfig` - `INFO` and above, per the `<Root level="info">` in Paper's
bundled `log4j2.xml`.

`WARN` is a plausible future default, on the theory that `INFO`-with-a-throwable is rare. Check
that against the `level` column before changing it rather than assuming.

### In-game commands

| Command | Permission | Description |
|---|---|---|
| `/excepttest` | `monumenta.excepttest` | Sends a synthetic exception to the ingest service with a pseudorandom class/method/line so every invocation creates a new exception group. Useful for verifying the full pipeline end-to-end. |
| `/exceptverbose` | `monumenta.exceptverbose` | Toggles verbose logging at runtime. Equivalent to setting `EXCEPTLOG_VERBOSE` at startup but can be flipped without a restart. Reports the new state to the sender. |

### Heap Leak Detector (`heap-logger/`)

A separate service, deployed as a Kubernetes DaemonSet, that analyzes heap dumps (triggered
by `HEAPLOG_AUTO_DUMP` or a manual `/spark heapdump`) with the `heaptool` Rust binary and
reports each detected memory-leak pattern to the Python server as a synthetic exception,
using the same ingest protocol the plugin uses (see "Synthetic exceptions from heap-logger"
in [PROTOCOL.md](PROTOCOL.md)). See [heap-logger/README.md](heap-logger/README.md) for how
it fits into the pipeline and [heap-logger/SCHEMA.md](heap-logger/SCHEMA.md) for the
`heaptool --json` output it consumes.

### Python Server (`server/`)

Receives events, fingerprints and groups them, stores in SQLite (WAL mode), and exposes a query/
mutation API consumed by the embedded Discord bot and the network API below.

**Packages:**

| File | Role |
|---|---|
| `tracker/config.py` | `TrackerConfig` dataclass + `from_env()` loader |
| `tracker/db.py` | SQLite init, schema, fingerprint migration, expiry |
| `tracker/fingerprint.py` | Message normalization + SHA-256 fingerprinting |
| `tracker/ingest.py` | Pydantic validation + ingest pipeline |
| `tracker/api.py` | `Tracker` class: all query and mutation methods |
| `tracker/chisel.py` | Fix-request logic shared by the `:wrench:` reaction handler and `POST /api/groups/<id>/fix` |
| `server.py` | Quart HTTP app (`POST /ingest`, `/chisel/*`, `/api/*`) + async entry point |
| `bot.py` | Discord bot (slash commands, channel message management) |
| `cli/exctl.py` | Standalone CLI for the network API (see "Network API" below) |

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
| `CHISEL_PUBLIC_URL` | Base URL of this server's public endpoint (e.g. `https://exceptions.example.com`). When set, enables the Chisel integration: the `/chisel/poll` and `/chisel/callback/*` endpoints become active and the fix-request reaction handler is enabled. |
| `CHISEL_FIX_PROMPT_PATH` | Path to the `fix_exception_prompt.md` template rendered when a fix is requested (default: `fix_exception_prompt.md`). |
| `CHISEL_ALLOWED_USERS` | Comma-separated list of Discord user IDs permitted to trigger Chisel fix requests. If empty (default), all users may trigger fixes. Users not on the list have their fix-request reaction silently removed with no further action. |
| `DISCORD_PURGE_USERS` | Comma-separated list of Discord user IDs permitted to run `/purge`. If empty (default), no one can run the command. |
| `REACTION_FIX_REQUEST` | Emoji that triggers a Chisel fix request (default: `:wrench:`). |
| `REACTION_FIX_WORKING` | Emoji shown while a fix is in progress (default: `:arrows_counterclockwise:`). Set to empty string to suppress. |
| `REACTION_FIX_SUCCESS` | Emoji shown when Chisel opens a PR (default: `:green_circle:`). Set to empty string to suppress. |
| `REACTION_FIX_FAILURE` | Emoji shown when Chisel fails (default: `:red_circle:`). Set to empty string to suppress. |
| `REACTION_FIX_DECLINED` | Emoji shown when Chisel declines the task (default: `:yellow_circle:`). Set to empty string to suppress. |
| `API_TOKEN_DEFAULT_TTL_HOURS` | Default lifetime of a network API bearer token minted by `/api-token create` when no `lifetime_hours` argument is given (default: `24`). |
| `API_TOKEN_MAX_TTL_HOURS` | Upper bound on `/api-token create`'s `lifetime_hours` argument; requests above it are clamped, not rejected (default: `168`, i.e. 1 week). |

## Architecture

### Fingerprinting

Each exception is fingerprinted by hashing three components: exception class, the normalized
*exception* message (not the log message that accompanied it), and the top 3 application stack
frames (`Class.method` only, no line numbers). Line numbers are excluded so minor code edits
that shift lines do not create new groups.

The normalization step replaces variable runtime content with stable tokens so the same logical
bug always groups together. The rules are applied in this order:

| Pattern | Token | Examples |
|---|---|---|
| Hyphenated UUID | `<uuid>` | `550e8400-e29b-41d4-a716-446655440000` |
| Bare (unhyphenated) UUID | `<uuid>` | `3601df3d96f54dc1b10b8a4ebcefd210` (Mojang auth URLs) |
| IP address | `<ip>` | `192.168.1.100` |
| Plugin version (`git describe`) | `<version>` | `v11.80.2-1-gf114425-SNAPSHOT` |
| Long opaque token (32+ `[A-Za-z0-9_-]` chars) | `<id>` | CDN/WAF request IDs, auth tokens, hashes |
| Guild permission key (`guild.<name>.<role>`) | `guild.<id>` | `guild.nova+.member`, `guild.lads.member` |
| Any run of digits | `<N>` | coordinates, entity IDs, task IDs, counts |
| World names after "measure distance between ... and ..." | `<world1>`, `<world2>` | `plot3769`, `ringinstance101` |
| Quoted string (single or double) | `<str>` | entity names, class names in NPE messages |
| Bracket data | `<data>` | boss tag lists, NBT |
| `Location{world=...}` block | `Location{<location>}` | Bukkit location dumps |

At startup the server re-fingerprints all existing groups using the current normalization
rules. Groups whose fingerprint changes are updated in place; groups that become identical
after re-normalization are merged (counts and occurrence records are combined, and any
orphaned Discord message for the removed duplicate is deleted by the bot's next refresh tick).
The migration is logged only when something changed, so normal restarts are quiet.

See [SCHEMA.md](SCHEMA.md) for the exact rule order and why it matters, the full hash
definition, and what a fingerprint deliberately excludes.

### Status model

Groups have three statuses: `active`, `muted`, `resolved`. **Status is never changed by ingest** -
active, muted, and resolved groups all receive count and `last_seen` updates on reoccurrence. Status
is only changed by explicit slash commands (`/mute`, `/unmute`, `/resolve`), their reaction
equivalents, or the network API. Resolved groups age out naturally after the retention window
expires (see `EXPIRY_DAYS`).

### Discord integration

When a new exception group is first observed, the bot posts a message to the configured channel
with fingerprint, timestamps, affected servers, count, exception class and message, and the stack
trace (truncated to fit Discord's 2000-character limit, with a trailer naming how many frames were
dropped). Anything that changes a
group - a further occurrence, a status change, a startup re-fingerprint - marks its message stale,
and a background refresh loop (default 300s) re-edits only the messages marked that way. When
expiry removes a group, its Discord message is deleted. On startup the bot posts messages for any
groups that have none, which covers groups ingested while it was offline.

Groups are identified in slash commands by their **short ID**: the first 8 hex characters of the
fingerprint. Muted groups are displayed as spoilers (`||..||`); resolved groups as strikethrough
(`~~..~~`).

**Slash commands (all ephemeral):**

Command names are prefixed by `SLASH_COMMAND_PREFIX` (default: empty, so names are as shown).

| Command | Args | Limit | Description |
|---|---|---|---|
| `/top` | `[window_hours=24]` | 20 | Top active groups by recent count |
| `/new` | `[hours=24] [before]` | none (time-windowed only) | Groups first seen in the last N hours, optionally in the N-hour window ending at the `before` Unix timestamp |
| `/search` | `query` | 20 | Search by exception class, message text, or stack frame (e.g. `ParticleManager.java`) |
| `/server` | `name` | 20 | Top active groups for a specific server |
| `/muted` | - | 20 | List muted groups |
| `/resolved` | - | 20 | List resolved groups |
| `/details` | `short_id` | n/a (single group) | Full details with stack trace, affected servers, latest raw message, and mute/resolve attribution |
| `/fix-history` | `short_id` | 20 | Chisel fix attempt history for a group |
| `/mute` | `short_id` | n/a | Mute a group |
| `/unmute` | `short_id` | n/a | Unmute a group |
| `/resolve` | `short_id` | n/a | Mark a group resolved |
| `/purge` | `[server] [older_than_days] [fixed] [muted]` | n/a | Delete exception groups matching the given filters; at least one is required (see `DISCORD_PURGE_USERS`) |
| `/notify add` | `pattern` | n/a | Add a personal notification rule (Python regex) |
| `/notify list` | - | n/a | List your notification rules with their IDs |
| `/notify remove` | `id` | n/a | Remove a notification rule by ID |
| `/notify test` | `id` | 5 DMs | Test a rule against all active groups |
| `/api-token create` | `[lifetime_hours]` | n/a | Mint a network API bearer token bound to your Discord ID, shown once |
| `/api-token revoke` | - | n/a | Revoke every network API token you have minted |

`/unmute` also un-resolves: it returns a group to `active` and clears both the mute and
the resolve attribution, matching the reaction behavior described below. Every one of
these commands has a network-API equivalent except `/purge` and `/notify` - see
[NETWORK_API.md](NETWORK_API.md). `/api-token` exists only in Discord, with no
network-API equivalent.

`/purge server:<name>` deletes only groups where that server is the *sole* contributor; a group
also seen elsewhere is left alone. `/purge older_than_days:N` is an early expiry pass: it runs the
same routine the hourly task does, with `N` in place of `EXPIRY_DAYS`, so it also drops occurrences
and hourly counts older than `N` days from groups it keeps.

**Personal notifications:**

Users can subscribe to be DMed whenever a new exception group is first observed. Each subscription
is a Python regex (case-sensitive) matched against the exception class, the normalized message, and
the stack trace. When a new group matches one or more of your rules, you receive a single DM listing
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
that type remain - making it easy to unmute an issue someone else muted. For `:question:` and
`:wrench:`, the bot removes the triggering reaction afterwards; this requires the **Manage
Messages** permission and is skipped with a warning logged if not granted.

### Chisel integration (automated fix requests)

When `CHISEL_PUBLIC_URL` is set, the server exposes two additional endpoints consumed by the
[Chisel](https://github.com/Combustible/discord-autopatch-chisel) service. When configured
and triggered on a specific exception via the discord channel, this integration will attempt
to automatically fix that exception and open a pull request.

| Endpoint | Description |
|---|---|
| `POST /chisel/poll` | Chisel polls this to claim the next pending fix job. Returns 200 with `{message, requester_id, callback_url}` or 204 if the queue is empty. Despite its name, `requester_id` is the exception group's 8-character short ID, not a Discord user ID. Authentication is handled at the Kubernetes ingress layer, not by the Quart app itself - see "Security" below. |
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

Any other `status` is rejected with `400`, an unknown `job_id` with `404`, and a job that was
cancelled before Chisel claimed it with `409`. `pr_url` is only
meaningful when `status` is `"success"`. `detail` is stored in the `fix_attempts` table but is
neither DMed to the requester nor served by the API.

The fix request workflow:

1. A developer adds `:wrench:` to an exception group's Discord message
2. The bot renders `fix_exception_prompt.md` with exception data, queues a fix attempt in the
   `fix_attempts` table (recording the requester's Discord user ID), removes `:wrench:`, and
   adds the working reaction. The wrench reaction is always removed regardless of outcome
   so it cannot linger on messages across bot restarts.
3. Chisel polls, claims the job, and attempts to create a pull request fixing the exception
4. On completion, Chisel POSTs the result; the bot swaps the working reaction for the
   outcome one (success / failure / declined) and DMs the requester with the
   status, message, summary, and PR URL if applicable

If a fix attempt is already pending or running for a group, a second `:wrench:` reaction is
silently ignored (the wrench is still removed). An attempt that gets no callback within an hour
is failed automatically, which is what stops a dead job blocking a group forever. Fix attempt
history is stored in the `fix_attempts` table and available via the `/fix-history` slash command
or `GET /api/groups/<id>/fix-attempts` (see [NETWORK_API.md](NETWORK_API.md)). The queue across
every group is `GET /api/fix-attempts?status=pending,running` (`exctl fix-list`), and a job
Chisel hasn't claimed yet can be withdrawn with `POST /api/fix-attempts/<job_id>/cancel`
(`exctl fix-cancel`). Both are API-only, with no Discord command.

The `fix_exception_prompt.md` template supports these variables. Anything else in `{braces}` is
left as written, so a brace in an exception message is harmless.

| Variable | Value |
|---|---|
| `{short_id}` | 8-character fingerprint prefix |
| `{exception_class}` | Fully qualified exception class |
| `{message}` | Normalized exception message (variable content replaced with tokens) |
| `{raw_message}` | Raw exception message from the most recent occurrence (un-normalized; falls back to `{message}` if no occurrences are retained) |
| `{stacktrace}` | Full stack trace of the most recent occurrence |
| `{cause_chain}` | Chained causes, outermost first, each with its own frames; `(none)` if the exception had no cause. Without this, a wrapped exception hands Chisel only the wrapper's frames - scheduler or tick-loop machinery - and asks it to fix a bug that appears nowhere in what it was given. |
| `{count}` | Total occurrence count |
| `{servers}` | Comma-separated list of servers affected |
| `{first_seen}` | ISO timestamp of first occurrence |
| `{last_seen}` | ISO timestamp of most recent occurrence |

### Network API (`/api/`)

`server/server.py` also exposes `/api/*` routes on the same Quart app and port as `/ingest`
and `/chisel/*`, for querying and mutating exception groups without going through Discord.
Reads are unauthenticated, same as `/ingest`; mutations require a bearer token minted via
`/api-token create` in Discord. There is no `/api/purge`. Full endpoint reference, query
parameters, and JSON conventions: [NETWORK_API.md](NETWORK_API.md). A zero-dependency Python
CLI, `server/cli/exctl.py`, wraps the API (see [`server/cli/README.md`](server/cli/README.md)).

### Async model

Quart (async Flask) and discord.py share a single asyncio event loop. SQLite calls use the
synchronous `sqlite3` module; at the expected write volume (a few thousand events/hour), individual
writes complete fast enough not to block the event loop meaningfully.

### Security

No authentication. Plain HTTP only. You must ensure that the server is properly
firewalled. `/ingest`, `/chisel/*`, and `/api/*` all share the same Quart app and port,
so this applies to all three alike.

`/api/*` reads are unauthenticated, same as `/ingest`. `/api/*` mutations additionally
require a bearer token (see [NETWORK_API.md](NETWORK_API.md)'s "Attribution" section);
the token identifies which Discord user a mutation is attributed to, but firewalling
is still what controls who can reach the port at all.

Because `/ingest` accepts anything that can reach the port, the server bounds what a single
event can cost it rather than trusting the sender: cause chains, stored raw payloads, and
event timestamps are all capped or clamped server-side (see [PROTOCOL.md](PROTOCOL.md)'s
"Server-side limits").

The external Chisel endpoints (`/chisel/poll`, `/chisel/callback/*`) are additionally
reachable from outside the cluster via `CHISEL_PUBLIC_URL`; that path is protected by
basic auth at the ingress, not by the Quart app.

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
- [NETWORK_API.md](NETWORK_API.md) - `/api/*` HTTP endpoint reference
- [server/cli/README.md](server/cli/README.md) - `exctl` CLI usage
- [heap-logger/README.md](heap-logger/README.md) - heap dump leak detection service
- [pr-bot/README.md](pr-bot/README.md) - GitHub PR tracking bot (unrelated to exception tracking)
