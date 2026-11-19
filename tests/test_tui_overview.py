"""The Figma-style account views: snapshots at three sizes plus Pilot checks.

Snapshots run against fakes for all three providers, a fixed theme, a frozen
clock and UTC, so they never read real accounts or drift with the time of day.
Update them with ``pytest tests/test_tui_overview.py --snapshot-update``.
"""

from __future__ import annotations

import dataclasses
import json
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from ccswap.models import AccountSnapshot
from ccswap.tui import overview
from ccswap.tui.theme import Palette
from ccswap.usage_store import UsageEntry
from tests.test_tui import FakeSwitcher, settle

FROZEN = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc).timestamp()
SIZES = [(132, 40), (100, 30), (80, 24)]


@pytest.fixture(autouse=True)
def _no_claude_plugin_list():
    with patch("ccswap.claude_integration.mod_installed", return_value=False):
        yield


@pytest.fixture
def frozen(monkeypatch):
    """One clock for every view, in UTC."""
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    monkeypatch.setattr(overview, "clock", lambda: FROZEN)
    monkeypatch.setattr("ccswap.tui.app.time", SimpleNamespace(time=lambda: FROZEN))
    yield
    monkeypatch.undo()
    time.tzset()


def _iso(seconds: float) -> str:
    return (
        datetime.fromtimestamp(FROZEN + seconds, tz=timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _entry(**last_good) -> UsageEntry:
    return UsageEntry(last_good=last_good, fetched_at=FROZEN - 5, age_s=5.0)


def _win(pct: float, seconds: float) -> dict:
    return {"pct": pct, "resets_at": _iso(seconds)}


def _acc(number, email, entry, *, active=False, org="", plan=None) -> AccountSnapshot:
    return AccountSnapshot(
        number=str(number),
        email=email,
        org_name=org,
        org_uuid="",
        is_active=active,
        kind="oauth",
        switchable=True,
        usage=entry,
        subscription_plan=plan,
    )


def _app(tmp_path, *, start="dashboard"):
    from ccswap.tui.app import CswapApp

    (tmp_path / "settings.json").write_text(json.dumps({"ui": {"theme": "dark"}}))
    claude = FakeSwitcher(
        [
            _acc(
                1, "john.doe@gmail.com",
                _entry(five_hour=_win(76, 9960), seven_day=_win(59, 3 * 86400)),
                active=True, plan={"plan": {"tier": "default_claude_max_5x"}},
            ),
            _acc(
                2, "john.doe@company.com",
                _entry(five_hour=_win(96, 9360), seven_day=_win(40, 4 * 86400)),
                org="Acme",
            ),
        ],
        tmp_path,
    )
    codex = FakeSwitcher(
        [
            _acc(
                1, "john.doe@gmail.com",
                _entry(weekly=_win(34, 3 * 86400), credits={"has_credits": False}),
                active=True, org="Codex Plus",
            ),
            _acc(2, "john.doe@company.com", _entry(weekly=_win(81, 86400)), org="Codex Team"),
        ],
        tmp_path,
    )
    agy_windows = [
        {"name": f"{group} · {window}", "group": group, "window": window,
         "pct": pct, "resets_at": _iso(reset)}
        for group, window, pct, reset in (
            ("Gemini models", "5h", 4, 16000),
            ("Gemini models", "weekly", 22, 5 * 86400),
            ("Claude & GPT models", "5h", 0, 16000),
            ("Claude & GPT models", "weekly", 8, 5 * 86400),
        )
    ]
    agy = FakeSwitcher(
        [_acc(1, "e.doe@gmail.com", _entry(scoped=agy_windows), active=True, org="Antigravity")],
        tmp_path,
    )
    for fake in (claude, codex, agy):
        fake.accounts_snapshot = _frozen_snapshot(fake)
    return CswapApp(claude, codex_switcher=codex, agy_switcher=agy, start=start)


def _frozen_snapshot(fake):
    original = fake.accounts_snapshot

    def snapshot(fetch=None):
        return dataclasses.replace(original(fetch), taken_at=FROZEN)

    return snapshot


# ---------------------------------------------------------------------------
# snapshots
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("size", SIZES, ids=lambda s: f"{s[0]}x{s[1]}")
def test_overview_snapshot(snap_compare, tmp_path, frozen, size):
    assert snap_compare(_app(tmp_path), terminal_size=size, run_before=settle)


@pytest.mark.parametrize("size", SIZES, ids=lambda s: f"{s[0]}x{s[1]}")
def test_watch_snapshot(snap_compare, tmp_path, frozen, size):
    assert snap_compare(_app(tmp_path, start="watch"), terminal_size=size, run_before=settle)


# ---------------------------------------------------------------------------
# Pilot
# ---------------------------------------------------------------------------


def _panels(app):
    from ccswap.tui.overview import ProviderPanel

    return [p.provider for p in app.screen.query(ProviderPanel)]


@pytest.mark.asyncio
async def test_exactly_one_panel_per_provider_after_back_to_back_snapshots(tmp_path, frozen):
    from ccswap.tui.data import PROVIDERS
    from ccswap.tui.widgets import ProviderDivider

    app = _app(tmp_path)
    async with app.run_test(size=(132, 40)) as pilot:
        await settle(pilot)
        for provider in PROVIDERS:
            snap = app.snapshots[provider]
            for _ in range(2):  # two publications before any repaint
                app._apply_snapshot(provider, app._applied_generation[provider] + 1, "normal", snap)
        await settle(pilot)
        assert _panels(app) == list(PROVIDERS)
        await pilot.press("w")
        await settle(pilot)
        for provider in PROVIDERS:
            snap = app.snapshots[provider]
            app._apply_snapshot(provider, app._applied_generation[provider] + 1, "normal", snap)
            app._apply_snapshot(provider, app._applied_generation[provider] + 1, "normal", snap)
        await settle(pilot)
        dividers = [d.provider for d in app.screen.query(ProviderDivider)]
        assert dividers == list(PROVIDERS)


@pytest.mark.asyncio
async def test_active_account_comes_first_in_its_panel(tmp_path, frozen):
    from ccswap.tui.widgets import AccountsPanel

    app = _app(tmp_path)
    app.switchers["claude"]._accounts = [
        dataclasses.replace(a, is_active=a.number == "2") for a in app.switchers["claude"]._accounts
    ]
    async with app.run_test(size=(132, 40)) as pilot:
        await settle(pilot)
        text = app.screen.query_one(AccountsPanel).render_text().plain
        claude = text.split("Codex", 1)[0]
        assert claude.index("● 2") < claude.index("○ 1")
        assert "ACTIVE" in claude.split("○ 1", 1)[0]


def _drawn(widget) -> str:
    """What the widget actually paints right now (not a fresh render call)."""
    from textual.geometry import Region

    strips = widget.render_lines(Region(0, 0, widget.outer_size.width, widget.outer_size.height))
    return "\n".join(strip.text for strip in strips)


@pytest.mark.asyncio
async def test_resize_follows_breakpoints_without_duplicating(tmp_path, frozen):
    from ccswap.tui.data import PROVIDERS

    app = _app(tmp_path)
    async with app.run_test(size=(132, 40)) as pilot:
        await settle(pilot)
        for size, cls in (((80, 24), "-narrow"), ((132, 40), "-wide"), ((100, 30), "-medium"), ((80, 24), "-narrow")):
            await pilot.resize_terminal(*size)
            await settle(pilot)
            assert app.screen.has_class(cls)
            assert _panels(app) == list(PROVIDERS)
            drawn = _drawn(app.screen.query_one("#panel-claude"))
            # Narrow drops the plan label and the reset clock, never a gauge row.
            assert ("Max 5x" in drawn) is (cls != "-narrow")
            assert ("· 14:46" in drawn) is (cls != "-narrow")
            assert "━" in drawn
            assert all(len(line) <= size[0] for line in drawn.splitlines())


@pytest.mark.asyncio
async def test_tabs_show_the_view_setting_and_number_keys_do_nothing(tmp_path, frozen):
    from ccswap.tui.overview import HeaderBar

    app = _app(tmp_path)
    async with app.run_test(size=(132, 40)) as pilot:
        await settle(pilot)
        for key in "1234":  # no tab keys: nothing changes, nothing is saved
            await pilot.press(key)
        await settle(pilot)
        assert app._view == "combined"
        assert "view" not in json.loads((tmp_path / "settings.json").read_text())["ui"]
        app.apply_view("codex")  # what Settings, the menu and the palette call
        await settle(pilot)
        assert [p.provider for p in app.screen.query("ProviderPanel") if p.display] == ["codex"]
        header = app.screen.query_one(HeaderBar).render()
        codex_at = header.plain.index(" Codex ")  # the tab, padded to a block
        accent = app.current_theme.primary
        assert any(str(span.style).endswith(f"on {accent}") and span.start == codex_at for span in header.spans)


@pytest.mark.asyncio
async def test_footer_lists_only_keys_that_work(tmp_path, frozen):
    from tests.test_tui import footer_after

    app = _app(tmp_path)
    async with app.run_test(size=(132, 40)) as pilot:
        await settle(pilot)
        assert "view" not in await footer_after(pilot)
        await pilot.press("w")
        await settle(pilot)
        labels = await footer_after(pilot)
        assert "Confirm" not in labels  # watch is read-only until s
        await pilot.press("enter")
        await settle(pilot)
        assert not any(c[0] == "switch_to" for c in app.switchers["claude"].calls)


@pytest.mark.asyncio
async def test_tall_panels_follow_the_cursor_and_the_menu_stays_usable(tmp_path, frozen):
    from textual.widgets import ListView

    from ccswap.tui.dashboard import WatchScreen
    from ccswap.tui.widgets import AccountsPanel

    app = _app(tmp_path)
    async with app.run_test(size=(80, 24)) as pilot:
        await settle(pilot)
        panel = app.screen.query_one(AccountsPanel)
        menu = app.screen.query_one("#menu", ListView)
        assert menu.has_focus and menu.region.height >= 3
        assert not panel.can_focus  # no Tab-into-panels focus state
        agy = app.screen.query_one("#panel-agy")
        assert not panel.region.contains_region(agy.region)  # below the fold
        await pilot.press("up")  # from the menu up to the last account: Antigravity
        await settle(pilot)
        assert panel.cursor == ("agy", "1")
        assert panel.scroll_y > 0  # scrolled to keep the cursor in view
        assert menu.has_focus
        await pilot.press("down", "down")  # the last account, then into the menu
        await settle(pilot)
        assert panel.cursor is None and menu.index == 1  # "Watch accounts"
        await pilot.press("enter")
        await settle(pilot)
        assert isinstance(app.screen, WatchScreen)



def test_gauge_glyphs_half_cell_and_threshold_tick():
    palette = Palette.DARK
    assert overview.gauge(25, 10, palette).plain == "━━╸───────"
    assert overview.gauge(None, 4, palette).plain == "────"
    assert overview.gauge(30, 10, palette, tick=90).plain == "━━━──────┃"



@pytest.mark.parametrize(
    "pct, tick, expected",
    [
        (59, 90, "dim"),        # under its gate: the track colour
        (None, 90, "dim"),      # unknown usage never claims a crossing
        (83, 80, "sev_warn"),   # at/over the gate, under 90%
        (80, 80, "sev_warn"),   # exactly at the gate counts as over
        (94, 90, "sev_crit"),   # over the gate and >= 90%
        (90, 75, "sev_crit"),
    ],
)
def test_tick_colour_follows_the_gate(pct, tick, expected):
    from ccswap.tui.theme import CSWAP_LIGHT

    for palette in (Palette.DARK, Palette.from_theme(CSWAP_LIGHT)):
        text = overview.gauge(pct, 20, palette, tick=tick)
        at = text.plain.index("┃")
        styles = [str(s.style) for s in text.spans if s.start <= at < s.end]
        assert styles == [getattr(palette, expected)]
