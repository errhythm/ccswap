"""Vendor-neutral operations shared by the CLI, TUI, menu bar and Raycast.

Every surface calls this class; none of them re-implements selection,
locking, verification or JSON shaping.  It only talks to adapters through the
capability protocols, so it contains no per-vendor branching except the
OpenCode ``provider`` qualifier, which is data (``AccountRecord.provider``).
"""

from __future__ import annotations

import dataclasses
import threading
import time
from collections.abc import Callable, Sequence
from typing import Any

from claude_swap.exceptions import ClaudeSwitchError
from claude_swap.json_output import SCHEMA_VERSION
from claude_swap.locking import FileLock
from claude_swap.vendors.core import (
    AccountNotRegistered,
    AccountRecord,
    AccountView,
    Capability,
    RegistrationCapability,
    SessionCapability,
    Support,
    SwitchingCapability,
    SwitchOutcome,
    ToolAdapter,
    UnsupportedCapability,
    UsageCapability,
    UsageResult,
    UsageState,
    VendorError,
)
from claude_swap.vendors.registry import ToolRegistry
from claude_swap.vendors.store import VendorStore, validate_alias
from claude_swap.vendors.usage_cache import ACTIVE_KEY, UsageCache

USAGE_TTL_OK = 60.0
USAGE_TTL_FAIL = 15.0
# Per-tool (success, failure) freshness: each probe spawns a CLI, OpenCode's aggregate
# scans every local message, so these are polled less often than a cheap status call.
USAGE_TTLS: dict[str, tuple[float, float]] = {
    "muse": (60.0, 20.0),
    "kiro": (180.0, 30.0),
    "opencode": (300.0, 60.0),
}


class VendorService:
    def __init__(
        self,
        registry: ToolRegistry | None = None,
        store: VendorStore | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.registry = registry or ToolRegistry.default()
        self.store = store or VendorStore()
        self._clock = clock
        self._usage_cache: dict[tuple[str, str], tuple[float, UsageResult]] = {}
        self._locks: dict[tuple[str, str], threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self.usage_store = UsageCache(self.store.root)

    # ---- helpers ---------------------------------------------------------

    def adapter(self, tool: str) -> ToolAdapter:
        return self.registry.get(tool)

    def _need(
        self, adapter: ToolAdapter, protocol: type, capability: Capability, verb: str
    ) -> None:
        if isinstance(adapter, protocol):
            return
        raise UnsupportedCapability(f"{adapter.label} does not support {verb}")

    def resolve(
        self,
        tool: str,
        selector: str,
        provider: str | None = None,
        *,
        allow_index: bool = True,
    ) -> AccountRecord:
        """Alias first (so a numeric alias stays reachable), then ``list`` position.

        ``allow_index=False`` is for selectors that are vendor identifiers (an OpenCode
        account id/label), which must never be reinterpreted as a list position.
        """
        records = [
            r for r in self.store.records(tool) if provider is None or r.provider == provider
        ]
        exact = [r for r in records if r.alias == selector]
        if exact:
            return exact[0]
        if allow_index and selector.isascii() and selector.isdigit():
            index = int(selector)
            if 1 <= index <= len(records):
                return records[index - 1]
            raise AccountNotRegistered(f"No registered {tool} account number {selector}")
        lowered = selector.lower()
        loose = [
            r
            for r in records
            if r.alias.lower() == lowered
            or lowered
            in (r.identity.get("email", "").lower(), r.identity.get("account", "").lower())
        ]
        if len(loose) == 1:
            return loose[0]
        if len(loose) > 1:
            raise VendorError(
                f"'{selector}' matches several {tool} accounts; use the alias or number"
            )
        raise AccountNotRegistered(
            f"No registered {tool} account '{selector}' (see `ccswap {tool} list`)"
        )

    def _secret(self, record: AccountRecord) -> str | None:
        return self.store.read_secret(record) if record.secret_ref else None

    # ---- read paths -------------------------------------------------------

    def capability_table(self, adapter: ToolAdapter) -> dict[str, dict]:
        info = adapter.detect()
        return {c.value: s.to_json() for c, s in adapter.capabilities(info).items()}

    def vendor_status(self, adapter: ToolAdapter) -> dict[str, Any]:
        """One vendor's isolated status; a failure never leaks into its siblings."""
        payload: dict[str, Any] = {"tool": adapter.key, "label": adapter.label, "error": None}
        try:
            info = adapter.detect()
            payload.update(info.to_json())
            payload["capabilities"] = {
                c.value: s.to_json() for c, s in adapter.capabilities(info).items()
            }
            payload["accounts"] = len(self.store.records(adapter.key))
            payload["auth"] = adapter.auth_summary() if info.installed else None
        except ClaudeSwitchError as exc:
            payload.setdefault("installed", False)
            payload["error"] = str(exc)
        return payload

    def vendors_payload(self) -> dict:
        return {
            "schemaVersion": SCHEMA_VERSION,
            "vendors": [self.vendor_status(a) for a in self.registry.adapters()],
        }

    def list_views(self, tool: str) -> list[AccountView]:
        adapter = self.adapter(tool)
        records = self.store.records(tool)
        if not adapter.detect().installed:
            return [
                AccountView(
                    tool,
                    r.alias,
                    r.kind,
                    r.provider,
                    r.identity,
                    active=None,
                    note=f"{adapter.label} is not installed",
                )
                for r in records
            ]
        return adapter.observe(records, self.store.active(tool))

    def list_payload(self, tool: str, views: list[AccountView] | None = None) -> dict:
        adapter = self.adapter(tool)
        info = adapter.detect()
        if views is None:
            views = self.list_views(tool)
        return {
            "schemaVersion": SCHEMA_VERSION,
            "tool": tool,
            "installed": info.installed,
            "version": info.version,
            "accounts": [v.to_json() for v in views],
        }

    def status_payload(self, tool: str) -> dict:
        payload = self.vendor_status(self.adapter(tool))
        payload["schemaVersion"] = SCHEMA_VERSION
        return payload

    # ---- mutations --------------------------------------------------------

    def add(self, tool: str, alias: str, **options: Any) -> AccountRecord:
        adapter = self.adapter(tool)
        self._need(adapter, RegistrationCapability, Capability.REGISTRATION, "account registration")
        validate_alias(alias)
        adapter.require_installed()
        record, secret = adapter.register(alias, **options)  # type: ignore[attr-defined]
        if record.identity:
            for other in self.store.records(tool):
                if (other.kind, other.provider, dict(other.identity)) == (
                    record.kind,
                    record.provider,
                    dict(record.identity),
                ):
                    raise VendorError(
                        f"That {adapter.label} account is already registered as '{other.alias}'"
                    )
        return self.store.add(record, secret)

    def remove(self, tool: str, selector: str, provider: str | None = None) -> AccountRecord:
        """Forget a registration only; the vendor's login is left untouched."""
        record = self.resolve(tool, selector, provider)
        removed = self.store.remove(tool, record.alias, record.provider)
        self.usage_store.drop(tool, removed.alias)
        return removed

    def switch(
        self,
        tool: str,
        selector: str,
        *,
        provider: str | None = None,
        assume_yes: bool = False,
        interactive: bool = False,
        raw: bool = False,
    ) -> tuple[SwitchOutcome, AccountRecord | None]:
        adapter = self.adapter(tool)
        self._need(adapter, SwitchingCapability, Capability.SWITCH, "account switching")
        adapter.require_installed()
        records = self.store.records(tool)
        record: AccountRecord | None = None
        if not raw:
            try:
                record = self.resolve(tool, selector, provider, allow_index=tool != "opencode")
            except AccountNotRegistered:
                if tool != "opencode" or provider is None:
                    raise
        if record is None and (tool != "opencode" or provider is None):
            raise AccountNotRegistered(f"No registered {tool} account '{selector}'")
        # OpenCode only: an unregistered but vendor-listed (provider, account) is switchable.
        secret = self._secret(record) if record else None
        lock = FileLock(self.store.root / f"switch-{tool}.lock", timeout=10.0)
        if not lock.acquire():
            raise VendorError(f"Another {adapter.label} switch is already running")
        try:
            if record is None:
                outcome = adapter.switch_account(provider, selector)  # type: ignore[attr-defined]
            else:
                outcome = adapter.switch(  # type: ignore[attr-defined]
                    record, secret, assume_yes=assume_yes, interactive=interactive, context=records
                )
            warnings = list(outcome.warnings)
            if record is not None and outcome.activates:
                try:
                    self.store.set_active(tool, record.alias, record.provider)
                except ClaudeSwitchError:
                    warnings.append("The switch succeeded but ccswap could not record it locally")
            return dataclasses.replace(outcome, warnings=tuple(warnings)), record
        finally:
            lock.release()

    # ---- usage ------------------------------------------------------------

    def usage(
        self, tool: str, selector: str | None = None, *, refresh: bool = False
    ) -> UsageResult:
        """One tool/account's usage: in-memory TTL cache, request coalescing, and the
        persistent last-good fallback (returned as ``STALE`` with the new error)."""
        adapter = self.adapter(tool)
        self._need(adapter, UsageCapability, Capability.USAGE, "usage reporting")
        record = self.resolve(tool, selector) if selector else None
        account_key = record.alias if record else ACTIVE_KEY
        key = (tool, account_key)
        with self._key_lock(key):  # concurrent askers share one vendor call
            now = self._clock()
            cached = self._usage_cache.get(key)
            if cached and not refresh:
                ttl_ok, ttl_fail = USAGE_TTLS.get(tool, (USAGE_TTL_OK, USAGE_TTL_FAIL))
                ttl = ttl_ok if cached[1].state is UsageState.AVAILABLE else ttl_fail
                if now - cached[0] < ttl:
                    result = cached[1]
                    return dataclasses.replace(
                        result,
                        observations=tuple(
                            dataclasses.replace(o, cached=True) for o in result.observations
                        ),
                    )
            try:
                fresh = adapter.usage(record, self._secret(record) if record else None)  # type: ignore[attr-defined]
            except ClaudeSwitchError as exc:
                fresh = UsageResult(UsageState.ERROR, str(exc))
            result = self._settle(tool, account_key, fresh)
            self._usage_cache[key] = (now, result)
            if account_key == ACTIVE_KEY and result.state is UsageState.AVAILABLE:
                owner = result.attributed_to
                if owner and any(r.alias == owner for r in self.store.records(tool)):
                    # Same reading, same owner: an account-level ask is answered without a
                    # second vendor call.
                    self._usage_cache[(tool, owner)] = (now, result)
            return result

    def _settle(self, tool: str, account_key: str, fresh: UsageResult) -> UsageResult:
        """Persist good data under the account it belongs to; fall back to it when stale."""
        if fresh.state is UsageState.AVAILABLE:
            self.usage_store.put(tool, account_key, fresh)
            if account_key == ACTIVE_KEY and fresh.attributed_to:
                # The tool told us whose numbers these are (e.g. Kiro's whoami email): file
                # them under that registered account too, never under any other.
                owner = self._owner(tool, fresh.attributed_to)
                if owner is not None:
                    self.usage_store.put(tool, owner, fresh)
                    fresh = dataclasses.replace(fresh, attributed_to=owner)
            return fresh
        if fresh.state in (UsageState.UNSUPPORTED, UsageState.UNAUTHENTICATED):
            return fresh
        last = self.usage_store.get(tool, account_key)
        if last is None:
            return fresh
        return dataclasses.replace(
            last,
            state=UsageState.STALE,
            error=fresh.reason or fresh.error or "refresh failed",
            hint=last.hint,
        )

    def _owner(self, tool: str, attributed: str) -> str | None:
        wanted = attributed.lower()
        matches = [
            r.alias
            for r in self.store.records(tool)
            if r.alias.lower() == wanted or r.identity.get("email", "").lower() == wanted
        ]
        return matches[0] if len(matches) == 1 else None

    def _key_lock(self, key: tuple[str, str]) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(key, threading.Lock())

    def accounts_usage(
        self, tool: str, *, refresh: bool = False
    ) -> list[tuple[AccountView, UsageResult]]:
        """Every registered account with *its own* usage (never the active one's)."""
        out = []
        for view in self.list_views(tool):
            if not view.registered:
                continue
            try:
                result = self.usage(tool, view.alias, refresh=refresh)
            except ClaudeSwitchError as exc:
                result = UsageResult(UsageState.ERROR, str(exc))
            out.append((view, result))
        return out

    def usage_payload(
        self, tool: str, selector: str | None = None, *, refresh: bool = False
    ) -> dict:
        return self.usage_envelope(tool, selector, self.usage(tool, selector, refresh=refresh))

    @staticmethod
    def usage_envelope(tool: str, selector: str | None, result: UsageResult) -> dict:
        payload = result.to_json()
        payload.update(schemaVersion=SCHEMA_VERSION, tool=tool, account=selector)
        return payload

    # ---- sessions -----------------------------------------------------------

    def run(
        self, tool: str, selector: str, args: Sequence[str], provider: str | None = None
    ) -> int:
        adapter = self.adapter(tool)
        self._need(adapter, SessionCapability, Capability.SESSION_ISOLATION, "isolated sessions")
        record = self.resolve(tool, selector, provider)
        spec = adapter.launch_spec(record, self._secret(record), list(args))  # type: ignore[attr-defined]
        return adapter.runner.run_interactive(
            spec.argv, env=spec.env_overlay, unset_env=spec.unset_env
        )

    # ---- policy -------------------------------------------------------------

    def auto_switch_state(self, tool: str) -> dict:
        """Auto-switching is only offered where quota is reliable *and* switching is safe."""
        adapter = self.adapter(tool)
        status = adapter.capabilities(adapter.detect())[Capability.AUTO_SWITCH]
        return {
            "tool": tool,
            "available": status.support is Support.SUPPORTED,
            "reason": status.mechanism,
        }
