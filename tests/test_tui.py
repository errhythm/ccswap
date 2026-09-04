"""Tests for the Textual TUI: data service units + Pilot-driven app tests.

The Pilot tests run the real app headlessly against a ``FakeSwitcher`` that
implements exactly the structured surface the TUI consumes
(``accounts_snapshot``, ``switch_to``/``switch``/``remove_account``/add
flows) — no scraping, no real credentials, no network.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_swap.autoswitch import NoSwitchEvent, SwitchEvent
from claude_swap.json_output import USAGE_API_KEY, USAGE_TOKEN_EXPIRED
from claude_swap.models import AccountSnapshot, AccountsSnapshot
from claude_swap.switcher import ClaudeAccountSwitcher
from claude_swap.tui import data as tui_data
from claude_swap.usage_store import UsageEntry


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _iso_in(seconds: float) -> str:
    return (
        (datetime.now(timezone.utc) + timedelta(seconds=seconds))
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def make_entry(
    pct5: float | None = 25.0,
    pct7: float | None = 10.0,
    *,
    sentinel: str | None = None,
    age_s: float = 5.0,
    scoped: list[tuple[str, float]] | None = None,
    spend: dict | None = None,
) -> UsageEntry:
    """``pct5``/``pct7`` of None omit that window (e.g. annual plans lack 7d)."""
    if sentinel is not None:
        return UsageEntry(sentinel=sentinel)
    last_good: dict = {}
    if pct5 is not None:
        last_good["five_hour"] = {"pct": pct5, "resets_at": _iso_in(7200)}
    if pct7 is not None:
        last_good["seven_day"] = {"pct": pct7, "resets_at": _iso_in(86400 * 3)}
    if scoped is not None:
        last_good["scoped"] = [
            {"name": name, "pct": pct, "resets_at": _iso_in(86400 * 2)}
            for name, pct in scoped
        ]
    if spend is not None:
        last_good["spend"] = spend
    return UsageEntry(
        last_good=last_good,
        fetched_at=time.time() - age_s,
        age_s=age_s,
    )


def make_account(
    number: int | str,
    *,
    active: bool = False,
    switchable: bool = True,
    kind: str = "oauth",
    entry: UsageEntry | None = None,
    email: str | None = None,
    alias: str = "",
    disabled: bool = False,
) -> AccountSnapshot:
    return AccountSnapshot(
        number=str(number),
        email=email or f"user{number}@example.com",
        org_name="",
        org_uuid="",
        is_active=active,
        kind=kind,
        switchable=switchable,
        usage=entry if entry is not None else make_entry(),
        alias=alias,
        disabled=disabled,
    )


def make_usage_at(
    fetched_at: float | None,
    pct: float = 25.0,
    *,
    sentinel: str | None = None,
) -> UsageEntry:
    return UsageEntry(
        sentinel=sentinel,
        last_good={"five_hour": {"pct": pct, "resets_at": _iso_in(7200)}},
        fetched_at=fetched_at,
        age_s=(time.time() - fetched_at) if fetched_at is not None else None,
    )


class FakeSwitcher:
    """Structured-surface stand-in for ClaudeAccountSwitcher."""

    def __init__(self, accounts: list[AccountSnapshot], backup_dir: Path):
        self._accounts = list(accounts)
        self.backup_dir = backup_dir
        self.active = next(
            (a.number for a in accounts if a.is_active), None
        )
        self.calls: list[tuple] = []
        self.fetch_sets: list[set[str] | None] = []

    # -- surface the TUI consumes ------------------------------------------

    def accounts_snapshot(self, fetch: set[str] | None = None) -> AccountsSnapshot:
        self.fetch_sets.append(fetch)
        return AccountsSnapshot(
            active_number=self.active,
            accounts=tuple(self._accounts),
            taken_at=time.time(),
        )

    def current_account_number(self) -> str | None:
        return self.active

    def switch_to(
        self, identifier: str, json_output: bool = False, force: bool = False
    ) -> dict:
        self.calls.append(("switch_to", str(identifier)))
        old = self.active
        self.active = str(identifier)
        self._accounts = [
            dataclasses.replace(a, is_active=(a.number == self.active))
            for a in self._accounts
        ]
        return {
            "switched": True,
            "from": {"number": int(old) if old else None, "email": ""},
            "to": {
                "number": int(identifier),
                "email": f"user{identifier}@example.com",
            },
            "reason": "requested",
        }

    def switch(self, strategy: str | None = None, json_output: bool = False) -> dict:
        self.calls.append(("switch", strategy))
        return {"switched": False, "from": None, "to": None, "reason": "no-better-target"}

    def remove_account(self, identifier: str, assume_yes: bool = False) -> None:
        self.calls.append(("remove", str(identifier), assume_yes))
        self._accounts = [a for a in self._accounts if a.number != str(identifier)]
        print(f"Removed account {identifier}")

    def set_account_disabled(self, identifier: str, disabled: bool) -> None:
        self.calls.append(("set_disabled", str(identifier), disabled))
        self._accounts = [
            dataclasses.replace(a, disabled=disabled)
            if a.number == str(identifier)
            else a
            for a in self._accounts
        ]
        verb = "Disabled" if disabled else "Enabled"
        print(f"{verb} Account-{identifier}")

    def add_account(self, slot: int | None = None, assume_yes: bool = False) -> None:
        self.calls.append(("add", slot, assume_yes))
        print("Added Account 9: fresh@example.com")

    def add_account_from_token(
        self,
        token: str,
        email: str | None = None,
        slot: int | None = None,
        assume_yes: bool = False,
    ) -> None:
        self.calls.append(("add_token", token, email, slot, assume_yes))
        print(f"Added Account {slot or 9}")

    def set_poll_policy_inputs(
        self, threshold: float, models: tuple[str, ...]
    ) -> None:
        self._poll_inputs_override = (threshold, models)

    def clear_poll_policy_inputs(self) -> None:
        self._poll_inputs_override = None


class BlockingSnapshotSwitcher(FakeSwitcher):
    """Fake switcher with independently gated normal/store snapshot lanes."""

    def __init__(
        self,
        normal_account: AccountSnapshot,
        store_account: AccountSnapshot,
        backup_dir: Path,
    ):
        super().__init__([normal_account], backup_dir)
        self.normal_account = normal_account
        self.store_account = store_account
        self.normal_started = threading.Event()
        self.normal_release = threading.Event()
        self.normal_done = threading.Event()
        self.store_started = threading.Event()
        self.store_release = threading.Event()
        self.store_done = threading.Event()
        self.block_store = False

    def accounts_snapshot(self, fetch: set[str] | None = None) -> AccountsSnapshot:
        self.fetch_sets.append(fetch)
        if fetch is None:
            self.normal_started.set()
            self.normal_release.wait(timeout=2)
            self.normal_done.set()
            account = self.normal_account
        else:
            self.store_started.set()
            if self.block_store:
                self.store_release.wait(timeout=2)
            self.store_done.set()
            account = self.store_account
        return AccountsSnapshot(
            active_number=account.number,
            accounts=(account,),
            taken_at=time.time(),
        )


class ErrorSnapshotSwitcher(FakeSwitcher):
    def accounts_snapshot(self, fetch: set[str] | None = None) -> AccountsSnapshot:
        from claude_swap.exceptions import ConfigError

        raise ConfigError("malformed Codex sequence.json")


class BlockingErrorSnapshotSwitcher(ErrorSnapshotSwitcher):
    def __init__(self, accounts: list[AccountSnapshot], backup_dir: Path):
        super().__init__(accounts, backup_dir)
        self.started = threading.Event()
        self.release = threading.Event()

    def accounts_snapshot(self, fetch: set[str] | None = None) -> AccountsSnapshot:
        self.started.set()
        self.release.wait(timeout=2)
        return super().accounts_snapshot(fetch)


class BlockingActionSwitcher(FakeSwitcher):
    def __init__(self, accounts: list[AccountSnapshot], backup_dir: Path):
        super().__init__(accounts, backup_dir)
        self.switch_started = threading.Event()
        self.switch_release = threading.Event()

    def switch_to(
        self, identifier: str, json_output: bool = False, force: bool = False
    ) -> dict:
        self.switch_started.set()
        self.switch_release.wait(timeout=2)
        return super().switch_to(identifier, json_output=json_output, force=force)


def make_app(
    fake: FakeSwitcher,
    codex: FakeSwitcher | None = None,
    *,
    start: str = "dashboard",
    detected: str | None = None,
):
    from claude_swap.tui.app import CswapApp

    # Existing Claude-focused Pilot tests should not instantiate the real
    # Codex switcher (and thereby consult real credentials). Combined-view
    # tests inject the provider fake they need explicitly.
    return CswapApp(
        fake,
        codex_switcher=codex or FakeSwitcher([], fake.backup_dir),
        start=start,
        detected=detected,
    )


def snap_of(app, provider: str = "claude") -> AccountsSnapshot | None:
    return app.snapshots[provider]


async def settle(pilot) -> None:
    """Let thread workers finish and their UI updates apply.

    The (fake) auto engine worker deliberately runs until its screen stops
    it, so waiting on it would block; wait on everything else.
    """
    app = pilot.app
    pending = [w for w in app.workers if w.group != "engine"]
    if pending:
        await app.workers.wait_for_complete(pending)
    await pilot.pause()
    await pilot.pause()


async def wait_event(event: threading.Event, timeout: float = 1.0) -> None:
    assert await asyncio.to_thread(event.wait, timeout)


async def menu_select(pilot, action_id: str) -> None:
    """Drive the dashboard menu: highlight the entry by id, press Enter."""
    from textual.widgets import ListView

    from claude_swap.tui.widgets import MenuItem

    menu = pilot.app.screen.query_one("#menu", ListView)
    items = list(menu.query(MenuItem))
    menu.index = next(
        i for i, item in enumerate(items) if item.action_id == action_id
    )
    await pilot.pause()
    await pilot.press("enter")
    await pilot.pause()


# ---------------------------------------------------------------------------
# Data service units (sync)
# ---------------------------------------------------------------------------


class TestFormatting:
    def test_format_duration(self):
        assert tui_data.format_duration(42) == "42s"
        assert tui_data.format_duration(180) == "3m"
        assert tui_data.format_duration(7980) == "2h 13m"
        assert tui_data.format_duration(3600 * 26) == "1d 2h"

    def test_format_age_fresh_is_silent(self):
        # Ages inside the serve TTL are the polling cadence at work, not
        # staleness worth flagging.
        assert tui_data.format_age(3.0) is None
        assert tui_data.format_age(120) is None
        assert tui_data.format_age(None) is None
        assert tui_data.format_age(400) == "· 6m ago"

    def test_sentinel_labels_match_cswap_list(self):
        # The TUI must describe sentinel states with the exact wording `cswap
        # list` prints — owned-and-expired means Claude Code refreshes the
        # active account, not that the user must re-login.
        assert (
            tui_data.sentinel_label(USAGE_TOKEN_EXPIRED)
            == "token expired — refresh deferred this pass; retries automatically"
        )
        from claude_swap.switcher import SENTINEL_NOTES

        for sentinel, note in SENTINEL_NOTES.items():
            assert tui_data.sentinel_label(sentinel) == note
        assert tui_data.sentinel_label("unknown state") == "unknown state"

    def test_sentinel_card_shows_last_seen_like_cswap_list(self):
        # A sentinel is a live overlay — the entry can still carry the last
        # good measurement, and `cswap list` prints it as a "last seen" line.
        # The card must too (except for API-key accounts, which have no quota).
        from claude_swap.tui.widgets import account_card_text

        entry = UsageEntry(
            sentinel=USAGE_TOKEN_EXPIRED,
            last_good={"five_hour": {"pct": 53.0}},
            fetched_at=time.time() - 720,
            age_s=720.0,
        )
        card = account_card_text(make_account(1, active=True, entry=entry), 80).plain
        assert "token expired — refresh deferred this pass; retries automatically" in card
        assert "last seen 53% used" in card

        no_history = account_card_text(
            make_account(1, entry=UsageEntry(sentinel=USAGE_TOKEN_EXPIRED)), 80
        ).plain
        assert "last seen" not in no_history

        api_key = account_card_text(
            make_account(
                1,
                kind="api_key",
                entry=dataclasses.replace(entry, sentinel=USAGE_API_KEY),
            ),
            80,
        ).plain
        assert "last seen" not in api_key

    def test_account_card_uses_light_palette_when_passed(self):
        from claude_swap.tui.theme import ACCENT_LIGHT, CSWAP_LIGHT, Palette
        from claude_swap.tui.widgets import account_card_text

        acc = make_account(1, active=True, entry=make_entry(pct5=95.0))
        text = account_card_text(acc, 100, palette=Palette.from_theme(CSWAP_LIGHT))
        styles = {str(span.style) for span in text.spans}
        assert any(ACCENT_LIGHT in s for s in styles)  # active marker uses light accent

    def test_window_helpers(self):
        entry = make_entry(pct5=47.0)
        assert tui_data.window_pct(entry.last_good, "five_hour") == 47.0
        assert tui_data.window_pct(None, "five_hour") is None
        text = tui_data.window_reset_text(entry.last_good, "five_hour", time.time())
        assert text is not None and text.startswith("resets ")
        assert tui_data.window_reset_text(None, "five_hour", time.time()) is None

    def test_reset_clock(self):
        # Same-day reset → bare HH:MM; a reset days out carries its date.
        now = time.time()
        entry = make_entry()  # 5h resets in 2h, 7d in 3d
        clock5 = tui_data.reset_clock(entry.last_good["five_hour"], now)
        assert clock5 is not None and clock5.count(":") == 1
        clock7 = tui_data.reset_clock(entry.last_good["seven_day"], now)
        import calendar

        months = list(calendar.month_abbr)[1:]
        assert clock7 is not None and any(m in clock7 for m in months)

    def test_reset_clock_unknown_or_elapsed_is_none(self):
        now = time.time()
        assert tui_data.reset_clock(None, now) is None
        assert tui_data.reset_clock({"pct": 5.0}, now) is None
        assert tui_data.reset_clock({"resets_at": "garbage"}, now) is None
        # elapsed reset: the row says "resets now" — no clock to show
        elapsed = {"resets_at": _iso_in(-60)}
        assert tui_data.reset_clock(elapsed, now) is None
        assert tui_data.reset_text(elapsed, now) == "resets now"


class TestSnapshotSource:
    def _source(self, tmp_path: Path, accounts=None):
        fake = FakeSwitcher(
            accounts
            or [make_account(1, active=True), make_account(2)],
            tmp_path,
        )
        return fake, tui_data.SnapshotSource(fake)

    def test_every_pass_is_store_governed(self, tmp_path):
        # Pacing lives in the usage store (poll plans + freshness + atomic
        # reservation), so every take is the same on-demand pass `cswap list`
        # runs — including the user's explicit refresh, which cannot bypass
        # the store's per-account cadence.
        fake, source = self._source(tmp_path)
        source.take()
        source.take()
        source.take(full=True)
        assert fake.fetch_sets == [None, None, None]

    def test_store_only_never_fetches(self, tmp_path):
        fake, source = self._source(tmp_path)
        source.take(store_only=True)
        assert fake.fetch_sets == [set()]

    def test_expired_sentinel_retained_until_fetched_at_advances(self, tmp_path):
        expired = make_account(
            1,
            active=True,
            entry=make_usage_at(100.0, sentinel=USAGE_TOKEN_EXPIRED),
        )
        fresh_same_stamp = make_account(1, active=True, entry=make_usage_at(100.0))
        fresh_new_stamp = make_account(1, active=True, entry=make_usage_at(101.0))
        fake, source = self._source(tmp_path, [expired])

        assert source.take().accounts[0].usage.sentinel == USAGE_TOKEN_EXPIRED
        fake._accounts = [fresh_same_stamp]
        assert source.take(store_only=True).accounts[0].usage.sentinel == USAGE_TOKEN_EXPIRED
        fake._accounts = [fresh_new_stamp]
        assert source.take(store_only=True).accounts[0].usage.sentinel is None

    def test_expired_sentinel_clears_on_superseding_sentinel(self, tmp_path):
        expired = make_account(
            1,
            active=True,
            entry=make_usage_at(100.0, sentinel=USAGE_TOKEN_EXPIRED),
        )
        api_key = make_account(
            1,
            active=True,
            kind="api_key",
            entry=make_usage_at(None, sentinel=USAGE_API_KEY),
        )
        fake, source = self._source(tmp_path, [expired])

        source.take()
        fake._accounts = [api_key]
        assert source.take(store_only=True).accounts[0].usage.sentinel == USAGE_API_KEY

    def test_expired_sentinel_clears_on_identity_replacement(self, tmp_path):
        expired = make_account(
            1,
            active=True,
            email="old@example.com",
            entry=make_usage_at(100.0, sentinel=USAGE_TOKEN_EXPIRED),
        )
        replacement = make_account(
            1,
            active=True,
            email="new@example.com",
            entry=make_usage_at(100.0),
        )
        fake, source = self._source(tmp_path, [expired])

        source.take()
        fake._accounts = [replacement]
        assert source.take(store_only=True).accounts[0].usage.sentinel is None

    def test_late_worker_fetched_at_regression_is_rejected(self, tmp_path):
        newer = make_account(1, active=True, entry=make_usage_at(200.0, pct=80.0))
        older = make_account(1, active=True, entry=make_usage_at(100.0, pct=10.0))
        fake, source = self._source(tmp_path, [newer])

        source.take()
        fake._accounts = [older]
        snap = source.take(store_only=True)
        usage = snap.accounts[0].usage
        assert usage.fetched_at == 200.0
        assert usage.last_good["five_hour"]["pct"] == 80.0

    def test_late_expired_sentinel_cannot_replace_newer_usage(self, tmp_path):
        newer = make_account(1, active=True, entry=make_usage_at(200.0, pct=80.0))
        older = make_account(
            1,
            active=True,
            entry=make_usage_at(100.0, pct=10.0, sentinel=USAGE_TOKEN_EXPIRED),
        )
        fake, source = self._source(tmp_path, [newer])

        source.take()
        fake._accounts = [older]
        usage = source.take(store_only=True).accounts[0].usage
        assert usage.sentinel is None
        assert usage.fetched_at == 200.0
        assert usage.last_good["five_hour"]["pct"] == 80.0


class TestUsageRows:
    """The card's rows must mirror the CLI's _format_usage_lines semantics."""

    def test_absent_window_produces_no_row(self):
        from claude_swap.tui.widgets import usage_rows

        entry = make_entry(pct5=47.0, pct7=None)  # annual plan: no 7d window
        labels = [label for label, *_ in usage_rows(entry.last_good, time.time())]
        assert labels == ["5h"]

    def test_scoped_models_and_over_limit_marker(self):
        from claude_swap.tui.widgets import usage_rows

        entry = make_entry(scoped=[("Fable", 100.0), ("Opus", 12.0)])
        rows = usage_rows(entry.last_good, time.time())
        labels = [label for label, *_ in rows]
        assert labels == ["5h", "7d", "Fable", "Opus"]
        fable = next(row for row in rows if row[0] == "Fable")
        assert "(!)" in fable[2]
        # the marker stays terminal in the clock-extended variant too
        assert fable[3].endswith("(!)") and " · " in fable[3]

    def test_spend_row_first_with_amounts(self):
        from claude_swap.tui.widgets import usage_rows

        entry = make_entry(spend={"used": 12.5, "limit": 50.0, "pct": 25.0, "currency": "USD"})
        rows = usage_rows(entry.last_good, time.time())
        assert rows[0][0] == "$$"
        assert "$12.50 / $50.00" in rows[0][2]

    def test_suffix_full_extends_countdown_with_clock(self):
        from claude_swap.tui.widgets import usage_rows

        entry = make_entry(pct5=47.0)
        row5 = usage_rows(entry.last_good, time.time())[0]
        assert row5[2].startswith("resets ")
        assert row5[3].startswith(row5[2] + " · ")

    def test_spend_clock_sits_with_reset_not_after_amounts(self):
        from claude_swap.tui.widgets import usage_rows

        entry = make_entry(
            spend={
                "used": 12.5,
                "limit": 50.0,
                "pct": 25.0,
                "currency": "USD",
                "resets_at": _iso_in(7200),
            }
        )
        spend = usage_rows(entry.last_good, time.time())[0]
        assert spend[0] == "$$"
        assert " · " in spend[3]
        assert spend[3].index(" · ") < spend[3].index("$12.50")

    def test_codex_weekly_row_and_banked_reset_text(self):
        from claude_swap.tui.widgets import reset_credits_text, usage_rows

        # Keep the boundary exact across platforms: round-tripping an arbitrary
        # time.time() value through ISO microseconds can land just below 48h,
        # which the countdown correctly floors to "1d 23h".
        now = 1_700_000_000.0
        usage = {
            "weekly": {"pct": 42.0},
            "reset_credits": {
                "available": 3,
                "expires_at": datetime.fromtimestamp(
                    now + 2 * 86400, timezone.utc
                ).isoformat(),
            },
        }

        assert [row[0] for row in usage_rows(usage, now)] == ["Weekly"]
        assert reset_credits_text(usage, now) == (
            "3 banked · earliest expires 2d"
        )

    def test_banked_resets_render_in_the_card_and_the_mini_line(self):
        """The banked-reset lines must actually render, in both themes.

        ``reset_credits_text`` being correct is not enough: only a Codex
        account has ``reset_credits``, so the two call sites that draw it are
        skipped by every Claude-only fixture — a bad style reference there
        raises NameError at paint time and no other test notices.
        """
        from claude_swap.tui.theme import CSWAP_LIGHT, Palette
        from claude_swap.tui.widgets import account_card_text, mini_account_text

        now = 1_700_000_000.0
        entry = UsageEntry(
            last_good={
                "weekly": {"pct": 42.0},
                "reset_credits": {"available": 3},
            },
            fetched_at=now,
        )
        acc = make_account(1, active=True, entry=entry)
        for palette in (Palette.DARK, Palette.from_theme(CSWAP_LIGHT)):
            assert "Resets 3 banked" in account_card_text(
                acc, 80, now=now, palette=palette
            ).plain
            assert "Resets 3 banked" in mini_account_text(
                acc, now, palette=palette
            ).plain

    def test_no_data_no_rows(self):
        from claude_swap.tui.widgets import usage_rows

        assert usage_rows(None, time.time()) == []
        assert usage_rows({}, time.time()) == []

    def test_seven_day_ahead_of_pace_marker(self):
        # 1 day elapsed of the week, 50% used -> far ahead of the ~14% expected.
        from claude_swap.tui.widgets import usage_rows

        now = time.time()
        last_good = {"seven_day": {"pct": 50.0, "resets_at": _iso_in(86400 * 6)}}
        row = usage_rows(last_good, now, now)[0]
        assert "(ahead of pace)" in row[2]
        assert "(ahead of pace)" in row[3]

    def test_five_hour_never_shows_pace_marker(self):
        from claude_swap.tui.widgets import usage_rows

        now = time.time()
        last_good = {"five_hour": {"pct": 90.0, "resets_at": _iso_in(3600 * 4)}}
        row = usage_rows(last_good, now, now)[0]
        assert "pace" not in row[2]

    def test_scoped_ahead_of_pace_marker(self):
        from claude_swap.tui.widgets import usage_rows

        now = time.time()
        last_good = {"scoped": [{"name": "Fable", "pct": 50.0, "resets_at": _iso_in(86400 * 6)}]}
        row = usage_rows(last_good, now, now)[0]
        assert "(ahead of pace)" in row[2]

    def test_maxed_scoped_marker_wins_over_pace(self):
        from claude_swap.tui.widgets import usage_rows

        now = time.time()
        last_good = {"scoped": [{"name": "Fable", "pct": 100.0, "resets_at": _iso_in(86400 * 6)}]}
        row = usage_rows(last_good, now, now)[0]
        assert "(!)" in row[2]
        assert "ahead of pace" not in row[2]

    def test_no_pace_marker_without_fetched_at(self):
        from claude_swap.tui.widgets import usage_rows

        now = time.time()
        last_good = {"seven_day": {"pct": 50.0, "resets_at": _iso_in(86400 * 6)}}
        row = usage_rows(last_good, now)[0]
        assert "pace" not in row[2]

    def test_card_shows_clock_only_where_it_fits(self):
        # Per-row degradation: the wide card shows every clock, a mid width
        # keeps 5h/7d clocks while the longer spend row falls back to its
        # countdown, and a narrow card is exactly the old countdown-only look.
        from claude_swap.tui.widgets import account_card_text

        entry = make_entry(
            spend={
                "used": 12.5,
                "limit": 50.0,
                "pct": 25.0,
                "currency": "USD",
                "resets_at": _iso_in(7200),
            }
        )
        acc = make_account(1, active=True, entry=entry)

        wide = account_card_text(acc, 100).plain
        assert wide.count(" · ") == 3

        mid_lines = account_card_text(acc, 78).plain.splitlines()
        spend_line = next(line for line in mid_lines if "$12.50" in line)
        assert " · " not in spend_line
        for line in mid_lines:
            if "resets" in line and "$12.50" not in line:
                assert " · " in line

        narrow = account_card_text(acc, 40).plain
        assert " · " not in narrow


class TestMiniAccountText:
    def test_seven_day_ahead_of_pace_marker(self):
        from claude_swap.tui.widgets import mini_account_text

        now = time.time()
        entry = UsageEntry(
            last_good={"seven_day": {"pct": 50.0, "resets_at": _iso_in(86400 * 6)}},
            fetched_at=now,
            age_s=0.0,
        )
        acc = make_account(1, entry=entry)
        assert "(ahead)" in mini_account_text(acc, now).plain

    def test_five_hour_never_shows_pace_marker(self):
        from claude_swap.tui.widgets import mini_account_text

        now = time.time()
        entry = UsageEntry(
            last_good={"five_hour": {"pct": 90.0, "resets_at": _iso_in(3600 * 4)}},
            fetched_at=now,
            age_s=0.0,
        )
        acc = make_account(1, entry=entry)
        assert "pace" not in mini_account_text(acc, now).plain

    def test_no_pace_marker_without_fetched_at(self):
        from claude_swap.tui.widgets import mini_account_text

        now = time.time()
        entry = UsageEntry(
            last_good={"seven_day": {"pct": 50.0, "resets_at": _iso_in(86400 * 6)}},
            fetched_at=None,
            age_s=None,
        )
        acc = make_account(1, entry=entry)
        assert "pace" not in mini_account_text(acc, now).plain


class TestRunAction:
    def test_captures_output_and_payload(self):
        def fn():
            print("hello")
            return {"switched": True}

        result = tui_data.run_action(fn)
        assert result.ok and result.payload == {"switched": True}
        assert "hello" in result.output

    def test_switch_error_is_captured_not_raised(self):
        from claude_swap.exceptions import ClaudeSwitchError

        def fn():
            raise ClaudeSwitchError("boom")

        result = tui_data.run_action(fn)
        assert not result.ok
        assert "boom" in result.output

    def test_unexpected_input_becomes_eoferror(self):
        def fn():
            input("should not block")

        result = tui_data.run_action(fn)
        assert not result.ok
        assert "interactive input" in result.output

    def test_first_line_strips_ansi(self):
        def fn():
            print("\x1b[1mBold headline\x1b[0m")

        assert tui_data.run_action(fn).first_line == "Bold headline"


# ---------------------------------------------------------------------------
# Pilot tests (async)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestDashboard:
    async def test_panel_shows_active_full_and_others_mini(self, tmp_path):
        fake = FakeSwitcher(
            [
                make_account(1, active=True, entry=make_entry(47.0, 63.0)),
                make_account(2, entry=make_entry(92.0, 71.0)),
            ],
            tmp_path,
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            from claude_swap.tui.widgets import AccountsPanel

            panel = app.screen.query_one(AccountsPanel).render().plain
            assert "user1@example.com" in panel and "● active" in panel
            assert "resets" in panel  # the active card is the full one
            assert "user2@example.com" in panel and "92%" in panel
            # the mini line has no bars — bar glyphs only in the active card
            mini_part = panel.split("user2@example.com", 1)[1]
            assert "━" not in mini_part

    async def test_disabled_marker_on_active_card_and_mini(self, tmp_path):
        # A disabled account is still shown; it's just annotated so the user
        # can see it's held out of auto-rotation — on the full card when it's
        # the active login, and on the one-line form otherwise.
        fake = FakeSwitcher(
            [
                make_account(1, active=True, disabled=True),
                make_account(2, disabled=True),
            ],
            tmp_path,
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            from claude_swap.tui.widgets import AccountsPanel

            panel = app.screen.query_one(AccountsPanel).render().plain
            assert "● active" in panel  # still the active card
            # both the active card and the mini row carry the marker
            assert panel.count("(disabled)") == 2

    async def test_active_card_skips_absent_window_and_shows_scoped(self, tmp_path):
        fake = FakeSwitcher(
            [
                make_account(
                    1,
                    active=True,
                    entry=make_entry(pct5=47.0, pct7=None, scoped=[("Fable", 62.0)]),
                )
            ],
            tmp_path,
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            from claude_swap.tui.widgets import AccountsPanel

            panel = app.screen.query_one(AccountsPanel).render().plain
            assert "5h" in panel
            assert "7d" not in panel  # annual plan: no invented row
            assert "usage unknown" not in panel
            assert "Fable" in panel and "62%" in panel

    async def test_mini_line_skips_absent_window(self, tmp_path):
        fake = FakeSwitcher(
            [
                make_account(1, active=True),
                make_account(2, entry=make_entry(pct5=92.0, pct7=None)),
            ],
            tmp_path,
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            from claude_swap.tui.widgets import AccountsPanel

            panel = app.screen.query_one(AccountsPanel).render().plain
            mini_part = panel.split("user2@example.com", 1)[1]
            assert "5h 92%" in mini_part
            assert "7d" not in mini_part

    async def test_menu_is_default_navigation_and_nests(self, tmp_path):
        fake = FakeSwitcher([make_account(1, active=True)], tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            from textual.widgets import ListView

            from claude_swap.tui.widgets import MenuItem

            menu = app.screen.query_one("#menu", ListView)
            root_items = list(menu.query(MenuItem))
            ids = [item.action_id for item in root_items]
            assert ids == [
                "switch",
                "watch",
                "auto",
                "add-menu",
                "disable-menu",
                "remove-menu",
                "settings-menu",
                "quit",
            ]
            from textual.widgets import Static

            auto_item = next(item for item in root_items if item.action_id == "auto")
            assert auto_item.query_one(Static).render().plain == "Auto-switch view…"
            # nest into Add (index 3), then back out with escape
            await pilot.press("down", "down", "down", "enter")
            await pilot.pause()
            ids = [item.action_id for item in menu.query(MenuItem)]
            assert ids == ["add-login:claude", "add-token", "add-login:codex", "back"]
            await pilot.press("escape")
            await pilot.pause()
            ids = [item.action_id for item in menu.query(MenuItem)]
            assert ids[0] == "switch"

    async def test_filtered_dashboard_shows_one_section_with_its_header(self, tmp_path):
        (tmp_path / "settings.json").write_text(json.dumps({"ui": {"view": "codex"}}))
        claude = FakeSwitcher([make_account(1, active=True)], tmp_path)
        codex = FakeSwitcher([make_account(2, active=True)], tmp_path)
        app = make_app(claude, codex)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            from claude_swap.tui.widgets import AccountsPanel

            panel = app.screen.query_one(AccountsPanel).render().plain
            assert "Codex" in panel
            assert "Claude Code" not in panel

    async def test_flipping_view_repaints_an_already_pushed_switch_screen(self, tmp_path):
        claude = FakeSwitcher([make_account(1, active=True)], tmp_path)
        codex = FakeSwitcher([make_account(2, active=True)], tmp_path)
        app = make_app(claude, codex)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            await menu_select(pilot, "switch")
            from claude_swap.tui.dashboard import SwitchScreen
            from claude_swap.tui.widgets import AccountItem

            assert isinstance(app.screen, SwitchScreen)
            assert {item.provider for item in app.screen.query(AccountItem)} == {"claude", "codex"}
            app.apply_view("claude")
            await pilot.pause()
            await pilot.pause()
            assert {item.provider for item in app.screen.query(AccountItem)} == {"claude"}

    async def test_cursor_never_lands_on_a_divider_when_filtered(self, tmp_path):
        (tmp_path / "settings.json").write_text(json.dumps({"ui": {"view": "codex"}}))
        claude = FakeSwitcher([make_account(1, active=True)], tmp_path)
        codex = FakeSwitcher([make_account(2, active=True)], tmp_path)
        app = make_app(claude, codex)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            await menu_select(pilot, "switch")
            from textual.widgets import ListView
            from claude_swap.tui.widgets import AccountItem

            highlighted = app.screen.query_one("#accounts", ListView).highlighted_child
            assert isinstance(highlighted, AccountItem)
            assert highlighted.provider == "codex"

    async def test_setting_the_view_persists_it(self, tmp_path):
        claude = FakeSwitcher([make_account(1, active=True)], tmp_path)
        codex = FakeSwitcher([make_account(2, active=True)], tmp_path)
        app = make_app(claude, codex)
        async with app.run_test() as pilot:
            await settle(pilot)
            app.apply_view("codex")
            assert json.loads((tmp_path / "settings.json").read_text())["ui"]["view"] == "codex"

    async def test_remove_menu_shows_alias_before_email(self, tmp_path):
        fake = FakeSwitcher(
            [
                make_account(1, active=True, alias="dev"),
                make_account(2, email="plain@example.com"),
            ],
            tmp_path,
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            from textual.widgets import ListView

            from claude_swap.tui.widgets import MenuItem

            await menu_select(pilot, "remove-menu")
            from textual.widgets import Static

            menu = app.screen.query_one("#menu", ListView)
            labels = [
                item.query_one(Static).render().plain for item in menu.query(MenuItem)
            ]
            assert any("dev (user1@example.com)" in label for label in labels)
            assert any("plain@example.com" in label for label in labels)
            assert not any("(plain@example.com)" in label for label in labels)

    async def test_remove_menu_label_renders_bracket_tag_literally(self, tmp_path):
        # The remove menu labels each account with `[{display_tag}]`, and an
        # org name of "red" makes that literally "[red]" — a valid Rich
        # color markup tag. MenuItem must render it as text, not consume it
        # as styling (which would silently drop the tag from the label).
        fake = FakeSwitcher(
            [dataclasses.replace(make_account(1, active=True), org_name="red")],
            tmp_path,
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            from textual.widgets import ListView, Static

            from claude_swap.tui.widgets import MenuItem

            await menu_select(pilot, "remove-menu")
            menu = app.screen.query_one("#menu", ListView)
            labels = [
                item.query_one(Static).render().plain for item in menu.query(MenuItem)
            ]
            assert any("[red]" in label for label in labels)

    async def test_back_menu_entry_pops_submenu(self, tmp_path):
        fake = FakeSwitcher([make_account(1, active=True)], tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            from textual.widgets import ListView

            from claude_swap.tui.widgets import MenuItem

            await menu_select(pilot, "add-menu")
            await menu_select(pilot, "back")
            menu = app.screen.query_one("#menu", ListView)
            ids = [item.action_id for item in menu.query(MenuItem)]
            assert ids[0] == "switch"

    async def test_both_sections_render_at_once_with_overlapping_slots(self, tmp_path):
        claude = FakeSwitcher(
            [
                make_account(1, active=True, email="claude-active@example.com"),
                make_account(2, email="claude-mini@example.com"),
            ],
            tmp_path,
        )
        codex = FakeSwitcher(
            [
                make_account(1, active=True, email="codex-active@example.com"),
                make_account(2, email="codex-mini@example.com"),
            ],
            tmp_path,
        )
        app = make_app(claude, codex)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            from claude_swap.tui.widgets import AccountsPanel

            panel = app.screen.query_one(AccountsPanel).render().plain
            assert panel.index("Claude Code") < panel.index("Codex")
            claude_section, codex_section = panel.split("Codex", 1)
            assert "claude-active@example.com" in claude_section
            assert "claude-mini@example.com" in claude_section
            assert "codex-active@example.com" not in claude_section
            assert "codex-active@example.com" in codex_section
            assert "codex-mini@example.com" in codex_section
            assert "claude-active@example.com" not in codex_section

            # In each provider section the active overlapping slot 1 is the
            # expanded card (marker + bar); slot 2 is the bar-free mini row.
            for section, active_email, mini_email in (
                (claude_section, "claude-active@example.com", "claude-mini@example.com"),
                (codex_section, "codex-active@example.com", "codex-mini@example.com"),
            ):
                active_part, mini_part = section.split(mini_email, 1)
                active_part = active_part.split(active_email, 1)[1]
                assert "● active" in active_part and "━" in active_part
                assert "● active" not in mini_part and "━" not in mini_part

    async def test_dashboard_collects_both_provider_sources_without_mode_toggle(
        self, tmp_path
    ):
        """Rewrite of the removed provider-menu test: its source-selection
        intent is now stronger because both injected sources stay live."""
        claude = FakeSwitcher(
            [make_account(1, active=True, email="claude@example.com")], tmp_path
        )
        codex = FakeSwitcher(
            [make_account(7, active=True, email="codex@example.com")], tmp_path
        )
        app = make_app(claude, codex)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            assert snap_of(app, "claude").accounts[0].email == "claude@example.com"
            assert snap_of(app, "codex").accounts[0].email == "codex@example.com"
            assert claude.fetch_sets == [None]
            assert codex.fetch_sets == [None]

    async def test_switch_routes_to_the_right_provider(self, tmp_path):
        claude = FakeSwitcher(
            [make_account(1, active=True), make_account(2)], tmp_path
        )
        codex = FakeSwitcher(
            [make_account(1, active=True), make_account(2)], tmp_path
        )
        app = make_app(claude, codex)
        async with app.run_test(size=(100, 48)) as pilot:
            await settle(pilot)
            await pilot.press("s")
            await pilot.pause()
            from textual.widgets import ListView

            from claude_swap.tui.widgets import AccountItem

            listview = app.screen.query_one("#accounts", ListView)
            items = list(listview.query(AccountItem))
            assert [(item.provider, item.number) for item in items] == [
                ("claude", "1"),
                ("claude", "2"),
                ("codex", "1"),
                ("codex", "2"),
            ]
            listview.index = next(
                index
                for index, item in enumerate(listview.children)
                if isinstance(item, AccountItem)
                and (item.provider, item.number) == ("codex", "2")
            )
            await pilot.press("enter")
            await settle(pilot)
            assert codex.calls == [("switch_to", "2")]
            assert claude.calls == []

    async def test_switch_screen_groups_rows_under_provider_dividers(self, tmp_path):
        claude = FakeSwitcher([make_account(1, active=True), make_account(2)], tmp_path)
        codex = FakeSwitcher([make_account(1, active=True), make_account(2)], tmp_path)
        app = make_app(claude, codex)
        async with app.run_test(size=(100, 48)) as pilot:
            await settle(pilot)
            await pilot.press("s")
            await pilot.pause()
            from textual.widgets import ListView

            from claude_swap.tui.widgets import AccountItem, ProviderDivider

            listview = app.screen.query_one("#accounts", ListView)
            children = list(listview.children)
            dividers = [
                (index, item.provider)
                for index, item in enumerate(children)
                if isinstance(item, ProviderDivider)
            ]
            assert [provider for _index, provider in dividers] == ["claude", "codex"]
            for position, (index, provider) in enumerate(dividers):
                next_divider = (
                    dividers[position + 1][0]
                    if position + 1 < len(dividers)
                    else len(children)
                )
                assert [
                    item.provider
                    for item in children[index + 1 : next_divider]
                    if isinstance(item, AccountItem)
                ] == [provider, provider]

    async def test_account_cursor_wrap_skips_provider_dividers(self, tmp_path):
        claude = FakeSwitcher([make_account(1, active=True)], tmp_path)
        codex = FakeSwitcher([make_account(1, active=True)], tmp_path)
        app = make_app(claude, codex)
        async with app.run_test(size=(100, 48)) as pilot:
            await settle(pilot)
            await pilot.press("s")
            await pilot.pause()
            from textual.widgets import ListView

            from claude_swap.tui.widgets import AccountItem

            listview = app.screen.query_one("#accounts", ListView)
            listview.index = next(
                index
                for index, item in enumerate(listview.children)
                if isinstance(item, AccountItem)
            )
            await pilot.press("up")
            assert isinstance(listview.children[listview.index], AccountItem)
            await pilot.press("down")
            assert isinstance(listview.children[listview.index], AccountItem)

    async def test_switch_first_build_places_cursor_on_active_account_row(self, tmp_path):
        claude = FakeSwitcher([make_account(1)], tmp_path)
        codex = FakeSwitcher([make_account(7, active=True)], tmp_path)
        app = make_app(claude, codex)
        async with app.run_test(size=(100, 48)) as pilot:
            await settle(pilot)
            await pilot.press("s")
            await pilot.pause()
            from textual.widgets import ListView

            from claude_swap.tui.widgets import AccountItem

            listview = app.screen.query_one("#accounts", ListView)
            item = listview.children[listview.index]
            assert isinstance(item, AccountItem)
            assert (item.provider, item.number) == ("codex", "7")

    async def test_empty_provider_contributes_no_list_divider(self, tmp_path):
        app = make_app(FakeSwitcher([make_account(1, active=True)], tmp_path))
        async with app.run_test(size=(100, 48)) as pilot:
            await settle(pilot)
            await pilot.press("s")
            await pilot.pause()
            from textual.widgets import ListView

            from claude_swap.tui.widgets import ProviderDivider

            listview = app.screen.query_one("#accounts", ListView)
            assert [
                item.provider
                for item in listview.children
                if isinstance(item, ProviderDivider)
            ] == ["claude"]

    async def test_same_email_in_both_providers_stays_two_rows(self, tmp_path):
        shared = "same@example.com"
        claude_acc = dataclasses.replace(
            make_account(
                1,
                active=True,
                email=shared,
                entry=make_usage_at(100.0, pct=11.0),
            ),
            org_name="Claude Org",
            alias="claude-alias",
        )
        codex_acc = dataclasses.replace(
            make_account(
                1,
                active=True,
                email=shared,
                entry=make_usage_at(200.0, pct=77.0),
            ),
            org_name="Codex Org",
            alias="codex-alias",
        )
        app = make_app(
            FakeSwitcher([claude_acc], tmp_path),
            FakeSwitcher([codex_acc], tmp_path),
        )
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            # Exercise the older-normal-after-newer-store merge branch with
            # an identity shared across providers. Only Claude's canonical
            # usage may advance; Codex must retain its independent row.
            claude_current = snap_of(app, "claude")
            claude_advanced = dataclasses.replace(
                claude_current,
                accounts=(
                    dataclasses.replace(
                        claude_current.accounts[0], usage=make_usage_at(300.0, pct=33.0)
                    ),
                ),
            )
            app._apply_snapshot(
                "claude",
                app._applied_generation["claude"] - 1,
                "normal",
                claude_advanced,
            )
            await pilot.pause()

            claude_row = snap_of(app, "claude").accounts[0]
            codex_row = snap_of(app, "codex").accounts[0]
            assert (claude_row.usage.fetched_at, codex_row.usage.fetched_at) == (
                300.0,
                200.0,
            )
            assert (
                claude_row.org_uuid,
                claude_row.org_name,
                claude_row.alias,
                claude_row.usage.last_good["five_hour"]["pct"],
            ) == ("", "Claude Org", "claude-alias", 33.0)
            assert (
                codex_row.org_uuid,
                codex_row.org_name,
                codex_row.alias,
                codex_row.usage.last_good["five_hour"]["pct"],
            ) == ("", "Codex Org", "codex-alias", 77.0)
            from claude_swap.tui.widgets import AccountsPanel

            panel = app.screen.query_one(AccountsPanel).render().plain
            claude_section, codex_section = panel.split("Codex", 1)
            assert (
                claude_section.count(shared) == 1
                and "claude-alias" in claude_section
                and "Claude Org" in claude_section
                and "33%" in claude_section
            )
            assert (
                codex_section.count(shared) == 1
                and "codex-alias" in codex_section
                and "Codex Org" in codex_section
                and "77%" in codex_section
            )

    async def test_codex_config_error_does_not_blank_claude(
        self, tmp_path, monkeypatch
    ):
        claude = FakeSwitcher(
            [make_account(1, active=True, email="safe@example.com")], tmp_path
        )
        codex = BlockingErrorSnapshotSwitcher([], tmp_path)
        app = make_app(claude, codex)
        async with app.run_test(size=(100, 32)) as pilot:
            await wait_event(codex.started)
            await pilot.pause()
            from claude_swap.tui.dashboard import DashboardScreen
            for _ in range(20):
                if snap_of(app, "claude") is not None:
                    break
                await pilot.pause()
            from claude_swap.tui.widgets import AccountsPanel

            widget = app.screen.query_one(AccountsPanel)
            refresh_errors: list[str] = []
            original_refresh = widget.refresh

            def tracked_refresh(*args, **kwargs):
                refresh_errors.append(app._last_refresh_error["codex"])
                return original_refresh(*args, **kwargs)

            monkeypatch.setattr(widget, "refresh", tracked_refresh)
            codex.release.set()
            for _ in range(20):
                if app._last_refresh_error["codex"]:
                    break
                await pilot.pause()
            await pilot.pause()

            assert isinstance(app.screen, DashboardScreen) and widget.is_mounted
            assert refresh_errors == ["malformed Codex sequence.json"]
            mounted_panel = widget.render().plain
            assert "safe@example.com" in mounted_panel
            assert "Codex" in mounted_panel
            assert "malformed Codex sequence.json" in mounted_panel
            assert mounted_panel.rsplit("Codex", 1)[1].strip() != "loading…"

    async def test_zero_codex_accounts_keeps_claude_and_hides_empty_section(
        self, tmp_path
    ):
        app = make_app(
            FakeSwitcher(
                [make_account(1, active=True, email="only-claude@example.com")],
                tmp_path,
            )
        )
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            from claude_swap.tui.widgets import AccountsPanel

            panel = app.screen.query_one(AccountsPanel).render().plain
            assert "only-claude@example.com" in panel
            assert "Claude Code" in panel
            assert "Codex" not in panel

    async def test_second_action_while_busy_is_refused(self, tmp_path):
        claude = BlockingActionSwitcher(
            [make_account(1, active=True), make_account(2)], tmp_path
        )
        codex = FakeSwitcher(
            [make_account(1, active=True), make_account(2)], tmp_path
        )
        app = make_app(claude, codex)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            app.do_switch("claude", "2")
            await wait_event(claude.switch_started)
            assert app.busy is True
            app.do_switch("codex", "2")
            assert codex.calls == []
            claude.switch_release.set()
            await settle(pilot)
            assert claude.calls == [("switch_to", "2")]
            assert codex.calls == []

    async def test_vim_keys_move_menu_cursor(self, tmp_path):
        fake = FakeSwitcher([make_account(1, active=True)], tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            from textual.widgets import ListView

            menu = app.screen.query_one("#menu", ListView)
            assert menu.index == 0
            await pilot.press("j")
            assert menu.index == 1
            await pilot.press("k")
            assert menu.index == 0

    async def test_menu_cursor_wraps_in_both_directions(self, tmp_path):
        fake = FakeSwitcher([make_account(1, active=True)], tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            from textual.widgets import ListView

            menu = app.screen.query_one("#menu", ListView)
            last = len(menu.children) - 1
            assert menu.index == 0
            await pilot.press("up")
            assert menu.index == last
            await pilot.press("down")
            assert menu.index == 0
            await pilot.press("k")
            assert menu.index == last
            await pilot.press("j")
            assert menu.index == 0

    async def test_s_opens_switch_screen_and_enter_switches(self, tmp_path):
        fake = FakeSwitcher(
            [make_account(1, active=True), make_account(2)], tmp_path
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            await pilot.press("s")
            await pilot.pause()
            from textual.widgets import ListView

            from claude_swap.tui.dashboard import DashboardScreen, SwitchScreen
            from claude_swap.tui.widgets import AccountItem

            assert isinstance(app.screen, SwitchScreen)
            listview = app.screen.query_one("#accounts", ListView)
            items = list(listview.query(AccountItem))
            assert [item.number for item in items] == ["1", "2"]
            assert isinstance(listview.children[listview.index], AccountItem)
            assert listview.children[listview.index].number == "1"  # active account
            await pilot.press("down", "enter")
            await settle(pilot)
            assert ("switch_to", "2") in fake.calls
            assert isinstance(app.screen, DashboardScreen)  # popped back
            assert snap_of(app).active_number == "2"

    async def test_switch_screen_escape_backs_out(self, tmp_path):
        fake = FakeSwitcher(
            [make_account(1, active=True), make_account(2)], tmp_path
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            await pilot.press("enter")  # menu: Switch account…
            await pilot.pause()
            from claude_swap.tui.dashboard import DashboardScreen, SwitchScreen

            assert isinstance(app.screen, SwitchScreen)
            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(app.screen, DashboardScreen)
            assert not any(call[0] == "switch_to" for call in fake.calls)

    async def test_remove_via_menu_confirms_then_removes(self, tmp_path):
        fake = FakeSwitcher(
            [make_account(1, active=True), make_account(2)], tmp_path
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            await menu_select(pilot, "remove-menu")
            await menu_select(pilot, "remove:claude:2")
            from claude_swap.tui.modals import ConfirmModal

            assert isinstance(app.screen, ConfirmModal)
            await pilot.press("y")
            await settle(pilot)
            assert ("remove", "2", True) in fake.calls

    async def test_remove_via_menu_cancel_is_safe(self, tmp_path):
        fake = FakeSwitcher(
            [make_account(1, active=True), make_account(2)], tmp_path
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            await menu_select(pilot, "remove-menu")
            await menu_select(pilot, "remove:claude:1")
            await pilot.press("n")
            await settle(pilot)
            assert not any(call[0] == "remove" for call in fake.calls)

    async def test_disable_via_menu_toggles_without_confirm(self, tmp_path):
        fake = FakeSwitcher(
            [make_account(1, active=True), make_account(2)], tmp_path
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            await menu_select(pilot, "disable-menu")
            await menu_select(pilot, "disable:claude:2")  # no modal — direct action
            await settle(pilot)
            assert ("set_disabled", "2", True) in fake.calls
            # the submenu pops back to root after the toggle
            from textual.widgets import ListView

            from claude_swap.tui.widgets import MenuItem

            menu = app.screen.query_one("#menu", ListView)
            ids = [item.action_id for item in menu.query(MenuItem)]
            assert ids[0] == "switch"

    async def test_disable_menu_row_reflects_state_and_re_enables(self, tmp_path):
        fake = FakeSwitcher(
            [make_account(1, active=True), make_account(2, disabled=True)],
            tmp_path,
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            await menu_select(pilot, "disable-menu")
            from textual.widgets import ListView, Static

            from claude_swap.tui.widgets import MenuItem

            menu = app.screen.query_one("#menu", ListView)
            labels = [
                item.query_one(Static).render().plain for item in menu.query(MenuItem)
            ]
            # the already-disabled account offers to enable; the active one to disable
            assert any("(disabled)" in label and "enable" in label for label in labels)
            assert any("disable" in label and "(disabled)" not in label for label in labels)
            # selecting the disabled account flips it back on
            await menu_select(pilot, "disable:claude:2")
            await settle(pilot)
            assert ("set_disabled", "2", False) in fake.calls

    async def test_modal_arrow_keys_choose_button(self, tmp_path):
        fake = FakeSwitcher(
            [make_account(1, active=True), make_account(2)], tmp_path
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            await menu_select(pilot, "remove-menu")
            await menu_select(pilot, "remove:claude:2")  # → confirm modal
            # focus starts on the confirm button; → moves to Cancel, enter presses it
            await pilot.press("right", "enter")
            await settle(pilot)
            assert not any(call[0] == "remove" for call in fake.calls)
            # reopen (menu index still on account 2), ← back to confirm, press it
            await pilot.press("enter")
            await pilot.pause()
            await pilot.press("right", "left", "enter")
            await settle(pilot)
            assert ("remove", "2", True) in fake.calls

    async def test_full_refresh_binding(self, tmp_path):
        fake = FakeSwitcher([make_account(1, active=True)], tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            await pilot.press("f")
            await settle(pilot)
            assert fake.fetch_sets[-1] is None  # full on-demand pass

    async def test_add_token_via_menu_passes_assume_yes(self, tmp_path):
        fake = FakeSwitcher([make_account(1, active=True)], tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            await menu_select(pilot, "add-menu")
            await menu_select(pilot, "add-token")
            from textual.widgets import Input

            app.screen.query_one("#token", Input).value = "sk-ant-oat01-test"
            app.screen.query_one("#slot", Input).value = "5"
            await pilot.click("#add")
            await settle(pilot)
            assert ("add_token", "sk-ant-oat01-test", None, 5, True) in fake.calls

    async def test_add_token_occupied_slot_asks_first(self, tmp_path):
        fake = FakeSwitcher(
            [make_account(1, active=True), make_account(2)], tmp_path
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            await menu_select(pilot, "add-menu")
            await menu_select(pilot, "add-token")
            from textual.widgets import Input

            app.screen.query_one("#token", Input).value = "sk-ant-oat01-test"
            app.screen.query_one("#slot", Input).value = "2"
            await pilot.click("#add")
            await pilot.pause()
            from claude_swap.tui.modals import ConfirmModal

            assert isinstance(app.screen, ConfirmModal)  # overwrite confirm
            await pilot.press("n")
            await settle(pilot)
            assert not any(call[0] == "add_token" for call in fake.calls)

    async def test_empty_state_hint_in_panel(self, tmp_path):
        fake = FakeSwitcher([], tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            from claude_swap.tui.widgets import AccountsPanel

            panel = app.screen.query_one(AccountsPanel).render().plain
            assert "No managed accounts yet" in panel

    async def test_palette_is_disabled(self, tmp_path):
        from claude_swap.tui.app import CswapApp

        assert CswapApp.ENABLE_COMMAND_PALETTE is False


@pytest.mark.asyncio
class TestWatchScreen:
    def _fake(self, tmp_path):
        return FakeSwitcher(
            [make_account(1, active=True), make_account(2)], tmp_path
        )

    async def test_w_opens_monitor_without_cursor(self, tmp_path):
        app = make_app(self._fake(tmp_path))
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            await pilot.press("w")
            await pilot.pause()
            from textual.widgets import ListView

            from claude_swap.tui.dashboard import WatchScreen
            from claude_swap.tui.widgets import AccountItem

            assert isinstance(app.screen, WatchScreen)
            listview = app.screen.query_one("#accounts", ListView)
            assert len(list(listview.query(AccountItem))) == 2  # full cards
            assert listview.index is None  # monitor mode: no cursor
            await pilot.press("enter")  # inert while just watching
            await settle(pilot)
            assert not any(call[0] == "switch_to" for call in fake_calls(app))

    async def test_watch_screen_groups_rows_under_provider_dividers(self, tmp_path):
        claude = FakeSwitcher([make_account(1, active=True), make_account(2)], tmp_path)
        codex = FakeSwitcher([make_account(1, active=True), make_account(2)], tmp_path)
        app = make_app(claude, codex)
        async with app.run_test(size=(100, 48)) as pilot:
            await settle(pilot)
            await pilot.press("w")
            await pilot.pause()
            from textual.widgets import ListView

            from claude_swap.tui.widgets import AccountItem, ProviderDivider

            listview = app.screen.query_one("#accounts", ListView)
            children = list(listview.children)
            dividers = [
                (index, item.provider)
                for index, item in enumerate(children)
                if isinstance(item, ProviderDivider)
            ]
            assert [provider for _index, provider in dividers] == ["claude", "codex"]
            for position, (index, provider) in enumerate(dividers):
                next_divider = (
                    dividers[position + 1][0]
                    if position + 1 < len(dividers)
                    else len(children)
                )
                assert [
                    item.provider
                    for item in children[index + 1 : next_divider]
                    if isinstance(item, AccountItem)
                ] == [provider, provider]

    async def test_filtered_watch_does_not_select_by_combined_indices(self, tmp_path):
        (tmp_path / "settings.json").write_text(json.dumps({"ui": {"view": "codex"}}))
        claude = FakeSwitcher(
            [make_account(1), make_account(2), make_account(3)], tmp_path
        )
        codex = FakeSwitcher(
            [make_account(10, active=True), make_account(11)], tmp_path
        )
        app = make_app(claude, codex)
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            await pilot.press("w", "s")
            await pilot.pause()
            from textual.widgets import ListView

            from claude_swap.tui.widgets import AccountItem

            highlighted = app.screen.query_one("#accounts", ListView).highlighted_child
            assert isinstance(highlighted, AccountItem)
            assert (highlighted.provider, highlighted.number) == ("codex", "10")

    async def test_s_arms_selection_switch_stays_watching(self, tmp_path):
        fake = self._fake(tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            await pilot.press("w")
            await pilot.pause()
            await pilot.press("s")
            await pilot.pause()
            from textual.widgets import ListView

            from claude_swap.tui.dashboard import WatchScreen
            from claude_swap.tui.widgets import AccountItem

            listview = app.screen.query_one("#accounts", ListView)
            assert isinstance(listview.children[listview.index], AccountItem)
            assert listview.children[listview.index].number == "1"  # active account
            await pilot.press("down", "enter")
            await settle(pilot)
            assert ("switch_to", "2") in fake.calls
            assert isinstance(app.screen, WatchScreen)  # stayed watching
            assert app.screen.query_one("#accounts", ListView).index is None
            assert snap_of(app).active_number == "2"

    async def test_escape_disarms_then_leaves(self, tmp_path):
        fake = self._fake(tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            await pilot.press("w")
            await pilot.pause()
            await pilot.press("s")
            await pilot.pause()
            await pilot.press("escape")  # disarm selection only
            await pilot.pause()
            from textual.widgets import ListView

            from claude_swap.tui.dashboard import DashboardScreen, WatchScreen

            assert isinstance(app.screen, WatchScreen)
            assert app.screen.query_one("#accounts", ListView).index is None
            await pilot.press("escape")  # now leave
            await pilot.pause()
            assert isinstance(app.screen, DashboardScreen)
            assert not any(call[0] == "switch_to" for call in fake.calls)

    async def test_menu_watch_entry_opens_it(self, tmp_path):
        app = make_app(self._fake(tmp_path))
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            await menu_select(pilot, "watch")
            from claude_swap.tui.dashboard import WatchScreen

            assert isinstance(app.screen, WatchScreen)

    async def test_app_start_watch_stacks_over_dashboard(self, tmp_path):
        app = make_app(self._fake(tmp_path), start="watch")
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            from claude_swap.tui.dashboard import DashboardScreen, WatchScreen

            assert isinstance(app.screen, WatchScreen)
            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(app.screen, DashboardScreen)

    async def test_blocked_normal_allows_store_only_repaint_without_stale_overpaint(
        self, tmp_path
    ):
        normal = make_account(1, active=True, entry=make_usage_at(100.0, pct=10.0))
        store = make_account(1, active=True, entry=make_usage_at(200.0, pct=80.0))
        fake = BlockingSnapshotSwitcher(normal, store, tmp_path)
        app = make_app(fake)

        async with app.run_test(size=(100, 40)) as pilot:
            await wait_event(fake.normal_started)
            app._tick()
            await wait_event(fake.store_done)
            await pilot.pause()
            assert snap_of(app).accounts[0].usage.last_good["five_hour"]["pct"] == 80.0

            fake.normal_release.set()
            await wait_event(fake.normal_done)
            await pilot.pause()
            assert snap_of(app).accounts[0].usage.last_good["five_hour"]["pct"] == 80.0
            assert fake.fetch_sets == [None, set()]

    async def test_out_of_order_per_provider_application_stays_isolated(
        self, tmp_path
    ):
        claude_seed = make_account(
            1, active=True, email="claude-seed@example.com", entry=make_usage_at(100.0, 13.0)
        )
        codex_seed = make_account(
            1, active=True, email="codex-seed@example.com", entry=make_usage_at(200.0, 21.0)
        )
        claude = BlockingSnapshotSwitcher(claude_seed, claude_seed, tmp_path)
        codex = BlockingSnapshotSwitcher(codex_seed, codex_seed, tmp_path)
        # Let the mount-time pass seed both current snapshots before starting
        # the deliberately out-of-order overlapping second pass.
        claude.normal_release.set()
        codex.normal_release.set()
        app = make_app(claude, codex)
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            assert snap_of(app, "claude").accounts[0].email == "claude-seed@example.com"
            assert snap_of(app, "codex").accounts[0].email == "codex-seed@example.com"

            claude.normal_account = make_account(
                1,
                active=True,
                email="claude-late@example.com",
                entry=make_usage_at(300.0, 63.0),
            )
            codex.normal_account = make_account(
                1,
                active=True,
                email="codex-first@example.com",
                entry=make_usage_at(400.0, 81.0),
            )
            for fake in (claude, codex):
                fake.normal_started.clear()
                fake.normal_release.clear()
                fake.normal_done.clear()

            app._tick()
            await wait_event(claude.normal_started)
            await wait_event(codex.normal_started)
            codex.normal_release.set()
            await wait_event(codex.normal_done)
            await pilot.pause()
            assert snap_of(app, "codex").accounts[0].email == "codex-first@example.com"
            assert snap_of(app, "claude").accounts[0].email == "claude-seed@example.com"

            claude.normal_release.set()
            await wait_event(claude.normal_done)
            await pilot.pause()
            assert snap_of(app, "claude").accounts[0].email == "claude-late@example.com"
            assert snap_of(app, "claude").accounts[0].usage.last_good["five_hour"]["pct"] == 63.0
            assert snap_of(app, "codex").accounts[0].usage.last_good["five_hour"]["pct"] == 81.0

    async def test_codex_hang_does_not_stall_claude_repaint(self, tmp_path):
        claude_acc = make_account(
            1, active=True, email="repainted@example.com"
        )
        codex_acc = make_account(1, active=True, email="hung@example.com")
        claude = BlockingSnapshotSwitcher(claude_acc, claude_acc, tmp_path)
        codex = BlockingSnapshotSwitcher(codex_acc, codex_acc, tmp_path)
        app = make_app(claude, codex)
        async with app.run_test(size=(100, 40)) as pilot:
            await wait_event(claude.normal_started)
            await wait_event(codex.normal_started)
            claude.normal_release.set()
            await wait_event(claude.normal_done)
            await pilot.pause()
            assert snap_of(app, "claude").accounts[0].email == "repainted@example.com"
            assert snap_of(app, "codex") is None
            codex.normal_release.set()
            await wait_event(codex.normal_done)

    async def test_flash_is_per_provider_and_number(self, tmp_path):
        old = make_usage_at(100.0, pct=20.0)
        claude = FakeSwitcher([make_account(1, active=True, entry=old)], tmp_path)
        codex = FakeSwitcher([make_account(1, active=True, entry=old)], tmp_path)
        app = make_app(claude, codex)
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            await pilot.press("w")
            await pilot.pause()
            current = snap_of(app, "claude")
            advanced = dataclasses.replace(
                current,
                accounts=(
                    dataclasses.replace(
                        current.accounts[0], usage=make_usage_at(200.0, pct=90.0)
                    ),
                ),
            )
            app._apply_snapshot(
                "claude",
                app._applied_generation["claude"] + 1,
                "normal",
                advanced,
            )
            await pilot.pause()
            from claude_swap.tui.widgets import AccountItem

            flashed = {
                (item.provider, item.number): item.has_class("flash")
                for item in app.screen.query(AccountItem)
            }
            assert flashed == {("claude", "1"): True, ("codex", "1"): False}

    async def test_late_normal_can_advance_usage_after_store_repaint(self, tmp_path):
        normal = make_account(1, active=True, entry=make_usage_at(200.0, pct=80.0))
        store = make_account(1, active=True, entry=make_usage_at(100.0, pct=10.0))
        fake = BlockingSnapshotSwitcher(normal, store, tmp_path)
        app = make_app(fake)

        async with app.run_test(size=(100, 40)) as pilot:
            await wait_event(fake.normal_started)
            app._tick()
            await wait_event(fake.store_done)
            await pilot.pause()
            assert snap_of(app).accounts[0].usage.last_good["five_hour"]["pct"] == 10.0

            fake.normal_release.set()
            await wait_event(fake.normal_done)
            await pilot.pause()
            assert snap_of(app).accounts[0].usage.last_good["five_hour"]["pct"] == 80.0

    async def test_repeated_ticks_keep_store_lane_single_flight(self, tmp_path):
        normal = make_account(1, active=True, entry=make_usage_at(100.0, pct=10.0))
        store = make_account(1, active=True, entry=make_usage_at(200.0, pct=80.0))
        fake = BlockingSnapshotSwitcher(normal, store, tmp_path)
        fake.block_store = True
        app = make_app(fake)

        async with app.run_test(size=(100, 40)):
            await wait_event(fake.normal_started)
            app._tick()
            await wait_event(fake.store_started)
            app._tick()
            app._tick()
            assert fake.fetch_sets == [None, set()]
            fake.store_release.set()
            fake.normal_release.set()
            await wait_event(fake.store_done)
            await wait_event(fake.normal_done)

    async def test_store_only_mode_launches_only_store_lane(self, tmp_path):
        fake = self._fake(tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            fake.fetch_sets.clear()
            app.set_store_only("claude", True)
            await settle(pilot)
            assert fake.fetch_sets == [set()]

    async def test_watch_title_shows_snapshot_age_and_long_refresh(self, tmp_path):
        app = make_app(self._fake(tmp_path))
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            await pilot.press("w")
            await pilot.pause()
            from textual.widgets import Static

            title = app.screen.query_one("#list-title", Static)
            # Fresh snapshots stay quiet; the age note is a staleness alarm.
            assert "snapshot" not in title.render().plain
            app.snapshots = {
                **app.snapshots,
                "claude": dataclasses.replace(
                    snap_of(app),
                    taken_at=time.time() - app.SNAPSHOT_AGE_NOTE_S - 1.0,
                ),
            }
            app._update_refresh_status()
            await pilot.pause()
            assert "snapshot 1m ago" in title.render().plain
            app._normal_refreshing["claude"] = True
            app._normal_started_at["claude"] = time.time() - app.POLL_INTERVAL_S - 1.0
            app._update_refresh_status()
            await pilot.pause()
            assert "refreshing" in title.render().plain


def fake_calls(app) -> list[tuple]:
    return app.switcher_for("claude").calls



class _FakeEngine:
    """Stands in for AutoSwitchEngine: records construction, blocks until stop."""

    instances: list["_FakeEngine"] = []

    def __init__(self, switcher, settings, on_event, *, dry_run=False, **kwargs):
        self.switcher = switcher
        self.settings = settings
        self.on_event = on_event
        self.dry_run = dry_run
        self.stopped = False
        self.applied_thresholds: list[float] = []
        self.applied_warmups: list[bool] = []
        self.wakes = 0
        self._stop = threading.Event()
        _FakeEngine.instances.append(self)

    def run_loop(self) -> int:
        self.on_event(NoSwitchEvent(reason="cooldown"))
        self._stop.wait(30)
        return 0

    def stop(self) -> None:
        self.stopped = True
        self._stop.set()

    def apply_threshold(self, threshold: float) -> None:
        self.settings = dataclasses.replace(self.settings, threshold=threshold)
        self.applied_thresholds.append(threshold)

    def apply_warmup_enabled(self, enabled: bool) -> None:
        self.settings = dataclasses.replace(
            self.settings, warmup_five_hour=enabled
        )
        self.applied_warmups.append(enabled)

    def wake(self) -> None:
        self.wakes += 1


@pytest.fixture
def fake_engine(monkeypatch):
    _FakeEngine.instances = []
    monkeypatch.setattr(
        "claude_swap.tui.autoview.AutoSwitchEngine", _FakeEngine
    )
    monkeypatch.setattr(
        "claude_swap.tui.autoview.CodexAutoSwitchEngine", _FakeEngine
    )
    return _FakeEngine


@pytest.mark.asyncio
class TestAutoScreen:
    async def _open(self, pilot):
        await settle(pilot)
        await pilot.press("g")
        await pilot.pause()

    async def test_opens_in_dry_run_and_store_only(self, tmp_path, fake_engine):
        fake = FakeSwitcher(
            [make_account(1, active=True), make_account(2)], tmp_path
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await self._open(pilot)
            from claude_swap.tui.autoview import AutoScreen

            assert isinstance(app.screen, AutoScreen)
            assert len(fake_engine.instances) == 1
            assert fake_engine.instances[0].dry_run is True
            assert app._store_only["claude"] is True
            await settle(pilot)
            # engine event reached the log via call_from_thread
            from textual.widgets import RichLog

            assert len(app.screen.query_one("#event-log", RichLog).lines) > 0

    async def test_auto_view_store_only_is_scoped(self, tmp_path, fake_engine):
        claude = FakeSwitcher(
            [make_account(1, active=True), make_account(2)], tmp_path
        )
        codex = FakeSwitcher(
            [make_account(1, active=True), make_account(2)], tmp_path
        )
        app = make_app(claude, codex)
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            claude.fetch_sets.clear()
            codex.fetch_sets.clear()
            await pilot.press("g")  # hidden binding uses remembered Claude default
            await settle(pilot)
            assert app._store_only == {"claude": True, "codex": False}
            assert claude.fetch_sets and claude.fetch_sets[-1] == set()
            assert codex.fetch_sets and codex.fetch_sets[-1] is None

    async def test_codex_provider_opens_its_auto_switch_engine(
        self, tmp_path, fake_engine
    ):
        claude = FakeSwitcher([make_account(1, active=True)], tmp_path)
        codex = FakeSwitcher([make_account(7, active=True)], tmp_path)
        app = make_app(claude, codex)
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            await menu_select(pilot, "auto")
            await menu_select(pilot, "auto:codex")
            await pilot.pause()

            from claude_swap.tui.autoview import AutoScreen

            assert isinstance(app.screen, AutoScreen)
            assert fake_engine.instances[-1].switcher is codex

    async def test_go_live_requires_confirmation(self, tmp_path, fake_engine):
        fake = FakeSwitcher(
            [make_account(1, active=True), make_account(2)], tmp_path
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await self._open(pilot)
            await pilot.press("l")
            await pilot.pause()
            from claude_swap.tui.modals import ConfirmModal

            assert isinstance(app.screen, ConfirmModal)
            await pilot.press("y")
            await settle(pilot)
            assert len(fake_engine.instances) == 2
            assert fake_engine.instances[0].stopped is True
            assert fake_engine.instances[1].dry_run is False

    async def test_five_hour_warmup_toggle_is_confirmed_and_persisted(
        self, tmp_path, fake_engine
    ):
        fake = FakeSwitcher(
            [make_account(1, active=True), make_account(2)], tmp_path
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await self._open(pilot)
            screen = app.screen

            await pilot.press("w")
            await pilot.pause()
            from claude_swap.tui.modals import ConfirmModal

            assert isinstance(app.screen, ConfirmModal)
            await pilot.press("y")
            await settle(pilot)

            assert screen._settings.warmup_five_hour is True
            assert fake_engine.instances[0].applied_warmups == [True]
            raw = json.loads((tmp_path / "settings.json").read_text())
            assert raw["autoswitch"]["warmupFiveHour"] is True
            from textual.widgets import Static

            summary = screen.query_one("#auto-summary", Static)
            assert "5h warm-up on" in summary.render().plain

            await pilot.press("w")
            await pilot.pause()
            assert screen._settings.warmup_five_hour is False
            assert fake_engine.instances[0].applied_warmups == [True, False]
            raw = json.loads((tmp_path / "settings.json").read_text())
            assert raw["autoswitch"]["warmupFiveHour"] is False

    async def test_live_warmup_confirmation_warns_requests_can_start_now(
        self, tmp_path, fake_engine
    ):
        fake = FakeSwitcher(
            [make_account(1, active=True), make_account(2)], tmp_path
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await self._open(pilot)
            await pilot.press("l")
            await pilot.pause()
            await pilot.press("y")
            await settle(pilot)

            await pilot.press("w")
            await pilot.pause()

            from claude_swap.tui.modals import ConfirmModal

            assert isinstance(app.screen, ConfirmModal)
            assert "Auto is LIVE" in app.screen._message

    async def test_back_stops_engine_and_restores_fetching(
        self, tmp_path, fake_engine
    ):
        fake = FakeSwitcher(
            [make_account(1, active=True), make_account(2)], tmp_path
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await self._open(pilot)
            await pilot.press("escape")
            await settle(pilot)
            from claude_swap.tui.dashboard import DashboardScreen

            assert isinstance(app.screen, DashboardScreen)
            assert fake_engine.instances[0].stopped is True
            assert app._store_only["claude"] is False

    async def test_threshold_adjust_is_session_only(self, tmp_path, fake_engine):
        fake = FakeSwitcher(
            [make_account(1, active=True), make_account(2)], tmp_path
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await self._open(pilot)
            screen = app.screen
            assert app.threshold_pct == 90.0  # mount syncs to the file value
            await pilot.press("right")  # inert outside adjust mode
            await pilot.pause()
            assert screen._settings.threshold == 90.0
            await pilot.press("t", "right", "right", "right")
            await pilot.pause()
            assert screen._settings.threshold == 93.0
            assert app.threshold_pct == 93.0
            engine = fake_engine.instances[0]
            assert engine.applied_thresholds == [91.0, 92.0, 93.0]
            from textual.widgets import Static

            summary = screen.query_one("#auto-summary", Static)
            assert "threshold 93% (session)" in summary.render().plain
            await pilot.press("enter")
            await pilot.pause()
            assert engine.wakes == 1  # one forced tick on leaving the mode
            # the override lives in memory only — nothing was persisted
            assert not (tmp_path / "settings.json").exists()
            # a dry↔live restart rebuilds the engine from the adjusted copy
            await pilot.press("l")
            await pilot.pause()
            await pilot.press("y")
            await settle(pilot)
            assert fake_engine.instances[1].settings.threshold == 93.0
            await pilot.press("escape")
            await settle(pilot)
            # leaving the screen reverts the tick and unpins poll planning
            assert app.threshold_pct == 90.0
            assert fake._poll_inputs_override is None

    async def test_threshold_adjust_escape_exits_mode_not_screen(
        self, tmp_path, fake_engine
    ):
        fake = FakeSwitcher(
            [make_account(1, active=True), make_account(2)], tmp_path
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await self._open(pilot)
            from claude_swap.tui.autoview import AutoScreen

            await pilot.press("t")
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(app.screen, AutoScreen)
            # no net change → no forced tick
            assert fake_engine.instances[0].wakes == 0
            await pilot.press("escape")
            await settle(pilot)
            from claude_swap.tui.dashboard import DashboardScreen

            assert isinstance(app.screen, DashboardScreen)

    async def test_threshold_clamps_and_keeps_meaningful_decimals(
        self, tmp_path, fake_engine
    ):
        import json as _json

        (tmp_path / "settings.json").write_text(_json.dumps({
            "schemaVersion": 1, "autoswitch": {"threshold": 99.0},
        }))
        fake = FakeSwitcher(
            [make_account(1, active=True), make_account(2)], tmp_path
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await self._open(pilot)
            screen = app.screen
            await pilot.press("t", "right", "right")
            await pilot.pause()
            assert screen._settings.threshold == 99.9  # spec's upper bound
            from textual.widgets import Static

            summary = screen.query_one("#auto-summary", Static)
            # never a lying "100%"
            assert "threshold 99.9% (session)" in summary.render().plain
            screen.action_threshold_step(-60.0)
            await pilot.pause()
            assert screen._settings.threshold == 50.0  # spec's lower bound

    async def test_candidates_ranked_by_headroom(self, tmp_path, fake_engine):
        fake = FakeSwitcher(
            [
                make_account(1, active=True, entry=make_entry(91.0, 20.0)),
                make_account(2, entry=make_entry(80.0, 10.0)),
                make_account(3, entry=make_entry(15.0, 5.0)),
            ],
            tmp_path,
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await self._open(pilot)
            await settle(pilot)
            from textual.widgets import Static

            plain = app.screen.query_one("#candidates", Static).render().plain
            assert plain.index("user3@example.com") < plain.index(
                "user2@example.com"
            )

    async def test_candidates_ranking_honors_configured_model(
        self, tmp_path, fake_engine
    ):
        """The 'Next best' ranking must use the same window set as the
        engine: with autoswitch.model set, a Fable-bound account ranks by
        its Fable pct, not its roomy 5h."""
        import json as _json

        (tmp_path / "settings.json").write_text(_json.dumps({
            "schemaVersion": 1, "autoswitch": {"model": "Fable"},
        }))
        fake = FakeSwitcher(
            [
                make_account(1, active=True, entry=make_entry(91.0, 20.0)),
                make_account(
                    2, entry=make_entry(10.0, 5.0, scoped=[("Fable", 95.0)])
                ),
                make_account(
                    3, entry=make_entry(50.0, 5.0, scoped=[("Fable", 20.0)])
                ),
            ],
            tmp_path,
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await self._open(pilot)
            await settle(pilot)
            from textual.widgets import Static

            plain = app.screen.query_one("#candidates", Static).render().plain
            # On 5h alone #2 (10% used) would rank first; Fable 95% binds it
            # below #3 (50% binding).
            assert plain.index("user3@example.com") < plain.index(
                "user2@example.com"
            )


class TestEventText:
    def test_switch_event_styling_and_content(self):
        event = SwitchEvent(
            trigger="proactive",
            from_ref={"number": 1, "email": "a@x.com"},
            to_ref={"number": 2, "email": "b@x.com"},
        )
        from claude_swap.tui.autoview import event_text

        assert event.human() in event_text(event).plain

    def test_event_text_uses_light_accent_for_switch(self):
        from claude_swap.tui.autoview import event_text
        from claude_swap.tui.theme import ACCENT_LIGHT, CSWAP_LIGHT, Palette

        event = SwitchEvent(
            trigger="proactive",
            from_ref={"number": 1, "email": "a@x.com"},
            to_ref={"number": 2, "email": "b@x.com"},
        )
        text = event_text(event, palette=Palette.from_theme(CSWAP_LIGHT))
        assert any(ACCENT_LIGHT in str(s.style) for s in text.spans)

    def test_failed_warmup_uses_warning_style(self):
        from claude_swap.autoswitch import WarmupAutoEvent
        from claude_swap.tui.autoview import event_text
        from claude_swap.tui.theme import Palette

        event = WarmupAutoEvent(
            action="failed",
            number="2",
            email="b@x.com",
            detail="state is unreadable",
        )
        text = event_text(event)

        assert "warm-up failed" in text.plain
        assert any(Palette.DARK.sev_warn in str(span.style) for span in text.spans)

    def test_live_warmup_uses_muted_style(self):
        from claude_swap.autoswitch import WarmupAutoEvent
        from claude_swap.tui.autoview import event_text
        from claude_swap.tui.theme import Palette

        event = WarmupAutoEvent(
            action="live",
            number="2",
            email="b@x.com",
            detail="the five-hour window is already active",
        )
        text = event_text(event)

        assert "five-hour window is already active" in text.plain
        assert "5h warm-up sent" not in text.plain
        assert Palette.DARK.muted in str(text.spans[-1].style)

    def test_warmed_account_uses_accent_and_marks_warmup_sent(self):
        from claude_swap.autoswitch import WarmupAutoEvent
        from claude_swap.tui.autoview import event_text
        from claude_swap.tui.theme import Palette

        event = WarmupAutoEvent(
            action="warmed",
            number="2",
            email="b@x.com",
            detail="sent guarded warm-up prompt",
        )
        text = event_text(event)

        assert text.plain.endswith("sent guarded warm-up prompt (5h warm-up sent)")
        assert Palette.DARK.accent in str(text.spans[-1].style)

    def test_dry_run_warmup_preview_uses_accent(self):
        from claude_swap.autoswitch import WarmupAutoEvent
        from claude_swap.tui.autoview import event_text
        from claude_swap.tui.theme import Palette

        event = WarmupAutoEvent(
            action="would-warm",
            number="2",
            email="b@x.com",
            detail="would send one minimal request",
        )
        text = event_text(event)

        assert "5h warm-up sent" not in text.plain
        assert Palette.DARK.accent in str(text.spans[-1].style)


# ---------------------------------------------------------------------------
# accounts_snapshot on the real switcher
# ---------------------------------------------------------------------------


class TestAccountsSnapshot:
    def test_one_pass_snapshot(self, temp_home, mock_claude_config):
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._init_sequence_file()
        data = switcher._get_sequence_data()
        data["sequence"] = [1, 2]
        data["accounts"] = {
            "1": {"email": "test@example.com", "uuid": "test-uuid-1234"},
            "2": {"email": "other@example.com", "uuid": "uuid-2"},
        }
        switcher._write_json(switcher.sequence_file, data)

        snap = switcher.accounts_snapshot(fetch=set())  # store-only: no network
        assert snap.active_number == "1"
        assert [acc.number for acc in snap.accounts] == ["1", "2"]
        active = snap.accounts[0]
        assert active.is_active and active.email == "test@example.com"
        assert all(acc.kind == "oauth" for acc in snap.accounts)
        # No stored credential backups: nothing is switchable, and usage is
        # sentinel'd rather than fetched.
        assert all(not acc.switchable for acc in snap.accounts)
        assert all(acc.usage.sentinel is not None for acc in snap.accounts)
        assert isinstance(snap.taken_at, float)


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------


class TestBareInvocation:
    def test_bare_tty_launches_tui(self, monkeypatch, temp_home):
        import claude_swap.cli as cli
        import claude_swap.tui as tui

        launched = {}

        def fake_run(switcher):
            launched["switcher"] = switcher
            return 0

        monkeypatch.setattr(sys, "argv", ["cswap"])
        monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
        monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr(tui, "run", fake_run)
        with pytest.raises(SystemExit) as excinfo:
            cli.main()
        assert excinfo.value.code == 0
        assert "switcher" in launched

    def test_bare_non_tty_keeps_usage_error(self, monkeypatch, temp_home):
        import claude_swap.cli as cli

        monkeypatch.setattr(sys, "argv", ["cswap"])
        monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
        monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
        with pytest.raises(SystemExit) as excinfo:
            cli.main()
        assert excinfo.value.code == 2  # argparse usage error

    def test_cswap_watch_opens_tui_on_watch_page(self, monkeypatch, temp_home):
        import claude_swap.cli as cli
        import claude_swap.tui as tui

        launched = {}

        def fake_run(switcher, start="dashboard"):
            launched["start"] = start
            return 0

        monkeypatch.setattr(sys, "argv", ["cswap", "watch"])
        monkeypatch.setattr(tui, "run", fake_run)
        with pytest.raises(SystemExit) as excinfo:
            cli.main()
        assert excinfo.value.code == 0
        assert launched["start"] == "watch"


# ---------------------------------------------------------------------------
# Theme wiring
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestSettingsMenu:
    async def test_threshold_cycles_up_from_an_off_ladder_value(self, tmp_path):
        (tmp_path / "settings.json").write_text(
            json.dumps({"autoswitch": {"threshold": 87.5}})
        )
        fake = FakeSwitcher([make_account(1, active=True)], tmp_path)
        app = make_app(fake)
        async with app.run_test() as pilot:
            await settle(pilot)
            from textual.widgets import ListView, Static
            from claude_swap.tui.widgets import MenuItem

            await menu_select(pilot, "settings-menu")
            menu = app.screen.query_one("#menu", ListView)
            labels = [item.query_one(Static).render().plain for item in menu.query(MenuItem)]
            assert "Auto-switch threshold: 87.5%" in labels
            await menu_select(pilot, "setting:threshold")
            assert app.threshold_pct == 90.0
            assert json.loads((tmp_path / "settings.json").read_text())["autoswitch"]["threshold"] == 90.0

    async def test_threshold_change_repaints_the_bar_tick_immediately(self, tmp_path):
        fake = FakeSwitcher([make_account(1, active=True)], tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            from claude_swap.tui.widgets import AccountsPanel

            panel = app.screen.query_one(AccountsPanel)
            before = panel.render().plain
            with patch.object(panel, "refresh", wraps=panel.refresh) as refresh:
                app.apply_threshold(75.0)
                assert refresh.called
            after = panel.render().plain
            assert after.index("┃") < before.index("┃")

    async def test_cycling_row_stays_on_the_settings_menu(self, tmp_path):
        fake = FakeSwitcher([make_account(1, active=True)], tmp_path)
        app = make_app(fake)
        async with app.run_test() as pilot:
            await settle(pilot)
            from textual.widgets import ListView, Static
            from claude_swap.tui.widgets import MenuItem

            await menu_select(pilot, "settings-menu")
            menu = app.screen.query_one("#menu", ListView)
            menu.index = 1  # Dashboard view: a non-zero settings row.
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert app.screen.query_one("#menu-title", Static).render().plain == "menu › settings"
            assert app._view == "claude"  # Combined → Claude Code
            menu = app.screen.query_one("#menu", ListView)
            assert menu.index == 1
            assert list(menu.query(MenuItem))[menu.index].action_id == "setting:view"
            labels = [
                item.query_one(Static).render().plain
                for item in menu.query(MenuItem)
            ]
            assert "Dashboard view: Claude Code" in labels

            await pilot.press("enter")
            await pilot.pause()
            assert app._view == "codex"  # Combined → Claude Code → Codex
            labels = [
                item.query_one(Static).render().plain
                for item in menu.query(MenuItem)
            ]
            assert "Dashboard view: Codex" in labels


@pytest.mark.asyncio
class TestThemeWiring:
    async def test_mount_selects_light_theme_from_settings(self, tmp_path):
        (tmp_path / "settings.json").write_text(json.dumps({"ui": {"theme": "light"}}))
        fake = FakeSwitcher([make_account("1", active=True)], tmp_path)
        app = make_app(fake)
        async with app.run_test() as pilot:
            await settle(pilot)
            assert app.theme == "cswap-light"

    async def test_auto_setting_uses_detected_light(self, tmp_path):
        (tmp_path / "settings.json").write_text(json.dumps({"ui": {"theme": "auto"}}))
        fake = FakeSwitcher([make_account("1", active=True)], tmp_path)
        app = make_app(fake, detected="light")
        async with app.run_test() as pilot:
            await settle(pilot)
            assert app.theme == "cswap-light"

    async def test_auto_setting_no_detection_falls_back_to_dark(self, tmp_path):
        (tmp_path / "settings.json").write_text(json.dumps({"ui": {"theme": "auto"}}))
        fake = FakeSwitcher([make_account("1", active=True)], tmp_path)
        app = make_app(fake, detected=None)
        async with app.run_test() as pilot:
            await settle(pilot)
            assert app.theme == "cswap-dark"

    async def test_toggle_cycles_dark_light_auto(self, tmp_path):
        (tmp_path / "settings.json").write_text(json.dumps({"ui": {"theme": "dark"}}))
        fake = FakeSwitcher([make_account("1", active=True)], tmp_path)
        app = make_app(fake, detected="light")
        async with app.run_test() as pilot:
            await settle(pilot)
            assert app.theme == "cswap-dark"          # setting dark
            app.action_toggle_theme(); await pilot.pause()
            assert app.theme == "cswap-light"          # → light
            app.action_toggle_theme(); await pilot.pause()
            assert app.theme == "cswap-light"          # → auto, detected=light
            assert json.loads((tmp_path / "settings.json").read_text())["ui"]["theme"] == "auto"
            app.action_toggle_theme(); await pilot.pause()
            assert app.theme == "cswap-dark"           # → back to dark

    async def test_theme_menu_marks_current_and_applies(self, tmp_path):
        from textual.widgets import ListView, Static

        from claude_swap.tui.widgets import MenuItem

        fake = FakeSwitcher([make_account("1", active=True)], tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            assert app._theme_name == "auto"  # default
            await menu_select(pilot, "settings-menu")
            menu = app.screen.query_one("#menu", ListView)
            labels = [it.query_one(Static).render().plain for it in menu.query(MenuItem)]
            assert "Theme: auto" in labels
            await menu_select(pilot, "setting:theme")  # auto → dark
            await menu_select(pilot, "setting:theme")  # dark → light
            assert app._theme_name == "light"
            assert app.theme == "cswap-light"
            assert json.loads((tmp_path / "settings.json").read_text())["ui"]["theme"] == "light"
