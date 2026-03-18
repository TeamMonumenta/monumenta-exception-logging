# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Byron Marohn
import sqlite3
import time
from typing import Any, Optional

from .config import TrackerConfig


def init_db(config: TrackerConfig) -> sqlite3.Connection:
    conn = sqlite3.connect(config.db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    _create_tables(conn)
    _migrate(conn)
    return conn


def _create_tables(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS error_groups (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            fingerprint        TEXT NOT NULL UNIQUE,
            exception_class    TEXT NOT NULL,
            message_template   TEXT NOT NULL,
            canonical_frames   TEXT NOT NULL,
            canonical_trace    TEXT NOT NULL,
            logger             TEXT NOT NULL,
            first_seen         INTEGER NOT NULL,
            last_seen          INTEGER NOT NULL,
            total_count        INTEGER NOT NULL DEFAULT 0,
            status             TEXT NOT NULL DEFAULT 'active'
                               CHECK (status IN ('active', 'muted', 'resolved')),
            discord_message_id TEXT,
            has_activity       INTEGER NOT NULL DEFAULT 0,
            muted_by           TEXT,
            muted_at           INTEGER,
            resolved_by        TEXT,
            resolved_at        INTEGER
        );

        CREATE UNIQUE INDEX IF NOT EXISTS idx_groups_fingerprint
            ON error_groups(fingerprint);
        CREATE INDEX IF NOT EXISTS idx_groups_status_last_seen
            ON error_groups(status, last_seen);
        CREATE INDEX IF NOT EXISTS idx_groups_first_seen
            ON error_groups(first_seen);

        CREATE TABLE IF NOT EXISTS occurrences (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id   INTEGER NOT NULL REFERENCES error_groups(id) ON DELETE CASCADE,
            server     TEXT NOT NULL,
            timestamp  INTEGER NOT NULL,
            message    TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_occurrences_group_timestamp
            ON occurrences(group_id, timestamp);
        CREATE INDEX IF NOT EXISTS idx_occurrences_timestamp
            ON occurrences(timestamp);

        CREATE TABLE IF NOT EXISTS server_hour_counts (
            group_id    INTEGER NOT NULL REFERENCES error_groups(id) ON DELETE CASCADE,
            server      TEXT NOT NULL,
            hour_bucket INTEGER NOT NULL,
            count       INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (group_id, server, hour_bucket)
        );

        CREATE INDEX IF NOT EXISTS idx_shc_hour_bucket
            ON server_hour_counts(hour_bucket);

        CREATE TABLE IF NOT EXISTS notify_subscriptions (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            discord_user_id TEXT NOT NULL,
            pattern         TEXT NOT NULL,
            created_at      INTEGER NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_notify_user
            ON notify_subscriptions(discord_user_id);

        CREATE TABLE IF NOT EXISTS pending_discord_deletes (
            message_id TEXT PRIMARY KEY
        );
    """)
    conn.commit()


def _migrate(conn: sqlite3.Connection) -> None:
    """Apply incremental schema changes to existing databases."""
    try:
        conn.execute(
            "ALTER TABLE error_groups ADD COLUMN has_activity INTEGER NOT NULL DEFAULT 0"
        )
        conn.commit()
    except sqlite3.OperationalError:
        pass  # Column already exists (fresh DB or previously migrated)


def set_discord_message_id(
    conn: sqlite3.Connection, fingerprint: str, message_id: Optional[str]
) -> None:
    with conn:
        conn.execute(
            "UPDATE error_groups SET discord_message_id = ? WHERE fingerprint = ?",
            (message_id, fingerprint)
        )


def get_all_discord_messages(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    rows = conn.execute(
        "SELECT fingerprint, discord_message_id FROM error_groups "
        "WHERE discord_message_id IS NOT NULL"
    ).fetchall()
    return [(row['fingerprint'], row['discord_message_id']) for row in rows]


def get_active_discord_messages(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """Return (fingerprint, message_id) pairs where has_activity=1 (need re-edit)."""
    rows = conn.execute(
        "SELECT fingerprint, discord_message_id FROM error_groups "
        "WHERE discord_message_id IS NOT NULL AND has_activity = 1"
    ).fetchall()
    return [(row['fingerprint'], row['discord_message_id']) for row in rows]


def get_fingerprint_by_discord_message_id(
    conn: sqlite3.Connection, message_id: str
) -> Optional[str]:
    """Return the fingerprint for the group tracked with the given Discord message ID."""
    row = conn.execute(
        "SELECT fingerprint FROM error_groups WHERE discord_message_id = ?",
        (message_id,)
    ).fetchone()
    return row['fingerprint'] if row is not None else None


def clear_has_activity(conn: sqlite3.Connection, fingerprint: str) -> None:
    """Reset has_activity to 0 after a Discord message has been successfully edited."""
    with conn:
        conn.execute(
            "UPDATE error_groups SET has_activity = 0 WHERE fingerprint = ?",
            (fingerprint,)
        )


def add_notify_subscription(
    conn: sqlite3.Connection, discord_user_id: str, pattern: str, created_at: int
) -> int:
    """Insert a new notify subscription and return its AUTOINCREMENT id."""
    with conn:
        cur = conn.execute(
            "INSERT INTO notify_subscriptions (discord_user_id, pattern, created_at) "
            "VALUES (?, ?, ?)",
            (discord_user_id, pattern, created_at)
        )
    row_id = cur.lastrowid
    if row_id is None:
        raise RuntimeError("INSERT into notify_subscriptions returned no lastrowid")
    return row_id


def count_notify_subscriptions(conn: sqlite3.Connection, discord_user_id: str) -> int:
    """Return the number of subscriptions owned by the user."""
    row = conn.execute(
        "SELECT COUNT(*) AS cnt FROM notify_subscriptions WHERE discord_user_id = ?",
        (discord_user_id,)
    ).fetchone()
    return int(row['cnt'])


def list_notify_subscriptions(
    conn: sqlite3.Connection, discord_user_id: str
) -> list[tuple[int, str, int]]:
    """Return [(id, pattern, created_at), ...] for the user, ordered by id ascending."""
    rows = conn.execute(
        "SELECT id, pattern, created_at FROM notify_subscriptions "
        "WHERE discord_user_id = ? ORDER BY id",
        (discord_user_id,)
    ).fetchall()
    return [(row['id'], row['pattern'], row['created_at']) for row in rows]


def remove_notify_subscription(
    conn: sqlite3.Connection, discord_user_id: str, sub_id: int
) -> bool:
    """Delete a subscription by id, scoped to the owning user. Returns True if a row was deleted."""
    with conn:
        cur = conn.execute(
            "DELETE FROM notify_subscriptions WHERE id = ? AND discord_user_id = ?",
            (sub_id, discord_user_id)
        )
    return cur.rowcount > 0


def get_all_notify_subscriptions(conn: sqlite3.Connection) -> list[tuple[int, str, str]]:
    """Return [(id, discord_user_id, pattern), ...] for all subscriptions, ordered by id."""
    rows = conn.execute(
        "SELECT id, discord_user_id, pattern FROM notify_subscriptions ORDER BY id"
    ).fetchall()
    return [(row['id'], row['discord_user_id'], row['pattern']) for row in rows]


def migrate_fingerprints(
    conn: sqlite3.Connection, app_packages: list[str]
) -> dict[str, Any]:
    """Re-fingerprint all groups with the current normalization rules.

    Returns a summary dict: {updated: N, merged: M, orphaned_discord_ids: [...]}.
    Inserts any orphaned Discord message IDs into pending_discord_deletes.
    All DB writes occur in a single transaction.
    """
    import json  # pylint: disable=import-outside-toplevel
    from .fingerprint import (  # pylint: disable=import-outside-toplevel
        compute_fingerprint, normalize_message, extract_app_frames,
    )

    rows = conn.execute(
        "SELECT id, fingerprint, exception_class, message_template, "
        "canonical_frames, discord_message_id, total_count, first_seen, last_seen "
        "FROM error_groups"
    ).fetchall()

    updated = 0
    merged = 0
    orphaned_discord_ids: list[str] = []

    with conn:
        for row in rows:
            frames = json.loads(row['canonical_frames'])
            new_normalized = normalize_message(row['message_template'])
            top_frames = extract_app_frames(frames, app_packages, 3)
            new_fp = compute_fingerprint(row['exception_class'], new_normalized, top_frames)

            if new_fp == row['fingerprint']:
                continue

            winner = conn.execute(
                "SELECT id, total_count, first_seen, last_seen, discord_message_id "
                "FROM error_groups WHERE fingerprint = ?",
                (new_fp,)
            ).fetchone()

            if winner is None:
                conn.execute(
                    "UPDATE error_groups SET fingerprint = ?, message_template = ?, has_activity = 1 "
                    "WHERE id = ?",
                    (new_fp, new_normalized, row['id'])
                )
                updated += 1
            else:
                conn.execute(
                    "UPDATE error_groups SET total_count = ?, first_seen = ?, last_seen = ?, "
                    "has_activity = 1 WHERE id = ?",
                    (
                        winner['total_count'] + row['total_count'],
                        min(winner['first_seen'], row['first_seen']),
                        max(winner['last_seen'], row['last_seen']),
                        winner['id'],
                    )
                )
                conn.execute(
                    "UPDATE occurrences SET group_id = ? WHERE group_id = ?",
                    (winner['id'], row['id'])
                )
                conn.execute(
                    """INSERT INTO server_hour_counts (group_id, server, hour_bucket, count)
                       SELECT ?, server, hour_bucket, count
                       FROM server_hour_counts WHERE group_id = ?
                       ON CONFLICT (group_id, server, hour_bucket)
                       DO UPDATE SET count = count + excluded.count""",
                    (winner['id'], row['id'])
                )
                conn.execute(
                    "DELETE FROM server_hour_counts WHERE group_id = ?",
                    (row['id'],)
                )
                if row['discord_message_id'] is not None:
                    if winner['discord_message_id'] is None:
                        # Winner has no Discord message — adopt the loser's instead of deleting it.
                        conn.execute(
                            "UPDATE error_groups SET discord_message_id = ? WHERE id = ?",
                            (row['discord_message_id'], winner['id'])
                        )
                    else:
                        conn.execute(
                            "INSERT OR IGNORE INTO pending_discord_deletes VALUES (?)",
                            (row['discord_message_id'],)
                        )
                        orphaned_discord_ids.append(row['discord_message_id'])
                conn.execute("DELETE FROM error_groups WHERE id = ?", (row['id'],))
                merged += 1

    return {'updated': updated, 'merged': merged, 'orphaned_discord_ids': orphaned_discord_ids}


def add_pending_discord_delete(conn: sqlite3.Connection, message_id: str) -> None:
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO pending_discord_deletes VALUES (?)",
            (message_id,)
        )


def pop_pending_discord_deletes(conn: sqlite3.Connection) -> list[str]:
    with conn:
        rows = conn.execute("SELECT message_id FROM pending_discord_deletes").fetchall()
        conn.execute("DELETE FROM pending_discord_deletes")
    return [row['message_id'] for row in rows]


def run_expiry(conn: sqlite3.Connection, expiry_days: int = 14) -> dict[str, Any]:
    cutoff = int(time.time()) - expiry_days * 86400
    with conn:
        id_rows = conn.execute(
            "SELECT discord_message_id FROM error_groups "
            "WHERE last_seen < ? AND discord_message_id IS NOT NULL",
            (cutoff,)
        ).fetchall()
        discord_message_ids = [row['discord_message_id'] for row in id_rows]

        cur = conn.execute("DELETE FROM occurrences WHERE timestamp < ?", (cutoff,))
        occ_deleted = cur.rowcount

        cur = conn.execute("DELETE FROM server_hour_counts WHERE hour_bucket < ?", (cutoff,))
        shc_deleted = cur.rowcount

        cur = conn.execute("DELETE FROM error_groups WHERE last_seen < ?", (cutoff,))
        groups_deleted = cur.rowcount

    return {
        "occurrences": occ_deleted,
        "server_hour_counts": shc_deleted,
        "error_groups": groups_deleted,
        "discord_message_ids": discord_message_ids,
    }
