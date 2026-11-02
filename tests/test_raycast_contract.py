"""Pins the ``list --json`` fields the Raycast extension reads (raycast/src/ccswap.ts).

A rename here breaks the extension silently, so change both together and bump
SCHEMA_VERSION in the extension when the shape changes.
"""

from claude_swap.json_output import SCHEMA_VERSION, account_row, usage_to_json


def test_schema_version_matches_extension():
    assert SCHEMA_VERSION == 1


def test_account_row_has_fields_the_extension_reads():
    row = account_row(
        3, "a@example.com", "Org", "uuid", True,
        {"five_hour": {"pct": 12.0}, "seven_day": {"pct": 40.0}},
        alias="work", disabled=True,
    )
    for key in ("number", "email", "organizationName", "active", "usage", "alias", "disabled"):
        assert key in row, key
    assert row["usage"]["fiveHour"]["pct"] == 12.0
    assert row["usage"]["sevenDay"]["pct"] == 40.0


def test_failed_refresh_keeps_last_good_usage():
    row = account_row(
        1, "a@example.com", "", "", False, None,
        usage_fetched_at=1_800_000_000.0, last_good_usage={"five_hour": {"pct": 5.0}},
    )
    assert row["usage"] is None
    assert row["lastGoodUsage"]["fiveHour"]["pct"] == 5.0


def test_codex_weekly_window_name():
    assert usage_to_json({"weekly": {"pct": 42.0}})["weekly"]["pct"] == 42.0
