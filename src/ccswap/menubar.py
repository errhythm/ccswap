"""macOS menu bar app for ccswap (``cswap --menubar``).

A thin GUI shell over ``ClaudeAccountSwitcher`` and the core auto-switch engine
(``ccswap.autoswitch``) — it never re-implements account, usage, or
auto-switch logic. Usage for display comes from ``switcher.accounts_snapshot()``
(backed by the shared usage store); auto-switching, when enabled, runs the same
``AutoSwitchEngine`` the CLI's ``cswap auto`` drives, sharing
``autoswitch_state.json`` and the ``autoswitch.*`` settings. The menu bar keeps
only its own display preferences.

Built on ``rumps`` (an optional extra, macOS only). The pure helpers below
(settings, formatting, log parsing) are import-safe without rumps so they can be
unit-tested in CI; ``rumps`` is imported lazily inside the app glue.
"""

from __future__ import annotations

import logging
import os
import platform
import plistlib
import re
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import ClassVar

from ccswap import oauth, pace
from ccswap.exceptions import ClaudeSwitchError
from ccswap.printer import warning
from ccswap.settings import (
    MenuBarConfig,
    load_menubar_settings,
    save_menubar_settings,
)
from ccswap.switcher import SENTINEL_NOTES, USAGE_RELOGIN_REQUIRED

ICON = "⇄"
REFRESH_CHOICES: tuple[int, ...] = (30, 60, 300)
AUTO_THRESHOLD_CHOICES: tuple[int, ...] = (80, 90, 95, 98)
SWITCH_HISTORY_LIMIT = 10
# How long an identical engine error stays quiet after being surfaced. The
# engine retries every tick, so an unconditional notification is a storm; an
# unconditional drop is how a broken auto-switch looked healthy for two days.
ENGINE_ERROR_REPEAT_S = 3600.0
NOTIFICATION_BUNDLE_ID = "com.ccswap.menubar"


def ensure_notification_identity(
    executable: Path | None = None,
    *,
    platform: str = sys.platform,
) -> Path | None:
    """Ensure rumps can resolve a bundle identifier for notifications.

    Fallback for runs outside the wrapper bundle (``ccswap.app_bundle``),
    whose Info.plist already carries the identifier: a bare command-line
    interpreter has no app bundle, so rumps looks for an ``Info.plist`` beside
    it. uv/pipx reinstalls can recreate that environment; repair the tiny
    plist on every launch when needed.
    """
    if platform != "darwin":
        return None
    path = (executable or Path(sys.executable)).parent / "Info.plist"
    data: dict = {}
    try:
        if path.exists():
            try:
                loaded = plistlib.loads(path.read_bytes())
            except Exception:
                loaded = None  # unreadable/corrupt — rebuild from scratch
            if isinstance(loaded, dict):
                data = loaded
        changed = False
        if not data.get("CFBundleIdentifier"):
            data["CFBundleIdentifier"] = NOTIFICATION_BUNDLE_ID
            changed = True
        if not data.get("CFBundleName"):
            data["CFBundleName"] = "claude-swap"
            changed = True
        if changed or not path.exists():
            # atomic: an interrupted write must not leave a half-written plist
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_bytes(plistlib.dumps(data))
            os.replace(tmp, path)
    except (OSError, plistlib.InvalidFileException, ValueError) as exc:
        logging.getLogger("ccswap").warning(
            "Could not prepare menu-bar notification identity: %s", exc
        )
        return None
    return path


@dataclass
class MenuBarSettings:
    """User-configurable menu bar behavior: the ``menubar`` section of settings.json.

    A mutable mirror of :class:`ccswap.settings.MenuBarConfig` so the menu can
    flip fields in place; ``ccswap config set menubar.*`` edits the same keys.
    Auto-switch *policy* (threshold, cooldown, hysteresis, …) stays in the
    ``autoswitch.*`` keys.
    """

    style: str = "popover"  # "popover" | "classic"
    title_provider: str = "claude"  # "auto" or a provider id
    show_account_name: bool = True
    title_pct: str = "both"  # one of TITLE_PCT_CHOICES
    title_scoped: bool = False  # append per-model weekly limits (e.g. Fable) to the title
    refresh_interval: int = 60
    auto_switch_enabled: bool = False
    codex_auto_switch_enabled: bool = False

    @classmethod
    def load(cls, backup_root: Path) -> "MenuBarSettings":
        """Load from settings.json (importing an old menubar_settings.json once)."""
        return cls(**asdict(load_menubar_settings(backup_root)))

    def save(self, backup_root: Path) -> None:
        """Write the menubar section of settings.json atomically.

        Atomic, because the CLI and the running menu bar both write it and a
        torn write would read back as defaults (auto-switch off).
        """
        save_menubar_settings(backup_root, MenuBarConfig(**asdict(self)))


# ---- pure display helpers (operate on the usage-window dict shape produced by
# ---- oauth.build_usage_result / stored in UsageEntry.last_good) --------------

def tightest_pct(usage: dict | str | None) -> float | None:
    """Highest 5h/7d utilization percentage, or None if unknown.

    Surfaces the binding window's utilization for display. Spend is excluded —
    it isn't a rate-limit window.
    """
    if not isinstance(usage, dict):
        return None
    pcts = [
        window["pct"]
        for window in (
            usage.get("five_hour"),
            usage.get("seven_day"),
            usage.get("weekly"),
        )
        if isinstance(window, dict) and isinstance(window.get("pct"), (int, float))
    ]
    return max(pcts) if pcts else None


def _window_pct(usage: dict | str | None, key: str) -> float | None:
    """Utilization pct for a usage window (``five_hour``/``seven_day``), or None."""
    if isinstance(usage, dict):
        window = usage.get(key)
        if isinstance(window, dict) and isinstance(window.get("pct"), (int, float)):
            return float(window["pct"])
    return None


def _resets_at_ts(window: dict | str | None) -> float:
    """POSIX timestamp of a usage window's ``resets_at``; inf if missing/bad."""
    if isinstance(window, dict):
        ra = window.get("resets_at")
        if isinstance(ra, str):
            try:
                return datetime.fromisoformat(ra).timestamp()
            except ValueError:
                pass
    return float("inf")


def _live_countdown(window: dict | str | None, now: float) -> str | None:
    """Time until a usage window resets, computed live from ``resets_at``.

    The cached usage dict's ``countdown`` string is frozen at fetch time, so a
    stale (e.g. last-known-good) entry would show a wrong remaining time. Deriving
    it from the absolute ``resets_at`` keeps it correct between/without refetches.
    Returns ``None`` when there's no ``resets_at`` or it has already passed.
    """
    ts = _resets_at_ts(window)
    if ts == float("inf"):
        return None
    remaining = int(ts - now)
    if remaining <= 0:
        return None
    days, rem = divmod(remaining, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days > 0:
        return f"{days}d {hours}h"
    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


_WEEKLY_PERIOD_S = 7 * 86400  # weekly limits reset on a fixed 7-day cadence


def _rolled_weekly_window(window: dict | None, now: float) -> dict | None:
    """A weekly window with a passed reset advanced to its next 7-day boundary.

    Weekly limits reset on a fixed weekly cadence, so once the stored
    ``resets_at`` is in the past we know the window rolled over — the stored pct
    belongs to a window that no longer exists. Return a copy reflecting the reset
    state (``pct`` 0, ``resets_at`` advanced to the next future boundary) so the
    menu bar shows the reset from the static schedule alone, without waiting to
    spend tokens on a fresh fetch. Missing/future/unparseable windows are
    returned unchanged.
    """
    if not isinstance(window, dict):
        return window
    ts = _resets_at_ts(window)
    if ts == float("inf") or ts > now:
        return window
    missed = int((now - ts) // _WEEKLY_PERIOD_S) + 1
    new_ts = ts + missed * _WEEKLY_PERIOD_S
    rolled = dict(window)
    rolled["pct"] = 0.0
    rolled["resets_at"] = datetime.fromtimestamp(new_ts, tz=timezone.utc).isoformat()
    rolled.pop("countdown", None)  # recomputed live from the rolled resets_at
    rolled.pop("clock", None)
    return rolled


MENU_NOTE_LIMIT = 40

# A reading older than this many display intervals is called stale in the
# health row, but never sooner than STALE_USAGE_FLOOR_S: while the engine
# runs, the display reads store-only and the engine polls the active account
# on its own cadence, so three short display intervals would be too strict.
STALE_REFRESH_INTERVALS = 3
STALE_USAGE_FLOOR_S = 15 * 60


@dataclass(frozen=True)
class EngineStoppedEvent:
    """Queued by the menu bar's engine thread when its ``run_loop`` ends.

    Carries the engine object, not just a flag: a restart starts the
    replacement before the old thread has finished, so the old thread's exit
    must be matched against the engine the app hosts *now* before anything is
    cleared (see :func:`engine_exit_applies`). ``error`` is the exception that
    ended the loop, or None when it returned on its own.
    """

    kind: ClassVar[str] = "engine-stopped"
    engine: object
    error: str | None = None

    def human(self) -> str:
        return self.error or "the engine loop ended on its own"


def engine_exit_applies(current, stopped) -> bool:
    """Whether a stopped engine is the one the menu bar currently hosts.

    False for a predecessor that finished after a restart, and for an engine
    the app already let go of through its own stop.
    """
    return current is not None and current is stopped


def _age_text(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return "just now"
    minutes, hours, days = seconds // 60, seconds // 3600, seconds // 86400
    if hours < 1:
        return f"{minutes}m ago"
    if days < 1:
        return f"{hours}h {minutes % 60}m ago"
    return f"{days}d {hours % 24}h ago"


def active_usage_age(snapshot: dict, now: float | None = None) -> float | None:
    """Seconds since the active account's usage was last read, or None.

    The health row dates the measurement, not the display's last look at the
    store: a store-only refresh (the engine paces all fetching while it runs)
    would otherwise call a two-hour-old reading "just now".
    """
    now = time.time() if now is None else now
    for row in snapshot.get("accounts") or []:
        if row[2]:  # is_active
            fetched_at = row[7]
            return max(0.0, now - fetched_at) if fetched_at else None
    return None


def health_label(
    *,
    auto_enabled: bool,
    engine_running: bool,
    usage_age: float | None,
    refresh_interval: int,
) -> tuple[str, bool]:
    """The menu's first row: ``(label, attention)``.

    Four states hide behind "the icon is there": auto-switch off in settings,
    enabled with the engine running, enabled with no engine (it failed to
    start or its thread died), and usage that stopped being read. The row
    names the first three and dates the fourth from the active account's last
    reading (:func:`active_usage_age`); ``attention`` is True for the state
    that needs a click, so the caller can wire a restart to the row and flag
    the title.
    """
    if not auto_enabled:
        status, attention = "Auto-switch off", False
    elif engine_running:
        status, attention = "Auto-switch running", False
    else:
        status, attention = "⚠ Auto-switch stopped, click to restart", True
    stale_after = max(STALE_REFRESH_INTERVALS * refresh_interval, STALE_USAGE_FLOOR_S)
    if usage_age is None:
        usage = "no usage reading yet"
    elif usage_age > stale_after:
        usage = f"usage stale, read {_age_text(usage_age)}"
    else:
        usage = f"usage read {_age_text(usage_age)}"
    return f"{status} · {usage}", attention


def menu_note(note: str, limit: int = MENU_NOTE_LIMIT) -> str:
    """The state half of a sentinel note, sized for a menu row.

    ``SENTINEL_NOTES`` carry a remedy after an em dash ("re-login needed —
    refresh token dead; log in with Claude Code, then run: cswap add"), which
    ``cswap list`` has the width for. A menu is as wide as its widest row, so
    one expired account otherwise doubles the menu and pushes every other
    row's numbers to the far edge. The row keeps the state; the full note
    travels as the row's tooltip (see ``rebuild_menu``).
    """
    state = note.split(" — ", 1)[0].strip()
    if len(state) > limit:
        state = state[: limit - 1].rstrip() + "…"
    return state


def _set_tooltip(item, text: str) -> None:
    """Attach ``text`` as the row's hover tooltip; NSMenuItem carries one natively."""
    ns_item = getattr(item, "_menuitem", None)
    if ns_item is not None and hasattr(ns_item, "setToolTip_"):
        ns_item.setToolTip_(text)


def usage_summary(
    usage: dict | str | None, now: float | None = None, fetched_at: float | None = None
) -> str:
    """One-line usage summary for an account row (reset countdown computed live).

    ``fetched_at`` is the underlying measurement's fetch time (may be older
    than ``now`` when serving last-good data) — used only to flag a weekly
    window that's meaningfully ahead of pace (issue #125), never the 5h one.
    """
    if isinstance(usage, str):
        return menu_note(usage)
    if usage is None:
        return "usage unavailable"
    if now is None:
        now = time.time()
    parts: list[str] = []
    rejected = usage.get("rejected")
    if isinstance(rejected, dict):
        # A refused setup-token probe leads the row: the account is at its
        # limit until the refusal ends, however low the windows below read.
        seg = "rejected (!)"
        countdown = _live_countdown(rejected, now)
        if countdown:
            seg += f" ({countdown})"
        parts.append(seg)
    for key, label in (
        ("five_hour", "5h"),
        ("seven_day", "7d"),
        ("weekly", "Weekly"),
    ):
        window = usage.get(key)
        pace_result = None
        if key in ("seven_day", "weekly"):
            window = _rolled_weekly_window(window, now)  # reflect a passed weekly reset
            # Pace against the rolled window, not the raw one: a stale window
            # rolled to 0% has no current-cycle data to compare against, so
            # its (correctly zeroed) pct naturally never reads as "ahead" —
            # computing pace pre-roll would otherwise pair last cycle's high
            # pct with this cycle's freshly-reset 0% display.
            pace_result = pace.compute_pace(window, fetched_at=fetched_at)
        if isinstance(window, dict) and isinstance(window.get("pct"), (int, float)):
            seg = f"{label} {window['pct']:.0f}%"
            if pace_result and pace_result.ahead:
                seg += " (ahead)"
            countdown = _live_countdown(window, now)
            if countdown:
                seg += f" ({countdown})"  # time until this window resets
            parts.append(seg)
    reset_credits = usage.get("reset_credits")
    if isinstance(reset_credits, dict) and isinstance(
        reset_credits.get("available"), int
    ):
        seg = f"Resets {reset_credits['available']}"
        expiry = {"resets_at": reset_credits.get("expires_at")}
        countdown = _live_countdown(expiry, now)
        if countdown:
            seg += f" (expires {countdown})"
        parts.append(seg)
    credits = usage.get("credits")
    if isinstance(credits, dict):
        if credits.get("limit_reached") is True:
            seg = "Credits limit reached"
        elif credits.get("unlimited") is True:
            seg = "Credits unlimited"
        elif credits.get("has_credits") is False:
            seg = "Credits none"
        elif isinstance(credits.get("balance"), (int, float)):
            seg = f"Credits {credits['balance']:,.2f}"
        elif credits.get("has_credits") is True:
            seg = "Credits available"
        else:
            seg = ""
        if seg:
            parts.append(seg)
    allowance = usage.get("credit_allowance")
    if isinstance(allowance, dict):
        if allowance.get("limit_reached") is True:
            seg = "Allowance limit reached"
        elif isinstance(allowance.get("remaining"), (int, float)):
            seg = f"Allowance {allowance['remaining']:,.2f} left"
        elif isinstance(allowance.get("limit"), (int, float)):
            seg = f"Allowance {allowance['limit']:,.2f} limit"
        else:
            seg = ""
        if seg:
            parts.append(seg)
    # Per-model weekly limits (e.g. Fable), from the usage API's ``limits`` array.
    for window in usage.get("scoped") or []:
        window = _rolled_scoped_window(window, now)
        pace_result = pace.compute_pace(window, fetched_at=fetched_at)  # against the rolled window, see above
        if isinstance(window, dict) and isinstance(window.get("pct"), (int, float)) and window.get("name"):
            seg = f"{window['name']} {window['pct']:.0f}%"
            if window["pct"] >= 100:
                seg += " (!)"  # maxed model — the usual reason to switch
            elif pace_result and pace_result.ahead:
                seg += " (ahead)"
            countdown = _live_countdown(window, now)
            if countdown:
                seg += f" ({countdown})"
            parts.append(seg)
    spend = usage.get("spend")
    if isinstance(spend, dict) and isinstance(spend.get("pct"), (int, float)):
        parts.append(f"$ {spend['pct']:.0f}%")
    return " · ".join(parts) if parts else "usage unavailable"


def format_account_label(
    num,
    email: str,
    usage: dict | str | None,
    now: float | None = None,
    alias: str | None = None,
    disabled: bool = False,
    fetched_at: float | None = None,
    login_expires_at: float | None = None,
    quarantined: bool = False,
) -> str:
    """Build one account row's menu label.

    Ends with ``· re-login in 5d 12h`` while the slot's stored login is due
    (:func:`oauth.format_login_expiry`, the same judgement the CLI/TUI views
    use), last so its position is what stays constant in a proportional-font
    NSMenu row.
    """
    label = f"{alias}  ({email})" if alias else email
    marker = "  (disabled)" if disabled else ""
    row = f"{num}  {label}{marker}  {usage_summary(usage, now, fetched_at)}"
    note = oauth.format_login_expiry(login_expires_at, quarantined, now)
    return f"{row} · {note}" if note else row


def _local_part(email: str, limit: int = 12) -> str:
    """Email text before '@', truncated with a trailing '*' marker."""
    local = email.split("@", 1)[0]
    if len(local) > limit:
        return local[: limit - 1] + "*"
    return local


def _rolled_scoped_window(window: dict | None, now: float) -> dict | None:
    """Roll a passed scoped window forward, weekly ones only.

    Antigravity buckets carry ``window: "5h"``; rolling those on a weekly
    cadence would show a wrong reset.
    """
    if isinstance(window, dict) and window.get("window") == "5h":
        return window
    return _rolled_weekly_window(window, now)


def _scoped_max(usage: dict | str | None, duration: str) -> float | None:
    """Highest pct among scoped windows of one duration (``"5h"``/``"weekly"``).

    Antigravity reports only per-group buckets, no account-wide window.
    """
    if not isinstance(usage, dict):
        return None
    pcts = [
        w["pct"] for w in usage.get("scoped") or []
        if isinstance(w, dict) and w.get("window") == duration and isinstance(w.get("pct"), (int, float))
    ]
    return max(pcts) if pcts else None


def title_usage_pct(usage: dict | str | None) -> float | None:
    """How close an account is to a limit, for picking the ``auto`` title provider.

    The tightest account-wide window; without one (Antigravity), the highest
    scoped window. A rejected probe counts as 100.
    """
    if isinstance(usage, dict) and isinstance(usage.get("rejected"), dict):
        return 100.0
    pct = tightest_pct(usage)
    if pct is not None or not isinstance(usage, dict):
        return pct
    scoped = [w["pct"] for w in usage.get("scoped") or [] if isinstance(w, dict) and isinstance(w.get("pct"), (int, float))]
    return max(scoped) if scoped else None


TITLE_TAGS = {"claude": "CC", "codex": "CX", "agy": "AG"}


def title_tag(provider_id: str, label: str) -> str:
    """Short prefix naming the provider in an ``auto`` title."""
    return TITLE_TAGS.get(provider_id) or label[:2].upper()


def resolve_title_provider(setting: str, actives: dict[str, dict | str | None]) -> str:
    """The provider whose numbers the title shows.

    ``actives`` maps each provider that has an active account to that
    account's usage, in registry order. ``auto`` picks the highest
    :func:`title_usage_pct` (ties go to the earlier provider); with no
    numbers anywhere, the first provider with an active account; else claude.
    """
    if setting != "auto":
        return setting
    best, best_pct = None, None
    for pid, usage in actives.items():
        pct = title_usage_pct(usage)
        if pct is not None and (best_pct is None or pct > best_pct):
            best, best_pct = pid, pct
    return best or next(iter(actives), "claude")


def format_title(
    active_email: str | None,
    active_usage: dict | str | None,
    settings: MenuBarSettings,
    now: float | None = None,
    alias: str | None = None,
    *,
    attention: bool = False,
    icon: bool = True,
    tag: str | None = None,
) -> str:
    """Build the menu-bar title from the active account and settings.

    ``attention`` prefixes a warning sign: auto-switch is enabled but not
    running, which the closed menu would otherwise never show. ``icon=False``
    drops the text icon (the popover shell draws a ring image instead).
    ``tag`` names the provider when the title provider is ``auto``.
    """
    mark = ICON if icon else ""
    icon = (f"⚠ {mark}" if attention else mark).strip()
    if active_email is None:
        return icon
    if now is None:
        now = time.time()
    segments: list[str] = [tag] if tag else []
    if settings.show_account_name:
        segments.append(alias if alias else _local_part(active_email))
    if isinstance(active_usage, dict) and isinstance(active_usage.get("rejected"), dict):
        segments.append("rejected (!)")
    if settings.title_pct in ("5h", "both"):
        p = _window_pct(active_usage, "five_hour")
        if p is None:
            p = _scoped_max(active_usage, "5h")
        if p is not None:
            segments.append(f"{p:.0f}%")
    if settings.title_pct in ("7d", "both"):
        seven = None
        if isinstance(active_usage, dict):
            seven = active_usage.get("seven_day") or active_usage.get("weekly")
        seven = _rolled_weekly_window(seven, now)  # reflect a passed weekly reset
        p = seven["pct"] if isinstance(seven, dict) and isinstance(seven.get("pct"), (int, float)) else None
        if p is None:
            p = _scoped_max(active_usage, "weekly")
        if p is not None:
            segments.append(f"{p:.0f}%")
    if settings.title_scoped and isinstance(active_usage, dict):
        # Per-model weekly limits (e.g. Fable), same shape/roll-forward as the
        # dropdown rows; named so multiple scoped models stay distinguishable.
        for window in active_usage.get("scoped") or []:
            window = _rolled_scoped_window(window, now)
            if isinstance(window, dict) and isinstance(window.get("pct"), (int, float)) and window.get("name"):
                segments.append(f"{window['name']} {window['pct']:.0f}%")
    if not segments:
        return icon
    return (f"{icon} " if icon else "") + " · ".join(segments)


def format_usage_log(email: str, usage: dict | str | None) -> str | None:
    """A log line of an account's session (5h) and weekly (7d) limits.

    Uses each window's absolute reset ``clock`` rather than a live countdown,
    since log lines are already timestamped. Returns ``None`` when no numeric
    window is available (sentinels, ``None``, or spend-only) so callers can skip
    logging nothing.
    """
    parts: list[str] = []
    for key, label in (
        ("five_hour", "5h"),
        ("seven_day", "7d"),
        ("weekly", "Weekly"),
    ):
        pct = _window_pct(usage, key)
        if pct is None:
            continue
        window = usage.get(key)  # a dict — _window_pct found a numeric pct in it
        clock = window.get("clock") if isinstance(window, dict) else None
        seg = f"{label} {pct:.0f}%"
        if clock:
            seg += f" (resets {clock})"
        parts.append(seg)
    if not parts:
        return None
    return f"usage {email}: " + " · ".join(parts)


def _usage_log_key(usage: dict | str | None) -> tuple[float | None, float | None]:
    """De-dupe key for usage logging: the short and weekly percentages only.

    Reset clocks change every refresh; keying on the percentages means an idle
    account isn't re-logged every cycle.
    """
    weekly = _window_pct(usage, "seven_day")
    if weekly is None:
        weekly = _window_pct(usage, "weekly")
    return (_window_pct(usage, "five_hour"), weekly)


_SWITCH_LOG_RE = re.compile(r"Switched from account (\d+) to (\d+)")


def should_surface_engine_error(event, surfaced: dict, now: float) -> bool:
    """Whether a hosted engine's error event should reach the log and the user.

    ``surfaced`` maps each message to when it was last surfaced (monotonic
    seconds) and is updated when this returns True. A message not seen within
    ``ENGINE_ERROR_REPEAT_S`` is surfaced; otherwise it stays quiet — per
    message, so two errors alternating tick by tick can't defeat the limit.
    """
    message = getattr(event, "message", None)
    if not message:
        return False
    # Digits stripped so a message carrying a changing id, count or time is
    # still one error, and stale entries pruned so the table stays bounded.
    key = re.sub(r"\d+", "#", message)
    for k in [k for k, t in surfaced.items() if now - t >= ENGINE_ERROR_REPEAT_S]:
        del surfaced[k]
    if key in surfaced:
        return False
    surfaced[key] = now
    return True


def parse_switch_history(log_text: str, limit: int = SWITCH_HISTORY_LIMIT) -> list[str]:
    """Recent account switches from the log, most-recent first.

    Reads the ``Switched from account X to Y`` lines the switcher logs and pairs
    each with its timestamp (trimmed to the minute). Returns at most ``limit``
    entries like ``"3 → 1   2026-06-27 02:06"``. Any unparseable line is skipped.
    """
    out: list[str] = []
    for line in log_text.splitlines():
        m = _SWITCH_LOG_RE.search(line)
        if not m:
            continue
        stamp = line.split(" - ", 1)[0].strip()[:16]  # "YYYY-MM-DD HH:MM"
        out.append(f"{m.group(1)} → {m.group(2)}   {stamp}")
    return out[-limit:][::-1]


def _account_display_usage(entry) -> dict | str | None:
    """Menu-display usage for a ``UsageEntry``.

    A human-readable note for a sentinel state (token expired / API key /
    keychain unavailable), otherwise the last-good measurement dict, otherwise
    ``None``.
    """
    if entry.sentinel:
        return SENTINEL_NOTES.get(entry.sentinel, entry.sentinel)
    return entry.last_good


EMPTY_SNAPSHOT: dict = {
    "accounts": [],
    "active_email": None,
    "active_usage": None,
    "active_alias": None,
}


def _adapt_snapshot(snap) -> dict:
    """Adapt an ``AccountsSnapshot`` to the menu bar's render dict.

    Shape: ``{"accounts": [(num, email, is_active, display_usage, last_good, alias, disabled, fetched_at, login_expires_at, quarantined), ...],
    "active_email": str | None, "active_usage": dict | str | None,
    "active_alias": str | None}``. The snapshot itself is produced by
    ``SnapshotSource`` (the paced read path), so this is a pure transform — no
    fetching, no I/O. Per-account ``fetched_at`` is the underlying
    measurement's fetch time, used only for the pace marker (issue #125).
    ``login_expires_at``/``quarantined`` are the same fields the TUI's re-login
    note reads (``AccountSnapshot.login_expires_at``, sentinel ==
    ``USAGE_RELOGIN_REQUIRED``) — no extra credential read.
    """
    accounts = []
    active_email = None
    active_usage = None
    active_alias = None
    for acc in snap.accounts:
        display = _account_display_usage(acc.usage)
        accounts.append(
            (
                acc.number, acc.email, acc.is_active, display, acc.usage.last_good,
                acc.alias, acc.disabled, acc.usage.fetched_at,
                acc.login_expires_at, acc.usage.sentinel == USAGE_RELOGIN_REQUIRED,
            )
        )
        if acc.is_active:
            active_email, active_usage, active_alias = acc.email, display, acc.alias
    return {
        "accounts": accounts,
        "active_email": active_email,
        "active_usage": active_usage,
        "active_alias": active_alias,
    }


def _take_worker_snapshot(
    snapshot_source, *, full: bool, has_engine: bool
) -> tuple[dict, bool]:
    """Take and adapt one display snapshot; returns ``(snap, store_only)``.

    Routine ticks read store-only while the hosted auto-switch engine is
    running; an explicit Refresh now (``full=True``) still runs a store-governed
    on-demand pass.
    """
    store_only = has_engine and not full
    raw = snapshot_source.take(full=full, store_only=store_only)
    return _adapt_snapshot(raw), store_only


# macOS 26 stopped drawing status items for processes launched through an
# exec trampoline, and a CPython *framework* build is exactly that: its
# ``bin/python3.x`` is a stub that posix_spawns into ``Python.app``
# (Mac/Tools/pythonw.c). Homebrew and python.org ship framework builds;
# uv-managed and most other interpreters do not.
#
# Measured on macOS 26.6.2 with rumps 0.4.0, same bare rumps app throughout:
#
#   Homebrew 3.14.6   sys._framework 'Python'   no status item
#   Homebrew 3.10.21  sys._framework 'Python'   no status item
#   uv 3.14.7         sys._framework ''         status item drawn
#   uv 3.13.15        sys._framework ''         status item drawn
#
# The interpreter version is not the variable; the build is.


MIN_AFFECTED_MACOS = 26


def _macos_major(mac_ver: str | None = None) -> int | None:
    """Major version of the running macOS, or None if it cannot be read."""
    raw = platform.mac_ver()[0] if mac_ver is None else mac_ver
    head = raw.split(".")[0]
    return int(head) if head.isdigit() else None


def framework_build_warning(
    framework=None, install_method=None, mac_ver: str | None = None
) -> str | None:
    """Text to show when this interpreter cannot draw a status item.

    Returns None wherever the menu bar is known to work. Both halves of the
    condition matter: the evidence is a framework build *on macOS 26*, and
    framework builds draw fine on earlier releases — gating on the build alone
    would nag every Homebrew user on macOS 14 or 15, on every launch and in
    the service log on every restart.

    Nothing here can fix the incompatibility. The point is that it fails
    silently, with a healthy process and empty logs, so it is worth one line.
    """
    fw = getattr(sys, "_framework", "") if framework is None else framework
    if not fw:
        return None

    major = _macos_major(mac_ver)
    if major is None or major < MIN_AFFECTED_MACOS:
        return None

    if install_method is None:
        from ccswap.update_check import _detect_install_method

        install_method = _detect_install_method()

    if install_method == "uv":
        remedy = (
            "  uv tool install --managed-python --force 'ccswap[menubar]'"
        )
    elif install_method == "pipx":
        remedy = (
            "  Reinstall against a non-framework interpreter, e.g. one from "
            "`uv python install 3.13`:\n"
            "  pipx install --force --python <that python> 'ccswap[menubar]'"
        )
    else:
        remedy = (
            "  Reinstall against a non-framework interpreter "
            "(uv-managed ones are; Homebrew and python.org are not)."
        )

    return (
        "This is a framework build of Python, which on macOS 26 has been "
        "observed not to draw the menu bar icon: the process runs and logs "
        "nothing, but no status item appears.\n" + remedy
    )


def acquire_single_instance(backup_dir: Path):
    """Hold ``menubar.lock`` for the life of the process, or raise.

    Two menu bars would run two auto-switch engines over the same accounts.
    Taken after the app-bundle hop: file descriptors do not survive exec.
    """
    from ccswap.locking import FileLock

    lock = FileLock(backup_dir / "menubar.lock", timeout=0)
    if not lock.acquire(timeout=0):
        raise ClaudeSwitchError(
            "Another ccswap menu bar is already running. Quit it first, or "
            "restart the service: launchctl kickstart -k gui/$(id -u)/com.ccswap.menubar"
        )
    return lock


def _importable(module: str) -> bool:
    import importlib

    try:
        importlib.import_module(module)
    except ImportError:
        return False
    return True


def pick_shell(style: str, classic: bool, has_webkit: bool, has_rumps: bool) -> str | None:
    """``"popover"``, ``"classic"`` or None when neither shell can run.

    ``--classic`` and ``menubar.style = classic`` pick the text menu; the
    popover falls back to it when WebKit is missing.
    """
    if not classic and style == "popover" and has_webkit:
        return "popover"
    return "classic" if has_rumps else None


def run(switcher, classic: bool = False) -> int:
    """Entry point for ``ccswap menubar``. Blocks until the user quits.

    Opens the shell ``menubar.style`` names (the popover by default);
    ``classic`` forces the rumps text menu for this run.
    """
    # This module is import-safe without the extra, so the CLI's guard around
    # ``from ccswap.menubar import run`` never sees a missing dependency; the
    # failure lands here and becomes the error type the CLI renders cleanly.
    if not _importable("AppKit"):
        raise ClaudeSwitchError(
            "Menu bar mode requires the menubar extra. "
            "Install with: uv tool install --force 'ccswap[menubar]'"
        )
    import AppKit

    settings = MenuBarSettings.load(switcher.backup_dir)
    shell_name = pick_shell(settings.style, classic, _importable("WebKit"), _importable("rumps"))
    if shell_name is None:
        raise ClaudeSwitchError(
            "Menu bar mode requires 'rumps' or pyobjc-framework-WebKit. "
            "Install with: uv tool install --force 'ccswap[menubar]'"
        )
    if shell_name == "classic" and settings.style == "popover" and not classic:
        warning(
            "The popover needs pyobjc-framework-WebKit; using the classic menu. "
            "Install with: uv tool install --force 'ccswap[menubar]'",
            file=sys.stderr,
        )

    from ccswap import app_bundle

    # Hop into the wrapper .app before anything AppKit-side is set up, so the
    # status item's owner has a bundle identifier that menu bar managers can
    # key on. Returns only when staying in this interpreter (already bundled,
    # opted out, framework build, or the bundle could not be built).
    app_bundle.maybe_reexec(switcher.backup_dir)
    if not app_bundle.in_bundle():
        ensure_notification_identity()
    _warn = framework_build_warning()
    if _warn:
        # stderr, not stdout: launchd sends stdout to the .log file where it
        # would sit in a block buffer for the life of the process. stderr stays
        # line-buffered even when redirected, so it lands immediately.
        warning(_warn, file=sys.stderr)

    # Accessory keeps the status item and dialog windows but stays out of the
    # Dock and the Cmd-Tab switcher (a framework Python would otherwise park a
    # "Python" icon in the Dock).
    AppKit.NSApplication.sharedApplication().setActivationPolicy_(
        AppKit.NSApplicationActivationPolicyAccessory
    )
    _instance_lock = acquire_single_instance(switcher.backup_dir)  # noqa: F841 - held until exit

    from ccswap.menubar_controller import MenuBarController

    if shell_name == "popover":
        from ccswap import menubar_popover as shell
    else:
        from ccswap import menubar_classic as shell
    controller = MenuBarController(
        switcher,
        switcher.backup_dir,
        switcher.backup_dir / "claude-swap.log",
        notify=shell.notify,
        alert=shell.alert,
    )
    controller.start()
    try:
        return shell.run(controller)
    finally:
        controller.stop()
