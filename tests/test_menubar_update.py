"""Update prompts: the menu bar's banner and upgrade, and the CLI's one-time question."""

from __future__ import annotations

import subprocess
import sys

import pytest

from ccswap import menubar_classic, menubar_controller as mc, menubar_popover, update_check
from ccswap.settings import set_setting
from tests.test_menubar_controller import env  # noqa: F401 - fixture

# conftest stubs this for every controller test; these exercise the real one.
_REAL_CHECK = mc.MenuBarController.__dict__["_check_update"]


@pytest.fixture
def checked(env, monkeypatch):  # noqa: F811
    monkeypatch.setattr(mc.MenuBarController, "_check_update", _REAL_CHECK)
    monkeypatch.setattr(update_check, "available_update", lambda current: "9.0.0")
    return env


def test_menubar_shows_an_available_update(checked):
    assert checked.ctl.tick()
    assert checked.ctl.payload()["update"] == {"version": "9.0.0", "busy": False}
    rows = menubar_classic.menu_spec(checked.ctl)
    assert any(r.label == "Update to ccswap 9.0.0" and r.action == "start_upgrade" for r in rows)


def test_menubar_skips_the_check_when_updates_are_off(checked):
    set_setting(checked.root, "updates.check", "off")
    checked.ctl.tick()
    assert checked.ctl.payload()["update"] is None


def test_successful_upgrade_restarts_into_the_new_version(checked, monkeypatch):
    restarts = []
    monkeypatch.setattr(update_check, "upgrade_quietly", lambda current: ("9.0.0", "Updated ccswap to 9.0.0."))
    monkeypatch.setattr(mc.MenuBarController, "_restart_after_update", lambda self, v: restarts.append(v))
    checked.ctl.tick()
    reply = menubar_popover.dispatch(checked.ctl, {"action": "upgrade"})
    assert reply["ok"] and restarts == ["9.0.0"]
    assert checked.ctl.payload()["update"] is None and checked.notes


def test_failed_upgrade_alerts_and_keeps_the_banner(checked, monkeypatch):
    monkeypatch.setattr(update_check, "upgrade_quietly", lambda current: (None, "uv tool upgrade ccswap failed: boom"))
    checked.ctl.tick()
    assert menubar_popover.dispatch(checked.ctl, {"action": "upgrade"})["ok"] is False
    assert checked.alerts[-1][1].endswith("boom")
    assert checked.ctl.payload()["update"]["version"] == "9.0.0"


@pytest.mark.parametrize("on_disk, version", [("9.0.0", "9.0.0"), ("1.0.0", None)])
def test_upgrade_quietly_reports_what_landed(monkeypatch, on_disk, version):
    monkeypatch.setattr(update_check, "_detect_install_method", lambda: "uv")
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, "", ""))
    monkeypatch.setattr(update_check, "_installed_version_on_disk", lambda: on_disk)
    got, message = update_check.upgrade_quietly("1.0.0")
    assert got == version
    assert ("still at" in message) is (version is None)


@pytest.mark.parametrize("answer, upgraded", [("y", True), ("", False)])
def test_cli_offers_the_upgrade_once_per_version(monkeypatch, answer, upgraded):
    ran, asked = [], []
    for stream in ("stdin", "stdout", "stderr"):
        monkeypatch.setattr(f"sys.{stream}.isatty", lambda: True, raising=False)
    monkeypatch.setattr(update_check, "can_self_upgrade", lambda: True)
    monkeypatch.setattr(update_check, "available_update", lambda current: "9.0.0")
    monkeypatch.setattr(update_check, "run_self_upgrade", lambda current: ran.append(current) or 0)
    monkeypatch.setattr("builtins.input", lambda prompt: asked.append(prompt) or answer)
    assert update_check.ask_to_upgrade("1.0.0")
    assert not update_check.ask_to_upgrade("1.0.0")  # asked about 9.0.0 already
    assert len(asked) == 1 and bool(ran) is upgraded
    monkeypatch.setattr(update_check, "available_update", lambda current: "9.1.0")
    assert update_check.ask_to_upgrade("1.0.0")  # a newer release asks again


def test_cli_never_asks_without_a_terminal(monkeypatch):
    monkeypatch.setattr(update_check, "available_update", lambda current: "9.0.0")
    monkeypatch.setattr("builtins.input", lambda prompt: pytest.fail("asked"))
    assert not update_check.ask_to_upgrade("1.0.0")
