"""ccswap's CLIProxyAPI-compatible hub, as T3 Code reads it."""
from __future__ import annotations

import http.client
import json
import stat
import sys
import threading
import time
from http.server import ThreadingHTTPServer

import pytest

from claude_swap import hub
from claude_swap.codex_usage import _convert_payload
from claude_swap.models import AccountSnapshot, AccountsSnapshot
from claude_swap.oauth import build_usage_result
from claude_swap.usage_store import STALE_OK_S, UsageEntry

CLAUDE_RAW = {
    "five_hour": {"utilization": 42.0, "resets_at": "2026-11-12T15:00:00+00:00"},
    "seven_day": {"utilization": 10.0, "resets_at": "2026-11-18T00:00:00+00:00"},
    "extra_usage": {"is_enabled": True, "monthly_limit": 5000, "used_credits": 1250, "utilization": 25.0},
    "limits": [{
        "kind": "weekly_scoped", "percent": 7.0, "resets_at": "2026-11-18T00:00:00+00:00",
        "scope": {"model": {"display_name": "Fable"}},
    }],
}
CODEX_RAW = {"rate_limit": {
    "primary_window": {"used_percent": 30, "reset_at": 1794500000, "limit_window_seconds": 18000},
    "secondary_window": {"used_percent": 60, "reset_at": 1795000000, "limit_window_seconds": 604800},
}}


def _acc(number, email, usage, *, kind="oauth", org_uuid="", disabled=False, age_s=0.0, sentinel=None):
    return AccountSnapshot(
        number=number, email=email, org_name="", org_uuid=org_uuid, is_active=False,
        kind=kind, switchable=True, disabled=disabled,
        usage=UsageEntry(last_good=usage, age_s=age_s, sentinel=sentinel),
    )


def _snaps():
    return {
        "claude": AccountsSnapshot("1", (
            _acc("1", "a@x.io", build_usage_result(CLAUDE_RAW), disabled=True),
            _acc("2", "key@x.io", None, kind="api_key"),
            _acc("3", "new@x.io", None),
        ), time.time()),
        "codex": AccountsSnapshot("1", (
            _acc("1", "c@x.io", _convert_payload(CODEX_RAW), org_uuid="ws-1"),
        ), time.time()),
    }


def _call(url, index, method="GET"):
    out = hub.api_call({"auth_index": index, "url": url, "method": method}, _snaps())
    return out["status_code"], json.loads(out["body"])


def test_auth_files_lists_subscription_accounts_and_never_disables():
    files = hub.auth_files(_snaps())["files"]
    assert [(f["provider"], f["auth_index"]) for f in files] == [
        ("claude", "claude-1"), ("claude", "claude-3"), ("codex", "codex-1"),
    ]
    assert not any(f["disabled"] for f in files)
    assert files[2]["id_token"] == {"chatgpt_account_id": "ws-1"}


def test_claude_usage_round_trips_through_ccswaps_parser():
    status, body = _call("https://api.anthropic.com/api/oauth/usage", "claude-1")
    assert status == 200
    assert build_usage_result(body) == build_usage_result(CLAUDE_RAW)
    assert body["extra_usage"]["monthly_limit"] == 5000


def test_codex_usage_round_trips_through_ccswaps_parser():
    status, body = _call("https://chatgpt.com/backend-api/wham/usage", "codex-1")
    assert status == 200
    assert _convert_payload(body) == _convert_payload(CODEX_RAW)


def test_weekly_only_codex_puts_weekly_in_primary():
    usage = _convert_payload({"rate_limit": {"primary_window": CODEX_RAW["rate_limit"]["secondary_window"]}})
    body = hub.codex_body(usage)
    assert body["rate_limit"]["primary_window"]["limit_window_seconds"] == 604800
    assert body["rate_limit"]["secondary_window"] is None


@pytest.mark.parametrize("url,index,method,status", [
    ("https://chatgpt.com/backend-api/wham/rate-limit-reset-credits", "codex-1", "GET", 404),
    ("https://chatgpt.com/backend-api/wham/usage", "codex-1", "POST", 404),
    ("https://evil.example/api/oauth/usage", "claude-1", "GET", 404),
    ("http://api.anthropic.com/api/oauth/usage", "claude-1", "GET", 404),
    ("https://api.anthropic.com/api/oauth/usage", "codex-1", "GET", 404),  # provider mismatch
    ("https://api.anthropic.com/api/oauth/usage", "claude-2", "GET", 404),  # API-key slot
    ("https://api.anthropic.com/api/oauth/usage", "claude-3", "GET", 503),  # never fetched
])
def test_api_call_refuses_everything_but_cached_usage(url, index, method, status):
    assert _call(url, index, method)[0] == status


def test_ensure_key_is_stable_and_private(tmp_path):
    path = tmp_path / "hub-key"
    key = hub.ensure_key(path)
    assert len(key) >= 40 and hub.ensure_key(path) == key
    if sys.platform != "win32":  # Windows has no POSIX modes to check
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.fixture
def server():
    calls = []

    def snapshots():
        calls.append(1)
        return _snaps()

    srv = ThreadingHTTPServer(("127.0.0.1", 0), hub.make_handler("secret", snapshots))
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv, calls
    srv.shutdown()
    srv.server_close()


def _request(srv, method, path, headers=None, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
    conn.request(method, path, body=body, headers=headers or {})
    resp = conn.getresponse()
    data = json.loads(resp.read() or b"{}")
    conn.close()
    return resp.status, data


def test_http_flow_as_t3_drives_it(server):
    srv, calls = server
    auth = {"Authorization": "Bearer secret"}
    status, data = _request(srv, "GET", "/v0/management/auth-files", auth)
    assert status == 200 and len(data["files"]) == 3
    payload = json.dumps({"auth_index": "claude-1", "method": "GET",
                          "url": "https://api.anthropic.com/api/oauth/usage",
                          "header": {"Authorization": "Bearer $TOKEN$"}})
    status, data = _request(srv, "POST", "/v0/management/api-call",
                            {**auth, "Content-Type": "application/json"}, payload)
    assert status == 200 and data["status_code"] == 200
    assert json.loads(data["body"])["five_hour"]["utilization"] == 42.0
    assert len(calls) == 1  # the per-account read reused the listing's pass


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}, {"X-Management-Key": "secre"}])
def test_http_rejects_bad_keys(server, headers):
    srv, calls = server
    assert _request(srv, "GET", "/v0/management/auth-files", headers)[0] == 401
    assert calls == []


def test_http_accepts_x_management_key_and_404s_other_routes(server):
    srv, _ = server
    assert _request(srv, "GET", "/v0/management/auth-files", {"X-Management-Key": "secret"})[0] == 200
    assert _request(srv, "GET", "/v0/management/config", {"X-Management-Key": "secret"})[0] == 404
    assert _request(srv, "POST", "/v0/management/api-call", {"X-Management-Key": "secret"}, b"[]")[0] == 400


def test_half_sent_header_does_not_stall_other_clients(server):
    import socket as _socket
    srv, _ = server
    # A trickling client resets a per-read timeout forever, so only
    # per-connection threads keep the hub answering.
    slow = _socket.create_connection(("127.0.0.1", srv.server_address[1]))
    slow.sendall(b"GET /v0/management/auth-files HTTP/1.1\r\nHost: x\r\n")
    idle = slow
    try:
        assert _request(srv, "GET", "/v0/management/auth-files", {"Authorization": "Bearer secret"})[0] == 200
    finally:
        idle.close()


def test_all_providers_failing_is_an_error_not_an_empty_hub():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), hub.make_handler("secret", lambda: {"claude": None, "codex": None}))
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        assert _request(srv, "GET", "/v0/management/auth-files", {"Authorization": "Bearer secret"})[0] == 502
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.mark.parametrize("usage_kwargs,error", [
    ({"age_s": STALE_OK_S + 60}, "no recent usage reading"),
    ({"sentinel": "token expired"}, "token expired"),
])
def test_untrustworthy_usage_is_unavailable_not_current(usage_kwargs, error):
    acc = _acc("1", "a@x.io", build_usage_result(CLAUDE_RAW), **usage_kwargs)
    out = hub.api_call(
        {"auth_index": "claude-1", "url": "https://api.anthropic.com/api/oauth/usage"},
        {"claude": AccountsSnapshot("1", (acc,), time.time()), "codex": None},
    )
    assert out["status_code"] == 503 and json.loads(out["body"])["error"] == error
