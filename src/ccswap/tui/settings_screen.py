"""Settings screen: the dashboard menu's look, one row per setting.

Every row writes through the app's ``apply_*`` methods, which call
``set_setting`` (the function behind ``ccswap config set``). The two Claude Code
rows run ``claude_integration`` off the UI thread.
"""

from __future__ import annotations

from bisect import bisect_left
from functools import partial
from typing import TYPE_CHECKING, Callable

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Footer, ListView, Static

from ccswap.i18n import localize_bindings, mark
from ccswap.settings import parse_model_thresholds
from ccswap.tui.data import PROVIDER_LABELS, PROVIDERS
from ccswap.tui.modals import OutputModal
from ccswap.tui.option_help import option_help
from ccswap.tui.overview import HeaderBar
from ccswap.tui.widgets import AccountsPanel, CyclingListView, MenuItem

if TYPE_CHECKING:
    from ccswap.tui.app import CswapApp

DEFAULT_THRESHOLD_PCT = 90.0

# None is "inherit autoswitch.threshold". Exact values outside these ladders
# are still `ccswap config set`; stepping goes to the next stop, and the last
# stop wraps to inherit (gates) or the first stop (threshold).
_GATE_STOPS = (40.0, 50.0, 60.0, 70.0, 75.0, 80.0, 85.0, 90.0, 95.0)
_THRESHOLD_STOPS = (75.0, 80.0, 85.0, 90.0, 95.0)
_BACK = "back"


def _gate_label(
    pct: float | None, inherited: float, tr: Callable[[str], str] = str
) -> str:
    if pct is None:
        return tr("inherit ({inherited:g}%)").format(inherited=inherited)
    return f"{pct:g}%"


def _fable_pct(model_thresholds: str | None) -> float | None:
    for name, pct in parse_model_thresholds(model_thresholds):
        if name.lower() == "fable":
            return pct
    return None


def _step_stops(stops: tuple[float, ...], current: float, delta: int) -> float:
    """Next (+1) or previous (-1) stop, wrapping; off-ladder values land on a neighbour."""
    index = bisect_left(stops, current)
    if delta > 0:
        if index < len(stops) and stops[index] == current:
            index += 1
        return stops[index % len(stops)]
    return stops[(index - 1) % len(stops)]


def _step_gate(current: float | None, delta: int) -> float | None:
    """Gate ladder with None (inherit) as the stop before 40% and after 95%."""
    if current is None:
        return _GATE_STOPS[0] if delta > 0 else _GATE_STOPS[-1]
    index = bisect_left(_GATE_STOPS, current)
    if delta > 0:
        if index < len(_GATE_STOPS) and _GATE_STOPS[index] == current:
            index += 1
        return _GATE_STOPS[index] if index < len(_GATE_STOPS) else None
    return _GATE_STOPS[index - 1] if index > 0 else None


class SettingsScreen(Screen):
    BINDINGS = [
        Binding("left", "change(-1)", mark("Change")),
        Binding("right", "change(1)", show=False),
        Binding("escape", "back", mark("Back")),
        Binding("q", "app.quit", mark("Quit")),
        Binding("question_mark", "option_help", mark("Option help")),
        Binding("j", "cursor_down", show=False),
        Binding("k", "cursor_up", show=False),
    ]

    app: "CswapApp"

    def __init__(self) -> None:
        super().__init__()
        # Live Claude Code integration state; None until the first off-thread
        # read (`claude plugin list` takes about a second), and None again if
        # that read fails (shown as "unavailable").
        self._claude_state: dict[str, bool | None] = {
            "claude.statusline": None,
            "claude.mod": None,
        }
        self._claude_busy = False

    def compose(self) -> ComposeResult:
        yield HeaderBar()
        yield AccountsPanel(
            provider=None if self.app._view == "combined" else self.app._view,
            id="accounts-panel",
        )
        yield Static("", id="menu-title")
        yield CyclingListView(id="menu")
        yield Footer()

    async def on_mount(self) -> None:
        localize_bindings(self, self.app.gettext)
        self.query_one("#accounts-panel").can_focus = True  # Tab in to scroll it without a mouse
        self.query_one("#menu-title", Static).update(
            " › ".join(self.app.gettext(t) for t in (mark("menu"), mark("settings")))
        )
        menu = self.query_one("#menu", ListView)
        await menu.extend(
            MenuItem(
                self.app.gettext("← back") if action_id == _BACK else label,
                action_id,
                muted=(action_id == _BACK),
                help_text=option_help(action_id),
            )
            for label, action_id in self._entries()
        )
        menu.index = 0
        menu.focus()
        self.watch(self.app, "theme", lambda _theme: self._relabel())
        self._start_claude_work()

    # -- rows ---------------------------------------------------------------

    def _entries(self) -> list[tuple[str, str]]:
        tr = self.app.gettext
        app = self.app
        view_labels = {
            "combined": tr("Combined"),
            **{provider: PROVIDER_LABELS[provider] for provider in PROVIDERS},
        }
        threshold = app.threshold_pct
        shared = threshold if threshold is not None else DEFAULT_THRESHOLD_PCT
        return [
            (tr("Theme: {theme_name}").format(theme_name=app._theme_name), "setting:theme"),
            (
                tr("Dashboard view: {view}").format(view=view_labels[app._view]),
                "setting:view",
            ),
            (
                tr("Mask account info: {state}").format(
                    state=tr("on") if app.mask_accounts else tr("off")
                ),
                "setting:mask",
            ),
            (
                tr("Auto-switch threshold: {threshold_label}%").format(
                    threshold_label=f"{shared:g}"
                ),
                "setting:threshold",
            ),
            (
                tr("5h gate: {gate}").format(
                    gate=_gate_label(app._threshold_5h, shared, tr)
                ),
                "setting:threshold5h",
            ),
            (
                tr("Weekly gate: {gate}").format(
                    gate=_gate_label(app._threshold_7d, shared, tr)
                ),
                "setting:threshold7d",
            ),
            (
                tr("Fable gate: {gate}").format(
                    gate=_gate_label(_fable_pct(app._model_thresholds), shared, tr)
                ),
                "setting:fable",
            ),
            (
                tr("Auto-switch strategy: {strategy_name}").format(
                    strategy_name=app._strategy_name
                ),
                "setting:strategy",
            ),
            (
                tr("Claude statusline: {state}").format(
                    state=self._claude_label("claude.statusline")
                ),
                "claude:claude.statusline",
            ),
            (
                tr("Claude Code mod: {state}").format(
                    state=self._claude_label("claude.mod")
                ),
                "claude:claude.mod",
            ),
            (tr("← back"), _BACK),
        ]

    def _claude_label(self, key: str) -> str:
        state = self._claude_state[key]
        if self._claude_busy:
            return "…"
        if state is None:
            return self.app.gettext("unavailable")
        return self.app.gettext("on") if state else self.app.gettext("off")

    def _relabel(self) -> None:
        """Rewrite row text in place (the cursor and widgets stay put)."""
        if not self.is_attached:
            return
        labels = dict((action_id, label) for label, action_id in self._entries())
        for item in self.query_one("#menu", ListView).query(MenuItem):
            if item.action_id != _BACK:
                item.query_one(Static).update(labels[item.action_id])

    def refresh_masked_labels(self) -> None:
        """`m` toggled masking: the mask row's label follows it."""
        self._relabel()

    # -- actions ------------------------------------------------------------

    def action_back(self) -> None:
        self.app.pop_screen()

    def action_cursor_down(self) -> None:
        self.query_one("#menu", ListView).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#menu", ListView).action_cursor_up()

    def action_option_help(self) -> None:
        item = self.query_one("#menu", ListView).highlighted_child
        if isinstance(item, MenuItem) and (help_text := option_help(item.action_id)):
            self.app.push_screen(OutputModal(self.app.gettext("Option help"), help_text))

    def action_change(self, delta: int) -> None:
        item = self.query_one("#menu", ListView).highlighted_child
        if isinstance(item, MenuItem) and item.action_id != _BACK:
            self.change(item.action_id, int(delta))

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        item = event.item
        if not isinstance(item, MenuItem):
            return
        if item.action_id == _BACK:
            self.action_back()
        else:
            self.change(item.action_id, 1)

    def change(self, action_id: str, delta: int) -> None:
        """Step one row forward (Enter, →) or back (←)."""
        app = self.app
        if action_id.startswith("claude:"):
            self._toggle_claude(action_id.split(":", 1)[1])
            return
        key = action_id.split(":", 1)[1]
        if key == "theme":
            order = ("dark", "light", "auto")
            value = order[(order.index(app._theme_name) + delta) % len(order)]
            app.apply_theme(value)
            app.notify(app.gettext("Theme: {value}").format(value=value))
        elif key == "view":
            order = ("combined", *PROVIDERS)
            app.apply_view(order[(order.index(app._view) + delta) % len(order)])
        elif key == "threshold":
            # ponytail: 95% is this compact ladder's ceiling; use
            # `ccswap config set autoswitch.threshold` for exact values.
            current = (
                app.threshold_pct if app.threshold_pct is not None else DEFAULT_THRESHOLD_PCT
            )
            app.apply_threshold(_step_stops(_THRESHOLD_STOPS, current, delta))
        elif key == "threshold5h":
            app.apply_window_gate("5h", _step_gate(app._threshold_5h, delta))
        elif key == "threshold7d":
            app.apply_window_gate("7d", _step_gate(app._threshold_7d, delta))
        elif key == "fable":
            app.apply_fable_gate(_step_gate(_fable_pct(app._model_thresholds), delta))
        elif key == "mask":
            app.apply_mask(not app.mask_accounts)
        elif key == "strategy":
            # An AutoScreen is never mounted under Settings (it is reached
            # from the dashboard and popped first), so no live engine needs
            # the new value; the next AutoScreen re-reads the file.
            order = ("best", "consume-first")
            app.apply_strategy(order[(order.index(app._strategy_name) + delta) % len(order)])
        self._relabel()

    # -- Claude Code integrations (off the UI thread) --------------------------

    def _toggle_claude(self, key: str) -> None:
        if self._claude_busy:
            self.app.notify(
                self.app.gettext("Still checking Claude Code, try again in a moment")
            )
            return
        state = self._claude_state[key]
        if state is None:  # the last read failed: read again, don't guess a value
            self._start_claude_work()
        else:
            self._start_claude_work(key, not state)

    def _start_claude_work(self, key: str | None = None, on: bool = False) -> None:
        """Read (and with ``key``, first toggle) the integrations: install and
        uninstall shell out to `claude plugin`."""
        self._claude_busy = True
        self._relabel()
        self.run_worker(
            partial(self._claude_work, key, on),
            thread=True,
            group="claude",
            exit_on_error=False,
        )

    def _claude_work(self, key: str | None, on: bool) -> None:
        from ccswap.claude_integration import is_enabled, set_enabled

        message, severity = None, "information"
        if key is not None:
            try:
                message = set_enabled(self.app.switcher_for("claude").backup_dir, key, on)
            except Exception as exc:  # surface any failure; never crash the UI
                message, severity = (
                    self.app.gettext("Could not change {key}: {exc}").format(key=key, exc=exc),
                    "error",
                )
        try:
            state = {k: is_enabled(k) for k in self._claude_state}
        except Exception as exc:  # show "unavailable", never hang on the busy marker
            state = {k: None for k in self._claude_state}
            message, severity = (
                self.app.gettext("Could not read Claude Code state: {exc}").format(exc=exc),
                "error",
            )
        try:
            self.app.call_from_thread(self._claude_done, state, message, severity)
        except RuntimeError:  # the app closed while we were reading
            pass

    def _claude_done(
        self, state: dict[str, bool | None], message: str | None, severity: str
    ) -> None:
        self._claude_busy = False
        self._claude_state = state
        if message:
            self.app.notify(message, severity=severity, timeout=6)
        self._relabel()
