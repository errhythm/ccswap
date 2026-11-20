"""Opt-in Claude Code integrations: the ccswap statusline and the ccswap mod.

Both are off by default and toggled from ccswap's settings surfaces
(``ccswap config set claude.statusline|claude.mod on|off``, the TUI Settings
menu, the menu bar Settings menu). Their state is never stored: it is read
back from Claude Code itself, so a manual removal shows as off.

- Statusline: ``statusLine`` in ``<config_home>/settings.json`` (see
  https://code.claude.com/docs/en/statusline). Installing saves the user's
  previous ``statusLine`` into ccswap's settings and uninstalling restores it
  exactly. ``claude.previousStatusline`` maps each Claude settings.json's
  resolved path to ``{"value": <statusLine>}`` (one slot per config dir, and
  an explicit ``null`` survives); no entry means there was no key.
- Mod: the ``ccswap`` plugin from the ``ccswap`` marketplace, managed through
  the ``claude plugin`` CLI at user scope (see
  https://code.claude.com/docs/en/plugins/cli-reference).
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from ccswap.exceptions import ConfigError
from ccswap.fsutil import replace_with_retry
from ccswap.paths import get_claude_config_home
from ccswap.settings import (
    SETTINGS_SCHEMA_VERSION,
    _read_raw_for_write,
    atomic_write_json,
    settings_lock,
    settings_path,
)

_logger = logging.getLogger("ccswap")

STATUSLINE_COMMAND = "ccswap statusline"
PLUGIN_ID = "ccswap@ccswap"
MARKETPLACE_SOURCE = "errhythm/ccswap"
RELOAD_HINT = "Run /reload-plugins in open Claude Code sessions to apply it."

_SECTION = "claude"
_PREVIOUS_KEY = "previousStatusline"
_LIST_TIMEOUT_S = 30
_INSTALL_TIMEOUT_S = 180


# -- statusline ---------------------------------------------------------------

def claude_settings_path() -> Path:
    """Claude Code's user settings.json (in the config home, not ~/.claude.json)."""
    return get_claude_config_home() / "settings.json"


def _is_ours(status_line) -> bool:
    if not isinstance(status_line, dict):
        return False
    command = status_line.get("command")
    if not isinstance(command, str):
        return False
    return command.split()[:2] in (["ccswap", "statusline"], ["cswap", "statusline"])


def _read_claude_settings(path: Path) -> tuple[dict, str | None]:
    """(settings, raw text) for a read-modify-write; corrupt JSON raises.

    Starting from ``{}`` over a malformed file would replace the user's whole
    Claude configuration, so anything but a missing file or a JSON object is
    an error and nothing gets written.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}, None
    except (OSError, UnicodeDecodeError) as e:
        raise ConfigError(f"could not read {path}: {e}") from e
    if not text.strip():
        return {}, text
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ConfigError(
            f"{path} is not valid JSON ({e}); fix it before turning the "
            "statusline on or off"
        ) from e
    if not isinstance(data, dict):
        raise ConfigError(f"{path} is not a JSON object; fix it first")
    return data, text


def _write_claude_settings(path: Path, data: dict, old_text: str | None) -> None:
    """Atomic write that keeps the file's mode and follows a symlinked path.

    Claude Code writes this file with two-space indentation, so that plus the
    original trailing newline is as close to the user's formatting as a json
    round trip gets. Key order is preserved.
    """
    target = Path(os.path.realpath(path))
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        mode = target.stat().st_mode & 0o777
    except FileNotFoundError:
        mode = None
    content = json.dumps(data, indent=2, ensure_ascii=False)
    if old_text is None or old_text.endswith("\n"):
        content += "\n"
    fd, tmp_path = tempfile.mkstemp(dir=str(target.parent), suffix=".tmp")
    try:
        os.write(fd, content.encode("utf-8"))
        os.close(fd)
        fd = -1
        if mode is not None and sys.platform != "win32":
            os.chmod(tmp_path, mode)
        # ponytail: no lock against Claude Code's own writer; the window is a
        # read and a rename, same as any editor saving this file.
        replace_with_retry(tmp_path, str(target))
    except BaseException:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _slot(path: Path) -> str:
    return os.path.realpath(path)


def _saved_previous(raw: dict, slot: str) -> dict | None:
    """The ``{"value": ...}`` entry saved for this settings.json, if any."""
    section = raw.get(_SECTION)
    saved = section.get(_PREVIOUS_KEY) if isinstance(section, dict) else None
    entry = saved.get(slot) if isinstance(saved, dict) else None
    return entry if isinstance(entry, dict) and "value" in entry else None


def _update_ccswap_section(backup_root: Path, slot: str, entry: dict | None) -> None:
    """Save ``entry`` for ``slot`` (``None`` clears it), re-reading settings.json under its lock."""
    with settings_lock(backup_root):
        raw = _read_raw_for_write(settings_path(backup_root))
        section = raw.get(_SECTION)
        if not isinstance(section, dict):
            section = {}
        saved = section.get(_PREVIOUS_KEY)
        if not isinstance(saved, dict):
            saved = {}
        if entry is None:
            if slot not in saved:
                return
            del saved[slot]
        else:
            saved[slot] = entry
        if saved:
            section[_PREVIOUS_KEY] = saved
        else:
            section.pop(_PREVIOUS_KEY, None)
        if section:
            raw[_SECTION] = section
        else:
            raw.pop(_SECTION, None)
        raw["schemaVersion"] = raw.get("schemaVersion", SETTINGS_SCHEMA_VERSION)
        atomic_write_json(settings_path(backup_root), raw)


def statusline_installed() -> bool:
    """True when Claude's settings.json statusLine runs ``ccswap statusline``."""
    try:
        data = json.loads(claude_settings_path().read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    return isinstance(data, dict) and _is_ours(data.get("statusLine"))


def install_statusline(backup_root: Path) -> str:
    path = claude_settings_path()
    data, text = _read_claude_settings(path)
    if _is_ours(data.get("statusLine")):
        return "Claude statusline is already on"
    # Save first: if the Claude write then fails, a stale saved value is
    # harmless (the next install overwrites it); the reverse order could lose
    # the user's statusline.
    entry = {"value": data["statusLine"]} if "statusLine" in data else None
    _update_ccswap_section(backup_root, _slot(path), entry)
    data["statusLine"] = {"type": "command", "command": STATUSLINE_COMMAND}
    _write_claude_settings(path, data, text)
    return f"Claude statusline on ({path})"


def uninstall_statusline(backup_root: Path) -> str:
    path = claude_settings_path()
    data, text = _read_claude_settings(path)
    if not _is_ours(data.get("statusLine")):
        return "Claude statusline is not ccswap's; left unchanged"
    # Strict read before touching Claude's file: a corrupt ccswap settings
    # file must fail here, not after the user's statusLine is gone.
    raw = _read_raw_for_write(settings_path(backup_root))
    slot = _slot(path)
    previous = _saved_previous(raw, slot)
    if previous is None:
        del data["statusLine"]
    else:
        data["statusLine"] = previous["value"]
    _write_claude_settings(path, data, text)
    _update_ccswap_section(backup_root, slot, None)
    if previous is None:
        return "Claude statusline off"
    return "Claude statusline off (previous statusline restored)"


# -- mod (Claude Code plugin) -------------------------------------------------

def _claude_bin() -> str:
    path = shutil.which("claude")
    if not path:
        raise ConfigError("Claude Code CLI not found on PATH")
    return path


def _run(argv: list[str], timeout: float) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            argv,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        raise ConfigError(f"'{' '.join(argv[1:])}' timed out after {timeout:g}s") from e
    except OSError as e:
        raise ConfigError(f"could not run {argv[0]}: {e}") from e


def _failure(result: subprocess.CompletedProcess, what: str) -> ConfigError:
    detail = (result.stderr or result.stdout or "").strip()
    return ConfigError(f"{what} failed (exit {result.returncode}): {detail}")


def _user_install(claude: str) -> dict | None:
    """The user-scope ccswap@ccswap entry from ``claude plugin list --json``.

    Only user scope counts: that is the scope install/uninstall manage, so a
    project-scope install is someone else's. An unreadable listing reads as
    not installed: this backs a status display, which must not fail.
    """
    try:
        result = _run([claude, "plugin", "list", "--json"], _LIST_TIMEOUT_S)
        plugins = json.loads(result.stdout) if result.returncode == 0 else []
    except (ConfigError, json.JSONDecodeError) as e:
        _logger.debug("claude plugin list failed: %s", e)
        return None
    if not isinstance(plugins, list):
        return None
    return next(
        (
            p for p in plugins
            if isinstance(p, dict) and p.get("id") == PLUGIN_ID and p.get("scope") == "user"
        ),
        None,
    )


def mod_installed() -> bool:
    """True when ccswap@ccswap is installed at user scope and enabled; a
    disabled install reads as off (turning it on enables it)."""
    claude = shutil.which("claude")
    if not claude:
        return False
    entry = _user_install(claude)
    return entry is not None and entry.get("enabled") is True


def install_mod() -> str:
    claude = _claude_bin()
    entry = _user_install(claude)
    if entry is not None and entry.get("enabled") is not True:
        enabled = _run(
            [claude, "plugin", "enable", PLUGIN_ID, "--scope", "user"], _INSTALL_TIMEOUT_S
        )
        if enabled.returncode != 0:
            raise _failure(enabled, "claude plugin enable")
        return f"Claude Code mod on. {RELOAD_HINT}"
    added = _run(
        [claude, "plugin", "marketplace", "add", MARKETPLACE_SOURCE], _INSTALL_TIMEOUT_S
    )
    # A repeat add exits 0 ("already on disk"), so any nonzero exit is real.
    if added.returncode != 0:
        raise _failure(added, "claude plugin marketplace add")
    installed = _run(
        [claude, "plugin", "install", PLUGIN_ID, "--scope", "user"], _INSTALL_TIMEOUT_S
    )
    if installed.returncode != 0:
        raise _failure(installed, "claude plugin install")
    return f"Claude Code mod on. {RELOAD_HINT}"


def uninstall_mod() -> str:
    claude = _claude_bin()
    result = _run(
        [claude, "plugin", "uninstall", PLUGIN_ID, "--scope", "user"], _INSTALL_TIMEOUT_S
    )
    if result.returncode != 0:
        raise _failure(result, "claude plugin uninstall")
    return f"Claude Code mod off. {RELOAD_HINT}"


# -- settings surface ---------------------------------------------------------

def is_enabled(dotted_key: str) -> bool:
    return statusline_installed() if dotted_key == "claude.statusline" else mod_installed()


def set_enabled(backup_root: Path, dotted_key: str, on: bool) -> str:
    """Apply ``claude.statusline`` / ``claude.mod``; returns a one-line message."""
    if dotted_key == "claude.statusline":
        return install_statusline(backup_root) if on else uninstall_statusline(backup_root)
    if dotted_key == "claude.mod":
        return install_mod() if on else uninstall_mod()
    raise ConfigError(f"unknown Claude integration '{dotted_key}'")
