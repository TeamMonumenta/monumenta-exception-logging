# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Byron Marohn
"""Discord bot for the Monumenta exception tracker.

Posts a channel message for each new exception group, edits it as the group
evolves, and provides slash commands for querying and managing groups.
"""

import asyncio
import logging
import re
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from tracker.api import FrameSummary, GroupDetails, GroupSummary, Tracker

logger = logging.getLogger(__name__)

_MAX_MSG_LEN = 2000
_MAX_NOTIFY_SUBS = 100   # maximum subscriptions per user
_NOTIFY_TEST_LIMIT = 5   # max DMs sent by /notify test
_EMOJI_MUTE = "\U0001F6AB"    # :no_entry:
_EMOJI_RESOLVE = "\u2705"     # :white_check_mark:
_EMOJI_QUESTION = "\u2753"    # :question:


# ---------------------------------------------------------------------------
# Message formatting
# ---------------------------------------------------------------------------

def _fmt_frame(frame: FrameSummary) -> str:
    file_info = f"{frame.file}:{frame.line}" if frame.file else "Unknown"
    return f"  at {frame.class_name}.{frame.method}({file_info})"


def _build_frames_block(frame_lines: list[str], available: int) -> str:
    """Return frame text that fits within *available* characters."""
    if not frame_lines:
        return ""
    total = len(frame_lines)
    full = "\n".join(frame_lines)
    if len(full) <= available:
        return full

    included: list[str] = []
    for i, line in enumerate(frame_lines):
        remaining = total - i - 1
        candidate = "\n".join(included + [line])
        if remaining > 0:
            suffix = f"\n  ... ({total - len(included) - 1} more frames)"
            if len(candidate + suffix) <= available:
                included.append(line)
            else:
                break
        else:
            if len(candidate) <= available:
                included.append(line)

    dropped = total - len(included)
    result = "\n".join(included)
    if dropped > 0:
        trailer = f"\n  ... ({dropped} more frames)"
        result = (result + trailer) if included else trailer.lstrip("\n")
    return result


def format_exception_message(details: GroupDetails, max_len: int = _MAX_MSG_LEN) -> str:
    """Build the Discord channel message for an exception group.

    Fits within max_len characters (default 2000). Pass a smaller value when
    the message will be prefixed with additional header text (e.g. notify DMs).
    """
    fp8 = details.fingerprint[:8]
    first_ts = int(details.first_seen.timestamp())
    last_ts = int(details.last_seen.timestamp())
    servers_str = ", ".join(sorted(details.servers_affected)) if details.servers_affected else "none"

    header = (
        f"Fingerprint: {fp8}\n"
        f"First seen: <t:{first_ts}:f>\n"
        f"Last seen: <t:{last_ts}:f>\n"
        f"Observed on: {servers_str}\n"
        f"Count: {details.total_count}\n"
    )

    exc_line = details.exception_class
    if details.message_template:
        exc_line += f": {details.message_template}"

    frame_lines = [_fmt_frame(f) for f in details.canonical_trace]

    error_open = f"Error: ```\n{exc_line}\n"
    error_close = "\n```"

    if details.status == "muted" and details.muted_at is not None:
        ts = int(details.muted_at.timestamp())
        by = details.muted_by or "unknown"
        wrap_prefix = f"Muted on: <t:{ts}:f> by {by}\n||\n"
        wrap_suffix = "\n||"
    elif details.status == "resolved" and details.resolved_at is not None:
        ts = int(details.resolved_at.timestamp())
        by = details.resolved_by or "unknown"
        wrap_prefix = f"Resolved on: <t:{ts}:f> by {by}\n~~\n"
        wrap_suffix = "\n~~"
    else:
        wrap_prefix = ""
        wrap_suffix = ""

    fixed = wrap_prefix + header + error_open + error_close + wrap_suffix
    available = max_len - len(fixed)
    frames_block = _build_frames_block(frame_lines, available)

    return wrap_prefix + header + error_open + frames_block + error_close + wrap_suffix


def format_notify_dm(details: GroupDetails, matching_rules: list[tuple[int, str]]) -> str:
    """Build the DM content for a notify alert.

    Prepends a header listing the matched rule IDs and patterns, then includes
    the standard exception message truncated to fit within _MAX_MSG_LEN total.
    """
    rule_parts = ", ".join(f"#{sub_id} (`{pattern}`)" for sub_id, pattern in matching_rules)
    header = f"Matched notify rule(s): {rule_parts}\n"
    body = format_exception_message(details, max_len=_MAX_MSG_LEN - len(header))
    return header + body


def _matches_notify(pattern: str, details: GroupDetails) -> bool:
    """Return True if the regex pattern matches any searchable field of the group.

    Searches the same three fields as /search: exception class, normalized
    message template, and canonical trace (reconstructed as a text blob of
    class.method(file) entries). Match is case-sensitive.
    """
    rx = re.compile(pattern)
    trace_text = " ".join(
        f"{f.class_name}.{f.method}({f.file or 'Unknown'})"
        for f in details.canonical_trace
    )
    return bool(
        rx.search(details.exception_class)
        or rx.search(details.message_template)
        or rx.search(trace_text)
    )


# ---------------------------------------------------------------------------
# Summary list formatting (for slash command responses)
# ---------------------------------------------------------------------------

def _fmt_summary_line(g: GroupSummary) -> str:
    fp8 = g.fingerprint[:8]
    servers = ",".join(sorted(g.server_counts.keys())) if g.server_counts else "—"
    return (
        f"`{fp8}` [{g.status}] **{g.exception_class}** "
        f"(recent: {g.recent_count}, total: {g.total_count}) "
        f"servers: {servers}"
    )


def _fmt_new_line(g: GroupSummary) -> str:
    fp8 = g.fingerprint[:8]
    servers = ",".join(sorted(g.server_counts.keys())) if g.server_counts else "—"
    last_ts = int(g.last_seen.timestamp())
    return (
        f"`{fp8}` [{g.status}] **{g.exception_class}** "
        f"(recent: {g.recent_count}, total: {g.total_count}) "
        f"servers: {servers}   last seen: <t:{last_ts}:f>"
    )


def _fmt_details_lines(details: GroupDetails) -> list[str]:
    """Build the line list for a group details response."""
    short_id = details.fingerprint[:8]
    lines = [
        f"**Details: `{short_id}`**",
        f"Class: `{details.exception_class}`",
        f"Status: {details.status}",
        f"First seen: <t:{int(details.first_seen.timestamp())}:f>",
        f"Last seen: <t:{int(details.last_seen.timestamp())}:f>",
        f"Total count: {details.total_count}",
        f"Servers: {', '.join(sorted(details.servers_affected)) or 'none'}",
        f"Logger: `{details.logger}`",
    ]
    if details.latest_message:
        lines.append(f"Latest message: `{details.latest_message}`")
    lines += ["**Stack trace:**"] + [_fmt_frame(f) for f in details.canonical_trace]
    if details.muted_by:
        ts = int(details.muted_at.timestamp()) if details.muted_at else 0
        lines.insert(1, f"Muted by {details.muted_by} on <t:{ts}:f>")
    if details.resolved_by:
        ts = int(details.resolved_at.timestamp()) if details.resolved_at else 0
        lines.insert(1, f"Resolved by {details.resolved_by} on <t:{ts}:f>")
    return lines


def _chunk_lines(lines: list[str], limit: int = _MAX_MSG_LEN) -> list[str]:
    """Split lines into chunks that each fit within *limit* characters."""
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for line in lines:
        needed = len(line) + (1 if current else 0)
        if current_len + needed > limit:
            chunks.append("\n".join(current))
            current = [line]
            current_len = len(line)
        else:
            current.append(line)
            current_len += needed
    if current:
        chunks.append("\n".join(current))
    return chunks if chunks else ["(no results)"]


async def _send_chunks(interaction: discord.Interaction, lines: list[str]) -> None:
    """Send a list of text lines as one or more ephemeral followup messages."""
    chunks = _chunk_lines(lines)
    first = True
    for chunk in chunks:
        if first:
            await interaction.followup.send(chunk, ephemeral=True)
            first = False
        else:
            await interaction.followup.send(chunk, ephemeral=True)


# ---------------------------------------------------------------------------
# Bot
# ---------------------------------------------------------------------------

class ExceptionBot(commands.Bot):
    """Discord bot that tracks Monumenta exception groups."""

    def __init__(self, tracker: Tracker, channel_id: int, refresh_period: int,
                 slash_command_prefix: str = ""):
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)
        self.tracker = tracker
        self.channel_id = channel_id
        self.refresh_period = refresh_period
        self.slash_command_prefix = slash_command_prefix
        self._refresh_running = False

    async def setup_hook(self) -> None:
        self._register_commands()
        await self.tree.sync()
        self.loop.create_task(self._refresh_loop())
        self.loop.create_task(self._backfill_missing_messages())

    # --- Channel helpers ---

    async def _get_channel(self) -> Optional[discord.TextChannel]:
        channel = self.get_channel(self.channel_id)
        if channel is None:
            try:
                channel = await self.fetch_channel(self.channel_id)
            except discord.NotFound:
                logger.error("Discord channel %d not found", self.channel_id)
                return None
        return channel  # type: ignore[return-value]

    async def post_new_exception(self, fingerprint: str) -> None:
        """Send a new channel message for a freshly-observed exception group,
        then DM any users whose notify subscriptions match the group.
        """
        channel = await self._get_channel()
        if channel is None:
            return
        details = self.tracker.get_group_details(fingerprint)
        if details is None:
            return
        content = format_exception_message(details)
        try:
            message = await channel.send(content)
            self.tracker.set_discord_message_id(fingerprint, str(message.id))
        except discord.DiscordException:
            logger.exception("Failed to post exception message for %s", fingerprint)
            return
        await self._notify_subscribers(details)

    async def _notify_subscribers(self, details: GroupDetails) -> None:
        """DM users whose notify subscriptions match a newly-observed group.

        Iterates all subscriptions, groups matching rules by Discord user, and
        sends one DM per user listing every matched rule ID and pattern followed
        by the standard exception message.
        """
        subs = self.tracker.get_all_notify_subscriptions()
        if not subs:
            return

        # Collect all matching (sub_id, pattern) pairs per user.
        user_matches: dict[str, list[tuple[int, str]]] = {}
        for sub_id, discord_user_id, pattern in subs:
            try:
                if _matches_notify(pattern, details):
                    user_matches.setdefault(discord_user_id, []).append((sub_id, pattern))
            except re.error:
                # Pattern somehow invalid (shouldn't happen — validated at add time).
                logger.warning("Invalid notify pattern #%d: %s", sub_id, pattern)

        for discord_user_id, matching_rules in user_matches.items():
            try:
                user = await self.fetch_user(int(discord_user_id))
                content = format_notify_dm(details, matching_rules)
                await user.send(content)
                logger.info(
                    "Notified user %s about group %s via rule(s) %s",
                    discord_user_id,
                    details.fingerprint[:8],
                    [r[0] for r in matching_rules],
                )
            except discord.NotFound:
                logger.warning("Could not find user %s for notify DM", discord_user_id)
            except discord.Forbidden:
                logger.warning("Could not DM user %s for notify (DMs disabled)", discord_user_id)
            except discord.DiscordException:
                logger.exception("Failed to DM user %s for notify", discord_user_id)

    async def edit_exception_message(self, fingerprint: str, message_id: str) -> None:
        """Edit an existing channel message with current group data."""
        channel = await self._get_channel()
        if channel is None:
            return
        details = self.tracker.get_group_details(fingerprint)
        if details is None:
            return
        content = format_exception_message(details)
        try:
            message = await channel.fetch_message(int(message_id))
            await message.edit(content=content)
        except discord.NotFound:
            logger.warning(
                "Message %s not found for fingerprint %s; clearing tracked ID",
                message_id, fingerprint
            )
            self.tracker.set_discord_message_id(fingerprint, None)
        except discord.DiscordException:
            logger.exception("Failed to edit message %s", message_id)

    async def delete_channel_message(self, message_id: str) -> None:
        """Delete a channel message by ID (e.g. after its group expires)."""
        channel = await self._get_channel()
        if channel is None:
            return
        try:
            message = await channel.fetch_message(int(message_id))
            await message.delete()
        except discord.NotFound:
            logger.warning("Message %s already gone; nothing to delete", message_id)
        except discord.DiscordException:
            logger.exception("Failed to delete message %s", message_id)

    async def _backfill_missing_messages(self) -> None:
        """On startup, post channel messages for any groups that lack a discord_message_id.

        This covers groups ingested while the bot was offline and groups whose fingerprint
        changed during the startup migration.
        """
        await self.wait_until_ready()
        fingerprints = self.tracker.get_fingerprints_without_discord_message()
        if not fingerprints:
            return
        logger.info("Backfilling Discord messages for %d untracked group(s)", len(fingerprints))
        for fingerprint in fingerprints:
            await self.post_new_exception(fingerprint)
            await asyncio.sleep(5)
        logger.info("Backfill complete")

    async def _refresh_loop(self) -> None:
        await self.wait_until_ready()
        while True:
            await asyncio.sleep(self.refresh_period)
            if self._refresh_running:
                logger.warning("Refresh loop skipping tick: previous run still in progress")
                continue
            self._refresh_running = True
            try:
                pairs = self.tracker.get_active_discord_messages()
                first = True
                for fingerprint, message_id in pairs:
                    if not first:
                        await asyncio.sleep(2)
                    first = False
                    await self.edit_exception_message(fingerprint, message_id)
                    self.tracker.clear_has_activity(fingerprint)
                for msg_id in self.tracker.pop_pending_discord_deletes():
                    await self.delete_channel_message(msg_id)
            finally:
                self._refresh_running = False

    # --- Reaction handlers ---

    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        """Mute, resolve, or show details based on the reaction emoji."""
        if payload.channel_id != self.channel_id:
            return
        if self.user and payload.user_id == self.user.id:
            return
        if payload.emoji.name not in (_EMOJI_MUTE, _EMOJI_RESOLVE, _EMOJI_QUESTION):
            return
        fingerprint = self.tracker.get_fingerprint_by_discord_message_id(str(payload.message_id))
        if fingerprint is None:
            return
        if payload.emoji.name == _EMOJI_QUESTION:
            await self._handle_question_reaction(payload, fingerprint)
            return
        actor = payload.member.display_name if payload.member else str(payload.user_id)
        if payload.emoji.name == _EMOJI_MUTE:
            ok = self.tracker.mute_group(fingerprint, actor=actor)
            action = "muted"
        else:
            ok = self.tracker.resolve_group(fingerprint, actor=actor)
            action = "resolved"
        if ok:
            await self.edit_exception_message(fingerprint, str(payload.message_id))
            logger.info("Reaction: %s group %s by %s", action, fingerprint[:8], actor)

    async def _handle_question_reaction(
        self, payload: discord.RawReactionActionEvent, fingerprint: str
    ) -> None:
        """DM the reacting user with group details, then remove the :question: reaction."""
        details = self.tracker.get_group_details(fingerprint)
        if details is None:
            return

        # Resolve a user we can both DM and pass to remove_reaction.
        # payload.member is populated for guild reactions; fall back to fetch_user.
        dm_user: discord.User | discord.Member
        if payload.member is not None:
            dm_user = payload.member
        else:
            try:
                dm_user = await self.fetch_user(payload.user_id)
            except discord.DiscordException:
                logger.exception("Could not fetch user %d for :question: DM", payload.user_id)
                return

        try:
            for chunk in _chunk_lines(_fmt_details_lines(details)):
                await dm_user.send(chunk)
            logger.info("Reaction: DMed details for group %s to %s", fingerprint[:8], dm_user)
        except discord.Forbidden:
            logger.warning("Could not DM user %s (DMs may be disabled)", dm_user)
        except discord.DiscordException:
            logger.exception("Failed to DM details for group %s to %s", fingerprint[:8], dm_user)

        # Remove the :question: reaction regardless of whether the DM succeeded.
        channel = await self._get_channel()
        if channel is None:
            return
        try:
            message = await channel.fetch_message(payload.message_id)
            await message.remove_reaction(payload.emoji, dm_user)
        except discord.Forbidden:
            logger.warning(
                "No permission to remove :question: reaction on message %d", payload.message_id
            )
        except discord.DiscordException:
            logger.exception(
                "Failed to remove :question: reaction on message %d", payload.message_id
            )

    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent) -> None:
        """Unmute a group when :no_entry: or :white_check_mark: is removed."""
        if payload.channel_id != self.channel_id:
            return
        if self.user and payload.user_id == self.user.id:
            return
        if payload.emoji.name not in (_EMOJI_MUTE, _EMOJI_RESOLVE):
            return
        fingerprint = self.tracker.get_fingerprint_by_discord_message_id(str(payload.message_id))
        if fingerprint is None:
            return
        ok = self.tracker.unmute_group(fingerprint)
        if ok:
            await self.edit_exception_message(fingerprint, str(payload.message_id))
            logger.info("Reaction removed: unmuted group %s by user %d",
                        fingerprint[:8], payload.user_id)

    # --- Slash command helpers ---

    def _resolve_short_id(self, short_id: str) -> Optional[str]:
        return self.tracker.get_fingerprint_by_short_id(short_id.lower()[:8])

    # --- Slash command registration ---

    def _register_commands(self) -> None:  # pylint: disable=too-many-statements
        p = self.slash_command_prefix

        @self.tree.command(name=f"{p}top", description="Top 20 active exception groups by recent count")
        @app_commands.describe(window_hours="Hours window to count recent occurrences (default 24)")
        async def cmd_top(interaction: discord.Interaction, window_hours: int = 24) -> None:
            await interaction.response.defer(ephemeral=True)
            groups = self.tracker.get_top_active_groups(limit=20, window_hours=window_hours)
            if not groups:
                await interaction.followup.send("No active groups found.", ephemeral=True)
                return
            lines = [f"**Top active groups (last {window_hours}h)**"] + [
                _fmt_summary_line(g) for g in groups
            ]
            await _send_chunks(interaction, lines)

        @self.tree.command(name=f"{p}new", description="Exception groups first seen in the last N hours")
        @app_commands.describe(
            hours="Look-back window in hours (default 24)",
            before="Only show groups first seen before this Unix timestamp (optional)",
        )
        async def cmd_new(
            interaction: discord.Interaction, hours: int = 24, before: Optional[int] = None
        ) -> None:
            await interaction.response.defer(ephemeral=True)
            groups = self.tracker.get_new_groups(hours=hours, before=before)
            if not groups:
                if before is not None:
                    await interaction.followup.send(
                        f"No new groups in the {hours}h window before <t:{before}:f>.",
                        ephemeral=True,
                    )
                else:
                    await interaction.followup.send(
                        f"No new groups in the last {hours}h.", ephemeral=True
                    )
                return
            header = (
                f"**New groups ({hours}h before <t:{before}:f>)**"
                if before is not None
                else f"**New groups (last {hours}h)**"
            )
            lines = [header] + [_fmt_new_line(g) for g in groups]
            await _send_chunks(interaction, lines)

        @self.tree.command(name=f"{p}search", description="Search exception groups by class, message, or stack frame")
        @app_commands.describe(query="Substring to search for in exception class, message, or stack trace (e.g. ParticleManager.java)")
        async def cmd_search(interaction: discord.Interaction, query: str) -> None:
            await interaction.response.defer(ephemeral=True)
            groups = self.tracker.search_groups(query)
            if not groups:
                await interaction.followup.send(
                    f"No groups matching `{query}`.", ephemeral=True
                )
                return
            lines = [f"**Search: `{query}`**"] + [_fmt_summary_line(g) for g in groups]
            await _send_chunks(interaction, lines)

        @self.tree.command(name=f"{p}server", description="Top exception groups for a specific server")
        @app_commands.describe(name="Server ID (e.g. survival-0)")
        async def cmd_server(interaction: discord.Interaction, name: str) -> None:
            await interaction.response.defer(ephemeral=True)
            groups = self.tracker.get_groups_for_server(name)
            if not groups:
                await interaction.followup.send(
                    f"No active groups for server `{name}`.", ephemeral=True
                )
                return
            lines = [f"**Groups for `{name}`**"] + [_fmt_summary_line(g) for g in groups]
            await _send_chunks(interaction, lines)

        @self.tree.command(name=f"{p}muted", description="List muted exception groups")
        async def cmd_muted(interaction: discord.Interaction) -> None:
            await interaction.response.defer(ephemeral=True)
            groups = self.tracker.get_muted_groups()
            if not groups:
                await interaction.followup.send("No muted groups.", ephemeral=True)
                return
            lines = ["**Muted groups**"] + [_fmt_summary_line(g) for g in groups]
            await _send_chunks(interaction, lines)

        @self.tree.command(name=f"{p}resolved", description="List resolved exception groups")
        async def cmd_resolved(interaction: discord.Interaction) -> None:
            await interaction.response.defer(ephemeral=True)
            groups = self.tracker.get_resolved_groups()
            if not groups:
                await interaction.followup.send("No resolved groups.", ephemeral=True)
                return
            lines = ["**Resolved groups**"] + [_fmt_summary_line(g) for g in groups]
            await _send_chunks(interaction, lines)

        @self.tree.command(name=f"{p}details", description="Full details for an exception group")
        @app_commands.describe(short_id="8-character short ID shown in group listings")
        async def cmd_details(interaction: discord.Interaction, short_id: str) -> None:
            await interaction.response.defer(ephemeral=True)
            fingerprint = self._resolve_short_id(short_id)
            if fingerprint is None:
                await interaction.followup.send(
                    f"No group with short ID `{short_id}`.", ephemeral=True
                )
                return
            details = self.tracker.get_group_details(fingerprint)
            if details is None:
                await interaction.followup.send(
                    f"No group with short ID `{short_id}`.", ephemeral=True
                )
                return
            await _send_chunks(interaction, _fmt_details_lines(details))

        @self.tree.command(name=f"{p}mute", description="Mute an exception group")
        @app_commands.describe(short_id="8-character short ID of the group to mute")
        async def cmd_mute(interaction: discord.Interaction, short_id: str) -> None:
            await interaction.response.defer(ephemeral=True)
            fingerprint = self._resolve_short_id(short_id)
            if fingerprint is None:
                await interaction.followup.send(
                    f"No group with short ID `{short_id}`.", ephemeral=True
                )
                return
            actor = interaction.user.display_name
            ok = self.tracker.mute_group(fingerprint, actor=actor)
            if not ok:
                await interaction.followup.send("Mute failed (group not found).", ephemeral=True)
                return
            msg_pairs = self.tracker.get_all_discord_messages()
            for fp, msg_id in msg_pairs:
                if fp == fingerprint:
                    await self.edit_exception_message(fingerprint, msg_id)
                    break
            await interaction.followup.send(f"Muted `{short_id}`.", ephemeral=True)

        @self.tree.command(name=f"{p}unmute", description="Unmute an exception group")
        @app_commands.describe(short_id="8-character short ID of the group to unmute")
        async def cmd_unmute(interaction: discord.Interaction, short_id: str) -> None:
            await interaction.response.defer(ephemeral=True)
            fingerprint = self._resolve_short_id(short_id)
            if fingerprint is None:
                await interaction.followup.send(
                    f"No group with short ID `{short_id}`.", ephemeral=True
                )
                return
            ok = self.tracker.unmute_group(fingerprint)
            if not ok:
                await interaction.followup.send("Unmute failed (group not found).", ephemeral=True)
                return
            msg_pairs = self.tracker.get_all_discord_messages()
            for fp, msg_id in msg_pairs:
                if fp == fingerprint:
                    await self.edit_exception_message(fingerprint, msg_id)
                    break
            await interaction.followup.send(f"Unmuted `{short_id}`.", ephemeral=True)

        @self.tree.command(name=f"{p}resolve", description="Mark an exception group as resolved")
        @app_commands.describe(short_id="8-character short ID of the group to resolve")
        async def cmd_resolve(interaction: discord.Interaction, short_id: str) -> None:
            await interaction.response.defer(ephemeral=True)
            fingerprint = self._resolve_short_id(short_id)
            if fingerprint is None:
                await interaction.followup.send(
                    f"No group with short ID `{short_id}`.", ephemeral=True
                )
                return
            actor = interaction.user.display_name
            ok = self.tracker.resolve_group(fingerprint, actor=actor)
            if not ok:
                await interaction.followup.send(
                    "Resolve failed (group not found).", ephemeral=True
                )
                return
            msg_pairs = self.tracker.get_all_discord_messages()
            for fp, msg_id in msg_pairs:
                if fp == fingerprint:
                    await self.edit_exception_message(fingerprint, msg_id)
                    break
            await interaction.followup.send(f"Resolved `{short_id}`.", ephemeral=True)

        # --- /notify subcommand group ---

        notify_group = app_commands.Group(
            name=f"{p}notify",
            description="Manage personal exception notification subscriptions",
        )

        @notify_group.command(
            name="add",
            description=f"Add a regex notification rule (max {_MAX_NOTIFY_SUBS} per user)",
        )
        @app_commands.describe(
            pattern="Python regex (case-sensitive) matched against exception class, "
                    "message, and stack trace"
        )
        async def notify_add(interaction: discord.Interaction, pattern: str) -> None:
            await interaction.response.defer(ephemeral=True)
            try:
                re.compile(pattern)
            except re.error as exc:
                await interaction.followup.send(
                    f"Invalid regex: {exc}", ephemeral=True
                )
                return
            user_id = str(interaction.user.id)
            try:
                sub_id = self.tracker.add_notify_subscription(user_id, pattern)
            except ValueError as exc:
                await interaction.followup.send(str(exc), ephemeral=True)
                return
            await interaction.followup.send(
                f"Added notify rule #{sub_id}: `{pattern}`", ephemeral=True
            )

        @notify_group.command(name="list", description="List your notification rules")
        async def notify_list(interaction: discord.Interaction) -> None:
            await interaction.response.defer(ephemeral=True)
            user_id = str(interaction.user.id)
            subs = self.tracker.list_notify_subscriptions(user_id)
            if not subs:
                await interaction.followup.send("You have no notify rules.", ephemeral=True)
                return
            lines = ["**Your notify rules:**"] + [
                f"#{sub_id} — `{pat}` (added <t:{int(created_at.timestamp())}:f>)"
                for sub_id, pat, created_at in subs
            ]
            await _send_chunks(interaction, lines)

        @notify_group.command(name="remove", description="Remove a notification rule by ID")
        @app_commands.describe(sub_id="Rule ID as shown in /notify list")
        async def notify_remove(interaction: discord.Interaction, sub_id: int) -> None:
            await interaction.response.defer(ephemeral=True)
            user_id = str(interaction.user.id)
            ok = self.tracker.remove_notify_subscription(user_id, sub_id)
            if not ok:
                await interaction.followup.send(
                    f"No rule with ID #{sub_id} found.", ephemeral=True
                )
                return
            await interaction.followup.send(
                f"Removed notify rule #{sub_id}.", ephemeral=True
            )

        @notify_group.command(
            name="test",
            description=f"Test a rule against all active groups (sends up to {_NOTIFY_TEST_LIMIT} DMs)",
        )
        @app_commands.describe(sub_id="Rule ID to test")
        async def notify_test(interaction: discord.Interaction, sub_id: int) -> None:
            await interaction.response.defer(ephemeral=True)
            user_id = str(interaction.user.id)
            user_subs = self.tracker.list_notify_subscriptions(user_id)
            sub = next((s for s in user_subs if s[0] == sub_id), None)
            if sub is None:
                await interaction.followup.send(
                    f"No rule with ID #{sub_id} found.", ephemeral=True
                )
                return
            _, pattern, _ = sub
            try:
                rx = re.compile(pattern)
            except re.error:
                await interaction.followup.send(
                    f"Rule #{sub_id} has an invalid pattern.", ephemeral=True
                )
                return

            fingerprints = self.tracker.get_active_fingerprints()
            sent = 0
            total_matches = 0
            for fp in fingerprints:
                details = self.tracker.get_group_details(fp)
                if details is None:
                    continue
                trace_text = " ".join(
                    f"{f.class_name}.{f.method}({f.file or 'Unknown'})"
                    for f in details.canonical_trace
                )
                matched = bool(
                    rx.search(details.exception_class)
                    or rx.search(details.message_template)
                    or rx.search(trace_text)
                )
                if not matched:
                    continue
                total_matches += 1
                if sent < _NOTIFY_TEST_LIMIT:
                    try:
                        content = format_notify_dm(details, [(sub_id, pattern)])
                        await interaction.user.send(content)
                        sent += 1
                    except discord.Forbidden:
                        await interaction.followup.send(
                            "Could not send DM (your DMs may be disabled).", ephemeral=True
                        )
                        return
                    except discord.DiscordException:
                        logger.exception(
                            "Failed to DM test result for rule #%d to user %s",
                            sub_id, user_id
                        )

            if total_matches == 0:
                await interaction.followup.send(
                    f"Rule #{sub_id} matched no active groups.", ephemeral=True
                )
            elif total_matches <= _NOTIFY_TEST_LIMIT:
                await interaction.followup.send(
                    f"Rule #{sub_id} matched {total_matches} group(s); sent {sent} DM(s).",
                    ephemeral=True,
                )
            else:
                extra = total_matches - _NOTIFY_TEST_LIMIT
                await interaction.followup.send(
                    f"Rule #{sub_id} matched {total_matches} group(s); "
                    f"sent {_NOTIFY_TEST_LIMIT} DMs ({extra} more matched, not sent).",
                    ephemeral=True,
                )

        self.tree.add_command(notify_group)
