# heap-logger

Receives heap dump notifications from the Minecraft exception-reporting plugin, analyzes
the dump using the `heaptool` Rust binary, and sends each detected leak pattern to the
exception-logger as a synthetic exception. Deployed as a Kubernetes DaemonSet so each
physical node has a local instance with direct access to its node-local heap dump files.

## How it fits in the pipeline

1. A Minecraft server receives a low-memory event from MonumentaNetworkRelay, or a
   developer runs `/spark heapdump` manually.
2. The exception-logging plugin's always-on log watcher detects spark's completion
   message, extracts the output path, and POSTs a job to this service.
3. heap-logger queues the job. Jobs are processed one at a time (heap dumps are multi-GB
   and running heaptool concurrently would exhaust node memory).
4. `heaptool --json` analyzes the dump and returns a JSON array of leak patterns.
5. Each pattern is formatted as a synthetic exception and POSTed to the exception-logger,
   where it is fingerprinted, grouped, and surfaced in Discord.
6. The processed dump is moved to a `processed/` subdirectory for optional retention.

## Ingest API
```
POST /ingest
Content-Type: application/json
```
```json
{
  "heapdump_path": "plugins/spark/heap-2026-04-30_23.26.24.hprof",
  "exception_logger_url": "http://exception-logger.play.svc.cluster.local/ingest",
  "server_id": "survival-0"
}
```

Returns HTTP 202 immediately. The job is queued for background processing.

`heapdump_path` is the relative path as logged by spark. heap-logger strips the directory
component and resolves the filename against `HEAPLOG_HEAPDUMP_DIR`.

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `HEAPLOG_PORT` | `8081` | HTTP listen port |
| `HEAPLOG_HEAPTOOL_PATH` | `/usr/local/bin/heaptool` | Path to the heaptool binary |
| `HEAPLOG_HEAPTOOL_EXTRA_ARGS` | `` | Extra args passed to heaptool. `--json` is always added implicitly and must not be included here. |
| `HEAPLOG_RETENTION_DAYS` | `0` | Days to retain processed dumps before deletion. `0` disables deletion (keep all). |
| `HEAPLOG_HEAPDUMP_DIR` | `/spark` | Directory where heap dumps are written by spark inside this container. |

## Processed dump lifecycle

After analysis completes (whether or not any patterns were found), the `.hprof` file is
moved to a `processed/` subdirectory under `HEAPLOG_HEAPDUMP_DIR`. If `HEAPLOG_RETENTION_DAYS`
is set to a value greater than 0, any file in `processed/` whose modification time is older
than that many days is deleted at the end of each job.

## Synthetic exception format

Each leak pattern is reported as an exception with these fields:

```
class_name : com.playmonumenta.memoryleak.MemoryLeakException
message    : Leaked: <first class in retention chain> x <instance count>
frames     : one frame per step in the retention chain
             class_name = the class at that step
             method     = the field holding the reference, or "<ref>" if unknown
```

The `message` field is the fingerprint key. The same leak pattern appearing on multiple
servers will be grouped into a single exception group by the exception-logger because
the first class and instance count are deterministic per pattern type.

## Running locally

Build heaptool first (from the monumenta-automation repo):

```bash
cd ~/dev/monumenta/monumenta-automation/rust
cargo build --release --bin heaptool
```

Then install Python dependencies and start the server:

```bash
cd heap-logger
pip install -r requirements.txt
HEAPLOG_HEAPTOOL_PATH=~/dev/monumenta/monumenta-automation/rust/target/release/heaptool \
HEAPLOG_HEAPDUMP_DIR=/path/to/heapdump/dir \
python server.py
```

Send a test job (adjust paths and URLs as needed):

```bash
curl -X POST http://localhost:8081/ingest \
  -H 'Content-Type: application/json' \
  -d '{"heapdump_path":"heap-test.hprof","exception_logger_url":"http://exception-logger.play.svc.cluster.local/ingest","server_id":"test-0"}'
```

## Building

```bash
docker build -t heap-logger .
```

The Dockerfile uses a two-stage build: a Rust stage clones monumenta-automation and
builds heaptool, then a Python stage copies the binary and installs the service.

## Deploying to Kubernetes

heap-logger is deployed as a cluster-wide DaemonSet

Minecraft pods in any namespace reach the local node's instance via the Service, which
uses `internalTrafficPolicy: Local` to route each pod's traffic to the DaemonSet pod on
the same node. Set `HEAPLOG_INGEST_URL` in each Minecraft pod to the full cross-namespace
FQDN:

```
http://heap-logger.<namespace>.svc.cluster.local:8081/ingest
```
