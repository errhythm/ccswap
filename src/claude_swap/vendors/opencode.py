"""OpenCode (``opencode``) adapter.

OpenCode is a coding agent *and* a hub for many model providers, so an
OpenCode "account" is a ``(provider, account)`` pair and several providers are
active simultaneously.  It is never modelled as one global login.

Verified against OpenCode 1.18.35 (installed) and
https://opencode.ai/docs/cli/providers (newer releases):

* 1.18.35: ``opencode auth {list,login,logout}`` only.  Credentials live in
  ``<data>/opencode/auth.json`` keyed by provider id -- one credential per
  provider, so there is nothing to switch.  No ``--format json``.
* Newer releases add ``auth switch <provider> <account>`` and store credentials
  in a SQLite database.  **That database is never read or written here**: all
  mutations go through the official ``opencode auth`` commands.

Capabilities are probed from ``--help`` at runtime instead of being inferred
from version numbers, so a release that adds (or drops) a command is handled
without a code change.  The JSON shape of ``auth list --format json`` could not
be verified against the installed version; parsing is therefore defensive and
documented as unverified.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from claude_swap.vendors.core import (
    AccountRecord,
    AccountView,
    Capability,
    CapabilityStatus,
    Support,
    SwitchOutcome,
    ToolAdapter,
    ToolInfo,
    UnsupportedCapability,
    UsageKind,
    UsageObservation,
    UsageResult,
    UsageState,
    VendorError,
    cap,
    help_text,
    redact,
    short_detail,
)

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
# One argv entry each, never a flag (first char alphanumeric); spaces are fine in a label.
_SELECTOR = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._@+:/ -]{0,126}[A-Za-z0-9._@+:/-])?$")


def valid_selector(value: str) -> str:
    """Provider/account selectors become argv entries; keep them inert."""
    if not isinstance(value, str) or not _SELECTOR.match(value):
        raise VendorError("Invalid provider or account selector")
    return value


def _data_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME")
    if base and os.path.isabs(base):
        return Path(base) / "opencode"
    return Path.home() / ".local" / "share" / "opencode"


def _config_file() -> Path:
    explicit = os.environ.get("OPENCODE_CONFIG")
    if explicit:
        return Path(explicit)
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base) if base and os.path.isabs(base) else Path.home() / ".config"
    return root / "opencode" / "opencode.json"


_PERIODS = ("5h", "24h", "7d", "30d", "all")
_TOKEN_FIELDS = (
    ("input", "$.tokens.input"),
    ("output", "$.tokens.output"),
    ("reasoning", "$.tokens.reasoning"),
    ("cacheRead", "$.tokens.cache.read"),
    ("cacheWrite", "$.tokens.cache.write"),
)


def _cutoffs(now: float) -> dict[str, int]:
    day = 86400 * 1000
    ms = int(now * 1000)
    return {"h5": ms - 5 * 3600 * 1000, "d1": ms - day, "d7": ms - 7 * day, "d30": ms - 30 * day}


def _sums() -> str:
    parts = []
    for period, cond in (
        ("5h", "time_created > {h5}"),
        ("24h", "time_created > {d1}"),
        ("7d", "time_created > {d7}"),
        ("30d", "time_created > {d30}"),
        ("all", "1"),
    ):
        parts.append(f'sum(case when {cond} then 1 else 0 end) as "n_{period}"')
        parts.append(
            f"sum(case when {cond} then coalesce(json_extract(data,'$.cost'),0) else 0 end) "
            f'as "cost_{period}"'
        )
        for name, path in _TOKEN_FIELDS:
            parts.append(
                f"sum(case when {cond} then coalesce(json_extract(data,'{path}'),0) else 0 end) "
                f'as "{name}_{period}"'
            )
    return ", ".join(parts)


# The newest assistant message per provider, only when it failed: tells whether a provider is
# currently refusing requests (e.g. OpenCode Go's "Workspace access denied").  Constant,
# read-only, message table only; it selects the error text, never message content.
_LAST_ERROR_SQL = (
    "select provider, message, status, at from (select json_extract(data,'$.providerID') as provider, "
    "json_extract(data,'$.error.data.message') as message, "
    "json_extract(data,'$.error.data.statusCode') as status, time_created as at, "
    "row_number() over (partition by json_extract(data,'$.providerID') order by time_created desc) as rn "
    "from message where json_extract(data,'$.role')='assistant') where rn = 1 and message is not null"
)

# OpenCode Go meters usage per model in dollar-equivalents (token price x tokens, the
# same prices OpenCode records as each message's cost), with rolling limits of 20% (5 h),
# 50% (week) and 100% (month) of each model's monthly limit: https://opencode.ai/docs/go/
GO_PROVIDER = "opencode-go"
GO_RULE = "Go limits each model separately: 5-hour ≤ 20%, weekly ≤ 50% of its monthly limit"


def _per_account_unavailable() -> UsageResult:
    return UsageResult(
        UsageState.UNAVAILABLE,
        "OpenCode records usage per provider and model, not per account",
        hint="The OpenCode tool view shows the per-provider statistics",
    )


def _record_matches(record: AccountRecord, row: Mapping[str, Any]) -> bool:
    """Does this vendor-listed (provider, account) row identify this record?"""
    claimed = {record.identity.get("account"), record.identity.get("id")} - {None, ""}
    return bool(claimed & {row.get("account"), row.get("id")})


def go_usage(providers: list[dict]) -> dict | None:
    """Per-model OpenCode Go usage in Go's own windows (dollar-equivalent, not credits)."""
    bucket = next((p for p in providers if p["provider"] == GO_PROVIDER), None)
    if bucket is None:
        return None
    models = [
        {
            "model": m["model"],
            "fiveHourUsd": m["5h"]["costUsd"],
            "weekUsd": m["7d"]["costUsd"],
            "thirtyDayUsd": m["30d"]["costUsd"],
            "messages30d": m["30d"]["messages"],
        }
        for m in bucket["models"]
        if m["30d"]["messages"] or m["7d"]["messages"]
    ]
    models.sort(key=lambda m: (-m["weekUsd"], -m["thirtyDayUsd"]))
    return {"models": models, "rule": GO_RULE, "unit": "usd-equivalent"}


# A constant, read-only aggregate over the message table only.  It never touches the
# credential/account tables, selects no message content, and takes no user input.
# Built at import time from module constants; only integer cutoffs are formatted in later.
_STATS_SQL = (
    "select json_extract(data,'$.providerID') as provider, json_extract(data,'$.modelID') as model, "  # nosec B608
    "max(time_created) as last, "
    + _sums()
    + " from message where json_extract(data,'$.role')='assistant' group by provider, model"
)


def summarize(rows: list) -> list[dict]:
    """Rows from ``_STATS_SQL`` -> [{provider, totals{period}, models[...]}] sorted by use."""
    providers: dict[str, dict] = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("provider"), str):
            continue
        model = row.get("model") if isinstance(row.get("model"), str) else "unknown"
        entry = {
            "model": model,
            "last": row.get("last") if isinstance(row.get("last"), (int, float)) else None,
        }
        for period in _PERIODS:

            def n(name: str, row: dict = row, period: str = period) -> float:
                value = row.get(f"{name}_{period}")
                return float(value) if isinstance(value, (int, float)) else 0.0

            entry[period] = {
                "messages": int(n("n")),
                "costUsd": round(n("cost"), 4),
                "tokens": {name: int(n(name)) for name, _ in _TOKEN_FIELDS},
            }
        bucket = providers.setdefault(
            row["provider"], {"provider": row["provider"], "models": [], "last": None}
        )
        bucket["models"].append(entry)
        if entry["last"] and (bucket["last"] is None or entry["last"] > bucket["last"]):
            bucket["last"] = entry["last"]
    out = []
    for bucket in providers.values():
        totals = {}
        for period in _PERIODS:
            totals[period] = {
                "messages": sum(m[period]["messages"] for m in bucket["models"]),
                "costUsd": round(sum(m[period]["costUsd"] for m in bucket["models"]), 4),
                "tokens": {
                    name: sum(m[period]["tokens"][name] for m in bucket["models"])
                    for name, _ in _TOKEN_FIELDS
                },
            }
        bucket["totals"] = totals
        bucket["models"].sort(key=lambda m: (-m["all"]["costUsd"], -m["all"]["messages"]))
        out.append(bucket)
    out.sort(key=lambda b: (-b["totals"]["30d"]["messages"], -b["totals"]["all"]["messages"]))
    return out


def _filtered_provider_stats(
    tool_result: UsageResult, provider: str, alias: str
) -> UsageResult:
    """The tool-level local statistics with every other provider removed."""
    for obs in tool_result.observations:
        data = obs.data if isinstance(obs.data, dict) else {}
        providers = data.get("providers")
        if not isinstance(providers, list):
            continue
        kept = [p for p in providers if isinstance(p, dict) and p.get("provider") == provider]
        if not kept:
            return UsageResult(
                UsageState.UNAVAILABLE,
                f"OpenCode has no recorded usage for provider '{provider}' yet",
                source=tool_result.source,
                attributed_to=alias,
            )
        filtered = dict(data)
        filtered["providers"] = kept
        if isinstance(data.get("lastErrors"), dict):
            filtered["lastErrors"] = {
                k: v for k, v in data["lastErrors"].items() if k == provider
            }
        go = go_usage(kept)
        if go is not None:
            filtered["go"] = go
        else:
            filtered.pop("go", None)
        return dataclasses.replace(
            tool_result,
            observations=(dataclasses.replace(obs, data=filtered),),
            attributed_to=alias,
        )
    return _per_account_unavailable()


class OpenCodeAdapter(ToolAdapter):
    key = "opencode"
    label = "OpenCode"
    binary = "opencode"
    docs = "https://opencode.ai/docs/cli/providers"
    kinds = ("provider-account",)
    # The official install script (opencode.ai/install) uses ~/.opencode/bin.
    install_dirs = ("~/.opencode/bin", "~/.local/bin", "/opt/homebrew/bin", "/usr/local/bin")

    def __init__(self, runner=None) -> None:
        super().__init__(runner)
        self._probe: dict[str, bool] | None = None
        self._db: bool | None = None
        self._probed_at = 0.0

    # ---- probing -----------------------------------------------------

    def probe(self) -> dict[str, bool]:
        """Which official account commands this install actually offers."""
        if self._probe is not None and time.monotonic() - self._probed_at < 300:
            return self._probe
        info = self.detect()
        flags = {
            "switch": False,
            "json": False,
            "provider-flag": False,
            "installed": info.installed,
        }
        if info.installed:
            group = self.runner.run([info.exe, "auth", "--help"], timeout=15)
            if group.ok:
                flags["switch"] = bool(
                    re.search(
                        r"^\s*opencode (?:auth|providers) switch\b", help_text(group), re.MULTILINE
                    )
                )
            listing = self.runner.run([info.exe, "auth", "list", "--help"], timeout=15)
            flags["json"] = listing.ok and "--format" in help_text(listing)
            login = self.runner.run([info.exe, "auth", "login", "--help"], timeout=15)
            flags["provider-flag"] = login.ok and "--provider" in help_text(login)
        self._probe = flags
        self._probed_at = time.monotonic()
        return flags

    def capabilities(self, info: ToolInfo) -> dict[Capability, CapabilityStatus]:
        S = Support
        flags = self.probe() if info.installed else {}
        switch = flags.get("switch")
        listing = (
            cap(S.SUPPORTED, "`opencode auth list --format json`")
            if flags.get("json")
            else cap(
                S.CONDITIONAL,
                "this version has no JSON listing; providers are read (names/types only) "
                "from the legacy auth.json, one credential per provider",
            )
        )
        return {
            Capability.DETECT: cap(S.SUPPORTED, "PATH lookup for `opencode`"),
            Capability.VERSION: cap(S.SUPPORTED, "`opencode --version`"),
            Capability.AUTH_STATE: listing,
            Capability.ACCOUNT_ENUMERATION: listing,
            Capability.REGISTRATION: cap(
                S.SUPPORTED, "alias for a provider/account OpenCode already lists"
            ),
            Capability.SWITCH: cap(
                S.SUPPORTED, "`opencode auth switch <provider> <account>`, verified by re-listing"
            )
            if switch
            else cap(
                S.UNSUPPORTED,
                "this OpenCode has no `auth switch` (added in newer releases); "
                "use `opencode auth login` or the TUI /connect",
            ),
            Capability.USAGE: cap(
                S.CONDITIONAL,
                "local statistics per provider and model (tokens, OpenCode's USD cost estimate; "
                "24h/7d/30d/all) via a read-only `opencode db` aggregate, or `opencode stats` "
                "text on older releases. Provider quotas and per-account usage: not reported",
            ),
            Capability.RATE_LIMIT_DETECTION: cap(
                S.UNSUPPORTED, "provider-specific; not exposed by OpenCode"
            ),
            Capability.AUTO_SWITCH: cap(S.UNSUPPORTED, "no verified per-account quota to compare"),
            Capability.SESSION_ISOLATION: cap(
                S.UNSUPPORTED,
                "credentials are resolved by the shared OpenCode server, not per process",
            ),
            Capability.LOGOUT: cap(
                S.CONDITIONAL, "`opencode auth logout <provider> [account]` (explicit only)"
            ),
        }

    # ---- enumeration -------------------------------------------------

    def discover(self) -> list[dict[str, Any]]:
        """Vendor-reported accounts: provider, account, id, type, active."""
        info = self.detect()
        if not info.installed:
            return []
        flags = self.probe()
        if flags["json"]:
            result = self.runner.run([info.exe, "auth", "list", "--format", "json"], timeout=20)
            if not result.ok:
                raise VendorError(f"`opencode auth list` failed: {short_detail(result)}")
            return self._parse_json_listing(result.stdout)
        return self._legacy_auth_json()

    @staticmethod
    def _parse_json_listing(text: str) -> list[dict[str, Any]]:
        try:
            doc = json.loads(text)
        except ValueError:
            raise VendorError("`opencode auth list` returned malformed JSON") from None
        rows = doc.get("accounts") if isinstance(doc, dict) and "accounts" in doc else doc
        if isinstance(rows, dict):  # {provider: [accounts]} or {provider: account}
            flat = []
            for provider, value in rows.items():
                for item in value if isinstance(value, list) else [value]:
                    if isinstance(item, dict):
                        flat.append({"provider": provider, **item})
            rows = flat
        if not isinstance(rows, list):
            raise VendorError("`opencode auth list` returned an unexpected shape")
        out: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            provider = row.get("provider") or row.get("integration") or row.get("providerID")
            ident = row.get("id") or row.get("credentialID") or row.get("credentialId")
            label = row.get("label") or row.get("name") or row.get("account") or ident
            if not isinstance(provider, str) or not isinstance(label, str):
                continue
            out.append(
                {
                    "provider": provider,
                    "account": label,
                    "id": str(ident) if ident is not None else None,
                    "type": str(row.get("type") or row.get("method") or "unknown"),
                    "active": bool(row["active"]) if "active" in row else None,
                }
            )
        return out

    @staticmethod
    def _legacy_auth_json() -> list[dict[str, Any]]:
        """Read-only: provider ids and credential *types*; values are never read out."""
        path = _data_dir() / "auth.json"
        try:
            if path.is_symlink() or not path.is_file():
                return []
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise VendorError("OpenCode auth.json is unreadable") from None
        if not isinstance(doc, dict):
            return []
        return [
            {
                "provider": provider,
                "account": "default",
                "id": None,
                "type": str(entry.get("type", "unknown")) if isinstance(entry, dict) else "unknown",
                "active": True,  # exactly one credential per provider in this layout
            }
            for provider, entry in sorted(doc.items())
            if _SELECTOR.match(str(provider))
        ]

    def provider_config(self) -> dict:
        """Configured providers/model, read-only and separate from login state."""
        path = _config_file()
        try:
            doc = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        except (OSError, ValueError):
            return {"readable": False}
        if not isinstance(doc, dict):
            return {"readable": False}
        providers = doc.get("provider")
        return {
            "readable": True,
            "model": doc.get("model") if isinstance(doc.get("model"), str) else None,
            "configuredProviders": sorted(providers) if isinstance(providers, dict) else [],
        }

    def auth_summary(self) -> dict:
        flags = self.probe() if self.detect().installed else {}
        return {
            "accountSwitching": bool(flags.get("switch")),
            "jsonListing": bool(flags.get("json")),
            "providerConfig": self.provider_config(),
        }

    def observe(
        self, records: Sequence[AccountRecord], last_active: str | None = None
    ) -> list[AccountView]:
        found = self.discover()

        def key(provider: str | None, ident: Mapping[str, Any]) -> tuple[str | None, Any]:
            return provider, ident.get("id") or ident.get("account")

        by_key = {key(r.provider, r.identity): r for r in records}
        views: list[AccountView] = []
        seen = set()
        for row in found:
            row_key = key(row["provider"], {"id": row["id"], "account": row["account"]})
            rec = by_key.get(row_key)
            seen.add(row_key)
            views.append(
                AccountView(
                    tool=self.key,
                    alias=rec.alias if rec else f"{row['provider']}/{row['account']}",
                    kind=row["type"],
                    provider=row["provider"],
                    identity={
                        "account": row["account"],
                        **({"id": row["id"]} if row["id"] else {}),
                    },
                    registered=rec is not None,
                    active=row["active"],
                )
            )
        for rec in records:
            if key(rec.provider, rec.identity) not in seen:
                views.append(
                    AccountView(
                        tool=self.key,
                        alias=rec.alias,
                        kind=rec.kind,
                        provider=rec.provider,
                        identity=rec.identity,
                        active=None,
                        note="no longer listed by OpenCode",
                    )
                )
        return views

    # ---- registration ------------------------------------------------

    def register(
        self,
        alias: str,
        *,
        provider: str | None = None,
        account: str | None = None,
        context: Sequence[AccountRecord] = (),
        **_: Any,
    ):
        if not provider:
            raise VendorError("A provider is required (see `ccswap opencode accounts`)")
        valid_selector(provider)
        # Local records are keyed by provider and model only, so two accounts of one
        # provider would show each other's usage. Fail fast: one account per provider.
        # (Pass the already-registered records as ``context``, like ``switch`` does;
        # ``service.add`` should wire ``context=self.store.records(tool)``.)
        for other in context:
            if other.provider == provider and other.alias != alias:
                raise VendorError(
                    f"Provider '{provider}' is already registered as '{other.alias}'; "
                    "OpenCode reports usage per provider and model, not per account, "
                    "so keep one account per provider"
                )
        rows = [r for r in self.discover() if r["provider"] == provider]
        if account is None:
            if len(rows) != 1:
                raise VendorError(
                    f"{len(rows)} accounts found for '{provider}'; pass --account to choose one"
                )
            row = rows[0]
        else:
            matches = [r for r in rows if account in (r["account"], r["id"])]
            if not matches:
                raise VendorError(
                    f"OpenCode lists no account '{account}' for provider '{provider}'"
                )
            if len(matches) > 1:
                raise VendorError(
                    f"Several '{provider}' accounts are called '{account}'; pass the account id"
                )
            row = matches[0]
        ident = {"account": row["account"]}
        if row["id"]:
            ident["id"] = row["id"]
        return AccountRecord(
            self.key, alias, "provider-account", provider, ident, created_at=time.time()
        ), None

    # ---- switching ---------------------------------------------------

    def switch_account(self, provider: str, account: str) -> SwitchOutcome:
        valid_selector(provider)
        valid_selector(account)
        info = self.require_installed()
        if not self.probe()["switch"]:
            raise UnsupportedCapability(
                f"OpenCode {info.version or 'this version'} has no `auth switch`; upgrade OpenCode, or run "
                f"`opencode auth login {provider}` / use /connect in the TUI"
            )
        before = self.discover()
        rows = [r for r in before if r["provider"] == provider]
        matches = [r for r in rows if account in (r["account"], r["id"])]
        if not matches:
            raise VendorError(f"OpenCode lists no account '{account}' for provider '{provider}'")
        if len(matches) > 1:
            raise VendorError(
                f"Several '{provider}' accounts are called '{account}'; use the account id"
            )
        target = matches[0]
        previous = next((r for r in rows if r["active"]), None)
        selector = target["id"] or target["account"]
        result = self.runner.run([info.exe, "auth", "switch", provider, selector], timeout=30)
        if not result.ok:
            raise VendorError(f"`opencode auth switch` failed: {short_detail(result)}")
        restart = "Running OpenCode servers/sessions keep the old account until restarted"
        try:
            after = [r for r in self.discover() if r["provider"] == provider]
        except VendorError:
            return SwitchOutcome(
                False,
                True,
                f"`opencode auth switch` succeeded for '{target['account']}' but the result could "
                f"not be re-read. {restart}",
                ("verification failed: `opencode auth list` did not answer",),
            )
        if all(r["active"] is None for r in after):
            return SwitchOutcome(
                False,
                True,
                f"`opencode auth switch` succeeded for '{target['account']}'; this OpenCode's "
                f"listing does not say which account is active, so it cannot be verified. {restart}",
            )
        landed = next((r for r in after if r["active"]), None)
        if not landed or (landed["account"], landed["id"]) != (target["account"], target["id"]):
            restored = False
            if previous:  # put it back through the same official command
                undo = self.runner.run(
                    [info.exe, "auth", "switch", provider, previous["id"] or previous["account"]],
                    timeout=30,
                )
                restored = undo.ok
            tail = (
                f"ccswap switched back to '{previous['account']}'"
                if previous and restored
                else "ccswap could not restore the previous account; check `opencode auth list`"
            )
            raise VendorError(
                f"OpenCode accepted the switch but still reports a different active account for "
                f"'{provider}'; {tail}"
            )
        return SwitchOutcome(
            True,
            True,
            f"Active {provider} account is now '{target['account']}' (verified with "
            f"`opencode auth list`). {restart}",
        )

    def switch(
        self,
        record: AccountRecord,
        secret: str | None,
        *,
        assume_yes: bool,
        interactive: bool,
        context: Sequence[AccountRecord] = (),
    ) -> SwitchOutcome:
        return self.switch_account(
            record.provider or "", record.identity.get("id") or record.identity.get("account", "")
        )

    # ---- login / logout ---------------------------------------------

    def login(self, provider: str | None) -> int:
        info = self.require_installed()
        argv = [info.exe, "auth", "login"]
        if provider:
            valid_selector(provider)
            argv += ["--provider", provider] if self.probe()["provider-flag"] else [provider]
        return self.runner.run_interactive(argv)

    def logout(self, provider: str, account: str | None) -> int:
        info = self.require_installed()
        argv = [info.exe, "auth", "logout", valid_selector(provider)]
        # Releases with `auth switch` take an account; 1.18.x documents `logout [provider]` only.
        if account and self.probe()["switch"]:
            argv.append(valid_selector(account))
        return self.runner.run_interactive(argv)

    # ---- usage -------------------------------------------------------

    def usage(
        self,
        record: AccountRecord | None,
        secret: str | None,
        *,
        context: Sequence[AccountRecord] = (),
    ) -> UsageResult:
        """Local token and cost statistics per provider and model.

        OpenCode exposes no provider quota (subscription windows, credits, rate limits),
        so nothing here is authoritative.  Its message records carry provider and model
        but no account, so one provider's statistics are attributed to a
        provider *account* only when that account is the provider's sole account:
        the single vendor-listed account matching this record, with no sibling
        ccswap registration for the provider (``context``, like ``switch``).
        Otherwise the account keeps today's UNAVAILABLE: two accounts of one
        provider would otherwise show each other's usage.
        """
        if record is not None and record.provider:
            return self._account_usage(record, context)
        if record is not None:
            return _per_account_unavailable()
        info = self.require_installed()
        if not self._db_supported(info.exe):
            stats = self.local_stats()
            return dataclasses.replace(
                stats, hint="Provider quotas are not reported by OpenCode. " + stats.hint
            )
        now = time.time()
        result = self.runner.run(
            [info.exe, "db", _STATS_SQL.format(**_cutoffs(now)), "--format", "json"],
            timeout=45,
        )
        if not result.ok:
            return UsageResult(UsageState.ERROR, f"`opencode db` failed: {short_detail(result)}")
        try:
            rows = json.loads(result.stdout or "[]")
        except ValueError:
            return UsageResult(UsageState.ERROR, "`opencode db` returned malformed JSON")
        if not isinstance(rows, list):
            return UsageResult(UsageState.ERROR, "`opencode db` returned an unexpected shape")
        providers = summarize(rows)
        if not providers:
            return UsageResult(
                UsageState.UNAVAILABLE,
                "OpenCode has no recorded model usage yet",
                source="opencode db (local message records)",
            )
        last_errors = self._last_errors(info.exe)
        data: dict = {"periods": list(_PERIODS), "providers": providers, "lastErrors": last_errors}
        go = go_usage(providers)
        if go is not None:
            data["go"] = go
        return UsageResult(
            UsageState.AVAILABLE,
            observations=(
                UsageObservation(
                    UsageKind.LOCAL_TOKENS,
                    "opencode db (local message records)",
                    False,
                    scope="local",
                    data=data,
                    fetched_at=now,
                ),
            ),
            source="opencode db (local message records)",
            authoritative=False,
            observed_at=now,
            hint="Local records. OpenCode Go counts dollar-equivalent usage per model (not "
            "credits); its limits and plan are only shown in the OpenCode console",
        )

    def _account_usage(
        self, record: AccountRecord, context: Sequence[AccountRecord]
    ) -> UsageResult:
        """This account's share of the tool-level statistics, when it owns the provider."""
        provider = record.provider or ""
        if any(r.provider == provider and r.alias != record.alias for r in context):
            return _per_account_unavailable()
        try:
            rows = [r for r in self.discover() if r["provider"] == provider]
        except VendorError as exc:
            return UsageResult(UsageState.ERROR, str(exc))
        if len(rows) != 1 or not _record_matches(record, rows[0]):
            return _per_account_unavailable()
        tool_result = self.usage(None, None)
        if tool_result.state is not UsageState.AVAILABLE:
            return dataclasses.replace(tool_result, attributed_to=record.alias)
        return _filtered_provider_stats(tool_result, provider, record.alias)

    def _last_errors(self, exe: str) -> dict[str, dict]:
        """{provider: {message, status, at}} when a provider's newest request failed."""
        result = self.runner.run([exe, "db", _LAST_ERROR_SQL, "--format", "json"], timeout=45)
        if not result.ok:
            return {}
        try:
            rows = json.loads(result.stdout or "[]")
        except ValueError:
            return {}
        out: dict[str, dict] = {}
        for row in rows if isinstance(rows, list) else []:
            if isinstance(row, dict) and isinstance(row.get("provider"), str):
                out[row["provider"]] = {
                    "message": redact(str(row.get("message") or ""))[:160],
                    "status": row.get("status") if isinstance(row.get("status"), int) else None,
                    "at": at / 1000 if isinstance(at := row.get("at"), (int, float)) else None,
                }
        return out

    def _db_supported(self, exe: str) -> bool:
        if self._db is None:
            result = self.runner.run([exe, "db", "--help"], timeout=15)
            if result.timed_out or result.missing:
                return False  # transient: ask again next time
            text = help_text(result)
            self._db = result.ok and "--format" in text and "query" in text
        return self._db

    def local_stats(self) -> UsageResult:
        info = self.require_installed()
        result = self.runner.run([info.exe, "stats"], timeout=30)
        if not result.ok:
            return UsageResult(
                UsageState.UNAVAILABLE, f"`opencode stats` failed: {short_detail(result)}"
            )
        text = redact(_ANSI.sub("", result.stdout)).strip()
        lines = text.splitlines()[:60]
        return UsageResult(
            UsageState.AVAILABLE,
            observations=(
                UsageObservation(
                    UsageKind.LOCAL_TOKENS,
                    "opencode stats",
                    False,
                    scope="local",
                    data={"text": "\n".join(lines)},
                    fetched_at=time.time(),
                ),
            ),
            hint="Local statistics across all providers; not an authoritative quota",
        )
