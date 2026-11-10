"""First-class tool pages: one per coding tool, plus an all-tools overview.

Claude Code, Codex and Antigravity keep their established home: the dashboard,
filtered to that tool (the reference experience).  Muse Code, Kiro CLI and
OpenCode get a page of their own with the same anatomy: identity, install and
login state, the current usage with real bars, every account with *its own*
usage, reset times, freshness and the operations the tool actually supports.

All pages share the number keys (``1``-``6``, ``0`` overview) and the same backend
(:mod:`claude_swap.vendors.overview` / ``VendorService``) as the CLI, menu bar and
Raycast.  Every vendor call runs in a thread worker; unknown values stay unknown.
"""

from __future__ import annotations

import contextvars
import logging
import threading
import time
import weakref
from datetime import datetime
from typing import ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Footer, ListItem, ListView, Static

from claude_swap.exceptions import ClaudeSwitchError
from claude_swap.i18n import localize_bindings, mark
from claude_swap.tui.data import mask_emails_in_text
from claude_swap.tui.modals import ConfirmModal, OutputModal
from claude_swap.tui.theme import Palette
from claude_swap.tui.widgets import CyclingListView, usage_bar
from claude_swap.vendors.core import AccountView, UsageResult, UsageState
from claude_swap.vendors.legacy import AgyTool, ClaudeTool, CodexTool
from claude_swap.vendors.overview import (
    LABELS,
    TOOL_ORDER,
    Overview,
    ToolSummary,
    headline,
    number_text,
    render_usage_lines,
    reset_text,
    summary_from_snapshot,
    tool_usage_title,
    usage_headline,
)
from claude_swap.vendors.service import VendorService

_log = logging.getLogger(__name__)

# Background threads started by tool pages (tests join them; the app never waits on them).
BACKGROUND: weakref.WeakSet[threading.Thread] = weakref.WeakSet()

DASHBOARD_TOOLS = ("claude", "codex", "agy")  # their page is the filtered dashboard
TOOL_KEYS = {str(i): tool for i, tool in enumerate(TOOL_ORDER, 1)}  # 1..6
NAV_BINDINGS = [
    *(Binding(key, f"goto('{tool}')", show=False) for key, tool in TOOL_KEYS.items()),
    Binding("0", "goto('overview')", mark("Overview"), show=False),
]


def strip_text(current: str) -> Text:
    """`` 1 Claude Code  2 Codex  [3 Muse Code] ... 0 Overview`` -- the peer row."""
    text = Text()
    for key, tool in TOOL_KEYS.items():
        label = f" {key} {LABELS[tool]} "
        text.append(label, style="bold reverse" if tool == current else "dim")
        text.append(" ")
    text.append(" 0 Overview ", style="bold reverse" if current == "overview" else "dim")
    return text


def navigate(app, target: str) -> None:
    """Shared by every page: number keys and overview rows land on the same place."""
    from claude_swap.tui.dashboard import DashboardScreen

    if target in DASHBOARD_TOOLS:
        while len(app.screen_stack) > 1 and not isinstance(app.screen, DashboardScreen):
            app.pop_screen()
        app.apply_view(target)
        return
    if target == "overview":
        if not isinstance(app.screen, OverviewScreen):
            _pop_tool_pages(app)
            app.push_screen(OverviewScreen())
        return
    if isinstance(app.screen, ToolScreen) and app.screen.tool == target:
        return
    _pop_tool_pages(app)
    app.push_screen(ToolScreen(target))


def _pop_tool_pages(app) -> None:
    while isinstance(app.screen, (ToolScreen, OverviewScreen)):
        app.pop_screen()


def service_of(app) -> VendorService:
    if getattr(app, "vendor_service", None) is None:
        app.vendor_service = VendorService()
    return app.vendor_service


def _when(ts: float | None) -> str:
    if not ts:
        return "never"
    age = max(0, int(time.time() - ts))
    span = (
        f"{age // 3600}h ago"
        if age >= 3600
        else f"{age // 60}m ago"
        if age >= 60
        else f"{age}s ago"
    )
    return f"{datetime.fromtimestamp(ts).strftime('%H:%M:%S')} ({span})"


def usage_renderable(result: UsageResult | None, palette: Palette, width: int = 22) -> Text:
    """Bars for windows that report a share; text for everything else."""
    out = Text()
    if result is None:
        out.append("no reading yet", style=palette.muted)
        return out
    stale = result.state is UsageState.STALE
    if result.plan:
        out.append(f"plan {result.plan}\n", style=palette.muted)
    for w in result.windows:
        pct = w.used_percent
        suffix_bits = []
        if w.used is not None and w.limit is not None:
            suffix_bits.append(f"{number_text(w.used)}/{number_text(w.limit)} {w.unit}")
        reset = reset_text(w)
        if reset:
            suffix_bits.append(reset)
        if w.scope not in ("account",):
            suffix_bits.append(w.scope)
        out.append(
            usage_bar(
                f"{w.label:<14}", pct, " · ".join(suffix_bits), width, stale=stale, palette=palette
            )
        )
        out.append("\n")
    lines = [line.strip() for line in render_usage_lines(result, indent="") if line]
    rest = [
        line
        for line in lines
        if not line.startswith(tuple(w.label for w in result.windows))
        and not line.startswith("plan:")
        and not line.startswith("Kiro labels")
    ]
    for line in rest:
        style = (
            palette.sev_warn
            if line.startswith(("stale", "refresh failed", "usage error", "sign-in"))
            else palette.muted
        )
        out.append(line + "\n", style=style)
    return out


class AccountRow(ListItem):
    def __init__(self, view: AccountView, usage: UsageResult, label: str) -> None:
        super().__init__(Static(Text(label), markup=False))
        self.view = view
        self.usage = usage


class _ToolPage(Screen):
    """Shared plumbing: number-key navigation, masking, safe worker hand-off."""

    def _spawn(self, group: str, work) -> None:
        """Run blocking vendor work on a daemon thread; deliver only the newest result.

        Not ``run_worker``: Textual joins thread workers on quit, so a tool stuck in a slow
        CLI would hold ``q`` for up to a minute.  ``work(post)`` calls ``post(fn, *args)``
        to hand its result to the UI thread; a result superseded by a newer call of the
        same group, or arriving after the page closed, is dropped.
        """
        generations = self.__dict__.setdefault("_generations", {})
        generation = generations[group] = generations.get(group, 0) + 1
        app = self.app
        context = contextvars.copy_context()  # keeps Textual's active-app lookup working

        def post(fn, *args) -> None:
            def apply() -> None:
                if self.is_attached and generations.get(group) == generation:
                    fn(*args)

            try:
                app.call_from_thread(apply)
            except Exception:  # noqa: BLE001 - the app stopped (RuntimeError/CancelledError)
                _log.debug("tool page result dropped: app stopped", exc_info=True)

        def body() -> None:
            try:
                context.run(work, post)
            except Exception:  # noqa: BLE001 - a background read must never print a traceback
                _log.debug("tool page background read failed", exc_info=True)

        thread = threading.Thread(target=body, name=f"ccswap-tui-{group}", daemon=True)
        BACKGROUND.add(thread)
        thread.start()

    def _mask(self, text: str) -> str:
        return mask_emails_in_text(text) if getattr(self.app, "mask_accounts", False) else text

    def _masked(self, text: Text) -> Text:
        """Masking replaces characters, so styled spans are dropped only when masking."""
        return Text(self._mask(text.plain)) if getattr(self.app, "mask_accounts", False) else text

    def _palette(self) -> Palette:
        return Palette.from_theme(self.app.current_theme)

    def action_goto(self, target: str) -> None:
        navigate(self.app, target)

    def action_back(self) -> None:
        self.app.pop_screen()


class ToolScreen(_ToolPage):
    """One tool, first class: Muse Code, Kiro CLI, OpenCode (and any future vendor)."""

    BINDINGS = [  # noqa: RUF012 - Textual convention
        Binding("r", "refresh", mark("Refresh")),
        Binding("enter,s", "switch", mark("Switch"), priority=True),
        Binding("a", "add_help", mark("Add")),
        Binding("x", "remove", mark("Remove")),
        Binding("c", "capabilities", mark("Capabilities")),
        Binding("left,h", "step(-1)", mark("Prev tool")),
        Binding("right,l", "step(1)", mark("Next tool")),
        Binding("escape,q", "back", mark("Back")),
        Binding("j", "cursor_down", show=False),
        Binding("k", "cursor_up", show=False),
        *NAV_BINDINGS,
    ]

    def __init__(self, tool: str, service: VendorService | None = None) -> None:
        super().__init__()
        self.tool = tool
        self._service = service
        self.summary: ToolSummary | None = None
        self._message = ""
        self._busy = False

    @property
    def service(self) -> VendorService:
        return self._service or service_of(self.app)

    def compose(self) -> ComposeResult:
        yield Static(strip_text(self.tool), id="tool-strip")
        yield Static("", id="tool-head")
        yield Static("", id="tool-usage")
        yield Static("", id="accounts-title")
        yield CyclingListView(id="taccounts")
        yield Static("", id="tool-detail")
        yield Footer()

    def on_mount(self) -> None:
        localize_bindings(self, self.app.gettext)
        self.query_one("#taccounts", ListView).focus()
        self._paint()
        self.action_refresh(force=False)

    # ---- data ------------------------------------------------------------------

    def action_refresh(self, force: bool = True) -> None:
        tool = self.tool
        self._message = "refreshing…" if force else self._message
        self._render_detail()

        service = self.service

        def work(post) -> None:
            try:
                summary = Overview(service, legacy=[]).collect_one(tool, refresh=force)
            except (ClaudeSwitchError, OSError) as exc:
                summary = ToolSummary(tool, LABELS.get(tool, tool), error=str(exc))
            post(self._loaded, summary, force)

        self._spawn(f"tool-{tool}", work)

    def _loaded(self, summary: ToolSummary, forced: bool) -> None:
        self.summary = summary
        if forced and self._message == "refreshing…":
            self._message = ""
        self._paint()

    # ---- rendering -------------------------------------------------------------

    def _paint(self) -> None:
        s = self.summary
        head = Text()
        head.append(LABELS.get(self.tool, self.tool), style="bold")
        if s is None:
            head.append("   loading…")
        elif not s.installed:
            head.append(f"   not installed ({self.service.adapter(self.tool).binary} not on PATH)")
        else:
            head.append(f"  {s.version or 'unknown version'}   ")
            head.append(headline(s))
            auth = s.auth or {}
            details = []
            if "loggedIn" in auth:
                details.append(
                    f"login: {auth.get('email')} ({auth.get('accountType')})"
                    if auth.get("loggedIn")
                    else "login: signed out"
                )
            if "storedCredential" in auth:
                details.append(
                    "login: "
                    + {True: "credential stored", False: "none", None: "unknown"}[
                        auth["storedCredential"]
                    ]
                    + " (identity not exposed by Muse)"
                )
            if auth.get("envKeyPresent"):
                details.append("! an API key in the environment overrides the stored login")
            if "accountSwitching" in auth:
                details.append(
                    "native account switching: "
                    + ("yes" if auth["accountSwitching"] else "not in this version")
                )
            pc = auth.get("providerConfig") or {}
            if pc.get("configuredProviders") or pc.get("model"):
                details.append(
                    f"model {pc.get('model') or '-'} · configured providers: {', '.join(pc.get('configuredProviders', [])) or '-'}"
                )
            details.append(f"refreshed {_when(s.refreshed_at)}")
            if s.error:
                details.append(f"! {s.error}")
            head.append("\n" + "\n".join(details), style=self._palette().muted)
        self.query_one("#tool-head", Static).update(self._masked(head))

        usage = Text()
        if s is not None and s.tool_usage is not None:
            usage.append(tool_usage_title(s) + "\n", style="bold")
            usage.append(usage_renderable(s.tool_usage, self._palette()))
        self.query_one("#tool-usage", Static).update(self._masked(usage))

        listview = self.query_one("#taccounts", ListView)
        index = listview.index
        listview.clear()
        rows = s.accounts if s else []
        self.query_one("#accounts-title", Static).update(
            Text(f"accounts ({len(rows)})" if s else "accounts", style="bold")
        )
        if s is not None and not rows:
            listview.append(ListItem(Static(Text(self._empty_hint()), markup=False), disabled=True))
        for n, (view, result) in enumerate(rows, 1):
            listview.append(AccountRow(view, result, self._mask(self._row_label(n, view, result))))
        if rows:
            listview.index = min(index or 0, len(rows) - 1)
        self._render_detail()

    def _empty_hint(self) -> str:
        s = self.summary
        if s is not None and not s.installed:
            return "Install the CLI to manage accounts here."
        return "No registered accounts. Press a to see how to add one."

    @staticmethod
    def _row_label(n: int, view: AccountView, result: UsageResult) -> str:
        shown = False if result.state is UsageState.UNAUTHENTICATED else view.active
        mark = {True: "●", False: "○", None: "?"}[shown]
        bits = [view.kind]
        if view.provider:
            bits.insert(0, view.provider)
        if view.identity.get("email") and view.identity["email"] != view.alias:
            bits.append(view.identity["email"])
        tag = "  [unregistered]" if not view.registered else ""
        return f"{mark} {n}. {view.alias}  ({', '.join(bits)}){tag}\n     {usage_headline(result)}"

    def _selected(self) -> AccountRow | None:
        item = self.query_one("#taccounts", ListView).highlighted_child
        return item if isinstance(item, AccountRow) else None

    def _render_detail(self) -> None:
        out = Text()
        if self._message:
            out.append(self._message + "\n", style="bold")
        row = self._selected()
        if row is not None:
            out.append(f"{row.view.alias}\n", style="bold")
            if row.view.note:
                out.append(row.view.note + "\n", style=self._palette().muted)
            out.append(usage_renderable(row.usage, self._palette()))
        self.query_one("#tool-detail", Static).update(self._masked(out))

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        self._render_detail()

    # ---- actions ---------------------------------------------------------------

    def action_cursor_down(self) -> None:
        self.query_one("#taccounts", ListView).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#taccounts", ListView).action_cursor_up()

    def action_step(self, step: int) -> None:
        order = list(TOOL_ORDER)
        navigate(
            self.app,
            order[(order.index(self.tool) + step) % len(order)] if self.tool in order else order[0],
        )

    def action_capabilities(self) -> None:
        tool = self.tool

        service = self.service

        def work(post) -> None:
            adapter = service.adapter(tool)
            try:
                table = service.capability_table(adapter)
                text = "\n".join(
                    f"{n:<22} {c['support']:<12} {c['mechanism']}" for n, c in table.items()
                )
            except (ClaudeSwitchError, OSError) as exc:
                text = str(exc)
            post(lambda: self.app.push_screen(OutputModal(f"{adapter.label} — capabilities", text)))

        self._spawn("tool-caps", work)

    def action_add_help(self) -> None:
        howto = {
            "muse": "ccswap muse add <alias> --api-key-stdin   API key (hidden prompt / stdin)\n"
            "ccswap muse add <alias> --browser         name the current browser login",
            "opencode": "opencode auth login                          add the account in OpenCode\n"
            "ccswap opencode add <alias> --provider <p> [--account <a>]",
            "kiro": "kiro-cli login                               sign in, then:\n"
            "ccswap kiro add <alias>                      register the current login\n"
            "ccswap kiro add <alias> --api-key            KIRO_API_KEY (hidden prompt / stdin)",
        }.get(self.tool, f"ccswap {self.tool} add <alias>")
        self.app.push_screen(
            OutputModal("Add an account", howto + "\n\nSecrets are never taken from arguments.")
        )

    def action_remove(self) -> None:
        row = self._selected()
        if row is None or not row.view.registered or self._busy:
            return
        tool, view = self.tool, row.view

        def done(confirmed: bool | None) -> None:
            if not confirmed:
                return

            service = self.service

            def work(post) -> None:
                try:
                    service.remove(tool, view.alias, view.provider)
                    message = f"Removed '{view.alias}' from ccswap; the {LABELS.get(tool, tool)} login was not touched"
                except (ClaudeSwitchError, OSError) as exc:
                    message = f"remove failed: {exc}"
                post(self._action_done, message)

            self._spawn("tool-remove", work)

        self.app.push_screen(
            ConfirmModal(
                f"Forget '{view.alias}' in ccswap? The {LABELS.get(tool, tool)} login itself is not changed.",
                title="Remove registration",
            ),
            done,
        )

    def action_switch(self) -> None:
        row = self._selected()
        if row is None or self._busy:
            return
        tool, view = self.tool, row.view
        # A dead Muse login still has active=True because the file names it. The click
        # rewrites the stored token. Every other active row stays a no-op.
        restore = (
            tool == "muse"
            and view.kind == "browser"
            and view.registered
            and row.usage.state is UsageState.UNAUTHENTICATED
        )
        if view.active and not restore:
            self._message = f"'{view.alias}' is already active"
            self._render_detail()
            return
        if tool == "opencode" and view.provider:
            self._start_switch(view, assume_yes=False)
        elif tool == "muse" and view.kind == "api-key":
            self.app.push_screen(
                OutputModal(
                    "Switch",
                    "A Muse API key has no allowance to show and is not a login. "
                    f"Start a session with `ccswap muse run {view.alias}`. "
                    "That sets META_API_KEY for that process only.",
                )
            )
        elif (tool, view.kind) in (("muse", "browser"), ("kiro", "login")):
            # ccswap swaps the Muse login it keeps; a Kiro profile goes through Kiro's own
            # picker.  Both are verified; a different Kiro login reports what it needs.
            self._start_switch(view, assume_yes=True)
        elif (tool, view.kind) == ("kiro", "api-key"):
            self.app.push_screen(
                OutputModal(
                    "Switch",
                    "KIRO_API_KEY is process-scoped. Start a session as this account with "
                    f"`ccswap kiro run {view.alias}`.",
                )
            )
        else:
            self.app.push_screen(
                OutputModal("Switch", "This tool does not support switching to this account.")
            )

    def _start_switch(self, view: AccountView, *, assume_yes: bool) -> None:
        self._busy = True
        tool = self.tool
        raw = tool == "opencode" and not view.registered
        selector = (
            (view.identity.get("id") or view.identity.get("account") or view.alias)
            if raw
            else view.alias
        )
        provider = view.provider if tool == "opencode" else None

        service = self.service

        def work(post) -> None:
            try:
                outcome, _ = service.switch(
                    tool, selector, provider=provider, assume_yes=assume_yes, raw=raw
                )
                message = outcome.message + (
                    "\n⟳ restart running sessions to use it" if outcome.restart_required else ""
                )
            except (ClaudeSwitchError, OSError) as exc:
                message = f"switch failed: {exc}"
            except Exception as exc:  # noqa: BLE001 - never leave the page stuck busy
                message = f"switch failed unexpectedly: {type(exc).__name__}"
            post(self._action_done, message)

        self._spawn("tool-switch", work)

    def _action_done(self, message: str) -> None:
        self._busy = False
        self._message = message
        self.action_refresh(force=True)
        self._message = message


class OverviewRow(ListItem):
    def __init__(self, tool: str, label: str) -> None:
        super().__init__(Static(Text(label), markup=False))
        self.tool = tool


class OverviewScreen(_ToolPage):
    """Every tool on one page; Enter opens that tool's own page."""

    BINDINGS = [  # noqa: RUF012 - Textual convention
        Binding("enter", "open", mark("Open"), priority=True),
        Binding("r", "refresh", mark("Refresh all")),
        Binding("escape,q", "back", mark("Back")),
        Binding("j", "cursor_down", show=False),
        Binding("k", "cursor_up", show=False),
        *NAV_BINDINGS,
    ]

    def __init__(self, overview: Overview | None = None) -> None:
        super().__init__()
        self._overview = overview
        self.summaries: dict[str, ToolSummary] = {}
        self._bridges: dict = {}

    def compose(self) -> ComposeResult:
        yield Static(strip_text("overview"), id="tool-strip")
        yield Static("all tools · enter opens a tool · r refreshes", id="tool-head")
        yield CyclingListView(id="overview")
        yield Footer()

    def on_mount(self) -> None:
        localize_bindings(self, self.app.gettext)
        self.query_one("#overview", ListView).focus()
        self._paint()
        self.watch(self.app, "snapshots", lambda _s: self._paint())
        self.action_refresh(force=False)

    def _vendor_tools(self) -> list[str]:
        return [t for t in self._ov().tools() if t not in DASHBOARD_TOOLS]

    def _ov(self) -> Overview:
        if self._overview is None:
            self._overview = Overview(service_of(self.app), legacy=[])
        return self._overview

    def action_refresh(self, force: bool = True) -> None:
        if force and hasattr(self.app, "action_refresh_full"):
            self.app.action_refresh_full()  # Claude/Codex/Antigravity: their own paced refresh
        for tool in self._vendor_tools():
            overview = self._ov()

            def work(post, tool: str = tool, overview: Overview = overview) -> None:
                try:
                    summary = overview.collect_one(tool, refresh=force)
                except (ClaudeSwitchError, OSError) as exc:
                    summary = ToolSummary(tool, LABELS.get(tool, tool), error=str(exc))
                post(self._loaded, summary)

            self._spawn(f"overview-{tool}", work)

    def _loaded(self, summary: ToolSummary) -> None:
        self.summaries[summary.tool] = summary
        self._paint()

    _BRIDGES: ClassVar[dict[str, type]] = {"claude": ClaudeTool, "codex": CodexTool, "agy": AgyTool}

    def _legacy_summary(self, tool: str) -> ToolSummary:
        # The bridge only converts the snapshot the app already polls; it never builds
        # a switcher of its own here (its switcher factory is lazy).
        adapter = self._bridges.setdefault(tool, self._BRIDGES[tool]())
        snap = (getattr(self.app, "snapshots", {}) or {}).get(tool)
        return summary_from_snapshot(adapter, snap)

    def _paint(self) -> None:
        listview = self.query_one("#overview", ListView)
        index = listview.index
        listview.clear()
        tools = [*TOOL_ORDER, *(t for t in self._vendor_tools() if t not in TOOL_ORDER)]
        for n, tool in enumerate(tools, 1):
            if tool in DASHBOARD_TOOLS:
                s = self._legacy_summary(tool)
                state = "loading…" if s.refreshed_at is None and not s.accounts else headline(s)
            else:
                s = self.summaries.get(tool) or ToolSummary(tool, LABELS.get(tool, tool))
                state = "loading…" if tool not in self.summaries else headline(s)
            active = s.active
            who = active[0].alias if active else ("—" if s.accounts else "no accounts")
            key = n if n <= 6 else " "
            label = (
                f"{key}  {LABELS.get(tool, tool):<12} {state}\n"
                f"   active: {who} · {len(s.accounts)} account(s)"
                + (f" · {s.version}" if s.version else "")
                + (f" · refreshed {_when(s.refreshed_at)}" if s.refreshed_at else "")
                + (f"\n   ! {s.error}" if s.error else "")
            )
            listview.append(OverviewRow(tool, self._mask(label)))
        listview.index = min(index or 0, len(tools) - 1)

    def action_open(self) -> None:
        item = self.query_one("#overview", ListView).highlighted_child
        if isinstance(item, OverviewRow):
            navigate(self.app, item.tool)

    def action_cursor_down(self) -> None:
        self.query_one("#overview", ListView).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#overview", ListView).action_cursor_up()
