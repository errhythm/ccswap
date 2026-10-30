"""Opt-in Claude window warming without copying or spending refresh tokens.

The existing auto loop supplies scheduling and usage polling. A durable claim
is published before launching Claude: an ambiguous outcome must never cause
an immediate retry. Credential/config locks are never held across network I/O.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import tempfile
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from claude_swap import oauth, spawn
from claude_swap.locking import FileLock
from claude_swap.settings import atomic_write_json

if TYPE_CHECKING:
    from claude_swap.models import AccountSnapshot
    from claude_swap.switcher import ClaudeAccountSwitcher

INTERVAL_S = 60.0
RETRY_S = 600.0
TIMEOUT_S = 60.0
WINDOW_S = 5 * 60 * 60
MAX_USAGE_AGE_S = 120.0
STATE_FILENAME = "warmup.json"


def _reset(window: dict) -> float | None:
    value = window.get("resets_at")
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.timestamp() if parsed.tzinfo else None
    except (ValueError, OverflowError, OSError):
        return None


def _pct(window: dict) -> float | None:
    value = window.get("pct")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(value) and 0 <= value <= 100 else None


def eligible(account: AccountSnapshot, now: float) -> bool:
    """Only fresh, readable subscription usage can authorize a real request."""
    entry = account.usage
    if (account.disabled or not account.switchable or account.kind != "oauth"
            or entry.sentinel or entry.last_error or entry.fetched_at is None
            or not 0 <= now - entry.fetched_at <= MAX_USAGE_AGE_S):
        return False
    usage = entry.last_good
    if not isinstance(usage, dict):
        return False
    weekly = usage.get("seven_day")
    if not isinstance(weekly, dict) or _pct(weekly) is None or _pct(weekly) >= 100:
        return False
    five = usage.get("five_hour")
    if five is not None:
        if not isinstance(five, dict) or _pct(five) is None:
            return False
        reset = _reset(five)
        if reset is None:
            if _pct(five) != 0 or five.get("resets_at") is not None:
                return False
        elif reset > entry.fetched_at:
            # Even a zero-percent window may already be running. A reading
            # taken before its reset cannot tell us that it is now idle.
            return False
    scoped_windows = usage.get("scoped", [])
    if not isinstance(scoped_windows, list):
        return False
    for scoped in scoped_windows:
        if not isinstance(scoped, dict):
            return False
        if "haiku" in str(scoped.get("name", "")).casefold():
            if _pct(scoped) is None or _pct(scoped) >= 100:
                return False
    return True


def _identity_key(identity: dict) -> str:
    stable = [identity["uuid"], identity["organizationUuid"]]
    return hashlib.sha256(json.dumps(stable).encode()).hexdigest()


def _read_claims(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    # Corrupt/unreadable guards are an error, never permission to send again.
    if not isinstance(data, dict) or any(
        not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v)
        for v in data.values()
    ):
        raise ValueError("Invalid warmup guard state")
    return data


def _run_prompt(binary: str, token: str, root: Path) -> bool:
    """Run one tool-free prompt in a disposable profile with access-token auth.

    Only OS launch essentials survive. In particular no API key, provider,
    proxy, Node preload, plugin or credential-store override is inherited.
    The refresh token never leaves the existing account store.
    """
    with tempfile.TemporaryDirectory(prefix="warmup-", dir=root, ignore_cleanup_errors=True) as directory:
        home = Path(directory)
        env = {k: v for k, v in os.environ.items() if k.upper() in {
            "PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT",
            "LANG", "LC_ALL", "TERM",
        }}
        env.update({
            "HOME": directory, "USERPROFILE": directory,
            "APPDATA": str(home / "AppData" / "Roaming"),
            "LOCALAPPDATA": str(home / "AppData" / "Local"),
            "TMPDIR": directory, "TMP": directory, "TEMP": directory,
            "CLAUDE_CONFIG_DIR": str(home / ".claude"),
            "CLAUDE_CODE_OAUTH_TOKEN": token,
            "DISABLE_AUTOUPDATER": "1",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        })
        result = spawn.run(
            [binary, "-p", "--model", "haiku", "--max-turns", "1",
             "--tools", "", "--setting-sources", "",
             "--settings", '{"disableAllHooks":true,"disableClaudeAiConnectors":true}',
             "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
             "--disable-slash-commands", "--no-session-persistence",
             "--output-format", "json"],
            input="Reply OK.", env=env, cwd=directory,
            capture_output=True, text=True, timeout=TIMEOUT_S,
        )
        # Never publish raw CLI output: it may contain auth diagnostics.
        try:
            output = json.loads(result.stdout)
        except ValueError:
            return False
        return (result.returncode == 0 and isinstance(output, dict)
                and output.get("type") == "result"
                and output.get("is_error") is False)


class WarmupEngine:
    """Inspect cached usage each minute; back off inconclusive identity probes."""

    def __init__(
        self, switcher: ClaudeAccountSwitcher, clock: Callable[[], float],
        report: Callable[[str], None], enabled: Callable[[], bool] = lambda: True,
    ):
        self.switcher = switcher
        self.clock = clock
        self.report = report
        self.enabled = enabled
        self.next_check = 0.0
        self._retry_after: dict[str, float] = {}

    def tick(self, *, dry_run: bool = False) -> None:
        now = self.clock()
        if now < self.next_check:
            return
        self.next_check = now + INTERVAL_S
        binary = shutil.which("claude")
        if binary is None:
            self.report("Claude Code is not on PATH; warmup skipped")
            return
        snapshot = self.switcher.accounts_snapshot(fetch=set())
        for account in snapshot.accounts:
            if not self.enabled():
                return
            if not eligible(account, self.clock()):
                continue
            try:
                if self._warm(account, binary, dry_run):
                    break  # Bound the auto-loop delay to one prompt per pass.
            except Exception:
                # Neither exception text nor child output is safe to log.
                self.report(f"Account-{account.number}: warmup skipped or failed")
                break

    def _warm(self, account: AccountSnapshot, binary: str, dry_run: bool) -> bool:
        switcher = self.switcher
        identity = switcher.account_identity(account.number)
        if (not identity.get("uuid") or identity.get("email") != account.email
                or (identity.get("organizationUuid") or "") != (account.org_uuid or "")):
            return False
        key = _identity_key(identity)
        if self._retry_after.get(key, 0) > self.clock():
            return False
        path = switcher.backup_dir / STATE_FILENAME
        with FileLock(switcher.lock_file):
            if _read_claims(path).get(key, 0) > self.clock():
                return False
            credentials = switcher.read_account_credentials(account.number, account.email)
        data = oauth.extract_oauth_data(credentials)
        expiry = data.get("expiresAt") if data else None
        if (not isinstance(expiry, (int, float)) or isinstance(expiry, bool)
                or not math.isfinite(expiry)
                or oauth.is_oauth_token_expired(expiry)):
            return False
        token = data.get("accessToken")
        if not isinstance(token, str) or not token:
            return False
        if dry_run:
            self.report(f"Account-{account.number}: would warm five-hour window (Haiku)")
            return False
        # A stale backup access token may fail here, but must never consume
        # its refresh grant to recover. Normal usage polling handles recovery.
        self._retry_after[key] = self.clock() + RETRY_S
        resolved = oauth.fetch_oauth_profile(token)
        if (not resolved or resolved.get("uuid") != identity["uuid"]
                or resolved.get("email") != identity["email"]
                or (resolved.get("organizationUuid") or "") != identity["organizationUuid"]):
            return False
        with FileLock(switcher.lock_file):
            claims = _read_claims(path)
            if (claims.get(key, 0) > self.clock()
                    or switcher.account_identity(account.number) != identity
                    or switcher.read_account_credentials(account.number, account.email) != credentials
                    or not switcher._account_is_switchable(account.number)
                    or switcher._disabled_from_data(switcher._get_sequence_data() or {}, account.number)
                    or not eligible(account, self.clock()) or not self.enabled()):
                return False
            now = self.clock()
            claims = {k: v for k, v in claims.items() if v > now}
            claims[key] = now + WINDOW_S + TIMEOUT_S
            atomic_write_json(path, claims)
        success = _run_prompt(binary, token, switcher.backup_dir)
        self.report(f"Account-{account.number}: " + (
            "warmup request completed (Haiku)" if success
            else "warmup unconfirmed; guard retained for five hours"
        ))
        return True
