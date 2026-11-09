"""Sanitized Codex quota snapshots for consumers that only read files."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from claude_swap.codex_usage import _iso_timestamp, _number
from claude_swap.exceptions import ConfigError, LockError
from claude_swap.fsutil import atomic_write_text
from claude_swap.locking import FileLock

if TYPE_CHECKING:
    from claude_swap.codex import CodexAccountSwitcher
    from claude_swap.usage_store import UsageEntry


_ERROR_CLASSES = frozenset({
    "CodexUsageError", "ClaudeSwitchError", "ConfigError", "SwitchError", "SessionError",
    "LockError", "AccountNotFoundError", "CredentialError", "CredentialReadError",
    "CredentialWriteError", "OSError", "FileNotFoundError", "PermissionError", "TimeoutError",
    "ConnectionError", "ConnectionAbortedError", "ConnectionRefusedError", "ConnectionResetError",
    "BrokenPipeError", "HTTPException", "NotConnected", "InvalidURL", "UnknownProtocol",
    "UnknownTransferEncoding", "UnimplementedFileMode", "IncompleteRead", "ImproperConnectionState",
    "CannotSendRequest", "CannotSendHeader", "ResponseNotReady", "BadStatusLine", "LineTooLong",
    "RemoteDisconnected",
})


def _error_class(value: object) -> str:
    return value if isinstance(value, str) and value in _ERROR_CLASSES else "Exception"


def _read_snapshot(path: Path) -> dict[str, Any]:
    """Treat malformed optional cache files as missing, including parser limits."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError, RecursionError):
        return {}
    return data if isinstance(data, dict) else {}


def _boolean(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _label(value: object) -> str:
    return value if isinstance(value, str) else ""


def _integer(value: object) -> int | None:
    number = _number(value)
    return int(number) if number is not None and number >= 0 and number.is_integer() else None


def _fields(value: object, fields: dict[str, Any]) -> dict[str, Any] | None:
    """Copy only named, typed values, including when retaining an old file."""
    if not isinstance(value, dict):
        return None
    result = {key: convert(value.get(key)) for key, convert in fields.items()}
    return {key: value for key, value in result.items() if value is not None} or None


def _measurement(record: dict[str, Any]) -> dict[str, Any]:
    windows = record.get("windows")
    windows = windows if isinstance(windows, dict) else {}
    return {
        "fetchedAt": _number(record.get("fetchedAt")),
        "windows": {
            name: window for name in ("weekly", "fiveHour")
            if (window := _fields(windows.get(name), {
                "usedPercent": _number, "windowMinutes": _integer,
                "resetsAt": _iso_timestamp,
            })) is not None and "usedPercent" in window
        },
        "credits": _fields(record.get("credits"), {
            "hasCredits": _boolean, "unlimited": _boolean, "balance": _number,
        }),
        "allowance": _fields(record.get("allowance"), {
            "remaining": _number, "limit": _number, "resetsAt": _iso_timestamp,
        }),
        "bankedResets": _fields(record.get("bankedResets"), {
            "count": _integer, "earliestExpiry": _iso_timestamp,
        }),
    }


def _renamed(value: object, fields: dict[str, str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {target: value.get(source) for source, target in fields.items()}


def _snapshot(entry: UsageEntry) -> dict[str, Any]:
    usage = entry.last_good or {}
    windows = {}
    # Use the client's duration classification, including its legacy fallback
    # for responses without durations; primary/secondary position is not a label.
    for source, target, minutes in (("weekly", "weekly", 10080), ("five_hour", "fiveHour", 300)):
        if isinstance(usage.get(source), dict):
            windows[target] = dict(_renamed(usage[source], {
                "pct": "usedPercent", "resets_at": "resetsAt",
            }), windowMinutes=minutes)
    return _measurement({
        "fetchedAt": entry.fetched_at,
        "windows": windows,
        "credits": _renamed(usage.get("credits"), {
            "has_credits": "hasCredits", "unlimited": "unlimited", "balance": "balance",
        }),
        "allowance": _renamed(usage.get("credit_allowance"), {
            "remaining": "remaining", "limit": "limit", "resets_at": "resetsAt",
        }),
        "bankedResets": _renamed(usage.get("reset_credits"), {
            "available": "count", "expires_at": "earliestExpiry",
        }),
    })


def write_usage(
    switcher: CodexAccountSwitcher, number: str, account: dict[str, Any] | None,
    entry: UsageEntry, attempted_at: float, error: Exception | None = None,
) -> None:
    """Best-effort publication; never change the in-memory serving policy.

    The cache lock also covers removal. Recheck the roster under it so a fetch
    finishing after removal/replacement cannot publish the old slot's usage.
    No account-store lock or credential read is needed here.
    """
    if account is None or not account.get("usageGeneration"):
        return
    try:
        with FileLock(switcher.provider_dir / ".usage.lock", timeout=0):
            if switcher._read_sequence()["accounts"].get(number) != account:
                return
            path = switcher.provider_dir / f"usage-{number}.json"
            previous = _read_snapshot(path)
            if (previous.get("schemaVersion") != 1 or previous.get("number") != int(number)
                    or previous.get("slotGeneration") != account["usageGeneration"]):
                previous = {}
            # A failed fetch's in-memory last_good is not generation-tagged.
            # Only the matching disk snapshot can supply retained measurements.
            measurement = _measurement(previous)
            previous_at = _number(previous.get("fetchedAt"))
            previous_error = previous.get("lastError")
            error_at = (_number(previous_error.get("attemptedAt"))
                        if isinstance(previous_error, dict) else None)
            last_error = (_fields(previous_error, {
                "class": _error_class, "attemptedAt": _number,
            }) if error_at is not None else None)
            if error is None:
                fetched_at = _number(entry.fetched_at)
                if fetched_at is not None and (previous_at is None or fetched_at >= previous_at):
                    measurement = _snapshot(entry)
                if error_at is None or attempted_at >= error_at:
                    last_error = None
            elif (previous_at is None or attempted_at > previous_at) and (
                    error_at is None or attempted_at >= error_at):
                last_error = {"class": _error_class(type(error).__name__),
                              "attemptedAt": attempted_at}
            record = {
                "schemaVersion": 1,
                "number": int(number),
                "slotGeneration": account["usageGeneration"],
                "email": _label(account.get("email")),
                "planType": _label(account.get("planType")),
                **measurement,
                "lastError": last_error,
            }
            atomic_write_text(path, json.dumps(record, indent=2, allow_nan=False) + "\n",
                              follow_symlinks=False)
    except (OSError, LockError, ConfigError):
        # Quota collection remains usable on read-only disks or a busy cache.
        pass


def remove_usage_locked(provider_dir: Path, number: str) -> None:
    """Invalidate a snapshot; caller must hold the store and usage locks."""
    (provider_dir / f"usage-{number}.json").unlink(missing_ok=True)
