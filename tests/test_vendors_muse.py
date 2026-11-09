from __future__ import annotations

import json

import pytest

from claude_swap.vendors.core import (
    AccountRecord,
    RunResult,
    UnsupportedCapability,
    UsageState,
    VendorError,
)
from claude_swap.vendors.muse import ENV_KEY, MuseAdapter
from tests.vendor_helpers import FakeRunner, fail, ok

KEY = "mk_live_0123456789abcdef"
MUSE = "/usr/local/bin/muse"


@pytest.fixture(autouse=True)
def _no_env_key(monkeypatch):
    monkeypatch.delenv(ENV_KEY, raising=False)


@pytest.fixture
def runner():
    r = FakeRunner({"muse": MUSE})
    r.on(MUSE, "--version", result=ok("Muse Code 1.4.14 (1.4.14-R7309.1)\n"))
    return r


@pytest.fixture
def adapter(runner):
    return MuseAdapter(runner)


def api_record(alias="work"):
    return AccountRecord("muse", alias, "api-key", secret_ref="muse-" + "0" * 32)


def test_detect_installed_and_version(adapter):
    info = adapter.detect()
    assert (info.installed, info.version, info.path) == (True, "1.4.14", MUSE)


def test_detect_missing():
    info = MuseAdapter(FakeRunner()).detect()
    assert not info.installed and info.version is None
    with pytest.raises(VendorError, match="not installed"):
        MuseAdapter(FakeRunner()).require_installed()


def test_capabilities_are_honest(adapter):
    caps = adapter.capabilities(adapter.detect())
    by = {c.value: s.support.value for c, s in caps.items()}
    assert by["account-enumeration"] == "unsupported"
    assert by["auto-switch"] == "unsupported"
    assert by["switch"] == "conditional" and by["usage"] == "supported"


def test_register_api_key_validates(adapter):
    record, secret = adapter.register("w", kind="api-key", api_key=f"  {KEY}\n")
    assert secret == KEY and record.kind == "api-key"
    for bad in ("", "short", "has space inside1"):
        with pytest.raises(VendorError):
            adapter.register("w", kind="api-key", api_key=bad)


def test_register_browser_has_no_secret(adapter):
    record, secret = adapter.register("me", kind="browser")
    assert secret is None and record.kind == "browser"


def test_switch_api_key_sends_secret_on_stdin_only(adapter, runner):
    runner.on(MUSE, "auth", "set", result=ok())
    outcome = adapter.switch(api_record(), KEY, assume_yes=True, interactive=False)
    call = runner.calls[-1]
    assert call.argv == (MUSE, "auth", "set", "--provider", "meta", "--api-key-stdin")
    assert call.input_text == KEY + "\n"
    assert KEY not in " ".join(call.argv) and KEY not in repr(call.env)
    assert outcome.restart_required and outcome.verified is False


def test_switch_requires_confirmation(adapter, runner):
    with pytest.raises(VendorError, match="--yes"):
        adapter.switch(api_record(), KEY, assume_yes=False, interactive=False)
    assert not any(c.argv[1:3] == ("auth", "set") for c in runner.calls)


def test_switch_failure_never_leaks_key(adapter, runner):
    runner.on(MUSE, "auth", "set", result=fail(f"rejected {KEY}"))
    with pytest.raises(VendorError) as exc:
        adapter.switch(api_record(), KEY, assume_yes=True, interactive=False)
    assert KEY not in str(exc.value)


def test_switch_warns_when_env_key_overrides(adapter, runner, monkeypatch):
    monkeypatch.setenv(ENV_KEY, "other-key-12345678")
    runner.on(MUSE, "auth", "set", result=ok())
    outcome = adapter.switch(api_record(), KEY, assume_yes=True, interactive=False)
    assert any("META_API_KEY" in w for w in outcome.warnings)


def test_browser_switch_is_unsupported_non_interactively(adapter):
    with pytest.raises(UnsupportedCapability, match="muse login"):
        adapter.switch(
            AccountRecord("muse", "b", "browser"), None, assume_yes=True, interactive=False
        )


def test_browser_switch_interactive_runs_login_and_admits_no_verification(adapter, runner):
    runner.on_interactive(MUSE, "login", code=0)
    outcome = adapter.switch(
        AccountRecord("muse", "b", "browser"), None, assume_yes=False, interactive=True
    )
    assert outcome.verified is False and "cannot verify" in outcome.message


def test_browser_login_failure_propagates(adapter, runner):
    runner.on_interactive(MUSE, "login", code=1)
    with pytest.raises(VendorError, match="status 1"):
        adapter.switch(
            AccountRecord("muse", "b", "browser"), None, assume_yes=False, interactive=True
        )


def test_usage_null_allowance_is_unavailable_not_zero(adapter, runner):
    runner.on(
        MUSE,
        "usage",
        "--json",
        result=ok(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "allowance": None,
                    "reason": "server_sent_no_usage",
                    "hint": "wait",
                }
            )
        ),
    )
    result = adapter.usage(None, None)
    assert (
        result.state is UsageState.UNAVAILABLE
        and result.windows == ()
        and result.observations == ()
    )
    assert "no allowance data" in result.reason


def test_usage_api_key_reason_is_explained(adapter, runner):
    runner.on(MUSE, "usage", "--json", result=ok('{"allowance": null, "reason": "api_key"}'))
    result = adapter.usage(api_record(), KEY)
    assert result.state is UsageState.UNAVAILABLE and "pay-as-you-go" in result.reason


def test_usage_not_logged_in_is_unauthenticated(adapter, runner):
    runner.on(
        MUSE, "usage", "--json", result=fail("muse usage: not logged in: run `muse login` first")
    )
    assert adapter.usage(None, None).state is UsageState.UNAUTHENTICATED


def test_usage_available_passes_vendor_data_through_uninterpreted(adapter, runner):
    runner.on(
        MUSE,
        "usage",
        "--json",
        result=ok(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "allowance": {"used": 3, "limit": 10, "resetsAt": "2026-11-09T12:00:00Z"},
                }
            )
        ),
    )
    result = adapter.usage(None, None)
    (obs,) = result.observations
    assert obs.authoritative and obs.kind.value == "subscription-allowance"
    assert obs.data == {"used": 3, "limit": 10, "resetsAt": "2026-11-09T12:00:00Z"}
    assert "percent" not in json.dumps(obs.to_json())


@pytest.mark.parametrize("stdout", ["not json", "[]", ""])
def test_usage_malformed_is_an_error_not_zero(adapter, runner, stdout):
    runner.on(MUSE, "usage", "--json", result=ok(stdout))
    result = adapter.usage(None, None)
    assert result.state is UsageState.ERROR and result.windows == ()


def test_usage_timeout_and_failure_isolated(adapter, runner):
    runner.on(MUSE, "usage", "--json", result=RunResult(124, timed_out=True))
    assert "timed out" in adapter.usage(None, None).reason
    runner.on(MUSE, "usage", "--json", result=fail(f"401 {KEY}"))
    assert KEY not in adapter.usage(api_record(), KEY).reason


def test_usage_for_api_key_account_injects_env_not_argv(adapter, runner):
    runner.on(MUSE, "usage", "--json", result=ok('{"allowance": null}'))
    adapter.usage(api_record(), KEY)
    call = runner.calls[-1]
    assert call.env == {ENV_KEY: KEY} and KEY not in " ".join(call.argv)


def test_launch_spec_isolates_via_env(adapter):
    spec = adapter.launch_spec(api_record(), KEY, ["exec", "hi"])
    assert spec.argv == (MUSE, "exec", "hi") and spec.env_overlay == {ENV_KEY: KEY}


def test_launch_spec_rejects_browser(adapter):
    with pytest.raises(UnsupportedCapability):
        adapter.launch_spec(AccountRecord("muse", "b", "browser"), None, [])


def test_auth_summary_never_contains_key_value(adapter, monkeypatch):
    monkeypatch.setenv(ENV_KEY, KEY)
    summary = adapter.auth_summary()
    assert summary["envKeyPresent"] is True and KEY not in json.dumps(summary)


def test_observe_flags_env_override(adapter, monkeypatch):
    monkeypatch.setenv(ENV_KEY, KEY)
    (view,) = adapter.observe([api_record()], "work")
    assert view.active is None  # muse cannot say which credential is in use
    assert "overrides" in view.note and "unconfirmed" in view.note


def test_browser_alias_usage_is_not_attributed_to_the_ambient_session(adapter, runner):
    result = adapter.usage(AccountRecord("muse", "b", "browser"), None)
    assert result.state is UsageState.UNAVAILABLE and result.windows == ()
    assert not any(c.argv[1:2] == ("usage",) for c in runner.calls)


def test_unselected_usage_is_labelled_as_the_active_credential(adapter, runner):
    runner.on(MUSE, "usage", "--json", result=ok(json.dumps({"allowance": {"used": 1}})))
    (obs,) = adapter.usage(None, None).observations
    assert obs.scope == "active-credential"


def test_browser_login_does_not_become_the_recorded_active_account(adapter, runner):
    runner.on_interactive(MUSE, "login", code=0)
    outcome = adapter.switch(
        AccountRecord("muse", "b", "browser"), None, assume_yes=False, interactive=True
    )
    assert outcome.activates is False


# ---- Browser logins identified by the email Muse records for them --------------------


def write_login(tmp_path, monkeypatch, email):
    cfg = tmp_path / "cfg"
    (cfg / "muse").mkdir(parents=True, exist_ok=True)
    (cfg / "muse" / "auth.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "providers": {
                    "meta": {
                        "access_token": "dca:SECRET-TOKEN-VALUE-0123456789",
                        "user_email": email,
                        "mechanism": "oauth",
                    }
                },
            }
        )
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg))


def test_live_email_reads_only_the_email(adapter, tmp_path, monkeypatch):
    write_login(tmp_path, monkeypatch, "j@x.com")
    assert adapter.live_email() == "j@x.com"
    summary = json.dumps(adapter.auth_summary())
    assert "SECRET-TOKEN" not in summary


def test_two_browser_logins_are_told_apart(adapter, tmp_path, monkeypatch):
    write_login(tmp_path, monkeypatch, "j@x.com")
    j, _ = adapter.register("jay", kind="browser")
    write_login(tmp_path, monkeypatch, "m@x.com")
    m, _ = adapter.register("mis", kind="browser")
    assert (j.identity, m.identity) == ({"email": "j@x.com"}, {"email": "m@x.com"})
    assert {v.alias: v.active for v in adapter.observe([j, m])} == {"jay": False, "mis": True}


def test_env_key_makes_browser_activity_unknown(adapter, tmp_path, monkeypatch):
    write_login(tmp_path, monkeypatch, "j@x.com")
    j, _ = adapter.register("jay", kind="browser")
    monkeypatch.setenv(ENV_KEY, KEY)
    assert adapter.observe([j])[0].active is None


def test_browser_usage_is_attributed_by_email_and_never_to_the_other_login(
    tmp_path, monkeypatch, runner
):
    from claude_swap.vendors.registry import ToolRegistry
    from claude_swap.vendors.service import VendorService
    from claude_swap.vendors.store import VendorStore

    allowance = {
        "tier": "High",
        "window": {"usedPercent": 40, "resetsAtMs": 4102444800000},
        "weekly": {"usedPercent": 9, "resetsAtMs": 4103049600000},
        "asOfMs": 4102400000000,
    }
    runner.on(MUSE, "usage", "--json", result=ok(json.dumps({"allowance": allowance})))
    svc = VendorService(ToolRegistry.default(runner), VendorStore(tmp_path / "v"))
    adapter = svc.adapter("muse")
    write_login(tmp_path, monkeypatch, "j@x.com")
    svc.store.add(adapter.register("jay", kind="browser")[0])
    write_login(tmp_path, monkeypatch, "m@x.com")
    svc.store.add(adapter.register("mis", kind="browser")[0])
    tool = svc.usage("muse")
    assert tool.attributed_to == "mis" and svc.usage("muse", "mis").windows == tool.windows
    jay = svc.usage("muse", "jay")
    assert jay.windows == () and "not the signed-in Muse login" in jay.reason
    # jay signs in later: its own reading, mis keeps its own (stale)
    write_login(tmp_path, monkeypatch, "j@x.com")
    svc2 = VendorService(svc.registry, svc.store)
    assert svc2.usage("muse").attributed_to == "jay"
    assert svc2.usage("muse", "mis").state.value == "stale"


def test_login_change_during_the_muse_probe_is_not_recorded(tmp_path, monkeypatch, runner, adapter):
    write_login(tmp_path, monkeypatch, "j@x.com")

    def swap_during_probe(call):
        write_login(tmp_path, monkeypatch, "m@x.com")
        return ok(json.dumps({"allowance": {"tier": "x", "window": {"usedPercent": 1}}}))

    runner.on(MUSE, "usage", "--json", result=swap_during_probe)
    result = adapter.usage(None, None)
    assert result.state is UsageState.UNAVAILABLE and "changed during" in result.reason
