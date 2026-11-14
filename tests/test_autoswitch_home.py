"""Return-to-home behaviour of the auto-switch engine (autoswitch.homeAccount)."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from claude_swap.autoswitch import NoSwitchEvent, SwitchEvent, TickOutcome
from tests.test_autoswitch import (
    _R_LATER,
    _R_SOON,
    EngineHarness,
    _usage,
    _entry_for,
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


def test_email_resolution_ignores_case(temp_home):
    h = _harness(temp_home)
    assert h.switcher.resolve_account("A@Example.COM")[0] == "1"


def test_shared_email_returns_to_the_stored_org_slot(temp_home):
    h = _harness(temp_home, home_account=f"{HOME}|org-b")
    h.seed(3, HOME)
    data = h.switcher._get_sequence_data()
    data["accounts"]["1"]["organizationUuid"] = "org-a"
    data["accounts"]["3"]["organizationUuid"] = "org-b"
    h.switcher._write_json(h.switcher.sequence_file, data)
    outcome = h.tick_with_usage({"1": _usage(10), "2": _usage(30), "3": _usage(10)})
    assert outcome is TickOutcome.SWITCHED
    assert h.active_number() == 3


def test_personal_home_is_not_confused_with_an_org_seat(temp_home):
    # "email|" pins a personal account; slot 3 is an org seat on the same email.
    h = _harness(temp_home, home_account=f"{HOME}|")
    h.seed(3, HOME)
    data = h.switcher._get_sequence_data()
    data["accounts"]["3"]["organizationUuid"] = "org-b"
    h.switcher._write_json(h.switcher.sequence_file, data)
    outcome = h.tick_with_usage({"1": _usage(10), "2": _usage(30), "3": _usage(10)})
    assert outcome is TickOutcome.SWITCHED
    assert h.active_number() == 1


def test_ambiguous_plain_email_reads_as_unset(temp_home):
    h = _harness(temp_home)
    h.seed(3, HOME)
    outcome = h.tick_with_usage({"1": _usage(10), "2": _usage(30), "3": _usage(10)})
    assert outcome is TickOutcome.NO_ACTION
    assert h.active_number() == 2


def test_fresh_read_overrules_a_recovered_stored_reading(temp_home):
    h = _harness(temp_home)
    stored = {
        "1": _entry_for(_usage(10), h.clock.now),
        "2": _entry_for(_usage(30), h.clock.now),
    }
    fresh = _entry_for(_usage(95), h.clock.now)

    def collect(fetch=None, **_kw):
        return {**stored, "1": fresh} if fetch == {"1"} else stored

    with patch.object(h.switcher, "usage_entries_by_account", side_effect=collect):
        assert h.engine.tick() is TickOutcome.NO_ACTION
    assert h.active_number() == 2


def test_blocked_return_is_reported_once_and_not_refetched(temp_home):
    h = _harness(temp_home)
    later = h.clock.now + 10_000
    entries = {}
    for num, pct in (("1", 10), ("2", 30)):
        entries[num] = replace(_entry_for(_usage(pct), h.clock.now), next_poll_at=later)
    home_fetches = []

    def collect(fetch=None, **_kw):
        if fetch == {"1"}:
            home_fetches.append(1)
        return entries

    with (
        patch.object(h.switcher, "usage_entries_by_account", side_effect=collect),
        patch.object(h.switcher, "live_session_pids_for", return_value=[4242]),
    ):
        for _ in range(3):
            assert h.engine.tick() is TickOutcome.NO_ACTION
            h.clock.advance(60)
    blocked = [
        e for e in h.events
        if isinstance(e, NoSwitchEvent) and e.reason == "home-blocked"
    ]
    assert [e.detail for e in blocked] == ["skip-live-session"]
    assert not home_fetches
    assert h.active_number() == 2


def test_per_gate_override_keeps_home_out(temp_home):
    h = _harness(temp_home, threshold_7d=70.0)
    # 7d at 65 is inside the 10 point margin of its 70 gate.
    outcome = h.tick_with_usage({"1": _usage7(10, 65), "2": _usage7(10, 10)})
    assert outcome is TickOutcome.NO_ACTION
    assert h.active_number() == 2
