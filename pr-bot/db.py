# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Byron Marohn
import sqlite3
import time
from typing import Optional

from config import PrBotConfig


def init_db(config: PrBotConfig) -> sqlite3.Connection:
    conn = sqlite3.connect(config.db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    _create_tables(conn)
    return conn


def _create_tables(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS messages (
            message_id  TEXT PRIMARY KEY,
            channel_id  TEXT NOT NULL,
            guild_id    TEXT,
            author_id   TEXT NOT NULL,
            created_at  INTEGER NOT NULL,
            has_links   INTEGER NOT NULL DEFAULT 0,
            done        INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS pr_links (
            message_id  TEXT NOT NULL REFERENCES messages(message_id) ON DELETE CASCADE,
            repo        TEXT NOT NULL,
            pr_number   INTEGER NOT NULL,
            PRIMARY KEY (message_id, repo, pr_number)
        );
        CREATE INDEX IF NOT EXISTS idx_pr_links_pr ON pr_links(repo, pr_number);

        CREATE TABLE IF NOT EXISTS prs (
            repo           TEXT NOT NULL,
            pr_number      INTEGER NOT NULL,
            review_status  TEXT NOT NULL DEFAULT 'none'
                           CHECK (review_status IN ('none','commented','approved','changes_requested')),
            merged         INTEGER NOT NULL DEFAULT 0,
            closed         INTEGER NOT NULL DEFAULT 0,
            last_reviewer  TEXT,
            merged_by      TEXT,
            closed_by      TEXT,
            labels         TEXT NOT NULL DEFAULT '',
            checks_failing INTEGER NOT NULL DEFAULT 0,
            updated_at     INTEGER,
            PRIMARY KEY (repo, pr_number)
        );

        CREATE TABLE IF NOT EXISTS notify_prefs (
            discord_user_id TEXT PRIMARY KEY,
            pref            TEXT NOT NULL DEFAULT 'any_review'
                            CHECK (pref IN ('off','review_comments','any_review','all')),
            updated_at      INTEGER NOT NULL
        );
    """)


# ── messages ──────────────────────────────────────────────────────────────────

def upsert_message(
    conn: sqlite3.Connection,
    message_id: str,
    channel_id: str,
    guild_id: Optional[str],
    author_id: str,
    created_at: int,
    has_links: int,
) -> None:
    with conn:
        conn.execute(
            """
            INSERT INTO messages (message_id, channel_id, guild_id, author_id, created_at, has_links)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(message_id) DO UPDATE SET has_links=excluded.has_links
            """,
            (message_id, channel_id, guild_id, author_id, created_at, has_links),
        )


def set_message_done(conn: sqlite3.Connection, message_id: str, done: int) -> None:
    with conn:
        conn.execute("UPDATE messages SET done=? WHERE message_id=?", (done, message_id))


def get_message(conn: sqlite3.Connection, message_id: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM messages WHERE message_id=?", (message_id,)
    ).fetchone()


def get_messages_in_window(
    conn: sqlite3.Connection, retention_days: int
) -> list[sqlite3.Row]:
    cutoff = int(time.time()) - retention_days * 86400
    return conn.execute(
        "SELECT * FROM messages WHERE created_at >= ?", (cutoff,)
    ).fetchall()


def delete_old_messages(conn: sqlite3.Connection, retention_days: int) -> int:
    cutoff = int(time.time()) - retention_days * 86400
    with conn:
        cur = conn.execute("DELETE FROM messages WHERE created_at < ?", (cutoff,))
    return cur.rowcount


# ── pr_links ──────────────────────────────────────────────────────────────────

def get_links_for_message(
    conn: sqlite3.Connection, message_id: str
) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT repo, pr_number FROM pr_links WHERE message_id=?", (message_id,)
    ).fetchall()


def set_links_for_message(
    conn: sqlite3.Connection,
    message_id: str,
    links: list[tuple[str, int]],
) -> None:
    """Replace the full link set for a message (delete-then-insert)."""
    with conn:
        conn.execute("DELETE FROM pr_links WHERE message_id=?", (message_id,))
        conn.executemany(
            "INSERT OR IGNORE INTO pr_links (message_id, repo, pr_number) VALUES (?,?,?)",
            [(message_id, repo, num) for repo, num in links],
        )


def get_messages_for_pr(
    conn: sqlite3.Connection, repo: str, pr_number: int
) -> list[sqlite3.Row]:
    """Return message rows linked to a specific PR."""
    return conn.execute(
        """
        SELECT m.* FROM messages m
        JOIN pr_links l ON l.message_id = m.message_id
        WHERE l.repo=? AND l.pr_number=?
        """,
        (repo, pr_number),
    ).fetchall()


# ── prs ───────────────────────────────────────────────────────────────────────

def get_pr(
    conn: sqlite3.Connection, repo: str, pr_number: int
) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM prs WHERE repo=? AND pr_number=?", (repo, pr_number)
    ).fetchone()


def upsert_pr(
    conn: sqlite3.Connection,
    repo: str,
    pr_number: int,
    review_status: str = "none",
    merged: int = 0,
    closed: int = 0,
    last_reviewer: Optional[str] = None,
    merged_by: Optional[str] = None,
    closed_by: Optional[str] = None,
) -> None:
    with conn:
        conn.execute(
            """
            INSERT INTO prs (repo, pr_number, review_status, merged, closed,
                             last_reviewer, merged_by, closed_by, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT(repo, pr_number) DO UPDATE SET
                review_status=excluded.review_status,
                merged=excluded.merged,
                closed=excluded.closed,
                last_reviewer=excluded.last_reviewer,
                merged_by=excluded.merged_by,
                closed_by=excluded.closed_by,
                updated_at=excluded.updated_at
            """,
            (repo, pr_number, review_status, merged, closed,
             last_reviewer, merged_by, closed_by, int(time.time())),
        )


def set_pr_labels(conn: sqlite3.Connection, repo: str, pr_number: int, labels: str) -> None:
    """Set only the labels column, preserving review/lifecycle/check state."""
    with conn:
        conn.execute(
            """
            INSERT INTO prs (repo, pr_number, labels, updated_at)
            VALUES (?,?,?,?)
            ON CONFLICT(repo, pr_number) DO UPDATE SET
                labels=excluded.labels, updated_at=excluded.updated_at
            """,
            (repo, pr_number, labels, int(time.time())),
        )


def set_pr_checks_failing(
    conn: sqlite3.Connection, repo: str, pr_number: int, checks_failing: int
) -> None:
    """Set only the checks_failing column, preserving review/lifecycle/label state."""
    with conn:
        conn.execute(
            """
            INSERT INTO prs (repo, pr_number, checks_failing, updated_at)
            VALUES (?,?,?,?)
            ON CONFLICT(repo, pr_number) DO UPDATE SET
                checks_failing=excluded.checks_failing, updated_at=excluded.updated_at
            """,
            (repo, pr_number, checks_failing, int(time.time())),
        )


def get_active_prs(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Return all prs rows that still have at least one non-done message."""
    return conn.execute(
        """
        SELECT DISTINCT p.* FROM prs p
        JOIN pr_links l ON l.repo=p.repo AND l.pr_number=p.pr_number
        JOIN messages m ON m.message_id=l.message_id
        WHERE m.done=0 AND p.merged=0 AND p.closed=0
        """
    ).fetchall()


def prune_orphan_prs(conn: sqlite3.Connection) -> int:
    with conn:
        cur = conn.execute(
            """
            DELETE FROM prs WHERE NOT EXISTS (
                SELECT 1 FROM pr_links WHERE pr_links.repo=prs.repo
                    AND pr_links.pr_number=prs.pr_number
            )
            """
        )
    return cur.rowcount


# ── notify_prefs ─────────────────────────────────────────────────────────────

def get_notify_pref(conn: sqlite3.Connection, discord_user_id: str) -> str:
    """Return the user's pref, defaulting to 'any_review' if no row exists."""
    row = conn.execute(
        "SELECT pref FROM notify_prefs WHERE discord_user_id=?", (discord_user_id,)
    ).fetchone()
    return str(row["pref"]) if row else "any_review"


def set_notify_pref(
    conn: sqlite3.Connection, discord_user_id: str, pref: str
) -> None:
    with conn:
        conn.execute(
            """
            INSERT INTO notify_prefs (discord_user_id, pref, updated_at)
            VALUES (?,?,?)
            ON CONFLICT(discord_user_id) DO UPDATE SET pref=excluded.pref, updated_at=excluded.updated_at
            """,
            (discord_user_id, pref, int(time.time())),
        )
