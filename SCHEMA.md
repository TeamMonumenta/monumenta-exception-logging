# Database Schema

SQLite database, WAL mode. All timestamps are Unix epoch seconds (INTEGER) unless noted.
The schema is created and migrated by `server/tracker/db.py`; every table below is
created with `IF NOT EXISTS` at startup, and columns added after the initial release
are also applied to existing databases by `_migrate()` (see "Adding a column" below).

This document covers, in order: the tables, how a group's fingerprint is computed,
which columns are identity and which are description, how the database survives a
change to the fingerprinting rules, and the maintenance passes that run on a timer.

## Tables

### `error_groups`

One row per unique bug fingerprint. This is the primary entity.

```sql
CREATE TABLE error_groups (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint        TEXT NOT NULL UNIQUE,       -- SHA-256 hex of the fingerprint inputs
    exception_class    TEXT NOT NULL,              -- e.g. "java.lang.NullPointerException"
    message_template   TEXT NOT NULL,              -- normalized exception message
    canonical_frames   TEXT NOT NULL,              -- JSON array of the hashed app frames
    canonical_trace    TEXT NOT NULL,              -- JSON array of all frames
    logger             TEXT NOT NULL,              -- logger name
    level              TEXT NOT NULL DEFAULT '',   -- log level, e.g. "WARN"
    thread             TEXT NOT NULL DEFAULT '',   -- thread name
    log_message_template TEXT NOT NULL DEFAULT '', -- normalized accompanying log message
    cause_chain        TEXT NOT NULL DEFAULT '[]', -- JSON chained causes, outermost first
    signature          TEXT NOT NULL DEFAULT '',   -- JSON of the exact inputs that produced `fingerprint`
    first_seen         INTEGER NOT NULL,
    last_seen          INTEGER NOT NULL,
    total_count        INTEGER NOT NULL DEFAULT 0,
    status             TEXT NOT NULL DEFAULT 'active'
                           CHECK (status IN ('active', 'muted', 'resolved')),
    discord_message_id TEXT,                       -- Discord channel message ID (null until first posted)
    has_activity       INTEGER NOT NULL DEFAULT 0, -- 1 when the Discord message is stale
    muted_by           TEXT,                       -- display name of user who muted (null if never muted)
    muted_at           INTEGER,                    -- epoch seconds when muted (null if never muted)
    resolved_by        TEXT,                       -- display name of user who resolved (null if never resolved)
    resolved_at        INTEGER                     -- epoch seconds when resolved (null if never resolved)
);

CREATE UNIQUE INDEX idx_groups_fingerprint ON error_groups(fingerprint);
CREATE INDEX idx_groups_status_last_seen  ON error_groups(status, last_seen);
CREATE INDEX idx_groups_first_seen        ON error_groups(first_seen);
```

`canonical_frames`, `canonical_trace`, `logger`, `level`, `thread`,
`log_message_template` and `cause_chain` are rewritten on every occurrence and so
describe the group's most recent one; `fingerprint`, `exception_class`,
`message_template` and `signature` are not. See "Identity and description columns"
below for why the split runs where it does.

**Column notes:**

- `fingerprint`: stable identifier for a bug. See "Fingerprinting algorithm" below.
- `message_template`: the exception message with variable content replaced by tokens -
  `"... Entity name='Souls Unleashed', tags=[boss_generic]"` is stored as
  `"... Entity name=<str>, tags=<data>"`. It is a fingerprint input, and is also what
  `/search` and notify rules match against.
- `canonical_frames`: JSON array of `{class_name, method, file, line, location}` objects
  for the application frames that were hashed. Their `class_name.method` sequence is by
  construction the same for every occurrence in the group - that is what the fingerprint
  hashes - but the `file`/`line` values track the newest occurrence.
- `canonical_trace`: the same JSON shape for *all* frames of the newest occurrence, not
  just the hashed ones. Used for the full stack in group detail views, and as the text
  blob that `/search`, `GET /api/groups?search=`, and notify rules match a stack frame
  against. `/search` and `?search=` also match `log_message_template` and
  `cause_chain`; notify rules don't.
- `cause_chain`: the flattened `cause` chain of the newest occurrence, outermost cause
  first, excluding the exception itself. Capped on ingest at `MAX_CAUSE_DEPTH` causes of
  `MAX_CAUSE_FRAMES` frames each (`tracker/ingest.py`); `/ingest` is unauthenticated, so
  these caps are enforced server-side rather than trusting the sender's own budget.
- `signature`: the exact inputs that produced `fingerprint`, as JSON. See "Regrouping"
  below.
- `discord_message_id`: ID of the Discord channel message for this group. Set after the
  bot first posts it; set back to null when the bot finds the message gone, which makes
  the startup backfill re-post it. Null while no bot has ever run.
- `has_activity`: means exactly "the Discord channel message for this group is stale".
  Set to `1` when a repeat occurrence arrives, when the group's status changes
  (`mute_group`, `unmute_group`, `resolve_group`, whether triggered from Discord or the
  network API), and by the startup fingerprint migration. A brand-new group does not need
  it: ingest posts a fresh message for it instead of editing one. Cleared to `0` only
  after the bot **successfully** edits the message, so an edit that fails is retried on
  the next refresh tick rather than silently dropped. Two deliberate exceptions: a
  `discord.NotFound` leaves the flag set (the tracked message ID is cleared instead, and
  the group is re-posted by the startup backfill), and a group whose edit fails 3
  consecutive times (`_MAX_EDIT_FAILURES` in `bot.py`) has the flag cleared so a durable
  failure is not retried forever; the next occurrence or status change re-arms it. The
  refresh loop edits only flagged groups, so unchanged messages cost no API calls.
- `muted_by` / `muted_at`: attribution for the most recent mute (`display_name` and epoch
  seconds). Null if the group has never been muted, and cleared again by an unmute.
- `resolved_by` / `resolved_at`: the same for the most recent resolve, likewise cleared by
  an unmute.

---

### `occurrences`

Individual exception events. Retained for a rolling window (default: 14 days,
configurable via `EXPIRY_DAYS`). This is the largest table by a wide margin; every
query against it goes through one of the indexes below.

```sql
CREATE TABLE occurrences (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id   INTEGER NOT NULL REFERENCES error_groups(id) ON DELETE CASCADE,
    server     TEXT NOT NULL,
    timestamp  INTEGER NOT NULL,
    message    TEXT NOT NULL,   -- raw (un-normalized) exception message from the event
    log_message TEXT NOT NULL DEFAULT '',  -- raw accompanying log message from the event
    raw_event   BLOB            -- zlib-compressed ingest payload; NULL past RAW_EVENT_CAP
);

CREATE INDEX idx_occurrences_group_timestamp ON occurrences(group_id, timestamp);
CREATE INDEX idx_occurrences_timestamp       ON occurrences(timestamp);  -- for expiry sweeps
CREATE INDEX idx_occurrences_server          ON occurrences(server);
```

Rows here drive per-server breakdowns, the hourly timeline, the list of servers affected
by a group, and the raw newest message shown in group details and handed to Chisel.

`timestamp` is the event's own `timestamp_ms` in seconds, clamped to at most 5 minutes
(`_MAX_CLOCK_SKEW_S`) ahead of the server's clock. It is client-supplied on an
unauthenticated endpoint and it selects the hour bucket that drives timelines, per-server
counts and the `raw_event` cap, so a sender that steps it forward on every event would
land in a fresh bucket each time - the cap would never trip and the timeline would extend
indefinitely into the future. Only the future side needs bounding: a backdated event
lands in a bucket expiry is already sweeping, so it limits itself.

`idx_occurrences_server` backs `GET /api/servers` (`SELECT DISTINCT server`) and the
`server` filter in `list_groups`/`count_groups`. Both run synchronously on the shared
event loop: measured at 1M rows, the `server` filter's subquery costs about 2.1s
unindexed versus about 70ms with the index, and `/api/groups?server=` runs it twice
(once for the page, once for the total).

`raw_event` holds the zlib-compressed ingest payload, and is the only per-occurrence
record of the fingerprint inputs - see "Regrouping" below for what it is for and what
its limits are.

---

### `server_hour_counts`

Pre-aggregated event counts per group, per server, per hour. Written atomically
alongside `occurrences` on every ingest. Used for "top N active" and per-server
queries that would be expensive to compute from raw occurrences.

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
DO UPDATE SET count = count + 1
RETURNING count;
```

The upsert runs *before* the `occurrences` insert so that the returned post-increment
count can gate `raw_event` without a second query.

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
  `exception_class`, `message_template`, and the group's `canonical_trace` rendered as
  a text blob of `Class.method(File)` entries.
- Maximum 100 subscriptions per user. This limit is enforced by the API layer, not a
  database constraint.
- This table is not subject to the `EXPIRY_DAYS` retention window; subscriptions
  persist until explicitly removed by the user.

---

### `pending_discord_deletes`

Discord message IDs queued for deletion, because the group they belonged to no longer
exists.

```sql
CREATE TABLE pending_discord_deletes (
    message_id TEXT PRIMARY KEY
);
```

**Notes:**

- Rows are inserted by `migrate_fingerprints()` at server startup, when a merge leaves
  two channel messages for what is now one group. If the winning group has no message of
  its own it adopts the loser's instead, and nothing is queued.
- The bot's `_refresh_loop` calls `pop_pending_discord_deletes()` (SELECT + DELETE in one
  transaction) each tick and issues one Discord API delete per returned ID.
- If the bot is not running, the IDs accumulate here until it starts. The table is small
  in practice (one row per merged group per deployment).

---

### `fix_attempts`

Records of Chisel automated fix requests. One row per fix attempt, keyed by a UUID
generated at queue time.

```sql
CREATE TABLE fix_attempts (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id                   TEXT NOT NULL UNIQUE,          -- UUID generated at queue time; embedded in callback_url
    fingerprint              TEXT NOT NULL,                 -- links to the exception group (not a FK)
    status                   TEXT NOT NULL DEFAULT 'pending'
                             CHECK (status IN ('pending', 'running', 'declined',
                                               'success', 'failure', 'cancelled')),
    rendered_message         TEXT NOT NULL,                 -- rendered fix_exception_prompt.md, stored at queue time
    requested_by_discord_id  TEXT,                          -- Discord user snowflake who triggered the fix
    message                  TEXT,                          -- short human-readable status (populated on completion)
    summary                  TEXT,                          -- full agent narrative (populated on completion)
    detail                   TEXT,                          -- step-by-step execution log (populated on completion)
    pr_url                   TEXT,                          -- PR URL; only present on success
    queued_at                INTEGER NOT NULL,              -- epoch seconds; set on insert
    started_at               INTEGER,                       -- epoch seconds; set when Chisel claims the job
    completed_at             INTEGER                        -- epoch seconds; set on callback receipt or timeout
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
| `cancelled` | Withdrawn via `POST /api/fix-attempts/<job_id>/cancel` while still `pending`; `message` is `Cancelled by <name>` |

**Notes:**

- At most one `pending` or `running` attempt exists per fingerprint at a time; both the
  `:wrench:` reaction and `POST /api/groups/<id>/fix` check for one before queueing.
- `fingerprint` is a plain TEXT column, not a foreign key. Fix attempt rows are
  intentionally not cascade-deleted when the parent error group expires; they accumulate
  for the `/fix-history` slash command and `GET /api/groups/<id>/fix-attempts`.
- `fingerprint` is remapped by the startup fingerprint migration whenever the parent
  group's fingerprint changes, so attempts always refer to the current fingerprint.
- `rendered_message` is the fully rendered prompt template captured at queue time, not at
  poll time. This is intentional: the state at the moment of the fix request is what
  Chisel acts on.
- Only a `pending` attempt can be cancelled. Chisel has no channel for being told to
  stop, so a `running` one is left to finish or time out. A Chisel callback for a
  `cancelled` attempt is rejected, so it can't be turned back into a result; a timed-out
  `failure` can still be overwritten by a late callback.
- A database created before `cancelled` existed has the old five-value `CHECK`. SQLite
  can't alter a constraint in place, so `_migrate_fix_attempts_status` copies the rows
  into a new table with the current definition and swaps it in, once, at startup.
- Nothing reconciles a `running` job whose worker died, so the hourly maintenance pass
  transitions any `pending` or `running` job older than 1 hour to `failure` with
  `message = 'Timed out: no response received'` (see "Auto-expiry" below). Without that
  sweep, such a job would block every further fix request for its group forever.

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
  once, in the ephemeral reply to `/api-token create`, and is never persisted - only
  its SHA-256 hash.
- `/api-token revoke` deletes every row for the calling user's `discord_id`.
  Revocation is immediate: the next request with that token 401s.
- Maximum 20 rows per user, enforced by the API layer rather than a database constraint.
  Expired-but-not-yet-swept rows count against that cap; revoking clears them immediately.
- Not subject to `EXPIRY_DAYS`; see "Auto-expiry" below for how tokens are swept.

---

### Adding a column

`_create_tables()` creates each table in full, but an existing database keeps whatever
columns it already had. `_ADDED_COLUMNS` in `db.py` lists every column added after the
initial schema and `_migrate()` applies each missing one with `ALTER TABLE`, so a new
column must be declared in **both** places and must carry a `DEFAULT` - existing rows
are not rewritten.

---

## Fingerprinting algorithm

The fingerprint is computed by the Python ingest service from the raw event. It must be
stable across re-occurrences of the same logical bug.

**Inputs:**

1. `exception_class`, taken directly from the event.
2. `normalized_message`: the exception's own `message` field with variable content
   replaced by tokens (see below).
3. `top_app_frames`: the first (closest to throw site) `fingerprint_frame_count` frames
   (3; a `TrackerConfig` field with no environment variable) whose `class_name` starts
   with any configured application package prefix (`APP_PACKAGES`, default
   `["com.playmonumenta"]`). Only `class_name` and `method` are hashed - no file or line,
   so minor code edits that shift lines do not split a group.

**Hash:**
```python
import hashlib

components = [
    exception_class,
    normalized_message,
    "|".join(f"{f['class_name']}.{f['method']}" for f in top_app_frames)
]
fingerprint = hashlib.sha256("|".join(components).encode()).hexdigest()
```

If no frame matches an application package (e.g. the exception originates entirely in
framework code), `extract_app_frames` falls back to the top 3 frames whatever their
package. This fallback is silent and has consequences noted below and under "Regrouping".

### Message normalization

`normalize_message()` applies these substitutions in order; each rule's output is the
next rule's input.

| # | Rule | Result | Notes |
|---|---|---|---|
| 1 | Hyphenated UUID | `<uuid>` | `[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}` |
| 2 | Bare (unhyphenated) UUID | `<uuid>` | `\b[0-9a-f]{32}\b`; catches player UUIDs in Mojang auth session URLs, e.g. `/profile/3601df3d96f54dc1b10b8a4ebcefd210?unsigned=false` |
| 3 | IP address | `<ip>` | `\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b` |
| 4 | Plugin version | `<version>` | `\bv?\d+\.\d+\.\d+(?:-\d+-g[0-9a-f]{4,}\|-SNAPSHOT)+\b`; catches `git describe` strings like `v11.80.2-1-gf114425-SNAPSHOT` |
| 5 | Long opaque token | `<id>` | 32 or more `[A-Za-z0-9_-]` characters: CDN/WAF request IDs, auth tokens, hashes |
| 6 | Guild permission key | `guild.<id>` | `guild.<name>.<role>`, e.g. `guild.nova+.member`, `guild.lads.member` |
| 7 | Any number | `<N>` | `\d+(?:\.\d+)*` - **every** run of digits, not just long ones: coordinates, entity IDs, counts, single digits |
| 8 | World names in "measure distance between X and Y" | `<world1>`, `<world2>` | e.g. `plot3769`, `ringinstance101` |
| 9 | Quoted string | `<str>` | `'...'` or `"..."` up to 64 characters; entity names, class names in NPE messages. A single quote preceded by a word character does not open a match, so an apostrophe in prose is not mistaken for a quote. |
| 10 | Bracketed data | `<data>` | `[...]` up to 256 characters, innermost brackets only; boss tag lists, NBT |
| 11 | `Location{world=...{...}...}` block | `Location{<location>}` | one level of nested braces |

The order matters in four places:

- Bare UUIDs (2) are consumed before long tokens (5), so a 32-hex-character UUID always
  becomes `<uuid>` rather than `<id>`.
- Versions (4) are consumed before numbers (7), so the abbreviated git hash is not
  fragmented into `g<N>` noise.
- Guild keys (6) are consumed before numbers (7), so guild names containing digits still
  collapse to a single token.
- World names (8) are matched *after* numbers (7), because a numeric world name has
  already become `plot<N>` by that point and the rule's character class allows `<` and `>`.

Rule 10 matching innermost brackets only means normalization is **not idempotent**:
re-normalizing an already-normalized message peels one more nesting level each pass
(`[x=1, y=2, z=[a, b]]` -> `[x=<N>, y=<N>, z=<data>]` -> `<data>`). That is the reason
`signature` exists; see "Regrouping".

### What is deliberately not fingerprinted

`level`, `thread`, `log_message_template` and `cause_chain` are excluded from the hash on
purpose. The accompanying log message varies per event (Paper's scheduler puts the task id
in it) and the cause varies independently of the call site, so folding either into the
fingerprint would split one bug across many groups. They are stored so that questions
about *how* an exception was reported are answerable from the database, not so that they
can identify it.

---

## Identity and description columns

Two kinds of column live on `error_groups`, and the distinction governs when each is
written.

**Identity.** `fingerprint`, `exception_class`, `message_template` and `signature`. These
are the fingerprint's inputs, or the hash itself, and they are identical across every
occurrence in a group *by construction* - two events that normalize differently get
different fingerprints and therefore different groups. Ingest writes them once, on
insert, and never rewrites them; the only thing that does is the startup migration, which
rewrites them together with the fingerprint they belong to (and which additionally
backfills `signature` alone, for groups inserted before that column existed).

**Description.** `canonical_frames`, `canonical_trace`, `logger`, `level`, `thread`,
`log_message_template` and `cause_chain`. None of these is hashed, so rewriting them can
neither split nor merge a group. Ingest rewrites all of them on every occurrence, so they
describe the newest one.

Describing the newest occurrence rather than the first matters for two reasons:

- **Line numbers go stale.** A group first seen six months ago would otherwise hand
  Chisel a six-month-old stack trace, with line numbers that no longer correspond to
  anything in the file it is being asked to edit.
- **A frozen sample masks everything behind it.** Where one fingerprint covers several
  underlying faults - a wrapper whose trace has no application frames, so
  `extract_app_frames` falls back to platform frames identical for every failure - the
  first cause captured would be the only one ever shown, and a bug that stays unfixed is
  exactly the one most likely to have been captured first.

Because the descriptive columns are rewritten unconditionally, there is no "has this group
been populated yet" state to detect and no backfill: a group written before one of these
columns existed acquires a real value on its next occurrence, like any other.

---

## Regrouping

Changing the fingerprinting rules changes which events belong to which group. Two stored
values exist so that such a change is a rebuild rather than a database wipe.

### `signature` - what was actually hashed

`signature` stores the fingerprint's inputs as JSON: `exception_class`, the normalized
`message_template`, and the `Class.method` strings of the hashed frames.

It exists so that regrouping can be audited against what was really hashed, rather than
inferred by re-deriving it. Re-deriving is subtly lossy in both directions:

- Re-normalizing `message_template` is not idempotent - each pass peels one level of
  bracket nesting (see "Message normalization"). It converges, but the value drifts on
  the way.
- Re-running `extract_app_frames` over `canonical_frames` cannot work, because those are
  already the *selected* frames. Worse, that function falls back to `frames[:count]` when
  nothing matches, so a widened application-package list yields plausible-looking wrong
  frames rather than an error.

### `raw_event` - why the payload is kept

`occurrences.raw_event` holds the zlib-compressed ingest payload for each occurrence.

Regrouping under a *changed* rule is only possible with per-occurrence inputs.
`migrate_fingerprints` computes one new fingerprint per existing group, so it can rename
a group or merge two, but it can never **split** one: splitting requires knowing which
occurrences belong to which child, and no group-level column can answer that. `raw_event`
is the only per-occurrence record of the fingerprint inputs.

Two practical limits:

- **It is the validated payload, not the literal bytes.** The stored JSON is
  `IngestEvent.model_dump_json()`, so fields the current model does not define are dropped
  before storage. This preserves every field the protocol defines today; it does not
  preserve fields a future plugin might send to an older server.
- **It is capped twice.** Past `RAW_EVENT_CAP` (50) occurrences for the same group,
  server and hour, the column is NULL: a group already has ample samples of itself within
  an hour, and that cap is what stops a misbehaving shard turning a roughly
  1 KB-per-occurrence column into gigabytes. Because that cap counts occurrences rather
  than bytes, a second one bounds a single payload: a compressed event larger than
  `RAW_EVENT_MAX_BYTES` (64 KiB) is dropped too, since one event can legitimately be huge
  (a deep chain of wide traces) and the `flatten_cause_chain` caps truncate the
  `cause_chain` column, not the stored payload. Budget about 1 KB per retained occurrence,
  and note that nothing `VACUUM`s the database automatically.

### Startup fingerprint migration

Every time the server starts, `migrate_fingerprints()` re-fingerprints all existing groups
using the current normalization rules, before the HTTP app or the bot starts. This is what
merges groups that a tightened rule (a new token type, say) should now consider identical.

**Algorithm (single transaction):**

1. For each group, re-derive the fingerprint from the inputs ingest itself consumes: the
   frames in `canonical_trace`, and the raw `message` of the group's newest retained
   occurrence. Ties are broken by `occurrences.id`, since client-supplied timestamps are
   second-granular and tie often; without the tiebreak, two runs over the same data could
   pick different messages. A group whose occurrences have all aged out falls back to its
   stored `message_template`, the best input left. (Re-deriving from `canonical_frames`
   or from `message_template` is wrong for the reasons given under `signature` above.)
2. If the new fingerprint equals the stored one, the group's identity has not changed.
   The freshly built `signature` is still written if it differs from the stored one -
   that is what populates the column for groups inserted before it existed, since ingest
   writes `signature` only on insert. Nothing else about the row changes.
3. If no other group has the new fingerprint, update the row in place: `fingerprint`,
   `message_template`, a freshly built `signature`, and `has_activity = 1`. Any
   `fix_attempts` rows referencing the old fingerprint are remapped to the new one.
4. If another group already has the new fingerprint, fold this group (the "loser") into
   that group (the "winner"):
   - Add `total_count` and widen `first_seen` / `last_seen` on the winner, and set its
     `has_activity = 1`.
   - Re-parent all `occurrences` and merge `server_hour_counts` into the winner (upsert,
     summing counts), then delete the loser's `server_hour_counts` rows.
   - Remap `fix_attempts.fingerprint` from the loser's fingerprint to the winner's.
   - If the loser had a `discord_message_id`: the winner adopts it when it has none of
     its own, otherwise the loser's is queued in `pending_discord_deletes`.
   - Delete the loser group row.

The migration is a no-op on fresh databases and on restarts where normalization rules have
not changed. It logs a summary only when at least one group was updated or merged.

Note that step 3 does not touch `canonical_frames`: after a change to `APP_PACKAGES`, a
group's stored frames stay as they were until its next occurrence rewrites them.

---

## Auto-expiry

A background task runs every hour and purges stale data in this order. The retention
window is controlled by the `EXPIRY_DAYS` environment variable (default: 14 days; the
cutoff below is `now - EXPIRY_DAYS * 86400`):

```sql
-- 1. Delete old occurrences
DELETE FROM occurrences WHERE timestamp < :cutoff;

-- 2. Delete old aggregated counts
DELETE FROM server_hour_counts WHERE hour_bucket < :cutoff;

-- 3. Delete groups not seen within the window
DELETE FROM error_groups WHERE last_seen < :cutoff;

-- 4. Delete API tokens past their own expires_at. Independent of EXPIRY_DAYS: this
--    uses wall-clock "now", not the cutoff above.
DELETE FROM api_tokens WHERE expires_at < :now;
```

Deleting in that order means the `ON DELETE CASCADE` on `occurrences` and
`server_hour_counts` only has to clean up rows newer than the cutoff that belong to a
group being removed - it is a backstop, not the main path.

The same pass collects the `discord_message_id` of every group it deletes and hands them
to the bot, which deletes the corresponding channel messages.

`/purge older_than_days:N` runs the same routine with a caller-supplied window instead of
`EXPIRY_DAYS`. Because the token sweep is keyed on wall-clock time rather than that
window, it never expires a token early.

The hourly task also times out stale fix attempts. Any `fix_attempts` row in `pending` or
`running` status whose `queued_at` is older than 1 hour is transitioned to `failure`:

```sql
UPDATE fix_attempts
SET status = 'failure',
    message = 'Timed out: no response received',
    completed_at = :now
WHERE status IN ('pending', 'running')
  AND queued_at < :now - 3600;
```

For each timed-out attempt, the bot swaps the working reaction to the failure reaction on
the associated exception group message and DMs the requester (if one was recorded).

---

## Initialization

```sql
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
PRAGMA synchronous = NORMAL;  -- safe with WAL; faster than FULL
```

`Tracker.close()` runs `PRAGMA wal_checkpoint(FULL)` before closing, so a clean shutdown
leaves no unmerged WAL.

---

## Expected query patterns

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

See also: [PROTOCOL.md](PROTOCOL.md) for the wire format these rows are built from,
[NETWORK_API.md](NETWORK_API.md) for how they are served, and [README.md](README.md)
for the system as a whole.
