"""Classic rumps text-menu shell for the menu bar.

Every provider in the registry is an equal section. ``menu_spec`` is pure so
tests need no rumps; the shell renders it and calls
``getattr(controller, row.action)(*row.args)`` on click.

Shell module API (same for menubar_popover): ``notify``, ``alert``, ``run``.
rumps is imported inside functions so this module imports without it.
"""

from __future__ import annotations

import os
import sys

from dataclasses import dataclass

from ccswap import menubar
from ccswap.providers import PROVIDERS
from ccswap.settings import TITLE_PCT_CHOICES

_TITLE_PCT_LABELS = {
    "off": "None", "5h": "Session (5h)", "7d": "Weekly (7d)", "both": "Both (5h · 7d)",
}
_INTERVAL_LABELS = {30: "30 seconds", 60: "60 seconds", 300: "5 minutes"}
_STRATEGY_LABELS = {"best": "Switch to best", "next-available": "Next available"}
_INTEGRATIONS = (("statusline", "Claude Code statusline"), ("mod", "Claude Code mod"))


@dataclass(frozen=True)
class MenuRow:
    label: str
    action: str | None = None  # MenuBarController method name; None = info row
    args: tuple = ()
    state: bool | None = None  # checkmark
    tooltip: str = ""
    children: tuple["MenuRow", ...] = ()
    separator: bool = False
    confirm: str = ""  # non-empty: the shell asks this before running the action


_SEP = MenuRow("", separator=True)


def _account_rows(pid, spec, state) -> list[MenuRow]:
    adapted = getattr(state, "adapted", None)
    if state is not None and getattr(state, "error", None) and not adapted:
        return [MenuRow(f"{spec.label} unavailable", tooltip=str(state.error))]
    accounts = (adapted or {}).get("accounts") or []
    if not accounts:
        return [MenuRow("No managed accounts")]
    rows = []
    for (num, email, active, display, _lg, alias, disabled, fetched_at, login_exp, quar) in accounts:
        tip = display if isinstance(display, str) and display != menubar.menu_note(display) else ""
        rows.append(MenuRow(
            menubar.format_account_label(
                num, email, display, alias=alias, disabled=disabled, fetched_at=fetched_at,
                login_expires_at=login_exp, quarantined=quar,
            ),
            action="switch_to" if spec.capabilities.switch else None,
            args=(pid, num),
            state=bool(active),
            tooltip=tip,
        ))
    return rows


def _manage_menu(pid, spec, state, controller) -> MenuRow:
    caps = spec.capabilities
    accounts = ((getattr(state, "adapted", None) or {}).get("accounts")) or []

    def pick(action, extra=()):
        if not accounts:
            return (MenuRow("No managed accounts"),)
        return tuple(
            MenuRow(
                f"{a[0]}  {a[5]}  ({a[1]})" if a[5] else f"{a[0]}  {a[1]}",
                action=action, args=(pid, a[0], *extra(a)),
                confirm=f"Remove {spec.label} account {a[0]} ({a[1]})?" if action == "remove" else "",
            )
            for a in accounts
        )

    add = [MenuRow("From current login", action="add_login", args=(pid,))]
    if caps.add_token:
        add.append(MenuRow("From setup-token…", action="add_token"))
    children = [MenuRow("Add account", children=tuple(add))]
    if caps.disable:
        children.append(MenuRow(
            "Disable / enable account",
            children=tuple(
                MenuRow(
                    f"{'Enable' if a[6] else 'Disable'}  {a[0]}  {a[1]}",
                    action="set_disabled", args=(pid, a[0], not a[6]), state=bool(a[6]),
                )
                for a in accounts
            ) or (MenuRow("No managed accounts"),),
        ))
    children.append(MenuRow("Remove account", children=pick("remove", lambda a: ())))
    return MenuRow(f"Manage {spec.label}", children=tuple(children))


def _settings_menu(controller, payload) -> MenuRow:
    s = controller.settings
    pset = payload.get("settings") or {}
    integrations = pset.get("claudeIntegrations") or {}
    items = [
        MenuRow("Title shows", children=tuple(
            MenuRow(label, action="set_display", args=("title_provider", value),
                    state=s.title_provider == value)
            for value, label in (("auto", "Auto"), *((p, sp.label) for p, sp in PROVIDERS.items()))
        )),
        MenuRow("Title percentage", children=tuple(
            MenuRow(_TITLE_PCT_LABELS[m], action="set_display", args=("title_pct", m),
                    state=s.title_pct == m)
            for m in TITLE_PCT_CHOICES
        )),
        MenuRow("Show account name in menu bar", action="set_display",
                args=("show_account_name", not s.show_account_name), state=s.show_account_name),
        MenuRow("Show model limits in title", action="set_display",
                args=("title_scoped", not s.title_scoped), state=s.title_scoped),
        MenuRow("Refresh interval", children=tuple(
            MenuRow(_INTERVAL_LABELS.get(n, f"{n} seconds"), action="set_display",
                    args=("refresh_interval", n), state=s.refresh_interval == n)
            for n in menubar.REFRESH_CHOICES
        )),
    ]
    auto = {e.get("id"): e.get("autoSwitch") for e in payload.get("providers") or []}
    for pid, spec in PROVIDERS.items():
        if spec.capabilities.auto_switch:
            enabled = bool((auto.get(pid) or {}).get("enabled"))
            items.append(MenuRow(f"Auto-switch {spec.label} accounts", action="set_auto",
                                 args=(pid, not enabled), state=enabled))
    current = pset.get("threshold")
    items.append(MenuRow("Auto-switch threshold", children=tuple(
        MenuRow(f"{p}%", action="set_threshold", args=(p,), state=current == p)
        for p in menubar.AUTO_THRESHOLD_CHOICES
    )))
    for key, label in _INTEGRATIONS:
        items.append(MenuRow(label, action="toggle_claude_integration", args=(key,),
                             state=bool(integrations.get(key))))
    items.append(MenuRow("Use popover", action="set_display", args=("style", "popover")))
    return MenuRow("Settings", children=tuple(items))


def menu_spec(controller) -> list[MenuRow]:
    """The whole menu, top to bottom."""
    payload = controller.payload()
    label, attention = controller.health()
    auto = {e.get("id"): e.get("autoSwitch") for e in payload.get("providers") or []}
    failed = next(
        ((pid, f) for pid in PROVIDERS if (f := (auto.get(pid) or {}).get("failure")
         or getattr(controller.states.get(pid), "engine_failure", None))),
        None,
    )
    if failed:
        rows = [MenuRow(label, action="restart_engine", args=(failed[0],), tooltip=str(failed[1]))]
    else:
        rows = [MenuRow(label)]
    rows.append(_SEP)
    for pid, spec in PROVIDERS.items():
        caps = spec.capabilities
        state = controller.states.get(pid)
        header = spec.label
        if caps.auto_switch:
            a = auto.get(pid) or {}
            header += "  ·  auto " + ("on" if a.get("running") else "failed" if a.get("enabled") else "off")
        rows.append(MenuRow(header))
        rows += _account_rows(pid, spec, state)
        if caps.strategies:
            rows.append(MenuRow("Rotate to next", action="rotate", args=(pid, None)))
            rows += [
                MenuRow(_STRATEGY_LABELS[st], action="rotate", args=(pid, st))
                for st in caps.strategies if st in _STRATEGY_LABELS
            ]
        rows.append(_manage_menu(pid, spec, state, controller))
        if pid == "claude":
            rows.append(MenuRow("Refresh current credentials", action="refresh_creds"))
        rows.append(_SEP)
    lines = list(controller.history() or [])
    rows.append(MenuRow("Switch history", children=(
        *(MenuRow(line) for line in lines),
        *([] if lines else [MenuRow("No switches logged yet")]),
        _SEP,
        MenuRow("Open full log…", action="open_log"),
    )))
    rows += [
        _SEP,
        _settings_menu(controller, payload),
        MenuRow("Refresh now", action="refresh", args=(None, True)),
        MenuRow("Quit", action="quit"),
    ]
    return rows


def notify(title: str, subtitle: str, message: str) -> None:
    import rumps

    rumps.notification(title, subtitle, message)


def alert(title: str, message: str) -> None:
    import rumps

    rumps.alert(title=title, message=message)


def _purge_callbacks(rumps, menu) -> None:
    """Stop a rumps memory leak: Menu.clear() never unregisters item callbacks.

    rumps registers each item's callback in the process-global
    NSApp._ns_to_py_and_callback, so rebuilding the menu on every refresh
    leaks every item (~1GB after days). Walk the native NSMenu tree (the rumps
    dict is keyed by title and drops same-title items). Guarded: a renamed
    private attribute degrades to "leaks again", not a crash.
    """
    reg = getattr(rumps.rumps.NSApp, "_ns_to_py_and_callback", None)
    if reg is None:
        return

    def purge(nsmenu):
        for it in nsmenu.itemArray():
            reg.pop(it, None)
            sub = it.submenu()
            if sub is not None:
                purge(sub)

    purge(menu._menu)


def _set_tooltip(item, text: str) -> None:
    ns_item = getattr(item, "_menuitem", None)
    if ns_item is not None and hasattr(ns_item, "setToolTip_"):
        ns_item.setToolTip_(text)


def _activate(controller, row) -> bool:
    """Run a clicked row's action; False when the user cancelled its confirmation."""
    import rumps

    if row.confirm and rumps.alert(
        title="Remove account?", message=row.confirm, ok="Remove", cancel="Cancel"
    ) != 1:
        return False
    if row.action == "add_token" and not row.args:
        _ask_token(controller)
    elif getattr(controller, row.action)(*row.args) and row.args[:1] == ("style",):
        # The style is saved; start again in it, as the popover's setStyle
        # does, once nothing is mid-way through a credential write.
        if controller.quiesce():
            os.execv(sys.executable, [sys.executable, "-m", "ccswap", "menubar"])
        from ccswap.menubar_controller import APP, STYLE_LATER

        controller.alert(APP, STYLE_LATER)
    return True


def _ask_token(controller) -> None:
    """Two input dialogs, then controller.add_token(email, token)."""
    import rumps

    # An accessory app isn't active, so a modal rumps.Window can render black
    # until the app is brought forward.
    import AppKit
    AppKit.NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
    email_resp = rumps.Window(
        title="Add account from setup-token", message="Email for this token (optional):",
        ok="Next", cancel="Cancel", dimensions=(320, 24),
    ).run()
    if email_resp.clicked != 1:  # like `ccswap add-token`, a blank email is detected from the token
        return
    token_resp = rumps.Window(
        title="Add account from setup-token", message="Setup token (sk-ant-oat01-…):",
        ok="Add", cancel="Cancel", dimensions=(320, 24),
    ).run()
    if token_resp.clicked != 1 or not token_resp.text.strip():
        return
    controller.add_token(email_resp.text.strip(), token_resp.text.strip())


def _paint_glyph(app, controller, has_text: bool) -> bool:
    """Set the status item's button image to the live glyph; False to fall back to text."""
    try:
        from ccswap import brand
        from ccswap.menubar_popover import tightest_used_pct

        button = app._nsapp.nsstatusitem.button()
        pct = tightest_used_pct(controller.payload())
        key = (None if pct is None else round(pct), controller.title_provider())
        cache = app.__dict__.setdefault("_glyphs", {})  # one image per whole percent and provider
        if key not in cache:
            cache[key] = brand.status_image(*key)
        button.setImage_(cache[key])
        button.setImagePosition_(2 if has_text else 1)  # NSImageLeft / NSImageOnly
        return True
    except Exception:  # noqa: BLE001 - any AppKit/rumps failure keeps the text icon
        return False


def run(controller) -> int:
    """Build the rumps app around the controller; blocks until quit."""
    import rumps

    class ClassicApp(rumps.App):
        def __init__(self):
            super().__init__(menubar.ICON, quit_button=None)
            self._interval = controller.settings.refresh_interval
            self.render()
            self.refresh_timer = rumps.Timer(lambda _t: controller.refresh(), self._interval)
            self.refresh_timer.start()
            self.tick_timer = rumps.Timer(self.on_tick, 1)
            self.tick_timer.start()

        def set_title(self, paint: bool = True):
            # The glyph only changes with the data, so the 1 s tick updates the
            # text alone; render() (after a data change) repaints the glyph.
            text = controller.title(icon=False)
            if paint:
                self._glyph_ok = _paint_glyph(self, controller, bool(text))
            self.title = text if getattr(self, "_glyph_ok", False) else controller.title()

        def on_tick(self, _timer):
            if controller.tick():
                self.render()
            else:
                self.set_title(paint=False)
            secs = controller.settings.refresh_interval
            if secs != self._interval:
                # rumps 0.4.0's Timer.interval setter is a no-op while running;
                # stop/start forces the new cadence immediately.
                self._interval = secs
                self.refresh_timer.stop()
                self.refresh_timer.interval = secs
                self.refresh_timer.start()

        def render(self):
            rows = menu_spec(controller)
            _purge_callbacks(rumps, self.menu)
            self.menu.clear()
            self.menu = [self._item(r) for r in rows]
            self.set_title()

        def _item(self, row):
            if row.separator:
                return None
            item = rumps.MenuItem(
                row.label, callback=self._callback(row) if row.action else None
            )
            if row.state is not None:
                item.state = 1 if row.state else 0
            if row.tooltip:
                _set_tooltip(item, row.tooltip)
            for child in row.children:
                item.add(self._item(child))
            return item

        def _callback(self, row):
            def cb(_sender):
                if row.action == "quit":
                    controller.stop()
                    rumps.quit_application()
                    return
                _activate(controller, row)
                self.render()
            return cb

    ClassicApp().run()
    return 0
