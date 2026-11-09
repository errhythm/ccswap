"""Quiescent custody reconciliation with synthetic credentials only."""

import copy
import json
from pathlib import Path

import pytest

from claude_swap import codex_session as cs
from claude_swap.codex import CodexAccountSwitcher
from claude_swap.locking import FileLock
from tests.test_codex_session import auth


@pytest.fixture
def custody(tmp_path):
    root, source = tmp_path / "backup", tmp_path / "default"
    root.mkdir()
    source.mkdir()
    switcher = CodexAccountSwitcher.__new__(CodexAccountSwitcher)
    switcher.backup_dir = root
    switcher.provider_dir = root / "codex"
    switcher.codex_home = source
    switcher.credentials_dir = root / "codex" / "credentials"
    switcher.sequence_file = root / "codex" / "sequence.json"
    switcher._usage_cache = {"1": object(), "2": object()}
    baseline = auth()
    home = cs.bootstrap_home(root, source, "1", baseline)
    switcher.credentials_dir.mkdir()
    cs._session_write(root, switcher._credential_path("1"), baseline)
    cs._session_write(root, switcher.sequence_file, {
        "accounts": {"1": {"email": "user@example.org", "accountId": "workspace",
                           "usageGeneration": "a" * 32}},
    })
    return switcher, home, baseline


def write(path, data):
    path.write_text(json.dumps(data))


def reconcile(custody):
    switcher, _, _ = custody
    with FileLock(switcher.backup_dir / ".lock"):
        return cs.reconcile_session(switcher, "1", quiescent=True)


def snapshot(custody):
    switcher, home, _ = custody
    return [path.read_bytes() if path.exists() else None for path in (
        switcher._credential_path("1"), home / "auth.json", home / ".ccswap-session.json",
    )]


def test_session_home_case_uses_native_path_semantics(custody, monkeypatch):
    import os
    from types import SimpleNamespace
    from claude_swap import process_detection

    switcher, home, _ = custody
    manifest_path = home / ".ccswap-session.json"
    manifest = cs.load_record(manifest_path, "manifest")
    manifest["home"] = str(home.with_name(home.name.upper()))
    assert manifest["home"] != str(home)
    write(manifest_path, manifest)
    monkeypatch.setattr(cs, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(process_detection, "scan_codex_home", lambda *_: {
        "state": "quiescent", "pids": [], "issues": []})
    expected = "quiescent" if os.name == "nt" else "conflict"
    assert cs.slot_custody(switcher, "1").state == expected
    result = reconcile(custody)
    assert result.state == ("unchanged" if os.name == "nt" else "conflict")
    if os.name != "nt":
        assert result.reason == "invalid_home"


@pytest.mark.parametrize("h,s,state", [
    (0, 0, "unchanged"), (1, 1, "unchanged"),
    (1, 0, "adopted_session"), (0, 1, "reseeded_session"), (1, 2, "conflict"),
])
def test_d6(custody, h, s, state):
    switcher, home, baseline = custody
    variants = [baseline, dict(baseline, unknown={"rotation": 1}),
                dict(baseline, unknown={"rotation": 2})]
    write(home / "auth.json", variants[h])
    write(switcher._credential_path("1"), variants[s])
    before = snapshot(custody)
    result = reconcile(custody)
    assert result.state == state
    if state == "conflict":
        assert result.reason == "both_moved" and snapshot(custody) == before
    else:
        stored = cs.load_document(switcher._credential_path("1"))
        current = cs.load_document(home / "auth.json")
        assert stored == current == variants[h if h else s]
        assert cs.load_record(home / ".ccswap-session.json", "manifest")[
            "baselineFingerprint"] == cs.auth_fingerprint(current)
        lineage = cs.load_record(home / ".ccswap-session.json", "manifest")["lineage"]
        seed = cs.auth_fingerprint(baseline)
        assert lineage == ([seed] if h == s == 0 else [seed, cs.auth_fingerprint(current)])
        assert ("1" in switcher._usage_cache) == (h == s == 0)
        assert "2" in switcher._usage_cache


@pytest.mark.parametrize("target", ["session", "backup"])
@pytest.mark.parametrize("kind", ["missing", "malformed", "invalid", "drift", "owner"])
def test_invalid_credentials_preserved(custody, target, kind):
    switcher, home, baseline = custody
    path = home / "auth.json" if target == "session" else switcher._credential_path("1")
    if kind == "missing":
        path.unlink()
    elif kind == "malformed":
        path.write_text('{"tokens":')
    elif kind == "invalid":
        write(path, {})
    elif kind == "drift":
        write(path, auth(chatgpt_user_id="other"))
    else:
        manifest = cs.load_record(home / ".ccswap-session.json", "manifest")
        manifest["identity"]["loginOwnerId"] = "baseline-owner"
        write(home / ".ccswap-session.json", manifest)
        changed = copy.deepcopy(baseline)
        changed["tokens"]["login_owner_id"] = "other-owner"
        write(path, changed)
    before = snapshot(custody)
    assert reconcile(custody).state == "conflict"
    assert snapshot(custody) == before


@pytest.mark.parametrize("target", ["backup", "auth", "manifest"])
def test_write_denial_retry(custody, monkeypatch, target):
    switcher, home, baseline = custody
    changed = dict(baseline, unknown="preserved")
    write(switcher._credential_path("1") if target == "auth" else home / "auth.json",
          changed)
    original = cs._session_write
    denied = {"backup": switcher._credential_path("1"), "auth": home / "auth.json",
              "manifest": home / ".ccswap-session.json"}[target]
    before = snapshot(custody)

    def refuse(tree, path, data):
        if path == denied:
            raise PermissionError("synthetic-secret")
        original(tree, path, data)

    monkeypatch.setattr(cs, "_session_write", refuse)
    result = reconcile(custody)
    assert result == cs.ReconcileResult("conflict", {
        "backup": "backup_write_failed", "auth": "auth_write_failed",
        "manifest": "manifest_write_failed",
    }[target])
    if target != "manifest":
        assert snapshot(custody) == before
    else:
        assert switcher._read_session_backup("1") == changed
        assert "1" not in switcher._usage_cache
    monkeypatch.setattr(cs, "_session_write", original)
    assert reconcile(custody).state != "conflict"
    assert switcher._read_session_backup("1") == cs.load_document(home / "auth.json")


@pytest.mark.parametrize("kind", ["missing_slot", "home", "source", "slot", "preparing",
                                  "committed_intent", "uncommitted_intent"])
def test_metadata_conflict(custody, kind):
    switcher, home, baseline = custody
    manifest_path = home / ".ccswap-session.json"
    manifest = cs.load_record(manifest_path, "manifest")
    if kind == "missing_slot":
        write(switcher.sequence_file, {"accounts": {}})
    elif kind.endswith("intent"):
        fp = cs.auth_fingerprint(baseline)
        write(home.parent / f".{home.name}.stale.json", dict(
            schemaVersion=1, home=str(home), accountNumber="1", identity=manifest["identity"],
            oldFingerprint=fp if kind == "uncommitted_intent" else "a" * 64,
            newFingerprint=fp if kind == "committed_intent" else "b" * 64, reason="refresh"))
    else:
        field, value = {"home": ("home", str(home.parent / "wrong")),
                        "source": ("sourceHome", str(home.parent)),
                        "slot": ("accountNumber", "2"),
                        "preparing": ("bootstrapState", "preparing")}[kind]
        manifest[field] = value
        write(manifest_path, manifest)
    before = snapshot(custody)
    assert reconcile(custody).state == "conflict"
    assert snapshot(custody) == before


def test_nonquiescent_no_access():
    assert cs.reconcile_session(object(), "1", quiescent=False) == cs.ReconcileResult(
        "conflict", "not_quiescent")


@pytest.mark.parametrize("target", ["sequence", "backup", "auth", "manifest"])
def test_read_denied(custody, monkeypatch, target):
    switcher, home, _ = custody
    denied = {"sequence": switcher.sequence_file, "backup": switcher._credential_path("1"),
              "auth": home / "auth.json", "manifest": home / ".ccswap-session.json"}[target]
    original = cs.os.open
    before = snapshot(custody)

    def refuse(path, *args, **kwargs):
        if Path(path) == denied:
            raise PermissionError("synthetic-secret")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(cs.os, "open", refuse)
    assert reconcile(custody).state == "conflict"
    assert snapshot(custody) == before


def test_clock_not_authority(custody):
    switcher, home, baseline = custody
    baseline["last_refresh"] = "2026-10-25T12:00:00Z"
    write(switcher._credential_path("1"), baseline)
    manifest = cs.load_record(home / ".ccswap-session.json", "manifest")
    manifest = cs.with_baseline(manifest, cs.auth_fingerprint(baseline))
    write(home / ".ccswap-session.json", manifest)
    changed = dict(baseline, last_refresh="2020-01-01T00:00:00Z")
    write(home / "auth.json", changed)
    assert reconcile(custody).state == "adopted_session"
    assert switcher._read_session_backup("1") == changed


def test_slot_workspace_mismatch(custody):
    switcher, _, _ = custody
    write(switcher.sequence_file, {"accounts": {
        "1": {"email": "user@example.org", "accountId": "other"},
    }})
    before = snapshot(custody)
    assert reconcile(custody) == cs.ReconcileResult("conflict", "identity_mismatch")
    assert snapshot(custody) == before


@pytest.mark.parametrize("kind", ["missing", "unreadable", "malformed"])
def test_strict_document_errors(tmp_path, monkeypatch, kind):
    path = tmp_path / "document.json"
    if kind == "missing":
        assert cs.load_document(path) is None
        return
    path.write_text("[]" if kind == "malformed" else "{}")
    if kind == "unreadable":
        def denied(*args, **kwargs):
            raise PermissionError("synthetic-secret")
        monkeypatch.setattr(cs.os, "open", denied)
    with pytest.raises(cs.SessionError, match=(
        "Unreadable" if kind == "unreadable" else "Malformed")) as caught:
        cs.load_document(path)
    assert "synthetic-secret" not in str(caught.value)


@pytest.mark.parametrize("kind", ["missing", "malformed", "symlink"])
def test_invalid_manifest(custody, kind):
    _, home, _ = custody
    path = home / ".ccswap-session.json"
    if kind == "malformed":
        path.write_text("{}")
    else:
        path.unlink()
        if kind == "symlink":
            path.symlink_to(home / "auth.json")
    before = snapshot(custody)
    assert reconcile(custody).state == "conflict"
    assert snapshot(custody) == before


def test_legacy_owner_cannot_bridge_conflicting_owners(custody):
    switcher, home, baseline = custody
    for path, owner in ((home / "auth.json", "one"),
                        (switcher._credential_path("1"), "two")):
        changed = copy.deepcopy(baseline)
        changed["tokens"]["login_owner_id"] = owner
        write(path, changed)
    before = snapshot(custody)
    assert reconcile(custody).reason == "identity_mismatch"
    assert snapshot(custody) == before


def test_semantic_equality(custody):
    switcher, home, baseline = custody
    (home / "auth.json").write_text(json.dumps(baseline, indent=4))
    before = snapshot(custody)
    assert reconcile(custody).state == "unchanged"
    assert snapshot(custody) == before


def test_legacy_owner_matching_identity_allowed(custody):
    _, home, baseline = custody
    changed = copy.deepcopy(baseline)
    changed["tokens"]["login_owner_id"] = "owner"
    write(home / "auth.json", changed)
    assert reconcile(custody).state == "adopted_session"


@pytest.mark.parametrize("reseed", [False, True])
def test_durable_write_order(custody, monkeypatch, reseed):
    switcher, home, baseline = custody
    credential = home / "auth.json" if reseed else switcher._credential_path("1")
    changed_path = switcher._credential_path("1") if reseed else home / "auth.json"
    write(changed_path, dict(baseline, rotation=1))
    events = []
    original = cs._session_write

    def record_write(tree, path, data):
        events.append(("write", path))
        original(tree, path, data)

    monkeypatch.setattr(cs, "_session_write", record_write)
    monkeypatch.setattr(cs, "_sync_directory", lambda path: events.append(("sync", path)))
    assert reconcile(custody).state == ("reseeded_session" if reseed else "adopted_session")
    assert events == [("write", credential), ("sync", credential.parent),
                      ("write", home / ".ccswap-session.json"), ("sync", home)]
    assert "1" not in switcher._usage_cache


@pytest.mark.parametrize("mode", ["adopt", "reseed", "equal"])
@pytest.mark.parametrize("fail_manifest_sync", [False, True])
def test_sync_failure(custody, monkeypatch, mode, fail_manifest_sync):
    switcher, home, baseline = custody
    changed = dict(baseline, rotation=1)
    if mode in ("adopt", "equal"):
        write(home / "auth.json", changed)
    if mode in ("reseed", "equal"):
        write(switcher._credential_path("1"), changed)
    manifest_path = home / ".ccswap-session.json"
    before = manifest_path.read_bytes()
    events = []
    original = cs._session_write

    def record_write(tree, path, data):
        events.append(path)
        original(tree, path, data)

    def sync(path):
        if (manifest_path in events) == fail_manifest_sync:
            raise OSError("synthetic-secret")

    monkeypatch.setattr(cs, "_session_write", record_write)
    monkeypatch.setattr(cs, "_sync_directory", sync)
    assert reconcile(custody) == cs.ReconcileResult(
        "conflict", "manifest_sync_failed" if fail_manifest_sync else "credential_sync_failed")
    if not fail_manifest_sync:
        assert manifest_path not in events
        assert manifest_path.read_bytes() == before
    assert "1" not in switcher._usage_cache


def test_raw_slot_email(custody):
    switcher, _, _ = custody
    write(switcher.sequence_file, {"accounts": {
        "1": {"email": " User@EXAMPLE.org ", "accountId": "workspace"},
    }})
    assert reconcile(custody).state == "unchanged"


def test_fingerprint_failure_reason(custody, monkeypatch):
    before = snapshot(custody)

    def fail(auth):
        raise cs.SessionError("synthetic-secret")

    monkeypatch.setattr(cs, "auth_fingerprint", fail)
    assert reconcile(custody) == cs.ReconcileResult("conflict", "fingerprint_failed")
    assert snapshot(custody) == before


def test_equal_drifted_identity_refused(custody):
    switcher, home, _ = custody
    drifted = auth(chatgpt_user_id="other")
    write(home / "auth.json", drifted)
    write(switcher._credential_path("1"), drifted)
    before = snapshot(custody)
    assert reconcile(custody) == cs.ReconcileResult("conflict", "identity_mismatch")
    assert snapshot(custody) == before


def test_leading_zero_slot_refused(custody):
    switcher, _, _ = custody
    before = snapshot(custody)
    with FileLock(switcher.backup_dir / ".lock"):
        assert cs.reconcile_session(switcher, "01", quiescent=True) == cs.ReconcileResult(
            "conflict", "invalid_slot")
    assert snapshot(custody) == before
