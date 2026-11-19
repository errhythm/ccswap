"""Menu bar <-> CLI parity: each bridge action reaches the same switcher method,
with the same arguments, as the CLI command it stands for.

Both sides run against recording fakes: the CLI with its switcher class
patched, the menu bar through ``menubar_popover.dispatch`` into a real
``MenuBarController``. Calls are normalised against the real switcher's
signature (defaults applied, ``json_output`` dropped: the CLI prints, the
menu bar does not), so ``switch(strategy=None)`` and
``switch(strategy=None, json_output=False, models=(), model_source=None)``
compare equal. No real store, Keychain or network is touched.
"""

from __future__ import annotations

import inspect
import sys
from dataclasses import replace
from unittest.mock import MagicMock, patch

import pytest

from ccswap import cli, menubar_controller as mc, menubar_popover as mp
from ccswap.agy import AgyAccountSwitcher
from ccswap.codex import CodexAccountSwitcher
from ccswap.providers import PROVIDERS
from ccswap.settings import AutoSwitchSettings
from ccswap.switcher import ClaudeAccountSwitcher
from tests.test_menubar_controller import FakeSwitcher, snap, usage

REAL = {"claude": ClaudeAccountSwitcher, "codex": CodexAccountSwitcher, "agy": AgyAccountSwitcher}
ACTIONS = ("switch", "switch_to", "set_account_disabled", "remove_account",
           "add_account", "add_account_from_token")


def norm(pid, name, args, kwargs):
    bound = inspect.signature(getattr(REAL[pid], name)).bind(None, *args, **kwargs)
    bound.apply_defaults()
    params = dict(bound.arguments)
    params.pop("self")
    params.pop("json_output", None)
    return name, params


def _recording(name):
    def method(self, *args, **kwargs):
        self.calls.append((name, args, kwargs))
    return method


class Recorder(FakeSwitcher):
    """FakeSwitcher whose actions record the raw call, whatever its arguments."""


for _name in ACTIONS:
    setattr(Recorder, _name, _recording(_name))


# ---- the two sides --------------------------------------------------------------------

@pytest.fixture
def menu(tmp_path, monkeypatch):
    """Send one bridge message; returns (reply, normalised switcher calls)."""
    sws = {
        "claude": Recorder(snap((1, "a@x.com", usage()), (2, "b@x.com", usage())),
                           config_path=tmp_path / "claude.json"),
        "codex": Recorder(snap((1, "c@x.com", usage())), auth_file=tmp_path / "auth.json"),
        "agy": Recorder(snap((1, "g@x.com", None))),
    }
    sws["claude"].current = ("a@x.com", "uuid")  # refreshCreds needs a live login
    providers = {"claude": PROVIDERS["claude"],
                 **{p: replace(PROVIDERS[p], factory=lambda p=p: sws[p]) for p in ("codex", "agy")}}
    monkeypatch.setattr("ccswap.claude_integration.is_enabled", lambda key: False)
    monkeypatch.setattr(mc, "load_settings", lambda root: AutoSwitchSettings())
    ctl = mc.MenuBarController(sws["claude"], tmp_path, tmp_path / "log", notify=lambda *a: None,
                               alert=lambda *a: None, providers=providers)
    ctl._spawn = lambda fn, *a: fn(*a)

    def send(body, settings=None):
        if settings is not None:
            monkeypatch.setattr(mc, "load_settings", lambda root: settings)
        for sw in sws.values():
            sw.calls.clear()
        reply = mp.dispatch(ctl, body, confirm=lambda *_: True)
        return reply, [norm(pid, *c) for pid, sw in sws.items() for c in sw.calls]

    send.ctl, send.sws = ctl, sws
    return send


def run_cli(pid, argv, settings=None):
    """Run ``ccswap [<provider>] <argv>`` over a mocked switcher; normalised calls."""
    mock = MagicMock()
    if pid == "claude":
        with patch("ccswap.cli.ClaudeAccountSwitcher", return_value=mock), \
             patch.object(sys, "argv", ["ccswap", *argv]), \
             patch("os.geteuid", return_value=1000, create=True), \
             patch("ccswap.settings.load_settings", return_value=settings or AutoSwitchSettings()), \
             patch("ccswap.update_check.check_for_update", return_value=None), \
             patch("ccswap.cli._person_at_terminal", return_value=False):
            cli.main()
    elif pid == "codex":
        with patch("ccswap.cli.CodexAccountSwitcher", return_value=mock):
            cli._codex_command(argv)
    else:
        cli._provider_command(replace(PROVIDERS[pid], factory=lambda **_: mock), argv)
    return [norm(pid, name, args, kwargs)
            for name, args, kwargs in mock.mock_calls if name in ACTIONS]


# ---- switch / rotate ---------------------------------------------------------------------

@pytest.mark.parametrize("pid", ["claude", "codex", "agy"])
def test_switch_to_matches_cli(menu, pid):
    expected = run_cli(pid, ["switch", "2"])
    reply, calls = menu({"action": "switch", "provider": pid, "number": 2})
    assert reply["ok"] and calls == expected == [norm(pid, "switch_to", ("2",), {})]
    assert reply["restart"] is PROVIDERS[pid].capabilities.restart_after_switch


@pytest.mark.parametrize("pid", ["claude", "codex"])
@pytest.mark.parametrize("strategy", [None, "best", "next-available", "consume-first"])
def test_rotate_matches_cli(menu, pid, strategy, monkeypatch):
    argv = ["switch"] + (["--strategy", strategy] if strategy else [])
    expected = run_cli(pid, argv)
    sw = menu.sws[pid]
    moved = replace(sw.snapshot, active_number="9")
    monkeypatch.setattr(sw, "switch", lambda **kw: (sw.calls.append(("switch", (), kw)), setattr(sw, "snapshot", moved)))
    reply, calls = menu({"action": "rotate", "provider": pid, "strategy": strategy})
    assert reply["ok"] and calls == expected and calls[0][1]["strategy"] == strategy
    assert reply["restart"] is PROVIDERS[pid].capabilities.restart_after_switch
    assert "note" not in reply


@pytest.mark.parametrize("pid", ["claude", "codex"])
def test_rotate_that_keeps_the_account_says_so_without_a_restart_hint(menu, pid):
    reply, _ = menu({"action": "rotate", "provider": pid, "strategy": "best"})
    assert reply["ok"] and reply["restart"] is False
    assert reply["note"] == f"{PROVIDERS[pid].label}: already on the best account (1)"


@pytest.mark.parametrize("strategy", [None, "best"])
def test_claude_rotate_reads_autoswitch_model_like_cli(menu, strategy):
    settings = AutoSwitchSettings(model="Fable, fable")
    argv = ["switch"] + (["--strategy", strategy] if strategy else [])
    expected = run_cli("claude", argv, settings=settings)
    _, calls = menu({"action": "rotate", "provider": "claude", "strategy": strategy}, settings=settings)
    assert calls == expected
    assert calls[0][1]["models"] == (("Fable",) if strategy else ())


def test_rotate_refused_where_the_cli_has_no_bare_switch(menu):
    with pytest.raises(SystemExit):  # `ccswap agy switch` needs NUM|EMAIL
        run_cli("agy", ["switch"])
    reply, calls = menu({"action": "rotate", "provider": "agy", "strategy": None})
    assert not reply["ok"] and calls == []


@pytest.mark.parametrize("pid", ["claude", "codex"])
def test_rotate_rejects_strategies_the_cli_rejects(menu, pid):
    with pytest.raises(SystemExit):
        run_cli(pid, ["switch", "--strategy", "bogus"])
    reply, calls = menu({"action": "rotate", "provider": pid, "strategy": "bogus"})
    assert not reply["ok"] and calls == []


# ---- add -----------------------------------------------------------------------------

@pytest.mark.parametrize("pid", ["claude", "codex", "agy"])
def test_add_login_matches_cli(menu, pid):
    expected = run_cli(pid, ["add"])
    reply, calls = menu({"action": "addLogin", "provider": pid})
    assert reply["ok"] and calls == expected == [norm(pid, "add_account", (), {})]


def test_add_login_reply_says_already_managed(menu):
    # the store does not change, so the page is told which account it was
    reply, _ = menu({"action": "addLogin", "provider": "codex"})
    assert "already account 1" in reply["note"] and "sign in to it" in reply["note"]


def test_add_login_reply_names_a_new_account(menu):
    sw = menu.sws["agy"]

    def add():
        sw.snapshot = snap((1, "g@x.com", None), (2, "new@x.com", None))
    sw.add_account = add
    reply, _ = menu({"action": "addLogin", "provider": "agy"})
    assert "account 2 added" in reply["note"] and "new@x.com" in reply["note"]


@pytest.mark.parametrize("email", [None, "me@x.com"])
def test_add_token_matches_cli(menu, email):
    expected = run_cli("claude", ["add-token", "sk-tok"] + (["--email", email] if email else []))
    reply, calls = menu({"action": "addToken", "provider": "claude", "token": "sk-tok", "email": email or ""})
    assert reply["ok"] and calls == expected
    assert calls[0][1]["token"] == "sk-tok" and calls[0][1]["email"] == email


@pytest.mark.parametrize("pid", ["codex", "agy"])
def test_add_token_only_where_the_cli_has_it(menu, pid):
    reply, calls = menu({"action": "addToken", "provider": pid, "token": "t"})
    assert not reply["ok"] and calls == []


def test_refresh_creds_is_claude_add(menu):
    expected = run_cli("claude", ["add"])
    reply, calls = menu({"action": "refreshCreds"})
    assert reply["ok"] and calls == expected


# ---- disable / enable / remove ----------------------------------------------------------

@pytest.mark.parametrize("disabled", [True, False])
def test_disable_enable_matches_cli(menu, disabled):
    expected = run_cli("claude", ["disable" if disabled else "enable", "2"])
    reply, calls = menu({"action": "toggleDisabled", "provider": "claude", "number": 2, "disabled": disabled})
    assert reply["ok"] and calls == expected


@pytest.mark.parametrize("disabled", [True, False])
def test_codex_disable_enable_matches_cli(menu, disabled):
    expected = run_cli("codex", ["disable" if disabled else "enable", "1"])
    reply, calls = menu({"action": "toggleDisabled", "provider": "codex", "number": 1, "disabled": disabled})
    assert reply["ok"] and calls == expected == [norm("codex", "set_account_disabled", ("1", disabled), {})]


def test_agy_cannot_disable_in_cli_or_menu(menu):
    with pytest.raises(SystemExit):
        run_cli("agy", ["disable", "1"])
    reply, calls = menu({"action": "toggleDisabled", "provider": "agy", "number": 1, "disabled": True})
    assert not reply["ok"] and calls == []


@pytest.mark.parametrize("pid", ["codex", "agy"])
def test_remove_matches_cli_yes(menu, pid):
    # the menu confirms natively first, then removes like `--yes`
    expected = run_cli(pid, ["remove", "1", "--yes"])
    reply, calls = menu({"action": "remove", "provider": pid, "number": 1})
    assert reply["ok"] and calls == expected


def test_claude_remove_matches_cli_after_confirmation(menu):
    # `ccswap remove N` prompts inside remove_account; the menu prompts first
    [(name, cli_args)] = run_cli("claude", ["remove", "2"])
    reply, calls = menu({"action": "remove", "provider": "claude", "number": 2})
    assert reply["ok"] and calls == [(name, {**cli_args, "assume_yes": True})]


def test_remove_cancelled_reaches_nothing(menu):
    reply = mp.dispatch(menu.ctl, {"action": "remove", "provider": "agy", "number": 1},
                        confirm=lambda *_: False)
    assert reply["cancelled"] and all(sw.calls == [] for sw in menu.sws.values())
