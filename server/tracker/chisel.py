# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Byron Marohn
"""
Fix-request logic shared by the Discord :wrench: reaction handler and the HTTP
`POST /api/groups/<id>/fix` route, so the two surfaces cannot drift.
"""

import re
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from .api import FrameSummary, GroupDetails, Tracker


class FixRequestOutcome(Enum):
    QUEUED = "queued"
    ALREADY_ACTIVE = "already_active"
    TEMPLATE_UNREADABLE = "template_unreadable"
    NOT_CONFIGURED = "not_configured"   # CHISEL_PUBLIC_URL unset
    GROUP_NOT_FOUND = "group_not_found"  # get_group_details returned None


@dataclass
class FixRequestResult:
    outcome: FixRequestOutcome
    job_id: Optional[str] = None


def fmt_frame(frame: FrameSummary) -> str:
    file_info = f"{frame.file}:{frame.line}" if frame.file else "Unknown"
    return f"  at {frame.class_name}.{frame.method}({file_info})"


def render_fix_prompt(template: str, details: GroupDetails) -> str:
    """Substitute template variables with exception group data.

    Uses regex substitution rather than str.format() so that curly braces in
    exception messages and stack traces do not cause KeyError or IndexError.
    Unknown variables (not in the substitution map) are left as-is.
    """
    stacktrace = "\n".join(fmt_frame(f) for f in details.canonical_trace)
    servers = ", ".join(sorted(details.servers_affected)) if details.servers_affected else "none"
    subs: dict[str, str] = {
        "short_id": details.fingerprint[:8],
        "exception_class": details.exception_class,
        "message": details.message_template,
        "raw_message": details.latest_message if details.latest_message is not None else details.message_template,
        "stacktrace": stacktrace,
        "count": str(details.total_count),
        "servers": servers,
        "first_seen": details.first_seen.isoformat(),
        "last_seen": details.last_seen.isoformat(),
    }

    def _replace(m: re.Match[str]) -> str:
        return subs.get(m.group(1), m.group(0))

    return re.sub(r"\{(\w+)\}", _replace, template)


def request_fix(
    tracker: Tracker,
    fingerprint: str,
    requested_by_discord_id: str,
    chisel_public_url: Optional[str],
    prompt_path: str,
) -> FixRequestResult:
    """The full check-configured -> check-active -> load-group -> load-template ->
    render -> queue sequence, shared by the reaction handler and the HTTP API route so
    they cannot drift.

    Returns GROUP_NOT_FOUND when get_group_details() returns None. The HTTP route
    checks the group exists before calling this (so this is a narrow race there), but
    this function must still have a defined result for it rather than falling through
    with no outcome.

    IMPORTANT: the has-active-attempt check and the queue call must remain synchronous
    with no `await` between them (this function is itself synchronous). Do not
    restructure this into two awaited steps: on the single-threaded event loop, an
    `await` between the check and the queue would let two concurrent requests both
    queue a job for the same group, silently breaking the one-active-attempt-per-group
    guarantee.
    """
    if not chisel_public_url:
        return FixRequestResult(FixRequestOutcome.NOT_CONFIGURED)
    if tracker.has_active_fix_attempt(fingerprint):
        return FixRequestResult(FixRequestOutcome.ALREADY_ACTIVE)
    details = tracker.get_group_details(fingerprint)
    if details is None:
        return FixRequestResult(FixRequestOutcome.GROUP_NOT_FOUND)
    try:
        with open(prompt_path, encoding="utf-8") as fh:
            template = fh.read()
    except OSError:
        return FixRequestResult(FixRequestOutcome.TEMPLATE_UNREADABLE)

    rendered = render_fix_prompt(template, details)
    job_id = tracker.queue_fix_attempt(fingerprint, rendered, requested_by_discord_id)
    return FixRequestResult(FixRequestOutcome.QUEUED, job_id=job_id)
