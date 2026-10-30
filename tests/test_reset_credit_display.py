"""Expiry presentation uses synthetic quota only, without credentials or I/O."""
from datetime import datetime, timedelta, timezone

import pytest

from claude_swap import reset_credit_display as display

NOW = 1_700_000_000.0


def usage(seconds, count=1):
    return {"reset_credits": {"available": count, "expires_at": datetime.fromtimestamp(
        NOW + seconds, timezone.utc).isoformat()}}


@pytest.mark.parametrize("seconds,severity", [
    (86401, "normal"), (86400, "warning"), (3601, "warning"),
    (3600, "critical"), (1, "critical"), (0, "expired"), (-60, "expired"),
])
def test_urgency_boundaries(seconds, severity):
    result = display.reset_credit_display(usage(seconds), NOW)
    assert result.severity == severity
    if seconds > 0:
        assert "expires in" in result.text
        assert "in 0m" not in result.text
    else:
        assert "expiry passed" in result.text
        assert "refresh usage to confirm" in result.text


def test_local_date_timezone_and_fractional_offset(monkeypatch):
    class LocalDateTime(datetime):
        def astimezone(self, tz=None):
            return super().astimezone(tz or timezone(timedelta(hours=5, minutes=45), "NPT"))
    monkeypatch.setattr(display, "datetime", LocalDateTime)
    result = display.reset_credit_display(usage(3600), NOW)
    assert "2023-11-15 04:58 NPT (+0545)" in result.text


@pytest.mark.parametrize("expiry", [None, "broken", "2026-10-30T12:00:00", 123])
def test_unknown_expiry_is_not_a_guessed_local_time(expiry):
    assert display.reset_credit_display({"reset_credits": {
        "available": 1, "expires_at": expiry}}, NOW).text == "1 banked · expiry unknown"


def test_zero_and_invalid_count_do_not_warn():
    assert display.reset_credit_display(usage(-60, 0), NOW).text == "0 banked"
    for count in (-1, True, "1"):
        assert display.reset_credit_display(usage(60, count), NOW) is None


@pytest.mark.skipif(not hasattr(__import__("time"), "tzset"), reason="POSIX timezone selection")
@pytest.mark.parametrize("iso,clock", [
    ("2026-10-25T00:30:00Z", "2026-10-25 02:30 CEST (+0200)"),
    ("2026-10-25T01:30:00Z", "2026-10-25 02:30 CET (+0100)"),
])
def test_dst_repeated_local_hour_has_unambiguous_offset(monkeypatch, iso, clock):
    import time
    with monkeypatch.context() as context:
        context.setenv("TZ", "Europe/Berlin")
        time.tzset()
        try:
            expiry = datetime.fromisoformat(iso.replace("Z", "+00:00"))
            result = display.reset_credit_display({"reset_credits": {
                "available": 1, "expires_at": iso}}, expiry.timestamp() - 60)
            assert clock in result.text
        finally:
            context.undo()
            time.tzset()


@pytest.mark.parametrize("seconds,countdown", [(86399, "23h 59m"), (3599, "59m"), (1, "less than 1m")])
def test_countdown_does_not_round_back_into_a_less_urgent_window(seconds, countdown):
    assert f"expires in {countdown}" in display.reset_credit_display(usage(seconds), NOW).text
