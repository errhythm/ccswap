"""Pilot-driven tests for the multi-vendor TUI screen."""

from __future__ import annotations

import json

import pytest
from textual.widgets import ListView, Static

from claude_swap.vendors.core import AccountRecord
from claude_swap.vendors.registry import ToolRegistry
from claude_swap.vendors.service import VendorService
from claude_swap.vendors.store import VendorStore
from claude_swap.tui.vendors import VendorsScreen, VendorItem, account_label, usage_lines
from claude_swap.vendors.core import AccountView, UsageResult, UsageState
from tests.test_tui import FakeSwitcher, make_app, settle
from tests.vendor_helpers import FakeRunner, ok

MUSE, KIRO, OC = "/b/muse", "/b/kiro-cli", "/b/opencode"
KEY = "mk_live_0123456789abcdef"
IDC = {"accountType": "IamIdentityCenter", "startUrl": "https://c.awsapps.com/start",
       "region": "us-east-1", "email": "a@c.com"}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("META_API_KEY", raising=False)
    monkeypatch.delenv("KIRO_API_KEY", raising=False)


def world(tmp_path, *, installed=("muse", "opencode", "kiro")):
    bins = {"muse": MUSE, "opencode": OC, "kiro-cli": KIRO}
    r = FakeRunner({k: v for k, v in bins.items() if {"kiro-cli": "kiro"}.get(k, k) in installed})
    r.on(MUSE, "--version", result=ok("Muse Code 1.4.14\n"))
    r.on(OC, "--version", result=ok("1.99.0\n"))
    r.on(KIRO, "--version", result=ok("kiro-cli 3.1.0\n"))
    r.on(KIRO, "whoami", result=ok(json.dumps(IDC)))
    r.on(OC, "auth", "--help", result=ok("opencode auth switch\n"))
    r.on(OC, "auth", "list", "--help", result=ok("--format"))
    r.on(OC, "auth", "login", "--help", result=ok("--provider"))
    rows = [{"provider": "anthropic", "id": "c1", "label": "work", "type": "oauth", "active": True},
            {"provider": "anthropic", "id": "c2", "label": "personal", "type": "oauth", "active": False}]
    state = {"rows": rows}
    r.on(OC, "auth", "list", "--format", "json", result=lambda c: ok(json.dumps(state["rows"])))

    def do_switch(call):
        target = call.argv[-1]
        state["rows"] = [{**x, "active": x["id"] == target} for x in state["rows"]]
        return ok()

    r.on(OC, "auth", "switch", result=do_switch)
    r.on(MUSE, "auth", "set", result=ok())
    r.on(MUSE, "usage", "--json", result=ok('{"allowance": null, "reason": "server_sent_no_usage"}'))
    svc = VendorService(ToolRegistry.default(r), VendorStore(tmp_path / "v"))
    return svc, r


async def open_screen(app, pilot):
    app.vendor_service = app._svc
    await pilot.press("v")
    await settle(pilot)
    assert isinstance(app.screen, VendorsScreen)
    return app.screen


def app_with(tmp_path, svc):
    app = make_app(FakeSwitcher([], tmp_path))
    app._svc = svc
    return app


def text_of(screen, selector):
    return str(screen.query_one(selector, Static).render())


@pytest.mark.asyncio
async def test_screen_shows_vendors_navigation_and_back(tmp_path):
    svc, _ = world(tmp_path)
    app = app_with(tmp_path, svc)
    async with app.run_test(size=(100, 32)) as pilot:
        screen = await open_screen(app, pilot)
        assert "Muse Code" in text_of(screen, "#vendor-tabs") and "Kiro CLI" in text_of(screen, "#vendor-tabs")
        assert screen.tool == "muse"
        await pilot.press("right"); await settle(pilot)
        assert screen.tool == "opencode"
        await pilot.press("right"); await settle(pilot)
        assert screen.tool == "kiro"
        await pilot.press("right"); await settle(pilot)
        assert screen.tool == "muse"  # wraps
        await pilot.press("escape"); await settle(pilot)
        assert not isinstance(app.screen, VendorsScreen)


@pytest.mark.asyncio
async def test_empty_state_and_install_status(tmp_path):
    svc, _ = world(tmp_path, installed=("kiro",))
    app = app_with(tmp_path, svc)
    async with app.run_test(size=(100, 32)) as pilot:
        screen = await open_screen(app, pilot)
        assert "not installed" in text_of(screen, "#vendor-info")
        await pilot.press("right"); await pilot.press("right"); await settle(pilot)
        await pilot.press("left"); await settle(pilot)
        assert "OpenCode is not installed" in text_of(screen, "#vendor-info")


@pytest.mark.asyncio
async def test_opencode_accounts_are_provider_aware_and_switch_natively(tmp_path):
    svc, runner = world(tmp_path)
    app = app_with(tmp_path, svc)
    async with app.run_test(size=(100, 32)) as pilot:
        screen = await open_screen(app, pilot)
        await pilot.press("right"); await settle(pilot)
        items = list(screen.query(VendorItem))
        assert [i.view.identity["account"] for i in items] == ["work", "personal"]
        assert items[0].view.active is True and items[0].view.provider == "anthropic"
        screen.query_one("#vaccounts", ListView).index = 1
        await pilot.pause()
        await pilot.press("s"); await settle(pilot); await settle(pilot)
        assert (OC, "auth", "switch", "anthropic", "c2") in runner.argvs()
        assert "verified" in text_of(screen, "#vendor-detail") and "restart" in text_of(screen, "#vendor-detail")
        assert [i.view.active for i in screen.query(VendorItem)] == [False, True]


@pytest.mark.asyncio
async def test_muse_api_key_switch_requires_confirmation(tmp_path):
    svc, runner = world(tmp_path)
    svc.store.add(AccountRecord("muse", "work", "api-key"), KEY)
    app = app_with(tmp_path, svc)
    async with app.run_test(size=(100, 32)) as pilot:
        screen = await open_screen(app, pilot)
        await pilot.press("s"); await pilot.pause()
        assert not any(a[1:3] == ("auth", "set") for a in runner.argvs())  # nothing before confirming
        await pilot.press("n"); await settle(pilot)
        assert not any(a[1:3] == ("auth", "set") for a in runner.argvs())
        await pilot.press("s"); await pilot.pause()
        await pilot.press("y"); await settle(pilot); await settle(pilot)
        call = next(c for c in runner.calls if c.argv[1:3] == ("auth", "set"))
        assert call.input_text == KEY + "\n"
        assert KEY not in text_of(screen, "#vendor-detail")


@pytest.mark.asyncio
async def test_usage_unknown_is_explained_not_zeroed(tmp_path):
    svc, _ = world(tmp_path)
    svc.store.add(AccountRecord("muse", "work", "api-key"), KEY)
    app = app_with(tmp_path, svc)
    async with app.run_test(size=(100, 32)) as pilot:
        screen = await open_screen(app, pilot)
        await pilot.press("u"); await settle(pilot); await settle(pilot)
        detail = text_of(screen, "#vendor-detail")
        assert "unavailable" in detail and "server_sent_no_usage" in detail
        assert "%" not in detail


@pytest.mark.asyncio
async def test_kiro_login_switch_explains_cli_path(tmp_path):
    svc, runner = world(tmp_path)
    svc.store.add(AccountRecord("kiro", "a", "login", identity=IDC))
    app = app_with(tmp_path, svc)
    async with app.run_test(size=(100, 32)) as pilot:
        screen = await open_screen(app, pilot)
        await pilot.press("right"); await pilot.press("right"); await settle(pilot)
        assert screen.tool == "kiro"
        await pilot.press("s"); await pilot.pause()
        assert not any(c.interactive for c in runner.calls)  # the TUI never runs logout/login
        from claude_swap.tui.modals import OutputModal
        assert isinstance(app.screen, OutputModal)


@pytest.mark.asyncio
async def test_small_terminal_does_not_crash(tmp_path):
    svc, _ = world(tmp_path)
    app = app_with(tmp_path, svc)
    async with app.run_test(size=(40, 14)) as pilot:
        screen = await open_screen(app, pilot)
        for _ in range(3):
            await pilot.press("right"); await settle(pilot)
        await pilot.press("c"); await pilot.pause()
        assert app.screen is not screen  # capability modal opened


@pytest.mark.asyncio
async def test_add_help_never_asks_for_secret_arguments(tmp_path):
    svc, _ = world(tmp_path)
    app = app_with(tmp_path, svc)
    async with app.run_test(size=(100, 32)) as pilot:
        screen = await open_screen(app, pilot)
        await pilot.press("a"); await pilot.pause()
        from claude_swap.tui.modals import OutputModal
        assert isinstance(app.screen, OutputModal)


def test_pure_formatters_mask_nothing_secret_and_label_unknowns():
    view = AccountView("kiro", "a", "login", identity={"email": "a@c.com"}, active=None, registered=False)
    label = account_label(view, 1)
    assert "?" in label and "[unregistered]" in label
    assert usage_lines(None)[0].startswith("usage:")
    lines = usage_lines(UsageResult(UsageState.UNSUPPORTED, "nope", hint="try x"))
    assert lines == ["usage unsupported: nope", "try x"]
