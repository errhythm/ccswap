"""Pilot tests: every tool is a first-class, independently reachable TUI page."""

from __future__ import annotations

import json

import pytest
from textual.widgets import ListView, Static

from claude_swap.tui.modals import ConfirmModal
from claude_swap.tui.tools import AccountRow, OverviewRow, OverviewScreen, ToolScreen, strip_text
from claude_swap.vendors.core import AccountRecord
from claude_swap.vendors.registry import ToolRegistry
from claude_swap.vendors.service import VendorService
from claude_swap.vendors.store import VendorStore
from tests.test_tui import FakeSwitcher, make_account, make_app, settle
from tests.vendor_helpers import FakeRunner, RunResult, fail, ok

MUSE, KIRO, OC = "/b/muse", "/b/kiro-cli", "/b/opencode"
KEY = "mk_live_0123456789abcdef"
IDC = {
    "accountType": "IamIdentityCenter",
    "startUrl": "https://c.awsapps.com/start",
    "region": "us-east-1",
    "email": "a@c.com",
}
OTHER = {**IDC, "email": "b@c.com"}
ALLOWANCE = {
    "tier": "Muse Code High Usage",
    "window": {"usedPercent": 34, "resetsAtMs": 4102444800000},
    "weekly": {"usedPercent": 7, "resetsAtMs": 4103049600000},
    "asOfMs": 4102400000000,
}
KIRO_REPORT = (
    "Estimated Usage | resets on 2099-12-01 | KIRO PRO+\n"
    "Credits (500.50 of 2000 covered in plan), 25.0%\nOverages: Disabled\n"
)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("META_API_KEY", raising=False)
    monkeypatch.delenv("KIRO_API_KEY", raising=False)


def world(tmp_path, *, installed=("muse", "opencode", "kiro"), who=IDC):
    bins = {"muse": MUSE, "opencode": OC, "kiro-cli": KIRO}
    r = FakeRunner({k: v for k, v in bins.items() if {"kiro-cli": "kiro"}.get(k, k) in installed})
    r.on(MUSE, "--version", result=ok("Muse Code 1.4.14\n"))
    r.on(OC, "--version", result=ok("1.99.0\n"))
    r.on(KIRO, "--version", result=ok("kiro-cli 3.1.0\n"))
    r.on(KIRO, "whoami", result=ok(json.dumps(who)))
    r.on(KIRO, "chat", "--help", result=ok("--no-interactive --v2 --v3"))
    r.on(KIRO, "chat", "--no-interactive", "--v2", "/usage", result=ok(KIRO_REPORT))
    r.on(OC, "auth", "--help", result=RunResult(0, stderr="  opencode auth switch <p> <a>\n"))
    r.on(OC, "auth", "list", "--help", result=RunResult(0, stderr="--format"))
    r.on(OC, "auth", "login", "--help", result=RunResult(0, stderr="--provider"))
    r.on(OC, "db", "--help", result=RunResult(0, stderr="query --format json"))
    r.on(
        OC,
        "db",
        result=ok(
            json.dumps(
                [
                    {
                        "provider": "anthropic",
                        "model": "m1",
                        "last": 1,
                        "n_24h": 2,
                        "cost_24h": 0.5,
                        "n_7d": 2,
                        "cost_7d": 0.5,
                        "n_30d": 3,
                        "cost_30d": 1.25,
                        "n_all": 9,
                        "cost_all": 4.0,
                    }
                ]
            )
        ),
    )
    rows = [
        {"provider": "anthropic", "id": "c1", "label": "work", "type": "oauth", "active": True},
        {
            "provider": "anthropic",
            "id": "c2",
            "label": "personal",
            "type": "oauth",
            "active": False,
        },
    ]
    state = {"rows": rows}
    r.on(OC, "auth", "list", "--format", "json", result=lambda c: ok(json.dumps(state["rows"])))

    def do_switch(call):
        state["rows"] = [{**x, "active": x["id"] == call.argv[-1]} for x in state["rows"]]
        return ok()

    r.on(OC, "auth", "switch", result=do_switch)
    r.on(MUSE, "auth", "set", result=ok())
    r.on(
        MUSE,
        "usage",
        "--json",
        result=lambda c: ok(
            json.dumps(
                {"allowance": ALLOWANCE}
                if "META_API_KEY" not in c.env
                else {"allowance": None, "reason": "api_key"}
            )
        ),
    )
    reg = ToolRegistry.default(r)
    reg.get("kiro").probe_dir = tmp_path / "probe"
    svc = VendorService(reg, VendorStore(tmp_path / "v"))
    return svc, r


def app_with(tmp_path, svc, accounts=()):
    app = make_app(FakeSwitcher(list(accounts), tmp_path))
    app.vendor_service = svc
    return app


def text_of(screen, selector):
    return str(screen.query_one(selector, Static).render())


async def drain(pilot):
    """Wait for the tool pages' daemon threads, then let their UI updates apply."""
    import asyncio

    from claude_swap.tui import tools

    for _ in range(3):
        for thread in list(tools.BACKGROUND):
            await asyncio.to_thread(thread.join, 5)
        await settle(pilot)


async def goto(pilot, key):
    await pilot.press(key)
    await drain(pilot)


@pytest.mark.asyncio
async def test_number_keys_reach_every_tool_as_a_peer(tmp_path):
    svc, _ = world(tmp_path)
    app = app_with(tmp_path, svc, [make_account(1, active=True)])
    async with app.run_test(size=(110, 40)) as pilot:
        await settle(pilot)
        for key, tool in (("3", "muse"), ("4", "kiro"), ("5", "opencode")):
            await goto(pilot, key)
            assert isinstance(app.screen, ToolScreen) and app.screen.tool == tool
            assert (
                len(app.screen_stack) == 3
            )  # default + dashboard + one page: pages replace each other
        await goto(pilot, "1")  # Claude Code: its dashboard, filtered
        assert not isinstance(app.screen, (ToolScreen, OverviewScreen)) and app._view == "claude"
        await goto(pilot, "2")
        assert app._view == "codex"
        await goto(pilot, "0")
        assert isinstance(app.screen, OverviewScreen)


@pytest.mark.asyncio
async def test_peer_row_marks_the_current_tool():
    assert "[" not in strip_text("kiro").plain  # styled, not bracketed
    plain = strip_text("kiro").plain
    for label in (
        "Claude Code",
        "Codex",
        "Muse Code",
        "Kiro CLI",
        "OpenCode",
        "Antigravity",
        "Overview",
    ):
        assert label in plain


@pytest.mark.asyncio
async def test_muse_page_shows_real_windows_and_reset(tmp_path):
    svc, _ = world(tmp_path)
    app = app_with(tmp_path, svc)
    async with app.run_test(size=(110, 40)) as pilot:
        await goto(pilot, "3")
        usage = text_of(app.screen, "#tool-usage")
        assert "5-hour window" in usage and "34%" in usage and "Weekly" in usage and "7%" in usage
        assert "resets in" in usage and "Muse Code High Usage" in usage
        assert "active credential" in usage  # Muse never says whose login it is


@pytest.mark.asyncio
async def test_muse_accounts_each_show_their_own_usage(tmp_path):
    svc, runner = world(tmp_path)
    svc.store.add(AccountRecord("muse", "payg", "api-key"), KEY)
    svc.store.add(AccountRecord("muse", "me", "browser"))
    app = app_with(tmp_path, svc)
    async with app.run_test(size=(110, 40)) as pilot:
        await goto(pilot, "3")
        rows = {r.view.alias: r for r in app.screen.query(AccountRow)}
        assert "API-key sign-in" in rows["payg"].usage.reason  # its own key, measured in isolation
        assert rows["me"].usage.windows == ()  # never the active credential's numbers
        payg_call = next(c for c in runner.calls if c.argv[1:2] == ("usage",) and c.env)
        assert payg_call.env == {"META_API_KEY": KEY}


@pytest.mark.asyncio
async def test_kiro_page_shows_credits_and_reset_for_the_signed_in_account_only(tmp_path):
    svc, _ = world(tmp_path)
    svc.store.add(AccountRecord("kiro", "a", "login", identity=IDC))
    svc.store.add(AccountRecord("kiro", "b", "login", identity=OTHER))
    app = app_with(tmp_path, svc)
    async with app.run_test(size=(110, 40)) as pilot:
        await goto(pilot, "4")
        usage = text_of(app.screen, "#tool-usage")
        assert "500" in usage and "2,000" in usage and "2099-12-01" in usage
        rows = {r.view.alias: r for r in app.screen.query(AccountRow)}
        assert rows["a"].view.active is True and rows["a"].usage.windows
        assert rows["b"].view.active is False and rows["b"].usage.windows == ()
        assert "not the signed-in" in rows["b"].usage.reason


@pytest.mark.asyncio
async def test_opencode_page_separates_local_stats_from_quota_and_switches(tmp_path):
    svc, runner = world(tmp_path)
    app = app_with(tmp_path, svc)
    async with app.run_test(size=(110, 40)) as pilot:
        await goto(pilot, "5")
        usage = text_of(app.screen, "#tool-usage")
        assert "local" in usage and "not a quota" in usage and "anthropic" in usage
        rows = list(app.screen.query(AccountRow))
        assert [(r.view.provider, r.view.identity["account"]) for r in rows] == [
            ("anthropic", "work"),
            ("anthropic", "personal"),
        ]
        app.screen.query_one("#taccounts", ListView).index = 1
        await pilot.pause()
        await goto(pilot, "s")
        await drain(pilot)
        assert (OC, "auth", "switch", "anthropic", "c2") in runner.argvs()
        assert [r.view.active for r in app.screen.query(AccountRow)] == [False, True]


@pytest.mark.asyncio
async def test_muse_switch_needs_confirmation_and_never_shows_the_key(tmp_path):
    svc, runner = world(tmp_path)
    svc.store.add(AccountRecord("muse", "work", "api-key"), KEY)
    app = app_with(tmp_path, svc)
    async with app.run_test(size=(110, 40)) as pilot:
        await goto(pilot, "3")
        await pilot.press("s")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmModal)
        assert not any(c.argv[1:3] == ("auth", "set") for c in runner.calls)
        await pilot.press("n")
        await drain(pilot)
        assert not any(c.argv[1:3] == ("auth", "set") for c in runner.calls)
        await pilot.press("s")
        await pilot.pause()
        await pilot.press("y")
        await drain(pilot)
        call = next(c for c in runner.calls if c.argv[1:3] == ("auth", "set"))
        assert call.input_text == KEY + "\n" and KEY not in " ".join(call.argv)
        for sel in ("#tool-head", "#tool-usage", "#tool-detail"):
            assert KEY not in text_of(app.screen, sel)


@pytest.mark.asyncio
async def test_kiro_switch_to_another_login_reports_what_it_needs_without_running_logout(tmp_path):
    svc, runner = world(tmp_path)
    svc.store.add(AccountRecord("kiro", "a", "login", identity=IDC))
    svc.store.add(AccountRecord("kiro", "b", "login", identity=OTHER))
    app = app_with(tmp_path, svc)
    async with app.run_test(size=(110, 40)) as pilot:
        await goto(pilot, "4")
        app.screen.query_one("#taccounts", ListView).index = 1
        await pilot.pause()
        await goto(pilot, "s")
        assert not any(c.interactive for c in runner.calls)  # never logout/login from the TUI
        assert "--login" in text_of(app.screen, "#tool-detail")


@pytest.mark.asyncio
async def test_muse_browser_accounts_switch_inside_ccswap(tmp_path, monkeypatch):
    cfg = tmp_path / "cfg" / "muse"
    cfg.mkdir(parents=True)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))

    def login(email):
        (cfg / "auth.json").write_text(
            json.dumps({"providers": {"meta": {"access_token": "t-" + email, "user_email": email}}})
        )

    svc, _ = world(tmp_path)
    login("j@x.com")
    svc.add("muse", "jay", kind="browser")
    login("m@x.com")
    svc.add("muse", "mis", kind="browser")
    app = app_with(tmp_path, svc)
    async with app.run_test(size=(110, 40)) as pilot:
        await goto(pilot, "3")
        rows = {r.view.alias: r for r in app.screen.query(AccountRow)}
        assert rows["mis"].view.active is True and rows["jay"].view.active is False
        app.screen.query_one("#taccounts", ListView).index = list(rows).index("jay")
        await pilot.pause()
        await goto(pilot, "s")
        assert (
            json.loads((cfg / "auth.json").read_text())["providers"]["meta"]["user_email"]
            == "j@x.com"
        )
        assert {r.view.alias: r.view.active for r in app.screen.query(AccountRow)} == {
            "jay": True,
            "mis": False,
        }


@pytest.mark.asyncio
async def test_remove_forgets_registration_only_after_confirm(tmp_path):
    svc, runner = world(tmp_path)
    svc.store.add(AccountRecord("muse", "work", "api-key"), KEY)
    app = app_with(tmp_path, svc)
    async with app.run_test(size=(110, 40)) as pilot:
        await goto(pilot, "3")
        await pilot.press("x")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmModal)
        await pilot.press("y")
        await drain(pilot)
        assert svc.store.records("muse") == []
        assert not any(c.argv[1:2] == ("logout",) for c in runner.calls)


@pytest.mark.asyncio
async def test_not_installed_tool_page_says_so(tmp_path):
    svc, _ = world(tmp_path, installed=("kiro",))
    app = app_with(tmp_path, svc)
    async with app.run_test(size=(110, 40)) as pilot:
        await goto(pilot, "3")
        assert "not installed" in text_of(app.screen, "#tool-head")
        await goto(pilot, "5")
        assert "not installed" in text_of(app.screen, "#tool-head")


@pytest.mark.asyncio
async def test_failed_refresh_keeps_last_good_data_marked_stale(tmp_path):
    svc, runner = world(tmp_path)
    app = app_with(tmp_path, svc)
    async with app.run_test(size=(110, 40)) as pilot:
        await goto(pilot, "3")
        assert "34%" in text_of(app.screen, "#tool-usage")
        runner.on(MUSE, "usage", "--json", result=fail("network down"))
        await goto(pilot, "r")
        await drain(pilot)
        usage = text_of(app.screen, "#tool-usage")
        assert "34%" in usage and "last reading (stale)" in usage and "network down" in usage


@pytest.mark.asyncio
async def test_overview_lists_all_six_tools_and_opens_them(tmp_path):
    svc, _ = world(tmp_path)
    app = app_with(tmp_path, svc, [make_account(1, active=True)])
    async with app.run_test(size=(110, 44)) as pilot:
        await settle(pilot)
        await goto(pilot, "0")
        await settle(pilot)
        rows = list(app.screen.query(OverviewRow))
        assert [r.tool for r in rows] == ["claude", "codex", "muse", "kiro", "opencode", "agy"]
        labels = {r.tool: str(r.query_one(Static).render()) for r in rows}
        assert (
            "5h 34%" in labels["muse"]
            and "credits" in labels["kiro"]
            and "local 30d" in labels["opencode"]
        )
        app.screen.query_one("#overview", ListView).index = 3
        await pilot.pause()
        await goto(pilot, "enter")
        assert isinstance(app.screen, ToolScreen) and app.screen.tool == "kiro"


@pytest.mark.asyncio
async def test_narrow_terminal_pages_render(tmp_path):
    svc, _ = world(tmp_path)
    app = app_with(tmp_path, svc)
    async with app.run_test(size=(48, 16)) as pilot:
        for key in ("3", "4", "5", "0"):
            await goto(pilot, key)
        await pilot.press("c")
        await drain(pilot)


@pytest.mark.asyncio
async def test_masking_hides_emails_on_tool_pages(tmp_path):
    svc, _ = world(tmp_path)
    svc.store.add(AccountRecord("kiro", "a", "login", identity=IDC))
    app = app_with(tmp_path, svc)
    async with app.run_test(size=(110, 40)) as pilot:
        await goto(pilot, "4")
        await pilot.press("m")
        await drain(pilot)
        app.screen._paint()
        assert "a@c.com" not in text_of(app.screen, "#tool-head")
