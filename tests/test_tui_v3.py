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
    "settings": ["esc Back", "q Quit", "? Option help", "m Mask off"],
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
    await settle(pilot)


@pytest.mark.asyncio
@pytest.mark.parametrize("state", list(NARROW))
@pytest.mark.parametrize("width", [80, 132])
async def test_footer_keys_per_screen_never_clip(tmp_path, fake_engine, state, width):
    app = make_app(FakeSwitcher([make_account(1, active=True), make_account(2)], tmp_path))
    async with app.run_test(size=(width, 30)) as pilot:
        await settle(pilot)
        await _goto(pilot, state)
        if state == "auto-adjusting":
            assert app.screen._adjusting
        expected = list(NARROW[state])
        if width >= 100:  # ^t Theme comes back before m Mask
            expected.insert(expected.index("m Mask off"), "^t Theme")
        assert await _footer(pilot, width) == expected
        assert app.screen.query_one("CswapFooter").compact is (width < 100)
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
        await pilot.press("enter")
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
