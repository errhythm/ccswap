"""Inactive refresh uses session custody and full-document CAS."""

import copy

import pytest

from ccswap import codex, codex_session as cs
from ccswap.codex_usage import CodexUsageError
from ccswap.locking import FileLock
from tests.test_codex_guards import home_for, slots, snapshot, write
from tests.test_codex_session import auth


@pytest.fixture
def inactive(slots):
    switcher, credentials = slots
    default = auth(chatgpt_user_id="default-user", chatgpt_account_id="default-workspace")
    default["tokens"].update(account_id="default-workspace", refresh_token="default-refresh")
    write(switcher.auth_file, default)
    return slots


def rotated(auth):
    result = copy.deepcopy(auth)
    result["tokens"].update(access_token="rotated-access", refresh_token="rotated-refresh")
    result["last_refresh"] = "2026-10-25T12:02:00Z"
    return result


@pytest.mark.parametrize("state", ["live", "uncertain", "conflict"])
def test_refresh_blocked_by_custody(inactive, monkeypatch, state):
    switcher, credentials = inactive
    home_for(inactive)
    monkeypatch.setattr(cs, "slot_custody", lambda *a: cs.Custody(state, "synthetic"))
    monkeypatch.setattr(codex, "refresh_codex_auth", lambda *a: pytest.fail("must not refresh"))
    before = snapshot(switcher)
    assert switcher._refresh_inactive_auth("1", credentials[0]) is None
    assert snapshot(switcher) == before


@pytest.mark.parametrize("home", [False, True])
def test_refresh_full_expected_fingerprint(inactive, monkeypatch, home):
    switcher, credentials = inactive
    if home:
        home_for(inactive)
    expected = dict(credentials[0], changed="same refresh token")
    monkeypatch.setattr(codex, "refresh_codex_auth", lambda *a: pytest.fail("stale caller"))
    before = snapshot(switcher)
    assert switcher._refresh_inactive_auth("1", expected) is None
    assert snapshot(switcher) == before


def test_refresh_compares_expected_after_adoption(inactive, monkeypatch):
    switcher, credentials = inactive
    home = home_for(inactive)
    current = dict(credentials[0], rotated="session")
    write(home / "auth.json", current)
    monkeypatch.setattr(codex, "refresh_codex_auth", lambda *a: pytest.fail("stale caller"))
    assert switcher._refresh_inactive_auth("1", credentials[0]) is None
    assert switcher._read_account_auth("1") == current


@pytest.mark.parametrize("collision", ["identity", "token", "lineage", "equal"])
def test_refresh_refuses_default_family(inactive, monkeypatch, collision):
    switcher, credentials = inactive
    data = switcher._read_sequence()
    data["accounts"]["1"]["email"] = "user@example.org"
    write(switcher.sequence_file, data)
    original = copy.deepcopy(credentials[0])
    if collision == "lineage":
        original["tokens"]["login_owner_id"] = "old-owner"
        credentials[0]["tokens"]["login_owner_id"] = "old-owner"
        write(switcher._credential_path("1"), original)
    home_for(inactive)
    expected = credentials[0]
    if collision == "identity":
        default = rotated(expected)
    elif collision == "token":
        default = cs.load_document(switcher.auth_file)
        default["tokens"]["refresh_token"] = expected["tokens"]["refresh_token"]
    elif collision == "lineage":
        expected = rotated(original)
        expected["tokens"]["login_owner_id"] = "new-owner"
        with FileLock(switcher.lock_file):
            cs.replace_session_credentials(switcher, "1", expected, "replacement")
        default = original
    else:
        default = expected
    write(switcher.auth_file, default)
    monkeypatch.setattr(codex, "refresh_codex_auth", lambda *a: pytest.fail("default family"))
    before = snapshot(switcher)
    assert switcher._refresh_inactive_auth("1", expected) is None
    assert snapshot(switcher) == before


@pytest.mark.parametrize("home", [False, True])
def test_refresh_success_persists_authority_and_invalidates_cache(inactive, monkeypatch, home):
    switcher, credentials = inactive
    session = home_for(inactive) if home else None
    new = rotated(credentials[0])
    calls, locks = [], []

    def lock(path):
        result = FileLock(path)
        locks.append(result)
        return result

    monkeypatch.setattr(codex, "FileLock", lock)

    def refresh(current):
        assert locks[-1]._locked
        calls.append(current)
        return new

    monkeypatch.setattr(codex, "refresh_codex_auth", refresh)
    switcher._usage_cache["1"] = object()
    assert switcher._refresh_inactive_auth("1", credentials[0]) == new
    assert calls == [credentials[0]]
    assert switcher._read_account_auth("1") == new
    assert "1" not in switcher._usage_cache
    if session:
        assert cs.load_document(session / "auth.json") == new
        manifest = cs.load_record(session / ".ccswap-session.json", "manifest")
        assert manifest["lineage"] == [cs.auth_fingerprint(credentials[0]),
            cs.auth_fingerprint(new)]
        assert not (session.parent / f".{session.name}.stale.json").exists()


def test_refresh_error_changes_nothing(inactive, monkeypatch):
    switcher, credentials = inactive
    home_for(inactive)

    def fail(*args):
        raise CodexUsageError("synthetic refresh failure")

    monkeypatch.setattr(codex, "refresh_codex_auth", fail)
    before = snapshot(switcher)
    with pytest.raises(CodexUsageError):
        switcher._refresh_inactive_auth("1", credentials[0])
    assert snapshot(switcher) == before


def test_refresh_crash_after_backup_is_recovered(inactive, monkeypatch):
    switcher, credentials = inactive
    home = home_for(inactive)
    new = rotated(credentials[0])
    real = cs._session_write

    def write_auth(tree, path, data):
        if path == home / "auth.json":
            raise OSError("synthetic interruption")
        return real(tree, path, data)

    monkeypatch.setattr(codex, "refresh_codex_auth", lambda *a: new)
    with monkeypatch.context() as patch:
        patch.setattr(cs, "_session_write", write_auth)
        with pytest.raises(codex.SwitchError, match="saved to the backup"):
            switcher._refresh_inactive_auth("1", credentials[0])
    assert switcher._read_account_auth("1") == new
    assert cs.load_document(home / "auth.json") == credentials[0]
    assert (home.parent / f".{home.name}.stale.json").exists()
    with FileLock(switcher.lock_file):
        switcher._guard_session_slot("1")
    assert cs.load_document(home / "auth.json") == new
    assert not (home.parent / f".{home.name}.stale.json").exists()


def test_unreadable_default_never_means_inactive(inactive, monkeypatch):
    switcher, credentials = inactive
    home_for(inactive)
    switcher.auth_file.write_text("garbled")
    monkeypatch.setattr(codex, "refresh_codex_auth", lambda *a: pytest.fail("unknown default"))
    assert switcher._refresh_inactive_auth("1", credentials[0]) is None


def test_refreshed_token_kept_when_session_changes_during_refresh(inactive, monkeypatch):
    switcher, credentials = inactive
    home = home_for(inactive)
    foreign = dict(credentials[0], rotation="foreign-writer")
    new = rotated(credentials[0])

    def refresh(stored):
        # A writer outside ccswap rotates H while the network request is in flight.
        write(home / "auth.json", foreign)
        return new

    monkeypatch.setattr(codex, "refresh_codex_auth", refresh)
    with pytest.raises(codex.SwitchError, match="saved to the backup"):
        switcher._refresh_inactive_auth("1", credentials[0])
    assert switcher._read_account_auth("1") == new
    assert cs.load_document(home / "auth.json") == foreign


def test_usage_survives_session_persistence_failure(inactive, monkeypatch):
    switcher, credentials = inactive
    monkeypatch.setattr(switcher, "_refresh_inactive_auth", lambda *a: (_ for _ in ()).throw(
        cs.SessionError("synthetic")))
    monkeypatch.setattr(codex, "fetch_codex_usage", lambda *a, **k: (_ for _ in ()).throw(
        CodexUsageError("expired", status_code=401)))
    effective = {"auth": credentials[0], "refresh": True, "reason": "", "base_url": "x",
                 "source": "backup", "custody": "none"}
    entry = switcher._usage_entry("1", credentials[0], True, 0.0, is_active=False,
                                  effective=effective)
    assert entry.last_error


def test_next_number_unreadable_sessions_refuses(slots, monkeypatch):
    switcher, _ = slots
    sessions = switcher.backup_dir / "codex" / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    real = type(sessions).iterdir

    def denied(path):
        if path == sessions:
            raise PermissionError("synthetic")
        return real(path)

    monkeypatch.setattr(type(sessions), "iterdir", denied)
    with pytest.raises(codex.SwitchError, match="slot explicitly"):
        switcher._next_number(switcher._read_sequence())
