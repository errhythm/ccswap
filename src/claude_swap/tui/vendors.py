"""Multi-vendor screen: Muse Code, OpenCode and Kiro CLI.

Same visual language as the switch screen (ListView + footer bindings) and the
same backend as the CLI (:class:`claude_swap.vendors.service.VendorService`);
nothing here re-implements switching.  Blocking work (subprocesses, locks)
runs in thread workers.  Unknown stays unknown: a vendor that cannot report
quota shows *why*, never a bar or a zero.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Footer, ListItem, ListView, Static

from claude_swap.exceptions import ClaudeSwitchError
from claude_swap.i18n import localize_bindings, mark
from claude_swap.tui.data import mask_emails_in_text
from claude_swap.tui.modals import ConfirmModal, OutputModal
from claude_swap.tui.widgets import CyclingListView
from claude_swap.vendors.core import AccountView, UsageResult, UsageState
from claude_swap.vendors.service import VendorService


@dataclass
class VendorPage:
    """Everything one vendor tab shows, gathered off the UI thread."""

    tool: str
    status: dict
    views: list[AccountView] = field(default_factory=list)
    error: str | None = None
    refreshed_at: float | None = None


def active_marker(view: AccountView) -> str:
    return {True: "●", False: "○", None: "?"}[view.active]


def account_label(view: AccountView, number: int) -> str:
    bits = [view.kind]
    if view.provider:
        bits.insert(0, view.provider)
    email = view.identity.get("email")
    if email:
        bits.append(email)
    tag = "" if view.registered else "  [unregistered]"
    return f"{active_marker(view)} {number}. {view.alias}  ({', '.join(bits)}){tag}"


def usage_lines(result: UsageResult | None) -> list[str]:
    if result is None:
        return ["usage: press u to query (never starts a model turn)"]
    if result.state is not UsageState.AVAILABLE:
        lines = [f"usage {result.state.value}: {result.reason}"]
        if result.hint:
            lines.append(result.hint)
        return lines
    lines = []
    for obs in result.observations:
        label = "vendor-reported" if obs.authoritative else "local statistics, not a quota"
        stamp = (
            datetime.fromtimestamp(obs.fetched_at).strftime("%H:%M:%S") if obs.fetched_at else "?"
        )
        lines.append(
            f"{obs.kind.value} ({label}; {obs.source}; {stamp}{'; cached' if obs.cached else ''})"
        )
        text = obs.data.get("text") if isinstance(obs.data, dict) else None
        lines.extend(
            str(text).splitlines()[:8]
            if text
            else [f"{k}: {v}" for k, v in list(obs.data.items())[:8]]
        )
    return lines


def info_lines(page: VendorPage) -> list[str]:
    st = page.status
    if not st.get("installed"):
        return [f"{st.get('label', page.tool)} is not installed — nothing to manage."]
    lines = [f"{st['label']} {st.get('version') or '?'}  ·  {st.get('accounts', 0)} registered"]
    auth = st.get("auth") or {}
    if "loggedIn" in auth:
        who = auth.get("email") or "not logged in"
        lines.append(f"login: {who} ({auth.get('accountType') or '-'})")
    if auth.get("envKeyPresent"):
        lines.append("! an API key in the environment overrides the stored login")
    if "accountSwitching" in auth:
        lines.append(
            "native account switching: "
            + ("yes" if auth["accountSwitching"] else "no (this OpenCode version)")
        )
    if "storedCredential" in auth:
        stored = {True: "present", False: "none", None: "unknown"}[auth["storedCredential"]]
        lines.append(f"stored credential: {stored} (type/identity not exposed by muse)")
    if st.get("error"):
        lines.append(f"! {st['error']}")
    return lines


class VendorItem(ListItem):
    def __init__(self, view: AccountView, label: str) -> None:
        super().__init__(Static(Text(label), markup=False))
        self.view = view


class VendorsScreen(Screen):
    BINDINGS = [  # noqa: RUF012 - Textual convention
        Binding("left,h", "prev_vendor", mark("Prev vendor")),
        Binding("right,l", "next_vendor", mark("Next vendor")),
        Binding("enter,s", "switch", mark("Switch"), priority=True),
        Binding("u", "usage", mark("Usage")),
        Binding("a", "add_help", mark("Add")),
        Binding("c", "capabilities", mark("Capabilities")),
        Binding("r", "refresh", mark("Refresh")),
        Binding("escape,q", "back", mark("Back")),
        Binding("j", "cursor_down", show=False),
        Binding("k", "cursor_up", show=False),
    ]

    def __init__(self, service: VendorService | None = None) -> None:
        super().__init__()
        self._service = service
        self._tools: list[str] = []
        self._index = 0
        self._pages: dict[str, VendorPage] = {}
        self._usage: dict[tuple[str, str], UsageResult] = {}
        self._message = ""
        self._busy = False

    @property
    def service(self) -> VendorService:
        if self._service is None:
            self._service = getattr(self.app, "vendor_service", None) or VendorService()
        return self._service

    # ---- layout -----------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Static("", id="vendor-tabs")
        yield Static("", id="vendor-info")
        yield CyclingListView(id="vaccounts")
        yield Static("", id="vendor-detail")
        yield Footer()

    def on_mount(self) -> None:
        localize_bindings(self, self.app.gettext)
        self._tools = self.service.registry.keys()
        self.query_one("#vaccounts", ListView).focus()
        self._render_all()
        self.action_refresh()

    # ---- state -> widgets ---------------------------------------------------

    @property
    def tool(self) -> str:
        return self._tools[self._index]

    def _mask(self, text: str) -> str:
        return mask_emails_in_text(text) if getattr(self.app, "mask_accounts", False) else text

    def _render_all(self) -> None:
        tabs = Text()
        for i, tool in enumerate(self._tools):
            label = self.service.adapter(tool).label
            page = self._pages.get(tool)
            dot = "" if page is None else ("" if page.status.get("installed") else " ✗")
            style = "bold reverse" if i == self._index else "dim"
            tabs.append(f" {label}{dot} ", style=style)
            tabs.append(" ")
        self.query_one("#vendor-tabs", Static).update(tabs)

        page = self._pages.get(self.tool)
        listview = self.query_one("#vaccounts", ListView)
        listview.clear()
        if page is None:
            self.query_one("#vendor-info", Static).update(Text("loading…"))
            self._render_detail()
            return
        self.query_one("#vendor-info", Static).update(Text(self._mask("\n".join(info_lines(page)))))
        if page.error:
            listview.append(ListItem(Static(Text(f"! {page.error}"), markup=False), disabled=True))
        elif not page.views:
            hint = (
                "no accounts — press a to see how to add one"
                if page.status.get("installed")
                else "install the CLI first"
            )
            listview.append(ListItem(Static(Text(hint), markup=False), disabled=True))
        for n, view in enumerate(page.views, 1):
            listview.append(VendorItem(view, self._mask(account_label(view, n))))
        if page.views:
            listview.index = 0
        self._render_detail()

    def _selected(self) -> AccountView | None:
        item = self.query_one("#vaccounts", ListView).highlighted_child
        return item.view if isinstance(item, VendorItem) else None

    def _render_detail(self) -> None:
        lines: list[str] = []
        view = self._selected()
        page = self._pages.get(self.tool)
        if view is not None:
            if view.note:
                lines.append(view.note)
            lines += usage_lines(self._usage.get((self.tool, view.alias)))
        elif page is not None and page.status.get("installed"):
            lines += usage_lines(self._usage.get((self.tool, "")))
        if page and page.refreshed_at:
            lines.append(
                "refreshed " + datetime.fromtimestamp(page.refreshed_at).strftime("%H:%M:%S")
            )
        if self._message:
            lines.insert(0, self._message)
        self.query_one("#vendor-detail", Static).update(
            Text(self._mask("\n".join(lines)), no_wrap=False)
        )

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        self._render_detail()

    # ---- actions ------------------------------------------------------------

    def action_back(self) -> None:
        self.app.pop_screen()

    def action_cursor_down(self) -> None:
        self.query_one("#vaccounts", ListView).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#vaccounts", ListView).action_cursor_up()

    def _go(self, step: int) -> None:
        self._index = (self._index + step) % len(self._tools)
        self._message = ""
        self._render_all()
        if self.tool not in self._pages:
            self.action_refresh()

    def action_prev_vendor(self) -> None:
        self._go(-1)

    def action_next_vendor(self) -> None:
        self._go(1)

    def action_refresh(self) -> None:
        tool = self.tool
        self.run_worker(
            lambda: self._load(tool),
            thread=True,
            group="vendors-load",
            exit_on_error=False,
            exclusive=True,
        )

    def _load(self, tool: str) -> None:
        service = self.service
        try:
            adapter = service.adapter(tool)
            page = VendorPage(tool, service.vendor_status(adapter), refreshed_at=time.time())
            if page.status.get("installed"):
                page.views = service.list_views(tool)
        except ClaudeSwitchError as exc:
            page = VendorPage(
                tool, {"installed": True, "label": tool}, error=str(exc), refreshed_at=time.time()
            )
        self.app.call_from_thread(self._loaded, page)

    def _loaded(self, page: VendorPage) -> None:
        self._pages[page.tool] = page
        if page.tool == self.tool:
            self._render_all()

    def action_usage(self) -> None:
        tool, view = self.tool, self._selected()
        alias = view.alias if view and view.registered and tool != "opencode" else ""

        def work() -> None:
            try:
                result = self.service.usage(tool, alias or None, refresh=True)
            except ClaudeSwitchError as exc:
                result = UsageResult(UsageState.UNAVAILABLE, str(exc))
            self.app.call_from_thread(self._usage_done, tool, view.alias if view else "", result)

        self.run_worker(work, thread=True, group="vendors-usage", exit_on_error=False)

    def _usage_done(self, tool: str, alias: str, result: UsageResult) -> None:
        self._usage[(tool, alias)] = result
        self._render_detail()

    def action_capabilities(self) -> None:
        adapter = self.service.adapter(self.tool)
        table = self.service.capability_table(adapter)
        text = "\n".join(
            f"{name:<22} {s['support']:<12} {s['mechanism']}" for name, s in table.items()
        )
        self.app.push_screen(OutputModal(f"{adapter.label} — capabilities", text))

    def action_add_help(self) -> None:
        prog = "ccswap"
        howto = {
            "muse": f"{prog} muse add <alias> --api-key-stdin     (API key via stdin/prompt)\n"
            f"{prog} muse add <alias> --browser           (register current browser login)",
            "opencode": f"opencode auth login                       (add the account in OpenCode)\n"
            f"{prog} opencode add <alias> --provider <p> [--account <a>]",
            "kiro": f"kiro-cli login                               (log in, then:)\n"
            f"{prog} kiro add <alias>                      (register the current login)\n"
            f"{prog} kiro add <alias> --api-key            (KIRO_API_KEY via stdin/prompt)",
        }[self.tool]
        self.app.push_screen(
            OutputModal("Add an account", howto + "\n\nSecrets are never taken from arguments.")
        )

    def action_switch(self) -> None:
        view = self._selected()
        if view is None or self._busy:
            return
        tool = self.tool
        if tool == "opencode":
            self._start_switch(view, assume_yes=False)
        elif tool == "muse" and view.kind == "api-key":
            self.app.push_screen(
                ConfirmModal(
                    "Switching replaces the credential stored by Muse (including a browser "
                    "session). Running sessions keep the old one until restarted.",
                    title=f"Switch Muse to '{view.alias}'?",
                ),
                lambda ok: self._start_switch(view, assume_yes=True) if ok else None,
            )
        else:
            explain = {
                (
                    "muse",
                    "browser",
                ): "Muse cannot switch browser sessions. Sign in again with `muse login`.",
                ("kiro", "api-key"): "KIRO_API_KEY is process-scoped. Launch with `ccswap kiro run "
                f"{view.alias}`.",
            }.get(
                (tool, view.kind),
                f"Interactive login required. Run `ccswap {tool} switch {view.alias} --login`"
                " from a terminal; it asks before ending your current Kiro login.",
            )
            self.app.push_screen(OutputModal("Switch from the command line", explain))

    def _start_switch(self, view: AccountView, *, assume_yes: bool) -> None:
        self._busy = True
        tool = self.tool
        selector = (
            (view.identity.get("id") or view.identity.get("account"))
            if tool == "opencode"
            else view.alias
        )
        provider = view.provider if tool == "opencode" else None

        def work() -> None:
            try:
                outcome, _ = self.service.switch(
                    tool, selector or view.alias, provider=provider, assume_yes=assume_yes
                )
                message = outcome.message + (
                    "\n⟳ restart running sessions to pick it up" if outcome.restart_required else ""
                )
            except ClaudeSwitchError as exc:
                message = f"switch failed: {exc}"
            self.app.call_from_thread(self._switch_done, message)

        self.run_worker(work, thread=True, group="vendors-switch", exit_on_error=False)

    def _switch_done(self, message: str) -> None:
        self._busy = False
        self._message = message
        self._render_detail()
        self.action_refresh()
