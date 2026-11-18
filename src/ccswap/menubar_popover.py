"""Popover shell for the menu bar: NSStatusItem + NSPopover hosting a WKWebView.

The page lives in ``menubar_web/``. All AppKit/WebKit imports stay inside
functions so this module imports without pyobjc.

Shell module API (same for menubar_classic): ``notify``, ``alert``, ``run``.
"""

from __future__ import annotations

import json
import math
import os
import sys
from pathlib import Path

# Messages the page may post; anything else is dropped.
BRIDGE_ACTIONS = frozenset({
    "ready", "refresh", "switch", "rotate", "toggleAuto", "restartEngine",
    "toggleDisabled", "remove", "addLogin", "addToken", "setTitleProvider",
    "setTitlePct", "setInterval", "setThreshold", "toggleName", "toggleScoped",
    "setStyle", "toggleIntegration", "refreshCreds", "openLog", "quit",
})

WEB_DIR = Path(__file__).with_name("menubar_web")
POPOVER_SIZE = (360, 560)
RELEASE_AFTER_S = 60.0
INTEGRATIONS = ("statusline", "mod")
_BAD = {"ok": False, "error": "invalid request"}


def _int(value):
    """An integer (bool excluded; 2.0 accepted as 2), else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        return int(value)
    return None


def _num(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def dispatch(controller, body, confirm=None) -> dict | None:
    """Validate one page message and run it on the controller.

    Whitelist, provider in the registry, integer account number, then the
    capability gate. Returns a reply for the page (``{"ok": bool, ...}``), or
    None for a message that is not from the whitelist. ``confirm(title,
    message) -> bool`` is the native confirmation shown before ``remove``.
    Never logs message bodies (``addToken`` carries a token).
    """
    from ccswap.providers import PROVIDERS
    from ccswap.settings import TITLE_PCT_CHOICES

    get = getattr(body, "get", None)
    action = get("action") if get else None
    if not isinstance(action, str) or action not in BRIDGE_ACTIONS:
        return None
    reply = {"ok": True, "action": action}

    def bad(why: str = "invalid request") -> dict:
        return {"ok": False, "action": action, "error": why}

    value = get("value")
    pid = get("provider")
    spec = None
    if pid is not None or action in _PROVIDER_ACTIONS:
        spec = PROVIDERS.get(pid) if isinstance(pid, str) else None
        if spec is None and not (action in ("refresh", "addToken") and pid is None):
            return bad("unknown provider")
    caps = spec.capabilities if spec else None

    def number():
        return _int(get("number"))

    try:
        if action in ("ready", "quit"):
            return reply
        if action == "refresh":
            controller.refresh(pid, full=True)
        elif action == "switch":
            n = number()
            if n is None or not caps.switch:
                return bad()
            reply["ok"] = bool(controller.switch_to(pid, n))
            reply["restart"] = bool(caps.restart_after_switch)
        elif action == "rotate":
            strategy = get("strategy")
            if not caps.strategies or (strategy is not None and strategy not in caps.strategies):
                return bad()
            reply["ok"] = bool(controller.rotate(pid, strategy))
        elif action in ("toggleAuto", "restartEngine"):
            if not caps.auto_switch:
                return bad()
            if action == "restartEngine":
                reply["ok"] = bool(controller.restart_engine(pid))
            else:
                enabled = get("enabled")
                if not isinstance(enabled, bool):
                    return bad()
                reply["ok"] = bool(controller.set_auto(pid, enabled))
        elif action == "toggleDisabled":
            n, disabled = number(), get("disabled")
            if n is None or not isinstance(disabled, bool) or not caps.disable:
                return bad()
            reply["ok"] = bool(controller.set_disabled(pid, n, disabled))
        elif action == "remove":
            n = number()
            if n is None or not caps.disable:
                return bad()
            if confirm and not confirm("Remove account?", f"Remove {spec.label} account {n}?"):
                return {**reply, "ok": False, "cancelled": True}
            reply["ok"] = bool(controller.remove(pid, n))
        elif action == "addLogin":
            reply["ok"] = bool(controller.add_login(pid))
        elif action == "addToken":
            email, token = get("email"), get("token")
            if not (spec or PROVIDERS["claude"]).capabilities.add_token:
                return bad()
            if not (isinstance(email, str) and email and isinstance(token, str) and token):
                return bad()
            reply["ok"] = bool(controller.add_token(email, token))
        elif action == "setTitleProvider":
            if not isinstance(value, str) or value not in ("auto", *PROVIDERS):
                return bad()
            reply["ok"] = bool(controller.set_display("title_provider", value))
        elif action == "setTitlePct":
            if not isinstance(value, str) or value not in TITLE_PCT_CHOICES:
                return bad()
            reply["ok"] = bool(controller.set_display("title_pct", value))
        elif action == "setInterval":
            n = _int(value)
            if n is None or not 10 <= n <= 3600:
                return bad()
            reply["ok"] = bool(controller.set_display("refresh_interval", n))
        elif action == "setThreshold":
            pct = _num(value)
            if pct is None or not 1 <= pct <= 100:
                return bad()
            reply["ok"] = bool(controller.set_threshold(pct))
        elif action in ("toggleName", "toggleScoped"):
            if not isinstance(value, bool):
                return bad()
            field = "show_account_name" if action == "toggleName" else "title_scoped"
            reply["ok"] = bool(controller.set_display(field, value))
        elif action == "setStyle":
            if value not in ("popover", "classic"):
                return bad()
            reply["ok"] = bool(controller.set_display("style", value))
        elif action == "toggleIntegration":
            key = get("key")
            if key not in INTEGRATIONS:
                return bad()
            reply["ok"] = bool(controller.toggle_claude_integration(key))
        elif action == "refreshCreds":
            if pid not in (None, "claude"):  # the controller method is Claude-only
                return bad()
            reply["ok"] = bool(controller.refresh_creds())
        elif action == "openLog":
            controller.open_log()
    except Exception as exc:  # never echo the message: it may hold a token
        return bad(type(exc).__name__)
    return reply


_PROVIDER_ACTIONS = frozenset({
    "switch", "rotate", "toggleAuto", "restartEngine", "toggleDisabled",
    "remove", "addLogin",
})


def notify(title: str, subtitle: str, message: str) -> None:
    from Foundation import NSUserNotification, NSUserNotificationCenter

    note = NSUserNotification.alloc().init()
    note.setTitle_(title)
    note.setSubtitle_(subtitle)
    note.setInformativeText_(message)
    NSUserNotificationCenter.defaultUserNotificationCenter().deliverNotification_(note)


def alert(title: str, message: str) -> None:
    from AppKit import NSAlert, NSApplication

    NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
    box = NSAlert.alloc().init()
    box.setMessageText_(title)
    box.setInformativeText_(message)
    box.runModal()


def _confirm(title: str, message: str) -> bool:
    from AppKit import NSAlert, NSApplication

    NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
    box = NSAlert.alloc().init()
    box.setMessageText_(title)
    box.setInformativeText_(message)
    box.addButtonWithTitle_("Remove")
    box.addButtonWithTitle_("Cancel")
    return box.runModal() == 1000  # NSAlertFirstButtonReturn


def tightest_used_pct(payload: dict) -> float | None:
    """Highest window pct of the title provider's active account (ring fill)."""
    resolved = (payload.get("title") or {}).get("resolved")
    for prov in payload.get("providers", ()):
        if prov.get("id") != resolved:
            continue
        for acct in prov.get("accounts", ()):
            if acct.get("active"):
                wins = acct.get("windows") or []
                acct_wide = [w for w in wins if not w.get("scope")] or wins
                pcts = [w["pct"] for w in acct_wide if isinstance(w.get("pct"), (int, float))]
                return max(pcts) if pcts else None
    return None


def _ring_image(used_pct: float | None):
    from AppKit import NSBezierPath, NSColor, NSImage

    img = NSImage.alloc().initWithSize_((18, 18))
    img.lockFocus()
    track = NSBezierPath.bezierPathWithOvalInRect_(((2, 2), (14, 14)))
    track.setLineWidth_(2.5)
    NSColor.colorWithCalibratedWhite_alpha_(0.0, 0.3).set()
    track.stroke()
    if used_pct is not None:
        frac = max(0.0, min(used_pct, 100.0)) / 100
        NSColor.blackColor().set()
        arc = NSBezierPath.bezierPath()
        arc.setLineWidth_(2.5)
        arc.appendBezierPathWithArcWithCenter_radius_startAngle_endAngle_clockwise_(
            (9, 9), 7, 90, 90 - 360 * max(frac, 0.02), True
        )
        arc.stroke()
    img.unlockFocus()
    img.setTemplate_(True)
    return img


def _make_shell_class():
    import objc
    from AppKit import (
        NSApplication, NSApplicationActivationPolicyAccessory, NSMakeRect,
        NSPopover, NSStatusBar, NSVariableStatusItemLength, NSViewController,
    )
    from Foundation import NSObject, NSRunLoop, NSRunLoopCommonModes, NSTimer, NSURL
    from WebKit import WKUserContentController, WKWebView, WKWebViewConfiguration

    class Shell(NSObject):
        def initWithController_(self, controller):
            self = objc.super(Shell, self).init()
            self.controller = controller
            self.web = self.popover = self.ucc = self.vc = None
            self.page_ready = False
            self.release_timer = None
            self.refresh_timer = None
            self.interval = None
            self.item = NSStatusBar.systemStatusBar().statusItemWithLength_(
                NSVariableStatusItemLength
            )
            button = self.item.button()
            button.setTarget_(self)
            button.setAction_("togglePopover:")
            self.paint_status()
            self._timer(1.0, "tick:")
            self.reschedule_refresh()
            return self

        @objc.python_method
        def _timer(self, seconds, selector, repeats=True):
            timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(
                seconds, self, selector, None, repeats
            )
            NSRunLoop.currentRunLoop().addTimer_forMode_(timer, NSRunLoopCommonModes)
            return timer

        @objc.python_method
        def reschedule_refresh(self):
            interval = int(self.controller.settings.refresh_interval)
            if interval == self.interval:
                return
            if self.refresh_timer is not None:
                self.refresh_timer.invalidate()
            self.interval = interval
            self.refresh_timer = self._timer(float(interval), "refreshTick:")

        # --- timers ---
        def tick_(self, _timer):
            self.reschedule_refresh()
            if self.controller.tick():
                self.repaint()

        def refreshTick_(self, _timer):
            self.controller.refresh()

        # --- painting ---
        @objc.python_method
        def paint_status(self, payload=None):
            button = self.item.button()
            title = self.controller.title(icon=False)
            button.setTitle_(f" {title}" if title else "")
            payload = payload or self.controller.payload()
            button.setImage_(_ring_image(tightest_used_pct(payload)))
            button.setImagePosition_(2 if title else 1)  # NSImageLeft / NSImageOnly
            return payload

        @objc.python_method
        def repaint(self):
            payload = self.paint_status()
            self.push(payload)

        @objc.python_method
        def push(self, payload=None):
            if self.web is None or not self.page_ready or not self.popover.isShown():
                return
            data = json.dumps(payload or self.controller.payload())
            self.web.evaluateJavaScript_completionHandler_(f"ccswap.render({data})", None)

        # --- popover ---
        @objc.python_method
        def ensure_popover(self):
            if self.web is not None:
                return
            cfg = WKWebViewConfiguration.alloc().init()
            self.ucc = WKUserContentController.alloc().init()
            self.ucc.addScriptMessageHandler_name_(self, "ccswap")
            cfg.setUserContentController_(self.ucc)
            w, h = POPOVER_SIZE
            self.web = WKWebView.alloc().initWithFrame_configuration_(NSMakeRect(0, 0, w, h), cfg)
            self.web.setValue_forKey_(False, "drawsBackground")
            self.page_ready = False
            self.web.loadFileURL_allowingReadAccessToURL_(
                NSURL.fileURLWithPath_(str(WEB_DIR / "index.html")),
                NSURL.fileURLWithPath_isDirectory_(str(WEB_DIR), True),
            )
            self.vc = NSViewController.alloc().init()
            self.vc.setView_(self.web)
            self.popover = NSPopover.alloc().init()
            self.popover.setContentViewController_(self.vc)
            self.popover.setContentSize_((w, h))
            self.popover.setBehavior_(1)  # NSPopoverBehaviorTransient
            self.popover.setDelegate_(self)

        def togglePopover_(self, _sender):
            if self.popover is not None and self.popover.isShown():
                self.popover.performClose_(None)
                return
            if self.release_timer is not None:
                self.release_timer.invalidate()
                self.release_timer = None
            self.ensure_popover()
            button = self.item.button()
            NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
            self.popover.showRelativeToRect_ofView_preferredEdge_(button.bounds(), button, 1)
            self.web.window().makeFirstResponder_(self.web)
            self.push()

        def popoverDidClose_(self, _note):
            if self.release_timer is not None:
                self.release_timer.invalidate()
            self.release_timer = self._timer(RELEASE_AFTER_S, "releaseWeb:", repeats=False)

        def releaseWeb_(self, _timer):
            self.release_timer = None
            if self.popover is None or self.popover.isShown():
                return
            self.ucc.removeScriptMessageHandlerForName_("ccswap")
            self.popover.setContentViewController_(None)
            self.popover.setDelegate_(None)
            self.web = self.popover = self.ucc = self.vc = None
            self.page_ready = False

        # --- JS bridge ---
        def userContentController_didReceiveScriptMessage_(self, _ucc, message):
            body = message.body()
            reply = dispatch(self.controller, body, confirm=_confirm)
            if reply is None:
                return
            action = reply["action"]
            if action == "ready":
                self.page_ready = True
                self.push()
                return
            if action == "quit":
                self.controller.stop()
                NSApplication.sharedApplication().terminate_(None)
                return
            if action == "setStyle" and reply["ok"]:
                self.controller.stop(wait=10)  # let a switch in progress finish before the exec
                os.execv(sys.executable, [sys.executable, "-m", "ccswap", "menubar"])
            if self.web is not None:
                self.web.evaluateJavaScript_completionHandler_(
                    f"ccswap.reply&&ccswap.reply({json.dumps(reply)})", None
                )
            self.repaint()

    return Shell, NSApplication, NSApplicationActivationPolicyAccessory


def run(controller) -> int:
    """Create the status item and lazy popover; blocks until quit."""
    Shell, NSApplication, accessory = _make_shell_class()
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(accessory)
    shell = Shell.alloc().initWithController_(controller)  # strong ref while app.run() blocks
    shell.repaint()
    app.run()  # quit goes through terminate_, so this rarely returns
    del shell
    return 0
