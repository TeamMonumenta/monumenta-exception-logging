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
            cur = conn.execute(
                """INSERT INTO error_groups
                   (fingerprint, exception_class, message_template, canonical_frames,
                    canonical_trace, logger, first_seen, last_seen, total_count, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, 'active')""",
                (fingerprint, event.exception.class_name, normalized_msg,
                 canonical_frames_json, canonical_trace_json,
                 event.logger, timestamp_s, timestamp_s)
            )
            group_id = cur.lastrowid
        else:
            group_id = row['id']
            conn.execute(
                """UPDATE error_groups
                   SET last_seen = ?, total_count = total_count + 1
                   WHERE id = ?""",
                (timestamp_s, group_id)
            )

        conn.execute(
            'INSERT INTO occurrences (group_id, server, timestamp, message) VALUES (?, ?, ?, ?)',
            (group_id, event.server_id, timestamp_s, raw_message)
        )

        conn.execute(
            """INSERT INTO server_hour_counts (group_id, server, hour_bucket, count)
               VALUES (?, ?, ?, 1)
               ON CONFLICT (group_id, server, hour_bucket)
               DO UPDATE SET count = count + 1""",
            (group_id, event.server_id, hour_bucket)
        )

    return fingerprint, is_new
