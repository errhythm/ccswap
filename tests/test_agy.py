"""Synthetic custody and CLI tests, with no real Antigravity process or store.

Failure cases include malformed roster/auth/cache, unsafe slots and backends,
write denial/timeout/uncertainty, external writers, lost refreshed credentials,
malformed quota groups, expired access tokens, secret diagnostics, combined JSON,
and a dashboard selecting the wrong provider or inventing a weekly pace for 5h.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from claude_swap import agy, cli, macos_keychain


def auth(email="one@example.com", **extra):
    return {
        "auth_method": "consumer", "id_token_email": email,
        "token": {"access_token": "synthetic-access", "refresh_token": "synthetic-refresh",
                  "expiry": "2000-01-01T00:00:00Z"},
        "writer_id": "synthetic-writer", "future_field": {"preserve": True}, **extra,
    }


def quota():
    return {"status": "SUCCESS", "command": {"name": "usage", "data": {"groups": [
        {"name": name, "description": "Synthetic quota", "buckets": [
            {"id": f"{name}-{window}", "name": f"{window} limit remaining", "window": window,
             "remaining_fraction": 0.75, "reset_time": "2030-01-01T00:00:00Z"}
            for window in windows]}
        for name, windows in (("Gemini Models", ("weekly", "5h")),
                              ("Claude and GPT models", ("weekly", "5h")),
                              ("Gemini 4 Argon", ("weekly",)))
    ]}}}


@pytest.fixture
def provider(monkeypatch, block_real_keychain):
    for key in (*agy._SSH_KEYS, "ANTIGRAVITY_GEMINI_DIR"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(agy.sys, "platform", "darwin")
    monkeypatch.setattr(agy.shutil, "which", lambda name: "/synthetic/agy")

    def usage(argv, **kwargs):
        assert argv == ["/synthetic/agy", "-p", "/usage", "--output-format", "json"]
        return SimpleNamespace(returncode=0, stdout=json.dumps(quota()), stderr="")

    monkeypatch.setattr(agy.spawn, "run", usage)
    return agy.AgyAccountSwitcher()


def native(document):
    macos_keychain.set_password("gemini", "antigravity", json.dumps(document))


def read_native():
    return json.loads(macos_keychain.get_password("gemini", "antigravity"))


def two_accounts(provider):
    native(auth())
    provider.add_account()
    native(auth("two@example.com"))
    provider.add_account()


def test_capture_switch_preserves_full_wrappers_and_unknown_outgoing(provider):
    two_accounts(provider)
    outgoing = auth("unknown@example.com", future_field={"revision": 3})
    native(outgoing)
    result = provider.switch_to("1", json_output=True)
    assert result["to"] == {"number": 1, "email": "one@example.com"}
    assert read_native() == auth()
    assert json.loads(provider._credential_path("3").read_text()) == outgoing
    assert provider.status()["activeAccountNumber"] == 1
    assert provider.backup_dir.stat().st_mode & 0o777 == 0o700
    for path in provider.backup_dir.iterdir():
        assert path.stat().st_mode & 0o777 == 0o600


def test_remove_deletes_only_backup_and_leaves_native_login(provider):
    native(auth())
    provider.add_account()
    provider.remove_account("1", assume_yes=True)
    assert not provider._credential_path("1").exists()
    assert read_native() == auth()
    assert provider.list_payload()["accounts"] == []


@pytest.mark.parametrize("bad", [
    {"version": 1, "accounts": {"../native": {}}},
    {"version": 1, "accounts": {"²": {}}},
    {"version": 2, "accounts": {}},
    {"version": True, "accounts": {}},
    {"version": 1, "accounts": []},
])
def test_corrupt_roster_fails_closed(provider, bad):
    provider._write(provider.sequence_file, bad)
    with pytest.raises(agy.AgyError, match="roster"):
        provider.list_payload()


@pytest.mark.parametrize("changes", [
    {"auth_method": "gcp"}, {"auth_method": "oauth-personal"},
    {"wif_provider": "synthetic-workforce"}, {"project_id": "synthetic-project"},
    {"token": {"access_token": "synthetic-only-access"}},
    {"id_token_email": "bad\x1b[2J@example.com"},
])
def test_unsupported_auth_cannot_mutate(provider, changes):
    native(auth(**changes))
    with pytest.raises(agy.AgyError):
        provider.add_account()
    assert not provider.sequence_file.exists()


def test_empty_inventory_cross_platform_never_reads_native(provider, monkeypatch):
    monkeypatch.setattr(agy.sys, "platform", "win32")
    monkeypatch.setattr(macos_keychain, "get_password", lambda *_: pytest.fail("native read"))
    assert provider.list_payload()["accounts"] == []
    assert provider.accounts_snapshot().accounts == ()
    assert provider.status()["activeAccountNumber"] is None


def test_unsupported_platform_mutation_fails(provider, monkeypatch):
    monkeypatch.setattr(agy.sys, "platform", "linux")
    with pytest.raises(agy.AgyError, match="macOS"):
        provider.add_account()


@pytest.mark.parametrize("kind", ["fallback", "marker", "override", "denied"])
def test_ambiguous_or_failed_native_backend_fails_closed(provider, monkeypatch, kind):
    two_accounts(provider)
    before = read_native()
    if kind == "fallback":
        provider._write(Path.home() / agy._RELATIVE_AUTH, auth())
    elif kind == "marker":
        marker = Path.home() / agy._RELATIVE_AUTH.parent / "cache/antigravity-keyring-unavailable"
        agy._private_dir(marker.parent)
        marker.touch()
    elif kind == "override":
        monkeypatch.setenv("ANTIGRAVITY_GEMINI_DIR", "/synthetic/root")
    else:
        def denied(*_):
            raise macos_keychain.KeychainError("synthetic-refresh secret must stay private")
        monkeypatch.setattr(macos_keychain, "get_password", denied)
    with pytest.raises(agy.AgyError) as raised:
        provider.switch_to("1")
    assert "synthetic-refresh" not in str(raised.value)
    if kind != "denied":
        assert read_native() == before


def test_oversized_target_rejected_before_native_write(provider, monkeypatch):
    native(auth(future_field="s" * 3000))
    provider.add_account()
    native(auth("two@example.com"))
    monkeypatch.setattr(macos_keychain, "set_password", lambda *_: pytest.fail("unsafe write"))
    with pytest.raises(agy.AgyError, match="safe Keychain"):
        provider.switch_to("1")
    assert read_native()["id_token_email"] == "two@example.com"


@pytest.mark.parametrize("write_kind", ["deny", "noop", "timeout_after_write", "external"])
def test_write_outcomes_read_back_without_overwriting_external_state(provider, monkeypatch, write_kind):
    two_accounts(provider)
    original_set = macos_keychain.set_password
    writes = []

    def write(service, account, value):
        writes.append(value)
        if write_kind == "deny":
            raise macos_keychain.KeychainError("synthetic-refresh")
        if write_kind == "timeout_after_write":
            original_set(service, account, value)
            raise macos_keychain.KeychainError("synthetic-refresh")
        if write_kind == "external":
            original_set(service, account, json.dumps(auth("external@example.com")))

    monkeypatch.setattr(macos_keychain, "set_password", write)
    if write_kind == "timeout_after_write":
        provider.switch_to("1")
        assert read_native() == auth()
    else:
        with pytest.raises(agy.AgyError) as raised:
            provider.switch_to("1")
        assert "synthetic-refresh" not in str(raised.value)
        expected = "external@example.com" if write_kind == "external" else "two@example.com"
        assert read_native()["id_token_email"] == expected
    assert len(writes) == 1


def test_external_change_before_activation_is_preserved(provider, monkeypatch):
    two_accounts(provider)
    original_capture = provider._capture

    def capture(*args):
        number = original_capture(*args)
        native(auth("external@example.com"))
        return number

    monkeypatch.setattr(provider, "_capture", capture)
    with pytest.raises(agy.AgyError, match="externally"):
        provider.switch_to("1")
    assert read_native()["id_token_email"] == "external@example.com"


def test_scoped_usage_preserves_every_bucket_and_accepts_expired_refreshable_auth(provider):
    two_accounts(provider)
    payload = provider.usage_status(json_output=True)
    for account in payload["accounts"]:
        assert account["error"] is None
        assert len(account["usage"]["scoped"]) == 5
        assert set(account["usage"]) == {"scoped"}
        assert account["usage"]["scoped"][1]["pct"] == 25
        assert account["usage"]["scoped"][1]["window"] == "5h"


@pytest.mark.parametrize("failure", ["exit", "timeout", "malformed"])
def test_inactive_refresh_persists_even_on_usage_failure(provider, monkeypatch, failure):
    two_accounts(provider)
    refreshed = auth()
    refreshed["token"]["refresh_token"] = "synthetic-rotated"
    monkeypatch.setenv("ANTIGRAVITY_API_KEY", "synthetic-must-not-inherit")

    def run(argv, **kwargs):
        env = kwargs["env"]
        assert "ANTIGRAVITY_API_KEY" not in env
        assert env["SSH_TTY"] == "/dev/ccswap-usage"
        assert kwargs["timeout"] == 45
        assert kwargs["stdin"] == subprocess.DEVNULL
        home = Path(env["HOME"])
        assert home == kwargs["cwd"]
        provider._write(home / agy._RELATIVE_AUTH, refreshed)
        if failure == "timeout":
            raise subprocess.TimeoutExpired(argv, 45, stderr="synthetic-refresh")
        return SimpleNamespace(returncode=1 if failure == "exit" else 0,
                               stdout="bad-json", stderr="synthetic-refresh")

    monkeypatch.setattr(agy.spawn, "run", run)
    with pytest.raises(agy.AgyError) as raised:
        provider._invoke_usage("1", agy.Credential.parse(auth()), False)
    assert "synthetic-refresh" not in str(raised.value)
    assert json.loads(provider._credential_path("1").read_text()) == refreshed
    assert read_native()["id_token_email"] == "two@example.com"


def test_active_usage_recaptures_native_refresh(provider, monkeypatch):
    native(auth())
    provider.add_account()
    refreshed = auth()
    refreshed["token"]["access_token"] = "synthetic-new-access"

    def run(*args, **kwargs):
        assert kwargs["env"]["HOME"] == str(Path.home())
        assert "SSH_TTY" not in kwargs["env"]
        native(refreshed)
        return SimpleNamespace(returncode=0, stdout=json.dumps(quota()))

    monkeypatch.setattr(agy.spawn, "run", run)
    provider.usage_status()
    assert json.loads(provider._credential_path("1").read_text()) == refreshed


def test_saved_refresh_conflict_keeps_both_versions(provider, monkeypatch):
    two_accounts(provider)
    external = auth(future_field="external-write")
    refreshed = auth(future_field="refreshed")

    def run(*args, **kwargs):
        provider._write(provider._credential_path("1"), external)
        provider._write(Path(kwargs["env"]["HOME"]) / agy._RELATIVE_AUTH, refreshed)
        return SimpleNamespace(returncode=0, stdout=json.dumps(quota()))

    monkeypatch.setattr(agy.spawn, "run", run)
    with pytest.raises(agy.AgyError, match="recovery"):
        provider._invoke_usage("1", agy.Credential.parse(auth()), False)
    assert json.loads(provider._credential_path("1").read_text()) == external
    recovery = list(provider.backup_dir.glob("refresh-conflict-1-*.json"))
    assert len(recovery) == 1
    assert json.loads(recovery[0].read_text()) == refreshed


@pytest.mark.parametrize("failure", ["missing", "malformed", "write"])
def test_failed_refresh_persistence_retains_private_child_state(provider, monkeypatch, tmp_path, failure):
    two_accounts(provider)
    monkeypatch.setattr(agy.tempfile, "tempdir", str(tmp_path))
    refreshed = auth()
    refreshed["token"]["refresh_token"] = "synthetic-recovery-only"
    child_files = []
    saved_path = provider._credential_path("1")
    write = provider._write

    def run(*args, **kwargs):
        child_file = Path(kwargs["env"]["HOME"]) / agy._RELATIVE_AUTH
        child_files.append(child_file)
        write(child_file, refreshed)
        if failure == "missing":
            saved_path.unlink()
        elif failure == "malformed":
            saved_path.write_text("{")
        else:
            def fail_saved_write(path, document):
                if path == saved_path:
                    raise agy.AgyError("Cannot save Antigravity account data")
                return write(path, document)
            monkeypatch.setattr(provider, "_write", fail_saved_write)
        return SimpleNamespace(returncode=0, stdout=json.dumps(quota()))

    monkeypatch.setattr(agy.spawn, "run", run)
    with pytest.raises(agy.AgyError) as raised:
        provider._invoke_usage("1", agy.Credential.parse(auth()), False)
    assert child_files[0].exists(), "the only refreshed credential copy must survive"
    assert json.loads(child_files[0].read_text()) == refreshed
    assert child_files[0].stat().st_mode & 0o777 == 0o600
    assert str(child_files[0]) in str(raised.value)
    assert "synthetic-recovery-only" not in str(raised.value)
    assert read_native()["id_token_email"] == "two@example.com"


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable PATH lookup")
def test_usage_resolves_relative_path_before_changing_directory(provider, monkeypatch, tmp_path):
    two_accounts(provider)
    executable = tmp_path / "bin" / "agy"
    executable.parent.mkdir()
    executable.write_text(f"#!{sys.executable}\nprint({json.dumps(quota())!r})\n")
    executable.chmod(0o700)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(agy.shutil, "which", lambda name: "bin/agy")
    monkeypatch.setattr(agy.spawn, "run", subprocess.run)
    result = provider._invoke_usage("1", agy.Credential.parse(auth()), False)
    assert len(result["scoped"]) == 5


@pytest.mark.parametrize("bad", [True, -0.1, 1.01, float("nan"), "0.5", None])
def test_malformed_quota_fraction_is_rejected(bad):
    document = quota()
    document["command"]["data"]["groups"][0]["buckets"][0]["remaining_fraction"] = bad
    with pytest.raises(agy.AgyError, match="quota"):
        agy.parse_usage(document)


def test_cli_json_includes_usage_and_safe_error_envelope(provider, monkeypatch, capsys):
    two_accounts(provider)
    capsys.readouterr()
    monkeypatch.setattr(cli, "AgyAccountSwitcher", lambda: provider)
    cli._agy_command(["list", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert len(payload["accounts"][0]["usage"]["scoped"]) == 5
    with pytest.raises(SystemExit) as raised:
        cli._agy_command(["switch", "missing", "--json"])
    assert raised.value.code == 1
    output = capsys.readouterr()
    assert json.loads(output.out)["error"]["type"] == "AgyError"
    assert "synthetic-access" not in output.out + output.err


def test_cli_subprocess_file_backend_round_trip(tmp_path):
    home = tmp_path / "child-home"
    native_path = home / agy._RELATIVE_AUTH
    agy._private_dir(native_path.parent)
    agy.atomic_write_text(native_path, json.dumps(auth()))
    env = {"HOME": str(home), "USERPROFILE": str(home), "SSH_TTY": "/dev/synthetic",
           "PATH": os.environ.get("PATH", ""), "TERM": "dumb", "NO_COLOR": "1",
           "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}

    def command(*args):
        result = subprocess.run([sys.executable, "-m", "claude_swap", "agy", *args],
                                cwd=tmp_path, env=env, capture_output=True, text=True, timeout=15)
        assert result.returncode == 0, result.stderr
        return result.stdout

    command("add")
    agy.atomic_write_text(native_path, json.dumps(auth("two@example.com")))
    command("add")
    assert json.loads(command("switch", "1", "--json"))["to"]["email"] == "one@example.com"
    assert json.loads(native_path.read_text()) == auth()
    assert json.loads(command("status", "--json"))["activeAccountNumber"] == 1
    command("remove", "2", "--yes")
    assert json.loads(native_path.read_text()) == auth()


@pytest.mark.parametrize("mode", ["adc", "gemini"])
def test_non_account_auth_modes_refuse_shared_switch(provider, monkeypatch, mode):
    two_accounts(provider)
    if mode == "adc":
        monkeypatch.setenv("AGY_ADC_AUTH", "true")
    else:
        provider._write(Path.home() / agy._RELATIVE_AUTH.parent / "settings.json", {"modelProvider": "gemini"})
    with pytest.raises(agy.AgyError, match="authentication|API-key"):
        provider.switch_to("1")
    assert read_native()["id_token_email"] == "two@example.com"


@pytest.mark.parametrize("cached", [
    {"fetchedAt": True}, {"attemptedAt": float("inf")},
    {"lastGood": {"scoped": [{"pct": "bad"}]}},
    {"error": "synthetic-secret-cannot-be-rendered"},
])
def test_malformed_usage_cache_fails_closed(provider, cached):
    native(auth())
    provider.add_account()
    provider._write(provider.backup_dir / "usage-1.json", cached)
    with pytest.raises(agy.AgyError, match="usage cache"):
        provider.accounts_snapshot(fetch=set())


def test_five_hour_scoped_quota_has_no_weekly_pace_and_healthy_compact_rows(provider):
    from claude_swap.models import AccountSnapshot
    from claude_swap.tui.widgets import mini_account_text, usage_rows
    from claude_swap.usage_store import UsageEntry

    usage = agy.parse_usage(quota())
    usage["scoped"] = [dict(usage["scoped"][1], pct=80, resets_at="2030-01-01T00:00:00Z")]
    rows = usage_rows(usage, 1_893_455_400, 1_893_455_400)
    assert "ahead" not in rows[0][2]
    account = AccountSnapshot("1", "one@example.com", "Antigravity", "", False, "oauth", True,
                              UsageEntry(last_good=usage))
    compact = mini_account_text(account, 1_893_455_400).plain
    assert "Gemini Models · 5h" in compact
    assert "80%" in compact
    assert "usage unavailable" not in compact


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_size", [(110, 48), (80, 32)])
async def test_dashboard_agy_manual_switch_add_remove_and_scoped_quotas(provider, tmp_path, monkeypatch, terminal_size):
    from textual.widgets import ListView
    from claude_swap.tui.app import CswapApp
    from claude_swap.tui.widgets import AccountItem, AccountsPanel
    from tests.test_tui import FakeSwitcher, settle

    two_accounts(provider)
    monkeypatch.setattr("claude_swap.claude_integration.mod_installed", lambda: False)
    app = CswapApp(FakeSwitcher([], tmp_path), codex_switcher=FakeSwitcher([], tmp_path),
                   agy_switcher=provider)
    app.title = "ccswap · synthetic test data"
    async with app.run_test(size=terminal_size) as pilot:
        await settle(pilot)
        panel = app.screen.query_one(AccountsPanel).render().plain
        assert "Antigravity" in panel and "Gemini Models · 5h" in panel
        assert "usage unavailable" not in panel
        screen = app.screen
        assert all(action != "auto:agy" for _, action in screen._auto_entries())
        assert all(not action.startswith("disable:agy:") for _, action in screen._disable_entries())
        assert any(action == "add-login:agy" for _, action in screen._add_entries())
        app.save_screenshot("agy-dashboard.svg", path=str(tmp_path))
        app.apply_view("agy")
        await pilot.press("s")
        await settle(pilot)
        listing = app.screen.query_one("#accounts", ListView)
        assert isinstance(listing.highlighted_child, AccountItem)
        assert listing.highlighted_child.provider == "agy"
        assert app.screen.check_action("switch_best", ()) is False
        await pilot.press("b")
        await settle(pilot)
        assert read_native()["id_token_email"] == "two@example.com"
        await pilot.press("up")
        await pilot.pause()
        assert listing.highlighted_child.number == "1"
        screenshot = app.save_screenshot("agy-switch.svg", path=str(tmp_path))
        assert Path(screenshot).is_file()
        await pilot.press("enter")
        await settle(pilot)
        assert read_native()["id_token_email"] == "one@example.com"
        app.do_switch_best("agy")
        app.do_toggle_disabled("agy", "1")
        app.open_auto("agy")
        assert app.screen is screen
        native(auth("three@example.com"))
        app.do_add_current("agy")
        await pilot.pause()
        await pilot.press("y")
        await settle(pilot)
        assert provider._roster()["accounts"]["3"]["email"] == "three@example.com"
        await pilot.press("escape")
        app.confirm_remove("agy", "3", "three@example.com")
        await pilot.pause()
        await pilot.press("y")
        await settle(pilot)
        assert "3" not in provider._roster()["accounts"]
        assert read_native()["id_token_email"] == "three@example.com"


def test_inactive_scoped_quota_keeps_each_group_when_one_is_exhausted(provider):
    from claude_swap.tui.widgets import mini_account_text

    two_accounts(provider)
    account = provider.accounts_snapshot().accounts[0]
    windows = account.usage.last_good["scoped"]
    windows[0]["pct"] = 100
    rendered = mini_account_text(account, 0).plain
    for window in windows:
        assert window["name"] in rendered
    assert len(rendered.splitlines()) == len(windows) + 1
    for line in rendered.splitlines()[1:]:
        assert sum(window["name"] in line for window in windows) == 1
