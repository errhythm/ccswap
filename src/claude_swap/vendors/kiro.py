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

import dataclasses
import datetime
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
    UsageWindow,
    VendorError,
    cap,
    help_text,
    percent,
    short_detail,
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
    """Same login = same email, same Identity Center start URL and, when both say, same type.

    ``whoami`` does not name the social provider, so a Google and a GitHub login with one
    email look identical; ``VendorService.add`` therefore refuses to register two logins
    with the same identity instead of guessing between them.
    """
    email, other = identity.get("email", ""), current.get("email", "")
    if not email or email.lower() != other.lower():
        return False
    if identity.get("startUrl", "") != current.get("startUrl", ""):
        return False
    kinds = (identity.get("accountType", ""), current.get("accountType", ""))
    return not all(kinds) or kinds[0].lower() == kinds[1].lower()


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


_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07")
_NUM = r"([0-9][0-9,]*(?:\.[0-9]+)?)"


def _f(value: str) -> float:
    return float(value.replace(",", ""))


def _reset_date(text: str, today: datetime.date) -> str | None:
    iso = re.search(r"resets? on (\d{4}-\d{2}-\d{2})", text)
    if iso:
        return iso.group(1)
    short = re.search(r"resets? on (\d{1,2})/(\d{1,2})", text)  # legacy MM/DD, year implied
    if short:
        month, day = int(short.group(1)), int(short.group(2))
        try:
            candidate = datetime.date(today.year, month, day)
        except ValueError:
            return None
        if candidate < today:
            candidate = datetime.date(today.year + 1, month, day)
        return candidate.isoformat()
    return None


def parse_usage_report(text: str, today: datetime.date | None = None) -> UsageResult | None:
    """Parse the V2 ``/usage`` report (kiro-cli 3.1 and the older box layout).

    3.1.0, verified live::

        Estimated Usage | resets on 2026-12-01 | KIRO PRO+
        Credits (1086.43 of 2000 covered in plan), 54.3%
        Overages: Disabled
        Your plan is managed by your organization's administrator.

    The percentage is recomputed from used/limit (same unit) rather than trusted, and a
    report without a plan allowance is plan-only: no zero is invented.
    """
    today = today or datetime.date.today()
    clean = re.sub(r"[┃┏┓┗┛┣┫━│║█▓▒░]+", " ", _ANSI.sub("", text or ""))
    lines = [re.sub(r"\s+", " ", line).strip() for line in clean.splitlines()]
    body = "\n".join(line for line in lines if line)
    if not re.search(r"covered in plan|credits used|Estimated Usage|Plan:", body, re.IGNORECASE):
        return None
    plan = None
    head = re.search(r"Estimated Usage\s*\|[^|\n]*\|\s*([^\n|]+)", body)
    if head:
        plan = head.group(1).strip()
    else:
        named = re.search(r"Plan:\s*([^|\n]+)", body) or re.search(
            r"\|\s*(KIRO[^|\n]*)$", body, re.MULTILINE
        )
        plan = named.group(1).strip() if named else None
    org = bool(re.search(r"managed by your organi[sz]ation", body, re.IGNORECASE))
    scope = "organization-managed" if org else "account"
    windows = []
    plan_credits = re.search(_NUM + r" of " + _NUM + r" covered in plan", body)
    reset = _reset_date(body, today)
    if plan_credits:
        used, limit = _f(plan_credits.group(1)), _f(plan_credits.group(2))
        if limit > 0:
            windows.append(
                UsageWindow(
                    key="monthly-credits",
                    label="Plan credits",
                    unit="credits",
                    used=used,
                    limit=limit,
                    remaining=round(max(0.0, limit - used), 2),
                    used_percent=percent(used, limit),
                    resets_on=reset,
                    scope=scope,
                    note="Kiro labels this an estimate",
                )
            )
    bonus = re.search(
        r"Bonus credits:?\s*" + _NUM + r"\s*/\s*" + _NUM + r"[^\n]*", body, re.IGNORECASE
    )
    if bonus:
        used, limit = _f(bonus.group(1)), _f(bonus.group(2))
        expires = re.search(r"expires in (\d+) days?", bonus.group(0))
        windows.append(
            UsageWindow(
                key="bonus-credits",
                label="Bonus credits",
                unit="credits",
                used=used,
                limit=limit,
                remaining=round(max(0.0, limit - used), 2),
                used_percent=percent(used, limit),
                scope=scope,
                note=f"expires in {expires.group(1)} days" if expires else "",
            )
        )
    overage = re.search(r"Overages?:\s*([^\n]+)", body)
    notes = []
    if overage:
        notes.append(f"Overages: {overage.group(1).strip()}")
    if org:
        notes.append("Plan managed by your organization")
    return UsageResult(
        UsageState.AVAILABLE if windows else UsageState.UNAVAILABLE,
        reason="" if windows else "Kiro reported a plan without a credit allowance",
        windows=tuple(windows),
        plan=plan,
        source="kiro-cli chat --no-interactive --v2 /usage",
        authoritative=True,
        observed_at=time.time(),
        hint="; ".join(notes),
    )


class KiroAdapter(ToolAdapter):
    key = "kiro"
    label = "Kiro CLI"
    binary = "kiro-cli"
    docs = "https://kiro.dev/docs/cli/"
    kinds = ("login", "api-key")

    def __init__(self, runner=None, probe_dir=None) -> None:
        super().__init__(runner)
        self.probe_dir = probe_dir
        self._v2: bool | None = None

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
                S.UNSUPPORTED, "switching a login needs an interactive logout/login"
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

    def whoami_state(self) -> tuple[str, dict[str, str] | None]:
        """("in", identity) | ("out", None) | ("error", None).

        Only an explicit "not logged in" counts as logged out. A timeout, crash or
        unparseable output is "error", and destructive steps must not run on it.
        """
        info = self.detect()
        if not info.installed:
            return "error", None
        result = self.runner.run([info.exe, "whoami", "-f", "json"], timeout=20)
        if result.ok:
            who = parse_whoami(result.stdout)
            return ("in", who) if who else ("error", None)
        text = f"{result.stdout}\n{result.stderr}".lower()
        if not result.timed_out and not result.missing and "not logged in" in text:
            return "out", None
        return "error", None

    def whoami(self) -> dict[str, str] | None:
        return self.whoami_state()[1]

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
        state, who = self.whoami_state()
        env = bool(os.environ.get(ENV_KEY))
        views: list[AccountView] = []
        matched = False
        for rec in records:
            if rec.kind == "login":
                is_active: bool | None = (
                    None if state == "error" else bool(who and _same_login(rec.identity, who))
                )
                matched = matched or bool(is_active)
                note = "kiro-cli whoami failed; login state unknown" if state == "error" else ""
                if env and is_active:
                    note = "KIRO_API_KEY in this environment overrides the login"
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
        state, current = self.whoami_state()
        if state == "error":
            raise VendorError(
                "Cannot read the current Kiro login (`kiro-cli whoami` failed or timed out); "
                "nothing was changed"
            )
        if current and _same_login(record.identity, current):
            return SwitchOutcome(
                True, False, f"'{record.alias}' is already the active Kiro login", tuple(warnings)
            )
        if not interactive:
            raise UnsupportedCapability(
                "Kiro logins can only be changed interactively. Re-run with --login to run "
                "`kiro-cli logout` and `kiro-cli login` here"
            )
        login_args = _hint_args(record.login_hint)
        recovery = " ".join(["kiro-cli", "login", *login_args])
        if current:  # logged in as someone else: ending that login needs every safeguard
            if not any(r.kind == "login" and _same_login(r.identity, current) for r in context):
                raise VendorError(
                    "The current Kiro login is not registered with ccswap, so it could not be "
                    "restored after logout. Run `ccswap kiro add <alias>` for it first"
                )
            if not assume_yes:
                raise VendorError(
                    "This ends the current Kiro login (`kiro-cli logout`). Re-run with --yes "
                    "to confirm"
                )
            if self.runner.run_interactive([info.exe, "logout"]) != 0:
                raise VendorError("`kiro-cli logout` failed; the login was left as it was")
        try:
            code = self.runner.run_interactive([info.exe, "login", *login_args])
        except KeyboardInterrupt:
            raise VendorError(
                f"Interrupted; you may be logged out. Recover with: {recovery}"
            ) from None
        state, after = self.whoami_state()
        if code != 0 or state != "in" or not after:
            raise VendorError(
                f"`kiro-cli login` did not complete; you may be logged out. Recover with: {recovery}"
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

    def _probe_dir(self) -> str:
        """Fixed working directory for the probe: Kiro saves an empty V2 session per run,
        keyed by cwd, so this keeps them out of the user's project resume lists."""
        from claude_swap import paths

        root = self.probe_dir or (paths.get_backup_root() / "vendors" / "kiro-probe")
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        return str(root)

    def _supports_v2(self, exe: str) -> bool:
        if self._v2 is None:
            result = self.runner.run([exe, "chat", "--help"], timeout=15)
            self._v2 = result.ok and "--v2" in help_text(result)
        return self._v2

    def usage(self, record: AccountRecord | None, secret: str | None) -> UsageResult:
        """``kiro-cli chat --no-interactive --v2 /usage``: plan credits, bonus credits, reset.

        Verified on kiro-cli 3.1.0: the V2 harness answers ``/usage`` locally (no model
        turn; repeated runs leave the credit count unchanged) and exits 0.  Without
        ``--v2`` a V3 engine may forward the text to the model, so a CLI without the
        flag is never probed.  Kiro measures only the signed-in login (or the
        ``KIRO_API_KEY`` given to the child), so a registered login that is not active
        is reported unavailable; the service then shows its last observation as stale.
        """
        info = self.require_installed()
        if not self._supports_v2(info.exe):
            return UsageResult(
                UsageState.UNSUPPORTED,
                "This kiro-cli has no --v2 harness; /usage could be sent to the model as a "
                "prompt, so ccswap does not run it",
                hint="Run `kiro-cli chat` and type /usage",
            )
        env: dict[str, str] | None = None
        unset: tuple[str, ...] = (ENV_KEY,)
        if record is not None and record.kind == "api-key":
            if secret is None:
                return UsageResult(
                    UsageState.ERROR, f"Account '{record.alias}' has no stored API key"
                )
            env, unset, who = {ENV_KEY: secret}, (), record.alias
        else:
            state, current = self.whoami_state()
            if state == "out":
                return UsageResult(UsageState.UNAUTHENTICATED, "Kiro CLI is not logged in")
            if state == "error" or not current:
                return UsageResult(UsageState.ERROR, "`kiro-cli whoami` failed; usage not read")
            if record is not None and not _same_login(record.identity, current):
                return UsageResult(
                    UsageState.UNAVAILABLE,
                    f"'{record.alias}' is not the signed-in Kiro login; Kiro only reports the "
                    "active login's usage",
                )
            who = record.alias if record is not None else current.get("email", "active login")
        result = self.runner.run(
            [info.exe, "chat", "--no-interactive", "--v2", "/usage"],
            timeout=40,
            env=env,
            unset_env=unset,
            cwd=self._probe_dir(),
        )
        secrets = [secret] if secret else []
        text = _ANSI.sub("", f"{result.stdout}\n{result.stderr}")
        if not result.ok:
            if re.search(r"not logged in|log ?in required|please log ?in", text, re.IGNORECASE):
                return UsageResult(UsageState.UNAUTHENTICATED, "Kiro CLI is not logged in")
            return UsageResult(
                UsageState.ERROR, f"Kiro /usage failed: {short_detail(result, secrets)}"
            )
        parsed = parse_usage_report(text)
        if parsed is None:
            return UsageResult(UsageState.ERROR, "Kiro /usage printed an unrecognised report")
        return dataclasses.replace(parsed, attributed_to=who)

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
