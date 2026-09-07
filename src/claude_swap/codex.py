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
import json
import os
import sys
import tempfile
import time
import tomllib
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from claude_swap.codex_usage import (
    DEFAULT_CHATGPT_BASE_URL,
    CodexUsageError,
    fetch_codex_usage,
    refresh_codex_auth,
)
from claude_swap.exceptions import AccountNotFoundError, ConfigError, SwitchError, ValidationError
from claude_swap.json_output import USAGE_API_KEY
from claude_swap.locking import FileLock
from claude_swap.models import AccountSnapshot, AccountsSnapshot
from claude_swap.oauth import format_reset
from claude_swap.paths import get_backup_root
from claude_swap.process_detection import is_codex_running
from claude_swap.usage_store import SERVE_TTL_S, UsageEntry


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
        """Plan type for one account: the stored value, else derived live.

        Accounts saved before ``planType`` was recorded have no stored plan, so
        fall back to decoding the token in the active auth.json (for the live
        account) or the account's own backup. Keeps the label correct without
        forcing a re-add.
        """
        stored = account.get("planType")
        if isinstance(stored, str) and stored:
            return stored
        auth = (
            self._read_json(self.auth_file)
            if number == active
            else self._read_account_auth(number)
        )
        return _plan_type_from_auth(auth) if auth is not None else ""

    def _credential_path(self, number: str) -> Path:
        return self.credentials_dir / f"account-{number}.json"

    def _read_account_auth(self, number: str) -> dict[str, Any] | None:
        return self._read_json(self._credential_path(number))

    def _next_number(self, data: dict[str, Any]) -> str:
        numbers = [int(number) for number in data["accounts"] if number.isdigit()]
        return str(max(numbers, default=0) + 1)

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
            if occupant and number != existing and not assume_yes:
                answer = input(f"Overwrite Codex account slot {number}? [y/N] ").strip().lower()
                if answer not in ("y", "yes"):
                    print("Cancelled")
                    return
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
            if not assume_yes:
                answer = input(
                    f"Permanently remove Codex Account {number} ({account['email']})? [y/N] "
                ).strip().lower()
                if answer not in ("y", "yes"):
                    print("Cancelled")
                    return
            self._credential_path(number).unlink(missing_ok=True)
            del data["accounts"][number]
            if data.get("activeAccountNumber") == int(number):
                data["activeAccountNumber"] = None
            self._write_sequence(data)
        print(f"Removed Codex Account {number} ({account['email']})")

    def switch_to(self, identifier: str, json_output: bool = False, force: bool = False) -> dict[str, Any]:
        del force
        with FileLock(self.lock_file):
            data = self._read_sequence()
            number = self._resolve(identifier, data)
            target = data["accounts"][number]
            auth = self._read_account_auth(number)
            if auth is None:
                raise SwitchError(f"Codex Account {number} has no stored auth.json backup")
            original_auth = self._read_json(self.auth_file)
            if self.auth_file.exists() and original_auth is None:
                raise ConfigError("Current Codex auth.json is unreadable; refusing to overwrite it")
            current = (
                self._current_account_for_auth(original_auth, data)
                if original_auth is not None
                else None
            )
            if current == number:
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
            if original_auth is not None and current is not None:
                self._write_json(self._credential_path(current), original_auth)
            try:
                self._write_json(self.auth_file, auth)
                data["activeAccountNumber"] = int(number)
                self._write_sequence(data)
            except Exception:
                if original_auth is not None:
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
        del strategy
        data = self._read_sequence()
        numbers = sorted(data["accounts"], key=int)
        if not numbers:
            raise SwitchError("No Codex accounts are managed yet")
        current = self.current_account_number()
        start = numbers.index(current) + 1 if current in numbers else 0
        rotation = (numbers[(start + offset) % len(numbers)] for offset in range(len(numbers)))
        target = next(
            (number for number in rotation if not self._disabled_from_data(data, number)),
            None,
        )
        if target is None:
            raise SwitchError(
                "Every managed Codex account is disabled — re-enable one first"
            )
        return self.switch_to(target, json_output=json_output)

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
        if json_output:
            print(json.dumps(payload, indent=2))
        elif account:
            tag = f" [{codex_org_label(plan_type)}]" if plan_type else ""
            print(f"Codex Account {number}: {account['email']}{tag} (active)")
        else:
            print("No managed Codex account is currently active")
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
                reset_credits = account["usage"].get("reset_credits")
                if isinstance(reset_credits, dict):
                    count = reset_credits.get("available")
                    if isinstance(count, int):
                        line = f"  Banked resets: {count}"
                        expires_at = reset_credits.get("expires_at")
                        if isinstance(expires_at, str) and expires_at:
                            countdown, clock = format_reset(expires_at)
                            line += f" · earliest expires in {countdown} ({clock})"
                        print(line)
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

    def accounts_snapshot(self, fetch: set[str] | None = None) -> AccountsSnapshot:
        data = self._read_sequence()
        active = self.current_account_number()
        now = time.time()
        eligible = set(data["accounts"]) if fetch is None else fetch
        live_auth = self._read_json(self.auth_file) if active else None
        entries: dict[str, UsageEntry] = {}
        plans: dict[str, str] = {}

        for number, account in data["accounts"].items():
            auth = live_auth if number == active and live_auth is not None else self._read_account_auth(number)
            entry = self._usage_entry(
                number, auth, number in eligible, now, is_active=number == active
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
                switchable=self._read_account_auth(number) is not None,
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
    ) -> UsageEntry:
        """Return a cached or freshly fetched Codex rate-limit measurement.

        SnapshotSource controls ``eligible`` so the TUI never polls every
        account on each redraw.  The cache intentionally stays in memory: it
        contains only read-only quota measurements and is discarded when the
        process exits instead of creating another credential-adjacent file.
        """
        previous = self._usage_cache.get(number)
        if auth is None:
            entry = UsageEntry(last_error="stored Codex auth.json is missing")
        elif auth.get("auth_mode") == "api_key" or (
            isinstance(auth.get("OPENAI_API_KEY"), str)
            and not isinstance(auth.get("tokens"), dict)
        ):
            entry = UsageEntry(sentinel=USAGE_API_KEY)
        elif not eligible or (previous is not None and previous.fresh(now, SERVE_TTL_S)):
            entry = previous or UsageEntry()
        else:
            try:
                usage = fetch_codex_usage(auth, base_url=self._chatgpt_base_url())
            except CodexUsageError as exc:
                # A saved inactive account has no running Codex process to
                # rotate it. Mirror Codex's own 401-recovery flow: rotate the
                # refresh token, save the whole auth document, then retry the
                # usage request. Never do this for the active account — Codex
                # owns that file and may be refreshing it concurrently.
                if exc.status_code == 401 and not is_active:
                    try:
                        refreshed_auth = self._refresh_inactive_auth(number, auth)
                        if refreshed_auth is not None:
                            usage = fetch_codex_usage(
                                refreshed_auth, base_url=self._chatgpt_base_url()
                            )
                        else:
                            raise exc
                    except (CodexUsageError, ConfigError) as refresh_exc:
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

        with FileLock(self.lock_file):
            data = self._read_sequence()
            live_auth = self._read_json(self.auth_file)
            if (
                live_auth is not None
                and self._current_account_for_auth(live_auth, data) == number
            ):
                return None
            live_tokens = live_auth.get("tokens") if live_auth is not None else None
            if (
                isinstance(live_tokens, dict)
                and live_tokens.get("refresh_token") == expected_refresh
            ):
                return None
            stored_auth = self._read_account_auth(number)
            stored_tokens = stored_auth.get("tokens") if stored_auth else None
            stored_refresh = (
                stored_tokens.get("refresh_token")
                if isinstance(stored_tokens, dict)
                else None
            )
            # Another process updated this backup while we were fetching the
            # initial usage value. It owns the newer rotation; don't consume
            # the stale token passed to this method.
            if stored_refresh != expected_refresh:
                return None
            refreshed_auth = refresh_codex_auth(stored_auth)
            self._write_json(self._credential_path(number), refreshed_auth)
            return refreshed_auth
