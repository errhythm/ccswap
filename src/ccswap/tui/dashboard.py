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

import asyncio
from functools import partial
from typing import TYPE_CHECKING, Callable

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Footer, ListView, Static

from ccswap.i18n import localize_bindings, mark
from ccswap.models import AccountSnapshot, AccountsSnapshot
from ccswap.tui.data import (
    PROVIDER_LABELS,
    PROVIDERS,
    capabilities,
    account_label,
    iter_accounts,
    present_tag,
)
from ccswap.tui.option_help import option_help
from ccswap.tui.settings_screen import SettingsScreen
from ccswap.tui.modals import OutputModal
from ccswap.tui.widgets import (
    AccountItem,
    AccountsPanel,
    CyclingListView,
    MenuItem,
    ProviderDivider,
)

if TYPE_CHECKING:
    from ccswap.tui.app import CswapApp

FLASH_S = 1.5  # how long a just-refreshed row stays highlighted

MenuEntries = list[tuple[str, str]]  # (label, action_id)

_BACK = ("← back", "back")

_LOGIN_LABELS = {
    "claude": mark("Claude Code — current login"),
    "codex": mark("Codex — current login"),
    "agy": mark("Antigravity — current login"),
}


class DashboardScreen(Screen):
    BINDINGS = [
        Binding("s", "open_switch", mark("Switch accounts")),
        Binding("w", "app.open_watch", mark("Watch")),
        Binding("escape,left", "menu_back", mark("Back"), show=False),
        Binding("q", "app.quit", mark("Quit")),
        Binding("question_mark", "option_help", mark("Option help")),
        # Power shortcuts; the menu is the discoverable path.
        Binding("comma", "open_settings", mark("Settings"), show=False),
        Binding("g", "app.open_auto", mark("Auto view"), show=False),
        Binding("f", "app.refresh_full", mark("Refresh usage"), show=False),
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
        localize_bindings(self, self.app.gettext)
        self.query_one("#menu", ListView).focus()
        await self._push_menu("menu", self._root_entries())

    # -- menu plumbing --------------------------------------------------------

    def _root_entries(self) -> MenuEntries:
        # No "Refresh" entry: every view auto-refreshes, so a menu item would
        # wrongly imply the user has to. `f` stays as a hidden escape hatch.
        # An entry shows only when some registered provider can do it.
        offered = {
            "switch": any(capabilities(p).switch for p in PROVIDERS),
            "auto": any(capabilities(p).auto_switch for p in PROVIDERS),
            "disable-menu": any(capabilities(p).disable for p in PROVIDERS),
        }
        entries = [
            (self.app.gettext("Switch account…"), "switch"),
            (self.app.gettext("Watch accounts"), "watch"),
            (self.app.gettext("Auto-switch view…"), "auto"),
            (self.app.gettext("Add account…"), "add-menu"),
            (self.app.gettext("Disable / enable account…"), "disable-menu"),
            (self.app.gettext("Remove account…"), "remove-menu"),
            (self.app.gettext("Settings…"), "settings-menu"),
            (self.app.gettext("Quit"), "quit"),
        ]
        return [e for e in entries if offered.get(e[1], True)]

    def _add_entries(self) -> MenuEntries:
        entries: MenuEntries = []
        for provider in PROVIDERS:
            # Literal msgids keep the shipped translations for the built-ins.
            label = _LOGIN_LABELS.get(provider) or "{label} — current login".format(
                label=PROVIDER_LABELS[provider]
            )
            entries.append((self.app.gettext(label), f"add-login:{provider}"))
            if capabilities(provider).add_token:
                entries.append(
                    (
                        self.app.gettext("Claude Code — setup token or API key…"),
                        "add-token",
                    )
                )
        entries.append(_BACK)
        return entries

    def _auto_entries(self) -> MenuEntries:
        entries = [
            (PROVIDER_LABELS[provider], f"auto:{provider}")
            for provider in PROVIDERS
            if capabilities(provider).auto_switch
            and (snapshot := self.app.snapshots[provider]) is not None
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
                f"{self._account_label(acc)}"
                + (f"  [{self._account_tag(acc)}]" if self.app.tag_shown(acc) else ""),
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
            if not capabilities(provider).disable:
                continue
            name = self._account_label(acc)
            action = (
                self.app.gettext("→ enable")
                if acc.disabled
                else self.app.gettext("→ disable")
            )
            state = self.app.gettext("  (disabled)") if acc.disabled else ""
            entries.append(
                (
                    f"{PROVIDER_LABELS[provider]} — {acc.number}  "
                    f"{name}{state}   {action}",
                    f"disable:{provider}:{acc.number}",
                )
            )
        entries.append(_BACK)
        return entries

    async def _push_menu(self, title: str, entries: MenuEntries) -> None:
        self._menu_stack.append((title, entries))
        await self._render_menu()

    async def _pop_menu(self) -> None:
        if len(self._menu_stack) > 1:
            self._menu_stack.pop()
            await self._render_menu()

    async def _render_menu(self) -> None:
        title, entries = self._menu_stack[-1]
        titles = {
            "menu": mark("menu"),
            "add account": mark("add account"),
            "remove account": mark("remove account"),
            "disable / enable": mark("disable / enable"),
            "auto-switch view": mark("auto-switch view"),
        }
        crumb = " › ".join(
            self.app.gettext(titles.get(t, t)) for t, _ in self._menu_stack
        )
        self.query_one("#menu-title", Static).update(crumb)
        menu = self.query_one("#menu", ListView)
        await menu.clear()
        await menu.extend(
            MenuItem(
                self.app.gettext("← back") if action_id == "back" else label,
                action_id,
                muted=(action_id == "back"),
                help_text=option_help(action_id),
            )
            for label, action_id in entries
        )
        menu.index = 0

    def action_option_help(self) -> None:
        menu = self.query_one("#menu", ListView)
        item = menu.highlighted_child
        if isinstance(item, MenuItem) and (help_text := option_help(item.action_id)):
            self.app.push_screen(OutputModal(self.app.gettext("Option help"), help_text))

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
                (
                    a.email
                    for a in (snap.accounts if snap else ())
                    if a.number == number
                ),
                "?",
            )
            app.confirm_remove(provider, number, email)
        elif action_id == "settings-menu":
            self.action_open_settings()
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
        # `s` must not reach a view the menu hides (no provider can switch).
        if not any(capabilities(p).switch for p in PROVIDERS):
            return
        if not isinstance(self.app.screen, SwitchScreen):
            self.app.push_screen(SwitchScreen())

    def action_open_settings(self) -> None:
        if not isinstance(self.app.screen, SettingsScreen):
            self.app.push_screen(SettingsScreen())

    async def action_menu_back(self) -> None:
        await self._pop_menu()

    def action_cursor_down(self) -> None:
        self.query_one("#menu", ListView).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#menu", ListView).action_cursor_up()

    def _account_label(self, acc: AccountSnapshot) -> str:
        return account_label(acc.alias, acc.email, mask=self.app.mask_accounts)

    def _account_tag(self, acc: AccountSnapshot) -> str:
        return present_tag(acc.display_tag, mask=self.app.mask_accounts)

    def refresh_masked_labels(self) -> None:
        """Rebuild an open menu whose rows contain account identities."""
        if self.is_mounted and self._menu_stack:
            self.call_after_refresh(self._refresh_open_menu)

    async def _refresh_open_menu(self) -> None:
        if not self._menu_stack:
            return
        title = self._menu_stack[-1][0]
        builders = {
            "remove account": self._remove_entries,
            "disable / enable": self._disable_entries,
        }
        builder = builders.get(title)
        if builder is None:
            return
        menu = self.query_one("#menu", ListView)
        index = menu.index
        self._menu_stack[-1] = (title, builder())
        await self._render_menu()
        if index is not None and menu.children:
            menu.index = min(index, len(menu.children) - 1)


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
        self._rebuild_lock = asyncio.Lock()

    def compose(self) -> ComposeResult:
        yield Static("", id="list-title")
        yield CyclingListView(id="accounts")
        yield Footer()

    def on_mount(self) -> None:
        localize_bindings(self, self.app.gettext)
        self.watch(self.app, "snapshots", self._on_snapshot)

    async def _on_snapshot(self, snapshots: dict[str, AccountsSnapshot | None]) -> None:
        # Serialize rebuilds: two snapshots arriving while clear()/extend()
        # are awaited would both rebuild and duplicate provider sections.
        async with self._rebuild_lock:
            await self._rebuild(snapshots)

    async def _rebuild(self, snapshots: dict[str, AccountsSnapshot | None]) -> None:
        rows = iter_accounts(
            snapshots,
            None if self.app._view == "combined" else self.app._view,
        )
        self._rows = rows
        listview = self.query_one("#accounts", ListView)
        children = []
        keys: list[tuple[str, str | None]] = []
        for provider in PROVIDERS:
            provider_rows = [
                acc for row_provider, acc in rows if row_provider == provider
            ]
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
                self._index_after_build(rows, first_build, previous) if keys else None
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
            provider_rows = [
                acc for row_provider, acc in rows if row_provider == provider
            ]
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
            (index for index, (_provider, acc) in zip(indices, rows) if acc.is_active),
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
                if (item.provider, item.number) in changed and not item.has_class(
                    "flash"
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
        Binding("enter", "select_highlighted", mark("Switch"), priority=True),
        Binding("b", "switch_best", mark("Switch (best)")),
        Binding("escape,q,s", "back", mark("Back")),
        Binding("j", "cursor_down", show=False),
        Binding("k", "cursor_up", show=False),
    ]

    def on_mount(self) -> None:
        localize_bindings(self, self.app.gettext)
        self.query_one("#list-title", Static).update(
            self.app.gettext("switch to which account?")
        )
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
        if isinstance(item, AccountItem) and "best" in capabilities(item.provider).strategies:
            self.app.do_switch_best(item.provider)

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        if action == "switch_best":
            item = self.query_one("#accounts", ListView).highlighted_child
            if isinstance(item, AccountItem) and not "best" in capabilities(item.provider).strategies:
                return False
        return True

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        self.refresh_bindings()

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

    _WATCH_TITLE = mark("watching all accounts")
    _SELECT_TITLE = mark("switch to which account? · enter confirm · esc cancel")

    BINDINGS = [
        Binding("s", "toggle_select", mark("Switch")),
        Binding("enter", "select_highlighted", mark("Confirm"), priority=True),
        Binding("f", "app.refresh_full", mark("Refresh"), show=False),
        Binding("escape,q", "back", mark("Back")),
        Binding("down,j", "nav_down", show=False),
        Binding("up,k", "nav_up", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._selecting = False

    def on_mount(self) -> None:
        localize_bindings(self, self.app.gettext)
        self.watch(self.app, "refresh_status", self._on_refresh_status)
        self.watch(self.app, "mask_accounts", self._on_refresh_status)
        self.query_one("#list-title", Static).update(self._title_text())
        super().on_mount()

    @property
    def locked(self) -> bool:
        """Read-only until ``s`` arms selection; the palette honours this."""
        return not self._selecting

    def _title_text(self) -> str:
        if self._selecting:
            return self.app.gettext(self._SELECT_TITLE)
        title = self.app.gettext(self._WATCH_TITLE)
        if self.app.mask_accounts:
            title += self.app.gettext(" · masked")
        status = self.app.refresh_status
        return f"{title} · {status}" if status else title

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
