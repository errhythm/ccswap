"""Tests for quota-based switching of saved Codex accounts."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from ccswap.autoswitch import TickOutcome
from ccswap.codex_autoswitch import CodexAutoSwitchEngine
from ccswap.models import AccountSnapshot, AccountsSnapshot
from ccswap.settings import AutoSwitchSettings
from ccswap.usage_store import UsageEntry


def _account(number: str, pct: float, *, active: bool = False) -> AccountSnapshot:
    return AccountSnapshot(
        number=number,
        email=f"{number}@example.com",
        org_name="Codex",
        org_uuid=number,
        is_active=active,
        kind="oauth",
        switchable=True,
        usage=UsageEntry(last_good={"five_hour": {"pct": pct}}),
    )


class FakeCodexSwitcher:
    def __init__(self, tmp_path: Path, accounts: tuple[AccountSnapshot, ...]) -> None:
        self.backup_dir = tmp_path
        self.accounts = accounts
        self.switched_to: list[str] = []
        self.fetches: list[set[str] | None] = []

    def accounts_snapshot(self, fetch: set[str] | None = None) -> AccountsSnapshot:
        self.fetches.append(fetch)
        active = next((a.number for a in self.accounts if a.is_active), None)
        return AccountsSnapshot(active, self.accounts, taken_at=0)

    def switch_to(self, number: str, *, json_output: bool) -> dict:
        self.switched_to.append(number)
        source = next(account for account in self.accounts if account.is_active)
        target = next(account for account in self.accounts if account.number == number)
        return {
            "switched": True,
            "from": {"number": int(source.number), "email": source.email},
            "to": {"number": int(target.number), "email": target.email},
        }


def test_switches_to_the_healthiest_candidate_and_records_restart_warning(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("ccswap.codex.is_codex_running", lambda: True)
    switcher = FakeCodexSwitcher(
        tmp_path, (_account("1", 95, active=True), _account("2", 20))
    )
    events = []
    engine = CodexAutoSwitchEngine(
        switcher, AutoSwitchSettings(), events.append, clock=lambda: 1_000
    )

    outcome = engine.tick()

    assert outcome is TickOutcome.SWITCHED
    assert switcher.fetches == [None]
    assert switcher.switched_to == ["2"]
    switch = events[-1]
    assert switch.kind == "switch"
    assert len(switch.warnings) == 1
    assert "Codex is running" in switch.warnings[0]


def test_records_next_launch_warning_when_codex_is_not_running(tmp_path, monkeypatch):
    monkeypatch.setattr("ccswap.codex.is_codex_running", lambda: False)
    switcher = FakeCodexSwitcher(
        tmp_path, (_account("1", 95, active=True), _account("2", 20))
    )
    events = []
    engine = CodexAutoSwitchEngine(
        switcher, AutoSwitchSettings(), events.append, clock=lambda: 1_000
    )

    outcome = engine.tick()

    assert outcome is TickOutcome.SWITCHED
    switch = events[-1]
    assert len(switch.warnings) == 1
    assert "next time you start Codex" in switch.warnings[0]


def test_does_not_switch_while_active_account_is_below_threshold(tmp_path):
    switcher = FakeCodexSwitcher(
        tmp_path, (_account("1", 75, active=True), _account("2", 10))
    )
    events = []
    engine = CodexAutoSwitchEngine(
        switcher, AutoSwitchSettings(), events.append, clock=lambda: 1_000
    )

    outcome = engine.tick()

    assert outcome is TickOutcome.NO_ACTION
    assert switcher.switched_to == []
    assert events[-1].kind == "no-switch"
    assert events[-1].reason == "below-threshold"


def test_dry_run_reports_a_switch_without_changing_the_active_account(tmp_path):
    switcher = FakeCodexSwitcher(
        tmp_path, (_account("1", 100, active=True), _account("2", 20))
    )
    events = []
    engine = CodexAutoSwitchEngine(
        switcher,
        AutoSwitchSettings(),
        events.append,
        dry_run=True,
        clock=lambda: 1_000,
    )

    outcome = engine.tick()

    assert outcome is TickOutcome.SWITCHED
    assert switcher.switched_to == []
    assert events[-1].kind == "switch"
    assert events[-1].dry_run


def test_session_candidate_is_not_switchable(tmp_path):
    from dataclasses import replace

    switcher = FakeCodexSwitcher(tmp_path, (
        _account('1', 100, active=True), replace(_account('2', 10), switchable=False),
    ))
    events = []
    engine = CodexAutoSwitchEngine(switcher, AutoSwitchSettings(), events.append)
    assert engine.tick() is TickOutcome.BLOCKED
    assert switcher.switched_to == []
    assert events[-1].reason == 'no-candidates'


def test_final_custody_rejection_is_not_retried(tmp_path, monkeypatch):
    from ccswap.exceptions import SwitchError

    switcher = FakeCodexSwitcher(tmp_path, (
        _account('1', 100, active=True), _account('2', 10), _account('3', 20),
    ))
    calls = []

    def refuse(number, **_):
        calls.append(number)
        raise SwitchError('session live')

    monkeypatch.setattr(switcher, 'switch_to', refuse)
    events = []
    engine = CodexAutoSwitchEngine(switcher, AutoSwitchSettings(), events.append)
    assert engine.tick() is TickOutcome.ERROR
    assert calls == ['2']
    assert events[-1].kind == 'error'
    assert not engine.state_path.exists()


def _weekly_account(number, pct, reset, *, active=False, weekly_pct=20):
    account = _account(number, pct, active=active)
    resets_at = (
        datetime.fromtimestamp(reset, timezone.utc).isoformat()
        if reset is not None else None
    )
    return replace(
        account,
        usage=UsageEntry(last_good={
            "five_hour": {"pct": pct},
            "weekly": {"pct": weekly_pct, "resets_at": resets_at},
        }),
    )


def test_per_window_gates_trigger_and_keep_absolute_poll_headroom(tmp_path, monkeypatch):
    monkeypatch.setattr("ccswap.codex.is_codex_running", lambda: False)
    for window in ("5h", "7d"):
        switcher = FakeCodexSwitcher(tmp_path, (
            _weekly_account("1", 75 if window == "5h" else 20, 3000,
                            active=True, weekly_pct=75 if window == "7d" else 20),
            _weekly_account("2", 10, 2000, weekly_pct=10),
        ))
        settings = AutoSwitchSettings(threshold=90, **{
            "threshold_5h" if window == "5h" else "threshold_7d": 75,
        })
        events = []
        engine = CodexAutoSwitchEngine(
            switcher, settings, events.append, dry_run=True, clock=lambda: 1000
        )
        assert engine.tick() is TickOutcome.SWITCHED
        assert events[0].headroom["1"] == 25
        assert events[-1].trigger == "proactive"


def test_proactive_candidate_must_clear_each_gate(tmp_path):
    for window in ("5h", "7d"):
        switcher = FakeCodexSwitcher(tmp_path, (
            _weekly_account("1", 95, 3000, active=True),
            _weekly_account("2", 71 if window == "5h" else 10, 2000,
                            weekly_pct=71 if window == "7d" else 10),
        ))
        settings = AutoSwitchSettings(threshold=90, **{
            "threshold_5h" if window == "5h" else "threshold_7d": 70,
        })
        events = []
        engine = CodexAutoSwitchEngine(
            switcher, settings, events.append, clock=lambda: 1000
        )
        assert engine.tick() is TickOutcome.BLOCKED
        assert not switcher.switched_to


def test_consume_first_below_threshold_switches_even_with_less_headroom(tmp_path, monkeypatch):
    monkeypatch.setattr("ccswap.codex.is_codex_running", lambda: False)
    switcher = FakeCodexSwitcher(tmp_path, (
        _weekly_account("1", 20, 3000, active=True),
        _weekly_account("2", 60, 2000),
    ))
    events = []
    engine = CodexAutoSwitchEngine(
        switcher, AutoSwitchSettings(strategy="consume-first"),
        events.append, clock=lambda: 1000,
    )
    assert engine.tick() is TickOutcome.SWITCHED
    assert switcher.switched_to == ["2"]
    assert events[-1].trigger == "consume-first"


def test_consume_first_below_threshold_holds_without_eligible_candidate(tmp_path):
    cases = (
        (2000, 3000, 20, 20),  # active resets first
        (2000, 2000, 20, 20),  # strictly sooner is required
        (None, 2000, 20, 20),
        (900, 2000, 20, 20),  # past active reset is unknown
        (3000, None, 20, 20),
        (3000, 900, 20, 20),  # past candidate reset is unknown
        (3000, 1000, 20, 20),  # reset must be in the future
        (3000, 2000, 100, 20),  # exhausted candidate
        (3000, 2000, 20, 70),  # candidate at its Weekly gate
        (3000, 2000, 70, 20),  # candidate at its 5h gate
    )
    for active_reset, candidate_reset, pct, weekly_pct in cases:
        switcher = FakeCodexSwitcher(tmp_path, (
            _weekly_account("1", 20, active_reset, active=True),
            _weekly_account("2", pct, candidate_reset, weekly_pct=weekly_pct),
        ))
        events = []
        engine = CodexAutoSwitchEngine(
            switcher, AutoSwitchSettings(strategy="consume-first",
                                         threshold_5h=70, threshold_7d=70),
            events.append, clock=lambda: 1000,
        )
        assert engine.tick() is TickOutcome.NO_ACTION
        assert not switcher.switched_to
        assert events[-1].reason == "below-threshold"


def test_consume_first_without_candidates_is_not_blocked(tmp_path):
    switcher = FakeCodexSwitcher(tmp_path, (
        _weekly_account("1", 20, 3000, active=True),
    ))
    events = []
    engine = CodexAutoSwitchEngine(
        switcher, AutoSwitchSettings(strategy="consume-first"),
        events.append, clock=lambda: 1000,
    )
    assert engine.tick() is TickOutcome.NO_ACTION
    assert events[-1].reason == "below-threshold"


def test_consume_first_obeys_cooldown(tmp_path):
    switcher = FakeCodexSwitcher(tmp_path, (
        _weekly_account("1", 20, 3000, active=True),
        _weekly_account("2", 30, 2000),
    ))
    events = []
    engine = CodexAutoSwitchEngine(
        switcher, AutoSwitchSettings(strategy="consume-first"),
        events.append, clock=lambda: 1000,
    )
    engine.state_path.write_text(json.dumps({"lastSwitchAt": 999}))
    assert engine.tick() is TickOutcome.NO_ACTION
    assert not switcher.switched_to
    assert events[-1].reason == "cooldown"


def test_consume_first_ranks_future_weekly_resets_before_headroom(tmp_path, monkeypatch):
    monkeypatch.setattr("ccswap.codex.is_codex_running", lambda: False)
    for active_pct in (95, 100):
        for unknown_reset in (None, 900):
            switcher = FakeCodexSwitcher(tmp_path, (
                _weekly_account("1", active_pct, 4000, active=True),
                _weekly_account("2", 10, unknown_reset),
                _weekly_account("3", 30, 3000),
                _weekly_account("4", 60, 2000),
            ))
            events = []
            engine = CodexAutoSwitchEngine(
                switcher, AutoSwitchSettings(strategy="consume-first"),
                events.append, dry_run=True, clock=lambda: 1000,
            )
            assert engine.tick() is TickOutcome.SWITCHED
            assert events[-1].to_ref["number"] == 4
            assert not switcher.switched_to


def test_consume_first_dry_run_does_not_write_state(tmp_path, monkeypatch):
    monkeypatch.setattr("ccswap.codex.is_codex_running", lambda: False)
    switcher = FakeCodexSwitcher(tmp_path, (
        _weekly_account("1", 20, 3000, active=True),
        _weekly_account("2", 30, 2000),
    ))
    events = []
    engine = CodexAutoSwitchEngine(
        switcher, AutoSwitchSettings(strategy="consume-first"),
        events.append, dry_run=True, clock=lambda: 1000,
    )
    assert engine.tick() is TickOutcome.SWITCHED
    assert events[-1].trigger == "consume-first"
    assert events[-1].dry_run
    assert not switcher.switched_to
    assert not engine.state_path.exists()


def test_consume_first_rechecks_cooldown_under_lock(tmp_path, monkeypatch):
    switcher = FakeCodexSwitcher(tmp_path, (
        _weekly_account("1", 20, 3000, active=True),
        _weekly_account("2", 30, 2000),
    ))
    events = []
    engine = CodexAutoSwitchEngine(
        switcher, AutoSwitchSettings(strategy="consume-first"),
        events.append, clock=lambda: 1000,
    )
    states = iter(({}, {"lastSwitchAt": 999}))
    monkeypatch.setattr(engine, "_read_state", lambda: next(states))
    assert engine.tick() is TickOutcome.NO_ACTION
    assert not switcher.switched_to
    assert events[-1].reason == "cooldown"


def test_consume_first_unknown_resets_fall_back_to_headroom(tmp_path, monkeypatch):
    monkeypatch.setattr("ccswap.codex.is_codex_running", lambda: False)
    switcher = FakeCodexSwitcher(tmp_path, (
        _weekly_account("1", 100, 3000, active=True),
        _weekly_account("2", 60, None),
        _weekly_account("3", 10, 900),
    ))
    events = []
    engine = CodexAutoSwitchEngine(
        switcher, AutoSwitchSettings(strategy="consume-first"),
        events.append, dry_run=True, clock=lambda: 1000,
    )
    assert engine.tick() is TickOutcome.SWITCHED
    assert events[-1].to_ref["number"] == 3


def test_below_threshold_detail_reports_the_tightest_gate(tmp_path):
    for strategy in ("best", "consume-first"):
        for five_hour, weekly, expected in (
            (70, 80, "Weekly 80% < 85%"),
            (86, 80, "5h 86% < 90%"),
            (85, 80, "5h 85% < 90%"),  # equal slack: higher usage binds
        ):
            switcher = FakeCodexSwitcher(tmp_path, (
                _weekly_account("1", five_hour, 3000, active=True, weekly_pct=weekly),
            ))
            events = []
            engine = CodexAutoSwitchEngine(
                switcher,
                AutoSwitchSettings(strategy=strategy, threshold=90, threshold_7d=85),
                events.append, clock=lambda: 1000,
            )
            assert engine.tick() is TickOutcome.NO_ACTION
            assert events[-1].reason == "below-threshold"
            assert events[-1].detail == expected


def test_below_threshold_detail_without_overrides_keeps_historical_text(tmp_path):
    switcher = FakeCodexSwitcher(tmp_path, (_account("1", 75.4, active=True),))
    events = []
    engine = CodexAutoSwitchEngine(
        switcher, AutoSwitchSettings(threshold=90), events.append, clock=lambda: 1000,
    )
    assert engine.tick() is TickOutcome.NO_ACTION
    assert events[-1].detail == "75% < 90%"


def test_best_ranks_candidates_by_distance_to_their_gates(tmp_path, monkeypatch):
    monkeypatch.setattr("ccswap.codex.is_codex_running", lambda: False)
    switcher = FakeCodexSwitcher(tmp_path, (
        _weekly_account("1", 95, 3000, active=True),
        _weekly_account("2", 10, 2000, weekly_pct=40),
        _weekly_account("3", 50, 2000, weekly_pct=0),
    ))
    events = []
    engine = CodexAutoSwitchEngine(
        switcher, AutoSwitchSettings(threshold=90, threshold_7d=50, hysteresis_pct=10),
        events.append, dry_run=True, clock=lambda: 1000,
    )
    assert engine.tick() is TickOutcome.SWITCHED
    assert events[-1].to_ref["number"] == 3
    # Polls continue reporting real quota headroom, not policy-adjusted values.
    assert events[0].headroom["2"] == 60
    assert events[0].headroom["3"] == 50


def test_stop_before_run_loop_is_not_lost(tmp_path):
    switcher = FakeCodexSwitcher(tmp_path, (_account("1", 10, active=True),))
    engine = CodexAutoSwitchEngine(
        switcher, AutoSwitchSettings(), lambda e: None, clock=lambda: 1_000
    )
    engine.stop()
    assert engine.run_loop() == 0  # returns at once instead of ticking forever
    assert switcher.fetches == []
