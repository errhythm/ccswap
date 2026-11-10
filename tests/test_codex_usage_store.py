"""Disk usage snapshots use synthetic accounts and mocked HTTP only."""

from __future__ import annotations

import http.client
import io
import json
import os
import urllib.error
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace

import pytest

from claude_swap import codex, codex_usage, codex_usage_store, fsutil
from claude_swap.codex_usage import CodexUsageError
from claude_swap.exceptions import LockError
from claude_swap.locking import FileLock
from claude_swap.usage_store import SERVE_TTL_S, UsageEntry
from tests.test_codex import _auth_with_plan, _write_live, switcher


@pytest.fixture
def saved(switcher, monkeypatch):
    instance, home = switcher
    auth = _auth_with_plan("one@example.com", "synthetic-account-id", "plus")
    _write_live(home, auth)
    instance.add_account(assume_yes=True)
    monkeypatch.setattr(codex.time, "time", lambda: 1_800_000_000.0)
    return instance, home, auth


def _path(instance):
    return instance.provider_dir / "usage-1.json"


def _record(instance):
    return json.loads(_path(instance).read_text(encoding="utf-8"))


def _usage():
    return {
        "weekly": {"pct": 42.0, "resets_at": "2027-01-22T08:00:00Z"},
        "five_hour": {"pct": 12.5, "resets_at": "2027-01-15T13:00:00Z"},
        "credits": {"has_credits": True, "unlimited": False, "balance": 123.5},
        "credit_allowance": {"remaining": 450.0, "limit": 500.0,
                             "resets_at": "2027-02-01T00:00:00Z"},
        "reset_credits": {"available": 2, "expires_at": "2027-01-20T00:00:00Z"},
    }


def test_fetch_writes_exact_sanitized_record(saved, monkeypatch):
    instance, _, auth = saved
    payload = {
        "rate_limit": {
            "primary_window": {"used_percent": 42, "limit_window_seconds": 604800,
                               "reset_at": 1_800_604_800},
            "secondary_window": {"used_percent": 12.5, "limit_window_seconds": 18000,
                                 "reset_at": 1_800_018_000},
        },
        "credits": {"has_credits": True, "unlimited": False, "balance": "123.5"},
        "spend_control": {"individual_limit": {
            "remaining": 450, "limit": 500, "reset_at": "2027-02-01T00:00:00Z"}},
        "rate_limit_reset_credits": {"available_count": 2},
        **auth,
        "account_id": "synthetic-account-id", "cookie": "synthetic-cookie",
    }
    details = {"available_count": 2, "credits": [
        {"status": "available", "expires_at": "2027-01-20T00:00:00Z",
         "id": "synthetic-credit-id"},
        {"status": "available", "expires_at": "2027-01-21T00:00:00Z"},
    ]}

    class Response:
        def __init__(self, value):
            self.value = value

        def read(self):
            return json.dumps(self.value).encode()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(codex_usage.urllib.request, "urlopen", lambda request, **_: Response(
        details if request.full_url.endswith("reset-credits") else payload))
    instance.accounts_snapshot()
    assert _record(instance) == {
        "schemaVersion": 1, "number": 1, "email": "one@example.com", "planType": "plus",
        "slotGeneration": instance._read_sequence()["accounts"]["1"]["usageGeneration"],
        "fetchedAt": 1_800_000_000.0,
        "windows": {
            "weekly": {"usedPercent": 42.0, "windowMinutes": 10080,
                       "resetsAt": "2027-01-22T08:00:00Z"},
            "fiveHour": {"usedPercent": 12.5, "windowMinutes": 300,
                         "resetsAt": "2027-01-15T13:00:00Z"},
        },
        "credits": {"hasCredits": True, "unlimited": False, "balance": 123.5},
        "allowance": {"remaining": 450.0, "limit": 500.0, "resetsAt": "2027-02-01T00:00:00Z"},
        "bankedResets": {"count": 2, "earliestExpiry": "2027-01-20T00:00:00Z"},
        "lastError": None,
    }
    text = _path(instance).read_text(encoding="utf-8")
    for secret in (*auth["tokens"].values(), "synthetic-cookie", "synthetic-credit-id",
                   '"tokens"', '"account_id"', '"accountId"', '"id_token"',
                   '"access_token"', '"refresh_token"', '"cookie"', '"claims"'):
        assert secret not in text
    if os.name != "nt":
        assert _path(instance).stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("restart", [False, True])
def test_failure_keeps_last_good_and_only_records_class_and_time(saved, monkeypatch, restart):
    instance, _, _ = saved
    monkeypatch.setattr(codex, "fetch_codex_usage", lambda *_a, **_kw: _usage())
    instance.accounts_snapshot()
    good = _record(instance)
    if restart:
        instance = codex.CodexAccountSwitcher()
        assert instance._usage_cache == {}

    def fail(*_args, **_kwargs):
        raise CodexUsageError("Bearer synthetic-secret; account-id=synthetic-account-id")

    attempted = 1_800_000_000.0 + SERVE_TTL_S + 1
    monkeypatch.setattr(codex.time, "time", lambda: attempted)
    monkeypatch.setattr(codex, "fetch_codex_usage", fail)
    instance.accounts_snapshot()
    assert _record(instance) == dict(good, lastError={
        "class": "CodexUsageError", "attemptedAt": attempted,
    })
    assert "synthetic-secret" not in _path(instance).read_text()
    # The disk snapshot does not seed the serving cache on a new process.
    assert (instance._usage_cache["1"].last_good is None) == restart
    monkeypatch.setattr(codex, "fetch_codex_usage", lambda *_a, **_kw: _usage())
    instance.accounts_snapshot()
    assert _record(instance)["lastError"] is None
    assert _record(instance)["fetchedAt"] == attempted


def test_first_failure_has_no_measurement(saved, monkeypatch):
    instance, _, _ = saved

    def fail(*_args, **_kwargs):
        raise CodexUsageError("synthetic-secret", status_code=429)

    monkeypatch.setattr(codex, "fetch_codex_usage", fail)
    instance.accounts_snapshot()
    assert _record(instance) == {
        "schemaVersion": 1, "number": 1, "email": "one@example.com", "planType": "plus",
        "slotGeneration": instance._read_sequence()["accounts"]["1"]["usageGeneration"],
        "fetchedAt": None, "windows": {}, "credits": None, "allowance": None,
        "bankedResets": None,
        "lastError": {"class": "CodexUsageError", "attemptedAt": 1_800_000_000.0},
    }


def test_cached_or_ineligible_usage_does_not_rewrite_file(saved, monkeypatch):
    instance, _, _ = saved
    monkeypatch.setattr(codex, "fetch_codex_usage", lambda *_a, **_kw: _usage())
    instance.accounts_snapshot()
    before = _path(instance).stat().st_mtime_ns
    monkeypatch.setattr(codex, "fetch_codex_usage", lambda *_a, **_kw: pytest.fail("fetch"))
    monkeypatch.setattr(
        codex_usage_store, "atomic_write_text", lambda *_a, **_kw: pytest.fail("write"))
    instance.accounts_snapshot()
    monkeypatch.setattr(codex.time, "time", lambda: 1_800_000_000.0 + SERVE_TTL_S + 1)
    instance.accounts_snapshot(fetch=set())
    assert _path(instance).stat().st_mtime_ns == before


def test_remove_deletes_usage_and_prevents_late_fetch_publication(saved, monkeypatch):
    instance, _, _ = saved
    monkeypatch.setattr(codex, "fetch_codex_usage", lambda *_a, **_kw: _usage())
    instance.accounts_snapshot()
    account = instance._read_sequence()["accounts"]["1"]
    entry = instance._usage_cache["1"]
    instance.remove_account("1", assume_yes=True)
    assert not _path(instance).exists()
    assert "1" not in instance._usage_cache
    codex_usage_store.write_usage(instance, "1", account, entry, 1_800_000_001.0)
    assert not _path(instance).exists()


def test_atomic_write_reuses_helper_and_cache_lock(saved, monkeypatch):
    instance, _, _ = saved
    monkeypatch.setattr(codex, "fetch_codex_usage", lambda *_a, **_kw: _usage())
    original = fsutil.atomic_write_text
    calls = []

    def write(path, text, **kwargs):
        calls.append(path)
        contender = FileLock(instance.provider_dir / ".usage.lock", timeout=0)
        assert not contender.acquire()
        assert kwargs == {"follow_symlinks": False}
        assert not path.exists()
        original(path, text, **kwargs)

    monkeypatch.setattr(codex_usage_store, "atomic_write_text", write)
    instance.accounts_snapshot()
    assert calls == [_path(instance)]
    assert not list(instance.provider_dir.glob("*.tmp"))


def test_failed_atomic_replace_keeps_previous_file_and_live_usage(saved, monkeypatch):
    instance, _, _ = saved
    monkeypatch.setattr(codex, "fetch_codex_usage", lambda *_a, **_kw: _usage())
    instance.accounts_snapshot()
    before = _path(instance).read_bytes()
    monkeypatch.setattr(codex.time, "time", lambda: 1_800_000_000.0 + SERVE_TTL_S + 1)
    monkeypatch.setattr(codex, "fetch_codex_usage", lambda *_a, **_kw: {"weekly": {"pct": 90}})

    def fail(*_args):
        raise OSError("synthetic replace failure")

    monkeypatch.setattr(fsutil, "replace_with_retry", fail)
    snapshot = instance.accounts_snapshot()
    assert snapshot.accounts[0].usage.last_good == {"weekly": {"pct": 90}}
    assert snapshot.accounts[0].usage.last_error is None
    assert _path(instance).read_bytes() == before
    assert not list(instance.provider_dir.glob("*.tmp"))


def test_windows_usage_path_and_transient_replace_retry(saved, monkeypatch):
    instance, _, _ = saved
    instance.provider_dir = instance.provider_dir / "quota space 雪"
    instance.provider_dir.mkdir()
    monkeypatch.setattr(codex, "fetch_codex_usage", lambda *_a, **_kw: _usage())
    monkeypatch.setattr(fsutil, "sys", SimpleNamespace(platform="win32"))
    original = os.replace
    calls = []

    def replace(source, target):
        calls.append(target)
        if len(calls) == 1:
            error = PermissionError("synthetic sharing violation")
            error.winerror = 32
            raise error
        original(source, target)

    monkeypatch.setattr(fsutil.os, "replace", replace)
    instance.accounts_snapshot()
    assert calls == [_path(instance), _path(instance)]
    assert _record(instance)["windows"]["weekly"]["usedPercent"] == 42


@pytest.mark.parametrize("failure", [False, True])
def test_redeem_usage_publishes_without_changing_memory_cache(saved, monkeypatch, failure):
    instance, _, _ = saved

    def fetch(*_args, **_kwargs):
        if failure:
            raise CodexUsageError("synthetic-secret")
        return _usage()

    monkeypatch.setattr(codex, "fetch_codex_usage", fetch)
    if failure:
        with pytest.raises(CodexUsageError):
            instance._fetch_redeem_usage("1")
        assert _record(instance)["lastError"]["class"] == "CodexUsageError"
    else:
        assert instance._fetch_redeem_usage("1") == _usage()
        assert _record(instance)["windows"]["weekly"]["usedPercent"] == 42
    assert instance._usage_cache == {}


def test_failure_resanitizes_existing_file(saved, monkeypatch):
    instance, _, _ = saved
    monkeypatch.setattr(codex, "fetch_codex_usage", lambda *_a, **_kw: _usage())
    instance.accounts_snapshot()
    previous = _record(instance)
    previous["access_token"] = "synthetic-secret"
    previous["windows"]["weekly"]["accountId"] = "synthetic-account-id"
    previous["credits"]["balance"] = "synthetic-secret"
    _path(instance).write_text(json.dumps(previous), encoding="utf-8")
    account = instance._read_sequence()["accounts"]["1"]
    codex_usage_store.write_usage(instance, "1", account, UsageEntry(), 1_800_000_001.0,
                                 CodexUsageError("synthetic-secret"))
    text = _path(instance).read_text()
    assert "synthetic" not in text
    assert "balance" not in _record(instance)["credits"]
    assert _record(instance)["windows"]["weekly"]["usedPercent"] == 42


@pytest.mark.parametrize("failure", [False, True])
def test_inactive_refresh_attempt_persists_final_result(saved, monkeypatch, failure):
    instance, home, auth = saved
    _write_live(home, _auth_with_plan("two@example.com", "synthetic-two", "plus"))
    instance.add_account(assume_yes=True)
    refreshed = dict(auth, tokens=dict(auth["tokens"], access_token="synthetic-refreshed"))

    def fetch(auth, **_kwargs):
        if auth["tokens"]["access_token"] != "synthetic-refreshed":
            raise CodexUsageError("synthetic-secret", status_code=401)
        return _usage()

    def refresh(_auth):
        if failure:
            raise codex.ConfigError("synthetic-refresh-secret")
        return refreshed

    monkeypatch.setattr(codex, "fetch_codex_usage", fetch)
    monkeypatch.setattr(codex, "refresh_codex_auth", refresh)
    instance.accounts_snapshot(fetch={"1"})
    if failure:
        assert _record(instance)["lastError"] == {
            "class": "ConfigError", "attemptedAt": 1_800_000_000.0,
        }
        assert _record(instance)["fetchedAt"] is None
    else:
        assert _record(instance)["lastError"] is None
        assert _record(instance)["windows"]["weekly"]["usedPercent"] == 42
    assert "synthetic" not in _path(instance).read_text()


def test_busy_cache_does_not_discard_successful_usage(saved, monkeypatch):
    instance, _, _ = saved
    monkeypatch.setattr(codex, "fetch_codex_usage", lambda *_a, **_kw: _usage())
    with FileLock(instance.provider_dir / ".usage.lock"):
        snapshot = instance.accounts_snapshot()
    assert snapshot.accounts[0].usage.last_good == _usage()
    assert snapshot.accounts[0].usage.last_error is None
    assert not _path(instance).exists()


def test_late_attempt_does_not_overwrite_newer_snapshot(saved, monkeypatch):
    instance, _, _ = saved
    monkeypatch.setattr(codex, "fetch_codex_usage", lambda *_a, **_kw: _usage())
    instance.accounts_snapshot()
    before = _path(instance).read_bytes()
    account = instance._read_sequence()["accounts"]["1"]
    codex_usage_store.write_usage(instance, "1", account,
                                 UsageEntry(last_good={"weekly": {"pct": 1}},
                                            fetched_at=1_799_999_999.0), 1_799_999_999.0)
    assert _path(instance).read_bytes() == before


def test_replaced_slot_rejects_inflight_snapshot(saved, monkeypatch):
    instance, _, _ = saved

    def fetch(*_args, **_kwargs):
        data = instance._read_sequence()
        data["accounts"]["1"]["email"] = "replacement@example.com"
        instance._write_sequence(data)
        return _usage()

    monkeypatch.setattr(codex, "fetch_codex_usage", fetch)
    instance.accounts_snapshot()
    assert not _path(instance).exists()


@pytest.mark.parametrize("previous", ["not JSON", "[]", '{"schemaVersion": 999}'])
def test_corrupt_snapshot_does_not_break_fetch(saved, monkeypatch, previous):
    instance, _, _ = saved
    _path(instance).write_text(previous, encoding="utf-8")
    monkeypatch.setattr(codex, "fetch_codex_usage", lambda *_a, **_kw: {"weekly": {"pct": 10}})
    instance.accounts_snapshot()
    assert _record(instance)["windows"] == {
        "weekly": {"usedPercent": 10, "windowMinutes": 10080},
    }
    assert _record(instance)["credits"] is None
    assert _record(instance)["allowance"] is None
    assert _record(instance)["bankedResets"] is None


@pytest.mark.parametrize("replacement", ["different-email", "same-email", "workspace", "api-key"])
@pytest.mark.parametrize("restart", [False, True])
def test_add_replacement_invalidates_generation(saved, monkeypatch, replacement, restart):
    instance, home, auth = saved
    monkeypatch.setattr(codex, "fetch_codex_usage", lambda *_a, **_kw: _usage())
    instance.accounts_snapshot()
    old_account = instance._read_sequence()["accounts"]["1"]
    old_entry = instance._usage_cache["1"]
    old_record = _record(instance)
    replacements = {
        "different-email": _auth_with_plan("two@example.com", "synthetic-two", "plus"),
        "same-email": auth,
        "workspace": _auth_with_plan("one@example.com", "synthetic-workspace-two", "team"),
        "api-key": {"auth_mode": "api_key", "OPENAI_API_KEY": "synthetic-key"},
    }
    _write_live(home, replacements[replacement])
    instance.add_account(slot=1, assume_yes=True)
    account = instance._read_sequence()["accounts"]["1"]
    assert account["usageGeneration"] != old_account["usageGeneration"]
    assert not _path(instance).exists()
    assert "1" not in instance._usage_cache
    codex_usage_store.write_usage(instance, "1", old_account, old_entry, 1_800_000_001.0)
    assert not _path(instance).exists()
    if restart:
        instance = codex.CodexAccountSwitcher()
    # Even another process's untagged in-memory value cannot seed the new slot.
    instance._usage_cache["1"] = old_entry
    monkeypatch.setattr(codex.time, "time", lambda: 1_800_000_000.0 + SERVE_TTL_S + 1)

    def fail(*_args, **_kwargs):
        raise CodexUsageError("synthetic failure")

    monkeypatch.setattr(codex, "fetch_codex_usage", fail)
    instance.accounts_snapshot()
    if replacement == "api-key":
        assert not _path(instance).exists()
    else:
        assert _record(instance)["fetchedAt"] is None
        assert _record(instance)["windows"] == {}
        # A stale file from an older generation must also be discarded.
        _path(instance).write_text(json.dumps(old_record), encoding="utf-8")
        instance.accounts_snapshot()
        assert _record(instance)["fetchedAt"] is None
        assert _record(instance)["slotGeneration"] == account["usageGeneration"]


def test_replacement_waits_for_writer_after_roster_check(saved, monkeypatch):
    instance, home, _ = saved
    account = instance._read_sequence()["accounts"]["1"]
    checked, release, replacing = Event(), Event(), Event()
    original_write = codex_usage_store.atomic_write_text
    original_acquire = FileLock.acquire
    errors = []

    def write(path, text, **kwargs):
        checked.set()
        assert release.wait(5)
        original_write(path, text, **kwargs)

    def acquire(lock, timeout=None):
        if lock.lock_path.name == ".usage.lock" and lock.timeout != 0:
            replacing.set()
        return original_acquire(lock, timeout)

    def replace():
        try:
            instance.add_account(slot=1, assume_yes=True)
        except BaseException as exc:
            errors.append(exc)

    monkeypatch.setattr(codex_usage_store, "atomic_write_text", write)
    monkeypatch.setattr(FileLock, "acquire", acquire)
    writer = Thread(target=codex_usage_store.write_usage, args=(
        instance, "1", account, UsageEntry(last_good=_usage(), fetched_at=100), 100))
    replacer = Thread(target=replace)
    _write_live(home, _auth_with_plan("one@example.com", "synthetic-two", "team"))
    writer.start()
    try:
        assert checked.wait(5)
        replacer.start()
        assert replacing.wait(5)
        assert instance._read_sequence()["accounts"]["1"] == account
    finally:
        release.set()
        writer.join(5)
        if replacer.ident is not None:
            replacer.join(5)
    assert not writer.is_alive() and not replacer.is_alive()
    assert errors == []
    current = instance._read_sequence()["accounts"]["1"]
    assert current["usageGeneration"] != account["usageGeneration"]
    assert not _path(instance).exists()


@pytest.mark.parametrize("operation", ["remove", "replace"])
@pytest.mark.parametrize("failure", ["lock", "unlink"])
def test_cache_cleanup_failure_leaves_account_retryable(saved, monkeypatch, operation, failure):
    instance, home, _ = saved
    monkeypatch.setattr(codex, "fetch_codex_usage", lambda *_a, **_kw: _usage())
    instance.accounts_snapshot()
    roster = instance.sequence_file.read_bytes()
    credential = instance._credential_path("1").read_bytes()
    snapshot = _path(instance).read_bytes()
    if operation == "replace":
        _write_live(home, _auth_with_plan("two@example.com", "synthetic-two", "plus"))

    def mutate():
        if operation == "remove":
            instance.remove_account("1", assume_yes=True)
        else:
            instance.add_account(slot=1, assume_yes=True)

    original_unlink = Path.unlink
    original_acquire = FileLock.acquire

    def unlink(path, *args, **kwargs):
        if path == _path(instance):
            raise PermissionError("synthetic unlink failure")
        return original_unlink(path, *args, **kwargs)

    def acquire(lock, timeout=None):
        return original_acquire(lock, 0 if lock.lock_path.name == ".usage.lock" else timeout)

    with monkeypatch.context() as patch:
        if failure == "lock":
            with FileLock(instance.provider_dir / ".usage.lock"):
                patch.setattr(FileLock, "acquire", acquire)
                with pytest.raises(LockError):
                    mutate()
        else:
            patch.setattr(Path, "unlink", unlink)
            with pytest.raises(PermissionError):
                mutate()
    assert instance.sequence_file.read_bytes() == roster
    assert instance._credential_path("1").read_bytes() == credential
    assert _path(instance).read_bytes() == snapshot
    mutate()
    assert not _path(instance).exists()


@pytest.mark.parametrize("timestamp", [
    "0001-01-01T00:00:00+01:00", "9999-12-31T23:59:59-01:00",
])
def test_failure_omits_overflowing_nested_timestamp(saved, monkeypatch, timestamp):
    instance, _, _ = saved
    monkeypatch.setattr(codex, "fetch_codex_usage", lambda *_a, **_kw: _usage())
    instance.accounts_snapshot()
    previous = _record(instance)
    previous["windows"]["weekly"]["resetsAt"] = timestamp
    _path(instance).write_text(json.dumps(previous), encoding="utf-8")
    instance._usage_cache.clear()

    def fail(*_args, **_kwargs):
        raise CodexUsageError("synthetic failure")

    monkeypatch.setattr(codex, "fetch_codex_usage", fail)
    result = instance.accounts_snapshot()
    assert result.accounts[0].usage.last_error
    assert _record(instance)["windows"]["weekly"] == {
        "usedPercent": 42.0, "windowMinutes": 10080,
    }


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("failure_first", [False, True])
def test_measurement_and_error_order_independently(saved, existing, failure_first):
    instance, _, _ = saved
    account = instance._read_sequence()["accounts"]["1"]
    if existing:
        codex_usage_store.write_usage(instance, "1", account,
                                     UsageEntry(last_good={"weekly": {"pct": 1}},
                                                fetched_at=50), 50)
    success = (UsageEntry(last_good=_usage(), fetched_at=100), 100, None)
    failure = (UsageEntry(), 200, CodexUsageError("synthetic failure"))
    for entry, attempted_at, error in ((failure, success) if failure_first else (success, failure)):
        codex_usage_store.write_usage(instance, "1", account, entry, attempted_at, error)
    record = _record(instance)
    assert record["fetchedAt"] == 100
    assert record["windows"]["weekly"]["usedPercent"] == 42
    assert record["lastError"] == {"class": "CodexUsageError", "attemptedAt": 200}
    codex_usage_store.write_usage(instance, "1", account,
                                 UsageEntry(last_good=_usage(), fetched_at=200), 200)
    assert _record(instance)["lastError"] is None
    codex_usage_store.write_usage(instance, "1", account, UsageEntry(), 150,
                                 CodexUsageError("older failure"))
    assert _record(instance)["lastError"] is None


def test_legacy_slot_gets_stable_generation_before_fetch(saved, monkeypatch):
    instance, _, _ = saved
    data = instance._read_sequence()
    del data["accounts"]["1"]["usageGeneration"]
    instance._write_sequence(data)
    monkeypatch.setattr(codex, "fetch_codex_usage", lambda *_a, **_kw: _usage())
    instance.accounts_snapshot()
    generation = instance._read_sequence()["accounts"]["1"]["usageGeneration"]
    assert len(generation) == 32
    assert _record(instance)["slotGeneration"] == generation
    instance = codex.CodexAccountSwitcher()
    instance.accounts_snapshot()
    assert _record(instance)["slotGeneration"] == generation


@pytest.mark.parametrize("failure_first", [False, True])
def test_success_clears_equally_recent_error_in_both_orders(saved, failure_first):
    instance, _, _ = saved
    account = instance._read_sequence()["accounts"]["1"]
    success = (UsageEntry(last_good=_usage(), fetched_at=100), None)
    failure = (UsageEntry(), CodexUsageError("synthetic failure"))
    for entry, error in ((failure, success) if failure_first else (success, failure)):
        codex_usage_store.write_usage(instance, "1", account, entry, 100, error)
    assert _record(instance)["fetchedAt"] == 100
    assert _record(instance)["lastError"] is None


@pytest.mark.parametrize("failure", [False, True])
@pytest.mark.parametrize("corruption", [
    "oversized-integer", "recursion-error", "oversized-whitespace", "oversized-string",
    "invalid-utf8",
])
def test_cache_parse_failure_does_not_break_fetch(saved, monkeypatch, corruption, failure):
    instance, _, _ = saved
    if corruption == "oversized-integer":
        previous = '{"x":' + "9" * 5000 + "}"
        parse_error = ValueError
    elif corruption == "recursion-error":
        previous = '{"x":' + "[" * 5000 + "0" + "]" * 5000 + "}"
        parse_error = RecursionError
        loads = json.loads

        def parse(text, *args, **kwargs):
            # Parser recursion limits vary by interpreter and available stack.
            if text == previous:
                raise RecursionError("synthetic parser limit")
            return loads(text, *args, **kwargs)

        monkeypatch.setattr(codex_usage_store.json, "loads", parse)
    elif corruption == "oversized-whitespace":
        previous = " " * (codex_usage_store._MAX_SNAPSHOT_BYTES + 1)
    elif corruption == "oversized-string":
        previous = '"' + "x" * (codex_usage_store._MAX_SNAPSHOT_BYTES - 1) + '"'
    else:
        previous = None
    if corruption in {"oversized-integer", "recursion-error"}:
        with pytest.raises(parse_error):
            json.loads(previous)
    _path(instance).write_bytes(previous.encode("utf-8") if previous is not None else b"\xff")

    def fetch(*_args, **_kwargs):
        if failure:
            raise CodexUsageError("synthetic failure")
        return _usage()

    monkeypatch.setattr(codex, "fetch_codex_usage", fetch)
    snapshot = instance.accounts_snapshot()
    record = _record(instance)
    assert "x" not in record
    if failure:
        assert snapshot.accounts[0].usage.last_error == "synthetic failure"
        assert record["fetchedAt"] is None
        assert record["windows"] == {}
        assert record["credits"] is None
        assert record["allowance"] is None
        assert record["bankedResets"] is None
        assert record["lastError"] == {
            "class": "CodexUsageError", "attemptedAt": 1_800_000_000.0,
        }
    else:
        assert snapshot.accounts[0].usage.last_good == _usage()
        assert record["fetchedAt"] == 1_800_000_000.0
        assert record["windows"]["weekly"]["usedPercent"] == 42
        assert record["lastError"] is None


@pytest.mark.parametrize("inactive_retry", [False, True])
def test_response_body_failure_publishes_and_reraises(saved, monkeypatch, inactive_retry):
    instance, home, auth = saved
    with monkeypatch.context() as seed:
        seed.setattr(codex, "fetch_codex_usage", lambda *_a, **_kw: _usage())
        instance.accounts_snapshot()
    good = _record(instance)
    instance._usage_cache.clear()
    if inactive_retry:
        _write_live(home, _auth_with_plan("two@example.com", "synthetic-two", "plus"))
        instance.add_account(assume_yes=True)
    refreshed = dict(auth, tokens=dict(auth["tokens"], access_token="synthetic-refreshed"))
    refresh_calls = []

    def refresh(value):
        refresh_calls.append(value)
        return refreshed

    failure = http.client.IncompleteRead(b"Bearer synthetic-partial-body", 100)

    class Response:
        def read(self):
            raise failure

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    requests = []

    def urlopen(request, **_kwargs):
        requests.append(request)
        if inactive_retry and len(requests) == 1:
            raise urllib.error.HTTPError(request.full_url, 401, "synthetic-message", {}, None)
        return Response()

    attempted = 1_800_000_100.0
    monkeypatch.setattr(codex.time, "time", lambda: attempted)
    monkeypatch.setattr(codex, "refresh_codex_auth", refresh)
    monkeypatch.setattr(codex_usage.urllib.request, "urlopen", urlopen)
    with pytest.raises(http.client.IncompleteRead) as raised:
        instance.accounts_snapshot(fetch={"1"})
    assert raised.value is failure
    assert len(requests) == (2 if inactive_retry else 1)
    assert refresh_calls == ([auth] if inactive_retry else [])
    assert _record(instance) == dict(good, lastError={
        "class": "IncompleteRead", "attemptedAt": attempted,
    })
    text = _path(instance).read_text(encoding="utf-8")
    assert "synthetic" not in text
    assert str(failure) not in text


@pytest.mark.parametrize("failure", [False, True])
def test_older_completion_sanitizes_retained_error_class(saved, monkeypatch, failure):
    instance, _, _ = saved
    monkeypatch.setattr(codex.time, "time", lambda: 50)
    monkeypatch.setattr(codex, "fetch_codex_usage", lambda *_a, **_kw: _usage())
    instance.accounts_snapshot()
    previous = _record(instance)
    previous["lastError"] = {
        "class": "Bearer synthetic-secret; raw-message", "attemptedAt": 200,
    }
    _path(instance).write_text(json.dumps(previous), encoding="utf-8")
    instance._usage_cache.clear()

    def fetch(*_args, **_kwargs):
        if failure:
            raise CodexUsageError("synthetic failure")
        return _usage()

    monkeypatch.setattr(codex.time, "time", lambda: 100)
    monkeypatch.setattr(codex, "fetch_codex_usage", fetch)
    instance.accounts_snapshot()
    assert _record(instance) == dict(previous, fetchedAt=50 if failure else 100,
                                    lastError={"class": "Exception", "attemptedAt": 200})
    assert "synthetic" not in _path(instance).read_text(encoding="utf-8")


@pytest.mark.parametrize("label", ["UnknownError", "Bearer synthetic-secret; raw-message"])
def test_new_unknown_error_class_uses_generic_label(saved, label):
    instance, _, _ = saved
    account = instance._read_sequence()["accounts"]["1"]
    error = type(label, (Exception,), {})("synthetic message")
    codex_usage_store.write_usage(instance, "1", account, UsageEntry(), 100, error)
    assert _record(instance)["lastError"] == {"class": "Exception", "attemptedAt": 100}
    assert "synthetic" not in _path(instance).read_text(encoding="utf-8")


@pytest.mark.parametrize("timestamp", [
    1e300, 1_800_000_100 + 86401, 253_402_300_800, -1, float("nan"), float("inf"),
    "NaN", "-Infinity", True, False, None,
])
@pytest.mark.parametrize("field", ["fetchedAt", "attemptedAt"])
@pytest.mark.parametrize("failure", [False, True])
def test_invalid_saved_attempt_timestamp_is_missing(saved, monkeypatch, timestamp, field, failure):
    instance, _, _ = saved
    account = instance._read_sequence()["accounts"]["1"]
    codex_usage_store.write_usage(instance, "1", account,
                                 UsageEntry(last_good=_usage(), fetched_at=1_800_000_000),
                                 1_800_000_000)
    previous = _record(instance)
    if field == "fetchedAt":
        previous["fetchedAt"] = timestamp
    else:
        previous["lastError"] = {"class": "TimeoutError", "attemptedAt": timestamp}
    _path(instance).write_text(json.dumps(previous), encoding="utf-8")

    attempted_at = 1_800_000_100
    monkeypatch.setattr(codex_usage_store, "time", SimpleNamespace(time=lambda: attempted_at))
    codex_usage_store.write_usage(
        instance, "1", account,
        UsageEntry() if failure else UsageEntry(last_good={"weekly": {"pct": 10}},
                                               fetched_at=attempted_at),
        attempted_at, CodexUsageError("synthetic failure") if failure else None,
    )
    record = _record(instance)
    if failure:
        assert record["lastError"] == {
            "class": "CodexUsageError", "attemptedAt": attempted_at,
        }
        if field == "fetchedAt":
            assert record["fetchedAt"] is None
            assert record["windows"] == {}
            assert record["credits"] is None
            assert record["allowance"] is None
            assert record["bankedResets"] is None
        else:
            assert record["fetchedAt"] == previous["fetchedAt"]
            assert record["windows"] == previous["windows"]
    else:
        assert record["fetchedAt"] == attempted_at
        assert record["windows"]["weekly"]["usedPercent"] == 10
        assert record["lastError"] is None


@pytest.mark.parametrize("offset", [1, 86400])
def test_saved_attempts_within_clock_skew_keep_independent_ordering(saved, offset):
    instance, _, _ = saved
    account = instance._read_sequence()["accounts"]["1"]
    attempted_at = 1_800_000_000
    codex_usage_store.write_usage(instance, "1", account,
                                 UsageEntry(last_good=_usage(), fetched_at=attempted_at + offset),
                                 attempted_at + offset)
    previous = _record(instance)
    previous["lastError"] = {"class": "TimeoutError", "attemptedAt": attempted_at + offset}
    _path(instance).write_text(json.dumps(previous), encoding="utf-8")
    codex_usage_store.write_usage(instance, "1", account,
                                 UsageEntry(last_good={"weekly": {"pct": 10}},
                                            fetched_at=attempted_at), attempted_at)
    assert _record(instance) == previous


@pytest.mark.parametrize("newer_first", [False, True])
@pytest.mark.parametrize("older_fails", [False, True])
@pytest.mark.parametrize("newer_fails", [False, True])
def test_suspended_attempts_keep_ordering_at_publication_time(
    saved, monkeypatch, newer_first, older_fails, newer_fails,
):
    instance, _, _ = saved
    account = instance._read_sequence()["accounts"]["1"]
    older_at = 1_800_000_000
    newer_at = older_at + 86401
    clock_calls = []

    def publication_clock():
        contender = FileLock(instance.provider_dir / ".usage.lock", timeout=0)
        assert not contender.acquire()
        # Both attempts finish after B starts, one second apart. In the
        # B-first order its snapshot/error was published just one second ago.
        now = newer_at + len(clock_calls) + 1
        clock_calls.append(now)
        return now

    monkeypatch.setattr(codex_usage_store, "time", SimpleNamespace(time=publication_clock))
    attempts = [
        (older_at, 10, TimeoutError("older failure") if older_fails else None),
        (newer_at, 20, CodexUsageError("newer failure") if newer_fails else None),
    ]
    for attempted_at, percent, error in (reversed(attempts) if newer_first else attempts):
        entry = (UsageEntry() if error else
                 UsageEntry(last_good={"weekly": {"pct": percent}}, fetched_at=attempted_at))
        codex_usage_store.write_usage(instance, "1", account, entry, attempted_at, error)

    assert clock_calls == [newer_at + 1, newer_at + 2]
    record = _record(instance)
    expected_at = newer_at if not newer_fails else (None if older_fails else older_at)
    assert record["fetchedAt"] == expected_at
    assert record["windows"] == ({} if expected_at is None else {
        "weekly": {"usedPercent": 20 if not newer_fails else 10, "windowMinutes": 10080},
    })
    assert record["lastError"] == ({
        "class": "CodexUsageError", "attemptedAt": newer_at,
    } if newer_fails else None)


@pytest.mark.parametrize("timestamp", [1e300, 1_800_000_000 + 86401])
@pytest.mark.parametrize("field", ["fetchedAt", "attemptedAt", "entry"])
def test_future_timestamps_are_filtered_against_publication_clock(
    saved, monkeypatch, timestamp, field,
):
    instance, _, _ = saved
    account = instance._read_sequence()["accounts"]["1"]
    published_at = 1_800_000_000
    monkeypatch.setattr(codex_usage_store, "time", SimpleNamespace(time=lambda: published_at))
    codex_usage_store.write_usage(instance, "1", account,
                                 UsageEntry(last_good=_usage(), fetched_at=published_at),
                                 published_at)
    previous = _record(instance)
    if field == "fetchedAt":
        previous["fetchedAt"] = timestamp
    elif field == "attemptedAt":
        previous["lastError"] = {"class": "TimeoutError", "attemptedAt": timestamp}
    _path(instance).write_text(json.dumps(previous), encoding="utf-8")

    # An attempt timestamp ahead of the publication clock must not widen
    # the plausibility cutoff for either saved data or the incoming entry.
    codex_usage_store.write_usage(
        instance, "1", account,
        UsageEntry(last_good={"weekly": {"pct": 99}},
                   fetched_at=timestamp if field == "entry" else None),
        published_at + 1,
    )
    record = _record(instance)
    assert record["fetchedAt"] == (None if field == "fetchedAt" else published_at)
    assert record["windows"] == ({} if field == "fetchedAt" else previous["windows"])
    assert record["lastError"] is None


@pytest.mark.parametrize("timestamp, expected", [
    (0, 0), (253_402_300_799.5, 253_402_300_799.5), (253_402_300_800, None),
])
def test_attempt_timestamp_unix_range_boundaries(timestamp, expected):
    assert codex_usage_store._attempt_timestamp(timestamp, 253_402_300_799) == expected


@pytest.mark.parametrize("kind", ["whitespace", "string"])
@pytest.mark.parametrize("extra_bytes", [1, 65536])
def test_snapshot_read_is_bounded(tmp_path, monkeypatch, kind, extra_bytes):
    cap = codex_usage_store._MAX_SNAPSHOT_BYTES
    content = (b" " * (cap + extra_bytes) if kind == "whitespace"
               else b'"' + b"x" * (cap + extra_bytes - 2) + b'"')
    path = tmp_path / "usage-1.json"
    path.write_bytes(content)
    assert path.stat().st_size == cap + extra_bytes
    reads = []

    class BoundedReader(io.BytesIO):
        def read(self, size=-1):
            reads.append(size)
            assert size == cap + 1
            return super().read(size)

    stream = BoundedReader(content)

    def open_snapshot(target, mode, **kwargs):
        assert target == path
        assert mode == "rb"
        return stream

    monkeypatch.setattr(Path, "open", open_snapshot)
    monkeypatch.setattr(codex_usage_store.json, "loads",
                        lambda *_a, **_kw: pytest.fail("oversized snapshot parsed"))
    assert codex_usage_store._read_snapshot(path) == {}
    assert reads == [cap + 1]
    assert stream.closed


def test_snapshot_at_size_limit_is_accepted(tmp_path):
    path = tmp_path / "usage-1.json"
    content = b'{"schemaVersion":1}'
    path.write_bytes(content.ljust(codex_usage_store._MAX_SNAPSHOT_BYTES, b" "))
    assert codex_usage_store._read_snapshot(path) == {"schemaVersion": 1}


def test_list_json_reads_published_usage_without_fetching(saved, monkeypatch):
    instance, _, _ = saved
    monkeypatch.setattr(codex, "fetch_codex_usage", lambda *_a, **_kw: _usage())
    instance.accounts_snapshot()
    monkeypatch.setattr(codex, "fetch_codex_usage", lambda *_a, **_kw: pytest.fail("fetch"))
    restarted = codex.CodexAccountSwitcher()
    payload = restarted.list_payload()
    assert payload["schemaVersion"] == 1
    row = payload["accounts"][0]
    assert row["usageStatus"] == "ok"
    assert row["usage"]["fiveHour"]["pct"] == 12.5
    assert row["usage"]["fiveHour"]["resetsAt"] == "2027-01-15T13:00:00Z"
    assert row["usage"]["weekly"]["pct"] == 42.0
    assert row["usage"]["credits"] == {"hasCredits": True, "unlimited": False, "balance": 123.5}
    assert row["usage"]["creditAllowance"]["remaining"] == 450.0
    assert row["usage"]["resetCredits"] == {"available": 2, "earliestExpiresAt": "2027-01-20T00:00:00Z"}
    assert row["usageFetchedAt"] == "2027-01-15T08:00:00Z"


def test_list_json_moves_old_usage_to_last_good(saved, monkeypatch):
    instance, _, _ = saved
    monkeypatch.setattr(codex, "fetch_codex_usage", lambda *_a, **_kw: _usage())
    instance.accounts_snapshot()
    monkeypatch.setattr(codex.time, "time", lambda: 1_800_000_000.0 + 3600)
    row = instance.list_payload()["accounts"][0]
    assert row["usageStatus"] == "unavailable" and row["usage"] is None
    assert row["lastGoodUsage"]["fiveHour"]["pct"] == 12.5
    assert row["lastGoodAgeSeconds"] == 3600


def test_list_json_reports_failure_and_ignores_replaced_slot(saved, monkeypatch):
    instance, _, _ = saved

    def fail(*_args, **_kwargs):
        raise CodexUsageError("synthetic-secret", status_code=429)

    monkeypatch.setattr(codex, "fetch_codex_usage", fail)
    instance.accounts_snapshot()
    row = instance.list_payload()["accounts"][0]
    assert row["usageStatus"] == "unavailable"
    assert row["usageError"] == "CodexUsageError"
    assert "synthetic-secret" not in json.dumps(row)

    data = instance._read_sequence()
    data["accounts"]["1"]["usageGeneration"] = "another-generation"
    instance._write_sequence(data)
    row = instance.list_payload()["accounts"][0]
    assert row["usageStatus"] == "unavailable" and "usageError" not in row
