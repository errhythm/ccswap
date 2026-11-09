"""Muse Code (``muse``) adapter.

Verified against Muse Code 1.4.14 (``muse --help``, ``muse auth set --help``,
``muse usage --help``) and https://www.musemuse.dev/docs/muse-code/auth.

Facts this adapter relies on:

* Credential precedence is ``META_API_KEY`` > stored API key > stored browser
  session.  An exported ``META_API_KEY`` therefore *hides* every stored login.
* ``muse auth set --api-key-stdin`` stores a key (stdin only, never argv);
  ``muse login`` is the browser/device flow; ``muse logout`` clears the stored
  credential and does not touch ``META_API_KEY``.
* There is **no** ``whoami``/``status`` command and no account selection for
  browser sessions, so browser accounts cannot be identified or switched
  programmatically -- CCSwap registers them as local aliases only.
* ``muse usage [--json]`` prints the subscription allowance without a model
  turn.  Its non-null payload schema is not documented, so it is passed
  through (scalars only) and never interpreted into percentages.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from claude_swap.vendors.core import (
    AccountRecord,
    AccountView,
    Capability,
    CapabilityStatus,
    LaunchSpec,
    Support,
    SwitchOutcome,
    ToolAdapter,
    ToolInfo,
    UnsupportedCapability,
    UsageKind,
    UsageObservation,
    UsageResult,
    UsageState,
    UsageWindow,
    VendorError,
    cap,
    redact,
    short_detail,
)

ENV_KEY = "META_API_KEY"
_SECRET_MIN = 8


def _config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    if base and os.path.isabs(base):
        return Path(base) / "muse"
    return Path.home() / ".config" / "muse"


def _scalars(value: Any, depth: int = 0, secrets: Sequence[str] = ()) -> Any:
    """Pass vendor JSON through, keeping only plain scalars/containers."""
    if depth > 4:
        return None
    if isinstance(value, dict):
        return {str(k): _scalars(v, depth + 1, secrets) for k, v in list(value.items())[:50]}
    if isinstance(value, list):
        return [_scalars(v, depth + 1, secrets) for v in value[:50]]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return redact(value, secrets) if isinstance(value, str) else value
    return None


_WINDOWS = (("window", "five-hour", "5-hour window"), ("weekly", "weekly", "Weekly"))


def parse_allowance(
    allowance: dict,
    *,
    attributed_to: str | None,
    account_scoped: bool,
    secrets: Sequence[str] = (),
) -> UsageResult:
    """Map Muse 1.4's allowance (verified live) to independent windows.

    ``{"tier", "window": {"usedPercent", "resetsAtMs"}, "weekly": {...}, "asOfMs", "source"}``.
    Muse reports percentages only; no request/token denominator is invented.
    """
    scope = "account" if account_scoped else "active-credential"
    windows = []
    for field_name, key, label in _WINDOWS:
        raw = allowance.get(field_name)
        if not isinstance(raw, dict):
            continue
        pct = raw.get("usedPercent")
        reset = raw.get("resetsAtMs")
        pct_value = (
            float(pct) if isinstance(pct, (int, float)) and not isinstance(pct, bool) else None
        )
        windows.append(
            UsageWindow(
                key=key,
                label=label,
                unit="percent",
                used_percent=pct_value,
                remaining=None if pct_value is None else max(0.0, 100.0 - pct_value),
                resets_at=float(reset) / 1000
                if isinstance(reset, (int, float)) and reset > 0
                else None,
                scope=scope,
            )
        )
    known = {"tier", "window", "weekly", "asOfMs", "source"}
    extra = {k: v for k, v in allowance.items() if k not in known}
    as_of = allowance.get("asOfMs")
    observed = float(as_of) / 1000 if isinstance(as_of, (int, float)) and as_of > 0 else time.time()
    observations: tuple[UsageObservation, ...] = ()
    if extra:  # keep anything a newer Muse adds, unmodified, instead of dropping it
        observations = (
            UsageObservation(
                UsageKind.SUBSCRIPTION_ALLOWANCE,
                "muse usage --json",
                True,
                scope=scope,
                data=_scalars(extra, secrets=secrets),
                fetched_at=observed,
            ),
        )
    tier = allowance.get("tier")
    if not windows and not observations:
        return UsageResult(
            UsageState.UNAVAILABLE,
            "Muse returned an allowance without usage windows",
            source="muse usage --json",
            attributed_to=attributed_to,
        )
    return UsageResult(
        UsageState.AVAILABLE,
        windows=tuple(windows),
        observations=observations,
        plan=redact(tier, secrets)[:80] if isinstance(tier, str) else None,
        source="muse usage --json",
        authoritative=True,
        attributed_to=attributed_to,
        observed_at=observed,
    )


class MuseAdapter(ToolAdapter):
    key = "muse"
    label = "Muse Code"
    binary = "muse"
    docs = "https://www.musemuse.dev/docs/muse-code/auth"
    kinds = ("api-key", "browser")

    # ---- capability table --------------------------------------------

    def capabilities(self, info: ToolInfo) -> dict[Capability, CapabilityStatus]:
        S = Support
        return {
            Capability.DETECT: cap(S.SUPPORTED, "PATH lookup for `muse`"),
            Capability.VERSION: cap(S.SUPPORTED, "`muse --version`"),
            Capability.AUTH_STATE: cap(
                S.CONDITIONAL,
                "META_API_KEY presence + provider list in ~/.config/muse/auth.json "
                "(credential type and account identity are not exposed by muse)",
            ),
            Capability.ACCOUNT_ENUMERATION: cap(
                S.UNSUPPORTED, "muse has no whoami/accounts command; CCSwap-registered only"
            ),
            Capability.REGISTRATION: cap(
                S.SUPPORTED,
                "API key read from stdin/prompt into CCSwap's 0600 store; "
                "browser sessions register as an alias only (no token copied)",
            ),
            Capability.SWITCH: cap(
                S.CONDITIONAL,
                "API-key accounts: `muse auth set --api-key-stdin` (replaces the stored "
                "credential). Browser sessions: not switchable; `muse login` only",
            ),
            Capability.USAGE: cap(
                S.SUPPORTED,
                "`muse usage --json` (no model turn): tier, 5-hour and weekly used % with reset "
                "times. Per account for API-key accounts (META_API_KEY child env); browser "
                "logins: the active credential only. Pay-as-you-go keys report no allowance",
            ),
            Capability.RATE_LIMIT_DETECTION: cap(S.UNSUPPORTED, "no stable muse interface"),
            Capability.AUTO_SWITCH: cap(
                S.UNSUPPORTED,
                "no per-account quota that is comparable across accounts, and browser "
                "sessions cannot be switched",
            ),
            Capability.SESSION_ISOLATION: cap(
                S.CONDITIONAL,
                "API-key accounts: child process gets META_API_KEY (documented highest "
                "precedence); browser sessions: unsupported",
            ),
            Capability.LOGOUT: cap(S.CONDITIONAL, "`muse logout` (never run implicitly)"),
        }

    # ---- auth state ------------------------------------------------------

    def _stored_credential(self) -> bool | None:
        path = _config_dir() / "auth.json"
        try:
            if path.is_symlink() or not path.is_file():
                return False if not path.exists() else None
            doc = json.loads(path.read_text(encoding="utf-8"))
            providers = doc.get("providers") if isinstance(doc, dict) else None
            return "meta" in providers if isinstance(providers, (list, dict)) else None
        except (OSError, ValueError):
            return None

    def auth_summary(self) -> dict:
        return {
            "envKeyPresent": bool(os.environ.get(ENV_KEY)),
            "storedCredential": self._stored_credential(),
            "precedence": "META_API_KEY > stored API key > stored browser session",
            "methods": ["browser", "api-key"],
            "identityExposed": False,
        }

    def observe(
        self, records: Sequence[AccountRecord], last_active: str | None = None
    ) -> list[AccountView]:
        """``active`` is always unknown: muse cannot say which credential it will use."""
        env = bool(os.environ.get(ENV_KEY))
        views = []
        for rec in records:
            notes = []
            if rec.kind == "api-key" and last_active == rec.alias:
                notes.append("last switched to by ccswap; unconfirmed (muse cannot report its key)")
            if env:
                notes.append("META_API_KEY in this environment overrides any stored login")
            views.append(
                AccountView(
                    tool=self.key,
                    alias=rec.alias,
                    kind=rec.kind,
                    identity=rec.identity,
                    active=None,
                    note="; ".join(notes),
                )
            )
        return views

    # ---- registration ----------------------------------------------------

    def register(self, alias: str, *, kind: str = "api-key", api_key: str | None = None, **_: Any):
        if kind == "api-key":
            key = (api_key or "").strip()
            if len(key) < _SECRET_MIN or any(c.isspace() for c in key):
                raise VendorError("A valid Meta API key is required")
            return AccountRecord(self.key, alias, "api-key", created_at=time.time()), key
        if kind == "browser":
            return AccountRecord(self.key, alias, "browser", created_at=time.time()), None
        raise VendorError("Muse account kind must be 'api-key' or 'browser'")

    # ---- switching -------------------------------------------------------

    def switch(
        self,
        record: AccountRecord,
        secret: str | None,
        *,
        assume_yes: bool,
        interactive: bool,
        context: Sequence[AccountRecord] = (),
    ) -> SwitchOutcome:
        info = self.require_installed()
        warnings = []
        if os.environ.get(ENV_KEY):
            warnings.append(
                "META_API_KEY is set in this environment and always takes priority; "
                "unset it for the stored login to take effect"
            )
        if record.kind == "browser":
            if not interactive:
                raise UnsupportedCapability(
                    "Muse cannot switch between browser sessions. Run `muse login` "
                    "(or pass --login to ccswap muse switch) to sign in again"
                )
            code = self.runner.run_interactive([info.exe, "login"])
            if code != 0:
                raise VendorError(f"`muse login` exited with status {code}")
            return SwitchOutcome(
                False,
                True,
                "Signed in with `muse login`. Muse exposes no identity, so ccswap "
                "cannot verify which account is now active",
                tuple(warnings),
                activates=False,
            )
        if secret is None:
            raise VendorError(f"Account '{record.alias}' has no stored API key")
        if not assume_yes:
            raise VendorError(
                "Switching replaces the credential stored by Muse (including a browser "
                "session). Re-run with --yes to confirm, or use `ccswap muse run "
                f"{record.alias}` to launch without changing anything"
            )
        result = self.runner.run(
            [info.exe, "auth", "set", "--provider", "meta", "--api-key-stdin"],
            timeout=20,
            input_text=secret + "\n",
        )
        if not result.ok:
            raise VendorError(f"`muse auth set` failed: {short_detail(result, [secret])}")
        return SwitchOutcome(
            False,
            True,
            f"Stored the API key for '{record.alias}' via `muse auth set`. Running Muse "
            "sessions keep their old credential until restarted; Muse exposes no way "
            "for ccswap to confirm the stored key",
            tuple(warnings),
        )

    # ---- usage -----------------------------------------------------------

    def usage(self, record: AccountRecord | None, secret: str | None) -> UsageResult:
        """``muse usage --json``: subscription windows without a model turn.

        API-key accounts are measured in isolation (``META_API_KEY`` for that child
        only, the documented top precedence).  Browser aliases cannot be: Muse keeps
        one login (in the macOS Keychain, so not even ``HOME`` isolates it), so their
        usage is the *active credential's*, reported at tool level only.
        """
        info = self.require_installed()
        if record is not None and record.kind != "api-key":
            return UsageResult(
                UsageState.UNAVAILABLE,
                "Muse keeps one browser login and does not say whose it is, so usage cannot "
                "be attributed to this alias",
                hint="The tool-level usage shows the active credential",
            )
        # An API-key account's key overrides any exported META_API_KEY for this child.  The
        # tool-level reading deliberately keeps an exported key: that *is* the credential
        # Muse uses in this environment, and it is labelled so.
        env = {ENV_KEY: secret} if secret else None
        result = self.runner.run([info.exe, "usage", "--json"], timeout=20, env=env)
        secrets = [secret] if secret else []
        who = (
            record.alias
            if record is not None
            else (
                "META_API_KEY from the environment"
                if os.environ.get(ENV_KEY)
                else "active credential"
            )
        )
        text = f"{result.stdout}\n{result.stderr}".lower()
        if "not logged in" in text:
            return UsageResult(
                UsageState.UNAUTHENTICATED,
                "Muse has no login (run `muse login` or register an API key)",
                source="muse usage --json",
            )
        if not result.ok:
            # A failure or timeout is transient: the service may show the last reading as stale.
            return UsageResult(
                UsageState.ERROR, f"muse usage failed: {short_detail(result, secrets)}"
            )
        try:
            doc = json.loads(result.stdout)
        except ValueError:
            return UsageResult(UsageState.ERROR, "muse usage returned malformed JSON")
        if not isinstance(doc, dict):
            return UsageResult(UsageState.ERROR, "muse usage returned an unexpected payload")
        allowance = doc.get("allowance")
        if not isinstance(allowance, dict):
            reason = str(doc.get("reason") or "")
            message = {
                "api_key": "This is a pay-as-you-go API key; it has no subscription allowance",
                "server_sent_no_usage": "The connected account returned no allowance data "
                "(this happens before the first use in a new window)",
            }.get(reason, redact(reason or "no allowance reported", secrets)[:120])
            return UsageResult(
                UsageState.UNAVAILABLE,
                message,
                hint=redact(str(doc.get("hint") or ""), secrets)[:240],
                source="muse usage --json",
                attributed_to=who,
            )
        return parse_allowance(
            allowance, attributed_to=who, account_scoped=record is not None, secrets=secrets
        )

    # ---- sessions --------------------------------------------------------

    def launch_spec(
        self, record: AccountRecord, secret: str | None, args: Sequence[str]
    ) -> LaunchSpec:
        info = self.require_installed()
        if record.kind != "api-key" or secret is None:
            raise UnsupportedCapability(
                "Only API-key accounts can run in isolation; Muse has a single stored "
                "browser session. Use `muse` directly or `ccswap muse switch --login`"
            )
        return LaunchSpec((info.exe, *args), env_overlay={ENV_KEY: secret})

    def logout(self) -> None:
        info = self.require_installed()
        result = self.runner.run([info.exe, "logout"], timeout=20)
        if not result.ok:
            raise VendorError(f"`muse logout` failed: {short_detail(result)}")
