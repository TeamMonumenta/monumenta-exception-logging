# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Byron Marohn
import asyncio
import logging
import signal
from typing import Any

from quart import Quart, jsonify, request

from bot import PrBot
from config import from_env
from github import CheckEvent, GitHubClient, parse_webhook_payload, verify_signature
from store import Store

logger = logging.getLogger(__name__)


def create_app(bot: PrBot) -> Quart:
    app = Quart(__name__)

    @app.post("/github/webhook")
    async def github_webhook() -> Any:
        raw_body_or_str = await request.get_data()
        raw_body = raw_body_or_str if isinstance(raw_body_or_str, bytes) else raw_body_or_str.encode()
        signature = request.headers.get("X-Hub-Signature-256")

        if not verify_signature(bot.config.github_webhook_secret, raw_body, signature):
            logger.warning("Webhook: invalid or missing signature")
            return jsonify({"error": "unauthorized"}), 401

        event_type = request.headers.get("X-GitHub-Event", "")
        logger.debug("Webhook: received %s event (signature ok)", event_type)

        if event_type == "ping":
            logger.debug("Webhook: received ping from GitHub")
            return "", 200

        payload = await request.get_json(force=True)
        if payload is None:
            logger.debug("Webhook: %s event had no JSON body", event_type)
            return jsonify({"error": "expected JSON body"}), 400

        parsed = parse_webhook_payload(event_type, payload)
        if parsed is None:
            logger.debug(
                "Webhook: ignoring %s event (action=%s)",
                event_type, payload.get("action"),
            )
            return "", 204  # unrecognized or ignored event

        if isinstance(parsed, CheckEvent):
            logger.info(
                "Webhook: %s event for %s PR(s) %s",
                event_type, parsed.repo, parsed.pr_numbers,
            )
        else:
            logger.info(
                "Webhook: %s event for %s#%d", event_type, parsed.repo, parsed.pr_number
            )

        asyncio.create_task(bot.handle_pr_event(parsed))
        return "", 204

    @app.get("/health")
    async def health() -> Any:
        return jsonify({"ok": True})

    return app


async def _cleanup_loop(store: Store, bot: PrBot) -> None:
    while True:
        try:
            result = store.run_cleanup()
            if result["messages_deleted"] or result["prs_pruned"]:
                logger.info("Cleanup: %s", result)
            else:
                logger.debug("Cleanup: nothing to delete")
        except Exception:  # pylint: disable=broad-exception-caught
            logger.exception("Cleanup loop failed")
        await asyncio.sleep(bot.config.pr_cleanup_period_seconds)


def _configure_logging(verbose: bool) -> None:
    """
    Set the root log level from VERBOSE: DEBUG when verbose, INFO otherwise.

    discord.py and aiohttp are extremely chatty at DEBUG (per-heartbeat gateway
    traffic, every HTTP request), so they are pinned to INFO even in verbose mode —
    verbose raises the detail of *our* logging, not the libraries'.
    """
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    # basicConfig is a no-op if handlers already exist; set the level explicitly
    # so this is correct regardless of prior configuration.
    logging.getLogger().setLevel(level)
    logging.getLogger("discord").setLevel(logging.INFO)
    logging.getLogger("aiohttp").setLevel(logging.INFO)


def _mask_token(token: str) -> str:
    if len(token) <= 2:
        return "*" * len(token)
    return token[0] + "*" * (len(token) - 2) + token[-1]


async def _run_until_stopped(
    app: Quart,
    bot: PrBot,
    stop: asyncio.Event,
) -> None:
    tasks: list[asyncio.Task[Any]] = [
        asyncio.create_task(app.run_task(host="0.0.0.0", port=bot.config.port), name="quart"),
        asyncio.create_task(bot.start(bot.config.discord_token), name="discord"),
    ]
    stop_task: asyncio.Task[Any] = asyncio.create_task(stop.wait(), name="stop")
    done, _ = await asyncio.wait([stop_task, *tasks], return_when=asyncio.FIRST_COMPLETED)
    stop_task.cancel()

    if stop_task in done:
        logger.info("Shutdown signal received, stopping...")
        for task in tasks:
            task.cancel()
        await bot.close()
        await asyncio.gather(*tasks, return_exceptions=True)


async def main() -> None:
    config = from_env()
    _configure_logging(config.verbose)

    logger.info(
        "Starting pr-bot with config:\n"
        "  DB_PATH=%s\n"
        "  PORT=%s\n"
        "  DISCORD_TOKEN=%s\n"
        "  DISCORD_CHANNEL=%s\n"
        "  GITHUB_REPOS=%s\n"
        "  PR_RETENTION_DAYS=%s\n"
        "  PR_DM_ENABLED=%s\n"
        "  REVIEW_COMMENT_IS_CHANGES=%s\n"
        "  VERBOSE=%s",
        config.db_path,
        config.port,
        _mask_token(config.discord_token),
        config.discord_channel,
        ",".join(config.github_repos),
        config.pr_retention_days,
        config.pr_dm_enabled,
        config.review_comment_is_changes,
        config.verbose,
    )

    store = Store(config)
    github_client = GitHubClient(config.github_api_token)
    bot = PrBot(store, github_client, config)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    app = create_app(bot)

    # Start cleanup loop and startup reconcile once the bot is ready
    async def _on_before_serving() -> None:
        asyncio.create_task(_cleanup_loop(store, bot))
        asyncio.create_task(bot.startup_reconcile())

    app.before_serving(_on_before_serving)

    try:
        await _run_until_stopped(app, bot, stop)
    finally:
        store.close()
        logger.info("Shutdown complete.")


if __name__ == "__main__":
    asyncio.run(main())
