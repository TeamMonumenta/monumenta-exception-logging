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
    first_seen         INTEGER NOT NULL,
    last_seen          INTEGER NOT NULL,
    total_count        INTEGER NOT NULL DEFAULT 0,
    status             TEXT NOT NULL DEFAULT 'active'
                           CHECK (status IN ('active', 'muted', 'resolved')),
    discord_message_id TEXT,                       -- Discord channel message ID (null until first posted)
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

- `fingerprint` — stable identifier for a bug. See Fingerprinting section below.
- `message_template` — the exception message with variable content replaced by tokens, e.g. `"boss_generictarget only works on mobs! Entity name='<name>', tags=[<tags>]"`. Used as part of the fingerprint and for display.
- `canonical_frames` — JSON array of `{class_name, method, file, line}` objects for the top application frames. These are the frames that were hashed into the fingerprint.
- `canonical_trace` — complete JSON frame array (all frames) from the very first occurrence. Used to show the full stack in group detail views.
- `discord_message_id` — ID of the Discord channel message for this group. Set after the bot first posts; cleared (set to null) if the message is deleted externally. Null until a bot is running.
- `muted_by` / `muted_at` — attribution for the most recent mute operation (`display_name` and epoch seconds). Null if the group has never been muted.
- `resolved_by` / `resolved_at` — attribution for the most recent resolve operation. Null if the group has never been resolved.

---

### `occurrences`

Individual exception events. Retained for a rolling 14-day window only. For high-volume groups, this table drives per-server counts and timeline data.

```sql
CREATE TABLE occurrences (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id   INTEGER NOT NULL REFERENCES error_groups(id) ON DELETE CASCADE,
    server     TEXT NOT NULL,
    timestamp  INTEGER NOT NULL,
    message    TEXT NOT NULL    -- raw (un-normalized) exception message from the event
);

CREATE INDEX idx_occurrences_group_timestamp ON occurrences(group_id, timestamp);
CREATE INDEX idx_occurrences_timestamp       ON occurrences(timestamp);  -- for expiry sweeps
```

**Why store raw occurrences at all?** Individual rows support:
- Per-server breakdown for a group in any arbitrary time window
- Timeline aggregation at any granularity
- Identifying affected servers for a group

At a few thousand events/hour across all servers, 14 days of occurrences is at most ~1M rows — well within SQLite's comfortable range.

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

## Fingerprinting Algorithm

The fingerprint is computed by the Python ingest service from the raw event. It must be stable across re-occurrences of the same logical bug.

**Inputs:**
1. `exception_class` — taken directly from the event.
2. `normalized_message` — the exception's `message` field with variable content replaced by tokens. Normalization rules (applied in order):
   - UUIDs → `<uuid>` (pattern: `[0-9a-f]{8}-[0-9a-f]{4}-...-[0-9a-f]{12}`)
   - IP addresses → `<ip>`
   - Long numbers (≥ 4 digits) → `<N>` (catches coordinates, entity IDs, counts)
   - Quoted string values → `<str>` (pattern: `'[^']{1,64}'` or `"[^"]{1,64}"`)
   - Sequences of tags/NBT-like content in brackets → `<data>`
3. `top_app_frames` — the first (closest to throw site) 3 frames whose `class_name` matches any of the configured application package prefixes (default: `["com.playmonumenta"]`). Each frame is represented as `"fully.qualified.ClassName.methodName"` (no file/line, to be stable across minor code changes).

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

A background task runs every hour and purges stale data in this order:

```sql
-- 1. Delete old occurrences (older than 14 days)
DELETE FROM occurrences WHERE timestamp < strftime('%s', 'now') - 1209600;

-- 2. Delete old aggregated counts (older than 14 days)
DELETE FROM server_hour_counts WHERE hour_bucket < strftime('%s', 'now') - 1209600;

-- 3. Delete groups not seen in the last 14 days (cascades to any remaining child rows)
DELETE FROM error_groups WHERE last_seen < strftime('%s', 'now') - 1209600;
```

The cascade deletes on `occurrences` and `server_hour_counts` (via `ON DELETE CASCADE`) ensure referential integrity when groups are removed.

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
