"""Opt-in usage stats: what is sent, and when nothing is."""

from __future__ import annotations

import json

import pytest

from ccswap import telemetry
from ccswap.settings import set_setting


@pytest.fixture
def sends(monkeypatch):
    """Telemetry able to send (a key, no blocking env); records bodies instead of posting."""
    sent: list[dict] = []
    monkeypatch.setattr(telemetry, "POSTHOG_KEY", "phc_test")
    for name in ("CCSWAP_TELEMETRY", "DO_NOT_TRACK", "CI"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(telemetry, "_spawn", lambda body: sent.append(json.loads(body)))
    return sent


@pytest.mark.parametrize("argv, expected", [
    ([], ("tui", None)),
    (["switch", "alex@example.com"], ("switch", None)),
    (["codex", "switch", "2"], ("switch", "codex")),
    (["--provider", "codex", "list"], ("list", None)),
    (["--list"], ("list", None)),
    (["frobnicate", "/Users/alex/secret"], ("other", None)),
    (["run", "2"], (None, None)),
    (["hub"], (None, None)),
    (["list", "--json"], (None, None)),
])
def test_command_of_sends_only_fixed_names(argv, expected):
    assert telemetry.command_of(argv) == expected


def test_off_until_opted_in_then_one_clean_event_per_hour(tmp_path, sends):
    telemetry.track_command(tmp_path, ["switch", "alex@example.com"])
    assert sends == []  # never asked: off
    set_setting(tmp_path, "telemetry.enabled", "on")
    telemetry.track_command(tmp_path, ["switch", "alex@example.com"])
    telemetry.track_command(tmp_path, ["switch", "3"])  # throttled
    assert len(sends) == 1
    body = json.dumps(sends[0])
    assert "@" not in body and str(tmp_path) not in body and "/Users" not in body
    props = sends[0]["properties"]
    assert props["command"] == "switch" and props["$process_person_profile"] is False
    assert sends[0]["distinct_id"] and sends[0]["api_key"] == "phc_test"
    telemetry.track_command(tmp_path, ["list"])
    assert len(sends) == 2 and sends[1]["distinct_id"] == sends[0]["distinct_id"]


@pytest.mark.parametrize("env", [("DO_NOT_TRACK", "1"), ("CCSWAP_TELEMETRY", "off"), ("CI", "true")])
def test_env_blocks_sending_and_asking(tmp_path, sends, monkeypatch, env):
    set_setting(tmp_path, "telemetry.enabled", "on")
    monkeypatch.setenv(*env)
    telemetry.track_command(tmp_path, ["list"])
    assert sends == [] and not telemetry.should_ask(tmp_path)


def test_no_key_sends_nothing(tmp_path, sends, monkeypatch):
    monkeypatch.setattr(telemetry, "POSTHOG_KEY", "")
    set_setting(tmp_path, "telemetry.enabled", "on")
    telemetry.track_command(tmp_path, ["list"])
    assert sends == []


def test_asks_until_any_answer_is_saved(tmp_path, sends):
    assert telemetry.should_ask(tmp_path)
    set_setting(tmp_path, "telemetry.enabled", "off")
    assert not telemetry.should_ask(tmp_path)


@pytest.mark.asyncio
async def test_tui_asks_once_and_saves_the_answer(tmp_path, sends):
    from ccswap.settings import load_telemetry_settings
    from ccswap.tui.modals import ConfirmModal
    from tests.test_tui import FakeSwitcher, make_account, make_app
    from tests.test_tui_v3 import settle

    app = make_app(FakeSwitcher([make_account(1, active=True)], tmp_path))
    async with app.run_test(size=(100, 30)) as pilot:
        await settle(pilot)
        assert isinstance(app.screen, ConfirmModal)
        await pilot.press("y")
        await settle(pilot)
        assert not isinstance(app.screen, ConfirmModal)
    assert load_telemetry_settings(tmp_path).enabled and not telemetry.should_ask(tmp_path)
