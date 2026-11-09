"""Kiro CLI (``kiro-cli``) adapter.

Verified against kiro-cli 3.1.0 (``kiro-cli --help``, ``login --help``,
``whoami -f json``, ``chat --help``) and the Kiro headless-mode documentation.
This is the **CLI**; Kiro IDE shares neither the binary nor any verified
account-selection interface and is out of scope.

Facts this adapter relies on:

* ``kiro-cli whoami -f json`` prints one JSON object (``accountType``,
  ``email``, ``startUrl``, ``region``) optionally followed by plain-text
  profile lines, so the first JSON value is decoded and the rest ignored.
* ``kiro-cli login`` is interactive (browser or device flow).  Flags select
  the method: ``--social google|github``, ``--license free`` (Builder ID),
  ``--license pro --identity-provider URL --region R`` (Identity Center).
* ``kiro-cli logout`` ends the *whole* user login.  It is therefore only run
  as part of an explicit, confirmed login switch whose current account is
  registered (so the user can get back), never implicitly.
* ``KIRO_API_KEY`` skips the login flow for that process (headless).  It is
  process-scoped, so API-key accounts are *run* as an account, not switched.
* There is no verified machine-readable usage interface (``/usage`` is an
  interactive slash command) and ccswap does not scrape terminals.
* Agents, steering, MCP servers, skills and sessions are never touched.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Mapping, Sequence
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
    UsageResult,
    UsageState,
    VendorError,
    cap,
)

ENV_KEY = "KIRO_API_KEY"
_REGION = re.compile(r"^[a-z]{2}(?:-[a-z]+)+-\d$")
_URL = re.compile(r"^https://[A-Za-z0-9.-]+(?::\d+)?(?:/[A-Za-z0-9._~/%-]*)?$")
SOCIALS = ("google", "github")


def parse_whoami(text: str) -> dict[str, str] | None:
    """First JSON object in ``whoami -f json`` output; trailing text ignored."""
    try:
        doc, _ = json.JSONDecoder().raw_decode((text or "").lstrip())
    except ValueError:
        return None
    if not isinstance(doc, dict):
        return None
    return {k: str(v) for k, v in doc.items() if isinstance(v, (str, int)) and v != ""}


def _same_login(identity: Mapping[str, str], current: Mapping[str, str]) -> bool:
    return bool(
        identity.get("email")
        and identity.get("email") == current.get("email")
        and identity.get("startUrl", "") == current.get("startUrl", "")
    )


def _hint_args(hint: Mapping[str, str]) -> list[str]:
    """Rebuild validated ``kiro-cli login`` flags from stored hints."""
    args: list[str] = []
    if hint.get("social") in SOCIALS:
        args += ["--social", hint["social"]]
    elif hint.get("license") == "free":
        args += ["--license", "free"]
    elif hint.get("license") == "pro" and _URL.match(hint.get("identityProvider", "")):
        args += ["--license", "pro", "--identity-provider", hint["identityProvider"]]
        if _REGION.match(hint.get("region", "")):
            args += ["--region", hint["region"]]
    if hint.get("deviceFlow") == "true":
        args.append("--use-device-flow")
    return args


class KiroAdapter(ToolAdapter):
    key = "kiro"
    label = "Kiro CLI"
    binary = "kiro-cli"
    docs = "https://kiro.dev/docs/cli/"
    kinds = ("login", "api-key")

    def capabilities(self, info: ToolInfo) -> dict[Capability, CapabilityStatus]:
        S = Support
        return {
            Capability.DETECT: cap(
                S.SUPPORTED, "PATH lookup for `kiro-cli` (not the IDE launcher `kiro`)"
            ),
            Capability.VERSION: cap(S.SUPPORTED, "`kiro-cli --version`"),
            Capability.AUTH_STATE: cap(
                S.SUPPORTED, "`kiro-cli whoami -f json` (+ KIRO_API_KEY presence)"
            ),
            Capability.ACCOUNT_ENUMERATION: cap(
                S.CONDITIONAL, "only the single active login is visible (`whoami`); no account list"
            ),
            Capability.REGISTRATION: cap(
                S.SUPPORTED,
                "login: identity metadata from `whoami` (no tokens copied); api-key: stdin/prompt "
                "into CCSwap's 0600 store",
            ),
            Capability.SWITCH: cap(
                S.CONDITIONAL,
                "login accounts: confirmed `kiro-cli logout` + `kiro-cli login <flags>` "
                "(interactive), verified with `whoami`. API-key accounts: not switchable "
                "(process-scoped) -- use `ccswap kiro run`",
            ),
            Capability.USAGE: cap(
                S.UNSUPPORTED,
                "`/usage` is interactive-only; no documented machine-readable interface",
            ),
            Capability.RATE_LIMIT_DETECTION: cap(S.UNSUPPORTED, "no stable kiro-cli interface"),
            Capability.AUTO_SWITCH: cap(
                S.UNSUPPORTED, "no programmatic usage and no non-interactive login switch"
            ),
            Capability.SESSION_ISOLATION: cap(
                S.CONDITIONAL,
                "API-key accounts: child gets KIRO_API_KEY (headless). Login accounts share the "
                "one machine-wide login and cannot run side by side",
            ),
            Capability.LOGOUT: cap(
                S.CONDITIONAL, "`kiro-cli logout`, only inside an explicit login switch"
            ),
        }

    # ---- state -------------------------------------------------------

    def whoami(self) -> dict[str, str] | None:
        info = self.detect()
        if not info.installed:
            return None
        result = self.runner.run([info.exe, "whoami", "-f", "json"], timeout=20)
        return parse_whoami(result.stdout) if result.ok else None

    def auth_summary(self) -> dict:
        who = self.whoami()
        return {
            "loggedIn": who is not None,
            "accountType": who.get("accountType") if who else None,
            "email": who.get("email") if who else None,
            "startUrl": who.get("startUrl") if who else None,
            "region": who.get("region") if who else None,
            "envKeyPresent": bool(os.environ.get(ENV_KEY)),
            "methods": ["google", "github", "builder-id", "identity-center", "api-key"],
            "switchRestartNote": "running kiro-cli chat sessions keep the old login until restarted",
        }

    def observe(
        self, records: Sequence[AccountRecord], last_active: str | None = None
    ) -> list[AccountView]:
        who = self.whoami()
        env = bool(os.environ.get(ENV_KEY))
        views: list[AccountView] = []
        matched = False
        for rec in records:
            if rec.kind == "login":
                is_active = bool(who and _same_login(rec.identity, who))
                matched = matched or is_active
                note = (
                    "KIRO_API_KEY in this environment overrides the login"
                    if env and is_active
                    else ""
                )
                views.append(
                    AccountView(
                        self.key,
                        rec.alias,
                        rec.kind,
                        identity=rec.identity,
                        active=is_active,
                        note=note,
                    )
                )
            else:
                views.append(
                    AccountView(
                        self.key,
                        rec.alias,
                        rec.kind,
                        identity=rec.identity,
                        active=None,
                        note="process-scoped; use `ccswap kiro run`",
                    )
                )
        if who and not matched:
            views.append(
                AccountView(
                    self.key,
                    f"(current {who.get('email', 'login')})",
                    "login",
                    identity={
                        k: v
                        for k, v in who.items()
                        if k in ("accountType", "email", "startUrl", "region")
                    },
                    registered=False,
                    active=True,
                    note="not registered; run `ccswap kiro add`",
                )
            )
        return views

    # ---- registration -------------------------------------------------

    def register(
        self,
        alias: str,
        *,
        kind: str = "login",
        api_key: str | None = None,
        social: str | None = None,
        license: str | None = None,
        identity_provider: str | None = None,
        region: str | None = None,
        device_flow: bool = False,
        **_: Any,
    ):
        if kind == "api-key":
            key = (api_key or "").strip()
            if len(key) < 8 or any(c.isspace() for c in key):
                raise VendorError("A valid Kiro API key is required")
            return AccountRecord(self.key, alias, "api-key", created_at=time.time()), key
        if kind != "login":
            raise VendorError("Kiro account kind must be 'login' or 'api-key'")
        who = self.whoami()
        if not who or not who.get("email"):
            raise VendorError(
                "Not logged in to Kiro CLI; run `kiro-cli login` first, then `ccswap kiro add`"
            )
        hint: dict[str, str] = {}
        if social:
            if social not in SOCIALS:
                raise VendorError("--social must be google or github")
            hint["social"] = social
        elif license == "free":
            hint["license"] = "free"
        elif (
            license == "pro"
            or identity_provider
            or "identitycenter" in who.get("accountType", "").lower()
        ):
            idp = identity_provider or who.get("startUrl", "")
            reg = region or who.get("region", "")
            if not _URL.match(idp) or (reg and not _REGION.match(reg)):
                raise VendorError("Invalid Identity Center URL or region")
            hint.update(
                {"license": "pro", "identityProvider": idp, **({"region": reg} if reg else {})}
            )
        if device_flow:
            hint["deviceFlow"] = "true"
        ident = {
            k: v for k, v in who.items() if k in ("accountType", "email", "startUrl", "region")
        }
        return AccountRecord(
            self.key, alias, "login", identity=ident, login_hint=hint, created_at=time.time()
        ), None

    # ---- switching ----------------------------------------------------

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
        if record.kind == "api-key":
            raise UnsupportedCapability(
                f"KIRO_API_KEY is process-scoped, so '{record.alias}' cannot be made the machine-wide "
                f"login. Use `ccswap kiro run {record.alias}` for an isolated session"
            )
        warnings = []
        if os.environ.get(ENV_KEY):
            warnings.append("KIRO_API_KEY is set in this environment and overrides the login")
        current = self.whoami()
        if current and _same_login(record.identity, current):
            return SwitchOutcome(
                True, False, f"'{record.alias}' is already the active Kiro login", tuple(warnings)
            )
        if not interactive:
            raise UnsupportedCapability(
                "Kiro logins can only be changed interactively. Re-run with --login to run "
                "`kiro-cli logout` and `kiro-cli login` here"
            )
        if current and not any(
            r.kind == "login" and _same_login(r.identity, current) for r in context
        ):
            raise VendorError(
                "The current Kiro login is not registered with ccswap, so it could not be "
                "restored after logout. Run `ccswap kiro add <alias>` for it first"
            )
        if current and not assume_yes:
            raise VendorError(
                "This ends the current Kiro login (`kiro-cli logout`). Re-run with --yes to confirm"
            )
        if self.runner.run_interactive([info.exe, "logout"]) != 0:
            raise VendorError("`kiro-cli logout` failed; the login was left as it was")
        code = self.runner.run_interactive([info.exe, "login", *_hint_args(record.login_hint)])
        after = self.whoami()
        if code != 0 or not after:
            raise VendorError(
                "`kiro-cli login` did not complete; you are logged out. "
                f"Recover with: kiro-cli login {' '.join(_hint_args(record.login_hint))}".strip()
            )
        if not _same_login(record.identity, after):
            raise VendorError(
                f"Logged in as {after.get('email', 'a different account')}, not the expected "
                f"'{record.alias}'. The Kiro login is now that account"
            )
        return SwitchOutcome(
            True,
            True,
            f"Kiro login is now '{record.alias}' (verified with `kiro-cli whoami`). Running chat "
            "sessions keep the old login until restarted",
            tuple(warnings),
        )

    # ---- usage ---------------------------------------------------------

    def usage(self, record: AccountRecord | None, secret: str | None) -> UsageResult:
        return UsageResult(
            UsageState.UNSUPPORTED,
            "Kiro usage/credits are only available through the interactive /usage command",
            hint="Run `kiro-cli chat`, then type /usage",
        )

    # ---- sessions -------------------------------------------------------

    def launch_spec(
        self, record: AccountRecord, secret: str | None, args: Sequence[str]
    ) -> LaunchSpec:
        info = self.require_installed()
        if record.kind == "api-key":
            if secret is None:
                raise VendorError(f"Account '{record.alias}' has no stored API key")
            return LaunchSpec((info.exe, "chat", *args), env_overlay={ENV_KEY: secret})
        current = self.whoami()
        if not (current and _same_login(record.identity, current)):
            raise VendorError(
                f"'{record.alias}' is not the active Kiro login and logins cannot run side by side; "
                f"run `ccswap kiro switch {record.alias} --login` first"
            )
        return LaunchSpec((info.exe, "chat", *args), unset_env=(ENV_KEY,))
