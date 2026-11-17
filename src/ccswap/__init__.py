"""Multi-account and usage manager for Claude Code and Codex."""

from importlib.metadata import version

__version__ = version("ccswap")

from ccswap.switcher import ClaudeAccountSwitcher

__all__ = ["ClaudeAccountSwitcher", "__version__"]
