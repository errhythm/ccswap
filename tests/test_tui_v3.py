"""TUI v3: footer fitting, header tabs, settings sections, the sidebar."""

from __future__ import annotations

import json

import pytest

from tests.test_tui import (  # noqa: F401 - fake_engine is a fixture
    FakeSwitcher,
    _FakeEngine,
    fake_engine,
    make_account,
    make_app,
    settle,
)


@pytest.fixture(autouse=True)
def _no_claude_plugin_list(monkeypatch):
    monkeypatch.setattr("ccswap.claude_integration.mod_installed", lambda: False)
    monkeypatch.setattr("ccswap.claude_integration.is_enabled", lambda key: False)


def _keys(screen) -> tuple[list[str], list]:
    from textual.widgets._footer import FooterKey

    keys = list(screen.query(FooterKey))
    left = [k for k in keys if not k.has_class("-command-palette")]
    return [f"{k.key_display} {k.description}" for k in left], keys


async def _footer(pilot, width):
    await settle(pilot)
    for _ in range(20):
        await pilot.pause()
    labels, keys = _keys(pilot.app.screen)
    palette = next(k for k in keys if k.has_class("-command-palette"))
    for key in keys:  # every key drawn whole, none under the palette key
        assert key.region.width >= len(f"{key.key_display} {key.description}")
        assert key.region.right <= width
        if key is not palette:
            assert key.region.right <= palette.region.x
    return labels


NARROW = {
    "dashboard": ["s Switch accounts", "w Watch", "q Quit", "←→ View", "? Option help", "m Mask off"],
    "switch": ["⏎ Switch", "b Switch (best)", "esc Back", "←→ View", "m Mask off"],
    "watch": ["s Switch", "esc Back", "←→ View", "m Mask off"],
    "watch-selecting": ["⏎ Confirm", "s Switch", "esc Back", "←→ View", "m Mask off"],
    "auto": ["l Go live / dry-run", "t Threshold", "s Strategy", "esc Back", "m Mask off"],
    "auto-adjusting": ["l Go live / dry-run", "t Threshold", "⏎ Done", "esc Back", "m Mask off"],
    "settings": ["←→ Section", "⏎ Edit", "esc Back", "q Quit", "? Option help", "m Mask off"],
    "settings-editing": ["←→ Change", "⏎ Done", "esc Cancel", "q Quit", "? Option help", "m Mask off"],
}
# Where the wide list differs by more than ^t Theme: the section pane exists.
WIDE = {
    "settings": ["← Sections", "⏎ Edit", "esc Back", "q Quit", "? Option help", "m Mask off"],
    "settings-sections": ["↑↓ Section", "⏎ Open", "esc Back", "q Quit", "m Mask off"],
}


async def _goto(pilot, state):
    app = pilot.app
    if state == "switch":
        await pilot.press("s")
    elif state == "watch":
        await pilot.press("w")
    elif state == "watch-selecting":
        await pilot.press("w")
        await settle(pilot)
        await pilot.press("s")
    elif state == "auto":
        app.open_auto("claude")
    elif state == "auto-adjusting":
        app.open_auto("claude")
        await settle(pilot)
        await pilot.press("t")
        await settle(pilot)
        app.pop_screen()  # the popup; the auto view's footer is what stays
    elif state == "settings":
        await pilot.press("comma")
    elif state == "settings-editing":
        await pilot.press("comma")
        await settle(pilot)
        await pilot.press("enter")  # Theme: edit mode
    elif state == "settings-sections":
        await pilot.press("comma")
        await settle(pilot)
        await pilot.press("left")
    await settle(pilot)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state, width",
    [(state, width) for state in NARROW for width in (80, 132)] + [("settings-sections", 132)],
)
async def test_footer_keys_per_screen_never_clip(tmp_path, fake_engine, state, width):
    app = make_app(FakeSwitcher([make_account(1, active=True), make_account(2)], tmp_path))
    async with app.run_test(size=(width, 30)) as pilot:
        await settle(pilot)
        await _goto(pilot, state)
        if state == "auto-adjusting":
            assert app.screen._adjusting
        expected = list(WIDE.get(state, NARROW.get(state, [])) if width >= 100 else NARROW[state])
        if width >= 100:  # ^t Theme comes back before m Mask
            expected.insert(expected.index("m Mask off"), "^t Theme")
        assert await _footer(pilot, width) == expected
        assert app.screen.query_one("CswapFooter").compact is (width < 100)
        for engine in list(_FakeEngine.instances):
            engine.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("state", list(NARROW))
async def test_footer_never_clips_at_72_cols(tmp_path, fake_engine, state):
    app = make_app(FakeSwitcher([make_account(1, active=True), make_account(2)], tmp_path))
    async with app.run_test(size=(72, 30)) as pilot:
        await settle(pilot)
        await _goto(pilot, state)
        labels = await _footer(pilot, 72)  # asserts every key fits left of ^p palette
        assert labels, state
        for engine in list(_FakeEngine.instances):
            engine.stop()


@pytest.mark.asyncio
async def test_narrow_footer_drops_option_help_then_view_before_clipping(tmp_path):
    app = make_app(FakeSwitcher([make_account(1, active=True)], tmp_path))
    async with app.run_test(size=(80, 30)) as pilot:
        labels = await _footer(pilot, 80)
        assert "? Option help" in labels and "←→ View" in labels
        await pilot.resize_terminal(72, 30)  # 9 cells less: option help goes
        labels = await _footer(pilot, 72)
        assert "? Option help" not in labels and "←→ View" in labels
        await pilot.resize_terminal(62, 30)
        labels = await _footer(pilot, 62)
        assert labels == ["s Switch accounts", "w Watch", "q Quit", "m Mask off"]
        await pilot.resize_terminal(132, 30)  # back to today's footer
        labels = await _footer(pilot, 132)
        assert "^t Theme" in labels and "? Option help" in labels
        await pilot.press("ctrl+t")  # hidden on narrow screens, never unbound
        await settle(pilot)
        assert app._theme_name == "dark"


# ---------------------------------------------------------------------------
# header tabs
# ---------------------------------------------------------------------------


def _saved(tmp_path) -> str | None:
    path = tmp_path / "settings.json"
    return path.read_text() if path.exists() else None


@pytest.mark.asyncio
async def test_arrow_keys_step_the_tabs_and_wrap_without_saving(tmp_path):
    (tmp_path / "settings.json").write_text(json.dumps({"ui": {"view": "codex"}}))
    before = _saved(tmp_path)
    codex = FakeSwitcher([make_account(1, active=True)], tmp_path)
    app = make_app(FakeSwitcher([make_account(1, active=True)], tmp_path), codex)
    async with app.run_test(size=(100, 30)) as pilot:
        await settle(pilot)
        assert app._view == "codex"  # startup shows the saved view
        seen = []
        for _ in range(4):
            await pilot.press("right")
            await settle(pilot)
            seen.append(app._view)
        assert seen == ["agy", "combined", "claude", "codex"]  # wraps
        await pilot.press("left")
        await settle(pilot)
        assert app._view == "claude"
        await pilot.press("left", "left")
        await settle(pilot)
        assert app._view == "agy"  # wraps backwards
        shown = [p.provider for p in app.screen.query("ProviderPanel") if p.display]
        assert shown == ["agy"] or shown == []
    assert _saved(tmp_path) == before


@pytest.mark.asyncio
async def test_clicking_a_tab_switches_the_view_without_saving(tmp_path):
    from ccswap.tui.overview import HeaderBar

    app = make_app(FakeSwitcher([make_account(1, active=True)], tmp_path))
    async with app.run_test(size=(132, 30)) as pilot:
        await settle(pilot)
        header = app.screen.query_one(HeaderBar)
        start, end, tab = next(s for s in header._spans if s[2] == "codex")
        await pilot.hover(HeaderBar, offset=(2 + start + 1, 0))  # padding 2
        await pilot.pause()
        assert header._hover == "codex"
        hover = app.current_theme.variables["block-hover-background"]
        styles = [str(s.style) for s in header.render().spans if s.start == start]
        assert any(style.endswith(f"on {hover}") for style in styles)
        await pilot.click(HeaderBar, offset=(2 + start + 1, 0))
        await settle(pilot)
        assert app._view == "codex"
        await pilot.click(HeaderBar, offset=(2 + header._spans[0][0], 0))
        await settle(pilot)
        assert app._view == "combined"
    assert _saved(tmp_path) is None


@pytest.mark.asyncio
async def test_tabs_work_in_locked_watch_and_select_nothing(tmp_path):
    from textual.widgets import ListView

    claude = FakeSwitcher([make_account(1, active=True), make_account(2)], tmp_path)
    codex = FakeSwitcher([make_account(1, active=True)], tmp_path)
    app = make_app(claude, codex, start="watch")
    async with app.run_test(size=(100, 30)) as pilot:
        await settle(pilot)
        assert app.screen.locked
        await pilot.press("right")
        await settle(pilot)
        assert app._view == "claude"
        assert app.screen.locked
        assert app.screen.query_one("#accounts", ListView).index is None
        await pilot.press("right", "enter")
        await settle(pilot)
        assert app._view == "codex"
        assert not any(c[0] == "switch_to" for c in claude.calls + codex.calls)
    assert _saved(tmp_path) is None


@pytest.mark.asyncio
async def test_switch_screen_refilters_and_keeps_a_valid_cursor(tmp_path):
    from textual.widgets import ListView

    from ccswap.tui.widgets import AccountItem

    claude = FakeSwitcher([make_account(1, active=True), make_account(2)], tmp_path)
    codex = FakeSwitcher([make_account(1, active=True), make_account(2), make_account(3)], tmp_path)
    app = make_app(claude, codex)
    async with app.run_test(size=(100, 40)) as pilot:
        await settle(pilot)
        await pilot.press("s")
        await settle(pilot)
        await pilot.press("right")  # combined → Claude Code
        await settle(pilot)
        listview = app.screen.query_one("#accounts", ListView)
        assert {i.provider for i in listview.query(AccountItem)} == {"claude"}
        assert isinstance(listview.highlighted_child, AccountItem)
        await pilot.press("right")  # → Codex
        await settle(pilot)
        assert [(i.provider, i.number) for i in listview.query(AccountItem)] == [
            ("codex", "1"), ("codex", "2"), ("codex", "3"),
        ]
        assert isinstance(listview.highlighted_child, AccountItem)
        await pilot.press("enter", "y")  # Codex asks first
        await settle(pilot)
        assert codex.calls and codex.calls[-1][0] == "switch_to"
    assert _saved(tmp_path) is None


@pytest.mark.asyncio
async def test_settings_and_auto_view_keep_arrows_for_their_values(tmp_path, fake_engine):
    from ccswap.tui.overview import HeaderBar

    app = make_app(FakeSwitcher([make_account(1, active=True)], tmp_path))
    async with app.run_test(size=(132, 30)) as pilot:
        await settle(pilot)
        await pilot.press("comma")
        await settle(pilot)
        assert not hasattr(app.screen, "action_view_tab")
        header = app.screen.query_one(HeaderBar)
        start = next(s[0] for s in header._spans if s[2] == "codex")
        await pilot.click(HeaderBar, offset=(2 + start + 1, 0))
        await settle(pilot)
        assert app._view == "combined"
        await pilot.press("escape")
        await settle(pilot)
        app.open_auto("claude")
        await settle(pilot)
        assert not hasattr(app.screen, "action_view_tab")
        await pilot.press("right")
        await settle(pilot)
        assert app._view == "combined"
        for engine in list(_FakeEngine.instances):
            engine.stop()


# ---------------------------------------------------------------------------
# settings sections
# ---------------------------------------------------------------------------


def _shown_rows(screen) -> list[str]:
    from ccswap.tui.widgets import MenuItem

    return [m.action_id for m in screen.query("#menu").first().query(MenuItem) if m.display]


@pytest.mark.asyncio
async def test_settings_down_walks_every_row_and_opens_the_next_section(tmp_path):
    from textual.widgets import ListView

    from ccswap.tui.settings_screen import SECTIONS

    app = make_app(FakeSwitcher([make_account(1, active=True)], tmp_path))
    async with app.run_test(size=(132, 40)) as pilot:
        await settle(pilot)
        await pilot.press("comma")
        await settle(pilot)
        screen = app.screen
        menu = screen.query_one("#menu", ListView)
        order = [row for _title, rows in SECTIONS for row in rows] + ["back"]
        visited, sections = [], []
        for _ in order:
            visited.append(menu.highlighted_child.action_id)
            sections.append(_shown_rows(screen))
            await pilot.press("down")
            await settle(pilot)
        assert visited == order  # every row, in today's order, then ← back
        assert menu.highlighted_child.action_id == order[0]  # wraps
        for row, shown in zip(visited, sections):
            if row != "back":
                assert row in shown  # the row's own section is on screen
                assert shown == list(next(rows for _t, rows in SECTIONS if row in rows))
        assert sections[-1] == list(SECTIONS[-1][1])  # ← back keeps the last section
        await pilot.press("k")  # up wraps to ← back, drawn in the nav
        await settle(pilot)
        assert menu.highlighted_child.action_id == "back"
        assert "› ← back" in str(screen.query_one("#settings-sections").render())
        await pilot.press("enter")
        await settle(pilot)
        assert type(app.screen).__name__ == "DashboardScreen"


@pytest.mark.asyncio
async def test_settings_rows_show_values_hints_and_config_line(tmp_path):
    (tmp_path / "settings.json").write_text(
        json.dumps({"autoswitch": {"threshold5h": 80.0, "strategy": "consume-first"}})
    )
    app = make_app(FakeSwitcher([make_account(1, active=True)], tmp_path))
    async with app.run_test(size=(132, 40)) as pilot:
        await settle(pilot)
        await pilot.press("comma")
        await settle(pilot)
        screen = app.screen
        rows = {r.action_id: r for r in screen.query("SettingRow")}
        assert rows["setting:threshold5h"].value == "80%"
        await pilot.press(*["down"] * 3)  # Display rows, then Threshold
        await pilot.press("down")  # 5h gate
        await settle(pilot)
        line = str(rows["setting:threshold5h"].query_one("Static").render())
        assert line.startswith("› 5h gate") and "‹ 80% ›" in line and "inherit · 40 … 95" in line
        help_text = str(screen.query_one("#setting-help").render())
        assert help_text.startswith("5h gate\n")
        assert help_text.endswith("$ ccswap config set autoswitch.threshold5h 80")
        assert "Claude Code · Codex" in str(screen.query_one("#section-title").render())
        await pilot.press("down")  # weekly gate inherits: the unset line
        await settle(pilot)
        assert str(screen.query_one("#setting-help").render()).endswith(
            "$ ccswap config unset autoswitch.threshold7d   # inherits 90%"
        )
        await pilot.press(*["up"] * 3)  # Mask account info: an on/off row
        await settle(pilot)
        assert "[  off]" in str(rows["setting:mask"].query_one("Static").render())
        await pilot.press("enter")
        await settle(pilot)
        assert "[● on ]" in str(rows["setting:mask"].query_one("Static").render())
        assert str(screen.query_one("#setting-help").render()).endswith("ccswap config set ui.mask on")


@pytest.mark.asyncio
async def test_settings_section_list_becomes_a_switcher_under_100_cols(tmp_path):
    app = make_app(FakeSwitcher([make_account(1, active=True)], tmp_path))
    async with app.run_test(size=(132, 30)) as pilot:
        await settle(pilot)
        await pilot.press("comma")
        await settle(pilot)
        nav = app.screen.query_one("#settings-sections")
        assert str(nav.render()).splitlines()[0] == "› Display"
        await pilot.resize_terminal(80, 24)
        await settle(pilot)
        line = str(nav.render())
        assert "\n" not in line
        assert line.startswith("Display   Auto-switch   Claude Code")
        assert line.endswith("← back") and len(line) == nav.content_size.width
        assert not app.screen.query_one("#section-title").display or app.screen.has_class("-narrow")
        await pilot.click("#settings-sections", offset=(1 + 10 + 1, 0))  # Auto-switch
        await settle(pilot)
        assert app.screen.query_one("#menu").highlighted_child.action_id == "setting:threshold"


@pytest.mark.asyncio
async def test_settings_view_row_steps_the_saved_view_not_the_tab(tmp_path):
    app = make_app(FakeSwitcher([make_account(1, active=True)], tmp_path))
    async with app.run_test(size=(132, 30)) as pilot:
        await settle(pilot)
        await pilot.press("right", "right")  # tabs: Codex for this session
        await settle(pilot)
        assert app._view == "codex"
        await pilot.press("comma")
        await settle(pilot)
        rows = {r.action_id: r for r in app.screen.query("SettingRow")}
        assert rows["setting:view"].value == "Combined"  # what is saved
        await pilot.press("down", "enter", "right", "enter")
        await settle(pilot)
        assert app._view == "claude" and app._saved_view == "claude"
        assert json.loads((tmp_path / "settings.json").read_text())["ui"]["view"] == "claude"


# ---------------------------------------------------------------------------
# sidebar: Up next + Recent switches
# ---------------------------------------------------------------------------

import time as _time  # noqa: E402
from datetime import datetime, timezone  # noqa: E402

from ccswap.usage_store import UsageEntry  # noqa: E402

_NOW = _time.time()


def _w(pct, reset_s):
    at = datetime.fromtimestamp(_NOW + reset_s, tz=timezone.utc).isoformat()
    return {"pct": pct, "resets_at": at}


def _acc(n, five, week, *, active=False, weekly_key="seven_day", reset=3 * 86400):
    last_good = {"five_hour": _w(five, 3600), weekly_key: _w(week, reset)}
    entry = UsageEntry(last_good=last_good, fetched_at=_NOW, age_s=1.0)
    return make_account(n, active=active, entry=entry)


def _rank(provider, accounts, strategy, tmp_path):
    from ccswap.models import AccountsSnapshot
    from ccswap.tui.sidebar import up_next

    fake = FakeSwitcher(accounts, tmp_path)
    snap = AccountsSnapshot(active_number=fake.active, accounts=tuple(accounts), taken_at=_NOW)
    return up_next(provider, fake, snap, strategy=strategy, models=(), gate_for=lambda _l: 90.0)


@pytest.mark.parametrize("provider, weekly_key", [("claude", "seven_day"), ("codex", "weekly")])
def test_up_next_picks_what_the_switch_strategy_picks(tmp_path, provider, weekly_key):
    from ccswap.switcher import ClaudeAccountSwitcher
    from ccswap.codex import CodexAccountSwitcher

    accounts = [
        _acc(1, 80, 50, active=True, weekly_key=weekly_key, reset=5 * 86400),
        _acc(2, 10, 20, weekly_key=weekly_key, reset=6 * 86400),  # most headroom
        _acc(3, 30, 40, weekly_key=weekly_key, reset=1 * 86400),  # resets soonest
    ]
    best = _rank(provider, accounts, "best", tmp_path)
    consume = _rank(provider, accounts, "consume-first", tmp_path)
    assert (best.active, best.target) == ("1", "2")
    assert (consume.active, consume.target) == ("1", "3")
    assert (best.window, best.pct, best.gate) == ("5h", 80.0, 90.0)  # closest to its gate
    # The same answer the switch command's own selector gives on this data.
    fake = FakeSwitcher(accounts, tmp_path)
    if provider == "claude":
        usage = {a.number: a.usage.decision_value() for a in accounts}
        assert ClaudeAccountSwitcher._select_best_switchable(fake, "1", (), usage)[0] == "2"
        assert ClaudeAccountSwitcher._select_consume_first_switchable(fake, "1", (), usage)[0] == "3"
    else:
        assert CodexAccountSwitcher._select_best(fake, accounts[0], accounts[1:])[0] == "2"
        assert CodexAccountSwitcher._select_consume_first(fake, accounts[0], accounts[1:])[0] == "3"


def test_up_next_says_stays_on_when_the_active_account_is_best(tmp_path):
    from ccswap.tui.sidebar import up_next_text
    from ccswap.tui.theme import Palette

    accounts = [_acc(1, 10, 20, active=True, reset=86400), _acc(2, 60, 70, reset=5 * 86400)]
    for strategy in ("best", "consume-first"):
        row = _rank("claude", accounts, strategy, tmp_path)
        assert row.target is None
        assert up_next_text([row], Palette.DARK).plain == "Claude  stays on #1 · 20% 7d"


def test_up_next_text_and_manual_providers(tmp_path):
    from ccswap.tui.sidebar import UpNext, up_next, up_next_text
    from ccswap.tui.theme import Palette

    agy = up_next("agy", None, None, strategy="best", models=(), gate_for=lambda _l: None)
    assert agy.manual
    row = UpNext("claude", active="1", target="3", window="5h", pct=94.0, gate=90.0)
    assert up_next_text([row, agy], Palette.DARK).plain.splitlines() == [
        "Claude       #1 → #3 at 90% of 5h",
        "Antigravity  manual switching only",
    ]


def test_expected_around_only_with_projection_data(tmp_path):
    from ccswap.tui.sidebar import up_next_text
    from ccswap.tui.theme import Palette

    # 7d 80% two days into the week: list --json projects exhaustion before the reset.
    burning = [_acc(1, 5, 80, active=True, reset=5 * 86400), _acc(2, 5, 10)]
    row = _rank("claude", burning, "best", tmp_path)
    assert row.window == "7d" and row.eta is not None
    assert "expected around" in up_next_text([row], Palette.DARK).plain
    # 7d 30% six days in: on pace, no projectedExhaustionAt, no line.
    calm = [_acc(1, 5, 30, active=True, reset=86400), _acc(2, 5, 10)]
    row = _rank("claude", calm, "best", tmp_path)
    assert row.eta is None
    assert "expected" not in up_next_text([row], Palette.DARK).plain
    # A 5h-bound account never has one (pace is weekly only).
    five = [_acc(1, 85, 20, active=True), _acc(2, 5, 10)]
    assert _rank("claude", five, "best", tmp_path).eta is None


def test_recent_switches_read_the_switch_log(tmp_path):
    from ccswap.tui.sidebar import recent_switches, recent_text, when
    from ccswap.tui.theme import Palette

    log = tmp_path / "claude-swap.log"
    assert recent_switches(log) == []  # no log yet
    lines = [f"2026-11-1{d} 1{d}:0{d}:00,000 - INFO - Switched from account {d} to {d + 1}" for d in range(1, 8)]
    log.write_text("\n".join(lines[:3] + ["2026-11-14 10:00:00,000 - INFO - Updated config file"] + lines[3:]) + "\n")
    rows = recent_switches(log)
    assert [(r.source, r.target) for r in rows] == [("7", "8"), ("6", "7"), ("5", "6"), ("4", "5"), ("3", "4")]
    now = datetime(2026, 11, 17, 20, 0).timestamp()  # local, like the log
    assert when(rows[0].at, now) == "17:07"
    assert when(rows[1].at, now) == "Mon"
    assert when(datetime(2026, 11, 1, 9, 0).timestamp(), now) == "Nov 01"
    text = recent_text(rows[:2], Palette.DARK, now).plain.splitlines()
    assert text == ["17:07  Claude  #7 → #8", "Mon    Claude  #6 → #7"]
    assert recent_text([], Palette.DARK, now).plain == "no switches logged yet"


@pytest.mark.asyncio
async def test_sidebar_shows_at_120_cols_and_hides_below_live(tmp_path):
    (tmp_path / "claude-swap.log").write_text(
        "2026-11-19 11:02:30,000 - INFO - Switched from account 2 to 1\n"
    )
    claude = FakeSwitcher([_acc(1, 50, 20, active=True), _acc(2, 5, 10)], tmp_path)
    app = make_app(claude)
    async with app.run_test(size=(132, 40)) as pilot:
        await settle(pilot)
        sidebar = app.screen.query_one("#sidebar")
        assert sidebar.display and sidebar.region.width == 44
        assert "#1 → #2 at 90% of 5h" in str(app.screen.query_one("#up-next").render())
        assert "#2 → #1" in str(app.screen.query_one("#recent-switches").render())
        app.step_view(-1)  # Antigravity: the log has no switches of its own
        await settle(pilot)
        assert str(app.screen.query_one("#recent-switches").render()) == (
            "only Claude Code switches are logged"
        )
        assert str(app.screen.query_one("#up-next").render()) == "Antigravity  manual switching only"
        app.step_view(1)
        await settle(pilot)
        for width, shown in ((119, False), (120, True), (100, False), (80, False), (125, True)):
            await pilot.resize_terminal(width, 40)
            await settle(pilot)
            assert (sidebar.region.width > 0) is shown, width
            panels = app.screen.query_one("#accounts-panel")
            assert panels.region.right <= (sidebar.region.x if shown else width)
        assert not any(c[0] in ("switch", "switch_to") for c in claude.calls)  # read-only


def test_sidebar_compute_failure_does_not_freeze_it(monkeypatch):
    from ccswap.tui import sidebar

    def boom(*_a, **_k):
        raise ValueError("malformed usage record")

    panel = sidebar.Sidebar.__new__(sidebar.Sidebar)
    panel._computing = True
    monkeypatch.setattr(panel, "_compute_once", boom, raising=False)
    panel._compute(("claude",), {})
    assert panel._computing is False  # the next snapshot can compute again


@pytest.mark.asyncio
async def test_clicks_and_drags_never_select_text(tmp_path):
    from ccswap.tui.app import CswapApp

    assert CswapApp.ALLOW_SELECT is False
    app = make_app(FakeSwitcher([make_account(1, active=True)], tmp_path))
    async with app.run_test(size=(100, 30)) as pilot:
        await settle(pilot)
        await pilot.mouse_down("#accounts-panel", offset=(4, 2))
        await pilot.hover("#accounts-panel", offset=(30, 3))
        await pilot.mouse_up("#accounts-panel", offset=(30, 3))
        await pilot.click("#accounts-panel", offset=(10, 2), times=2)
        await settle(pilot)
        assert not app.screen.selections
        assert app.screen.get_selected_text() is None


# ---------------------------------------------------------------------------
# the dashboard's one cursor
# ---------------------------------------------------------------------------


def _three_providers(tmp_path):
    claude = FakeSwitcher([make_account(1, active=True), make_account(2), make_account(3, disabled=True)], tmp_path)
    codex = FakeSwitcher([make_account(1, active=True), make_account(2)], tmp_path)
    agy = FakeSwitcher([make_account(1, active=True), make_account(2)], tmp_path)
    from ccswap.tui.app import CswapApp

    return CswapApp(claude, codex_switcher=codex, agy_switcher=agy), claude, codex, agy


@pytest.mark.asyncio
async def test_arrows_walk_every_account_row_then_the_menu_and_wrap(tmp_path):
    from textual.widgets import ListView

    app, *_ = _three_providers(tmp_path)
    async with app.run_test(size=(132, 60)) as pilot:
        await settle(pilot)
        panel = app.screen.query_one("#accounts-panel")
        menu = app.screen.query_one("#menu", ListView)
        menu_size = len(menu.children)

        def where():
            return panel.cursor or ("menu", menu.index)

        await pilot.press("down")  # start on the menu's first entry; walk to its end
        seen = [where()]
        for _ in range(menu_size - 1 + 7):
            await pilot.press("down")
            seen.append(where())
        accounts = [("claude", "1"), ("claude", "2"), ("claude", "3"),
                    ("codex", "1"), ("codex", "2"), ("agy", "1"), ("agy", "2")]
        assert seen[menu_size - 1:] == accounts + [("menu", 0)]  # wrapped
        for _ in range(2):
            await pilot.press("up")
        assert where() == ("agy", "1")
        # one highlight at a time: the menu shows none while an account has it
        assert menu.index is None and menu.has_focus
        drawn = app.screen.query_one("#panel-agy").render()
        palette_cursor = __import__("ccswap.tui.theme", fromlist=["Palette"]).Palette.from_theme(
            app.current_theme).cursor
        assert sum(1 for s in drawn.spans if palette_cursor in str(s.style)) >= 1
        assert not any(palette_cursor in str(s.style) for s in app.screen.query_one("#panel-claude").render().spans)


@pytest.mark.asyncio
async def test_enter_on_an_account_switches_like_the_switch_screen(tmp_path):
    app, claude, codex, agy = _three_providers(tmp_path)
    async with app.run_test(size=(132, 60)) as pilot:
        await settle(pilot)
        await pilot.press("up", *["up"] * 6)  # menu 0 → agy 2 → … → claude 1 (active)
        assert app.screen.query_one("#accounts-panel").cursor == ("claude", "1")
        await pilot.press("enter")  # the active row does nothing
        await settle(pilot)
        assert claude.calls == []
        await pilot.press("down", "enter")
        await settle(pilot)
        assert claude.calls == [("switch_to", "2")]  # the Switch screen's do_switch
        # #2 is now active and drawn first; walk to the disabled #3.
        panel = app.screen.query_one("#accounts-panel")
        for _ in range(5):
            if panel.cursor == ("claude", "3"):
                break
            await pilot.press("down")
        await pilot.press("enter")  # disabled: still switches, like `ccswap switch 3`
        await settle(pilot)
        assert claude.calls[-1] == ("switch_to", "3")


@pytest.mark.asyncio
@pytest.mark.parametrize("provider, ups", [("codex", 3), ("agy", 1)])
async def test_codex_and_antigravity_ask_before_switching(tmp_path, provider, ups):
    from ccswap.tui.modals import ConfirmModal

    app, _claude, codex, agy = _three_providers(tmp_path)
    fake = {"codex": codex, "agy": agy}[provider]
    async with app.run_test(size=(132, 60)) as pilot:
        await settle(pilot)
        await pilot.press(*["up"] * ups)
        assert app.screen.query_one("#accounts-panel").cursor == (provider, "2")
        await pilot.press("enter")
        await settle(pilot)
        assert isinstance(app.screen, ConfirmModal) and fake.calls == []
        await pilot.press("n")
        await settle(pilot)
        assert fake.calls == []
        await pilot.press("enter", "y")
        await settle(pilot)
        assert fake.calls == [("switch_to", "2")]


@pytest.mark.asyncio
async def test_one_click_picks_a_row_and_only_a_second_click_switches(tmp_path):
    app, claude, *_ = _three_providers(tmp_path)
    async with app.run_test(size=(132, 60)) as pilot:
        await settle(pilot)
        panel = app.screen.query_one("#panel-claude")
        start = next(s for s, _e, n in panel.rows if n == "2")
        await pilot.click("#panel-claude", offset=(4, 1 + start))
        await settle(pilot)
        assert app.screen.query_one("#accounts-panel").cursor == ("claude", "2")
        assert claude.calls == []
        await pilot.press("up", "down")  # moving away forgets the first click
        await pilot.click("#panel-claude", offset=(4, 1 + start))
        await settle(pilot)
        assert claude.calls == []
        await pilot.click("#panel-claude", offset=(4, 1 + start))
        await settle(pilot)
        assert claude.calls == [("switch_to", "2")]


@pytest.mark.asyncio
async def test_locked_watch_moves_the_cursor_but_never_switches(tmp_path):
    from textual.widgets import ListView

    from ccswap.tui.widgets import AccountItem

    claude = FakeSwitcher([make_account(1, active=True), make_account(2)], tmp_path)
    app = make_app(claude, start="watch")
    async with app.run_test(size=(100, 40)) as pilot:
        await settle(pilot)
        listview = app.screen.query_one("#accounts", ListView)
        assert app.screen.locked and listview.index is None
        await pilot.press("down", "down")
        await settle(pilot)
        assert isinstance(listview.highlighted_child, AccountItem)
        assert listview.highlighted_child.number == "2"
        await pilot.press("enter")
        await settle(pilot)
        await pilot.click(AccountItem)
        await settle(pilot)
        assert claude.calls == [] and app.screen.locked
        await pilot.press("s", "down", "enter")  # armed: Enter switches
        await settle(pilot)
        assert claude.calls == [("switch_to", "2")]


# ---------------------------------------------------------------------------
# settings: two panes, values change only in edit mode
# ---------------------------------------------------------------------------


async def _open_settings(pilot):
    await settle(pilot)
    await pilot.press("comma")
    await settle(pilot)
    return pilot.app.screen


@pytest.mark.asyncio
async def test_left_moves_to_the_section_pane_and_back(tmp_path):
    from textual.widgets import ListView

    app = make_app(FakeSwitcher([make_account(1, active=True)], tmp_path))
    async with app.run_test(size=(132, 40)) as pilot:
        screen = await _open_settings(pilot)
        menu = screen.query_one("#menu", ListView)
        await pilot.press("left")
        await settle(pilot)
        assert screen._pane == "sections" and menu.index is None  # one highlight: the section
        assert _shown_rows(screen) == ["setting:theme", "setting:view", "setting:mask"]
        await pilot.press("down")
        await settle(pilot)
        assert _shown_rows(screen)[0] == "setting:threshold"  # the rows follow the section
        await pilot.press("down", "down")  # wraps
        await settle(pilot)
        assert screen._section == 0
        await pilot.press("up")
        await settle(pilot)
        assert screen._section == 2
        await pilot.press("right")  # into that section's rows
        await settle(pilot)
        assert screen._pane == "rows"
        assert menu.highlighted_child.action_id == "claude:claude.statusline"
        await pilot.press("left", "up", "enter")  # Enter opens a section too
        await settle(pilot)
        assert menu.highlighted_child.action_id == "setting:threshold"
        await pilot.press("left", "left")  # ← on the section list leaves
        await settle(pilot)
        assert type(app.screen).__name__ == "DashboardScreen"
        await pilot.press("comma")
        await settle(pilot)
        await pilot.press("left", "escape")  # so does Esc
        await settle(pilot)
        assert type(app.screen).__name__ == "DashboardScreen"
    assert not (tmp_path / "settings.json").exists()


@pytest.mark.asyncio
async def test_narrow_settings_arrows_switch_sections(tmp_path):
    from textual.widgets import ListView

    app = make_app(FakeSwitcher([make_account(1, active=True)], tmp_path))
    async with app.run_test(size=(80, 24)) as pilot:
        screen = await _open_settings(pilot)
        menu = screen.query_one("#menu", ListView)
        await pilot.press("right")
        await settle(pilot)
        assert menu.highlighted_child.action_id == "setting:threshold"
        await pilot.press("right")
        await settle(pilot)
        assert menu.highlighted_child.action_id == "claude:claude.statusline"
        await pilot.press("left", "left", "left")  # wraps: Claude Code → … → Claude Code
        await settle(pilot)
        assert screen._section == 2 and type(app.screen).__name__ == "SettingsScreen"
    assert not (tmp_path / "settings.json").exists()


@pytest.mark.asyncio
async def test_arrows_never_change_a_value_outside_edit_mode(tmp_path):
    from tests.test_tui import setting_values

    (tmp_path / "settings.json").write_text(json.dumps({"ui": {"theme": "dark"}}))
    before = (tmp_path / "settings.json").read_bytes()
    app = make_app(FakeSwitcher([make_account(1, active=True)], tmp_path))
    async with app.run_test(size=(132, 40)) as pilot:
        screen = await _open_settings(pilot)
        values = setting_values(screen)
        for key in ("right", "left", "right", "down", "right", "down", "right"):
            await pilot.press(key)
        await settle(pilot)
        assert setting_values(screen) == values
    assert (tmp_path / "settings.json").read_bytes() == before


@pytest.mark.asyncio
async def test_enter_edits_a_row_and_esc_restores_it(tmp_path):
    from ccswap.tui.settings_screen import SettingRow
    from tests.test_tui import setting_values

    (tmp_path / "settings.json").write_text(json.dumps({"ui": {"theme": "dark"}}))
    before = (tmp_path / "settings.json").read_bytes()
    app = make_app(FakeSwitcher([make_account(1, active=True)], tmp_path))
    async with app.run_test(size=(132, 40)) as pilot:
        screen = await _open_settings(pilot)
        await pilot.press("down", "down", "down")  # Threshold (unset: 90)
        await pilot.press("enter")
        await settle(pilot)
        row = next(r for r in screen.query(SettingRow) if r.action_id == "setting:threshold")
        assert row.editing and screen._editing == "setting:threshold"
        assert "←→ change · ⏎ done · esc cancel" in str(screen.query_one("#setting-help").render())
        await pilot.press("right", "right")  # 90 → 95 → 75, written as it goes
        await settle(pilot)
        assert setting_values(screen)["setting:threshold"] == "75%"
        assert json.loads((tmp_path / "settings.json").read_text())["autoswitch"]["threshold"] == 75.0
        await pilot.press("escape")  # back to what it was, and no explicit copy of the default
        await settle(pilot)
        assert not row.editing and app.threshold_pct == 90.0
        assert setting_values(screen)["setting:threshold"] == "90%"
        after = json.loads((tmp_path / "settings.json").read_text())
        after.pop("schemaVersion")  # set_setting stamps it on any write
        assert after.get("autoswitch", {}) == {} and after["ui"] == json.loads(before)["ui"]
        assert type(app.screen).__name__ == "SettingsScreen"  # Esc cancelled, didn't leave
        await pilot.press("up", "up", "up", "enter", "right", "enter")  # Theme: dark → light, kept
        await settle(pilot)
        assert app._theme_name == "light" and screen._editing is None
        assert json.loads((tmp_path / "settings.json").read_text())["ui"]["theme"] == "light"
        await pilot.press("enter", "left", "escape")  # light → dark → back to light
        await settle(pilot)
        assert app._theme_name == "light"
        assert json.loads((tmp_path / "settings.json").read_text())["ui"]["theme"] == "light"


@pytest.mark.asyncio
async def test_enter_flips_an_on_off_row_without_edit_mode(tmp_path):
    app = make_app(FakeSwitcher([make_account(1, active=True)], tmp_path))
    async with app.run_test(size=(132, 40)) as pilot:
        screen = await _open_settings(pilot)
        await pilot.press("down", "down")  # Mask account info
        await pilot.press("enter")
        await settle(pilot)
        assert app.mask_accounts and screen._editing is None
        assert json.loads((tmp_path / "settings.json").read_text())["ui"]["mask"] is True
        await pilot.press("right", "left")  # not editing: ← goes to the section pane
        await settle(pilot)
        assert app.mask_accounts and screen._pane == "sections"


# ---------------------------------------------------------------------------
# review round 1: hidden rows, paging, row help
# ---------------------------------------------------------------------------


async def _cursor_on(pilot, target):
    panel = pilot.app.screen.query_one("#accounts-panel")
    for _ in range(20):
        if panel.cursor == target:
            return panel
        await pilot.press("up")
    raise AssertionError(f"cursor never reached {target}")


@pytest.mark.asyncio
@pytest.mark.parametrize("how", ["keys", "click", "settings"])
async def test_a_view_change_moves_the_cursor_off_hidden_rows(tmp_path, how):
    from ccswap.tui.overview import HeaderBar

    app, claude, codex, agy = _three_providers(tmp_path)
    async with app.run_test(size=(132, 60)) as pilot:
        await settle(pilot)
        panel = await _cursor_on(pilot, ("claude", "2"))
        if how == "keys":
            await pilot.press("right", "right")  # → Codex
        elif how == "click":
            header = app.screen.query_one(HeaderBar)
            start = next(s for s, _e, t in header._spans if t == "codex")
            await pilot.click(HeaderBar, offset=(2 + start + 1, 0))
        else:
            app.apply_view("codex")  # what Settings and the palette call
        await settle(pilot)
        assert app._view == "codex"
        assert panel.cursor == ("codex", "1")  # the first shown account row
        await pilot.press("down", "enter")  # Codex #2: asks first
        await settle(pilot)
        await pilot.press("n")
        await settle(pilot)
        assert claude.calls == [] and codex.calls == []


@pytest.mark.asyncio
async def test_a_stale_cursor_or_click_never_switches(tmp_path):
    app, claude, *_ = _three_providers(tmp_path)
    async with app.run_test(size=(132, 60)) as pilot:
        await settle(pilot)
        app.step_view(2)  # Codex only
        await settle(pilot)
        screen = app.screen
        screen._cursor = ("account", "claude", "2")  # stale, as before the fix
        screen.action_select_cursor()
        screen.account_clicked("claude", "2")
        screen.account_clicked("claude", "2")
        await settle(pilot)
        assert claude.calls == []


@pytest.mark.asyncio
async def test_page_keys_scroll_the_panels(tmp_path):
    app, *_ = _three_providers(tmp_path)
    async with app.run_test(size=(80, 24)) as pilot:
        await settle(pilot)
        panel = app.screen.query_one("#accounts-panel")
        assert panel.scroll_y == 0 and panel.max_scroll_y > 0
        await pilot.press("pagedown")
        await settle(pilot)
        assert panel.scroll_y > 0
        await pilot.press("pageup")
        await settle(pilot)
        assert panel.scroll_y == 0
        assert ("pagedown" not in str(k) for k in app.screen.query("FooterKey"))


@pytest.mark.asyncio
async def test_question_mark_on_an_account_row_explains_switching(tmp_path):
    from ccswap.tui.modals import OutputModal
    from ccswap.tui.option_help import option_help

    app, *_ = _three_providers(tmp_path)
    async with app.run_test(size=(132, 60)) as pilot:
        await settle(pilot)
        await _cursor_on(pilot, ("claude", "2"))
        await pilot.press("question_mark")
        await settle(pilot)
        assert isinstance(app.screen, OutputModal)
        assert app.screen._output == option_help("switch")


# ---------------------------------------------------------------------------
# full review (10 items)
# ---------------------------------------------------------------------------


def _palette_labels(app):
    from ccswap.tui.commands import static_commands

    return [c.label for c in static_commands(app)]


@pytest.mark.asyncio
async def test_settings_never_opens_over_an_auto_view(tmp_path, fake_engine):  # 1
    app = make_app(FakeSwitcher([make_account(1, active=True)], tmp_path))
    async with app.run_test(size=(100, 30)) as pilot:
        await settle(pilot)
        assert "Settings" in _palette_labels(app)
        app.open_auto("claude")
        await settle(pilot)
        assert "Settings" not in _palette_labels(app)
        app.open_settings()
        await settle(pilot)
        assert type(app.screen).__name__ == "AutoScreen"
        for engine in list(_FakeEngine.instances):
            engine.stop()


@pytest.mark.asyncio
async def test_settings_is_never_stacked_twice(tmp_path):  # 6
    app = make_app(FakeSwitcher([make_account(1, active=True)], tmp_path))
    async with app.run_test(size=(100, 30)) as pilot:
        await settle(pilot)
        app.open_settings()
        await settle(pilot)
        assert "Settings" not in _palette_labels(app)
        app.open_settings()
        await settle(pilot)
        assert [type(s).__name__ for s in app.screen_stack].count("SettingsScreen") == 1


def _weekly(tmp_path, **autoswitch):
    (tmp_path / "settings.json").write_text(
        json.dumps({"autoswitch": {"weeklyThreshold": 80, **autoswitch}})
    )


@pytest.mark.asyncio
async def test_weekly_ticks_and_up_next_use_weekly_threshold(tmp_path):  # 2
    _weekly(tmp_path)
    claude = FakeSwitcher([_acc(1, 5, 10, active=True)], tmp_path)
    codex = FakeSwitcher(
        [_acc(1, 5, 85, active=True, weekly_key="weekly"), _acc(2, 5, 10, weekly_key="weekly")], tmp_path
    )
    app = make_app(claude, codex)
    async with app.run_test(size=(132, 40)) as pilot:
        await settle(pilot)
        assert app.bar_threshold("Weekly") == 80.0  # Codex wk → the 7d gate → weeklyThreshold
        assert app.bar_threshold("7d") == 80.0 and app.bar_threshold("5h") == 90.0
        codex_line = next(
            line for line in str(app.screen.query_one("#up-next").render()).splitlines()
            if line.startswith("Codex")
        )
        assert codex_line.split(maxsplit=1)[1] == "#1 → #2 at 80% of wk"


@pytest.mark.asyncio
async def test_inherit_labels_show_weekly_threshold(tmp_path):  # 5
    from tests.test_tui import setting_values

    _weekly(tmp_path, model="Fable")
    app = make_app(FakeSwitcher([make_account(1, active=True)], tmp_path))
    async with app.run_test(size=(132, 40)) as pilot:
        screen = await _open_settings(pilot)
        values = setting_values(screen)
        assert values["setting:threshold5h"] == "inherit (90%)"
        assert values["setting:threshold7d"] == "inherit (80%)"
        assert values["setting:fable"] == "inherit (80%)"
        await pilot.press("down", "down", "down", "down", "down")  # Weekly gate
        await settle(pilot)
        assert str(screen.query_one("#setting-help").render()).endswith(
            "ccswap config unset autoswitch.threshold7d   # inherits 80%"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["settings-row", "palette"])
async def test_masking_from_any_path_relabels_an_open_account_menu(tmp_path, path):  # 3
    from tests.test_tui import menu_select

    app = make_app(FakeSwitcher([make_account(1, active=True, email="alice@acme.com")], tmp_path))
    async with app.run_test(size=(100, 30)) as pilot:
        await settle(pilot)
        await menu_select(pilot, "remove-menu")
        await settle(pilot)
        dashboard = app.screen
        if path == "settings-row":
            app.open_settings()
            await settle(pilot)
            await pilot.press("down", "down", "enter", "escape")  # Mask: Enter flips it
        else:
            app.apply_mask(True)  # what the palette's command calls
        await settle(pilot)
        labels = [label for label, _id in dashboard._menu_stack[-1][1]]
        assert any("a•••e@a•••.com" in label for label in labels)
        assert not any("alice@acme.com" in label for label in labels)


@pytest.mark.asyncio
async def test_clicking_a_row_from_the_section_pane_returns_to_rows(tmp_path):  # 4
    from ccswap.tui.settings_screen import SettingRow

    app = make_app(FakeSwitcher([make_account(1, active=True)], tmp_path))
    async with app.run_test(size=(132, 40)) as pilot:
        screen = await _open_settings(pilot)
        await pilot.press("left")
        await settle(pilot)
        assert screen._pane == "sections"
        await pilot.click(next(r for r in screen.query(SettingRow) if r.action_id == "setting:view"))
        await settle(pilot)
        assert screen._pane == "rows"
        enter = screen._bindings.key_to_bindings["enter"][0]
        assert enter.description in ("Edit", "Done")  # the rows' keys, not the pane's "Open"


@pytest.mark.asyncio
async def test_esc_on_a_view_edit_restores_the_session_tab(tmp_path):  # 7
    app = make_app(FakeSwitcher([make_account(1, active=True)], tmp_path))
    async with app.run_test(size=(132, 40)) as pilot:
        await settle(pilot)
        await pilot.press("right", "right")  # tab: Codex for this session
        await settle(pilot)
        screen = await _open_settings(pilot)
        await pilot.press("down", "enter", "right")  # saved view: Combined → Claude Code
        await settle(pilot)
        assert app._saved_view == "claude"
        await pilot.press("escape")
        await settle(pilot)
        assert app._saved_view == "combined" and app._view == "codex"
        assert screen._editing is None


@pytest.mark.asyncio
async def test_a_failed_sidebar_update_still_runs_the_queued_one(tmp_path, monkeypatch):  # 8
    from ccswap.tui.sidebar import Sidebar

    app = make_app(FakeSwitcher([make_account(1, active=True)], tmp_path))
    async with app.run_test(size=(132, 40)) as pilot:
        await settle(pilot)
        sidebar = app.screen.query_one(Sidebar)
        calls = []
        original = Sidebar._compute_once

        def flaky(self, providers, snapshots):
            calls.append(1)
            if len(calls) == 1:
                self._rerun = True  # a request arrived while this one ran
                raise RuntimeError("bad record")
            return original(self, providers, snapshots)

        monkeypatch.setattr(Sidebar, "_compute_once", flaky)
        sidebar.update_panels()
        for _ in range(5):
            await settle(pilot)
        assert len(calls) == 2  # the failed run handed over to the queued one
        assert not sidebar._computing


@pytest.mark.asyncio
async def test_watch_asks_before_switching_codex(tmp_path):  # 9 (Switch: test_switch_routes_…)
    from ccswap.tui.modals import ConfirmModal
    from ccswap.tui.widgets import AccountItem

    codex = FakeSwitcher([make_account(1, active=True), make_account(2)], tmp_path)
    app = make_app(FakeSwitcher([], tmp_path), codex, start="watch")
    async with app.run_test(size=(100, 40)) as pilot:
        await settle(pilot)
        await pilot.press("s")
        listview = app.screen.query_one("#accounts")
        listview.index = next(
            i for i, item in enumerate(listview.children)
            if isinstance(item, AccountItem) and item.number == "2"
        )
        await pilot.press("enter")
        await settle(pilot)
        assert isinstance(app.screen, ConfirmModal) and codex.calls == []
        await pilot.press("y")
        await settle(pilot)
        assert codex.calls == [("switch_to", "2")]


@pytest.mark.asyncio
async def test_mask_with_a_submenu_open_keeps_an_account_cursor(tmp_path):  # 10
    from tests.test_tui import menu_select

    app, *_ = _three_providers(tmp_path)
    async with app.run_test(size=(132, 60)) as pilot:
        await settle(pilot)
        await menu_select(pilot, "remove-menu")
        await settle(pilot)
        panel = await _cursor_on(pilot, ("claude", "2"))
        await pilot.press("m")
        await settle(pilot)
        assert panel.cursor == ("claude", "2")
        assert app.screen.query_one("#menu").index is None
