"""Popover shell: the bridge dispatcher and the page files (no GUI)."""

import re
import subprocess
import sys
from pathlib import Path

import pytest

from ccswap import menubar_popover as mp


class Fake:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def call(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return True
        return call


def run(body, confirm=None):
    ctl = Fake()
    return mp.dispatch(ctl, body, confirm=confirm), ctl.calls


def test_unknown_or_malformed_dropped():
    assert run({"action": "format-disk"}) == (None, [])
    assert run({"action": 3}) == (None, [])
    assert run("switch") == (None, [])


@pytest.mark.parametrize("number", ["1", 1.5, True, None])
def test_bad_number_rejected(number):
    reply, calls = run({"action": "switch", "provider": "claude", "number": number})
    assert not reply["ok"] and calls == []


def test_unknown_provider_rejected():
    for pid in ("nope", None, 7):
        reply, calls = run({"action": "switch", "provider": pid, "number": 1})
        assert not reply["ok"] and calls == []


def test_valid_switch_calls_controller_with_int():
    reply, calls = run({"action": "switch", "provider": "codex", "number": 2.0})
    assert reply["ok"] and reply["restart"] is True
    assert calls == [("switch_to", ("codex", 2), {})]
    assert type(calls[0][1][1]) is int


def test_capability_gates():
    assert not run({"action": "rotate", "provider": "agy", "strategy": None})[0]["ok"]
    assert not run({"action": "rotate", "provider": "claude", "strategy": "bogus"})[0]["ok"]
    assert run({"action": "rotate", "provider": "claude", "strategy": "best"})[0]["ok"]
    assert not run({"action": "toggleDisabled", "provider": "agy", "number": 1, "disabled": True})[0]["ok"]
    assert run({"action": "remove", "provider": "agy", "number": 1})[0]["ok"]  # `ccswap agy remove`
    assert not run({"action": "toggleAuto", "provider": "agy", "enabled": True})[0]["ok"]
    assert not run({"action": "addToken", "provider": "codex", "email": "a@b", "token": "t"})[0]["ok"]
    reply, calls = run({"action": "addToken", "email": "a@b", "token": "t"})
    assert reply["ok"] and calls == [("add_token", ("a@b", "t"), {})]


def test_remove_needs_confirmation():
    body = {"action": "remove", "provider": "claude", "number": 1}
    reply, calls = run(body, confirm=lambda *_: False)
    assert reply["cancelled"] and calls == []
    assert run(body, confirm=lambda *_: True)[1] == [("remove", ("claude", 1), {})]


def test_settings_actions_validated():
    assert not run({"action": "setTitleProvider", "value": "nope"})[0]["ok"]
    assert run({"action": "setTitleProvider", "value": "auto"})[1] == [("set_display", ("title_provider", "auto"), {})]
    assert not run({"action": "setInterval", "value": 1})[0]["ok"]
    assert not run({"action": "setThreshold", "value": 500})[0]["ok"]
    assert not run({"action": "toggleName", "value": "yes"})[0]["ok"]
    assert not run({"action": "toggleIntegration", "key": "rm"})[0]["ok"]


def test_controller_exception_does_not_leak_message():
    class Boom(Fake):
        def add_token(self, email, token):
            raise RuntimeError("secret-token-123")

    reply = mp.dispatch(Boom(), {"action": "addToken", "email": "a@b", "token": "t"})
    assert not reply["ok"] and "secret" not in str(reply)


def test_tightest_used_pct_prefers_account_wide():
    payload = {"title": {"resolved": "claude"}, "providers": [{"id": "claude", "accounts": [
        {"active": True, "windows": [{"pct": 40, "scope": None}, {"pct": 95, "scope": {"model": "x"}}]}]}]}
    assert mp.tightest_used_pct(payload) == 40


def test_web_files_local_only():
    web = Path(mp.__file__).with_name("menubar_web")
    for name in ("index.html", "app.css", "app.js"):
        assert (web / name).is_file()
    html = (web / "index.html").read_text()
    assert "default-src 'self'" in html
    refs = re.findall(r'(?:src|href)="([^"]+)"', html)
    assert refs and all(not re.match(r"(?:[a-z]+:)?//", r) for r in refs)
    assert "innerHTML" not in (web / "app.js").read_text()


def test_import_does_not_load_appkit():
    code = "import sys, ccswap.menubar_popover; print('AppKit' in sys.modules or 'WebKit' in sys.modules)"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "False"


def test_shell_class_selectors_register():
    pytest.importorskip("WebKit")
    shell = mp._make_shell_class()[0]
    for sel in ("togglePopover:", "tick:", "refreshTick:", "webViewWebContentProcessDidTerminate:",
                "userContentController:didReceiveScriptMessage:"):
        assert shell.instancesRespondToSelector_(sel)


@pytest.mark.parametrize("pid", ["claude", "codex"])
def test_rotate_accepts_null_strategy(pid):
    for body in ({"action": "rotate", "provider": pid, "strategy": None},
                 {"action": "rotate", "provider": pid}):
        reply, calls = run(body)
        assert reply["ok"] and calls == [("rotate", (pid, None), {})]


def test_rotate_rejected_for_agy_without_strategies():
    reply, calls = run({"action": "rotate", "provider": "agy"})
    assert not reply["ok"] and calls == []


def test_refresh_creds_is_claude_only():
    reply, calls = run({"action": "refreshCreds"})
    assert reply["ok"] and calls == [("refresh_creds", (), {})]
    assert run({"action": "refreshCreds", "provider": "claude"})[0]["ok"]
    reply, calls = run({"action": "refreshCreds", "provider": "codex"})
    assert not reply["ok"] and calls == []


def test_provider_logos_ship_and_are_local():
    web = Path(mp.__file__).with_name("menubar_web")
    js = (web / "app.js").read_text()
    for name in ("claude.svg", "codex.png", "antigravity.svg"):
        assert (web / "logos" / name).is_file() and f"logos/{name}" in js
    assert (web / "mark.svg").is_file()
    # every file the page loads sits under the read-access root
    for ref in re.findall(r'"((?:logos/|mark)[^"]+\.(?:svg|png))"', js + (web / "index.html").read_text()):
        assert (web / ref).is_file()


def test_page_has_no_inline_style_or_remote_urls():
    web = Path(mp.__file__).with_name("menubar_web")
    for name in ("index.html", "app.js", "app.css"):
        text = (web / name).read_text()
        assert not re.search(r"https?://(?!www\.w3\.org)", text), name
    assert " style=" not in (web / "index.html").read_text()


def test_add_token_email_is_optional_like_the_cli():
    calls = []

    class C:
        def add_token(self, email, token):
            calls.append((email, token))
            return True

    assert mp.dispatch(C(), {"action": "addToken", "token": "sk-ant-oat01-x"})["ok"] is True
    assert mp.dispatch(C(), {"action": "addToken", "token": "sk-ant-oat01-y", "email": "a@x.io"})["ok"] is True
    assert mp.dispatch(C(), {"action": "addToken", "token": "  "})["ok"] is False
    assert mp.dispatch(C(), {"action": "addToken", "token": "t", "email": 5})["ok"] is False
    assert calls == [("", "sk-ant-oat01-x"), ("a@x.io", "sk-ant-oat01-y")]


# ---- action thread (ActionRunner) ---------------------------------------------------

import threading

from ccswap.menubar_controller import MenuBarController


class ThreadedCtl(Fake):
    """Real action threads; main-thread calls queue like the controller's, drained by hand."""

    _on_main = MenuBarController._on_main

    def __init__(self, gate=None):
        super().__init__()
        self._lock, self._calls, self.threads = threading.Lock(), [], []
        self.gate = gate  # held by the test to keep an action running

    def _spawn(self, fn, *args):
        t = threading.Thread(target=fn, args=args)
        self.threads.append(t)
        t.start()
        return t

    def switch_to(self, *args):
        self.calls.append(("switch_to", args, {}))
        self.call_thread = threading.current_thread()
        if self.gate:
            self.gate.wait(5)
        return True

    def drain(self):  # what tick() does on the main thread
        for t in self.threads:
            t.join(5)
        with self._lock:
            calls, self._calls = self._calls, []
        for fn, args in calls:
            fn(*args)


def _runner(ctl, confirm=None):
    got, wakes = [], []
    runner = mp.ActionRunner(ctl, lambda r: got.append((r, threading.current_thread())),
                             wake=lambda: wakes.append(1), confirm=confirm)
    return runner, got, wakes


def test_slow_action_runs_off_main_and_replies_on_main():
    ctl = ThreadedCtl()
    runner, got, wakes = _runner(ctl)
    runner.handle({"action": "switch", "provider": "claude", "number": 2})
    for t in ctl.threads:
        t.join(5)
    assert ctl.call_thread is not threading.main_thread()
    assert got == [] and wakes == [1]  # the reply waits for the main thread's drain
    ctl.drain()
    assert [(r["ok"], r["action"], th) for r, th in got] == [(True, "switch", threading.main_thread())]
    assert runner.running is None


def test_second_action_while_one_runs_is_refused_not_run():
    gate = threading.Event()
    ctl = ThreadedCtl(gate)
    runner, got, _ = _runner(ctl)
    runner.handle({"action": "switch", "provider": "claude", "number": 2})
    runner.handle({"action": "switch", "provider": "claude", "number": 3})
    runner.handle({"action": "toggleName", "value": True})
    runner.handle({"action": "quit"})
    runner.handle({"action": "ready"})  # a page reload still gets its reply
    assert [r["action"] for r, _ in got] == ["switch", "toggleName", "quit", "ready"]
    assert all(r["busy"] and not r["ok"] for r, _ in got[:3]) and got[3][0]["ok"]
    gate.set()
    ctl.drain()
    assert [c[0] for c in ctl.calls] == ["switch_to"]  # only the first ran
    assert got[-1][0]["ok"] and got[-1][0]["action"] == "switch" and "busy" not in got[-1][0]
    runner.handle({"action": "toggleName", "value": True})  # free again
    assert got[-1][0] == {"ok": True, "action": "toggleName"}


def test_remove_confirms_on_main_before_the_action_thread():
    asked = []
    ctl = ThreadedCtl()
    runner, got, _ = _runner(ctl, confirm=lambda *a: asked.append(threading.current_thread()) or False)
    runner.handle({"action": "remove", "provider": "claude", "number": 2})
    assert asked == [threading.main_thread()] and ctl.threads == [] and ctl.calls == []
    assert got[0][0]["cancelled"]

    ctl = ThreadedCtl()
    runner, got, _ = _runner(ctl, confirm=lambda *a: True)
    runner.handle({"action": "remove", "provider": "claude", "number": 2})
    ctl.drain()
    assert ctl.calls == [("remove", ("claude", 2), {})] and got[0][0]["ok"]

    runner.handle({"action": "remove", "provider": "claude", "number": "x"})  # invalid: no dialog
    assert got[-1][0]["error"] == "invalid request"


def test_a_failing_queued_call_does_not_strand_the_runner(tmp_path):
    from ccswap.providers import PROVIDERS

    ctl = MenuBarController(None, tmp_path, tmp_path / "l.log", notify=lambda *a: None,
                            alert=lambda *a: None, providers={"claude": PROVIDERS["claude"]})
    started = []
    ctl._spawn = lambda fn, *a: started.append((fn, a))
    runner, got, _ = _runner(ctl)
    runner.handle({"action": "refreshCreds"})

    def boom():
        raise RuntimeError("notify failed")

    t = threading.Thread(target=lambda: (ctl._on_main(boom), started[0][0](*started[0][1])))
    t.start()
    t.join(5)
    ctl.tick()  # boom runs first and raises; the reply queued after it still lands
    assert runner.running is None and got and got[-1][0]["action"] == "refreshCreds"


def test_spawn_failure_clears_running():
    ctl = ThreadedCtl()
    ctl._spawn = lambda *a: (_ for _ in ()).throw(RuntimeError("no threads"))
    runner, got, _ = _runner(ctl)
    runner.handle({"action": "switch", "provider": "claude", "number": 2})
    assert runner.running is None and got[0][0] == {"ok": False, "action": "switch", "error": "RuntimeError"}


def test_quit_from_outside_waits_for_the_running_action():
    gate = threading.Event()
    ctl = ThreadedCtl(gate)
    replies = []  # stands in for NSApp.replyToApplicationShouldTerminate_
    runner = mp.ActionRunner(ctl, lambda r: None, reply_terminate=replies.append)
    assert runner.should_terminate() == mp.NS_TERMINATE_NOW  # idle
    runner.handle({"action": "switch", "provider": "claude", "number": 2})
    assert runner.should_terminate() == mp.NS_TERMINATE_LATER and replies == []
    gate.set()
    ctl.drain()
    assert replies == [True] and runner.running is None


def test_shell_is_the_terminate_delegate():
    pytest.importorskip("WebKit")
    shell = mp._make_shell_class()[0]
    assert shell.instancesRespondToSelector_("applicationShouldTerminate:")
    sig = shell.instanceMethodSignatureForSelector_("applicationShouldTerminate:")
    assert sig.methodReturnType() in (b"Q", b"L", b"I")  # NSApplicationTerminateReply, not an object


def test_terminate_replies_are_appkits():
    appkit = pytest.importorskip("AppKit")
    assert (mp.NS_TERMINATE_NOW, mp.NS_TERMINATE_LATER) == (appkit.NSTerminateNow, appkit.NSTerminateLater)
    assert appkit.NSTerminateCancel not in (mp.NS_TERMINATE_NOW, mp.NS_TERMINATE_LATER)
