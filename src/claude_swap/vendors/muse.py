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
from pathlib import Path
from typing import Any, Sequence

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


def _scalars(value: Any, depth: int = 0) -> Any:
    """Pass vendor JSON through, keeping only plain scalars/containers."""
    if depth > 4:
        return None
    if isinstance(value, dict):
        return {str(k): _scalars(v, depth + 1) for k, v in list(value.items())[:50]}
    if isinstance(value, list):
        return [_scalars(v, depth + 1) for v in value[:50]]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return redact(value) if isinstance(value, str) else value
    return None


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
                S.CONDITIONAL,
                "`muse usage --json` (subscription allowance, no model turn); "
                "empty for pay-as-you-go keys; payload schema unverified",
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

    def observe(self, records: Sequence[AccountRecord], last_active: str | None = None) -> list[AccountView]:
        env = bool(os.environ.get(ENV_KEY))
        views = []
        for rec in records:
            note = ""
            active: bool | None = None
            if rec.kind == "api-key" and last_active == rec.alias:
                active = True
                note = "last activated by ccswap (muse cannot confirm which key is stored)"
            if env:
                note = (note + "; " if note else "") + "META_API_KEY in this environment overrides stored login"
            views.append(
                AccountView(
                    tool=self.key, alias=rec.alias, kind=rec.kind, identity=rec.identity,
                    active=active, note=note,
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
        self, record: AccountRecord, secret: str | None, *, assume_yes: bool,
        interactive: bool, context: Sequence[AccountRecord] = (),
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
            code = self.runner.run_interactive([info.path, "login"])
            if code != 0:
                raise VendorError(f"`muse login` exited with status {code}")
            return SwitchOutcome(
                False, True,
                "Signed in with `muse login`. Muse exposes no identity, so ccswap "
                "cannot verify which account is now active",
                tuple(warnings),
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
            [info.path, "auth", "set", "--provider", "meta", "--api-key-stdin"],
            timeout=20, input_text=secret + "\n",
        )
        if not result.ok:
            raise VendorError(f"`muse auth set` failed: {short_detail(result, [secret])}")
        return SwitchOutcome(
            False, True,
            f"Stored the API key for '{record.alias}' via `muse auth set`. Running Muse "
            "sessions keep their old credential until restarted; Muse exposes no way "
            "for ccswap to confirm the stored key",
            tuple(warnings),
        )

    # ---- usage -----------------------------------------------------------

    def usage(self, record: AccountRecord | None, secret: str | None) -> UsageResult:
        info = self.require_installed()
        env = {ENV_KEY: secret} if secret else None
        result = self.runner.run([info.path, "usage", "--json"], timeout=20, env=env)
        secrets = [secret] if secret else []
        if not result.ok:
            return UsageResult(UsageState.UNAVAILABLE, f"muse usage failed: {short_detail(result, secrets)}")
        try:
            doc = json.loads(result.stdout)
        except ValueError:
            return UsageResult(UsageState.UNAVAILABLE, "muse usage returned malformed JSON")
        if not isinstance(doc, dict):
            return UsageResult(UsageState.UNAVAILABLE, "muse usage returned an unexpected payload")
        allowance = doc.get("allowance")
        if not isinstance(allowance, dict):
            return UsageResult(
                UsageState.UNAVAILABLE,
                redact(str(doc.get("reason") or "no allowance reported"))[:120],
                hint=redact(str(doc.get("hint") or ""), secrets)[:240],
            )
        return UsageResult(
            UsageState.AVAILABLE,
            observations=(
                UsageObservation(
                    UsageKind.SUBSCRIPTION_ALLOWANCE, "muse usage --json", True,
                    data=_scalars(allowance), fetched_at=time.time(),
                ),
            ),
            hint="Vendor payload shown as reported; ccswap does not derive percentages from it",
        )

    # ---- sessions --------------------------------------------------------

    def launch_spec(self, record: AccountRecord, secret: str | None, args: Sequence[str]) -> LaunchSpec:
        info = self.require_installed()
        if record.kind != "api-key" or secret is None:
            raise UnsupportedCapability(
                "Only API-key accounts can run in isolation; Muse has a single stored "
                "browser session. Use `muse` directly or `ccswap muse switch --login`"
            )
        return LaunchSpec((info.path, *args), env_overlay={ENV_KEY: secret})

    def logout(self) -> None:
        info = self.require_installed()
        result = self.runner.run([info.path, "logout"], timeout=20)
        if not result.ok:
            raise VendorError(f"`muse logout` failed: {short_detail(result)}")
