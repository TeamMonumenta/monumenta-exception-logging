# Network API

`server/server.py` exposes a set of `/api/*` HTTP routes on the same Quart app and
port as `/ingest` and `/chisel/*`, for querying and mutating exception groups without
going through Discord. A zero-dependency Python CLI, `server/cli/exctl.py` (see
[server/cli/README.md](server/cli/README.md) for usage), wraps this API.

```bash
curl http://localhost:8080/api/health
curl 'http://localhost:8080/api/groups?status=active&sort=recent&limit=5'
curl -X POST http://localhost:8080/api/groups/95daa028/mute \
  -H 'Authorization: Bearer <token from /api-token create>'
```

## Trust model

Plain HTTP, same port as `/ingest`. Read endpoints (every `GET`) are unauthenticated.
See [README.md](README.md#security) for the full threat model shared by `/ingest`,
`/chisel/*`, and `/api/*`.

Mutations (every `POST`) require a bearer token (see "Attribution" below).

**There is no `/api/purge`.** Purge stays Discord-only, via `/purge`.

## Attribution

All mutating (`POST`) endpoints require an `Authorization: Bearer <token>` header.
Missing or malformed → `401 {"error": "missing Authorization: Bearer token"}`;
unknown, expired, or revoked → `401 {"error": "invalid or expired token"}`. The
token's bound `discord_id` is resolved to a display name for attribution: the
guild member's display name (including nickname), falling back to the account's
global display name, and to the raw ID string if Discord can't resolve the user or
is disabled. A fix request DMs the requester when the account is reachable.

The token is minted in Discord, not over the API:

| Command | Description |
|---|---|
| `/api-token create [lifetime_hours]` | Mints a token bound to your Discord ID. Replies ephemerally with the raw token (shown once — it is never recoverable again, only re-mintable) and its expiry. `lifetime_hours` defaults to `API_TOKEN_DEFAULT_TTL_HOURS` and is clamped to `[1, API_TOKEN_MAX_TTL_HOURS]` rather than rejected out of range. Capped at 20 live tokens per user; past that, mint fails until you revoke some. |
| `/api-token revoke` | Deletes every token you've minted. Use it if a token leaks. |

Only the token's SHA-256 hash is ever persisted (the `api_tokens` table). Expired
tokens are swept during the hourly expiry pass.

## JSON conventions

- `<id>` in a path accepts either the 8-character short ID or the full 64-character
  fingerprint, in either case.
- All response timestamps are **epoch seconds as JSON integers**, never ISO-8601
  strings, matching how the database stores them.
- Every response body is a JSON **object**. List endpoints wrap their array in a named
  key (`{groups: [...]}`, not a bare array).
- Errors are `{"error": "..."}` with a non-2xx status, including for an unknown
  `/api/*` path (`404`), a wrong method (`405`), and an unhandled server-side error
  (`500`). Only `/api/*` responds this way; `/ingest` and `/chisel/*` keep Quart's
  default pages.

## Endpoints

| Method | Path | Body / Query | Notes |
|---|---|---|---|
| `GET` | `/api/health` | - | `{"ok": true}` |
| `GET` | `/api/groups` | `status, server, search, new_within_hours, window_hours=24, sort, limit=50 (<=500), offset=0` | `{groups: [...], total: N}`. |
| `GET` | `/api/groups/<id>` | - | Full group details JSON; `404` if unknown. |
| `GET` | `/api/groups/<id>/occurrences` | `limit=20` (<=500) | `{occurrences: [...]}`. Raw, un-normalized messages, newest first. |
| `GET` | `/api/groups/<id>/fix-attempts` | `limit=20` (<=500) | `{fix_attempts: [...]}`, newest first. |
| `GET` | `/api/fix-attempts/<job_id>` | - | Single fix attempt's status; `404` if unknown. |
| `GET` | `/api/servers` | - | `{"servers": [...]}` |
| `POST` | `/api/groups/<id>/mute` | `Authorization: Bearer` | Updated group JSON. |
| `POST` | `/api/groups/<id>/unmute` | `Authorization: Bearer` | Also un-resolves, matching `/unmute`'s behavior. |
| `POST` | `/api/groups/<id>/resolve` | `Authorization: Bearer` | Updated group JSON. |
| `POST` | `/api/groups/<id>/fix` | `Authorization: Bearer` | `{"job_id": "..."}`. `404` unknown group, `401` missing/invalid token, `503` Chisel not configured, `403` token's `discord_id` not in `CHISEL_ALLOWED_USERS` (only when that list is non-empty), `409` a fix attempt is already active for the group. |

A fix requested through `POST /api/groups/<id>/fix` gets the same Discord treatment as
one triggered by the wrench reaction: the working emoji is added to the group's
channel message while it runs, swapped for the outcome emoji on completion, and the
requester is DMed.

### `GET /api/groups` query parameters

| Parameter | Accepted values | Behavior |
|---|---|---|
| `status` | `active`, `muted`, `resolved`, `all` | Anything else is `400`. Omitted or `all` means no status filter. |
| `sort` | `last_seen`, `first_seen`, `total_count`, `recent` | Always descending; anything else is `400`. `recent` orders by the count within `window_hours`, reproducing `/top`. Ties break by insertion order so paging with `offset` is stable. |
| `server` | any server name | Matches if that server has ever contributed an occurrence to the group, regardless of `window_hours`. |
| `search` | any text | Case-insensitive substring over exception class, normalized message, and the full stack trace. `%` and `_` act as SQL `LIKE` wildcards. |
| `new_within_hours` | integer | Only groups first seen within the last N hours. Reproduces `/new`. |
| `window_hours` | integer, default 24 | Window for each group's `recent_count` and per-server breakdown. |
| `limit` / `offset` | integers | Paging. A non-integer value for any integer parameter is `400`. Out-of-range values are clamped, not rejected: `limit` to <=500, `offset` to >=0, and `window_hours`/`new_within_hours` to >=0 and no more than the retention window. |

See also: [README.md](README.md) for the rest of the system, [server/cli/README.md](server/cli/README.md)
for the `exctl` CLI that wraps this API, [PROTOCOL.md](PROTOCOL.md) for the wire
format events arrive in, and [SCHEMA.md](SCHEMA.md) for the underlying tables.
