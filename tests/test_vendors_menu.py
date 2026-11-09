"""Menu-bar model: one honest submenu per tool, from the shared overview."""

from __future__ import annotations

import json

from claude_swap.vendors.core import (
    AccountRecord,
    AccountView,
    UsageResult,
    UsageState,
    UsageWindow,
)
from claude_swap.vendors.menu import (
    collect_sections,
    loading_section,
    section_for,
    switchable,
)
from claude_swap.vendors.overview import Overview, ToolSummary
from claude_swap.vendors.registry import ToolRegistry
from claude_swap.vendors.service import VendorService
from claude_swap.vendors.store import VendorStore
from tests.vendor_helpers import FakeRunner, RunResult, ok

NOW = 2_000_000_000.0
OC = "/b/opencode"


def credits(used=1136.0, limit=2000.0, state=UsageState.AVAILABLE, **kw):
    return UsageResult(
        state,
        windows=(
            UsageWindow(
                "monthly-credits",
                "Plan credits",
                "credits",
                used=used,
                limit=limit,
                remaining=limit - used,
                used_percent=round(used / limit * 100, 1),
                resets_on="2099-12-01",
            ),
        ),
        plan="KIRO PRO+",
        source="kiro-cli chat --no-interactive --v2 /usage",
        authoritative=True,
        observed_at=NOW - 120,
        **kw,
    )


def labels(section):
    return [r.label for r in section.rows if r is not None]


def test_only_inactive_known_opencode_accounts_are_switchable():
    assert switchable("opencode", False, "anthropic")
    assert not switchable("opencode", True, "anthropic")
    assert not switchable("opencode", None, "anthropic")
    assert not switchable("muse", False, None)
    assert not switchable("kiro", False, "x")


def test_kiro_section_title_and_rows_carry_real_numbers_and_reset():
    summary = ToolSummary(
        "kiro",
        "Kiro CLI",
        installed=True,
        version="3.1.0",
        auth={"loggedIn": True, "email": "a@c.com", "accountType": "IamIdentityCenter"},
        tool_usage=credits(),
    )
    section = section_for(summary, "https://kiro.dev/docs/cli/", NOW)
    assert section.title == "Kiro CLI · credits 1,136/2,000 (56.8%)"
    text = "\n".join(labels(section))
    assert "Plan: KIRO PRO+" in text and "1,136 of 2,000 credits used (56.8%)" in text
    assert "resets on 2099-12-01" in text and "Updated" in text and "2m ago" in text
    assert any(r.action == "refresh" for r in section.rows if r)
    assert any(r.action == "open" and r.url.startswith("https://") for r in section.rows if r)


def test_unavailable_usage_is_never_shown_as_zero():
    summary = ToolSummary(
        "muse",
        "Muse Code",
        installed=True,
        tool_usage=UsageResult(
            UsageState.UNAVAILABLE, "The connected account returned no allowance data"
        ),
    )
    section = section_for(summary, None, NOW)
    assert section.title == "Muse Code · usage unavailable"
    assert not any("0%" in label for label in labels(section))
    assert any("no allowance data" in label for label in labels(section))


def test_stale_data_is_labelled_with_the_failure():
    summary = ToolSummary(
        "kiro",
        "Kiro CLI",
        installed=True,
        tool_usage=credits(state=UsageState.STALE, error="Kiro /usage failed: timed out"),
    )
    section = section_for(summary, None, NOW)
    assert section.title.startswith("Kiro CLI · stale · credits")
    assert any(
        "refresh failed: Kiro /usage failed: timed out" in label for label in labels(section)
    )


def test_not_installed_offers_only_install_docs():
    section = section_for(ToolSummary("opencode", "OpenCode"), "https://opencode.ai/docs", NOW)
    assert section.title == "OpenCode · not installed"
    assert [r.action for r in section.rows if r] == [None, "open"]


def test_account_rows_keep_each_accounts_own_usage_and_switchability():
    accounts = [
        (
            AccountView(
                "opencode",
                "work",
                "oauth",
                "anthropic",
                {"account": "work", "id": "c1"},
                active=True,
            ),
            UsageResult(UsageState.UNAVAILABLE, "per provider, not per account"),
        ),
        (
            AccountView(
                "opencode",
                "alt",
                "oauth",
                "anthropic",
                {"account": "alt", "id": "c2"},
                active=False,
            ),
            UsageResult(UsageState.UNAVAILABLE, "per provider, not per account"),
        ),
    ]
    summary = ToolSummary("opencode", "OpenCode", installed=True, accounts=accounts)
    rows = [r for r in section_for(summary, None, NOW).rows if r and r.label.startswith("  ")]
    assert [(r.checked, r.action, r.selector) for r in rows] == [
        (True, None, None),
        (False, "switch", "c2"),
    ]


def test_muse_and_kiro_account_rows_are_never_clickable_switches():
    for tool in ("muse", "kiro"):
        view = AccountView(tool, "b", "login", active=False)
        summary = ToolSummary(tool, tool, installed=True, accounts=[(view, credits())])
        rows = [r for r in section_for(summary, None, NOW).rows if r and "b ·" in r.label]
        assert rows and all(r.action is None for r in rows)
        assert "switch in terminal" in rows[0].label


def test_loading_section_is_explicit():
    assert loading_section("kiro").title == "Kiro CLI · loading…"


def test_collect_sections_isolates_a_broken_tool(tmp_path):
    r = FakeRunner({"opencode": OC})
    r.on(OC, "--version", result=ok("1.99.0"))
    r.on(OC, "auth", "--help", result=RunResult(0, stderr="opencode auth switch"))
    r.on(OC, "auth", "list", "--help", result=RunResult(0, stderr="--format"))
    r.on(OC, "auth", "login", "--help", result=RunResult(0, stderr=""))
    r.on(OC, "auth", "list", "--format", "json", result=ok("garbage"))
    r.on(OC, "db", "--help", result=RunResult(0, stderr="query --format"))
    r.on(OC, "db", result=ok("[]"))
    svc = VendorService(ToolRegistry.default(r), VendorStore(tmp_path / "v"))
    sections = collect_sections(Overview(svc, legacy=[]), now=NOW)
    assert set(sections) == {"muse", "kiro", "opencode"}
    assert sections["muse"].title == "Muse Code · not installed"
    assert any("malformed" in label for label in labels(sections["opencode"]))


def test_registered_muse_rows_say_switch_in_terminal(tmp_path):
    r = FakeRunner({"muse": "/b/muse"})
    r.on("/b/muse", "--version", result=ok("Muse Code 1.4.14"))
    r.on(
        "/b/muse",
        "usage",
        "--json",
        result=ok(json.dumps({"allowance": None, "reason": "api_key"})),
    )
    svc = VendorService(ToolRegistry.default(r), VendorStore(tmp_path / "v"))
    svc.store.add(AccountRecord("muse", "work", "api-key"), "key-aaaaaaaa")
    section = collect_sections(Overview(svc, legacy=[]), ("muse",), now=NOW)["muse"]
    row = next(r for r in section.rows if r and "work" in r.label)
    assert row.action is None and "pay-as-you-go" not in section.title
