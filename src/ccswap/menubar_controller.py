"""UI-agnostic menu bar state and actions, shared by the popover and classic shells.

Signatures and the payload shape below are frozen (menu bar phase).

Threading: shells own the timers. ``tick()`` and ``payload()`` run on the main
thread; refresh workers only rebind plain attributes and append to lock-guarded
queues, so nothing here touches AppKit. Actions run on the main thread, or (the
popover's slow ones) on one action thread at a time: ``notify``, ``alert`` and
the follow-up refresh from there queue for ``tick()`` via ``_on_main``.

Payload (``build_payload``), pushed to the popover page as JSON::

    {"schemaVersion": 1,
     "lastCheckedAt": float | None,          # epoch seconds
     "health": {"label": str, "attention": bool},
     "title": {"setting": str, "resolved": str, "text": str},
     "settings": {"style", "titleProvider", "titlePct", "showAccountName",
                  "titleScoped", "refreshInterval", "threshold", "strategy",
                  "claudeIntegrations": {"statusline": bool, "mod": bool}},
     "history": [str, ...],                  # Claude switch log lines
     "providers": [
        json_output.provider_entry(spec, section, error) + {
            "autoSwitch": {"enabled": bool, "running": bool, "failure": str | None} | None,
            "refreshing": bool,
            "addToken": bool}]}

``section.accounts[]`` rows: number, email, alias, active, disabled,
**json_output.usage_row_fields(...) (so ``windows`` and ``windowsSource``),
plus ``note`` (menubar.menu_note of the sentinel), ``plan`` (short plan name or null) and ``loginNote``
(oauth.format_login_expiry). Built from the controller's snapshots, never
from ``list_payload()`` (which fetches).
"""

from __future__ import annotations

import logging
import subprocess
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

from ccswap import oauth, subscription
from ccswap.exceptions import ClaudeSwitchError, CredentialReadError
from ccswap.json_output import provider_entry, usage_row_fields
from ccswap.menubar import (
    EMPTY_SNAPSHOT,
    REFRESH_CHOICES,  # noqa: F401 - re-exported for shells
    EngineStoppedEvent,
    MenuBarSettings,
    _adapt_snapshot,
    _rolled_scoped_window,
    _rolled_weekly_window,
    _take_worker_snapshot,
    _usage_log_key,
    active_usage_age,
    engine_exit_applies,
    format_title,
    format_usage_log,
    health_label,
    menu_note,
    parse_switch_history,
    resolve_title_provider,
    should_surface_engine_error,
)
from ccswap.settings import TITLE_PCT_CHOICES, load_settings, parse_model_names, set_setting
from ccswap.snapshot_source import SnapshotSource
from ccswap.switcher import SENTINEL_NOTES, USAGE_RELOGIN_REQUIRED

PAYLOAD_SCHEMA_VERSION = 1

Notify = Callable[[str, str, str], None]  # title, subtitle, message
Alert = Callable[[str, str], None]  # title, message

APP = "ccswap"
STYLE_LATER = ("A refresh or switch is still running, so the menu bar didn't restart. "
               "The new style applies the next time it starts.")
STYLE_CHOICES = ("popover", "classic")
REFRESH_MIN_S, REFRESH_MAX_S = 15, 3600  # same bounds as settings menubar.refreshInterval
# Settings flag that turns a provider's hosted engine on (names kept for back-compat).
AUTO_FIELDS = {"claude": "auto_switch_enabled", "codex": "codex_auto_switch_enabled"}
CLAUDE_INTEGRATIONS = {"statusline": "claude.statusline", "mod": "claude.mod"}
CLAUDE_RESTART_NOTE = "Switch takes effect within ~30s — restart Claude Code to apply immediately."


def _engine_class(provider_id: str):
    """The auto-switch engine class for a provider (lazy imports), or None."""
    if provider_id == "claude":
        from ccswap.autoswitch import AutoSwitchEngine

        return AutoSwitchEngine
    if provider_id == "codex":
        from ccswap.codex_autoswitch import CodexAutoSwitchEngine

        return CodexAutoSwitchEngine
    return None


def _restart_note(spec) -> str:
    """What to tell the user after a switch of this provider."""
    if spec.id == "claude":
        return CLAUDE_RESTART_NOTE
    if spec.id == "codex":
        from ccswap.codex import _codex_restart_hint

        return _codex_restart_hint()
    return f"Restart existing {spec.label} sessions to use the selected account."


class _Capture:
    """Hands the raw snapshot back next to the adapted one _take_worker_snapshot returns."""

    def __init__(self, source) -> None:
        self.source, self.raw = source, None

    def take(self, **kwargs):
        self.raw = self.source.take(**kwargs)
        return self.raw


def _spawn(fn, *args) -> threading.Thread:
    thread = threading.Thread(target=fn, args=args, daemon=True)
    thread.start()
    return thread


@dataclass
class ProviderState:
    """Everything the shells know about one registered provider."""

    id: str
    switcher: object | None = None
    error: str | None = None  # the switcher could not be built, or the last refresh failed
    snapshot: object | None = None  # raw models.AccountsSnapshot
    adapted: dict | None = None  # menubar._adapt_snapshot(snapshot)
    refreshing: bool = False
    engine: object | None = None  # running auto-switch engine, if any
    engine_failure: str | None = None


def _config_watch(switcher):
    """Claude: ~/.claude.json mtime, then the active email (cheap local reads)."""
    path = switcher._get_claude_config_path()

    def changed(adapted: dict) -> bool:
        current = switcher._get_current_account()
        email = current[0] if current else None
        return bool(email) and email != adapted.get("active_email")

    return path, changed


def _auth_watch(switcher):
    """Codex: auth.json mtime, then the active account number (also fires on logout)."""

    def changed(adapted: dict) -> bool:
        try:
            current = switcher.current_account_number()
        except Exception:
            return False  # unreadable auth store; keep the last good snapshot
        active = next((a[0] for a in adapted["accounts"] if a[2]), None)
        return current != active

    return switcher.auth_file, changed


# Providers with a cheap local "active account changed" signal; others are polled.
WATCHERS = {"claude": _config_watch, "codex": _auth_watch}


class MenuBarController:
    """State per provider, polling, auto-switch engines, notifications, actions."""

    def __init__(
        self,
        claude_switcher,
        backup_root: Path,
        log_path: Path,
        *,
        notify: Notify,
        alert: Alert,
        providers: dict | None = None,  # default: ccswap.providers.PROVIDERS
        on_interval_changed: Callable[[int], None] | None = None,
    ) -> None:
        if providers is None:
            from ccswap.providers import PROVIDERS as providers
        self.providers = providers
        self.backup_root = backup_root
        self.log_path = log_path
        # AppKit is main-thread only: from an action thread these queue for tick()
        self.notify = lambda *a: self._on_main(notify, *a)
        self.alert = lambda *a: self._on_main(alert, *a)
        self.on_interval_changed = on_interval_changed
        self.last_note: str | None = None  # outcome of the last add_login, for the popover reply
        self.last_switch_changed = True  # False when the last rotate kept the same account
        self._logger = getattr(claude_switcher, "_logger", None) or logging.getLogger("ccswap")
        self.settings = MenuBarSettings.load(backup_root)
        self.states: dict[str, ProviderState] = {}
        self._sources: dict[str, SnapshotSource] = {}
        self._watch: dict[str, tuple] = {}  # pid -> (path, changed(adapted), last mtime)
        self._lock = threading.Lock()
        self._results: list = []  # worker -> main: (pid, snapshot | None, error | None)
        self._events: list = []  # engine threads -> main: (pid, event)
        self._calls: list = []  # action thread -> main: (fn, args), run by tick()
        self._errors_surfaced: dict[str, dict[str, float]] = {}
        self._last_usage_log: dict = {}  # (provider, account num) -> last-logged key
        self._checked_at: float | None = None
        self._last_health: tuple[str, bool] | None = None
        self._claude_state: dict[str, bool | None] = {k: None for k in CLAUDE_INTEGRATIONS.values()}
        self._claude_busy = False
        self._claude_note: tuple[bool, str] | None = None
        self._claude_dirty = False
        self._spawn = _spawn  # tests swap in a synchronous runner
        self._workers: list[threading.Thread] = []  # refresh and integration threads
        self._engine_threads: dict[str, threading.Thread | None] = {}
        for pid, spec in providers.items():
            self.states[pid] = self._build_state(pid, spec, claude_switcher)

    def _build_state(self, pid, spec, claude_switcher) -> ProviderState:
        state = ProviderState(pid)
        try:
            state.switcher = claude_switcher if pid == "claude" else spec.factory()
            self._sources[pid] = SnapshotSource(state.switcher)
            if pid in WATCHERS:
                path, changed = WATCHERS[pid](state.switcher)
                self._watch[pid] = (path, changed, self._mtime(path))
        except Exception as e:  # one provider failing must not take the others down
            self._logger.warning("%s unavailable: %s", spec.label, e)
            state.switcher, state.error = None, str(e)
            self._sources.pop(pid, None)
            self._watch.pop(pid, None)
        return state

    @staticmethod
    def _mtime(path) -> float:
        try:
            return Path(path).stat().st_mtime
        except OSError:
            return 0.0

    # ---- lifecycle ------------------------------------------------------------
    def start(self) -> None:
        """First refresh, then start the engines the settings enable."""
        self._start_claude_work()
        self.refresh()
        for pid in self.states:
            if self._auto_enabled(pid):
                self._start_engine(pid)

    def stop(self, wait: float = 0) -> bool:
        """Stop every engine (quit, or before a style re-exec).

        ``wait`` seconds bounds how long to join the engine threads; a re-exec
        passes one so a switch in progress finishes before the process is replaced.
        """
        threads = [t for t in self._engine_threads.values() if t is not None]
        for pid in self.states:
            self._stop_engine(pid)
        deadline = time.monotonic() + wait
        for thread in threads:
            thread.join(max(0.0, deadline - time.monotonic()))
        return not any(t.is_alive() for t in threads)

    def _track(self, thread) -> None:
        if isinstance(thread, threading.Thread):
            self._workers = [t for t in self._workers if t.is_alive()] + [thread]

    def quiesce(self, wait: float = 60) -> bool:
        """Before a re-exec: True once no thread can still be writing
        credentials. Refresh and integration workers go first (one may be
        between spending a refresh token and saving its replacement); only
        then are the engines stopped and joined. False means something is
        still running and the process must not be replaced."""
        deadline = time.monotonic() + wait
        for thread in self._workers:
            thread.join(max(0.0, deadline - time.monotonic()))
        if any(t.is_alive() for t in self._workers):
            return False  # engines untouched: the menu bar carries on as it was
        return self.stop(wait=max(0.0, deadline - time.monotonic()))

    # ---- refresh plumbing -----------------------------------------------------
    def refresh(self, provider_id: str | None = None, full: bool = False) -> None:
        """Start a background refresh for one provider (None = all), one worker each."""
        for pid in [provider_id] if provider_id else list(self.states):
            state = self.states.get(pid)
            if state is None or state.switcher is None or state.refreshing:
                continue  # in-flight guard: a provider's pacing state has one writer
            state.refreshing = True
            self._track(self._spawn(self._worker, pid, full))

    def _worker(self, pid: str, full: bool) -> None:
        # Worker thread: rebinds plain attributes and queues the result; the main
        # thread applies it in tick(). Store-only while this provider's engine
        # runs (it paces all fetching); an explicit full refresh still runs a pass.
        state = self.states[pid]
        store_only = False
        try:
            try:
                capture = _Capture(self._sources[pid])
                snap, store_only = _take_worker_snapshot(
                    capture, full=full, has_engine=state.engine is not None
                )
                self._log_usage(snap, pid)
                result = (pid, capture.raw, snap, None)
            except Exception as e:
                # Keep the last good snapshot rather than blanking the provider.
                self._logger.debug("menubar %s snapshot failed", pid, exc_info=True)
                result = (pid, None, None, f"{type(e).__name__}: {e}")
            with self._lock:
                self._results.append(result)
            if result[3] is None and store_only and self.providers[pid].capabilities.plans:
                self._sources[pid].refresh_plans()  # store-only reads never fetch plans
        finally:
            state.refreshing = False

    def _log_usage(self, snap: dict, pid: str) -> None:
        """Log each account's session/weekly limits when they change (de-duped)."""
        for num, email, _active, _display, last_good, *_rest in snap["accounts"]:
            key = _usage_log_key(last_good)
            dedupe = (pid, num)
            if key == (None, None) or self._last_usage_log.get(dedupe) == key:
                continue
            line = format_usage_log(email, last_good)
            if line:
                self._logger.info(line)
                self._last_usage_log[dedupe] = key

    # ---- main-thread tick -----------------------------------------------------
    def tick(self) -> bool:
        """Main thread, about once a second. True when the shell should repaint."""
        with self._lock:
            calls, self._calls = self._calls, []
        for fn, args in calls:
            try:  # one failing call must not drop the rest (an action's reply among them)
                fn(*args)
            except Exception:
                self._logger.exception("menubar main-thread call failed")
        changed = self._apply_results() or bool(calls)
        note, self._claude_note = self._claude_note, None
        if note is not None:
            ok, message = note
            if ok:
                self.notify(APP, "Claude Code", message)
            else:
                self.alert(APP, message)
            changed = True
        if self._claude_dirty:  # a Claude integration check finished
            self._claude_dirty = False
            changed = True
        for pid in list(self._watch):
            self._detect_active_change(pid)
        changed |= self._drain_events()
        health = self.health()
        if health != self._last_health:
            self._last_health, changed = health, True
        return changed

    def _apply_results(self) -> bool:
        with self._lock:
            results, self._results = self._results, []
        for pid, raw, snap, error in results:
            state = self.states[pid]
            state.error = error
            if error is None:
                state.snapshot, state.adapted = raw, snap
                self._checked_at = time.time()
        return bool(results)

    def _on_main(self, fn, *args) -> None:
        """Call now on the main thread; from any other thread, queue it for tick()."""
        if threading.current_thread() is threading.main_thread():
            fn(*args)
            return
        with self._lock:
            self._calls.append((fn, args))

    def _detect_active_change(self, pid: str) -> None:
        # Reflect switches from any source within ~1s, refreshing only this
        # provider. Gated on the file's mtime (a stat) so nothing is parsed per tick.
        state = self.states[pid]
        if state.refreshing or state.switcher is None:
            return
        path, changed, last = self._watch[pid]
        mtime = self._mtime(path)
        if mtime == 0.0 or mtime == last:
            return
        self._watch[pid] = (path, changed, mtime)
        if changed(state.adapted or EMPTY_SNAPSHOT):
            self.refresh(pid)

    def health(self) -> tuple[str, bool]:
        claude = self.states.get("claude")
        adapted = (claude.adapted if claude else None) or EMPTY_SNAPSHOT
        return health_label(
            auto_enabled=self._auto_enabled("claude"),
            engine_running=claude is not None and claude.engine is not None,
            usage_age=active_usage_age(adapted),
            refresh_interval=self.settings.refresh_interval,
        )

    def title_provider(self) -> str:
        """menubar.resolve_title_provider over the active accounts."""
        actives = {
            pid: s.adapted["active_usage"]
            for pid, s in self.states.items()
            if s.adapted and s.adapted["active_email"] is not None
        }
        return resolve_title_provider(self.settings.title_provider, actives)

    def title(self, icon: bool = True) -> str:
        pid = self.title_provider()
        state = self.states.get(pid)
        adapted = (state.adapted if state else None) or EMPTY_SNAPSHOT
        return format_title(
            adapted["active_email"],
            adapted["active_usage"],
            self.settings,
            alias=adapted.get("active_alias"),
            attention=self.health()[1],
            icon=icon,
        )

    def title_label(self) -> str:
        """Name of the provider the title shows, for the status item's tooltip."""
        spec = self.providers.get(self.title_provider())
        return spec.label if spec else "ccswap"

    def history(self) -> list[str]:
        try:
            text = self.log_path.read_text(encoding="utf-8")
        except OSError:
            text = ""
        return parse_switch_history(text)

    def payload(self) -> dict:
        return build_payload(self)

    def strategy(self) -> str:
        """Auto-switch strategy from core settings ("best" when unreadable)."""
        try:
            return load_settings(self.backup_root).strategy
        except Exception:
            return "best"

    def threshold(self) -> int:
        """Current auto-switch threshold from core settings (0 when unreadable)."""
        try:
            return int(load_settings(self.backup_root).threshold)
        except Exception:
            return 0

    def restart_note(self, provider_id: str) -> str:
        return _restart_note(self.providers[provider_id])

    # ---- auto-switch engines --------------------------------------------------
    def _auto_enabled(self, pid: str) -> bool:
        field = AUTO_FIELDS.get(pid)
        return bool(field and getattr(self.settings, field, False))

    def _supports_engine(self, pid: str) -> bool:
        return (
            pid in self.providers
            and self.providers[pid].capabilities.auto_switch
            and pid in AUTO_FIELDS
            and self.states[pid].switcher is not None
        )

    def _start_engine(self, pid: str) -> None:
        state = self.states[pid]
        if state.engine is not None or not self._supports_engine(pid):
            return
        label = self.providers[pid].label
        try:
            engine = _engine_class(pid)(
                state.switcher,
                load_settings(self.backup_root),
                lambda ev, pid=pid: self._on_engine_event(pid, ev),
                dry_run=False,
            )
        except Exception as e:  # never let a bad start crash the menu bar
            self._logger.warning("%s auto-switch engine failed to start: %s", label, e)
            self.notify(APP, f"{label} auto-switch failed to start", str(e))
            state.engine_failure = str(e)
            return
        state.engine, state.engine_failure = engine, None
        self._engine_threads[pid] = self._spawn(self._run_engine, pid, engine)

    def _run_engine(self, pid: str, engine) -> None:
        # Engine thread. Whatever ends the loop, tell the main thread which
        # engine ended; it decides whether that is still the hosted one.
        error = None
        try:
            engine.run_loop()
        except Exception as e:
            error = f"{type(e).__name__}: {e}"
            self._logger.debug("%s auto-switch engine crashed", pid, exc_info=True)
        finally:
            self._on_engine_event(pid, EngineStoppedEvent(engine, error))

    def _stop_engine(self, pid: str) -> None:
        state = self.states[pid]
        if state.engine is not None:
            state.engine.stop()
            state.engine = None
        state.engine_failure = None

    def _restart_running_engines(self) -> None:
        for pid, state in self.states.items():
            if state.engine is not None:
                self._stop_engine(pid)
                self._start_engine(pid)

    def _on_engine_event(self, pid: str, event) -> None:
        with self._lock:  # engine thread; must not raise
            self._events.append((pid, event))

    def _drain_events(self) -> bool:
        with self._lock:
            events, self._events = self._events, []
        changed = False
        for pid, ev in events:
            state, spec = self.states[pid], self.providers[pid]
            own = "" if pid == "claude" else f"{spec.label} "  # Claude keeps its plain titles
            kind = ev.kind
            if kind == "switch" and not getattr(ev, "dry_run", False):
                title = "Auto-switched account" if not own else f"{own}auto-switched account"
                self.notify(APP, title, ev.human())
                self.refresh(pid)  # reflect the switch promptly
            elif kind == "all-exhausted":
                title = f"All {own}accounts exhausted"
                self.notify(APP, title, ev.human())
            elif kind == "account-quarantined":
                self.notify(APP, "Account quarantined", ev.human())
            elif kind == "waste-warning":
                self.notify(APP, "Quota about to go unused", ev.human())
            elif kind == "config-warning":
                self.notify(APP, "Configuration warning", ev.human())
            elif kind == "error":
                # A failed tick; the engine retries itself, so say it once per
                # distinct message, then stay quiet for a while.
                surfaced = self._errors_surfaced.setdefault(pid, {})
                if should_surface_engine_error(ev, surfaced, time.monotonic()):
                    self._logger.warning("%sauto-switch %s", own, ev.human())
                    self.notify(APP, f"{own}auto-switch error" if own else "Auto-switch error", ev.human())
            elif kind == "engine-stopped":
                if engine_exit_applies(state.engine, ev.engine):
                    state.engine, state.engine_failure = None, ev.human()
                    self._logger.warning(
                        "%sauto-switch stopped (%s); usage keeps showing but it will "
                        "not switch until restarted", own, state.engine_failure,
                    )
                    self.notify(
                        APP,
                        f"{own}auto-switch stopped" if own else "Auto-switch stopped",
                        f"{state.engine_failure}. Open the menu to restart it.",
                    )
                    self.refresh(pid)  # display fetching resumes on its own
            else:
                continue
            changed = True
        return changed

    # ---- Claude Code integrations (statusline, mod) ---------------------------
    def _start_claude_work(self, key: str | None = None, on: bool = False) -> None:
        self._claude_busy = True
        self._track(self._spawn(self._claude_work, key, on))

    def _claude_work(self, key, on) -> None:
        from ccswap.claude_integration import is_enabled, set_enabled

        try:
            if key is not None:
                try:
                    self._claude_note = (True, set_enabled(self.backup_root, key, on))
                except Exception as e:
                    self._claude_note = (False, f"Couldn't change {key}: {e}")
            self._claude_state = {k: is_enabled(k) for k in self._claude_state}
        finally:
            self._claude_busy = False
            self._claude_dirty = True

    # ---- actions (main thread, or the popover's action thread) ----------------
    def _guard(self, fn) -> bool:
        """Run a switcher action, surfacing ClaudeSwitchError via an alert."""
        try:
            fn()
            return True
        except ClaudeSwitchError as e:
            self.alert(APP, str(e))
            return False

    def _usable(self, pid: str, allowed=True):
        """The provider's switcher when it exists and the capability holds."""
        state = self.states.get(pid)
        if state is None or pid not in self.providers or not allowed:
            return None
        return state.switcher

    def _done(self, pid: str) -> bool:
        self._on_main(self.refresh, pid)  # refresh()'s in-flight guard has one writer: main
        return True

    def switch_to(self, provider_id: str, number: int) -> bool:
        spec = self.providers.get(provider_id)
        sw = self._usable(provider_id, spec and spec.capabilities.switch)
        if sw is None or not self._guard(lambda: sw.switch_to(str(number))):
            return False
        self._notified_switch(spec)
        return self._done(provider_id)

    def rotate(self, provider_id: str, strategy: str | None) -> bool:
        spec = self.providers.get(provider_id)
        caps = spec.capabilities if spec else None
        ok = caps is not None and caps.switch and caps.strategies and (
            strategy is None or strategy in caps.strategies
        )
        sw = self._usable(provider_id, ok)
        if sw is None:
            return False
        kwargs = {"strategy": strategy}
        if provider_id == "claude" and strategy is not None:
            # like `ccswap switch --strategy`: autoswitch.model steers the usage-aware pick
            models = parse_model_names(load_settings(self.backup_root).model)
            if models:
                kwargs.update(models=models, model_source="autoswitch.model")
        self.last_note = None
        before = self._active_number(sw)
        if not self._guard(lambda: sw.switch(**kwargs)):
            return False
        after = self._active_number(sw)
        self.last_switch_changed = before is None or after != before
        if self.last_switch_changed:
            self._notified_switch(spec)
        else:
            self.last_note = f"{spec.label}: already on the best account ({after})"
        return self._done(provider_id)

    def _notified_switch(self, spec) -> None:
        title = "Account switched" if spec.id == "claude" else f"{spec.label} account switched"
        self.notify(APP, title, _restart_note(spec))

    def set_disabled(self, provider_id: str, number: int, disabled: bool) -> bool:
        spec = self.providers.get(provider_id)
        sw = self._usable(provider_id, spec and spec.capabilities.disable)
        if sw is None or not self._guard(lambda: sw.set_account_disabled(str(number), bool(disabled))):
            return False
        return self._done(provider_id)

    def remove(self, provider_id: str, number: int) -> bool:
        sw = self._usable(provider_id)
        if sw is None or not self._guard(lambda: sw.remove_account(str(number), assume_yes=True)):
            return False
        return self._done(provider_id)

    def add_login(self, provider_id: str) -> bool:
        """``ccswap add`` (or ``ccswap <provider> add``) for the provider's current login.

        The switcher reports its outcome by printing, which a menu bar never
        shows, so compare the account list before and after and say what happened.
        """
        self.last_note = None
        sw = self._usable(provider_id)
        if sw is None:
            return False
        before = self._accounts_now(sw)
        if not self._guard(sw.add_account):
            return False
        after = self._accounts_now(sw)
        label = self.providers[provider_id].label
        added = [n for n in after if n not in before]
        said = None
        if added:
            said = (f"{label}: account {added[0]} added", after[added[0]])
        else:
            active = self._active_number(sw)
            if active in after:
                said = (f"{label}: already account {active}",
                        f"Saved login refreshed for {after[active]}. "
                        f"To add another account, sign in to it in {label} first.")
        if said:
            self.notify(APP, *said)
        # the popover shows this inline: a notification can go unseen or be blocked
        self.last_note = ". ".join(said) if said else None
        return self._done(provider_id)

    @staticmethod
    def _accounts_now(sw) -> dict[str, str]:
        """Number -> email from the local store, without touching the network."""
        try:
            snap = sw.accounts_snapshot(fetch=set())
        except Exception:  # noqa: BLE001 - only used to word the notification
            return {}
        return {acc.number: acc.email for acc in snap.accounts}

    @staticmethod
    def _active_number(sw) -> str | None:
        try:
            return sw.accounts_snapshot(fetch=set()).active_number
        except Exception:  # noqa: BLE001
            return None

    def add_token(self, email: str, token: str) -> bool:
        """Add a Claude account from a setup-token; the token is never logged."""
        spec = self.providers.get("claude")
        sw = self._usable("claude", spec and spec.capabilities.add_token)
        if sw is None or not hasattr(sw, "add_account_from_token"):
            return False
        if not self._guard(lambda: sw.add_account_from_token(
            token=token.strip(), email=email.strip() or None, slot=None
        )):
            return False
        return self._done("claude")

    def refresh_creds(self) -> bool:
        sw = self._usable("claude")
        if sw is None:
            return False
        if sw._get_current_account() is None:
            self.alert(APP, "No active Claude Code login detected. Log in first.")
            return False
        try:
            sw.add_account(slot=None)
        except CredentialReadError:
            # The Keychain did not answer (locked, or the security call timed
            # out); relaunching from a Terminal is not a fix.
            self.alert(
                APP,
                "Couldn't read the active credential: the Keychain didn't answer "
                "(locked, or the security call timed out). Unlock it or try again "
                "in a moment; `ccswap add` in a Terminal shows the full error.",
            )
            return False
        except ClaudeSwitchError as e:
            self.alert(APP, str(e))
            return False
        return self._done("claude")

    def set_auto(self, provider_id: str, enabled: bool) -> bool:
        if provider_id not in self.states or not self._supports_engine(provider_id):
            return False
        if not self._save_setting(AUTO_FIELDS[provider_id], bool(enabled)):
            return False
        if enabled:
            self._start_engine(provider_id)
        else:
            self._stop_engine(provider_id)
        return True

    def restart_engine(self, provider_id: str) -> bool:
        """Start the engine again (the health row's click, or after a failure)."""
        if provider_id not in self.states or not self._supports_engine(provider_id):
            return False
        self._stop_engine(provider_id)
        self._start_engine(provider_id)
        return True

    def set_threshold(self, pct: float) -> bool:
        try:
            set_setting(self.backup_root, "autoswitch.threshold", str(int(pct)))
        except Exception as e:
            self.alert(APP, f"Couldn't set threshold: {e}")
            return False
        self._restart_running_engines()  # the shared setting applies immediately
        return True

    def set_display(self, field: str, value) -> bool:
        """title_pct, show_account_name, title_scoped, refresh_interval, title_provider, style."""
        is_int = isinstance(value, int) and not isinstance(value, bool)
        valid = {
            "title_pct": lambda: value in TITLE_PCT_CHOICES,
            "show_account_name": lambda: isinstance(value, bool),
            "title_scoped": lambda: isinstance(value, bool),
            "refresh_interval": lambda: is_int and REFRESH_MIN_S <= value <= REFRESH_MAX_S,
            "title_provider": lambda: value == "auto" or value in self.providers,
            "style": lambda: value in STYLE_CHOICES,
        }.get(field)
        if valid is None or not valid():
            return False  # a bad value from a shell is a bug, not user input to alert about
        if not self._save_setting(field, value):
            return False
        if field == "refresh_interval" and self.on_interval_changed:
            self.on_interval_changed(value)
        return True

    def _save_setting(self, field: str, value) -> bool:
        """Persist first, then apply in memory, so the file and the menu never disagree.

        The file is re-read so a key changed with `ccswap config set` since
        launch keeps that value instead of the copy loaded at start."""
        try:
            candidate = replace(MenuBarSettings.load(self.backup_root), **{field: value})
            candidate.save(self.backup_root)
        except (ClaudeSwitchError, OSError) as e:
            self.alert(APP, f"Couldn't save the setting: {e}")
            return False
        setattr(self.settings, field, value)
        return True

    def toggle_claude_integration(self, key: str) -> bool:
        key = CLAUDE_INTEGRATIONS.get(key, key)
        if key not in self._claude_state or self._claude_busy:
            return False
        if self._claude_state[key] is None:
            return False  # state unknown (still checking, or the check failed): don't guess
        self._start_claude_work(key, not self._claude_state[key])
        return True

    def open_log(self) -> None:
        # Reveal the log in Finder (-R); if it doesn't exist yet, open the dir.
        target = self.log_path if self.log_path.exists() else self.log_path.parent
        subprocess.run(["open", "-R", str(target)], check=False)


def _rolled_usage(usage: dict | None, now: float) -> dict | None:
    """A usage dict with passed weekly resets rolled forward (5h windows untouched)."""
    if not isinstance(usage, dict):
        return usage
    out = dict(usage)
    for key in ("seven_day", "weekly"):
        if key in out:
            out[key] = _rolled_weekly_window(out[key], now)
    if "scoped" in out:
        out["scoped"] = [_rolled_scoped_window(w, now) for w in out["scoped"]]
    return out


def _plan(acc, provider_label: str) -> str | None:
    """Short plan name for the card chip: Claude's stored plan record, else a
    label like "Codex Plus" with the provider name dropped."""
    record = acc.subscription_plan
    if isinstance(record, dict) and isinstance(record.get("plan"), dict):
        return subscription.plan_label(record["plan"])
    org = acc.org_name or ""
    if org.startswith(provider_label + " "):
        return org[len(provider_label) + 1:] or None
    return None


def _account_row(acc, now: float, provider_label: str = "") -> dict:
    entry = acc.usage
    quarantined = entry.sentinel == USAGE_RELOGIN_REQUIRED
    return {
        "number": int(acc.number),
        "email": acc.email,
        "alias": acc.alias or None,
        "active": acc.is_active,
        "disabled": acc.disabled,
        **usage_row_fields(
            _rolled_usage(entry.decision_value(), now),
            usage_fetched_at=entry.fetched_at,
            usage_age_s=entry.age_s,
            last_good_usage=_rolled_usage(entry.last_good, now),
            last_error=entry.last_error,
            backoff_until=entry.backoff_until if entry.in_backoff(now) else None,
        ),
        "note": menu_note(SENTINEL_NOTES.get(entry.sentinel, entry.sentinel)) if entry.sentinel else None,
        "loginNote": oauth.format_login_expiry(acc.login_expires_at, quarantined, now),
        "plan": _plan(acc, provider_label),
    }


def build_payload(controller: MenuBarController) -> dict:
    """The popover's JSON payload (shape in the module docstring)."""
    now = time.time()
    label, attention = controller.health()
    settings = controller.settings
    providers = []
    for pid, spec in controller.providers.items():
        state = controller.states[pid]
        snap = state.snapshot
        section = None
        if snap is not None:
            section = {
                "activeAccountNumber": int(snap.active_number) if snap.active_number else None,
                "accounts": [_account_row(acc, now, spec.label) for acc in snap.accounts],
            }
        caps = spec.capabilities
        entry = provider_entry(spec, section, state.error)
        entry["autoSwitch"] = (
            {
                "enabled": controller._auto_enabled(pid),
                "running": state.engine is not None,
                "failure": state.engine_failure,
            }
            if caps.auto_switch
            else None
        )
        entry["refreshing"] = state.refreshing
        entry["addToken"] = bool(
            caps.add_token and hasattr(state.switcher, "add_account_from_token")
        )
        providers.append(entry)
    return {
        "schemaVersion": PAYLOAD_SCHEMA_VERSION,
        "lastCheckedAt": controller._checked_at,
        "health": {"label": label, "attention": attention},
        "title": {
            "setting": settings.title_provider,
            "resolved": controller.title_provider(),
            "text": controller.title(icon=False),
        },
        "settings": {
            "style": settings.style,
            "titleProvider": settings.title_provider,
            "titlePct": settings.title_pct,
            "showAccountName": settings.show_account_name,
            "titleScoped": settings.title_scoped,
            "refreshInterval": settings.refresh_interval,
            "threshold": controller.threshold(),
            "strategy": controller.strategy(),
            "claudeIntegrations": {
                short: bool(controller._claude_state.get(key))
                for short, key in CLAUDE_INTEGRATIONS.items()
            },
        },
        "history": controller.history(),
        "providers": providers,
    }
