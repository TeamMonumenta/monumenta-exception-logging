# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Byron Marohn
import hashlib
import hmac
import logging
import re
from dataclasses import dataclass
from typing import Any, Optional

import aiohttp

logger = logging.getLogger(__name__)

# ── link parsing ─────────────────────────────────────────────────────────────

_PR_LINK_RE = re.compile(
    r"https?://github\.com/(?P<owner>[\w.\-]+)/(?P<repo>[\w.\-]+)/pull/(?P<number>\d+)",
    re.IGNORECASE,
)


@dataclass
class ParsedPrLink:
    repo: str        # "owner/name", normalized lower-case
    pr_number: int


def parse_pr_links(content: str, configured_repos: list[str]) -> list[ParsedPrLink]:
    """Extract and de-duplicate PR links that match the configured repos."""
    seen: set[tuple[str, int]] = set()
    result: list[ParsedPrLink] = []
    for m in _PR_LINK_RE.finditer(content):
        repo = f"{m.group('owner')}/{m.group('repo')}".lower()
        if repo not in configured_repos:
            continue
        pr_number = int(m.group("number"))
        key = (repo, pr_number)
        if key in seen:
            continue
        seen.add(key)
        result.append(ParsedPrLink(repo=repo, pr_number=pr_number))
    return result


# ── autopost message parsing ──────────────────────────────────────────────────

# Recognizes the bot's own auto-post message format so startup_reconcile can
# rebuild tracking rows after a DB wipe. `<@!?\d+>` covers both the modern
# `<@123>` and legacy nickname-mention `<@!123>` forms.
_AUTOPOST_RE = re.compile(
    r"<@!?(?P<uid>\d+)>\s*\|\s*`(?P<gh>[^`]+)`\s+opened a pull request:",
)


@dataclass
class ParsedAutopost:
    discord_user_id: str
    github_username: str


def parse_autopost_message(content: str) -> Optional[ParsedAutopost]:
    """Return the (discord_user_id, github_username) embedded in a bot auto-post,
    or None if the content doesn't match the auto-post format."""
    m = _AUTOPOST_RE.search(content)
    if not m:
        return None
    return ParsedAutopost(
        discord_user_id=m.group("uid"), github_username=m.group("gh")
    )


# ── label matching ─────────────────────────────────────────────────────────────

def match_label_categories(
    label_names: list[str], label_map: dict[str, str]
) -> set[str]:
    """
    Map a PR's raw label names to the configured category keys.

    label_map is {lower-cased configured label name: category}; matching is
    case-insensitive (label capitalization varies across repos).
    """
    return {
        label_map[name.lower()]
        for name in label_names
        if name.lower() in label_map
    }


# ── check-run aggregation ───────────────────────────────────────────────────────

# Conclusions that mean a completed check failed. "cancelled", "neutral",
# "skipped" and "success" are treated as not-failing.
_FAILING_CONCLUSIONS = {
    "failure", "timed_out", "action_required", "startup_failure", "stale",
}


def _check_runs_failing(check_runs: list[dict[str, Any]]) -> bool:
    """True if any completed check run has a failing conclusion."""
    return any(
        str(cr.get("status", "")) == "completed"
        and str(cr.get("conclusion", "")) in _FAILING_CONCLUSIONS
        for cr in check_runs
    )


# ── HMAC verification ─────────────────────────────────────────────────────────

def verify_signature(
    secret: str, raw_body: bytes, signature_header: Optional[str]
) -> bool:
    """Return True iff the X-Hub-Signature-256 header matches HMAC-SHA256(secret, body)."""
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(
        secret.encode(), raw_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature_header)


# ── webhook event types ────────────────────────────────────────────────────────

@dataclass
class ReviewEvent:
    repo: str           # "owner/name" lower-case
    pr_number: int
    action: str         # "submitted" | "dismissed"
    review_state: str   # "approved" | "changes_requested" | "commented" | "dismissed"
    reviewer: str       # GitHub login of the reviewer
    pr_author: str      # GitHub login of the PR author


@dataclass
class PrLifecycleEvent:
    repo: str
    pr_number: int
    merged: bool
    closed: bool        # True for closed (merged or not)
    actor: str          # merged_by or closer login


@dataclass
class LabelEvent:
    repo: str
    pr_number: int
    label_names: list[str]   # full current label set on the PR (raw names)
    pr_author: Optional[str] = None   # GitHub login from the webhook payload
    pr_title: Optional[str] = None    # PR title from the webhook payload


@dataclass
class CheckEvent:
    repo: str
    pr_numbers: list[int]    # PRs associated with the completed check suite
    failing: bool            # this suite's own conclusion (fallback when REST is unreadable)


WebhookEvent = ReviewEvent | PrLifecycleEvent | LabelEvent | CheckEvent


def _parse_review_event(payload: dict[str, Any]) -> Optional[ReviewEvent]:
    if str(payload.get("action", "")) == "edited":
        return None
    review = payload.get("review", {})
    pr = payload.get("pull_request", {})
    return ReviewEvent(
        repo=str(payload.get("repository", {}).get("full_name", "")).lower(),
        pr_number=int(pr.get("number", 0)),
        action=str(payload.get("action", "")),
        review_state=str(review.get("state", "")).lower(),
        reviewer=str(review.get("user", {}).get("login", "")),
        pr_author=str(pr.get("user", {}).get("login", "")),
    )


def _parse_pull_request_event(
    payload: dict[str, Any]
) -> Optional[LabelEvent | PrLifecycleEvent]:
    action = str(payload.get("action", ""))
    pr = payload.get("pull_request", {})
    repo_full = str(payload.get("repository", {}).get("full_name", "")).lower()

    # 'opened' is included so a PR opened with the ready label already attached
    # flows through the same auto-post path as a later label add. GitHub fires
    # 'opened' (not 'labeled') for that case.
    if action in ("labeled", "unlabeled", "opened"):
        # The payload carries the full current label set — recompute from scratch.
        label_names = [
            str(lab.get("name", "")) for lab in pr.get("labels", []) if lab.get("name")
        ]
        pr_author = str(pr.get("user", {}).get("login", "")) or None
        pr_title = str(pr.get("title", "")) or None
        return LabelEvent(
            repo=repo_full, pr_number=int(pr.get("number", 0)),
            label_names=label_names, pr_author=pr_author, pr_title=pr_title,
        )

    if action != "closed":
        return None  # 'reopened' and all other actions are ignored

    is_merged = bool(pr.get("merged", False))
    if is_merged:
        actor = str(pr.get("merged_by", {}).get("login", ""))
    else:
        actor = str(payload.get("sender", {}).get("login", ""))
    return PrLifecycleEvent(
        repo=repo_full,
        pr_number=int(pr.get("number", 0)),
        merged=is_merged,
        closed=True,
        actor=actor,
    )


def _parse_check_suite_event(payload: dict[str, Any]) -> Optional[CheckEvent]:
    if str(payload.get("action", "")) != "completed":
        return None
    suite = payload.get("check_suite", {})
    pr_numbers = [
        int(pr.get("number", 0))
        for pr in suite.get("pull_requests", [])
        if pr.get("number")
    ]
    if not pr_numbers:
        return None  # branch push / fork PR with no associated PR — nothing to track
    conclusion = str(suite.get("conclusion", "")).lower()
    return CheckEvent(
        repo=str(payload.get("repository", {}).get("full_name", "")).lower(),
        pr_numbers=pr_numbers,
        failing=conclusion in _FAILING_CONCLUSIONS,
    )


def parse_webhook_payload(
    event_type: str, payload: dict[str, Any]
) -> Optional[WebhookEvent]:
    """Parse a GitHub webhook payload into a typed event, or None to ignore."""
    if event_type == "pull_request_review":
        return _parse_review_event(payload)
    if event_type == "pull_request":
        return _parse_pull_request_event(payload)
    if event_type == "check_suite":
        return _parse_check_suite_event(payload)
    return None


# ── latest-wins review state derivation ───────────────────────────────────────

def _derive_review_status(
    reviews: list[dict[str, Any]], pr_author: str = ""
) -> tuple[str, Optional[str]]:
    """
    Walk the reviews list and return (review_status, last_reviewer).

    Rules:
    - approved / changes_requested: latest one wins (re-review after fixes flips state)
    - commented: sets status to 'commented' only if current status is 'none', and
      only when the commenter is not the PR author (a self-comment never signals
      "needs attention")
    - dismissed: resets status to 'none'
    """
    status = "none"
    last_reviewer: Optional[str] = None

    for review in reviews:
        state = str(review.get("state", "")).lower()
        login = str(review.get("user", {}).get("login", ""))
        if state in ("approved", "changes_requested"):
            status = state
            last_reviewer = login
        elif state == "dismissed":
            status = "none"
            last_reviewer = None
        elif state == "commented":
            if status == "none" and (not pr_author or login != pr_author):
                status = "commented"
                last_reviewer = login

    return status, last_reviewer


# ── REST client ────────────────────────────────────────────────────────────────

class GitHubClient:
    _BASE = "https://api.github.com"

    def __init__(self, token: str) -> None:
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    async def _get_json(self, session: aiohttp.ClientSession, url: str) -> Any:
        logger.debug("GitHub GET %s", url)
        async with session.get(url) as resp:
            if resp.status >= 400:
                logger.warning("GitHub GET %s -> %d", url, resp.status)
            resp.raise_for_status()
            return await resp.json()

    async def fetch_pr_state(
        self, repo: str, pr_number: int
    ) -> dict[str, Any]:
        """
        Return a dict with keys: review_status, merged, closed, last_reviewer,
        merged_by, closed_by, pr_author, labels (raw names), checks_failing.

        checks_failing is True/False, or None when the check status could not be
        read (see _fetch_checks_failing_for_sha) — callers must treat None as
        "leave the existing check state alone".
        """
        async with aiohttp.ClientSession(headers=self._headers) as session:
            pr_data: dict[str, Any] = await self._get_json(
                session, f"{self._BASE}/repos/{repo}/pulls/{pr_number}"
            )
            # Reviews list (single page - PRs have few reviews)
            reviews_data: list[dict[str, Any]] = await self._get_json(
                session, f"{self._BASE}/repos/{repo}/pulls/{pr_number}/reviews"
            )
            logger.debug(
                "GitHub %s#%d: %d review(s) fetched", repo, pr_number, len(reviews_data)
            )
            head_sha = str(pr_data.get("head", {}).get("sha", ""))
            checks_failing = await self._fetch_checks_failing_for_sha(
                session, repo, head_sha
            )

        is_merged = bool(pr_data.get("merged", False))
        pr_state = str(pr_data.get("state", "open"))
        is_closed = pr_state == "closed"

        merged_by: Optional[str] = None
        if is_merged:
            merged_by_obj = pr_data.get("merged_by")
            if merged_by_obj:
                merged_by = str(merged_by_obj.get("login", ""))

        pr_author = str(pr_data.get("user", {}).get("login", ""))
        review_status, last_reviewer = _derive_review_status(reviews_data, pr_author)
        labels = [
            str(lab.get("name", "")) for lab in pr_data.get("labels", []) if lab.get("name")
        ]

        return {
            "review_status": review_status,
            "merged": is_merged,
            "closed": is_closed,
            "last_reviewer": last_reviewer,
            "merged_by": merged_by,
            "closed_by": None,  # closed-not-merged has no closer field in the REST API
            "pr_author": pr_author,
            "title": str(pr_data.get("title", "")),
            "labels": labels,
            "checks_failing": checks_failing,
        }

    async def _fetch_checks_failing_for_sha(
        self, session: aiohttp.ClientSession, repo: str, head_sha: str
    ) -> Optional[bool]:
        """
        Aggregate all check-runs for a commit.

        Returns True if any has failed, False if none are failing, or None if the
        status could not be determined. Reading the Checks API needs a classic
        PAT or a GitHub App token — a fine-grained PAT cannot be granted it
        (GitHub disabled the `Checks` permission for fine-grained tokens), so the
        request comes back 403. On any such failure we return None so callers
        leave the existing check state (set by the check_suite webhook) untouched
        rather than clobbering it with a false "passing".
        """
        if not head_sha:
            return False
        url = f"{self._BASE}/repos/{repo}/commits/{head_sha}/check-runs?per_page=100"
        try:
            data: dict[str, Any] = await self._get_json(session, url)
        except aiohttp.ClientResponseError as exc:
            if exc.status in (403, 404):
                logger.warning(
                    "GitHub %s @ %s: cannot read check-runs (HTTP %d) — the token "
                    "lacks Checks read access (fine-grained PATs can't be granted "
                    "it); leaving check state unchanged",
                    repo, head_sha[:8], exc.status,
                )
            else:
                logger.warning(
                    "GitHub %s @ %s: check-runs fetch failed (HTTP %d); leaving "
                    "check state unchanged", repo, head_sha[:8], exc.status,
                )
            return None
        except aiohttp.ClientError as exc:
            logger.warning(
                "GitHub %s @ %s: check-runs fetch failed (%s); leaving check "
                "state unchanged", repo, head_sha[:8], exc,
            )
            return None
        check_runs = data.get("check_runs", [])
        failing = _check_runs_failing(check_runs)
        logger.debug(
            "GitHub %s @ %s: %d check-run(s), failing=%s",
            repo, head_sha[:8], len(check_runs), failing,
        )
        return failing

    async def fetch_checks_failing(
        self, repo: str, pr_number: int
    ) -> Optional[bool]:
        """
        Aggregate the check-runs on a PR's *current* head commit; True if any
        has failed, False if none are, or None if the status could not be read
        (see _fetch_checks_failing_for_sha). Re-resolves the head SHA from the PR
        so a stale webhook SHA is never used.
        """
        async with aiohttp.ClientSession(headers=self._headers) as session:
            pr_data: dict[str, Any] = await self._get_json(
                session, f"{self._BASE}/repos/{repo}/pulls/{pr_number}"
            )
            head_sha = str(pr_data.get("head", {}).get("sha", ""))
            return await self._fetch_checks_failing_for_sha(session, repo, head_sha)
