"""Window warming must never spend a refresh grant or retry ambiguous sends."""

import json
import os
import subprocess
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from claude_swap import warmup
from claude_swap.locking import FileLock
from claude_swap.models import AccountSnapshot, AccountsSnapshot
from claude_swap.settings import AutoSwitchSettings, load_settings, set_setting
from claude_swap.usage_store import UsageEntry

NOW = 1_800_000_000.0
IDENTITY = {"email": "test@example.org", "organizationUuid": "org", "uuid": "user"}
RUN_PROMPT = warmup._run_prompt


def iso(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


def account(**changes):
    return replace(AccountSnapshot(
        number="1", email=IDENTITY["email"], org_name="", org_uuid="org",
        is_active=False, kind="oauth", switchable=True,
        usage=UsageEntry(fetched_at=NOW, last_good={
            "five_hour": {"pct": 0, "resets_at": iso(NOW - 10)},
            "seven_day": {"pct": 25},
        }),
    ), **changes)


@pytest.fixture
def harness(tmp_path, monkeypatch):
    state = SimpleNamespace(now=NOW, identity=dict(IDENTITY), account=account(),
                            enabled=True, events=[], sends=[])
    state.credentials = json.dumps({"claudeAiOauth": {
        "accessToken": "fake-access", "refreshToken": "must-never-leave-store",
        "expiresAt": 9_000_000_000_000,
    }})
    switcher = SimpleNamespace(
        backup_dir=tmp_path, lock_file=tmp_path / ".lock",
        account_identity=lambda num: dict(state.identity),
        read_account_credentials=lambda num, email: state.credentials,
        _account_is_switchable=lambda num: True,
        _disabled_from_data=lambda data, num: not state.enabled,
        _get_sequence_data=lambda: {},
        accounts_snapshot=lambda fetch: AccountsSnapshot(None, (state.account,), state.now),
    )
    state.switcher = switcher
    state.engine = warmup.WarmupEngine(switcher, lambda: state.now, state.events.append)
    monkeypatch.setattr(warmup.shutil, "which", lambda name: "/fake/claude")

    def assert_unlocked(*args):
        with FileLock(switcher.lock_file, timeout=0):
            pass

    def profile(token):
        assert token == "fake-access"
        assert_unlocked()
        return dict(IDENTITY)

    def send(*args):
        assert_unlocked()
        assert warmup._read_claims(tmp_path / warmup.STATE_FILENAME)
        state.sends.append(args)
        return True

    monkeypatch.setattr(warmup.oauth, "fetch_oauth_profile", profile)
    monkeypatch.setattr(warmup, "_run_prompt", send)
    return state


@pytest.mark.parametrize("change", [
    {"disabled": True}, {"kind": "api_key"}, {"switchable": False},
    {"usage": UsageEntry()},
    {"usage": UsageEntry(sentinel="expired", fetched_at=NOW, last_good={})},
    {"usage": UsageEntry(last_error="http-429", fetched_at=NOW, last_good={})},
])
def test_ineligible_accounts(harness, change):
    harness.account = account(**change)
    harness.engine.tick()
    assert harness.sends == []


@pytest.mark.parametrize("five,weekly,scoped", [
    ({"pct": 0, "resets_at": iso(NOW + 1)}, {"pct": 25}, []),
    ({"pct": 0, "resets_at": "bad"}, {"pct": 25}, []),
    ({"pct": 1}, {"pct": 25}, []),
    ({"pct": 0}, None, []),
    ({"pct": 0}, {"pct": 100}, []),
    ({"pct": 0}, {"pct": float("nan")}, []),
    ({"pct": 0}, {"pct": 25}, [{"name": "Haiku", "pct": 100}]),
    ({"pct": 0}, {"pct": 25}, None),
    ({"pct": 0}, {"pct": 25}, ["Haiku"]),
])
def test_windows_must_prove_headroom(harness, five, weekly, scoped):
    harness.account.usage.last_good.update(five_hour=five, seven_day=weekly, scoped=scoped)
    harness.engine.tick()
    assert harness.sends == []


@pytest.mark.parametrize("fetched", [NOW - 121, NOW + 1, NOW - 20])
def test_stale_future_or_pre_reset_reading_cannot_authorize(harness, fetched):
    harness.account = replace(harness.account, usage=replace(harness.account.usage, fetched_at=fetched))
    harness.engine.tick()
    assert harness.sends == []


def test_cold_window_without_five_hour_reading(harness):
    del harness.account.usage.last_good["five_hour"]
    harness.engine.tick()
    assert len(harness.sends) == 1


def test_guard_survives_engine_restart_and_slot_renumbering(harness):
    harness.engine.tick()
    harness.account = replace(harness.account, number="2")
    warmup.WarmupEngine(harness.switcher, lambda: harness.now, harness.events.append).tick()
    assert len(harness.sends) == 1
    assert "completed" in harness.events[0]


def test_guard_published_before_send_even_with_competing_engine(harness, monkeypatch):
    def send(*args):
        warmup.WarmupEngine(harness.switcher, lambda: harness.now, harness.events.append).tick()
        harness.sends.append(args)
        raise subprocess.TimeoutExpired("claude", 60, output="sensitive-output")
    monkeypatch.setattr(warmup, "_run_prompt", send)
    harness.engine.tick()
    harness.engine.next_check = 0
    harness.engine.tick()
    assert len(harness.sends) == 1
    assert "sensitive-output" not in str(harness.events)
    assert warmup._read_claims(harness.switcher.backup_dir / warmup.STATE_FILENAME)


def test_dry_run_does_not_reserve_or_probe_identity(harness, monkeypatch):
    monkeypatch.setattr(warmup.oauth, "fetch_oauth_profile", lambda token: pytest.fail("probe"))
    harness.engine.tick(dry_run=True)
    assert not (harness.switcher.backup_dir / warmup.STATE_FILENAME).exists()
    assert not harness.sends
    assert "would warm" in harness.events[0]


@pytest.mark.parametrize("mutation", ["identity", "token", "disable", "stale"])
def test_revalidate_after_identity_probe(harness, monkeypatch, mutation):
    def profile(token):
        if mutation == "identity":
            harness.identity["uuid"] = "someone-else"
        elif mutation == "token":
            harness.credentials += " "
        elif mutation == "disable":
            harness.enabled = False
        else:
            harness.now += 121
        return dict(IDENTITY)
    monkeypatch.setattr(warmup.oauth, "fetch_oauth_profile", profile)
    harness.engine.tick()
    assert not harness.sends
    assert not (harness.switcher.backup_dir / warmup.STATE_FILENAME).exists()


@pytest.mark.parametrize("profile", [None, {**IDENTITY, "uuid": "wrong"}])
def test_identity_resolution_fails_closed(harness, monkeypatch, profile):
    monkeypatch.setattr(warmup.oauth, "fetch_oauth_profile", lambda token: profile)
    harness.engine.tick()
    assert not harness.sends


@pytest.mark.parametrize("expiry", [None, 0, "bad", True])
def test_unknown_or_expired_token_is_not_used(harness, expiry):
    credentials = json.loads(harness.credentials)
    credentials["claudeAiOauth"]["expiresAt"] = expiry
    harness.credentials = json.dumps(credentials)
    harness.engine.tick()
    assert not harness.sends


@pytest.mark.parametrize("raw", ["{", "[]", '{"key": "invalid"}'])
def test_corrupt_guard_does_not_authorize_requests(harness, raw):
    (harness.switcher.backup_dir / warmup.STATE_FILENAME).write_text(raw)
    harness.engine.tick()
    assert not harness.sends


def test_claim_expires_conservatively(harness):
    harness.engine.tick()
    harness.now += warmup.WINDOW_S + warmup.TIMEOUT_S + 1
    harness.account = replace(harness.account, usage=replace(harness.account.usage, fetched_at=harness.now))
    harness.engine.tick()
    assert len(harness.sends) == 2


def test_prompt_is_isolated_and_does_not_expose_or_copy_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "wrong-key")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://wrong.invalid")
    monkeypatch.setenv("NODE_OPTIONS", "--require injected.js")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/wrong")
    seen = []

    def run(argv, **kwargs):
        directory = Path(kwargs["cwd"])
        seen.append(directory)
        assert directory.is_dir()
        if os.name != "nt":
            assert directory.stat().st_mode & 0o777 == 0o700
        assert kwargs["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "fake-access"
        assert kwargs["env"]["HOME"] == str(directory)
        assert kwargs["env"]["CLAUDE_CONFIG_DIR"] == str(directory / ".claude")
        assert not any(k.startswith("ANTHROPIC_") for k in kwargs["env"])
        assert "NODE_OPTIONS" not in kwargs["env"]
        assert "fake-access" not in str(argv)
        assert kwargs["input"] == "Reply OK."
        assert kwargs["timeout"] == 60
        assert argv[argv.index("--model") + 1] == "haiku"
        assert argv[argv.index("--tools") + 1] == ""
        assert argv[argv.index("--setting-sources") + 1] == ""
        assert "--no-session-persistence" in argv
        assert "--strict-mcp-config" in argv
        assert not list(directory.rglob(".credentials.json"))
        return subprocess.CompletedProcess(argv, 0, '{"type":"result","is_error":false}')

    monkeypatch.setattr(warmup.spawn, "run", run)
    assert warmup._run_prompt("/fake/claude", "fake-access", tmp_path)
    assert not seen[0].exists()


def test_opt_in_setting_is_strict(tmp_path):
    assert not AutoSwitchSettings().warmup_five_hour
    set_setting(tmp_path, "autoswitch.warmupFiveHour", "true")
    assert load_settings(tmp_path).warmup_five_hour
    set_setting(tmp_path, "autoswitch.warmupFiveHour", "false")
    assert not load_settings(tmp_path).warmup_five_hour
    (tmp_path / "settings.json").write_text('{"autoswitch":{"warmupFiveHour":"false"}}')
    assert not load_settings(tmp_path).warmup_five_hour


def test_personal_identity_without_organization(harness, monkeypatch):
    harness.identity["organizationUuid"] = ""
    harness.account = replace(harness.account, org_uuid="")
    monkeypatch.setattr(warmup.oauth, "fetch_oauth_profile", lambda token: {
        **IDENTITY, "organizationUuid": None,
    })
    harness.engine.tick()
    assert len(harness.sends) == 1


def test_timeout_cleans_disposable_profile_and_preserves_guard(harness, monkeypatch):
    seen = []
    monkeypatch.setattr(warmup, "_run_prompt", RUN_PROMPT)

    def timeout(argv, **kwargs):
        seen.append(Path(kwargs["cwd"]))
        raise subprocess.TimeoutExpired(argv, 60, output="sensitive-output")

    monkeypatch.setattr(warmup.spawn, "run", timeout)
    harness.engine.tick()
    assert seen and not seen[0].exists()
    assert warmup._read_claims(harness.switcher.backup_dir / warmup.STATE_FILENAME)
    assert "sensitive-output" not in str(harness.events)


@pytest.mark.parametrize("output", ["sensitive-output", '{"type":"result","is_error":true}'])
def test_unsuccessful_output_is_not_reported(harness, monkeypatch, output):
    monkeypatch.setattr(warmup, "_run_prompt", RUN_PROMPT)
    monkeypatch.setattr(warmup.spawn, "run", lambda *a, **kw:
                        subprocess.CompletedProcess(a, 1, output, "sensitive-stderr"))
    harness.engine.tick()
    assert "unconfirmed" in harness.events[0]
    assert "sensitive" not in str(harness.events)
