"""Opt-in anonymous usage stats, sent to PostHog. Off unless ``telemetry.enabled`` is on.

Each event carries a random install ID, the ccswap version, the OS, the Python
version and a command name from ``COMMANDS``. It never carries emails, account
or org names, tokens, paths or arguments. ``DO_NOT_TRACK=1``, ``CCSWAP_TELEMETRY=0``
or ``CI`` turn it off whatever the setting. docs/privacy.md lists every field.
Nothing here raises or blocks a command.
"""

from __future__ import annotations

import json
import os
import platform
import sys
import threading
import time
import urllib.request
import uuid
from pathlib import Path

from ccswap.fsutil import atomic_write_text

# PostHog project API key. It is write-only and public by design; empty sends nothing.
POSTHOG_KEY = ""
CAPTURE_URL = "https://eu.i.posthog.com/i/v0/e/"
STATE_NAME = "telemetry.json"
THROTTLE_S = 3600  # one event per command per hour: cron and Raycast poll often
TIMEOUT_S = 2.0

# The only command names ever sent. Anything else is "other"; `run` replaces the
# process before a send could finish, `statusline` runs on every Claude prompt,
# and `hub` serves other apps' requests, so those three never send.
COMMANDS = frozenset({
    "add", "add-token", "alias", "auto", "config", "disable", "enable", "export",
    "help", "import", "import-usage", "list", "map", "menubar", "move", "purge",
    "remove", "status", "swap", "switch", "tui", "unclaimed", "unmap", "upgrade", "watch",
})
_SILENT = frozenset({"run", "statusline", "hub"})
PROVIDERS = ("claude", "codex", "agy")


def blocked() -> bool:
    """True when nothing may be sent, whatever the setting says."""
    env = os.environ
    return (
        not POSTHOG_KEY
        or env.get("DO_NOT_TRACK", "").strip() not in ("", "0")
        or env.get("CCSWAP_TELEMETRY", "").strip().lower() in ("0", "off", "false", "no")
        or bool(env.get("CI"))
    )


def should_ask(backup_root: Path) -> bool:
    """Ask once: only while sending is possible and the user never chose."""
    from ccswap.settings import _read_raw, settings_path

    if blocked():
        return False
    section = _read_raw(settings_path(backup_root)).get("telemetry")
    return not (isinstance(section, dict) and "enabled" in section)


def command_of(argv: list[str]) -> tuple[str | None, str | None]:
    """(command, provider) from the raw arguments, using only fixed names.

    Returns (None, None) for a command that must not send.
    """
    args = list(argv)
    provider = None
    if args and args[0] in PROVIDERS:
        provider = args.pop(0)
    words = [a for a in args if not a.startswith("-")]
    flags = [a[2:] for a in args if a.startswith("--")]
    if words and words[0] in _SILENT:
        return None, None
    if "json" in flags:
        return None, None
    if words:  # a flag's value (`--provider codex`) can come first
        return next((w for w in words if w in COMMANDS), "other"), provider
    legacy = next((f for f in flags if f in COMMANDS), None)  # `--list`, `--switch`
    if legacy:
        return legacy, provider
    return ("tui" if not args else "other"), provider


def _read_state(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _send(body: bytes) -> None:
    try:
        request = urllib.request.Request(
            CAPTURE_URL, data=body, headers={"Content-Type": "application/json"}
        )
        urllib.request.urlopen(request, timeout=TIMEOUT_S).close()
    except Exception:
        pass


def _spawn(body: bytes) -> None:
    # Not a daemon: a short command waits up to TIMEOUT_S at exit for the send.
    threading.Thread(target=_send, args=(body,), name="ccswap-telemetry").start()


def event(install_id: str, command: str, provider: str | None) -> dict:
    from ccswap import __version__

    return {
        "api_key": POSTHOG_KEY,
        "event": "command",
        "distinct_id": install_id,
        "properties": {
            "command": command,
            "provider": provider,
            "version": __version__,
            "os": platform.system(),
            "python": platform.python_version(),
            "$process_person_profile": False,
            "$geoip_disable": True,
        },
    }


def track_command(backup_root: Path, argv: list[str]) -> None:
    """Send one ``command`` event when the user opted in. Never raises."""
    try:
        from ccswap.settings import load_telemetry_settings

        if blocked() or not load_telemetry_settings(backup_root).enabled:
            return
        command, provider = command_of(argv)
        if command is None:
            return
        path = Path(backup_root) / "cache" / STATE_NAME
        state = _read_state(path)
        sent = state.get("sent") if isinstance(state.get("sent"), dict) else {}
        now = time.time()
        if now - float(sent.get(command) or 0) < THROTTLE_S:
            return
        install_id = state.get("id") if isinstance(state.get("id"), str) else uuid.uuid4().hex
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, json.dumps({"id": install_id, "sent": {**sent, command: now}}))
        body = json.dumps(event(install_id, command, provider)).encode("utf-8")
        _spawn(body)
    except Exception as exc:
        if os.environ.get("CCSWAP_DEBUG"):
            print(f"telemetry: {exc}", file=sys.stderr)
