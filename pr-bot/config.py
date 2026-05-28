# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Byron Marohn
import os
from dataclasses import dataclass, field


@dataclass
class PrBotConfig:
    discord_token: str
    discord_channel: int
    db_path: str = "prbot.db"
    port: int = 8080
    github_repos: list[str] = field(default_factory=lambda: [])
    github_webhook_secret: str = ""
    github_api_token: str = ""
    pr_retention_days: int = 21
    pr_cleanup_period_seconds: int = 3600
    pr_dm_enabled: bool = True
    review_comment_is_changes: bool = True
    reaction_approved: str = "✅"
    reaction_changes: str = "💬"
    reaction_merged: str = "🔀"
    reaction_closed: str = "❌"
    reaction_question: str = "❓"
    # Label-driven reactions (the label name to match is also configurable).
    label_ready: str = "ready"
    label_not_ready: str = "Not Ready/Delayed"
    label_tested: str = "Tested"
    label_monthly_balance: str = "monthly-balance"
    reaction_ready: str = "🟢"
    reaction_not_ready: str = "🟠"
    reaction_tested: str = "🧪"
    reaction_monthly_balance: str = "⚖️"
    # Automated-check status reaction (🐶 when any check is failing).
    reaction_checks_failed: str = "🐶"
    verbose: bool = True

    def label_map(self) -> dict[str, str]:
        """Map each configured label name (lower-cased) to its category key."""
        return {
            self.label_ready.lower(): "ready",
            self.label_not_ready.lower(): "not_ready",
            self.label_tested.lower(): "tested",
            self.label_monthly_balance.lower(): "monthly_balance",
        }

    def label_reaction(self, category: str) -> str:
        """Return the configured reaction for a label category key."""
        return {
            "ready": self.reaction_ready,
            "not_ready": self.reaction_not_ready,
            "tested": self.reaction_tested,
            "monthly_balance": self.reaction_monthly_balance,
        }[category]


def from_env() -> PrBotConfig:
    discord_token = os.environ["DISCORD_TOKEN"]
    discord_channel = int(os.environ["DISCORD_CHANNEL"])
    db_path = os.environ.get("DB_PATH", "prbot.db")
    port = int(os.environ.get("PORT", "8080"))

    raw_repos = os.environ["GITHUB_REPOS"]
    github_repos = [r.strip().lower() for r in raw_repos.split(",") if r.strip()]

    github_webhook_secret = os.environ["GITHUB_WEBHOOK_SECRET"]
    github_api_token = os.environ["GITHUB_API_TOKEN"]

    pr_retention_days = int(os.environ.get("PR_RETENTION_DAYS", "21"))
    pr_cleanup_period_seconds = int(os.environ.get("PR_CLEANUP_PERIOD_SECONDS", "3600"))
    pr_dm_enabled = os.environ.get("PR_DM_ENABLED", "true").lower() not in ("false", "0", "no")
    review_comment_is_changes = (
        os.environ.get("REVIEW_COMMENT_IS_CHANGES", "true").lower() not in ("false", "0", "no")
    )
    # VERBOSE is on unless explicitly set to "false" (case-insensitive); any other
    # value (including unset) enables verbose logging.
    verbose = os.environ.get("VERBOSE", "true").lower() != "false"

    return PrBotConfig(
        discord_token=discord_token,
        discord_channel=discord_channel,
        db_path=db_path,
        port=port,
        github_repos=github_repos,
        github_webhook_secret=github_webhook_secret,
        github_api_token=github_api_token,
        pr_retention_days=pr_retention_days,
        pr_cleanup_period_seconds=pr_cleanup_period_seconds,
        pr_dm_enabled=pr_dm_enabled,
        review_comment_is_changes=review_comment_is_changes,
        reaction_approved=os.environ.get("REACTION_APPROVED", "✅"),
        reaction_changes=os.environ.get("REACTION_CHANGES", "💬"),
        reaction_merged=os.environ.get("REACTION_MERGED", "🔀"),
        reaction_closed=os.environ.get("REACTION_CLOSED", "❌"),
        reaction_question=os.environ.get("REACTION_QUESTION", "❓"),
        label_ready=os.environ.get("LABEL_READY", "ready"),
        label_not_ready=os.environ.get("LABEL_NOT_READY", "Not Ready/Delayed"),
        label_tested=os.environ.get("LABEL_TESTED", "Tested"),
        label_monthly_balance=os.environ.get("LABEL_MONTHLY_BALANCE", "monthly-balance"),
        reaction_ready=os.environ.get("REACTION_READY", "🟢"),
        reaction_not_ready=os.environ.get("REACTION_NOT_READY", "🟠"),
        reaction_tested=os.environ.get("REACTION_TESTED", "🧪"),
        reaction_monthly_balance=os.environ.get("REACTION_MONTHLY_BALANCE", "⚖️"),
        reaction_checks_failed=os.environ.get("REACTION_CHECKS_FAILED", "🐶"),
        verbose=verbose,
    )
