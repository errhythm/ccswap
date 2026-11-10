"""A private, throwaway directory for running a vendor CLI as one account.

Muse reads its login from ``$XDG_CONFIG_HOME/muse/auth.json`` and Kiro from
``$HOME/Library/Application Support/kiro-cli``, so pointing that variable at a
fresh directory lets ccswap measure an account that is not the signed-in one
without touching the live login.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from types import TracebackType


class IsolatedHome:
    """A 0700 temporary directory, removed on exit whatever happened inside."""

    def __init__(self, prefix: str) -> None:
        self._prefix = prefix
        self.path: Path | None = None

    def __enter__(self) -> Path:
        self.path = Path(tempfile.mkdtemp(prefix=f"ccswap-{self._prefix}-"))
        os.chmod(self.path, 0o700)
        return self.path

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self.path is not None:
            shutil.rmtree(self.path, ignore_errors=True)

    @staticmethod
    def write_private(path: Path, text: str) -> None:
        """Write ``text`` to ``path`` with 0700 parents and a 0600 file."""
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
