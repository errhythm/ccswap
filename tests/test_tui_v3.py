"""TUI v3: header tabs, footer fitting, settings sections, the sidebar."""

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
