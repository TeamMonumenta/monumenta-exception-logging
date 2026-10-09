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

from tracker.api import (
    FixAttemptStatus, FrameSummary, GroupDetails, GroupSummary, Tracker,
)
from tracker.chisel import FixRequestOutcome, fmt_frame, request_fix
from tracker.config import TrackerConfig

logger = logging.getLogger(__name__)

_MAX_MSG_LEN = 2000
_MAX_NOTIFY_SUBS = 100   # maximum subscriptions per user
_MAX_API_TOKENS = 20     # maximum API tokens per user
_NOTIFY_TEST_LIMIT = 5   # max DMs sent by /notify test
_MAX_EDIT_FAILURES = 3   # consecutive failed edits before a group stops being retried
_EMOJI_MUTE = "\U0001F6AB"    # :no_entry:
_EMOJI_RESOLVE = "\u2705"     # :white_check_mark:
_EMOJI_QUESTION = "\u2753"    # :question:


# ---------------------------------------------------------------------------
# Message formatting
# ---------------------------------------------------------------------------

def _build_frames_block(frame_lines: list[str], available: int) -> str:
    """Return frame text that fits within *available* characters.

    When frames must be truncated, the '... (N more frames)' trailer is always
    shown. Its space is pre-budgeted using the worst-case trailer length so it
    is guaranteed to fit.
    """
    if not frame_lines or available <= 0:
        return ""
    total = len(frame_lines)
    full = "\n".join(frame_lines)
    if len(full) <= available:
        return full

    # Pre-budget for the longest possible trailer so it always fits.
    max_trailer = f"\n  ... ({total} more frames)"
    frame_budget = available - len(max_trailer)

    if frame_budget <= 0:
        bare = max_trailer.lstrip("\n")
        return bare if len(bare) <= available else ""

    included: list[str] = []
    for line in frame_lines:
        candidate = "\n".join(included + [line])
        if len(candidate) <= frame_budget:
            included.append(line)
        else:
            break

    dropped = total - len(included)
    result = "\n".join(included)
    if included:
        return result + f"\n  ... ({dropped} more frames)"
    return f"  ... ({dropped} more frames)"


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

    frame_lines = [fmt_frame(f) for f in details.canonical_trace]

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

    # Skeleton length: everything except exc_line and frames.
    # Full structure: wrap_prefix + header + "Error: ```\n" + exc_line + "\n" + frames + "\n```" + wrap_suffix
    skeleton = wrap_prefix + header + "Error: ```\n\n\n```" + wrap_suffix
    budget = max_len - len(skeleton)

    if len(exc_line) > budget:
        exc_line = exc_line[:max(0, budget - 3)] + "..."

    frames_block = _build_frames_block(frame_lines, budget - len(exc_line))

    return wrap_prefix + header + f"Error: ```\n{exc_line}\n" + frames_block + "\n```" + wrap_suffix


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

    Searches exception class, normalized message template, and canonical trace
    (reconstructed as a text blob of class.method(file) entries). Match is
    case-sensitive. Deliberately narrower than /search, which also covers the log
    message and cause chain: widening this would make existing rules start
    matching text their authors never tested them against.
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


def _resolve_token_ttl(requested: Optional[int], default: int, maximum: int) -> int:
    """Resolve the lifetime for a newly minted API token, in hours.

    `requested` is the caller's `/api-token create` argument; falls back to
    `default` when omitted. Clamped to [1, maximum] rather than rejected, so a
    caller who asks for more than the configured ceiling still gets a token
    (just a shorter-lived one) instead of an error.
    """
    hours = requested if requested is not None else default
    return max(1, min(hours, max(1, maximum)))


def _format_api_token_created_message(token: str, expires_at: int, prefix: str) -> str:
    """Render the ephemeral reply for `/api-token create`.

    The token is wrapped in inline code *inside* the spoiler (`` ||`token`|| ``, not
    `||token||`) because Discord still applies markdown inside a spoiler: a token from
    `secrets.token_urlsafe` can contain `-`/`_`, and an unlucky run of those can be
    parsed as italics/underline, silently dropping characters from what gets copied.

    The literal `{token}` in the second line is a placeholder in example text, not the
    real secret. This is an f-string, so `{{token}}` is deliberately double-braced to
    produce that literal instead of interpolating (the actual secret only appears once,
    in the spoiler above).
    """
    return (
        f"Token (expires <t:{expires_at}:R>), shown once, copy it now:\n"
        f"||`{token}`||\n"
        f"Set it as `EXCTL_API_TOKEN` (or pass `--token`) for `exctl`, or send it "
        f"as `Authorization: Bearer {{token}}` directly. "
        f"Use `/{prefix}api-token revoke` to invalidate every token you've minted."
    )


# ---------------------------------------------------------------------------
# Summary list formatting (for slash command responses)
# ---------------------------------------------------------------------------

def _fmt_owning_suffix(g: GroupSummary) -> str:
    """` at Foo.bar:42 (via CauseClass)` for a summary line, or "" without an owning frame.

    Matches exctl's list line, so a wrapper class like ServerSchedulerException isn't
    the only hint at where the bug is.
    """
    frame = g.owning_frame
    if frame is None:
        return ""
    text = f"{frame.class_name.rsplit('.', 1)[-1]}.{frame.method}"
    if frame.line >= 0:
        text += f":{frame.line}"
    if g.owning_cause_class:
        text += f" (via {g.owning_cause_class.rsplit('.', 1)[-1]})"
    return f" at `{text}`"


def _fmt_summary_line(g: GroupSummary) -> str:
    fp8 = g.fingerprint[:8]
    servers = ",".join(sorted(g.server_counts.keys())) if g.server_counts else "—"
    return (
        f"`{fp8}` [{g.status}] **{g.exception_class}** "
        f"(recent: {g.recent_count}, total: {g.total_count}) "
        f"servers: {servers}" + _fmt_owning_suffix(g)
    )


def _fmt_new_line(g: GroupSummary) -> str:
    fp8 = g.fingerprint[:8]
    servers = ",".join(sorted(g.server_counts.keys())) if g.server_counts else "—"
    last_ts = int(g.last_seen.timestamp())
    return (
        f"`{fp8}` [{g.status}] **{g.exception_class}** "
        f"(recent: {g.recent_count}, total: {g.total_count}) "
        f"servers: {servers}   last seen: <t:{last_ts}:f>" + _fmt_owning_suffix(g)
    )


# Frames shown per cause after folding. Ingest keeps the first 200 frames of a
# cause, and truncation drops exactly the tail it would share with its enclosing
# trace, so folding alone doesn't bound a deep cause (a StackOverflowError, say).
_MAX_CAUSE_FRAMES_SHOWN = 30

# Longest exception or log message shown inline in a details view. Messages are
# uncapped at ingest, and _chunk_lines splits an over-long line blindly, which would
# cut an inline code span in half.
_MAX_INLINE_MESSAGE = 500


def _inline_code(text: str) -> str:
    """Render free text (an exception or log message) as one inline code span.

    A backtick in the text would close the span early and newlines break it, so
    backticks become a lookalike and newlines a visible marker.
    """
    text = text.replace("`", "\u02cb").replace("\r\n", "\n").replace("\n", " \u23ce ")
    if len(text) > _MAX_INLINE_MESSAGE:
        text = text[:_MAX_INLINE_MESSAGE - 3] + "..."
    return f"`{text}`"


def _shared_tail(frames: list[FrameSummary], enclosing: list[FrameSummary]) -> int:
    """Number of trailing frames `frames` has in common with `enclosing`."""
    shared = 0
    for mine, theirs in zip(reversed(frames), reversed(enclosing)):
        if mine != theirs:
            break
        shared += 1
    return shared


def _fmt_cause_chain_lines(details: GroupDetails) -> list[str]:
    """`Caused by:` blocks for each cause, outermost first.

    Frames a cause shares with the trace enclosing it are folded into `... N more`,
    the way Java prints them: a scheduler wrapper's cause otherwise repeats the
    whole scheduler and tick-loop stack below its app frames. What's left is capped
    at _MAX_CAUSE_FRAMES_SHOWN per cause; `exctl show` has every stored frame.
    """
    lines: list[str] = []
    enclosing = details.canonical_trace
    for cause in details.cause_chain:
        header = cause.class_name + (f": {cause.message}" if cause.message else "")
        lines.append(f"**Caused by:** {_inline_code(header)}")
        shared = _shared_tail(cause.frames, enclosing)
        unique = cause.frames[:len(cause.frames) - shared]
        lines += [fmt_frame(f) for f in unique[:_MAX_CAUSE_FRAMES_SHOWN]]
        hidden = shared + max(0, len(unique) - _MAX_CAUSE_FRAMES_SHOWN)
        if hidden:
            lines.append(f"  ... {hidden} more")
        enclosing = cause.frames
    return lines


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
        f"Logger: `{details.logger}`" + (f" [{details.level}]" if details.level else ""),
    ]
    if details.latest_message:
        lines.append(f"Latest message: {_inline_code(details.latest_message)}")
    # The accompanying log line is often the only place the real context is, e.g.
    # Paper's "Could not pass event X to Monumenta v11.88.1".
    if details.latest_log_message and details.latest_log_message != details.latest_message:
        lines.append(f"Logged as: {_inline_code(details.latest_log_message)}")
    lines += ["**Stack trace:**"] + [fmt_frame(f) for f in details.canonical_trace]
    lines += _fmt_cause_chain_lines(details)
    if details.status == "muted" and details.muted_by:
        ts = int(details.muted_at.timestamp()) if details.muted_at else 0
        lines.insert(1, f"Muted by {details.muted_by} on <t:{ts}:f>")
    if details.status == "resolved" and details.resolved_by:
        ts = int(details.resolved_at.timestamp()) if details.resolved_at else 0
        lines.insert(1, f"Resolved by {details.resolved_by} on <t:{ts}:f>")
    return lines


def _fmt_fix_history_lines(short_id: str, attempts: list[FixAttemptStatus]) -> list[str]:
    """Build the line list for a group's fix-attempt history response."""
    lines = [f"**Fix history: `{short_id}`**"]
    if not attempts:
        lines.append("No fix attempts.")
        return lines
    for a in attempts:
        ts = int(a.queued_at.timestamp())
        # The full job ID, since that's what `exctl fix-status`/`fix-cancel` take.
        lines.append(f"`{a.job_id}` [{a.status}] - queued <t:{ts}:f>")
        if a.message:
            lines.append(f"  {a.message}")
        if a.pr_url:
            lines.append(f"  {a.pr_url}")
    return lines


def _chunk_lines(lines: list[str], limit: int = _MAX_MSG_LEN) -> list[str]:
    """Split lines into chunks that each fit within *limit* characters."""
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for line in lines:
        # Split lines that exceed the limit into pieces.
        # Use [""] for empty lines so they still produce one part.
        if len(line) <= limit:
            parts = [line]
        else:
            parts: list[str] = []
            for i in range(0, len(line), limit):
                parts.append(line[i : i + limit])
        for part in parts:
            needed = len(part) + (1 if current else 0)
            if current_len + needed > limit:
                chunks.append("\n".join(current))
                current = [part]
                current_len = len(part)
            else:
                current.append(part)
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
                 slash_command_prefix: str = "",
                 config: Optional[TrackerConfig] = None):
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)
        self.tracker = tracker
        self.channel_id = channel_id
        self.refresh_period = refresh_period
        self.slash_command_prefix = slash_command_prefix
        self._refresh_running = False
        # Consecutive edit_exception_message failures per fingerprint. has_activity is
        # only cleared on a successful edit (so a missed edit is retried), which means a
        # group whose edit fails for a durable reason would otherwise be retried on every
        # refresh tick forever. After _MAX_EDIT_FAILURES the flag is cleared to stop the
        # loop; new activity on the group sets it again and the retries resume.
        self._edit_failures: dict[str, int] = {}
        cfg = config or TrackerConfig()
        self._chisel_public_url: Optional[str] = cfg.chisel_public_url
        self._chisel_fix_prompt_path: str = cfg.chisel_fix_prompt_path
        self._chisel_allowed_users: list[str] = cfg.chisel_allowed_users
        self._reaction_fix_request: str = cfg.reaction_fix_request
        self._reaction_fix_working: str = cfg.reaction_fix_working
        self._reaction_fix_success: str = cfg.reaction_fix_success
        self._reaction_fix_failure: str = cfg.reaction_fix_failure
        self._reaction_fix_declined: str = cfg.reaction_fix_declined
        self._purge_allowed_users: list[str] = cfg.purge_allowed_users
        self._api_token_default_ttl_hours: int = cfg.api_token_default_ttl_hours
        self._api_token_max_ttl_hours: int = cfg.api_token_max_ttl_hours

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
                # Pattern somehow invalid (shouldn't happen, validated at add time).
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
            # Clear only on a successful edit, so a failed one is retried by the next
            # refresh tick. has_activity means exactly "the channel message is stale".
            self.tracker.clear_has_activity(fingerprint)
            self._edit_failures.pop(fingerprint, None)
        except discord.NotFound:
            logger.warning(
                "Message %s not found for fingerprint %s; clearing tracked ID",
                message_id, fingerprint
            )
            # Deliberately leaves has_activity set: the group now has no tracked message
            # and is picked up by _backfill_missing_messages instead.
            self.tracker.set_discord_message_id(fingerprint, None)
            self._edit_failures.pop(fingerprint, None)
        except discord.DiscordException:
            logger.exception("Failed to edit message %s", message_id)
            self._record_edit_failure(fingerprint)

    def _record_edit_failure(self, fingerprint: str) -> None:
        """Give up retrying a group whose edit keeps failing.

        Without a ceiling, a durable failure (message too long to edit, a permission
        that was revoked, a channel the bot can no longer write to) would be retried on
        every refresh tick indefinitely, each one logging a traceback and spending a
        Discord API call. After _MAX_EDIT_FAILURES consecutive failures the flag is
        cleared so the loop stops; the next occurrence of the exception - or the next
        mute/unmute/resolve - sets has_activity again and retries resume from scratch.
        """
        failures = self._edit_failures.get(fingerprint, 0) + 1
        if failures < _MAX_EDIT_FAILURES:
            self._edit_failures[fingerprint] = failures
            return
        logger.error(
            "Giving up on editing message for group %s after %d consecutive failures; "
            "will retry when the group next sees activity",
            fingerprint[:8], failures,
        )
        self.tracker.clear_has_activity(fingerprint)
        self._edit_failures.pop(fingerprint, None)

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
            if self._refresh_running:
                logger.warning("Refresh loop skipping tick: previous run still in progress")
                await asyncio.sleep(self.refresh_period)
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
                for msg_id in self.tracker.pop_pending_discord_deletes():
                    await self.delete_channel_message(msg_id)
            finally:
                self._refresh_running = False
            await asyncio.sleep(self.refresh_period)

    # --- Reaction handlers ---

    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        """Mute, resolve, show details, or queue a fix based on the reaction emoji."""
        if payload.channel_id != self.channel_id:
            return
        if self.user and payload.user_id == self.user.id:
            return
        emoji = payload.emoji.name
        known = (_EMOJI_MUTE, _EMOJI_RESOLVE, _EMOJI_QUESTION, self._reaction_fix_request)
        if emoji not in known:
            return
        fingerprint = self.tracker.get_fingerprint_by_discord_message_id(str(payload.message_id))
        if fingerprint is None:
            return
        if emoji == self._reaction_fix_request:
            await self._handle_fix_request_reaction(payload, fingerprint)
            return
        if emoji == _EMOJI_QUESTION:
            await self._handle_question_reaction(payload, fingerprint)
            return
        actor = payload.member.display_name if payload.member else str(payload.user_id)
        if emoji == _EMOJI_MUTE:
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

    async def _handle_fix_request_reaction(
        self, payload: discord.RawReactionActionEvent, fingerprint: str
    ) -> None:
        """Queue a Chisel fix attempt and swap the trigger reaction to the working emoji.

        The wrench reaction is always removed regardless of whether the job is accepted,
        so it cannot linger on the message after a bot restart.
        """
        if not self._chisel_public_url:
            return

        # If an allowed-users list is configured, silently ignore unauthorised users.
        # The wrench is still removed below so no lingering reaction is left.
        user_allowed = (
            not self._chisel_allowed_users
            or str(payload.user_id) in self._chisel_allowed_users
        )

        job_queued = False
        if not user_allowed:
            pass  # fall through to wrench removal
        else:
            result = request_fix(
                self.tracker, fingerprint, str(payload.user_id),
                self._chisel_public_url, self._chisel_fix_prompt_path,
            )
            if result.outcome == FixRequestOutcome.QUEUED:
                job_queued = True
                logger.info(
                    "Fix attempt queued: job %s for group %s by user %d",
                    result.job_id, fingerprint[:8], payload.user_id,
                )
            elif result.outcome == FixRequestOutcome.ALREADY_ACTIVE:
                logger.info(
                    "Fix request ignored: active fix attempt already exists for %s",
                    fingerprint[:8],
                )
            elif result.outcome == FixRequestOutcome.TEMPLATE_UNREADABLE:
                logger.error(
                    "Could not read fix prompt template: %s", self._chisel_fix_prompt_path
                )
            # NOT_CONFIGURED can't happen here (checked above); GROUP_NOT_FOUND is a
            # silent no-op, matching prior behavior when get_group_details() returned None.

        # Always remove the wrench reaction so it cannot linger after a restart.
        channel = await self._get_channel()
        if channel is None:
            return
        try:
            message = await channel.fetch_message(payload.message_id)
            react_user: Optional[discord.User | discord.Member] = None
            if payload.member is not None:
                react_user = payload.member
            else:
                try:
                    react_user = await self.fetch_user(payload.user_id)
                except discord.DiscordException:
                    logger.warning(
                        "Could not fetch user %d to remove fix reaction", payload.user_id
                    )
            if react_user is not None:
                try:
                    await message.remove_reaction(self._reaction_fix_request, react_user)
                except discord.Forbidden:
                    logger.warning(
                        "No permission to remove fix reaction on message %d",
                        payload.message_id,
                    )
                except discord.DiscordException:
                    logger.exception(
                        "Failed to remove fix reaction on message %d", payload.message_id
                    )
            if job_queued and self._reaction_fix_working:
                await message.add_reaction(self._reaction_fix_working)
        except discord.DiscordException:
            logger.exception(
                "Failed to update reactions for fix request on message %d", payload.message_id
            )

    async def add_fix_working_reaction(self, fingerprint: str) -> None:
        """Mark a group's channel message as having a fix in flight.

        The :wrench: reaction handler does this inline as part of its reaction swap
        (see _handle_fix_request_reaction). A fix requested through
        POST /api/groups/<id>/fix has no reaction to swap, so the API route calls this
        instead - otherwise channel watchers would see the outcome emoji appear with no
        prior sign that anything was running, and on_fix_attempt_completed would be
        removing a working reaction that was never added.
        """
        if not self._reaction_fix_working:
            return
        message_id = self.tracker.get_discord_message_id(fingerprint)
        if message_id is None:
            return
        channel = await self._get_channel()
        if channel is None:
            return
        try:
            message = await channel.fetch_message(int(message_id))
            await message.add_reaction(self._reaction_fix_working)
        except discord.DiscordException:
            logger.exception(
                "Failed to add working reaction for API fix request on group %s",
                fingerprint[:8],
            )

    async def resolve_display_name(self, discord_id: str) -> str:
        """Resolve a Discord user ID to a display name: the guild member's
        nickname-aware display name, falling back to the account's global display
        name, and to the raw ID string if Discord can't resolve them.
        """
        try:
            user_id = int(discord_id)
        except ValueError:
            return discord_id
        channel = await self._get_channel()
        guild = getattr(channel, "guild", None)
        if guild is not None:
            member = guild.get_member(user_id)
            if member is None:
                try:
                    member = await guild.fetch_member(user_id)
                except discord.DiscordException:
                    member = None
            if member is not None:
                return member.display_name
        try:
            user = await self.fetch_user(user_id)
            return user.display_name
        except discord.DiscordException:
            return discord_id

    async def on_fix_attempt_completed(
        self,
        fingerprint: str,
        status: str,
        message: str,
        summary: str,
        pr_url: Optional[str] = None,
        requester_discord_id: Optional[str] = None,
    ) -> None:
        """Swap the working reaction to an outcome emoji and DM the requester."""
        message_id = self.tracker.get_discord_message_id(fingerprint)
        if message_id is None:
            logger.warning(
                "No Discord message found for fingerprint %s on fix completion", fingerprint[:8]
            )
        outcome = {
            "success": self._reaction_fix_success,
            "failure": self._reaction_fix_failure,
            "declined": self._reaction_fix_declined,
            # A cancelled job never ran, so it gets no outcome emoji; the working
            # reaction is just removed.
            "cancelled": "",
        }.get(status, self._reaction_fix_failure)

        discord_message_url: Optional[str] = None
        if message_id is not None:
            channel = await self._get_channel()
            if channel is not None:
                discord_message_url = (
                    f"https://discord.com/channels/{channel.guild.id}/{channel.id}/{message_id}"
                )
                try:
                    chan_message = await channel.fetch_message(int(message_id))
                    if self.user is not None and self._reaction_fix_working:
                        try:
                            await chan_message.remove_reaction(self._reaction_fix_working, self.user)
                        except (discord.NotFound, discord.HTTPException):
                            pass  # reaction may already be gone
                    if outcome:
                        await chan_message.add_reaction(outcome)
                except discord.DiscordException:
                    logger.exception(
                        "Failed to update reactions for fix completion on group %s",
                        fingerprint[:8],
                    )

        if status == "cancelled":
            logger.info("Fix cancelled for group %s: %s", fingerprint[:8], message)
        elif pr_url:
            logger.info("Fix completed for group %s: %s - %s", fingerprint[:8], status, pr_url)
        else:
            logger.info("Fix completed for group %s: %s", fingerprint[:8], status)

        if requester_discord_id is not None:
            await self._dm_fix_result(
                requester_discord_id, fingerprint, status, message, summary, pr_url,
                discord_message_url,
            )

    async def _dm_fix_result(
        self,
        discord_user_id: str,
        fingerprint: str,
        status: str,
        message: str,
        summary: str,
        pr_url: Optional[str],
        discord_message_url: Optional[str] = None,
    ) -> None:
        """DM the user who requested a fix with the outcome."""
        fp8 = fingerprint[:8]
        if status == "cancelled":
            status_line = (f"Your fix request for exception `{fp8}` was **cancelled** "
                           "before Chisel picked it up")
        else:
            status_line = f"Fix attempt **{status}** for exception `{fp8}`"
        lines = [status_line]
        if discord_message_url:
            lines.append(f"**Exception:** {discord_message_url}")
        if message:
            lines.append(f"**Result:** {message}")
        if pr_url:
            lines.append(f"**PR:** {pr_url}")
        if summary:
            lines.append(f"**Summary:** {summary}")
        try:
            user = await self.fetch_user(int(discord_user_id))
            for chunk in _chunk_lines(lines):
                await user.send(chunk)
            logger.info("DMed fix result for group %s to user %s", fp8, discord_user_id)
        except discord.NotFound:
            logger.warning("Could not find user %s to DM fix result", discord_user_id)
        except discord.Forbidden:
            logger.warning("Could not DM user %s for fix result (DMs disabled)", discord_user_id)
        except discord.DiscordException:
            logger.exception("Failed to DM fix result to user %s", discord_user_id)

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
        actor = payload.member.display_name if payload.member else str(payload.user_id)
        ok = self.tracker.unmute_group(fingerprint, actor=actor)
        if ok:
            await self.edit_exception_message(fingerprint, str(payload.message_id))
            logger.info("Reaction removed: unmuted group %s by %s",
                        fingerprint[:8], actor)

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

        @self.tree.command(name=f"{p}search", description="Search exception groups by class, message, log message, stack frame, or cause")
        @app_commands.describe(query="Substring of class, message, log message, stack trace, or causes (e.g. ParticleManager.java)")
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

        @self.tree.command(
            name=f"{p}fix-history", description="Fix attempt history for an exception group"
        )
        @app_commands.describe(short_id="8-character short ID shown in group listings")
        async def cmd_fix_history(interaction: discord.Interaction, short_id: str) -> None:
            await interaction.response.defer(ephemeral=True)
            fingerprint = self._resolve_short_id(short_id)
            if fingerprint is None:
                await interaction.followup.send(
                    f"No group with short ID `{short_id}`.", ephemeral=True
                )
                return
            attempts = self.tracker.get_fix_attempts_for_group(fingerprint)
            await _send_chunks(interaction, _fmt_fix_history_lines(short_id, attempts))

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
            msg_id = self.tracker.get_discord_message_id(fingerprint)
            if msg_id is not None:
                await self.edit_exception_message(fingerprint, msg_id)
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
            ok = self.tracker.unmute_group(fingerprint, actor=interaction.user.display_name)
            if not ok:
                await interaction.followup.send("Unmute failed (group not found).", ephemeral=True)
                return
            msg_id = self.tracker.get_discord_message_id(fingerprint)
            if msg_id is not None:
                await self.edit_exception_message(fingerprint, msg_id)
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
            msg_id = self.tracker.get_discord_message_id(fingerprint)
            if msg_id is not None:
                await self.edit_exception_message(fingerprint, msg_id)
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

        # --- /api-token subcommand group ---

        api_token_group = app_commands.Group(
            name=f"{p}api-token",
            description="Manage bearer tokens for the network API (see NETWORK_API.md)",
        )

        @api_token_group.command(
            name="create",
            description=f"Mint a network API bearer token attributed to you "
                        f"(max {_MAX_API_TOKENS} per user)",
        )
        @app_commands.describe(
            lifetime_hours=f"Hours until the token expires (default "
                           f"{self._api_token_default_ttl_hours}, max "
                           f"{self._api_token_max_ttl_hours})"
        )
        async def api_token_create(
            interaction: discord.Interaction, lifetime_hours: Optional[int] = None
        ) -> None:
            await interaction.response.defer(ephemeral=True)
            ttl_hours = _resolve_token_ttl(
                lifetime_hours, self._api_token_default_ttl_hours, self._api_token_max_ttl_hours
            )
            discord_id = str(interaction.user.id)
            try:
                token, expires_at = self.tracker.create_api_token(discord_id, ttl_hours)
            except ValueError as exc:
                await interaction.followup.send(str(exc), ephemeral=True)
                return
            await interaction.followup.send(
                _format_api_token_created_message(token, expires_at, p), ephemeral=True
            )

        @api_token_group.command(
            name="revoke",
            description="Revoke every network API token you've minted",
        )
        async def api_token_revoke(interaction: discord.Interaction) -> None:
            await interaction.response.defer(ephemeral=True)
            discord_id = str(interaction.user.id)
            count = self.tracker.revoke_api_tokens(discord_id)
            if count == 0:
                await interaction.followup.send("You have no active tokens to revoke.",
                                                 ephemeral=True)
                return
            await interaction.followup.send(f"Revoked {count} token(s).", ephemeral=True)

        self.tree.add_command(api_token_group)

        # --- /purge ---

        @self.tree.command(
            name=f"{p}purge",
            description="Purge exception groups from the database (admin only)",
        )
        @app_commands.describe(
            server="Delete groups where this server is the only contributor",
            older_than_days="Delete groups not seen in the last N days (early expiry; min 1)",
            fixed="Delete all groups marked as resolved",
            muted="Delete all groups marked as muted",
        )
        async def cmd_purge(
            interaction: discord.Interaction,
            server: Optional[str] = None,
            older_than_days: Optional[int] = None,
            fixed: Optional[bool] = None,
            muted: Optional[bool] = None,
        ) -> None:
            await interaction.response.defer(ephemeral=True)

            if not self._purge_allowed_users or str(interaction.user.id) not in self._purge_allowed_users:
                await interaction.followup.send("Not authorized.", ephemeral=True)
                return

            if not any([server, older_than_days is not None, fixed, muted]):
                await interaction.followup.send(
                    "Specify at least one filter: `server`, `older_than_days`, `fixed`, or `muted`.",
                    ephemeral=True,
                )
                return

            if older_than_days is not None and older_than_days < 1:
                await interaction.followup.send(
                    "`older_than_days` must be at least 1.", ephemeral=True
                )
                return

            total_groups = 0
            all_message_ids: set[str] = set()

            if older_than_days is not None:
                n, msg_ids = self.tracker.purge_older_than(older_than_days)
                total_groups += n
                all_message_ids.update(msg_ids)

            if server:
                n, msg_ids = self.tracker.purge_server(server)
                total_groups += n
                all_message_ids.update(msg_ids)

            if fixed:
                n, msg_ids = self.tracker.purge_by_status("resolved")
                total_groups += n
                all_message_ids.update(msg_ids)

            if muted:
                n, msg_ids = self.tracker.purge_by_status("muted")
                total_groups += n
                all_message_ids.update(msg_ids)

            for msg_id in all_message_ids:
                await self.delete_channel_message(msg_id)

            if total_groups == 0:
                await interaction.followup.send(
                    "No groups matched the specified filters.", ephemeral=True
                )
                return

            msg_count = len(all_message_ids)
            summary = f"Purge complete: {total_groups} group(s) deleted"
            if msg_count:
                summary += f", {msg_count} Discord message(s) removed."
            else:
                summary += "."
            await interaction.followup.send(summary, ephemeral=True)
