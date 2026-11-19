"""Logging configuration for Claude Swap."""

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path


class _LazyDirRotatingFileHandler(RotatingFileHandler):
    """RotatingFileHandler that creates its parent dir on first emit.

    Keeps the backup root from being materialized just because the switcher
    was instantiated. Necessary so a no-op run (e.g. ``cswap --status`` with
    no managed accounts) doesn't lay down ``cache/`` or log files inside the
    XDG path, which would later trip the legacy → XDG migration collision
    check if a legacy directory appeared between runs.
    """

    def _open(self):  # type: ignore[override]
        Path(self.baseFilename).parent.mkdir(parents=True, exist_ok=True)
        return super()._open()


def setup_logging(log_dir: Path, debug: bool = False) -> logging.Logger:
    """Setup logging with file and optional console output.

    The log directory is *not* created eagerly; it materializes on the first
    log record actually written, via ``_LazyDirRotatingFileHandler``.

    Args:
        log_dir: Directory to store log files.
        debug: Enable debug logging to console.

    Returns:
        Configured logger instance.
    """
    logger = logging.getLogger("ccswap")
    logger.setLevel(logging.DEBUG if debug else logging.INFO)

    # Clear any existing handlers
    logger.handlers.clear()

    # File handler - opens lazily so the dir is only created when something
    # is actually logged.
    log_file = log_dir / "claude-swap.log"
    file_handler = _LazyDirRotatingFileHandler(
        log_file,
        maxBytes=1024 * 1024,  # 1MB
        backupCount=3,
        delay=True,
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    )
    logger.addHandler(file_handler)

    # Console handler for debug mode
    if debug:
        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.DEBUG)
        console_handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
        logger.addHandler(console_handler)

    return logger


def log_switch(log_dir: Path, provider: str, source: object, target: object) -> None:
    """Record a Codex or Antigravity switch in the shared switch log.

    Claude's switcher writes "Switched from account X to Y"; these lines name
    the provider so readers that only want Claude (the menu bar) skip them.
    """
    logger = logging.getLogger("ccswap")
    log_file = os.path.abspath(Path(log_dir) / "claude-swap.log")
    # A provider-only process (`ccswap codex switch`) has no handler yet; one
    # set up for another root must not receive this line either.
    if not any(getattr(h, "baseFilename", None) == log_file for h in logger.handlers):
        setup_logging(log_dir)
    logger.info(f"Switched {provider} from account {source or '?'} to {target}")
