# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Byron Marohn
import json
import sqlite3
from typing import Any, Optional

from pydantic import BaseModel

from .config import TrackerConfig
from .fingerprint import compute_fingerprint, extract_app_frames, normalize_message


class FrameModel(BaseModel):
    class_name: str
    method: str
    file: Optional[str] = None
    line: int = -1
    location: Optional[str] = None


class ExceptionModel(BaseModel):
    class_name: str
    message: Optional[str] = None
    frames: list[FrameModel]
    cause: Optional['ExceptionModel'] = None


ExceptionModel.model_rebuild()


class IngestEvent(BaseModel):
    schema_version: int
    server_id: str
    timestamp_ms: int
    level: str
    logger: str
    thread: str
    message: str
    exception: ExceptionModel


def parse_event(raw: dict[str, Any]) -> IngestEvent:
    return IngestEvent.model_validate(raw)


def _frames_to_json_shape(frames: list[FrameModel]) -> list[dict[str, Any]]:
    return [
        {'class_name': f.class_name, 'method': f.method, 'file': f.file, 'line': f.line}
        for f in frames
    ]


# The plugin's own budget is 5, and it spends one on the outermost throwable, so a
# well-behaved payload contributes at most 4 causes. /ingest is unauthenticated, so
# these are enforced here rather than assumed: a hand-crafted POST is free to send a
# chain of any depth and width, and the result is written verbatim into a single
# error_groups row that is then re-parsed on every Discord refresh tick.
MAX_CAUSE_DEPTH = 4
MAX_CAUSE_FRAMES = 200


def flatten_cause_chain(exc: ExceptionModel) -> list[dict[str, Any]]:
    """Flatten exc's chained causes into a list, outermost cause first.

    The exception itself is excluded - only what it wrapped. Truncated to
    MAX_CAUSE_DEPTH causes of MAX_CAUSE_FRAMES frames each. The loop also guards
    against a cyclic payload; pydantic rejects one built from JSON long before it
    gets here, but nothing else stops a caller constructing the model directly.
    """
    chain: list[dict[str, Any]] = []
    seen: set[int] = set()
    cause = exc.cause
    while cause is not None and id(cause) not in seen and len(chain) < MAX_CAUSE_DEPTH:
        seen.add(id(cause))
        chain.append({
            'class_name': cause.class_name,
            'message': cause.message or '',
            'frames': _frames_to_json_shape(cause.frames[:MAX_CAUSE_FRAMES]),
        })
        cause = cause.cause
    return chain


def ingest_event(
    event: IngestEvent, conn: sqlite3.Connection, config: TrackerConfig
) -> tuple[str, bool]:
    timestamp_s = event.timestamp_ms // 1000
    hour_bucket = (timestamp_s // 3600) * 3600

    frames = [f.model_dump() for f in event.exception.frames]
    raw_message = event.exception.message or ''
    normalized_msg = normalize_message(raw_message)
    top_frames = extract_app_frames(frames, config.app_packages, config.fingerprint_frame_count)
    fingerprint = compute_fingerprint(event.exception.class_name, normalized_msg, top_frames)

    canonical_frames_json = json.dumps([
        {'class_name': f['class_name'], 'method': f['method'],
         'file': f.get('file'), 'line': f.get('line', -1)}
        for f in top_frames
    ])
    canonical_trace_json = json.dumps([
        {'class_name': f['class_name'], 'method': f['method'],
         'file': f.get('file'), 'line': f.get('line', -1)}
        for f in frames
    ])

    with conn:
        row = conn.execute(
            'SELECT id, status FROM error_groups WHERE fingerprint = ?',
            (fingerprint,)
        ).fetchone()

        is_new = row is None
        if is_new:
            # Group-level context, computed only on insert. Flattening the cause
            # chain and normalizing the log message are the two most expensive
            # things here, and on a repeat occurrence the results are discarded -
            # which for a hostile payload meant paying for them on every event.
            #
            # The log message accompanying the throwable (Paper's "Task #N for
            # Monumenta vX generated an exception") carries context the exception's
            # own message does not. Deliberately NOT fingerprinted: it varies
            # independently of the bug.
            normalized_log_msg = normalize_message(event.message)
            cause_chain_json = json.dumps(flatten_cause_chain(event.exception))
            cur = conn.execute(
                """INSERT INTO error_groups
                   (fingerprint, exception_class, message_template, canonical_frames,
                    canonical_trace, logger, level, log_message_template, cause_chain,
                    first_seen, last_seen, total_count, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 'active')""",
                (fingerprint, event.exception.class_name, normalized_msg,
                 canonical_frames_json, canonical_trace_json,
                 event.logger, event.level, normalized_log_msg, cause_chain_json,
                 timestamp_s, timestamp_s)
            )
            group_id = cur.lastrowid
        else:
            group_id = row['id']
            conn.execute(
                """UPDATE error_groups
                   SET last_seen = ?, total_count = total_count + 1, has_activity = 1
                   WHERE id = ?""",
                (timestamp_s, group_id)
            )

        conn.execute(
            'INSERT INTO occurrences (group_id, server, timestamp, message, log_message) '
            'VALUES (?, ?, ?, ?, ?)',
            (group_id, event.server_id, timestamp_s, raw_message, event.message)
        )

        conn.execute(
            """INSERT INTO server_hour_counts (group_id, server, hour_bucket, count)
               VALUES (?, ?, ?, 1)
               ON CONFLICT (group_id, server, hour_bucket)
               DO UPDATE SET count = count + 1""",
            (group_id, event.server_id, hour_bucket)
        )

    return fingerprint, is_new
