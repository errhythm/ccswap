"""Multi-account and usage manager for Claude Code and Codex."""

from importlib.metadata import version

__version__ = version("ccswap")

__all__ = ["ClaudeAccountSwitcher", "__version__"]


def __getattr__(name: str):
    # Lazy so importing ccswap.providers does not pull in the 8.9k-line switcher.
    if name == "ClaudeAccountSwitcher":
        from ccswap.switcher import ClaudeAccountSwitcher

        return ClaudeAccountSwitcher
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
