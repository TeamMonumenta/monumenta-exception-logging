# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Byron Marohn
"""
Internal Python API for the Monumenta exception tracker.

Consumed directly by the Discord bot and any other internal tooling — this is
not an HTTP API. All methods are synchronous. Callers in an async context
should wrap calls with asyncio.get_event_loop().run_in_executor(None, func).
"""

import hashlib
import json
import secrets
import sqlite3
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from .config import TrackerConfig
from . import db
from .ingest import IngestEvent, ingest_event as _ingest_event


# --- Data classes ---

@dataclass
class FrameSummary:
    class_name: str
    method: str
    file: Optional[str]
    line: int  # -1 if unknown (native method or compiled without debug info)


@dataclass
class GroupSummary:
    fingerprint: str
    exception_class: str
    message_template: str   # normalized exception message; variable parts replaced with tokens
    status: str             # 'active' | 'muted' | 'resolved'
    first_seen: datetime
    last_seen: datetime
    total_count: int
    recent_count: int               # occurrences within the queried time window
    server_counts: dict[str, int]   # server_id -> count within the queried time window


@dataclass
class FixAttemptJob:
    job_id: str
    fingerprint: str
    rendered_message: str


@dataclass
class FixAttemptStatus:
    job_id: str
    fingerprint: str
    status: str              # 'pending' | 'running' | 'declined' | 'success' | 'failure'
    message: Optional[str]
    summary: Optional[str]
    pr_url: Optional[str]
    queued_at: datetime
    started_at: Optional[datetime]
    completed_at: Optional[datetime]


@dataclass
class OccurrenceSummary:
    timestamp: datetime
    server: str
    message: str


@dataclass
class GroupDetails:
    fingerprint: str
    exception_class: str
    message_template: str
    status: str
    first_seen: datetime
    last_seen: datetime
    total_count: int
    logger: str
    canonical_frames: list[FrameSummary]  # top app frames that were hashed into the fingerprint
    canonical_trace: list[FrameSummary]   # full stack trace captured from the first occurrence only
    servers_affected: list[str]           # servers seen within the retention window
    server_counts_24h: dict[str, int]     # fixed 24-hour window
    hourly_timeline: list[tuple[datetime, int]]  # (hour_start, count), fixed 7-day window
    latest_message: Optional[str] = None  # most recent raw (un-normalized) exception message
    muted_by: Optional[str] = None
    muted_at: Optional[datetime] = None
    resolved_by: Optional[str] = None
    resolved_at: Optional[datetime] = None


# --- Stateless helpers ---

def _ts_to_dt(ts: int) -> datetime:
    return datetime.fromtimestamp(ts, tz=timezone.utc)


def _frames_from_json(json_str: str) -> list[FrameSummary]:
    return [
        FrameSummary(
            class_name=f['class_name'],
            method=f['method'],
            file=f.get('file'),
            line=f.get('line', -1),
        )
        for f in json.loads(json_str)
    ]


def _row_to_fix_attempt_status(row: sqlite3.Row) -> FixAttemptStatus:
    return FixAttemptStatus(
        job_id=row['job_id'],
        fingerprint=row['fingerprint'],
        status=row['status'],
        message=row['message'],
        summary=row['summary'],
        pr_url=row['pr_url'],
        queued_at=_ts_to_dt(row['queued_at']),
        started_at=_ts_to_dt(row['started_at']) if row['started_at'] is not None else None,
        completed_at=_ts_to_dt(row['completed_at']) if row['completed_at'] is not None else None,
    )


def _clamp_int(value: int, minimum: int, maximum: int) -> int:
    return max(minimum, min(value, maximum))


def _row_to_summary(
    row: sqlite3.Row, recent_count: int, server_counts: dict[str, int]
) -> GroupSummary:
    return GroupSummary(
        fingerprint=row['fingerprint'],
        exception_class=row['exception_class'],
        message_template=row['message_template'],
        status=row['status'],
        first_seen=_ts_to_dt(row['first_seen']),
        last_seen=_ts_to_dt(row['last_seen']),
        total_count=row['total_count'],
        recent_count=recent_count,
        server_counts=server_counts,
    )


# --- list_groups / count_groups constants ---

# 'all' is accepted as an explicit synonym for "no status filter" (i.e. None). Callers
# reach for it naturally — ?status=all reads better than omitting the parameter — and
# rejecting it with a 400 was a needless trap.
_VALID_STATUSES = ('active', 'muted', 'resolved', 'all')

# Maps an accepted `sort` value to the ORDER BY column expression. 'recent' is handled
# separately (it orders by a windowed aggregate, not a plain error_groups column) but is
# still a valid `sort` value — see _VALID_SORTS. This is the one place a request value is
# interpolated into SQL, so it must stay a literal allowlist — never build this dict from
# request input.
_SORT_COLUMNS: dict[str, str] = {
    'last_seen': 'g.last_seen',
    'first_seen': 'g.first_seen',
    'total_count': 'g.total_count',
}

_VALID_SORTS = frozenset({*_SORT_COLUMNS.keys(), 'recent'})

_LIST_GROUPS_MAX_LIMIT = 500


# --- Tracker ---

class Tracker:
    def __init__(self, config: TrackerConfig):
        self._config = config
        self._conn = db.init_db(config)

    def close(self) -> None:
        """Checkpoint WAL and close the database connection cleanly."""
        self._conn.execute("PRAGMA wal_checkpoint(FULL)")
        self._conn.close()

    # --- Ingest ---

    def ingest_event(self, event: IngestEvent) -> tuple[str, bool]:
        """Process one exception event from the plugin. Returns (fingerprint, is_new_group).

        is_new_group is True when the group is first inserted (not previously in the DB).
        Status is never changed by ingest — active, muted, and resolved groups all
        receive count and last_seen updates. A resolved group will stop updating
        naturally once the fix reaches production and then age out via expiry.
        """
        return _ingest_event(event, self._conn, self._config)

    # --- Queries ---

    def _get_server_counts(self, group_id: int, cutoff_s: int) -> dict[str, int]:
        rows = self._conn.execute(
            """SELECT server, SUM(count) AS cnt
               FROM server_hour_counts
               WHERE group_id = ? AND hour_bucket >= ?
               GROUP BY server""",
            (group_id, cutoff_s)
        ).fetchall()
        return {row['server']: row['cnt'] for row in rows}

    def get_top_active_groups(self, limit: int = 20, window_hours: int = 24) -> list[GroupSummary]:
        cutoff = int(time.time()) - window_hours * 3600
        rows = self._conn.execute(
            """SELECT g.id, g.fingerprint, g.exception_class, g.message_template,
                      g.status, g.first_seen, g.last_seen, g.total_count,
                      SUM(s.count) AS recent_count
               FROM error_groups g
               JOIN server_hour_counts s ON s.group_id = g.id
               WHERE g.status = 'active'
                 AND s.hour_bucket >= ?
               GROUP BY g.id
               ORDER BY recent_count DESC
               LIMIT ?""",
            (cutoff, limit)
        ).fetchall()
        result: list[GroupSummary] = []
        for row in rows:
            server_counts = self._get_server_counts(row['id'], cutoff)
            result.append(_row_to_summary(row, row['recent_count'], server_counts))
        return result

    def get_new_groups(self, hours: int = 24, before: Optional[int] = None) -> list[GroupSummary]:
        """Return groups first seen within the `hours`-hour window ending at `before`.

        If `before` is None the window ends at the current time.
        Includes groups of all statuses — a newly detected exception that was
        immediately muted or resolved still appears here.
        """
        end = before if before is not None else int(time.time())
        cutoff = end - hours * 3600
        if before is not None:
            # Use the last occurrence strictly before `before` as last_seen so the
            # displayed timestamp isn't contaminated by occurrences after the window.
            rows = self._conn.execute(
                """SELECT g.id, g.fingerprint, g.exception_class, g.message_template,
                          g.status, g.first_seen, g.total_count,
                          COALESCE(
                              (SELECT MAX(o.timestamp) FROM occurrences o
                               WHERE o.group_id = g.id AND o.timestamp < ?),
                              g.first_seen
                          ) AS last_seen
                   FROM error_groups g
                   WHERE g.first_seen >= ? AND g.first_seen < ?
                   ORDER BY g.first_seen DESC""",
                (end, cutoff, end)
            ).fetchall()
        else:
            rows = self._conn.execute(
                """SELECT id, fingerprint, exception_class, message_template,
                          status, first_seen, last_seen, total_count
                   FROM error_groups
                   WHERE first_seen >= ? AND first_seen <= ?
                   ORDER BY first_seen DESC""",
                (cutoff, end)
            ).fetchall()
        result: list[GroupSummary] = []
        for row in rows:
            recent_count_row = self._conn.execute(
                """SELECT COALESCE(SUM(count), 0) AS cnt
                   FROM server_hour_counts
                   WHERE group_id = ? AND hour_bucket >= ?""",
                (row['id'], cutoff)
            ).fetchone()
            server_counts = self._get_server_counts(row['id'], cutoff)
            result.append(_row_to_summary(row, recent_count_row['cnt'], server_counts))
        return result

    def get_group_details(self, fingerprint: str) -> Optional[GroupDetails]:
        """Return full details for a single group.

        hourly_timeline and servers_affected use fixed windows (7 days and 24 hours
        respectively) that are not configurable per-call.
        """
        row = self._conn.execute(
            """SELECT id, fingerprint, exception_class, message_template,
                      status, first_seen, last_seen, total_count,
                      logger, canonical_frames, canonical_trace,
                      muted_by, muted_at, resolved_by, resolved_at
               FROM error_groups
               WHERE fingerprint = ?""",
            (fingerprint,)
        ).fetchone()
        if row is None:
            return None

        group_id = row['id']
        now = int(time.time())
        cutoff_24h = now - 86400
        cutoff_7d = now - 604800
        cutoff_retention = now - self._config.expiry_days * 86400

        server_rows = self._conn.execute(
            'SELECT DISTINCT server FROM occurrences WHERE group_id = ? AND timestamp >= ?',
            (group_id, cutoff_retention)
        ).fetchall()

        timeline_rows = self._conn.execute(
            """SELECT (timestamp / 3600) * 3600 AS hour, COUNT(*) AS count
               FROM occurrences
               WHERE group_id = ? AND timestamp >= ?
               GROUP BY hour
               ORDER BY hour""",
            (group_id, cutoff_7d)
        ).fetchall()

        latest_msg_row = self._conn.execute(
            "SELECT message FROM occurrences WHERE group_id = ? ORDER BY timestamp DESC LIMIT 1",
            (group_id,)
        ).fetchone()
        latest_message = latest_msg_row['message'] if latest_msg_row is not None else None

        return GroupDetails(
            fingerprint=row['fingerprint'],
            exception_class=row['exception_class'],
            message_template=row['message_template'],
            status=row['status'],
            first_seen=_ts_to_dt(row['first_seen']),
            last_seen=_ts_to_dt(row['last_seen']),
            total_count=row['total_count'],
            logger=row['logger'],
            canonical_frames=_frames_from_json(row['canonical_frames']),
            canonical_trace=_frames_from_json(row['canonical_trace']),
            servers_affected=[r['server'] for r in server_rows],
            server_counts_24h=self._get_server_counts(group_id, cutoff_24h),
            hourly_timeline=[(_ts_to_dt(r['hour']), r['count']) for r in timeline_rows],
            latest_message=latest_message,
            muted_by=row['muted_by'],
            muted_at=_ts_to_dt(row['muted_at']) if row['muted_at'] is not None else None,
            resolved_by=row['resolved_by'],
            resolved_at=_ts_to_dt(row['resolved_at']) if row['resolved_at'] is not None else None,
        )

    def get_groups_for_server(
        self, server_id: str, limit: int = 20, window_hours: int = 24
    ) -> list[GroupSummary]:
        cutoff = int(time.time()) - window_hours * 3600
        rows = self._conn.execute(
            """SELECT g.id, g.fingerprint, g.exception_class, g.message_template,
                      g.status, g.first_seen, g.last_seen, g.total_count,
                      SUM(s.count) AS recent_count
               FROM error_groups g
               JOIN server_hour_counts s ON s.group_id = g.id
               WHERE g.status = 'active'
                 AND s.server = ?
                 AND s.hour_bucket >= ?
               GROUP BY g.id
               ORDER BY recent_count DESC
               LIMIT ?""",
            (server_id, cutoff, limit)
        ).fetchall()
        result: list[GroupSummary] = []
        for row in rows:
            server_counts = self._get_server_counts(row['id'], cutoff)
            result.append(_row_to_summary(row, row['recent_count'], server_counts))
        return result

    def search_groups(self, query: str, limit: int = 20, window_hours: int = 24) -> list[GroupSummary]:
        """Case-insensitive substring search over exception_class, message_template, and canonical_trace.

        canonical_trace is stored as a JSON text blob, so a LIKE match over it catches file names
        (e.g. "ParticleManager.java"), class names, and method names anywhere in the full stack trace.

        Includes groups of all statuses.
        """
        cutoff = int(time.time()) - window_hours * 3600
        pattern = f'%{query.lower()}%'
        rows = self._conn.execute(
            """SELECT id, fingerprint, exception_class, message_template,
                      status, first_seen, last_seen, total_count
               FROM error_groups
               WHERE LOWER(exception_class) LIKE ?
                  OR LOWER(message_template) LIKE ?
                  OR LOWER(canonical_trace) LIKE ?
               ORDER BY last_seen DESC
               LIMIT ?""",
            (pattern, pattern, pattern, limit)
        ).fetchall()
        result: list[GroupSummary] = []
        for row in rows:
            recent_count_row = self._conn.execute(
                """SELECT COALESCE(SUM(count), 0) AS cnt
                   FROM server_hour_counts
                   WHERE group_id = ? AND hour_bucket >= ?""",
                (row['id'], cutoff)
            ).fetchone()
            server_counts = self._get_server_counts(row['id'], cutoff)
            result.append(_row_to_summary(row, recent_count_row['cnt'], server_counts))
        return result

    def _get_groups_by_status(self, status: str, limit: int, window_hours: int) -> list[GroupSummary]:
        cutoff = int(time.time()) - window_hours * 3600
        rows = self._conn.execute(
            """SELECT id, fingerprint, exception_class, message_template,
                      status, first_seen, last_seen, total_count
               FROM error_groups
               WHERE status = ?
               ORDER BY last_seen DESC
               LIMIT ?""",
            (status, limit)
        ).fetchall()
        result: list[GroupSummary] = []
        for row in rows:
            recent_count_row = self._conn.execute(
                """SELECT COALESCE(SUM(count), 0) AS cnt
                   FROM server_hour_counts
                   WHERE group_id = ? AND hour_bucket >= ?""",
                (row['id'], cutoff)
            ).fetchone()
            server_counts = self._get_server_counts(row['id'], cutoff)
            result.append(_row_to_summary(row, recent_count_row['cnt'], server_counts))
        return result

    def get_muted_groups(self, limit: int = 20, window_hours: int = 24) -> list[GroupSummary]:
        return self._get_groups_by_status('muted', limit, window_hours)

    def get_resolved_groups(self, limit: int = 20, window_hours: int = 24) -> list[GroupSummary]:
        """Return resolved groups ordered by most recently active.

        A non-zero recent_count indicates the fix has not yet fully taken effect.
        """
        return self._get_groups_by_status('resolved', limit, window_hours)

    def _list_groups_where(
        self,
        status: Optional[str],
        server: Optional[str],
        search: Optional[str],
        new_within_hours: Optional[int],
    ) -> tuple[str, list[Any]]:
        """Build the WHERE clause + bound params shared by list_groups and count_groups.

        window_hours, sort, limit and offset are NOT filters and must never appear here —
        divergence between this and the row count would make `total` disagree with `groups`.
        """
        clauses: list[str] = []
        params: list[Any] = []
        if status is not None and status != 'all':
            clauses.append("g.status = ?")
            params.append(status)
        if server is not None:
            clauses.append(
                "g.id IN (SELECT DISTINCT group_id FROM occurrences WHERE server = ?)"
            )
            params.append(server)
        if search is not None:
            pattern = f'%{search.lower()}%'
            clauses.append(
                "(LOWER(g.exception_class) LIKE ? OR LOWER(g.message_template) LIKE ? "
                "OR LOWER(g.canonical_trace) LIKE ?)"
            )
            params.extend([pattern, pattern, pattern])
        if new_within_hours is not None:
            cutoff = int(time.time()) - new_within_hours * 3600
            clauses.append("g.first_seen >= ?")
            params.append(cutoff)
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        return where_sql, params

    def _batch_group_aggregates(
        self, group_ids: list[int], cutoff: int
    ) -> tuple[dict[int, int], dict[int, dict[str, int]]]:
        """Fetch recent_count and per-server counts for a page of group ids in two
        batched queries (never one aggregate query per row)."""
        if not group_ids:
            return {}, {}
        placeholders = ','.join('?' * len(group_ids))
        recent_rows = self._conn.execute(
            f"""SELECT group_id, SUM(count) AS cnt FROM server_hour_counts
                WHERE group_id IN ({placeholders}) AND hour_bucket >= ?
                GROUP BY group_id""",
            [*group_ids, cutoff]
        ).fetchall()
        recent_counts = {row['group_id']: row['cnt'] for row in recent_rows}

        server_rows = self._conn.execute(
            f"""SELECT group_id, server, SUM(count) AS cnt FROM server_hour_counts
                WHERE group_id IN ({placeholders}) AND hour_bucket >= ?
                GROUP BY group_id, server""",
            [*group_ids, cutoff]
        ).fetchall()
        server_counts: dict[int, dict[str, int]] = {}
        for row in server_rows:
            server_counts.setdefault(row['group_id'], {})[row['server']] = row['cnt']

        return recent_counts, server_counts

    def list_groups(
        self,
        status: Optional[str] = None,
        server: Optional[str] = None,
        search: Optional[str] = None,
        new_within_hours: Optional[int] = None,
        window_hours: int = 24,
        sort: str = "last_seen",
        limit: int = 50,
        offset: int = 0,
    ) -> list[GroupSummary]:
        """"Everything, with filters" — backs GET /api/groups.

        `server` matches if the server appears anywhere in the group's occurrences,
        regardless of window_hours (deliberately different from get_groups_for_server,
        which requires status='active' and restricts to the window — that method is
        unchanged and continues to back only the /server slash command).

        `status` accepts 'active' | 'muted' | 'resolved' | 'all' | None; the last two
        both mean "no status filter".

        Every sort is tie-broken by `g.id DESC` so that paging with `offset` is stable.
        Without it, ties (equal last_seen, or the very common case of many groups sharing
        a recent_count of 0) leave the row order up to the query planner, and successive
        pages can repeat or skip rows.

        Raises ValueError for an unknown `status` or `sort`. Out-of-range integers are
        clamped rather than rejected: limit is hard-capped at 500, offset/window_hours/
        new_within_hours are clamped to non-negative and no larger than the retention
        window (expiry_days * 24 hours) — beyond that there is nothing to find.
        """
        if status is not None and status not in _VALID_STATUSES:
            raise ValueError(f"invalid status: {status!r}")
        if sort not in _VALID_SORTS:
            raise ValueError(f"invalid sort: {sort!r}")

        max_window = self._config.expiry_days * 24
        window_hours = _clamp_int(window_hours, 0, max_window)
        if new_within_hours is not None:
            new_within_hours = _clamp_int(new_within_hours, 0, max_window)
        limit = _clamp_int(limit, 0, _LIST_GROUPS_MAX_LIMIT)
        offset = max(0, offset)

        where_sql, params = self._list_groups_where(status, server, search, new_within_hours)
        cutoff = int(time.time()) - window_hours * 3600

        if sort == 'recent':
            rows = self._conn.execute(
                f"""SELECT g.id, g.fingerprint, g.exception_class, g.message_template,
                           g.status, g.first_seen, g.last_seen, g.total_count,
                           COALESCE(
                               (SELECT SUM(s.count) FROM server_hour_counts s
                                WHERE s.group_id = g.id AND s.hour_bucket >= ?), 0
                           ) AS sort_recent_count
                    FROM error_groups g
                    {where_sql}
                    ORDER BY sort_recent_count DESC, g.id DESC
                    LIMIT ? OFFSET ?""",
                [cutoff, *params, limit, offset]
            ).fetchall()
        else:
            order_col = _SORT_COLUMNS[sort]
            rows = self._conn.execute(
                f"""SELECT g.id, g.fingerprint, g.exception_class, g.message_template,
                           g.status, g.first_seen, g.last_seen, g.total_count
                    FROM error_groups g
                    {where_sql}
                    ORDER BY {order_col} DESC, g.id DESC
                    LIMIT ? OFFSET ?""",
                [*params, limit, offset]
            ).fetchall()

        group_ids = [row['id'] for row in rows]
        recent_counts, server_counts = self._batch_group_aggregates(group_ids, cutoff)

        return [
            _row_to_summary(row, recent_counts.get(row['id'], 0), server_counts.get(row['id'], {}))
            for row in rows
        ]

    def count_groups(
        self,
        status: Optional[str] = None,
        server: Optional[str] = None,
        search: Optional[str] = None,
        new_within_hours: Optional[int] = None,
    ) -> int:
        """Total matching rows for the same filters as list_groups (before limit/offset).

        Raises ValueError for an unknown `status`, matching list_groups.
        """
        if status is not None and status not in _VALID_STATUSES:
            raise ValueError(f"invalid status: {status!r}")
        max_window = self._config.expiry_days * 24
        if new_within_hours is not None:
            new_within_hours = _clamp_int(new_within_hours, 0, max_window)
        where_sql, params = self._list_groups_where(status, server, search, new_within_hours)
        row = self._conn.execute(
            f"SELECT COUNT(*) AS cnt FROM error_groups g {where_sql}", params
        ).fetchone()
        return int(row['cnt'])

    def get_recent_occurrences(self, fingerprint: str, limit: int = 20) -> list[OccurrenceSummary]:
        """Return the most recent raw occurrences for a group, newest first."""
        group_row = self._conn.execute(
            "SELECT id FROM error_groups WHERE fingerprint = ?", (fingerprint,)
        ).fetchone()
        if group_row is None:
            return []
        limit = _clamp_int(limit, 0, _LIST_GROUPS_MAX_LIMIT)
        rows = self._conn.execute(
            "SELECT timestamp, server, message FROM occurrences WHERE group_id = ? "
            "ORDER BY timestamp DESC LIMIT ?",
            (group_row['id'], limit)
        ).fetchall()
        return [
            OccurrenceSummary(timestamp=_ts_to_dt(row['timestamp']), server=row['server'],
                               message=row['message'])
            for row in rows
        ]

    def get_distinct_servers(self) -> list[str]:
        """Return every server that has ever contributed an occurrence, sorted.

        Reads the same `occurrences` table list_groups' `server` filter reads (§6.1) —
        must stay in sync so a server listed here always matches at least one group in
        list_groups(server=...).
        """
        rows = self._conn.execute(
            "SELECT DISTINCT server FROM occurrences ORDER BY server"
        ).fetchall()
        return [row['server'] for row in rows]

    def group_exists(self, fingerprint: str) -> bool:
        """Cheap indexed existence check for a full fingerprint.

        Exists so callers that only need "is this a real group?" don't pay for
        get_group_details, which issues four extra queries (servers, 7-day timeline,
        latest message, 24h server counts) whose results they then throw away.
        """
        row = self._conn.execute(
            "SELECT 1 FROM error_groups WHERE fingerprint = ?", (fingerprint,)
        ).fetchone()
        return row is not None

    def get_discord_message_id(self, fingerprint: str) -> Optional[str]:
        """Single indexed lookup of a group's tracked Discord message ID, or None."""
        row = self._conn.execute(
            "SELECT discord_message_id FROM error_groups WHERE fingerprint = ?",
            (fingerprint,)
        ).fetchone()
        return row['discord_message_id'] if row is not None else None

    # --- Muting and resolution ---

    def mute_group(self, fingerprint: str, actor: str = "unknown") -> bool:
        now = int(time.time())
        with self._conn:
            cur = self._conn.execute(
                "UPDATE error_groups SET status = 'muted', muted_by = ?, muted_at = ?, "
                "has_activity = 1 "
                "WHERE fingerprint = ?",
                (actor, now, fingerprint)
            )
        return cur.rowcount > 0

    def unmute_group(  # pylint: disable=unused-argument
        self, fingerprint: str, actor: str = "unknown"
    ) -> bool:
        """Reactivate a group, clearing any mute/resolve attribution.

        `actor` is accepted for log/audit symmetry with mute_group/resolve_group; the
        schema records current state only, so there is nothing to persist it into.
        """
        with self._conn:
            cur = self._conn.execute(
                "UPDATE error_groups SET status = 'active', "
                "muted_by = NULL, muted_at = NULL, "
                "resolved_by = NULL, resolved_at = NULL, "
                "has_activity = 1 "
                "WHERE fingerprint = ?",
                (fingerprint,)
            )
        return cur.rowcount > 0

    def resolve_group(self, fingerprint: str, actor: str = "unknown") -> bool:
        """Mark a group resolved. Ingest will not reactivate it — the group
        accumulates counts silently and ages out via expiry once errors stop arriving.
        """
        now = int(time.time())
        with self._conn:
            cur = self._conn.execute(
                "UPDATE error_groups SET status = 'resolved', resolved_by = ?, resolved_at = ?, "
                "has_activity = 1 "
                "WHERE fingerprint = ?",
                (actor, now, fingerprint)
            )
        return cur.rowcount > 0

    def set_discord_message_id(self, fingerprint: str, message_id: Optional[str]) -> None:
        """Persist (or clear) the Discord message ID for a group."""
        db.set_discord_message_id(self._conn, fingerprint, message_id)

    def get_all_discord_messages(self) -> list[tuple[str, str]]:
        """Return [(fingerprint, discord_message_id), ...] for all tracked groups."""
        return db.get_all_discord_messages(self._conn)

    def get_active_discord_messages(self) -> list[tuple[str, str]]:
        """Return [(fingerprint, discord_message_id), ...] only for groups with has_activity=1."""
        return db.get_active_discord_messages(self._conn)

    def clear_has_activity(self, fingerprint: str) -> None:
        """Reset has_activity to 0 after the Discord message for a group has been edited."""
        db.clear_has_activity(self._conn, fingerprint)

    def get_fingerprint_by_short_id(self, short_id: str) -> Optional[str]:
        """Look up a full fingerprint from an 8-character prefix (short ID)."""
        row = self._conn.execute(
            "SELECT fingerprint FROM error_groups WHERE SUBSTR(fingerprint, 1, 8) = ?",
            (short_id,)
        ).fetchone()
        return row['fingerprint'] if row is not None else None

    def get_fingerprint_by_discord_message_id(self, message_id: str) -> Optional[str]:
        """Look up a fingerprint by its tracked Discord message ID."""
        return db.get_fingerprint_by_discord_message_id(self._conn, message_id)

    # --- Notify subscriptions ---

    def add_notify_subscription(self, discord_user_id: str, pattern: str) -> int:
        """Store a new notify subscription. Returns the stable DB id.

        Raises ValueError if the user already has 100 subscriptions.
        The caller must validate the regex before calling this method.
        """
        if db.count_notify_subscriptions(self._conn, discord_user_id) >= 100:
            raise ValueError("Maximum of 100 notify subscriptions per user")
        return db.add_notify_subscription(
            self._conn, discord_user_id, pattern, int(time.time())
        )

    def list_notify_subscriptions(
        self, discord_user_id: str
    ) -> list[tuple[int, str, datetime]]:
        """Return [(id, pattern, created_at), ...] for the user's subscriptions, ordered by id."""
        rows = db.list_notify_subscriptions(self._conn, discord_user_id)
        return [(sub_id, pattern, _ts_to_dt(ts)) for sub_id, pattern, ts in rows]

    def remove_notify_subscription(self, discord_user_id: str, sub_id: int) -> bool:
        """Remove subscription by its DB id, scoped to the owning user."""
        return db.remove_notify_subscription(self._conn, discord_user_id, sub_id)

    def get_all_notify_subscriptions(self) -> list[tuple[int, str, str]]:
        """Return [(sub_id, discord_user_id, pattern), ...] for all subscriptions."""
        return db.get_all_notify_subscriptions(self._conn)

    # --- Network API bearer tokens ---

    def create_api_token(self, discord_id: str, ttl_hours: int) -> tuple[str, int]:
        """Mint a new bearer token proving discord_id, valid for ttl_hours hours.

        Returns (raw_token, expires_at_epoch_s). Only the token's SHA-256 hash is
        ever persisted; the raw value is returned once here and cannot be recovered
        later — losing it means minting a new one.

        Raises ValueError if the user already has 20 tokens. Expired-but-not-yet-swept
        rows count against the cap too (the hourly expiry pass hasn't reclaimed them
        yet); calling /api-token revoke first clears them immediately.
        """
        if db.count_api_tokens(self._conn, discord_id) >= 20:
            raise ValueError(
                "Maximum of 20 API tokens per user — revoke old ones with /api-token revoke"
            )
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        created_at = int(time.time())
        expires_at = created_at + ttl_hours * 3600
        db.create_api_token(self._conn, token_hash, discord_id, created_at, expires_at)
        return token, expires_at

    def verify_api_token(self, token: str) -> Optional[str]:
        """Return the discord_id bound to token, or None if unknown/expired/revoked."""
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        row = db.get_api_token(self._conn, token_hash)
        if row is None or row['expires_at'] < int(time.time()):
            return None
        return str(row['discord_id'])

    def revoke_api_tokens(self, discord_id: str) -> int:
        """Revoke every token belonging to discord_id. Returns the number revoked."""
        return db.revoke_api_tokens(self._conn, discord_id)

    def get_active_fingerprints(self) -> list[str]:
        """Return fingerprints of all active groups, newest first."""
        rows = self._conn.execute(
            "SELECT fingerprint FROM error_groups WHERE status = 'active' "
            "ORDER BY last_seen DESC"
        ).fetchall()
        return [row['fingerprint'] for row in rows]

    def get_fingerprints_without_discord_message(self) -> list[str]:
        """Return fingerprints of all groups that have no Discord message ID, newest first."""
        rows = self._conn.execute(
            "SELECT fingerprint FROM error_groups WHERE discord_message_id IS NULL "
            "ORDER BY last_seen DESC"
        ).fetchall()
        return [row['fingerprint'] for row in rows]

    # --- Fingerprint migration ---

    def migrate_fingerprints(self) -> dict[str, Any]:
        """Re-fingerprint all groups with the current normalization rules."""
        return db.migrate_fingerprints(self._conn, self._config.app_packages)

    def add_pending_discord_delete(self, message_id: str) -> None:
        """Queue a Discord message ID for deletion on the next refresh loop tick."""
        db.add_pending_discord_delete(self._conn, message_id)

    def pop_pending_discord_deletes(self) -> list[str]:
        """Return and clear all pending Discord message IDs queued for deletion."""
        return db.pop_pending_discord_deletes(self._conn)

    # --- Maintenance ---

    def timeout_stale_fix_attempts(
        self, timeout_seconds: int = 3600
    ) -> list[tuple[str, str, Optional[str]]]:
        """Mark pending/running fix attempts older than timeout_seconds as failed.

        Returns list of (job_id, fingerprint, requested_by_discord_id).
        """
        return db.timeout_stale_fix_attempts(self._conn, timeout_seconds)

    def run_expiry(self) -> dict[str, Any]:
        """Delete occurrences, aggregates, and groups older than expiry_days, plus any
        API tokens past their own (unrelated) expires_at.

        Deletes in dependency order (occurrences and aggregates first) before
        removing groups, so ON DELETE CASCADE never fires unexpectedly.
        Returns row counts plus discord_message_ids for groups that had tracked messages.
        """
        return db.run_expiry(self._conn, self._config.expiry_days)

    # --- Chisel fix attempts ---

    def queue_fix_attempt(
        self,
        fingerprint: str,
        rendered_message: str,
        requested_by_discord_id: Optional[str] = None,
    ) -> str:
        """Queue a new fix attempt for the given group. Returns the new job_id (UUID)."""
        job_id = str(uuid.uuid4())
        db.insert_fix_attempt(
            self._conn, job_id, fingerprint, rendered_message,
            int(time.time()), requested_by_discord_id,
        )
        return job_id

    def has_active_fix_attempt(self, fingerprint: str) -> bool:
        """Return True if a pending or running fix attempt exists for this fingerprint."""
        return db.has_active_fix_attempt(self._conn, fingerprint)

    def get_fix_attempt(self, job_id: str) -> Optional[FixAttemptStatus]:
        """Return a single fix attempt's current state, or None if job_id is unknown."""
        row = db.get_fix_attempt(self._conn, job_id)
        return _row_to_fix_attempt_status(row) if row is not None else None

    def get_fix_attempts_for_group(
        self, fingerprint: str, limit: int = 20
    ) -> list[FixAttemptStatus]:
        """Return fix attempt history for a group, newest first.

        `limit` is clamped to the same hard cap as the other listing methods so an API
        caller can page further back than the default 20 without being able to ask for
        an unbounded result set.
        """
        limit = _clamp_int(limit, 0, _LIST_GROUPS_MAX_LIMIT)
        rows = db.get_fix_attempts_for_group(self._conn, fingerprint, limit)
        return [_row_to_fix_attempt_status(row) for row in rows]

    def claim_fix_attempt(self) -> Optional[FixAttemptJob]:
        """Atomically claim the oldest pending fix attempt.

        Marks it as 'running' and returns its data, or None if the queue is empty.
        """
        row = db.claim_fix_attempt(self._conn)
        if row is None:
            return None
        return FixAttemptJob(
            job_id=str(row["job_id"]),
            fingerprint=str(row["fingerprint"]),
            rendered_message=str(row["rendered_message"]),
        )

    # --- Purge ---

    def purge_server(self, server: str) -> tuple[int, list[str]]:
        """Delete groups where `server` is the only contributing server.

        Groups that also have occurrences from other servers are left untouched.
        Returns (groups_deleted, discord_message_ids). The caller is responsible
        for deleting the Discord messages.
        """
        rows = self._conn.execute(
            """SELECT id, discord_message_id FROM error_groups
               WHERE id IN (
                   SELECT DISTINCT group_id FROM occurrences WHERE server = ?
               )
               AND id NOT IN (
                   SELECT DISTINCT group_id FROM occurrences WHERE server != ?
               )""",
            (server, server),
        ).fetchall()
        if not rows:
            return 0, []
        group_ids = [r['id'] for r in rows]
        message_ids = [r['discord_message_id'] for r in rows if r['discord_message_id'] is not None]
        placeholders = ','.join('?' * len(group_ids))
        with self._conn:
            self._conn.execute(
                f"DELETE FROM error_groups WHERE id IN ({placeholders})",
                group_ids,
            )
        return len(group_ids), message_ids

    def purge_by_status(self, status: str) -> tuple[int, list[str]]:
        """Delete all groups with the given status ('muted' or 'resolved').

        Returns (groups_deleted, discord_message_ids). The caller is responsible
        for deleting the Discord messages.
        """
        rows = self._conn.execute(
            "SELECT id, discord_message_id FROM error_groups WHERE status = ?",
            (status,),
        ).fetchall()
        if not rows:
            return 0, []
        group_ids = [r['id'] for r in rows]
        message_ids = [r['discord_message_id'] for r in rows if r['discord_message_id'] is not None]
        placeholders = ','.join('?' * len(group_ids))
        with self._conn:
            self._conn.execute(
                f"DELETE FROM error_groups WHERE id IN ({placeholders})",
                group_ids,
            )
        return len(group_ids), message_ids

    def purge_older_than(self, days: int) -> tuple[int, list[str]]:
        """Delete groups not seen within `days` days, plus orphaned occurrence/aggregate rows.

        Delegates to db.run_expiry() with a caller-supplied retention window instead of
        the configured expiry_days. This also sweeps any API tokens past their own
        expires_at, same as the regular hourly expiry pass, but — since that sweep is
        keyed on wall-clock time, not `days` — never sweeps a token early.

        Returns (groups_deleted, discord_message_ids). The caller is responsible
        for deleting the Discord messages.
        """
        result = db.run_expiry(self._conn, days)
        return result['error_groups'], result['discord_message_ids']

    def complete_fix_attempt(
        self,
        job_id: str,
        status: str,
        message: str,
        summary: str,
        detail: str,
        pr_url: Optional[str],
    ) -> Optional[tuple[str, Optional[str]]]:
        """Record the result of a fix attempt.

        Returns (fingerprint, requested_by_discord_id), or None if the job_id is unknown.
        """
        return db.complete_fix_attempt(
            self._conn, job_id, status, message, summary, detail, pr_url, int(time.time())
        )
