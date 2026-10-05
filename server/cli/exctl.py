#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Byron Marohn
"""
exctl - CLI for the Monumenta exception tracker's network API.

Zero-dependency (stdlib only) so it needs no venv. See server/cli/README.md for usage.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Any, Optional, cast

DEFAULT_BASE_URL = "http://localhost:8080"
DEFAULT_TIMEOUT = 30

# Proxies are bypassed: urllib honours $http_proxy/$https_proxy, and
# `proxy_bypass('localhost')` is False on Linux, so a configured proxy would
# otherwise swallow every request to a local server.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

_VALID_STATUSES = ('active', 'muted', 'resolved', 'all')
_VALID_SORTS = ('last_seen', 'first_seen', 'total_count', 'recent')

# CLI command name -> API route segment. `reopen` is a CLI-only alias for `unmute`
# (there is no separate /api/.../reopen route).
_MUTATE_ROUTES = {
    'mute': 'mute',
    'unmute': 'unmute',
    'reopen': 'unmute',
    'resolve': 'resolve',
    'fix': 'fix',
}


class ApiError(Exception):
    """Raised for any HTTP error response, connection failure, or timeout.

    str(err) is the server's JSON error message when there was one, otherwise a
    description of the connection/timeout failure.
    """

    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def request(
    base_url: str, method: str, path: str, timeout: int,
    body: Optional[dict[str, Any]] = None, token: Optional[str] = None,
) -> Any:
    """Make one API call and return its parsed JSON body (or None for a 204/empty
    response). Raises ApiError for any HTTP error status, connection failure, or
    timeout, with the server's JSON error message surfaced verbatim when present.

    `token` is sent as `Authorization: Bearer <token>`, required by every
    mutating endpoint (see NETWORK_API.md's "Attribution" section)."""
    url = f"{base_url.rstrip('/')}{path}"
    data = json.dumps(body).encode('utf-8') if body is not None else None
    headers: dict[str, str] = {'Content-Type': 'application/json'} if data is not None else {}
    if token is not None:
        headers['Authorization'] = f'Bearer {token}'
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        raw = e.read()
        message = f"HTTP {e.code}"
        try:
            payload = json.loads(raw)
            if isinstance(payload, dict):
                error_value = cast(dict[str, Any], payload).get('error')
                if error_value is not None:
                    message = str(error_value)
        except (json.JSONDecodeError, TypeError):
            pass
        raise ApiError(message, status=e.code) from e
    except TimeoutError as e:
        raise ApiError(f"request to {url} timed out after {timeout}s") from e
    except urllib.error.URLError as e:
        raise ApiError(f"could not reach {base_url}: {e.reason}") from e


# ---------------------------------------------------------------------------
# Pure helpers: id/token resolution, query building, formatting
# ---------------------------------------------------------------------------

def resolve_base_url(cli_value: Optional[str], env: Optional[dict[str, str]] = None) -> str:
    """--base-url, else $EXCTL_BASE_URL, else localhost:8080.

    Someone who always talks to the same port-forward shouldn't have to repeat
    the flag on every invocation.
    """
    if cli_value is not None:
        return cli_value
    return (env or {}).get('EXCTL_BASE_URL') or DEFAULT_BASE_URL


def resolve_api_token(env: dict[str, str]) -> Optional[str]:
    """$EXCTL_API_TOKEN, or None if unset. See `/api-token create` in Discord to mint one.

    Deliberately environment-only, with no --token flag: a token is a secret, and a
    CLI flag would land it in shell history and in any other process's view of `ps`.
    """
    return env.get('EXCTL_API_TOKEN')


def quote_id(value: str) -> str:
    """Percent-encode a path segment.

    IDs come off a command line and are pasted from logs and chat, so they can carry
    a stray slash or space. Without quoting those silently rewrite the request path
    into a different (or invalid) endpoint instead of returning a clean 404.
    """
    return urllib.parse.quote(value, safe='')


def resolve_list_query(command: str, args: argparse.Namespace) -> dict[str, Any]:
    """Expand `top`/`new` shorthand into canonical list-query parameters.

    `top` is `list --sort recent`; `new` is `list --sort first_seen --new-within-hours
    24` (both only when the user didn't already pass an explicit value). A pure
    function of (command, parsed args) so alias expansion is testable without a
    live server.
    """
    status = None if args.status in (None, 'all') else args.status
    sort = args.sort
    new_within_hours = args.new_within_hours

    if command == 'top':
        sort = sort or 'recent'
    elif command == 'new':
        sort = sort or 'first_seen'
        if new_within_hours is None:
            new_within_hours = 24

    query: dict[str, Any] = {}
    if status is not None:
        query['status'] = status
    if args.server is not None:
        query['server'] = args.server
    if args.search is not None:
        query['search'] = args.search
    if sort is not None:
        query['sort'] = sort
    if args.window_hours is not None:
        query['window_hours'] = args.window_hours
    if new_within_hours is not None:
        query['new_within_hours'] = new_within_hours
    if args.limit is not None:
        query['limit'] = args.limit
    if args.offset is not None:
        query['offset'] = args.offset
    return query


def fmt_timestamp(epoch: int) -> str:
    """Render an epoch-second int (see NETWORK_API.md) as local time."""
    return datetime.fromtimestamp(epoch).strftime('%Y-%m-%d %H:%M:%S')


def fmt_group_line(g: dict[str, Any]) -> str:
    short_id = g['fingerprint'][:8]
    servers = ','.join(sorted(g['server_counts'].keys())) if g.get('server_counts') else '-'
    return (
        f"{short_id} [{g['status']}] {g['exception_class']} "
        f"(recent: {g['recent_count']}, total: {g['total_count']}) "
        f"servers: {servers}   last seen: {fmt_timestamp(g['last_seen'])}"
    )


def fmt_list_footer(shown: int, total: int, offset: Any = 0) -> str:
    """Summarize a listing page.

    Says which slice of the result set is on screen, not just the total, so it's
    obvious when a listing is truncated and --offset/--limit are worth reaching for.
    """
    if shown == 0:
        return f"No groups shown (total matching: {total})"
    start = int(offset or 0) + 1
    end = start + shown - 1
    if start == 1 and end == total:
        return f"Total: {total}"
    return f"Showing {start}-{end} of {total}"


def fmt_occurrence_line(o: dict[str, Any]) -> str:
    line = f"{fmt_timestamp(o['timestamp'])}  {o['server']}  {o['message']}"
    # Only when it adds something: a wrapper like ServerSchedulerException repeats
    # its exception message verbatim as the log message.
    log_message = o.get('log_message')
    if log_message and log_message != o['message']:
        line += f"  [{log_message}]"
    return line


def fmt_frame_line(frame: dict[str, Any]) -> str:
    file_info = f"{frame['file']}:{frame['line']}" if frame['file'] else "Unknown"
    return f"  at {frame['class_name']}.{frame['method']}({file_info})"


def fmt_details(d: dict[str, Any]) -> str:
    lines = [
        f"Details: {d['fingerprint'][:8]}",
        f"Class: {d['exception_class']}",
        f"Status: {d['status']}",
        f"First seen: {fmt_timestamp(d['first_seen'])}",
        f"Last seen: {fmt_timestamp(d['last_seen'])}",
        f"Total count: {d['total_count']}",
        f"Servers: {', '.join(sorted(d['servers_affected'])) or 'none'}",
        f"Logger: {d['logger']}" + (f" [{d['level']}]" if d.get('level') else ""),
    ]
    if d.get('latest_message'):
        lines.append(f"Latest message: {d['latest_message']}")
    if d.get('latest_log_message'):
        lines.append(f"Logged as: {d['latest_log_message']}")
    if d['status'] == 'muted' and d.get('muted_by'):
        lines.append(f"Muted by {d['muted_by']} on {fmt_timestamp(d['muted_at'])}")
    if d['status'] == 'resolved' and d.get('resolved_by'):
        lines.append(f"Resolved by {d['resolved_by']} on {fmt_timestamp(d['resolved_at'])}")
    lines.append("Stack trace:")
    lines.extend(fmt_frame_line(f) for f in d['canonical_trace'])
    causes: list[dict[str, Any]] = d.get('cause_chain') or []
    for cause in causes:
        detail = f": {cause['message']}" if cause['message'] else ""
        lines.append(f"Caused by: {cause['class_name']}{detail}")
        frames: list[dict[str, Any]] = cause['frames']
        lines.extend(fmt_frame_line(f) for f in frames)
    return "\n".join(lines)


def fmt_fix_attempt(a: dict[str, Any]) -> str:
    lines = [
        f"Job: {a['job_id']}",
        f"Status: {a['status']}",
        f"Queued: {fmt_timestamp(a['queued_at'])}",
    ]
    if a.get('started_at') is not None:
        lines.append(f"Started: {fmt_timestamp(a['started_at'])}")
    if a.get('completed_at') is not None:
        lines.append(f"Completed: {fmt_timestamp(a['completed_at'])}")
    if a.get('message'):
        lines.append(f"Message: {a['message']}")
    if a.get('summary'):
        lines.append(f"Summary: {a['summary']}")
    if a.get('pr_url'):
        lines.append(f"PR: {a['pr_url']}")
    return "\n".join(lines)


_MUTATE_VERBS = {
    'mute': 'Muted',
    'unmute': 'Unmuted',
    'reopen': 'Unmuted',
    'resolve': 'Resolved',
}


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

_JSON_HELP = "print the raw API response as JSON instead of human-readable text"
_ID_HELP = "8-character short ID or full 64-character fingerprint (case-insensitive)"

_LIST_DESCRIPTIONS = {
    'list': "List exception groups, with filters. The unwindowed query Discord has no room for.",
    'top': "Busiest groups in the recent window. Shorthand for `list --sort recent`.",
    'new': ("Groups first seen recently. Shorthand for "
            "`list --sort first_seen --new-within-hours 24`."),
}

_MUTATE_DESCRIPTIONS = {
    'mute': "Mute a group: it keeps collecting occurrences but is hidden from active listings.",
    'unmute': "Return a group to active. Also un-resolves it, matching Discord's /unmute.",
    'reopen': "Alias for `unmute`.",
    'resolve': "Mark a group resolved. It ages out naturally after the retention window.",
    'fix': "Ask Chisel to attempt an automated fix and open a pull request.",
}


def _add_list_args(sp: argparse.ArgumentParser) -> None:
    sp.add_argument('--status', choices=_VALID_STATUSES, default=None,
                    help="only groups with this status ('all' = no filter; default: all)")
    sp.add_argument('--server', default=None,
                    help="only groups this server has ever reported (see `exctl servers`)")
    sp.add_argument('--search', default=None,
                    help="substring match over exception class, message, and stack trace")
    sp.add_argument('--sort', choices=_VALID_SORTS, default=None,
                    help="sort order, always descending (default: last_seen; "
                         "'recent' = count within --window-hours)")
    sp.add_argument('--window-hours', dest='window_hours', type=int, default=None,
                    help="window used for the 'recent' count and per-server breakdown "
                         "(default: 24; capped at the server's retention period)")
    sp.add_argument('--new-within-hours', dest='new_within_hours', type=int, default=None,
                    help="only groups first seen within the last N hours")
    sp.add_argument('--limit', type=int, default=None,
                    help="maximum groups to return (default: 50, server hard cap: 500)")
    sp.add_argument('--offset', type=int, default=None,
                    help="skip the first N matching groups, for paging (default: 0)")
    sp.add_argument('--json', action='store_true', help=_JSON_HELP)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='exctl',
        description="CLI for the Monumenta exception tracker's network API.",
        epilog="Timestamps are shown in local time. See server/cli/README.md for the "
               "port-forward and in-pod workflows.",
    )
    parser.add_argument('--base-url', dest='base_url', default=None, metavar='URL',
                        help=f"server base URL (default: $EXCTL_BASE_URL, else "
                             f"{DEFAULT_BASE_URL})")
    parser.add_argument('--timeout', type=int, default=DEFAULT_TIMEOUT, metavar='SECONDS',
                        help=f"per-request timeout (default: {DEFAULT_TIMEOUT})")

    sub = parser.add_subparsers(dest='command', required=True, metavar='<command>')

    for name in ('list', 'top', 'new'):
        description = _LIST_DESCRIPTIONS[name]
        _add_list_args(sub.add_parser(name, help=description, description=description))

    sp_show = sub.add_parser(
        'show', help="Show one group's full details and stack trace.",
        description="Show one group's full details and stack trace.")
    sp_show.add_argument('id', help=_ID_HELP)
    sp_show.add_argument('--json', action='store_true', help=_JSON_HELP)

    sp_occurrences = sub.add_parser(
        'occurrences', help="Show a group's recent raw (un-normalized) occurrences.",
        description="Show a group's recent raw occurrences: timestamp, server, and the "
                    "original message before fingerprint normalization.")
    sp_occurrences.add_argument('id', help=_ID_HELP)
    sp_occurrences.add_argument('--limit', type=int, default=None,
                                help="maximum occurrences to return (default: 20)")
    sp_occurrences.add_argument('--json', action='store_true', help=_JSON_HELP)

    sp_servers = sub.add_parser(
        'servers', help="List every server that has ever reported an exception.",
        description="List every server that has ever reported an exception.")
    sp_servers.add_argument('--json', action='store_true', help=_JSON_HELP)

    for name in ('mute', 'unmute', 'reopen', 'resolve', 'fix'):
        description = _MUTATE_DESCRIPTIONS[name]
        sp = sub.add_parser(name, help=description, description=description)
        sp.add_argument('id', help=_ID_HELP)
        sp.add_argument('--json', action='store_true', help=_JSON_HELP)

    sp_fix_status = sub.add_parser(
        'fix-status', help="Check one fix attempt by job ID.",
        description="Check one fix attempt by job ID, as returned by `exctl fix`.")
    sp_fix_status.add_argument('job_id', help="job ID printed by `exctl fix`")
    sp_fix_status.add_argument('--json', action='store_true', help=_JSON_HELP)

    sp_fix_history = sub.add_parser(
        'fix-history', help="List every fix attempt for a group, newest first.",
        description="List every fix attempt for a group, newest first.")
    sp_fix_history.add_argument('id', help=_ID_HELP)
    sp_fix_history.add_argument('--limit', type=int, default=None,
                                help="maximum attempts to return (default: 20)")
    sp_fix_history.add_argument('--json', action='store_true', help=_JSON_HELP)

    sp_health = sub.add_parser(
        'health', help="Check that the server is reachable and responding.",
        description="Check that the server is reachable and responding. Useful for "
                    "confirming a port-forward is up before reaching for anything else.")
    sp_health.add_argument('--json', action='store_true', help=_JSON_HELP)

    return parser


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

def _cmd_list(args: argparse.Namespace, base_url: str, timeout: int) -> int:
    query = resolve_list_query(args.command, args)
    qs = urllib.parse.urlencode(query)
    path = f'/api/groups?{qs}' if qs else '/api/groups'
    result = request(base_url, 'GET', path, timeout)
    if args.json:
        print(json.dumps(result))
        return 0
    for g in result['groups']:
        print(fmt_group_line(g))
    print(fmt_list_footer(len(result['groups']), result['total'], query.get('offset', 0)))
    return 0


def _cmd_show(args: argparse.Namespace, base_url: str, timeout: int) -> int:
    result = request(base_url, 'GET', f'/api/groups/{quote_id(args.id)}', timeout)
    if args.json:
        print(json.dumps(result))
        return 0
    print(fmt_details(result))
    return 0


def _cmd_occurrences(args: argparse.Namespace, base_url: str, timeout: int) -> int:
    path = f'/api/groups/{quote_id(args.id)}/occurrences'
    if args.limit is not None:
        path += f'?{urllib.parse.urlencode({"limit": args.limit})}'
    result = request(base_url, 'GET', path, timeout)
    if args.json:
        print(json.dumps(result))
        return 0
    occurrences = result['occurrences']
    if not occurrences:
        print("No occurrences retained.")
        return 0
    for o in occurrences:
        print(fmt_occurrence_line(o))
    return 0


def _cmd_servers(args: argparse.Namespace, base_url: str, timeout: int) -> int:
    result = request(base_url, 'GET', '/api/servers', timeout)
    if args.json:
        print(json.dumps(result))
        return 0
    for s in result['servers']:
        print(s)
    return 0


def _cmd_health(args: argparse.Namespace, base_url: str, timeout: int) -> int:
    result = request(base_url, 'GET', '/api/health', timeout)
    if args.json:
        print(json.dumps(result))
        return 0
    print(f"OK: {base_url} is responding")
    return 0


def _cmd_mutate(args: argparse.Namespace, base_url: str, timeout: int) -> int:
    token = resolve_api_token(dict(os.environ))
    if not token:
        print(
            "error: $EXCTL_API_TOKEN is required for this command. "
            "Run /api-token create in Discord to mint one.",
            file=sys.stderr,
        )
        return 1
    route = _MUTATE_ROUTES[args.command]
    result = request(
        base_url, 'POST', f'/api/groups/{quote_id(args.id)}/{route}', timeout, token=token,
    )
    if args.json:
        print(json.dumps(result))
        return 0
    if args.command == 'fix':
        print(f"Fix requested for {args.id}: job {result['job_id']}")
    else:
        print(f"{_MUTATE_VERBS[args.command]} {result['fingerprint'][:8]} "
              f"(status: {result['status']})")
    return 0


def _cmd_fix_status(args: argparse.Namespace, base_url: str, timeout: int) -> int:
    result = request(base_url, 'GET', f'/api/fix-attempts/{quote_id(args.job_id)}', timeout)
    if args.json:
        print(json.dumps(result))
        return 0
    print(fmt_fix_attempt(result))
    return 0


def _cmd_fix_history(args: argparse.Namespace, base_url: str, timeout: int) -> int:
    path = f'/api/groups/{quote_id(args.id)}/fix-attempts'
    if args.limit is not None:
        path += f'?{urllib.parse.urlencode({"limit": args.limit})}'
    result = request(base_url, 'GET', path, timeout)
    if args.json:
        print(json.dumps(result))
        return 0
    attempts = result['fix_attempts']
    if not attempts:
        print("No fix attempts.")
        return 0
    print("\n\n".join(fmt_fix_attempt(a) for a in attempts))
    return 0


_HANDLERS = {
    'list': _cmd_list, 'top': _cmd_list, 'new': _cmd_list,
    'show': _cmd_show,
    'occurrences': _cmd_occurrences,
    'servers': _cmd_servers,
    'health': _cmd_health,
    'mute': _cmd_mutate, 'unmute': _cmd_mutate, 'reopen': _cmd_mutate,
    'resolve': _cmd_mutate, 'fix': _cmd_mutate,
    'fix-status': _cmd_fix_status,
    'fix-history': _cmd_fix_history,
}


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    base_url = resolve_base_url(args.base_url, dict(os.environ))

    try:
        return _HANDLERS[args.command](args, base_url, args.timeout)
    except ApiError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except BrokenPipeError:
        # `exctl list | head` closes the pipe early. Without this, Python reports
        # the failure at interpreter shutdown as an ugly "Exception ignored" trace.
        _silence_broken_pipe_at_exit()
        return 0
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


def _silence_broken_pipe_at_exit() -> None:
    """Redirect stdout to devnull so the interpreter's final flush can't fail."""
    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, sys.stdout.fileno())


if __name__ == '__main__':
    sys.exit(main())
