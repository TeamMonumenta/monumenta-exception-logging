# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Byron Marohn
import json
import sqlite3
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
    """Project frames onto the subset of fields stored in the database.

    `location` is dropped: it identifies the source jar, which is useful when
    reading a trace live but not worth storing on every frame of every group.
    """
    return [
        {'class_name': f['class_name'], 'method': f['method'],
         'file': f.get('file'), 'line': f.get('line', -1)}
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
            # The same four keys _frames_to_json_shape produces, read straight off
            # the model: routing through model_dump() to share that helper costs
            # about 10x here, and this is the one path with a hostile-input cap.
            'frames': [
                {'class_name': f.class_name, 'method': f.method,
                 'file': f.file, 'line': f.line}
                for f in cause.frames[:MAX_CAUSE_FRAMES]
            ],
        })
        cause = cause.cause
    return chain


def _log_context(event: IngestEvent) -> tuple[str, str, str]:
    """The three group-level log-context columns, as stored.

    Context, not identity: none of it is fingerprinted. See SCHEMA.md for why.
    """
    return (
        event.level,
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
        # `level` is the sentinel for "no log context stored yet": it is the one
        # of the three columns a real producer always fills (log4j events always
        # carry a level; heap-logger hardcodes ERROR), whereas an empty
        # log_message_template or a '[]' cause_chain are ordinary stored values.
        row = conn.execute(
            "SELECT id, level = '' AS needs_context "
            "FROM error_groups WHERE fingerprint = ?",
            (fingerprint,)
        ).fetchone()

        is_new = row is None
        if is_new:
            cur = conn.execute(
                """INSERT INTO error_groups
                   (fingerprint, exception_class, message_template, canonical_frames,
                    canonical_trace, logger, level, log_message_template, cause_chain,
                    first_seen, last_seen, total_count, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 'active')""",
                (fingerprint, event.exception.class_name, normalized_msg,
                 canonical_frames_json, canonical_trace_json,
                 event.logger, *_log_context(event),
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
            if row['needs_context'] and event.level:
                # Backfill a group stored before these columns existed. Without
                # this the groups that most need a cause chain - the long-lived
                # ones - would never get one, since a group row is written once
                # and only updated after that.
                #
                # `event.level` is required so this stays a one-off per group.
                # /ingest is unauthenticated and does not constrain the field, so
                # a payload with an empty level would otherwise leave the group
                # flagged and re-run this on every subsequent occurrence.
                conn.execute(
                    """UPDATE error_groups
                       SET level = ?, log_message_template = ?, cause_chain = ?
                       WHERE id = ?""",
                    (*_log_context(event), group_id)
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
