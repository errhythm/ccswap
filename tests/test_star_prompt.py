"""The star prompt shows rarely, only interactively, and respects "never"."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from ccswap import star_prompt
from ccswap.usage_store import FetchRecord, UsageStore


class FakeSwitcher:
    def __init__(self, backup_dir: Path, active: int):
        self.backup_dir = backup_dir
        self.active = active

    def _get_sequence_data(self) -> dict:
        return {
            "activeAccountNumber": self.active,
            "accounts": {
                "1": {"email": "a@example.com", "organizationUuid": ""},
                "2": {"email": "b@example.com", "organizationUuid": ""},
            },
        }


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(star_prompt, "_interactive", lambda: True)
    opened: list[str] = []
    monkeypatch.setattr(star_prompt.webbrowser, "open", opened.append)
    answers: list[str] = []
    monkeypatch.setattr("builtins.input", lambda _prompt: answers.pop(0))
    return tmp_path, answers, opened


def _left_at(tmp_path: Path, pct: float) -> None:
    UsageStore(tmp_path / "cache").record(
        {"1": FetchRecord(usage={"five_hour": {"pct": pct}, "seven_day": {"pct": 10.0}})},
        {"1": ("a@example.com", "")},
    )


def _state(tmp_path: Path) -> dict:
    return json.loads((tmp_path / "cache" / star_prompt.STATE_NAME).read_text())


def test_shows_after_switch_off_a_near_limit_account(env, capsys):
    tmp_path, answers, opened = env
    _left_at(tmp_path, 96)
    answers.append("")
    star_prompt.maybe_ask(FakeSwitcher(tmp_path, active=2), "1")
    assert "a ⭐ on GitHub" in capsys.readouterr().out
    assert opened == [star_prompt.REPO_URL]
    assert _state(tmp_path)["never"] is True


def test_silent_below_threshold_or_without_switch(env, capsys):
    tmp_path, _answers, _opened = env
    _left_at(tmp_path, 80)
    star_prompt.maybe_ask(FakeSwitcher(tmp_path, active=2), "1")
    _left_at(tmp_path, 99)
    star_prompt.maybe_ask(FakeSwitcher(tmp_path, active=1), "1")  # stayed on 1
    assert "⭐" not in capsys.readouterr().out


def test_later_waits_sixty_days_and_never_is_final(env, capsys, monkeypatch):
    tmp_path, answers, opened = env
    _left_at(tmp_path, 97)
    switcher = FakeSwitcher(tmp_path, active=2)

    answers.extend(["x", "l"])  # unknown input asks again; l is "later"
    star_prompt.maybe_ask(switcher, "1")
    assert "⭐" in capsys.readouterr().out
    assert opened == []
    star_prompt.maybe_ask(switcher, "1")  # within 60 days
    assert "⭐" not in capsys.readouterr().out

    later = time.time() + star_prompt.INTERVAL_S + 1
    monkeypatch.setattr(star_prompt.time, "time", lambda: later)
    monkeypatch.setattr(star_prompt, "_left_near_limit", lambda *a: True)
    answers.append("n")
    star_prompt.maybe_ask(switcher, "1")
    assert "⭐" in capsys.readouterr().out
    monkeypatch.setattr(star_prompt.time, "time", lambda: later * 2)
    star_prompt.maybe_ask(switcher, "1")
    assert "⭐" not in capsys.readouterr().out


def test_never_prompts_without_a_terminal(env, capsys, monkeypatch):
    tmp_path, _answers, _opened = env
    _left_at(tmp_path, 99)
    monkeypatch.setattr(star_prompt, "_interactive", lambda: False)
    star_prompt.maybe_ask(FakeSwitcher(tmp_path, active=2), "1")
    assert "⭐" not in capsys.readouterr().out
