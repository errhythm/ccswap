"""All tools as peers: one collection pass and one presentation vocabulary.

The CLI (``ccswap usage``), the TUI tool pages and overview, the menu bar and
Raycast all read :func:`collect` / :func:`summary_json` and format with
:func:`headline` / :func:`window_text`, so the same account never shows two
different numbers on two surfaces.  Every tool is collected on its own worker
with its own deadline: one hung CLI never delays the others.
"""

from __future__ import annotations

import concurrent.futures as cf
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from claude_swap.exceptions import ClaudeSwitchError
from claude_swap.json_output import SCHEMA_VERSION
from claude_swap.vendors.core import AccountView, UsageResult, UsageState, UsageWindow
from claude_swap.vendors.legacy import LEGACY_FACTORIES, LegacyTool
from claude_swap.vendors.service import VendorService

# Display order: the peers, then Antigravity.
TOOL_ORDER = ("claude", "codex", "muse", "kiro", "opencode", "agy")
LABELS = {
    "claude": "Claude Code",
    "codex": "Codex",
    "muse": "Muse Code",
    "kiro": "Kiro CLI",
    "opencode": "OpenCode",
    "agy": "Antigravity",
}
TOOL_TIMEOUT_S = 75.0
MAX_WORKERS = 4


@dataclass
class ToolSummary:
    tool: str
    label: str
    installed: bool = False
    version: str | None = None
    surface: str = "tool"  # "dashboard" for Claude/Codex/Antigravity, else "tool"
    accounts: list[tuple[AccountView, UsageResult]] = field(default_factory=list)
    tool_usage: UsageResult | None = None  # tool-level (active credential / local stats)
    auth: dict = field(default_factory=dict)
    error: str | None = None
    refreshed_at: float | None = None

    @property
    def active(self) -> tuple[AccountView, UsageResult] | None:
        return next(((v, u) for v, u in self.accounts if v.active), None)

    @property
    def primary_usage(self) -> UsageResult | None:
        """The usage that describes 'this tool now': the tool-level reading when the
        tool has one, else the active account's own reading."""
        if self.tool_usage is not None:
            return self.tool_usage
        active = self.active
        return active[1] if active else None


class Overview:
    def __init__(
        self,
        service: VendorService | None = None,
        legacy: Iterable[LegacyTool] | None = None,
        *,
        legacy_fetch: bool = True,
    ):
        # legacy_fetch=False: read Claude/Codex/Antigravity from their usage stores only
        # (for surfaces that already run those switchers' own paced refresh).
        self.legacy_fetch = legacy_fetch
        self.service = service or VendorService()
        self.legacy = {
            t.key: t for t in (legacy if legacy is not None else (f() for f in LEGACY_FACTORIES))
        }

    def tools(self) -> list[str]:
        known = set(self.legacy) | set(self.service.registry.keys())
        ordered = [t for t in TOOL_ORDER if t in known]
        return ordered + sorted(known - set(ordered))  # future vendors appear automatically

    def collect_one(self, tool: str, *, refresh: bool = False) -> ToolSummary:
        if tool in self.legacy:
            return self._legacy(self.legacy[tool], refresh)
        return self._vendor(tool, refresh)

    def collect(
        self, tools: Iterable[str] | None = None, *, refresh: bool = False
    ) -> list[ToolSummary]:
        wanted = list(tools) if tools is not None else self.tools()
        results: dict[str, ToolSummary] = {}
        pool = cf.ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="ccswap-usage")
        try:
            futures = {pool.submit(self.collect_one, t, refresh=refresh): t for t in wanted}
            deadline = time.monotonic() + TOOL_TIMEOUT_S
            for future, tool in futures.items():
                try:
                    results[tool] = future.result(timeout=max(0.1, deadline - time.monotonic()))
                except cf.TimeoutError:
                    results[tool] = ToolSummary(tool, LABELS.get(tool, tool), error="timed out")
                except Exception as exc:  # noqa: BLE001 - one tool must never break the others
                    results[tool] = ToolSummary(
                        tool, LABELS.get(tool, tool), error=_safe_error(exc)
                    )
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        return [results[t] for t in wanted]

    # ---- per kind ----------------------------------------------------------

    def _legacy(self, adapter: LegacyTool, refresh: bool) -> ToolSummary:
        summary = ToolSummary(adapter.key, adapter.label, surface="dashboard")
        info = adapter.detect()
        summary.installed, summary.version = info.installed, info.version
        try:
            summary.accounts = adapter.accounts_usage(refresh=refresh or self.legacy_fetch)
        except (ClaudeSwitchError, OSError) as exc:
            summary.error = _safe_error(exc)
        summary.refreshed_at = time.time()
        return summary

    def _vendor(self, tool: str, refresh: bool) -> ToolSummary:
        service = self.service
        adapter = service.adapter(tool)
        summary = ToolSummary(tool, adapter.label)
        info = adapter.detect()
        summary.installed, summary.version = info.installed, info.version
        if not info.installed:
            summary.refreshed_at = time.time()
            return summary
        try:
            summary.auth = adapter.auth_summary()
        except (ClaudeSwitchError, OSError) as exc:
            summary.error = _safe_error(exc)
        try:
            summary.tool_usage = service.usage(tool, refresh=refresh)
        except ClaudeSwitchError as exc:
            summary.tool_usage = UsageResult(UsageState.ERROR, _safe_error(exc))
        try:
            summary.accounts = service.accounts_usage(tool, refresh=refresh)
            # Discovered-but-unregistered accounts (e.g. OpenCode provider accounts,
            # Kiro's current login) are shown too, without usage of their own.
            registered = {v.alias for v, _ in summary.accounts}
            for view in service.list_views(tool):
                if not view.registered and view.alias not in registered:
                    summary.accounts.append(
                        (view, UsageResult(UsageState.UNAVAILABLE, "Not registered with ccswap"))
                    )
        except (ClaudeSwitchError, OSError) as exc:
            summary.error = _safe_error(exc)
        summary.refreshed_at = time.time()
        return summary


def summary_from_snapshot(
    adapter: LegacyTool, snapshot: Any, *, installed: bool = True
) -> ToolSummary:
    """A Claude/Codex/Antigravity summary from a snapshot the caller already has
    (the TUI and menu bar poll those switchers themselves; no second fetch)."""
    summary = ToolSummary(adapter.key, adapter.label, installed=installed, surface="dashboard")
    if snapshot is not None:
        for acc in snapshot.accounts:
            identity = {"number": str(acc.number)}
            if acc.email:
                identity["email"] = acc.email
            view = AccountView(
                adapter.key,
                acc.alias or acc.number,
                acc.kind,
                identity=identity,
                active=acc.is_active,
                note="disabled" if acc.disabled else "",
            )
            summary.accounts.append((view, adapter.usage_of(acc)))
        summary.refreshed_at = getattr(snapshot, "taken_at", None)
    return summary


def _safe_error(exc: BaseException) -> str:
    if isinstance(exc, ClaudeSwitchError):
        return str(exc)[:200]
    return f"{type(exc).__name__} while reading this tool"


# ---- presentation ---------------------------------------------------------

_SHORT = {"five-hour": "5h", "weekly": "wk", "monthly-credits": "credits", "bonus-credits": "bonus"}


def _num(value: float) -> str:
    return f"{value:,.0f}" if abs(value) >= 100 or value == int(value) else f"{value:,.2f}"


def window_text(window: UsageWindow, *, short: bool = False) -> str:
    """ "5h 34% used" / "1,108 of 2,000 credits used (55.4%)" -- never a fake number."""
    label = _SHORT.get(window.key, window.label) if short else window.label
    if window.used is not None and window.limit is not None:
        unit = "" if window.unit in ("percent",) else f" {window.unit}"
        pct = f" ({window.used_percent:g}%)" if window.used_percent is not None else ""
        if short:
            return f"{label} {_num(window.used)}/{_num(window.limit)}{pct}"
        return f"{label}: {_num(window.used)} of {_num(window.limit)}{unit} used{pct}"
    if window.used_percent is not None:
        return f"{label} {window.used_percent:g}%" + ("" if short else " used")
    if window.used is not None:
        return f"{label} {_num(window.used)} {window.unit} used"
    return f"{label}: not reported"


def reset_text(window: UsageWindow, now: float | None = None) -> str | None:
    if window.resets_at is not None:
        now = now or time.time()
        delta = window.resets_at - now
        when = datetime.fromtimestamp(window.resets_at).strftime("%a %H:%M")
        if delta <= 0:
            return f"reset due ({when})"
        hours, rem = divmod(int(delta), 3600)
        days, hours = divmod(hours, 24)
        span = f"{days}d {hours}h" if days else f"{hours}h {rem // 60}m"
        return f"resets in {span} ({when})"
    if window.resets_on:
        return f"resets on {window.resets_on}"
    return None


_STATE_WORDS = {
    UsageState.UNAVAILABLE: "usage unavailable",
    UsageState.UNSUPPORTED: "usage not reported",
    UsageState.UNAUTHENTICATED: "sign-in required",
    UsageState.ERROR: "usage error",
}


def usage_headline(result: UsageResult | None) -> str:
    if result is None:
        return "no active account"
    if result.windows:
        text = " · ".join(window_text(w, short=True) for w in result.windows[:2])
        return f"stale · {text}" if result.state is UsageState.STALE else text
    local = local_cost_line(result)
    if local:
        return local
    return _STATE_WORDS.get(result.state, result.state.value)


def local_cost_line(result: UsageResult, period: str = "30d") -> str | None:
    for obs in result.observations:
        providers = obs.data.get("providers") if isinstance(obs.data, dict) else None
        if isinstance(providers, list) and providers:
            cost = sum(
                p["totals"][period]["costUsd"] for p in providers if period in p.get("totals", {})
            )
            msgs = sum(
                p["totals"][period]["messages"] for p in providers if period in p.get("totals", {})
            )
            return f"local {period}: {msgs:,} msgs · ${cost:,.2f} est."
    return None


def headline(summary: ToolSummary) -> str:
    if not summary.installed and not summary.accounts:
        return "not installed"
    if summary.error and not summary.accounts and summary.tool_usage is None:
        return "error"
    return usage_headline(summary.primary_usage)


def account_json(view: AccountView, usage: UsageResult) -> dict[str, Any]:
    return {**view.to_json(), "usage": usage.to_json(), "headline": usage_headline(usage)}


def summary_json(summary: ToolSummary) -> dict[str, Any]:
    return {
        "tool": summary.tool,
        "label": summary.label,
        "installed": summary.installed,
        "version": summary.version,
        "surface": summary.surface,
        "headline": headline(summary),
        "error": summary.error,
        "refreshedAt": summary.refreshed_at,
        "auth": summary.auth,
        "usage": summary.tool_usage.to_json() if summary.tool_usage else None,
        "accounts": [account_json(v, u) for v, u in summary.accounts],
    }


def overview_json(summaries: list[ToolSummary]) -> dict[str, Any]:
    return {"schemaVersion": SCHEMA_VERSION, "tools": [summary_json(s) for s in summaries]}


# ---- plain-text rendering (CLI) ---------------------------------------------


def tool_usage_title(summary: ToolSummary) -> str:
    if summary.tool == "opencode":
        return "local usage, all providers"
    who = summary.tool_usage.attributed_to if summary.tool_usage else None
    if who and who != "active credential":
        return f"signed-in account ({who})"
    return "active credential (the tool does not report whose)"


def render_usage_lines(result: UsageResult, indent: str = "    ") -> list[str]:
    lines: list[str] = []
    if result.plan:
        lines.append(f"{indent}plan: {result.plan}")
    for w in result.windows:
        reset = reset_text(w)
        scope = "" if w.scope == "account" else f" [{w.scope}]"
        lines.append(f"{indent}{window_text(w)}{scope}" + (f", {reset}" if reset else ""))
        if w.note:
            lines.append(f"{indent}  {w.note}")
    for obs in result.observations:
        providers = obs.data.get("providers") if isinstance(obs.data, dict) else None
        if isinstance(providers, list):
            lines.append(f"{indent}local statistics ({obs.source}; not a quota):")
            for p in providers[:12]:
                t = p["totals"]
                lines.append(
                    f"{indent}  {p['provider']:<18} 24h {t['24h']['messages']:>6,} msgs"
                    f"  30d {t['30d']['messages']:>7,} msgs ${t['30d']['costUsd']:>8,.2f}"
                    f"  all ${t['all']['costUsd']:>8,.2f}"
                )
        elif isinstance(obs.data, dict) and isinstance(obs.data.get("text"), str):
            lines.extend(f"{indent}{line}" for line in obs.data["text"].splitlines()[:30])
        elif obs.data:
            pairs = ", ".join(f"{k}={v}" for k, v in list(obs.data.items())[:6])
            lines.append(f"{indent}{obs.kind.value}: {pairs}")
    if result.state is not UsageState.AVAILABLE:
        word = _STATE_WORDS.get(result.state, result.state.value)
        lines.append(f"{indent}{word}: {result.reason or result.error or ''}".rstrip(": "))
        if result.state is UsageState.STALE and result.error:
            lines.append(f"{indent}refresh failed: {result.error}")
    if result.observed_at:
        age = max(0, int(time.time() - result.observed_at))
        stamp = datetime.fromtimestamp(result.observed_at).strftime("%H:%M:%S")
        span = f"{age // 60}m ago" if age >= 60 else f"{age}s ago"
        lines.append(f"{indent}as of {stamp} ({span}) · source: {result.source or 'n/a'}")
    if result.hint and result.state is UsageState.AVAILABLE:
        lines.append(f"{indent}{result.hint}")
    return lines


def render_summary(summary: ToolSummary) -> list[str]:
    head = f"{summary.label}"
    if not summary.installed:
        head += "  — not installed"
    elif summary.version:
        head += f"  {summary.version}"
    lines = [head + f"   {headline(summary)}"]
    if summary.error:
        lines.append(f"  ! {summary.error}")
    if summary.tool_usage is not None:
        lines.append(f"  {tool_usage_title(summary)}:")
        lines.extend(render_usage_lines(summary.tool_usage))
    for view, usage in summary.accounts:
        mark = {True: "*", False: " ", None: "?"}[view.active]
        prov = (
            f"{view.provider}/"
            if view.provider
            and summary.tool == "opencode"
            and not view.alias.startswith(view.provider)
            else ""
        )
        email = view.identity.get("email")
        tag = "" if view.registered else " [unregistered]"
        lines.append(
            f"  {mark} {prov}{view.alias}"
            + (f" <{email}>" if email and email != view.alias else "")
            + tag
        )
        if (
            usage.state is UsageState.UNAVAILABLE
            and summary.tool_usage is not None
            and not usage.windows
        ):
            lines.append(f"      {usage.reason}")
        else:
            lines.extend(render_usage_lines(usage, indent="      "))
    if not summary.accounts and summary.installed and summary.tool_usage is None:
        lines.append("  no accounts")
    return lines
