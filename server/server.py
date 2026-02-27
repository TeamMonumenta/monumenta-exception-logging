# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Byron Marohn
import asyncio
import logging
import os
import signal
from typing import TYPE_CHECKING, Any, Optional

from pydantic import ValidationError
from quart import Quart, jsonify, request

from tracker.api import Tracker
from tracker.config import from_env
from tracker.ingest import IngestEvent, parse_event

if TYPE_CHECKING:
    from bot import ExceptionBot

logger = logging.getLogger(__name__)


def _format_verbose_event(event: IngestEvent, fingerprint: str, is_new: bool) -> str:
    status = "NEW" if is_new else "DUP"
    short_fp = fingerprint[:8]
    exc = event.exception
    msg = exc.message or "(no message)"
    lines = [f"[{status}] {event.server_id} | {exc.class_name}: {msg} (fp: {short_fp})"]
    for frame in exc.frames[:10]:
        if frame.file and frame.line >= 0:
            location = f"({frame.file}:{frame.line})"
        elif frame.file:
            location = f"({frame.file})"
        else:
            location = "(Unknown Source)"
        lines.append(f"  at {frame.class_name}.{frame.method}{location}")
    if len(exc.frames) > 10:
        lines.append(f"  ... {len(exc.frames) - 10} more frames")
    return "\n".join(lines)


def create_app(tracker: Tracker, bot: Optional["ExceptionBot"] = None,
               verbose: bool = True) -> Quart:
    app = Quart(__name__)

    @app.post('/ingest')
    async def ingest_endpoint():
        raw = await request.get_json(force=True)
        if raw is None:
            return jsonify({'error': 'expected JSON body'}), 400
        try:
            event = parse_event(raw)
        except ValidationError as e:
            return jsonify({'error': e.errors()}), 400
        fingerprint, is_new = tracker.ingest_event(event)
        if verbose:
            logger.info(_format_verbose_event(event, fingerprint, is_new))
        if is_new and bot is not None:
            asyncio.create_task(bot.post_new_exception(fingerprint))
        return '', 204

    @app.before_serving
    async def startup():
        asyncio.create_task(_expiry_loop(tracker, bot))

    return app


async def _expiry_loop(tracker: Tracker, bot: Optional["ExceptionBot"] = None) -> None:
    while True:
        await asyncio.sleep(3600)
        try:
            result = tracker.run_expiry()
            logger.info('Expiry complete: %s', result)
            if bot is not None:
                for msg_id in result.get("discord_message_ids", []):
                    asyncio.create_task(bot.delete_channel_message(msg_id))
        except Exception:  # pylint: disable=broad-exception-caught
            logger.exception('Expiry task failed')


def _mask_token(token: str) -> str:
    if len(token) <= 2:
        return '*' * len(token)
    return token[0] + '*' * (len(token) - 2) + token[-1]


async def _run_until_stopped(
    app: Quart,
    port: int,
    bot: Optional["ExceptionBot"],
    discord_token: Optional[str],
    stop: asyncio.Event,
) -> None:
    """Run Quart and the optional Discord bot until a stop signal is received."""
    tasks: list[asyncio.Task[Any]] = [
        asyncio.create_task(app.run_task(host='0.0.0.0', port=port), name='quart'),
    ]
    if bot is not None and discord_token:
        tasks.append(asyncio.create_task(bot.start(discord_token), name='discord'))

    # asyncio.wait requires Task/Future objects, so wrap the stop event in a task.
    stop_task: asyncio.Task[Any] = asyncio.create_task(stop.wait(), name='stop')
    done, _ = await asyncio.wait([stop_task, *tasks], return_when=asyncio.FIRST_COMPLETED)
    stop_task.cancel()

    if stop_task in done:
        logger.info('Shutdown signal received, stopping...')
        for task in tasks:
            task.cancel()
        if bot is not None:
            await bot.close()
        await asyncio.gather(*tasks, return_exceptions=True)


async def main():
    config = from_env()
    port = int(os.environ.get('PORT', '8080'))
    discord_token = os.environ.get('DISCORD_TOKEN')
    channel_id = int(os.environ.get('DISCORD_CHANNEL', '0'))
    refresh_period = int(os.environ.get('DISCORD_REFRESH_PERIOD_SECONDS', '300'))
    slash_command_prefix = os.environ.get('SLASH_COMMAND_PREFIX', '')

    logger.info(
        "Starting with config:\n"
        "  DB_PATH=%s\n"
        "  APP_PACKAGES=%s\n"
        "  EXPIRY_DAYS=%s\n"
        "  PORT=%s\n"
        "  VERBOSE=%s\n"
        "  DISCORD_TOKEN=%s\n"
        "  DISCORD_CHANNEL=%s\n"
        "  DISCORD_REFRESH_PERIOD_SECONDS=%s\n"
        "  SLASH_COMMAND_PREFIX=%s",
        config.db_path,
        ','.join(config.app_packages),
        config.expiry_days,
        port,
        config.verbose,
        _mask_token(discord_token) if discord_token else '(not set)',
        channel_id if discord_token else '(not set)',
        refresh_period if discord_token else '(not set)',
        repr(slash_command_prefix) if discord_token else '(not set)',
    )

    tracker = Tracker(config)

    # Register signal handlers so both Ctrl+C and Kubernetes SIGTERM trigger a clean shutdown.
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    bot: Optional["ExceptionBot"] = None
    if discord_token:
        from bot import ExceptionBot  # pylint: disable=import-outside-toplevel
        bot = ExceptionBot(tracker, channel_id, refresh_period, slash_command_prefix)
    app = create_app(tracker, bot, verbose=config.verbose)

    try:
        await _run_until_stopped(app, port, bot, discord_token, stop)
    finally:
        tracker.close()
        logger.info('Shutdown complete.')


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
