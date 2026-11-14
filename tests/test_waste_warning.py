"""Waste warning: a weekly window about to reset with much of it unused."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from unittest.mock import patch

import pytest

from claude_swap.autoswitch import WasteWarningEvent
from claude_swap.exceptions import ConfigError
from claude_swap.settings import (
    SETTING_SPECS,
    UsageSettings,
    load_usage_settings,
    parse_setting_value,
    set_setting,
)
from tests.test_autoswitch import EngineHarness, _iso_at, _usage7


@pytest.fixture
def h(temp_home):
    h = EngineHarness(temp_home)
    h.seed(1, "a@example.com")
    h.seed(2, "b@example.com")
    h.make_live("a@example.com", 1)
    return h


def _enable(h):
    set_setting(h.switcher.backup_dir, "usage.wasteWarning", "true")


def _warnings(h):
    return [e for e in h.events if isinstance(e, WasteWarningEvent)]


def _tick(h, pct7=40, hours=3):
    reset = _iso_at(h.clock.now + hours * 3600)
    h.tick_with_usage({"1": _usage7(10, pct7, reset), "2": _usage7(10, 10, None)})


def test_defaults_and_validation(tmp_path):
    assert UsageSettings().waste_warning is False
    assert (UsageSettings().waste_warning_hours, UsageSettings().waste_warning_pct) == (6, 40)
    spec = SETTING_SPECS["usage.wasteWarningHours"]
    assert parse_setting_value(spec, "12") == 12
    for bad in ("0", "169", "x"):
        with pytest.raises(ConfigError):
            parse_setting_value(spec, bad)
    with pytest.raises(ConfigError):
        parse_setting_value(SETTING_SPECS["usage.wasteWarningPct"], "101")
    (tmp_path / "settings.json").write_text(
        json.dumps({"usage": {"wasteWarning": True, "wasteWarningHours": 99999}})
    )
    loaded = load_usage_settings(tmp_path)
    assert loaded.waste_warning is True and loaded.waste_warning_hours == 6


def test_fires_when_condition_holds(h):
    _enable(h)
    _tick(h)
    [ev] = _warnings(h)
    assert ev.human() == "Account 1 (a@example.com) resets in 3h with 60% unused"


def test_off_by_default(h):
    _tick(h)
    assert _warnings(h) == []


def test_not_when_reset_is_far(h):
    _enable(h)
    _tick(h, hours=20)
    assert _warnings(h) == []


def test_not_when_little_is_left(h):
    _enable(h)
    _tick(h, pct7=70)
    assert _warnings(h) == []


def test_once_per_reset_across_restart_then_again_next_reset(h):
    _enable(h)
    _tick(h)
    h.engine = h._make_engine()  # a restarted process reads the state file
    _tick(h)
    assert len(_warnings(h)) == 1
    h.clock.advance(8 * 24 * 3600)  # next weekly window
    _tick(h)
    assert len(_warnings(h)) == 2, [e.human() for e in h.events]


def _tick_at(h, reset_epoch, pct7=40, usage_one=None):
    iso = datetime.fromtimestamp(reset_epoch, timezone.utc).isoformat()
    h.tick_with_usage({
        "1": usage_one if usage_one is not None else _usage7(10, pct7, iso),
        "2": _usage7(10, 10, None),
    })


def test_reset_drift_between_polls_is_one_warning(h):
    _enable(h)
    base = h.clock.now + 3 * 3600
    for drift in (0.0, 0.7, -0.8):
        _tick_at(h, base + drift)
    assert len(_warnings(h)) == 1
    h.clock.advance(8 * 24 * 3600)
    _tick_at(h, h.clock.now + 3 * 3600)
    assert len(_warnings(h)) == 2, [e.human() for e in h.events]


def test_renumbered_account_is_not_warned_again(h):
    _enable(h)
    _tick(h)
    data = h.switcher._get_sequence_data()
    data["accounts"]["3"] = data["accounts"].pop("1")
    data["sequence"] = [2, 3]
    h.switcher._write_json(h.switcher.sequence_file, data)
    reset = _iso_at(h.clock.now + 3 * 3600)
    h.tick_with_usage({"3": _usage7(10, 40, reset), "2": _usage7(10, 10, None)})
    assert len(_warnings(h)) == 1


def test_second_engine_with_empty_memory_does_not_double_notify(h):
    _enable(h)
    _tick(h)
    other = h._make_engine()  # fresh memory, but the claim is in the state file
    h.engine = other
    _tick(h)
    assert len(_warnings(h)) == 1


def test_dry_run_dedupes_in_memory_without_writing_state(h):
    _enable(h)
    h.engine.dry_run = True
    _tick(h)
    _tick(h)
    assert len(_warnings(h)) == 1
    assert "wasteWarned" not in h.state()


def test_sibling_accounts_sharing_an_email_each_warn_once(h):
    _enable(h)
    data = h.switcher._get_sequence_data()
    data["accounts"]["2"].update(email="a@example.com", organizationUuid="org-b")
    h.switcher._write_json(h.switcher.sequence_file, data)
    for _ in range(4):
        h.tick_with_usage({
            "1": _usage7(10, 40, _iso_at(h.clock.now + 3 * 3600)),
            "2": _usage7(10, 40, _iso_at(h.clock.now + 5 * 3600)),
        })
        h.clock.advance(0)  # same instant, resets stay fixed below
    assert len(_warnings(h)) == 2, [e.human() for e in h.events]


def test_already_claimed_key_does_not_rewrite_state_every_tick(h):
    _enable(h)
    _tick(h)
    h.engine = h._make_engine()  # fresh memory; the claim is in the file
    with patch.object(h.engine, "_mutate_state", wraps=h.engine._mutate_state) as m:
        _tick(h)
        _tick(h)
    assert m.call_count == 1
    assert len(_warnings(h)) == 1


def test_ignored_usage_shapes(h):
    _enable(h)
    soon = h.clock.now + 3 * 3600
    _tick_at(h, soon, usage_one="token-expired")
    _tick_at(h, soon, usage_one={"five_hour": {"pct": 1.0}})
    _tick_at(h, h.clock.now - 60)  # reset already past
    assert _warnings(h) == []
