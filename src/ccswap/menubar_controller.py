"""UI-agnostic menu bar state and actions, shared by the popover and classic shells.

Contract stub (menu bar phase, step 0). Implementer A fills it in; the
signatures and the payload shape below are frozen. Plan:
/private/tmp/claude-501/-Users-rhythm-Developer-cc-swap/380a1ec1-9bdb-4323-b32b-2e763919b93a/scratchpad/phase-menubar/plan.md

Threading: shells own the timers. ``tick()``, ``payload()`` and every action
run on the main thread; refresh workers only rebind plain attributes and
append to lock-guarded queues, so nothing here touches AppKit.

Payload (``build_payload``), pushed to the popover page as JSON::

    {"schemaVersion": 1,
     "lastCheckedAt": float | None,          # epoch seconds
     "health": {"label": str, "attention": bool},
     "title": {"setting": str, "resolved": str, "text": str},
     "settings": {"style", "titleProvider", "titlePct", "showAccountName",
                  "titleScoped", "refreshInterval", "threshold",
                  "claudeIntegrations": {"statusline": bool, "mod": bool}},
     "history": [str, ...],                  # Claude switch log lines
     "providers": [
        json_output.provider_entry(spec, section, error) + {
            "autoSwitch": {"enabled": bool, "running": bool, "failure": str | None} | None,
            "refreshing": bool,
            "addToken": bool}]}

``section.accounts[]`` rows: number, email, alias, active, disabled,
**json_output.usage_row_fields(...) (so ``windows`` and ``windowsSource``),
plus ``note`` (menubar.menu_note of the sentinel) and ``loginNote``
(oauth.format_login_expiry). Built from the controller's snapshots, never
from ``list_payload()`` (which fetches).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

PAYLOAD_SCHEMA_VERSION = 1

Notify = Callable[[str, str, str], None]  # title, subtitle, message
Alert = Callable[[str, str], None]  # title, message


@dataclass
class ProviderState:
    """Everything the shells know about one registered provider."""

    id: str
    switcher: object | None = None
    error: str | None = None  # the switcher could not be built, or the last refresh failed
    snapshot: object | None = None  # raw models.AccountsSnapshot
    adapted: dict | None = None  # menubar._adapt_snapshot(snapshot)
    refreshing: bool = False
    engine: object | None = None  # running auto-switch engine, if any
    engine_failure: str | None = None


class MenuBarController:
    """State per provider, polling, auto-switch engines, notifications, actions."""

    def __init__(
        self,
        claude_switcher,
        backup_root: Path,
        log_path: Path,
        *,
        notify: Notify,
        alert: Alert,
        providers: dict | None = None,  # default: ccswap.providers.PROVIDERS
        on_interval_changed: Callable[[int], None] | None = None,
    ) -> None:
        raise NotImplementedError

    settings: object  # menubar.MenuBarSettings
    states: dict[str, ProviderState]

    def start(self) -> None:
        """First refresh, then start the engines the settings enable."""
        raise NotImplementedError

    def stop(self) -> None:
        """Stop every engine (quit, or before a style re-exec)."""
        raise NotImplementedError

    def tick(self) -> bool:
        """Main thread, about once a second. True when the shell should repaint."""
        raise NotImplementedError

    def refresh(self, provider_id: str | None = None, full: bool = False) -> None:
        """Start a background refresh for one provider (None = all), one worker each."""
        raise NotImplementedError

    def health(self) -> tuple[str, bool]:
        raise NotImplementedError

    def title_provider(self) -> str:
        """menubar.resolve_title_provider over the active accounts."""
        raise NotImplementedError

    def title(self, icon: bool = True) -> str:
        raise NotImplementedError

    def history(self) -> list[str]:
        raise NotImplementedError

    def payload(self) -> dict:
        return build_payload(self)

    # actions: main thread; ClaudeSwitchError -> alert(), return False
    def switch_to(self, provider_id: str, number: int) -> bool:
        raise NotImplementedError

    def rotate(self, provider_id: str, strategy: str | None) -> bool:
        raise NotImplementedError

    def set_disabled(self, provider_id: str, number: int, disabled: bool) -> bool:
        raise NotImplementedError

    def remove(self, provider_id: str, number: int) -> bool:
        raise NotImplementedError

    def add_login(self, provider_id: str) -> bool:
        raise NotImplementedError

    def add_token(self, email: str, token: str) -> bool:
        raise NotImplementedError

    def refresh_creds(self) -> bool:
        raise NotImplementedError

    def set_auto(self, provider_id: str, enabled: bool) -> bool:
        raise NotImplementedError

    def restart_engine(self, provider_id: str) -> bool:
        raise NotImplementedError

    def set_threshold(self, pct: float) -> bool:
        raise NotImplementedError

    def set_display(self, field: str, value) -> bool:
        """title_pct, show_account_name, title_scoped, refresh_interval, title_provider, style."""
        raise NotImplementedError

    def toggle_claude_integration(self, key: str) -> bool:
        raise NotImplementedError

    def open_log(self) -> None:
        raise NotImplementedError


def build_payload(controller: MenuBarController) -> dict:
    """The popover's JSON payload (shape in the module docstring)."""
    raise NotImplementedError
