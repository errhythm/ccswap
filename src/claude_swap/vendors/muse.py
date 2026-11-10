"""Muse Code (``muse``) adapter.

Verified against Muse Code 1.4.14 (``muse --help``, ``muse auth set --help``,
``muse usage --help``) and https://www.musemuse.dev/docs/muse-code/auth.

Facts this adapter relies on:

* Credential precedence is ``META_API_KEY`` > stored API key > stored browser
  session.  An exported ``META_API_KEY`` therefore *hides* every stored login.
* ``muse auth set --api-key-stdin`` stores a key (stdin only, never argv);
  ``muse login`` is the browser/device flow; ``muse logout`` clears the stored
  credential and does not touch ``META_API_KEY``.
* There is **no** ``whoami``.  A browser login is the email plus a token in the
  login file.  A file that only says ``storage: keychain`` is a pointer: writing
  it changes the email label and does not move the keychain item.
* ``muse usage [--json]`` prints the subscription allowance without a model
  turn.  Its non-null payload schema is not documented, so it is passed
  through (scalars only) and never interpreted into percentages.
"""

from __future__ import annotations

import dataclasses
import json
import os
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from claude_swap.fsutil import atomic_write_text
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


def _meta(text: str) -> dict | None:
    try:
        meta = (json.loads(text).get("providers") or {}).get("meta")
    except (ValueError, AttributeError):
        return None
    return meta if isinstance(meta, dict) else None


def _login_email(text: str) -> str | None:
    """``providers.meta.user_email`` of a Muse login file's contents (nothing else is read)."""
    meta = _meta(text)
    email = meta.get("user_email") if meta else None
    return email if isinstance(email, str) and "@" in email and len(email) < 255 else None


def _login_restorable(text: str) -> bool:
    """True when writing this file back can restore the session.

    Muse's normal login file names the account and sets ``storage`` to ``keychain``.
    The token then lives in the macOS Keychain, which ccswap cannot copy.  Writing that
    pointer changes ``user_email`` and leaves the keychain item where it was.
    """
    meta = _meta(text)
    if meta is None:
        return False
    for field in ("access_token", "refresh_token"):
        value = meta.get(field)
        if isinstance(value, str) and value.strip() and not any(c.isspace() for c in value):
            return True
    return False


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
                S.SUPPORTED,
                "Browser logins: ccswap keeps a login only when the file holds a token, swaps "
                "Muse's auth.json, and accepts the switch when `muse usage` does not say "
                "not logged in. A keychain pointer is refused. API-key accounts are for "
                "`ccswap muse run` and are not menu switches",
            ),
            Capability.USAGE: cap(
                S.SUPPORTED,
                "`muse usage --json` (no model turn): tier, 5-hour and weekly used % with reset "
                "times. Per account for API-key accounts (META_API_KEY child env); browser "
                "logins: the active credential only. An API-key sign-in reports no allowance",
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

    def live_email(self) -> str | None:
        """The signed-in Meta account's email, from Muse's own ``auth.json``.

        ``muse`` has no whoami.  Its login file records ``providers.meta.user_email`` (the
        field muse-swap identifies accounts by, verified on 1.4.14).  Only that field is
        read; tokens are never read out, copied or logged.  ``None`` when unknown.
        """
        path = _config_dir() / "auth.json"
        try:
            if path.is_symlink() or not path.is_file():
                return None
            return _login_email(path.read_text(encoding="utf-8"))
        except OSError:
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
        """Browser logins are matched by the email Muse recorded for its login.

        API-key accounts stay unknown (Muse cannot say which key it stored), and so does
        everything while an exported ``META_API_KEY`` overrides the stored login.
        """
        env = bool(os.environ.get(ENV_KEY))
        live = None if env else self.live_email()
        views = []
        for rec in records:
            notes = []
            active: bool | None = None
            if rec.kind == "browser" and live and rec.identity.get("email"):
                active = rec.identity["email"].lower() == live.lower()
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
                    active=active,
                    note="; ".join(notes),
                )
            )
        return views

    @staticmethod
    def owner_of(record: AccountRecord, identity: Mapping[str, str]) -> bool:
        email = identity.get("email", "").lower()
        return (
            record.kind == "browser"
            and bool(email)
            and record.identity.get("email", "").lower() == email
        )

    # ---- registration ----------------------------------------------------

    def register(
        self,
        alias: str,
        *,
        kind: str = "api-key",
        api_key: str | None = None,
        from_file: str | None = None,
        **_: Any,
    ):
        if kind == "api-key":
            key = (api_key or "").strip()
            if len(key) < _SECRET_MIN or any(c.isspace() for c in key):
                raise VendorError("A valid Meta API key is required")
            return AccountRecord(self.key, alias, "api-key", created_at=time.time()), key
        if kind == "browser":
            # ccswap keeps the login itself (0600 store) so it can switch to it later, the way
            # it keeps Claude Code logins.  Default: the live login; or a saved login file.
            text = self._read_login(
                Path(from_file).expanduser() if from_file else self._auth_path()
            )
            email = _login_email(text)
            if email is None:
                raise VendorError("That is not a Muse login file (no providers.meta.user_email)")
            if not _login_restorable(text):
                raise VendorError(
                    "That Muse login is only a keychain pointer, so ccswap cannot switch "
                    "back to it. Sign in with `muse login` and add it while the login file "
                    "still holds the token."
                )
            return AccountRecord(
                self.key, alias, "browser", identity={"email": email}, created_at=time.time()
            ), text
        raise VendorError("Muse account kind must be 'api-key' or 'browser'")

    # ---- login custody (browser accounts) ------------------------------

    @staticmethod
    def _auth_path() -> Path:
        return _config_dir() / "auth.json"

    @staticmethod
    def _read_login(path: Path) -> str:
        try:
            if path.is_symlink() or not path.is_file():
                raise OSError
            text = path.read_text(encoding="utf-8")
        except OSError:
            raise VendorError(f"Cannot read the Muse login file {path}") from None
        if _login_email(text) is None:
            raise VendorError(f"{path} is not a Muse login file")
        return text

    def capture_live(self) -> tuple[dict[str, str], str] | None:
        """The live login (identity, file contents) so the service can save it back to its
        account before a switch: Muse refreshes tokens in place, and a stale copy would be
        rejected later."""
        try:
            text = self._read_login(self._auth_path())
        except VendorError:
            return None
        email = _login_email(text)
        if not email or not _login_restorable(text):
            return None
        return ({"email": email}, text)

    def _live_text(self) -> str | None:
        try:
            return self._read_login(self._auth_path())
        except VendorError:
            return None

    def _usage_rejects_login(self, exe: str) -> bool:
        check = self.runner.run([exe, "usage", "--json"], timeout=20, unset_env=(ENV_KEY,))
        return "not logged in" in f"{check.stdout}\n{check.stderr}".lower()

    def _write_login(self, text: str) -> None:
        path = self._auth_path()
        if path.is_symlink():
            raise VendorError("Muse's login file is a symbolic link; refusing to replace it")
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        atomic_write_text(path, text, mode=0o600, follow_symlinks=False)

    def _switch_browser(
        self,
        exe: str,
        record: AccountRecord,
        secret: str | None,
        context: Sequence[AccountRecord],
        warnings: list[str],
    ) -> SwitchOutcome:
        target_email = record.identity.get("email", "")
        if secret is None or (_login_email(secret) or "").lower() != target_email.lower():
            raise VendorError(
                f"ccswap holds no login for '{record.alias}'. Sign in to it once with `muse login`, "
                f"then run `ccswap muse remove {record.alias}` and `ccswap muse add {record.alias} --browser`"
            )
        if not _login_restorable(secret):
            raise VendorError(
                f"ccswap's copy of '{record.alias}' is only a keychain pointer. Switching would "
                "change the email label and leave the keychain session where it is. Sign in with "
                f"`muse login`, then `ccswap muse remove {record.alias}` and "
                f"`ccswap muse add {record.alias} --browser` while the login file still holds the token."
            )
        live_text = self._live_text()
        live_email = (_login_email(live_text) or "") if live_text else ""
        if live_email.lower() == target_email.lower() and live_text:
            session_dead = not _login_restorable(live_text) and self._usage_rejects_login(exe)
            if not session_dead:
                return SwitchOutcome(
                    True,
                    False,
                    f"'{record.alias}' is already the signed-in Muse login",
                    tuple(warnings),
                )
        elif live_email and not any(
            r.kind == "browser" and r.identity.get("email", "").lower() == live_email.lower()
            for r in context
        ):
            raise VendorError(
                "The signed-in Muse login is not registered with ccswap, so replacing it would lose "
                "it. Register it first: ccswap muse add <alias> --browser"
            )
        previous = live_text
        self._write_login(secret)
        problem = None
        if (self.live_email() or "").lower() != target_email.lower():
            problem = "Muse's login file did not take the new login"
        elif self._usage_rejects_login(exe):
            problem = "Muse does not accept the stored login (it may have expired)"
        if problem:
            if previous is not None:
                self._write_login(previous)  # put the previous login back exactly
            raise VendorError(
                f"{problem}; the previous login was restored. Sign in to '{record.alias}' again "
                "with `muse login` and re-add it"
            )
        return SwitchOutcome(
            True,
            True,
            f"Muse login is now '{record.alias}' (muse usage accepted this login). Running Muse "
            "sessions keep the previous login until restarted",
            tuple(warnings),
        )

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
            return self._switch_browser(info.exe, record, secret, context, warnings)
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
        live = None if os.environ.get(ENV_KEY) else self.live_email()
        if record is not None and record.kind != "api-key":
            email = record.identity.get("email", "")
            if not email or not live or email.lower() != live.lower():
                return UsageResult(
                    UsageState.UNAVAILABLE,
                    f"'{record.alias}' is not the signed-in Muse login; Muse only reports the "
                    "signed-in login's allowance",
                    retain_last=False,
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
                else (live or "active credential")
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
                "api_key": "Muse does not report usage for an API-key sign-in. The Muse Code "
                "subscription key and pay-as-you-go keys look the same here; "
                "dev.meta.ai/api-keys says which one this is",
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
        parsed = parse_allowance(
            allowance, attributed_to=who, account_scoped=record is not None, secrets=secrets
        )
        if secret is None and live:
            # The reading is the signed-in login's only if that login did not change meanwhile.
            if self.live_email() != live:
                return UsageResult(
                    UsageState.UNAVAILABLE,
                    "The Muse login changed during the usage check; not recorded",
                )
            parsed = dataclasses.replace(parsed, identity={"email": live})
        return parsed

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
