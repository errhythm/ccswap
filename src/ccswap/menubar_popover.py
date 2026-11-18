"""Popover shell for the menu bar: NSStatusItem + NSPopover hosting a WKWebView.

Contract stub (menu bar phase, step 0); implementer B fills it in. The page
lives in ``menubar_web/``. All AppKit/WebKit imports stay inside functions so
this module imports without pyobjc.

Shell module API (same for menubar_classic): ``notify``, ``alert``, ``run``.
"""

from __future__ import annotations

# Messages the page may post; anything else is dropped.
BRIDGE_ACTIONS = frozenset({
    "ready", "refresh", "switch", "rotate", "toggleAuto", "restartEngine",
    "toggleDisabled", "remove", "addLogin", "addToken", "setTitleProvider",
    "setTitlePct", "setInterval", "setThreshold", "toggleName", "toggleScoped",
    "setStyle", "toggleIntegration", "openLog", "quit",
})


def dispatch(controller, body) -> dict | None:
    """Validate one page message and run it on the controller.

    Whitelist, provider in the registry, integer account number, then the
    capability gate. Returns a reply for the page, or None.
    """
    raise NotImplementedError


def notify(title: str, subtitle: str, message: str) -> None:
    raise NotImplementedError


def alert(title: str, message: str) -> None:
    raise NotImplementedError


def run(controller) -> int:
    """Create the status item and lazy popover; blocks until quit."""
    raise NotImplementedError
