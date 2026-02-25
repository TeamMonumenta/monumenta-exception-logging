"""
Internal Python API for the Monumenta exception tracker.

Consumed directly by the Discord bot and any other internal tooling — this is
not an HTTP API. All methods are synchronous. Callers in an async context
should wrap calls with asyncio.get_event_loop().run_in_executor(None, func).
"""

import json
import sqlite3
import time
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
    servers_affected: list[str]           # servers seen within the 14-day retention window
    server_counts_24h: dict[str, int]     # fixed 24-hour window
    hourly_timeline: list[tuple[datetime, int]]  # (hour_start, count), fixed 7-day window
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


# --- Tracker ---

class Tracker:
    def __init__(self, config: TrackerConfig):
        self._config = config
        self._conn = db.init_db(config)

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
        result = []
        for row in rows:
            server_counts = self._get_server_counts(row['id'], cutoff)
            result.append(_row_to_summary(row, row['recent_count'], server_counts))
        return result

    def get_new_groups(self, hours: int = 24) -> list[GroupSummary]:
        """Return groups first seen within the last `hours` hours.

        Includes groups of all statuses — a newly detected exception that was
        immediately muted or resolved still appears here.
        """
        cutoff = int(time.time()) - hours * 3600
        rows = self._conn.execute(
            """SELECT id, fingerprint, exception_class, message_template,
                      status, first_seen, last_seen, total_count
               FROM error_groups
               WHERE first_seen >= ?
               ORDER BY first_seen DESC""",
            (cutoff,)
        ).fetchall()
        result = []
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
        result = []
        for row in rows:
            server_counts = self._get_server_counts(row['id'], cutoff)
            result.append(_row_to_summary(row, row['recent_count'], server_counts))
        return result

    def search_groups(self, query: str, limit: int = 20, window_hours: int = 24) -> list[GroupSummary]:
        """Case-insensitive substring search over exception_class and message_template.

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
               ORDER BY last_seen DESC
               LIMIT ?""",
            (pattern, pattern, limit)
        ).fetchall()
        result = []
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
        result = []
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

    # --- Muting and resolution ---

    def mute_group(self, fingerprint: str, actor: str = "unknown") -> bool:
        now = int(time.time())
        with self._conn:
            cur = self._conn.execute(
                "UPDATE error_groups SET status = 'muted', muted_by = ?, muted_at = ? "
                "WHERE fingerprint = ?",
                (actor, now, fingerprint)
            )
        return cur.rowcount > 0

    def unmute_group(self, fingerprint: str) -> bool:
        with self._conn:
            cur = self._conn.execute(
                "UPDATE error_groups SET status = 'active' WHERE fingerprint = ?",
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
                "UPDATE error_groups SET status = 'resolved', resolved_by = ?, resolved_at = ? "
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

    def get_fingerprint_by_short_id(self, short_id: str) -> Optional[str]:
        """Look up a full fingerprint from an 8-character prefix (short ID)."""
        row = self._conn.execute(
            "SELECT fingerprint FROM error_groups WHERE SUBSTR(fingerprint, 1, 8) = ?",
            (short_id,)
        ).fetchone()
        return row['fingerprint'] if row is not None else None

    # --- Maintenance ---

    def run_expiry(self) -> dict[str, Any]:
        """Delete occurrences, aggregates, and groups older than expiry_days.

        Deletes in dependency order (occurrences and aggregates first) before
        removing groups, so ON DELETE CASCADE never fires unexpectedly.
        Returns row counts plus discord_message_ids for groups that had tracked messages.
        """
        return db.run_expiry(self._conn, self._config.expiry_days)
