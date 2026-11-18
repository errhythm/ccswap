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
    assert not run({"action": "remove", "provider": "agy", "number": 1})[0]["ok"]
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
    for sel in ("togglePopover:", "tick:", "refreshTick:", "releaseWeb:", "popoverDidClose:",
                "userContentController:didReceiveScriptMessage:"):
        assert shell.instancesRespondToSelector_(sel)
