"""Settings screen: the dashboard menu's look, one row per setting.

Every row writes through the app's ``apply_*`` methods, which call
``set_setting`` (the function behind ``ccswap config set``). The two Claude Code
rows run ``claude_integration`` off the UI thread.
"""

from __future__ import annotations

from bisect import bisect_left
from functools import partial
from typing import TYPE_CHECKING, Callable

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import ListView, Static

from ccswap.i18n import localize_bindings, mark
from ccswap.settings import parse_model_thresholds
from ccswap.tui.data import PROVIDER_LABELS, PROVIDERS, capabilities
from ccswap.tui.modals import OutputModal
from ccswap.tui.option_help import option_help
from ccswap.tui.overview import HeaderBar
from ccswap.tui.theme import Palette
from ccswap.tui.widgets import CswapFooter, CyclingListView, MenuItem

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


# (section, rows): today's row order, split as the Figma settings frames do.
SECTIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (mark("Display"), ("setting:theme", "setting:view", "setting:mask")),
    (
        mark("Auto-switch"),
        (
            "setting:threshold",
            "setting:threshold5h",
            "setting:threshold7d",
            "setting:fable",
            "setting:strategy",
        ),
    ),
    (mark("Claude Code"), ("claude:claude.statusline", "claude:claude.mod")),
)
_ROW_NAMES = {
    "setting:theme": mark("Theme"),
    "setting:view": mark("Dashboard view"),
    "setting:mask": mark("Mask account info"),
    "setting:threshold": mark("Threshold"),
    "setting:threshold5h": mark("5h gate"),
    "setting:threshold7d": mark("Weekly gate"),
    "setting:fable": mark("Fable gate"),
    "setting:strategy": mark("Strategy"),
    "claude:claude.statusline": mark("Statusline"),
    "claude:claude.mod": mark("Claude Code mod"),
}
_HINTS = {
    "setting:theme": "dark · light · auto",
    "setting:view": "Combined · Claude Code · Codex · Antigravity",
    "setting:mask": "on · off",
    "setting:threshold": " · ".join(f"{v:g}" for v in _THRESHOLD_STOPS),
    "setting:threshold5h": "inherit · 40 … 95",
    "setting:threshold7d": "inherit · 40 … 95",
    "setting:fable": "inherit · 40 … 95 · Claude only",
    "setting:strategy": "best · consume-first",
    "claude:claude.statusline": "on · off",
    "claude:claude.mod": "on · off",
}
_TOGGLES = {"setting:mask", "claude:claude.statusline", "claude:claude.mod"}
NAME_W = 20  # widest row name plus a gap
VALUE_W = 21  # "‹ inherit (90%) ›" plus a gap


def section_of(action_id: str) -> int:
    """Index of the section holding a row; ← back belongs to none (-1)."""
    return next((i for i, (_t, rows) in enumerate(SECTIONS) if action_id in rows), -1)


class SettingRow(MenuItem):
    """One setting: name, value, and a dim list of its stops, on one line."""

    def __init__(self, action_id: str, *, help_text: str | None = None) -> None:
        super().__init__("", action_id, help_text=help_text)
        self.name_text = ""
        self.value = ""  # the value as written, e.g. "90%", "on", "inherit (90%)"
        self.toggle: bool | None = None  # on/off rows: the state, None if unknown

    def set_value(self, name: str, value: str, toggle: bool | None = None) -> None:
        self.name_text, self.value, self.toggle = name, value, toggle
        self.redraw()

    def watch_highlighted(self, value: bool) -> None:
        super().watch_highlighted(value)
        self.redraw()

    def redraw(self) -> None:
        if not self.is_mounted:
            return
        palette = Palette.from_theme(self.app.current_theme)
        line = Text(no_wrap=True, overflow="ellipsis")
        line.append("› " if self.highlighted else "  ", style=f"bold {palette.accent}")
        line.append(self.name_text.ljust(NAME_W)[: NAME_W], style=palette.foreground)
        value = Text()
        if self.action_id in _TOGGLES and self.toggle is not None:
            value.append("[")
            value.append("● on " if self.toggle else "  off", style="bold")
            value.append("]")
        elif self.action_id in _TOGGLES:
            value.append(self.value, style=palette.muted)
        else:
            value.append("‹ ", style=palette.muted)
            value.append(self.value, style="bold")
            value.append(" ›", style=palette.muted)
        value.pad_right(max(1, VALUE_W - value.cell_len))
        line.append(value)
        line.append(_HINTS.get(self.action_id, ""), style=palette.dim)
        self.query_one(Static).update(line)


class SettingsScreen(Screen):
    """Settings in sections: the section list on the left (a one-line
    switcher under 100 cols), the focused section's rows, and a help block
    with the row's option help and its `ccswap config set` line.

    ↑/↓ walk every row in order, crossing into the next section; ←/→ and
    Enter step a value; ← back is the last stop."""

    BINDINGS = [
        Binding("left", "change(-1)", mark("Change"), show=False),
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
        self._section = 0  # the section whose rows are shown

    def compose(self) -> ComposeResult:
        yield HeaderBar()
        with Horizontal(id="settings-body"):
            with Vertical(id="settings-nav"):
                yield Static("", id="menu-title")
                yield Static("", id="settings-sections")
            with Vertical(id="settings-main"):
                yield Static("", id="section-title")
                yield CyclingListView(id="menu")
                yield Static("", id="setting-help")
        yield CswapFooter()

    async def on_mount(self) -> None:
        localize_bindings(self, self.app.gettext)
        self.query_one("#menu-title", Static).update(
            " › ".join(self.app.gettext(t) for t in (mark("menu"), mark("settings")))
        )
        menu = self.query_one("#menu", ListView)
        rows = [action_id for _title, ids in SECTIONS for action_id in ids]
        await menu.extend(
            [SettingRow(action_id, help_text=option_help(action_id)) for action_id in rows]
            + [MenuItem(self.app.gettext("← back"), _BACK, muted=True, help_text=option_help(_BACK))]
        )
        menu.index = 0
        menu.focus()
        self.watch(self.app, "theme", lambda _theme: self._relabel())
        self._start_claude_work()
        self._show_section()

    def on_resize(self) -> None:
        self.call_after_refresh(self._draw_nav)  # list ↔ switcher at 100 cols

    # -- rows ---------------------------------------------------------------

    def _values(self) -> dict[str, tuple[str, bool | None]]:
        """Each row's current value (and on/off state for toggles)."""
        tr = self.app.gettext
        app = self.app
        view_labels = {
            "combined": tr("Combined"),
            **{provider: PROVIDER_LABELS[provider] for provider in PROVIDERS},
        }
        threshold = app.threshold_pct
        shared = threshold if threshold is not None else DEFAULT_THRESHOLD_PCT
        return {
            "setting:theme": (app._theme_name, None),
            "setting:view": (view_labels[app._saved_view], None),
            "setting:mask": (tr("on") if app.mask_accounts else tr("off"), app.mask_accounts),
            "setting:threshold": (f"{shared:g}%", None),
            "setting:threshold5h": (_gate_label(app._threshold_5h, shared, tr), None),
            "setting:threshold7d": (_gate_label(app._threshold_7d, shared, tr), None),
            "setting:fable": (_gate_label(_fable_pct(app._model_thresholds), shared, tr), None),
            "setting:strategy": (app._strategy_name, None),
            **{
                f"claude:{key}": (
                    self._claude_label(key),
                    None if self._claude_busy else self._claude_state[key],
                )
                for key in self._claude_state
            },
        }

    def config_command(self, action_id: str) -> str | None:
        """The `ccswap config` line that writes this row's current value."""
        app = self.app
        shared = app.threshold_pct if app.threshold_pct is not None else DEFAULT_THRESHOLD_PCT

        def optional(key: str, value: float | str | None) -> str:
            if value is None:
                return f"ccswap config unset {key}"
            return f"ccswap config set {key} {value:g}" if isinstance(value, float) else (
                f"ccswap config set {key} {value}"
            )

        if action_id.startswith("claude:"):
            key = action_id.split(":", 1)[1]
            state = self._claude_state[key]
            value = "on|off" if state is None else ("on" if state else "off")
            return f"ccswap config set {key} {value}"
        return {
            "setting:theme": f"ccswap config set ui.theme {app._theme_name}",
            "setting:view": f"ccswap config set ui.view {app._saved_view}",
            "setting:mask": f"ccswap config set ui.mask {'on' if app.mask_accounts else 'off'}",
            "setting:threshold": f"ccswap config set autoswitch.threshold {shared:g}",
            "setting:threshold5h": optional("autoswitch.threshold5h", app._threshold_5h),
            "setting:threshold7d": optional("autoswitch.threshold7d", app._threshold_7d),
            "setting:fable": optional("autoswitch.modelThresholds", app._model_thresholds),
            "setting:strategy": f"ccswap config set autoswitch.strategy {app._strategy_name}",
        }.get(action_id)

    def _relabel(self) -> None:
        """Rewrite row text in place (the cursor and widgets stay put)."""
        if not self.is_attached:
            return
        values = self._values()
        for row in self.query_one("#menu", ListView).query(SettingRow):
            value, toggle = values[row.action_id]
            row.set_value(self.app.gettext(_ROW_NAMES[row.action_id]), value, toggle)
        self._draw_help()

    def _current(self) -> MenuItem | None:
        item = self.query_one("#menu", ListView).highlighted_child
        return item if isinstance(item, MenuItem) else None

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        item = event.item
        if isinstance(item, MenuItem) and (section := section_of(item.action_id)) >= 0:
            self._section = section
        self._show_section()

    def _show_section(self) -> None:
        """Only the current section's rows are shown; ← back never is (the
        nav draws it), though it stays a cursor stop."""
        if not self.is_attached:
            return
        shown = SECTIONS[self._section][1]
        for item in self.query_one("#menu", ListView).query(MenuItem):
            item.display = item.action_id in shown
        palette = Palette.from_theme(self.app.current_theme)
        title, rows = SECTIONS[self._section]
        heading = Text()
        heading.append(self.app.gettext(title), style=f"bold {palette.accent}")
        if rows[0].startswith("setting:threshold"):  # auto-switch: who it drives
            heading.append(
                "   " + " · ".join(
                    PROVIDER_LABELS[p] for p in PROVIDERS if capabilities(p).auto_switch
                ),
                style=palette.muted,
            )
        self.query_one("#section-title", Static).update(heading)
        self._relabel()
        self._draw_nav()

    def _on_back(self) -> bool:
        item = self._current()
        return item is not None and item.action_id == _BACK

    def _draw_nav(self) -> None:
        if not self.is_attached:
            return
        tr = self.app.gettext
        palette = Palette.from_theme(self.app.current_theme)
        on_back = self._on_back()
        nav = self.query_one("#settings-sections", Static)
        text = Text(no_wrap=True, overflow="ellipsis")
        back_style = f"bold {palette.accent}" if on_back else palette.muted
        if self.has_class("-narrow"):
            for i, (title, _rows) in enumerate(SECTIONS):
                if i:
                    text.append("   ")
                style = (
                    f"bold underline {palette.accent}" if i == self._section else palette.muted
                )
                text.append(tr(title), style=style)
            back = tr("← back")
            width = nav.content_size.width or 76
            text.append(" " * max(2, width - text.cell_len - len(back)))
            text.append(back, style=back_style)
        else:
            for i, (title, _rows) in enumerate(SECTIONS):
                current = i == self._section
                text.append("› " if current else "  ", style=f"bold {palette.accent}")
                text.append(
                    tr(title) + "\n",
                    style=f"bold {palette.accent}" if current else palette.foreground,
                )
            text.append("\n")
            text.append("› " if on_back else "  ", style=f"bold {palette.accent}")
            text.append(tr("← back"), style=back_style)
        nav.update(text)

    def _draw_help(self) -> None:
        item = self._current()
        help_widget = self.query_one("#setting-help", Static)
        if item is None:
            help_widget.update("")
            return
        palette = Palette.from_theme(self.app.current_theme)
        name = (
            self.app.gettext(_ROW_NAMES[item.action_id])
            if item.action_id in _ROW_NAMES
            else self.app.gettext("Back")
        )
        text = Text()
        text.append(name, style=f"bold {palette.foreground}")
        help_text = option_help(item.action_id)
        if help_text:
            text.append("\n" + help_text, style=palette.muted)
        command = self.config_command(item.action_id)
        if command:
            text.append("\n\n$ ", style=palette.dim)
            text.append(command, style=palette.foreground)
        help_widget.update(text)

    def on_click(self, event) -> None:
        """A click on a section (or ← back) in the nav goes there."""
        nav = self.query_one("#settings-sections", Static)
        if event.widget is not nav:
            return
        offset = event.get_content_offset(nav)
        if offset is None:
            return
        menu = self.query_one("#menu", ListView)
        if self.has_class("-narrow"):
            back_at = (nav.content_size.width or 76) - len(self.app.gettext("← back"))
            if offset.x >= back_at:
                self.action_back()
                return
            x = 0
            for i, (title, _rows) in enumerate(SECTIONS):
                width = len(self.app.gettext(title))
                if x <= offset.x < x + width:
                    menu.index = self._first_row(i)
                    return
                x += width + 3
        elif offset.y < len(SECTIONS):
            menu.index = self._first_row(offset.y)
        elif offset.y == len(SECTIONS) + 1:
            self.action_back()

    def _first_row(self, section: int) -> int:
        first = SECTIONS[section][1][0]
        menu = self.query_one("#menu", ListView)
        return next(i for i, m in enumerate(menu.query(MenuItem)) if m.action_id == first)

    def _claude_label(self, key: str) -> str:
        state = self._claude_state[key]
        if self._claude_busy:
            return "…"
        if state is None:
            return self.app.gettext("unavailable")
        return self.app.gettext("on") if state else self.app.gettext("off")

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
        item = self._current()
        if item is not None and (help_text := option_help(item.action_id)):
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
            app.apply_view(order[(order.index(app._saved_view) + delta) % len(order)])
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
