"""Menu-bar model: one honest submenu per tool, from the shared overview."""

from __future__ import annotations

from claude_swap.vendors.core import (
    AccountView,
    UsageKind,
    UsageObservation,
    UsageResult,
    UsageState,
    UsageWindow,
)
from claude_swap.vendors.menu import (
    collect_sections,
    loading_section,
    menu_usage_text,
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


def test_which_accounts_a_click_can_switch():
    assert switchable("opencode", False, "anthropic")
    assert not switchable("opencode", True, "anthropic")
    assert not switchable("opencode", None, "anthropic")
    assert switchable("muse", False, None, "browser")
    assert not switchable("muse", None, None, "api-key")  # `ccswap muse run`, not a login
    assert switchable("kiro", False, None, "login")
    assert not switchable("kiro", False, None, "api-key")  # process-scoped: `kiro run`
    assert not switchable("muse", True, None, "browser")  # already active
    assert not switchable("kiro", False, None, "login", registered=False)


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
    assert section.title == "Muse Code · —"
    assert not any("0%" in label for label in labels(section))
    assert "unavailable" not in "\n".join(labels(section)).lower()
    assert "no allowance data" not in "\n".join(labels(section))


def test_stale_data_shows_last_numbers_with_no_label():
    summary = ToolSummary(
        "kiro",
        "Kiro CLI",
        installed=True,
        tool_usage=credits(state=UsageState.STALE, error="Kiro /usage failed: timed out"),
    )
    section = section_for(summary, None, NOW)
    assert section.title == "Kiro CLI · credits 1,136/2,000 (56.8%)"
    text = "\n".join(labels(section))
    assert "1,136 of 2,000 credits used (56.8%)" in text  # last numbers, kept
    assert "stale" not in text.lower() and "failed" not in text.lower()


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


def test_muse_api_key_accounts_are_listed_but_never_clickable():
    accounts = [
        (
            AccountView(
                "muse",
                "mis",
                "browser",
                identity={"email": "m@x.com"},
                active=True,
            ),
            UsageResult(
                UsageState.AVAILABLE,
                windows=(
                    UsageWindow("five-hour", "5-hour window", "percent", used_percent=0),
                ),
            ),
        ),
        (
            AccountView("muse", "lane-key", "api-key", active=None),
            UsageResult(UsageState.UNAVAILABLE, "Muse does not report usage for an API-key sign-in"),
        ),
    ]
    section = section_for(
        ToolSummary("muse", "Muse Code", installed=True, accounts=accounts), None, NOW
    )
    top = [r.label for r in section.accounts]
    assert len(top) == 2 and "lane-key" in top[1]  # every account is shown
    key_row = section.accounts[1]
    assert key_row.action is None and key_row.selector is None  # never clickable
    assert key_row.label.endswith("—")  # no usage words, just the placeholder
    detail = "\n".join(r.label for r in section.rows if r)
    assert "lane-key" in detail and "switch in terminal" not in detail


def test_a_dead_muse_login_is_not_checked_and_can_be_restored():
    accounts = [
        (
            AccountView(
                "muse", "mis", "browser", identity={"email": "m@x.com"}, active=True
            ),
            UsageResult(UsageState.UNAUTHENTICATED, "Muse has no login"),
        )
    ]
    section = section_for(
        ToolSummary("muse", "Muse Code", installed=True, accounts=accounts), None, NOW
    )
    row = section.accounts[0]
    assert row.checked is False and row.action == "switch" and row.selector == "mis"
    assert "sign-in" not in row.label.lower() and row.label.endswith("—")
    detail = next(r for r in section.rows if r and r.selector == "mis")
    assert detail.checked is False and detail.action == "switch"
    assert detail.label.startswith("  ○")


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


def test_top_level_account_rows_look_like_claude_codes():
    arn = "arn:aws:codewhisperer:us-east-1:151869951925:profile/QXHDHVXUCRC4"
    accounts = [
        (
            AccountView("kiro", "kiro-a", "login", identity={"email": "a@c.com"}, active=True),
            credits(),
        ),
        (
            AccountView(
                "kiro",
                "kiro-b",
                "login",
                identity={"email": "a@c.com", "profileArn": arn},
                active=False,
            ),
            credits(used=10000.0, limit=10000.0, state=UsageState.STALE),
        ),
        (
            AccountView("kiro", "ci", "api-key", active=None),
            UsageResult(UsageState.UNAVAILABLE, "x"),
        ),
    ]
    section = section_for(
        ToolSummary("kiro", "Kiro CLI", installed=True, accounts=accounts), None, NOW
    )
    rows = section.accounts
    assert [r.label.split("  ")[0] for r in rows] == ["1", "2", "3"]
    assert rows[0].checked and rows[0].action is None  # active: the check mark, no action
    assert rows[1].action == "switch" and rows[1].selector == "kiro-b" and not rows[1].raw
    assert "[AWS 151869951925]" in rows[1].label
    assert "credits 10,000/10,000" in rows[1].label and "stale" not in rows[1].label.lower()
    assert rows[2].action is None  # an API key is run, not switched


def test_menu_usage_text_is_numbers_or_placeholder():
    assert menu_usage_text(credits()) == "credits 1,136/2,000 (56.8%)"
    assert menu_usage_text(credits(state=UsageState.STALE)) == "credits 1,136/2,000 (56.8%)"
    assert menu_usage_text(UsageResult(UsageState.UNAVAILABLE, "nothing yet")) == "—"
    assert menu_usage_text(None) == "—"


def go_result(state=UsageState.AVAILABLE):
    return UsageResult(
        state,
        "Workspace access denied",
        observations=(
            UsageObservation(
                UsageKind.LOCAL_TOKENS,
                "opencode db (local message records)",
                False,
                scope="local",
                data={
                    "providers": [
                        {
                            "provider": "opencode-go",
                            "models": [],
                            "totals": {"30d": {"messages": 7, "costUsd": 2.5}},
                        }
                    ],
                    "go": {
                        "models": [
                            {
                                "model": "muse-spark",
                                "fiveHourUsd": 0.5,
                                "weekUsd": 4.0,
                                "thirtyDayUsd": 38.48,
                            }
                        ],
                        "rule": "Go limits each model separately",
                        "unit": "usd-equivalent",
                    },
                    "lastErrors": {
                        "opencode-go": {
                            "message": "Workspace access denied",
                            "status": 403,
                            "at": 1.0,
                        }
                    },
                },
            ),
        ),
        error="last request refused: 403",
        observed_at=NOW - 60,
    )


BANNED_MENU_WORDS = ["stale", "unavailable", "sign-in", "refused", "error", "failed", "not logged"]


def test_menu_rows_never_show_failure_text():
    for state in UsageState:
        variants = {
            "bare": UsageResult(
                state,
                "usage unavailable: not logged in; sign-in required",
                error="last request refused with error; refresh failed",
            ),
            "windows": credits(
                state=state,
                reason="usage unavailable: not logged in",
                error="refresh failed with error",
            ),
            "go": go_result(state),
        }
        for name, usage in variants.items():
            summary = ToolSummary(
                "muse",
                "Muse Code",
                installed=True,
                version="1.4.14",
                accounts=[
                    (
                        AccountView(
                            "muse",
                            "me",
                            "browser",
                            identity={"email": "m@x.com"},
                            active=True,
                        ),
                        usage,
                    )
                ],
                tool_usage=usage,
            )
            section = section_for(summary, None, NOW)
            text = "\n".join(
                [section.title]
                + [r.label for r in section.rows if r is not None]
                + [r.label for r in section.accounts]
            )
            lowered = text.lower()
            for word in BANNED_MENU_WORDS:
                assert word not in lowered, (state, name, word, text)
            if name == "bare":
                assert "—" in text
            elif name == "windows":
                assert "1,136" in text  # last-good numbers, kept with no label
            else:
                assert "$0.50" in text and "$4.00" in text  # Go figures, kept


def test_unregistered_opencode_rows_switch_by_vendor_id():
    accounts = [
        (
            AccountView(
                "opencode",
                "anthropic/alt",
                "oauth",
                "anthropic",
                {"account": "alt", "id": "c2"},
                registered=False,
                active=False,
            ),
            UsageResult(UsageState.UNAVAILABLE, "x"),
        )
    ]
    (row,) = section_for(
        ToolSummary("opencode", "OpenCode", installed=True, accounts=accounts), None, NOW
    ).accounts
    assert (row.action, row.selector, row.provider, row.raw) == ("switch", "c2", "anthropic", True)
