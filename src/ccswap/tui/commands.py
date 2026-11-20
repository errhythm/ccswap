"""Command palette (``ctrl+p``): every command calls an existing app method.

No logic lives here. Each command's help line names the CLI command it matches.
Static commands reach the palette through ``CswapApp.get_system_commands``
(``static_commands``); the per-account ones (switch to N, remove N, ...) come
from ``AccountCommands``, a Provider that only searches (its ``discover`` is
empty, so it costs nothing before the user types).

A watch screen that has not armed selection (``s``) is read-only, and that holds
on whatever screen sits above it: only commands that change nothing are offered.
The empty-search list leads with commands that change nothing (``lead``), so
ctrl+p then Enter never mutates.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Callable

from textual.command import Hit, Hits, Provider

from ccswap.tui.autoview import AutoScreen
from ccswap.tui.dashboard import WatchScreen
from ccswap.tui.data import PROVIDER_LABELS, PROVIDERS, capabilities, mask_email
from ccswap.tui.modals import ConfirmModal
from ccswap.tui.settings_screen import SettingsScreen

if TYPE_CHECKING:
    from ccswap.tui.app import CswapApp

# How each provider spells the CLI command that the palette mirrors.
_CLI = {"claude": "ccswap", "codex": "ccswap codex", "agy": "ccswap agy"}


@dataclass(frozen=True)
class Command:
    label: str
    help: str
    run: Callable[[], None]
    lead: bool = False  # listed before the user types: changes nothing at all
    ok_locked: bool = False  # still offered while a watch screen is locked


def is_locked(app: "CswapApp") -> bool:
    return any(isinstance(s, WatchScreen) and s.locked for s in app.screen_stack)


def _visible(app: "CswapApp", commands: list[Command]) -> list[Command]:
    return [c for c in commands if c.ok_locked] if is_locked(app) else commands


def _who(app: "CswapApp", acc) -> str:
    return mask_email(acc.email) if app.mask_accounts and acc.email else acc.email


def _switch_or_ask(app: "CswapApp", provider: str, run: Callable[[], None]) -> Callable[[], None]:
    """Codex and Antigravity need a restart after a switch: ask first."""
    if not capabilities(provider).restart_after_switch:
        return run

    def ask() -> None:
        app.push_screen(
            ConfirmModal(
                app.gettext(
                    "Switch {provider}? Restart its sessions afterwards to load the new login."
                ).format(provider=PROVIDER_LABELS[provider]),
                title=app.gettext("Switch account"),
                yes_label=app.gettext("Switch"),
            ),
            lambda yes: run() if yes else None,
        )

    return ask


def static_commands(app: "CswapApp") -> list[Command]:
    tr = app.gettext
    out: list[Command] = []

    def add(label, help, run, *, lead=False, ok=False) -> None:
        out.append(Command(label, help, run, lead, ok))

    providers_with = [
        p for p in PROVIDERS if (s := app.snapshots.get(p)) is not None and s.accounts
    ]

    # -- read-only (these lead the empty-search list) -----------------------------
    add(tr("Refresh usage"), tr("Same as ccswap list"), app.action_refresh_full, lead=True, ok=True)
    add(tr("Watch accounts"), tr("Same as ccswap watch"), app.action_open_watch, lead=True, ok=True)
    for p in providers_with:
        if capabilities(p).auto_switch and not any(
            isinstance(screen, (AutoScreen, SettingsScreen)) for screen in app.screen_stack
        ):
            add(
                tr("Open auto-switch: {provider}").format(provider=PROVIDER_LABELS[p]),
                tr("Same as {cli} auto --dry-run (l goes live)").format(cli=_CLI.get(p, "ccswap")),
                partial(app.open_auto, p),
                lead=True, ok=True,
            )
    add(tr("Keys"), tr("Show the key help"), app.action_show_help_panel, lead=True, ok=True)

    # -- accounts -----------------------------------------------------------------
    for p in providers_with:
        if len(app.snapshots[p].accounts) > 1 and "best" in capabilities(p).strategies:
            add(
                tr("Switch {provider} using {strategy}").format(
                    provider=PROVIDER_LABELS[p], strategy="best"
                ),
                tr("Same as {cli} switch --strategy best").format(cli=_CLI.get(p, "ccswap")),
                _switch_or_ask(app, p, partial(app.do_switch_best, p)),
            )
    for p in PROVIDERS:
        add(
            tr("Add {provider} current login").format(provider=PROVIDER_LABELS[p]),
            tr("Same as {cli} add").format(cli=_CLI.get(p, "ccswap")),
            partial(app.do_add_current, p),
        )
    if capabilities("claude").add_token:
        add(tr("Add Claude setup token or API key…"), tr("Same as ccswap add-token"),
            partial(app.action_add_token, "claude"))

    # -- auto-switch --------------------------------------------------------------
    auto = next((s for s in reversed(app.screen_stack) if isinstance(s, AutoScreen)), None)
    if auto is not None:
        add(tr("Go live / back to dry-run"),
            tr("Same as ccswap auto, or ccswap auto --dry-run"), auto.action_toggle_live)
    for strategy in ("best", "consume-first"):
        add(
            tr("Auto-switch strategy: {strategy}").format(strategy=strategy),
            tr("Same as ccswap config set autoswitch.strategy {strategy}").format(strategy=strategy),
            partial(auto.set_strategy if auto is not None else app.apply_strategy, strategy),
        )

    # -- settings -----------------------------------------------------------------
    for theme in ("dark", "light", "auto"):
        add(tr("Theme: {name}").format(name=theme),
            tr("Same as ccswap config set ui.theme {name}").format(name=theme),
            partial(app.apply_theme, theme), ok=True)
    add(tr("Dashboard view: Combined"), tr("Same as ccswap config set ui.view combined"),
        partial(app.apply_view, "combined"), ok=True)
    for p in PROVIDERS:
        add(tr("Dashboard view: {provider}").format(provider=PROVIDER_LABELS[p]),
            tr("Same as ccswap config set ui.view {name}").format(name=p),
            partial(app.apply_view, p), ok=True)
    for on in (True, False):
        add(tr("Mask account info: {state}").format(state=tr("on") if on else tr("off")),
            tr("Same as ccswap config set ui.mask {state}").format(state="on" if on else "off"),
            partial(app.apply_mask, on), ok=True)
    if not any(isinstance(screen, (AutoScreen, SettingsScreen)) for screen in app.screen_stack):
        add(tr("Settings"), tr("Same as ccswap config"), app.open_settings)
    add(tr("Quit"), tr("Stops any engine this app started"), app.exit, ok=True)
    return _visible(app, out)


def account_commands(app: "CswapApp") -> list[Command]:
    tr = app.gettext
    out: list[Command] = []
    for p, snap in ((p, app.snapshots.get(p)) for p in PROVIDERS):
        if snap is None:
            continue
        name, caps, cli = PROVIDER_LABELS[p], capabilities(p), _CLI.get(p, "ccswap")
        for acc in snap.accounts:
            who = _who(app, acc)
            if caps.switch and not acc.is_active and not acc.disabled:
                out.append(Command(
                    tr("Switch {provider} to {number} · {email}").format(
                        provider=name, number=acc.number, email=who
                    ),
                    tr("Same as {cli} switch {number}").format(cli=cli, number=acc.number),
                    _switch_or_ask(app, p, partial(app.do_switch, p, acc.number)),
                ))
            if caps.disable:
                verb = "enable" if acc.disabled else "disable"
                out.append(Command(
                    f"{tr(verb.capitalize())} {name} {acc.number} · {who}",
                    tr("Same as {cli} {verb} {number}").format(cli=cli, verb=verb, number=acc.number),
                    partial(app.do_toggle_disabled, p, acc.number),
                ))
            out.append(Command(
                tr("Remove {provider} {number} · {email}…").format(
                    provider=name, number=acc.number, email=who
                ),
                tr("Same as {cli} remove {number}").format(cli=cli, number=acc.number),
                partial(app.confirm_remove, p, acc.number, acc.email),
            ))
    return _visible(app, out)


class AccountCommands(Provider):
    """Per-account commands. Search only: nothing is listed before typing."""

    async def search(self, query: str) -> Hits:
        matcher = self.matcher(query)
        for c in account_commands(self.app):
            score = matcher.match(c.label)
            if score > 0:
                yield Hit(score, matcher.highlight(c.label), c.run, text=c.label, help=c.help)
