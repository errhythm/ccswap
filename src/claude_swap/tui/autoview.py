"""Live auto-switch screen: the active provider's engine, visualized.

Runs :class:`AutoSwitchEngine` in a thread worker and renders its typed
events. Opens in **dry-run** — opening a view must never start switching
accounts on its own; going live is an explicit, confirmed action. The
engine's own state file semantics make it safe to run alongside an external
``ccswap auto`` process.

The active account's full card sits on top (same widget as the dashboard's
panel, with the threshold tick); this screen adds the engine badge, the
ranked switch candidates, and the decision log. While it is up, the app's
snapshot poller runs store-only: the engine is the only fetcher.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import Footer, Label, RichLog, Static

from claude_swap.autoswitch import (
    AutoSwitchEngine,
    AutoSwitchEvent,
    binding_pct,
    pct_label,
)
from claude_swap.codex_autoswitch import CodexAutoSwitchEngine
from claude_swap.i18n import localize_bindings, mark
from claude_swap.models import AccountsSnapshot
from claude_swap.settings import (
    SETTING_SPECS,
    effective_model_names,
    gate_thresholds,
    load_settings,
    parse_model_names,
    parse_model_thresholds,
    parse_window_selection,
)
from claude_swap.tui import data
from claude_swap.tui.modals import ConfirmModal
from claude_swap.tui.theme import Palette
from claude_swap.tui.widgets import AccountsPanel

if TYPE_CHECKING:
    from claude_swap.tui.app import CswapApp

_EVENT_ROLES = {
    "switch": "accent",
    "error": "sev_warn",
    "account-quarantined": "sev_warn",
    "all-exhausted": "sev_crit",
}
_QUIET_KINDS = {"poll", "no-switch", "sleep", "account-unquarantined"}


def event_text(
    event: AutoSwitchEvent, *, palette: Palette = Palette.DARK, mask: bool = False
) -> Text:
    """Log line for one engine event, styled like the CLI's human renderer.

    ``mask`` redacts mailboxes in the human line only. The engine's own
    ``human()`` stays intact for the CLI.
    """
    role = _EVENT_ROLES.get(event.kind)
    if role is not None:
        style = getattr(palette, role)
    else:
        style = palette.muted if event.kind in _QUIET_KINDS else palette.foreground
    body = data.mask_emails_in_text(event.human()) if mask else event.human()
    text = Text()
    text.append(f"{data.clock_stamp()}  ", style=palette.muted)
    text.append(body, style=style)
    return text


class AutoScreen(Screen):
    BINDINGS = [
        Binding("l", "toggle_live", mark("Go live / dry-run")),
        Binding("t", "adjust_threshold", mark("Threshold")),
        Binding("left", "threshold_step(-1)", mark("-1%")),
        Binding("right", "threshold_step(1)", mark("+1%")),
        Binding("enter", "adjust_done", mark("Done")),
        Binding("escape,q", "back", mark("Back")),
    ]

    app: "CswapApp"

    def __init__(self, provider: str) -> None:
        super().__init__()
        self.provider = provider
        self._engine: AutoSwitchEngine | CodexAutoSwitchEngine | None = None
        self._settings = None
        # ``t`` opens ThresholdAdjustModal. The shared threshold stays
        # session-only (never written; unmount restores the file value).
        # 5h, weekly, and Fable are real gates and are saved as they change.
        # ``_configured_threshold`` is the mount-time file value the screen
        # reverts to on exit; ``_entry_*`` is the value when adjust mode was
        # entered (wake/log only on a net change).
        self._adjusting = False
        self._adjust_index = 0
        self._configured_threshold: float | None = None
        self._entry_threshold: float | None = None
        self._entry_gates: tuple[float | None, float | None, str | None] = (
            None,
            None,
            None,
        )
        # Notes and events, so toggling the mask can redraw the log. The
        # engine does not keep a transcript of its own.
        self._log_items: list[tuple[str, str | AutoSwitchEvent]] = []

    def compose(self) -> ComposeResult:
        yield AccountsPanel(
            show_minis=False,
            provider=self.provider,
            id="auto-active-panel",
        )
        with Vertical(id="auto-top"):
            with Horizontal(id="auto-title-row"):
                yield Static(
                    self.app.gettext(" DRY-RUN "), id="mode-badge", classes="dry"
                )
                yield Static("", id="auto-summary")
            yield Static("", id="candidates")
        yield RichLog(id="event-log", highlight=False, markup=False, wrap=True)
        yield Footer()

    # -- lifecycle ----------------------------------------------------------

    def on_mount(self) -> None:
        localize_bindings(self, self.app.gettext)
        self.app.set_store_only(self.provider, True)
        switcher = self.app.switcher_for(self.provider)
        self._settings = load_settings(switcher.backup_dir)
        # The bar tick everywhere reads app.threshold_pct, loaded once at app
        # startup — sync it to the fresh file value so bars and engine agree,
        # and remember that value: unmount restores it (only the session
        # adjustment reverts, not this correction).
        self._configured_threshold = self._settings.threshold
        self.app.threshold_pct = self._settings.threshold
        self._update_summary()
        self.watch(self.app, "snapshots", self._on_snapshots)
        self.watch(self.app, "theme", self._on_theme_change)
        self.watch(self.app, "mask_accounts", self._on_mask_change)
        self._start_engine(dry_run=True)

    def on_unmount(self) -> None:
        if self._engine is not None:
            self._engine.stop()
        # A session threshold must not outlive the engine it steered: unpin
        # the poll planner and put the bar tick back on the file value.
        self.app.switcher_for(self.provider).clear_poll_policy_inputs()
        if self._configured_threshold is not None:
            self.app.threshold_pct = self._configured_threshold
        self.app.set_store_only(self.provider, False)

    def _on_theme_change(self, _theme: str) -> None:
        self._update_summary()
        self._update_badge()
        snap = self.app.snapshots[self.provider]
        if snap is not None:
            self._on_snapshot(snap)

    def action_back(self) -> None:
        if self._adjusting:
            self._end_adjust()
            return
        self.app.pop_screen()

    # -- threshold adjust mode ------------------------------------------------

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        if action in ("threshold_step", "adjust_done") and not self._adjusting:
            return False  # hidden and inert until adjust mode is armed
        return True

    def _adjust_categories(self) -> tuple[tuple[str, str], ...]:
        """Rows in the adjust popup. Codex has no per-window gates."""
        if self.provider != "claude":
            return (("all", self.app.gettext("threshold")),)
        return (
            ("all", self.app.gettext("threshold")),
            ("5h", "5h"),
            ("7d", "7d"),
            ("fable", "Fable"),
        )

    def _selected_gate(self) -> str:
        categories = self._adjust_categories()
        return categories[self._adjust_index % len(categories)][0]

    def action_adjust_threshold(self) -> None:
        if self._adjusting:
            # ``t`` toggles. The popup owns the key while it is up; this is
            # the path when adjust mode is armed without a modal.
            self._end_adjust()
            return
        self._adjusting = True
        self._adjust_index = 0
        self._entry_threshold = self._settings.threshold
        self._entry_gates = (
            self._settings.threshold_5h,
            self._settings.threshold_7d,
            self._settings.model_thresholds,
        )
        self._update_summary()
        self.refresh_bindings()
        self.app.push_screen(ThresholdAdjustModal(self), self._on_adjust_dismiss)

    def _on_adjust_dismiss(self, _result: None) -> None:
        if self._adjusting:
            self._end_adjust()

    def action_adjust_done(self) -> None:
        if self._adjusting:
            self._end_adjust()

    def move_adjust_category(self, delta: int) -> None:
        count = len(self._adjust_categories())
        self._adjust_index = (self._adjust_index + delta) % count

    def action_threshold_step(self, delta: float) -> None:
        if not self._adjusting:
            return
        self._step_selected(float(delta))

    def clear_selected_gate(self) -> None:
        """Return the selected 5h/7d/Fable gate to the shared threshold."""
        key = self._selected_gate()
        if key == "all":
            return
        self._set_gate(key, None)

    def _step_selected(self, delta: float) -> None:
        key = self._selected_gate()
        if key == "all":
            spec = SETTING_SPECS["autoswitch.threshold"]
            value = min(spec.hi, max(spec.lo, self._settings.threshold + delta))
            self._set_threshold(value)
            return
        spec = SETTING_SPECS["autoswitch.threshold5h"]
        current = self._explicit_gate(key)
        base = self._settings.threshold if current is None else current
        value = round(min(spec.hi, max(spec.lo, base + delta)), 1)
        self._set_gate(key, value)

    def _explicit_gate(self, key: str) -> float | None:
        if key == "5h":
            return self._settings.threshold_5h
        if key == "7d":
            return self._settings.threshold_7d
        for name, pct in parse_model_thresholds(self._settings.model_thresholds):
            if name.lower() == "fable":
                return pct
        return None

    def _set_gate(self, key: str, value: float | None) -> None:
        """Persist one gate and point the running engine at it."""
        if self._explicit_gate(key) == value:
            return
        if key == "5h":
            self.app.apply_window_gate("5h", value)
            self._settings = replace(self._settings, threshold_5h=value)
        elif key == "7d":
            self.app.apply_window_gate("7d", value)
            self._settings = replace(self._settings, threshold_7d=value)
        else:
            self.app.apply_fable_gate(value)
            self._settings = replace(
                self._settings, model_thresholds=self.app._model_thresholds
            )
        self._sync_engine_settings()
        self._update_summary()

    def _sync_engine_settings(self) -> None:
        engine = self._engine
        if engine is None:
            return
        engine.apply_gates(self._settings)

    def adjust_menu_text(self) -> Text:
        palette = Palette.from_theme(self.app.current_theme)
        text = Text()
        categories = self._adjust_categories()
        selected = self._adjust_index % len(categories)
        for index, (key, label) in enumerate(categories):
            style = palette.accent if index == selected else palette.foreground
            mark = "▸" if index == selected else " "
            text.append(f"{mark} {label:<12}{self._format_gate(key)}\n", style=style)
        return text

    def _format_gate(self, key: str) -> str:
        if key == "all":
            label = f"{pct_label(self._settings.threshold)}%"
            if self._settings.threshold != self._configured_threshold:
                label += self.app.gettext("  session")
            return label
        value = self._explicit_gate(key)
        if value is None:
            return self.app.gettext("inherit ({threshold}%)").format(
                threshold=pct_label(self._settings.threshold)
            )
        return f"{pct_label(value)}%"

    def _end_adjust(self) -> None:
        self._adjusting = False
        self._update_summary()
        self.refresh_bindings()
        threshold_changed = self._settings.threshold != self._entry_threshold
        gates_changed = self._entry_gates != (
            self._settings.threshold_5h,
            self._settings.threshold_7d,
            self._settings.model_thresholds,
        )
        if not threshold_changed and not gates_changed:
            return  # no net change: nothing to announce, no tick to force
        if self._engine is not None:
            self._engine.wake()  # show a decision at the new value now
        if threshold_changed and not gates_changed:
            self._log_note(
                self.app.gettext(
                    "— threshold set to {threshold}{percent} for this session —"
                ).format(threshold=pct_label(self._settings.threshold), percent="%")
            )
            return
        parts = []
        if threshold_changed:
            parts.append(
                self.app.gettext("threshold {percentage}% (session)").format(
                    percentage=pct_label(self._settings.threshold)
                )
            )
        for key, label in self._adjust_categories():
            if key == "all":
                continue
            parts.append(f"{label} {self._format_gate(key)}")
        self._log_note("— " + " · ".join(parts) + " —")

    def _set_threshold(self, value: float) -> None:
        if value == self._settings.threshold:
            return
        self._settings = replace(self._settings, threshold=value)
        if self._engine is not None:
            self._engine.apply_threshold(value)
        self.app.threshold_pct = value
        self.query_one("#auto-active-panel", AccountsPanel).refresh()
        self._update_summary()

    def _update_summary(self) -> None:
        palette = Palette.from_theme(self.app.current_theme)
        text = Text()
        text.append(self.app.gettext("auto-switch · "))
        text.append(
            self.app.gettext("threshold {percentage}%").format(
                percentage=pct_label(self._settings.threshold)
            ),
            style=palette.accent if self._adjusting else "",
        )
        gates = []
        if self._settings.threshold_5h is not None:
            gates.append(
                self.app.gettext("5h {percentage}%").format(
                    percentage=pct_label(self._settings.threshold_5h)
                )
            )
        if self._settings.threshold_7d is not None:
            gates.append(
                self.app.gettext("7d {percentage}%").format(
                    percentage=pct_label(self._settings.threshold_7d)
                )
            )
        for name, pct in parse_model_thresholds(self._settings.model_thresholds):
            gates.append(f"{name} {pct_label(pct)}%")
        if gates:
            text.append(" · " + " ".join(gates))
        if self._settings.threshold != self._configured_threshold:
            text.append(self.app.gettext(" (session)"), style=palette.muted)
        text.append(
            self.app.gettext(" · poll every {interval_seconds:.0f}s").format(
                interval_seconds=self._settings.interval_seconds
            )
        )
        if self.app.mask_accounts:
            text.append(self.app.gettext(" · masked"), style=palette.muted)
        if self._adjusting:
            text.append(
                self.app.gettext("   ↑↓ category · ←→ ±1% · enter done"),
                style=palette.muted,
            )
        self.query_one("#auto-summary", Static).update(text)

    # -- engine -------------------------------------------------------------

    def _start_engine(self, *, dry_run: bool) -> None:
        engine_type = (
            AutoSwitchEngine if self.provider == "claude" else CodexAutoSwitchEngine
        )
        engine = engine_type(
            self.app.switcher_for(self.provider),
            self._settings,
            self._emit_from_thread,
            dry_run=dry_run,
        )
        self._engine = engine
        self.run_worker(
            engine.run_loop,
            thread=True,
            group="engine",
            exit_on_error=False,
            name=f"auto-engine-{'dry' if dry_run else 'live'}",
        )
        self._update_badge()
        self._log_note(
            self.app.gettext("— engine started: DRY-RUN (watching only) —")
            if dry_run
            else self.app.gettext("— engine started: LIVE (will switch accounts) —")
        )

    def _emit_from_thread(self, event: AutoSwitchEvent) -> None:
        """Engine ``on_event`` callback — runs on the worker thread."""
        try:
            self.app.call_from_thread(self._on_engine_event, event)
        except Exception:
            # App/screen tearing down mid-tick; the event has nowhere to go.
            pass

    def _on_engine_event(self, event: AutoSwitchEvent) -> None:
        if not self.is_attached:
            return
        self._log_items.append(("event", event))
        self._write_log_item(("event", event))
        if event.kind == "switch":
            self.app.request_refresh(self.provider)

    def _log_note(self, note: str) -> None:
        item: tuple[str, str | AutoSwitchEvent] = ("note", note)
        self._log_items.append(item)
        self._write_log_item(item)

    def _write_log_item(self, item: tuple[str, str | AutoSwitchEvent]) -> None:
        if not self.is_mounted:
            return
        palette = Palette.from_theme(self.app.current_theme)
        kind, payload = item
        log = self.query_one("#event-log", RichLog)
        if kind == "note":
            log.write(Text(str(payload), style=palette.muted))
            return
        assert isinstance(payload, AutoSwitchEvent)
        log.write(event_text(payload, palette=palette, mask=self.app.mask_accounts))

    def _repaint_log(self) -> None:
        if not self.is_mounted:
            return
        log = self.query_one("#event-log", RichLog)
        log.clear()
        for item in self._log_items:
            self._write_log_item(item)

    def _on_mask_change(self, _on: bool) -> None:
        self._update_summary()
        self._on_snapshot(self.app.snapshots[self.provider])
        self._repaint_log()

    def action_toggle_live(self) -> None:
        if self._engine is None:
            return
        if self._engine.dry_run:
            self.app.push_screen(
                ConfirmModal(
                    self.app.gettext(
                        "Go live? ccswap will switch your active account automatically when the threshold is reached.\n\n(Same behavior as the provider's `ccswap auto` command.)"
                    ),
                    title=self.app.gettext("Go live"),
                    yes_label=self.app.gettext("Go live"),
                ),
                self._on_live_confirm,
            )
        else:
            self._restart_engine(dry_run=True)

    def _on_live_confirm(self, confirmed: bool | None) -> None:
        if confirmed:
            self._restart_engine(dry_run=False)

    def _restart_engine(self, *, dry_run: bool) -> None:
        if self._engine is not None:
            self._engine.stop()
        self._start_engine(dry_run=dry_run)

    def _update_badge(self) -> None:
        badge = self.query_one("#mode-badge", Static)
        if self._engine is not None and not self._engine.dry_run:
            badge.update(self.app.gettext(" LIVE "))
            badge.set_classes("live")
        else:
            badge.update(self.app.gettext(" DRY-RUN "))
            badge.set_classes("dry")

    # -- candidates -----------------------------------------------------------

    def _on_snapshot(self, snap: AccountsSnapshot | None) -> None:
        if snap is None:
            return
        self.query_one("#candidates", Static).update(
            self._candidates_text(snap, active_number=snap.active_number)
        )

    def _on_snapshots(self, snapshots: dict[str, AccountsSnapshot | None]) -> None:
        self._on_snapshot(snapshots[self.provider])

    def _candidates_text(
        self, snap: AccountsSnapshot, active_number: str | None
    ) -> Text:
        """Switch targets ranked by remaining headroom (best first)."""
        # Same window set as the engine (autoswitch.model and
        # autoswitch.windows included), so the displayed ranking can never
        # disagree with the account it picks.
        palette = Palette.from_theme(self.app.current_theme)
        claude = self.provider == "claude" and self._settings is not None
        if claude:
            models = effective_model_names(self._settings)
            overrides = gate_thresholds(self._settings).overrides
        else:
            models = parse_model_names(self._settings.model) if self._settings else ()
            overrides = {}
        windows = (
            parse_window_selection(self._settings.windows)
            if self._settings
            else ("5h", "7d")
        )
        ranked: list[tuple[float, str]] = []  # (sort key: pct used, number)
        lines: dict[str, Text] = {}
        for acc in snap.accounts:
            if acc.number == active_number or not acc.switchable:
                continue
            pct = binding_pct(
                acc.usage.last_good,
                models,
                windows,
                gate_thresholds=overrides or None,
                default_threshold=self._settings.threshold
                if claude and overrides
                else None,
            )
            entry = Text()
            entry.append(f"\n  {acc.number:>2}  ", style=palette.foreground)
            entry.append(
                data.present_email(acc.email, mask=self.app.mask_accounts),
                style=palette.foreground,
            )
            if acc.usage.sentinel is not None:
                entry.append(
                    f"  {data.sentinel_label(acc.usage.sentinel)}", style=palette.muted
                )
                ranked.append((998.0, acc.number))
            elif pct is None:
                entry.append(self.app.gettext("  usage unknown"), style=palette.muted)
                ranked.append((999.0, acc.number))
            else:
                entry.append(
                    self.app.gettext("  {pct:3.0f}{percent} used").format(
                        pct=pct, percent="%"
                    ),
                    style=palette.severity(pct),
                )
                ranked.append((pct, acc.number))
            lines[acc.number] = entry

        text = Text()
        text.append(self.app.gettext("Next best"), style=palette.muted)
        if not ranked:
            text.append(
                self.app.gettext("\n  no other switchable accounts"),
                style=palette.muted,
            )
            return text
        for _pct, number in sorted(ranked):
            text.append(lines[number])
        return text


class ThresholdAdjustModal(ModalScreen[None]):
    """Popup for the auto-switch view: ↑↓ picks a gate, ←→ nudges it by 1%.

    The shared threshold is session-only. 5h, weekly, and Fable are saved.
    Backspace returns a gate to inherit. Enter, Esc, or ``t`` closes.
    """

    BINDINGS = [
        Binding("left", "step(-1)", show=False),
        Binding("right", "step(1)", show=False),
        Binding("up", "category(-1)", show=False),
        Binding("down", "category(1)", show=False),
        Binding("backspace,delete", "inherit", show=False),
        Binding("enter,escape,t", "close", show=False),
    ]

    def __init__(self, host: AutoScreen) -> None:
        super().__init__()
        self._host = host

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal-box"):
            yield Label(self.app.gettext("Adjust thresholds"), classes="modal-title")
            yield Static("", id="adjust-body")
            yield Static(
                self.app.gettext("↑↓ category    ←→ ±1%    ⌫ inherit    enter done"),
                classes="modal-hint",
            )

    def on_mount(self) -> None:
        localize_bindings(self, self.app.gettext)
        self._redraw()

    def _redraw(self) -> None:
        self.query_one("#adjust-body", Static).update(self._host.adjust_menu_text())

    def action_step(self, delta: float) -> None:
        self._host.action_threshold_step(delta)
        self._redraw()

    def action_category(self, delta: int) -> None:
        self._host.move_adjust_category(delta)
        self._redraw()

    def action_inherit(self) -> None:
        self._host.clear_selected_gate()
        self._redraw()

    def action_close(self) -> None:
        self.dismiss(None)
