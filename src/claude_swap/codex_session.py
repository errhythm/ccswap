"""Codex session custody and internal terminal supervision."""

from __future__ import annotations

import base64
import ctypes
import errno
import hashlib
import json
import ntpath
import os
import re
import shutil
import stat
import sys
import tempfile
import time
import tomllib
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

from claude_swap.exceptions import SessionError
from claude_swap.fsutil import atomic_write_bytes, atomic_write_text, replace_with_retry
from claude_swap.session import slugify_email

if TYPE_CHECKING:
    from claude_swap.codex import CodexAccountSwitcher


def _require(condition: bool) -> None:
    if not condition:
        raise SessionError("Malformed session data.")


def _text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip()) and "\x00" not in value


def _decimal(value: object, *, positive: bool = False) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9]+", value) is not None and (
        not positive or any(character != "0" for character in value)
    )


def _absolute(value: object) -> bool:
    return _text(value) and os.path.isabs(value) and os.path.normpath(value) == value


def _hex(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _object(value: object, fields: tuple[str, ...]) -> dict[str, Any]:
    _require(isinstance(value, dict) and set(fields) <= value.keys())
    return value


def validate_identity(value: object) -> dict[str, Any]:
    """Validate the normalized identity stored in session coordination records."""
    data = _object(value, ("userId", "workspaceId", "email", "loginOwnerId"))
    _require(all(_text(data[field]) for field in ("userId", "workspaceId", "email")))
    _require(all(data[field] == data[field].strip() for field in ("userId", "workspaceId")))
    _require(data["email"] == data["email"].strip().casefold())
    owner = data["loginOwnerId"]
    _require(owner is None or (_text(owner) and owner == owner.strip()))
    return data


def same_identity(left: object, right: object) -> bool:
    """Compare custody, allowing legacy absence of the optional login owner."""
    left_identity = validate_identity(left)
    right_identity = validate_identity(right)
    return all(
        left_identity[field] == right_identity[field]
        for field in ("userId", "workspaceId")
    ) and (
        left_identity["loginOwnerId"] is None
        or right_identity["loginOwnerId"] is None
        or left_identity["loginOwnerId"] == right_identity["loginOwnerId"]
    )


def _json(text: str) -> Any:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result = dict(items)
        _require(len(result) == len(items))
        return result

    return json.loads(text, object_pairs_hook=pairs, parse_constant=lambda _: _require(False))


def extract_identity(auth: object) -> tuple[dict[str, Any], str | None]:
    """Decode local labels, without claiming JWT signature verification."""
    try:
        data = _object(auth, ("tokens",))
        _require(data.get("auth_mode") in (None, "chatgpt"))
        _require(all(
            data.get(field) is None
            for field in (
                "OPENAI_API_KEY",
                "personal_access_token",
                "agent_identity",
                "bedrock_api_key",
                "bedrock_access_keys",
            )
        ))
        tokens = _object(
            data["tokens"], ("id_token", "access_token", "refresh_token", "account_id"),
        )
        _require(all(
            _text(tokens[field]) for field in ("id_token", "access_token", "refresh_token")
        ))
        if "last_refresh" in data:
            _refresh_timestamp(data["last_refresh"])
        if "oauth_provenance" in tokens:
            provenance = _object(tokens["oauth_provenance"], ("issuer", "client_id"))
            _require(all(isinstance(provenance[field], str) for field in ("issuer", "client_id")))
        pieces = tokens["id_token"].split(".")
        _require(len(pieces) == 3 and all(
            re.fullmatch(r"[A-Za-z0-9_-]+", piece) for piece in pieces
        ))
        decoded_payload = base64.b64decode(
            pieces[1] + "=" * (-len(pieces[1]) % 4),
            altchars=b"-_",
            validate=True,
        )
        _require(base64.urlsafe_b64encode(decoded_payload).decode().rstrip("=") == pieces[1])
        claims = _object(_json(decoded_payload.decode("utf-8")), ("https://api.openai.com/auth",))
        nested = _object(
            claims["https://api.openai.com/auth"],
            ("chatgpt_account_id",),
        )
        _require(nested["chatgpt_account_id"] == tokens["account_id"])
        user_id = nested.get("chatgpt_user_id")
        fallback_user = nested.get("user_id")
        _require(user_id is None or fallback_user is None or user_id == fallback_user)
        profile = claims.get("https://api.openai.com/profile")
        profile_email = _object(profile, ()).get("email") if profile is not None else None
        email = claims.get("email")
        _require(email is None or _text(email))
        _require(profile_email is None or _text(profile_email))
        _require(email is None or profile_email is None
                 or email.strip().casefold() == profile_email.strip().casefold())
        email = email if email is not None else profile_email
        _require(_text(email))
        identity = validate_identity(dict(
            userId=user_id if user_id is not None else fallback_user,
            workspaceId=nested["chatgpt_account_id"],
            email=email.strip().casefold(),
            loginOwnerId=tokens.get("login_owner_id"),
        ))
        plan = nested.get("chatgpt_plan_type")
        _require(plan is None or _text(plan))
        return identity, plan
    except (ValueError, TypeError, UnicodeError, RecursionError, SessionError):
        raise SessionError("Invalid ChatGPT session identity.") from None


def session_plan_supported(plan: object) -> bool:
    """Accept the known complement of the plans that receive cloud-managed configuration."""
    return isinstance(plan, str) and plan in {
        "free",
        "go",
        "plus",
        "pro",
        "prolite",
        "promax",
        "team",
        "self_serve_business_prolite",
        "self_serve_business_usage_based",
    }


def auth_fingerprint(auth: object) -> str:
    """Hash the complete semantic document; never discard unknown auth fields."""
    try:
        _require(isinstance(auth, dict))
        encoded = json.dumps(
            auth,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise SessionError("Malformed auth document.") from None


def _process(value: object) -> None:
    data = _object(value, ("pid", "start"))
    _require(type(data["pid"]) is int and 0 < data["pid"] <= 2**32 - 1)
    start = _object(data["start"], ("kind",))
    kind = start["kind"]
    _require(kind in ("linux", "macos", "windows"))
    field = {"linux": "ticks", "macos": "uniqueId", "windows": "creationTime100ns"}[kind]
    _require(_decimal(start.get(field)))
    if kind != "windows":
        _require(_text(start.get("bootId")))


def validate_record(value: object, kind: str, *, run_id: str | None = None) -> dict[str, Any]:
    """Validate schema structure; filesystem ownership/alias checks belong to transactions."""
    data = _object(value, ("schemaVersion", "home"))
    _require(type(data["schemaVersion"]) is int and data["schemaVersion"] == 1)
    _require(_absolute(data["home"]))
    if kind in ("manifest", "stale"):
        _object(data, ("accountNumber", "identity"))
        _require(_decimal(data["accountNumber"], positive=True)
                 and not data["accountNumber"].startswith("0"))
        validate_identity(data["identity"])
        if kind == "stale":
            _object(data, ("oldFingerprint", "newFingerprint", "reason"))
            _require(_hex(data["oldFingerprint"]) and _hex(data["newFingerprint"]))
            _require(data["reason"] in ("replacement", "refresh"))
        else:
            _object(
                data,
                ("sourceHome", "baselineFingerprint", "lineage", "bootstrapState", "sharedItems"),
            )
            _require(_absolute(data["sourceHome"]) and _hex(data["baselineFingerprint"]))
            lineage = data["lineage"]
            _require(isinstance(lineage, list) and 1 <= len(lineage) <= 64
                     and all(_hex(item) for item in lineage)
                     and lineage[-1] == data["baselineFingerprint"])
            _require(data["bootstrapState"] in ("ready", "preparing"))
            _require(isinstance(data["sharedItems"], dict))
            for destination, item in data["sharedItems"].items():
                _require(
                    _text(destination)
                    and not os.path.isabs(destination)
                    and "\\" not in destination
                    and all(part not in ("", ".", "..") for part in destination.split("/"))
                )
                _require(destination in (
                    "config.toml", "AGENTS.md", "AGENTS.override.md", "rules", "prompts",
                ) or (destination.startswith("skills/") and destination.count("/") == 1
                      and destination != "skills/.system"))
                _object(item, ("source", "mode"))
                _require(_absolute(item["source"]) and item["mode"] in ("symlink", "copy"))
    elif kind == "run":
        _object(data, ("runId", "supervisor", "child", "phase", "shared", "jobName", "exitCode"))
        try:
            run_uuid = UUID(data["runId"])
            _require(run_uuid.version == 4 and str(run_uuid) == data["runId"] == run_id)
        except (ValueError, TypeError, AttributeError):
            raise SessionError("Malformed session data.") from None
        _process(data["supervisor"])
        if data["child"] is not None:
            _process(data["child"])
        _require(data["phase"] in ("starting", "running", "waiting", "finished"))
        _require(type(data["shared"]) is bool)
        exit_code = data["exitCode"]
        _require(exit_code is None or (
            type(exit_code) is int and -(2**31) <= exit_code <= 2**32 - 1
        ))
        windows = data["supervisor"]["start"]["kind"] == "windows"
        _require(data["jobName"] == ("Local\\ccswap-codex-" + run_id if windows else None))
    else:
        raise SessionError("Unknown session record kind.")
    return data


def load_document(path: Path) -> dict[str, Any] | None:
    """Read only: missing returns None; unreadable and malformed remain distinct errors."""
    try:
        _ordinary(path)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        if not hasattr(os, "O_NOFOLLOW"):
            _require(not path.is_symlink())
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError as error:
        if error.errno == errno.ELOOP or path.is_dir() or path.is_symlink():
            raise SessionError("Malformed session record.") from None
        raise SessionError("Unreadable session record.") from None
    try:
        _require(stat.S_ISREG(os.fstat(descriptor).st_mode))
        with os.fdopen(descriptor, "r", encoding="utf-8", closefd=False) as stream:
            return _object(_json(stream.read()), ())
    except OSError:
        raise SessionError("Unreadable session record.") from None
    except (ValueError, TypeError, UnicodeError, RecursionError, SessionError):
        raise SessionError("Malformed session record.") from None
    finally:
        os.close(descriptor)


def load_record(path: Path, kind: str) -> dict[str, Any] | None:
    """Strictly read and validate a coordination record."""
    data = load_document(path)
    return None if data is None else validate_record(data, kind, run_id=path.stem)


@dataclass(frozen=True)
class Custody:
    """Fixed custody reasons; counts and PIDs are diagnostic only."""

    state: str
    reason: str
    pids: tuple[int, ...] = ()
    invalid_records: int = 0
    issue_count: int = 0
    intent: bool = False


def sessions_absent(switcher: CodexAccountSwitcher) -> bool:
    """Prove absence for lock-free observations only; writers must check under lock.

    Links, malformed parents and access failures cannot establish absence.
    A concurrent creator is serialized by the independent writer guards.
    """
    try:
        for path in (switcher.backup_dir, switcher.backup_dir / "codex",
                     switcher.backup_dir / "codex" / "sessions"):
            if not _ordinary(path, directory=True):
                return True
    except (OSError, SessionError):
        pass
    return False


def slot_home(switcher: CodexAccountSwitcher, number: str) -> Path:
    """Locate a slot from current metadata, without reading credentials."""
    _require(_decimal(number, positive=True) and not number.startswith("0"))
    sequence = load_document(switcher.sequence_file)
    slot = sequence["accounts"][number]
    _require(_text(slot["email"]))
    return switcher.backup_dir / "codex" / "sessions" / (
        f"{number}-{slugify_email(slot['email'].strip().casefold())}"
    )


def slot_custody(switcher: CodexAccountSwitcher, number: str, *,
                 completed: dict[str, Any] | None = None) -> Custody:
    """Observe custody under caller-held R/.lock; never refresh or launch.

    A7 path checks exclude hostile same-uid swaps. A8 attribution and residual
    orphan limitations are implemented by scan_codex_home. An existing session
    with a missing default home yields conflict/invalid_home.
    """
    from claude_swap import process_detection, windows_job

    invalid, records = 0, []
    try:
        home = slot_home(switcher, number)
        parent = switcher.backup_dir
        for part in home.parent.relative_to(parent).parts:
            parent = parent / part
            if not _ordinary(parent, directory=True):
                return Custody("none", "no_session_home")
        if any(path != home and path.name.startswith(f"{number}-")
               for path in home.parent.iterdir()):
            return Custody("conflict", "retained_home")
        intent = _sharing_exists(home.parent / f".{home.name}.stale.json")
        if not _ordinary(home, directory=True):
            return (
                Custody("conflict", "missing_home") if intent
                else Custody("none", "no_session_home")
            )
        _checked_parents(home, switcher.backup_dir)
        _owned(home)
        manifest = load_record(home / ".ccswap-session.json", "manifest")
        # Bootstrap resolves path casing; native equality keeps Windows homes valid
        # without resolving aliases past the ordinary-directory checks above.
        _require(manifest is not None and Path(manifest["home"]) == home
                 and manifest["accountNumber"] == number
                 and manifest["sourceHome"] == str(switcher.codex_home.resolve(strict=True))
                 and manifest["bootstrapState"] == "ready")
        slot = load_document(switcher.sequence_file)["accounts"][number]
        _require(slot["accountId"] == manifest["identity"]["workspaceId"]
                 and slot["email"].strip().casefold() == manifest["identity"]["email"]
                 and not home.is_relative_to(switcher.codex_home.resolve())
                 and not switcher.codex_home.resolve().is_relative_to(home))
        runs = home / "runs"
        if _ordinary(runs, directory=True):
            for path in runs.iterdir():
                try:
                    record = load_record(path, "run")
                    _require(record is not None and Path(record["home"]) == home)
                    records.append(record)
                except (OSError, ValueError, SessionError):
                    invalid += 1
    except (OSError, ValueError, TypeError, KeyError, SessionError):
        return Custody("conflict", "invalid_home")
    supervisors = [(r["supervisor"]["pid"], r["supervisor"]["start"])
                   for r in records if r["phase"] != "finished"]
    if sys.platform in ("linux", "darwin"):
        scan = process_detection.scan_codex_home(home, supervisors, invalid)
        return Custody(scan["state"], "writers_present" if scan["state"] == "live" else (
            "incomplete_scan" if scan["state"] == "uncertain" else "no_writers"),
            tuple(scan["pids"]), invalid, len(scan["issues"]), intent)
    if sys.platform != "win32":
        return Custody("uncertain", "unsupported_platform", invalid_records=invalid, intent=intent)
    pids, issues = set(), invalid
    deadline = time.monotonic() + 10
    for record in records:
        job = windows_job.query_job(record["runId"])
        if job == "alive":
            pids.add(record["supervisor"]["pid"])
        elif job != "gone":
            issues += 1
        # Finished drops reservations; A6 still needs both identities gone after job disappearance.
        for identity in (record["supervisor"], record["child"]):
            if identity is None or (identity == completed and record["phase"] == "finished"
                                    and identity == record["supervisor"]):
                continue
            state = windows_job.process_state(identity["pid"], identity["start"])
            if job == "gone" and state != "gone":
                state = windows_job.wait_identity_gone(
                    identity["pid"], identity["start"], max(0, deadline - time.monotonic()))
                issues += state != "gone"
            elif state == "alive":
                if identity != record["supervisor"] or record["phase"] != "finished":
                    pids.add(identity["pid"])
            elif state != "gone":
                issues += 1
    state = "live" if pids else "uncertain" if issues else "quiescent"
    return Custody(state, {"live": "writers_present", "uncertain": "incomplete_scan",
                          "quiescent": "no_writers"}[state], tuple(sorted(pids)), invalid,
                   issues, intent)


@dataclass(frozen=True)
class ReconcileResult:
    """Report custody changes without exposing credential contents."""

    state: str
    reason: str | None = None


def reconcile_session(
    switcher: CodexAccountSwitcher, number: str, *, quiescent: bool, read_only: bool = False,
) -> ReconcileResult:
    """Apply D6 under the caller-held R/.lock after a fresh quiescence check.

    Never scans, refreshes or launches. Path checks follow A7: hostile same-uid
    swaps between validation and mutation are outside the threat model.
    Stale-intent recovery belongs to the explicit replacement transaction.
    """
    if quiescent is not True:
        return ReconcileResult("conflict", "not_quiescent")
    reason = "invalid_slot"
    try:
        _require(_decimal(number, positive=True) and not number.startswith("0"))
        root = switcher.backup_dir
        _require(_absolute(str(root)) and root.resolve(strict=True) == root)
        _checked_parents(switcher.sequence_file.parent, root)
        sequence = load_document(switcher.sequence_file)
        if sequence is None:
            return ReconcileResult("conflict", "missing_slot")
        accounts = _object(sequence, ("accounts",))["accounts"]
        _require(isinstance(accounts, dict))
        if number not in accounts:
            return ReconcileResult("conflict", "missing_slot")
        slot = _object(accounts[number], ("email", "accountId"))
        _require(_text(slot["email"]))
        email = slot["email"].strip().casefold()
        home = root / "codex" / "sessions" / f"{number}-{slugify_email(email)}"
        reason = "invalid_home"
        _checked_parents(home, root)
        _owned(home)
        manifest_path = home / ".ccswap-session.json"
        manifest = load_record(manifest_path, "manifest")
        _require(manifest is not None)
        source = switcher.codex_home.resolve(strict=True)
        _require(manifest["accountNumber"] == number and Path(manifest["home"]) == home
                 and manifest["sourceHome"] == str(source)
                 and manifest["bootstrapState"] == "ready"
                 and not home.is_relative_to(source) and not source.is_relative_to(home))
        reason = "stale_intent"
        intent = home.parent / f".{home.name}.stale.json"
        if load_record(intent, "stale") is not None:
            return ReconcileResult("conflict", reason)
        reason = "invalid_backup"
        stored = switcher._read_session_backup(number)
        _require(stored is not None)
        stored_identity, _ = extract_identity(stored)
        reason = "invalid_session_auth"
        current = load_document(home / "auth.json")
        _require(current is not None)
        current_identity, _ = extract_identity(current)
        reason = "identity_mismatch"
        baseline = manifest["identity"]
        _require(slot["accountId"] == baseline["workspaceId"])
        _require(all(same_identity(left, right) for left, right in (
            (baseline, stored_identity), (baseline, current_identity),
            (stored_identity, current_identity),
        )))
        _require(all(identity["email"] == slot["email"].strip().casefold()
                     for identity in (baseline, stored_identity, current_identity)))
        reason = "fingerprint_failed"
        h, s = auth_fingerprint(current), auth_fingerprint(stored)
        b = manifest["baselineFingerprint"]
        state = "unchanged"
        credential_directories = [switcher.credentials_dir, home]
        if h == s:
            pass
        elif s == b:
            if read_only:
                return ReconcileResult("adopted_session")
            reason = "backup_write_failed"
            switcher._write_session_backup(number, current)
            state = "adopted_session"
            credential_directories = [switcher.credentials_dir]
        elif h == b:
            if read_only:
                return ReconcileResult("reseeded_session")
            reason = "auth_write_failed"
            _session_write(root, home / "auth.json", stored)
            h = s
            state = "reseeded_session"
            credential_directories = [home]
        else:
            return ReconcileResult("conflict", "both_moved")
        if read_only:
            return ReconcileResult(state)
        if h != b:
            # Also invalidate after interrupted adoption where H == S but B is stale.
            switcher._usage_cache.pop(number, None)
            reason = "credential_sync_failed"
            for directory in credential_directories:
                _sync_directory(directory)
            reason = "manifest_write_failed"
            _session_write(root, manifest_path, with_baseline(manifest, h))
            reason = "manifest_sync_failed"
            _sync_directory(home)
        return ReconcileResult(state)
    except (OSError, ValueError, TypeError, RecursionError, SessionError):
        return ReconcileResult("conflict", reason)


def _clear_intent(path: Path) -> None:
    path.unlink()
    _sync_directory(path.parent)


def recover_session_credentials(switcher: CodexAccountSwitcher, number: str) -> None:
    """Finish an intent under caller-held lock after proven quiescence."""
    home = slot_home(switcher, number)
    path = home.parent / f".{home.name}.stale.json"
    intent = load_record(path, "stale")
    manifest = load_record(home / ".ccswap-session.json", "manifest")
    stored = switcher._read_session_backup(number)
    _require(intent is not None and manifest is not None and stored is not None)
    identity, _ = extract_identity(stored)
    accepted = intent["identity"]
    _require(Path(intent["home"]) == home and intent["accountNumber"] == number)
    _require(all(accepted[key] == manifest["identity"][key]
                 for key in ("userId", "workspaceId", "email")))
    _require(intent["reason"] == "replacement"
             or same_identity(accepted, manifest["identity"]))
    _require(
        manifest["baselineFingerprint"] in (intent["oldFingerprint"], intent["newFingerprint"]))
    fingerprint = auth_fingerprint(stored)
    _require(fingerprint in (intent["oldFingerprint"], intent["newFingerprint"]))
    if fingerprint == intent["oldFingerprint"]:
        _require(same_identity(identity, manifest["identity"])
                 and identity["email"] == manifest["identity"]["email"])
        current = load_document(home / "auth.json")
        _require(current is not None)
        current_identity, _ = extract_identity(current)
        _require(same_identity(current_identity, manifest["identity"])
                 and current_identity["email"] == accepted["email"])
    else:
        _require(identity == accepted)
        current = load_document(home / "auth.json")
        if current is not None:
            _require(auth_fingerprint(current) in (
                intent["oldFingerprint"], intent["newFingerprint"],
                manifest["baselineFingerprint"]))
            current_identity, _ = extract_identity(current)
            _require(current_identity["email"] == accepted["email"] and (
                same_identity(current_identity, manifest["identity"])
                or same_identity(current_identity, accepted)))
        _sync_directory(switcher.credentials_dir)
        _session_write(switcher.backup_dir, home / "auth.json", stored)
        _sync_directory(home)
        updated = dict(with_baseline(manifest, fingerprint), identity=accepted)
        _session_write(switcher.backup_dir, home / ".ccswap-session.json", updated)
        _sync_directory(home)
        switcher._usage_cache.pop(number, None)
    _clear_intent(path)


def replace_session_credentials(
    switcher: CodexAccountSwitcher, number: str, new_auth: dict[str, Any], reason: str,
) -> None:
    """Replace credentials under caller-held lock, quiescent guard and D6."""
    identity, _ = extract_identity(new_auth)
    _require(reason in ("replacement", "refresh"))
    stored = switcher._read_session_backup(number)
    stored_identity, _ = extract_identity(stored)
    _require(all(identity[key] == stored_identity[key] for key in ("userId", "workspaceId")))
    home = slot_home(switcher, number)
    if not _ordinary(home, directory=True):
        switcher._write_session_backup(number, new_auth)
        _sync_directory(switcher.credentials_dir)
        return
    manifest = load_record(home / ".ccswap-session.json", "manifest")
    _require(manifest is not None and stored is not None)
    _require(all(identity[key] == manifest["identity"][key]
                 for key in ("userId", "workspaceId", "email")))
    _require(reason == "replacement" or same_identity(identity, manifest["identity"]))
    current = load_document(home / "auth.json")
    _require(current is not None and auth_fingerprint(current) == auth_fingerprint(stored)
             == manifest["baselineFingerprint"])
    intent = home.parent / f".{home.name}.stale.json"
    _require(not _sharing_exists(intent))
    data = dict(schemaVersion=1, home=str(home), accountNumber=number, identity=identity,
                oldFingerprint=auth_fingerprint(stored), newFingerprint=auth_fingerprint(new_auth),
                reason=reason)
    _session_write(switcher.backup_dir, intent, validate_record(data, "stale"))
    _sync_directory(home.parent)
    switcher._write_session_backup(number, new_auth)
    _sync_directory(switcher.credentials_dir)
    recover_session_credentials(switcher, number)


def with_baseline(manifest: dict[str, Any], fingerprint: str) -> dict[str, Any]:
    """Advance the baseline and bounded lineage together, without mutating input."""
    lineage = manifest["lineage"]
    if fingerprint != manifest["baselineFingerprint"]:
        lineage = (lineage + [fingerprint])[-64:]
    return dict(manifest, baselineFingerprint=fingerprint, lineage=lineage)


def _refresh_timestamp(value: object) -> datetime:
    """Parse Codex's timezone-aware last_refresh without guessing a timezone."""
    _require(isinstance(value, str) and bool(re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt][0-9]{2}:[0-9]{2}:[0-9]{2}"
        r"(?:\.[0-9]+)?(?:[Zz]|[+-][0-9]{2}:[0-9]{2})", value,
    )))
    parsed = datetime.fromisoformat(value.replace("t", "T").replace("z", "Z"))
    _require(parsed.utcoffset() is not None)
    return parsed


def default_write_decision(
    d_auth: dict[str, Any], h_auth: dict[str, Any], s_auth: dict[str, Any],
    manifest: dict[str, Any],
) -> str:
    """Classify D -> S under A10; never infer order from fingerprints.

    An ancestor evicted after 64 baselines is reported as conflict (safe).
    """
    try:
        validate_record(manifest, "manifest")
        d, h, s = map(auth_fingerprint, (d_auth, h_auth, s_auth))
        b = manifest["baselineFingerprint"]
        if d == s or d in manifest["lineage"] or (d == h and s == b):
            return "skip"
        if h == s == b:
            d_time = _refresh_timestamp(d_auth.get("last_refresh"))
            s_time = _refresh_timestamp(s_auth.get("last_refresh"))
            if d_time > s_time:
                return "write"
    except (ValueError, TypeError, SessionError):
        pass
    return "conflict"


def classify_session(switcher: CodexAccountSwitcher, number: str) -> ReconcileResult:
    """Classify D6 without writes, using reconciliation's exact checks; lock required."""
    return reconcile_session(switcher, number, quiescent=True, read_only=True)


def _ordinary(path: Path, *, directory: bool = False) -> bool:
    """Reject links (including Windows reparse points), not just their targets."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    _require(not stat.S_ISLNK(info.st_mode)
             and not getattr(info, "st_file_attributes", 0) & 0x400)
    _require(stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
    return True


def _checked_parents(path: Path, tree: Path) -> None:
    _require(path.is_relative_to(tree) and ".." not in path.relative_to(tree).parts)
    _require(_ordinary(tree, directory=True))
    for part in path.relative_to(tree).parts:
        tree = tree / part
        _require(_ordinary(tree, directory=True))


def _private_directory(path: Path, tree: Path) -> bool:
    _checked_parents(path.parent, tree)
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        _require(_ordinary(path, directory=True))
        return False
    if os.name == "posix":
        path.chmod(0o700)
    return True


def _private_credentials(home: Path) -> None:
    """Keep a reused home private: Codex rewrites auth.json in place, keeping its mode."""
    auth = home / "auth.json"
    _require(_ordinary(home, directory=True) and _ordinary(auth))
    # A second link would receive every rotated token outside the session home.
    if auth.lstat().st_nlink != 1:
        raise SessionError(f"Session login {auth} has another hard link; remove it, "
                           "then retry. No new session writer started.")
    if os.name != "posix":
        return
    for path, mode in ((home, 0o700), (auth, 0o600)):
        flags = os.O_RDONLY | os.O_NOFOLLOW | (os.O_DIRECTORY if path == home else 0)
        descriptor = os.open(path, flags)
        try:
            if stat.S_IMODE(os.fstat(descriptor).st_mode) & 0o077:
                os.fchmod(descriptor, mode)
        finally:
            os.close(descriptor)


def _session_write(tree: Path, path: Path, data: dict[str, Any]) -> None:
    """Write only ordinary files below the checked, wrapper-owned tree."""
    _require(path.is_relative_to(tree) and path != tree
             and ".." not in path.relative_to(tree).parts)
    _checked_parents(path.parent, tree)
    _ordinary(path)
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, allow_nan=False),
                      mode=0o600, follow_symlinks=False)


def _publish_home(stage: Path, home: Path) -> None:
    """Atomic no-replace publication; no check-then-rename POSIX fallback."""
    if os.name == "nt":
        os.rename(stage, home)  # Windows rename refuses an existing destination.
        return
    # The kernel ABI follows the real OS, not the platform view tests steer elsewhere.
    system = os.uname().sysname
    darwin = system == "Darwin"
    _require(darwin or system == "Linux")
    library = ctypes.CDLL("/usr/lib/libSystem.B.dylib" if darwin else None, use_errno=True)
    rename = getattr(library, "renamex_np" if darwin else "renameat2", None)
    _require(rename is not None)
    rename.argtypes = ([ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint] if darwin else
                      [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p,
                       ctypes.c_uint])
    rename.restype = ctypes.c_int
    arguments = ((os.fsencode(stage), os.fsencode(home), 0x00000004) if darwin else
                 (-100, os.fsencode(stage), -100, os.fsencode(home), 1))
    if rename(*arguments):
        raise OSError(ctypes.get_errno(), "Session publication refused.")


_BOOTSTRAP_DIRS = ("skills", "sessions", "archived_sessions", "log")
_BOOTSTRAP_FILES = ("auth.json", ".ccswap-session.json")


def _new_manifest(home: Path, source: Path, number: str, auth: dict[str, Any]) -> dict[str, Any]:
    identity, _ = extract_identity(auth)
    fingerprint = auth_fingerprint(auth)
    return validate_record(dict(
        schemaVersion=1, accountNumber=number, identity=identity,
        home=str(home), sourceHome=str(source), baselineFingerprint=fingerprint,
        lineage=[fingerprint],
        bootstrapState="preparing", sharedItems={},
    ), "manifest")


def _owned(path: Path) -> None:
    if os.name == "posix":
        _require(path.lstat().st_uid == os.geteuid())


def _resume_stage(stage: Path, manifest: dict[str, Any]) -> None:
    """Validate the entire allowlist before removing any writer leftovers."""
    _require(_ordinary(stage, directory=True))
    _owned(stage)
    entries = list(stage.iterdir())
    if not entries:
        return
    receipt = stage / ".ccswap-session.json"
    _require(_ordinary(receipt))
    previous = load_record(receipt, "manifest")
    _require(previous is not None and all(
        previous[field] == manifest[field]
        for field in ("accountNumber", "identity", "home", "sourceHome")
    ))
    manifest["lineage"] = with_baseline(previous, manifest["baselineFingerprint"])["lineage"]
    leftovers = []
    for entry in entries:
        _owned(entry)
        if entry.name in _BOOTSTRAP_DIRS:
            _require(_ordinary(entry, directory=True) and not any(entry.iterdir()))
        else:
            _require(_ordinary(entry))
            if entry.name not in _BOOTSTRAP_FILES:
                # fsutil: <target[:40]>.<8 tempfile characters>.tmp.
                _require(any(re.fullmatch(re.escape(name[:40]) + r"\.[a-z0-9_]{8}\.tmp",
                                          entry.name) for name in _BOOTSTRAP_FILES))
                leftovers.append(entry)
    for entry in leftovers:
        entry.unlink()


def _sync_directory(path: Path) -> None:
    if os.name != "posix":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        _require(stat.S_ISDIR(os.fstat(descriptor).st_mode))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def bootstrap_home(
    backup_root: Path, source_home: Path, number: str, auth: dict[str, Any],
) -> Path:
    """Bootstrap only a new home; caller holds R/.lock. Never scan or launch.

    A matching manifest is the staging ownership receipt; an empty stage can
    also be resumed. Other stages without a receipt are retained. Existing
    final homes belong to reconciliation, never to this bootstrap operation.
    A7 permits path-based checks: a hostile same-uid process can still swap
    checked paths between validation and mutation; that adversary is out of scope.
    """
    stage = None
    try:
        _require(_decimal(number, positive=True) and not number.startswith("0"))
        identity, _ = extract_identity(auth)
        _require(_absolute(str(backup_root)) and _absolute(str(source_home)))
        backup_root = backup_root.resolve(strict=True)
        source_home = source_home.resolve(strict=True)
        _require(_ordinary(backup_root, directory=True))
        _require(_ordinary(source_home, directory=True))
        sessions = backup_root / "codex" / "sessions"
        home = sessions / f"{number}-{slugify_email(identity['email'])}"
        stage = sessions / f".{home.name}.preparing"
        for destination in (home, stage):
            _require(not source_home.is_relative_to(destination)
                     and not destination.is_relative_to(source_home))
        manifest = _new_manifest(home, source_home, number, auth)
        _private_directory(sessions.parent, backup_root)
        _private_directory(sessions, backup_root)
        _require(not os.path.lexists(home))
        if not _private_directory(stage, backup_root):
            _resume_stage(stage, manifest)
        _session_write(backup_root, stage / ".ccswap-session.json", manifest)
        # Codex creates databases, caches and runtime coordination itself.
        for name in _BOOTSTRAP_DIRS:
            _private_directory(stage / name, backup_root)
        _session_write(backup_root, stage / "auth.json", auth)
        manifest["bootstrapState"] = "ready"
        _session_write(backup_root, stage / ".ccswap-session.json", manifest)
        _checked_parents(stage, backup_root)
        _sync_directory(stage)
        _publish_home(stage, home)
        _sync_directory(sessions)
        return home
    except (OSError, ValueError, TypeError, RecursionError, RuntimeError, SessionError) as error:
        location = f" at {stage}" if stage is not None else ""
        detail = ""
        if isinstance(error, OSError):
            detail = f" ({errno.errorcode.get(error.errno, 'UNKNOWN')})"
        raise SessionError(
            f"Session bootstrap refused{location}{detail}; existing evidence retained."
        ) from None


_SHARED_FILES = ("config.toml", "AGENTS.md", "AGENTS.override.md")
_SHARED_DIRS = ("rules", "prompts")


def _sharing_path(path: Path, source: Path) -> bool:
    if not path.is_relative_to(source) or path == source:
        return False
    parts = path.relative_to(source).parts
    return (parts[0] in _SHARED_FILES and len(parts) == 1
            or parts[0] in _SHARED_DIRS
            or parts[0] == "skills" and len(parts) > 1 and parts[1] != ".system")


def _sharing_exists(path: Path) -> bool:
    try:
        os.lstat(path)
        return True
    except (FileNotFoundError, NotADirectoryError):
        return False


def _system_skill(path: Path, source: Path) -> bool:
    system = source / "skills" / ".system"
    folded = sys.platform in ("darwin", "win32")
    if path.parent == system.parent and (
            path.name == ".system" or folded and path.name.casefold() == ".system"):
        return True
    resolved = path.resolve(strict=True)
    if _sharing_exists(system):
        actual = system.resolve(strict=True)
        if os.path.samefile(resolved, actual) or resolved.is_relative_to(actual):
            return True
        if folded and str(resolved).casefold() == str(actual).casefold():
            return True
    return False


def _sharing_source(path: Path, source: Path, backup: Path, *, copying: bool = False,
                    ancestors: frozenset[Path] = frozenset()) -> bool:
    info = path.lstat()
    redirected = stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400
    _require(not copying or not redirected)
    resolved = path.resolve(strict=True)
    _require(not resolved.is_relative_to(backup)
             and (not resolved.is_relative_to(source) or _sharing_path(resolved, source))
             and resolved.name.casefold() not in ("auth.json", ".credentials.json")
             and not _system_skill(path, source))
    directory = stat.S_ISDIR(resolved.stat().st_mode)
    _require(_ordinary(resolved, directory=directory))
    if copying and directory:
        _require(resolved not in ancestors)
        for child in path.iterdir():
            _sharing_source(child, source, backup, copying=True, ancestors=ancestors | {resolved})
    return directory


def _sharing_copy_owned(path: Path) -> bool:
    """Validate the whole copy before any recursive removal or replacement."""
    info = path.lstat()
    directory = stat.S_ISDIR(info.st_mode)
    _require(_ordinary(path, directory=directory))
    _owned(path)
    if directory:
        for child in path.iterdir():
            _sharing_copy_owned(child)
    return directory


def _same_link(left: str, right: str) -> bool:
    """Compare absolute link spellings without resolving possibly missing targets."""
    paths = ntpath if os.name == "nt" else os.path

    def normalize(value: str) -> str | None:
        if os.name == "nt" and value.startswith("\\\\?\\") and re.match(
                r"[A-Za-z]:[\\/]", value[4:]):
            value = value[4:]
        if not paths.isabs(value) or os.name == "nt" and not paths.splitdrive(value)[0]:
            return None
        return paths.normcase(paths.normpath(value))

    normalized = normalize(left)
    return normalized is not None and normalized == normalize(right)


def _sharing_destination(path: Path, item: dict[str, Any] | None) -> None:
    if not _sharing_exists(path):
        return
    _require(item is not None)
    if item["mode"] == "symlink":
        _require(stat.S_ISLNK(path.lstat().st_mode)
                 and _same_link(os.readlink(path), item["source"]))
        _owned(path)
    else:
        _sharing_copy_owned(path)


def _remove_shared(path: Path, item: dict[str, Any]) -> None:
    _sharing_destination(path, item)
    if not _sharing_exists(path):
        return
    if item["mode"] == "copy" and stat.S_ISDIR(path.lstat().st_mode):
        shutil.rmtree(path)
    else:
        path.unlink()


def _sharing_temporary(home: Path, backup: Path) -> Path:
    temporary = home / ".ccswap-tmp"
    _private_directory(temporary, backup)
    _owned(temporary)
    leftovers = list(temporary.iterdir())
    for entry in leftovers:
        _require(entry.name.startswith((".ccswap-new-", ".ccswap-old-")))
        _sharing_copy_owned(entry)
    for entry in leftovers:
        if entry.name.startswith(".ccswap-new-"):
            _remove_shared(entry, dict(mode="copy"))
    return temporary


def _copy_shared(source: Path, destination: Path, source_home: Path, backup: Path,
                 temporary: Path, previous: dict[str, Any]) -> None:
    directory = _sharing_source(source, source_home, backup, copying=True)
    _sharing_destination(destination, previous)
    stage = Path(tempfile.mkdtemp(prefix=".ccswap-new-", dir=temporary))
    old = None
    try:
        if directory:
            shutil.copytree(source, stage, dirs_exist_ok=True, copy_function=shutil.copyfile)
            if os.name == "posix":
                stage.chmod(0o700)
                for entry in stage.rglob("*"):
                    entry.chmod(0o700 if entry.is_dir() else 0o600)
            publication = stage
        else:
            publication = stage / "item"
            atomic_write_bytes(publication, source.read_bytes(), mode=0o600,
                               follow_symlinks=False)
        _sharing_destination(destination, previous)
        if _sharing_exists(destination) and (
                directory or stat.S_ISDIR(destination.lstat().st_mode)):
            old = Path(tempfile.mkdtemp(prefix=".ccswap-old-", dir=temporary))
            old.rmdir()
            replace_with_retry(destination, old)
        try:
            replace_with_retry(publication, destination)
        except BaseException:
            if old is not None and _sharing_exists(old):
                replace_with_retry(old, destination)
            raise
        if old is not None:
            _remove_shared(old, dict(mode="copy"))
    finally:
        if _sharing_exists(stage):
            _remove_shared(stage, dict(mode="copy"))


def sync_sharing(backup_root: Path, home: Path, source_home: Path,
             manifest: dict[str, Any], persist: Callable[[dict[str, Any]], None],
                 ) -> tuple[dict[str, Any], list[str]]:
    """Refresh under caller-held R/.lock; persist must durably write each receipt.

    Copies are recorded before writing and refreshed on every call, overwriting
    local edits. Exact absolute symlinks can be adopted after interrupted writes.
    Windows copies are derived data. Orphaned old copies survive until all
    copies are republished or their sources are gone. A7 accepts path-based checks;
    hostile same-uid swaps between validation and mutation are out of scope.
    """
    try:
        _require(callable(persist))
        validate_record(manifest, "manifest")
        _require(all(_absolute(str(path)) for path in (backup_root, home, source_home)))
        backup_root = backup_root.resolve(strict=True)
        source_home = source_home.resolve(strict=True)
        _checked_parents(home, backup_root)
        _owned(home)
        expected = backup_root / "codex" / "sessions" / (
            manifest["accountNumber"] + "-" + slugify_email(manifest["identity"]["email"]))
        _require(home == expected and Path(manifest["home"]) == home
                 and manifest["sourceHome"] == str(source_home)
                 and manifest["bootstrapState"] == "ready")
        _require(not home.is_relative_to(source_home) and not source_home.is_relative_to(home))
        _require(_ordinary(source_home, directory=True))
        _require(_ordinary(home / "skills", directory=True))
        _owned(home / "skills")
        previous = manifest["sharedItems"]
        wanted, warnings = {}, []
        for name in (*_SHARED_FILES, *_SHARED_DIRS):
            path = source_home / name
            if _sharing_exists(path):
                directory = _sharing_source(path, source_home, backup_root)
                _require(directory == (name in _SHARED_DIRS))
                wanted[name] = path
        skills = source_home / "skills"
        if _sharing_exists(skills):
            _require(_ordinary(skills, directory=True))
            for entry in skills.iterdir():
                if entry.name == ".system" or (sys.platform in ("darwin", "win32")
                                               and entry.name.casefold() == ".system"):
                    continue
                name = "skills/" + entry.name
                try:
                    candidate = dict(manifest, sharedItems={name: dict(
                        source=str(entry), mode="symlink")})
                    validate_record(candidate, "manifest")
                    if _system_skill(entry, source_home):
                        if entry.name.casefold() != ".system":
                            warnings.append(f"Skipping skill entry: {entry.name}")
                        continue
                    _sharing_source(entry, source_home, backup_root)
                except (SessionError, FileNotFoundError, NotADirectoryError, RuntimeError):
                    warnings.append(f"Skipping skill entry: {entry.name}")
                    continue
                except OSError as error:
                    if error.errno != errno.ELOOP:
                        raise
                    warnings.append(f"Skipping skill entry: {entry.name}")
                    continue
                wanted[name] = entry
        # Validate every name and destination before any mutation, including staging cleanup.
        candidate = dict(manifest, sharedItems={name: dict(source=str(path), mode="symlink")
                                               for name, path in wanted.items()})
        validate_record(candidate, "manifest")
        adopted = {}
        for name in previous.keys() | wanted.keys():
            old = previous.get(name)
            _require(not (sys.platform in ("darwin", "win32")
                          and name.casefold() == "skills/.system"))
            if old is not None:
                _require(old["source"] == str(source_home / name))
            destination = home / name
            _checked_parents(destination.parent, backup_root)
            if old is None and name in wanted and _sharing_exists(destination):
                old = dict(source=str(source_home / name), mode="symlink")
                adopted[name] = old
            _sharing_destination(destination, old)
        temporary = _sharing_temporary(home, backup_root)
        result = dict(manifest, sharedItems=dict(previous, **adopted))

        def save() -> None:
            validate_record(result, "manifest")
            persist(dict(result, sharedItems=dict(result["sharedItems"])))

        for name, old in previous.items():
            if name not in wanted:
                _remove_shared(home / name, old)
                _sync_directory((home / name).parent)
                del result["sharedItems"][name]
                save()
        for name, source in wanted.items():
            destination = home / name
            old = result["sharedItems"].get(name)
            if old is None or old["mode"] != "copy":
                if not _sharing_exists(destination):
                    try:
                        destination.symlink_to(source, target_is_directory=source.is_dir())
                        old = dict(source=str(source), mode="symlink")
                    except OSError:
                        if sys.platform != "win32":
                            raise
                        old = dict(source=str(source), mode="copy")
                else:
                    old = dict(source=str(source), mode="symlink")
            result["sharedItems"][name] = old
            if old["mode"] == "copy":
                save()  # Write-ahead ownership receipt, including a missing destination.
                _copy_shared(source, destination, source_home, backup_root, temporary, old)
            _sync_directory(destination.parent)
            save()
        for entry in temporary.iterdir():
            if entry.name.startswith(".ccswap-old-"):
                _remove_shared(entry, dict(mode="copy"))
        return result, warnings
    except (OSError, ValueError, TypeError, RecursionError, RuntimeError, SessionError) as error:
        detail = ""
        if isinstance(error, OSError):
            detail = f" ({errno.errorcode.get(error.errno, 'UNKNOWN')})"
        raise SessionError(
            f"Session sharing refused{detail}; existing evidence retained."
        ) from None


def _policy_error(source: object, key: str) -> None:
    raise SessionError(
        f"Cannot establish a compatible isolated Codex configuration: {source}/{key}. "
        "No new session writer started."
    )


def _program_data() -> Path:
    import ctypes

    fallback = Path(r"C:\ProgramData")
    try:
        shell = ctypes.WinDLL("shell32")
        ole = ctypes.WinDLL("ole32")
        query = shell.SHGetKnownFolderPath
        query.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p,
                          ctypes.POINTER(ctypes.c_void_p)]
        query.restype = ctypes.c_int32
        free = ole.CoTaskMemFree
        free.argtypes = [ctypes.c_void_p]
        free.restype = None
        guid = (ctypes.c_ubyte * 16).from_buffer_copy(
            UUID("62ab5d82-fdc1-4dc3-a9dd-070d1d495d97").bytes_le,
        )
        pointer = ctypes.c_void_p()
        try:
            if query(ctypes.byref(guid), 0, None, ctypes.byref(pointer)) != 0 or not pointer.value:
                return fallback
            path = Path(ctypes.wstring_at(pointer))
            return path if path.is_absolute() else fallback
        finally:
            if pointer.value:
                free(pointer)
    except (OSError, AttributeError, ValueError):
        return fallback


def _windows_requirements_path() -> Path | None:
    import winreg

    source = r"HKLM\SOFTWARE\Policies\OpenAI\Codex"
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SOFTWARE\Policies\OpenAI\Codex", 0,
                            winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
            value, kind = winreg.QueryValueEx(key, "RequirementsPath")
    except FileNotFoundError:
        return None
    except OSError:
        _policy_error(source, "RequirementsPath")
    if kind != winreg.REG_SZ or not _text(value) or not Path(value).is_absolute():
        _policy_error(source, "RequirementsPath")
    return Path(value)


def _system_policy_paths() -> list[Path]:
    if sys.platform == "win32":
        root = _program_data() / "OpenAI" / "Codex"
        return [root / "config.toml", root / "requirements.toml"]
    paths = [Path("/etc/codex") / name for name in (
        "config.toml", "requirements.toml", "managed_config.toml",
    )]
    if sys.platform == "darwin":

        root = Path("/Library/Managed Preferences")
        try:
            import pwd

            user = pwd.getpwuid(os.getuid()).pw_name
        except (ImportError, KeyError, OSError):
            _policy_error(root, "managed Codex preferences are not supported yet")
        paths.extend([root / "com.openai.codex.plist",
                      root / user / "com.openai.codex.plist"])
    return paths


def _project_policy_paths(cwd: Path) -> list[Path]:
    try:
        directory = cwd.resolve(strict=True)
        if not directory.is_dir():
            _policy_error(cwd, "project discovery")
    except (OSError, RuntimeError):
        _policy_error(cwd, "project discovery")
    return [parent / ".codex" / "config.toml" for parent in (directory, *directory.parents)]


def _check_policy_table(table: dict[str, Any], source: Path, workspace: str) -> None:
    for key, value in table.items():
        if key == "config_server":
            _policy_error(source, "config_server (unresolved managed source)")
        elif key in ("profile", "experimental_thread_store_endpoint"):
            # Codex rejects these legacy settings even when TOML parsing succeeds.
            _policy_error(source, key)
        elif key == "forced_login_method":
            if value != "chatgpt":
                _policy_error(source, key)
        elif key == "allowed_login_methods":
            if (not isinstance(value, list) or not all(
                isinstance(item, str) and item in ("api", "chatgpt") for item in value
            ) or "chatgpt" not in value):
                _policy_error(source, key)
        elif key in ("forced_chatgpt_workspace_id", "allowed_chatgpt_workspaces"):
            items = ([value] if key == "forced_chatgpt_workspace_id"
                     and isinstance(value, str) else value)
            if not isinstance(items, list) or not all(isinstance(item, str) for item in items):
                _policy_error(source, key)
            workspaces = [item.strip() for item in items if item.strip()]
            if (workspaces or key == "allowed_chatgpt_workspaces") and workspace not in workspaces:
                _policy_error(source, key)
        if isinstance(value, dict):
            _check_policy_table(value, source, workspace)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    _check_policy_table(item, source, workspace)


def _check_policy_file(path: Path, workspace: str) -> None:
    try:
        # stat distinguishes a missing file from a dangling link or denied lookup.
        try:
            metadata = path.stat()
        except FileNotFoundError:
            path.lstat()
            _policy_error(path, "source")
        if path.suffix == ".plist":
            _policy_error(path, "managed Codex preferences are not supported yet")
        if not stat.S_ISREG(metadata.st_mode):
            _policy_error(path, "source")
        with path.open("rb") as stream:
            document = tomllib.load(stream)
        _check_policy_table(document, path, workspace)
    except FileNotFoundError:
        # Only a missing directory entry (not a dangling symlink) is absent.
        try:
            path.lstat()
        except FileNotFoundError:
            return
        except OSError:
            pass
        _policy_error(path, "source")
    except (OSError, ValueError, UnicodeError, RecursionError):
        _policy_error(path, "source")


def check_login_policy(
    default_home: Path, cwd: Path, identity: object, plan: object, *,
    system_paths: Iterable[Path] | None = None,
    windows_policy: Callable[[], Path | None] | None = None,
) -> None:
    """Conservatively scan local layers; this is not an effective-policy resolver.

    Injected system_paths replaces OS discovery (including managed plist paths).
    windows_policy supplies the registry path reader; its errors are never absence.
    Profile argv rejection belongs to launch-form validation, before this gate.
    """
    if not session_plan_supported(plan):
        raise SessionError(
            "Session mode does not support workspace-managed "
            "(business/enterprise/education) accounts yet."
        )
    workspace = validate_identity(identity)["workspaceId"]
    paths = [default_home / "config.toml"]
    paths.extend(_system_policy_paths() if system_paths is None else system_paths)
    if sys.platform == "win32" or windows_policy is not None:
        try:
            policy = (windows_policy or _windows_requirements_path)()
        except (OSError, ValueError):
            _policy_error(r"HKLM\SOFTWARE\Policies\OpenAI\Codex", "RequirementsPath")
        if policy is not None:
            if not isinstance(policy, Path) or not policy.is_absolute():
                _policy_error(r"HKLM\SOFTWARE\Policies\OpenAI\Codex", "RequirementsPath")
            paths.append(policy)
    paths.extend(_project_policy_paths(cwd))
    for path in paths:
        _check_policy_file(path, workspace)


_CHILD_DROP = (
    "OPENAI_API_KEY", "OPENAI_BASE_URL", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN",
    "CODEX_REFRESH_TOKEN_URL_OVERRIDE", "CODEX_APP_SERVER_LOGIN_CLIENT_ID",
    "CODEX_EXEC_SERVER_URL",
    # login/src/auth/{manager.rs:226,personal_access_token.rs:12,agent_identity.rs:29–30}.
    "CODEX_REVOKE_TOKEN_URL_OVERRIDE", "CODEX_AUTHAPI_BASE_URL",
    "CODEX_AGENT_IDENTITY_AUTHAPI_BASE_URL", "CODEX_AGENT_IDENTITY_JWKS_BASE_URL",
    # tui/src/session_log.rs:85–92; rollout-trace/src/thread.rs:44,107.
    "CODEX_TUI_RECORD_SESSION", "CODEX_TUI_SESSION_LOG_PATH", "CODEX_ROLLOUT_TRACE_ROOT",
    # app-server/src/request_processors/account_processor.rs:37,628;
    # codex-mcp/src/mcp/mod.rs:68,617; app-server-transport/src/daemon_shutdown.rs:14–46.
    "CODEX_APP_SERVER_LOGIN_ISSUER", "CODEX_CONNECTORS_TOKEN", "CODEX_DAEMON_SHUTDOWN_FILE",
)
_CHILD_FORBIDDEN = {
    "cli_auth_credentials_store", "mcp_oauth_credentials_store", "model_provider",
    "model_providers", "profile", "profiles", "chatgpt_base_url",
    "experimental_thread_store_endpoint", "forced_login_method", "forced_chatgpt_workspace_id",
    # Endpoint keys from PROJECT_LOCAL_CONFIG_DENYLIST (config/src/loader/mod.rs:88–102) that
    # carry account tokens; notify/otel/metadata keys there stay ordinary user settings.
    "openai_base_url",
    "experimental_realtime_ws_base_url", "experimental_realtime_webrtc_call_base_url",
}


# Managed layers outrank the child's -c overrides: requirements replace routing and the
# credential store exactly (core/src/config/requirements.rs:33–39), so refuse them early.
_MANAGED_ROUTING = _CHILD_FORBIDDEN - {
    "cli_auth_credentials_store", "mcp_oauth_credentials_store",
    "forced_login_method", "forced_chatgpt_workspace_id",
}


def _check_managed_routing(table: dict[str, Any], source: Path) -> None:
    for key in sorted(_MANAGED_ROUTING & table.keys()):
        _policy_error(source, key)
    for key in ("cli_auth_credentials_store", "mcp_oauth_credentials_store"):
        if key in table and table[key] != "file":
            _policy_error(source, key)


def refuse_inherited_home(env: dict[str, str]) -> None:
    """Reject an explicitly selected parent home before account discovery."""
    if env.get("CODEX_HOME"):
        raise SessionError("Inherited CODEX_HOME is not supported.")


def _child_resolved(path: Path) -> Path:
    resolved = path.resolve()
    parent = resolved
    while True:
        try:
            parent.resolve(strict=True)
            return resolved
        except FileNotFoundError:
            if parent == parent.parent:
                raise SessionError("Cannot resolve isolated child path.")
            parent = parent.parent


def _child_private(value: str, home: Path, base: Path) -> bool:
    try:
        if value == "~" or value.startswith(("~/", "~\\")):
            raise SessionError("Home expansion is not supported in child paths.")
        path = Path(value)
        if not path.is_absolute():
            path = base / path
        return _child_resolved(path).is_relative_to(_child_resolved(home))
    except (OSError, RuntimeError, ValueError) as exc:
        raise SessionError("Cannot resolve isolated child path.") from exc


def child_env(env: dict[str, str], home: Path, *,
              cwd: Path | None = None) -> tuple[dict[str, str], list[str]]:
    """Build a private child environment; diagnostics contain variable names only."""
    result = env.copy()
    warnings = []
    for name in _CHILD_DROP:
        if name in result:
            del result[name]
            warnings.append(f"Removed {name}.")
    if "CODEX_SQLITE_HOME" in result:
        value = result["CODEX_SQLITE_HOME"].strip()
        if not value or not _child_private(value, home, cwd or Path.cwd()):
            del result["CODEX_SQLITE_HOME"]
            warnings.append("Removed CODEX_SQLITE_HOME.")
        elif not Path(value).is_absolute() or value != result["CODEX_SQLITE_HOME"]:
            # core/src/config/mod.rs:272–282 resolves this against --cd, not the parent cwd.
            # Pin a private relative value before the child's working directory can change.
            result["CODEX_SQLITE_HOME"] = str(((cwd or Path.cwd()) / value).resolve())
    result["CODEX_HOME"] = str(home)
    return result, warnings


def _child_override(raw: str) -> tuple[str, Any]:
    key, separator, value = raw.partition("=")
    if not separator or not key.strip():
        raise SessionError("Invalid child config override.")
    key = key.strip()
    # Codex uses literal dot-separated path components: config/src/overrides.rs:22.
    root_key = key.split(".")[0]
    if root_key in _CHILD_FORBIDDEN:
        raise SessionError("Unsupported child config key: " + root_key)
    # utils/cli/src/config_override.rs:65–94: first '=', TOML sentinel, quote stripping fallback.
    try:
        parsed = tomllib.loads("_x_ = " + value.strip())["_x_"]
    except (tomllib.TOMLDecodeError, KeyError):
        # Rust toml accepts syntax that tomllib cannot safely interpret.
        if root_key in {"sqlite_home", "log_dir"}:
            raise SessionError("Unparsable child path override: " + root_key) from None
        parsed = value.strip().strip("\"'")
    return key, parsed


# Root value options: tui/src/cli.rs:75–76, utils/cli/src/shared_options.rs:10–93,
# cli/src/main.rs:964–988. Images consume 1.. values, unlike exec resume's single image.
_CHILD_VALUES = {
    "--model", "-m", "--sandbox", "-s", "--ask-for-approval", "-a", "--cd", "-C",
    "--add-dir", "--enable", "--disable", "--image", "-i", "--config", "-c",
}
_CHILD_FLAGS = {
    "--strict-config", "--search", "--no-alt-screen", "--no-daemon",
    "--approve-for-me", "--not-so-yolo", "--dangerously-bypass-approvals-and-sandbox",
    "--yolo", "--dangerously-bypass-hook-trust", "--help", "-h", "--version", "-V",
}
_CHILD_COMMANDS = set("""agents tcp-tunnel review login logout mcp plugin app-server remote-control
app completion update doctor sandbox debug execpolicy apply a queue archive delete migrate-rollouts
migrate-nfs-db unarchive cloud cloud-tasks responses-api-proxy stdio-to-uds features exec-server
help""".split())


def _child_parse(
    args: list[str], *, cwd: Path | None = None,
) -> tuple[str, list[tuple[str, Any]], Path]:
    form, positional, overrides = "interactive", False, []
    base = cwd if cwd is not None else Path.cwd()
    cwd = base
    index = 0
    while index < len(args):
        token = args[index]
        index += 1
        if token == "--":
            break
        if token.startswith("-") and token != "-":
            option, equal, value = token.partition("=")
            if not token.startswith("--") and len(token) > 2:
                option, value, equal = token[:2], token[2:].lstrip("="), "="
            if option in {"-p", "--profile", "--oss", "--local-provider",
                          "--remote", "--remote-auth-token-env", "--ignore-user-config"}:
                raise SessionError("Unsupported child option: " + option)
            values = _CHILD_VALUES.copy()
            flags = _CHILD_FLAGS.copy()
            if form != "interactive":
                flags.update({"--last", "--all", "--include-non-interactive"})
            if form.startswith("exec"):
                values.update({"--thread-source", "--cyber-access-program", "--output-schema",
                               "--color", "--output-last-message", "-o"})
                flags.update({"--skip-git-repo-check", "--ephemeral",
                              "--ignore-rules", "--json", "--experimental-json"})
            if option in values:
                if not equal:
                    if index == len(args) or args[index].startswith("-"):
                        raise SessionError("Missing child option value: " + option)
                    value = args[index]
                    index += 1
                if option in {"-C", "--cd"}:
                    if value.startswith("~"):
                        raise SessionError("Home expansion is not supported in child paths.")
                    cwd = base / value
                if option in {"-c", "--config"}:
                    overrides.append(_child_override(value))
                if option in {"-i", "--image"} and form != "exec resume" and not equal:
                    while index < len(args) and not args[index].startswith("-"):
                        index += 1
            elif option not in flags or equal:
                raise SessionError("Unsupported child option.")
            continue
        if not positional and form == "interactive" and token in {"exec", "e", "resume", "fork"}:
            form = "exec" if token == "e" else token
        elif not positional and form == "exec" and token == "resume":
            form = "exec resume"
        else:
            # tui/src/cli.rs:17–19 and exec/src/cli.rs:88–92 declare one PROMPT.
            # Never reinterpret a later command word as an interactive launch.
            if positional and form in {"interactive", "exec"}:
                raise SessionError("Unexpected child positional argument.")
            if (form in {"interactive", "exec"}) and (
                token in _CHILD_COMMANDS or (form == "exec" and token == "fork")
            ):
                raise SessionError("Unsupported child subcommand: " + token)
            positional = True
    if form in {"resume", "fork", "exec resume"}:
        # A resumed session can reload config relative to its recorded cwd.
        for key, value in overrides:
            if key.split(".")[0] in {"sqlite_home", "log_dir"} and (
                not isinstance(value, str) or not Path(value).is_absolute()
            ):
                raise SessionError("Relative child path override is unsupported for " + form)
    return form, overrides, cwd


def _child_config_keys(source: Path, table: dict[str, Any]) -> None:
    # All forbidden keys are top-level; nested tables may reuse such names harmlessly.
    for key in table:
        # Shared configs may explicitly select the same private file stores
        # that child_argv enforces. Other stores and CLI overrides stay refused.
        if key in {"cli_auth_credentials_store", "mcp_oauth_credentials_store"} \
                and table[key] == "file":
            continue
        if key in _CHILD_FORBIDDEN:
            raise SessionError(f"Unsupported child config key in {source}: {key}")


def _child_paths(home: Path, layers: list[tuple[Path, dict[str, Any], bool]],
                 overrides: list[tuple[str, Any]], cwd: Path) -> list[str]:
    result = []
    for key, default in (("sqlite_home", home), ("log_dir", home / "log")):
        # Every layer value counts, not only the one this parser would rank highest:
        # an escaping value anywhere forces the private override, which wins over all.
        selected = []
        for source, table, managed in layers:
            if key in table:
                value = table[key]
                if managed:
                    if not isinstance(value, str) or not _child_private(value, home, source.parent):
                        raise SessionError("Managed child path escapes home: " + key)
                selected.append((value, source.parent))
        for name, value in overrides:
            if name == key or name.startswith(key + "."):
                if name != key:
                    raise SessionError("Invalid child path override: " + key)
                selected = [(value, cwd)]
        if not selected:
            if not _child_private(str(default), home, cwd):
                raise SessionError("Default child path escapes home: " + key)
        else:
            if any(not isinstance(value, str) for value, _ in selected):
                raise SessionError("Invalid child path: " + key)
            if not all(_child_private(value, home, base) for value, base in selected):
                if not _child_private(str(default), home, cwd):
                    raise SessionError("Default child path escapes home: " + key)
                result.extend(["-c", key + "=" + json.dumps(str(default.resolve()))])
    return result


def _child_has_cd(codex_args: list[str]) -> bool:
    boundary = codex_args.index("--") if "--" in codex_args else len(codex_args)
    return any(arg == "--cd" or arg.startswith(("--cd=", "-C"))
               for arg in codex_args[:boundary])


def child_argv(codex_args: list[str], home: Path, *,
               config_layers: list[tuple[Path, dict[str, Any], bool]],
               cwd: Path | None = None) -> list[str]:
    """Validate supported invocation forms and add root isolation controls."""
    form, overrides, cwd = _child_parse(codex_args, cwd=cwd)
    for source, table, managed in config_layers:
        if not managed:
            _child_config_keys(source, table)
    root = [] if form.startswith("exec") else ["--no-daemon"]
    if form in {"resume", "fork"} and not _child_has_cd(codex_args):
        # Without --cd the TUI may reload config in the recorded session cwd
        # (tui/src/session_resume.rs:35–44); pin the cwd whose layers were checked.
        root.extend(["--cd", str(cwd.resolve())])
    # Global -c reaches exec/resume too: utils/cli/src/config_override.rs:30–36;
    # cli/src/config_override_args.rs:25–39 preserves overrides across scopes.
    root.extend(["-c", 'cli_auth_credentials_store="file"',
                 "-c", 'mcp_oauth_credentials_store="file"'])
    # Global replacements follow all user overrides, including those in child scopes.
    paths = _child_paths(home, config_layers, overrides, cwd)
    if paths:
        # Put replacements before '--' or after all parsed options; -c is global.
        boundary = codex_args.index("--") if "--" in codex_args else len(codex_args)
        return root + codex_args[:boundary] + paths + codex_args[boundary:]
    return root + codex_args


def _run_layers(home: Path, cwd: Path) -> list[tuple[Path, dict[str, Any], bool]]:
    sources = [(p, p.name != "config.toml") for p in _system_policy_paths()]
    sources += [(home / "config.toml", False), (cwd / "config.toml", False)]
    # Codex loads project layers root first, nearest last (config/src/loader/mod.rs:1862).
    sources += [(p, False) for p in reversed(_project_policy_paths(cwd))]
    if sys.platform == "win32":
        requirements = _windows_requirements_path()
        if requirements is not None:
            sources.append((requirements, True))
    layers = []
    for path, managed in sources:
        try:
            with path.open("rb") as stream:
                table = tomllib.load(stream)
        except FileNotFoundError:
            # A dangling config link is not an absent configuration.
            if path.is_symlink():
                _policy_error(path, "unreadable configuration")
            continue
        except (OSError, ValueError):
            _policy_error(path, "unreadable configuration")
        layers.append((path, table, managed))
    return layers


def _run_notice(message: str) -> None:
    print(message, file=sys.stderr)


def _run_refuse(message: str) -> None:
    _run_notice(message)
    raise SessionError("Codex run refused.")


def _run_custody(switcher: CodexAccountSwitcher, number: str, shared: bool) -> Custody:
    custody = slot_custody(switcher, number)
    if custody.state == "uncertain" or custody.issue_count or custody.invalid_records:
        _run_refuse(f"Account {number}: session liveness is uncertain; credential writes refused. "
                           f"{custody.reason}. Stop identified processes or repair scan "
                           "permissions, then retry.")
    if custody.state == "conflict":
        _run_refuse(f"Account {number}: session/backup identity or generation conflict; "
                           f"files retained at {slot_home(switcher, number)}. "
                           "Reconcile the login explicitly before retrying.")
    if custody.state == "live" and not shared:
        _run_refuse(f"Account {number} already has a live session. Use --shared to join with "
                           "the same refresh race as several Codex processes in ~/.codex.")
    return custody


def _run_prepare(switcher: CodexAccountSwitcher, number: str, shared: bool,
                 args: list[str], env: dict[str, str],
                 cwd: Path) -> tuple[Path, list[str], dict[str, str]]:
    custody = _run_custody(switcher, number, shared)
    if custody.state != "live":
        switcher._guard_session_slot(number)
    home = slot_home(switcher, number)
    if custody.state == "none":
        home = bootstrap_home(switcher.backup_dir, switcher.codex_home, number,
                              switcher._read_session_backup(number))
    _private_credentials(home)
    manifest_path = home / ".ccswap-session.json"
    manifest = load_record(manifest_path, "manifest")
    auth = load_document(home / "auth.json")
    identity, plan = extract_identity(auth)
    _require(same_identity(identity, manifest["identity"])
             and identity["email"] == manifest["identity"]["email"])
    if custody.state == "live":
        _require(not custody.intent)
        stored = switcher._read_session_backup(number)
        _require(same_identity(extract_identity(stored)[0], identity)
                 and auth_fingerprint(stored) in {manifest["baselineFingerprint"],
                                                  auth_fingerprint(auth)})
        _run_notice("Shared session: refresh tokens may race. "
                    "Capture pending until all writers exit.")
    _, notices = sync_sharing(switcher.backup_dir, home, switcher.codex_home, manifest,
                             lambda value: _session_write(
                                 switcher.backup_dir, manifest_path, value))
    for notice in notices:
        _run_notice(notice)
    _, _, effective = _child_parse(args, cwd=cwd)
    check_login_policy(home, effective, identity, plan)
    layers = _run_layers(home, effective)
    for source, table, managed in layers:
        _check_policy_table(table, source, identity["workspaceId"])
        if managed:
            _check_managed_routing(table, source)
    child, notices = child_env(env, home, cwd=cwd)
    if notices:
        names = [notice.removeprefix("Removed ").removesuffix(".") for notice in notices]
        _run_notice("Ignoring " + ", ".join(names) + " for this isolated run.")
    return home, child_argv(args, home, config_layers=layers, cwd=cwd), child


def _run_subreaper() -> None:
    """Adopt descendants so zombies cannot obstruct the final home scan.

    Linux status is process-wide and retained by this CLI main process.
    """
    if sys.platform == "linux":
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
            _run_refuse("Cannot establish Codex descendant supervision.")


def _run_identity(pid: int) -> dict[str, Any]:
    from claude_swap import process_detection, windows_job

    start = (windows_job.process_identity(pid)[1] if sys.platform == "win32"
             else process_detection.codex_process_identity(pid))
    if start is None:
        _run_refuse("Cannot establish Codex process identity.")
    return {"pid": pid, "start": start}


def _run_wait(process: Any) -> int:
    import signal

    previous = {}
    if sys.platform == "win32":
        # The console delivers Ctrl-C/Ctrl-Break to Codex directly; a KeyboardInterrupt
        # here would skip the run's finish and capture.
        try:
            for signum in (signal.SIGINT, getattr(signal, "SIGBREAK", None)):
                if signum is not None:
                    previous[signum] = signal.signal(signum, signal.SIG_IGN)
            return process.wait()
        finally:
            for signum, handler in previous.items():
                signal.signal(signum, signal.SIG_DFL if handler is None else handler)
    def forward(signum: int, frame: Any) -> None:
        try:
            process.send_signal(signum)
        except ProcessLookupError:
            pass
    try:
        # The terminal already delivers SIGINT to the child's process group; forwarding
        # it again would turn one Ctrl-C into Codex's double-press quit.
        previous[signal.SIGINT] = signal.signal(signal.SIGINT, signal.SIG_IGN)
        for signum in (signal.SIGTERM, signal.SIGHUP):
            previous[signum] = signal.signal(signum, forward)
        result = process.wait()
        return result if result >= 0 else 128 - result
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, signal.SIG_DFL if handler is None else handler)


def _run_reap() -> None:
    if sys.platform == "linux":
        # The CLI owns its children; probes have already been reaped.
        try:
            while os.waitpid(-1, os.WNOHANG)[0]:
                pass
        except ChildProcessError:
            pass


def _run_finish(switcher: CodexAccountSwitcher, number: str, record: dict[str, Any],
                result: int) -> int:
    from claude_swap.locking import FileLock

    home = Path(record["home"])
    deadline = time.monotonic() + 1
    while True:
        _run_reap()
        with FileLock(switcher.lock_file):
            record.update(phase="finished", exitCode=result)
            _session_write(switcher.backup_dir, home / "runs" / (record["runId"] + ".json"), record)
            custody = slot_custody(switcher, number, completed=record["supervisor"])
            if custody.state == "quiescent":
                if custody.intent:
                    recover_session_credentials(switcher, number)
                outcome = reconcile_session(switcher, number, quiescent=True)
                if outcome.state == "conflict":
                    _run_refuse(f"Account {number}: session/backup identity or generation "
                                "conflict; "
                                f"files retained at {home}. Reconcile the login explicitly "
                                "before retrying.")
                return result
            if custody.state == "conflict":
                _run_refuse(f"Account {number}: session/backup identity or generation "
                            f"conflict; files retained at {home}. Reconcile the login "
                            "explicitly before retrying.")
            if custody.state != "live" or custody.issue_count or custody.invalid_records:
                _run_refuse("Codex session liveness is uncertain; credential writes refused. "
                                   f"{custody.reason}. Stop identified processes or repair scan "
                                   "permissions, then retry.")
        if time.monotonic() >= deadline:
            _run_notice(f"Account {number}: capture pending; "
                        "session-home processes are still live.")
            return result
        time.sleep(0.05)


def run_session(switcher: CodexAccountSwitcher, identifier: str | None, *,
                require_session: bool, shared: bool, codex_args: list[str],
                env: dict[str, str], cwd: Path) -> int:
    """Supervise one terminal run. Caller MUST refuse_inherited_home before constructing switcher.

    """
    import signal
    import subprocess
    from uuid import uuid4
    from claude_swap import windows_job
    from claude_swap.codex_binary import probe_codex, resolve_codex_binary
    from claude_swap.exceptions import ClaudeSwitchError
    from claude_swap.locking import FileLock

    record, process, committed, ran = None, None, False, False
    try:
        cwd = Path(cwd).resolve(strict=True)
        refuse_inherited_home(env)
        def select() -> tuple[str, bool]:
            data = switcher._read_sequence()
            default = switcher._read_switch_auth()
            _require(not default or default.get("tokens") is None
                     or isinstance(default["tokens"], dict))
            aliases = switcher._default_session_slots(default, data) if default else []
            if default and aliases:
                actual, _ = extract_identity(default)
                _require(all(same_identity(actual, extract_identity(
                    switcher._read_session_backup(alias))[0]) for alias in aliases))
            if identifier is None:
                _require(len(aliases) == 1)
                number = aliases[0]
            else:
                number = switcher._resolve(identifier, data)
            if switcher._disabled_from_data(data, number):
                _run_refuse("Disabled Codex account cannot be launched.")
            plain = number in aliases
            # Only the plain shortcut runs on the default's token family; another
            # account's isolated run never touches an alias's session.
            for alias in aliases if plain else []:
                custody = _run_custody(switcher, alias, False)
                if custody.state == "quiescent":
                    _require(not custody.intent
                             and classify_session(switcher, alias).state != "conflict")
            if not plain and sys.platform == "darwin":
                _run_refuse("macOS session supervision awaits native CI validation.")
            if default and not plain and isinstance(default.get("tokens"), dict):
                saved = switcher._read_session_backup(number)
                extract_identity(saved)
                _require(not any(default.get("tokens", {}).get(key)
                    and default["tokens"][key] == saved.get("tokens", {}).get(key)
                    for key in ("access_token", "refresh_token")))
            if plain:
                if require_session:
                    _run_refuse(f"Account {number} is the default login; "
                                       "--require-session refuses an unisolated run.")
            return number, plain

        with FileLock(switcher.lock_file):
            number, plain = select()
        if not plain:
            try:
                _child_parse(codex_args, cwd=cwd)
            except SessionError:
                _run_notice("Unsupported Codex launch syntax.")
                return 2
        binary = resolve_codex_binary(env)
        if not plain:
            probe_codex(binary)  # External commands never hold the account-store lock.
        with FileLock(switcher.lock_file):
            # A probe can take seconds: recheck selection, identity and custody.
            if select() != (number, plain):
                _run_refuse("Codex state changed during preflight; retry.")
            if not plain:
                home, args, child = _run_prepare(switcher, number, shared, codex_args, env, cwd)
                _run_subreaper()
                run_id = str(uuid4())
                record = dict(schemaVersion=1, runId=run_id, home=str(home),
                              supervisor=_run_identity(os.getpid()), child=None, phase="starting",
                              shared=shared, jobName=windows_job.job_name(run_id)
                              if sys.platform == "win32" else None, exitCode=None)
                _private_directory(home / "runs", switcher.backup_dir)
                path = home / "runs" / (run_id + ".json")
                def persist_child(pid: int, start: dict[str, Any]) -> None:
                    record.update(child={"pid": pid, "start": start}, phase="running")
                    _session_write(switcher.backup_dir, path,
                                   validate_record(record, "run", run_id=run_id))
                _session_write(switcher.backup_dir, path,
                               validate_record(record, "run", run_id=run_id))
                committed = True
                if sys.platform == "win32":
                    process = windows_job.launch(binary[0], args, child, str(cwd), run_id,
                                                 persist_child)
                else:
                    process = subprocess.Popen(binary + args, env=child, cwd=cwd, shell=False)
                    identity = _run_identity(process.pid)
                    persist_child(process.pid, identity["start"])
        if plain:
            _run_notice("Using the default Codex login; this run is not isolated.")
            process = subprocess.Popen(binary + codex_args, env=env, cwd=cwd, shell=False)
            return _run_wait(process)
        # Keep Ctrl-C from interrupting the finish; the child got its own interrupt.
        interrupts = [signal.SIGINT]
        if sys.platform == "win32" and getattr(signal, "SIGBREAK", None) is not None:
            interrupts.append(signal.SIGBREAK)
        previous = {}
        try:
            for signum in interrupts:
                previous[signum] = signal.signal(signum, signal.SIG_IGN)
            result = _run_wait(process)
            if sys.platform == "win32":
                process.close()
            process, committed, ran = None, False, True
            return _run_finish(switcher, number, record, result)
        finally:
            for signum, handler in previous.items():
                signal.signal(signum, signal.SIG_DFL if handler is None else handler)
    except (ClaudeSwitchError, OSError, ValueError, TypeError, KeyError, RuntimeError,
            KeyboardInterrupt) as exc:
        # Session and switch errors carry fixed, token-free reasons worth showing.
        if isinstance(exc, ClaudeSwitchError) and str(exc) != "Codex run refused.":
            _run_notice(str(exc))
        if ran:
            _run_notice("Codex exited, but the session capture failed; evidence retained.")
        else:
            _run_notice("Codex session setup or capture failed; evidence retained."
                        if committed else "Codex session was not started.")
        if committed:
            try:
                if process is not None:
                    if sys.platform == "win32":
                        process.close()
                    else:
                        process.kill()
                        process.wait()
                _run_finish(switcher, number, record, 1)
            except (ClaudeSwitchError, OSError, ValueError, TypeError, KeyError, RuntimeError):
                pass
        return 1
