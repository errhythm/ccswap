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

from textual.app import App, SystemCommand
from textual.binding import Binding
from textual.reactive import reactive
from textual.system_commands import SystemCommandsProvider
from textual.worker import WorkerState

from ccswap import printer
from ccswap.codex import CodexAccountSwitcher
from ccswap.agy import AgyAccountSwitcher
from ccswap.i18n import localize_bindings, mark, translator
from ccswap.models import AccountSnapshot, AccountsSnapshot, org_tag_shown
from ccswap.settings import (
    AutoSwitchSettings,
    gate_thresholds,
    load_settings,
    load_ui_settings,
    parse_model_names,
    set_setting,
    unset_setting,
    with_model_threshold,
)
from ccswap import providers as registry
from ccswap.snapshot_source import account_identity
from ccswap.switcher import ClaudeAccountSwitcher
from ccswap.tui.autoview import AutoScreen
from ccswap.tui.commands import AccountCommands, static_commands
from ccswap.tui.dashboard import AccountListScreen, DashboardScreen, WatchScreen
from ccswap.tui.data import (
    PROVIDER_LABELS,
    PROVIDERS,
    capabilities,
    ActionResult,
    SnapshotSource,
    format_duration,
    mask_email,
    mask_emails_in_text,
    run_action,
)
from ccswap.tui.overview import BREAKPOINTS, HeaderBar, tabs
from ccswap.tui.modals import AddTokenModal, ConfirmModal, OutputModal, TokenForm
from ccswap.tui.theme import CSWAP_DARK, CSWAP_LIGHT
from ccswap.tui.sidebar import Sidebar
from ccswap.tui.widgets import AccountsPanel


class CswapApp(App):
    """ccswap interactive dashboard."""

    TITLE = "ccswap"
    CSS_PATH = "cswap.tcss"
    # A keyboard app: a click picks a row, it never starts a text selection.
    ALLOW_SELECT = False
    # ctrl+p: ccswap's own commands only, each calling an app method the menus
    # already use. Static ones come from get_system_commands, per-account ones
    # from AccountCommands.
    COMMANDS = {SystemCommandsProvider, AccountCommands}
    BINDINGS = [
        Binding("ctrl+t", "toggle_theme", mark("Theme")),
        # Description is rewritten to "Mask on" / "Mask off" so the footer
        # shows the live state, not just the key.
        Binding("m", "toggle_mask", mark("Mask off")),
        # The header shows this key ("★ star on GitHub *"), so the footer doesn't.
        Binding("asterisk", "star", mark("Star on GitHub"), show=False),
    ]

    # Screen classes -narrow / -medium / -wide drive the layout from TCSS.
    HORIZONTAL_BREAKPOINTS = BREAKPOINTS

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
    # Display-only. Slot numbers and aliases stay readable so a masked
    # dashboard, watch page, and auto-switch view can still be operated.
    mask_accounts: reactive[bool] = reactive(False)
    # The header's star hint: "prompt", "thanks" (a few seconds), or "".
    star_hint: reactive[str] = reactive("")

    def __init__(
        self,
        switcher: ClaudeAccountSwitcher,
        *,
        codex_switcher: CodexAccountSwitcher | None = None,
        agy_switcher: AgyAccountSwitcher | None = None,
        start: str = "dashboard",
        detected: str | None = None,
    ) -> None:
        super().__init__()
        given = {"claude": switcher, "codex": codex_switcher, "agy": agy_switcher}
        self.switchers = {
            provider: given[provider]
            if given.get(provider) is not None
            else spec.factory()
            for provider, spec in registry.PROVIDERS.items()
        }
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
            # The rest of autoswitch (weeklyThreshold, model) that gates read.
            self._auto_settings = settings
            self.threshold_pct: float | None = settings.threshold
            self._strategy_name = settings.strategy
            self._threshold_5h: float | None = settings.threshold_5h
            self._threshold_7d: float | None = settings.threshold_7d
            self._model_thresholds: str | None = settings.model_thresholds
        except Exception:
            self._auto_settings = AutoSwitchSettings()
            self.threshold_pct = None
            self._strategy_name = "best"
            self._threshold_5h = None
            self._threshold_7d = None
            self._model_thresholds = None
        try:
            ui_settings = load_ui_settings(switcher.backup_dir)
            self._theme_name = ui_settings.theme
            self._view = ui_settings.view
            self._saved_view = ui_settings.view  # Settings' row; tabs never write it
            self.mask_accounts = ui_settings.mask
            self.org_tag = ui_settings.org_tag
            self._star_setting = ui_settings.star_prompt
        except Exception:
            self._star_setting = False
            self._theme_name = "auto"
            self._view = "combined"
            self._saved_view = "combined"
            self.mask_accounts = False
            self.org_tag = "auto"

        translation = translator()
        self.gettext = translation.gettext
        self.ngettext = translation.ngettext

    def get_system_commands(self, screen):
        # No stock commands (theme picker, screenshot, ...): they would bypass
        # set_setting. "Keys" and "Quit" are in static_commands.
        for c in static_commands(self):
            yield SystemCommand(c.label, c.help, c.run, discover=c.lead)

    def get_theme_variable_defaults(self) -> dict[str, str]:
        # Our own tokens, so the stylesheet parses before cswap-* is applied.
        return {
            key: CSWAP_DARK.variables[key]
            for key in ("line", "dim", "codex", "antigravity", "track", "mark")
        }

    def switcher_for(self, provider: str):
        """Return a switcher only when its provider is explicit."""
        try:
            return self.switchers[provider]
        except KeyError:
            raise ValueError(f"Unknown provider: {provider}") from None

    def on_mount(self) -> None:
        localize_bindings(self, self.gettext)
        self._sync_star()
        self.register_theme(CSWAP_DARK)
        self.register_theme(CSWAP_LIGHT)
        resolved = self._resolved_theme()
        # We own the theme; $TEXTUAL_THEME is intentionally not honoured.
        self.theme = f"cswap-{resolved}"
        printer.set_theme(resolved)
        # Before any footer composes, so the first paint already says
        # "Mask on" when ui.mask was saved.
        self._sync_mask_binding()
        self.push_screen(DashboardScreen())
        if self._start == "watch":
            # Stacked over the dashboard so Esc lands there, not on exit.
            self.push_screen(WatchScreen())
        self.set_interval(self.POLL_INTERVAL_S, self._tick)
        self.set_interval(1.0, self._update_refresh_status)
        self._tick()
        self._maybe_ask_telemetry()

    def _maybe_ask_telemetry(self) -> None:
        """Ask once whether to send anonymous usage stats; any answer is saved."""
        from ccswap.telemetry import PROMPT, should_ask

        root = self.switcher_for("claude").backup_dir
        try:
            if not should_ask(root):
                return
        except Exception:
            return

        def answered(yes: bool | None) -> None:
            try:
                set_setting(root, "telemetry.enabled", "on" if yes else "off")
            except Exception as exc:
                self.notify(
                    self.gettext("Could not save the usage stats setting: {exc}").format(exc=exc),
                    severity="warning",
                )

        self.push_screen(
            ConfirmModal(
                self.gettext(PROMPT),
                title=self.gettext("Anonymous usage stats"),
                yes_label=self.gettext("Share"),
            ),
            answered,
        )

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
        if store_only and provider == "claude":
            source.refresh_plans()

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
                parts.append(
                    self.gettext("{label} snapshot {duration} ago").format(
                        label=label, duration=format_duration(age)
                    )
                )
            started_at = self._normal_started_at[provider]
            if self._normal_refreshing[provider] and started_at is not None:
                elapsed = now - started_at
                if elapsed >= self.POLL_INTERVAL_S:
                    parts.append(
                        self.gettext("{label} refreshing {duration}").format(
                            label=label, duration=format_duration(elapsed)
                        )
                    )
        self.refresh_status = " · ".join(parts)

    def request_refresh(
        self, provider: str | None = None, *, full: bool = False
    ) -> None:
        if full:
            for key in PROVIDERS if provider is None else (provider,):
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
                lane_label = (
                    self.gettext("store refresh")
                    if lane == "refresh-store"
                    else self.gettext("refresh")
                )
                self.notify(
                    self.gettext("{provider} {lane_label} failed: {msg}").format(
                        provider=PROVIDER_LABELS[provider],
                        lane_label=lane_label,
                        msg=msg,
                    ),
                    severity="warning",
                    timeout=6,
                )
        elif group == "action":
            self.busy = False
            self.notify(
                self.gettext("Action failed: {error}").format(error=event.worker.error),
                severity="error",
            )
        elif group == "engine":
            self.notify(
                self.gettext("Auto-switch engine stopped: {error}").format(
                    error=event.worker.error
                ),
                severity="error",
            )

    # -- mutating actions (single-flight, captured, off-thread) ---------------

    def _start_action(self, label: str, fn, *, show_output: bool = False) -> None:
        if self.busy:
            self.notify(
                self.gettext("Another action is still running"), severity="warning"
            )
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

    def _action_done(self, label: str, result: ActionResult, show_output: bool) -> None:
        self.busy = False
        self.request_refresh()
        if self.mask_accounts and result.output:
            result = replace(result, output=mask_emails_in_text(result.output))
        if not result.ok:
            self.push_screen(
                OutputModal(
                    self.gettext("{label} — failed").format(label=label), result.output
                )
            )
            return
        payload = result.payload or {}
        if "switched" in payload:
            if payload.get("switched"):
                to = payload.get("to") or {}
                email = to.get("email") or ""
                if email:
                    target = mask_email(email) if self.mask_accounts else email
                else:
                    target = self.gettext("account {number}").format(
                        number=to.get("number")
                    )
                self.notify(
                    self.gettext("Switched to {target}").format(target=target)
                    + (f"\n{payload['note']}" if payload.get("note") else ""),
                    title=self.gettext("Switch"),
                )
            else:
                reason = str(
                    payload.get("reason") or self.gettext("no switch performed")
                )
                self.notify(reason, title=self.gettext("No switch"), severity="warning")
            return
        if show_output and result.output.strip():
            self.push_screen(OutputModal(label, result.output))
        elif result.first_line:
            self.notify(result.first_line)

    # -- account operations ----------------------------------------------------

    def do_switch(self, provider: str, number: str) -> None:
        if not capabilities(provider).switch:
            return
        self._start_action(
            self.gettext("Switch {provider} to account {number}").format(
                provider=PROVIDER_LABELS[provider], number=number
            ),
            partial(self.switcher_for(provider).switch_to, number, json_output=True),
        )

    def do_switch_best(self, provider: str) -> None:
        if "best" not in capabilities(provider).strategies:
            return
        kwargs: dict = {}
        if provider == "claude":
            # like `ccswap switch --strategy`: autoswitch.model steers the pick
            models = parse_model_names(
                load_settings(self.switcher_for("claude").backup_dir).model
            )
            if models:
                kwargs.update(models=models, model_source="autoswitch.model")
        self._start_action(
            self.gettext("Switch (best)"),
            partial(
                self.switcher_for(provider).switch,
                strategy="best",
                json_output=True,
                **kwargs,
            ),
        )

    def do_toggle_disabled(self, provider: str, number: str) -> None:
        """Hold the account out of auto-rotation, or return it — reads its
        current state from the live snapshot to pick the direction."""
        if not capabilities(provider).disable:
            return
        snap = self.snapshots[provider]
        acc = next(
            (a for a in (snap.accounts if snap else ()) if a.number == number), None
        )
        if acc is None:
            return
        target = not acc.disabled
        label = (
            self.gettext("Disable account {number}")
            if target
            else self.gettext("Enable account {number}")
        )
        self._start_action(
            label.format(number=number),
            partial(self.switcher_for(provider).set_account_disabled, number, target),
        )

    def confirm_remove(self, provider: str, number: str, email: str) -> None:
        shown = mask_email(email) if self.mask_accounts and email else email
        self.push_screen(
            ConfirmModal(
                self.gettext(
                    "Remove account {number} ({shown})?\n\nIts stored credentials and config backup are deleted."
                ).format(number=number, shown=shown),
                title=self.gettext("Remove account"),
                yes_label=self.gettext("Remove"),
            ),
            partial(self._on_remove_confirm, provider, number),
        )

    def _on_remove_confirm(
        self, provider: str, number: str, confirmed: bool | None
    ) -> None:
        if confirmed:
            self._start_action(
                self.gettext("Remove account {number}").format(number=number),
                partial(
                    self.switcher_for(provider).remove_account,
                    number,
                    assume_yes=True,
                ),
            )

    def do_add_current(self, provider: str) -> None:
        self.push_screen(
            ConfirmModal(
                self.gettext(
                    "Back up the current {provider} login as a managed account?\n\nIf this account is already managed, its stored credentials are refreshed in place."
                ).format(provider=PROVIDER_LABELS[provider]),
                title=self.gettext("Add account"),
                yes_label=self.gettext("Add"),
            ),
            partial(self._on_add_confirm, provider),
        )

    def _on_add_confirm(self, provider: str, confirmed: bool | None) -> None:
        if confirmed:
            self._start_action(
                self.gettext("Add current login"),
                partial(self.switcher_for(provider).add_account),
                show_output=True,
            )

    def action_add_token(self, provider: str = "claude") -> None:
        if not capabilities(provider).add_token:
            self.notify(
                self.gettext(
                    "Codex accounts are added from the current 'codex login' session"
                ),
                severity="warning",
            )
            return
        self.push_screen(AddTokenModal(), self._on_token_form)

    def _on_token_form(self, form: TokenForm | None) -> None:
        if form is None:
            return
        run = partial(
            self._start_action,
            self.gettext("Add account from token"),
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
                    self.gettext(
                        "Slot {slot} is occupied by {occupant}. Overwrite?"
                    ).format(slot=form.slot, occupant=occupant),
                    title=self.gettext("Overwrite slot"),
                    yes_label=self.gettext("Overwrite"),
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
                return mask_email(acc.email) if self.mask_accounts else acc.email
        return None

    # -- navigation -------------------------------------------------------------

    def action_refresh_full(self) -> None:
        self.request_refresh(full=True)
        self.notify(self.gettext("Refreshing usage…"), timeout=2)

    def action_open_auto(self) -> None:
        if isinstance(self.screen, AutoScreen):
            return
        eligible = [
            provider
            for provider in PROVIDERS
            if capabilities(provider).auto_switch
            and (snapshot := self.snapshots[provider]) is not None
            and snapshot.accounts
        ]
        provider = (
            self._last_auto_provider
            if self._last_auto_provider in eligible
            else (eligible[0] if eligible else self._last_auto_provider)
        )
        self.open_auto(provider)

    def open_auto(self, provider: str) -> None:
        from ccswap.tui.settings_screen import SettingsScreen

        if not capabilities(provider).auto_switch or any(
            # Never over Settings (or a second auto view): mirrors open_settings.
            isinstance(s, (AutoScreen, SettingsScreen)) for s in self.screen_stack
        ):
            return
        self._last_auto_provider = provider
        self.push_screen(AutoScreen(provider))

    def action_open_watch(self) -> None:
        if isinstance(self.screen, WatchScreen):
            return
        self.push_screen(WatchScreen())

    def tag_shown(self, acc: AccountSnapshot) -> bool:
        """Whether ``ui.orgTag`` draws ``acc``'s org tag in any TUI account view."""
        # Compared within acc's own provider: a Codex login sharing a Claude
        # account's email is not an ambiguity the tag has to resolve.
        accounts = next(
            (snap.accounts for snap in self.snapshots.values()
             if snap is not None and acc in snap.accounts),
            (),
        )
        return org_tag_shown(self.org_tag, acc.email, [a.email for a in accounts])

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
            self.notify(
                self.gettext("Could not save theme: {exc}").format(exc=exc),
                severity="warning",
            )

    def action_toggle_theme(self) -> None:
        order = ("dark", "light", "auto")
        nxt = order[(order.index(self._theme_name) + 1) % len(order)]
        self.apply_theme(nxt)
        self.notify(self.gettext("Theme: {nxt}").format(nxt=nxt))

    # -- settings -----------------------------------------------------------

    def _refresh_accounts_panels(self) -> None:
        """Refresh panels on every mounted screen, not just the top one."""
        for screen in self.screen_stack:
            for panel in screen.query(AccountsPanel):
                panel.refresh(layout=True)

    def step_view(self, delta: int) -> None:
        """Header tabs (←/→, a click): this session only, never saved, so they
        stay a read-only act even in locked watch."""
        order = tabs()
        current = order.index(self._view) if self._view in order else 0
        self.apply_view(order[(current + delta) % len(order)], persist=False)

    def apply_view(self, name: str, *, persist: bool = True) -> None:
        """Apply the dashboard-only provider filter without refetching data.
        ``persist`` (Settings, the palette) also saves it as ``ui.view``."""
        self._view = name
        for screen in self.screen_stack:
            for header in screen.query(HeaderBar):
                header.refresh()
        provider = None if name == "combined" else name
        for screen in self.screen_stack:
            if isinstance(screen, DashboardScreen):
                panel = screen.query_one("#accounts-panel", AccountsPanel)
                panel.provider = provider
                panel.refresh(layout=True)
                for sidebar in screen.query(Sidebar):
                    sidebar.update_panels()
                # The cursor never stays on a row the new view hides.
                screen.call_after_refresh(screen._keep_cursor)
            elif isinstance(screen, AccountListScreen):
                # A view flip does not change `snapshots`, so its watcher would
                # otherwise leave this stacked list stale until the next poll.
                screen.call_after_refresh(screen._on_snapshot, self.snapshots)
        if not persist:
            return
        self._saved_view = name
        try:
            set_setting(self.switcher_for("claude").backup_dir, "ui.view", name)
        except Exception as exc:  # persistence is best-effort; never crash the UI
            self.notify(
                self.gettext("Could not save dashboard view: {exc}").format(exc=exc),
                severity="warning",
            )

    def _saved_settings(self) -> AutoSwitchSettings:
        """settings.json as the engines read it, re-parsed only when the file
        changes. A file that fails to parse keeps the last good copy."""
        import json

        root = self.switcher_for("claude").backup_dir
        path = root / "settings.json"
        try:
            stamp = path.stat().st_mtime_ns
        except OSError:
            stamp = None
        if stamp != getattr(self, "_gate_stamp", object()):
            cached = getattr(self, "_gate_cache", self._auto_settings)
            try:
                if stamp is not None:
                    json.loads(path.read_text(encoding="utf-8"))  # load_settings hides a corrupt file
                self._gate_cache = load_settings(root)
            except Exception:
                self._gate_cache = cached
            self._gate_stamp = stamp
        return self._gate_cache

    def _gate_settings(self) -> AutoSwitchSettings:
        """The gates the ticks and the sidebar show: settings.json (so CLI
        edits count), plus the auto view's session-only threshold while one
        is open (its `t` change is never saved)."""
        saved = self._saved_settings()
        if self.threshold_pct is not None and any(isinstance(s, AutoScreen) for s in self.screen_stack):
            return replace(saved, threshold=self.threshold_pct)
        return saved

    def bar_threshold(self, label: str) -> float | None:
        """The gate the engines use for one usage row (settings.gate_thresholds:
        a window's own gate, then weeklyThreshold for weekly ones, then the
        shared threshold). Codex's weekly window ("Weekly"/"wk") is the 7d gate."""
        if self.threshold_pct is None:
            return None
        key = label.lower()
        key = "7d" if key in ("weekly", "wk") else key
        return gate_thresholds(self._gate_settings()).for_label(key)

    def inherited_gate(self, label: str) -> float:
        """What a 5h / 7d / model gate falls back to while it is unset."""
        settings = self._gate_settings()
        if label == "5h":
            settings = replace(settings, threshold_5h=None)
        elif label == "7d":
            settings = replace(settings, threshold_7d=None)
        else:
            settings = replace(
                settings, model_thresholds=with_model_threshold(settings.model_thresholds, label, None)
            )
        return gate_thresholds(settings).for_label(label)

    def open_settings(self) -> None:
        """Settings, from the menu, `,` or the palette. Never over an auto view:
        its engine would not see the changes, and leaving it restores its own."""
        from ccswap.tui.settings_screen import SettingsScreen

        if any(isinstance(s, (AutoScreen, SettingsScreen)) for s in self.screen_stack):
            return
        self.push_screen(SettingsScreen())

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
            self.notify(
                self.gettext("Could not save auto-switch threshold: {exc}").format(
                    exc=exc
                ),
                severity="warning",
            )

    def apply_window_gate(self, which: str, value: float | None) -> None:
        """Persist the 5h or weekly gate. None inherits the shared threshold."""
        if which == "5h":
            self._threshold_5h = value
            dotted = "autoswitch.threshold5h"
            label = self.gettext("5h gate")
        else:
            self._threshold_7d = value
            dotted = "autoswitch.threshold7d"
            label = self.gettext("weekly gate")
        self._write_optional_setting(
            dotted, None if value is None else str(value), label
        )
        self._refresh_accounts_panels()

    def apply_fable_gate(self, value: float | None) -> None:
        """Persist Fable's gate, keeping any other model thresholds."""
        self._model_thresholds = with_model_threshold(
            self._model_thresholds, "Fable", value
        )
        self._write_optional_setting(
            "autoswitch.modelThresholds",
            self._model_thresholds,
            self.gettext("Fable gate"),
        )
        self._refresh_accounts_panels()

    def _write_optional_setting(
        self, dotted: str, value: str | None, label: str
    ) -> None:
        try:
            root = self.switcher_for("claude").backup_dir
            if value is None:
                unset_setting(root, dotted)
            else:
                set_setting(root, dotted, value)
        except Exception as exc:  # persistence is best-effort; never crash the UI
            self.notify(
                self.gettext("Could not save {label}: {exc}").format(
                    label=label, exc=exc
                ),
                severity="warning",
            )

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
            self.notify(
                self.gettext("Could not save auto-switch strategy: {exc}").format(
                    exc=exc
                ),
                severity="warning",
            )

    def apply_mask(self, on: bool) -> None:
        """Hide or show account emails and organization names.

        Cards, the watch title, and the auto-switch view follow the
        ``mask_accounts`` reactive. Open menus keep a copy of the label
        from when they were built, so the keybinding refreshes those.
        """
        self.mask_accounts = bool(on)
        self._sync_mask_binding()
        # Every path (m, the Settings row, the palette): open menus that list
        # accounts relabel too.
        for screen in self.screen_stack:
            refresh = getattr(screen, "refresh_masked_labels", None)
            if refresh is not None:
                refresh()
        try:
            set_setting(
                self.switcher_for("claude").backup_dir,
                "ui.mask",
                "on" if on else "off",
            )
        except Exception as exc:  # persistence is best-effort; never crash the UI
            self.notify(
                self.gettext("Could not save account masking: {exc}").format(exc=exc),
                severity="warning",
            )

    def _sync_mask_binding(self) -> None:
        """Keep the bottom-bar key label equal to the current mask state."""
        label = (
            self.gettext("Mask on") if self.mask_accounts else self.gettext("Mask off")
        )
        current = self._bindings.key_to_bindings.get("m", [])
        updated = [
            replace(binding, description=label, show=True)
            if binding.action == "toggle_mask"
            else binding
            for binding in current
        ]
        if not any(binding.action == "toggle_mask" for binding in updated):
            updated.append(Binding("m", "toggle_mask", label, show=True))
        self._bindings.key_to_bindings["m"] = updated
        if self._is_mounted:
            for screen in self.screen_stack:
                screen.refresh_bindings()

    def _sync_star(self) -> None:
        from ccswap.star_prompt import dismissed

        try:
            gone = dismissed(self.switcher_for("claude").backup_dir)
        except Exception:
            gone = True
        self.star_hint = "prompt" if self._star_setting and not gone else ""
        self.refresh_bindings()

    def apply_star_prompt(self, on: bool) -> None:
        """Settings › Display › Star prompt: writes ui.starPrompt."""
        self._star_setting = bool(on)
        self._sync_star()
        try:
            set_setting(self.switcher_for("claude").backup_dir, "ui.starPrompt", "on" if on else "off")
        except Exception as exc:
            self.notify(
                self.gettext("Could not save the star prompt setting: {exc}").format(exc=exc),
                severity="warning",
            )

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action == "star":
            return self.star_hint == "prompt"
        return True

    def action_star(self) -> None:
        if self.star_hint != "prompt":
            return
        self.star_hint = "thanks"
        self.refresh_bindings()
        self.run_worker(self._star_work, thread=True, exclusive=True, group="star")

    def _star_work(self) -> None:
        from ccswap.star_prompt import star

        try:
            how = star(self.switcher_for("claude").backup_dir)
        except Exception:
            how = "opened"
        if how == "opened":
            self.call_from_thread(self.notify, self.gettext("Opened GitHub in your browser"), timeout=3)
        self.call_from_thread(self.set_timer, 4.0, lambda: setattr(self, "star_hint", ""))

    def action_toggle_mask(self) -> None:
        self.apply_mask(not self.mask_accounts)
        self.notify(
            self.gettext("Account info masked")
            if self.mask_accounts
            else self.gettext("Account info visible"),
            timeout=2,
        )
