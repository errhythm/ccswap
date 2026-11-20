"""Switch custody guards, using only synthetic credentials and homes."""

import copy
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from ccswap import codex_session as cs, process_detection as pd, windows_job as wj
from ccswap.codex import CodexAccountSwitcher
from ccswap.exceptions import SwitchError
from ccswap.locking import FileLock
from tests.test_codex_session import auth, record


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


@pytest.fixture
def slots(tmp_path, monkeypatch):
    from ccswap import codex

    root, source = tmp_path / "backup", tmp_path / "default"
    source.mkdir()
    monkeypatch.setattr(codex, "get_backup_root", lambda: root)
    monkeypatch.setenv("CODEX_HOME", str(source))
    switcher = CodexAccountSwitcher()
    switcher._setup_directories()
    credentials = []
    for number in ("1", "2", "3"):
        credential = auth(chatgpt_user_id=f"user-{number}")
        credential["tokens"]["refresh_token"] = f"synthetic-{number}"
        credential["tokens"]["access_token"] = f"synthetic-access-{number}"
        credentials.append(credential)
        write(switcher._credential_path(number), credential)
    write(switcher.sequence_file, {"activeAccountNumber": 3, "accounts": {
        n: {"email": " User@EXAMPLE.org ", "accountId": "workspace"} for n in ("1", "2", "3")
    }})
    write(switcher.auth_file, credentials[0])
    monkeypatch.setattr(cs, "sys", SimpleNamespace(platform="linux"))
    # No actual process scan is needed for switch tests; its adapter is tested below.
    monkeypatch.setattr(pd, "scan_codex_home", lambda *a: {
        "state": "quiescent", "pids": [], "issues": [],
    })
    return switcher, credentials


def home_for(slots, number="1"):
    switcher, credentials = slots
    return cs.bootstrap_home(switcher.backup_dir, switcher.codex_home,
                             number, credentials[int(number) - 1])


def observed(switcher, number="1"):
    with FileLock(switcher.lock_file):
        return cs.slot_custody(switcher, number)


def snapshot(switcher):
    return {p: p.read_bytes() for p in switcher.backup_dir.rglob("*.json")} | {
        switcher.auth_file: switcher.auth_file.read_bytes(),
    }


@pytest.mark.parametrize("side", ["1", "2"])
@pytest.mark.parametrize("state", ["live", "uncertain", "conflict"])
@pytest.mark.parametrize("force", [False, True])
def test_both_sides_block(slots, monkeypatch, side, state, force):
    switcher, _ = slots
    real = cs.slot_custody
    monkeypatch.setattr(cs, "slot_custody", lambda s, n: (
        cs.Custody(state, "synthetic_issue") if n == side else real(s, n)))
    before = snapshot(switcher)
    with pytest.raises(SwitchError, match=state):
        switcher.switch_to("2", json_output=True, force=force)
    assert snapshot(switcher) == before


@pytest.mark.parametrize("state", ["live", "uncertain", "conflict"])
def test_same_account_guard(slots, monkeypatch, state):
    switcher, _ = slots
    monkeypatch.setattr(cs, "slot_custody", lambda *a: cs.Custody(state, "blocked"))
    with pytest.raises(SwitchError, match=state):
        switcher.switch_to("1", json_output=True)


@pytest.mark.parametrize("b,d,h,s,expected", [
    (0, 0, 0, 0, 0), (0, 1, 0, 0, 1), (0, 0, 1, 0, 1),
    (0, 0, 0, 1, 1), (0, 0, 1, 1, 1), (0, 1, 1, 0, 1),
    (0, 1, 0, 1, 1), (0, 1, 1, 1, 1), (0, 0, 1, 2, None),
    (0, 2, 1, 0, None), (0, 2, 0, 1, None),
    (2, 0, 2, 2, 2), (2, 1, 2, 2, None),
])
def test_a9(slots, b, d, h, s, expected):
    switcher, credentials = slots
    variants = [dict(credentials[0], rotation=n, last_refresh=f"2026-10-25T12:0{n}:00Z")
                for n in range(3)]
    home = cs.bootstrap_home(switcher.backup_dir, switcher.codex_home, "1", variants[0])
    manifest = cs.load_record(home / ".ccswap-session.json", "manifest")
    write(home / ".ccswap-session.json",
        cs.with_baseline(manifest, cs.auth_fingerprint(variants[b])))
    write(switcher.auth_file, variants[d])
    write(home / "auth.json", variants[h])
    write(switcher._credential_path("1"), variants[s])
    before = snapshot(switcher)
    if expected is not None:
        assert switcher.switch_to("2", json_output=True)["switched"]
        assert switcher._read_account_auth("1") == variants[expected]
        # A default-only generation reaches H at the next reconcile, not during switch.
        session = h if (b, d, h, s) == (0, 1, 0, 0) else expected
        assert cs.load_document(home / "auth.json") == variants[session]
    else:
        with pytest.raises(SwitchError, match="conflict"):
            switcher.switch_to("2", json_output=True)
        assert snapshot(switcher) == before



def test_incoming_uses_adopted_generation(slots):
    switcher, credentials = slots
    home = home_for(slots, "2")
    rotated = dict(credentials[1], rotation=1)
    write(home / "auth.json", rotated)
    switcher.switch_to("2", json_output=True)
    assert cs.load_document(switcher.auth_file) == rotated


def test_alias_identity_guards_even_with_wrong_metadata(slots, monkeypatch):
    switcher, credentials = slots
    home_for(slots, "2")
    write(switcher._credential_path("2"), credentials[0])
    data = switcher._read_sequence()
    data["accounts"]["2"]["email"] = "alias@example.org"
    write(switcher.sequence_file, data)
    real = cs.slot_custody
    monkeypatch.setattr(cs, "slot_custody", lambda s, n: (
        cs.Custody("live", "writers_present") if n == "2" else real(s, n)))
    with pytest.raises(SwitchError, match="live"):
        switcher.switch_to("3", json_output=True)


def test_unverified_token_alias_refused(slots):
    switcher, credentials = slots
    home_for(slots, "2")
    alias = copy.deepcopy(credentials[1])
    alias["tokens"]["refresh_token"] = credentials[0]["tokens"]["refresh_token"]
    write(switcher._credential_path("2"), alias)
    with pytest.raises(SwitchError, match="alias identity"):
        switcher.switch_to("3", json_output=True)


def test_email_workspace_does_not_authorize_session(slots):
    switcher, _ = slots
    home_for(slots)
    write(switcher.auth_file, auth(chatgpt_user_id="foreign"))
    with pytest.raises(SwitchError, match="verified session identity"):
        switcher.switch_to("2", json_output=True)


@pytest.mark.parametrize("external_rotation", [False, True])
def test_rollback_preserves_new_generation(slots, monkeypatch, external_rotation):
    switcher, credentials = slots
    newer = dict(credentials[1], rotation="external")

    def fail(data):
        if external_rotation:
            write(switcher.auth_file, newer)
        raise OSError("synthetic failure")

    monkeypatch.setattr(switcher, "_write_sequence", fail)
    with pytest.raises(OSError):
        switcher.switch_to("2", json_output=True)
    assert cs.load_document(switcher.auth_file) == (newer if external_rotation else credentials[0])


@pytest.mark.parametrize("state", ["live", "uncertain", "conflict"])
def test_rotation_filters_and_rechecks(slots, monkeypatch, state):
    switcher, _ = slots
    real = cs.slot_custody
    calls = []

    def custody(s, number):
        calls.append(number)
        return cs.Custody(state, "blocked") if number == "2" else real(s, number)

    monkeypatch.setattr(cs, "slot_custody", custody)
    assert switcher.switch(json_output=True)["to"]["number"] == 3
    assert calls.count("3") == 2  # selection and locked final guard


def add_run(home, *, finished=False, windows=False):
    run = record("run", home)
    run.update(home=str(home), phase="finished" if finished else "starting")
    if windows:
        run["supervisor"]["start"] = {"kind": "windows", "creationTime100ns": "1"}
        run["child"] = {"pid": 2, "start": {"kind": "windows", "creationTime100ns": "2"}}
        run["jobName"] = "Local\\ccswap-codex-" + run["runId"]
    write(home / "runs" / f"{run['runId']}.json", run)
    return run


@pytest.mark.parametrize("platform", ["linux", "darwin"])
@pytest.mark.parametrize("state", ["live", "uncertain"])
def test_scan_adapter(slots, monkeypatch, platform, state):
    switcher, _ = slots
    home = home_for(slots)
    active = add_run(home)
    add_run(home, finished=True)
    write(home / "runs" / f"{uuid4()}.json", {})
    unreadable = home / "runs" / f"{uuid4()}.json"
    write(unreadable, {})
    real_load = cs.load_record

    def load(path, kind):
        if path == unreadable:
            raise cs.SessionError("synthetic secret")
        return real_load(path, kind)

    monkeypatch.setattr(cs, "load_record", load)
    monkeypatch.setattr(cs, "sys", SimpleNamespace(platform=platform))

    def scan(path, supervisors, invalid):
        assert path == home
        expected = [(active["supervisor"]["pid"], active["supervisor"]["start"])]
        assert supervisors == expected
        assert invalid == 2
        return {"state": state, "pids": [42] if state == "live" else [], "issues": ["redacted"]}

    monkeypatch.setattr(pd, "scan_codex_home", scan)
    result = observed(switcher)
    assert result.state == state and result.issue_count == 1
    assert result.pids == ((42,) if state == "live" else ())
    assert "secret" not in repr(result)


@pytest.mark.parametrize("kind", ["stale", "manifest", "runs_link", "unsupported", "denied"])
def test_custody_fail_closed(slots, monkeypatch, kind):
    switcher, _ = slots
    home = home_for(slots)
    if kind == "stale":
        (home.parent / f".{home.name}.stale.json").write_text("garbled")
    elif kind == "manifest":
        (home / ".ccswap-session.json").write_text("{}")
    elif kind == "runs_link":
        (home / "runs").symlink_to(home / "sessions", target_is_directory=True)
    elif kind == "unsupported":
        monkeypatch.setattr(cs, "sys", SimpleNamespace(platform="unsupported"))
    else:
        real = cs.Path.lstat

        # Patch Path itself: 3.12/3.13 reach os.stat, later versions os.lstat.
        def denied(path):
            if str(path) == str(home):
                raise PermissionError("synthetic secret")
            return real(path)

        monkeypatch.setattr(cs.Path, "lstat", denied)
    outcome = observed(switcher)
    if kind == "stale":
        assert outcome.state == "quiescent" and outcome.intent
        with FileLock(switcher.lock_file):
            with pytest.raises(SwitchError, match="conflict"):
                switcher._guard_session_slot("1")
    else:
        assert outcome.state == ("uncertain" if kind == "unsupported" else "conflict")
    assert "secret" not in repr(outcome)


@pytest.mark.parametrize("job,process,wait,expected", [
    ("gone", "gone", "gone", "quiescent"),
    ("alive", "gone", "gone", "live"),
    ("unknown", "gone", "gone", "uncertain"),
    ("gone", "alive", "gone", "quiescent"),
    ("gone", "alive", "alive", "uncertain"),
    ("gone", "unknown", "unknown", "uncertain"),
    ("alive", "alive", "gone", "live"),
])
def test_windows_a6(slots, monkeypatch, job, process, wait, expected):
    switcher, _ = slots
    home = home_for(slots)
    run = add_run(home, windows=True)
    monkeypatch.setattr(cs, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(wj, "query_job", lambda rid: job)
    monkeypatch.setattr(wj, "process_state", lambda *a: process)
    waits = []

    def waited(pid, start, timeout):
        assert 0 <= timeout <= 10
        waits.append(pid)
        return wait

    monkeypatch.setattr(wj, "wait_identity_gone", waited)
    assert observed(switcher).state == expected
    if job == "gone" and process != "gone":
        assert waits == [run["supervisor"]["pid"], run["child"]["pid"]]


def test_no_home_and_stale_without_home(slots):
    switcher, _ = slots
    assert observed(switcher).state == "none"
    home = cs.slot_home(switcher, "1")
    home.parent.mkdir()
    (home.parent / f".{home.name}.stale.json").write_text("{}")
    assert observed(switcher).state == "conflict"


def test_selection_race_final_guard_refuses(slots, monkeypatch):
    switcher, _ = slots
    real = cs.slot_custody
    selected = 0

    def custody(s, number):
        nonlocal selected
        if number == "2":
            selected += 1
            if selected == 2:
                return cs.Custody("live", "writers_present")
        return real(s, number)

    monkeypatch.setattr(cs, "slot_custody", custody)
    before = snapshot(switcher)
    with pytest.raises(SwitchError, match="live"):
        switcher.switch(json_output=True)
    assert snapshot(switcher) == before


def test_incoming_reconciliation_conflict_preserved(slots):
    switcher, credentials = slots
    home = home_for(slots, "2")
    write(home / "auth.json", dict(credentials[1], rotation=1))
    write(switcher._credential_path("2"), dict(credentials[1], rotation=2))
    before = snapshot(switcher)
    with pytest.raises(SwitchError, match="both_moved"):
        switcher.switch_to("2", json_output=True)
    assert snapshot(switcher) == before


def test_default_unreadable_never_absent(slots, monkeypatch):
    switcher, _ = slots
    from pathlib import Path

    real = Path.read_text
    before = snapshot(switcher)

    def denied(path, *args, **kwargs):
        if path == switcher.auth_file:
            raise PermissionError("synthetic secret")
        return real(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", denied)
    from ccswap.exceptions import ConfigError

    with pytest.raises(ConfigError, match="unreadable") as caught:
        switcher.switch_to("2", json_output=True)
    assert "secret" not in str(caught.value)
    assert snapshot(switcher) == before


@pytest.mark.parametrize("supervisor", ["alive", "gone"])
def test_windows_finished_still_requires_a6_termination(slots, monkeypatch, supervisor):
    switcher, _ = slots
    home = home_for(slots)
    run = add_run(home, finished=True, windows=True)
    monkeypatch.setattr(cs, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(wj, "query_job", lambda rid: "gone")
    checked = []

    def state(pid, start):
        checked.append(pid)
        return supervisor if pid == run["supervisor"]["pid"] else "gone"

    monkeypatch.setattr(wj, "process_state", state)
    monkeypatch.setattr(wj, "wait_identity_gone", lambda *a: supervisor)
    assert observed(switcher).state == ("uncertain" if supervisor == "alive" else "quiescent")
    assert checked == [run["supervisor"]["pid"], run["child"]["pid"]]


@pytest.mark.parametrize("session", [False, True])
def test_target_alias_shortcut_preserves_default_and_newer_backup(slots, session):
    switcher, credentials = slots
    baseline = credentials[0]
    write(switcher._credential_path("2"), baseline)
    if session:
        cs.bootstrap_home(switcher.backup_dir, switcher.codex_home, "2", baseline)
    newer = dict(baseline, rotation=2)
    write(switcher._credential_path("2"), newer)
    result = switcher.switch_to("2", json_output=True)
    assert result["reason"] == "already-active" and not result["switched"]
    assert cs.load_document(switcher.auth_file) == baseline
    assert switcher._read_account_auth("2") == newer


@pytest.mark.parametrize("changed", ["default", "home", "backup", "manifest"])
def test_guard_rotation_refuses_further_writes(slots, monkeypatch, changed):
    switcher, credentials = slots
    home = home_for(slots, "2")
    real = switcher._guard_session_slot
    writes = []
    original_write = switcher._write_json

    def guard(number):
        outcome = real(number)
        if number == "2":
            if changed == "default":
                write(switcher.auth_file, dict(credentials[0], rotation=2))
            elif changed == "home":
                write(home / "auth.json", dict(credentials[1], rotation=2))
            elif changed == "backup":
                write(switcher._credential_path("2"), dict(credentials[1], rotation=2))
            else:
                manifest = cs.load_record(home / ".ccswap-session.json", "manifest")
                write(home / ".ccswap-session.json", dict(manifest, baselineFingerprint="f" * 64))
        return outcome

    def tracked(path, data):
        writes.append(path)
        original_write(path, data)

    monkeypatch.setattr(switcher, "_guard_session_slot", guard)
    monkeypatch.setattr(switcher, "_write_json", tracked)
    with pytest.raises(SwitchError, match="Codex state changed during switch; retry"):
        switcher.switch_to("2", json_output=True)
    assert writes == []


def test_stale_default_ancestor_does_not_block_switch(slots):
    switcher, credentials = slots
    home = home_for(slots)
    rotated = dict(credentials[0], rotation=2)
    write(home / "auth.json", rotated)
    switcher.switch_to("2", json_output=True)
    assert switcher._read_account_auth("1") == rotated
    assert cs.load_document(switcher.auth_file) == credentials[1]


@pytest.mark.parametrize("h,s,expected", [(0, 0, "unchanged"), (1, 0, "adopted_session"),
                                        (0, 1, "reseeded_session"), (1, 2, "conflict")])
def test_classification_is_read_only(slots, h, s, expected):
    switcher, credentials = slots
    home = home_for(slots, "2")
    variants = [credentials[1], dict(credentials[1], rotation=1), dict(credentials[1], rotation=2)]
    write(home / "auth.json", variants[h])
    write(switcher._credential_path("2"), variants[s])
    before = snapshot(switcher)
    with FileLock(switcher.lock_file):
        assert cs.classify_session(switcher, "2").state == expected
    assert snapshot(switcher) == before


def test_rotation_skips_d6_conflict(slots):
    switcher, credentials = slots
    home = home_for(slots, "2")
    write(home / "auth.json", dict(credentials[1], rotation=1))
    write(switcher._credential_path("2"), dict(credentials[1], rotation=2))
    before = (home / "auth.json").read_bytes()
    assert switcher.switch(json_output=True)["to"]["number"] == 3
    assert (home / "auth.json").read_bytes() == before


def test_rotation_retries_candidate_conflict_bounded(slots, monkeypatch):
    switcher, _ = slots
    attempted = []
    real = switcher.switch_to

    def switch(number, **kwargs):
        attempted.append(number)
        if number == "2":
            raise SwitchError("Codex Account 2 session conflict: both_moved")
        return real(number, **kwargs)

    monkeypatch.setattr(switcher, "switch_to", switch)
    assert switcher.switch(json_output=True)["to"]["number"] == 3
    assert attempted == ["2", "3"]
    attempted.clear()

    def conflict(number, **kwargs):
        attempted.append(number)
        raise SwitchError("Codex session conflict: both_moved")

    monkeypatch.setattr(switcher, "switch_to", conflict)
    with pytest.raises(SwitchError, match="No managed"):
        switcher.switch(json_output=True)
    assert attempted == ["2", "3", "1"]


def test_default_auth_symlink_still_works(slots, tmp_path):
    switcher, credentials = slots
    target = tmp_path / "linked-default.json"
    switcher.auth_file.rename(target)
    switcher.auth_file.symlink_to(target)
    assert switcher.switch_to("2", json_output=True)["switched"]
    assert switcher._read_account_auth("1") == credentials[0]


@pytest.mark.parametrize("content", ["{", "[]"])
def test_default_malformed_refused(slots, content):
    from ccswap.exceptions import ConfigError

    switcher, _ = slots
    switcher.auth_file.write_text(content)
    before = snapshot(switcher)
    with pytest.raises(ConfigError, match="unreadable"):
        switcher.switch_to("2", json_output=True)
    assert snapshot(switcher) == before


@pytest.mark.parametrize("session", [False, True])
def test_alias_backups_use_session_writer_and_clear_cache(slots, monkeypatch, session):
    switcher, credentials = slots
    baseline = dict(credentials[0], last_refresh="2026-10-25T12:00:00Z")
    write(switcher._credential_path("2"), baseline)
    if session:
        cs.bootstrap_home(switcher.backup_dir, switcher.codex_home, "2", baseline)
    write(switcher.auth_file, dict(baseline, rotation=1, last_refresh="2026-10-25T12:01:00Z"))
    switcher._usage_cache.update({"1": object(), "2": object()})
    writes = []
    original = switcher._write_session_backup

    def tracked(number, auth):
        writes.append(number)
        original(number, auth)

    monkeypatch.setattr(switcher, "_write_session_backup", tracked)
    assert switcher.switch_to("3", json_output=True)["switched"]
    assert writes == (["2"] if session else [])
    assert "1" not in switcher._usage_cache and "2" not in switcher._usage_cache


def test_missing_default_home_is_invalid_home(slots):
    switcher, _ = slots
    home_for(slots)
    switcher.codex_home = switcher.codex_home / "missing"
    assert observed(switcher) == cs.Custody("conflict", "invalid_home")


def test_target_revalidated_after_outgoing_backups(slots, monkeypatch):
    switcher, credentials = slots
    home_for(slots, "2")
    write(switcher.auth_file, dict(credentials[0], rotation=1))
    original = switcher._write_json

    def changed(path, data):
        original(path, data)
        if path == switcher._credential_path("1"):
            write(switcher._credential_path("2"), dict(credentials[1], rotation=2))

    monkeypatch.setattr(switcher, "_write_json", changed)
    with pytest.raises(SwitchError, match="Codex state changed during switch; retry"):
        switcher.switch_to("2", json_output=True)
    assert cs.load_document(switcher.auth_file) == dict(credentials[0], rotation=1)


def test_guard_cannot_hide_rotation_as_unchanged_reconcile(slots, monkeypatch):
    switcher, credentials = slots
    home = home_for(slots, "2")
    newer = dict(credentials[1], rotation=2)
    write(switcher._credential_path("2"), newer)
    real = switcher._guard_session_slot

    def guard(number):
        if number == "2":
            write(home / "auth.json", newer)
        return real(number)

    monkeypatch.setattr(switcher, "_guard_session_slot", guard)
    with pytest.raises(SwitchError, match="Codex state changed during switch; retry"):
        switcher.switch_to("2", json_output=True)
    assert cs.load_document(switcher.auth_file) == credentials[0]


@pytest.mark.parametrize("field", ["default", "home"])
def test_guard_document_change_not_python_numeric_equality(slots, monkeypatch, field):
    switcher, credentials = slots
    home = home_for(slots, "2")
    path = switcher.auth_file if field == "default" else home / "auth.json"
    original = dict(credentials[0 if field == "default" else 1], extra=1)
    write(path, original)
    if field == "home":
        write(switcher._credential_path("2"), original)
        manifest = cs.load_record(home / ".ccswap-session.json", "manifest")
        write(home / ".ccswap-session.json",
            cs.with_baseline(manifest, cs.auth_fingerprint(original)))
    real = switcher._guard_session_slot

    def guard(number):
        outcome = real(number)
        if number == "2":
            write(path, dict(original, extra=True))
        return outcome

    monkeypatch.setattr(switcher, "_guard_session_slot", guard)
    with pytest.raises(SwitchError, match="Codex state changed during switch; retry"):
        switcher.switch_to("2", json_output=True)


@pytest.mark.parametrize("field", ["default", "home"])
def test_guard_malformed_change_is_retry(slots, monkeypatch, field):
    switcher, _ = slots
    home = home_for(slots, "2")
    real = switcher._guard_session_slot

    def guard(number):
        outcome = real(number)
        if number == "2":
            (switcher.auth_file if field == "default" else home / "auth.json").write_text("{")
        return outcome

    monkeypatch.setattr(switcher, "_guard_session_slot", guard)
    with pytest.raises(SwitchError, match="Codex state changed during switch; retry"):
        switcher.switch_to("2", json_output=True)


def test_adopted_lineage_preserves_rotation_against_old_default(slots):
    switcher, credentials = slots
    home = home_for(slots)
    seed = cs.auth_fingerprint(credentials[0])
    rotated = dict(credentials[0], rotation=2)
    write(home / "auth.json", rotated)
    with FileLock(switcher.lock_file):
        assert cs.reconcile_session(switcher, "1", quiescent=True).state == "adopted_session"
    assert switcher.switch_to("2", json_output=True)["switched"]
    assert switcher._read_account_auth("1") == cs.load_document(home / "auth.json") == rotated
    assert cs.load_record(home / ".ccswap-session.json", "manifest")["lineage"] == [
        seed, cs.auth_fingerprint(rotated)]


@pytest.mark.parametrize("timestamp", [None, "garbled", "2026-10-25T11:59:00Z",
                                       "2026-10-25T12:00:00Z", "2026-10-25T12:01:00",
                                       "2026-02-30T12:01:00Z"])
def test_unknown_default_requires_strictly_newer_refresh(slots, timestamp):
    switcher, credentials = slots
    baseline = dict(credentials[0], last_refresh="2026-10-25T12:00:00Z")
    home = cs.bootstrap_home(switcher.backup_dir, switcher.codex_home, "1", baseline)
    write(switcher._credential_path("1"), baseline)
    unknown = dict(credentials[0], rotation="unknown")
    if timestamp is not None:
        unknown["last_refresh"] = timestamp
    write(switcher.auth_file, unknown)
    before = snapshot(switcher)
    with pytest.raises(SwitchError, match="conflict|verified session identity"):
        switcher.switch_to("2", json_output=True)
    assert snapshot(switcher) == before
    assert cs.load_document(home / "auth.json") == baseline


def test_default_newer_written_only_to_backup(slots):
    switcher, credentials = slots
    baseline = dict(credentials[0], last_refresh="2026-10-25T12:00:00Z")
    home = cs.bootstrap_home(switcher.backup_dir, switcher.codex_home, "1", baseline)
    write(switcher._credential_path("1"), baseline)
    newer = dict(baseline, last_refresh="2026-10-25T14:01:00+02:00")
    write(switcher.auth_file, newer)
    assert switcher.switch_to("2", json_output=True)["switched"]
    assert switcher._read_account_auth("1") == newer
    assert cs.load_document(home / "auth.json") == baseline
    with FileLock(switcher.lock_file):
        assert cs.reconcile_session(switcher, "1", quiescent=True).state == "reseeded_session"
    assert cs.load_record(home / ".ccswap-session.json", "manifest")["lineage"] == [
        cs.auth_fingerprint(baseline), cs.auth_fingerprint(newer)]


@pytest.mark.parametrize("read_number", [3, 4])
def test_default_rotation_before_outgoing_backup_retried_without_writes(slots, monkeypatch,
    read_number):
    switcher, credentials = slots
    baseline = dict(credentials[0], last_refresh="2026-10-25T12:00:00Z")
    cs.bootstrap_home(switcher.backup_dir, switcher.codex_home, "1", baseline)
    write(switcher._credential_path("1"), baseline)
    write(switcher.auth_file, dict(baseline, last_refresh="2026-10-25T12:01:00Z"))
    before = snapshot(switcher)
    original = switcher._read_switch_auth
    reads = 0

    def rotated(**kwargs):
        nonlocal reads
        reads += 1
        if reads == read_number:
            write(switcher.auth_file, dict(baseline, last_refresh="2026-10-25T12:02:00Z"))
        return original(**kwargs)

    monkeypatch.setattr(switcher, "_read_switch_auth", rotated)
    # Reads: initial snapshot, after guards, immediately before backup, before activation.
    if read_number == 3:
        with pytest.raises(SwitchError, match="Codex state changed during switch; retry"):
            switcher.switch_to("2", json_output=True)
        after = snapshot(switcher)
        before.pop(switcher.auth_file)
        after.pop(switcher.auth_file)
        assert after == before
    else:
        # Rotation after a durable backup is retained in D and never rolled back.
        with pytest.raises(SwitchError, match="Codex state changed during switch; retry"):
            switcher.switch_to("2", json_output=True)
        assert cs.load_document(switcher.auth_file)["last_refresh"] == "2026-10-25T12:02:00Z"
