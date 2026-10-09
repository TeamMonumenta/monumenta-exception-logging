# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Byron Marohn
import asyncio
import logging
import os
import signal
from typing import TYPE_CHECKING, Any, Optional

from pydantic import ValidationError
from quart import Quart, jsonify, request
from werkzeug.exceptions import HTTPException, InternalServerError

from tracker.api import (
    CauseSummary, FixAttemptStatus, FrameSummary, GroupDetails, GroupSummary,
    OccurrenceSummary, Tracker,
)
from tracker.chisel import FixRequestOutcome, request_fix
from tracker.config import from_env
from tracker.ingest import IngestEvent, parse_event

if TYPE_CHECKING:
    from bot import ExceptionBot

logger = logging.getLogger(__name__)

# Tracks every asyncio.create_task() fired from this module so none is garbage
# collected mid-flight.
_background_tasks: set["asyncio.Task[Any]"] = set()


def _track_task(task: "asyncio.Task[Any]") -> None:
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


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


# ---------------------------------------------------------------------------
# JSON serialization (see NETWORK_API.md)
#
# All timestamps are epoch seconds as JSON integers. Optional fields are always
# emitted explicitly (as null when unset), never omitted, so API consumers can rely
# on every documented key being present.
# ---------------------------------------------------------------------------

def _frame_to_json(frame: FrameSummary) -> dict[str, Any]:
    return {
        'class_name': frame.class_name,
        'method': frame.method,
        'file': frame.file,
        'line': frame.line,
        'location': frame.location,
    }


def _cause_to_json(cause: CauseSummary) -> dict[str, Any]:
    return {
        'class_name': cause.class_name,
        'message': cause.message,
        'frames': [_frame_to_json(f) for f in cause.frames],
    }


def _summary_to_json(g: GroupSummary) -> dict[str, Any]:
    return {
        'fingerprint': g.fingerprint,
        'exception_class': g.exception_class,
        'message_template': g.message_template,
        'status': g.status,
        'first_seen': int(g.first_seen.timestamp()),
        'last_seen': int(g.last_seen.timestamp()),
        'total_count': g.total_count,
        'recent_count': g.recent_count,
        'server_counts': g.server_counts,
        'owning_frame': _frame_to_json(g.owning_frame) if g.owning_frame is not None else None,
        'owning_cause_class': g.owning_cause_class,
    }


def _details_to_json(d: GroupDetails) -> dict[str, Any]:
    return {
        'fingerprint': d.fingerprint,
        'exception_class': d.exception_class,
        'message_template': d.message_template,
        'status': d.status,
        'first_seen': int(d.first_seen.timestamp()),
        'last_seen': int(d.last_seen.timestamp()),
        'total_count': d.total_count,
        'logger': d.logger,
        'level': d.level,
        'thread': d.thread,
        'log_message_template': d.log_message_template,
        'cause_chain': [_cause_to_json(c) for c in d.cause_chain],
        'canonical_frames': [_frame_to_json(f) for f in d.canonical_frames],
        'canonical_trace': [_frame_to_json(f) for f in d.canonical_trace],
        'servers_affected': d.servers_affected,
        'server_counts_24h': d.server_counts_24h,
        'hourly_timeline': [[int(ts.timestamp()), count] for ts, count in d.hourly_timeline],
        'latest_message': d.latest_message,
        'latest_log_message': d.latest_log_message,
        'muted_by': d.muted_by,
        'muted_at': int(d.muted_at.timestamp()) if d.muted_at is not None else None,
        'resolved_by': d.resolved_by,
        'resolved_at': int(d.resolved_at.timestamp()) if d.resolved_at is not None else None,
    }


def _occurrence_to_json(o: OccurrenceSummary) -> dict[str, Any]:
    return {
        'timestamp': int(o.timestamp.timestamp()),
        'server': o.server,
        'message': o.message,
        'log_message': o.log_message,
    }


def _fix_attempt_to_json(s: FixAttemptStatus) -> dict[str, Any]:
    return {
        'job_id': s.job_id,
        'fingerprint': s.fingerprint,
        'status': s.status,
        'message': s.message,
        'summary': s.summary,
        'pr_url': s.pr_url,
        'queued_at': int(s.queued_at.timestamp()),
        'started_at': int(s.started_at.timestamp()) if s.started_at is not None else None,
        'completed_at': int(s.completed_at.timestamp()) if s.completed_at is not None else None,
    }


# ---------------------------------------------------------------------------
# /api/* request helpers
# ---------------------------------------------------------------------------

_INT_PARSE_ERROR = object()


def _parse_int(raw: Optional[str], default: Any) -> Any:
    """Parse a query-string integer.

    Returns `default` when raw is absent, the parsed int when raw is a valid
    integer, or _INT_PARSE_ERROR when raw is present but not parseable as one.
    """
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return _INT_PARSE_ERROR


def _resolve_fingerprint(tracker: Tracker, id_: str) -> Optional[str]:
    """Resolve an 8-char short ID or a full 64-char fingerprint, case-insensitively.

    Returns None when nothing matches. Both lookups are case-sensitive at the DB
    layer, so this lowercases first. Fingerprints are
    stored as lowercase SHA-256 hex, so lowercasing can only ever help: without it
    /api/groups/<UPPERCASE-64-CHAR> 404s while the same ID in its short form works,
    which is a trap for anyone pasting an ID out of a log or a chat message.
    """
    normalized = id_.lower()
    if len(normalized) == 64:
        # Cheap existence check; the caller re-reads full details when it needs them.
        return normalized if tracker.group_exists(normalized) else None
    return tracker.get_fingerprint_by_short_id(normalized)


def _require_authenticated_discord_id(tracker: Tracker) -> "str | tuple[Any, int, dict[str, str]]":
    """Verify the request's `Authorization: Bearer <token>` header.

    Returns the discord_id the token was minted for on success, or a
    ready-to-return (json_body, status, headers) response tuple on failure.
    Callers distinguish the two with isinstance, which narrows the type without
    needing an assert that `python -O` would strip.

    This discord_id is verified, not self-reported: the token can only have been
    obtained by running `/api-token create` as that Discord user (see
    NETWORK_API.md's "Attribution" section).
    """
    auth_header = {'WWW-Authenticate': 'Bearer'}
    scheme, _, raw_token = request.headers.get('Authorization', '').partition(' ')
    token = raw_token.strip()
    if scheme.lower() != 'bearer' or not token:
        return jsonify({'error': 'missing Authorization: Bearer token'}), 401, auth_header
    discord_id = tracker.verify_api_token(token)
    if discord_id is None:
        return jsonify({'error': 'invalid or expired token'}), 401, auth_header
    return discord_id


def create_app(
    tracker: Tracker,
    bot: Optional["ExceptionBot"] = None,
    verbose: bool = True,
    chisel_public_url: Optional[str] = None,
    chisel_allowed_users: Optional[list[str]] = None,
    chisel_fix_prompt_path: str = "fix_exception_prompt.md",
) -> Quart:
    app = Quart(__name__)
    allowed_users = chisel_allowed_users or []

    def _dispatch_discord_edit(fingerprint: str) -> None:
        """Fire-and-forget re-edit of the group's live Discord message, if any.

        Mirrors what the slash commands already do after a mutation. A no-op when
        Discord is disabled (bot is None) or the group has no tracked message.
        """
        if bot is None:
            return
        message_id = tracker.get_discord_message_id(fingerprint)
        if message_id is not None:
            _track_task(asyncio.create_task(bot.edit_exception_message(fingerprint, message_id)))

    async def _resolve_actor(discord_id: str) -> str:
        """Resolve a token's discord_id to a display name, or the raw ID if Discord
        is disabled.
        """
        if bot is None:
            return discord_id
        return await bot.resolve_display_name(discord_id)

    def _mutated_group_response(fingerprint: str) -> tuple[Any, int]:
        """Re-read a group after a mutation and return it as the JSON response.

        404s if the group vanished between the mutation and this read (an expiry or a
        /purge racing the request). Returning a status explicitly rather than asserting
        keeps the race a 404 instead of a 500, and does not depend on assertions being
        enabled, since `python -O` strips them.
        """
        details = tracker.get_group_details(fingerprint)
        if details is None:
            return jsonify({'error': 'group not found'}), 404
        return jsonify(_details_to_json(details)), 200

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
            _track_task(asyncio.create_task(bot.post_new_exception(fingerprint)))
        return '', 204

    @app.post('/chisel/poll')
    async def chisel_poll():
        if not chisel_public_url:
            return jsonify({'error': 'chisel integration not configured'}), 503
        job = tracker.claim_fix_attempt()
        if job is None:
            return '', 204
        return jsonify({
            'message': job.rendered_message,
            'requester_id': job.fingerprint[:8],
            'callback_url': f'{chisel_public_url}/chisel/callback/{job.job_id}',
        })

    @app.post('/chisel/callback/<job_id>')
    async def chisel_callback(job_id: str):
        if not chisel_public_url:
            return jsonify({'error': 'chisel integration not configured'}), 503
        raw = await request.get_json(force=True)
        if raw is None:
            return jsonify({'error': 'expected JSON body'}), 400
        status = raw.get('status')
        if status not in ('success', 'failure', 'declined'):
            return jsonify({'error': 'status must be success, failure, or declined'}), 400
        result = tracker.complete_fix_attempt(
            job_id,
            status=str(status),
            message=str(raw.get('message', '')),
            summary=str(raw.get('summary', '')),
            detail=str(raw.get('detail', '')),
            pr_url=raw.get('pr_url') or None,
        )
        if result is None:
            return jsonify({'error': 'unknown job_id'}), 404
        fingerprint, requester_discord_id = result
        if bot is not None:
            _track_task(asyncio.create_task(
                bot.on_fix_attempt_completed(
                    fingerprint, str(status),
                    str(raw.get('message', '')),
                    str(raw.get('summary', '')),
                    raw.get('pr_url'),
                    requester_discord_id,
                )
            ))
        return jsonify({'ok': True})

    # -----------------------------------------------------------------------
    # /api/* routes (see NETWORK_API.md)
    #
    # Reads are unauthenticated, same trust model as /ingest and /chisel/* above
    # (see README.md's "Security" section). Mutations require a bearer token
    # minted by /api-token create. There is no /api/purge; that stays Discord-only.
    # -----------------------------------------------------------------------

    @app.get('/api/health')
    async def api_health():
        return jsonify({'ok': True})

    @app.get('/api/groups')
    async def api_list_groups():
        status = request.args.get('status')
        server_filter = request.args.get('server')
        search = request.args.get('search')
        sort = request.args.get('sort', 'last_seen')

        window_hours = _parse_int(request.args.get('window_hours'), 24)
        limit = _parse_int(request.args.get('limit'), 50)
        offset = _parse_int(request.args.get('offset'), 0)
        new_within_hours = _parse_int(request.args.get('new_within_hours'), None)
        if _INT_PARSE_ERROR in (window_hours, limit, offset, new_within_hours):
            return jsonify({
                'error': 'limit, offset, window_hours, and new_within_hours must be integers'
            }), 400

        try:
            groups = tracker.list_groups(
                status=status, server=server_filter, search=search,
                new_within_hours=new_within_hours, window_hours=window_hours,
                sort=sort, limit=limit, offset=offset,
            )
            total = tracker.count_groups(
                status=status, server=server_filter, search=search,
                new_within_hours=new_within_hours,
            )
        except ValueError as e:
            return jsonify({'error': str(e)}), 400

        return jsonify({'groups': [_summary_to_json(g) for g in groups], 'total': total})

    @app.get('/api/groups/<group_id>')
    async def api_get_group(group_id: str):
        fingerprint = _resolve_fingerprint(tracker, group_id)
        if fingerprint is None:
            return jsonify({'error': 'group not found'}), 404
        details = tracker.get_group_details(fingerprint)
        if details is None:
            return jsonify({'error': 'group not found'}), 404
        return jsonify(_details_to_json(details))

    @app.get('/api/groups/<group_id>/occurrences')
    async def api_get_group_occurrences(group_id: str):
        fingerprint = _resolve_fingerprint(tracker, group_id)
        if fingerprint is None:
            return jsonify({'error': 'group not found'}), 404
        limit = _parse_int(request.args.get('limit'), 20)
        if limit is _INT_PARSE_ERROR:
            return jsonify({'error': 'limit must be an integer'}), 400
        occurrences = tracker.get_recent_occurrences(fingerprint, limit=limit)
        # Wrapped in an object rather than returned as a bare array, matching
        # /api/groups and /api/servers. A top-level array leaves no room to add
        # paging metadata later without breaking every existing consumer.
        return jsonify({'occurrences': [_occurrence_to_json(o) for o in occurrences]})

    @app.get('/api/groups/<group_id>/fix-attempts')
    async def api_get_group_fix_attempts(group_id: str):
        fingerprint = _resolve_fingerprint(tracker, group_id)
        if fingerprint is None:
            return jsonify({'error': 'group not found'}), 404
        limit = _parse_int(request.args.get('limit'), 20)
        if limit is _INT_PARSE_ERROR:
            return jsonify({'error': 'limit must be an integer'}), 400
        attempts = tracker.get_fix_attempts_for_group(fingerprint, limit=limit)
        return jsonify({'fix_attempts': [_fix_attempt_to_json(a) for a in attempts]})

    @app.get('/api/fix-attempts/<job_id>')
    async def api_get_fix_attempt(job_id: str):
        status = tracker.get_fix_attempt(job_id)
        if status is None:
            return jsonify({'error': 'unknown job_id'}), 404
        return jsonify(_fix_attempt_to_json(status))

    @app.get('/api/servers')
    async def api_list_servers():
        return jsonify({'servers': tracker.get_distinct_servers()})

    @app.post('/api/groups/<group_id>/mute')
    async def api_mute_group(group_id: str):
        fingerprint = _resolve_fingerprint(tracker, group_id)
        if fingerprint is None:
            return jsonify({'error': 'group not found'}), 404
        discord_id = _require_authenticated_discord_id(tracker)
        if not isinstance(discord_id, str):
            return discord_id
        tracker.mute_group(fingerprint, actor=await _resolve_actor(discord_id))
        _dispatch_discord_edit(fingerprint)
        return _mutated_group_response(fingerprint)

    @app.post('/api/groups/<group_id>/unmute')
    async def api_unmute_group(group_id: str):
        # Also un-resolves, matching /unmute's existing "always unmutes" semantics
        # (README.md); unmute_group clears both mute and resolve attribution.
        fingerprint = _resolve_fingerprint(tracker, group_id)
        if fingerprint is None:
            return jsonify({'error': 'group not found'}), 404
        discord_id = _require_authenticated_discord_id(tracker)
        if not isinstance(discord_id, str):
            return discord_id
        tracker.unmute_group(fingerprint, actor=await _resolve_actor(discord_id))
        _dispatch_discord_edit(fingerprint)
        return _mutated_group_response(fingerprint)

    @app.post('/api/groups/<group_id>/resolve')
    async def api_resolve_group(group_id: str):
        fingerprint = _resolve_fingerprint(tracker, group_id)
        if fingerprint is None:
            return jsonify({'error': 'group not found'}), 404
        discord_id = _require_authenticated_discord_id(tracker)
        if not isinstance(discord_id, str):
            return discord_id
        tracker.resolve_group(fingerprint, actor=await _resolve_actor(discord_id))
        _dispatch_discord_edit(fingerprint)
        return _mutated_group_response(fingerprint)

    @app.post('/api/groups/<group_id>/fix')
    async def api_request_fix(group_id: str):  # pylint: disable=too-many-return-statements
        # Status-code precedence (see NETWORK_API.md): unknown group -> 404;
        # missing/invalid bearer token -> 401; CHISEL_PUBLIC_URL unset -> 503;
        # discord_id not in CHISEL_ALLOWED_USERS (only when non-empty) -> 403;
        # already-active -> 409.
        fingerprint = _resolve_fingerprint(tracker, group_id)
        if fingerprint is None:
            return jsonify({'error': 'group not found'}), 404
        discord_id = _require_authenticated_discord_id(tracker)
        if not isinstance(discord_id, str):
            return discord_id
        if not chisel_public_url:
            return jsonify({'error': 'chisel integration not configured'}), 503
        if allowed_users and discord_id not in allowed_users:
            return jsonify({'error': 'discord_id not authorized to request fixes'}), 403

        result = request_fix(
            tracker, fingerprint, discord_id, chisel_public_url, chisel_fix_prompt_path,
        )
        if result.outcome == FixRequestOutcome.QUEUED:
            logger.info(
                "Fix attempt queued via API: job %s for group %s by %s",
                result.job_id, fingerprint[:8], discord_id,
            )
            # Show the same in-flight marker the :wrench: reaction path adds, so the
            # channel message reflects that a fix is running no matter which surface
            # started it.
            if bot is not None:
                _track_task(asyncio.create_task(bot.add_fix_working_reaction(fingerprint)))
            return jsonify({'job_id': result.job_id})
        if result.outcome == FixRequestOutcome.ALREADY_ACTIVE:
            return jsonify({'error': 'a fix attempt is already active for this group'}), 409
        if result.outcome == FixRequestOutcome.GROUP_NOT_FOUND:
            return jsonify({'error': 'group not found'}), 404
        if result.outcome == FixRequestOutcome.TEMPLATE_UNREADABLE:
            return jsonify({'error': 'fix prompt template could not be read'}), 500
        # NOT_CONFIGURED can't happen here since chisel_public_url was checked above.
        return jsonify({'error': 'fix request failed'}), 500

    # Quart's default error pages are HTML. For /api/* that means a typo'd path or an
    # unhandled DB error reaches the CLI as a wall of markup it can't pull a message
    # out of, so it falls back to printing a bare "HTTP 404". Everything under /api
    # answers in the same {"error": ...} shape the routes above use; other prefixes
    # (/ingest, /chisel/*) keep Quart's defaults.
    def _is_api_request() -> bool:
        return request.path.startswith('/api/')

    @app.errorhandler(404)
    async def api_not_found(err: HTTPException):
        if _is_api_request():
            return jsonify({'error': 'no such endpoint'}), 404
        return err  # Quart renders its own default page for non-/api paths

    @app.errorhandler(405)
    async def api_method_not_allowed(err: HTTPException):
        if _is_api_request():
            return jsonify({'error': 'method not allowed for this endpoint'}), 405
        return err

    @app.errorhandler(500)
    async def api_internal_error(err: Exception):
        # `err` here is the original exception, not an HTTPException. It is logged
        # either way; only the response body differs by path prefix.
        logger.error('Unhandled error serving %s', request.path, exc_info=err)
        if _is_api_request():
            return jsonify({'error': 'internal server error'}), 500
        return InternalServerError()

    @app.before_serving
    async def startup():
        _track_task(asyncio.create_task(_expiry_loop(tracker, bot)))

    return app


async def _expiry_loop(tracker: Tracker, bot: Optional["ExceptionBot"] = None) -> None:
    while True:
        try:
            result = tracker.run_expiry()
            logger.info('Expiry complete: %s', result)
            if bot is not None:
                for msg_id in result.get("discord_message_ids", []):
                    _track_task(asyncio.create_task(bot.delete_channel_message(msg_id)))
            timed_out = tracker.timeout_stale_fix_attempts()
            if timed_out:
                logger.info('Timed out %d stale fix attempt(s)', len(timed_out))
                if bot is not None:
                    for _job_id, fingerprint, requester_discord_id in timed_out:
                        _track_task(asyncio.create_task(
                            bot.on_fix_attempt_completed(
                                fingerprint, 'failure',
                                'Timed out: no response received',
                                '',
                                None,
                                requester_discord_id,
                            )
                        ))
        except Exception:  # pylint: disable=broad-exception-caught
            logger.exception('Expiry task failed')
        await asyncio.sleep(3600)


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
        "  SLASH_COMMAND_PREFIX=%s\n"
        "  CHISEL_PUBLIC_URL=%s",
        config.db_path,
        ','.join(config.app_packages),
        config.expiry_days,
        port,
        config.verbose,
        _mask_token(discord_token) if discord_token else '(not set)',
        channel_id if discord_token else '(not set)',
        refresh_period if discord_token else '(not set)',
        repr(slash_command_prefix) if discord_token else '(not set)',
        config.chisel_public_url or '(not set)',
    )

    tracker = Tracker(config)
    result = tracker.migrate_fingerprints()
    if result['updated'] or result['merged']:
        logger.info('Fingerprint migration: %s', result)

    # Register signal handlers so both Ctrl+C and Kubernetes SIGTERM trigger a clean shutdown.
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    bot: Optional["ExceptionBot"] = None
    if discord_token:
        from bot import ExceptionBot  # pylint: disable=import-outside-toplevel
        bot = ExceptionBot(tracker, channel_id, refresh_period, slash_command_prefix, config)
    app = create_app(
        tracker, bot, verbose=config.verbose, chisel_public_url=config.chisel_public_url,
        chisel_allowed_users=config.chisel_allowed_users,
        chisel_fix_prompt_path=config.chisel_fix_prompt_path,
    )

    try:
        await _run_until_stopped(app, port, bot, discord_token, stop)
    finally:
        tracker.close()
        logger.info('Shutdown complete.')


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
