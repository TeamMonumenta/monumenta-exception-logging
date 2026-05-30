# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Byron Marohn
import asyncio
import logging
import re
import sqlite3
import time
from typing import Any, Optional

import discord
from discord import app_commands
from discord.ext import commands

from config import PrBotConfig
from github import (
    CheckEvent,
    GitHubClient,
    LabelEvent,
    ParsedPrLink,
    PrLifecycleEvent,
    ReviewEvent,
    WebhookEvent,
    match_label_categories,
    parse_autopost_message,
    parse_pr_links,
)
from store import PrLink, PrRow, Store

_GITHUB_USERNAME_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})")

logger = logging.getLogger(__name__)

_MAX_MSG_LEN = 2000

# ── emoji helpers ─────────────────────────────────────────────────────────────

def _emoji_matches(reaction: discord.Reaction, configured: str) -> bool:
    """
    Compare a discord.Reaction to a configured emoji string.

    Configured can be a unicode char ("✅") or a custom emoji in "name:id" form.
    Custom emoji on a message stringify as "<:name:id>" but react as "name:id",
    so we normalize by comparing name+id components.
    """
    emoji = reaction.emoji
    if isinstance(emoji, (discord.Emoji, discord.PartialEmoji)):
        # Custom emoji: compare as "name:id"
        if emoji.id is not None:
            return configured == f"{emoji.name}:{emoji.id}"
        return configured == str(emoji.name)
    # Unicode emoji
    return configured == str(emoji)


def _reaction_str(configured: str) -> str:
    """
    Return the string to pass to add_reaction / remove_reaction.
    Both unicode chars and "name:id" strings work directly with discord.py.
    """
    return configured


# ── label category storage ──────────────────────────────────────────────────────

def _parse_label_categories(labels: str) -> set[str]:
    """Split the stored comma-separated category string into a set."""
    return {c for c in labels.split(",") if c}


def _join_label_categories(categories: set[str]) -> str:
    """Serialize a category set to a stable comma-separated string for storage."""
    return ",".join(sorted(categories))


# ── reaction state machine ────────────────────────────────────────────────────

def _apply_review_event(
    pr: PrRow,
    action: str,
    review_state: str,
    reviewer: str,
    pr_author: str,
) -> PrRow:
    """Return an updated PrRow based on a review event (does not write to DB)."""
    status = pr.review_status
    last_reviewer = pr.last_reviewer

    if action == "dismissed":
        status = "none"
        last_reviewer = None
    elif action == "submitted":
        if review_state in ("approved", "changes_requested"):
            status = review_state
            last_reviewer = reviewer
        elif review_state == "commented":
            # A comment from the PR author on their own PR never signals
            # "needs attention" — only other users' comments do.
            if status == "none" and (not pr_author or reviewer != pr_author):
                status = "commented"
                last_reviewer = reviewer

    return PrRow(
        repo=pr.repo,
        pr_number=pr.pr_number,
        review_status=status,
        merged=pr.merged,
        closed=pr.closed,
        last_reviewer=last_reviewer,
        merged_by=pr.merged_by,
        closed_by=pr.closed_by,
        labels=pr.labels,
        checks_failing=pr.checks_failing,
        updated_at=int(time.time()),
    )


def compute_desired_reactions(
    prs: list[PrRow],
    config: PrBotConfig,
) -> set[str]:
    """
    Compute the set of managed emoji that should be present on a message.
    Returns a set of configured emoji strings.
    """
    if not prs:
        return {config.reaction_question}

    all_terminal = all(p.merged or p.closed for p in prs)
    open_prs = [p for p in prs if not p.merged and not p.closed]

    desired: set[str] = set()

    # ✅ approved — every linked PR has review_status == approved
    if all(p.review_status == "approved" for p in prs):
        desired.add(config.reaction_approved)

    # 💬 changes — any open PR needs changes (or has comment if REVIEW_COMMENT_IS_CHANGES)
    for p in open_prs:
        if p.review_status == "changes_requested":
            desired.add(config.reaction_changes)
            break
        if config.review_comment_is_changes and p.review_status == "commented":
            desired.add(config.reaction_changes)
            break

    # Label reactions — any open PR carrying the label (🟢/🟠/🧪/⚖️).
    # Drop off naturally once the message is terminal (open_prs empty).
    for p in open_prs:
        for category in _parse_label_categories(p.labels):
            desired.add(config.label_reaction(category))

    # 🐶 checks failing — any open PR has a failing automated check.
    if any(p.checks_failing for p in open_prs):
        desired.add(config.reaction_checks_failed)

    # Terminal reactions only when every PR is terminal
    if all_terminal:
        if any(p.merged for p in prs):
            desired.add(config.reaction_merged)
        if any(p.closed and not p.merged for p in prs):
            desired.add(config.reaction_closed)

    return desired


def _managed_emoji(config: PrBotConfig) -> set[str]:
    return {
        config.reaction_approved,
        config.reaction_changes,
        config.reaction_merged,
        config.reaction_closed,
        config.reaction_question,
        config.reaction_ready,
        config.reaction_not_ready,
        config.reaction_tested,
        config.reaction_monthly_balance,
        config.reaction_checks_failed,
    }


# ── DM helpers ────────────────────────────────────────────────────────────────

def _should_dm(pref: str, transition: str) -> bool:
    """
    transition: "approved" | "changes_requested" | "commented" | "merged" | "closed"
                | "checks_failed"
    Returns True if a user with this pref should be DMed for this transition.
    Note: bare 'commented' DMs fire for any_review/all regardless of REVIEW_COMMENT_IS_CHANGES.
    A failing check DMs every pref except 'off'.
    """
    if pref == "off":
        return False
    if transition == "checks_failed" or pref == "all":
        return True  # failing checks notify every non-off pref; 'all' gets everything
    # Remaining prefs are 'review_comments' / 'any_review' (DB-constrained).
    if transition in ("approved", "changes_requested"):
        return True
    if transition == "commented":
        return pref == "any_review"
    return False  # merged / closed reach here only for non-'all' prefs


def _format_dm(
    config: PrBotConfig,
    repo: str,
    pr_number: int,
    transition: str,
    actor: str,
) -> str:
    url = f"https://github.com/{repo}/pull/{pr_number}"
    pr_ref = f"[{repo}#{pr_number}]({url})"
    if transition == "approved":
        emoji = config.reaction_approved
        verb = "**approved**"
        by_clause = f" by @{actor}" if actor else ""
        return f"{emoji} Your PR {pr_ref} was {verb}{by_clause}"
    if transition == "changes_requested":
        emoji = config.reaction_changes
        return f"{emoji} Your PR {pr_ref} has **requested changes** by @{actor}"
    if transition == "commented":
        emoji = config.reaction_changes
        return f"{emoji} Your PR {pr_ref} was **commented on** by @{actor}"
    if transition == "merged":
        emoji = config.reaction_merged
        return f"{emoji} Your PR {pr_ref} was **merged** by @{actor}"
    if transition == "checks_failed":
        emoji = config.reaction_checks_failed
        return f"{emoji} A check **failed** on your PR {pr_ref}"
    # closed
    emoji = config.reaction_closed
    return f"{emoji} Your PR {pr_ref} was **closed** without merging by @{actor}"


# ── Bot ───────────────────────────────────────────────────────────────────────

class PrBot(commands.Bot):
    def __init__(self, store: Store, github_client: GitHubClient, config: PrBotConfig) -> None:
        intents = discord.Intents.default()
        intents.message_content = True   # PRIVILEGED - required to read PR links
        super().__init__(command_prefix="!", intents=intents)
        self.store = store
        self.github = github_client
        self.config = config
        self._autoposting: set[tuple[str, int]] = set()

    async def setup_hook(self) -> None:
        logger.debug("setup_hook: registering and syncing slash commands")
        self._register_commands()
        synced = await self.tree.sync()
        logger.info("Synced %d slash command(s)", len(synced))

    async def on_ready(self) -> None:
        user = self.user
        logger.info(
            "Logged in as %s (id %s); watching channel %d",
            user, user.id if user else "?", self.config.discord_channel,
        )

    # ── channel helper ────────────────────────────────────────────────────────

    async def _get_channel(self) -> Optional[discord.TextChannel]:
        channel = self.get_channel(self.config.discord_channel)
        if channel is None:
            try:
                channel = await self.fetch_channel(self.config.discord_channel)
            except discord.NotFound:
                logger.error("Discord channel %d not found", self.config.discord_channel)
                return None
        return channel  # type: ignore[return-value]

    async def _resolve_reply_links(self, message: discord.Message) -> list[ParsedPrLink]:
        """
        If `message` is a self-reply (the author replying to their own message,
        e.g. a "bump"), return the PR links of the replied-to message so the reply
        tracks the same PR(s).  Replies to another user's message inherit nothing.
        """
        ref = message.reference
        if (ref is None or ref.message_id is None
                or ref.channel_id != self.config.discord_channel):
            return []  # not a reply, or a cross-channel reference
        ref_id = str(ref.message_id)
        author_id = str(message.author.id)

        # Prefer stored links: a bump persists its inherited links, so a chain
        # (original -> bump -> bump) resolves via each reply's stored row.
        ref_msg = self.store.get_message(ref_id)
        if ref_msg is not None:
            if ref_msg.author_id != author_id:
                return []  # self-replies only
            return [
                ParsedPrLink(repo=lnk.repo, pr_number=lnk.pr_number)
                for lnk in self.store.get_links_for_message(ref_id)
            ]

        # Referenced message not tracked — fetch it and parse its content.
        referenced = await self._fetch_referenced_message(ref, ref.message_id)
        if referenced is None or str(referenced.author.id) != author_id:
            return []  # gone, or not a self-reply
        return parse_pr_links(referenced.content, self.config.github_repos)

    async def _fetch_referenced_message(
        self, ref: discord.MessageReference, message_id: int
    ) -> Optional[discord.Message]:
        """Return the replied-to message (from the cached reference or a fetch)."""
        referenced = ref.resolved
        if isinstance(referenced, discord.Message):
            return referenced
        channel = await self._get_channel()
        if channel is None:
            return None
        try:
            return await channel.fetch_message(message_id)
        except discord.NotFound:
            return None
        except discord.DiscordException:
            logger.exception("reply resolve: failed to fetch referenced %d", message_id)
            return None

    # ── ingest pipeline ───────────────────────────────────────────────────────

    async def ingest_message(
        self,
        message_id: str,
        channel_id: str,
        guild_id: Optional[str],
        author_id: str,
        created_at: int,
        content: str,
        fetch_pr_state: bool = True,
        extra_links: Optional[list[ParsedPrLink]] = None,
    ) -> None:
        """
        Parse PR links, persist the message and its links, optionally fetch PR state,
        and reconcile reactions.  Shared by on_message, on_raw_message_edit, and startup.

        extra_links carries PR links inherited from a replied-to message (self-reply
        "bump"); they are unioned with the message's own links, de-duplicated.
        """
        links = parse_pr_links(content, self.config.github_repos)
        if extra_links:
            seen = {(lnk.repo, lnk.pr_number) for lnk in links}
            for lnk in extra_links:
                key = (lnk.repo, lnk.pr_number)
                if key not in seen:
                    seen.add(key)
                    links.append(lnk)
        has_links = bool(links)
        logger.debug(
            "ingest_message %s: parsed %d link(s) %s (fetch_pr_state=%s)",
            message_id,
            len(links),
            [f"{lnk.repo}#{lnk.pr_number}" for lnk in links],
            fetch_pr_state,
        )

        self.store.upsert_message(
            message_id=message_id,
            channel_id=channel_id,
            guild_id=guild_id,
            author_id=author_id,
            created_at=created_at,
            has_links=has_links,
        )
        pr_links = [PrLink(repo=lnk.repo, pr_number=lnk.pr_number) for lnk in links]
        self.store.set_links_for_message(message_id, pr_links)

        # Fetch state for any PR we haven't seen yet
        if fetch_pr_state:
            for lnk in links:
                if self.store.get_pr(lnk.repo, lnk.pr_number) is None:
                    await self._fetch_and_store_pr(lnk.repo, lnk.pr_number)

        await self.reconcile_message(message_id)

        logger.debug(
            "ingest_message %s complete: %d link(s), has_links=%s",
            message_id, len(links), has_links,
        )

    async def _fetch_and_store_pr(self, repo: str, pr_number: int) -> None:
        """Fetch current PR state from GitHub and persist it."""
        logger.debug("Fetching initial state for %s#%d from GitHub", repo, pr_number)
        try:
            state = await self.github.fetch_pr_state(repo, pr_number)
            logger.debug("Fetched state for %s#%d: %s", repo, pr_number, state)
            self.store.upsert_pr(
                repo=repo,
                pr_number=pr_number,
                review_status=str(state["review_status"]),
                merged=bool(state["merged"]),
                closed=bool(state["closed"]),
                last_reviewer=state.get("last_reviewer"),  # type: ignore[arg-type]
                merged_by=state.get("merged_by"),  # type: ignore[arg-type]
                closed_by=state.get("closed_by"),  # type: ignore[arg-type]
            )
            categories = match_label_categories(
                list(state.get("labels", [])), self.config.label_map()
            )
            self.store.set_pr_labels(repo, pr_number, _join_label_categories(categories))
            checks = state["checks_failing"]
            if checks is not None:  # None = couldn't read checks; keep webhook-set state
                self.store.set_pr_checks_failing(repo, pr_number, bool(checks))
        except Exception:  # pylint: disable=broad-exception-caught
            logger.exception("Failed to fetch PR state for %s#%d", repo, pr_number)
            # Leave the row uninitialized; reconcile will use defaults

    # ── reconcile ─────────────────────────────────────────────────────────────

    async def reconcile_message(
        self, message_id: str, purge_unknown: bool = False,
    ) -> None:
        """Set the message's reactions to exactly the desired managed-emoji set.

        purge_unknown: also remove bot reactions whose emoji isn't in the current
        managed set. Used by startup reconcile to clean up reactions left from a
        previous config (e.g. an emoji default that has since been changed).
        """
        channel = await self._get_channel()
        if channel is None:
            return

        prs = self.store.get_pr_states_for_message(message_id)
        msg_row = self.store.get_message(message_id)
        if msg_row is None:
            return

        # If has_links is False (❓ case), treat prs as empty for reaction logic
        if not msg_row.has_links:
            prs = []

        desired = compute_desired_reactions(prs, self.config)
        managed = _managed_emoji(self.config)

        try:
            message = await channel.fetch_message(int(message_id))
        except discord.NotFound:
            logger.warning("reconcile_message: message %s not found", message_id)
            return
        except discord.DiscordException:
            logger.exception("reconcile_message: failed to fetch message %s", message_id)
            return

        # Managed emoji where the bot itself has already reacted, plus any
        # bot-placed reactions that don't match the current managed set
        # (only collected when purge_unknown is set).
        present: set[str] = set()
        stale: list[Any] = []
        for reaction in message.reactions:
            if not reaction.me:
                continue
            matched = False
            for cfg_emoji in managed:
                if _emoji_matches(reaction, cfg_emoji):
                    present.add(cfg_emoji)
                    matched = True
                    break
            if not matched and purge_unknown:
                stale.append(reaction.emoji)

        to_add = desired - present
        to_remove = present - desired

        if to_add or to_remove or stale:
            logger.info(
                "Reconcile %s: +%s -%s stale=%s (desired=%s)",
                message_id, sorted(to_add), sorted(to_remove),
                [str(e) for e in stale], sorted(desired),
            )
        else:
            logger.debug(
                "Reconcile %s: no change (desired=%s)", message_id, sorted(desired)
            )

        for emoji in to_add:
            try:
                await message.add_reaction(_reaction_str(emoji))
            except discord.DiscordException:
                logger.exception("reconcile: failed to add reaction %r to %s", emoji, message_id)

        for emoji in to_remove:
            await self._remove_bot_reaction(message, _reaction_str(emoji), message_id)

        for stale_emoji in stale:
            await self._remove_bot_reaction(message, stale_emoji, message_id)

        # Update done flag
        if prs:
            all_terminal = all(p.merged or p.closed for p in prs)
            if all_terminal != msg_row.done:
                logger.info(
                    "Message %s marked %s", message_id,
                    "done (all PRs terminal)" if all_terminal else "active",
                )
                self.store.set_message_done(message_id, all_terminal)

    async def _remove_bot_reaction(
        self, message: discord.Message, emoji: Any, message_id: str,
    ) -> None:
        """Clear `emoji` from `message`, falling back to per-user removal if we lack Manage Messages."""
        try:
            await message.clear_reaction(emoji)
        except discord.Forbidden:
            # No Manage Messages — remove only our own reaction
            try:
                if self.user:
                    await message.remove_reaction(emoji, self.user)
            except discord.DiscordException:
                logger.exception(
                    "reconcile: failed to remove reaction %r from %s", emoji, message_id
                )
        except discord.DiscordException:
            logger.exception(
                "reconcile: failed to clear reaction %r from %s", emoji, message_id
            )

    # ── DM helper ─────────────────────────────────────────────────────────────

    async def _dm_user(self, author_id: str, text: str) -> None:
        if not self.config.pr_dm_enabled:
            logger.debug("DM suppressed (PR_DM_ENABLED=false): would DM %s: %s", author_id, text)
            return
        try:
            user = await self.fetch_user(int(author_id))
            # Chunk if needed (shouldn't normally exceed 2000 chars here)
            for i in range(0, len(text), _MAX_MSG_LEN):
                await user.send(text[i:i + _MAX_MSG_LEN])
            logger.info("DM sent to %s: %s", author_id, text)
        except discord.NotFound:
            logger.warning("DM: user %s not found", author_id)
        except discord.Forbidden:
            logger.warning("DM: user %s has DMs closed", author_id)
        except discord.DiscordException:
            logger.exception("DM: failed to DM user %s", author_id)

    # ── GitHub event handler ──────────────────────────────────────────────────

    async def handle_pr_event(self, event: WebhookEvent) -> None:
        """Dispatch a parsed GitHub webhook event to the right handler."""
        if isinstance(event, LabelEvent):
            await self._handle_label_event(event)
        elif isinstance(event, CheckEvent):
            await self._handle_check_event(event)
        else:
            await self._handle_review_or_lifecycle(event)

    async def _handle_review_or_lifecycle(
        self, event: ReviewEvent | PrLifecycleEvent
    ) -> None:
        """Process a review or merge/close webhook event."""
        repo = event.repo
        pr_number = event.pr_number
        logger.debug("handle_pr_event %s#%d: %r", repo, pr_number, event)

        # Determine new state
        existing = self.store.get_pr(repo, pr_number)
        old_status = existing.review_status if existing else "none"
        old_merged = existing.merged if existing else False
        old_closed = existing.closed if existing else False

        if isinstance(event, ReviewEvent):
            if existing is None:
                existing = PrRow(
                    repo=repo, pr_number=pr_number,
                    review_status="none", merged=False, closed=False,
                    last_reviewer=None, merged_by=None, closed_by=None,
                    labels="", checks_failing=False,
                    updated_at=None,
                )
            updated = _apply_review_event(
                existing, event.action, event.review_state,
                event.reviewer, event.pr_author,
            )
            self.store.upsert_pr(
                repo=repo, pr_number=pr_number,
                review_status=updated.review_status,
                merged=updated.merged,
                closed=updated.closed,
                last_reviewer=updated.last_reviewer,
                merged_by=updated.merged_by,
                closed_by=updated.closed_by,
            )
            new_status = updated.review_status
            new_merged = updated.merged
            new_closed = updated.closed
            actor = event.reviewer
            pr_author = event.pr_author
        else:  # PrLifecycleEvent
            merged_by = event.actor if event.merged else (existing.merged_by if existing else None)
            closed_by = event.actor if not event.merged else (existing.closed_by if existing else None)
            review_status = existing.review_status if existing else "none"
            last_reviewer = existing.last_reviewer if existing else None
            self.store.upsert_pr(
                repo=repo, pr_number=pr_number,
                review_status=review_status,
                merged=event.merged,
                closed=event.closed,
                last_reviewer=last_reviewer,
                merged_by=merged_by,
                closed_by=closed_by,
            )
            new_status = review_status
            new_merged = event.merged
            new_closed = event.closed
            actor = event.actor
            pr_author = ""

        # Determine what transitioned
        status_changed = new_status != old_status
        merged_changed = new_merged and not old_merged
        closed_changed = new_closed and not old_closed and not new_merged

        if status_changed:
            logger.info(
                "%s#%d review_status: %s -> %s (by %s)",
                repo, pr_number, old_status, new_status, actor,
            )
        if merged_changed:
            logger.info("%s#%d merged by %s", repo, pr_number, actor)
        if closed_changed:
            logger.info("%s#%d closed without merging by %s", repo, pr_number, actor)
        if not (status_changed or merged_changed or closed_changed):
            logger.debug("%s#%d: no effective state change", repo, pr_number)

        # Reconcile all messages linking this PR
        messages = self.store.get_messages_for_pr(repo, pr_number)
        if not messages:
            logger.debug("%s#%d: no tracked messages link this PR", repo, pr_number)
            return

        # Collect distinct recipient author_ids and their applicable transitions.
        # Using a set per author dedupes the case where the same user posted the
        # same PR link in multiple messages — otherwise they'd get one DM per message.
        recipient_transitions: dict[str, set[str]] = {}
        if status_changed and new_status in ("approved", "changes_requested", "commented") \
                and actor != pr_author:
            for msg in messages:
                if msg.done:
                    continue
                recipient_transitions.setdefault(msg.author_id, set()).add(new_status)
        if merged_changed:
            for msg in messages:
                recipient_transitions.setdefault(msg.author_id, set()).add("merged")
        if closed_changed:
            for msg in messages:
                recipient_transitions.setdefault(msg.author_id, set()).add("closed")

        for msg in messages:
            if msg.done and not (merged_changed or closed_changed):
                continue
            await self.reconcile_message(msg.message_id)

        # DM distinct recipients
        for author_id, transitions in recipient_transitions.items():
            pref = self.store.get_notify_pref(author_id)
            for transition in transitions:
                if _should_dm(pref, transition):
                    text = _format_dm(self.config, repo, pr_number, transition, actor)
                    await self._dm_user(author_id, text)

    async def _handle_label_event(self, event: LabelEvent) -> None:
        """Apply a labeled/unlabeled/opened event (full label set) and reconcile.

        Also runs the auto-post path: if the PR has the 'ready' label, isn't
        already tracked by any message, and its author has linked a Discord
        identity via /pr_linkaccount, the bot posts the PR link itself.
        No DM.
        """
        repo = event.repo
        pr_number = event.pr_number

        await self._maybe_autopost_ready_pr(event)

        categories = match_label_categories(event.label_names, self.config.label_map())
        new_labels = _join_label_categories(categories)

        existing = self.store.get_pr(repo, pr_number)
        old_labels = existing.labels if existing else ""
        if new_labels == old_labels:
            logger.debug("%s#%d: labels unchanged (%s)", repo, pr_number, new_labels or "none")
            return

        logger.info(
            "%s#%d labels: %s -> %s",
            repo, pr_number, old_labels or "none", new_labels or "none",
        )
        self.store.set_pr_labels(repo, pr_number, new_labels)
        for msg in self.store.get_messages_for_pr(repo, pr_number):
            if msg.done:
                continue
            await self.reconcile_message(msg.message_id)

    async def _maybe_autopost_ready_pr(  # pylint: disable=too-many-return-statements
        self, event: LabelEvent,
    ) -> None:
        """If the PR is ready, untracked, and the author is linked, post its link."""
        if not self.config.autopost_ready_prs:
            return
        repo = event.repo
        pr_number = event.pr_number
        if repo not in self.config.github_repos:
            return
        categories = match_label_categories(event.label_names, self.config.label_map())
        if "ready" not in categories:
            return
        # "Not already tracked" covers human-posted messages and any prior auto-post.
        if self.store.get_messages_for_pr(repo, pr_number):
            logger.debug(
                "autopost %s#%d: PR is already tracked by a message; skipping",
                repo, pr_number,
            )
            return

        pr_author = event.pr_author or ""
        pr_title = event.pr_title or ""
        if not pr_author:
            # 'labeled' payloads always carry user.login, but fall back to a REST
            # fetch if a future webhook shape omits it.
            try:
                state = await self.github.fetch_pr_state(repo, pr_number)
                pr_author = str(state.get("pr_author") or "")
                pr_title = str(state.get("title") or "")
            except Exception:  # pylint: disable=broad-exception-caught
                logger.exception("autopost %s#%d: PR author fetch failed", repo, pr_number)
                return
        if not pr_author:
            logger.debug("autopost %s#%d: PR author unknown; skipping", repo, pr_number)
            return

        link = self.store.get_link_by_github(pr_author)
        if link is None:
            logger.debug(
                "autopost %s#%d: GitHub user %r has no /pr_linkaccount; skipping",
                repo, pr_number, pr_author,
            )
            return

        # Guard against concurrent webhooks (e.g. "ready" + "tested" arriving back-to-back)
        # both passing the get_messages_for_pr check before either post is ingested.
        guard_key = (repo, pr_number)
        if guard_key in self._autoposting:
            logger.debug(
                "autopost %s#%d: autopost already in progress; skipping duplicate",
                repo, pr_number,
            )
            return
        self._autoposting.add(guard_key)

        channel = await self._get_channel()
        if channel is None:
            self._autoposting.discard(guard_key)
            return
        title_line = f"{pr_title}\n" if pr_title else ""
        content = (
            f"<@{link.discord_user_id}> | `{pr_author}` opened a pull request:\n"
            f"{title_line}"
            f"https://github.com/{repo}/pull/{pr_number}"
        )
        try:
            posted = await channel.send(
                content,
                allowed_mentions=discord.AllowedMentions(users=True, everyone=False, roles=False),
            )
        except discord.DiscordException:
            logger.exception(
                "autopost %s#%d: failed to send message", repo, pr_number,
            )
            self._autoposting.discard(guard_key)
            return
        logger.info(
            "Auto-posted ready PR %s#%d as message %s (discord=%s, gh=%s)",
            repo, pr_number, posted.id, link.discord_user_id, pr_author,
        )
        await self.ingest_message(
            message_id=str(posted.id),
            channel_id=str(posted.channel.id),
            guild_id=str(posted.guild.id) if posted.guild else None,
            author_id=link.discord_user_id,
            created_at=int(posted.created_at.timestamp()),
            content=content,
        )
        self._autoposting.discard(guard_key)

    async def _handle_check_event(self, event: CheckEvent) -> None:
        """Re-aggregate check status for each associated PR; reconcile and DM on failure."""
        repo = event.repo
        if repo not in self.config.github_repos:
            logger.debug("Check event for untracked repo %s; ignoring", repo)
            return
        for pr_number in event.pr_numbers:
            messages = self.store.get_messages_for_pr(repo, pr_number)
            if not messages:
                logger.debug("%s#%d: no tracked messages link this PR", repo, pr_number)
                continue
            try:
                new_failing = await self.github.fetch_checks_failing(repo, pr_number)
            except Exception:  # pylint: disable=broad-exception-caught
                logger.exception("Failed to fetch check status for %s#%d", repo, pr_number)
                continue
            if new_failing is None:
                # REST check-runs read unavailable (e.g. a fine-grained PAT, which
                # GitHub won't grant Checks access). Fall back to this webhook's own
                # suite conclusion. That's a single-suite signal, not a cross-suite
                # aggregate, so a PR with multiple check suites is last-suite-wins.
                new_failing = event.failing
                logger.info(
                    "%s#%d: REST check-runs unreadable; using webhook suite "
                    "conclusion (failing=%s)", repo, pr_number, new_failing,
                )

            existing = self.store.get_pr(repo, pr_number)
            old_failing = existing.checks_failing if existing else False
            if new_failing == old_failing:
                logger.debug("%s#%d: check status unchanged (failing=%s)", repo, pr_number, new_failing)
                continue

            logger.info("%s#%d checks_failing: %s -> %s", repo, pr_number, old_failing, new_failing)
            self.store.set_pr_checks_failing(repo, pr_number, new_failing)
            for msg in messages:
                if msg.done:
                    continue
                await self.reconcile_message(msg.message_id)

            # DM the failing transition (0 -> 1) to distinct non-off posters.
            if new_failing and not old_failing:
                seen_authors: set[str] = set()
                for msg in messages:
                    if msg.done or msg.author_id in seen_authors:
                        continue
                    seen_authors.add(msg.author_id)
                    pref = self.store.get_notify_pref(msg.author_id)
                    if _should_dm(pref, "checks_failed"):
                        await self._dm_user(
                            msg.author_id,
                            _format_dm(self.config, repo, pr_number, "checks_failed", ""),
                        )

    # ── Discord events ────────────────────────────────────────────────────────

    async def on_message(self, message: discord.Message) -> None:  # pylint: disable=arguments-differ
        if message.channel.id != self.config.discord_channel:
            return
        if self.user and message.author.id == self.user.id:
            return
        logger.info(
            "New message %s in #pending-prs from %s (%s)",
            message.id, message.author, message.author.id,
        )
        guild_id = str(message.guild.id) if message.guild else None
        created_at = int(message.created_at.timestamp())
        await self.ingest_message(
            message_id=str(message.id),
            channel_id=str(message.channel.id),
            guild_id=guild_id,
            author_id=str(message.author.id),
            created_at=created_at,
            content=message.content,
            extra_links=await self._resolve_reply_links(message),
        )

    async def on_raw_message_edit(
        self, payload: discord.RawMessageUpdateEvent
    ) -> None:
        if payload.channel_id != self.config.discord_channel:
            return
        channel = await self._get_channel()
        if channel is None:
            return
        try:
            message = await channel.fetch_message(payload.message_id)
        except discord.NotFound:
            return
        except discord.DiscordException:
            logger.exception("on_raw_message_edit: failed to fetch message %d", payload.message_id)
            return
        if self.user and message.author.id == self.user.id:
            return
        logger.info("Message %s edited; reprocessing", message.id)
        guild_id = str(message.guild.id) if message.guild else None
        created_at = int(message.created_at.timestamp())
        await self.ingest_message(
            message_id=str(message.id),
            channel_id=str(message.channel.id),
            guild_id=guild_id,
            author_id=str(message.author.id),
            created_at=created_at,
            content=message.content,
            extra_links=await self._resolve_reply_links(message),
        )

    async def on_raw_message_delete(
        self, payload: discord.RawMessageDeleteEvent
    ) -> None:
        if payload.channel_id != self.config.discord_channel:
            return
        msg_row = self.store.get_message(str(payload.message_id))
        if msg_row is None:
            return
        logger.info("Message %s deleted; dropping tracking rows", payload.message_id)
        self.store.delete_message(str(payload.message_id))

    # ── startup ───────────────────────────────────────────────────────────────

    async def _startup_poll_pr(self, pr: PrRow) -> None:
        """Fetch current GitHub state for one PR and reconcile+DM on transitions."""
        state = await self.github.fetch_pr_state(pr.repo, pr.pr_number)
        new_status = str(state["review_status"])
        new_merged = bool(state["merged"])
        new_closed = bool(state["closed"])
        new_labels = _join_label_categories(
            match_label_categories(list(state.get("labels", [])), self.config.label_map())
        )
        # None = check status couldn't be read; keep whatever the webhook set.
        raw_checks = state["checks_failing"]
        checks_known = raw_checks is not None
        new_checks_failing = bool(raw_checks) if checks_known else pr.checks_failing

        status_changed = new_status != pr.review_status
        merged_changed = new_merged and not pr.merged
        closed_changed = new_closed and not pr.closed and not new_merged
        labels_changed = new_labels != pr.labels
        checks_failed_transition = checks_known and new_checks_failing and not pr.checks_failing
        checks_changed = checks_known and new_checks_failing != pr.checks_failing

        if not (status_changed or merged_changed or closed_changed
                or labels_changed or checks_changed):
            logger.debug("Startup poll %s#%d: no change", pr.repo, pr.pr_number)
            return

        logger.info(
            "Startup poll %s#%d: missed transition (review %s->%s, merged %s->%s, "
            "closed %s->%s, labels %s->%s, checks_failing %s->%s)",
            pr.repo, pr.pr_number, pr.review_status, new_status,
            pr.merged, new_merged, pr.closed, new_closed,
            pr.labels or "none", new_labels or "none", pr.checks_failing, new_checks_failing,
        )

        self.store.upsert_pr(
            repo=pr.repo, pr_number=pr.pr_number,
            review_status=new_status, merged=new_merged, closed=new_closed,
            last_reviewer=state.get("last_reviewer"),  # type: ignore[arg-type]
            merged_by=state.get("merged_by"),  # type: ignore[arg-type]
            closed_by=state.get("closed_by"),  # type: ignore[arg-type]
        )
        self.store.set_pr_labels(pr.repo, pr.pr_number, new_labels)
        if checks_known:
            self.store.set_pr_checks_failing(pr.repo, pr.pr_number, new_checks_failing)
        messages = self.store.get_messages_for_pr(pr.repo, pr.pr_number)
        for msg in messages:
            await self.reconcile_message(msg.message_id)

        # DM for missed transitions — same gating as live webhook path, deduped by author_id
        seen_authors: set[str] = set()
        for msg in messages:
            if msg.done:
                continue
            author_id = msg.author_id
            if author_id in seen_authors:
                continue
            seen_authors.add(author_id)
            pref = self.store.get_notify_pref(author_id)
            if merged_changed and _should_dm(pref, "merged"):
                await self._dm_user(
                    author_id,
                    _format_dm(self.config, pr.repo, pr.pr_number, "merged",
                               str(state.get("merged_by") or "")),
                )
            if closed_changed and _should_dm(pref, "closed"):
                await self._dm_user(
                    author_id,
                    _format_dm(self.config, pr.repo, pr.pr_number, "closed",
                               str(state.get("closed_by") or "")),
                )
            if status_changed and new_status in ("approved", "changes_requested", "commented"):
                reviewer = str(state.get("last_reviewer") or "")
                pr_author = str(state.get("pr_author") or "")
                if _should_dm(pref, new_status) and (not reviewer or reviewer != pr_author):
                    await self._dm_user(
                        author_id,
                        _format_dm(self.config, pr.repo, pr.pr_number, new_status, reviewer),
                    )
            if checks_failed_transition and _should_dm(pref, "checks_failed"):
                await self._dm_user(
                    author_id,
                    _format_dm(self.config, pr.repo, pr.pr_number, "checks_failed", ""),
                )

    async def startup_reconcile(self) -> None:
        """
        One-shot startup sync:
        1. Scan recent channel history; ingest any untracked messages.
        2. Poll all active PRs for missed events; reconcile and DM on transitions.
        """
        await self.wait_until_ready()
        logger.info("Startup reconcile: scanning channel history...")
        channel = await self._get_channel()
        if channel is None:
            return

        cutoff = int(time.time()) - self.config.pr_retention_days * 86400
        msgs_in_window = self.store.get_messages_in_window()
        tracked_ids = {msg.message_id for msg in msgs_in_window}
        # Re-check messages previously stored with no links: config may have changed
        no_link_ids = {msg.message_id for msg in msgs_in_window if not msg.has_links}
        backfilled = 0

        async for message in channel.history(limit=None, oldest_first=True):
            if int(message.created_at.timestamp()) < cutoff:
                continue
            # Bot-authored messages are normally skipped, but auto-post messages
            # need to be re-ingested with the original poster's discord_user_id
            # as author_id so DMs go to the human, not the bot.
            override_author_id: Optional[str] = None
            if self.user and message.author.id == self.user.id:
                parsed = parse_autopost_message(message.content)
                if parsed is None:
                    continue
                override_author_id = parsed.discord_user_id
            msg_id = str(message.id)
            if msg_id not in tracked_ids or msg_id in no_link_ids:
                if msg_id not in tracked_ids:
                    backfilled += 1
                    logger.debug("Backfilling untracked message %s", message.id)
                else:
                    logger.debug("Re-checking no-link message %s (config may have changed)", message.id)
                guild_id = str(message.guild.id) if message.guild else None
                author_id = override_author_id or str(message.author.id)
                await self.ingest_message(
                    message_id=msg_id,
                    channel_id=str(message.channel.id),
                    guild_id=guild_id,
                    author_id=author_id,
                    created_at=int(message.created_at.timestamp()),
                    content=message.content,
                    extra_links=await self._resolve_reply_links(message),
                )
                await asyncio.sleep(0.5)

        active_prs = self.store.get_active_prs()
        logger.info(
            "Startup reconcile: backfilled %d new message(s); polling %d active PR(s)...",
            backfilled, len(active_prs),
        )
        for pr in active_prs:
            try:
                await self._startup_poll_pr(pr)
            except Exception:  # pylint: disable=broad-exception-caught
                logger.exception("Startup poll failed for %s#%d", pr.repo, pr.pr_number)
            await asyncio.sleep(0.5)

        # Final sweep: reconcile every tracked in-window message with
        # purge_unknown=True so bot reactions left from a previous config
        # (e.g. an emoji default that has since been changed) get cleaned up.
        # Reconciles run earlier in this method only cover newly-backfilled
        # messages and those linking PRs with a missed transition; this sweep
        # catches the rest.
        sweep_msgs = self.store.get_messages_in_window()
        logger.info(
            "Startup reconcile: sweeping %d tracked message(s) for stale reactions...",
            len(sweep_msgs),
        )
        for msg in sweep_msgs:
            try:
                await self.reconcile_message(msg.message_id, purge_unknown=True)
            except Exception:  # pylint: disable=broad-exception-caught
                logger.exception("Startup sweep failed for message %s", msg.message_id)
            await asyncio.sleep(0.2)

        logger.info("Startup reconcile complete.")

    # ── slash commands ────────────────────────────────────────────────────────

    def _register_commands(self) -> None:
        @self.tree.command(name="pr_notify", description="Set your PR review DM level")
        @app_commands.describe(state="When to receive DMs about your PRs")
        @app_commands.choices(state=[
            app_commands.Choice(name="off", value="off"),
            app_commands.Choice(name="review comments", value="review_comments"),
            app_commands.Choice(name="any review", value="any_review"),
            app_commands.Choice(name="all", value="all"),
        ])
        async def pr_notify(
            interaction: discord.Interaction,
            state: app_commands.Choice[str],
        ) -> None:
            logger.debug(
                "/pr_notify by %s (%s) -> %s",
                interaction.user, interaction.user.id, state.value,
            )
            self.store.set_notify_pref(str(interaction.user.id), state.value)
            labels = {
                "off": "off (no DMs)",
                "review_comments": "review comments only",
                "any_review": "any review",
                "all": "all (including merge/close)",
            }
            await interaction.response.send_message(
                f"PR DM preference set to **{labels.get(state.value, state.value)}**.",
                ephemeral=True,
            )

        @self.tree.command(name="pr_status", description="Show tracked PRs for a message")
        @app_commands.describe(message_link_or_id="Discord message link or ID")
        async def pr_status(
            interaction: discord.Interaction,
            message_link_or_id: str,
        ) -> None:
            logger.debug(
                "/pr_status by %s for %r", interaction.user, message_link_or_id
            )
            await interaction.response.defer(ephemeral=True)
            message_id = _parse_message_id(message_link_or_id)
            if message_id is None:
                await interaction.followup.send("Invalid message link or ID.", ephemeral=True)
                return
            msg_row = self.store.get_message(message_id)
            if msg_row is None:
                await interaction.followup.send("Message not tracked.", ephemeral=True)
                return
            links = self.store.get_links_for_message(message_id)
            if not links:
                await interaction.followup.send(
                    f"Message `{message_id}`: no PR links tracked (❓).", ephemeral=True
                )
                return
            lines = [f"Message `{message_id}` (done={msg_row.done}):"]
            for lnk in links:
                pr = self.store.get_pr(lnk.repo, lnk.pr_number)
                if pr:
                    terminal = "merged" if pr.merged else ("closed" if pr.closed else "open")
                    extra = f", labels={pr.labels}" if pr.labels else ""
                    if pr.checks_failing:
                        extra += ", checks=FAILING"
                    lines.append(
                        f"  {lnk.repo}#{lnk.pr_number}: "
                        f"review={pr.review_status}, {terminal}{extra}"
                    )
                else:
                    lines.append(f"  {lnk.repo}#{lnk.pr_number}: (no state cached)")
            await interaction.followup.send("\n".join(lines), ephemeral=True)

        @self.tree.command(name="pr_resync", description="Force-reprocess a message")
        @app_commands.describe(message_link_or_id="Discord message link or ID")
        async def pr_resync(
            interaction: discord.Interaction,
            message_link_or_id: str,
        ) -> None:
            logger.info(
                "/pr_resync by %s for %r", interaction.user, message_link_or_id
            )
            await interaction.response.defer(ephemeral=True)
            message_id = _parse_message_id(message_link_or_id)
            if message_id is None:
                await interaction.followup.send("Invalid message link or ID.", ephemeral=True)
                return
            channel = await self._get_channel()
            if channel is None:
                await interaction.followup.send("Channel not found.", ephemeral=True)
                return
            try:
                message = await channel.fetch_message(int(message_id))
            except discord.NotFound:
                await interaction.followup.send("Message not found in channel.", ephemeral=True)
                return
            except discord.DiscordException:
                await interaction.followup.send("Failed to fetch message.", ephemeral=True)
                return
            guild_id = str(message.guild.id) if message.guild else None
            await self.ingest_message(
                message_id=str(message.id),
                channel_id=str(message.channel.id),
                guild_id=guild_id,
                author_id=str(message.author.id),
                created_at=int(message.created_at.timestamp()),
                content=message.content,
                extra_links=await self._resolve_reply_links(message),
            )
            await interaction.followup.send(
                f"Message `{message_id}` resynced.", ephemeral=True
            )

        @self.tree.command(name="pr_repos", description="List the configured repos")
        async def pr_repos(interaction: discord.Interaction) -> None:
            logger.debug("/pr_repos by %s", interaction.user)
            if self.config.github_repos:
                lines = ["**Configured repos:**"] + [f"  - {r}" for r in self.config.github_repos]
                await interaction.response.send_message("\n".join(lines), ephemeral=True)
            else:
                await interaction.response.send_message("No repos configured.", ephemeral=True)

        link_group = app_commands.Group(
            name="pr_linkaccount",
            description="Link your Discord identity to a GitHub username for PR auto-posts",
        )

        @link_group.command(name="add", description="Link your Discord account to a GitHub username")
        @app_commands.describe(github_username="Your GitHub login (case-insensitive)")
        async def link_add(
            interaction: discord.Interaction, github_username: str,
        ) -> None:
            gh = github_username.strip()
            logger.info("/pr_linkaccount add by %s -> %r", interaction.user, gh)
            if not gh or not _GITHUB_USERNAME_RE.fullmatch(gh):
                await interaction.response.send_message(
                    "Not a valid GitHub username.", ephemeral=True,
                )
                return
            try:
                self.store.add_github_link(str(interaction.user.id), gh)
            except sqlite3.IntegrityError:
                await interaction.response.send_message(
                    f"`{gh}` is already linked to another Discord user.",
                    ephemeral=True,
                )
                return
            await interaction.response.send_message(
                f"Linked your Discord account to GitHub user `{gh}`.",
                ephemeral=True,
            )

        @link_group.command(name="remove", description="Remove your Discord ↔ GitHub link")
        async def link_remove(interaction: discord.Interaction) -> None:
            logger.info("/pr_linkaccount remove by %s", interaction.user)
            removed = self.store.remove_github_link(str(interaction.user.id))
            await interaction.response.send_message(
                "Link removed." if removed else "You have no link to remove.",
                ephemeral=True,
            )

        @link_group.command(name="list", description="List all Discord ↔ GitHub links")
        async def link_list(interaction: discord.Interaction) -> None:
            logger.debug("/pr_linkaccount list by %s", interaction.user)
            rows = self.store.list_github_links()
            if not rows:
                await interaction.response.send_message(
                    "No links registered.", ephemeral=True,
                )
                return
            lines = ["**Registered links:**"] + [
                f"- <@{r.discord_user_id}> ↔ `{r.github_username}`" for r in rows
            ]
            await interaction.response.send_message(
                "\n".join(lines),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )

        self.tree.add_command(link_group)


# ── utilities ─────────────────────────────────────────────────────────────────

def _parse_message_id(value: str) -> Optional[str]:
    """
    Accept a bare integer ID or a Discord message link
    (https://discord.com/channels/<guild>/<channel>/<id>).
    """
    value = value.strip()
    if value.isdigit():
        return value
    # Discord message link
    parts = value.rstrip("/").split("/")
    if parts and parts[-1].isdigit():
        return parts[-1]
    return None
