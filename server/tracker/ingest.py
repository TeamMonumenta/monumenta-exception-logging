# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Byron Marohn
import json
import sqlite3
import zlib
from collections.abc import Iterable
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


def _frames_to_json_shape(frames: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Project frames onto the fields stored in the database.

    `location` is the source jar, which distinguishes our code from a third-party
    plugin's in an otherwise identical-looking trace.
    """
    return [
        {'class_name': f['class_name'], 'method': f['method'],
         'file': f.get('file'), 'line': f.get('line', -1),
         'location': f.get('location')}
        for f in frames
    ]


# Bounds on a stored cause chain. /ingest is unauthenticated, so the plugin's own
# depth budget (PROTOCOL.md) is a convention, not a guarantee: these caps are what
# stops a hand-crafted POST writing an unbounded chain into a single error_groups
# row that every Discord refresh tick then re-parses.
MAX_CAUSE_DEPTH = 4
MAX_CAUSE_FRAMES = 200


def flatten_cause_chain(exc: ExceptionModel) -> list[dict[str, Any]]:
    """Flatten exc's chained causes into a list, outermost cause first.

    The exception itself is excluded - only what it wrapped. Truncated to
    MAX_CAUSE_DEPTH causes of MAX_CAUSE_FRAMES frames each.
    """
    chain: list[dict[str, Any]] = []
    cause = exc.cause
    while cause is not None and len(chain) < MAX_CAUSE_DEPTH:
        chain.append({
            'class_name': cause.class_name,
            'message': cause.message or '',
            # The same keys _frames_to_json_shape produces, read straight off the
            # model: routing through model_dump() to share that helper costs about
            # 10x here, and this is the one path with a hostile-input cap.
            'frames': [
                {'class_name': f.class_name, 'method': f.method,
                 'file': f.file, 'line': f.line, 'location': f.location}
                for f in cause.frames[:MAX_CAUSE_FRAMES]
            ],
        })
        cause = cause.cause
    return chain


# Occurrences per (group, server, hour) that keep their raw payload. Past this the
# column is NULL: a group already has RAW_EVENT_CAP samples of itself for that hour,
# and the cap is what stops a misbehaving shard turning a ~1 KB/occurrence column
# into gigabytes. See SCHEMA.md.
RAW_EVENT_CAP = 50


def build_signature(
    exception_class: str, normalized_message: str, top_frames: list[dict[str, Any]]
) -> str:
    """The exact inputs `compute_fingerprint` consumed, as JSON.

    Stored so a regroup recomputes from what was hashed. Recomputing from
    message_template instead re-normalizes an already-normalized value, which is
    not a no-op: nested bracket data loses one nesting level per pass.
    """
    return json.dumps({
        'exception_class': exception_class,
        'message_template': normalized_message,
        'frames': [f"{f['class_name']}.{f['method']}" for f in top_frames],
    }, sort_keys=True)


def _descriptive_columns(event: IngestEvent) -> tuple[str, str, str, str, str]:
    """Columns describing how the exception was reported, for the newest occurrence.

    None of this is a fingerprint input, so refreshing it on every occurrence can
    neither split nor merge a group. See SCHEMA.md.
    """
    return (
        event.logger,
        event.level,
        event.thread,
        normalize_message(event.message),
        json.dumps(flatten_cause_chain(event.exception)),
    )


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

    canonical_frames_json = json.dumps(_frames_to_json_shape(top_frames))
    canonical_trace_json = json.dumps(_frames_to_json_shape(frames))

    with conn:
        row = conn.execute(
            'SELECT id FROM error_groups WHERE fingerprint = ?', (fingerprint,)
        ).fetchone()

        is_new = row is None
        if is_new:
            cur = conn.execute(
                """INSERT INTO error_groups
                   (fingerprint, exception_class, message_template, signature,
                    canonical_frames, canonical_trace, logger, level, thread,
                    log_message_template, cause_chain,
                    first_seen, last_seen, total_count, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 'active')""",
                (fingerprint, event.exception.class_name, normalized_msg,
                 build_signature(event.exception.class_name, normalized_msg, top_frames),
                 canonical_frames_json, canonical_trace_json,
                 *_descriptive_columns(event), timestamp_s, timestamp_s)
            )
            group_id = cur.lastrowid
        else:
            group_id = row['id']
            # The descriptive columns are rewritten, not preserved, so the group
            # describes its newest occurrence rather than one from months ago.
            # `signature` and `message_template` are fingerprint inputs and are
            # identical across the group by construction, so they are left alone.
            conn.execute(
                """UPDATE error_groups
                   SET last_seen = ?, total_count = total_count + 1, has_activity = 1,
                       canonical_frames = ?, canonical_trace = ?,
                       logger = ?, level = ?, thread = ?,
                       log_message_template = ?, cause_chain = ?
                   WHERE id = ?""",
                (timestamp_s, canonical_frames_json, canonical_trace_json,
                 *_descriptive_columns(event), group_id)
            )

        # Bumped before the occurrence insert so its post-increment value can gate
        # raw_event without a second count query.
        hour_count = conn.execute(
            """INSERT INTO server_hour_counts (group_id, server, hour_bucket, count)
               VALUES (?, ?, ?, 1)
               ON CONFLICT (group_id, server, hour_bucket)
               DO UPDATE SET count = count + 1
               RETURNING count""",
            (group_id, event.server_id, hour_bucket)
        ).fetchone()['count']

        raw_event = (
            zlib.compress(event.model_dump_json().encode('utf-8'))
            if hour_count <= RAW_EVENT_CAP else None
        )

        conn.execute(
            'INSERT INTO occurrences '
            '(group_id, server, timestamp, message, log_message, raw_event) '
            'VALUES (?, ?, ?, ?, ?, ?)',
            (group_id, event.server_id, timestamp_s, raw_message, event.message,
             raw_event)
        )

    return fingerprint, is_new
