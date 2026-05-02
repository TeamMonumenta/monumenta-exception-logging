import asyncio
import json
import logging
import os
import shlex
import shutil
import time
from pathlib import Path

import aiohttp
from quart import Quart, request

PORT = int(os.environ.get("HEAPLOG_PORT", "8081"))
HEAPTOOL_PATH = os.environ.get("HEAPLOG_HEAPTOOL_PATH", "/usr/local/bin/heaptool")
HEAPTOOL_EXTRA_ARGS = os.environ.get("HEAPLOG_HEAPTOOL_EXTRA_ARGS", "")
RETENTION_DAYS = int(os.environ.get("HEAPLOG_RETENTION_DAYS", "0"))
HEAPDUMP_DIR = os.environ.get("HEAPLOG_HEAPDUMP_DIR", "/spark")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

app = Quart(__name__)
_queue: asyncio.Queue = asyncio.Queue()
_http_session: aiohttp.ClientSession | None = None


@app.before_serving
async def startup() -> None:
    global _http_session
    _http_session = aiohttp.ClientSession()
    app.add_background_task(_worker)
    log.info("heap-logger started on port %d, heapdump dir: %s", PORT, HEAPDUMP_DIR)


@app.after_serving
async def shutdown() -> None:
    if _http_session:
        await _http_session.close()


@app.post("/ingest")
async def ingest():
    data = await request.get_json(force=True, silent=True)
    if not isinstance(data, dict):
        return {"error": "invalid JSON"}, 400
    missing = [f for f in ("heapdump_path", "exception_logger_url", "server_id") if f not in data]
    if missing:
        return {"error": f"missing fields: {missing}"}, 400
    await _queue.put(data)
    log.info("queued %s from %s (queue depth: %d)", data["heapdump_path"], data["server_id"], _queue.qsize())
    return "", 202


async def _worker() -> None:
    while True:
        job = await _queue.get()
        try:
            await _process(job)
        except Exception:
            log.exception("unhandled error processing %s", job.get("heapdump_path"))
        finally:
            _queue.task_done()


async def _process(job: dict) -> None:
    raw_path: str = job["heapdump_path"]
    exception_logger_url: str = job["exception_logger_url"]
    server_id: str = job["server_id"]

    filename = os.path.basename(raw_path)
    heap_path = os.path.join(HEAPDUMP_DIR, filename)

    if not os.path.isfile(heap_path):
        log.error("heapdump not found: %s", heap_path)
        return

    extra = shlex.split(HEAPTOOL_EXTRA_ARGS) if HEAPTOOL_EXTRA_ARGS.strip() else []
    cmd = [HEAPTOOL_PATH, "--json"] + extra + [heap_path]
    log.info("running: %s", " ".join(cmd))

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_bytes, stderr_bytes = await proc.communicate()

    if stderr_bytes:
        log.info("[heaptool] %s", stderr_bytes.decode(errors="replace").rstrip())

    # Move to processed/ whether or not patterns were found
    processed_dir = os.path.join(HEAPDUMP_DIR, "processed")
    os.makedirs(processed_dir, exist_ok=True)
    dest = os.path.join(processed_dir, filename)
    shutil.move(heap_path, dest)
    log.info("moved %s -> %s", heap_path, dest)

    _sweep_processed(processed_dir)

    raw_stdout = stdout_bytes.decode(errors="replace").strip()
    if not raw_stdout:
        log.info("heaptool produced no output (no patterns above threshold)")
        return

    try:
        patterns = json.loads(raw_stdout)
    except json.JSONDecodeError as e:
        log.error("failed to parse heaptool JSON: %s\noutput was: %.500s", e, raw_stdout)
        return

    if not patterns:
        log.info("no leak patterns found")
        return

    log.info("found %d leak pattern(s), reporting to %s", len(patterns), exception_logger_url)
    timestamp_ms = int(time.time() * 1000)
    for pattern in patterns:
        await _post_exception(exception_logger_url, _build_exception(pattern, server_id, timestamp_ms))


def _sweep_processed(processed_dir: str) -> None:
    if RETENTION_DAYS <= 0:
        return
    cutoff = time.time() - RETENTION_DAYS * 86400
    for p in Path(processed_dir).iterdir():
        if p.is_file() and p.stat().st_mtime < cutoff:
            log.info("deleting expired dump: %s", p)
            p.unlink(missing_ok=True)


def _build_exception(pattern: dict, server_id: str, timestamp_ms: int) -> dict:
    instance_count: int = pattern["instance_count"]
    chain: list[dict] = pattern["chain"]
    first_class: str = chain[0]["class_name"] if chain else "unknown"

    frames = [
        {
            "class_name": entry["class_name"],
            "method": entry["field_name"] if entry["field_name"] else "<ref>",
            "file": None,
            "line": -1,
            "location": None,
        }
        for entry in chain
    ]

    return {
        "schema_version": 1,
        "server_id": server_id,
        "timestamp_ms": timestamp_ms,
        "level": "ERROR",
        "logger": "com.playmonumenta.memoryleak.HeapAnalyzer",
        "thread": "heap-worker",
        "message": "Memory leak detected in heap dump",
        "exception": {
            "class_name": "com.playmonumenta.memoryleak.MemoryLeakException",
            "message": f"Leaked: {first_class} × {instance_count}",
            "frames": frames,
            "cause": None,
        },
    }


async def _post_exception(url: str, payload: dict) -> None:
    assert _http_session is not None
    try:
        async with _http_session.post(url, json=payload) as resp:
            if resp.status not in (200, 201, 202):
                body = await resp.text()
                log.warning("exception-logger returned %d: %.200s", resp.status, body)
    except Exception as e:
        log.error("failed to POST to exception-logger (%s): %s", url, e)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
