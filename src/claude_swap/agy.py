"""Antigravity personal OAuth accounts, native custody, and scoped quotas."""

from __future__ import annotations

import base64
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from claude_swap import macos_keychain, paths, spawn
from claude_swap.exceptions import ClaudeSwitchError
from claude_swap.fsutil import atomic_write_text
from claude_swap.locking import FileLock
from claude_swap.models import AccountSnapshot, AccountsSnapshot
from claude_swap.usage_store import UsageEntry, UsageRecovery

_SERVICE = "gemini"
_ACCOUNT = "antigravity"
_AUTH_METHOD = "consumer"
_RELATIVE_AUTH = Path(".gemini/antigravity-cli/antigravity-oauth-token")
_SSH_KEYS = ("SSH_TTY", "SSH_CLIENT", "SSH_CONNECTION")
_USAGE_TIMEOUT = 45
_POLL_SECONDS = 60
_CLI_MISSING = (
    "Antigravity CLI is not installed. Install it from "
    "https://antigravity.google/docs/getting-started/"
)
_USAGE_ERRORS = (
    _CLI_MISSING,
    "Antigravity CLI is not installed",
    "Antigravity usage command failed",
    "Antigravity usage command failed or timed out",
    "Antigravity returned invalid quota JSON",
    "Antigravity returned invalid or unavailable quota data",
    "Antigravity usage unavailable",
)


class AgyError(ClaudeSwitchError):
    """A fixed, secret-free diagnostic suitable for every interface."""


class _RecoveryRequired(AgyError):
    def __init__(self, recovery: UsageRecovery, *, attempted: bool = True) -> None:
        self.recovery = recovery
        self.attempted = attempted
        super().__init__(recovery.instruction)


class _UsageSkipped(Exception):
    """The snapshot deadline passed before a child could start."""


def _private_dir(path: Path) -> None:
    missing = []
    current = path
    while not current.exists():
        missing.append(current)
        current = current.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o700)
    if path.is_symlink() or not path.is_dir():
        raise AgyError("Antigravity storage directory is unsafe")
    path.chmod(0o700)


def _json_file(path: Path) -> dict:
    try:
        if path.is_symlink():
            raise ValueError
        document = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            raise ValueError
        return document
    except (OSError, ValueError, UnicodeError):
        raise AgyError("Cannot read valid Antigravity account data") from None


def _encode(document: dict) -> str:
    return json.dumps(document, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class Credential:
    document: dict
    email: str

    @classmethod
    def parse(cls, document: dict) -> Credential:
        token = document.get("token")
        email = document.get("id_token_email")
        if (
            document.get("auth_method") != _AUTH_METHOD
            or not isinstance(email, str)
            or "@" not in email
            or any(ord(char) < 32 for char in email)
            or not isinstance(token, dict)
            or not isinstance(token.get("access_token"), str)
            or not token["access_token"]
            or not isinstance(token.get("refresh_token"), str)
            or not token["refresh_token"]
            or any(document.get(key) for key in ("wif_provider", "saved_wif_provider", "project_id", "region"))
        ):
            raise AgyError("Only personal Google OAuth Antigravity accounts are supported")
        expiry = token.get("expiry")
        if expiry is not None:
            try:
                if not isinstance(expiry, str):
                    raise ValueError
                parsed = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    raise ValueError
            except ValueError:
                raise AgyError("Antigravity credentials have an invalid expiry") from None
        return cls(document, email.strip().lower())


@dataclass(frozen=True)
class NativeLogin:
    backend: str
    credential: Credential | None


def parse_usage(document: dict) -> dict:
    """Keep every group and bucket; never infer an account-wide quota."""
    try:
        command = document["command"]
        if document["status"] != "SUCCESS" or command["name"] != "usage":
            raise ValueError
        groups = command["data"]["groups"]
        if not isinstance(groups, list) or not groups:
            raise ValueError
        windows = []
        for group in groups:
            name, buckets = group["name"], group["buckets"]
            if not isinstance(name, str) or not name or not isinstance(buckets, list):
                raise ValueError
            for bucket in buckets:
                fraction = bucket["remaining_fraction"]
                window, reset = bucket["window"], bucket["reset_time"]
                bucket_id, bucket_name = bucket["id"], bucket["name"]
                if (
                    isinstance(fraction, bool)
                    or not isinstance(fraction, (int, float))
                    or not math.isfinite(fraction)
                    or not 0 <= fraction <= 1
                    or window not in ("weekly", "5h")
                    or not isinstance(bucket_id, str)
                    or not isinstance(bucket_name, str)
                    or not isinstance(reset, str)
                ):
                    raise ValueError
                parsed_reset = datetime.fromisoformat(reset.replace("Z", "+00:00"))
                if parsed_reset.tzinfo is None:
                    raise ValueError
                windows.append({
                    "name": f"{name} · {window}",
                    "group": name,
                    "bucket_id": bucket_id,
                    "bucket_name": bucket_name,
                    "window": window,
                    "pct": (1 - fraction) * 100,
                    "resets_at": reset,
                })
        if not windows:
            raise ValueError
        return {"scoped": windows}
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        raise AgyError("Antigravity returned invalid or unavailable quota data") from None


class AgyAccountSwitcher:
    """Serialize only Antigravity operations; external writers remain independent."""

    def __init__(self, debug: bool = False) -> None:
        del debug
        self.backup_dir = paths.get_backup_root() / "agy"
        self.sequence_file = self.backup_dir / "accounts.json"
        self.lock_file = self.backup_dir / ".lock"

    def _lock(self) -> FileLock:
        _private_dir(self.backup_dir)
        if self.lock_file.is_symlink():
            raise AgyError("Antigravity lock path is unsafe")
        descriptor = os.open(self.lock_file, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(descriptor)
        self.lock_file.chmod(0o600)
        return FileLock(self.lock_file, timeout=_USAGE_TIMEOUT + 10)

    def _roster(self) -> dict:
        if not self.sequence_file.exists() and not self.sequence_file.is_symlink():
            return {"version": 1, "accounts": {}}
        data = _json_file(self.sequence_file)
        accounts = data.get("accounts")
        if type(data.get("version")) is not int or data["version"] != 1 or not isinstance(accounts, dict):
            raise AgyError("Invalid Antigravity account roster")
        emails = set()
        for number, account in accounts.items():
            if (
                not isinstance(number, str) or not number.isascii() or not number.isdigit() or len(number) > 9
                or str(int(number)) != number or int(number) < 1
                or not isinstance(account, dict)
                or not isinstance(account.get("email"), str)
                or "@" not in account["email"]
                or any(ord(char) < 32 for char in account["email"])
                or account["email"] != account["email"].strip().lower()
                or account.get("authMethod") != _AUTH_METHOD
                or account["email"] in emails
            ):
                raise AgyError("Invalid Antigravity account roster")
            emails.add(account["email"])
        return data

    def _credential_path(self, number: str) -> Path:
        return self.backup_dir / f"account-{number}.json"

    def _saved(self, number: str, data: dict) -> Credential:
        credential = Credential.parse(_json_file(self._credential_path(number)))
        if credential.email != data["accounts"][number]["email"]:
            raise AgyError("Antigravity saved account identity changed")
        return credential

    def _write(self, path: Path, document: dict) -> None:
        try:
            _private_dir(path.parent)
            atomic_write_text(path, _encode(document), follow_symlinks=False)
        except OSError:
            raise AgyError("Cannot save Antigravity account data") from None

    def _capture(self, data: dict, credential: Credential) -> str:
        number = self._matching(data, credential)
        if number is None:
            number = str(max(map(int, data["accounts"]), default=0) + 1)
        self._write(self._credential_path(number), credential.document)
        data["accounts"][number] = {"email": credential.email, "authMethod": _AUTH_METHOD}
        self._write(self.sequence_file, data)
        return number

    @staticmethod
    def _matching(data: dict, credential: Credential | None) -> str | None:
        if credential is None:
            return None
        return next((number for number, account in data["accounts"].items()
                     if account["email"] == credential.email), None)

    def _native(self) -> NativeLogin:
        if os.environ.get("AGY_ADC_AUTH", "").lower() == "true":
            raise AgyError("Antigravity ADC authentication is not supported for account switching")
        if "ANTIGRAVITY_GEMINI_DIR" in os.environ:
            raise AgyError("Antigravity config-root overrides are not supported")
        fallback = Path.home() / _RELATIVE_AUTH
        settings = fallback.parent / "settings.json"
        if settings.exists() or settings.is_symlink():
            if _json_file(settings).get("modelProvider") == "gemini":
                raise AgyError("Antigravity Gemini API-key mode is not supported for account switching")
        if fallback.is_symlink():
            raise AgyError("Antigravity fallback credential path is unsafe")
        if any(os.environ.get(key) for key in _SSH_KEYS):
            credential = Credential.parse(_json_file(fallback)) if fallback.exists() else None
            return NativeLogin("file", credential)
        if sys.platform != "darwin":
            raise AgyError("Native Antigravity switching requires macOS Keychain or an SSH file backend")
        marker = fallback.parent / "cache/antigravity-keyring-unavailable"
        if marker.exists():
            raise AgyError("Antigravity native backend is ambiguous; keyring-unavailable marker exists")
        try:
            raw = macos_keychain.get_password(_SERVICE, _ACCOUNT)
        except macos_keychain.KEYCHAIN_ERRORS:
            raise AgyError("Cannot read Antigravity native Keychain") from None
        credential = None
        if raw is not None:
            try:
                # Antigravity's Go keyring client wraps macOS values in prefixed base64.
                raw = raw.strip()
                prefix = "go-keyring-base64:"
                if raw.startswith(prefix):
                    raw = base64.b64decode(raw[len(prefix):], validate=True).decode("utf-8")
                document = json.loads(raw)
                if not isinstance(document, dict):
                    raise ValueError
            except (ValueError, TypeError):
                raise AgyError("Invalid Antigravity native credentials") from None
            credential = Credential.parse(document)
        if fallback.exists():
            file_credential = Credential.parse(_json_file(fallback))
            if credential is None or file_credential.document != credential.document:
                raise AgyError("Antigravity Keychain and fallback file conflict")
            # A stale duplicate would become active if the Keychain later failed.
            raise AgyError("Antigravity has both Keychain and fallback credentials; backend is ambiguous")
        return NativeLogin("keychain", credential)

    @staticmethod
    def _preflight(login: NativeLogin, target: Credential) -> None:
        if login.backend == "keychain":
            command = (f'add-generic-password -U -a "{_ACCOUNT}" -s "{_SERVICE}" '
                       f'-X {_encode(target.document).encode("utf-8").hex()}\n')
            if len(command.encode("utf-8")) > macos_keychain.SECURITY_STDIN_LINE_LIMIT:
                raise AgyError("Antigravity credentials exceed the safe Keychain write size")

    def _activate(self, original: NativeLogin, target: Credential) -> None:
        if self._native() != original:
            raise AgyError("Antigravity native login changed externally; switch cancelled")
        failed = False
        try:
            if original.backend == "file":
                self._write(Path.home() / _RELATIVE_AUTH, target.document)
            else:
                macos_keychain.set_password(_SERVICE, _ACCOUNT, _encode(target.document))
        except (AgyError, *macos_keychain.KEYCHAIN_ERRORS):
            failed = True
        try:
            observed = self._native()
        except AgyError:
            raise AgyError("Antigravity write could not be verified; native state was not overwritten again") from None
        if observed == NativeLogin(original.backend, target):
            return
        if observed != original:
            raise AgyError("Antigravity native login changed during write; external state was preserved")
        message = "Antigravity native write failed" if failed else "Antigravity native write did not persist"
        raise AgyError(message)

    @staticmethod
    def _resolve(identifier: str, data: dict) -> str:
        if identifier in data["accounts"]:
            return identifier
        matches = [number for number, account in data["accounts"].items()
                   if account["email"].lower() == identifier.lower()]
        if len(matches) != 1:
            raise AgyError("No unique saved Antigravity account matches that target")
        return matches[0]

    def add_account(self) -> None:
        with self._lock():
            data, login = self._roster(), self._native()
            if login.credential is None:
                raise AgyError("No Antigravity login found; sign in with agy first")
            number = self._capture(data, login.credential)
            cached = self._usage_cache(number)
            if cached["recovery"] is not None:
                cached.update(recovery=None, error=None, attemptedAt=None)
                self._write(self.backup_dir / f"usage-{number}.json", cached)
        print(f"Saved Antigravity account {number}: {login.credential.email}")

    def switch_to(self, identifier: str, json_output: bool = False) -> dict:
        with self._lock():
            data = self._roster()
            number = self._resolve(identifier, data)
            recovery = self._usage_cache(number)["recovery"]
            if recovery is not None:
                raise _RecoveryRequired(UsageRecovery(**recovery))
            target = self._saved(number, data)
            original = self._native()
            previous = self._matching(data, original.credential)
            if previous == number:
                target = original.credential
            self._preflight(original, target)
            if original.credential is not None:
                self._capture(data, original.credential)
            self._activate(original, target)
        payload = {"provider": "agy", "switched": True,
                   "from": {"number": int(previous) if previous else None,
                            "email": original.credential.email if original.credential else None},
                   "to": {"number": int(number), "email": target.email}, "reason": "requested",
                   "note": "Shared Antigravity login changed. Restart existing CLI, App, or IDE sessions to use it."}
        if not json_output:
            print(f"Switched Antigravity to account {number}: {target.email}")
            print(payload["note"])
        return payload

    def remove_account(self, identifier: str, assume_yes: bool = False) -> None:
        with self._lock():
            data = self._roster()
            number = self._resolve(identifier, data)
            if not assume_yes and input(f"Remove saved Antigravity account {number}? [y/N] ").lower() != "y":
                return
            del data["accounts"][number]
            self._write(self.sequence_file, data)
            for path in (self._credential_path(number), self.backup_dir / f"usage-{number}.json"):
                path.unlink(missing_ok=True)
        print(f"Removed saved Antigravity account {number}")

    def _list(self, data: dict) -> dict:
        login = self._native() if data["accounts"] else None
        active = self._matching(data, login.credential) if login else None
        accounts = []
        for number, account in sorted(data["accounts"].items(), key=lambda pair: int(pair[0])):
            self._saved(number, data)
            accounts.append({"number": int(number), "email": account["email"],
                             "authMode": _AUTH_METHOD, "active": number == active})
        return {"provider": "agy", "activeAccountNumber": int(active) if active else None, "accounts": accounts}

    def list_payload(self) -> dict:
        snapshot = self.accounts_snapshot()
        return {"provider": "agy", "activeAccountNumber": int(snapshot.active_number) if snapshot.active_number else None,
                "accounts": [{"number": int(account.number), "email": account.email,
                              "authMode": _AUTH_METHOD, "active": account.is_active,
                              "usage": account.usage.last_good, "error": account.usage.last_error}
                             for account in snapshot.accounts]}

    def list_accounts(self, json_output: bool = False) -> dict:
        payload = self.list_payload()
        if json_output:
            print(json.dumps(payload, indent=2))
        elif not payload["accounts"]:
            print("No managed Antigravity accounts")
        else:
            for account in payload["accounts"]:
                marker = " ● active" if account["active"] else ""
                print(f"{account['number']:>2}  {account['email']}  [Antigravity]{marker}")
                for window in (account["usage"] or {}).get("scoped", []):
                    print(f"    {window['name']}: {window['pct']:.0f}% used")
                if account["error"]:
                    print(f"    {account['error']}")
        return payload

    def status(self, json_output: bool = False) -> dict:
        if self.sequence_file.exists():
            with self._lock():
                listing = self._list(self._roster())
        else:
            listing = self._list(self._roster())
        active = next((account for account in listing["accounts"] if account["active"]), None)
        payload = {"provider": "agy", "activeAccountNumber": listing["activeAccountNumber"],
                   "email": active["email"] if active else None}
        if json_output:
            print(json.dumps(payload, indent=2))
        else:
            print(f"Active Antigravity: {active['email']}" if active else "No managed Antigravity account is active")
        return payload

    def _persist_refresh(self, number: str, baseline: Credential, refreshed: Credential) -> None:
        if refreshed.email != baseline.email:
            raise AgyError("Antigravity usage returned a different account identity")
        path = self._credential_path(number)
        current = Credential.parse(_json_file(path))
        if current.document != baseline.document:
            if current.document == refreshed.document:
                return
            recovery = self.backup_dir / f"refresh-conflict-{number}-{time.time_ns()}.json"
            self._write(recovery, refreshed.document)
            raise _RecoveryRequired(UsageRecovery("refresh-conflict", str(recovery.absolute())))
        self._write(path, refreshed.document)

    def _invoke_usage(self, number: str, credential: Credential, active: bool,
                      timeout: float = _USAGE_TIMEOUT, *, deadline: float | None = None) -> dict:
        if deadline is None:
            deadline = time.monotonic() + timeout
        cached = self._usage_cache(number)
        if cached["recovery"] is not None:
            raise _RecoveryRequired(UsageRecovery(**cached["recovery"]))
        binary = shutil.which("agy")
        if binary is None:
            raise AgyError(_CLI_MISSING)
        binary = str(Path(binary).absolute())
        directory = tempfile.TemporaryDirectory(prefix="ccswap-agy-", delete=False)
        home = Path(directory.name)
        auth_path = home / _RELATIVE_AUTH
        pending = UsageRecovery("refresh-persistence", str(auth_path.absolute()))
        retain_state = False
        try:
            _private_dir(home)
            self._write(auth_path, credential.document)
            env = {key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL", "SYSTEMROOT", "WINDIR", "TMPDIR") if key in os.environ}
            if active:
                env["HOME"] = str(Path.home())
                for key in _SSH_KEYS:
                    if os.environ.get(key):
                        env[key] = os.environ[key]
            else:
                env.update(HOME=str(home), SSH_TTY="/dev/ccswap-usage")
            env["USERPROFILE"] = env["HOME"]
            if deadline <= time.monotonic():
                raise _UsageSkipped
            # The child may rotate its refresh token before returning or crashing.
            # Persist its private recovery location before granting it that chance.
            journal = dict(cached, recovery={"kind": pending.kind, "path": pending.path})
            self._write(self.backup_dir / f"usage-{number}.json", journal)
            retain_state = True
            # Float rounding can push this past the budget when a coarse clock hasn't ticked.
            timeout = min(deadline - time.monotonic(), timeout)
            if timeout <= 0:
                try:
                    self._write(self.backup_dir / f"usage-{number}.json", cached)
                except (AgyError, OSError):
                    raise _RecoveryRequired(pending, attempted=False) from None
                retain_state = False
                raise _UsageSkipped
            result = None
            failure = None
            try:
                result = spawn.run([binary, "-p", "/usage", "--output-format", "json"],
                                   cwd=home, env=env, capture_output=True, text=True,
                                   stdin=subprocess.DEVNULL, timeout=timeout)
            except (OSError, subprocess.SubprocessError):
                failure = "Antigravity usage command failed or timed out"
            finally:
                try:
                    refreshed = (self._native().credential if active else
                                 Credential.parse(_json_file(auth_path)))
                    if refreshed is None:
                        raise AgyError("Antigravity login disappeared during usage refresh")
                    if active:
                        self._write(auth_path, refreshed.document)
                    self._persist_refresh(number, credential, refreshed)
                except _RecoveryRequired as exc:
                    try:
                        self._record_recovery(number, dict(cached), exc.recovery)
                    except (AgyError, OSError):
                        raise _RecoveryRequired(pending) from None
                    retain_state = False
                    raise
                except (AgyError, OSError):
                    raise _RecoveryRequired(pending) from None
                try:
                    self._write(self.backup_dir / f"usage-{number}.json", cached)
                except (AgyError, OSError):
                    raise _RecoveryRequired(pending) from None
                retain_state = False
            if failure:
                raise AgyError(failure)
            if result is None or result.returncode != 0:
                raise AgyError("Antigravity usage command failed")
            try:
                document = json.loads(result.stdout)
            except (ValueError, TypeError):
                raise AgyError("Antigravity returned invalid quota JSON") from None
            return parse_usage(document)
        finally:
            if not retain_state:
                try:
                    directory.cleanup()
                except OSError:
                    # Saved auth or a durable conflict copy already owns custody.
                    pass

    def _usage_cache(self, number: str) -> dict:
        path = self.backup_dir / f"usage-{number}.json"
        cached = _json_file(path) if path.exists() else {}
        # Caches written before recovery state existed remain valid.
        cached.setdefault("recovery", None)
        self._validate_cache(cached)
        return cached

    def _record_recovery(self, number: str, cached: dict, recovery: UsageRecovery) -> None:
        cached.update(recovery={"kind": recovery.kind, "path": recovery.path},
                      error="Antigravity usage unavailable")
        self._write(self.backup_dir / f"usage-{number}.json", cached)

    def _usage_entry(self, number: str, cached: dict, credential: Credential | None,
                     active: bool, deadline: float | None) -> UsageEntry:
        now = time.time()
        fetched = cached.get("fetchedAt")
        attempted = cached.get("attemptedAt")
        diagnostic = cached.get("error")
        if (credential is not None and cached["recovery"] is None and deadline is not None
                and deadline > time.monotonic()
                and (attempted is None or now - attempted >= _POLL_SECONDS)):
            attempted_fetch = True
            try:
                cached["lastGood"] = self._invoke_usage(number, credential, active, deadline=deadline)
                cached["fetchedAt"] = now
                cached["error"] = None
                diagnostic = None
            except _RecoveryRequired as exc:
                cached["recovery"] = {"kind": exc.recovery.kind, "path": exc.recovery.path}
                attempted_fetch = exc.attempted
                if attempted_fetch:
                    cached["error"] = "Antigravity usage unavailable"
            except _UsageSkipped:
                return self._usage_entry(number, cached, None, active, None)
            except AgyError as exc:
                diagnostic = str(exc)
                cached["error"] = diagnostic if diagnostic in _USAGE_ERRORS else "Antigravity usage unavailable"
            if attempted_fetch:
                cached["attemptedAt"] = now
            try:
                self._write(self.backup_dir / f"usage-{number}.json", cached)
            except (AgyError, OSError):
                if cached["recovery"] is None:
                    raise
            fetched = cached.get("fetchedAt")
        recovery = UsageRecovery(**cached["recovery"]) if cached["recovery"] is not None else None
        if recovery is not None:
            diagnostic = recovery.instruction
        return UsageEntry(last_good=cached.get("lastGood"), fetched_at=fetched,
                          age_s=now - fetched if isinstance(fetched, (int, float)) else None,
                          last_attempt_at=cached.get("attemptedAt"), last_error=diagnostic,
                          recovery=recovery)

    @staticmethod
    def _validate_cache(cached: dict) -> None:
        try:
            for key in ("fetchedAt", "attemptedAt"):
                value = cached.get(key)
                if value is not None and (type(value) not in (int, float) or not math.isfinite(value) or value < 0):
                    raise ValueError
            if cached.get("error") is not None and cached["error"] not in _USAGE_ERRORS:
                raise ValueError
            recovery = cached.get("recovery")
            if recovery is not None:
                if (not isinstance(recovery, dict) or set(recovery) != {"kind", "path"}
                        or recovery["kind"] not in ("refresh-conflict", "refresh-persistence")
                        or not isinstance(recovery["path"], str)
                        or not Path(recovery["path"]).is_absolute()
                        or any(ord(char) < 32 or ord(char) == 127 for char in recovery["path"])):
                    raise ValueError
            last_good = cached.get("lastGood")
            if last_good is None:
                return
            windows = last_good["scoped"]
            if not isinstance(windows, list) or not windows:
                raise ValueError
            for window in windows:
                pct = window["pct"]
                if type(pct) not in (int, float) or not math.isfinite(pct) or not 0 <= pct <= 100:
                    raise ValueError
                if window["window"] not in ("weekly", "5h"):
                    raise ValueError
                if any(not isinstance(window[key], str) for key in ("name", "group", "bucket_id", "bucket_name", "resets_at")):
                    raise ValueError
                if datetime.fromisoformat(window["resets_at"].replace("Z", "+00:00")).tzinfo is None:
                    raise ValueError
        except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
            raise AgyError("Invalid Antigravity usage cache") from None

    def accounts_snapshot(self, fetch: set[str] | None = None) -> AccountsSnapshot:
        if not self.sequence_file.exists() and not self.sequence_file.is_symlink():
            return AccountsSnapshot(None, (), time.time())
        with self._lock():
            deadline = time.monotonic() + _USAGE_TIMEOUT
            data = self._roster()
            login = self._native() if data["accounts"] else None
            active = self._matching(data, login.credential) if login else None
            accounts = []
            caches = {number: self._usage_cache(number) for number in data["accounts"]}
            ordered = sorted(data["accounts"], key=lambda number:
                             (caches[number].get("attemptedAt") is not None,
                              caches[number].get("attemptedAt") or 0, int(number)))
            for number in ordered:
                account, cached = data["accounts"][number], caches[number]
                credential = None
                if cached["recovery"] is None:
                    saved = self._saved(number, data)
                    credential = login.credential if number == active and login else saved
                    if credential != saved:
                        try:
                            self._persist_refresh(number, saved, credential)
                        except _RecoveryRequired as exc:
                            self._record_recovery(number, cached, exc.recovery)
                selected_deadline = deadline if fetch is None or number in fetch else None
                entry = self._usage_entry(number, cached, credential, number == active, selected_deadline)
                accounts.append(AccountSnapshot(number, account["email"], "Antigravity", "", number == active,
                                                "oauth", entry.recovery is None, entry))
            return AccountsSnapshot(active, tuple(sorted(accounts, key=lambda account: int(account.number))), time.time())

    def usage_status(self, json_output: bool = False) -> dict:
        snapshot = self.accounts_snapshot()
        payload = {"provider": "agy", "accounts": [
            {"number": int(account.number), "email": account.email, "active": account.is_active,
             "usage": account.usage.last_good, "error": account.usage.last_error}
            for account in snapshot.accounts]}
        if json_output:
            print(json.dumps(payload, indent=2))
        elif not snapshot.accounts:
            print("No managed Antigravity accounts")
        else:
            for account in snapshot.accounts:
                print(f"{account.number}  {account.email}")
                for window in (account.usage.last_good or {}).get("scoped", []):
                    print(f"  {window['name']}: {window['pct']:.0f}% used; resets {window['resets_at']}")
                if account.usage.last_error:
                    print(f"  {account.usage.last_error}")
        return payload
