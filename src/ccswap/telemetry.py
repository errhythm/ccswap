"""Opt-in anonymous usage stats, sent to PostHog. Off unless ``telemetry.enabled`` is on.

Two events, both keyed by a random install ID and carrying the ccswap version,
the OS and the Python version. ``command`` names a command from ``COMMANDS``.
``daily`` counts switches per provider since the last one and the number of
saved accounts per provider, grouped. Neither carries emails, account or org
names, tokens, paths or arguments. ``DO_NOT_TRACK=1``, ``CCSWAP_TELEMETRY=0``
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
from ccswap.locking import FileLock

# PostHog project API key. It is write-only and public by design; empty sends nothing.
POSTHOG_KEY = "phc_pvViPC3zjzQPYfZpXLcr3ZN9D77nhn73CdfJRsPPwZCP"
CAPTURE_URL = "https://eu.i.posthog.com/i/v0/e/"
STATE_NAME = "telemetry.json"
THROTTLE_S = 3600  # one event per command per hour: cron and Raycast poll often
TIMEOUT_S = 2.0
DAY_S = 86400

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
ROSTERS = {"claude": "sequence.json", "codex": "codex/sequence.json", "agy": "agy/accounts.json"}
PROMPT = (
    "Help improve ccswap by sending anonymous usage stats? They hold command names, "
    "switch counts, how many accounts you have (grouped), the ccswap version and OS. "
    "Never emails, account names, tokens or paths. Change it any time with "
    "`ccswap config set telemetry.enabled on|off`."
)


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
    from ccswap.paths import get_legacy_backup_root
    from ccswap.settings import _read_raw, settings_path

    if blocked():
        return False
    legacy = get_legacy_backup_root()
    if legacy.exists() and legacy.resolve() != Path(backup_root).resolve():
        # Saving the answer would create settings.json in the new root, which
        # makes migrate_legacy_backup_dir refuse to move the legacy one. Ask
        # on a later run, after the switcher has migrated.
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


def _payload(install_id: str, name: str, properties: dict) -> dict:
    from ccswap import __version__

    return {
        "api_key": POSTHOG_KEY,
        "event": name,
        "distinct_id": install_id,
        "properties": {
            **properties,
            "version": __version__,
            "os": platform.system(),
            "python": platform.python_version(),
            "$process_person_profile": False,
            "$geoip_disable": True,
        },
    }


def event(install_id: str, command: str, provider: str | None) -> dict:
    return _payload(install_id, "command", {"command": command, "provider": provider})


def bucket(count: int) -> str:
    return str(count) if count < 3 else "3-5" if count <= 5 else "6+"


def daily_event(install_id: str, backup_root: Path, since: float) -> dict:
    """Switches per provider logged after ``since``, and saved accounts per provider."""
    from ccswap.logging_config import recent_switches

    root = Path(backup_root)
    props: dict = {f"switches_{p}": 0 for p in PROVIDERS}
    # shortcut: reads only the current log, not rotated ones; a day of switches fits in 1 MB.
    for switch in recent_switches(root / "claude-swap.log", limit=None):
        if switch.at > since and f"switches_{switch.provider}" in props:
            props[f"switches_{switch.provider}"] += 1
    for provider, name in ROSTERS.items():
        accounts = _read_state(root / name).get("accounts")
        props[f"accounts_{provider}"] = bucket(len(accounts) if isinstance(accounts, dict) else 0)
    return _payload(install_id, "daily", props)


def ask_in_terminal(backup_root: Path, argv: list[str]) -> None:
    """Ask once on an interactive CLI run; the TUI asks in its own dialog. Never raises."""
    try:
        if not all(stream.isatty() for stream in (sys.stdin, sys.stdout, sys.stderr)):
            return
        if command_of(argv)[0] in (None, "tui") or not should_ask(backup_root):
            return
        from ccswap.settings import set_setting

        print(PROMPT, file=sys.stderr)
        try:
            answer = input("Share anonymous usage stats? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print(file=sys.stderr)
            return  # no answer: ask again next time
        set_setting(backup_root, "telemetry.enabled", "on" if answer in ("y", "yes") else "off")
    except Exception as exc:
        if os.environ.get("CCSWAP_DEBUG"):
            print(f"telemetry: {exc}", file=sys.stderr)


def track_command(backup_root: Path, argv: list[str]) -> None:
    """Send the ``command`` and ``daily`` events that are due, if the user opted in. Never raises."""
    try:
        from ccswap.settings import load_telemetry_settings

        if blocked() or not load_telemetry_settings(backup_root).enabled:
            return
        command, provider = command_of(argv)
        if command is None:
            return
        path = Path(backup_root) / "cache" / STATE_NAME
        bodies = []
        # Held across read, due check and write so two commands started together
        # can't both send the same event. Busy for a second: skip, never block.
        with FileLock(path.with_name(".telemetry.lock"), timeout=1.0):
            state = _read_state(path)
            sent = state.get("sent") if isinstance(state.get("sent"), dict) else {}
            install_id = state.get("id") if isinstance(state.get("id"), str) else uuid.uuid4().hex
            last_daily = float(state.get("daily") or 0)
            now = time.time()
            if now - float(sent.get(command) or 0) >= THROTTLE_S:
                sent = {**sent, command: now}
                bodies.append(event(install_id, command, provider))
            if now - last_daily >= DAY_S:
                bodies.append(daily_event(install_id, backup_root, last_daily or now - DAY_S))
                last_daily = now
            if not bodies:
                return
            atomic_write_text(path, json.dumps({"id": install_id, "sent": sent, "daily": last_daily}))
        for body in bodies:
            _spawn(json.dumps(body).encode("utf-8"))
    except Exception as exc:
        if os.environ.get("CCSWAP_DEBUG"):
            print(f"telemetry: {exc}", file=sys.stderr)
