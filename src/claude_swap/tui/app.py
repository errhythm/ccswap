"""The ccswap Textual application.

Owns the snapshot poll loop and every mutating action (switch/add/remove),
so the dashboard, the auto view, and the command palette all drive the same
code paths. Blocking switcher work always runs in thread workers — the UI
loop never touches file locks, keychain subprocesses, or the network.
"""

from __future__ import annotations

import time
from dataclasses import replace
from functools import partial

from textual.app import App
from textual.binding import Binding
from textual.reactive import reactive
from textual.worker import WorkerState

from claude_swap import printer
from claude_swap.codex import CodexAccountSwitcher
from claude_swap.models import AccountsSnapshot
from claude_swap.snapshot_source import account_identity
from claude_swap.settings import load_settings, load_ui_settings, set_setting
from claude_swap.switcher import ClaudeAccountSwitcher
from claude_swap.tui.autoview import AutoScreen
from claude_swap.tui.dashboard import AccountListScreen, DashboardScreen, WatchScreen
from claude_swap.tui.data import (
    PROVIDERS,
    PROVIDER_LABELS,
    ActionResult,
    SnapshotSource,
    format_duration,
    run_action,
)
from claude_swap.tui.modals import AddTokenModal, ConfirmModal, OutputModal, TokenForm
from claude_swap.tui.theme import CSWAP_DARK, CSWAP_LIGHT
from claude_swap.tui.widgets import AccountsPanel


class CswapApp(App):
    """ccswap interactive dashboard."""

    TITLE = "ccswap"
    CSS_PATH = "cswap.tcss"
    # No command palette: actions live in the dashboard's nested menu, in
    # their own context — not in a global searchable list.
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [Binding("ctrl+t", "toggle_theme", "Theme")]

    POLL_INTERVAL_S = 3.0  # matches the old watch view's recapture cadence
    # Snapshot age stays hidden while polling is healthy (age never exceeds
    # ~POLL_INTERVAL_S); past this it surfaces as a staleness alarm. 60s also
    # keeps format_duration in whole minutes, so the note never ticks per
    # second.
    SNAPSHOT_AGE_NOTE_S = 60.0

    # Errors live beside snapshots, so an error-only publication can carry an
    # equal snapshot mapping. Always update ensures its fresh-dict assignment
    # still wakes mounted panels to replace "loading…" with the error line.
    snapshots: reactive[dict[str, AccountsSnapshot | None]] = reactive(
        dict, always_update=True
    )
    refresh_status: reactive[str] = reactive("")
    # Deliberately global: both provider switchers acquire the same lock file.
    # Separate busy flags could start two same-process actions whose distinct
    # file descriptors contend, recreating the lock-timeout/deadlock class the
    # app-level single-flight is meant to prevent.
    busy: reactive[bool] = reactive(False)

    def __init__(
        self,
        switcher: ClaudeAccountSwitcher,
        *,
        codex_switcher: CodexAccountSwitcher | None = None,
        start: str = "dashboard",
        detected: str | None = None,
    ) -> None:
        super().__init__()
        codex = codex_switcher if codex_switcher is not None else CodexAccountSwitcher()
        self.switchers = {"claude": switcher, "codex": codex}
        # Both sources live for the app's lifetime. In particular, Codex keeps
        # its usage cache in memory, so rebuilding it on a view change would
        # discard measurements.
        self.sources = {
            provider: SnapshotSource(provider_switcher)
            for provider, provider_switcher in self.switchers.items()
        }
        self._start = start  # "dashboard" | "watch" (`cswap watch`)
        self._detected = detected  # terminal background sensed pre-driver, or None
        self.snapshots = {provider: None for provider in PROVIDERS}
        self._store_only = {provider: False for provider in PROVIDERS}
        self._full_next = {provider: False for provider in PROVIDERS}
        # Refresh lanes and generations are provider-scoped: a slow Codex
        # request must neither suppress Claude fetching nor make one provider's
        # result eligible for the other's stale-result merge.
        self._normal_refreshing = {provider: False for provider in PROVIDERS}
        self._store_refreshing = {provider: False for provider in PROVIDERS}
        self._normal_started_at: dict[str, float | None] = {
            provider: None for provider in PROVIDERS
        }
        self._refresh_generation = {provider: 0 for provider in PROVIDERS}
        self._applied_generation = {provider: 0 for provider in PROVIDERS}
        self._last_refresh_error = {provider: "" for provider in PROVIDERS}
        self._last_auto_provider = "claude"
        # The auto-switch threshold, drawn as a tick on the status strip's
        # bars everywhere. Missing/invalid settings fall back to the default.
        try:
            settings = load_settings(switcher.backup_dir)
            self.threshold_pct: float | None = settings.threshold
            self._strategy_name = settings.strategy
        except Exception:
            self.threshold_pct = None
            self._strategy_name = "best"
        try:
            ui_settings = load_ui_settings(switcher.backup_dir)
            self._theme_name = ui_settings.theme
            self._view = ui_settings.view
        except Exception:
            self._theme_name = "auto"
            self._view = "combined"

    def switcher_for(self, provider: str):
        """Return a switcher only when its provider is explicit."""
        try:
            return self.switchers[provider]
        except KeyError:
            raise ValueError(f"Unknown provider: {provider}") from None

    def on_mount(self) -> None:
        self.register_theme(CSWAP_DARK)
        self.register_theme(CSWAP_LIGHT)
        resolved = self._resolved_theme()
        # We own the theme; $TEXTUAL_THEME is intentionally not honoured.
        self.theme = f"cswap-{resolved}"
        printer.set_theme(resolved)
        self.push_screen(DashboardScreen())
        if self._start == "watch":
            # Stacked over the dashboard so Esc lands there, not on exit.
            self.push_screen(WatchScreen())
        self.set_interval(self.POLL_INTERVAL_S, self._tick)
        self.set_interval(1.0, self._update_refresh_status)
        self._tick()

    # -- snapshot poll loop ---------------------------------------------------

    def _tick(self) -> None:
        """Start one eligible refresh lane per provider.

        Normal mode prefers the fetch-enabled lane. When that lane is blocked,
        the poll tick may still observe another process's store update through
        one store-only lane. Auto mode already has an engine fetching, so it
        launches only store-only snapshots.
        """
        for provider in PROVIDERS:
            if self._store_only[provider]:
                self._start_store_refresh(provider)
            elif not self._normal_refreshing[provider]:
                full = self._full_next[provider]
                self._full_next[provider] = False
                self._start_normal_refresh(provider, full=full)
            else:
                self._start_store_refresh(provider)

    def _start_normal_refresh(self, provider: str, *, full: bool) -> None:
        if self._normal_refreshing[provider]:
            return
        self._normal_refreshing[provider] = True
        self._normal_started_at[provider] = time.time()
        generation = self._next_refresh_generation(provider)
        self._update_refresh_status()
        self.run_worker(
            partial(
                self._refresh_blocking,
                provider,
                self.sources[provider],
                generation,
                "normal",
                full,
                False,
            ),
            thread=True,
            group=f"refresh-normal:{provider}",
            exit_on_error=False,
            name=f"{provider}-snapshot-refresh",
        )

    def _start_store_refresh(self, provider: str) -> None:
        if self._store_refreshing[provider]:
            return
        self._store_refreshing[provider] = True
        generation = self._next_refresh_generation(provider)
        self._update_refresh_status()
        self.run_worker(
            partial(
                self._refresh_blocking,
                provider,
                self.sources[provider],
                generation,
                "store",
                False,
                True,
            ),
            thread=True,
            group=f"refresh-store:{provider}",
            exit_on_error=False,
            name=f"{provider}-snapshot-store-refresh",
        )

    def _next_refresh_generation(self, provider: str) -> int:
        self._refresh_generation[provider] += 1
        return self._refresh_generation[provider]

    def _refresh_blocking(
        self,
        provider: str,
        source: SnapshotSource,
        generation: int,
        lane: str,
        full: bool,
        store_only: bool,
    ) -> None:
        snap = source.take(full=full, store_only=store_only)
        self.call_from_thread(self._apply_snapshot, provider, generation, lane, snap)

    def _apply_snapshot(
        self, provider: str, generation: int, lane: str, snap: AccountsSnapshot
    ) -> None:
        if lane == "normal":
            self._normal_refreshing[provider] = False
            self._normal_started_at[provider] = None
        else:
            self._store_refreshing[provider] = False
        self._last_refresh_error[provider] = ""
        current = self.snapshots[provider]
        updated = snap
        if generation >= self._applied_generation[provider]:
            self._applied_generation[provider] = generation
        elif current is not None:
            # A later-started store repaint owns account metadata, but the
            # older worker may have completed a genuinely newer provider fetch.
            # SnapshotSource has already rejected per-account regressions, so
            # merge its canonical usage rows without restoring stale metadata.
            incoming = {acc.number: acc for acc in snap.accounts}
            accounts = tuple(
                replace(acc, usage=other.usage)
                if (
                    (other := incoming.get(acc.number)) is not None
                    and account_identity(acc) == account_identity(other)
                )
                else acc
                for acc in current.accounts
            )
            updated = replace(
                current,
                accounts=accounts,
                taken_at=max(current.taken_at, snap.taken_at),
            )
        # Textual cannot observe an in-place dict mutation. Always publish a
        # fresh mapping so every panel/list watcher fires.
        self.snapshots = {**self.snapshots, provider: updated}
        self._update_refresh_status()

    def _update_refresh_status(self) -> None:
        parts: list[str] = []
        now = time.time()
        for provider in PROVIDERS:
            snapshot = self.snapshots[provider]
            label = PROVIDER_LABELS[provider]
            if snapshot is not None:
                age = max(0.0, now - snapshot.taken_at)
            else:
                age = 0.0
            if age >= self.SNAPSHOT_AGE_NOTE_S:
                parts.append(f"{label} snapshot {format_duration(age)} ago")
            started_at = self._normal_started_at[provider]
            if self._normal_refreshing[provider] and started_at is not None:
                elapsed = now - started_at
                if elapsed >= self.POLL_INTERVAL_S:
                    parts.append(f"{label} refreshing {format_duration(elapsed)}")
        self.refresh_status = " · ".join(parts)

    def request_refresh(self, provider: str | None = None, *, full: bool = False) -> None:
        if full:
            for key in (PROVIDERS if provider is None else (provider,)):
                self._full_next[key] = True
        self._tick()

    def set_store_only(self, provider: str, value: bool) -> None:
        """Auto screen: the engine fetches, the poller only reads the store."""
        self._store_only[provider] = value
        self.request_refresh(provider)

    def on_worker_state_changed(self, event) -> None:
        if event.state is not WorkerState.ERROR:
            return
        group = event.worker.group
        if group.startswith("refresh-normal:") or group.startswith("refresh-store:"):
            lane, provider = group.split(":", 1)
            if lane == "refresh-normal":
                self._normal_refreshing[provider] = False
                self._normal_started_at[provider] = None
            else:
                self._store_refreshing[provider] = False
            self._update_refresh_status()
            msg = str(event.worker.error)
            if msg != self._last_refresh_error[provider]:
                self._last_refresh_error[provider] = msg
                # Error text is part of the reactive panel state too.
                self.snapshots = dict(self.snapshots)
                lane_label = "store refresh" if lane == "refresh-store" else "refresh"
                self.notify(
                    f"{PROVIDER_LABELS[provider]} {lane_label} failed: {msg}",
                    severity="warning",
                    timeout=6,
                )
        elif group == "action":
            self.busy = False
            self.notify(f"Action failed: {event.worker.error}", severity="error")
        elif group == "engine":
            self.notify(
                f"Auto-switch engine stopped: {event.worker.error}",
                severity="error",
            )

    # -- mutating actions (single-flight, captured, off-thread) ---------------

    def _start_action(self, label: str, fn, *, show_output: bool = False) -> None:
        if self.busy:
            self.notify("Another action is still running", severity="warning")
            return
        self.busy = True
        self.run_worker(
            partial(self._action_blocking, label, fn, show_output),
            thread=True,
            group="action",
            exit_on_error=False,
            name=label,
        )

    def _action_blocking(self, label: str, fn, show_output: bool) -> None:
        result = run_action(fn)
        self.call_from_thread(self._action_done, label, result, show_output)

    def _action_done(
        self, label: str, result: ActionResult, show_output: bool
    ) -> None:
        self.busy = False
        self.request_refresh()
        if not result.ok:
            self.push_screen(OutputModal(f"{label} — failed", result.output))
            return
        payload = result.payload or {}
        if "switched" in payload:
            if payload.get("switched"):
                to = payload.get("to") or {}
                target = to.get("email") or f"account {to.get('number')}"
                self.notify(f"Switched to {target}", title="Switch")
            else:
                reason = str(payload.get("reason") or "no switch performed")
                self.notify(reason, title="No switch", severity="warning")
            return
        if show_output and result.output.strip():
            self.push_screen(OutputModal(label, result.output))
        elif result.first_line:
            self.notify(result.first_line)

    # -- account operations ----------------------------------------------------

    def do_switch(self, provider: str, number: str) -> None:
        self._start_action(
            f"Switch {PROVIDER_LABELS[provider]} to account {number}",
            partial(self.switcher_for(provider).switch_to, number, json_output=True),
        )

    def do_switch_best(self, provider: str) -> None:
        self._start_action(
            "Switch (best)" if provider == "claude" else "Switch (next)",
            partial(
                self.switcher_for(provider).switch,
                strategy="best",
                json_output=True,
            ),
        )

    def do_toggle_disabled(self, provider: str, number: str) -> None:
        """Hold the account out of auto-rotation, or return it — reads its
        current state from the live snapshot to pick the direction."""
        snap = self.snapshots[provider]
        acc = next(
            (a for a in (snap.accounts if snap else ()) if a.number == number), None
        )
        if acc is None:
            return
        target = not acc.disabled
        verb = "Disable" if target else "Enable"
        self._start_action(
            f"{verb} account {number}",
            partial(self.switcher_for(provider).set_account_disabled, number, target),
        )

    def confirm_remove(self, provider: str, number: str, email: str) -> None:
        self.push_screen(
            ConfirmModal(
                f"Remove account {number} ({email})?\n\n"
                "Its stored credentials and config backup are deleted.",
                title="Remove account",
                yes_label="Remove",
            ),
            partial(self._on_remove_confirm, provider, number),
        )

    def _on_remove_confirm(
        self, provider: str, number: str, confirmed: bool | None
    ) -> None:
        if confirmed:
            self._start_action(
                f"Remove account {number}",
                partial(
                    self.switcher_for(provider).remove_account,
                    number,
                    assume_yes=True,
                ),
            )

    def do_add_current(self, provider: str) -> None:
        self.push_screen(
            ConfirmModal(
                f"Back up the current {PROVIDER_LABELS[provider]} login as a managed account?\n\n"
                "If this account is already managed, its stored credentials "
                "are refreshed in place.",
                title="Add account",
                yes_label="Add",
            ),
            partial(self._on_add_confirm, provider),
        )

    def _on_add_confirm(self, provider: str, confirmed: bool | None) -> None:
        if confirmed:
            self._start_action(
                "Add current login",
                partial(self.switcher_for(provider).add_account),
                show_output=True,
            )

    def action_add_token(self, provider: str = "claude") -> None:
        if provider != "claude":
            self.notify(
                "Codex accounts are added from the current 'codex login' session",
                severity="warning",
            )
            return
        self.push_screen(AddTokenModal(), self._on_token_form)

    def _on_token_form(self, form: TokenForm | None) -> None:
        if form is None:
            return
        run = partial(
            self._start_action,
            "Add account from token",
            partial(
                self.switcher_for("claude").add_account_from_token,
                token=form.token,
                email=form.email,
                slot=form.slot,
                assume_yes=True,
            ),
            show_output=True,
        )
        occupant = self._slot_occupant(form.slot)
        if occupant is not None:
            self.push_screen(
                ConfirmModal(
                    f"Slot {form.slot} is occupied by {occupant}. Overwrite?",
                    title="Overwrite slot",
                    yes_label="Overwrite",
                ),
                lambda confirmed: run() if confirmed else None,
            )
        else:
            run()

    def _slot_occupant(self, slot: int | None) -> str | None:
        snapshot = self.snapshots["claude"]
        if slot is None or snapshot is None:
            return None
        # Setup-token slots are Claude slots; overlapping Codex numbers must
        # never trigger an overwrite warning here.
        for acc in snapshot.accounts:
            if acc.number == str(slot):
                return acc.email
        return None

    # -- navigation -------------------------------------------------------------

    def action_refresh_full(self) -> None:
        self.request_refresh(full=True)
        self.notify("Refreshing usage…", timeout=2)

    def action_open_auto(self) -> None:
        if isinstance(self.screen, AutoScreen):
            return
        eligible = [
            provider
            for provider in PROVIDERS
            if (snapshot := self.snapshots[provider]) is not None
            and snapshot.accounts
        ]
        provider = (
            self._last_auto_provider
            if self._last_auto_provider in eligible
            else (eligible[0] if eligible else self._last_auto_provider)
        )
        self.open_auto(provider)

    def open_auto(self, provider: str) -> None:
        if isinstance(self.screen, AutoScreen):
            return
        self._last_auto_provider = provider
        self.push_screen(AutoScreen(provider))

    def action_open_watch(self) -> None:
        if isinstance(self.screen, WatchScreen):
            return
        self.push_screen(WatchScreen())

    # -- theme --------------------------------------------------------------

    def _resolved_theme(self) -> str:
        """Concrete 'dark'/'light' for the current setting; auto → detected → dark."""
        if self._theme_name == "auto":
            return self._detected or "dark"
        return self._theme_name

    def apply_theme(self, name: str) -> None:
        """Switch the live theme: TUI + captured printer output + persistence.

        ``name`` is the setting; ``auto`` resolves against the pre-driver
        detection (never re-probes mid-session)."""
        self._theme_name = name
        resolved = self._resolved_theme()
        self.theme = f"cswap-{resolved}"
        printer.set_theme(resolved)
        try:
            set_setting(self.switcher_for("claude").backup_dir, "ui.theme", name)
        except Exception as exc:  # persistence is best-effort; never crash the UI
            self.notify(f"Could not save theme: {exc}", severity="warning")

    def action_toggle_theme(self) -> None:
        order = ("dark", "light", "auto")
        nxt = order[(order.index(self._theme_name) + 1) % len(order)]
        self.apply_theme(nxt)
        self.notify(f"Theme: {nxt}")

    # -- settings -----------------------------------------------------------

    def _refresh_accounts_panels(self) -> None:
        """Refresh panels on every mounted screen, not just the top one."""
        for screen in self.screen_stack:
            for panel in screen.query(AccountsPanel):
                panel.refresh(layout=True)

    def apply_view(self, name: str) -> None:
        """Apply the dashboard-only provider filter without refetching data."""
        self._view = name
        provider = None if name == "combined" else name
        for screen in self.screen_stack:
            if isinstance(screen, DashboardScreen):
                panel = screen.query_one("#accounts-panel", AccountsPanel)
                panel.provider = provider
                panel.refresh(layout=True)
            elif isinstance(screen, AccountListScreen):
                # A view flip does not change `snapshots`, so its watcher would
                # otherwise leave this stacked list stale until the next poll.
                screen.call_after_refresh(screen._on_snapshot, self.snapshots)
        try:
            set_setting(self.switcher_for("claude").backup_dir, "ui.view", name)
        except Exception as exc:  # persistence is best-effort; never crash the UI
            self.notify(f"Could not save dashboard view: {exc}", severity="warning")

    def apply_threshold(self, value: float) -> None:
        """Update bar ticks immediately and persist the shared auto setting."""
        self.threshold_pct = value
        self._refresh_accounts_panels()
        try:
            set_setting(
                self.switcher_for("claude").backup_dir,
                "autoswitch.threshold",
                str(value),
            )
        except Exception as exc:  # persistence is best-effort; never crash the UI
            self.notify(f"Could not save auto-switch threshold: {exc}", severity="warning")

    def apply_strategy(self, name: str) -> None:
        """Persist the selection for the next AutoScreen engine."""
        self._strategy_name = name
        try:
            set_setting(
                self.switcher_for("claude").backup_dir,
                "autoswitch.strategy",
                name,
            )
        except Exception as exc:  # persistence is best-effort; never crash the UI
            self.notify(f"Could not save auto-switch strategy: {exc}", severity="warning")
