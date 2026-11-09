"""Tool registry: adding a vendor means writing one adapter and listing it here."""

from __future__ import annotations

from collections.abc import Callable, Iterable

from claude_swap.vendors.core import Runner, ToolAdapter, VendorError
from claude_swap.vendors.kiro import KiroAdapter
from claude_swap.vendors.muse import MuseAdapter
from claude_swap.vendors.opencode import OpenCodeAdapter

AdapterFactory = Callable[[Runner | None], ToolAdapter]

# Order is display order (CLI, TUI, JSON).
DEFAULT_FACTORIES: tuple[AdapterFactory, ...] = (MuseAdapter, OpenCodeAdapter, KiroAdapter)


class ToolRegistry:
    def __init__(self, adapters: Iterable[ToolAdapter]) -> None:
        self._adapters = {a.key: a for a in adapters}

    @classmethod
    def default(cls, runner: Runner | None = None) -> ToolRegistry:
        return cls(factory(runner) for factory in DEFAULT_FACTORIES)

    def keys(self) -> list[str]:
        return list(self._adapters)

    def adapters(self) -> list[ToolAdapter]:
        return list(self._adapters.values())

    def get(self, key: str) -> ToolAdapter:
        try:
            return self._adapters[key]
        except KeyError:
            raise VendorError(
                f"Unknown vendor '{key}'. Known: {', '.join(self._adapters)}"
            ) from None
