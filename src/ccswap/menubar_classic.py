"""Classic rumps text-menu shell for the menu bar.

Contract stub (menu bar phase, step 0); implementer C fills it in. Every
provider in the registry is an equal section. ``menu_spec`` is pure so tests
need no rumps; the shell renders it and calls
``getattr(controller, row.action)(*row.args)`` on click.

Shell module API (same for menubar_popover): ``notify``, ``alert``, ``run``.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MenuRow:
    label: str
    action: str | None = None  # MenuBarController method name; None = info row
    args: tuple = ()
    state: bool | None = None  # checkmark
    tooltip: str = ""
    children: tuple["MenuRow", ...] = ()
    separator: bool = False


def menu_spec(controller) -> list[MenuRow]:
    """The whole menu, top to bottom."""
    raise NotImplementedError


def notify(title: str, subtitle: str, message: str) -> None:
    raise NotImplementedError


def alert(title: str, message: str) -> None:
    raise NotImplementedError


def run(controller) -> int:
    """Build the rumps app around the controller; blocks until quit."""
    raise NotImplementedError
