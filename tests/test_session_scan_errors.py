"""A failed directory scan must never authorize a profile mutation."""
import os
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from ccswap.process_detection import list_sessions, scan_sessions
from ccswap import process_detection
from ccswap.session import profile_is_quiescent, scan_live_sessions
from ccswap.exceptions import SessionError
from ccswap.switcher import ClaudeAccountSwitcher
from ccswap import oauth, session as session_mod
from ccswap.models import Platform


def fail_enumeration(monkeypatch, directory, error):
    # Inject at the OS boundary used by iterdir: listdir on Python 3.12,
    # scandir on newer versions. No filesystem permission/root assumptions.
    for name in ("listdir", "scandir"):
        original = getattr(os, name)

        def listing(path, *args, _original=original, **kwargs):
            if path == directory or path == str(directory):
                raise error("synthetic directory enumeration failure")
            return _original(path, *args, **kwargs)

        monkeypatch.setattr(os, name, listing)


@pytest.mark.parametrize("error", [PermissionError, OSError, NotADirectoryError])
def test_directory_scan_failure_is_not_quiescent(temp_home, monkeypatch, error):
    switcher = ClaudeAccountSwitcher()
    profile = switcher._session_dir("2", "test@example.com")
    sessions_dir = profile / "sessions"
    sessions_dir.mkdir(parents=True)
    fail_enumeration(monkeypatch, sessions_dir, error)
    assert scan_sessions(profile) == ([], 1)
    assert scan_live_sessions(profile) == ([], 1)
    assert not profile_is_quiescent(profile)
    with pytest.raises(SessionError, match="could not be read"):
        switcher._ensure_no_live_session("2", "test@example.com", "the operation")
    # Read-only listings remain tolerant instead of raising filesystem errors.
    assert list_sessions(profile) == []


def test_profile_existence_probe_cannot_hide_scan_failure(tmp_path, monkeypatch):
    """Path.exists() can suppress errors; the scanner must decide absence."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    original_exists = Path.exists

    def exists(path):
        # Model a suppressed filesystem error at the outer profile check.
        return False if path == tmp_path else original_exists(path)

    monkeypatch.setattr(Path, "exists", exists)
    fail_enumeration(monkeypatch, sessions_dir, PermissionError)
    assert scan_live_sessions(tmp_path) == ([], 1)
    assert not profile_is_quiescent(tmp_path)


def test_directory_scan_preserves_platform_glob_matching(tmp_path, monkeypatch):
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    for name in (".json", "lower.json", "upper.JSON", "record.json.tmp"):
        (sessions_dir / name).write_text(
            json.dumps({"pid": 1, "sessionId": name}), encoding="utf-8")
    expected = sorted(path.name for path in sessions_dir.glob("*.json"))
    monkeypatch.setattr(process_detection, "is_pid_alive", lambda pid: True)
    monkeypatch.setattr(process_detection, "pid_matches_record", lambda *args: True)
    sessions, unreadable = scan_sessions(tmp_path)
    assert sorted(session.session_id for session in sessions) == expected
    assert unreadable == 0


@pytest.fixture
def profile_switcher(temp_home, monkeypatch):
    monkeypatch.setattr(Platform, "detect", classmethod(lambda cls: Platform.MACOS))
    switcher = ClaudeAccountSwitcher()
    switcher._setup_directories()
    profile = switcher._session_dir("2", "test@example.com")
    (profile / "sessions").mkdir(parents=True)
    (profile / ".credentials.json").write_text("profile credential", encoding="utf-8")
    return switcher, profile


@pytest.mark.parametrize("uncertain,marker_lands", [(True, True), (True, False), (False, True)])
def test_backup_write_preserves_uncertain_profile(
    profile_switcher, monkeypatch, caplog, uncertain, marker_lands
):
    switcher, profile = profile_switcher
    delete_keychain = Mock()
    monkeypatch.setattr(session_mod, "delete_macos_keychain_entry", delete_keychain)
    if uncertain:
        fail_enumeration(monkeypatch, profile / "sessions", PermissionError)
    if not marker_lands:
        monkeypatch.setattr(session_mod, "mark_session_stale", lambda _: False)

    switcher._write_account_credentials("2", "test@example.com", "new backup")

    assert switcher._read_account_credentials("2", "test@example.com") == "new backup"
    assert (profile / ".credentials.json").exists() == uncertain
    assert session_mod.is_session_stale(profile) == (uncertain and marker_lands)
    assert delete_keychain.call_count == (0 if uncertain else 1)
    if not marker_lands:
        assert any(record.levelname == "ERROR" for record in caplog.records)


def test_mutation_guard_does_not_discard_first_scan_uncertainty(
    profile_switcher, monkeypatch
):
    switcher, _ = profile_switcher
    scan = Mock(side_effect=[([], 1), ([SimpleNamespace(pid=12345)], 0)])
    monkeypatch.setattr(session_mod, "scan_live_sessions", scan)
    with pytest.raises(SessionError, match="could not be read"):
        switcher._ensure_no_live_session("2", "test@example.com", "the operation")
    assert scan.call_count == 1


@pytest.mark.parametrize("failure", ["root-stat", "profile-stat", "enumeration", "late-enumeration"])
def test_purge_refuses_uninspectable_session_tree(profile_switcher, monkeypatch, failure):
    switcher, profile = profile_switcher
    sessions_root = profile.parent
    original_stat = os.stat
    target = profile if failure == "profile-stat" else sessions_root

    def stat(path, *args, **kwargs):
        if path == target or path == str(target):
            raise PermissionError("synthetic stat denial")
        return original_stat(path, *args, **kwargs)

    if failure.endswith("stat"):
        # is_dir uses Path.stat on 3.12, os.stat directly on newer Python.
        monkeypatch.setattr(os, "stat", stat)
    elif failure == "enumeration":
        fail_enumeration(monkeypatch, sessions_root, PermissionError)
    else:
        original_iterdir = Path.iterdir

        def iterdir(path):
            yield from original_iterdir(path)
            if path == sessions_root:
                raise OSError("synthetic partial enumeration failure")

        monkeypatch.setattr(Path, "iterdir", iterdir)
    prompt = Mock(side_effect=AssertionError("purge prompt must not be reached"))
    delete_keychain = Mock()
    monkeypatch.setattr("builtins.input", prompt)
    monkeypatch.setattr(session_mod, "delete_macos_keychain_entry", delete_keychain)
    with pytest.raises(SessionError, match="could not be read"):
        switcher.purge()
    prompt.assert_not_called()
    delete_keychain.assert_not_called()
    assert (profile / ".credentials.json").read_text(encoding="utf-8") == "profile credential"


def test_purge_tolerates_a_disappeared_child(profile_switcher, monkeypatch):
    switcher, profile = profile_switcher
    original_iterdir = Path.iterdir

    def iterdir(path):
        yield from original_iterdir(path)
        if path == profile.parent:
            yield path / "disappeared-profile"

    monkeypatch.setattr(Path, "iterdir", iterdir)
    prompt = Mock(return_value="n")
    monkeypatch.setattr("builtins.input", prompt)
    switcher.purge()
    prompt.assert_called_once()
    assert (profile / ".credentials.json").exists()


@pytest.mark.parametrize("uncertain", [True, False])
def test_refresh_does_not_consume_uncertain_profile(
    profile_switcher, monkeypatch, uncertain
):
    switcher, profile = profile_switcher
    backup = json.dumps({"claudeAiOauth": {
        "accessToken": "old-access", "refreshToken": "old-refresh", "expiresAt": 1,
    }})
    newer = json.dumps({"claudeAiOauth": {
        "accessToken": "profile-access", "refreshToken": "profile-refresh", "expiresAt": 2,
    }})
    # Seed the backup directly: a backup change would otherwise invalidate this profile.
    switcher._store._write_account_credentials("2", "test@example.com", backup)
    (profile / ".credentials.json").write_text(newer, encoding="utf-8")
    monkeypatch.setattr(session_mod, "read_session_credentials", lambda _: newer)
    monkeypatch.setattr(session_mod, "session_identity_drifted", lambda *args: False)
    if uncertain:
        fail_enumeration(monkeypatch, profile / "sessions", PermissionError)
    refresh = Mock(return_value=oauth.RefreshOutcome(None, "review-stop"))
    monkeypatch.setattr(oauth, "try_refresh_oauth_credentials", refresh)

    result = switcher.consume_backup_grant("2", "test@example.com", backup)

    if uncertain:
        refresh.assert_not_called()
        assert result.credentials is None
        assert result.error == "session-unreadable"
        assert switcher._read_account_credentials("2", "test@example.com") == backup
        assert (profile / ".credentials.json").read_text(encoding="utf-8") == newer
    else:
        refresh.assert_called_once_with(newer)
        assert switcher._read_account_credentials("2", "test@example.com") == newer
