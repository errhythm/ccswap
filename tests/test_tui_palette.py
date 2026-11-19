"""Command palette: help lines, read-only rules, confirmation for restarts."""

from __future__ import annotations

import pytest

from tests.test_tui import FakeSwitcher, make_account, make_app, settle

pytestmark = pytest.mark.asyncio

MUTATING = ("Switch ", "Remove ", "Disable ", "Enable ", "Add ", "Settings", "Auto-switch strategy", "Go live")


def _all(app):
    from ccswap.tui.commands import account_commands, static_commands

    return static_commands(app) + account_commands(app)


def _apps(tmp_path, **kw):
    claude = FakeSwitcher([make_account(1, active=True), make_account(2)], tmp_path)
    codex = FakeSwitcher([make_account(1, active=True), make_account(2)], tmp_path)
    return claude, codex, make_app(claude, codex, **kw)


async def test_help_names_the_matching_cli_command(tmp_path):
    claude, codex, app = _apps(tmp_path)
    async with app.run_test(size=(100, 32)) as pilot:
        await settle(pilot)
        by_label = {c.label: c.help for c in _all(app)}
        switch = next(h for l, h in by_label.items() if l.startswith("Switch Claude Code to 2"))
        assert switch == "Same as ccswap switch 2"
        assert next(h for l, h in by_label.items() if l.startswith("Switch Codex to 2")) == "Same as ccswap codex switch 2"
        assert all(h.startswith(("Same as", "Show", "Stops")) for h in by_label.values())


async def test_claude_switch_runs_directly_codex_switch_asks_first(tmp_path):
    from ccswap.tui.modals import ConfirmModal

    claude, codex, app = _apps(tmp_path)
    async with app.run_test(size=(100, 32)) as pilot:
        await settle(pilot)
        cmds = {c.label.split(" · ")[0]: c for c in _all(app)}
        cmds["Switch Claude Code to 2"].run()
        await settle(pilot)
        assert ("switch_to", "2") in claude.calls

        cmds["Switch Codex to 2"].run()
        await pilot.pause()
        assert isinstance(app.screen, ConfirmModal)
        await pilot.press("n")
        await settle(pilot)
        assert codex.calls == []
        cmds["Switch Codex to 2"].run()
        await pilot.pause()
        await pilot.press("y")
        await settle(pilot)
        assert ("switch_to", "2") in codex.calls


async def test_locked_watch_offers_only_commands_that_change_nothing(tmp_path):
    claude, codex, app = _apps(tmp_path, start="watch")
    async with app.run_test(size=(100, 32)) as pilot:
        await settle(pilot)
        labels = [c.label for c in _all(app)]
        assert "Refresh usage" in labels and "Keys" in labels and "Quit" in labels
        assert not [l for l in labels if l.startswith(MUTATING)], labels
        await pilot.press("s")  # arm selection: the watch screen unlocks
        await pilot.pause()
        labels = [c.label for c in _all(app)]
        assert any(l.startswith("Switch Claude Code to 2") for l in labels)
        assert "Settings" in labels


async def test_palette_over_a_screen_above_locked_watch_stays_locked(tmp_path):
    from ccswap.tui.autoview import AutoScreen

    claude, codex, app = _apps(tmp_path, start="watch")
    async with app.run_test(size=(100, 32)) as pilot:
        await settle(pilot)
        app.open_auto("claude")
        await pilot.pause()
        assert isinstance(app.screen, AutoScreen)
        labels = [c.label for c in _all(app)]
        assert not [l for l in labels if l.startswith(MUTATING)], labels


async def test_default_list_leads_with_commands_that_change_nothing(tmp_path):
    claude, codex, app = _apps(tmp_path)
    async with app.run_test(size=(100, 32)) as pilot:
        await settle(pilot)
        from ccswap.tui.commands import static_commands

        lead = [c for c in static_commands(app) if c.lead]
        assert lead and all(c.ok_locked for c in lead)
        assert not [c.label for c in lead if c.label.startswith(MUTATING)]
        discovered = {c.title for c in app.get_system_commands(app.screen) if c.discover}
        assert discovered == {c.label for c in lead}


async def test_ctrl_p_then_enter_never_mutates(tmp_path):
    from textual.command import CommandPalette

    claude, codex, app = _apps(tmp_path)
    async with app.run_test(size=(100, 32)) as pilot:
        await settle(pilot)
        await pilot.press("ctrl+p")
        await pilot.pause(0.3)
        assert isinstance(app.screen, CommandPalette)
        await pilot.press("enter")
        await settle(pilot)
        assert claude.calls == [] and codex.calls == []
        assert not (tmp_path / "settings.json").exists()


async def test_back_to_back_snapshots_keep_one_divider_per_provider(tmp_path, monkeypatch):
    """Two snapshots racing through clear()/extend() used to duplicate sections."""
    import asyncio
    import dataclasses

    from textual.widgets import ListView

    from ccswap.tui.widgets import ProviderDivider

    claude, codex, app = _apps(tmp_path)
    async with app.run_test(size=(100, 48)) as pilot:
        await settle(pilot)
        await pilot.press("w")
        await pilot.pause()
        screen = app.screen
        # Make extend() yield, as a real mount does, so the rebuilds interleave.
        real_extend = ListView.extend

        async def slow_extend(self, items):
            await asyncio.sleep(0.05)
            return await real_extend(self, items)

        monkeypatch.setattr(ListView, "extend", slow_extend)
        base = dict(app.snapshots)
        more = dataclasses.replace(
            base["codex"], accounts=base["codex"].accounts + (make_account(3),)
        )
        await asyncio.gather(
            screen._on_snapshot(base),
            screen._on_snapshot({**base, "codex": more}),
            screen._on_snapshot(base),
        )
        await pilot.pause()
        dividers = [
            c.provider
            for c in screen.query_one("#accounts", ListView).children
            if isinstance(c, ProviderDivider)
        ]
        assert sorted(dividers) == sorted(set(dividers)) == ["claude", "codex"]
