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
    assert by["switch"] == "conditional" and by["usage"] == "conditional"


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
        adapter.switch(AccountRecord("muse", "b", "browser"), None, assume_yes=True, interactive=False)


def test_browser_switch_interactive_runs_login_and_admits_no_verification(adapter, runner):
    runner.on_interactive(MUSE, "login", code=0)
    outcome = adapter.switch(AccountRecord("muse", "b", "browser"), None, assume_yes=False, interactive=True)
    assert outcome.verified is False and "cannot verify" in outcome.message


def test_browser_login_failure_propagates(adapter, runner):
    runner.on_interactive(MUSE, "login", code=1)
    with pytest.raises(VendorError, match="status 1"):
        adapter.switch(AccountRecord("muse", "b", "browser"), None, assume_yes=False, interactive=True)


def test_usage_null_allowance_is_unavailable_not_zero(adapter, runner):
    runner.on(MUSE, "usage", "--json", result=ok(json.dumps(
        {"schemaVersion": 1, "allowance": None, "reason": "server_sent_no_usage", "hint": "wait"})))
    result = adapter.usage(None, None)
    assert result.state is UsageState.UNAVAILABLE and result.observations == ()
    assert result.reason == "server_sent_no_usage"


def test_usage_available_passes_vendor_data_through_uninterpreted(adapter, runner):
    runner.on(MUSE, "usage", "--json", result=ok(json.dumps(
        {"schemaVersion": 1, "allowance": {"used": 3, "limit": 10, "resetsAt": "2026-11-09T12:00:00Z"}})))
    result = adapter.usage(None, None)
    (obs,) = result.observations
    assert obs.authoritative and obs.kind.value == "subscription-allowance"
    assert obs.data == {"used": 3, "limit": 10, "resetsAt": "2026-11-09T12:00:00Z"}
    assert "percent" not in json.dumps(obs.to_json())


@pytest.mark.parametrize("stdout", ["not json", "[]", ""])
def test_usage_malformed_is_unavailable(adapter, runner, stdout):
    runner.on(MUSE, "usage", "--json", result=ok(stdout))
    assert adapter.usage(None, None).state is UsageState.UNAVAILABLE


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
    assert view.active is True and "overrides" in view.note
