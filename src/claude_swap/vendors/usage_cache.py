"""Last-good usage per (tool, account), persisted so every surface can show it.

A failed or impossible refresh never erases a valid earlier observation: the
service returns it as ``STALE`` with its age and the new error.  Entries are
keyed by the account they were *attributed to when observed*, so one account's
numbers are never shown for another.  The file holds usage figures only (no
credentials), is written atomically under the vendor store lock, and a corrupt
file is treated as empty rather than fatal.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from claude_swap.exceptions import ClaudeSwitchError
from claude_swap.fsutil import atomic_write_text
from claude_swap.locking import FileLock
from claude_swap.vendors.core import UsageResult, UsageState

SCHEMA_VERSION = 1
ACTIVE_KEY = "@active"  # "whatever credential the tool is using now", for tools without identity
MAX_AGE_S = 30 * 86400  # older observations are dropped rather than shown


class UsageCache:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.file = self.root / "usage.json"

    def _read(self) -> dict:
        try:
            if self.file.is_symlink():
                return {}
            doc = json.loads(self.file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        entries = doc.get("entries") if isinstance(doc, dict) else None
        return entries if isinstance(entries, dict) else {}

    @staticmethod
    def _key(tool: str, account: str) -> str:
        return f"{tool}\x1f{account}"

    def get(self, tool: str, account: str) -> UsageResult | None:
        raw = self._read().get(self._key(tool, account))
        if not isinstance(raw, dict):
            return None
        try:
            result = UsageResult.from_json(raw)
        except (ValueError, TypeError):
            return None
        if result.observed_at is not None and time.time() - result.observed_at > MAX_AGE_S:
            return None
        return result

    def put(self, tool: str, account: str, result: UsageResult) -> None:
        if result.state is not UsageState.AVAILABLE or not result.has_data:
            return
        self._update(
            lambda entries: entries.__setitem__(self._key(tool, account), result.to_json())
        )

    def drop(self, tool: str, account: str) -> None:
        self._update(lambda entries: entries.pop(self._key(tool, account), None))

    def _update(self, change) -> None:
        try:
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
            with FileLock(self.root / ".usage.lock", timeout=5.0):
                entries = self._read()
                change(entries)
                atomic_write_text(
                    self.file,
                    json.dumps(
                        {"schemaVersion": SCHEMA_VERSION, "entries": entries}, sort_keys=True
                    ),
                    follow_symlinks=False,  # never write through a planted link
                )
        except (OSError, ClaudeSwitchError):
            pass  # a cache that cannot be written (or locked) only costs freshness
