"""Shared vocabulary for vendor integrations (Muse, OpenCode, Kiro, ...).

Six concerns are kept apart on purpose:

* **tool identity**    -- the CLI being managed (``ToolAdapter.key``)
* **account identity** -- a CCSwap-registered account/profile of one tool
  (:class:`AccountRecord`)
* **model provider**   -- Anthropic/OpenAI/Meta...; only OpenCode surfaces it
  (``AccountRecord.provider``)
* **authentication**   -- how the tool legitimately logs in / selects accounts
* **usage**            -- :class:`UsageResult`, with an explicit measurement kind
* **execution**        -- :class:`LaunchSpec`

Adapters implement only the capability protocols they genuinely support.  The
:class:`CapabilityStatus` table says, per capability, whether that is
``supported``, ``conditional``, ``unsupported`` or ``unverified`` and *which
vendor mechanism* backs it.  Nothing here fabricates data: unknown stays
unknown.
"""

from __future__ import annotations

import enum
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from claude_swap import spawn
from claude_swap.exceptions import ClaudeSwitchError

# --------------------------------------------------------------------------
# Errors.  Messages are fixed, secret-free strings.
# --------------------------------------------------------------------------


class VendorError(ClaudeSwitchError):
    """A fixed, secret-free vendor diagnostic (exit status 1)."""

    exit_code = 1


class UnsupportedCapability(VendorError):
    """The vendor product cannot do this (exit status 3)."""

    exit_code = 3


class AccountNotRegistered(VendorError):
    """No registered account matches a selector (as opposed to an ambiguous one)."""


class ToolMissing(VendorError):
    """The vendor binary is not installed."""


# --------------------------------------------------------------------------
# Capability descriptors
# --------------------------------------------------------------------------


class Capability(str, enum.Enum):
    DETECT = "detect"
    VERSION = "version"
    AUTH_STATE = "auth-state"
    ACCOUNT_ENUMERATION = "account-enumeration"
    REGISTRATION = "registration"
    SWITCH = "switch"
    USAGE = "usage"
    RATE_LIMIT_DETECTION = "rate-limit-detection"
    AUTO_SWITCH = "auto-switch"
    SESSION_ISOLATION = "session-isolation"
    LOGOUT = "logout"


class Support(str, enum.Enum):
    SUPPORTED = "supported"
    CONDITIONAL = "conditional"
    UNSUPPORTED = "unsupported"
    UNVERIFIED = "unverified"


@dataclass(frozen=True)
class CapabilityStatus:
    support: Support
    mechanism: str = ""  # the vendor mechanism relied on (or why it is absent)

    def to_json(self) -> dict:
        return {"support": self.support.value, "mechanism": self.mechanism}


def cap(support: Support, mechanism: str = "") -> CapabilityStatus:
    return CapabilityStatus(support, mechanism)


# --------------------------------------------------------------------------
# Domain data
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolInfo:
    installed: bool
    path: str | None = None
    version: str | None = None

    @property
    def exe(self) -> str:
        """The executable path; only valid once ``installed`` was checked."""
        if not self.path:
            raise ToolMissing("tool is not installed")
        return self.path

    def to_json(self) -> dict:
        return {"installed": self.installed, "path": self.path, "version": self.version}


@dataclass(frozen=True)
class AccountRecord:
    """Persisted, secret-free metadata for one registered account."""

    tool: str
    alias: str
    kind: str  # api-key | browser | login | provider-account
    provider: str | None = None  # OpenCode model provider (anthropic, openai, ...)
    identity: Mapping[str, str] = field(default_factory=dict)  # non-secret ids
    secret_ref: str | None = None  # key into SecretStore; never the secret
    login_hint: Mapping[str, str] = field(default_factory=dict)  # e.g. kiro login flags
    created_at: float = 0.0

    def to_json(self) -> dict:
        return {
            "alias": self.alias,
            "kind": self.kind,
            "provider": self.provider,
            "identity": dict(self.identity),
            "hasSecret": self.secret_ref is not None,
            "createdAt": self.created_at,
        }


@dataclass(frozen=True)
class AccountView:
    """What ``list`` shows: a record or a vendor-discovered account + live state."""

    tool: str
    alias: str
    kind: str
    provider: str | None = None
    identity: Mapping[str, str] = field(default_factory=dict)
    registered: bool = True
    active: bool | None = None  # None == the vendor does not tell us
    note: str = ""

    def to_json(self) -> dict:
        return {
            "alias": self.alias,
            "kind": self.kind,
            "provider": self.provider,
            "identity": dict(self.identity),
            "registered": self.registered,
            "active": self.active,
            "note": self.note,
        }


class UsageKind(str, enum.Enum):
    SUBSCRIPTION_ALLOWANCE = "subscription-allowance"
    RATE_LIMIT_WINDOW = "rate-limit-window"
    CREDITS = "credits"
    LOCAL_TOKENS = "local-tokens"
    LOCAL_COST_ESTIMATE = "local-cost-estimate"


class UsageState(str, enum.Enum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"  # supported, but the vendor returned nothing now
    UNSUPPORTED = "unsupported"  # no stable programmatic interface exists
    UNAUTHENTICATED = "unauthenticated"  # the tool reports no usable login
    STALE = "stale"  # last good observation, kept because a refresh failed or is impossible now
    ERROR = "error"  # the source exists but failed, and there is no earlier observation


@dataclass(frozen=True)
class UsageWindow:
    """One independently reported limit.  Never merged with another window.

    Fields the vendor did not report stay ``None``; ``used_percent`` is only set when the
    vendor reported it or both ``used`` and ``limit`` are known in the same unit.
    """

    key: str  # stable id: "five-hour", "weekly", "monthly-credits", "bonus-credits", ...
    label: str  # human label as the vendor names it
    unit: str  # "percent" | "credits" | "tokens" | "usd" | ...
    used: float | None = None
    limit: float | None = None
    remaining: float | None = None
    used_percent: float | None = None
    resets_at: float | None = None  # epoch seconds, UTC
    scope: str = "account"  # account | organization | active-credential | local
    note: str = ""
    resets_on: str | None = None  # ISO date when the vendor reports a day but no time

    def to_json(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "unit": self.unit,
            "used": self.used,
            "limit": self.limit,
            "remaining": self.remaining,
            "usedPercent": self.used_percent,
            "resetsAt": self.resets_at,
            "scope": self.scope,
            "note": self.note,
            "resetsOn": self.resets_on,
        }

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> UsageWindow:
        def num(name: str) -> float | None:
            value = raw.get(name)
            return (
                float(value)
                if isinstance(value, (int, float)) and not isinstance(value, bool)
                else None
            )

        return cls(
            key=str(raw.get("key", "")),
            label=str(raw.get("label", "")),
            unit=str(raw.get("unit", "")),
            used=num("used"),
            limit=num("limit"),
            remaining=num("remaining"),
            used_percent=num("usedPercent"),
            resets_at=num("resetsAt"),
            scope=str(raw.get("scope", "account")),
            note=str(raw.get("note", "")),
            resets_on=raw.get("resetsOn") if isinstance(raw.get("resetsOn"), str) else None,
        )


def percent(used: float | None, limit: float | None) -> float | None:
    """Used share, only when both are known and the limit is a real positive denominator."""
    if used is None or limit is None or limit <= 0:
        return None
    return round(used / limit * 100.0, 1)


@dataclass(frozen=True)
class UsageObservation:
    kind: UsageKind
    source: str  # the exact vendor mechanism
    authoritative: bool  # vendor-reported (True) vs locally derived (False)
    scope: str = "account"  # account | team | model | local
    data: Mapping[str, Any] = field(default_factory=dict)  # vendor payload, unmodified scalars
    fetched_at: float | None = None
    cached: bool = False

    def to_json(self) -> dict:
        return {
            "kind": self.kind.value,
            "source": self.source,
            "authoritative": self.authoritative,
            "scope": self.scope,
            "data": dict(self.data),
            "fetchedAt": self.fetched_at,
            "cached": self.cached,
        }

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> UsageObservation:
        return cls(
            kind=UsageKind(raw.get("kind", UsageKind.LOCAL_TOKENS.value)),
            source=str(raw.get("source", "")),
            authoritative=bool(raw.get("authoritative")),
            scope=str(raw.get("scope", "account")),
            data=data if isinstance(data := raw.get("data"), dict) else {},
            fetched_at=raw.get("fetchedAt")
            if isinstance(raw.get("fetchedAt"), (int, float))
            else None,
            cached=bool(raw.get("cached")),
        )


@dataclass(frozen=True)
class UsageResult:
    """One usage answer for one tool/account.  ``windows`` are authoritative limits;
    ``observations`` carry everything else (local statistics, raw vendor payloads)."""

    state: UsageState
    reason: str = ""
    observations: tuple[UsageObservation, ...] = ()
    hint: str = ""
    windows: tuple[UsageWindow, ...] = ()
    plan: str | None = None
    source: str = ""  # the vendor mechanism the windows came from
    authoritative: bool = False
    attributed_to: str | None = None  # alias / identity the numbers belong to, when known
    observed_at: float | None = None  # when the vendor measured (or ccswap fetched) it
    error: str | None = None  # last refresh failure, kept beside stale data
    # Non-secret identity the tool reported *for this reading* (e.g. Kiro's whoami).  Used
    # only to file the reading under the matching registered account; never serialized.
    identity: Mapping[str, str] = field(default_factory=dict, compare=False, repr=False)

    @property
    def has_data(self) -> bool:
        return bool(self.windows or self.observations)

    def to_json(self) -> dict:
        return {
            "state": self.state.value,
            "reason": self.reason,
            "hint": self.hint,
            "plan": self.plan,
            "source": self.source,
            "authoritative": self.authoritative,
            "attributedTo": self.attributed_to,
            "observedAt": self.observed_at,
            "error": self.error,
            "windows": [w.to_json() for w in self.windows],
            "observations": [o.to_json() for o in self.observations],
        }

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> UsageResult:
        observed = raw.get("observedAt")
        return cls(
            state=UsageState(raw.get("state", UsageState.UNAVAILABLE.value)),
            reason=str(raw.get("reason") or ""),
            hint=str(raw.get("hint") or ""),
            plan=raw.get("plan") if isinstance(raw.get("plan"), str) else None,
            source=str(raw.get("source") or ""),
            authoritative=bool(raw.get("authoritative")),
            attributed_to=raw.get("attributedTo")
            if isinstance(raw.get("attributedTo"), str)
            else None,
            observed_at=float(observed) if isinstance(observed, (int, float)) else None,
            error=raw.get("error") if isinstance(raw.get("error"), str) else None,
            windows=tuple(
                UsageWindow.from_json(w) for w in raw.get("windows", []) if isinstance(w, dict)
            ),
            observations=tuple(
                UsageObservation.from_json(o)
                for o in raw.get("observations", [])
                if isinstance(o, dict)
            ),
        )


@dataclass(frozen=True)
class LaunchSpec:
    """How to start the tool as one account without touching global state."""

    argv: tuple[str, ...]
    env_overlay: Mapping[str, str] = field(default_factory=dict)  # may hold secrets
    unset_env: tuple[str, ...] = ()


@dataclass(frozen=True)
class SwitchOutcome:
    verified: bool  # did we observe the new state through a vendor interface?
    restart_required: bool
    message: str
    warnings: tuple[str, ...] = ()
    # False when nothing ccswap can name became "the active account" (e.g. a browser login).
    activates: bool = True

    def to_json(self) -> dict:
        return {
            "verified": self.verified,
            "restartRequired": self.restart_required,
            "message": self.message,
            "warnings": list(self.warnings),
        }


# --------------------------------------------------------------------------
# Process boundary
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RunResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""
    missing: bool = False
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.missing and not self.timed_out


@runtime_checkable
class Runner(Protocol):
    def run(
        self,
        argv: Sequence[str],
        *,
        timeout: float = 20.0,
        env: Mapping[str, str] | None = None,
        unset_env: Sequence[str] = (),
        input_text: str | None = None,
        cwd: str | None = None,
    ) -> RunResult: ...

    def run_interactive(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        unset_env: Sequence[str] = (),
    ) -> int: ...

    def which(self, name: str) -> str | None: ...


def _child_env(
    overlay: Mapping[str, str] | None, unset: Sequence[str], *, plain: bool = False
) -> dict[str, str]:
    env = dict(os.environ)
    for name in unset:
        env.pop(name, None)
    env.update(overlay or {})
    if plain:  # captured output is parsed: ask for no colour codes
        env.setdefault("NO_COLOR", "1")
    return env


class SubprocessRunner:
    """The real process boundary: argv lists only, never a shell."""

    def which(self, name: str) -> str | None:
        return shutil.which(name)

    def run(
        self,
        argv: Sequence[str],
        *,
        timeout: float = 20.0,
        env: Mapping[str, str] | None = None,
        unset_env: Sequence[str] = (),
        input_text: str | None = None,
        cwd: str | None = None,
    ) -> RunResult:
        try:
            proc = spawn.run(
                list(argv),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                input=input_text,
                env=_child_env(env, unset_env, plain=True),
                stdin=None if input_text is not None else subprocess.DEVNULL,
                cwd=cwd,
            )
        except FileNotFoundError:
            return RunResult(127, missing=True)
        except subprocess.TimeoutExpired:
            return RunResult(124, timed_out=True)
        except OSError:
            return RunResult(126, stderr="cannot execute")
        return RunResult(proc.returncode, proc.stdout or "", proc.stderr or "")

    def run_interactive(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        unset_env: Sequence[str] = (),
    ) -> int:
        """Run with the terminal inherited; propagate the exit status.

        SIGINT reaches the child through the shared foreground process group;
        a SIGTERM aimed at us is forwarded so the child is never orphaned.
        """
        child_env = _child_env(env, unset_env)
        try:
            child = subprocess.Popen(list(argv), env=child_env)  # nosec B603 - argv list, no shell
        except FileNotFoundError:
            return 127
        except OSError:
            return 126
        previous = None
        if sys.platform != "win32":
            previous = signal.signal(signal.SIGTERM, lambda _s, _f: child.terminate())
        try:
            while True:
                try:
                    return _exit_code(child.wait())
                except KeyboardInterrupt:
                    continue  # the child got the same SIGINT; keep waiting for it
        finally:
            if previous is not None:
                signal.signal(signal.SIGTERM, previous)


def _exit_code(code: int) -> int:
    return 128 - code if code < 0 else code  # POSIX: killed by signal N -> 128+N


# --------------------------------------------------------------------------
# Redaction
# --------------------------------------------------------------------------

_TOKENISH = re.compile(r"(?i)(?:ksk_|sk-|sk_|ghp_|gho_|xox[abp]-|bearer\s+|eyJ)[A-Za-z0-9._\-]{8,}")


def redact(text: str, secrets: Sequence[str] = ()) -> str:
    """Remove known secrets and token-shaped strings from vendor output."""
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[redacted]")
    return _TOKENISH.sub("[redacted]", text)


def help_text(result: RunResult) -> str:
    """CLI help, wherever the tool prints it (OpenCode/yargs writes it to stderr)."""
    return f"{result.stdout}\n{result.stderr}"


def short_detail(result: RunResult, secrets: Sequence[str] = ()) -> str:
    """One redacted, bounded line describing why a vendor call failed."""
    if result.missing:
        return "binary not found"
    if result.timed_out:
        return "timed out"
    first = (result.stderr or result.stdout or "").strip().splitlines()
    # Redact the whole line first: truncating first could cut a secret in half and keep a prefix.
    return redact(first[0], secrets)[:200] if first else f"exit status {result.returncode}"


# --------------------------------------------------------------------------
# Capability protocols and the adapter base
# --------------------------------------------------------------------------


class ToolAdapter:
    """Identity + detection + the capability table.  Everything else is opt-in."""

    key: str = ""
    label: str = ""
    binary: str = ""
    docs: str = ""
    # Which accounts-kinds ``add`` can register, for help text and the TUI.
    kinds: tuple[str, ...] = ()
    # Where the vendor's own installer puts the binary when it is not on PATH (launchd
    # services such as the menu bar get a minimal PATH).  ``~`` is expanded.
    install_dirs: tuple[str, ...] = ("~/.local/bin", "/opt/homebrew/bin", "/usr/local/bin")

    DETECT_TTL = 5.0  # a status pass asks several times; the answer cannot change that fast

    def __init__(self, runner: Runner | None = None) -> None:
        self.runner: Runner = runner or SubprocessRunner()
        self._detected: tuple[float, ToolInfo] | None = None

    def detect(self) -> ToolInfo:
        now = time.monotonic()
        if self._detected and now - self._detected[0] < self.DETECT_TTL:
            return self._detected[1]
        path = self.runner.which(self.binary) or self._installed_elsewhere()
        info = ToolInfo(True, path, self._version(path)) if path else ToolInfo(False)
        self._detected = (now, info)
        return info

    def _installed_elsewhere(self) -> str | None:
        """The binary in its installer's directory, for the real process runner only."""
        if not isinstance(self.runner, SubprocessRunner):
            return None  # injected runners (tests) define the whole world
        for directory in self.install_dirs:
            candidate = os.path.join(os.path.expanduser(directory), self.binary)
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
        return None

    def _version(self, path: str) -> str | None:
        result = self.runner.run([path, "--version"], timeout=10)
        return parse_version(result.stdout) if result.ok else None

    def capabilities(self, info: ToolInfo) -> dict[Capability, CapabilityStatus]:
        raise NotImplementedError

    # Optional capability defaults: adapters override what the vendor really offers.
    def auth_summary(self) -> dict:
        return {}

    def observe(
        self, records: Sequence[AccountRecord], last_active: str | None = None
    ) -> list[AccountView]:
        return [
            AccountView(self.key, r.alias, r.kind, r.provider, r.identity, active=None)
            for r in records
        ]

    def require_installed(self) -> ToolInfo:
        info = self.detect()
        if not info.installed:
            raise ToolMissing(f"{self.label} is not installed ({self.binary} not found on PATH)")
        return info


@runtime_checkable
class RegistrationCapability(Protocol):
    def register(self, alias: str, **options: Any) -> tuple[AccountRecord, str | None]:
        """Return the record to persist and the secret (if any) for SecretStore."""
        ...


@runtime_checkable
class ObservationCapability(Protocol):
    def observe(
        self, records: Sequence[AccountRecord], last_active: str | None = None
    ) -> list[AccountView]:
        """Merge registered records with live vendor state."""
        ...

    def auth_summary(self) -> dict: ...


@runtime_checkable
class SwitchingCapability(Protocol):
    def switch(
        self,
        record: AccountRecord,
        secret: str | None,
        *,
        assume_yes: bool,
        interactive: bool,
        context: Sequence[AccountRecord] = (),
    ) -> SwitchOutcome: ...


@runtime_checkable
class UsageCapability(Protocol):
    def usage(self, record: AccountRecord | None, secret: str | None) -> UsageResult: ...


@runtime_checkable
class SessionCapability(Protocol):
    def launch_spec(
        self, record: AccountRecord, secret: str | None, args: Sequence[str]
    ) -> LaunchSpec: ...


def parse_version(text: str) -> str | None:
    match = re.search(r"\d+\.\d+\.\d+(?:[-+.][0-9A-Za-z.\-+]+)?", text or "")
    return match.group(0) if match else None


def version_tuple(version: str | None) -> tuple[int, ...]:
    if not version:
        return ()
    return tuple(int(p) for p in re.findall(r"\d+", version.split("-")[0].split("+")[0])[:3])
