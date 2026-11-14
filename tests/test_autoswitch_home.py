"""Return-to-home behaviour of the auto-switch engine (autoswitch.homeAccount)."""

from __future__ import annotations

from pathlib import Path

from claude_swap.autoswitch import NoSwitchEvent, SwitchEvent, TickOutcome
from tests.test_autoswitch import (
    _R_LATER,
    _R_SOON,
    EngineHarness,
    _usage,
    _usage7,
)

HOME = "a@example.com"


def _harness(temp_home: Path, *, active: int = 2, **settings) -> EngineHarness:
    # threshold 90, hysteresis 10: home must read below 80 to be returned to.
    settings.setdefault("home_account", HOME)
    settings.setdefault("cooldown_seconds", 0.0)
    h = EngineHarness(temp_home, **settings)
    h.seed(1, HOME)
    h.seed(2, "b@example.com")
    h.make_live(HOME if active == 1 else "b@example.com", active)
    data = h.switcher._get_sequence_data()
    data["activeAccountNumber"] = active
    h.switcher._write_json(h.switcher.sequence_file, data)
    return h


def _switch(h: EngineHarness) -> SwitchEvent:
    return next(e for e in h.events if isinstance(e, SwitchEvent))


def test_returns_home_once_below_gate_minus_hysteresis(temp_home):
    h = _harness(temp_home)
    assert h.tick_with_usage({"1": _usage(70), "2": _usage(30)}) is TickOutcome.SWITCHED
    assert h.active_number() == 1
    assert _switch(h).trigger == "home"


def test_no_return_inside_hysteresis_band(temp_home):
    h = _harness(temp_home)
    assert h.tick_with_usage({"1": _usage(85), "2": _usage(30)}) is TickOutcome.NO_ACTION
    assert h.active_number() == 2


def test_no_return_when_home_usage_unknown(temp_home):
    h = _harness(temp_home)
    assert h.tick_with_usage({"1": None, "2": _usage(30)}) is TickOutcome.NO_ACTION
    assert h.active_number() == 2


def test_no_return_when_home_disabled(temp_home):
    h = _harness(temp_home)
    h.switcher.set_account_disabled("1", True)
    assert h.tick_with_usage({"1": _usage(10), "2": _usage(30)}) is TickOutcome.NO_ACTION
    assert h.active_number() == 2


def test_unset_setting_never_returns(temp_home):
    h = _harness(temp_home, home_account=None)
    assert h.tick_with_usage({"1": _usage(10), "2": _usage(30)}) is TickOutcome.NO_ACTION
    assert h.active_number() == 2


def test_unknown_home_reads_as_unset(temp_home):
    h = _harness(temp_home, home_account="gone@example.com")
    assert h.tick_with_usage({"1": _usage(10), "2": _usage(30)}) is TickOutcome.NO_ACTION
    assert h.active_number() == 2


def test_cooldown_holds_the_return(temp_home):
    h = _harness(temp_home, cooldown_seconds=300.0)
    usage = {"1": _usage(10), "2": _usage(30)}
    # A switch just happened (lastSwitchAt = now): the return waits.
    h.engine._mutate_state(lambda s: s.update(lastSwitchAt=h.clock.now))
    assert h.tick_with_usage(usage) is TickOutcome.NO_ACTION
    assert h.active_number() == 2
    h.clock.advance(301)
    assert h.tick_with_usage(usage) is TickOutcome.SWITCHED
    assert h.active_number() == 1


def test_dry_run_reports_without_switching(temp_home):
    h = _harness(temp_home)
    h.engine.dry_run = True
    assert h.tick_with_usage({"1": _usage(10), "2": _usage(30)}) is TickOutcome.SWITCHED
    assert h.active_number() == 2
    assert _switch(h).trigger == "home" and _switch(h).dry_run


def test_consume_first_does_not_leave_home_below_gates(temp_home):
    h = _harness(temp_home, active=1, strategy="consume-first")
    outcome = h.tick_with_usage({
        "1": _usage7(20, 20, _R_LATER),
        "2": _usage7(10, 10, _R_SOON),  # resets sooner, would normally win
    })
    assert outcome is TickOutcome.NO_ACTION
    assert h.active_number() == 1
    reasons = [e.reason for e in h.events if isinstance(e, NoSwitchEvent)]
    assert reasons == ["below-threshold"]


def test_leaves_home_once_it_hits_a_gate(temp_home):
    h = _harness(temp_home, active=1, strategy="consume-first")
    outcome = h.tick_with_usage({
        "1": _usage7(95, 20, _R_LATER),
        "2": _usage7(10, 10, _R_SOON),
    })
    assert outcome is TickOutcome.SWITCHED
    assert h.active_number() == 2
