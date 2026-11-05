"""Synthetic custody and CLI tests, with no real Antigravity process or store.

Failure cases include encoded Keychain values, malformed roster/auth/cache, unsafe slots and backends,
write denial/timeout/uncertainty, external writers, lost refreshed credentials,
malformed quota groups, expired access tokens, secret diagnostics, combined JSON,
and a dashboard selecting the wrong provider or inventing a weekly pace for 5h.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import time
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
    monkeypatch.setattr(agy, "sys", SimpleNamespace(platform="darwin"))
    fake_binary = str((Path.home() / "synthetic-agy").absolute())
    monkeypatch.setattr(agy.shutil, "which", lambda name: fake_binary)

    def usage(argv, **kwargs):
        assert argv == [fake_binary, "-p", "/usage", "--output-format", "json"]
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
    if os.name != "nt":
        assert provider.backup_dir.stat().st_mode & 0o777 == 0o700
        for path in provider.backup_dir.iterdir():
            assert path.stat().st_mode & 0o777 == 0o600


def test_add_decodes_go_keyring_value_without_changing_native_login(provider):
    document = auth(future_field={"preserve": "non-ASCII 雪"})
    encoded = "go-keyring-base64:" + base64.b64encode(
        json.dumps(document, ensure_ascii=False).encode("utf-8")
    ).decode("ascii")
    macos_keychain.set_password("gemini", "antigravity", encoded)

    provider.add_account()

    assert json.loads(provider._credential_path("1").read_text()) == document
    assert provider.status()["activeAccountNumber"] == 1
    assert macos_keychain.get_password("gemini", "antigravity") == encoded


@pytest.mark.parametrize("encoded", [
    "go-keyring-base64:not-base64!",
    "go-keyring-base64:/w==",
    "go-keyring-base64:" + base64.b64encode(b"synthetic-not-json").decode(),
    "go-keyring-base64:" + base64.b64encode(b"[]").decode(),
])
def test_invalid_go_keyring_value_does_not_save_or_leak(provider, encoded):
    macos_keychain.set_password("gemini", "antigravity", encoded)
    with pytest.raises(agy.AgyError) as raised:
        provider.add_account()
    assert str(raised.value) == "Invalid Antigravity native credentials"
    assert not provider.sequence_file.exists()
    assert macos_keychain.get_password("gemini", "antigravity") == encoded


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


def test_usage_timeout_never_exceeds_budget_on_a_stalled_clock(provider, monkeypatch):
    two_accounts(provider)
    # (t + 45) - t rounds to 45.0000000000001 here; Windows' coarse clock often returns the same t twice.
    frozen = SimpleNamespace(monotonic=lambda: 1017.3252014074263, time=time.time, time_ns=time.time_ns)
    monkeypatch.setattr(agy, "time", frozen)
    timeouts = []

    def run(argv, **kwargs):
        timeouts.append(kwargs["timeout"])
        return SimpleNamespace(returncode=1, stdout="", stderr="")

    monkeypatch.setattr(agy.spawn, "run", run)
    with pytest.raises(agy.AgyError):
        provider._invoke_usage("1", agy.Credential.parse(auth()), False)
    assert timeouts == [45]


@pytest.mark.parametrize("failure", ["exit", "timeout", "malformed"])
def test_inactive_refresh_persists_even_on_usage_failure(provider, monkeypatch, failure):
    two_accounts(provider)
    refreshed = auth()
    refreshed["token"]["refresh_token"] = "synthetic-rotated"
    monkeypatch.setenv("ANTIGRAVITY_API_KEY", "synthetic-must-not-inherit")

    def run(argv, **kwargs):
        env = kwargs["env"]
        assert env["USERPROFILE"] == env["HOME"]
        assert "ANTIGRAVITY_API_KEY" not in env
        assert env["SSH_TTY"] == "/dev/ccswap-usage"
        assert 0 < kwargs["timeout"] <= 45
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
    if os.name != "nt":
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


@pytest.mark.parametrize("diagnostic", [
    "Antigravity CLI is not installed",
    "Antigravity usage command failed",
    "Antigravity usage command failed or timed out",
    "Antigravity returned invalid quota JSON",
    "Antigravity returned invalid or unavailable quota data",
    "Antigravity usage unavailable",
])
def test_known_usage_diagnostics_survive_cooldown_in_cli(provider, monkeypatch, capsys, diagnostic):
    native(auth())
    provider.add_account()
    capsys.readouterr()
    attempts = []

    def fail_usage(*args, **kwargs):
        attempts.append(args)
        raise agy.AgyError(diagnostic)

    monkeypatch.setattr(provider, "_invoke_usage", fail_usage)
    monkeypatch.setattr(cli, "AgyAccountSwitcher", lambda: provider)
    cli._agy_command(["usage"])
    assert diagnostic in capsys.readouterr().out
    for _ in range(2):
        cli._agy_command(["usage", "--json"])
        assert json.loads(capsys.readouterr().out)["accounts"][0]["error"] == diagnostic
        cli._agy_command(["usage"])
        assert diagnostic in capsys.readouterr().out
    assert len(attempts) == 1


def test_missing_cli_usage_keeps_install_hint_during_cooldown(provider, monkeypatch, capsys):
    native(auth())
    provider.add_account()
    capsys.readouterr()
    lookups = []

    def missing_cli(name):
        lookups.append(name)
        return None

    monkeypatch.setattr(agy.shutil, "which", missing_cli)
    monkeypatch.setattr(agy.spawn, "run", lambda *args, **kwargs: pytest.fail("missing CLI started"))
    monkeypatch.setattr(cli, "AgyAccountSwitcher", lambda: provider)
    for _ in range(2):
        cli._agy_command(["usage", "--json"])
        diagnostic = json.loads(capsys.readouterr().out)["accounts"][0]["error"]
        assert "Antigravity CLI is not installed" in diagnostic
        assert "Install" in diagnostic
        assert "https://antigravity.google/docs/getting-started/" in diagnostic
        cli._agy_command(["usage"])
        assert diagnostic in capsys.readouterr().out
    assert lookups == ["agy"]


def test_unknown_usage_diagnostic_is_not_persisted(provider, monkeypatch):
    native(auth())
    provider.add_account()

    def fail_usage(*args, **kwargs):
        raise agy.AgyError("synthetic-unrecognized-error")

    monkeypatch.setattr(provider, "_invoke_usage", fail_usage)
    assert provider.accounts_snapshot().accounts[0].usage.last_error == "synthetic-unrecognized-error"
    assert provider.accounts_snapshot().accounts[0].usage.last_error == "Antigravity usage unavailable"
    assert "synthetic-unrecognized-error" not in (provider.backup_dir / "usage-1.json").read_text()


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
        encoded = "go-keyring-base64:" + base64.b64encode(
            json.dumps(auth("three@example.com")).encode()
        ).decode()
        macos_keychain.set_password("gemini", "antigravity", encoded)
        app.do_add_current("agy")
        await pilot.pause()
        await pilot.press("y")
        await settle(pilot)
        assert provider._roster()["accounts"]["3"]["email"] == "three@example.com"
        app.save_screenshot("agy-add-encoded.svg", path=str(tmp_path))
        await pilot.press("escape")
        app.confirm_remove("agy", "3", "three@example.com")
        await pilot.pause()
        await pilot.press("y")
        await settle(pilot)
        assert "3" not in provider._roster()["accounts"]
        assert macos_keychain.get_password("gemini", "antigravity") == encoded


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


def test_snapshot_bounds_total_subprocess_time_and_rotates_persisted_order(provider, monkeypatch):
    two_accounts(provider)
    native(auth("three@example.com"))
    provider.add_account()
    clock = [1000.0]
    monkeypatch.setattr(agy.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(agy.time, "time", lambda: clock[0])
    calls = []

    def run(argv, **kwargs):
        email = (read_native()["id_token_email"] if kwargs["env"]["HOME"] == str(Path.home()) else
                 json.loads((Path(kwargs["env"]["HOME"]) / agy._RELATIVE_AUTH).read_text())["id_token_email"])
        calls.append((email, kwargs["timeout"]))
        clock[0] += kwargs["timeout"]
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(agy.spawn, "run", run)
    snapshot = provider.accounts_snapshot()
    assert [call[0] for call in calls] == ["one@example.com"]
    assert sum(call[1] for call in calls) <= 45
    assert [account.number for account in snapshot.accounts] == ["1", "2", "3"]
    assert snapshot.accounts[1].usage.last_attempt_at is None
    assert snapshot.accounts[1].usage.last_error is None
    clock[0] += 61
    agy.AgyAccountSwitcher().accounts_snapshot()
    clock[0] += 61
    agy.AgyAccountSwitcher().accounts_snapshot()
    assert [call[0] for call in calls] == ["one@example.com", "two@example.com", "three@example.com"]


def test_snapshot_passes_remaining_budget_and_preserves_fetch_selection(provider, monkeypatch):
    two_accounts(provider)
    clock = [100.0]
    monkeypatch.setattr(agy.time, "monotonic", lambda: clock[0])
    calls = []

    def run(argv, **kwargs):
        calls.append(kwargs["timeout"])
        clock[0] += 30 if len(calls) == 1 else kwargs["timeout"]
        return SimpleNamespace(returncode=0, stdout=json.dumps(quota()))

    monkeypatch.setattr(agy.spawn, "run", run)
    provider.accounts_snapshot()
    assert calls == [45, 15]
    provider.accounts_snapshot(fetch={"2"})
    assert calls == [45, 15]


@pytest.mark.parametrize("active", [False, True])
def test_write_denial_after_refresh_keeps_durable_recovery(provider, monkeypatch, active):
    two_accounts(provider)
    if active:
        native(auth())
    write = provider._write
    refreshed = auth(future_field="only-refreshed-copy")
    child_files = []

    def run(*args, **kwargs):
        child_file = Path(kwargs["cwd"]) / agy._RELATIVE_AUTH
        child_files.append(child_file)
        assert provider._usage_cache("1")["recovery"]["path"] == str(child_file)
        if active:
            native(refreshed)
        else:
            write(child_file, refreshed)

        def deny_backup(path, document):
            if path.is_relative_to(provider.backup_dir):
                raise agy.AgyError("Cannot save Antigravity account data")
            write(path, document)

        monkeypatch.setattr(provider, "_write", deny_backup)
        return SimpleNamespace(returncode=0, stdout=json.dumps(quota()))

    monkeypatch.setattr(agy.spawn, "run", run)
    account = provider.accounts_snapshot(fetch={"1"}).accounts[0]
    assert account.switchable is False
    recovery = account.usage.recovery
    assert recovery is not None and recovery.path in account.usage.last_error
    assert json.loads(Path(recovery.path).read_text()) == refreshed
    assert Path(recovery.path) == child_files[0]
    monkeypatch.setattr(provider, "_write", write)
    monkeypatch.setattr(agy.spawn, "run", lambda *args, **kwargs: pytest.fail("recovery refetched"))
    restarted = agy.AgyAccountSwitcher()
    assert restarted.accounts_snapshot(fetch={"1"}).accounts[0].usage.recovery == recovery
    with pytest.raises(agy.AgyError, match="Recovery"):
        restarted.switch_to("1", json_output=True)


def test_usage_refuses_to_launch_without_durable_intent(provider, monkeypatch):
    two_accounts(provider)
    write = provider._write

    def deny_journal(path, document):
        if path == provider.backup_dir / "usage-1.json":
            raise agy.AgyError("Cannot save Antigravity account data")
        write(path, document)

    monkeypatch.setattr(provider, "_write", deny_journal)
    monkeypatch.setattr(agy.spawn, "run", lambda *args, **kwargs: pytest.fail("unjournaled child"))
    with pytest.raises(agy.AgyError, match="Cannot save"):
        provider._invoke_usage("1", agy.Credential.parse(auth()), False)


@pytest.mark.parametrize("failure", ["conflict-marker", "clear-intent"])
def test_failed_journal_finalization_keeps_recovery_copy(provider, monkeypatch, failure):
    two_accounts(provider)
    refreshed = auth(future_field="latest")
    write = provider._write
    children = []

    def run(*args, **kwargs):
        child = Path(kwargs["cwd"]) / agy._RELATIVE_AUTH
        children.append(child)
        write(child, refreshed)
        if failure == "conflict-marker":
            write(provider._credential_path("1"), auth(future_field="external"))

        def deny_journal(path, document):
            if path == provider.backup_dir / "usage-1.json":
                raise agy.AgyError("Cannot save Antigravity account data")
            write(path, document)

        monkeypatch.setattr(provider, "_write", deny_journal)
        return SimpleNamespace(returncode=0, stdout=json.dumps(quota()))

    monkeypatch.setattr(agy.spawn, "run", run)
    with pytest.raises(agy.AgyError) as raised:
        provider._invoke_usage("1", agy.Credential.parse(auth()), False)
    pending = agy.AgyAccountSwitcher()._usage_cache("1")["recovery"]
    assert pending["path"] == str(children[0])
    assert pending["path"] in str(raised.value)
    assert json.loads(children[0].read_text()) == refreshed
    if failure == "clear-intent":
        assert json.loads(provider._credential_path("1").read_text()) == refreshed
    else:
        conflict = next(provider.backup_dir.glob("refresh-conflict-1-*.json"))
        assert json.loads(conflict.read_text()) == refreshed


def test_conflict_recovery_survives_temp_cleanup_failure(provider, monkeypatch):
    two_accounts(provider)
    refreshed = auth(future_field="rotated")
    cleanup = agy.tempfile.TemporaryDirectory.cleanup
    directories = []

    def run(*args, **kwargs):
        directories.append(Path(kwargs["cwd"]))
        provider._write(Path(kwargs["cwd"]) / agy._RELATIVE_AUTH, refreshed)
        provider._write(provider._credential_path("1"), auth(future_field="external"))
        return SimpleNamespace(returncode=0, stdout=json.dumps(quota()))

    def deny_cleanup(self):
        raise PermissionError("synthetic cleanup failure")

    monkeypatch.setattr(agy.spawn, "run", run)
    monkeypatch.setattr(agy.tempfile.TemporaryDirectory, "cleanup", deny_cleanup)
    with pytest.raises(agy.AgyError, match="Recovery") as raised:
        provider._invoke_usage("1", agy.Credential.parse(auth()), False)
    pending = provider._usage_cache("1")["recovery"]
    assert pending["path"] in str(raised.value)
    assert json.loads(Path(pending["path"]).read_text()) == refreshed
    monkeypatch.setattr(agy.tempfile.TemporaryDirectory, "cleanup", cleanup)
    for directory in directories:
        agy.shutil.rmtree(directory)


@pytest.mark.parametrize("slow_step", ["setup", "journal"])
def test_expired_setup_never_launches_or_marks_an_attempt(provider, monkeypatch, slow_step):
    two_accounts(provider)
    clock = [100.0]
    monkeypatch.setattr(agy.time, "monotonic", lambda: clock[0])
    write = provider._write

    def slow_write(path, document):
        write(path, document)
        if ((slow_step == "setup" and path.name == agy._RELATIVE_AUTH.name)
                or (slow_step == "journal" and document.get("recovery") is not None)):
            clock[0] += 50

    monkeypatch.setattr(provider, "_write", slow_write)
    monkeypatch.setattr(agy.spawn, "run", lambda *args, **kwargs: pytest.fail("expired child launched"))
    account = provider.accounts_snapshot(fetch={"1"}).accounts[0]
    assert account.usage.last_attempt_at is None
    assert account.usage.last_error is None
    assert account.usage.recovery is None
    restarted = agy.AgyAccountSwitcher().accounts_snapshot(fetch=set()).accounts[0]
    assert restarted.usage.recovery is None


def test_deadline_includes_setup_and_journal_time(provider, monkeypatch):
    two_accounts(provider)
    clock = [100.0]
    monkeypatch.setattr(agy.time, "monotonic", lambda: clock[0])
    write = provider._write
    calls = []

    def slow_write(path, document):
        write(path, document)
        if path.name == agy._RELATIVE_AUTH.name or document.get("recovery") is not None:
            clock[0] += 5

    def run(*args, **kwargs):
        calls.append(kwargs["timeout"])
        return SimpleNamespace(returncode=0, stdout=json.dumps(quota()))

    monkeypatch.setattr(provider, "_write", slow_write)
    monkeypatch.setattr(agy.spawn, "run", run)
    provider.accounts_snapshot(fetch={"1"})
    assert calls == [35]


def test_expired_journal_cleanup_failure_keeps_intent_without_attempt(provider, monkeypatch):
    two_accounts(provider)
    clock = [100.0]
    monkeypatch.setattr(agy.time, "monotonic", lambda: clock[0])
    write = provider._write

    def fail_clear(path, document):
        if path == provider.backup_dir / "usage-1.json":
            if document.get("recovery") is None:
                raise agy.AgyError("Cannot save Antigravity account data")
            clock[0] += 50
        write(path, document)

    monkeypatch.setattr(provider, "_write", fail_clear)
    monkeypatch.setattr(agy.spawn, "run", lambda *args, **kwargs: pytest.fail("expired child launched"))
    account = provider.accounts_snapshot(fetch={"1"}).accounts[0]
    assert account.usage.last_attempt_at is None
    pending = provider._usage_cache("1")
    assert pending.get("attemptedAt") is None and pending.get("error") is None
    assert account.usage.recovery is not None
    assert json.loads(Path(account.usage.recovery.path).read_text()) == auth()
    assert agy.AgyAccountSwitcher().accounts_snapshot(fetch=set()).accounts[0].switchable is False


def test_success_saves_then_clears_intent_before_temp_cleanup(provider, monkeypatch):
    two_accounts(provider)
    refreshed = auth(future_field="latest")
    write = provider._write
    children = []
    cleared = []

    def observe_write(path, document):
        if path == provider.backup_dir / "usage-1.json" and document.get("recovery") is None:
            assert json.loads(provider._credential_path("1").read_text()) == refreshed
            assert children[0].exists()
            cleared.append(path)
        write(path, document)

    def run(*args, **kwargs):
        child = Path(kwargs["cwd"]) / agy._RELATIVE_AUTH
        children.append(child)
        assert provider._usage_cache("1")["recovery"]["path"] == str(child)
        write(child, refreshed)
        return SimpleNamespace(returncode=0, stdout=json.dumps(quota()))

    monkeypatch.setattr(provider, "_write", observe_write)
    monkeypatch.setattr(agy.spawn, "run", run)
    assert provider._invoke_usage("1", agy.Credential.parse(auth()), False)["scoped"]
    assert cleared and not children[0].exists()
    assert agy.AgyAccountSwitcher()._usage_cache("1")["recovery"] is None


def test_queued_manual_switch_succeeds_after_bounded_refresh(provider, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from claude_swap.locking import FileLock

    two_accounts(provider)
    for email in ("three@example.com", "four@example.com"):
        native(auth(email))
        provider.add_account()
    started, release, queued = Event(), Event(), Event()
    monkeypatch.setattr(agy, "_USAGE_TIMEOUT", 0.12)
    monkeypatch.setattr(agy.AgyAccountSwitcher, "_lock", lambda self: FileLock(self.lock_file, timeout=0.35))
    calls = []

    def run(argv, **kwargs):
        calls.append(kwargs["timeout"])
        started.set()
        assert release.wait(2)
        Event().wait(kwargs["timeout"])
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(agy.spawn, "run", run)

    def switch():
        queued.set()
        return agy.AgyAccountSwitcher().switch_to("1", json_output=True)

    with ThreadPoolExecutor(max_workers=2) as pool:
        refresh = pool.submit(provider.accounts_snapshot)
        assert started.wait(2)
        manual = pool.submit(switch)
        assert queued.wait(2)
        release.set()
        assert manual.result(timeout=3)["to"]["number"] == 1
        refresh.result(timeout=3)
    assert len(calls) == 1
    assert read_native()["id_token_email"] == "one@example.com"


def induce_recovery(provider, monkeypatch, failure="conflict"):
    two_accounts(provider)
    provider.accounts_snapshot()
    cache_path = provider.backup_dir / "usage-1.json"
    cached = json.loads(cache_path.read_text())
    cached["attemptedAt"] = 0
    provider._write(cache_path, cached)
    refreshed = auth(future_field="refreshed-recovery")

    def run(argv, **kwargs):
        provider._write(Path(kwargs["env"]["HOME"]) / agy._RELATIVE_AUTH, refreshed)
        if failure == "missing":
            provider._credential_path("1").unlink()
        elif failure == "malformed":
            provider._credential_path("1").write_text("{")
        else:
            provider._write(provider._credential_path("1"), auth(future_field="external"))
        return SimpleNamespace(returncode=0, stdout=json.dumps(quota()))

    monkeypatch.setattr(agy.spawn, "run", run)
    return provider.accounts_snapshot(fetch={"1"}).accounts[0]


@pytest.mark.parametrize("failure", ["conflict", "missing", "malformed"])
def test_recovery_survives_cooldown_and_missing_saved_file(provider, monkeypatch, failure):
    first = induce_recovery(provider, monkeypatch, failure)
    assert first.switchable is False
    assert first.usage.recovery is not None
    recovery_path = first.usage.recovery.path
    assert Path(recovery_path).is_absolute() and Path(recovery_path).is_file()
    monkeypatch.setattr(agy.spawn, "run", lambda *args, **kwargs: pytest.fail("pending recovery refetched"))
    monkeypatch.setattr(agy.time, "time", lambda: first.usage.last_attempt_at + 100)
    native(auth(future_field="native-newer"))
    fresh = agy.AgyAccountSwitcher()
    account = fresh.accounts_snapshot(fetch={"1"}).accounts[0]
    assert account.usage.recovery == first.usage.recovery
    assert recovery_path in account.usage.last_error
    assert account.usage.last_good == first.usage.last_good
    assert account.switchable is False
    with pytest.raises(agy.AgyError, match="Recovery"):
        fresh.switch_to("1", json_output=True)
    assert Path(recovery_path).is_file()


def test_explicit_add_clears_only_matching_recovery_and_incidental_capture_preserves_it(provider, monkeypatch):
    first = induce_recovery(provider, monkeypatch)
    cache_path = provider.backup_dir / "usage-1.json"
    before = json.loads(cache_path.read_text())
    native(auth(future_field="signed-in"))
    provider.switch_to("2", json_output=True)
    assert json.loads(cache_path.read_text()) == before
    provider.add_account()
    assert json.loads(cache_path.read_text()) == before
    native(auth(future_field="signed-in"))
    provider.add_account()
    healed = agy.AgyAccountSwitcher().accounts_snapshot(fetch=set()).accounts[0]
    assert healed.switchable is True
    assert healed.usage.recovery is None
    assert healed.usage.last_error is None
    assert Path(first.usage.recovery.path).is_file()


def test_failed_explicit_add_preserves_recovery(provider, monkeypatch):
    first = induce_recovery(provider, monkeypatch)
    native(auth(future_field="signed-in"))
    before = (provider.backup_dir / "usage-1.json").read_text()
    write = provider._write

    def fail_roster(path, document):
        if path == provider.sequence_file:
            raise agy.AgyError("Cannot save Antigravity account data")
        write(path, document)

    monkeypatch.setattr(provider, "_write", fail_roster)
    with pytest.raises(agy.AgyError, match="Cannot save"):
        provider.add_account()
    assert (provider.backup_dir / "usage-1.json").read_text() == before
    assert Path(first.usage.recovery.path).is_file()


@pytest.mark.parametrize("metadata", ["secret", {}, {"kind": "raw-secret", "path": "/tmp/file"},
    {"kind": "refresh-conflict", "path": "relative"},
    {"kind": "refresh-conflict", "path": "/tmp/bad\x1bpath"},
    {"kind": "refresh-conflict", "path": 123}])
def test_malformed_recovery_metadata_fails_without_leaking(provider, metadata):
    native(auth())
    provider.add_account()
    provider._write(provider.backup_dir / "usage-1.json", {"recovery": metadata})
    with pytest.raises(agy.AgyError) as raised:
        provider.accounts_snapshot(fetch=set())
    assert str(raised.value) == "Invalid Antigravity usage cache"


def test_full_and_compact_cards_show_recovery_with_cached_quota(provider, monkeypatch):
    from claude_swap.tui.widgets import account_card_text, mini_account_text

    account = induce_recovery(provider, monkeypatch)
    assert account.usage.last_good is not None
    for text in (account_card_text(account, 100).plain, mini_account_text(account, 0).plain):
        assert "Recovery required" in text
        assert "ccswap agy add" in text
        assert account.usage.recovery.path in text
        assert "Gemini Models" in text


@pytest.mark.asyncio
@pytest.mark.parametrize("active", [False, True])
async def test_dashboard_shows_pending_recovery_with_cached_quota(provider, monkeypatch, tmp_path, active):
    from claude_swap.tui.app import CswapApp
    from claude_swap.tui.widgets import AccountsPanel
    from tests.test_tui import FakeSwitcher, settle

    account = induce_recovery(provider, monkeypatch)
    if active:
        native(auth())
    monkeypatch.setattr("claude_swap.claude_integration.mod_installed", lambda: False)
    app = CswapApp(FakeSwitcher([], tmp_path), codex_switcher=FakeSwitcher([], tmp_path),
                   agy_switcher=provider)
    app.title = "ccswap · synthetic recovery test data"
    async with app.run_test(size=(120, 48)) as pilot:
        await settle(pilot)
        panel = app.screen.query_one(AccountsPanel).render().plain
        assert "Recovery required" in panel and "ccswap agy add" in panel
        assert account.usage.recovery.path in panel
        assert "Gemini Models" in panel
        app.save_screenshot(f"agy-recovery-{'full' if active else 'compact'}.svg", path=str(tmp_path))
