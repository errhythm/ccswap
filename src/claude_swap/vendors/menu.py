"""Menu-bar model: one submenu per tool (no AppKit/rumps imports: unit-testable).

``menubar.py`` renders these sections verbatim, so what the menu shows is decided
here, from the same :class:`~claude_swap.vendors.overview.ToolSummary` the CLI and
TUI use.  A Muse browser login and a Kiro profile are clickable.  A Muse API key
is omitted: Muse reports no allowance for a key, and storing the key replaces the
browser login.  Use ``ccswap muse run``.  A Kiro API key stays listed and is not
clickable, for the same reason it is process-scoped.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from claude_swap.vendors.core import AccountView, UsageResult, UsageState
from claude_swap.vendors.overview import (
    LABELS,
    Overview,
    ToolSummary,
    go_lines,
    headline,
    local_cost_line,
    reset_text,
    usage_headline,
    window_text,
)

MENU_TOOLS = ("muse", "kiro", "opencode")  # Claude/Codex keep their established menus


@dataclass(frozen=True)
class MenuRow:
    label: str
    tool: str
    action: str | None = None  # None (info) | "switch" | "refresh" | "open"
    selector: str | None = None
    provider: str | None = None
    url: str | None = None
    checked: bool = False
    raw: bool = False  # selector is a vendor id (unregistered OpenCode account), not an alias


@dataclass
class MenuSection:
    tool: str
    title: str  # "Kiro CLI · credits 1,136/2,000 (56.8%)"
    rows: list[MenuRow | None] = field(default_factory=list)  # details submenu; None = separator
    # One row per account, shown at the top level like Claude Code's: "1  alias  usage".
    accounts: list[MenuRow] = field(default_factory=list)


def _session_dead(usage: UsageResult) -> bool:
    return usage.state is UsageState.UNAUTHENTICATED


def _row_switchable(tool: str, view: AccountView, usage: UsageResult) -> bool:
    if switchable(tool, view.active, view.provider, view.kind, view.registered):
        return True
    # The file still names this login after Muse drops the session. A click rewrites
    # the stored token, which a plain "already active" row would refuse to do.
    return (
        _session_dead(usage)
        and tool == "muse"
        and view.kind == "browser"
        and view.registered
        and view.active is True
    )


def _hidden_menu_account(tool: str, view: AccountView) -> bool:
    """A Muse API key is a lane credential.  The menu has no honest usage line for it."""
    return tool == "muse" and view.kind == "api-key"


def switchable(
    tool: str, active: bool | None, provider: str | None, kind: str = "", registered: bool = True
) -> bool:
    """Can a click switch to this account without a terminal?

    OpenCode: native ``auth switch``.  Muse: a browser login ccswap keeps a token for.
    A Muse API key is omitted from the menu.  Kiro: another profile of the signed-in login,
    via Kiro's own picker.  A different Kiro *login* still needs an interactive sign-in, and
    a Kiro API key is process-scoped (``ccswap kiro run``).
    """
    if active is True:
        return False
    if tool == "opencode":
        return active is False and bool(provider)
    if tool == "muse":
        # An API key is `ccswap muse run`, not a login.  A click used to store the key
        # and replace the browser session, and the row could only say "usage unavailable".
        return registered and kind == "browser"
    if tool == "kiro":
        return registered and kind == "login"
    return False


def _ago(ts: float | None, now: float) -> str:
    if not ts:
        return "never"
    age = max(0, int(now - ts))
    span = (
        f"{age // 3600}h ago" if age >= 3600 else f"{age // 60}m ago" if age >= 60 else "just now"
    )
    return f"{datetime.fromtimestamp(ts).strftime('%H:%M')} ({span})"


def usage_rows(tool: str, result: UsageResult, now: float, indent: str = "") -> list[MenuRow]:
    rows: list[MenuRow] = []
    if result.plan:
        rows.append(MenuRow(f"{indent}Plan: {result.plan}", tool))
    for w in result.windows:
        rows.append(MenuRow(f"{indent}{window_text(w)}", tool))
        reset = reset_text(w, now)
        if reset:
            rows.append(MenuRow(f"{indent}    {reset}", tool))
    for line in go_lines(result):
        rows.append(
            MenuRow(
                f"{indent}{line.strip() if not line.startswith('  ') else '    ' + line.strip()}"[
                    :110
                ],
                tool,
            )
        )
    local = local_cost_line(result)
    if local:
        rows.append(MenuRow(f"{indent}{local} (OpenCode estimate, not a quota)", tool))
        for obs in result.observations:
            for p in (obs.data.get("providers") or [])[:6] if isinstance(obs.data, dict) else []:
                t = p["totals"]["30d"]
                rows.append(
                    MenuRow(
                        f"{indent}    {p['provider']}: {t['messages']:,} msgs · ${t['costUsd']:,.2f}",
                        tool,
                    )
                )
    if result.state is UsageState.STALE:
        rows.append(
            MenuRow(f"{indent}Stale — refresh failed: {result.error or 'unknown reason'}", tool)
        )
    elif result.state is not UsageState.AVAILABLE:
        rows.append(
            MenuRow(f"{indent}{usage_headline(result).capitalize()}: {result.reason}"[:110], tool)
        )
    if result.observed_at:
        rows.append(MenuRow(f"{indent}Updated {_ago(result.observed_at, now)}", tool))
    return rows


def section_for(summary: ToolSummary, docs: str | None, now: float) -> MenuSection:
    tool = summary.tool
    label = LABELS.get(tool, summary.label)
    section = MenuSection(tool, f"{label} · {headline(summary)}")
    rows = section.rows
    if not summary.installed:
        rows.append(MenuRow(f"{label} is not installed", tool))
        if docs:
            rows.append(MenuRow("Install instructions…", tool, action="open", url=docs))
        return section
    rows.append(MenuRow(f"{label} {summary.version or ''}".strip(), tool))
    auth = summary.auth or {}
    if "loggedIn" in auth:
        rows.append(
            MenuRow(
                f"Signed in: {auth.get('email')} ({auth.get('accountType')})"
                if auth["loggedIn"]
                else "Signed out",
                tool,
            )
        )
    if "storedCredential" in auth:
        stored = {True: "credential stored", False: "no stored login", None: "unknown"}[
            auth["storedCredential"]
        ]
        rows.append(MenuRow(f"Login: {stored} (Muse does not say whose)", tool))
    if auth.get("envKeyPresent"):
        rows.append(MenuRow("An API key in the environment overrides the stored login", tool))
    if summary.error:
        rows.append(MenuRow(f"Error: {summary.error}"[:110], tool))
    if summary.tool_usage is not None:
        rows.append(None)
        rows.extend(usage_rows(tool, summary.tool_usage, now))
    rows.append(None)
    rows.append(MenuRow("Accounts", tool))
    shown = [
        (view, usage)
        for view, usage in summary.accounts
        if not _hidden_menu_account(tool, view)
    ]
    if not summary.accounts:
        rows.append(MenuRow(f"  No {label} accounts registered", tool))
    elif not shown:
        rows.append(
            MenuRow("  API keys are used with `ccswap muse run` and are not listed here", tool)
        )
    for number, (view, usage) in enumerate(shown, 1):
        can = _row_switchable(tool, view, usage)
        selector = (
            (view.identity.get("id") or view.identity.get("account"))
            if tool == "opencode"
            else view.alias
        )
        email = view.identity.get("email", "")
        label = f"{number}  {view.alias}"
        if email and email != view.alias:
            label += f"  {email}"
        if view.identity.get("profileArn"):
            label += f"  [AWS {view.identity['profileArn'].split(':')[4]}]"
        label += f"  {usage_headline(usage)}"
        section.accounts.append(
            MenuRow(
                label[:140],
                tool,
                action="switch" if can else None,
                selector=selector if can else None,
                provider=view.provider,
                checked=bool(view.active) and not _session_dead(usage),
                raw=tool == "opencode" and not view.registered,
            )
        )
    for view, usage in shown:
        mark = {True: "●", False: "○", None: "?"}[view.active]
        where = (
            f"{view.provider}/"
            if view.provider and not view.alias.startswith(f"{view.provider}/")
            else ""
        )
        ok = _row_switchable(tool, view, usage)
        hint = "" if ok or view.active else ("  — switch in terminal" if tool != "opencode" else "")
        mark = "○" if _session_dead(usage) else mark
        text = f"  {mark} {where}{view.alias} · {usage_headline(usage)}{hint}"
        if not ok:
            selector = None
        elif tool == "opencode":
            selector = view.identity.get("id") or view.identity.get("account")
        else:
            selector = view.alias
        rows.append(
            MenuRow(
                text[:110],
                tool,
                action="switch" if ok else None,
                selector=selector,
                raw=tool == "opencode" and not view.registered,
                provider=view.provider,
                checked=bool(view.active) and not _session_dead(usage),
            )
        )
    rows.append(None)
    rows.append(MenuRow(f"Refresh {label} usage", tool, action="refresh"))
    if docs:
        rows.append(MenuRow(f"{label} documentation…", tool, action="open", url=docs))
    return section


def collect_sections(
    overview: Overview,
    tools: tuple[str, ...] = MENU_TOOLS,
    *,
    refresh: bool = False,
    now: float | None = None,
) -> dict[str, MenuSection]:
    """Blocking (spawns vendor CLIs): call from a worker thread.  Per-tool isolation
    comes from :meth:`Overview.collect`."""
    import time

    now = now or time.time()
    known = [t for t in tools if t in overview.tools()]
    out: dict[str, MenuSection] = {}
    for summary in overview.collect(known, refresh=refresh):
        try:
            docs = overview.service.adapter(summary.tool).docs or None
        except Exception:  # noqa: BLE001 - docs links are cosmetic
            docs = None
        out[summary.tool] = section_for(summary, docs, now)
    return out


def loading_section(tool: str) -> MenuSection:
    label = LABELS.get(tool, tool)
    return MenuSection(tool, f"{label} · loading…", [MenuRow("Loading…", tool)])
