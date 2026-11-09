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
    assert by["switch"] == "supported" and by["usage"] == "supported"


def test_register_api_key_validates(adapter):
    record, secret = adapter.register("w", kind="api-key", api_key=f"  {KEY}\n")
    assert secret == KEY and record.kind == "api-key"
    for bad in ("", "short", "has space inside1"):
        with pytest.raises(VendorError):
            adapter.register("w", kind="api-key", api_key=bad)


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


# ---- Switching browser logins inside ccswap (login custody) --------------------------


def test_register_browser_keeps_the_login_for_switching(adapter, tmp_path, monkeypatch):
    write_login(tmp_path, monkeypatch, "j@x.com")
    record, secret = adapter.register("jay", kind="browser")
    assert record.identity == {"email": "j@x.com"} and "SECRET-TOKEN" in secret
    assert "SECRET-TOKEN" not in json.dumps(record.to_json())  # metadata never holds it


def test_register_browser_from_a_saved_login_file(adapter, tmp_path, monkeypatch):
    write_login(tmp_path, monkeypatch, "j@x.com")
    saved = tmp_path / "other.json"
    saved.write_text(
        json.dumps({"providers": {"meta": {"access_token": "t2", "user_email": "m@x.com"}}})
    )
    record, secret = adapter.register("mis", kind="browser", from_file=str(saved))
    assert record.identity == {"email": "m@x.com"} and '"t2"' in secret
    bad = tmp_path / "bad.json"
    bad.write_text("{}")
    with pytest.raises(VendorError, match="not a Muse login"):
        adapter.register("x", kind="browser", from_file=str(bad))


def muse_switch_world(tmp_path, monkeypatch, runner):
    from claude_swap.vendors.registry import ToolRegistry
    from claude_swap.vendors.service import VendorService
    from claude_swap.vendors.store import VendorStore

    runner.on(
        MUSE, "usage", "--json", result=ok('{"allowance": null, "reason": "server_sent_no_usage"}')
    )
    svc = VendorService(ToolRegistry.default(runner), VendorStore(tmp_path / "v"))
    a = svc.adapter("muse")
    write_login(tmp_path, monkeypatch, "j@x.com")
    svc.add("muse", "jay", kind="browser")
    write_login(tmp_path, monkeypatch, "m@x.com")
    svc.add("muse", "mis", kind="browser")
    return svc, a


def live_doc(tmp_path):
    return json.loads((tmp_path / "cfg" / "muse" / "auth.json").read_text())["providers"]["meta"]


def test_browser_switch_swaps_the_login_and_verifies(tmp_path, monkeypatch, runner):
    svc, _ = muse_switch_world(tmp_path, monkeypatch, runner)
    outcome, _ = svc.switch("muse", "jay")
    assert outcome.verified and live_doc(tmp_path)["user_email"] == "j@x.com"
    import stat

    mode = stat.S_IMODE((tmp_path / "cfg" / "muse" / "auth.json").stat().st_mode)
    assert mode == 0o600
    assert {v.alias: v.active for v in svc.list_views("muse")} == {"jay": True, "mis": False}
    assert svc.switch("muse", "jay")[0].message.endswith("already the signed-in Muse login")


def test_outgoing_login_is_saved_back_before_switching(tmp_path, monkeypatch, runner):
    svc, _ = muse_switch_world(tmp_path, monkeypatch, runner)
    # Muse refreshed mis's token in place since it was registered
    path = tmp_path / "cfg" / "muse" / "auth.json"
    doc = json.loads(path.read_text())
    doc["providers"]["meta"]["access_token"] = "REFRESHED"
    path.write_text(json.dumps(doc))
    svc.switch("muse", "jay")
    svc.switch("muse", "mis")
    assert live_doc(tmp_path)["access_token"] == "REFRESHED"


def test_unregistered_live_login_is_never_overwritten(tmp_path, monkeypatch, runner):
    svc, _ = muse_switch_world(tmp_path, monkeypatch, runner)
    write_login(tmp_path, monkeypatch, "stranger@x.com")
    with pytest.raises(VendorError, match="not registered"):
        svc.switch("muse", "jay")
    assert live_doc(tmp_path)["user_email"] == "stranger@x.com"


def test_rejected_login_is_rolled_back(tmp_path, monkeypatch, runner):
    svc, _ = muse_switch_world(tmp_path, monkeypatch, runner)
    runner.on(MUSE, "usage", "--json", result=fail("muse usage: not logged in"))
    with pytest.raises(VendorError, match="previous login was restored"):
        svc.switch("muse", "jay")
    assert live_doc(tmp_path)["user_email"] == "m@x.com"


def test_symlinked_login_file_is_refused(tmp_path, monkeypatch, runner):
    svc, _ = muse_switch_world(tmp_path, monkeypatch, runner)
    path = tmp_path / "cfg" / "muse" / "auth.json"
    target = tmp_path / "elsewhere.json"
    target.write_text(path.read_text())
    path.unlink()
    path.symlink_to(target)
    with pytest.raises(VendorError):
        svc.switch("muse", "jay")
    assert json.loads(target.read_text())["providers"]["meta"]["user_email"] == "m@x.com"
