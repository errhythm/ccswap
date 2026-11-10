"""Menu-bar model: one submenu per tool (no AppKit/rumps imports: unit-testable).

``menubar.py`` renders these sections verbatim, so what the menu shows is decided
here, from the same :class:`~claude_swap.vendors.overview.ToolSummary` the CLI and
TUI use.  The menu shows numbers only: a usage reading renders its figures, a
bare failure renders the "—" placeholder, and no state or reason words ever
appear here (the CLI and TUI keep the full details).  A Muse browser login and a
Kiro profile are clickable.  A Muse API key is listed but never clickable: it is
a lane credential run with ``ccswap muse run``.  A Kiro API key stays listed and
is not clickable, for the same reason it is process-scoped.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from claude_swap.vendors.core import AccountView, UsageResult, UsageState
from claude_swap.vendors.overview import (
    LABELS,
    Overview,
    ToolSummary,
    go_headline,
    go_lines,
    local_cost_line,
    reset_text,
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


def switchable(
    tool: str, active: bool | None, provider: str | None, kind: str = "", registered: bool = True
) -> bool:
    """Can a click switch to this account without a terminal?

    OpenCode: native ``auth switch``.  Muse: a browser login ccswap keeps a token for.
    A Muse API key is listed but never clickable (``ccswap muse run``).  Kiro:
    another profile of the signed-in login, via Kiro's own picker.  A different Kiro
    *login* still needs an interactive sign-in, and a Kiro API key is process-scoped
    (``ccswap kiro run``).
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


_REFUSED_SUFFIX = " · last request refused"


def _menu_numbers(result: UsageResult) -> str | None:
    """This reading's figures with no state words: windows, else the Go/local line."""
    if result.windows:
        return " · ".join(window_text(w, short=True) for w in result.windows[:2])
    text = go_headline(result)
    if text is not None:
        return text.removesuffix(_REFUSED_SUFFIX)
    return local_cost_line(result)


def menu_usage_text(result: UsageResult | None) -> str:
    """One account's menu text: its numbers, or "—" when there is nothing to show.

    A past reading keeps its numbers with no state label; a bare failure becomes
    the placeholder, so failure words structurally cannot reach the menu bar.
    """
    if result is not None and (numbers := _menu_numbers(result)):
        return numbers
    return "—"


def menu_headline(summary: ToolSummary) -> str:
    """A section title's figures: like overview.headline without the state words."""
    if not summary.installed and not summary.accounts:
        return "not installed"
    primary = summary.primary_usage
    if primary is None:
        return "—"
    return menu_usage_text(primary)


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
        if "refused" in line.lower():
            continue  # the menu never names a refused request; the CLI/TUI detail keeps it
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
    # No state or reason row: a past reading shows its numbers above with no label,
    # and a bare failure shows no usage rows at all (callers render "—").
    if result.observed_at:
        rows.append(MenuRow(f"{indent}Updated {_ago(result.observed_at, now)}", tool))
    return rows


def section_for(summary: ToolSummary, docs: str | None, now: float) -> MenuSection:
    tool = summary.tool
    label = LABELS.get(tool, summary.label)
    section = MenuSection(tool, f"{label} · {menu_headline(summary)}")
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
        rows.append(MenuRow(summary.error[:110], tool))
    if summary.tool_usage is not None:
        rows.append(None)
        rows.extend(usage_rows(tool, summary.tool_usage, now))
    rows.append(None)
    rows.append(MenuRow("Accounts", tool))
    if not summary.accounts:
        rows.append(MenuRow(f"  No {label} accounts registered", tool))
    for number, (view, usage) in enumerate(summary.accounts, 1):
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
        label += f"  {menu_usage_text(usage)}"
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
    for view, usage in summary.accounts:
        mark = {True: "●", False: "○", None: "?"}[view.active]
        where = (
            f"{view.provider}/"
            if view.provider and not view.alias.startswith(f"{view.provider}/")
            else ""
        )
        ok = _row_switchable(tool, view, usage)
        # A Muse API key is listed for the captain's "every account" view but stays
        # inert: no click and no "switch in terminal" hint (it runs, not switches).
        inert_key = tool == "muse" and view.kind == "api-key"
        hint = (
            ""
            if ok or view.active or inert_key
            else ("  — switch in terminal" if tool != "opencode" else "")
        )
        mark = "○" if _session_dead(usage) else mark
        text = f"  {mark} {where}{view.alias} · {menu_usage_text(usage)}{hint}"
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
