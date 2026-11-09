"""Namespaced, versioned, lock-protected storage for vendor account metadata.

``<backup-root>/vendors/vendors.json`` holds metadata only (aliases, kinds,
non-secret identifiers).  API keys that CCSwap itself must keep live in
``<backup-root>/vendors/secrets/<random-id>.secret``: a 0600 file inside a 0700
directory, named by a random id (never by the alias, so aliases cannot traverse
paths).  This is the permission-protected fallback the OS keychains are not
used for: ``security add-generic-password`` would put the key into a process
argument list, which is exactly what this project must never do.  Anyone who
can read your home directory as you can read these files -- the same trust
boundary as ``~/.config/muse/auth.json`` -- and that limitation is documented.
"""

from __future__ import annotations

import json
import os
import re
import secrets as _secrets
import stat
import time
from pathlib import Path
from typing import Any

from claude_swap import paths
from claude_swap.fsutil import atomic_write_text
from claude_swap.locking import FileLock
from claude_swap.vendors.core import AccountRecord, VendorError

SCHEMA_VERSION = 1
_ALIAS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@+-]{0,63}$")
_SECRET_REF_RE = re.compile(r"^[a-z]+-[0-9a-f]{32}$")


def validate_alias(alias: str) -> str:
    if not isinstance(alias, str) or not _ALIAS_RE.match(alias):
        raise VendorError(
            "Invalid alias: use 1-64 letters, digits, '.', '_', '@', '+' or '-', "
            "starting with a letter or digit"
        )
    return alias


def _private_dir(path: Path) -> None:
    if path.is_symlink():
        raise VendorError("Vendor storage directory is unsafe (symbolic link)")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not path.is_dir():
        raise VendorError("Vendor storage directory is unsafe")
    if os.name != "nt":
        path.chmod(0o700)


class VendorStore:
    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root) if root else paths.get_backup_root() / "vendors"
        self.file = self.root / "vendors.json"
        self.secrets_dir = self.root / "secrets"

    # ---- document -------------------------------------------------------

    def _lock(self) -> FileLock:
        _private_dir(self.root)
        return FileLock(self.root / ".lock", timeout=10.0)

    def _read(self) -> dict[str, Any]:
        if not self.file.exists():
            return {"schemaVersion": SCHEMA_VERSION, "tools": {}}
        if self.file.is_symlink():
            raise VendorError("Vendor account data is unsafe (symbolic link)")
        try:
            doc = json.loads(self.file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise VendorError(
                f"Vendor account data is unreadable; leaving {self.file} untouched. "
                "Restore it from a backup or remove it to start fresh"
            ) from None
        if not isinstance(doc, dict) or not isinstance(doc.get("tools", {}), dict):
            raise VendorError(f"Vendor account data has an unexpected shape: {self.file}")
        version = doc.get("schemaVersion", 1)
        if not isinstance(version, int) or version > SCHEMA_VERSION:
            raise VendorError(
                "Vendor account data was written by a newer ccswap; upgrade before editing it"
            )
        doc.setdefault("tools", {})
        return doc

    def _write(self, doc: dict[str, Any]) -> None:
        doc["schemaVersion"] = SCHEMA_VERSION
        atomic_write_text(self.file, json.dumps(doc, indent=2, sort_keys=True) + "\n")

    @staticmethod
    def _tool(doc: dict[str, Any], tool: str) -> dict[str, Any]:
        entry = doc["tools"].setdefault(tool, {})
        if not isinstance(entry, dict):
            raise VendorError("Vendor account data has an unexpected shape")
        entry.setdefault("accounts", [])
        return entry

    # ---- records --------------------------------------------------------

    @staticmethod
    def _record(tool: str, raw: Any) -> AccountRecord | None:
        if not isinstance(raw, dict) or not isinstance(raw.get("alias"), str):
            return None  # corrupted entry: skipped, but preserved on disk
        ref = raw.get("secretRef")
        if ref is not None and not (isinstance(ref, str) and _SECRET_REF_RE.match(ref)):
            return None
        ident = raw.get("identity")
        hint = raw.get("loginHint")
        return AccountRecord(
            tool=tool,
            alias=raw["alias"],
            kind=str(raw.get("kind", "")),
            provider=raw.get("provider") if isinstance(raw.get("provider"), str) else None,
            identity={str(k): str(v) for k, v in ident.items()} if isinstance(ident, dict) else {},
            secret_ref=ref,
            login_hint={str(k): str(v) for k, v in hint.items()} if isinstance(hint, dict) else {},
            created_at=float(raw.get("createdAt") or 0.0),
        )

    @staticmethod
    def _raw(record: AccountRecord) -> dict[str, Any]:
        return {
            "alias": record.alias,
            "kind": record.kind,
            "provider": record.provider,
            "identity": dict(record.identity),
            "secretRef": record.secret_ref,
            "loginHint": dict(record.login_hint),
            "createdAt": record.created_at,
        }

    def records(self, tool: str) -> list[AccountRecord]:
        doc = self._read()
        entry = doc["tools"].get(tool, {})
        raws = entry.get("accounts", []) if isinstance(entry, dict) else []
        found = (self._record(tool, raw) for raw in raws)
        return [r for r in found if r is not None]

    def add(self, record: AccountRecord, secret: str | None = None) -> AccountRecord:
        validate_alias(record.alias)
        with self._lock():
            doc = self._read()
            entry = self._tool(doc, record.tool)
            existing = [self._record(record.tool, r) for r in entry["accounts"]]
            if any(
                r and r.alias.lower() == record.alias.lower() and r.provider == record.provider
                for r in existing
            ):
                raise VendorError(f"An account named '{record.alias}' is already registered")
            ref = None
            if secret is not None:
                ref = self._write_secret(record.tool, secret)
            stored = AccountRecord(
                tool=record.tool,
                alias=record.alias,
                kind=record.kind,
                provider=record.provider,
                identity=record.identity,
                secret_ref=ref,
                login_hint=record.login_hint,
                created_at=record.created_at or time.time(),
            )
            entry["accounts"].append(self._raw(stored))
            try:
                self._write(doc)
            except BaseException:
                self._drop_secret(ref)
                raise
            return stored

    def remove(self, tool: str, alias: str, provider: str | None = None) -> AccountRecord:
        """Forget a registration (and CCSwap's own copy of its API key).

        The vendor's own credential store is never touched.
        """
        with self._lock():
            doc = self._read()
            entry = self._tool(doc, tool)
            for index, raw in enumerate(entry["accounts"]):
                rec = self._record(tool, raw)
                if rec and rec.alias == alias and rec.provider == provider:
                    del entry["accounts"][index]
                    if entry.get("active") == alias:
                        entry["active"] = None
                    self._write(doc)
                    self._drop_secret(rec.secret_ref)
                    return rec
        raise VendorError(f"No registered {tool} account named '{alias}'")

    def active(self, tool: str, provider: str | None = None) -> str | None:
        doc = self._read()
        entry = doc["tools"].get(tool, {})
        if not isinstance(entry, dict):
            return None
        if provider is not None:
            by_provider = entry.get("activeByProvider")
            value = by_provider.get(provider) if isinstance(by_provider, dict) else None
        else:
            value = entry.get("active")
        return value if isinstance(value, str) else None

    def set_active(self, tool: str, alias: str | None, provider: str | None = None) -> None:
        with self._lock():
            doc = self._read()
            entry = self._tool(doc, tool)
            if provider is None:
                entry["active"] = alias
            else:
                by_provider = entry.setdefault("activeByProvider", {})
                by_provider[provider] = alias
            self._write(doc)

    # ---- secrets --------------------------------------------------------

    def _write_secret(self, tool: str, secret: str) -> str:
        _private_dir(self.secrets_dir)
        ref = f"{re.sub('[^a-z]', '', tool.lower())}-{_secrets.token_hex(16)}"
        atomic_write_text(self.secrets_dir / f"{ref}.secret", secret, mode=0o600)
        return ref

    def read_secret(self, record: AccountRecord) -> str:
        ref = record.secret_ref
        if not ref or not _SECRET_REF_RE.match(ref):
            raise VendorError(f"Account '{record.alias}' has no stored credential")
        path = self.secrets_dir / f"{ref}.secret"
        try:
            if path.is_symlink() or self.secrets_dir.is_symlink():
                raise OSError
            mode = path.stat().st_mode
            if os.name != "nt" and mode & (stat.S_IRWXG | stat.S_IRWXO):
                path.chmod(0o600)  # repair, never trust a loosened mode silently
            value = path.read_text(encoding="utf-8")
        except OSError:
            raise VendorError(
                f"The stored API key for '{record.alias}' is missing or unsafe; "
                "remove and re-add the account"
            ) from None
        if not value.strip():
            raise VendorError(f"The stored API key for '{record.alias}' is empty")
        return value

    def replace_secret(self, record: AccountRecord, secret: str) -> None:
        """Overwrite an account's stored secret in place (e.g. a refreshed login)."""
        ref = record.secret_ref
        if not ref or not _SECRET_REF_RE.match(ref):
            raise VendorError(f"Account '{record.alias}' has no stored credential")
        _private_dir(self.secrets_dir)
        path = self.secrets_dir / f"{ref}.secret"
        if path.is_symlink():
            raise VendorError("Stored credential is unsafe (symbolic link)")
        atomic_write_text(path, secret, mode=0o600, follow_symlinks=False)

    def _drop_secret(self, ref: str | None) -> None:
        if ref and _SECRET_REF_RE.match(ref):
            try:
                (self.secrets_dir / f"{ref}.secret").unlink()
            except OSError:
                pass
