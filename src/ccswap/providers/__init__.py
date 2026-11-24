"""Provider registry: static capability data plus lazy factories.

Importing this module must stay cheap and cycle-free, so it never imports
``switcher``, ``codex`` or ``agy`` at module level; factories import inside.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol

from ccswap.models import AccountsSnapshot


class UsageProvider(Protocol):
    """Every provider, usage-only ones included."""

    def accounts_snapshot(self, fetch: set[str] | None = None) -> AccountsSnapshot: ...

    def list_payload(self) -> dict: ...  # schema-v1 section, never prints

    def add_account(self) -> None: ...  # capture the current native login

    def remove_account(self, identifier: str, assume_yes: bool = False) -> None: ...


class SwitchingProvider(UsageProvider, Protocol):
    """Gated by ``capabilities.switch``."""

    def switch_to(self, identifier: str, json_output: bool = False) -> dict: ...


class RotatingProvider(SwitchingProvider, Protocol):
    """Gated by a non-empty ``capabilities.strategies``."""

    def switch(self, strategy: str | None = None, json_output: bool = False) -> dict: ...


class DisablingProvider(Protocol):
    """Gated by ``capabilities.disable``."""

    def set_account_disabled(self, identifier: str, disabled: bool) -> None: ...


@dataclass(frozen=True)
class Capabilities:
    switch: bool = True  # False = usage-only provider
    strategies: tuple[str, ...] = ()
    disable: bool = False
    auto_switch: bool = False
    restart_after_switch: bool = False
    session_mode: bool = False  # `run` subcommand
    warmup: bool = False
    plans: bool = False  # refresh_subscription_plans
    add_token: bool = False


@dataclass(frozen=True)
class ProviderSpec:
    id: str
    label: str
    capabilities: Capabilities
    factory: Callable[..., UsageProvider]  # imports lazily inside
    cli: Callable[[list[str]], None] | None = None  # None = generic verb parser


def _claude(debug: bool = False) -> UsageProvider:
    from ccswap.switcher import ClaudeAccountSwitcher

    return ClaudeAccountSwitcher(debug=debug)


def _codex(debug: bool = False) -> UsageProvider:
    from ccswap.codex import CodexAccountSwitcher

    return CodexAccountSwitcher(debug=debug)


def _agy(debug: bool = False) -> UsageProvider:
    from ccswap.agy import AgyAccountSwitcher

    return AgyAccountSwitcher(debug=debug)


def _codex_cli(argv: list[str]) -> None:
    from ccswap.cli import _codex_command

    _codex_command(argv)


_STRATEGIES = ("best", "next-available", "consume-first")

# Insertion order is display order.
PROVIDERS: dict[str, ProviderSpec] = {
    "claude": ProviderSpec(
        "claude",
        "Claude Code",
        Capabilities(
            strategies=_STRATEGIES,
            disable=True,
            auto_switch=True,
            session_mode=True,
            warmup=True,
            plans=True,
            add_token=True,
        ),
        _claude,
    ),
    "codex": ProviderSpec(
        "codex",
        "Codex",
        Capabilities(
            strategies=_STRATEGIES,
            disable=True,
            auto_switch=True,
            restart_after_switch=True,
            session_mode=True,
        ),
        _codex,
        _codex_cli,
    ),
    "agy": ProviderSpec(
        "agy",
        "Antigravity",
        Capabilities(restart_after_switch=True),
        _agy,
    ),
}


def get(provider_id: str) -> ProviderSpec:
    return PROVIDERS[provider_id]
