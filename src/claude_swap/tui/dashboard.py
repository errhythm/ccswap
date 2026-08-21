"""Dashboard: static account overview on top, a nested action menu below.

The accounts panel is the monitor (active account full-size, others as
one-line minis); the arrow keys drive the *menu*, not the accounts. Anything
account-targeted opens a context of its own:

- ``s`` / menu "Switch account" → :class:`SwitchScreen` — every account
  full-size, Enter switches, pops back.
- ``w`` / menu "Watch accounts" / ``cswap watch`` → :class:`WatchScreen` —
  the same full cards but read-only: a live monitor. ``s`` arms selection
  (cursor appears on the active account), Enter switches and *stays
  watching*, Esc disarms.
- "Remove account" nests into a submenu listing the accounts.

No global command palette: actions live where their context is.
"""

from __future__ import annotations

from bisect import bisect_left
from functools import partial
from typing import TYPE_CHECKING, Callable

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Footer, ListView, Static

from claude_swap.models import AccountSnapshot, AccountsSnapshot
from claude_swap.tui.data import PROVIDERS, PROVIDER_LABELS, iter_accounts
from claude_swap.tui.widgets import (
    AccountItem,
    AccountsPanel,
    CyclingListView,
    MenuItem,
    ProviderDivider,
)

if TYPE_CHECKING:
    from claude_swap.tui.app import CswapApp

FLASH_S = 1.5  # how long a just-refreshed row stays highlighted
DEFAULT_THRESHOLD_PCT = 90.0

MenuEntries = list[tuple[str, str]]  # (label, action_id)

_BACK = ("← back", "back")


class DashboardScreen(Screen):
    BINDINGS = [
        Binding("s", "open_switch", "Switch accounts"),
        Binding("w", "app.open_watch", "Watch"),
        Binding("escape,left", "menu_back", "Back", show=False),
        Binding("q", "app.quit", "Quit"),
        # Power shortcuts; the menu is the discoverable path.
        Binding("g", "app.open_auto", "Auto view", show=False),
        Binding("f", "app.refresh_full", "Refresh usage", show=False),
        Binding("j", "cursor_down", show=False),
        Binding("k", "cursor_up", show=False),
    ]

    app: "CswapApp"

    def __init__(self) -> None:
        super().__init__()
        # Stack of (title, entries); depth 1 = root menu.
        self._menu_stack: list[tuple[str, MenuEntries]] = []

    def compose(self) -> ComposeResult:
        yield AccountsPanel(
            provider=None if self.app._view == "combined" else self.app._view,
            id="accounts-panel",
        )
        yield Static("", id="menu-title")
        yield CyclingListView(id="menu")
        yield Footer()

    async def on_mount(self) -> None:
        self.query_one("#menu", ListView).focus()
        await self._push_menu("menu", self._root_entries())

    # -- menu plumbing --------------------------------------------------------

    def _root_entries(self) -> MenuEntries:
        # No "Refresh" entry: every view auto-refreshes, so a menu item would
        # wrongly imply the user has to. `f` stays as a hidden escape hatch.
        return [
            ("Switch account…", "switch"),
            ("Watch accounts", "watch"),
            ("Auto-switch view…", "auto"),
            ("Add account…", "add-menu"),
            ("Disable / enable account…", "disable-menu"),
            ("Remove account…", "remove-menu"),
            ("Settings…", "settings-menu"),
            ("Quit", "quit"),
        ]

    def _add_entries(self) -> MenuEntries:
        return [
            ("Claude Code — current login", "add-login:claude"),
            ("Claude Code — setup token or API key…", "add-token"),
            ("Codex — current login", "add-login:codex"),
            _BACK,
        ]

    def _auto_entries(self) -> MenuEntries:
        entries = [
            (PROVIDER_LABELS[provider], f"auto:{provider}")
            for provider in PROVIDERS
            if (snapshot := self.app.snapshots[provider]) is not None
            and snapshot.accounts
        ]
        entries.append(_BACK)
        return entries

    def _remove_entries(self) -> MenuEntries:
        # Deliberately unfiltered: ui.view is a dashboard display preference,
        # not an account scope, so destructive menus must not hide accounts.
        entries: MenuEntries = [
            (
                f"{PROVIDER_LABELS[provider]} — {acc.number}  "
                f"{f'{acc.alias} ({acc.email})' if acc.alias else acc.email}"
                f"  [{acc.display_tag}]",
                f"remove:{provider}:{acc.number}",
            )
            for provider, acc in iter_accounts(self.app.snapshots)
        ]
        entries.append(_BACK)
        return entries

    def _disable_entries(self) -> MenuEntries:
        """One row per account, labelled with its current state and the action
        selecting it will take (enable a disabled one, disable an active one)."""
        # Deliberately unfiltered: ui.view is a dashboard display preference,
        # not an account scope, so administrative menus must not hide accounts.
        entries: MenuEntries = []
        for provider, acc in iter_accounts(self.app.snapshots):
            name = f"{acc.alias} ({acc.email})" if acc.alias else acc.email
            action = "→ enable" if acc.disabled else "→ disable"
            state = "  (disabled)" if acc.disabled else ""
            entries.append(
                (
                    f"{PROVIDER_LABELS[provider]} — {acc.number}  "
                    f"{name}{state}   {action}",
                    f"disable:{provider}:{acc.number}",
                )
            )
        entries.append(_BACK)
        return entries

    def _settings_entries(self) -> MenuEntries:
        """Inline preference cycles keep settings one level deep."""
        view_labels = {
            "combined": "Combined",
            "claude": PROVIDER_LABELS["claude"],
            "codex": PROVIDER_LABELS["codex"],
        }
        threshold = self.app.threshold_pct
        threshold_value = (
            threshold if threshold is not None else DEFAULT_THRESHOLD_PCT
        )
        threshold_label = f"{threshold_value:g}"
        return [
            (f"Theme: {self.app._theme_name}", "setting:theme"),
            (f"Dashboard view: {view_labels[self.app._view]}", "setting:view"),
            (f"Auto-switch threshold: {threshold_label}%", "setting:threshold"),
            (f"Auto-switch strategy: {self.app._strategy_name}", "setting:strategy"),
            _BACK,
        ]

    async def _push_menu(self, title: str, entries: MenuEntries) -> None:
        self._menu_stack.append((title, entries))
        await self._render_menu()

    async def _pop_menu(self) -> None:
        if len(self._menu_stack) > 1:
            self._menu_stack.pop()
            await self._render_menu()

    async def _render_menu(self) -> None:
        title, entries = self._menu_stack[-1]
        crumb = " › ".join(t for t, _ in self._menu_stack)
        self.query_one("#menu-title", Static).update(crumb)
        menu = self.query_one("#menu", ListView)
        await menu.clear()
        await menu.extend(
            MenuItem(label, action_id, muted=(action_id == "back"))
            for label, action_id in entries
        )
        menu.index = 0

    async def on_list_view_selected(self, event: ListView.Selected) -> None:
        item = event.item
        if isinstance(item, MenuItem):
            await self._dispatch(item.action_id)

    async def _dispatch(self, action_id: str) -> None:
        app = self.app
        actions: dict[str, Callable[[], None]] = {
            "switch": self.action_open_switch,
            "watch": app.action_open_watch,
            "add-token": app.action_add_token,
            "quit": app.exit,
        }
        if action_id == "back":
            await self._pop_menu()
        elif action_id == "add-menu":
            await self._push_menu("add account", self._add_entries())
        elif action_id.startswith("add-login:"):
            app.do_add_current(action_id.split(":", 1)[1])
        elif action_id == "auto":
            entries = self._auto_entries()
            if len(entries) == 2:  # one provider plus the back row
                app.open_auto(entries[0][1].split(":", 1)[1])
            elif len(entries) > 2:
                await self._push_menu("auto-switch view", entries)
            else:
                app.action_open_auto()
        elif action_id.startswith("auto:"):
            app.open_auto(action_id.split(":", 1)[1])
        elif action_id == "remove-menu":
            await self._push_menu("remove account", self._remove_entries())
        elif action_id.startswith("remove:"):
            _, provider, number = action_id.split(":", 2)
            snap = app.snapshots[provider]
            email = next(
                (a.email for a in (snap.accounts if snap else ()) if a.number == number),
                "?",
            )
            app.confirm_remove(provider, number, email)
        elif action_id == "settings-menu":
            await self._push_menu("settings", self._settings_entries())
        elif action_id.startswith("setting:"):
            key = action_id.split(":", 1)[1]
            if key == "theme":
                order = ("dark", "light", "auto")
                value = order[(order.index(app._theme_name) + 1) % len(order)]
                app.apply_theme(value)
                app.notify(f"Theme: {value}")
            elif key == "view":
                order = ("combined", "claude", "codex")
                value = order[(order.index(app._view) + 1) % len(order)]
                app.apply_view(value)
            elif key == "threshold":
                # ponytail: 95% is this compact TUI ladder's ceiling; use
                # `ccswap config set autoswitch.threshold` for exact values.
                ladder = (75.0, 80.0, 85.0, 90.0, 95.0)
                current = (
                    app.threshold_pct
                    if app.threshold_pct is not None
                    else DEFAULT_THRESHOLD_PCT
                )
                index = bisect_left(ladder, current)
                if index < len(ladder) and ladder[index] == current:
                    index += 1
                app.apply_threshold(ladder[index % len(ladder)])
            elif key == "strategy":
                order = ("best", "consume-first")
                value = order[(order.index(app._strategy_name) + 1) % len(order)]
                # AutoScreen owns a running engine for its whole lifetime.
                # Reaching Settings first unmounts it and stops that engine,
                # and the next AutoScreen re-reads this file: no live strategy
                # reach-in or misleading warning is needed. Its on_unmount
                # threshold restore cannot strand a Settings change either,
                # because Settings is unreachable until that unmount. This
                # depends on AutoScreen staying top for its engine's whole
                # lifetime; if the screen model changes, guards belong here.
                app.apply_strategy(value)
            menu = self.query_one("#menu", ListView)
            index = menu.index
            self._menu_stack[-1] = ("settings", self._settings_entries())
            await self._render_menu()
            # Unlike a new menu, cycling refreshes this menu in place: keep its row.
            menu.index = index
        elif action_id == "disable-menu":
            await self._push_menu("disable / enable", self._disable_entries())
        elif action_id.startswith("disable:"):
            _, provider, number = action_id.split(":", 2)
            app.do_toggle_disabled(provider, number)
            await self._pop_menu()
        else:
            actions[action_id]()

    # -- actions ----------------------------------------------------------------

    def action_open_switch(self) -> None:
        if not isinstance(self.app.screen, SwitchScreen):
            self.app.push_screen(SwitchScreen())

    async def action_menu_back(self) -> None:
        await self._pop_menu()

    def action_cursor_down(self) -> None:
        self.query_one("#menu", ListView).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#menu", ListView).action_cursor_up()


class AccountListScreen(Screen):
    """Shared machinery: a live ListView of full account cards.

    Subclasses decide what the cursor does — :class:`SwitchScreen` is
    selection-first, :class:`WatchScreen` is a monitor that can arm
    selection on demand.
    """

    app: "CswapApp"

    def __init__(self) -> None:
        super().__init__()
        self._keys: list[tuple[str, str | None]] = []
        self._rows: list[tuple[str, AccountSnapshot]] = []
        self._stamps: dict[tuple[str, str], float | None] = {}

    def compose(self) -> ComposeResult:
        yield Static("", id="list-title")
        yield CyclingListView(id="accounts")
        yield Footer()

    def on_mount(self) -> None:
        self.watch(self.app, "snapshots", self._on_snapshot)

    async def _on_snapshot(
        self, snapshots: dict[str, AccountsSnapshot | None]
    ) -> None:
        rows = iter_accounts(
            snapshots,
            None if self.app._view == "combined" else self.app._view,
        )
        self._rows = rows
        listview = self.query_one("#accounts", ListView)
        children = []
        keys: list[tuple[str, str | None]] = []
        for provider in PROVIDERS:
            provider_rows = [acc for row_provider, acc in rows if row_provider == provider]
            if not provider_rows:
                continue
            children.append(ProviderDivider(provider))
            keys.append((provider, None))
            children.extend(AccountItem(acc, provider) for acc in provider_rows)
            keys.extend((provider, acc.number) for acc in provider_rows)
        if keys != self._keys:
            first_build = not self._keys
            previous = listview.index
            await listview.clear()
            await listview.extend(children)
            self._keys = keys
            listview.index = (
                self._index_after_build(rows, first_build, previous)
                if keys
                else None
            )
        else:
            for item, (_provider, acc) in zip(listview.query(AccountItem), rows):
                item.set_account(acc)
        self._flash_updated(rows, listview)

    def _index_after_build(
        self,
        rows: list[tuple[str, AccountSnapshot]],
        first_build: bool,
        previous: int | None,
    ) -> int | None:
        """Where the cursor lands after the list is (re)built."""
        indices = self._account_child_indices(rows)
        if not indices:
            return None
        if first_build:
            return self._active_index(rows)
        target = previous or 0
        return min(indices, key=lambda index: abs(index - target))

    @staticmethod
    def _account_child_indices(rows: list[tuple[str, AccountSnapshot]]) -> list[int]:
        """Child indices occupied by account rows after provider headings."""
        indices: list[int] = []
        child_index = 0
        for provider in PROVIDERS:
            provider_rows = [acc for row_provider, acc in rows if row_provider == provider]
            if not provider_rows:
                continue
            child_index += 1  # the ProviderDivider for this non-empty section
            indices.extend(range(child_index, child_index + len(provider_rows)))
            child_index += len(provider_rows)
        return indices

    def _active_index(self, rows: list[tuple[str, AccountSnapshot]]) -> int:
        # There are two legitimate active accounts. The flat list starts at
        # the first active row rather than consulting either provider's
        # active_number as though it were globally unique.
        indices = self._account_child_indices(rows)
        return next(
            (
                index
                for index, (_provider, acc) in zip(indices, rows)
                if acc.is_active
            ),
            indices[0] if indices else 0,
        )

    def _flash_updated(
        self, rows: list[tuple[str, AccountSnapshot]], listview: ListView
    ) -> None:
        """Briefly highlight rows whose stored measurement just advanced."""
        new_stamps = {
            (provider, acc.number): acc.usage.fetched_at for provider, acc in rows
        }
        if self._stamps:
            changed = {
                num
                for num, ts in new_stamps.items()
                if ts is not None and ts != self._stamps.get(num)
            }
            for item in listview.query(AccountItem):
                if (
                    (item.provider, item.number) in changed
                    and not item.has_class("flash")
                ):
                    item.add_class("flash")
                    self.set_timer(FLASH_S, partial(item.remove_class, "flash"))
        self._stamps = new_stamps

    def action_cursor_down(self) -> None:
        self.query_one("#accounts", ListView).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#accounts", ListView).action_cursor_up()


class SwitchScreen(AccountListScreen):
    """All accounts, full-size and alive: arrows pick, Enter switches."""

    BINDINGS = [
        # priority: outranks the focused ListView's own (hidden) enter binding
        # so "Switch" is visible in the footer; the action delegates right back
        # to the list cursor, so behavior is identical.
        Binding("enter", "select_highlighted", "Switch", priority=True),
        Binding("b", "switch_best", "Best / next"),
        Binding("escape,q,s", "back", "Back"),
        Binding("j", "cursor_down", show=False),
        Binding("k", "cursor_up", show=False),
    ]

    def on_mount(self) -> None:
        self.query_one("#list-title", Static).update("switch to which account?")
        self.query_one("#accounts", ListView).focus()
        super().on_mount()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        item = event.item
        if isinstance(item, AccountItem):
            self.app.do_switch(item.provider, item.number)
            self.app.pop_screen()

    def action_switch_best(self) -> None:
        listview = self.query_one("#accounts", ListView)
        item = listview.highlighted_child
        if isinstance(item, AccountItem):
            self.app.do_switch_best(item.provider)

    def action_select_highlighted(self) -> None:
        listview = self.query_one("#accounts", ListView)
        if listview.display:
            listview.action_select_cursor()

    def action_back(self) -> None:
        self.app.pop_screen()


class WatchScreen(AccountListScreen):
    """Live monitor of every account, full detail, hands-off by default.

    ``s`` arms selection (cursor appears on the active account); Enter then
    switches and stays here — you keep watching on the new account. Esc
    disarms selection first, then leaves the screen.
    """

    _WATCH_TITLE = "watching all accounts"
    _SELECT_TITLE = "switch to which account? · enter confirm · esc cancel"

    BINDINGS = [
        Binding("s", "toggle_select", "Switch"),
        Binding("enter", "select_highlighted", "Confirm", priority=True),
        Binding("f", "app.refresh_full", "Refresh", show=False),
        Binding("escape,q", "back", "Back"),
        Binding("down,j", "nav_down", show=False),
        Binding("up,k", "nav_up", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._selecting = False

    def on_mount(self) -> None:
        self.watch(self.app, "refresh_status", self._on_refresh_status)
        self.query_one("#list-title", Static).update(self._title_text())
        super().on_mount()

    def _title_text(self) -> str:
        if self._selecting:
            return self._SELECT_TITLE
        status = self.app.refresh_status
        return f"{self._WATCH_TITLE} · {status}" if status else self._WATCH_TITLE

    def _on_refresh_status(self, status: str) -> None:
        if not self._selecting:
            self.query_one("#list-title", Static).update(self._title_text())

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        if action == "select_highlighted" and not self._selecting:
            return False  # hidden and inert until selection is armed
        return True

    def _index_after_build(
        self,
        rows: list[tuple[str, AccountSnapshot]],
        first_build: bool,
        previous: int | None,
    ) -> int | None:
        if not self._selecting:
            return None  # monitor mode: no cursor at all
        return super()._index_after_build(rows, first_build, previous)

    def _set_selecting(self, on: bool) -> None:
        self._selecting = on
        listview = self.query_one("#accounts", ListView)
        title = self.query_one("#list-title", Static)
        if on:
            if self._rows:
                listview.index = self._active_index(self._rows)
            listview.focus()
            title.update(self._SELECT_TITLE)
        else:
            listview.index = None
            self.set_focus(None)
            title.update(self._title_text())
        self.refresh_bindings()

    def action_toggle_select(self) -> None:
        self._set_selecting(not self._selecting)

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if not self._selecting:
            return  # e.g. a stray click while just watching
        item = event.item
        if isinstance(item, AccountItem):
            self.app.do_switch(item.provider, item.number)
            self._set_selecting(False)  # stay here, keep watching

    def action_select_highlighted(self) -> None:
        if self._selecting:
            self.query_one("#accounts", ListView).action_select_cursor()

    def action_back(self) -> None:
        if self._selecting:
            self._set_selecting(False)
        else:
            self.app.pop_screen()

    def action_nav_down(self) -> None:
        listview = self.query_one("#accounts", ListView)
        if self._selecting:
            listview.action_cursor_down()
        else:
            listview.scroll_down(animate=False)

    def action_nav_up(self) -> None:
        listview = self.query_one("#accounts", ListView)
        if self._selecting:
            listview.action_cursor_up()
        else:
            listview.scroll_up(animate=False)
