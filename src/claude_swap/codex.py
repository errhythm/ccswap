"""File-backed multi-account switching for the Codex CLI.

Codex keeps local authentication in ``$CODEX_HOME/auth.json`` when its
``cli_auth_credentials_store`` setting is ``file`` or when ``auto`` falls back
to the file store.  This module manages independent copies of that complete
authentication document while deliberately leaving every other Codex setting
and state file untouched.

Keyring-only Codex installations are rejected.  Codex can select different
keyring backends across releases and platforms, so writing a guessed keyring
entry would be less safe than requiring the documented file backend.
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import os
import sys
import tempfile
import time
import tomllib
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from claude_swap.codex_usage import (
    DEFAULT_CHATGPT_BASE_URL,
    CodexUsageError,
    consume_reset_credit,
    fetch_codex_usage,
    list_reset_credits,
    refresh_codex_auth,
)
from claude_swap.exceptions import (
    AccountNotFoundError, ClaudeSwitchError, ConfigError, LockError, SessionError,
    SwitchError, ValidationError,
)
from claude_swap.json_output import USAGE_API_KEY
from claude_swap.locking import FileLock
from claude_swap.models import AccountSnapshot, AccountsSnapshot
from claude_swap.oauth import account_headroom, format_reset
from claude_swap.paths import get_backup_root
from claude_swap.poll_policy import parse_reset_ts
from claude_swap.reset_credit_display import reset_credit_display
from claude_swap.printer import accent, dimmed, warning
from claude_swap.process_detection import is_codex_running
from claude_swap.usage_store import SERVE_TTL_S, UsageEntry

if TYPE_CHECKING:
    from claude_swap.codex_session import Custody, ReconcileResult


def _codex_restart_hint() -> str:
    """Phrase the post-switch reminder based on whether Codex is live.

    A running Codex holds the previous login in memory and never re-reads
    ``auth.json``, so the swap only takes effect on its next launch.
    """
    if is_codex_running():
        return (
            "Codex is running — quit and relaunch it (or start a new session) "
            "to use the selected account."
        )
    return "The selected account is ready and takes effect the next time you start Codex."


def get_codex_home() -> Path:
    """Return Codex's configured home directory without creating it."""
    configured = os.environ.get("CODEX_HOME")
    if configured:
        return Path(os.path.expanduser(configured))
    return Path.home() / ".codex"


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _decode_jwt_payload(token: object) -> dict[str, Any]:
    """Return unverified display claims from a JWT-shaped token.

    The claims only label a locally supplied credential; authentication remains
    entirely Codex's responsibility when it later uses the stored token.
    """
    if not isinstance(token, str):
        return {}
    pieces = token.split(".")
    if len(pieces) != 3:
        return {}
    try:
        payload = pieces[1] + "=" * (-len(pieces[1]) % 4)
        decoded = base64.urlsafe_b64decode(payload.encode("ascii"))
        result = json.loads(decoded.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
        return {}
    return result if isinstance(result, dict) else {}


def _plan_type_from_auth(auth: dict[str, Any]) -> str:
    """Return the ChatGPT plan (``team``/``pro``/``plus``/...) for a login.

    Codex stores no workspace name locally, only this plan claim inside the
    signed tokens, so it is the closest label to Claude's org name that can be
    derived without another network round-trip. Returns "" when unknown.
    """
    tokens = auth.get("tokens")
    token_data = tokens if isinstance(tokens, dict) else {}
    for key in ("id_token", "access_token"):
        claims = _decode_jwt_payload(token_data.get(key))
        namespace = claims.get("https://api.openai.com/auth")
        if isinstance(namespace, dict):
            plan = namespace.get("chatgpt_plan_type")
            if isinstance(plan, str) and plan:
                return plan
    return ""


def codex_org_label(plan_type: str | None) -> str:
    """Map a ChatGPT plan to the account label shown in place of "Codex"."""
    if not plan_type:
        return "Codex"
    known = {
        "free": "Codex Free",
        "plus": "Codex Plus",
        "pro": "Codex Pro",
        "team": "Codex Team",
        "business": "Codex Business",
        "enterprise": "Codex Enterprise",
        "edu": "Codex Edu",
    }
    return known.get(plan_type.lower(), f"Codex {plan_type.replace('_', ' ').title()}")


class CodexAccountSwitcher:
    """Manage multiple file-backed Codex authentication documents."""

    def __init__(self, debug: bool = False) -> None:
        self.backup_dir = get_backup_root()
        self.provider_dir = self.backup_dir / "codex"
        self.credentials_dir = self.provider_dir / "credentials"
        self.sequence_file = self.provider_dir / "sequence.json"
        self.lock_file = self.backup_dir / ".lock"
        self.codex_home = get_codex_home()
        self.auth_file = self.codex_home / "auth.json"
        self.debug = debug
        self._usage_cache: dict[str, UsageEntry] = {}

    def _setup_directories(self) -> None:
        for directory in (self.backup_dir, self.provider_dir, self.credentials_dir):
            directory.mkdir(parents=True, exist_ok=True)
            if sys.platform != "win32":
                os.chmod(directory, 0o700)

    def _read_json(self, path: Path) -> dict[str, Any] | None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def _write_json(self, path: Path, data: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            if sys.platform != "win32":
                os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            if sys.platform != "win32":
                os.chmod(path, 0o600)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise

    def _read_sequence(self) -> dict[str, Any]:
        data = self._read_json(self.sequence_file)
        if data is None:
            return {"activeAccountNumber": None, "lastUpdated": _timestamp(), "accounts": {}}
        accounts = data.get("accounts")
        if not isinstance(accounts, dict):
            raise ConfigError(f"Invalid Codex account metadata in {self.sequence_file}")
        return data

    def _write_sequence(self, data: dict[str, Any]) -> None:
        data["lastUpdated"] = _timestamp()
        self._write_json(self.sequence_file, data)

    def _config(self) -> dict[str, Any]:
        config_path = self.codex_home / "config.toml"
        try:
            config_text = config_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except (OSError, UnicodeDecodeError) as exc:
            raise ConfigError(f"Could not read Codex config: {exc}") from exc
        try:
            config = tomllib.loads(config_text)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"Could not parse Codex config: {exc}") from exc
        return config

    def _configured_store(self) -> str | None:
        value = self._config().get("cli_auth_credentials_store")
        return value if isinstance(value, str) else None

    def _chatgpt_base_url(self) -> str:
        value = self._config().get("chatgpt_base_url")
        return value if isinstance(value, str) and value else DEFAULT_CHATGPT_BASE_URL

    def _read_live_auth(self) -> dict[str, Any]:
        if not self.codex_home.is_dir():
            raise ConfigError("No Codex home found. Run 'codex login' first.")
        store = self._configured_store()
        if store == "keyring":
            raise ConfigError(
                "Codex is configured to use the OS keyring. Set "
                "cli_auth_credentials_store = \"file\" in "
                f"{self.codex_home / 'config.toml'}, log in with 'codex login', then retry."
            )
        auth = self._read_json(self.auth_file)
        if auth is None:
            raise ConfigError(
                "No file-backed Codex login found. Codex may be using the OS keyring; "
                "configure cli_auth_credentials_store = \"file\", log in again, and retry."
            )
        if not self._identity_from_auth(auth)[0]:
            raise ValidationError("Codex auth.json does not contain a usable account identity")
        return auth

    @staticmethod
    def _identity_from_auth(auth: dict[str, Any]) -> tuple[str, str, str]:
        mode = auth.get("auth_mode")
        mode = mode if isinstance(mode, str) else "unknown"
        tokens = auth.get("tokens")
        token_data = tokens if isinstance(tokens, dict) else {}
        claims = _decode_jwt_payload(token_data.get("id_token"))
        email = claims.get("email")
        if not isinstance(email, str) or not email:
            identity = auth.get("agent_identity")
            if isinstance(identity, dict):
                email = identity.get("email")
        account_id = token_data.get("account_id") or claims.get("chatgpt_account_id")
        if not isinstance(account_id, str) or not account_id:
            account_id = email if isinstance(email, str) else ""
        if not isinstance(email, str) or not email:
            if isinstance(auth.get("OPENAI_API_KEY"), str):
                digest = hashlib.sha256(auth["OPENAI_API_KEY"].encode("utf-8")).hexdigest()[:10]
                email = f"api-key-{digest}@codex.local"
                account_id = account_id or email
            else:
                return "", "", mode
        return email, account_id, mode

    def _account_plan(self, number: str, account: dict[str, Any], active: str | None) -> str:
        """Read the effective token's plan without refreshing or capturing."""
        from claude_swap.codex_session import sessions_absent

        if sessions_absent(self):
            stored = account.get("planType", "")
            if stored:
                return stored
            try:
                return _plan_type_from_auth(self._read_account_auth(number) or {})
            except (ConfigError, OSError, ValueError, TypeError):
                return ""
        try:
            with FileLock(self.lock_file, timeout=2):
                effective = self._effective_account_auth(number, allow_refresh=False)
        except LockError:
            return account.get("planType", "")
        auth = effective["auth"]
        return (_plan_type_from_auth(auth) if auth else "") or account.get("planType", "")

    def _effective_account_auth(self, number: str, *, allow_refresh: bool,
                                sessions_absent: bool = False) -> dict[str, Any]:
        """Resolve authority without acquiring a lock.

        Caller holds the store lock unless it just proved sessions absent. That
        observation may race a creator, so it only reads backup/default credentials;
        every later refresh independently rechecks custody under the store lock.
        Display never reconciles; locked usage may reconcile quiescent homes.
        """
        from claude_swap import codex_session as cs
        from claude_swap.exceptions import SessionError

        custody = (cs.Custody("none", "no_session_home") if sessions_absent
                   else cs.slot_custody(self, number))
        result = {"auth": None, "source": "unavailable", "custody": custody.state,
                  "refresh": False, "reason": {
                      "uncertain": "Codex session liveness is uncertain",
                      "conflict": "Codex session credentials are unavailable",
                  }.get(custody.state, "stored Codex auth.json is missing"),
                  "base_url": DEFAULT_CHATGPT_BASE_URL}
        try:
            default_auth = None
            default_known = True
            if custody.state in ("none", "quiescent"):
                try:
                    default_auth = self._read_switch_auth()
                except (ConfigError, OSError, ValueError, TypeError):
                    # The backup remains useful for read-only quota requests.
                    # Unknown live identity never grants refresh permission.
                    default_known = False
            active = (default_auth is not None and
                      self._current_account_for_auth(default_auth, self._read_sequence()) == number)
            if default_auth is not None and not active:
                try:
                    active = cs.same_identity(
                        cs.extract_identity(default_auth)[0],
                        cs.extract_identity(self._read_account_auth(number))[0],
                    )
                except SessionError:
                    pass
            if custody.state == "none" or (custody.state == "quiescent" and active):
                result["reason"] = "Codex configuration is unreadable or invalid"
                base_url = self._chatgpt_base_url()
                result.update(auth=default_auth if active else
                              self._read_account_auth(number),
                              source="default" if active else "backup",
                              refresh=allow_refresh and not active and default_known,
                              base_url=base_url, reason="stored Codex auth.json is missing")
            elif custody.state in ("live", "quiescent"):
                home = cs.slot_home(self, number)
                reconcile = custody.state == "quiescent" and allow_refresh and default_known
                if reconcile:
                    self._guard_session_slot(number)
                auth, stored, manifest, h, saved, baseline = self._session_auth_snapshot(number)
                if custody.state == "quiescent" and (reconcile or h == baseline):
                    auth = stored
                # Session tokens only go to the known backend. Copied routing is
                # mutable and has no independently validated endpoint authority.
                config = home / "config.toml"
                if config.exists():
                    values = tomllib.loads(config.read_text(encoding="utf-8"))
                    if cs._CHILD_FORBIDDEN.intersection(values):
                        raise SwitchError("unvalidated_session_routing")
                result.update(auth=auth, source="session" if custody.state == "live" or
                              (not reconcile and h != baseline) else "backup",
                              refresh=allow_refresh and custody.state == "quiescent" and default_known)
        except ConfigError:
            result.update(auth=None, source="unavailable", refresh=False)
        except (OSError, ValueError, TypeError, KeyError, SessionError, SwitchError):
            result.update(auth=None, source="unavailable", refresh=False,
                          reason="Codex session credentials are busy, unreadable or conflicted")
        authority = (result["source"], result["custody"], result["base_url"],
                     cs.auth_fingerprint(result["auth"]) if result["auth"] else None)
        authorities = getattr(self, "_usage_authorities", {})
        if authorities.get(number) != authority:
            self._usage_cache.pop(number, None)
        authorities[number] = authority
        self._usage_authorities = authorities
        return result

    def _session_auth_snapshot(self, number: str) -> tuple:
        """Strict bounded H read and identity/generation checks; store lock required."""
        from claude_swap import codex_session as cs
        from claude_swap.exceptions import SessionError

        home = cs.slot_home(self, number)
        auth = None
        for attempt in range(3):
            try:
                auth = cs.load_document(home / "auth.json")
                if auth is None:
                    raise SessionError("missing auth")
                break
            except (OSError, ValueError, SessionError):
                if attempt == 2:
                    raise SessionError("Session credentials are busy or unreadable") from None
        manifest = cs.load_record(home / ".ccswap-session.json", "manifest")
        if not cs.same_identity(cs.extract_identity(auth)[0], manifest["identity"]):
            raise SwitchError("identity_conflict")
        stored = self._read_session_backup(number)
        if not cs.same_identity(cs.extract_identity(stored)[0], manifest["identity"]):
            raise SwitchError("identity_conflict")
        h, saved = cs.auth_fingerprint(auth), cs.auth_fingerprint(stored)
        baseline = manifest["baselineFingerprint"]
        if h != saved and h != baseline and saved != baseline:
            raise SwitchError("generation_conflict")
        return auth, stored, manifest, h, saved, baseline

    def _credential_path(self, number: str) -> Path:
        return self.credentials_dir / f"account-{number}.json"

    def _read_account_auth(self, number: str) -> dict[str, Any] | None:
        return self._read_json(self._credential_path(number))

    def _read_session_backup(self, number: str) -> dict[str, Any] | None:
        """Strict session-only read; caller holds the store lock."""
        from claude_swap.codex_session import _checked_parents, load_document

        path = self._credential_path(number)
        _checked_parents(path.parent, self.backup_dir)
        return load_document(path)

    def _write_session_backup(self, number: str, auth: dict[str, Any]) -> None:
        """Replace an existing backup only; never recreate a removed slot."""
        from claude_swap.codex_session import _ordinary, _require, _session_write

        path = self._credential_path(number)
        _require(_ordinary(path))
        _session_write(self.backup_dir, path, auth)
        self._usage_cache.pop(number, None)

    def _next_number(self, data: dict[str, Any]) -> str:
        from claude_swap.codex_session import _ordinary

        numbers = [int(number) for number in data["accounts"] if number.isdigit()]
        sessions = self.backup_dir / "codex" / "sessions"
        try:
            retained = [path.name.split("-", 1)[0] for path in sessions.iterdir()] if (
                _ordinary(sessions, directory=True)) else []
        except OSError:
            raise SwitchError("Cannot read Codex session homes; choose a slot explicitly") from None
        number = max(numbers, default=0) + 1
        while str(number) in retained:
            number += 1
        return str(number)

    def _resolve(self, identifier: str, data: dict[str, Any]) -> str:
        if identifier.isdigit() and identifier in data["accounts"]:
            return identifier
        matches = [
            number
            for number, account in data["accounts"].items()
            if account.get("email") == identifier
        ]
        if not matches:
            raise AccountNotFoundError(f"No Codex account found with identifier: {identifier}")
        if len(matches) > 1:
            raise ConfigError(f"Codex email '{identifier}' is ambiguous; use its account number")
        return matches[0]

    def current_account_number(self) -> str | None:
        try:
            auth = self._read_live_auth()
        except ConfigError:
            return None
        data = self._read_sequence()
        return self._current_account_for_auth(auth, data)

    @staticmethod
    def _match_account_number(
        accounts: dict[str, Any],
        email: str,
        account_id: str,
    ) -> str | None:
        """Find the slot holding this login, identified by email *and* workspace.

        Neither field identifies a login on its own. Seats in one ChatGPT Team
        workspace share a ``chatgpt_account_id``, and one person can hold seats
        in several workspaces under a single email. Matching on either field
        alone therefore collapses two distinct logins onto one slot, and that
        slot's stored credentials are then overwritten with the other login's
        tokens -- the failure this helper exists to prevent, in one direction or
        the other.

        So anything short of an exact match is ``None``, which callers read as
        "not managed here": :meth:`add_account` allocates a new slot instead of
        claiming an existing one, and :meth:`switch_to` refuses rather than
        writing the live login into a slot that may belong to someone else. A
        renamed login is the cost -- it needs a fresh ``add`` -- and that is a
        far better trade than silently destroying a login.
        """
        if not email:
            return None
        for number, account in accounts.items():
            if account.get("accountId") == account_id and account.get("email") == email:
                return number
        return None

    def _current_account_for_auth(
        self, auth: dict[str, Any], data: dict[str, Any]
    ) -> str | None:
        email, account_id, _mode = self._identity_from_auth(auth)
        return self._match_account_number(data["accounts"], email, account_id)

    def add_account(self, slot: int | None = None, assume_yes: bool = False) -> None:
        live_auth = self._read_live_auth()
        email, account_id, mode = self._identity_from_auth(live_auth)
        plan_type = _plan_type_from_auth(live_auth)
        with FileLock(self.lock_file):
            self._setup_directories()
            if self._read_live_auth() != live_auth:
                raise SwitchError("Codex login changed; retry")
            data = self._read_sequence()
            # Only the very same login may claim an occupied slot. A different
            # seat in this workspace, or this email in a different workspace, is
            # a separate login and gets its own slot -- resolving either onto an
            # existing slot would overwrite that login's tokens without asking,
            # since the prompt below is skipped when number == existing.
            existing = self._match_account_number(data["accounts"], email, account_id)
            number = str(slot) if slot is not None else existing or self._next_number(data)
            if not number.isdigit() or int(number) < 1:
                raise ValidationError("Codex account slot must be a positive integer")
            occupant = data["accounts"].get(number)
            from claude_swap.codex_session import (
                _ordinary, _sharing_exists, default_write_decision, extract_identity,
                load_document, load_record, replace_session_credentials, slot_home,
            )

            for guarded in dict.fromkeys([n for n in (existing, number) if n in data["accounts"]]):
                self._guard_session_slot(guarded)
            home = slot_home(self, number) if occupant else None
            sessions = self.backup_dir / "codex" / "sessions"
            retained = ([path for path in sessions.iterdir() if path.name.startswith(f"{number}-")]
                        if _ordinary(sessions, directory=True) else [])
            if retained and (home is None or any(path != home for path in retained)):
                raise SwitchError(
                    f"Codex session home kept at {retained[0]}; manual disposition required")
            if home is not None and _sharing_exists(home):
                manifest = load_record(home / ".ccswap-session.json", "manifest")
                from claude_swap.exceptions import SessionError

                try:
                    identity, _ = extract_identity(live_auth)
                except SessionError:
                    raise SwitchError("Codex session generation conflict; files retained") from None
                if any(identity[key] != manifest["identity"][key]
                       for key in ("userId", "workspaceId", "email")):
                    raise SwitchError(
                        f"Codex session home kept at {home}; manual disposition required")
                stored = self._read_session_backup(number)
                if occupant and number != existing and not assume_yes:
                    answer = input(f"Overwrite Codex account slot {number}? [y/N] ").strip().lower()
                    if answer not in ("y", "yes"):
                        print("Cancelled")
                        return
                decision = default_write_decision(
                    live_auth, load_document(home / "auth.json"), stored, manifest)
                if decision == "conflict":
                    raise SwitchError("Codex session generation conflict; files retained")
                if decision == "skip":
                    print("The saved Codex login is already current")
                    return
                if self._read_live_auth() != live_auth:
                    raise SwitchError("Codex login changed; retry")
                replace_session_credentials(self, number, live_auth, "replacement")
            else:
                if occupant and number != existing and not assume_yes:
                    answer = input(f"Overwrite Codex account slot {number}? [y/N] ").strip().lower()
                    if answer not in ("y", "yes"):
                        print("Cancelled")
                        return
                if self._read_live_auth() != live_auth:
                    raise SwitchError("Codex login changed; retry")
                self._write_json(self._credential_path(number), live_auth)
            data["accounts"][number] = {
                "email": email,
                "accountId": account_id,
                "authMode": mode,
                "planType": plan_type,
                "added": _timestamp(),
            }
            data["activeAccountNumber"] = int(number)
            self._write_sequence(data)
        print(f"Added Codex Account {number}: {email}")

    def remove_account(self, identifier: str, assume_yes: bool = False) -> None:
        with FileLock(self.lock_file):
            data = self._read_sequence()
            number = self._resolve(identifier, data)
            account = data["accounts"][number]
            self._guard_session_slot(number)
            before = self._switch_slot_snapshot(number)
            if not assume_yes:
                answer = input(
                    f"Permanently remove Codex Account {number} ({account['email']})? [y/N] "
                ).strip().lower()
                if answer not in ("y", "yes"):
                    print("Cancelled")
                    return
            from claude_swap.codex_session import slot_custody, slot_home

            custody = slot_custody(self, number)
            if custody.state not in ("none", "quiescent") or custody.intent:
                raise SwitchError("Codex session custody changed during removal; retry")
            if self._switch_slot_snapshot(number) != before or self._read_sequence() != data:
                raise SwitchError("Codex session state changed during removal; retry")
            self._guard_session_slot(number)
            home = slot_home(self, number) if before[0] is not None else None
            del data["accounts"][number]
            if data.get("activeAccountNumber") == int(number):
                data["activeAccountNumber"] = None
            self._write_sequence(data)
            self._credential_path(number).unlink(missing_ok=True)
        print(f"Removed Codex Account {number} ({account['email']})")
        if home is not None:
            print(f"Session home kept at {home}")

    def _guard_session_slot(self, number: str) -> Custody | ReconcileResult:
        """Require custody and reconcile; caller must hold R/.lock."""
        from claude_swap.codex_session import (
            reconcile_session, recover_session_credentials, slot_custody,
        )
        from claude_swap.exceptions import SessionError

        custody = slot_custody(self, number)
        if custody.state == "quiescent" and custody.intent:
            try:
                recover_session_credentials(self, number)
            except (OSError, ValueError, TypeError, SessionError):
                raise SwitchError(
                    f"Codex Account {number} session conflict; files retained") from None
        outcome = (reconcile_session(self, number, quiescent=True)
                   if custody.state == "quiescent" else custody)
        if outcome.state in ("live", "uncertain", "conflict"):
            raise SwitchError(f"Codex Account {number} session {outcome.state}: {outcome.reason}")
        return outcome

    def _default_session_slots(self, auth: dict[str, Any], data: dict[str, Any]) -> list[str]:
        """Find strict identity aliases; legacy matching never authorizes a session."""
        from claude_swap.codex_session import (
            _sharing_exists, extract_identity, same_identity, slot_home,
        )
        from claude_swap.exceptions import SessionError

        try:
            identity, _ = extract_identity(auth)
        except SessionError:
            identity = None
        matches = []
        legacy = self._current_account_for_auth(auth, data)
        for number in data["accounts"]:
            stored = self._read_account_auth(number)
            try:
                other, _ = extract_identity(stored)
                if identity is not None and same_identity(identity, other):
                    matches.append(number)
                    continue
            except SessionError:
                pass
            tokens = auth.get("tokens")
            other_tokens = stored.get("tokens") if stored else None
            if (isinstance(tokens, dict) and isinstance(other_tokens, dict)
                    and any(tokens.get(key) and tokens[key] == other_tokens.get(key)
                            for key in ("access_token", "refresh_token"))
                    and _sharing_exists(slot_home(self, number))):
                raise SwitchError(
                    "Codex session conflict: default token alias identity is unverified")
            if number == legacy:
                if _sharing_exists(slot_home(self, number)):
                    raise SwitchError("Current Codex login has no verified session identity")
                matches.append(number)
        return matches

    def _switch_slot_snapshot(self, number: str) -> tuple:
        """Read H, S and the complete manifest under R/.lock."""
        from claude_swap.codex_session import _sharing_exists, load_document, load_record, slot_home

        home = slot_home(self, number)
        if not _sharing_exists(home):
            return None, self._read_account_auth(number), None
        return (load_document(home / "auth.json"), self._read_session_backup(number),
                load_record(home / ".ccswap-session.json", "manifest"))

    def _read_switch_auth(self, *, recheck: bool = False) -> dict[str, Any] | None:
        """Read D through ordinary links, distinguishing absence from read failure."""
        try:
            auth = json.loads(self.auth_file.read_text(encoding="utf-8"))
            if isinstance(auth, dict):
                return auth
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            pass
        if recheck:
            raise SwitchError("Codex state changed during switch; retry")
        raise ConfigError("Current Codex auth.json is unreadable; refusing to overwrite it")

    def switch_to(self, identifier: str, json_output: bool = False, force: bool = False) -> dict[str, Any]:
        """Switch under R/.lock; Codex itself does not take this lock.

        A residual race remains between the final D check and activation write.
        """
        from claude_swap.codex_session import (
            auth_fingerprint, default_write_decision, extract_identity, with_baseline,
        )
        from claude_swap.exceptions import SessionError

        del force
        with FileLock(self.lock_file):
            data = self._read_sequence()
            number = self._resolve(identifier, data)
            target = data["accounts"][number]
            original_auth = self._read_switch_auth()
            current = (
                self._current_account_for_auth(original_auth, data)
                if original_auth is not None
                else None
            )
            try:
                outgoing = self._default_session_slots(original_auth, data) if original_auth else []
            except (OSError, SessionError, ValueError, TypeError, KeyError):
                raise SwitchError("Codex session conflict: cannot verify default aliases") from None
            involved = list(dict.fromkeys([number, *outgoing]))
            checking = False
            try:
                before = {slot: self._switch_slot_snapshot(slot) for slot in involved}
                writes = []
                for slot in outgoing:
                    h, stored, manifest = before[slot]
                    if manifest is None:
                        if original_auth != stored:
                            writes.append(slot)
                        continue
                    if number in outgoing or current == number:
                        continue
                    decision = default_write_decision(original_auth, h, stored, manifest)
                    if decision == "skip":
                        continue
                    if decision == "conflict":
                        raise SwitchError(
                            "Codex session conflict: default credential lineage diverged")
                    writes.append(slot)
                outcomes = {slot: self._guard_session_slot(slot) for slot in involved}
                checking = True
                for slot, (h, stored, manifest) in before.items():
                    if manifest is not None:
                        ph, ps = map(auth_fingerprint, (h, stored))
                        planned = ("unchanged" if ph == ps else "adopted_session"
                                   if ps == manifest["baselineFingerprint"] else "reseeded_session")
                        if outcomes[slot].state != planned:
                            raise SwitchError("Codex state changed during switch; retry")
                        value = h if outcomes[slot].state == "adopted_session" else stored
                        expected = (value, value, with_baseline(manifest, auth_fingerprint(value)))
                    else:
                        expected = (h, stored, manifest)
                    if (
                        auth_fingerprint({"slot": self._switch_slot_snapshot(slot)})
                        != auth_fingerprint({"slot": expected})
                    ):
                        raise SwitchError("Codex state changed during switch; retry")
                if (
                    auth_fingerprint({"D": self._read_switch_auth(recheck=True)})
                    != auth_fingerprint({"D": original_auth})
                ):
                    raise SwitchError("Codex state changed during switch; retry")
            except (OSError, SessionError, ValueError, TypeError, KeyError):
                raise SwitchError("Codex state changed during switch; retry" if checking else
                                  "Codex session conflict: cannot verify switch snapshot") from None
            if outgoing:
                current = current if current in outgoing else outgoing[0]
            if number in outgoing or current == number:
                return {
                    "switched": False,
                    "from": {"number": int(number), "email": target["email"]},
                    "to": {"number": int(number), "email": target["email"]},
                    "reason": "already-active",
                }
            if original_auth is not None and current is None:
                raise SwitchError(
                    "The current Codex login is unmanaged. Run 'ccswap codex add' "
                    "to preserve it before switching accounts."
                )
            expected_target = self._switch_slot_snapshot(number)
            for slot in writes:
                if (
                    auth_fingerprint({"D": self._read_switch_auth(recheck=True)})
                    != auth_fingerprint({"D": original_auth})
                ):
                    raise SwitchError("Codex state changed during switch; retry")
                if before[slot][2] is not None:
                    self._write_session_backup(slot, original_auth)
                else:
                    self._write_json(self._credential_path(slot), original_auth)
                    self._usage_cache.pop(slot, None)
            auth = self._read_account_auth(number)
            if auth is None:
                raise SwitchError(f"Codex Account {number} has no stored auth.json backup")
            if not self._identity_from_auth(auth)[0]:
                raise SwitchError(f"Codex Account {number} has an invalid stored auth.json backup")
            if before[number][2] is not None:
                extract_identity(auth)
            if (
                auth_fingerprint({"slot": self._switch_slot_snapshot(number)})
                != auth_fingerprint({"slot": expected_target})
                or auth_fingerprint(auth) != auth_fingerprint(expected_target[1])
                or auth_fingerprint({"D": self._read_switch_auth(recheck=True)})
                != auth_fingerprint({"D": original_auth})
            ):
                raise SwitchError("Codex state changed during switch; retry")
            try:
                self._write_json(self.auth_file, auth)
                data["activeAccountNumber"] = int(number)
                self._write_sequence(data)
            except Exception:
                # An external rotation owns any different generation of D.
                if (original_auth is not None
                        and self._read_json(self.auth_file) in (auth, original_auth)):
                    self._write_json(self.auth_file, original_auth)
                raise
        result = {
            "switched": True,
            "from": {"number": int(current) if current else None, "email": ""},
            "to": {"number": int(number), "email": target["email"]},
            "reason": "requested",
        }
        if not json_output:
            print(f"Switched to Codex Account {number}: {target['email']}")
            print(_codex_restart_hint())
        return result

    def switch(self, strategy: str | None = None, json_output: bool = False) -> dict[str, Any]:
        if strategy is not None:
            return self._switch_with_strategy(strategy, json_output)
        data = self._read_sequence()
        numbers = sorted(data["accounts"], key=int)
        if not numbers:
            raise SwitchError("No Codex accounts are managed yet")
        current = self.current_account_number()
        start = numbers.index(current) + 1 if current in numbers else 0
        rotation = (numbers[(start + offset) % len(numbers)] for offset in range(len(numbers)))
        from claude_swap.codex_session import classify_session, slot_custody

        with FileLock(self.lock_file):
            candidates = []
            for number in rotation:
                if self._disabled_from_data(data, number):
                    continue
                custody = slot_custody(self, number)
                if custody.state == "none" or (custody.state == "quiescent"
                        and classify_session(self, number).state != "conflict"):
                    candidates.append(number)
        for target in candidates:
            try:
                return self.switch_to(target, json_output=json_output)
            except SwitchError as exc:
                if "session conflict" not in str(exc):
                    raise
        raise SwitchError(
            "No managed Codex account is available (disabled or session custody blocked)")

    @staticmethod
    def _strategy_usage(account: AccountSnapshot) -> dict[str, Any] | None:
        """Ignore last-good measurements masked by a live usage error."""
        entry = account.usage
        if entry.sentinel is None and isinstance(entry.last_good, dict):
            return entry.last_good
        return None

    def _select_best(
        self, current: AccountSnapshot, others: list[AccountSnapshot],
    ) -> tuple[str | None, str, str]:
        """Return (target, "", "") only for strictly more headroom.

        Stays return (None, reason, message): usage-unavailable for unknown or
        incomplete comparisons, candidates-exhausted when all are spent, and
        already-best when current is provably best (including ties).
        """
        headroom = account_headroom(self._strategy_usage(current))
        if headroom is None:
            return (
                None, "usage-unavailable",
                "Current Codex usage is unavailable; staying put.",
            )
        scored = [(account_headroom(self._strategy_usage(a)), a.number) for a in others]
        known = [(h, n) for h, n in scored if h is not None]
        if not known:
            return (
                None, "usage-unavailable",
                "No other Codex account has measurable usage; staying put.",
            )
        best, target = max(known, key=lambda item: item[0])
        if best > headroom:
            return target, "", ""
        if any(h is None for h, _ in scored):
            return (
                None, "usage-unavailable",
                "Incomplete comparison: some Codex usage is unavailable; staying put.",
            )
        if headroom <= 0:
            return (
                None, "candidates-exhausted",
                "All eligible Codex accounts are at their limit; staying put.",
            )
        return (
            None, "already-best",
            "Current Codex account already has the most headroom; staying put.",
        )

    def _select_next_available(
        self, current: AccountSnapshot, others: list[AccountSnapshot],
    ) -> tuple[str | None, str, str]:
        """Return (target, "", "") for the next slot not known to be exhausted.

        Stays return (None, reason, message): usage-unavailable without eligible
        targets, or candidates-exhausted when every other eligible slot is spent.
        """
        if not others:
            return (
                None, "usage-unavailable",
                "No other switchable, enabled ChatGPT account is available; staying put.",
            )
        after = [a for a in others if int(a.number) > int(current.number)]
        before = [a for a in others if int(a.number) < int(current.number)]
        for account in [*after, *before]:
            headroom = account_headroom(self._strategy_usage(account))
            if headroom is None or headroom > 0:
                return account.number, "", ""
        return (
            None, "candidates-exhausted",
            "All other eligible Codex accounts are at their limit; staying put.",
        )

    def _select_consume_first(
        self, current: AccountSnapshot, others: list[AccountSnapshot],
    ) -> tuple[str | None, str, str]:
        """Return (target, "", "") for quota whose future weekly reset is soonest.

        Current winning returns (None, already-best, message). Without rankable
        quota, defer to best and its usage-unavailable, candidates-exhausted or
        already-best stays.
        """
        ranked: list[tuple[float, str]] = []
        now = time.time()
        for account in [current, *others]:
            usage = self._strategy_usage(account)
            headroom = account_headroom(usage)
            weekly = usage.get("weekly") if usage else None
            reset = (
                parse_reset_ts(weekly.get("resets_at"))
                if isinstance(weekly, dict) else None
            )
            # Past resets are stale measurements, not quota about to expire.
            if headroom is not None and headroom > 0 and reset is not None and reset > now:
                ranked.append((reset, account.number))
        if not ranked:
            return self._select_best(current, others)
        # min() preserves ties: current first, then the lowest slot.
        target = min(ranked, key=lambda item: item[0])[1]
        if target == current.number:
            return (
                None, "already-best",
                "Current Codex account's weekly limit resets soonest; staying put.",
            )
        return target, "", ""

    def _switch_with_strategy(self, strategy: str, json_output: bool) -> dict[str, Any]:
        selectors = {
            "best": self._select_best,
            "next-available": self._select_next_available,
            "consume-first": self._select_consume_first,
        }
        if strategy not in selectors:
            raise ValueError(f"Unknown Codex switch strategy: {strategy}")
        snapshot = self.accounts_snapshot()
        accounts = sorted(snapshot.accounts, key=lambda a: int(a.number))
        current = next((a for a in accounts if a.number == snapshot.active_number), None)
        # API-key accounts have no subscription quota to compare, as in `codex auto`.
        others = [
            a for a in accounts
            if a.number != snapshot.active_number
            and a.switchable and not a.disabled and a.kind == "oauth"
        ]
        if len(accounts) == 1:
            selection = (
                None, "only-one-account", "Only one Codex account is managed; staying put.",
            )
        elif current is None or current.kind != "oauth":
            selection = (
                None, "usage-unavailable",
                "No active managed ChatGPT login to compare; staying put.",
            )
        else:
            selection = selectors[strategy](current, others)
        return self._finish_strategy_switch(current, strategy, selection, json_output)

    def _finish_strategy_switch(
        self,
        current: AccountSnapshot | None,
        strategy: str,
        selection: tuple[str | None, str, str],
        json_output: bool,
    ) -> dict[str, Any]:
        """Apply the selected target or report a stay without changing the login."""
        target, reason, message = selection
        if target is not None:
            result = self.switch_to(target, json_output=json_output)
            result["strategy"] = strategy
            return result
        active = {
            "number": int(current.number) if current else None,
            "email": current.email if current else "",
        }
        if not json_output:
            if reason == "already-best":
                lead, separator, tail = message.partition(";")
                print(f"{accent(lead)}{separator}{tail}")
            elif reason == "candidates-exhausted":
                warning(message)
            else:
                hint = (
                    " Run 'ccswap codex switch' to rotate."
                    if reason == "usage-unavailable" else ""
                )
                print(dimmed(message + hint))
        return {
            "switched": False, "from": active, "to": dict(active),
            "reason": reason, "strategy": strategy, "message": message,
        }

    @staticmethod
    def _disabled_from_data(data: dict[str, Any], number: str) -> bool:
        record = data["accounts"].get(number)
        return bool(record and record.get("disabled"))

    def is_account_disabled(self, identifier: str) -> bool:
        data = self._read_sequence()
        return self._disabled_from_data(data, self._resolve(identifier, data))

    def set_account_disabled(self, identifier: str, disabled: bool) -> None:
        """Hold a Codex account out of automatic rotation, or return it.

        Same contract as the Claude switcher's: disabling only affects
        automatic selection (bare ``switch`` rotation and the auto engine).
        The account stays managed and remains a valid explicit
        ``ccswap codex switch <num|email>`` target.
        """
        with FileLock(self.lock_file):
            data = self._read_sequence()
            number = self._resolve(identifier, data)
            record = data["accounts"][number]
            email = record.get("email", "")
            verb = "disabled" if disabled else "enabled"
            if bool(record.get("disabled")) == disabled:
                print(f"Codex Account {number} ({email}) is already {verb}.")
                return
            if disabled:
                record["disabled"] = True
            else:
                record.pop("disabled", None)
            self._write_sequence(data)
        print(f"{verb.capitalize()} Codex Account {number} ({email}).")

    def clear_poll_policy_inputs(self) -> None:
        """No-op: Codex usage polling has no persisted poll plan to pin.

        Present so provider-agnostic callers (the TUI auto view) can unmount
        against either switcher.
        """

    def _session_status(self, number: str) -> dict[str, Any]:
        """Observe session state without capture, refresh or credential writes."""
        from claude_swap.codex_session import sessions_absent

        if sessions_absent(self):
            return {"state": "absent", "capturePending": False, "runCount": 0, "shared": False}
        try:
            with FileLock(self.lock_file, timeout=2):
                return self._locked_session_status(number)
        except LockError:
            return {"state": "uncertain", "capturePending": True, "runCount": 0, "shared": False}

    def _locked_session_status(self, number: str) -> dict[str, Any]:
        """Classify display state under the caller-held store lock."""
        from claude_swap import codex_session as cs, process_detection, windows_job
        from claude_swap.exceptions import SessionError

        custody = cs.slot_custody(self, number)
        state = {"none": "absent", "quiescent": "idle", "conflict": "uncertain"}.get(
            custody.state, custody.state)
        pending = state in ("live", "uncertain") or custody.intent
        count, shared = 0, False
        if state != "absent":
            try:
                if custody.intent:
                    home = cs.slot_home(self, number)
                    intent = cs.load_record(home.parent / f".{home.name}.stale.json", "stale")
                    if intent is None:
                        raise SessionError("Unreadable session intent")
                    if state == "idle":
                        state = "stale"
                else:
                    _, _, _, h, saved, baseline = self._session_auth_snapshot(number)
                    pending |= h != saved or h != baseline
                runs = cs.slot_home(self, number) / "runs"
                if runs.exists():
                    process_state = (windows_job.process_state if sys.platform == "win32"
                                     else process_detection.codex_process_state)
                    for path in runs.iterdir():
                        run = cs.load_record(path, "run")
                        if run["phase"] == "finished":
                            continue
                        supervisor = run["supervisor"]
                        alive = process_state(supervisor["pid"], supervisor["start"])
                        if alive == "alive":
                            count += 1
                            shared |= run["shared"]
                        elif alive == "unknown" and state != "conflict":
                            state, pending = "uncertain", True
            except SwitchError:
                state = "conflict"
            except (OSError, ValueError, TypeError, KeyError, SessionError):
                if state != "conflict":
                    state, pending = "uncertain", True
        return {"state": state, "capturePending": pending, "runCount": count, "shared": shared}

    def status(self, json_output: bool = False) -> dict[str, Any]:
        data = self._read_sequence()
        number = self.current_account_number()
        account = data["accounts"].get(number) if number else None
        plan_type = self._account_plan(number, account, number) if account else ""
        payload = {
            "provider": "codex",
            "activeAccountNumber": int(number) if number else None,
            "email": account.get("email") if account else None,
            "planType": plan_type,
            "label": codex_org_label(plan_type),
        }
        payload["sessions"] = [{"number": int(n), "session": self._session_status(n)}
                               for n in sorted(data["accounts"], key=int)]
        if json_output:
            print(json.dumps(payload, indent=2))
        elif account:
            tag = f" [{codex_org_label(plan_type)}]" if plan_type else ""
            print(f"Codex Account {number}: {account['email']}{tag} (active)")
        else:
            print("No managed Codex account is currently active")
        if not json_output:
            for row in payload["sessions"]:
                if row["session"]["capturePending"]:
                    print(f"Account {row['number']}: capture pending")
        return payload

    def list_payload(self) -> dict[str, Any]:
        """Build the Codex account listing without printing anything.

        Lets the top-level ``ccswap list`` merge Codex into a combined payload
        while ``list_accounts`` keeps rendering the standalone view.
        """
        data = self._read_sequence()
        active = self.current_account_number()
        accounts = []
        for number, account in sorted(data["accounts"].items(), key=lambda item: int(item[0])):
            plan = self._account_plan(number, account, active)
            accounts.append(
                {
                    "number": int(number),
                    "email": account.get("email", ""),
                    "authMode": account.get("authMode", "unknown"),
                    "planType": plan,
                    "label": codex_org_label(plan),
                    "active": number == active,
                    "disabled": bool(account.get("disabled")),
                    "session": self._session_status(number),
                }
            )
        return {
            "provider": "codex",
            "activeAccountNumber": int(active) if active else None,
            "accounts": accounts,
        }

    def list_accounts(self, json_output: bool = False) -> dict[str, Any]:
        payload = self.list_payload()
        accounts = payload["accounts"]
        if json_output:
            print(json.dumps(payload, indent=2))
        elif not accounts:
            print("No managed Codex accounts")
        else:
            for account in accounts:
                marker = " ● active" if account["active"] else ""
                marker += " (disabled)" if account["disabled"] else ""
                marker += " (capture pending)" if account["session"]["capturePending"] else ""
                # Prefer the plan label (e.g. "Codex Team"); fall back to the
                # auth mode for API-key logins or backups added before planType
                # was recorded.
                tag = account["label"] if account["planType"] else f"Codex ({account['authMode']})"
                print(f"{account['number']:>2}  {account['email']}  [{tag}]{marker}")
        return payload

    def usage_status(self, json_output: bool = False) -> dict[str, Any]:
        """Fetch and print read-only Codex quota data for every saved account."""
        snapshot = self.accounts_snapshot(fetch=None)
        accounts = [
            {
                "number": int(account.number),
                "email": account.email,
                "active": account.is_active,
                "usage": account.usage.last_good,
                "error": account.usage.last_error,
                "status": account.usage.sentinel,
            }
            for account in snapshot.accounts
        ]
        payload = {"provider": "codex", "accounts": accounts}
        if json_output:
            print(json.dumps(payload, indent=2))
            return payload
        if not accounts:
            print("No managed Codex accounts")
            return payload
        for account in accounts:
            marker = " (active)" if account["active"] else ""
            print(f"Codex Account {account['number']}: {account['email']}{marker}")
            reset_display = reset_credit_display(account["usage"], time.time())
            if reset_display and reset_display.urgent:
                stale = " · last known" if account["status"] or account["error"] else ""
                print(f"  ⚠ Resets: {reset_display.text}{stale}")
            if account["status"]:
                print(f"  {account['status']}")
            elif account["usage"]:
                for key, label in (
                    ("five_hour", "5h"),
                    ("seven_day", "7d"),
                    ("weekly", "Weekly"),
                ):
                    window = account["usage"].get(key)
                    if isinstance(window, dict):
                        line = f"  {label}: {window.get('pct', 0):.0f}% used"
                        resets_at = window.get("resets_at")
                        if isinstance(resets_at, str) and resets_at:
                            countdown, clock = format_reset(resets_at)
                            line += f" · resets in {countdown} ({clock})"
                        print(line)
                if reset_display and not reset_display.urgent:
                    print(f"  Resets: {reset_display.text}")
                credits = account["usage"].get("credits")
                if isinstance(credits, dict):
                    if credits.get("limit_reached") is True:
                        line = "  Credits: limit reached"
                    elif credits.get("unlimited") is True:
                        line = "  Credits: unlimited"
                    elif credits.get("has_credits") is False:
                        line = "  Credits: none"
                    elif isinstance(credits.get("balance"), (int, float)):
                        line = f"  Credits: {credits['balance']:,.2f} available"
                    elif credits.get("has_credits") is True:
                        line = "  Credits: available (balance not reported)"
                    else:
                        line = "  Credits: status unavailable"
                    print(line)
                allowance = account["usage"].get("credit_allowance")
                if isinstance(allowance, dict):
                    if allowance.get("limit_reached") is True:
                        line = "  Credit allowance: spending limit reached"
                    elif isinstance(allowance.get("remaining"), (int, float)):
                        line = (
                            f"  Credit allowance: {allowance['remaining']:,.2f} remaining"
                        )
                    elif isinstance(allowance.get("limit"), (int, float)):
                        line = f"  Credit allowance: {allowance['limit']:,.2f} limit"
                    else:
                        line = "  Credit allowance: status unavailable"
                    if isinstance(allowance.get("limit"), (int, float)) and isinstance(
                        allowance.get("remaining"), (int, float)
                    ):
                        line += f" · {allowance['limit']:,.2f} limit"
                    resets_at = allowance.get("resets_at")
                    if isinstance(resets_at, str) and resets_at:
                        countdown, clock = format_reset(resets_at)
                        line += f" · resets in {countdown} ({clock})"
                    print(line)
            else:
                print(f"  usage unavailable: {account['error'] or 'no data returned'}")
        return payload

    def redeem_reset(
        self, identifier: str, assume_yes: bool = False, json_output: bool = False
    ) -> dict[str, Any]:
        """Redeem only after explicit consent, then confirm with fresh usage."""
        if json_output and not assume_yes:
            raise SwitchError("--json requires --yes")
        if not assume_yes and not sys.stdin.isatty():
            raise SwitchError("Non-interactive reset redemption requires --yes")
        data = self._read_sequence()
        number = self._resolve(identifier, data)
        record = data["accounts"][number]
        try:
            before = self._fetch_redeem_usage(number)
            if before.get("reset_credits", {}).get("available", 0) <= 0:
                raise SwitchError("No banked resets available for this account")
            auth, _ = self._redeem_auth(number)
            cards = list_reset_credits(auth, base_url=self._chatgpt_base_url())
            if not cards:
                raise SwitchError("Reset card details unavailable; nothing was redeemed")
            card = cards[0]
            if not json_output:
                print(f"Codex Account {number}: {record.get('email', '')}")
                self._print_redeem_usage(before)
            if not assume_yes:
                self._confirm_redeem(card)
            try:
                response, credit_id = self._consume_reset(
                    number, record, str(uuid.uuid4()), card["id"]
                )
            except CodexUsageError as exc:
                # A 401 means nothing was spent; anything else may have been.
                if json_output and exc.status_code != 401:
                    print(json.dumps({
                        "account": {"number": int(number), "email": record.get("email", "")},
                        "code": "unknown", "creditId": card["id"],
                        "confirmed": False, "error": str(exc),
                    }))
                raise
        except CodexUsageError as exc:
            raise SwitchError(str(exc)) from exc
        result = {
            "account": {"number": int(number), "email": record.get("email", "")},
            "code": response["code"],
            "windowsReset": response.get("windows_reset", 0),
            "creditId": credit_id,
            "usageAfter": None,
            "confirmed": False,
        }
        if result["code"] == "reset":
            try:
                result["usageAfter"] = self._fetch_redeem_usage(number)
                remaining = result["usageAfter"].get("reset_credits", {}).get("available")
                result["confirmed"] = (
                    isinstance(remaining, int)
                    and not isinstance(remaining, bool)
                    and remaining < before["reset_credits"]["available"]
                )
            except (CodexUsageError, ClaudeSwitchError, OSError, http.client.HTTPException):
                pass
        self._report_redeem(result, json_output)
        return result

    def _redeem_auth(self, number: str) -> tuple[dict[str, Any], bool]:
        """Read the selected login, refusing missing ChatGPT or API-key auth."""
        active = self.current_account_number() == number
        auth = self._read_json(self.auth_file) if active else self._read_account_auth(number)
        if (
            not auth
            or auth.get("auth_mode") == "api_key"
            or not isinstance(auth.get("tokens"), dict)
        ):
            raise SwitchError(
                "Reset redemption requires a stored/live ChatGPT login, not an API key"
            )
        return auth, active

    def _fetch_redeem_usage(self, number: str) -> dict[str, Any]:
        """Fetch fresh usage with one inactive-account 401 recovery."""
        auth, active = self._redeem_auth(number)
        try:
            return fetch_codex_usage(auth, base_url=self._chatgpt_base_url())
        except CodexUsageError as exc:
            if exc.status_code != 401 or active:
                raise
            refreshed = self._refresh_inactive_auth(number, auth)
            if refreshed is None:
                raise
            return fetch_codex_usage(refreshed, base_url=self._chatgpt_base_url())

    @staticmethod
    def _print_redeem_usage(usage: dict[str, Any]) -> None:
        """Print available quota windows and banked resets with countdowns."""
        for key, label in (("five_hour", "5h"), ("weekly", "Weekly")):
            window = usage.get(key)
            if isinstance(window, dict):
                line = f"  {label}: {window.get('pct', 0):.0f}% used"
                resets_at = window.get("resets_at")
                if isinstance(resets_at, str) and resets_at:
                    countdown, clock = format_reset(resets_at)
                    line += f" · resets in {countdown} ({clock})"
                print(line)
        credits = usage.get("reset_credits")
        if isinstance(credits, dict) and isinstance(credits.get("available"), int):
            line = f"  Banked resets: {credits['available']}"
            expires_at = credits.get("expires_at")
            if isinstance(expires_at, str) and expires_at:
                countdown, clock = format_reset(expires_at)
                line += f" · earliest expires in {countdown} ({clock})"
            print(line)

    @staticmethod
    def _confirm_redeem(card: dict[str, Any]) -> None:
        """Require affirmative consent, treating EOF and interruption as No."""
        try:
            expiry = card.get("expires_at") or "unknown"
            consent = input(
                f"Redeem the banked reset expiring {expiry} now? [y/N] "
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            consent = ""
        if consent not in ("y", "yes"):
            raise SwitchError("Reset redemption cancelled")

    def _consume_reset(
        self, number: str, record: dict[str, Any], request_id: str, credit_id: str,
    ) -> tuple[dict[str, Any], str]:
        """Consume exactly the selected card under lock, with no POST retry."""
        with FileLock(self.lock_file):
            if self._read_sequence()["accounts"].get(number) != record:
                raise SwitchError("Saved account changed; reset redemption cancelled")
            auth, _ = self._redeem_auth(number)
            from claude_swap import codex_session as cs

            custody = cs.slot_custody(self, number)
            if (custody.state not in ("none", "live", "quiescent") or custody.intent
                    or custody.invalid_records or custody.issue_count):
                raise SwitchError("Codex session state is uncertain or conflicted; reset cancelled")
            email, account_id, _ = self._identity_from_auth(auth)
            if (email.strip().casefold() != record.get("email", "").strip().casefold()
                    or account_id != record.get("accountId")):
                raise SwitchError("Codex credential identity changed; reset cancelled")
            if custody.state != "none":
                # Observe identity/generation conflicts without writing or rotating.
                try:
                    self._session_auth_snapshot(number)
                except (OSError, ValueError, TypeError, KeyError, SessionError, SwitchError):
                    raise SwitchError("Codex session credentials conflict; reset cancelled") from None
            try:
                response = consume_reset_credit(
                    auth, base_url=self._chatgpt_base_url(),
                    redeem_request_id=request_id, credit_id=credit_id,
                )
            except CodexUsageError as exc:
                if exc.status_code == 401:
                    raise CodexUsageError(
                        "Codex rejected the login (HTTP 401); nothing was redeemed. "
                        "Check the login before trying again.", status_code=401,
                    ) from exc
                raise
        return response, credit_id

    def _report_redeem(self, result: dict[str, Any], json_output: bool) -> None:
        """Report one outcome, leaving error messages to the CLI."""
        code = result["code"]
        if json_output:
            print(json.dumps(result))
        if code not in ("reset", "nothing_to_reset"):
            message = {
                "no_credit": "No reset card is available.",
                "already_redeemed": "The reset card was already redeemed.",
            }.get(code, f"Unknown reset response code: {code}")
            raise SwitchError(message)
        if json_output:
            return
        if code == "nothing_to_reset":
            print("Nothing to reset; the card was kept.")
        elif result["confirmed"]:
            print("Reset confirmed.")
            self._print_redeem_usage(result["usageAfter"])
        elif result["usageAfter"] is not None:
            warning(
                "Reset accepted, but usage does not show it yet; "
                "run 'ccswap codex usage' to check"
            )
        else:
            warning(
                "Reset accepted but could not be confirmed; run 'ccswap codex usage' to check"
            )

    def accounts_snapshot(self, fetch: set[str] | None = None) -> AccountsSnapshot:
        data = self._read_sequence()
        active = self.current_account_number()
        now = time.time()
        eligible = set(data["accounts"]) if fetch is None else fetch
        effective = {}
        entries: dict[str, UsageEntry] = {}
        plans: dict[str, str] = {}

        for number, account in data["accounts"].items():
            try:
                from claude_swap.codex_session import sessions_absent

                if sessions_absent(self):
                    effective[number] = self._effective_account_auth(
                        number, allow_refresh=True, sessions_absent=True)
                else:
                    with FileLock(self.lock_file):
                        effective[number] = self._effective_account_auth(number, allow_refresh=True)
            except LockError:
                # Another ccswap holds the store (e.g. a refresh); report, do not abort.
                effective[number] = {"auth": None, "source": "unavailable", "custody": "uncertain",
                                     "refresh": False, "base_url": DEFAULT_CHATGPT_BASE_URL,
                                     "reason": "Codex account store is busy"}
            auth = effective[number]["auth"]
            entry = self._usage_entry(
                number, auth, number in eligible, now, is_active=number == active,
                effective=effective[number]
            )
            entries[number] = entry
            # Prefer the live token's plan; fall back to what was stored at add
            # time so a missing/unreadable backup still shows its last label.
            plans[number] = (
                _plan_type_from_auth(auth) if auth is not None else ""
            ) or account.get("planType", "")

        accounts = tuple(
            AccountSnapshot(
                number=number,
                email=account.get("email", ""),
                org_name=codex_org_label(plans.get(number, "")),
                org_uuid=account.get("accountId", ""),
                is_active=number == active,
                kind="api_key" if account.get("authMode") == "api_key" else "oauth",
                switchable=(effective[number]["auth"] is not None
                            and effective[number]["custody"] in ("none", "quiescent")),
                usage=entries[number],
                disabled=bool(account.get("disabled")),
            )
            for number, account in sorted(data["accounts"].items(), key=lambda item: int(item[0]))
        )
        return AccountsSnapshot(active_number=active, accounts=accounts, taken_at=time.time())

    def _usage_entry(
        self,
        number: str,
        auth: dict[str, Any] | None,
        eligible: bool,
        now: float,
        *,
        is_active: bool,
        effective: dict[str, Any] | None = None,
    ) -> UsageEntry:
        """Return a cached or freshly fetched Codex rate-limit measurement.

        SnapshotSource controls ``eligible`` so the TUI never polls every
        account on each redraw.  The cache intentionally stays in memory: it
        contains only read-only quota measurements and is discarded when the
        process exits instead of creating another credential-adjacent file.
        """
        if effective is None:
            with FileLock(self.lock_file):
                effective = self._effective_account_auth(number, allow_refresh=True)
        auth = effective["auth"]
        previous = self._usage_cache.get(number)
        if auth is None:
            entry = UsageEntry(last_error=effective["reason"])
        elif auth.get("auth_mode") == "api_key" or (
            isinstance(auth.get("OPENAI_API_KEY"), str)
            and not isinstance(auth.get("tokens"), dict)
        ):
            entry = UsageEntry(sentinel=USAGE_API_KEY)
        elif not eligible or (previous is not None and previous.fresh(now, SERVE_TTL_S)):
            entry = previous or UsageEntry()
        else:
            try:
                usage = fetch_codex_usage(auth, base_url=effective["base_url"])
            except CodexUsageError as exc:
                # A saved inactive account has no running Codex process to
                # rotate it. Mirror Codex's own 401-recovery flow: rotate the
                # refresh token, save the whole auth document, then retry the
                # usage request. Never do this for the active account — Codex
                # owns that file and may be refreshing it concurrently.
                if exc.status_code == 401 and effective["refresh"]:
                    try:
                        refreshed_auth = self._refresh_inactive_auth(number, auth)
                        if refreshed_auth is not None:
                            usage = fetch_codex_usage(
                                refreshed_auth, base_url=effective["base_url"]
                            )
                        else:
                            raise exc
                    except (CodexUsageError, ConfigError, SwitchError, SessionError,
                            OSError) as refresh_exc:
                        entry = self._usage_failure_entry(previous, refresh_exc)
                    else:
                        entry = UsageEntry(last_good=usage, fetched_at=now)
                else:
                    entry = self._usage_failure_entry(previous, exc)
            except ConfigError as exc:
                entry = self._usage_failure_entry(previous, exc)
            else:
                entry = UsageEntry(last_good=usage, fetched_at=now)
        if entry.fetched_at is not None:
            entry = replace(entry, age_s=max(0.0, now - entry.fetched_at))
        self._usage_cache[number] = entry
        return entry

    @staticmethod
    def _usage_failure_entry(
        previous: UsageEntry | None, exc: Exception
    ) -> UsageEntry:
        return UsageEntry(
            last_good=previous.last_good if previous else None,
            fetched_at=previous.fetched_at if previous else None,
            last_error=str(exc),
            consecutive_failures=(previous.consecutive_failures + 1) if previous else 1,
        )

    def _refresh_inactive_auth(
        self, number: str, expected_auth: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Refresh one inactive backup without racing a swap.

        Refresh-token rotation invalidates the input token. The switch lock
        therefore deliberately covers this uncommon network request: a second
        ``ccswap`` process cannot activate the account between consuming its
        old refresh token and atomically saving the replacement.

        Identity alone does not decide what is safe to rotate. A login whose
        stored email no longer matches its slot is not matched by
        :meth:`_match_account_number`, so it reads as inactive even while Codex
        is using it -- and rotating the token it holds logs that session out.
        The token itself is the reliable check, so bail whenever the live
        auth.json carries the very token about to be consumed.
        """
        expected_tokens = expected_auth.get("tokens")
        expected_refresh = (
            expected_tokens.get("refresh_token")
            if isinstance(expected_tokens, dict)
            else None
        )
        if not isinstance(expected_refresh, str) or not expected_refresh:
            raise CodexUsageError("saved login has no ChatGPT refresh token")

        from claude_swap.codex_session import (
            auth_fingerprint, default_write_decision, extract_identity,
            replace_session_credentials, same_identity,
        )
        from claude_swap.exceptions import SessionError

        with FileLock(self.lock_file):
            data = self._read_sequence()
            if number not in data["accounts"]:
                return None
            try:
                # Unknown default identity must not trigger even idle reconciliation.
                self._read_switch_auth()
                self._guard_session_slot(number)
                h_auth, stored_auth, manifest = self._switch_slot_snapshot(number)
                # A reconciled rotation belongs to the newer caller, even if its
                # refresh token happens to be unchanged.
                if (stored_auth is None
                        or auth_fingerprint(stored_auth) != auth_fingerprint(expected_auth)):
                    return None
                live_auth = self._read_switch_auth()
                if live_auth is not None:
                    if self._current_account_for_auth(live_auth, data) == number:
                        return None
                    if manifest is not None:
                        live_identity, _ = extract_identity(live_auth)
                        stored_identity, _ = extract_identity(stored_auth)
                        if same_identity(live_identity, stored_identity):
                            return None
                        # A lineage member under another login owner is still this family.
                        if default_write_decision(
                            live_auth, h_auth, stored_auth, manifest
                        ) == "skip":
                            return None
                    live_tokens = live_auth.get("tokens")
                    if (isinstance(live_tokens, dict)
                            and live_tokens.get("refresh_token") == expected_refresh):
                        return None
                if manifest is not None and not (
                        auth_fingerprint(h_auth) == auth_fingerprint(stored_auth)
                        == manifest["baselineFingerprint"]):
                    return None
            except (SwitchError, ConfigError, SessionError):
                return None
            refreshed_auth = refresh_codex_auth(stored_auth)
            if manifest is not None:
                try:
                    replace_session_credentials(self, number, refreshed_auth, "refresh")
                except (OSError, SessionError):
                    # The old refresh token is already consumed: keep the new one in the
                    # backup so the next reconcile re-seeds or reports both_moved.
                    self._write_session_backup(number, refreshed_auth)
                    raise SwitchError(
                        "Codex session changed during refresh; the refreshed login was "
                        "saved to the backup and awaits reconciliation") from None
            else:
                self._write_json(self._credential_path(number), refreshed_auth)
                self._usage_cache.pop(number, None)
            return refreshed_auth
