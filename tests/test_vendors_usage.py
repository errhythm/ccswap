"""Usage: parsers against real captured output, attribution, staleness, the overview."""

from __future__ import annotations

import datetime
import json
import threading
import time
from types import SimpleNamespace

import pytest

from claude_swap.vendors.core import AccountRecord, RunResult, UsageResult, UsageState, UsageWindow
from claude_swap.vendors.kiro import parse_usage_report
from claude_swap.vendors.legacy import LegacyTool, windows_from_usage
from claude_swap.vendors.muse import parse_allowance
from claude_swap.vendors.opencode import OpenCodeAdapter, summarize
from claude_swap.vendors.overview import Overview, headline, overview_json, window_text
from claude_swap.vendors.registry import ToolRegistry
from claude_swap.vendors.service import VendorService
from claude_swap.vendors.store import VendorStore
from tests.vendor_helpers import FakeRunner, fail, ok

KIRO = "/b/kiro-cli"
MUSE = "/b/muse"
IDC = {
    "accountType": "IamIdentityCenter",
    "startUrl": "https://c.awsapps.com/start",
    "region": "us-east-1",
    "email": "a@c.com",
}
OTHER = {**IDC, "email": "b@c.com"}

# Captured from kiro-cli 3.1.0 (`kiro-cli chat --no-interactive --v2 /usage`), Nov 2026.
KIRO_31 = (
    "\x1b[1mEstimated Usage\x1b[0m | resets on 2026-12-01 | KIRO PRO+\n"
    "Credits (1086.43 of 2000 covered in plan), 54.3%\n"
    "Overages: Disabled\n"
    "Your plan is managed by your organization's administrator.\n"
)
# The older box layout (documented by third-party integrations of earlier releases).
KIRO_BOX = (
    "┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓\n"
    "┃                                          | KIRO FREE        ┃\n"
    "┃ Monthly credits:                                            ┃\n"
    "┃ ████████████████████████ 100% (resets on 01/01)             ┃\n"
    "┃                    (0.00 of 50 covered in plan)             ┃\n"
    "┃ Bonus credits:                                              ┃\n"
    "┃ 12.50/100 credits used, expires in 88 days                  ┃\n"
    "┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛\n"
)
# Captured from Muse Code 1.4.14 (`muse usage --json`), Nov 2026.
MUSE_14 = {
    "tier": "Muse Code High Usage",
    "window": {"usedPercent": 0, "resetsAtMs": 1794221620000},
    "weekly": {"usedPercent": 1, "resetsAtMs": 1794787200000},
    "asOfMs": 1794204422012,
    "source": "mint",
}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("META_API_KEY", raising=False)
    monkeypatch.delenv("KIRO_API_KEY", raising=False)


# ---- Kiro parser ------------------------------------------------------------


def test_kiro_31_report():
    r = parse_usage_report(KIRO_31)
    (w,) = r.windows
    assert r.state is UsageState.AVAILABLE and r.plan == "KIRO PRO+" and r.authoritative
    assert (w.used, w.limit, w.remaining, w.used_percent) == (1086.43, 2000.0, 913.57, 54.3)
    assert w.resets_on == "2026-12-01" and w.resets_at is None  # a date, no invented time
    assert w.scope == "organization-managed" and "Overages: Disabled" in r.hint


def test_kiro_box_report_with_bonus_and_implied_year():
    r = parse_usage_report(KIRO_BOX, today=datetime.date(2026, 11, 9))
    plan, bonus = r.windows
    assert r.plan == "KIRO FREE"
    assert (plan.used, plan.limit, plan.used_percent) == (
        0.0,
        50.0,
        0.0,
    )  # the printed 100% is ignored
    assert plan.resets_on == "2027-01-01"
    assert (bonus.used, bonus.limit, bonus.remaining) == (12.5, 100.0, 87.5)
    assert bonus.note == "expires in 88 days"


def test_kiro_plan_only_report_is_not_zero():
    r = parse_usage_report("Plan: KIRO PRO MAX | 1 usage breakdowns\n")
    assert r.state is UsageState.UNAVAILABLE and r.windows == () and r.plan == "KIRO PRO MAX"


def test_kiro_exhausted_allowance():
    r = parse_usage_report(
        "Estimated Usage | resets on 2026-12-01 | KIRO PRO\nCredits (1000.00 of 1000 covered in plan), 100%\n"
    )
    (w,) = r.windows
    assert w.remaining == 0.0 and w.used_percent == 100.0


@pytest.mark.parametrize("text", ["", "Hello! How can I help?", "Error: something", "\x1b[2J"])
def test_kiro_unrecognised_output_is_rejected(text):
    assert parse_usage_report(text) is None


def kiro_world(tmp_path, who=IDC, report=KIRO_31):
    r = FakeRunner({"kiro-cli": KIRO})
    r.on(KIRO, "--version", result=ok("kiro-cli 3.1.0"))
    r.on(KIRO, "whoami", result=ok(json.dumps(who)) if who else fail("Not logged in"))
    r.on(KIRO, "chat", "--help", result=ok("--no-interactive --v2"))
    r.on(KIRO, "chat", "--no-interactive", "--v2", "/usage", result=ok(report))
    reg = ToolRegistry.default(r)
    reg.get("kiro").probe_dir = tmp_path / "probe"
    return VendorService(reg, VendorStore(tmp_path / "v")), r


def test_kiro_probe_runs_in_fixed_cwd_without_an_ambient_api_key(tmp_path, monkeypatch):
    monkeypatch.setenv("KIRO_API_KEY", "ksk_should_not_be_used_123")
    svc, r = kiro_world(tmp_path)
    svc.usage("kiro")
    call = next(c for c in r.calls if "/usage" in c.argv)
    assert call.cwd == str(tmp_path / "probe") and call.unset_env == ("KIRO_API_KEY",)
    assert (tmp_path / "probe").is_dir()


def test_kiro_tool_usage_is_filed_under_the_signed_in_account_only(tmp_path):
    svc, _ = kiro_world(tmp_path)
    svc.store.add(AccountRecord("kiro", "a", "login", identity=IDC))
    svc.store.add(AccountRecord("kiro", "b", "login", identity=OTHER))
    tool = svc.usage("kiro")
    assert tool.attributed_to == "a"
    assert svc.usage("kiro", "a").windows == tool.windows
    b = svc.usage("kiro", "b")
    assert b.windows == () and "not the signed-in" in b.reason


def test_kiro_inactive_account_shows_its_own_last_observation_as_stale(tmp_path):
    svc, r = kiro_world(tmp_path)
    svc.store.add(AccountRecord("kiro", "a", "login", identity=IDC))
    svc.store.add(AccountRecord("kiro", "b", "login", identity=OTHER))
    first = svc.usage("kiro", "a")  # observed while a is signed in
    r.on(KIRO, "whoami", result=ok(json.dumps(OTHER)))  # user switched to b elsewhere
    r.on(
        KIRO,
        "chat",
        "--no-interactive",
        "--v2",
        "/usage",
        result=ok(KIRO_31.replace("1086.43", "10.00")),
    )
    svc2 = VendorService(svc.registry, svc.store)  # a new process: only the persisted cache
    a = svc2.usage("kiro", "a")
    assert a.state is UsageState.STALE and a.windows == first.windows  # a's own numbers
    assert "not the signed-in" in a.error
    b = svc2.usage("kiro", "b")
    assert b.state is UsageState.AVAILABLE and b.windows[0].used == 10.0  # never a's


def test_kiro_api_key_account_is_measured_in_isolation(tmp_path):
    svc, r = kiro_world(tmp_path)
    svc.store.add(AccountRecord("kiro", "ci", "api-key"), "ksk_abcdef123456")
    svc.usage("kiro", "ci")
    call = next(c for c in r.calls if "/usage" in c.argv)
    assert call.env == {"KIRO_API_KEY": "ksk_abcdef123456"} and call.unset_env == ()
    assert "ksk_abcdef123456" not in " ".join(call.argv)


def test_kiro_signed_out_is_unauthenticated_and_never_probes(tmp_path):
    svc, r = kiro_world(tmp_path, who=None)
    assert svc.usage("kiro").state is UsageState.UNAUTHENTICATED
    assert not any("/usage" in c.argv for c in r.calls)


def test_kiro_probe_failure_is_an_error_with_redacted_detail(tmp_path):
    svc, r = kiro_world(tmp_path)
    r.on(KIRO, "chat", "--no-interactive", "--v2", "/usage", result=RunResult(124, timed_out=True))
    result = svc.usage("kiro")
    assert result.state is UsageState.ERROR and "timed out" in result.reason


# ---- Muse parser ------------------------------------------------------------


def test_muse_14_allowance():
    r = parse_allowance(MUSE_14, attributed_to="active credential", account_scoped=False)
    five, week = r.windows
    assert r.plan == "Muse Code High Usage" and r.observed_at == pytest.approx(1794204422.012)
    assert (five.key, five.used_percent, five.remaining, five.resets_at) == (
        "five-hour",
        0.0,
        100.0,
        1794221620.0,
    )
    assert (week.key, week.used_percent, week.resets_at) == ("weekly", 1.0, 1794787200.0)
    assert five.scope == "active-credential" and five.used is None and five.limit is None


def test_muse_unknown_new_fields_are_kept_not_dropped():
    r = parse_allowance(
        {**MUSE_14, "monthly": {"usedPercent": 3}}, attributed_to=None, account_scoped=True
    )
    assert r.observations and r.observations[0].data == {"monthly": {"usedPercent": 3}}


def test_muse_missing_window_fields_stay_unknown():
    r = parse_allowance({"tier": "x", "window": {}}, attributed_to=None, account_scoped=True)
    (w,) = r.windows
    assert w.used_percent is None and w.resets_at is None and w.remaining is None


def test_muse_allowance_without_windows_is_unavailable():
    assert (
        parse_allowance({"tier": "x"}, attributed_to=None, account_scoped=True).state
        is UsageState.UNAVAILABLE
    )


def test_window_text_never_invents_a_denominator():
    assert (
        window_text(UsageWindow("five-hour", "5-hour window", "percent", used_percent=34.0))
        == "5-hour window 34% used"
    )
    assert window_text(UsageWindow("x", "X", "percent")) == "X: not reported"


# ---- OpenCode -----------------------------------------------------------------


def row(provider, model, n30, c30, nall, call):
    base = {"provider": provider, "model": model, "last": 5}
    for p in ("24h", "7d"):
        base[f"n_{p}"], base[f"cost_{p}"] = 0, 0.0
    base["n_30d"], base["cost_30d"], base["n_all"], base["cost_all"] = n30, c30, nall, call
    base["input_30d"] = 100
    return base


def test_opencode_summary_groups_by_provider_and_sorts():
    out = summarize(
        [
            row("a", "m1", 1, 0.5, 5, 2.0),
            row("b", "x", 9, 1.0, 9, 1.0),
            row("a", "m2", 2, 0.25, 2, 0.25),
            {"bad": 1},
        ]
    )
    assert [p["provider"] for p in out] == ["b", "a"]
    a = out[1]
    assert a["totals"]["30d"] == {
        "messages": 3,
        "costUsd": 0.75,
        "tokens": {"input": 200, "output": 0, "reasoning": 0, "cacheRead": 0, "cacheWrite": 0},
    }
    assert [m["model"] for m in a["models"]] == ["m1", "m2"]


def test_opencode_stats_query_is_read_only_and_avoids_credential_tables():
    from claude_swap.vendors.opencode import _STATS_SQL, _cutoffs

    sql = _STATS_SQL.format(**_cutoffs(time.time())).lower()
    assert sql.startswith("select ") and " from message " in sql
    for word in ("insert", "update", "delete", "drop", "credential", "account", "attach", "pragma"):
        assert word not in sql.replace("json_extract", "")


def test_opencode_per_account_usage_is_never_the_providers_total(tmp_path):
    r = FakeRunner({"opencode": "/b/opencode"})
    r.on("/b/opencode", "--version", result=ok("1.99.0"))
    adapter = OpenCodeAdapter(r)
    rec = AccountRecord("opencode", "w", "provider-account", "anthropic", {"account": "w"})
    result = adapter.usage(rec, None)
    assert result.state is UsageState.UNAVAILABLE and not result.observations


# ---- Service: staleness, coalescing --------------------------------------------


def muse_world(tmp_path, payload):
    r = FakeRunner({"muse": MUSE})
    r.on(MUSE, "--version", result=ok("Muse Code 1.4.14"))
    r.on(MUSE, "usage", "--json", result=payload)
    return VendorService(ToolRegistry.default(r), VendorStore(tmp_path / "v")), r


def test_failed_refresh_returns_last_good_as_stale_with_error(tmp_path):
    svc, r = muse_world(tmp_path, ok(json.dumps({"allowance": MUSE_14})))
    assert svc.usage("muse").state is UsageState.AVAILABLE
    r.on(MUSE, "usage", "--json", result=fail("network unreachable"))
    stale = svc.usage("muse", refresh=True)
    assert stale.state is UsageState.STALE and stale.windows[0].used_percent == 0.0
    assert "network unreachable" in stale.error


def test_no_earlier_data_means_a_plain_error_not_stale(tmp_path):
    svc, _ = muse_world(tmp_path, fail("boom"))
    assert svc.usage("muse").state is UsageState.ERROR


def test_unauthenticated_is_not_masked_by_old_data(tmp_path):
    svc, r = muse_world(tmp_path, ok(json.dumps({"allowance": MUSE_14})))
    svc.usage("muse")
    r.on(MUSE, "usage", "--json", result=fail("muse usage: not logged in"))
    assert svc.usage("muse", refresh=True).state is UsageState.UNAUTHENTICATED


def test_concurrent_requests_share_one_vendor_call(tmp_path):
    calls = []
    gate = threading.Event()

    def slow(call):
        calls.append(1)
        gate.wait(2)
        return ok(json.dumps({"allowance": MUSE_14}))

    svc, _ = muse_world(tmp_path, slow)
    threads = [threading.Thread(target=svc.usage, args=("muse",)) for _ in range(5)]
    [t.start() for t in threads]
    time.sleep(0.2)
    gate.set()
    [t.join() for t in threads]
    assert len(calls) == 1


def test_removing_an_account_drops_its_cached_usage(tmp_path):
    svc, _ = kiro_world(tmp_path)
    svc.store.add(AccountRecord("kiro", "a", "login", identity=IDC))
    svc.usage("kiro", "a")
    assert svc.usage_store.get("kiro", "a") is not None
    svc.remove("kiro", "a")
    assert svc.usage_store.get("kiro", "a") is None


def test_usage_cache_file_holds_no_secrets(tmp_path):
    svc, _ = kiro_world(tmp_path)
    svc.store.add(AccountRecord("kiro", "ci", "api-key"), "ksk_abcdef123456")
    svc.usage("kiro", "ci")
    assert "ksk_abcdef123456" not in (svc.store.root / "usage.json").read_text()


def test_corrupt_usage_cache_is_ignored(tmp_path):
    svc, _ = muse_world(tmp_path, ok(json.dumps({"allowance": MUSE_14})))
    svc.store.root.mkdir(parents=True, exist_ok=True)
    (svc.store.root / "usage.json").write_text("{nope")
    assert svc.usage("muse").state is UsageState.AVAILABLE


# ---- Legacy bridges -------------------------------------------------------------


CLAUDE_USAGE = {
    "five_hour": {"pct": 34.0, "resets_at": "2099-01-01T05:00:00Z"},
    "seven_day": {"pct": 100.0, "resets_at": "2099-01-05T00:00:00+00:00"},
    "scoped": [{"name": "Fable", "pct": 0.0, "resets_at": "2099-01-05T00:00:00Z"}],
    "spend": {"used": 12.5, "limit": 50.0, "pct": 25.0, "currency": "USD"},
}


def test_claude_usage_dict_maps_to_independent_windows():
    windows, _ = windows_from_usage(CLAUDE_USAGE)
    assert [w.key for w in windows] == ["five-hour", "weekly", "scoped:Fable", "spend"]
    assert windows[0].resets_at == datetime.datetime(2099, 1, 1, 5, tzinfo=datetime.UTC).timestamp()
    spend = windows[3]
    assert (spend.unit, spend.used, spend.limit, spend.used_percent) == ("usd", 12.5, 50.0, 25.0)


def test_naive_reset_timestamps_are_not_guessed():
    windows, _ = windows_from_usage({"five_hour": {"pct": 1.0, "resets_at": "2099-01-01T05:00:00"}})
    assert windows[0].resets_at is None


def fake_acc(number, *, active, last_good=None, sentinel=None, age=10.0, email="x@y.z"):
    usage = SimpleNamespace(
        sentinel=sentinel,
        last_good=last_good,
        fetched_at=time.time() - age,
        age_s=age,
        last_error=None,
        trust_extended=False,
    )
    return SimpleNamespace(
        number=str(number),
        email=email,
        org_name="",
        alias="",
        kind="oauth",
        is_active=active,
        disabled=False,
        usage=usage,
    )


class FakeBridge(LegacyTool):
    key, label, binary = "claude", "Claude Code", "claude"

    def __init__(self, accounts):
        super().__init__(lambda: None, FakeRunner({"claude": "/b/claude"}))
        self.runner.on("/b/claude", "--version", result=ok("2.1.0"))
        self._accounts = accounts

    def snapshot(self, *, refresh=False):
        return SimpleNamespace(accounts=self._accounts, taken_at=time.time())


def test_bridge_keeps_per_account_usage_separate_and_flags_states():
    bridge = FakeBridge(
        [
            fake_acc(1, active=False, last_good=CLAUDE_USAGE),
            fake_acc(2, active=True, last_good={"five_hour": {"pct": 2.0}}),
            fake_acc(3, active=False, sentinel="api key"),
            fake_acc(4, active=False, last_good={"five_hour": {"pct": 9.0}}, age=99999),
        ]
    )
    out = bridge.accounts_usage()
    assert [u.windows[0].used_percent if u.windows else None for _, u in out] == [
        34.0,
        2.0,
        None,
        9.0,
    ]
    assert out[2][1].state is UsageState.UNAVAILABLE and "API-key" in out[2][1].reason
    assert out[3][1].state is UsageState.STALE


# ---- Overview ---------------------------------------------------------------------


def test_overview_isolates_failures_and_keeps_order(tmp_path, monkeypatch):
    svc, _ = muse_world(tmp_path, ok(json.dumps({"allowance": MUSE_14})))
    bridge = FakeBridge([fake_acc(1, active=True, last_good=CLAUDE_USAGE)])
    ov = Overview(svc, legacy=[bridge])

    def boom(*a, **k):
        raise RuntimeError("bug")

    monkeypatch.setattr(svc, "accounts_usage", boom)
    summaries = ov.collect(["claude", "muse", "kiro"])
    assert [s.tool for s in summaries] == ["claude", "muse", "kiro"]
    assert headline(summaries[0]) == "5h 34% · wk 100%"
    assert summaries[1].error == "RuntimeError while reading this tool"
    assert headline(summaries[2]) == "not installed"


def test_overview_times_out_a_hung_tool(tmp_path, monkeypatch):
    import claude_swap.vendors.overview as ov_mod

    monkeypatch.setattr(ov_mod, "TOOL_TIMEOUT_S", 0.3)
    gate = threading.Event()

    def hang(call):
        gate.wait(5)
        return ok("{}")

    svc, _ = muse_world(tmp_path, hang)
    bridge = FakeBridge([fake_acc(1, active=True, last_good=CLAUDE_USAGE)])
    started = time.monotonic()
    summaries = Overview(svc, legacy=[bridge]).collect(["claude", "muse"])
    gate.set()
    assert time.monotonic() - started < 3
    assert summaries[1].error == "timed out" and headline(summaries[0]).startswith("5h")


def test_overview_json_contract(tmp_path):
    svc, _ = muse_world(tmp_path, ok(json.dumps({"allowance": MUSE_14})))
    doc = overview_json(Overview(svc, legacy=[]).collect(["muse"]))
    (tool,) = doc["tools"]
    assert doc["schemaVersion"] == 1
    assert {
        "tool",
        "label",
        "installed",
        "version",
        "surface",
        "headline",
        "error",
        "refreshedAt",
        "auth",
        "usage",
        "accounts",
    } <= tool.keys()
    w = tool["usage"]["windows"][0]
    assert {
        "key",
        "label",
        "unit",
        "used",
        "limit",
        "remaining",
        "usedPercent",
        "resetsAt",
        "resetsOn",
        "scope",
        "note",
    } <= w.keys()


def test_future_vendor_appears_without_ui_changes(tmp_path):
    from claude_swap.vendors.core import ToolAdapter, ToolInfo

    class Zed(ToolAdapter):
        key, label, binary = "zed", "Zed Agent", "zed"

        def capabilities(self, info: ToolInfo):
            return {}

        def usage(self, record, secret):
            return UsageResult(UsageState.UNSUPPORTED, "no usage source")

    svc = VendorService(
        ToolRegistry([Zed(FakeRunner({"zed": "/b/zed"}))]), VendorStore(tmp_path / "v")
    )
    svc.adapter("zed").runner.on("/b/zed", "--version", result=ok("0.1.0"))
    ov = Overview(svc, legacy=[])
    assert ov.tools() == ["zed"]
    (s,) = ov.collect()
    assert s.installed and headline(s) == "usage not reported"


# ---- CLI ------------------------------------------------------------------------------


def test_usage_command_json_and_unknown_tool(tmp_path, capsys):
    from claude_swap.vendors.cli import usage_command

    svc, _ = muse_world(tmp_path, ok(json.dumps({"allowance": MUSE_14})))
    ov = Overview(svc, legacy=[])
    assert usage_command(["muse", "--json"], ov) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["tools"][0]["headline"] == "5h 0% · wk 1%"
    assert usage_command(["nope"], ov) == 1
    assert "Unknown tool" in capsys.readouterr().err


def test_usage_command_human_output(tmp_path, capsys):
    from claude_swap.vendors.cli import usage_command

    svc, _ = muse_world(tmp_path, ok(json.dumps({"allowance": MUSE_14})))
    assert usage_command(["muse"], Overview(svc, legacy=[])) == 0
    out = capsys.readouterr().out
    assert "Muse Code" in out and "5-hour window 0% used" in out and "resets" in out


def test_claude_namespace_maps_onto_the_established_commands():
    from claude_swap.vendors.cli import claude_argv

    assert claude_argv(["list", "--json"]) == ["--list", "--provider", "claude", "--json"]
    assert claude_argv(["usage"]) == ["--list", "--provider", "claude"]
    assert claude_argv(["switch", "2"]) == ["switch", "2"]
    assert claude_argv([]) == ["--help"]


def test_tool_usage_subcommand_includes_every_account(tmp_path, capsys):
    from claude_swap.vendors.cli import tool_command

    svc, _ = muse_world(tmp_path, ok(json.dumps({"allowance": MUSE_14})))
    svc.store.add(AccountRecord("muse", "me", "browser"))
    assert tool_command("muse", ["usage", "--json"], svc) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["state"] == "available" and doc["accounts"][0]["alias"] == "me"
    assert doc["accounts"][0]["usage"]["windows"] == []  # not the active credential's


# ---- Regressions from the independent review -------------------------------------


def test_new_credential_never_inherits_the_previous_credentials_numbers(tmp_path):
    svc, r = muse_world(tmp_path, ok(json.dumps({"allowance": MUSE_14})))
    assert svc.usage("muse").windows
    # The user now uses a pay-as-you-go key: a definitive "no allowance", not a failure.
    r.on(MUSE, "usage", "--json", result=ok(json.dumps({"allowance": None, "reason": "api_key"})))
    now = svc.usage("muse", refresh=True)
    assert now.state is UsageState.UNAVAILABLE and now.windows == ()


def test_same_email_on_another_identity_center_is_not_attributed(tmp_path):
    elsewhere = {**IDC, "startUrl": "https://other.awsapps.com/start"}
    svc, _ = kiro_world(tmp_path, who=elsewhere)
    svc.store.add(AccountRecord("kiro", "work", "login", identity=IDC))
    tool = svc.usage("kiro")
    assert tool.attributed_to != "work"
    assert svc.usage_store.get("kiro", "work") is None
    assert svc.usage("kiro", "work").windows == ()


def test_an_alias_that_looks_like_an_email_never_captures_readings(tmp_path):
    svc, _ = kiro_world(tmp_path)
    svc.store.add(AccountRecord("kiro", "a@c.com", "api-key"), "ksk_abcdef123456")
    tool = svc.usage("kiro")
    assert tool.attributed_to == "a@c.com"  # the whoami email, as a label only
    assert svc.usage_store.get("kiro", "a@c.com") is None


def test_login_change_during_the_probe_discards_the_reading(tmp_path):
    svc, r = kiro_world(tmp_path)
    svc.store.add(AccountRecord("kiro", "a", "login", identity=IDC))
    answers = iter([ok(json.dumps(IDC)), ok(json.dumps(OTHER))])  # before, after the probe
    r.on(KIRO, "whoami", result=lambda c: next(answers, ok(json.dumps(OTHER))))
    result = svc.usage("kiro", "a")
    assert result.state is UsageState.UNAVAILABLE and "changed during" in result.reason
    assert svc.usage_store.get("kiro", "a") is None


def test_kiro_31_signed_out_json_is_unauthenticated(tmp_path):
    svc, r = kiro_world(tmp_path)
    r.on(KIRO, "whoami", result=RunResult(1, stdout='{"account":null}\n'))
    assert svc.adapter("kiro").whoami_state() == ("out", None)
    assert svc.usage("kiro").state is UsageState.UNAUTHENTICATED


def test_free_text_mentioning_a_plan_is_not_a_usage_report():
    assert parse_usage_report("Sure! Your Plan: Pro looks fine. Estimated usage is low.") is None


def test_cache_lock_failure_does_not_lose_a_good_reading(tmp_path, monkeypatch):
    from claude_swap.exceptions import LockError
    from claude_swap.vendors import usage_cache

    class Busy:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            raise LockError("busy")

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(usage_cache, "FileLock", Busy)
    svc, _ = muse_world(tmp_path, ok(json.dumps({"allowance": MUSE_14})))
    assert svc.usage("muse").state is UsageState.AVAILABLE


def test_transient_help_failures_are_not_cached(tmp_path):
    svc, r = kiro_world(tmp_path)
    r.on(KIRO, "chat", "--help", result=RunResult(124, timed_out=True))
    assert svc.usage("kiro").state is UsageState.UNSUPPORTED
    r.on(KIRO, "chat", "--help", result=ok("--no-interactive --v2"))
    assert svc.usage("kiro", refresh=True).state is UsageState.AVAILABLE


def test_numbers_beside_a_problem_are_labelled_as_last_reading():
    from claude_swap.vendors.overview import usage_headline

    window = UsageWindow("five-hour", "5-hour window", "percent", used_percent=40.0)
    assert usage_headline(UsageResult(UsageState.UNAUTHENTICATED, "x", windows=(window,))) == (
        "sign-in required · last 5h 40%"
    )
    assert usage_headline(UsageResult(UsageState.STALE, windows=(window,))) == "stale · 5h 40%"


def test_overview_threads_never_block_exit(tmp_path, monkeypatch):
    import claude_swap.vendors.overview as ov_mod

    monkeypatch.setattr(ov_mod, "TOOL_TIMEOUT_S", 0.2)
    gate = threading.Event()
    svc, _ = muse_world(tmp_path, lambda c: (gate.wait(5), ok("{}"))[1])
    Overview(svc, legacy=[]).collect(["muse"])
    stragglers = [t for t in threading.enumerate() if t.name.startswith("ccswap-usage-")]
    assert stragglers and all(t.daemon for t in stragglers)
    gate.set()


def test_removed_account_does_not_keep_its_in_memory_reading(tmp_path):
    svc, _ = kiro_world(tmp_path)
    svc.store.add(AccountRecord("kiro", "a", "login", identity=IDC))
    svc.usage("kiro", "a")
    svc.remove("kiro", "a")
    svc.store.add(AccountRecord("kiro", "a", "login", identity=OTHER))
    assert svc.usage("kiro", "a").windows == ()  # the new 'a' is not signed in


def test_claude_namespace_rejects_other_tools_and_passes_claude_verbs():
    from claude_swap.vendors.cli import claude_argv

    for other in ("codex", "kiro", "muse", "opencode", "agy", "vendors"):
        with pytest.raises(ValueError):
            claude_argv([other, "list"])
    assert claude_argv(["run", "1"]) == ["run", "1"]
    assert claude_argv(["--status"]) == ["--status"]


def test_forced_refresh_reuses_only_this_passes_readings(tmp_path):
    svc, r = kiro_world(tmp_path)
    svc.store.add(AccountRecord("kiro", "a", "login", identity=IDC))
    summary = Overview(svc, legacy=[]).collect_one("kiro", refresh=True)
    probes = [c for c in r.calls if "/usage" in c.argv]
    assert len(probes) == 1  # tool-level probe answered the active account too
    assert summary.accounts[0][1].windows == summary.tool_usage.windows
    svc.usage("kiro", "a", refresh=True)
    assert len([c for c in r.calls if "/usage" in c.argv]) == 2  # a user refresh asks again


def test_unregistered_current_login_gets_the_reading_only_for_its_own_identity(tmp_path):
    svc, _ = kiro_world(tmp_path)
    summary = Overview(svc, legacy=[]).collect_one("kiro")
    (view, usage) = summary.accounts[0]
    assert not view.registered and view.active and usage.windows == summary.tool_usage.windows
    svc2, _ = kiro_world(tmp_path / "x")
    svc2.store.add(AccountRecord("kiro", "b", "login", identity=OTHER))
    rows = {v.alias: u for v, u in Overview(svc2, legacy=[]).collect_one("kiro").accounts}
    assert rows["b"].windows == ()  # a registered, inactive login never gets it


def test_login_change_mid_probe_never_shows_the_previous_logins_numbers(tmp_path):
    svc, r = kiro_world(tmp_path)
    assert svc.usage("kiro").windows  # an earlier reading of login A
    answers = iter([ok(json.dumps(IDC)), ok(json.dumps(OTHER))])
    r.on(KIRO, "whoami", result=lambda c: next(answers, ok(json.dumps(OTHER))))
    now = svc.usage("kiro", refresh=True)
    assert now.state is UsageState.UNAVAILABLE and now.windows == ()


def test_lock_wait_timeout_shows_the_accounts_own_last_reading_as_stale(tmp_path, monkeypatch):
    monkeypatch.setattr("claude_swap.vendors.service.LOCK_WAIT_S", 0.05)
    svc, _ = muse_world(tmp_path, ok(json.dumps({"allowance": MUSE_14})))
    svc.usage("muse")
    lock = svc._key_lock(("muse", "@active"))
    lock.acquire()
    try:
        result = svc.usage("muse", refresh=True)
    finally:
        lock.release()
    assert result.state is UsageState.STALE and "still running" in result.error and result.windows


def test_a_late_reading_never_resurrects_a_removed_account(tmp_path):
    svc, _ = kiro_world(tmp_path)
    rec = AccountRecord("kiro", "ci", "api-key")
    svc.store.add(rec, "ksk_abcdef123456")
    svc.remove("kiro", "ci")
    fresh = parse_usage_report(KIRO_31)
    svc._settle(svc.adapter("kiro"), "kiro", "ci", fresh)
    assert svc.usage_store.get("kiro", "ci") is None


def test_claude_namespace_keeps_every_established_verb():
    from claude_swap.vendors.cli import claude_argv

    for verb in ("tui", "watch", "config", "menubar", "upgrade", "update", "status", "export"):
        assert claude_argv([verb]) == [verb]
