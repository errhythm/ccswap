"""Serve ccswap's usage to T3 Code as a CLIProxyAPI-compatible hub.

T3 Code reads a CLIProxyAPI hub through two management routes: ``GET
auth-files`` lists accounts and ``POST api-call`` relays a provider usage
request with the account's token. This hub answers ``api-call`` from ccswap's
own usage store instead of relaying it, so no request reaches a provider on
T3's behalf and no token leaves ccswap. A caller holding the management key
can read usage percentages and nothing else.
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import logging
import os
import secrets
import socket
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from claude_swap.models import AccountSnapshot, AccountsSnapshot
from claude_swap.paths import get_backup_root

_logger = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8317  # CLIProxyAPI's default, so T3's placeholder URL works.
_PREFIX = "/v0/management/"
_MAX_BODY = 64 * 1024
_CACHE_TTL_S = 30
_WINDOW_SECONDS = {"five_hour": 5 * 60 * 60, "weekly": 7 * 24 * 60 * 60}

# provider -> (host, path suffix) of the one usage URL T3 asks for.
_USAGE_URLS = {
    "claude": ("api.anthropic.com", "/api/oauth/usage"),
    "codex": ("chatgpt.com", "/wham/usage"),
}

Snapshots = Callable[[], dict[str, AccountsSnapshot | None]]


def key_path() -> Path:
    return get_backup_root() / "hub-key"


def ensure_key(path: Path | None = None) -> str:
    """Return the management key, creating it (mode 0600) on first use."""
    path = path or key_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        # O_EXCL: two first runs racing must agree on one key.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        key = path.read_text().strip()
        if not key:
            raise ValueError(f"{path} is empty; delete it to create a new key")
        return key
    key = secrets.token_urlsafe(32)
    with os.fdopen(fd, "w") as f:
        f.write(key + "\n")
    return key


def _account_id(provider: str, acc: AccountSnapshot) -> str:
    return f"{provider}-{acc.number}"


def _hub_accounts(snaps: dict[str, AccountsSnapshot | None]):
    for provider in ("claude", "codex"):
        snap = snaps.get(provider)
        for acc in snap.accounts if snap else ():
            # API-key slots have no subscription usage to show.
            if acc.kind != "api_key":
                yield provider, acc


def auth_files(snaps: dict[str, AccountsSnapshot | None]) -> dict:
    files = []
    for provider, acc in _hub_accounts(snaps):
        ident = _account_id(provider, acc)
        entry = {
            "id": ident,
            "auth_index": ident,
            "name": acc.alias or acc.email,
            "provider": provider,
            "email": acc.email,
            # ccswap's "disabled" only holds a slot out of auto-rotation; T3
            # hides disabled hub accounts entirely, so never report it.
            "disabled": False,
            "status": "active",
        }
        if provider == "codex" and acc.org_uuid:
            entry["id_token"] = {"chatgpt_account_id": acc.org_uuid}
        files.append(entry)
    return {"files": files}


def _claude_window(window: object) -> dict | None:
    if not isinstance(window, dict) or not isinstance(window.get("pct"), (int, float)):
        return None
    return {"utilization": window["pct"], "resets_at": window.get("resets_at")}


def claude_body(usage: dict) -> dict:
    """Rebuild Anthropic's ``/api/oauth/usage`` shape from ccswap's usage dict."""
    body: dict = {
        "five_hour": _claude_window(usage.get("five_hour")),
        "seven_day": _claude_window(usage.get("seven_day")),
    }
    spend = usage.get("spend")
    if isinstance(spend, dict):
        # build_usage_result stored cents as currency units; undo that.
        body["extra_usage"] = {
            "is_enabled": True,
            "monthly_limit": spend["limit"] * 100,
            "used_credits": spend["used"] * 100,
            "utilization": spend["pct"],
            "currency": spend.get("currency", "USD"),
        }
    limits = [
        {
            "kind": "weekly_scoped",
            "percent": w["pct"],
            "resets_at": w.get("resets_at"),
            "scope": {"model": {"display_name": w["name"]}},
        }
        for w in usage.get("scoped") or ()
        if isinstance(w, dict) and w.get("name") and isinstance(w.get("pct"), (int, float))
    ]
    if limits:
        body["limits"] = limits
    return body


def _epoch(iso: object) -> int | None:
    if not isinstance(iso, str):
        return None
    try:
        return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def _codex_window(usage: dict, key: str) -> dict | None:
    window = usage.get(key)
    if not isinstance(window, dict) or not isinstance(window.get("pct"), (int, float)):
        return None
    return {
        "used_percent": window["pct"],
        "reset_at": _epoch(window.get("resets_at")),
        "limit_window_seconds": _WINDOW_SECONDS[key],
    }


def codex_body(usage: dict) -> dict:
    """Rebuild ChatGPT's ``/wham/usage`` shape from ccswap's usage dict."""
    windows = [w for w in (_codex_window(usage, "five_hour"), _codex_window(usage, "weekly")) if w]
    return {
        "rate_limit": {
            "primary_window": windows[0] if windows else None,
            "secondary_window": windows[1] if len(windows) > 1 else None,
        }
    }


def _wanted_provider(url: object) -> str | None:
    if not isinstance(url, str):
        return None
    parts = urlsplit(url)
    for provider, (host, suffix) in _USAGE_URLS.items():
        if parts.scheme == "https" and parts.hostname == host and parts.path.endswith(suffix):
            return provider
    return None


def api_call(request: dict, snaps: dict[str, AccountsSnapshot | None]) -> dict:
    """Answer one relayed request from the usage store, never the network."""

    def reply(status: int, body: dict) -> dict:
        return {"status_code": status, "header": {}, "body": json.dumps(body)}

    index = next(
        (request[k] for k in ("auth_index", "authIndex", "AuthIndex") if isinstance(request.get(k), str)),
        "",
    )
    provider = _wanted_provider(request.get("url"))
    if provider is None or str(request.get("method", "GET")).upper() != "GET":
        return reply(404, {"error": "ccswap hub serves usage only"})
    match = next(
        (acc for p, acc in _hub_accounts(snaps) if p == provider and _account_id(p, acc) == index),
        None,
    )
    if match is None:
        return reply(404, {"error": "unknown account"})
    # Same trust rule as switch decisions: a sentinel (e.g. login needed) or a
    # reading too old to act on is unavailable, not a current percentage.
    usage = match.usage.decision_value()
    if not isinstance(usage, dict):
        return reply(503, {"error": usage or "no recent usage reading"})
    return reply(200, claude_body(usage) if provider == "claude" else codex_body(usage))


def make_handler(key: str, snapshots: Snapshots) -> type[BaseHTTPRequestHandler]:
    expected = key.encode()
    cache: dict[str, AccountsSnapshot | None] = {}
    lock = threading.Lock()  # one snapshot pass at a time, whatever the thread count

    class Handler(BaseHTTPRequestHandler):
        server_version = "ccswap-hub"
        # Each connection has its own thread, so a slow client only holds its
        # own; the timeout reclaims that thread.
        timeout = 10

        def log_message(self, fmt: str, *args) -> None:
            _logger.debug("hub %s " + fmt, self.address_string(), *args)

        def _send(self, status: int, payload: dict) -> None:
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _route(self) -> str | None:
            auth = self.headers.get("Authorization", "")
            given = auth[7:] if auth.startswith("Bearer ") else self.headers.get("X-Management-Key", "")
            if not hmac.compare_digest(given.strip().encode(), expected):
                self._send(401, {"error": "invalid management key"})
                return None
            path = urlsplit(self.path).path
            return path[len(_PREFIX):] if path.startswith(_PREFIX) else ""

        def _snaps(self, fresh: bool) -> dict[str, AccountsSnapshot | None]:
            # T3 lists accounts, then reads each one: refresh on the listing
            # and let the per-account reads reuse that same pass.
            stale = any(s and time.time() - s.taken_at > _CACHE_TTL_S for s in cache.values())
            if fresh or stale or not cache:
                cache.clear()
                cache.update(snapshots())
            return cache

        def do_GET(self) -> None:
            route = self._route()
            if route is None:
                return
            if route != "auth-files":
                return self._send(404, {"error": "not found"})
            try:
                with lock:
                    snaps = self._snaps(fresh=True)
                    body = auth_files(snaps) if any(snaps.values()) else None
                if body is None:
                    return self._send(502, {"error": "ccswap could not read its accounts"})
                self._send(200, body)
            except Exception:
                _logger.exception("hub: account listing failed")
                self._send(502, {"error": "ccswap could not read its accounts"})

        def do_POST(self) -> None:
            route = self._route()
            if route is None:
                return
            if route != "api-call":
                return self._send(404, {"error": "not found"})
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if not 0 < length <= _MAX_BODY:
                return self._send(400 if length <= 0 else 413, {"error": "bad request body"})
            try:
                request = json.loads(self.rfile.read(length))
            except ValueError:
                return self._send(400, {"error": "invalid JSON"})
            if not isinstance(request, dict):
                return self._send(400, {"error": "invalid JSON"})
            try:
                with lock:
                    body = api_call(request, self._snaps(fresh=False))
                self._send(200, body)
            except Exception:
                _logger.exception("hub: api-call failed")
                self._send(502, {"error": "ccswap could not read usage"})

    return Handler


def default_snapshots(debug: bool = False) -> Snapshots:
    from claude_swap.codex import CodexAccountSwitcher
    from claude_swap.switcher import ClaudeAccountSwitcher

    claude = ClaudeAccountSwitcher(debug=debug)
    codex = CodexAccountSwitcher(debug=debug)

    def take() -> dict[str, AccountsSnapshot | None]:
        out: dict[str, AccountsSnapshot | None] = {}
        # fetch=None: the usage store paces the network exactly as `ccswap list`.
        for name, switcher in (("claude", claude), ("codex", codex)):
            try:
                out[name] = switcher.accounts_snapshot(fetch=None)
            except Exception:
                _logger.warning("hub: %s snapshot failed", name, exc_info=True)
                out[name] = None
        return out

    return take


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def serve(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, debug: bool = False) -> None:
    server_cls = ThreadingHTTPServer
    if ":" in host:
        server_cls = type("HTTPServer6", (ThreadingHTTPServer,), {"address_family": socket.AF_INET6})
    server = server_cls((host, port), make_handler(ensure_key(), default_snapshots(debug)))
    try:
        server.serve_forever()
    finally:
        server.server_close()
