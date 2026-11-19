"""Settings screen: the dashboard menu's look, one row per setting.

Every row writes through the app's ``apply_*`` methods, which call
``set_setting`` (the function behind ``ccswap config set``). The two Claude Code
rows run ``claude_integration`` off the UI thread.
"""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import replace
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
    (mark("Display"), ("setting:theme", "setting:view", "setting:mask", "setting:star")),
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
    "setting:star": mark("Star prompt"),
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
    "setting:star": "on · off",
    "setting:threshold": " · ".join(f"{v:g}" for v in _THRESHOLD_STOPS),
    "setting:threshold5h": "inherit · 40 … 95",
    "setting:threshold7d": "inherit · 40 … 95",
    "setting:fable": "inherit · 40 … 95 · Claude only",
    "setting:strategy": "best · consume-first",
    "claude:claude.statusline": "on · off",
    "claude:claude.mod": "on · off",
}
_TOGGLES = {"setting:mask", "setting:star", "claude:claude.statusline", "claude:claude.mod"}
# The settings.json key each edited row writes (Esc restores it).
_CONFIG_KEYS = {
    "setting:theme": "ui.theme",
    "setting:view": "ui.view",
    "setting:threshold": "autoswitch.threshold",
    "setting:threshold5h": "autoswitch.threshold5h",
    "setting:threshold7d": "autoswitch.threshold7d",
    "setting:fable": "autoswitch.modelThresholds",
    "setting:strategy": "autoswitch.strategy",
}
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
        self.editing = False  # Enter armed it: ←/→ step the value

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
            arrows = f"bold {palette.accent}" if self.editing else palette.muted
            value.append("‹ ", style=arrows)
            value.append(self.value, style=f"bold {palette.accent}" if self.editing else "bold")
            value.append(" ›", style=arrows)
        value.pad_right(max(1, VALUE_W - value.cell_len))
        line.append(value)
        line.append(_HINTS.get(self.action_id, ""), style=palette.dim)
        self.query_one(Static).update(line)


class SettingsScreen(Screen):
    """Settings in sections: the section list on the left (a one-line
    switcher under 100 cols), the focused section's rows, and a help block
    with the row's option help and its `ccswap config set` line.

    Two panes. In the rows, ↑/↓ walk every row in order (crossing into the
    next section, ← back last), Enter edits a choice row or flips an on/off
    one, and ← moves to the section list; there ↑/↓ pick a section and →
    or Enter go back into its rows, ← or Esc leave. A value changes only
    while its row is being edited: ←/→ step it, Enter keeps it, Esc puts
    back what it was. Under 100 cols the section list is a switcher, so
    ←/→ change section instead."""

    # The four keys whose meaning (and footer label) follows the state;
    # _sync_keys rewrites them. priority: the focused list binds them too.
    BINDINGS = [
        Binding("up", "up", mark("Section"), key_display="↑↓", show=False, priority=True),
        Binding("left", "left", mark("Sections"), key_display="←", priority=True),
        Binding("enter", "enter", mark("Edit"), priority=True),
        Binding("escape", "escape", mark("Back")),
        Binding("q", "app.quit", mark("Quit")),
        Binding("question_mark", "option_help", mark("Option help")),
        Binding("down", "down", show=False, priority=True),
        Binding("k", "up", show=False, priority=True),
        Binding("j", "down", show=False, priority=True),
        Binding("right", "right", show=False, priority=True),
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
        self._pane = "rows"  # or "sections": where ↑/↓ and ←/→ act
        self._editing: str | None = None  # the row whose value ←/→ step
        self._session_view = "combined"
        self._before: tuple | None = None  # (value, key was in the file) to restore
        self._changed = False

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
        self._sync_keys()

    def on_resize(self) -> None:
        # list ↔ switcher at 100 cols; the switcher has no section pane
        if self.has_class("-narrow") and self._pane == "sections":
            self._enter_rows()
        self.call_after_refresh(self._draw_nav)
        self.call_after_refresh(self._sync_keys)

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
            "setting:star": (tr("on") if app._star_setting else tr("off"), app._star_setting),
            "setting:threshold": (f"{shared:g}%", None),
            # "inherit" shows what the engine falls back to (weeklyThreshold too).
            "setting:threshold5h": (_gate_label(app._threshold_5h, app.inherited_gate("5h"), tr), None),
            "setting:threshold7d": (_gate_label(app._threshold_7d, app.inherited_gate("7d"), tr), None),
            "setting:fable": (
                _gate_label(_fable_pct(app._model_thresholds), app.inherited_gate("Fable"), tr),
                None,
            ),
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

        def optional(key: str, value: float | str | None, inherits: float | None = None) -> str:
            if value is None:
                note = f"   # inherits {inherits:g}%" if inherits is not None else ""
                return f"ccswap config unset {key}{note}"
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
            "setting:star": f"ccswap config set ui.starPrompt {'on' if app._star_setting else 'off'}",
            "setting:threshold": f"ccswap config set autoswitch.threshold {shared:g}",
            "setting:threshold5h": optional(
                "autoswitch.threshold5h", app._threshold_5h, app.inherited_gate("5h")
            ),
            "setting:threshold7d": optional(
                "autoswitch.threshold7d", app._threshold_7d, app.inherited_gate("7d")
            ),
            "setting:fable": optional(
                "autoswitch.modelThresholds",
                app._model_thresholds,
                app.inherited_gate("Fable") if _fable_pct(app._model_thresholds) is None else None,
            ),
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
        menu = self._menu()
        if item is not menu.highlighted_child:  # a late event from before ← or a section change
            return
        if isinstance(item, MenuItem) and (section := section_of(item.action_id)) >= 0:
            self._section = section
        if (  # a click on a row; not a late event from before ← cleared the index
            self._pane == "sections"
            and menu.index is not None
            and item is not None
            and item is menu.highlighted_child
        ):
            self._pane = "rows"
            self._sync_keys()
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
            width = nav.content_size.width or 25
            for i, (title, _rows) in enumerate(SECTIONS):
                current = i == self._section
                # The section pane's cursor: the rows' accent tint.
                band = f" on {palette.cursor}" if current and self._pane == "sections" else ""
                line = Text()
                line.append("› " if current else "  ", style=f"bold {palette.accent}{band}")
                line.append(
                    tr(title),
                    style=(f"bold {palette.accent}" if current else palette.foreground) + band,
                )
                if band:
                    line.append(" " * max(0, width - line.cell_len), style=band.strip())
                text.append(line)
                text.append("\n")
            text.append("\n")
            text.append("› " if on_back else "  ", style=f"bold {palette.accent}")
            text.append(tr("← back"), style=back_style)
        nav.update(text)

    def _draw_help(self) -> None:
        item = self._current()
        help_widget = self.query_one("#setting-help", Static)
        palette = Palette.from_theme(self.app.current_theme)
        if self._pane == "sections":
            text = Text()
            text.append(self.app.gettext(SECTIONS[self._section][0]), style=f"bold {palette.foreground}")
            text.append(
                "\n" + self.app.gettext("↑↓ choose a section · → or ⏎ open it · ← or esc leave settings"),
                style=palette.muted,
            )
            help_widget.update(text)
            return
        if item is None:
            help_widget.update("")
            return
        name = (
            self.app.gettext(_ROW_NAMES[item.action_id])
            if item.action_id in _ROW_NAMES
            else self.app.gettext("Back")
        )
        text = Text()
        text.append(name, style=f"bold {palette.foreground}")
        if self._editing == item.action_id:
            text.append("   " + self.app.gettext("←→ change · ⏎ done · esc cancel"), style=palette.accent)
        help_text = option_help(item.action_id)
        if help_text:
            text.append("\n" + help_text, style=palette.muted)
        command = self.config_command(item.action_id)
        if command:
            command, _sep, note = command.partition("   # ")
            text.append("\n\n$ ", style=palette.dim)
            text.append(command, style=palette.foreground)
            if note:
                text.append("   # " + note, style=palette.dim)
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
                    self._finish_edit()
                    menu.index = self._first_row(i)
                    return
                x += width + 3
        elif offset.y < len(SECTIONS):
            self._finish_edit()
            self._section = offset.y
            self._enter_rows()
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
        self._finish_edit()
        self.app.pop_screen()

    def action_option_help(self) -> None:
        item = self._current()
        if item is not None and (help_text := option_help(item.action_id)):
            self.app.push_screen(OutputModal(self.app.gettext("Option help"), help_text))

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        if action == "option_help" and self._pane == "sections":
            return False
        return True

    # -- keys: one meaning per state -----------------------------------------

    def _state(self) -> str:
        if self._editing is not None:
            return "editing"
        if self.has_class("-narrow"):
            return "narrow"
        return self._pane

    def _sync_keys(self) -> None:
        """Footer labels for the four state keys (descriptions, shown or not)."""
        tr = self.app.gettext
        state = self._state()
        table = {
            # key: (show, key_display, description)
            "up": (state == "sections", "↑↓", mark("Section")),
            "left": (
                state != "sections",
                "←" if state == "rows" else "←→",
                {"editing": mark("Change"), "narrow": mark("Section"), "rows": mark("Sections")}.get(
                    state, mark("Sections")
                ),
            ),
            "enter": (True, None, {"editing": mark("Done"), "sections": mark("Open")}.get(state, mark("Edit"))),
            "escape": (True, None, mark("Cancel") if state == "editing" else mark("Back")),
        }
        for key, (show, display, description) in table.items():
            self._bindings.key_to_bindings[key] = [
                replace(
                    binding,
                    show=show,
                    description=tr(description),
                    key_display=display if display is not None else binding.key_display,
                )
                for binding in self._bindings.key_to_bindings.get(key, [])
            ]
        self.refresh_bindings()

    def _menu(self) -> ListView:
        return self.query_one("#menu", ListView)

    def action_up(self) -> None:
        self._vertical(-1)

    def action_down(self) -> None:
        self._vertical(1)

    def _vertical(self, step: int) -> None:
        if self._pane == "sections":
            self._section = (self._section + step) % len(SECTIONS)
            self._show_section()
            return
        self._finish_edit()  # moving off a row keeps what it was set to
        if step > 0:
            self._menu().action_cursor_down()
        else:
            self._menu().action_cursor_up()

    def action_left(self) -> None:
        state = self._state()
        if state == "editing":
            self._step(-1)
        elif state == "narrow":
            self._jump_section(-1)
        elif state == "rows":
            self._pane = "sections"
            self._menu().index = None
            self._show_section()
            self._sync_keys()
        else:
            self.action_back()

    def action_right(self) -> None:
        state = self._state()
        if state == "editing":
            self._step(1)
        elif state == "narrow":
            self._jump_section(1)
        elif state == "sections":
            self._enter_rows()

    def action_enter(self) -> None:
        if self._editing is not None:
            self._finish_edit()
        elif self._pane == "sections":
            self._enter_rows()
        elif (item := self._current()) is not None:
            self._activate(item)

    def action_escape(self) -> None:
        if self._editing is not None:
            self._cancel_edit()
        else:
            self.action_back()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        """A click on a row: the same as Enter on it."""
        item = event.item
        if not isinstance(item, MenuItem):
            return
        if self._editing == item.action_id:
            self._finish_edit()
        else:
            self._finish_edit()
            self._activate(item)

    def _enter_rows(self) -> None:
        self._pane = "rows"
        self._menu().index = self._first_row(self._section)
        self._show_section()
        self._sync_keys()

    def _jump_section(self, step: int) -> None:
        self._section = (self._section + step) % len(SECTIONS)
        self._menu().index = self._first_row(self._section)

    # -- editing ---------------------------------------------------------------

    def _activate(self, item: MenuItem) -> None:
        if item.action_id == _BACK:
            self.action_back()
        elif item.action_id in _TOGGLES:
            self.change(item.action_id, 1)  # on/off: Enter flips it, no edit mode
        else:
            self._editing = item.action_id
            self._before = (self._saved_value(item.action_id), self._in_file(item.action_id))
            self._session_view = self.app._view  # the tab, which a view edit moves
            self._changed = False
            self._set_row_editing(True)

    def _step(self, delta: int) -> None:
        if self._editing is not None:
            self._changed = True
            self.change(self._editing, delta)

    def _finish_edit(self) -> None:
        if self._editing is None:
            return
        self._set_row_editing(False)
        self._editing = self._before = None

    def _cancel_edit(self) -> None:
        """Esc: write back the value the row had before editing."""
        action_id, before, changed = self._editing, self._before, self._changed
        self._finish_edit()
        if action_id is None or before is None or not changed:
            return
        value, in_file = before
        app = self.app
        key = action_id.split(":", 1)[1]
        if key == "theme":
            app.apply_theme(value)
        elif key == "view":
            app.apply_view(value)
            app.apply_view(self._session_view, persist=False)  # and the tab it had
        elif key == "threshold":
            app.apply_threshold(value)
        elif key == "threshold5h":
            app.apply_window_gate("5h", value)
        elif key == "threshold7d":
            app.apply_window_gate("7d", value)
        elif key == "fable":
            app._model_thresholds = value
            app._write_optional_setting(_CONFIG_KEYS[action_id], value, app.gettext("Fable gate"))
            app._refresh_accounts_panels()
        elif key == "strategy":
            app.apply_strategy(value)
        if not in_file:  # it was the default: leave no explicit copy behind
            app._write_optional_setting(_CONFIG_KEYS[action_id], None, _CONFIG_KEYS[action_id])
        self._relabel()

    def _saved_value(self, action_id: str):
        app = self.app
        return {
            "setting:theme": app._theme_name,
            "setting:view": app._saved_view,
            "setting:threshold": app.threshold_pct,
            "setting:threshold5h": app._threshold_5h,
            "setting:threshold7d": app._threshold_7d,
            "setting:fable": app._model_thresholds,
            "setting:strategy": app._strategy_name,
        }.get(action_id)

    def _in_file(self, action_id: str) -> bool:
        """Whether settings.json sets this row's key (else it is the default)."""
        import json

        from ccswap.settings import settings_path

        section, _dot, name = _CONFIG_KEYS.get(action_id, ".").partition(".")
        try:
            raw = json.loads(settings_path(self.app.switcher_for("claude").backup_dir).read_text())
        except (OSError, ValueError):
            return False
        node = raw.get(section) if isinstance(raw, dict) else None
        return isinstance(node, dict) and name in node

    def _set_row_editing(self, on: bool) -> None:
        for row in self._menu().query(SettingRow):
            if row.action_id == self._editing:
                row.editing = on
                row.redraw()
        self._draw_help()
        self._sync_keys()

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
        elif key == "star":
            app.apply_star_prompt(not app._star_setting)
        elif key == "strategy":
            # app.open_settings refuses while an AutoScreen is open, so no
            # live engine needs the new value; the next AutoScreen re-reads
            # the file.
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
