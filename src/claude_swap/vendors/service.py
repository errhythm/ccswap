"""Vendor-neutral operations shared by the CLI, TUI, menu bar and Raycast.

Every surface calls this class; none of them re-implements selection,
locking, verification or JSON shaping.  It only talks to adapters through the
capability protocols, so it contains no per-vendor branching except the
OpenCode ``provider`` qualifier, which is data (``AccountRecord.provider``).
"""

from __future__ import annotations

import dataclasses
import time
from typing import Any, Callable, Sequence

from claude_swap import paths
from claude_swap.exceptions import ClaudeSwitchError
from claude_swap.json_output import SCHEMA_VERSION
from claude_swap.locking import FileLock
from claude_swap.vendors.core import (
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

USAGE_TTL_OK = 60.0
USAGE_TTL_FAIL = 15.0


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

    # ---- helpers ---------------------------------------------------------

    def adapter(self, tool: str) -> ToolAdapter:
        return self.registry.get(tool)

    def _need(self, adapter: ToolAdapter, protocol: type, capability: Capability, verb: str) -> None:
        if isinstance(adapter, protocol):
            return
        raise UnsupportedCapability(f"{adapter.label} does not support {verb}")

    def resolve(
        self, tool: str, selector: str, provider: str | None = None
    ) -> AccountRecord:
        records = [
            r for r in self.store.records(tool) if provider is None or r.provider == provider
        ]
        if selector.isdigit():
            index = int(selector)
            if 1 <= index <= len(records):
                return records[index - 1]
            raise VendorError(f"No registered {tool} account number {selector}")
        exact = [r for r in records if r.alias == selector]
        loose = exact or [
            r
            for r in records
            if r.alias.lower() == selector.lower()
            or selector.lower() in (r.identity.get("email", "").lower(), r.identity.get("account", "").lower())
        ]
        if len(loose) == 1:
            return loose[0]
        if len(loose) > 1:
            raise VendorError(f"'{selector}' matches several {tool} accounts; use the alias or number")
        raise VendorError(f"No registered {tool} account '{selector}' (see `ccswap {tool} list`)")

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
                AccountView(tool, r.alias, r.kind, r.provider, r.identity, active=None,
                            note=f"{adapter.label} is not installed")
                for r in records
            ]
        return adapter.observe(records, self.store.active(tool))

    def list_payload(self, tool: str) -> dict:
        adapter = self.adapter(tool)
        info = adapter.detect()
        return {
            "schemaVersion": SCHEMA_VERSION,
            "tool": tool,
            "installed": info.installed,
            "version": info.version,
            "accounts": [v.to_json() for v in self.list_views(tool)],
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
        return self.store.add(record, secret)

    def remove(self, tool: str, selector: str, provider: str | None = None) -> AccountRecord:
        """Forget a registration only; the vendor's login is left untouched."""
        record = self.resolve(tool, selector, provider)
        return self.store.remove(tool, record.alias, record.provider)

    def switch(
        self,
        tool: str,
        selector: str,
        *,
        provider: str | None = None,
        assume_yes: bool = False,
        interactive: bool = False,
    ) -> tuple[SwitchOutcome, AccountRecord | None]:
        adapter = self.adapter(tool)
        self._need(adapter, SwitchingCapability, Capability.SWITCH, "account switching")
        adapter.require_installed()
        records = self.store.records(tool)
        try:
            record: AccountRecord | None = self.resolve(tool, selector, provider)
        except VendorError:
            if tool != "opencode" or provider is None:
                raise
            record = None  # OpenCode: an unregistered but vendor-listed account is fine
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
            if record is not None:
                try:
                    self.store.set_active(tool, record.alias, record.provider)
                except ClaudeSwitchError:
                    warnings.append("The switch succeeded but ccswap could not record it locally")
            return dataclasses.replace(outcome, warnings=tuple(warnings)), record
        finally:
            lock.release()

    # ---- usage ------------------------------------------------------------

    def usage(self, tool: str, selector: str | None = None, *, refresh: bool = False) -> UsageResult:
        adapter = self.adapter(tool)
        self._need(adapter, UsageCapability, Capability.USAGE, "usage reporting")
        record = self.resolve(tool, selector) if selector else None
        key = (tool, record.alias if record else "")
        now = self._clock()
        cached = self._usage_cache.get(key)
        if cached and not refresh:
            age = now - cached[0]
            ttl = USAGE_TTL_OK if cached[1].state is UsageState.AVAILABLE else USAGE_TTL_FAIL
            if age < ttl:
                result = cached[1]
                return dataclasses.replace(
                    result,
                    observations=tuple(dataclasses.replace(o, cached=True) for o in result.observations),
                )
        result = adapter.usage(record, self._secret(record) if record else None)  # type: ignore[attr-defined]
        self._usage_cache[key] = (now, result)
        return result

    def usage_payload(self, tool: str, selector: str | None = None, *, refresh: bool = False) -> dict:
        payload = self.usage(tool, selector, refresh=refresh).to_json()
        payload.update(schemaVersion=SCHEMA_VERSION, tool=tool, account=selector)
        return payload

    # ---- sessions -----------------------------------------------------------

    def run(self, tool: str, selector: str, args: Sequence[str], provider: str | None = None) -> int:
        adapter = self.adapter(tool)
        self._need(adapter, SessionCapability, Capability.SESSION_ISOLATION, "isolated sessions")
        record = self.resolve(tool, selector, provider)
        spec = adapter.launch_spec(record, self._secret(record), list(args))  # type: ignore[attr-defined]
        return adapter.runner.run_interactive(spec.argv, env=spec.env_overlay, unset_env=spec.unset_env)

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
