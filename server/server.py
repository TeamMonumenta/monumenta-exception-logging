import asyncio
import logging
import os
from typing import TYPE_CHECKING, Optional

from pydantic import ValidationError
from quart import Quart, jsonify, request

from tracker.api import Tracker
from tracker.config import from_env
from tracker.ingest import parse_event

if TYPE_CHECKING:
    from bot import ExceptionBot

logger = logging.getLogger(__name__)


def create_app(tracker: Tracker, bot: Optional["ExceptionBot"] = None) -> Quart:
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


async def main():
    config = from_env()
    tracker = Tracker(config)
    port = int(os.environ.get('PORT', '8080'))

    discord_token = os.environ.get('DISCORD_TOKEN')
    if discord_token:
        from bot import ExceptionBot  # pylint: disable=import-outside-toplevel
        channel_id = int(os.environ.get('DISCORD_CHANNEL', '0'))
        refresh_period = int(os.environ.get('DISCORD_REFRESH_PERIOD_SECONDS', '300'))
        bot = ExceptionBot(tracker, channel_id, refresh_period)
        app = create_app(tracker, bot)
        await asyncio.gather(
            app.run_task(host='0.0.0.0', port=port),
            bot.start(discord_token),
        )
    else:
        app = create_app(tracker)
        await asyncio.gather(app.run_task(host='0.0.0.0', port=port))


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
