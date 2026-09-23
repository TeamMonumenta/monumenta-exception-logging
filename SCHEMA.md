# Database Schema

SQLite database, WAL mode. All timestamps are Unix epoch seconds (INTEGER) unless noted.

## Tables

### `error_groups`

One row per unique bug fingerprint. This is the primary entity.

```sql
CREATE TABLE error_groups (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint        TEXT NOT NULL UNIQUE,       -- SHA-256 hex of fingerprint components
    exception_class    TEXT NOT NULL,              -- e.g. "java.lang.NullPointerException"
    message_template   TEXT NOT NULL,              -- normalized exception message (variable content stripped)
    canonical_frames   TEXT NOT NULL,              -- JSON array of top app frames used for fingerprinting
    canonical_trace    TEXT NOT NULL,              -- JSON full stack trace from the first-ever occurrence
    logger             TEXT NOT NULL,              -- logger name from first occurrence
    level              TEXT NOT NULL DEFAULT '',   -- log level, e.g. "WARN"; '' until first captured
    log_message_template TEXT NOT NULL DEFAULT '', -- normalized accompanying log message (NOT the exception's)
    cause_chain        TEXT NOT NULL DEFAULT '[]', -- JSON array of chained causes, outermost first (<= 4)
    first_seen         INTEGER NOT NULL,
    last_seen          INTEGER NOT NULL,
    total_count        INTEGER NOT NULL DEFAULT 0,
    status             TEXT NOT NULL DEFAULT 'active'
                           CHECK (status IN ('active', 'muted', 'resolved')),
    discord_message_id TEXT,                       -- Discord channel message ID (null until first posted)
    has_activity       INTEGER NOT NULL DEFAULT 0, -- 1 when the group has unsent edits; cleared after successful edit
    muted_by           TEXT,                       -- display name of user who muted (null if never muted)
    muted_at           INTEGER,                    -- epoch seconds when muted (null if never muted)
    resolved_by        TEXT,                       -- display name of user who resolved (null if never resolved)
    resolved_at        INTEGER                     -- epoch seconds when resolved (null if never resolved)
);

CREATE UNIQUE INDEX idx_groups_fingerprint ON error_groups(fingerprint);
CREATE INDEX idx_groups_status_last_seen  ON error_groups(status, last_seen);
CREATE INDEX idx_groups_first_seen        ON error_groups(first_seen);
```

**Column notes:**

- `fingerprint`: stable identifier for a bug. See Fingerprinting section below.
- `message_template`: the exception message with variable content replaced by tokens, e.g. `"boss_generictarget only works on mobs! Entity name='<name>', tags=[<tags>]"`. Used as part of the fingerprint and for display.
- `canonical_frames`: JSON array of `{class_name, method, file, line}` objects for the top application frames. These are the frames that were hashed into the fingerprint.
- `canonical_trace`: complete JSON frame array (all frames) from the very first occurrence. Used to show the full stack in group detail views.
- `discord_message_id`: ID of the Discord channel message for this group. Set after the bot first posts; cleared (set to null) if the message is deleted externally. Null until a bot is running.
- `has_activity`: "the Discord channel message for this group is stale". Set to `1` whenever new occurrences arrive, whenever the group's status changes (`mute_group`, `unmute_group`, `resolve_group`, whether triggered from Discord or the network API), and by the startup fingerprint migration. Cleared to `0` only after the bot **successfully** edits the Discord message, so an edit that fails is retried by the next refresh tick rather than silently dropped. Two deliberate exceptions: a `discord.NotFound` leaves the flag set (the tracked message ID is cleared instead and the group is re-posted by the startup backfill), and a group whose edit fails 3 consecutive times has the flag cleared so a durable failure isn't retried forever; the next occurrence or status change re-arms it. The refresh loop uses this flag to avoid re-editing messages that have not changed.
- `muted_by` / `muted_at`: attribution for the most recent mute operation (`display_name` and epoch seconds). Null if the group has never been muted.
- `resolved_by` / `resolved_at`: attribution for the most recent resolve operation. Null if the group has never been resolved.

---

### `occurrences`

Individual exception events. Retained for a rolling window (default: 14 days, configurable via `EXPIRY_DAYS`). For high-volume groups, this table drives per-server counts and timeline data.

```sql
CREATE TABLE occurrences (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id   INTEGER NOT NULL REFERENCES error_groups(id) ON DELETE CASCADE,
    server     TEXT NOT NULL,
    timestamp  INTEGER NOT NULL,
    message    TEXT NOT NULL,   -- raw (un-normalized) exception message from the event
    log_message TEXT NOT NULL DEFAULT ''  -- raw accompanying log message from the event
);

CREATE INDEX idx_occurrences_group_timestamp ON occurrences(group_id, timestamp);
CREATE INDEX idx_occurrences_timestamp       ON occurrences(timestamp);  -- for expiry sweeps
CREATE INDEX idx_occurrences_server          ON occurrences(server);     -- for /api/servers + the server filter
```

Individual rows drive per-server breakdowns, timeline aggregation, and the list of
servers affected by a group. This is the largest table by a wide margin; every query
against it goes through one of the indexes above.

---

### `server_hour_counts`

Pre-aggregated event counts per group, per server, per hour. Written atomically alongside `occurrences` on every ingest. Used for fast "top N active" queries that would be expensive to compute from raw occurrences.

```sql
CREATE TABLE server_hour_counts (
    group_id    INTEGER NOT NULL REFERENCES error_groups(id) ON DELETE CASCADE,
    server      TEXT NOT NULL,
    hour_bucket INTEGER NOT NULL,  -- floor(timestamp / 3600) * 3600  (start of hour, epoch seconds)
    count       INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (group_id, server, hour_bucket)
);

CREATE INDEX idx_shc_hour_bucket ON server_hour_counts(hour_bucket);  -- for expiry sweeps
```

**Upsert pattern on ingest:**
```sql
INSERT INTO server_hour_counts (group_id, server, hour_bucket, count)
VALUES (?, ?, ?, 1)
ON CONFLICT (group_id, server, hour_bucket)
DO UPDATE SET count = count + 1;
```

---

### `notify_subscriptions`

Per-user regex notification rules. When a new exception group is first observed,
the bot checks all subscriptions and DMs matching users.

```sql
CREATE TABLE notify_subscriptions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    discord_user_id TEXT NOT NULL,   -- Discord user snowflake ID (stored as text)
    pattern         TEXT NOT NULL,   -- Python regex, validated at insert time
    created_at      INTEGER NOT NULL -- epoch seconds
);

CREATE INDEX idx_notify_user ON notify_subscriptions(discord_user_id);
```

**Notes:**

- `id` uses `AUTOINCREMENT`: IDs are monotonically increasing and are never reused
  after deletion. This makes IDs safe to reference in DMs (`Matched notify rule #5`)
  even after other rules have been removed.
- `discord_user_id` is the Discord user snowflake as a string (e.g. `"123456789012345678"`).
- `pattern` is stored as-is (Python `re.compile` is called at add time to validate).
  Matching at notification time uses `re.search` (case-sensitive) against three fields:
  exception class, normalized message template, and canonical trace (as a text blob).
- Maximum 100 subscriptions per user. This limit is enforced by the API layer, not a
  database constraint.
- This table is not subject to the `EXPIRY_DAYS` retention window; subscriptions
  persist until explicitly removed by the user.

---

### `pending_discord_deletes`

Discord message IDs queued for deletion. Populated by the startup fingerprint migration when two
groups are merged (the loser's channel message is orphaned and must be deleted). The bot's refresh
loop drains this table each tick, deleting each listed message from the channel.

```sql
CREATE TABLE pending_discord_deletes (
    message_id TEXT PRIMARY KEY
);
```

**Notes:**

- Rows are inserted by `migrate_fingerprints()` at server startup whenever a merge occurs and the
  losing group had a `discord_message_id`.
- The bot's `_refresh_loop` calls `pop_pending_discord_deletes()` (atomic SELECT + DELETE) and
  issues one Discord API delete per returned ID.
- If the bot is not running, the IDs accumulate in this table until it starts. The table is small
  in practice (one row per merged group per deployment).

---

### `fix_attempts`

Records of Chisel automated fix requests. One row per fix attempt, keyed by a UUID generated
at queue time.

```sql
CREATE TABLE fix_attempts (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id                   TEXT NOT NULL UNIQUE,          -- UUID generated at queue time; embedded in callback_url
    fingerprint              TEXT NOT NULL,                 -- links to the exception group (not a FK)
    status                   TEXT NOT NULL DEFAULT 'pending'
                             CHECK (status IN ('pending', 'running', 'declined', 'success', 'failure')),
    rendered_message         TEXT NOT NULL,                 -- rendered fix_exception_prompt.md, stored at queue time
    requested_by_discord_id  TEXT,                         -- Discord user snowflake who triggered the fix
    message                  TEXT,                         -- short human-readable status (populated on completion)
    summary                  TEXT,                         -- full agent narrative (populated on completion)
    detail                   TEXT,                         -- step-by-step execution log (populated on completion)
    pr_url                   TEXT,                         -- PR URL; only present on success
    queued_at                INTEGER NOT NULL,              -- epoch seconds; set on insert
    started_at               INTEGER,                      -- epoch seconds; set when poll response is claimed
    completed_at             INTEGER                       -- epoch seconds; set on callback receipt or timeout
);

CREATE INDEX idx_fix_attempts_fingerprint ON fix_attempts(fingerprint);
CREATE INDEX idx_fix_attempts_status      ON fix_attempts(status, queued_at);
```

**Status lifecycle:**

| Status | Description |
|---|---|
| `pending` | Queued; waiting for Chisel to poll |
| `running` | Claimed by Chisel via `POST /chisel/poll`; processing in progress |
| `declined` | Agent declined the task (wrong package, too complex, etc.) |
| `success` | Agent created a PR; `pr_url` is populated |
| `failure` | Agent failed, or the attempt timed out |

**Notes:**

- `fingerprint` is a plain TEXT column, not a foreign key. Fix attempt rows are intentionally
  not cascade-deleted when the parent error group expires; they accumulate for the
  `/fix-history` slash command and `GET /api/groups/<id>/fix-attempts`.
- `rendered_message` is the fully rendered prompt template captured at queue time, not at poll
  time. This is intentional: the state at the moment of the fix request is what Chisel acts on.
- If the server restarts while a job is `running`, the job remains stuck indefinitely.
  The hourly expiry task automatically transitions any `pending` or `running` job whose
  `queued_at` is older than 1 hour to `failure` with `message = 'Timed out: no response received'`.
- `fingerprint` is updated by the startup fingerprint migration (`migrate_fingerprints`)
  whenever the parent group's fingerprint changes, so pending/running jobs always refer to the
  current canonical fingerprint.

---

### `api_tokens`

Bearer tokens for the network API (see NETWORK_API.md's "Attribution" section), minted
by the `/api-token create` slash command and verified on every mutating `/api/*` request.

```sql
CREATE TABLE api_tokens (
    token_hash  TEXT PRIMARY KEY,   -- SHA-256 hex of the raw token; the raw value is never stored
    discord_id  TEXT NOT NULL,      -- Discord user snowflake the token proves (stored as text)
    created_at  INTEGER NOT NULL,   -- epoch seconds
    expires_at  INTEGER NOT NULL    -- epoch seconds; absolute, independent of EXPIRY_DAYS
);

CREATE INDEX idx_api_tokens_discord_id ON api_tokens(discord_id);  -- for revoke_api_tokens
CREATE INDEX idx_api_tokens_expires_at ON api_tokens(expires_at);  -- for the expiry sweep
```

**Notes:**

- The raw token (`secrets.token_urlsafe(32)`, 256 bits) is returned to the user exactly
  once, in the ephemeral reply to `/api-token create`, and is never persisted — only
  its SHA-256 hash.
- `/api-token revoke` deletes every row for the calling user's `discord_id`.
  Revocation is immediate — the next request with that token 401s.
- Maximum 20 live (including expired-but-not-yet-swept) tokens per user, enforced by
  the API layer, not a database constraint.
- Not subject to `EXPIRY_DAYS`; see "Auto-Expiry" below for how tokens are swept.

---

## Fingerprinting Algorithm

The fingerprint is computed by the Python ingest service from the raw event. It must be stable across re-occurrences of the same logical bug.

**Inputs:**
1. `exception_class`: taken directly from the event.
2. `normalized_message`: the exception's `message` field with variable content replaced by tokens. Normalization rules (applied in order):
   - Hyphenated UUIDs -> `<uuid>` (pattern: `[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}`)
   - Bare (unhyphenated) UUIDs -> `<uuid>` (pattern: `\b[0-9a-f]{32}\b`; catches player UUIDs embedded in Mojang auth session URLs, e.g. `/profile/3601df3d96f54dc1b10b8a4ebcefd210?unsigned=false`)
   - IP addresses -> `<ip>`
   - Plugin versions -> `<version>` (pattern: `\bv?\d+\.\d+\.\d+(?:-\d+-g[0-9a-f]{4,}|-SNAPSHOT)+\b`; catches `git describe` build strings like `v11.80.2-1-gf114425-SNAPSHOT`. A git-describe or `-SNAPSHOT` suffix is required, so plain dotted numbers are left to the number rule)
   - Long numbers (>= 4 digits) -> `<N>` (catches coordinates, entity IDs, counts)
   - Quoted string values -> `<str>` (pattern: `'[^']{1,64}'` or `"[^"]{1,64}"`)
   - Sequences of tags/NBT-like content in brackets -> `<data>`
   - Long opaque alphanumeric tokens (>= 32 characters composed of `[A-Za-z0-9_-]`) -> `<id>` (catches CDN/WAF request IDs, auth tokens, hashes, etc.; any long token that isn't a bare UUID)
   - Guild permission keys matching `guild.<name>.<role>` -> `guild.<id>` (catches varying guild names like `guild.nova+.member`, `guild.lads.member`)

   Rules are applied in order; each rule's output is the input to the next. Bare UUIDs are consumed before the long-token rule, so they always produce `<uuid>` rather than `<id>`. Versions are consumed before the number rule, so the abbreviated git hash isn't fragmented into `g<N>` noise.
3. `top_app_frames`: the first (closest to throw site) 3 frames whose `class_name` matches any of the configured application package prefixes (default: `["com.playmonumenta"]`). Each frame is represented as `"fully.qualified.ClassName.methodName"` (no file/line, to be stable across minor code changes).

**Log-event context is stored but never fingerprinted.** `level`, `log_message_template` and
`cause_chain` are excluded from the hash deliberately: the accompanying log message varies per
event (Paper's scheduler puts the task id in it) and the cause varies independently of the call
site, so folding either into the fingerprint would split one bug across many groups. They exist
so that questions about *how* an exception was reported are answerable from the database.

They are captured once per group and then left alone, like `logger` and `canonical_trace`, so
after a re-fingerprint merge they describe whichever group won rather than the earliest
occurrence. Nothing here is fingerprinted, so that cannot split or mis-group anything.

"Once per group" is not the same as "on the group's first occurrence". A group that predates
these columns has `level = ''`, and ingest fills all three from the next occurrence it sees.
Without that, the groups whose cause chains are worth reading - the long-lived ones, which are
never re-inserted because a group row is written once and only updated afterwards - would stay
empty permanently.

`level` is the sentinel because it is the one of the three a real producer always fills: log4j
events always carry a level and heap-logger hardcodes `ERROR`, whereas an empty
`log_message_template` (an event logged with no message) and a `'[]'` `cause_chain` (an
exception with no cause) are ordinary values on a fully populated group. `/ingest` is
unauthenticated and does not constrain `level`, so a crafted payload can still store an empty
one; ingest therefore backfills only from an event that carries a level, which keeps the fill
one-off rather than repeating on every occurrence of such a group.

Nothing backfills from history: the data was discarded at ingest time, so it can only arrive on
a new occurrence.

**Hash:**
```python
import hashlib, json

components = [
    exception_class,
    normalized_message,
    "|".join(f"{f['class_name']}.{f['method']}" for f in top_app_frames)
]
fingerprint = hashlib.sha256("|".join(components).encode()).hexdigest()
```

If no application frames are found (e.g. the exception originates entirely in framework code), fall back to the top 3 frames regardless of package.

---

## Auto-Expiry

A background task runs every hour and purges stale data in this order. The retention window is
controlled by the `EXPIRY_DAYS` environment variable (default: 14 days; `1209600` = 14 x 86400 s):

```sql
-- 1. Delete old occurrences (older than EXPIRY_DAYS)
DELETE FROM occurrences WHERE timestamp < strftime('%s', 'now') - 1209600;

-- 2. Delete old aggregated counts (older than EXPIRY_DAYS)
DELETE FROM server_hour_counts WHERE hour_bucket < strftime('%s', 'now') - 1209600;

-- 3. Delete groups not seen within EXPIRY_DAYS (cascades to any remaining child rows)
DELETE FROM error_groups WHERE last_seen < strftime('%s', 'now') - 1209600;

-- 4. Delete API tokens past their own expires_at (independent of EXPIRY_DAYS — this
--    uses wall-clock "now", not an EXPIRY_DAYS-scaled cutoff)
DELETE FROM api_tokens WHERE expires_at < strftime('%s', 'now');
```

The cascade deletes on `occurrences` and `server_hour_counts` (via `ON DELETE CASCADE`) ensure referential integrity when groups are removed.

The same hourly task also times out stale fix attempts. Any `fix_attempt` row in `pending` or
`running` status whose `queued_at` is older than 1 hour is transitioned to `failure`:

```sql
UPDATE fix_attempts
SET status = 'failure',
    message = 'Timed out: no response received',
    completed_at = strftime('%s', 'now')
WHERE status IN ('pending', 'running')
  AND queued_at < strftime('%s', 'now') - 3600;
```

For each timed-out attempt, the bot swaps the `:arrows_counterclockwise:` reaction to `:red_circle:`
on the associated exception group message and DMs the requester (if one was recorded).

---

## Startup Fingerprint Migration

Every time the server starts, `migrate_fingerprints()` re-fingerprints all existing groups using
the current normalization rules. This handles the case where normalization rules are tightened
(e.g. a new token type is added) and previously separate groups should now be merged.

**Algorithm (single transaction):**

1. For each group, re-compute the fingerprint from its stored `exception_class`,
   `message_template` (re-normalized), and `canonical_frames`.
2. If the new fingerprint equals the stored one, skip (no change).
3. If no other group has the new fingerprint, update the row in place (`fingerprint` and
   `message_template` columns only). Also updates `fix_attempts.fingerprint` for any fix
   attempts that reference the old fingerprint.
4. If another group already has the new fingerprint (a merge), fold the current group (the
   "loser") into that group (the "winner"):
   - Add counts and extend `first_seen` / `last_seen` on the winner.
   - Re-parent all `occurrences` and merge `server_hour_counts` (upsert).
   - Remap `fix_attempts.fingerprint` from the loser's fingerprint to the winner's.
   - Queue the loser's `discord_message_id` (if any) in `pending_discord_deletes`.
   - Delete the loser group row.

The migration is a no-op on fresh databases and on restarts where normalization rules have not
changed. It logs a summary only when at least one group was updated or merged.

---

## Initialization

```sql
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
PRAGMA synchronous = NORMAL;  -- safe with WAL; faster than FULL
```

---

## Expected Query Patterns

**Top N active groups in the last 24 hours:**
```sql
SELECT
    g.id, g.fingerprint, g.exception_class, g.message_template,
    g.first_seen, g.last_seen, g.total_count,
    SUM(s.count) AS recent_count
FROM error_groups g
JOIN server_hour_counts s ON s.group_id = g.id
WHERE g.status = 'active'
  AND s.hour_bucket >= strftime('%s', 'now') - 86400
GROUP BY g.id
ORDER BY recent_count DESC
LIMIT 20;
```

**Per-server breakdown for a group in the last 24 hours:**
```sql
SELECT server, SUM(count) AS count
FROM server_hour_counts
WHERE group_id = ?
  AND hour_bucket >= strftime('%s', 'now') - 86400
GROUP BY server
ORDER BY count DESC;
```

**Occurrence timeline for a group (hourly buckets, last 7 days):**
```sql
SELECT (timestamp / 3600) * 3600 AS hour, COUNT(*) AS count
FROM occurrences
WHERE group_id = ?
  AND timestamp >= strftime('%s', 'now') - 604800
GROUP BY hour
ORDER BY hour;
```

**New groups (first seen within last 24 hours):**
```sql
SELECT * FROM error_groups
WHERE first_seen >= strftime('%s', 'now') - 86400
ORDER BY first_seen DESC;
```
