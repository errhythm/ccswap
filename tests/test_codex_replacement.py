"""Replacement crash recovery and add/remove custody, with synthetic homes."""

import pytest

from ccswap import codex_session as cs, process_detection as pd
from ccswap.exceptions import SwitchError
from ccswap.locking import FileLock
from tests.test_codex_guards import home_for, slots, snapshot, write
from tests.test_codex_session import auth


def replacement(slots):
    return dict(slots[1][0], rotation="fresh", last_refresh="2026-10-25T12:01:00Z")


def intent_for(slots, home, new):
    switcher, credentials = slots
    path = home.parent / f".{home.name}.stale.json"
    write(path, dict(schemaVersion=1, home=str(home), accountNumber="1",
                     identity=cs.extract_identity(new)[0], reason="replacement",
                     oldFingerprint=cs.auth_fingerprint(credentials[0]),
                     newFingerprint=cs.auth_fingerprint(new)))
    return path


@pytest.mark.parametrize("step", range(1, 12))
def test_transaction_crashes_recover(slots, monkeypatch, step):
    switcher, credentials = slots
    home = home_for(slots)
    history = home / "history.jsonl"
    history.write_text("retained")
    new = replacement(slots)
    calls = 0
    real_write, real_sync, real_clear = cs._session_write, cs._sync_directory, cs._clear_intent

    def crash(function):
        def wrapped(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == step:
                raise OSError("synthetic failure")
            return function(*args, **kwargs)
        return wrapped

    with monkeypatch.context() as patch:
        patch.setattr(cs, "_session_write", crash(real_write))
        patch.setattr(cs, "_sync_directory", crash(real_sync))
        patch.setattr(cs, "_clear_intent", crash(real_clear))
        with FileLock(switcher.lock_file):
            switcher._guard_session_slot("1")
            try:
                cs.replace_session_credentials(switcher, "1", new, "replacement")
            except OSError:
                pass
    committed = switcher._read_account_auth("1") == new
    with FileLock(switcher.lock_file):
        switcher._guard_session_slot("1")
    expected = new if committed else credentials[0]
    assert cs.load_document(home / "auth.json") == expected
    manifest = cs.load_record(home / ".ccswap-session.json", "manifest")
    assert manifest["baselineFingerprint"] == cs.auth_fingerprint(expected)
    assert not (home.parent / f".{home.name}.stale.json").exists()
    assert history.read_text() == "retained"


@pytest.mark.parametrize("case", ["third", "backup_identity", "home_identity", "missing_auth_old",
                                  "missing_auth_new", "missing_home", "changed_slot"])
def test_recovery_conflicts_and_committed_missing_auth(slots, case):
    switcher, credentials = slots
    home = home_for(slots)
    new = replacement(slots)
    intent_for(slots, home, new)
    if case == "third":
        write(switcher._credential_path("1"), dict(new, rotation="third"))
    elif case == "backup_identity":
        write(switcher._credential_path("1"), auth(chatgpt_user_id="other"))
    elif case == "home_identity":
        write(switcher._credential_path("1"), new)
        write(home / "auth.json", auth(chatgpt_user_id="other"))
    elif case.startswith("missing_auth"):
        (home / "auth.json").unlink()
        if case.endswith("new"):
            write(switcher._credential_path("1"), new)
    elif case == "missing_home":
        home.rename(home.with_name("retained-home"))
    else:
        data = switcher._read_sequence()
        data["accounts"]["1"]["accountId"] = "changed"
        write(switcher.sequence_file, data)
    before = snapshot(switcher)
    with FileLock(switcher.lock_file):
        if case == "missing_auth_new":
            switcher._guard_session_slot("1")
            assert cs.load_document(home / "auth.json") == new
        else:
            with pytest.raises(SwitchError, match="conflict"):
                switcher._guard_session_slot("1")
            assert snapshot(switcher) == before


@pytest.mark.parametrize("state", ["live", "uncertain"])
@pytest.mark.parametrize("operation", ["intent", "add_target", "add_victim", "remove"])
@pytest.mark.parametrize("yes", [False, True])
def test_guards_cannot_be_bypassed(slots, monkeypatch, state, operation, yes):
    switcher, _ = slots
    number = "2" if operation == "add_victim" else "1"
    home = home_for(slots, number)
    if operation == "intent":
        intent_for(slots, home, replacement(slots))
    monkeypatch.setattr(pd, "scan_codex_home", lambda *a: {
        "state": state, "pids": [42] if state == "live" else [], "issues": [],
    })
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("guard must precede prompt"))
    before = snapshot(switcher)
    with pytest.raises(SwitchError, match=state):
        if operation == "intent":
            with FileLock(switcher.lock_file):
                switcher._guard_session_slot("1")
        elif operation == "remove":
            switcher.remove_account("1", assume_yes=yes)
        else:
            switcher.add_account(slot=int(number), assume_yes=yes)
    assert snapshot(switcher) == before


def test_add_ancestor_does_not_lose_rotation(slots, capsys):
    switcher, credentials = slots
    home = home_for(slots)
    for rotation in (1, 2):
        write(home / "auth.json", dict(credentials[0], rotation=rotation))
        with FileLock(switcher.lock_file):
            switcher._guard_session_slot("1")
    before = snapshot(switcher)
    switcher.add_account(slot=1, assume_yes=True)
    assert snapshot(switcher) == before
    assert "already current" in capsys.readouterr().out


@pytest.mark.parametrize("owner", [None, "new-owner"])
def test_add_replaces_same_user_login(slots, owner):
    switcher, credentials = slots
    credentials[0]["last_refresh"] = "2026-10-25T12:00:00Z"
    write(switcher._credential_path("1"), credentials[0])
    home = home_for(slots)
    new = replacement(slots)
    if owner:
        new["tokens"] = dict(new["tokens"], login_owner_id=owner)
    write(switcher.auth_file, new)
    switcher.add_account(slot=1, assume_yes=True)
    assert switcher._read_account_auth("1") == new
    assert cs.load_document(home / "auth.json") == new
    assert cs.load_record(home / ".ccswap-session.json", "manifest")[
        "identity"] == cs.extract_identity(new)[0]


def test_different_occupant_retained(slots):
    switcher, _ = slots
    home_for(slots, "2")
    before = snapshot(switcher)
    with pytest.raises(SwitchError, match="home kept at"):
        switcher.add_account(slot=2, assume_yes=True)
    assert snapshot(switcher) == before


@pytest.mark.parametrize("change", ["live", "uncertain", "auth", "manifest", "slot"])
def test_remove_rechecks_after_prompt(slots, monkeypatch, change):
    switcher, credentials = slots
    home = home_for(slots)

    def answer(*args):
        if change in ("live", "uncertain"):
            monkeypatch.setattr(pd, "scan_codex_home", lambda *a: {
                "state": change, "pids": [], "issues": [],
            })
        elif change == "auth":
            write(home / "auth.json", dict(credentials[0], rotation=1))
        elif change == "manifest":
            manifest = cs.load_record(home / ".ccswap-session.json", "manifest")
            write(home / ".ccswap-session.json", dict(manifest, extra="changed"))
        else:
            data = switcher._read_sequence()
            data["accounts"]["1"]["email"] = "changed@example.org"
            write(switcher.sequence_file, data)
        return "yes"

    monkeypatch.setattr("builtins.input", answer)
    with pytest.raises(SwitchError, match="changed"):
        switcher.remove_account("1")
    assert switcher._credential_path("1").exists()
    assert home.exists()


def test_remove_keeps_home_and_reuse_refused(slots, capsys):
    switcher, credentials = slots
    home = home_for(slots)
    before = {p: p.read_bytes() for p in home.rglob("*") if p.is_file()}
    switcher.remove_account("1", assume_yes=True)
    assert "Session home kept" in capsys.readouterr().out
    assert all(p.read_bytes() == data for p, data in before.items())
    with pytest.raises(SwitchError, match="manual disposition"):
        switcher.add_account(slot=1, assume_yes=True)
    data = switcher._read_sequence()
    data["accounts"]["1"] = dict(data["accounts"]["2"])
    write(switcher.sequence_file, data)
    write(switcher._credential_path("1"), credentials[1])
    with FileLock(switcher.lock_file):
        with pytest.raises(SwitchError, match="conflict"):
            switcher._guard_session_slot("1")


def test_add_rereads_default_under_lock(slots, monkeypatch):
    switcher, credentials = slots
    reads = iter([credentials[0], credentials[1]])
    monkeypatch.setattr(switcher, "_read_live_auth", lambda: next(reads))
    before = snapshot(switcher)
    with pytest.raises(SwitchError, match="Codex login changed; retry"):
        switcher.add_account(slot=1, assume_yes=True)
    assert snapshot(switcher) == before


def test_no_home_replacement_needs_no_intent(slots):
    switcher, _ = slots
    with FileLock(switcher.lock_file):
        cs.replace_session_credentials(switcher, "1", replacement(slots), "replacement")
    assert switcher._read_account_auth("1") == replacement(slots)
    assert not (switcher.backup_dir / "codex" / "sessions").exists()


def test_uncommitted_intent_runs_ordinary_d6(slots):
    switcher, credentials = slots
    home = home_for(slots)
    intent_for(slots, home, replacement(slots))
    rotated = dict(credentials[0], rotation="session")
    write(home / "auth.json", rotated)
    with FileLock(switcher.lock_file):
        assert switcher._guard_session_slot("1").state == "adopted_session"
    assert switcher._read_account_auth("1") == rotated


@pytest.mark.parametrize("change", ["user", "workspace", "incomplete"])
def test_replacement_rejects_invalid_identity_before_intent(slots, change):
    from ccswap.exceptions import SessionError

    switcher, _ = slots
    home_for(slots)
    new = auth(chatgpt_user_id="other") if change == "user" else replacement(slots)
    if change == "workspace":
        new = auth(chatgpt_account_id="other")
    elif change == "incomplete":
        new = {"tokens": {}}
    before = snapshot(switcher)
    with FileLock(switcher.lock_file):
        switcher._guard_session_slot("1")
        with pytest.raises(SessionError):
            cs.replace_session_credentials(switcher, "1", new, "replacement")
    assert snapshot(switcher) == before


def test_add_checks_default_again_after_guard(slots, monkeypatch):
    switcher, credentials = slots
    credentials[0]["last_refresh"] = "2026-10-25T12:00:00Z"
    write(switcher._credential_path("1"), credentials[0])
    home_for(slots)
    new = replacement(slots)
    write(switcher.auth_file, new)
    real = switcher._guard_session_slot

    def guard(number):
        result = real(number)
        write(switcher.auth_file, dict(new, changed=True))
        return result

    monkeypatch.setattr(switcher, "_guard_session_slot", guard)
    with pytest.raises(SwitchError, match="Codex login changed; retry"):
        switcher.add_account(slot=1, assume_yes=True)
    assert switcher._read_account_auth("1") == slots[1][0]


def test_add_equal_is_noop(slots):
    switcher, _ = slots
    home_for(slots)
    before = snapshot(switcher)
    switcher.add_account(slot=1, assume_yes=True)
    assert snapshot(switcher) == before


def test_no_home_add_and_remove_preserve_existing_behavior(slots, monkeypatch):
    switcher, credentials = slots
    monkeypatch.setattr("builtins.input", lambda *args: "yes")
    switcher.add_account(slot=2)
    assert switcher._read_account_auth("2") == credentials[0]
    assert switcher._read_sequence()["activeAccountNumber"] == 2
    switcher.remove_account("2")
    assert "2" not in switcher._read_sequence()["accounts"]
    assert not switcher._credential_path("2").exists()


def test_evicted_ancestor_conflicts(slots):
    switcher, credentials = slots
    home = home_for(slots)
    original = credentials[0]
    manifest = cs.load_record(home / ".ccswap-session.json", "manifest")
    for number in range(65):
        current = dict(original, generation=number, last_refresh="2026-10-25T12:00:00Z")
        manifest = cs.with_baseline(manifest, cs.auth_fingerprint(current))
    assert cs.auth_fingerprint(original) not in manifest["lineage"]
    assert cs.default_write_decision(original, current, current, manifest) == "conflict"


def test_reuse_different_email_home_conflicts(slots):
    switcher, _ = slots
    home_for(slots)
    data = switcher._read_sequence()
    data["accounts"]["1"]["email"] = "other@example.org"
    write(switcher.sequence_file, data)
    with FileLock(switcher.lock_file):
        with pytest.raises(SwitchError, match="conflict"):
            switcher._guard_session_slot("1")


@pytest.mark.parametrize("case", ["baseline", "refresh_owner", "email"])
def test_recovery_rejects_changed_coordination_or_identity(slots, case):
    import base64
    import json

    switcher, _ = slots
    home = home_for(slots)
    new = replacement(slots)
    if case == "refresh_owner":
        new["tokens"] = dict(new["tokens"], login_owner_id="new-owner")
        manifest = cs.load_record(home / ".ccswap-session.json", "manifest")
        manifest["identity"]["loginOwnerId"] = "old-owner"
        write(home / ".ccswap-session.json", manifest)
    intent = intent_for(slots, home, new)
    write(switcher._credential_path("1"), new)
    if case == "baseline":
        manifest = cs.load_record(home / ".ccswap-session.json", "manifest")
        write(home / ".ccswap-session.json", cs.with_baseline(manifest, "f" * 64))
    elif case == "refresh_owner":
        data = cs.load_record(intent, "stale")
        write(intent, dict(data, reason="refresh"))
    else:
        current = cs.load_document(home / "auth.json")
        payload = current["tokens"]["id_token"].split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        claims["email"] = "other@example.org"
        payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
        current["tokens"]["id_token"] = f"e30.{payload}.sig"
        write(home / "auth.json", current)
    before = snapshot(switcher)
    with FileLock(switcher.lock_file):
        with pytest.raises(SwitchError, match="conflict"):
            switcher._guard_session_slot("1")
    assert snapshot(switcher) == before


@pytest.mark.parametrize("timestamp", ["2026-10-25T11:59:00Z", "2026-10-25T12:00:00Z",
                                       None, "garbled"])
def test_add_rejects_unknown_default_generation(slots, timestamp):
    switcher, credentials = slots
    credentials[0]["last_refresh"] = "2026-10-25T12:00:00Z"
    write(switcher._credential_path("1"), credentials[0])
    home_for(slots)
    new = dict(credentials[0], generation="unknown")
    if timestamp is None:
        new.pop("last_refresh")
    else:
        new["last_refresh"] = timestamp
    write(switcher.auth_file, new)
    before = snapshot(switcher)
    with pytest.raises(SwitchError, match="conflict"):
        switcher.add_account(slot=1, assume_yes=True)
    assert snapshot(switcher) == before


def test_add_evicted_ancestor_conflicts(slots):
    switcher, credentials = slots
    original = dict(credentials[0], last_refresh="2026-10-25T11:00:00Z")
    write(switcher._credential_path("1"), original)
    home = cs.bootstrap_home(switcher.backup_dir, switcher.codex_home, "1", original)
    for number in range(65):
        current = dict(original, rotation=number, last_refresh="2026-10-25T12:00:00Z")
        write(home / "auth.json", current)
        with FileLock(switcher.lock_file):
            switcher._guard_session_slot("1")
    write(switcher.auth_file, original)
    before = snapshot(switcher)
    with pytest.raises(SwitchError, match="conflict"):
        switcher.add_account(slot=1, assume_yes=True)
    assert snapshot(switcher) == before


def test_recovery_retains_foreign_rotation_after_backup_commit(slots):
    switcher, credentials = slots
    home = home_for(slots)
    new = replacement(slots)
    intent_for(slots, home, new)
    write(switcher._credential_path("1"), new)
    t3 = dict(credentials[0], rotation="foreign-T3")
    write(home / "auth.json", t3)
    before = snapshot(switcher)
    with FileLock(switcher.lock_file):
        with pytest.raises(SwitchError, match="conflict"):
            switcher._guard_session_slot("1")
    assert snapshot(switcher) == before
    assert cs.load_document(home / "auth.json") == t3


@pytest.mark.parametrize("moved", ["home", "backup", "baseline"])
def test_replacement_checks_cas_itself(slots, moved):
    from ccswap.exceptions import SessionError

    switcher, credentials = slots
    home = home_for(slots)
    other = dict(credentials[0], rotation="changed")
    if moved == "home":
        write(home / "auth.json", other)
    elif moved == "backup":
        write(switcher._credential_path("1"), other)
    else:
        manifest = cs.load_record(home / ".ccswap-session.json", "manifest")
        write(home / ".ccswap-session.json", cs.with_baseline(manifest, cs.auth_fingerprint(other)))
    before = snapshot(switcher)
    with FileLock(switcher.lock_file):
        with pytest.raises(SessionError):
            cs.replace_session_credentials(switcher, "1", replacement(slots), "replacement")
    assert snapshot(switcher) == before


@pytest.mark.parametrize("yes", [False, True])
def test_add_session_retains_overwrite_prompt(slots, monkeypatch, yes):
    switcher, credentials = slots
    credentials[0]["last_refresh"] = "2026-10-25T12:00:00Z"
    write(switcher._credential_path("1"), credentials[0])
    data = switcher._read_sequence()
    data["accounts"]["1"]["email"] = "user@example.org"
    write(switcher.sequence_file, data)
    home_for(slots)
    write(switcher.auth_file, replacement(slots))
    prompts = []
    monkeypatch.setattr("builtins.input", lambda prompt: prompts.append(prompt) or "no")
    before = snapshot(switcher)
    switcher.add_account(slot=1, assume_yes=yes)
    assert len(prompts) == (0 if yes else 1)
    if not yes:
        assert snapshot(switcher) == before
    else:
        assert switcher._read_account_auth("1") == replacement(slots)


@pytest.mark.parametrize("failure", ["sequence", "unlink"])
def test_remove_crash_keeps_recoverable_store(slots, monkeypatch, failure):
    from pathlib import Path

    switcher, _ = slots
    real = Path.unlink

    def unlink(path, *args, **kwargs):
        if path == switcher._credential_path("3"):
            assert "3" not in switcher._read_sequence()["accounts"]
            raise OSError("synthetic interruption")
        return real(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        if failure == "sequence":
            patch.setattr(switcher, "_write_sequence", lambda *a: (_ for _ in ()).throw(OSError()))
        else:
            patch.setattr(Path, "unlink", unlink)
        with pytest.raises(OSError):
            switcher.remove_account("3", assume_yes=True)
    assert switcher._credential_path("3").exists()
    if failure == "sequence":
        switcher.remove_account("3", assume_yes=True)
    else:
        switcher.add_account(slot=3, assume_yes=True)
        assert "3" in switcher._read_sequence()["accounts"]


def test_automatic_number_skips_retained_highest_home(slots):
    switcher, _ = slots
    home = home_for(slots, "3")
    switcher.remove_account("3", assume_yes=True)
    new = auth(chatgpt_account_id="new-workspace")
    new["tokens"]["account_id"] = "new-workspace"
    write(switcher.auth_file, new)
    switcher.add_account(assume_yes=True)
    assert "4" in switcher._read_sequence()["accounts"]
    assert home.exists()


def test_documented_conflict_exit_moves_home_and_intent(slots, tmp_path):
    # docs/codex.md "Resolving a session conflict": both artifacts leave sessions/.
    switcher, _ = slots
    home = home_for(slots)
    intent = intent_for(slots, home, replacement(slots))
    write(switcher._credential_path("1"), dict(replacement(slots), rotation="third"))
    with FileLock(switcher.lock_file), pytest.raises(SwitchError, match="conflict"):
        switcher._guard_session_slot("1")
    retained = tmp_path / "retained"
    retained.mkdir()
    home.rename(retained / home.name)
    assert cs.slot_custody(switcher, "1").reason == "missing_home"
    intent.rename(retained / intent.name)
    assert cs.slot_custody(switcher, "1").state == "none"
    with FileLock(switcher.lock_file):
        switcher._guard_session_slot("1")
