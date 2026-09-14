# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Byron Marohn
"""
Tests for server/cli/exctl.py (see server/cli/README.md).

Exercises the CLI's pure functions directly: argument parsing, the top/new alias
expansion, output formatting, epoch-int timestamp rendering, and --base-url/
$EXCTL_API_TOKEN env-var fallback resolution. Not a full subprocess/live-HTTP
integration suite (see
server/tests/test_api_http.py for the HTTP layer this CLI talks to) - kept fast and
dependency-free, like the script itself.
"""

import sys
import os
import io
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest
from cli import exctl

# Module-private in the CLI, but part of what these tests drive: the shared opener
# (patched to simulate HTTP failures) and the command dispatch table.
OPENER = exctl._OPENER      # pylint: disable=protected-access
HANDLERS = exctl._HANDLERS  # pylint: disable=protected-access


# ===========================================================================
# --base-url resolution / $EXCTL_API_TOKEN resolution
# ===========================================================================

def test_resolve_base_url_explicit():
    assert exctl.resolve_base_url("http://example.com:9000") == "http://example.com:9000"


def test_resolve_base_url_default():
    assert exctl.resolve_base_url(None) == exctl.DEFAULT_BASE_URL
    assert exctl.DEFAULT_BASE_URL == "http://localhost:8080"


def test_resolve_base_url_falls_back_to_env():
    env = {'EXCTL_BASE_URL': 'http://example.com:9000'}
    assert exctl.resolve_base_url(None, env) == 'http://example.com:9000'
    assert exctl.resolve_base_url('http://cli-wins:1', env) == 'http://cli-wins:1'


def test_resolve_api_token_from_env():
    env = {'EXCTL_API_TOKEN': 'env-token'}
    assert exctl.resolve_api_token(env) == 'env-token'


def test_resolve_api_token_none_when_unset():
    assert exctl.resolve_api_token({}) is None


def test_parser_has_no_token_flag():
    """A token is a secret: it must only be settable via $EXCTL_API_TOKEN, never
    as a CLI argument (which would land it in shell history and in `ps` output)."""
    parser = exctl.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(['--token', 'sometoken', 'list'])


# ===========================================================================
# argument parsing
# ===========================================================================

def test_parser_requires_a_command():
    parser = exctl.build_parser()
    try:
        parser.parse_args([])
        assert False, "expected SystemExit"
    except SystemExit:
        pass


def test_parser_list_defaults():
    parser = exctl.build_parser()
    args = parser.parse_args(['list'])
    assert args.command == 'list'
    assert args.status is None
    assert args.sort is None
    assert args.json is False


def test_parser_list_all_flags():
    parser = exctl.build_parser()
    args = parser.parse_args([
        'list', '--status', 'muted', '--server', 'build', '--search', 'NPE',
        '--sort', 'total_count', '--window-hours', '48', '--new-within-hours', '12',
        '--limit', '10', '--offset', '5', '--json',
    ])
    assert args.status == 'muted'
    assert args.server == 'build'
    assert args.search == 'NPE'
    assert args.sort == 'total_count'
    assert args.window_hours == 48
    assert args.new_within_hours == 12
    assert args.limit == 10
    assert args.offset == 5
    assert args.json is True


def test_parser_rejects_invalid_status_choice():
    parser = exctl.build_parser()
    try:
        parser.parse_args(['list', '--status', 'bogus'])
        assert False, "expected SystemExit"
    except SystemExit:
        pass


def test_parser_show_requires_id():
    parser = exctl.build_parser()
    try:
        parser.parse_args(['show'])
        assert False, "expected SystemExit"
    except SystemExit:
        pass
    args = parser.parse_args(['show', 'deadbeef'])
    assert args.id == 'deadbeef'


def test_parser_mutate_commands_take_id():
    parser = exctl.build_parser()
    for cmd in ('mute', 'unmute', 'reopen', 'resolve', 'fix'):
        args = parser.parse_args([cmd, 'deadbeef'])
        assert args.command == cmd
        assert args.id == 'deadbeef'


def test_parser_fix_status_takes_job_id():
    parser = exctl.build_parser()
    args = parser.parse_args(['fix-status', 'some-job-id'])
    assert args.job_id == 'some-job-id'


def test_parser_global_flags():
    parser = exctl.build_parser()
    args = parser.parse_args([
        '--base-url', 'http://example.com', '--timeout', '5', 'servers',
    ])
    assert args.base_url == 'http://example.com'
    assert args.timeout == 5


def test_parser_timeout_default():
    parser = exctl.build_parser()
    args = parser.parse_args(['servers'])
    assert args.timeout == exctl.DEFAULT_TIMEOUT
    assert exctl.DEFAULT_TIMEOUT == 30


# ===========================================================================
# resolve_list_query - top/new alias expansion (§11)
# ===========================================================================

def _list_args(command: str, **overrides):
    parser = exctl.build_parser()
    argv = [command]
    args = parser.parse_args(argv)
    for key, value in overrides.items():
        setattr(args, key, value)
    return args


def test_list_query_plain_list_has_no_implicit_sort():
    args = _list_args('list')
    query = exctl.resolve_list_query('list', args)
    assert 'sort' not in query
    assert 'new_within_hours' not in query


def test_top_is_list_sort_recent():
    args = _list_args('top')
    query = exctl.resolve_list_query('top', args)
    assert query['sort'] == 'recent'
    assert 'new_within_hours' not in query


def test_new_is_list_sort_first_seen_new_within_24():
    args = _list_args('new')
    query = exctl.resolve_list_query('new', args)
    assert query['sort'] == 'first_seen'
    assert query['new_within_hours'] == 24


def test_new_respects_explicit_new_within_hours_override():
    args = _list_args('new', new_within_hours=6)
    query = exctl.resolve_list_query('new', args)
    assert query['new_within_hours'] == 6


def test_top_respects_explicit_sort_override():
    args = _list_args('top', sort='last_seen')
    query = exctl.resolve_list_query('top', args)
    assert query['sort'] == 'last_seen'


def test_list_query_status_all_means_no_filter():
    args = _list_args('list', status='all')
    query = exctl.resolve_list_query('list', args)
    assert 'status' not in query


def test_list_query_includes_only_set_filters():
    args = _list_args('list', server='build', limit=25)
    query = exctl.resolve_list_query('list', args)
    assert query == {'server': 'build', 'limit': 25}


# ===========================================================================
# output formatting
# ===========================================================================

def test_fmt_timestamp_is_local_time_string():
    import time
    epoch = 1700000000
    expected = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(epoch))
    assert exctl.fmt_timestamp(epoch) == expected


def test_fmt_group_line_contains_short_id_and_class():
    g = {
        'fingerprint': 'abcd1234' + '0' * 56,
        'status': 'active',
        'exception_class': 'java.lang.Exception',
        'recent_count': 3,
        'total_count': 10,
        'server_counts': {'build': 3},
        'last_seen': 1700000000,
    }
    line = exctl.fmt_group_line(g)
    assert line.startswith('abcd1234')
    assert '[active]' in line
    assert 'java.lang.Exception' in line
    assert 'recent: 3' in line
    assert 'total: 10' in line
    assert 'build' in line


def test_fmt_group_line_no_servers():
    g = {
        'fingerprint': 'a' * 64, 'status': 'active', 'exception_class': 'X',
        'recent_count': 0, 'total_count': 0, 'server_counts': {}, 'last_seen': 0,
    }
    assert 'servers: -' in exctl.fmt_group_line(g)


def test_fmt_frame_line_with_file():
    frame = {'class_name': 'com.example.Foo', 'method': 'bar', 'file': 'Foo.java', 'line': 42}
    assert exctl.fmt_frame_line(frame) == "  at com.example.Foo.bar(Foo.java:42)"


def test_fmt_frame_line_without_file():
    frame = {'class_name': 'com.example.Foo', 'method': 'bar', 'file': None, 'line': -1}
    assert exctl.fmt_frame_line(frame) == "  at com.example.Foo.bar(Unknown)"


def _details_stub(**overrides):
    base = {
        'fingerprint': 'abcd1234' + '0' * 56,
        'exception_class': 'java.lang.Exception',
        'status': 'active',
        'first_seen': 1700000000,
        'last_seen': 1700000100,
        'total_count': 5,
        'servers_affected': ['build', 'survival-0'],
        'logger': 'com.example.Foo',
        'latest_message': None,
        'muted_by': None,
        'muted_at': None,
        'resolved_by': None,
        'resolved_at': None,
        'canonical_trace': [],
    }
    base.update(overrides)
    return base


def test_fmt_details_basic_fields():
    d = _details_stub()
    text = exctl.fmt_details(d)
    assert "Details: abcd1234" in text
    assert "Class: java.lang.Exception" in text
    assert "Status: active" in text
    assert "Servers: build, survival-0" in text


def test_fmt_details_no_muted_line_when_active():
    """Mirrors the server-side _fmt_details_lines status guard: stale muted_by on an
    active group must not be displayed."""
    d = _details_stub(status='active', muted_by='Alice', muted_at=1700000000)
    text = exctl.fmt_details(d)
    assert 'Muted by' not in text


def test_fmt_details_shows_muted_line_when_muted():
    d = _details_stub(status='muted', muted_by='Alice', muted_at=1700000000)
    text = exctl.fmt_details(d)
    assert 'Muted by Alice' in text


def test_fmt_details_no_resolved_line_when_muted():
    d = _details_stub(status='muted', resolved_by='Bob', resolved_at=1700000000)
    text = exctl.fmt_details(d)
    assert 'Resolved by' not in text


def test_fmt_details_includes_stack_trace():
    d = _details_stub(canonical_trace=[
        {'class_name': 'com.example.Foo', 'method': 'bar', 'file': 'Foo.java', 'line': 1},
    ])
    text = exctl.fmt_details(d)
    assert "at com.example.Foo.bar(Foo.java:1)" in text


def test_fmt_fix_attempt_pending():
    a = {
        'job_id': 'j1', 'status': 'pending',
        'queued_at': 1700000000, 'started_at': None, 'completed_at': None,
        'message': None, 'summary': None, 'pr_url': None,
    }
    text = exctl.fmt_fix_attempt(a)
    assert "Job: j1" in text
    assert "Status: pending" in text
    assert "Started:" not in text
    assert "PR:" not in text


def test_fmt_fix_attempt_completed():
    a = {
        'job_id': 'j1', 'status': 'success',
        'queued_at': 1700000000, 'started_at': 1700000010, 'completed_at': 1700000020,
        'message': 'Fixed!', 'summary': 'Added a null check',
        'pr_url': 'https://github.com/example/repo/pull/1',
    }
    text = exctl.fmt_fix_attempt(a)
    assert "Started:" in text
    assert "Completed:" in text
    assert "Message: Fixed!" in text
    assert "PR: https://github.com/example/repo/pull/1" in text


# ===========================================================================
# fmt_list_footer / quote_id
# ===========================================================================

def test_fmt_list_footer_full_result_set():
    assert exctl.fmt_list_footer(3, 3, 0) == "Total: 3"


def test_fmt_list_footer_shows_the_slice_when_truncated():
    """A page that isn't the whole result set must say so, or a truncated listing
    looks like the complete answer."""
    assert exctl.fmt_list_footer(2, 7, 0) == "Showing 1-2 of 7"
    assert exctl.fmt_list_footer(2, 7, 2) == "Showing 3-4 of 7"


def test_fmt_list_footer_empty_page():
    assert "No groups shown" in exctl.fmt_list_footer(0, 0, 0)


def test_quote_id_escapes_path_separators():
    """An unescaped '/' would rewrite the request path into a different endpoint."""
    assert exctl.quote_id("a/b") == "a%2Fb"
    assert exctl.quote_id("a b") == "a%20b"
    assert exctl.quote_id("deadbeef") == "deadbeef"


# ===========================================================================
# HTTP error mapping — what the user actually sees when something is wrong
# ===========================================================================

def _http_error(code: int, body: bytes) -> urllib.error.HTTPError:
    """A real HTTPError carrying a response body, as urlopen would raise."""
    return urllib.error.HTTPError(
        "http://x/api/groups", code, "err", {}, io.BytesIO(body)  # type: ignore[arg-type]
    )


def _request_raising(exc):
    def _open(_req, timeout=None):  # pylint: disable=unused-argument
        raise exc
    return _open


def test_request_surfaces_server_error_message(monkeypatch):
    err = _http_error(409, b'{"error": "a fix attempt is already active for this group"}')
    monkeypatch.setattr(OPENER, 'open', _request_raising(err))
    with pytest.raises(exctl.ApiError) as excinfo:
        exctl.request("http://x", "POST", "/api/groups/abc/fix", 5)
    assert str(excinfo.value) == "a fix attempt is already active for this group"
    assert excinfo.value.status == 409


def test_request_falls_back_to_status_when_body_is_not_json(monkeypatch):
    err = _http_error(500, b'<!doctype html><title>500</title>')
    monkeypatch.setattr(OPENER, 'open', _request_raising(err))
    with pytest.raises(exctl.ApiError) as excinfo:
        exctl.request("http://x", "GET", "/api/groups", 5)
    assert str(excinfo.value) == "HTTP 500"
    assert excinfo.value.status == 500


def test_request_reports_connection_failure(monkeypatch):
    monkeypatch.setattr(
        OPENER, 'open',
        _request_raising(urllib.error.URLError(ConnectionRefusedError(111, "Connection refused")))
    )
    with pytest.raises(exctl.ApiError) as excinfo:
        exctl.request("http://localhost:8080", "GET", "/api/groups", 5)
    assert "could not reach http://localhost:8080" in str(excinfo.value)
    assert excinfo.value.status is None


def test_request_reports_timeout(monkeypatch):
    monkeypatch.setattr(OPENER, 'open', _request_raising(TimeoutError()))
    with pytest.raises(exctl.ApiError) as excinfo:
        exctl.request("http://x", "GET", "/api/groups", 7)
    assert "timed out after 7s" in str(excinfo.value)


def test_request_adds_bearer_header_when_token_given(monkeypatch):
    captured_requests: list[urllib.request.Request] = []

    class _FakeResponse(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def _open(req, timeout=None):  # pylint: disable=unused-argument
        captured_requests.append(req)
        return _FakeResponse(b'{}')

    monkeypatch.setattr(OPENER, 'open', _open)
    exctl.request("http://x", "POST", "/api/groups/abc/mute", 5, token="my-token")
    assert captured_requests[0].get_header('Authorization') == 'Bearer my-token'


def test_request_omits_authorization_header_without_a_token(monkeypatch):
    captured_requests: list[urllib.request.Request] = []

    class _FakeResponse(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def _open(req, timeout=None):  # pylint: disable=unused-argument
        captured_requests.append(req)
        return _FakeResponse(b'{}')

    monkeypatch.setattr(OPENER, 'open', _open)
    exctl.request("http://x", "GET", "/api/groups", 5)
    assert captured_requests[0].get_header('Authorization') is None


def test_request_bypasses_proxies():
    """$http_proxy must not capture requests to a port-forwarded localhost.

    urllib reads the proxy env vars by default and does NOT bypass localhost on Linux,
    so a developer machine with a proxy configured would send every request to it and
    report the server as unreachable. The opener is built with an empty ProxyHandler;
    urllib drops a proxy handler that has no proxies, so the check is that no handler
    carrying proxies survived.
    """
    proxied = [
        h for h in OPENER.handlers
        if isinstance(h, urllib.request.ProxyHandler) and h.proxies
    ]
    assert not proxied, f"opener would route through proxies: {proxied}"


# ===========================================================================
# Response envelope parsing — these break loudly if the API's shape changes
# ===========================================================================

def _capture_request(response):
    calls: list[tuple[str, str, str]] = []

    def _fake(base_url, method, path, timeout, body=None):  # pylint: disable=unused-argument
        calls.append((base_url, method, path))
        return response

    return _fake, calls


def test_list_reads_groups_and_total(monkeypatch, capsys):
    response = {
        'groups': [{
            'fingerprint': 'abcd1234' + '0' * 56, 'exception_class': 'java.lang.Exception',
            'status': 'active', 'total_count': 5, 'recent_count': 2,
            'server_counts': {'srv-1': 2}, 'last_seen': 1700000000,
        }],
        'total': 1,
    }
    fake, _calls = _capture_request(response)
    monkeypatch.setattr(exctl, 'request', fake)
    args = exctl.build_parser().parse_args(['list'])
    assert HANDLERS['list'](args, "http://x", 30) == 0
    out = capsys.readouterr().out
    assert 'abcd1234' in out
    assert 'Total: 1' in out


def test_occurrences_reads_the_envelope(monkeypatch, capsys):
    response = {'occurrences': [
        {'timestamp': 1700000000, 'server': 'srv-1', 'message': 'boom'},
    ]}
    fake, calls = _capture_request(response)
    monkeypatch.setattr(exctl, 'request', fake)
    args = exctl.build_parser().parse_args(['occurrences', 'deadbeef'])
    assert HANDLERS['occurrences'](args, "http://x", 30) == 0
    assert calls[0][2] == '/api/groups/deadbeef/occurrences'
    out = capsys.readouterr().out
    assert 'srv-1' in out and 'boom' in out


def test_occurrences_passes_limit(monkeypatch, capsys):
    fake, calls = _capture_request({'occurrences': []})
    monkeypatch.setattr(exctl, 'request', fake)
    args = exctl.build_parser().parse_args(['occurrences', 'deadbeef', '--limit', '5'])
    assert HANDLERS['occurrences'](args, "http://x", 30) == 0
    assert calls[0][2] == '/api/groups/deadbeef/occurrences?limit=5'
    assert 'No occurrences retained.' in capsys.readouterr().out


def test_fix_history_reads_the_envelope(monkeypatch, capsys):
    response = {'fix_attempts': [{
        'job_id': 'j1', 'status': 'success',
        'queued_at': 1700000000, 'started_at': None, 'completed_at': None,
        'message': 'done', 'summary': None, 'pr_url': None,
    }]}
    fake, _calls = _capture_request(response)
    monkeypatch.setattr(exctl, 'request', fake)
    args = exctl.build_parser().parse_args(['fix-history', 'deadbeef'])
    assert HANDLERS['fix-history'](args, "http://x", 30) == 0
    assert 'Job: j1' in capsys.readouterr().out


def test_mutate_requires_a_token(monkeypatch, capsys):
    monkeypatch.delenv('EXCTL_API_TOKEN', raising=False)
    args = exctl.build_parser().parse_args(['mute', 'deadbeef'])
    assert HANDLERS['mute'](args, "http://x", 30) == 1
    assert '$EXCTL_API_TOKEN' in capsys.readouterr().err


def test_mutate_sends_the_token_as_a_bearer_header_and_quotes_the_path(monkeypatch, capsys):
    captured: dict[str, object] = {}

    def _fake(base_url, method, path, timeout, body=None, token=None):
        # pylint: disable=unused-argument
        captured['path'] = path
        captured['body'] = body
        captured['token'] = token
        return {'fingerprint': 'abcd1234' + '0' * 56, 'status': 'muted'}

    monkeypatch.setattr(exctl, 'request', _fake)
    monkeypatch.setenv('EXCTL_API_TOKEN', 'sometoken')
    args = exctl.build_parser().parse_args(['mute', 'dead/beef'])
    assert HANDLERS['mute'](args, "http://x", 30) == 0
    assert captured['path'] == '/api/groups/dead%2Fbeef/mute'
    assert captured['body'] is None
    assert captured['token'] == 'sometoken'
    assert 'Muted abcd1234' in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Log-event context rendering
#
# _details_stub deliberately omits the newer keys, so these also pin that
# fmt_details stays tolerant of a server older than the CLI.
# ---------------------------------------------------------------------------

def test_fmt_details_without_log_context():
    out = exctl.fmt_details(_details_stub())
    assert 'Logger: com.example.Foo' in out
    assert 'Caused by:' not in out
    assert 'Logged as:' not in out


def test_fmt_details_renders_level_beside_logger():
    out = exctl.fmt_details(_details_stub(level='WARN'))
    assert 'Logger: com.example.Foo [WARN]' in out


def test_fmt_details_renders_log_message():
    out = exctl.fmt_details(_details_stub(
        latest_log_message='Task #42 for Monumenta generated an exception'))
    assert 'Logged as: Task #42 for Monumenta generated an exception' in out


def test_fmt_details_renders_cause_chain_after_the_trace():
    out = exctl.fmt_details(_details_stub(cause_chain=[
        {'class_name': 'java.lang.IllegalArgumentException', 'message': 'World unloaded',
         'frames': [{'class_name': 'com.playmonumenta.plugins.Depths', 'method': 'run',
                     'file': 'Depths.java', 'line': 258}]},
        {'class_name': 'java.lang.NullPointerException', 'message': '', 'frames': []},
    ]))
    lines = out.splitlines()
    assert lines.index('Stack trace:') < lines.index(
        'Caused by: java.lang.IllegalArgumentException: World unloaded')
    assert '  at com.playmonumenta.plugins.Depths.run(Depths.java:258)' in lines
    # A cause with no message renders without a trailing colon.
    assert 'Caused by: java.lang.NullPointerException' in lines


def test_fmt_occurrence_line_shows_log_message_only_when_it_differs():
    base = {'timestamp': 1700000000, 'server': 'valley', 'message': 'boom'}
    assert exctl.fmt_occurrence_line(base).endswith('boom')
    assert exctl.fmt_occurrence_line({**base, 'log_message': ''}).endswith('boom')
    # A wrapper exception's log message and exception message are the same string;
    # repeating it would just be noise.
    assert exctl.fmt_occurrence_line({**base, 'log_message': 'boom'}).endswith('boom')
    assert exctl.fmt_occurrence_line(
        {**base, 'log_message': 'Task #42 failed'}).endswith('boom  [Task #42 failed]')
