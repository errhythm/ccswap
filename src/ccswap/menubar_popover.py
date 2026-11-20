"""Popover shell for the menu bar: NSStatusItem + NSPopover hosting a WKWebView.

The page lives in ``menubar_web/``. All AppKit/WebKit imports stay inside
functions so this module imports without pyobjc.

Shell module API (same for menubar_classic): ``notify``, ``alert``, ``run``.
"""

from __future__ import annotations

import functools
import json
import math
import os
import sys
from pathlib import Path

from ccswap.menubar_controller import STYLE_LATER

# Messages the page may post; anything else is dropped.
BRIDGE_ACTIONS = frozenset({
    "ready", "refresh", "switch", "rotate", "toggleAuto", "restartEngine",
    "toggleDisabled", "remove", "addLogin", "addToken", "setTitleProvider",
    "setTitlePct", "setInterval", "setThreshold", "toggleName", "toggleScoped",
    "setStyle", "toggleIntegration", "refreshCreds", "openLog", "upgrade", "quit",
})

WEB_DIR = Path(__file__).with_name("menubar_web")
POPOVER_SIZE = (360, 560)
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
            changed = getattr(controller, "last_switch_changed", True)
            reply["restart"] = bool(caps.restart_after_switch) and changed is not False
            note = getattr(controller, "last_note", None)
            if changed is False and isinstance(note, str):
                reply["note"] = note
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
            if n is None:  # every provider's CLI has `remove`, unlike disable/enable
                return bad()
            if confirm and not confirm("Remove account?", f"Remove {spec.label} account {n}?"):
                return {**reply, "ok": False, "cancelled": True}
            reply["ok"] = bool(controller.remove(pid, n))
        elif action == "addLogin":
            reply["ok"] = bool(controller.add_login(pid))
            note = getattr(controller, "last_note", None)
            if reply["ok"] and isinstance(note, str):
                reply["note"] = note
        elif action == "addToken":
            email, token = get("email"), get("token")
            if not (spec or PROVIDERS["claude"]).capabilities.add_token:
                return bad()
            # like `ccswap add-token`: the token is required, the email is an optional label
            if not (isinstance(token, str) and token.strip()) or not (email is None or isinstance(email, str)):
                return bad()
            reply["ok"] = bool(controller.add_token(email or "", token))
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
        elif action == "upgrade":
            reply["ok"] = bool(controller.upgrade())
    except Exception as exc:  # never echo the message: it may hold a token
        return bad(type(exc).__name__)
    return reply


_PROVIDER_ACTIONS = frozenset({
    "switch", "rotate", "toggleAuto", "restartEngine", "toggleDisabled",
    "remove", "addLogin",
})


# Credential writes, Keychain reads and network calls: seconds on the main
# thread would freeze the page's spinner, so they run on an action thread.
BACKGROUND_ACTIONS = frozenset({"switch", "rotate", "remove", "addLogin", "addToken", "refreshCreds", "upgrade"})
# Allowed while one runs: a page reload, and a refresh that only starts its own worker.
WHILE_BUSY = frozenset({"ready", "refresh"})


# AppKit's NSApplicationTerminateReply (NSTerminateCancel is 0); plain ints so
# this module imports without pyobjc. A test checks them against AppKit.
NS_TERMINATE_NOW = 1
NS_TERMINATE_LATER = 2


class ActionRunner:
    """One page action at a time; the slow ones run off the main thread.

    ``handle`` runs on the main thread. ``deliver(reply)`` is always called on
    the main thread: directly, or from the controller's ``tick()`` queue, which
    ``wake()`` asks the main thread to drain now instead of on the next tick.
    """

    def __init__(self, controller, deliver, wake=lambda: None, confirm=None,
                 reply_terminate=lambda ok: None):
        self.controller, self.deliver, self.wake, self.confirm = controller, deliver, wake, confirm
        self.reply_terminate = reply_terminate  # NSApp.replyToApplicationShouldTerminate_
        self.running = None  # the action on the action thread, until its reply is delivered
        self.quit_pending = False

    def should_terminate(self) -> int:
        """applicationShouldTerminate: Later while an action runs, else Now."""
        if self.running:
            self.quit_pending = True  # _finish answers once the credential write is done
            return NS_TERMINATE_LATER
        return NS_TERMINATE_NOW

    def handle(self, body) -> None:
        get = getattr(body, "get", None)
        action = get("action") if get else None
        if self.running and action in BRIDGE_ACTIONS and action not in WHILE_BUSY:
            # the page blocks its own buttons while busy, but the bridge doesn't rely on it
            self.deliver({"ok": False, "action": action, "busy": True,
                          "error": "Another action is still running"})
            return
        if action not in BACKGROUND_ACTIONS:
            reply = dispatch(self.controller, body, confirm=self.confirm)
            if reply is not None:
                self.deliver(reply)
            return
        if action == "remove" and self.confirm:
            # Validate and ask here (NSAlert is main-thread only), stopping before the
            # controller call; the action thread then removes without asking again.
            said = []
            reply = dispatch(self.controller, body,
                             confirm=lambda *a: said.append(self.confirm(*a)) and False)
            if said != [True]:
                self.deliver(reply)  # invalid request, or the user cancelled
                return
        self.running = action
        try:
            self.controller._spawn(self._work, body)
        except Exception as exc:
            self._finish({"ok": False, "action": action, "error": type(exc).__name__})

    def _work(self, body) -> None:  # action thread
        reply = {"ok": False, "action": self.running, "error": "internal error"}
        try:
            reply = dispatch(self.controller, body)  # catches the controller's exceptions itself
        finally:
            self.controller._on_main(self._finish, reply)
            self.wake()

    def _finish(self, reply) -> None:  # main thread
        self.running = None
        try:
            self.deliver(reply)
        finally:
            if self.quit_pending:  # a quit (logout, restart, osascript) waited for this action
                self.quit_pending = False
                self.reply_terminate(True)


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


@functools.cache  # one ObjC class per process: a second definition raises objc.error
def _make_shell_class():
    import objc
    from AppKit import (
        NSApplication, NSApplicationActivationPolicyAccessory, NSMakeRect,
        NSPopover, NSStatusBar, NSVariableStatusItemLength, NSViewController,
    )
    from Foundation import NSObject, NSRunLoop, NSRunLoopCommonModes, NSTimer, NSURL
    from WebKit import (
        WKUserContentController, WKWebView, WKWebViewConfiguration,
    )

    class Shell(NSObject):
        def initWithController_(self, controller):
            self = objc.super(Shell, self).init()
            self.controller = controller
            self.web = self.popover = self.ucc = self.vc = None
            self.page_ready = False
            self.refresh_timer = None
            self.interval = None
            self.actions = ActionRunner(
                controller, self.deliver,
                # run tick: now so the reply doesn't wait for the 1 s timer
                wake=lambda: self.performSelectorOnMainThread_withObject_waitUntilDone_("tick:", None, False),
                confirm=_confirm,
                reply_terminate=lambda ok: NSApplication.sharedApplication().replyToApplicationShouldTerminate_(ok),
            )
            self.item = NSStatusBar.systemStatusBar().statusItemWithLength_(
                NSVariableStatusItemLength
            )
            button = self.item.button()
            button.setTarget_(self)
            button.setAction_("togglePopover:")
            self.paint_status()
            # Build and load the page now: starting WebKit on the first click left
            # the popover blank for about two seconds.
            self.ensure_popover()
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
            button.setToolTip_(f"ccswap: {self.controller.title_label()}")  # the title itself carries no provider tag
            payload = payload or self.controller.payload()
            used = tightest_used_pct(payload)
            try:
                from ccswap.brand import status_image

                image = status_image(used, self.controller.title_provider())
            except Exception:  # brand glyph not available yet: keep the ring
                image = _ring_image(used)
            button.setImage_(image)
            button.setImagePosition_(2 if title else 1)  # NSImageLeft / NSImageOnly
            return payload

        @objc.python_method
        def repaint(self):
            payload = self.paint_status()
            self.push(payload)

        @objc.python_method
        def push(self, payload=None):
            # Also while hidden, so the popover opens on current numbers.
            if self.web is None or not self.page_ready:
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
            self.web.setNavigationDelegate_(self)
            self.page_ready = False
            self.web.loadFileURL_allowingReadAccessToURL_(
                NSURL.fileURLWithPath_(str(WEB_DIR / "index.html")),
                NSURL.fileURLWithPath_isDirectory_(str(WEB_DIR), True),
            )
            self.vc = NSViewController.alloc().init()
            self.vc.setView_(self.web)  # the page paints its own solid graphite background
            self.popover = NSPopover.alloc().init()
            self.popover.setContentViewController_(self.vc)
            self.popover.setContentSize_((w, h))
            self.popover.setBehavior_(1)  # NSPopoverBehaviorTransient
            self.popover.setAnimates_(False)  # open at once, like tinybar

        def togglePopover_(self, _sender):
            if self.popover is not None and self.popover.isShown():
                self.popover.performClose_(None)
                return
            self.ensure_popover()
            self.controller.refresh()  # refresh on open, like tinybar; the usage store paces the network
            button = self.item.button()
            NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
            self.popover.showRelativeToRect_ofView_preferredEdge_(button.bounds(), button, 1)
            self.web.window().makeFirstResponder_(self.web)
            self.push()

        def webViewWebContentProcessDidTerminate_(self, web):
            # macOS can kill the page's process under memory pressure; reload so
            # the long-lived page doesn't stay blank.
            self.page_ready = False
            web.reload()

        # --- app delegate ---
        def applicationShouldTerminate_(self, _app):
            return self.actions.should_terminate()

        # --- JS bridge ---
        def userContentController_didReceiveScriptMessage_(self, _ucc, message):
            # Next run-loop turn, so the page's busy state is on screen before a
            # main-thread action (or the remove confirm) runs.
            self.performSelector_withObject_afterDelay_("handleMessage:", message.body(), 0.02)

        def handleMessage_(self, body):
            self.actions.handle(body)

        @objc.python_method
        def deliver(self, reply):  # main thread
            action = reply["action"]
            if action == "ready":
                self.page_ready = True
                self.push()
                return
            if action == "quit" and reply["ok"]:  # not a busy refusal
                self.controller.stop()
                NSApplication.sharedApplication().terminate_(None)
                return
            if action == "setStyle" and reply["ok"]:
                if self.controller.quiesce():  # nothing mid-way through a credential write
                    os.execv(sys.executable, [sys.executable, "-m", "ccswap", "menubar"])
                reply = {**reply, "ok": False, "error": STYLE_LATER}
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
    app.setDelegate_(shell)  # no other delegate: lets a quit wait for a running action
    shell.repaint()
    app.run()  # quit goes through terminate_, so this rarely returns
    del shell
    return 0
