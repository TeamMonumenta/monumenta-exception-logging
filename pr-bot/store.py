# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Byron Marohn
import sqlite3
import time
from dataclasses import dataclass
from typing import Optional

import db as _db
from config import PrBotConfig


@dataclass
class MessageRow:
    message_id: str
    channel_id: str
    guild_id: Optional[str]
    author_id: str
    created_at: int
    has_links: bool
    done: bool


@dataclass
class PrRow:
    repo: str
    pr_number: int
    review_status: str   # none | commented | approved | changes_requested
    merged: bool
    closed: bool
    last_reviewer: Optional[str]
    merged_by: Optional[str]
    closed_by: Optional[str]
    labels: str          # comma-separated sorted category keys, e.g. "ready,tested"
    checks_failing: bool
    updated_at: Optional[int]
    title: Optional[str]


@dataclass
class PrLink:
    repo: str
    pr_number: int


@dataclass
class GithubLinkRow:
    discord_user_id: str
    github_username: str   # stored as the user typed it; comparisons are NOCASE
    linked_at: int


def _row_to_message(row: sqlite3.Row) -> MessageRow:
    return MessageRow(
        message_id=str(row["message_id"]),
        channel_id=str(row["channel_id"]),
        guild_id=str(row["guild_id"]) if row["guild_id"] else None,
        author_id=str(row["author_id"]),
        created_at=int(row["created_at"]),
        has_links=bool(row["has_links"]),
        done=bool(row["done"]),
    )


def _row_to_github_link(row: sqlite3.Row) -> GithubLinkRow:
    return GithubLinkRow(
        discord_user_id=str(row["discord_user_id"]),
        github_username=str(row["github_username"]),
        linked_at=int(row["linked_at"]),
    )


def _row_to_pr(row: sqlite3.Row) -> PrRow:
    return PrRow(
        repo=str(row["repo"]),
        pr_number=int(row["pr_number"]),
        review_status=str(row["review_status"]),
        merged=bool(row["merged"]),
        closed=bool(row["closed"]),
        last_reviewer=str(row["last_reviewer"]) if row["last_reviewer"] else None,
        merged_by=str(row["merged_by"]) if row["merged_by"] else None,
        closed_by=str(row["closed_by"]) if row["closed_by"] else None,
        labels=str(row["labels"]) if row["labels"] else "",
        checks_failing=bool(row["checks_failing"]),
        updated_at=int(row["updated_at"]) if row["updated_at"] else None,
        title=str(row["title"]) if row["title"] else None,
    )


class Store:
    def __init__(self, config: PrBotConfig) -> None:
        self._conn = _db.init_db(config)
        self._config = config

    def close(self) -> None:
        self._conn.close()

    # ── messages ──────────────────────────────────────────────────────────────

    def upsert_message(
        self,
        message_id: str,
        channel_id: str,
        guild_id: Optional[str],
        author_id: str,
        created_at: int,
        has_links: bool,
    ) -> None:
        _db.upsert_message(
            self._conn, message_id, channel_id, guild_id,
            author_id, created_at, int(has_links),
        )

    def get_message(self, message_id: str) -> Optional[MessageRow]:
        row = _db.get_message(self._conn, message_id)
        return _row_to_message(row) if row else None

    def set_message_done(self, message_id: str, done: bool) -> None:
        _db.set_message_done(self._conn, message_id, int(done))

    def get_messages_in_window(self) -> list[MessageRow]:
        rows = _db.get_messages_in_window(self._conn, self._config.pr_retention_days)
        return [_row_to_message(r) for r in rows]

    def delete_message(self, message_id: str) -> None:
        with self._conn:
            self._conn.execute("DELETE FROM messages WHERE message_id=?", (message_id,))

    # ── pr_links ──────────────────────────────────────────────────────────────

    def get_links_for_message(self, message_id: str) -> list[PrLink]:
        rows = _db.get_links_for_message(self._conn, message_id)
        return [PrLink(repo=str(r["repo"]), pr_number=int(r["pr_number"])) for r in rows]

    def set_links_for_message(self, message_id: str, links: list[PrLink]) -> None:
        _db.set_links_for_message(
            self._conn, message_id, [(lnk.repo, lnk.pr_number) for lnk in links]
        )

    def get_messages_for_pr(self, repo: str, pr_number: int) -> list[MessageRow]:
        rows = _db.get_messages_for_pr(self._conn, repo, pr_number)
        return [_row_to_message(r) for r in rows]

    # ── prs ───────────────────────────────────────────────────────────────────

    def get_pr(self, repo: str, pr_number: int) -> Optional[PrRow]:
        row = _db.get_pr(self._conn, repo, pr_number)
        return _row_to_pr(row) if row else None

    def upsert_pr(
        self,
        repo: str,
        pr_number: int,
        review_status: str = "none",
        merged: bool = False,
        closed: bool = False,
        last_reviewer: Optional[str] = None,
        merged_by: Optional[str] = None,
        closed_by: Optional[str] = None,
        title: Optional[str] = None,
    ) -> None:
        _db.upsert_pr(
            self._conn, repo, pr_number, review_status,
            int(merged), int(closed), last_reviewer, merged_by, closed_by,
            title=title,
        )

    def set_pr_labels(self, repo: str, pr_number: int, labels: str) -> None:
        _db.set_pr_labels(self._conn, repo, pr_number, labels)

    def set_pr_checks_failing(self, repo: str, pr_number: int, checks_failing: bool) -> None:
        _db.set_pr_checks_failing(self._conn, repo, pr_number, int(checks_failing))

    def set_pr_title(self, repo: str, pr_number: int, title: str) -> None:
        _db.set_pr_title(self._conn, repo, pr_number, title)

    def get_ready_prs(self) -> list[PrRow]:
        return [_row_to_pr(r) for r in _db.get_ready_prs(self._conn)]

    def get_first_author_for_pr(self, repo: str, pr_number: int) -> Optional[str]:
        return _db.get_first_author_for_pr(self._conn, repo, pr_number)

    def get_active_prs(self) -> list[PrRow]:
        rows = _db.get_active_prs(self._conn)
        return [_row_to_pr(r) for r in rows]

    # ── cleanup ───────────────────────────────────────────────────────────────

    def run_cleanup(self) -> dict[str, int]:
        msgs_deleted = _db.delete_old_messages(self._conn, self._config.pr_retention_days)
        prs_pruned = _db.prune_orphan_prs(self._conn)
        return {"messages_deleted": msgs_deleted, "prs_pruned": prs_pruned}

    # ── notify_prefs ─────────────────────────────────────────────────────────

    def get_notify_pref(self, discord_user_id: str) -> str:
        return _db.get_notify_pref(self._conn, discord_user_id)

    def set_notify_pref(self, discord_user_id: str, pref: str) -> None:
        _db.set_notify_pref(self._conn, discord_user_id, pref)

    # ── github_links ─────────────────────────────────────────────────────────

    def add_github_link(self, discord_user_id: str, github_username: str) -> None:
        _db.add_github_link(self._conn, discord_user_id, github_username)

    def remove_github_link(self, discord_user_id: str) -> bool:
        return _db.remove_github_link(self._conn, discord_user_id) > 0

    def get_link_by_discord(self, discord_user_id: str) -> Optional[GithubLinkRow]:
        row = _db.get_link_by_discord(self._conn, discord_user_id)
        return _row_to_github_link(row) if row else None

    def get_link_by_github(self, github_username: str) -> Optional[GithubLinkRow]:
        row = _db.get_link_by_github(self._conn, github_username)
        return _row_to_github_link(row) if row else None

    def list_github_links(self) -> list[GithubLinkRow]:
        return [_row_to_github_link(r) for r in _db.list_github_links(self._conn)]

    # ── aggregate helpers ─────────────────────────────────────────────────────

    def get_pr_states_for_message(self, message_id: str) -> list[PrRow]:
        """Return the PrRow for every PR linked by this message (creates rows if needed)."""
        links = self.get_links_for_message(message_id)
        result: list[PrRow] = []
        for lnk in links:
            pr = self.get_pr(lnk.repo, lnk.pr_number)
            if pr is None:
                self.upsert_pr(lnk.repo, lnk.pr_number)
                pr = PrRow(
                    repo=lnk.repo, pr_number=lnk.pr_number,
                    review_status="none", merged=False, closed=False,
                    last_reviewer=None, merged_by=None, closed_by=None,
                    labels="", checks_failing=False,
                    updated_at=int(time.time()),
                    title=None,
                )
            result.append(pr)
        return result
