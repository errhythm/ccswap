from __future__ import annotations

import json
import os
import stat
import sys
import threading

import pytest

from claude_swap.vendors.core import (
    AccountRecord,
    VendorError,
    parse_version,
    redact,
    version_tuple,
)
from claude_swap.vendors.store import VendorStore, validate_alias

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")


def rec(alias="work", tool="muse", kind="api-key", **kw):
    return AccountRecord(tool, alias, kind, **kw)


@pytest.fixture
def store(tmp_path):
    return VendorStore(tmp_path / "vendors")


def test_parse_version_variants():
    assert parse_version("Muse Code 1.4.14 (1.4.14-R7309.1)") == "1.4.14"
    assert parse_version("kiro-cli 3.1.0") == "3.1.0"
    assert parse_version("1.18.35\n") == "1.18.35"
    assert parse_version("garbage") is None
    assert version_tuple("1.18.35-beta") == (1, 18, 35)
    assert version_tuple(None) == ()


def test_redact_known_and_token_shaped():
    assert "hunter22secret" not in redact("key hunter22secret failed", ["hunter22secret"])
    assert "ksk_abcdefghijklmnop" not in redact("bad ksk_abcdefghijklmnop key")
    assert "Bearer" not in redact("Authorization: Bearer abcdefghijkl1234")


@pytest.mark.parametrize("bad", ["", "../x", "a/b", " a", "-x", "a" * 65, "a;b", "a\nb", "$(x)"])
def test_alias_rejects_unsafe(bad):
    with pytest.raises(VendorError):
        validate_alias(bad)


@pytest.mark.parametrize("good", ["work", "a.b-c_d", "me@corp.com", "Z9"])
def test_alias_accepts(good):
    assert validate_alias(good) == good


def test_add_list_remove_roundtrip(store):
    store.add(rec("a"), "key-aaaaaaaa")
    store.add(rec("b", kind="browser"))
    assert [r.alias for r in store.records("muse")] == ["a", "b"]
    assert store.records("kiro") == []
    removed = store.remove("muse", "a")
    assert removed.alias == "a"
    assert [r.alias for r in store.records("muse")] == ["b"]


def test_duplicate_alias_case_insensitive(store):
    store.add(rec("Work"))
    with pytest.raises(VendorError, match="already registered"):
        store.add(rec("work"))


def test_same_alias_allowed_for_different_providers(store):
    store.add(rec("main", "opencode", "provider-account", provider="anthropic"))
    store.add(rec("main", "opencode", "provider-account", provider="openai"))
    assert len(store.records("opencode")) == 2


def test_namespacing_isolates_tools(store):
    store.add(rec("x", "muse"))
    store.add(rec("x", "kiro", "login"))
    assert len(store.records("muse")) == 1 and len(store.records("kiro")) == 1


@posix_only
def test_secret_files_are_private_and_not_alias_named(store):
    saved = store.add(rec("my.account"), "key-aaaaaaaa")
    secret_files = list(store.secrets_dir.iterdir())
    assert len(secret_files) == 1
    assert stat.S_IMODE(secret_files[0].stat().st_mode) == 0o600
    assert stat.S_IMODE(store.secrets_dir.stat().st_mode) == 0o700
    assert saved.alias not in secret_files[0].name
    assert store.read_secret(saved) == "key-aaaaaaaa"


def test_metadata_file_never_contains_secret(store):
    store.add(rec("a"), "super-secret-value")
    assert "super-secret-value" not in store.file.read_text()


def test_remove_deletes_ccswap_copy_of_secret(store):
    saved = store.add(rec("a"), "key-aaaaaaaa")
    store.remove("muse", "a")
    assert list(store.secrets_dir.iterdir()) == []
    with pytest.raises(VendorError):
        store.read_secret(saved)


def test_remove_unknown(store):
    with pytest.raises(VendorError, match="No registered"):
        store.remove("muse", "ghost")


@posix_only
def test_loosened_secret_mode_is_repaired(store):
    saved = store.add(rec("a"), "key-aaaaaaaa")
    path = next(store.secrets_dir.iterdir())
    path.chmod(0o666)
    store.read_secret(saved)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@posix_only
def test_symlinked_secret_refused(store, tmp_path):
    saved = store.add(rec("a"), "key-aaaaaaaa")
    path = next(store.secrets_dir.iterdir())
    target = tmp_path / "elsewhere"
    target.write_text("stolen")
    path.unlink()
    path.symlink_to(target)
    with pytest.raises(VendorError, match="missing or unsafe"):
        store.read_secret(saved)


@posix_only
def test_symlinked_store_dir_refused(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    with pytest.raises(VendorError, match="unsafe"):
        VendorStore(link).add(rec("a"))


def test_tampered_secret_ref_is_ignored(store):
    store.add(rec("a"))
    doc = json.loads(store.file.read_text())
    doc["tools"]["muse"]["accounts"][0]["secretRef"] = "../../../etc/passwd"
    store.file.write_text(json.dumps(doc))
    assert store.records("muse") == []  # entry skipped, not followed


def test_corrupt_file_is_never_overwritten(store):
    store.root.mkdir(parents=True)
    store.file.write_text("{not json")
    with pytest.raises(VendorError, match="untouched"):
        store.add(rec("a"))
    assert store.file.read_text() == "{not json"


def test_newer_schema_refused(store):
    store.root.mkdir(parents=True)
    store.file.write_text(json.dumps({"schemaVersion": 99, "tools": {}}))
    with pytest.raises(VendorError, match="newer ccswap"):
        store.records("muse")


def test_unknown_fields_survive_rewrite(store):
    store.root.mkdir(parents=True)
    store.file.write_text(
        json.dumps({"schemaVersion": 1, "futureTop": {"k": 1},
                    "tools": {"muse": {"accounts": [], "futureTool": True}}})
    )
    store.add(rec("a"))
    doc = json.loads(store.file.read_text())
    assert doc["futureTop"] == {"k": 1} and doc["tools"]["muse"]["futureTool"] is True


def test_corrupt_entry_skipped_but_preserved(store):
    store.root.mkdir(parents=True)
    store.file.write_text(json.dumps({"schemaVersion": 1, "tools": {"muse": {"accounts": [{"nope": 1}]}}}))
    assert store.records("muse") == []
    store.add(rec("a"))
    assert {"nope": 1} in json.loads(store.file.read_text())["tools"]["muse"]["accounts"]


def test_active_tracking_and_clear_on_remove(store):
    store.add(rec("a"))
    store.set_active("muse", "a")
    assert store.active("muse") == "a"
    store.remove("muse", "a")
    assert store.active("muse") is None


def test_active_by_provider(store):
    store.set_active("opencode", "w", "anthropic")
    store.set_active("opencode", "p", "openai")
    assert store.active("opencode", "anthropic") == "w" and store.active("opencode", "openai") == "p"


def test_concurrent_adds_lose_nothing(store):
    errors = []

    def worker(i):
        try:
            store.add(rec(f"acct{i}"), f"key-value-{i:04d}")
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors
    assert len(store.records("muse")) == 8
    assert len(list(store.secrets_dir.iterdir())) == 8
