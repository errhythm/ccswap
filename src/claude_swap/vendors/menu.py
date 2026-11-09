"""Menu-bar model for the vendor submenu (no AppKit/rumps imports: unit-testable).

The menu bar shares the backend with the CLI and TUI (:class:`VendorService`).
Only OpenCode accounts are switchable from here: Muse replaces the stored
credential (needs confirmation) and Kiro needs an interactive terminal login,
so those rows are shown, greyed out, with the exact command to run.
"""

from __future__ import annotations

from dataclasses import dataclass

from claude_swap.exceptions import ClaudeSwitchError
from claude_swap.vendors.service import VendorService


@dataclass(frozen=True)
class MenuRow:
    label: str
    tool: str
    selector: str | None = None  # set only when the row is switchable
    provider: str | None = None


def switchable(tool: str, active: bool | None, provider: str | None) -> bool:
    return tool == "opencode" and active is False and bool(provider)


def collect(service: VendorService) -> dict[str, list[MenuRow]]:
    """Blocking (spawns vendor CLIs): call from a worker thread. Failures stay per vendor."""
    result: dict[str, list[MenuRow]] = {}
    for adapter in service.registry.adapters():
        tool = adapter.key
        rows: list[MenuRow] = []
        try:
            info = adapter.detect()
            if not info.installed:
                rows.append(MenuRow(f"{adapter.label} — not installed", tool))
            else:
                rows.append(MenuRow(f"{adapter.label} {info.version or ''}".strip(), tool))
                views = service.list_views(tool)
                if not views:
                    rows.append(MenuRow(f"  no {adapter.label} accounts", tool))
                for view in views:
                    mark = {True: "●", False: "○", None: "?"}[view.active]
                    where = f"{view.provider}/" if view.provider and tool != "opencode" else ""
                    ok = switchable(tool, view.active, view.provider)
                    selector = (
                        (view.identity.get("id") or view.identity.get("account")) if ok else None
                    )
                    hint = "" if ok or view.active else "  (switch in terminal)"
                    rows.append(
                        MenuRow(
                            f"  {mark} {where}{view.alias}{hint}", tool, selector, view.provider
                        )
                    )
        except ClaudeSwitchError as exc:
            rows.append(MenuRow(f"{adapter.label} — {exc}"[:80], tool))
        result[tool] = rows
    return result
