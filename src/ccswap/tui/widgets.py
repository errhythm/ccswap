"""Shared widgets: the accounts panel, account cards and list rows, menus.

The account text itself comes from ``ccswap.tui.overview`` (gauges, rows);
this module keeps the credit/allowance wording it shares.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable

from rich.text import Text
from textual.containers import VerticalScroll
from textual.widgets import ListItem, ListView, Static

from ccswap.models import AccountSnapshot
from ccswap.tui import data
from ccswap.tui.data import PROVIDER_LABELS, PROVIDERS
from ccswap.tui.overview import ProviderPanel, account_block
from ccswap.tui.theme import Palette

if TYPE_CHECKING:
    from ccswap.tui.app import CswapApp

def credits_text(
    last_good: dict | None,
    now: float,
    *,
    compact: bool = False,
    tr: Callable[[str], str] = str,
) -> str | None:
    """Codex workspace/purchased credit balance."""
    del now
    if not isinstance(last_good, dict):
        return None
    credits = last_good.get("credits")
    if not isinstance(credits, dict):
        return None
    if credits.get("limit_reached") is True:
        return tr("limit reached")
    if credits.get("unlimited") is True:
        return tr("unlimited")
    if credits.get("has_credits") is False:
        return tr("none")

    balance = credits.get("balance")
    if isinstance(balance, (int, float)):
        return tr("{balance:,.2f} available").format(balance=balance)
    if credits.get("has_credits") is True:
        return tr("available") if compact else tr("available · balance not reported")
    return None


def credit_allowance_text(
    last_good: dict | None,
    now: float,
    *,
    compact: bool = False,
    tr: Callable[[str], str] = str,
) -> str | None:
    """Per-user monthly workspace allowance, separate from credit balance."""
    if not isinstance(last_good, dict):
        return None
    allowance = last_good.get("credit_allowance")
    if not isinstance(allowance, dict):
        return None

    remaining = allowance.get("remaining")
    limit = allowance.get("limit")
    if allowance.get("limit_reached") is True:
        text = tr("limit reached")
    elif isinstance(remaining, (int, float)):
        text = tr("{remaining:,.2f} remaining").format(remaining=remaining)
    elif isinstance(limit, (int, float)):
        text = tr("{limit:,.2f} limit").format(limit=limit)
    else:
        return None
    if (
        not compact
        and isinstance(limit, (int, float))
        and isinstance(remaining, (int, float))
    ):
        text += tr(" · {limit:,.2f} limit").format(limit=limit)
    reset = data.reset_text({"resets_at": allowance.get("resets_at")}, now, tr=tr)
    if reset and not compact:
        text += f" · {reset}"
    return text


# The CLI command that saves each provider's current login.
ADD_COMMANDS = {"claude": "ccswap add", "codex": "ccswap codex add", "agy": "ccswap agy add"}


def empty_state(
    providers: tuple[str, ...], palette: Palette, tr: Callable[[str], str] = str
) -> Text:
    """No accounts in the view: how to save a login, one line per provider shown."""
    text = Text(tr("No accounts yet. Sign in to a tool, then save that login:"), style=palette.muted)
    text.append("\n")
    name_w = max(len(PROVIDER_LABELS[p]) for p in providers)
    for provider in providers:
        text.append("\n  ")
        text.append(PROVIDER_LABELS[provider].ljust(name_w), style=f"bold {palette.provider(provider)}")
        text.append("   ")
        text.append(ADD_COMMANDS.get(provider, f"ccswap {provider} add"), style=palette.foreground)
    text.append("\n\n")
    text.append(tr("Or pick "), style=palette.muted)
    text.append(tr("Add account…"), style=palette.foreground)
    text.append(tr(" in the menu below."), style=palette.muted)
    return text


class AccountsPanel(VerticalScroll, can_focus=False):
    """Static account overview: one rounded panel per provider, the active
    account full-size and the others as one-line rows. The dashboard's (and,
    with ``show_minis=False``, the auto and settings screens') monitor.

    The panels are composed once, one per registered provider, and only
    repaint on new snapshots, so a provider can never be drawn twice."""

    def __init__(
        self,
        *,
        show_minis: bool = True,
        provider: str | None = None,
        id: str | None = None,
    ) -> None:
        super().__init__(id=id, classes="accounts-panel")
        self._show_minis = show_minis
        self._provider = provider

    @property
    def provider(self) -> str | None:
        return self._provider

    @provider.setter
    def provider(self, value: str | None) -> None:
        self._provider = value
        if self.is_mounted:
            self._sync()

    def compose(self):
        yield Static("", classes="panel-empty")
        for provider in PROVIDERS:
            yield ProviderPanel(provider, show_minis=self._show_minis)

    def on_resize(self) -> None:
        # The screen's breakpoint class changes with the same resize; repaint
        # once it has, so -narrow / -wide take effect live.
        self.call_after_refresh(self._sync)

    def on_mount(self) -> None:
        self.watch(self.app, "snapshots", lambda _snap: self._sync())
        self.watch(self.app, "theme", lambda _t: self._sync())
        self.watch(self.app, "mask_accounts", lambda _on: self._sync())
        self.set_interval(30.0, self._sync)

    def refresh(self, *args, **kwargs):  # noqa: D102 - repaint the panels too
        if self.is_mounted:
            for panel in self.query(ProviderPanel):
                panel.refresh(layout=True)
        return super().refresh(*args, **kwargs)

    def _sync(self) -> None:
        app: "CswapApp" = self.app  # type: ignore[assignment]
        providers = (self._provider,) if self._provider is not None else tuple(PROVIDERS)
        for panel in self.query(ProviderPanel):
            snap = app.snapshots.get(panel.provider)
            # A configured provider with zero accounts stays out of the way.
            panel.display = panel.provider in providers and (snap is None or bool(snap.accounts))
            panel.update_border()
        empty = self.query_one(".panel-empty", Static)
        none = all(
            app.snapshots.get(p) is not None and not app.snapshots[p].accounts
            for p in providers
        )
        empty.display = none
        empty.update(
            empty_state(providers, Palette.from_theme(app.current_theme), app.gettext)
            if none
            else ""
        )
        self.refresh(layout=True)

    def render_text(self) -> Text:
        """Every shown panel's title and content as one Text (tests, logs)."""
        text = Text()
        empty = self.query_one(".panel-empty", Static)
        if empty.display:
            return Text(str(empty.render()))
        for panel in self.query(ProviderPanel):
            if not panel.display:
                continue
            if text:
                text.append("\n\n")
            text.append(f"{PROVIDER_LABELS[panel.provider]}  {panel.border_subtitle}\n")
            text.append(panel.render())
        return text


class AccountCard(Static):
    """One account rendered full-size (the switch and watch lists)."""

    def __init__(
        self,
        acc: AccountSnapshot,
        *,
        provider: str = "claude",
        threshold: float | None = None,
        tag: str | None = None,
    ) -> None:
        super().__init__()
        self._acc = acc
        self._provider = provider
        self._threshold = threshold
        self._tag = tag

    def set_account(self, acc: AccountSnapshot) -> None:
        self._acc = acc
        self.refresh(layout=True)

    def on_mount(self) -> None:
        self.watch(self.app, "mask_accounts", lambda _on: self.refresh(layout=True))
        self.set_interval(30.0, lambda: self.refresh(layout=True))

    def on_resize(self) -> None:
        self.call_after_refresh(self.refresh, layout=True)  # see AccountsPanel

    def render(self) -> Text:
        item = self.parent
        highlighted = isinstance(item, ListItem) and item.highlighted
        return account_block(
            self._acc,
            self._provider,
            width=max(20, self.content_size.width or 80),
            now=overview_clock(),
            palette=Palette.from_theme(self.app.current_theme),
            mask=bool(getattr(self.app, "mask_accounts", False)),
            show_tag=self.app.tag_shown(self._acc),
            narrow=self.screen.has_class("-narrow"),
            marker="› " if highlighted else "  ",
            tag=self._tag,
        )


class AccountItem(ListItem):
    """ListView row wrapping an :class:`AccountCard`; remembers its slot."""

    def __init__(self, acc: AccountSnapshot, provider: str) -> None:
        super().__init__(AccountCard(acc, provider=provider))
        self.provider = provider
        self.number = acc.number
        self.email = acc.email

    def watch_highlighted(self, value: bool) -> None:
        super().watch_highlighted(value)
        self.query_one(AccountCard).refresh()

    def set_account(self, acc: AccountSnapshot) -> None:
        self.number = acc.number
        self.email = acc.email
        self.query_one(AccountCard).set_account(acc)


class ProviderDivider(ListItem):
    """A non-selectable provider heading inside an account list: the top edge
    of a rounded panel (the rows below draw its sides, the last one its
    bottom), with the provider's name as the border title."""

    def __init__(self, provider: str) -> None:
        super().__init__(disabled=True, classes=f"-provider-{provider}")
        self.provider = provider
        self.border_title = PROVIDER_LABELS[provider]

    def render(self) -> Text:
        return Text()


class CyclingListView(ListView):
    """A ListView whose cursor wraps at both ends."""

    def _selectable_from(self, start: int, step: int) -> int | None:
        """Find the next enabled child while walking circularly from ``start``."""
        for offset in range(len(self.children)):
            index = (start + offset * step) % len(self.children)
            if not self.children[index].disabled:
                return index
        return None

    def action_cursor_up(self) -> None:
        if not self.children:
            return
        start = len(self.children) - 1 if self.index is None else self.index - 1
        # ListView's stock one-way movement skips disabled rows, but wrapping
        # here bypasses it; provider headings must never become a cursor stop.
        self.index = self._selectable_from(start, -1)

    def action_cursor_down(self) -> None:
        if not self.children:
            return
        start = 0 if self.index is None else self.index + 1
        self.index = self._selectable_from(start, 1)


class MenuItem(ListItem):
    """One menu row: a label plus an action id the screen dispatches on."""

    def __init__(
        self, label: str, action_id: str, *, muted: bool = False, help_text: str | None = None
    ) -> None:
        item = Static(label, markup=False)
        if muted:
            item.add_class("menu-item-muted")
        super().__init__(item)
        self.action_id = action_id
        self.tooltip = Text(help_text) if help_text else None


def overview_clock() -> float:
    from ccswap.tui import overview

    return overview.clock()
