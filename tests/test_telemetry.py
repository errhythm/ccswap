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
    commands = [s for s in sends if s["event"] == "command"]
    assert len(commands) == 1
    body = json.dumps(sends)
    assert "@" not in body and str(tmp_path) not in body and "/Users" not in body
    props = commands[0]["properties"]
    assert props["command"] == "switch" and props["$process_person_profile"] is False
    assert commands[0]["distinct_id"] and commands[0]["api_key"] == "phc_test"
    telemetry.track_command(tmp_path, ["list"])
    commands = [s for s in sends if s["event"] == "command"]
    assert len(commands) == 2 and commands[1]["distinct_id"] == commands[0]["distinct_id"]


def test_daily_counts_switches_since_last_and_groups_accounts(tmp_path, sends, monkeypatch):
    import time
    from datetime import datetime

    set_setting(tmp_path, "telemetry.enabled", "on")
    (tmp_path / "sequence.json").write_text(json.dumps({"accounts": {"1": {}, "2": {}}}))
    (tmp_path / "codex").mkdir()
    (tmp_path / "codex" / "sequence.json").write_text(
        json.dumps({"accounts": {str(n): {} for n in range(1, 5)}})
    )
    stamp = lambda at: datetime.fromtimestamp(at).strftime("%Y-%m-%d %H:%M:%S,000")
    now = time.time()
    (tmp_path / "claude-swap.log").write_text("".join(
        f"{stamp(at)} - INFO - {msg}\n" for at, msg in [
            (now - 3 * 86400, "Switched from account 1 to 2"),  # older than a day
            (now - 3600, "Switched from account 2 to 1"),
            (now - 600, "Switched codex from account ? to 3"),
            (now - 300, "Usage fetch failed for account 1"),
        ]
    ))
    telemetry.track_command(tmp_path, ["list"])
    telemetry.track_command(tmp_path, ["status"])  # daily already sent today
    daily = [s for s in sends if s["event"] == "daily"]
    assert len(daily) == 1
    props = daily[0]["properties"]
    assert (props["switches_claude"], props["switches_codex"], props["switches_agy"]) == (1, 1, 0)
    assert (props["accounts_claude"], props["accounts_codex"], props["accounts_agy"]) == ("2", "3-5", "0")
    assert "@" not in json.dumps(daily)

    monkeypatch.setattr(telemetry.time, "time", lambda: now + telemetry.DAY_S + 60)
    telemetry.track_command(tmp_path, ["status"])
    daily = [s for s in sends if s["event"] == "daily"]
    assert len(daily) == 2 and daily[1]["properties"]["switches_claude"] == 0  # counted once


@pytest.mark.parametrize("answer, enabled", [("y", True), ("", False), ("nope", False)])
def test_cli_asks_once_on_a_terminal(tmp_path, sends, monkeypatch, answer, enabled):
    from ccswap.settings import load_telemetry_settings

    asked = []
    for stream in ("stdin", "stdout", "stderr"):
        monkeypatch.setattr(f"sys.{stream}.isatty", lambda: True, raising=False)
    monkeypatch.setattr("builtins.input", lambda prompt: asked.append(prompt) or answer)
    telemetry.ask_in_terminal(tmp_path, [])  # the TUI asks in its own dialog
    telemetry.ask_in_terminal(tmp_path, ["list", "--json"])  # machine output: never
    assert asked == []
    telemetry.ask_in_terminal(tmp_path, ["list"])
    telemetry.ask_in_terminal(tmp_path, ["list"])
    assert len(asked) == 1
    assert load_telemetry_settings(tmp_path).enabled is enabled


def test_no_question_while_a_legacy_migration_is_pending(tmp_path, sends, monkeypatch):
    from ccswap import paths

    legacy = tmp_path / "legacy"
    root = tmp_path / "xdg"
    monkeypatch.setattr(paths, "get_legacy_backup_root", lambda: legacy)
    legacy.mkdir()
    (legacy / "sequence.json").write_text("{}")
    assert not telemetry.should_ask(root)  # answering would block the migration
    assert not root.exists()
    paths.migrate_legacy_backup_dir(root)
    assert telemetry.should_ask(root)


def test_cli_never_asks_without_a_terminal(tmp_path, sends, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt: pytest.fail("asked"))
    telemetry.ask_in_terminal(tmp_path, ["list"])  # pytest captures stdio: not a tty
    assert telemetry.should_ask(tmp_path)


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
