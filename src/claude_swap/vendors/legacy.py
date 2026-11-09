"""Claude Code, Codex and Antigravity seen through the shared usage model.

These are *read-only bridges*: their switchers (``switcher.py``, ``codex.py``,
``agy.py``) keep owning credentials, switching, pacing and usage fetching,
unchanged.  A bridge only turns a switcher's structured
:class:`~claude_swap.models.AccountsSnapshot` into :class:`AccountView` and
:class:`UsageResult`, so the overview, ``ccswap usage``, the menu bar and Raycast
can show all six tools as peers from one model without a second implementation
of anything the switchers already do.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime
from typing import Any, ClassVar

from claude_swap.models import AccountsSnapshot
from claude_swap.usage_store import STALE_OK_S
from claude_swap.vendors.core import (
    AccountView,
    Capability,
    CapabilityStatus,
    Support,
    ToolAdapter,
    ToolInfo,
    UsageKind,
    UsageObservation,
    UsageResult,
    UsageState,
    UsageWindow,
    cap,
    percent,
)

# Keyed by the switchers' sentinel strings (json_output.USAGE_*).
_SENTINEL_STATES = {
    "api key": (UsageState.UNAVAILABLE, "API-key login: no subscription quota"),
    "token expired": (UsageState.STALE, "Token expired; ccswap refreshes it automatically"),
    "re-login needed": (UsageState.UNAUTHENTICATED, "Re-login needed, then `ccswap add`"),
    "no credentials": (UsageState.UNAUTHENTICATED, "No stored credential for this account"),
    "keychain unavailable": (UsageState.ERROR, "Keychain locked or in use"),
    "foreign credential": (UsageState.STALE, "The live credential belongs to another account"),
}


def _epoch(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)  # 3.11+ accepts a trailing Z
    except ValueError:
        return None
    return parsed.timestamp() if parsed.tzinfo else None


def _pct_window(key: str, label: str, raw: Any, scope: str = "account") -> UsageWindow | None:
    if not isinstance(raw, dict) or not isinstance(raw.get("pct"), (int, float)):
        return None
    pct = float(raw["pct"])
    return UsageWindow(
        key=key,
        label=label,
        unit="percent",
        used_percent=pct,
        remaining=max(0.0, 100.0 - pct),
        resets_at=_epoch(raw.get("resets_at")),
        scope=scope,
    )


def windows_from_usage(usage: dict | None) -> tuple[list[UsageWindow], dict]:
    """The switchers' internal usage dict -> independent windows (+ leftover extras)."""
    if not isinstance(usage, dict):
        return [], {}
    windows: list[UsageWindow] = []
    for key, out_key, label in (
        ("five_hour", "five-hour", "5-hour window"),
        ("seven_day", "weekly", "7-day window"),
        ("weekly", "weekly", "Weekly"),
    ):
        window = _pct_window(out_key, label, usage.get(key))
        if window:
            windows.append(window)
    for scoped in usage.get("scoped") or []:
        if isinstance(scoped, dict):
            name = str(scoped.get("name") or scoped.get("bucket_name") or "model")
            window = _pct_window(f"scoped:{name}", name, scoped)
            if window:
                windows.append(window)
    spend = usage.get("spend")
    if isinstance(spend, dict) and isinstance(spend.get("limit"), (int, float)):
        used = float(spend.get("used") or 0)
        limit = float(spend["limit"])
        windows.append(
            UsageWindow(
                key="spend",
                label="Extra usage",
                unit=str(spend.get("currency") or "usd").lower(),
                used=used,
                limit=limit,
                remaining=max(0.0, limit - used),
                used_percent=percent(used, limit),
                resets_at=_epoch(spend.get("resets_at")),
            )
        )
    allowance = usage.get("credit_allowance")
    if isinstance(allowance, dict) and isinstance(allowance.get("limit"), (int, float)):
        allowance_used = allowance.get("used")
        windows.append(
            UsageWindow(
                key="credits",
                label="Credits",
                unit="credits",
                used=float(allowance_used) if isinstance(allowance_used, (int, float)) else None,
                limit=float(allowance["limit"]),
                remaining=float(allowance["remaining"])
                if isinstance(allowance.get("remaining"), (int, float))
                else None,
                used_percent=float(allowance["pct"])
                if isinstance(allowance.get("pct"), (int, float))
                else None,
                resets_at=_epoch(allowance.get("resets_at")),
            )
        )
    extras = {k: usage[k] for k in ("credits", "reset_credits") if isinstance(usage.get(k), dict)}
    return windows, extras


class LegacyTool(ToolAdapter):
    """A bridge: identity, detection and a read-only usage/accounts projection."""

    surface = "dashboard"  # where its full first-class experience lives

    def __init__(self, switcher_factory: Callable[[], Any], runner=None) -> None:
        super().__init__(runner)
        self._factory = switcher_factory
        self._switcher: Any = None

    @property
    def switcher(self) -> Any:
        if self._switcher is None:
            self._switcher = self._factory()
        return self._switcher

    def capabilities(self, info: ToolInfo) -> dict[Capability, CapabilityStatus]:
        return dict(self.CAPS)

    CAPS: ClassVar[dict[Capability, CapabilityStatus]] = {}

    def snapshot(self, *, refresh: bool = False) -> AccountsSnapshot:
        # ``fetch=None`` lets the switcher's own pacing decide what is due (as `list`
        # does); store-only reads never touch the network.
        return self.switcher.accounts_snapshot(fetch=None if refresh else set())

    def accounts_usage(self, *, refresh: bool = False) -> list[tuple[AccountView, UsageResult]]:
        snap = self.snapshot(refresh=refresh)
        out = []
        for acc in snap.accounts:
            identity = {"number": str(acc.number)}
            if acc.email:
                identity["email"] = acc.email
            if acc.org_name:
                identity["organization"] = acc.org_name
            view = AccountView(
                self.key,
                acc.alias or acc.number,
                acc.kind,
                identity=identity,
                active=acc.is_active,
                note="disabled" if acc.disabled else "",
            )
            out.append((view, self.usage_of(acc)))
        return out

    def usage_of(self, acc: Any) -> UsageResult:
        entry = acc.usage
        source = f"ccswap {self.key} usage store"
        observed = time.time() - entry.age_s if entry.age_s is not None else entry.fetched_at
        windows, extras = windows_from_usage(entry.last_good)
        observations: tuple[UsageObservation, ...] = ()
        if extras:
            observations = (
                UsageObservation(UsageKind.CREDITS, source, True, data=extras, fetched_at=observed),
            )
        if entry.sentinel:
            state, reason = _SENTINEL_STATES.get(entry.sentinel, (UsageState.ERROR, entry.sentinel))
            return UsageResult(
                state if windows or state is not UsageState.STALE else UsageState.ERROR,
                reason,
                windows=tuple(windows),
                observations=observations,
                source=source,
                authoritative=True,
                attributed_to=acc.alias or acc.email,
                observed_at=observed,
                error=reason,
            )
        if not windows and not observations:
            return UsageResult(
                UsageState.UNAVAILABLE,
                entry.last_error or "No usage measured yet",
                source=source,
                attributed_to=acc.alias or acc.email,
            )
        stale = entry.age_s is not None and entry.age_s > STALE_OK_S and not entry.trust_extended
        return UsageResult(
            UsageState.STALE if stale else UsageState.AVAILABLE,
            windows=tuple(windows),
            observations=observations,
            source=source,
            authoritative=True,
            attributed_to=acc.alias or acc.email,
            observed_at=observed,
            error=entry.last_error if stale else None,
        )


_S = Support


class ClaudeTool(LegacyTool):
    key, label, binary = "claude", "Claude Code", "claude"
    docs = "https://docs.claude.com/en/docs/claude-code"
    CAPS: ClassVar[dict[Capability, CapabilityStatus]] = {
        Capability.ACCOUNT_ENUMERATION: cap(_S.SUPPORTED, "ccswap account store (`ccswap list`)"),
        Capability.SWITCH: cap(_S.SUPPORTED, "`ccswap switch` (credential swap, verified)"),
        Capability.USAGE: cap(
            _S.SUPPORTED, "Anthropic OAuth usage API per account (5h, 7d, per-model)"
        ),
        Capability.AUTO_SWITCH: cap(_S.SUPPORTED, "`ccswap auto`"),
        Capability.SESSION_ISOLATION: cap(_S.SUPPORTED, "`ccswap run <account>`"),
    }

    def __init__(self, runner=None) -> None:
        from claude_swap.switcher import ClaudeAccountSwitcher

        super().__init__(ClaudeAccountSwitcher, runner)


class CodexTool(LegacyTool):
    key, label, binary = "codex", "Codex", "codex"
    docs = "https://developers.openai.com/codex"
    CAPS: ClassVar[dict[Capability, CapabilityStatus]] = {
        Capability.ACCOUNT_ENUMERATION: cap(
            _S.SUPPORTED, "ccswap Codex store (`ccswap codex list`)"
        ),
        Capability.SWITCH: cap(_S.SUPPORTED, "`ccswap codex switch`"),
        Capability.USAGE: cap(_S.SUPPORTED, "ChatGPT plan usage per account (5h, weekly, credits)"),
        Capability.AUTO_SWITCH: cap(_S.SUPPORTED, "`ccswap auto` (Codex engine)"),
        Capability.SESSION_ISOLATION: cap(_S.SUPPORTED, "`ccswap codex run <account>`"),
    }

    def __init__(self, runner=None) -> None:
        from claude_swap.codex import CodexAccountSwitcher

        super().__init__(CodexAccountSwitcher, runner)


class AgyTool(LegacyTool):
    key, label, binary = "agy", "Antigravity", "agy"
    docs = "https://antigravity.google/docs/getting-started/"
    CAPS: ClassVar[dict[Capability, CapabilityStatus]] = {
        Capability.ACCOUNT_ENUMERATION: cap(_S.SUPPORTED, "ccswap Antigravity store"),
        Capability.SWITCH: cap(_S.SUPPORTED, "`ccswap agy switch`"),
        Capability.USAGE: cap(
            _S.SUPPORTED, "`agy -p /usage --output-format json` (per-model scoped)"
        ),
        Capability.AUTO_SWITCH: cap(_S.UNSUPPORTED, "not offered by ccswap for Antigravity"),
    }

    def __init__(self, runner=None) -> None:
        from claude_swap.agy import AgyAccountSwitcher

        super().__init__(AgyAccountSwitcher, runner)


LEGACY_FACTORIES: tuple[Callable[..., LegacyTool], ...] = (ClaudeTool, CodexTool, AgyTool)
