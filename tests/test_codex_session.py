"""Session primitives use synthetic credentials and temporary records only."""

import base64
import copy
import json
import os
import re
import sys
from uuid import uuid4

import pytest

from ccswap import codex_session
from ccswap.exceptions import SessionError


def auth(**changes):
    """Build a synthetic auth document with optional nested claim changes."""
    nested = dict(
        chatgpt_user_id="user",
        chatgpt_account_id="workspace",
        chatgpt_plan_type="plus",
    )
    nested.update(changes)
    claims = {"email": " User@EXAMPLE.org ", "https://api.openai.com/auth": nested}
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return {
        "tokens": dict(
            id_token=f"e30.{payload}.sig",
            access_token="synthetic",
            refresh_token="synthetic",
            account_id="workspace",
        ),
    }


def record(kind, tmp_path):
    """Build a valid synthetic record of the requested schema kind."""
    data = dict(
        schemaVersion=1,
        home=str(tmp_path / "session"),
        accountNumber="1",
        identity=codex_session.extract_identity(auth())[0],
    )
    if kind == "manifest":
        data.update(
            sourceHome=str(tmp_path / "default"),
            baselineFingerprint="a" * 64, lineage=["a" * 64],
            bootstrapState="ready",
            sharedItems={},
        )
    elif kind == "stale":
        data.update(oldFingerprint="a" * 64, newFingerprint="b" * 64, reason="refresh")
    else:
        data = dict(
            schemaVersion=1,
            home=str(tmp_path / "session"),
            runId=str(uuid4()),
            child=None,
            supervisor=dict(pid=1, start=dict(kind="linux", bootId="boot", ticks="0")),
            phase="starting",
            shared=False,
            jobName=None,
            exitCode=None,
        )
    return data


def test_identity_and_fingerprint():
    """Check custody comparisons and complete semantic auth fingerprints."""
    original = auth()
    identity, plan = codex_session.extract_identity(original)
    assert identity["email"] == "user@example.org" and plan == "plus"
    other_identity = codex_session.extract_identity(auth(chatgpt_user_id="other"))[0]
    assert not codex_session.same_identity(identity, other_identity)
    owned = dict(identity, loginOwnerId="one")
    assert codex_session.same_identity(identity, owned)
    assert not codex_session.same_identity(owned, dict(identity, loginOwnerId="two"))
    changed = copy.deepcopy(original)
    changed["tokens"]["login_owner_id"] = "one"
    assert codex_session.extract_identity(changed)[0] == owned
    reformatted = json.loads(json.dumps(original, indent=2))
    assert codex_session.auth_fingerprint(original) == codex_session.auth_fingerprint(reformatted)
    changed = dict(original, unknown={"unicode": "ä", "oauth_provenance": "preserved"})
    assert codex_session.auth_fingerprint(changed) != codex_session.auth_fingerprint(original)
    preserved = copy.deepcopy(changed)
    codex_session.extract_identity(changed)
    codex_session.auth_fingerprint(changed)
    assert changed == preserved
    for invalid_auth in (None, {"x": float("nan")}, {"x": object()}):
        with pytest.raises(SessionError):
            codex_session.auth_fingerprint(invalid_auth)


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda data: data.update(auth_mode="api"), id="api-mode"),
        pytest.param(lambda data: data.update(OPENAI_API_KEY="synthetic"), id="api-key"),
        pytest.param(
            lambda data: data["tokens"].update(account_id="other"), id="workspace-mismatch",
        ),
        pytest.param(
            lambda data: data["tokens"].pop("refresh_token"), id="missing-refresh-token",
        ),
        pytest.param(lambda data: data["tokens"].update(id_token="bad"), id="malformed-token"),
        pytest.param(lambda data: data["tokens"].update(login_owner_id=""), id="empty-owner"),
    ],
)
def test_invalid_auth(mutate):
    """Reject invalid credentials without including their contents in the error."""
    data = auth()
    mutate(data)
    with pytest.raises(SessionError, match="^Invalid ChatGPT session identity.$"):
        codex_session.extract_identity(data)


@pytest.mark.parametrize(
    "changes",
    [
        pytest.param({"chatgpt_user_id": None}, id="missing-user"),
        pytest.param({"chatgpt_account_id": ""}, id="empty-workspace"),
        pytest.param({"user_id": "other"}, id="contradictory-user"),
    ],
)
def test_invalid_nested_claims(changes):
    """Reject incomplete or contradictory nested identity claims."""
    with pytest.raises(SessionError):
        codex_session.extract_identity(auth(**changes))


@pytest.mark.parametrize("plan", [None, "unknown", "business", "enterprise", "edu", "sci_plus"])
def test_plan_gate(plan):
    """Reject unsupported plans while allowing the known team plan."""
    assert not codex_session.session_plan_supported(plan)
    assert codex_session.session_plan_supported("team")


@pytest.mark.parametrize("kind", ["manifest", "run", "stale"])
def test_schema_and_load_failures(kind, tmp_path, monkeypatch):
    """Distinguish missing, malformed and unreadable records for every schema."""
    data = record(kind, tmp_path)
    path = tmp_path / (data.get("runId", kind) + ".json")
    assert codex_session.load_record(path, kind) is None
    path.write_text(json.dumps(data))
    assert codex_session.load_record(path, kind) == data
    for field in data:
        without_field = {key: value for key, value in data.items() if key != field}
        with pytest.raises(SessionError):
            codex_session.validate_record(without_field, kind, run_id=path.stem)
        for invalid_value in (None, [], True):
            invalid_record = dict(data, **{field: invalid_value})
            unchanged = type(invalid_value) is type(data[field]) and invalid_value == data[field]
            valid_shared = field == "shared" and invalid_value is True
            if unchanged or valid_shared:
                continue
            with pytest.raises(SessionError):
                codex_session.validate_record(invalid_record, kind, run_id=path.stem)
    for text in ('{', '[]', '{"schemaVersion":1,"schemaVersion":1}', '{"x":NaN}'):
        path.write_text(text)
        with pytest.raises(SessionError, match="Malformed"):
            codex_session.load_record(path, kind)

    def deny_read(*args):
        raise PermissionError()

    monkeypatch.setattr(codex_session.os, "open", deny_read)
    with pytest.raises(SessionError, match="Unreadable"):
        codex_session.load_record(path, kind)


@pytest.mark.parametrize(
    "start",
    [
        pytest.param(dict(kind="linux", bootId="b", ticks="1"), id="linux"),
        pytest.param(dict(kind="macos", bootId="b", uniqueId="1"), id="macos"),
        pytest.param(dict(kind="windows", creationTime100ns="1"), id="windows"),
    ],
)
def test_process_ranges(start):
    """Accept each process identity variant and reject invalid process IDs."""
    codex_session._process(dict(pid=1, start=start))
    for pid in (0, -1, True, None, 2**32):
        with pytest.raises(SessionError):
            codex_session._process(dict(pid=pid, start=start))


@pytest.mark.parametrize(
    "kind,key,value",
    [
        pytest.param("manifest", "schemaVersion", 2, id="unknown-schema"),
        pytest.param("manifest", "accountNumber", "0", id="zero-account"),
        pytest.param("manifest", "home", "/a/../b", id="noncanonical-home"),
        pytest.param("manifest", "baselineFingerprint", "A" * 64, id="uppercase-fingerprint"),
        pytest.param("manifest", "bootstrapState", "unknown", id="unknown-bootstrap-state"),
        pytest.param("stale", "reason", "unknown", id="unknown-stale-reason"),
        pytest.param("run", "phase", "unknown", id="unknown-run-phase"),
        pytest.param("run", "runId", str(uuid4()), id="run-filename-mismatch"),
        pytest.param(
            "manifest",
            "sharedItems",
            {"../escape": {"source": "/source", "mode": "copy"}},
            id="escaping-shared-path",
        ),
    ],
)
def test_schema_values(kind, key, value, tmp_path):
    """Reject invalid schema versions, values and paths."""
    data = record(kind, tmp_path)
    run_id = data.get("runId")
    if key == "home":
        value = str(tmp_path) + os.sep + "a" + os.sep + ".." + os.sep + "b"
    elif key == "sharedItems":
        value = {"../escape": {"source": str(tmp_path / "source"), "mode": "copy"}}
    data[key] = value
    with pytest.raises(SessionError):
        codex_session.validate_record(data, kind, run_id=run_id)


def test_nonordinary_record(tmp_path):
    """Reject a dangling symlink instead of treating it as a missing record."""
    path = tmp_path / "link"
    path.symlink_to(tmp_path / "missing")
    with pytest.raises(SessionError, match="Malformed"):
        codex_session.load_record(path, "manifest")


def replace_claims(data, claims):
    """Replace only the synthetic JWT claims, preserving the auth envelope."""
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    data["tokens"]["id_token"] = f"e30.{payload}.sig"
    return data


@pytest.mark.parametrize(
    "user_fallback,email_fallback", [(True, False), (False, True), (True, True)],
)
def test_identity_fallbacks(user_fallback, email_fallback):
    """Use Codex's nested user and profile email fallbacks."""
    data = auth()
    payload = data["tokens"]["id_token"].split(".")[1]
    claims = json.loads(base64.urlsafe_b64decode(payload + "=="))
    nested = claims["https://api.openai.com/auth"]
    if user_fallback:
        nested["user_id"] = nested.pop("chatgpt_user_id")
    if email_fallback:
        claims["https://api.openai.com/profile"] = {"email": claims.pop("email")}
    assert codex_session.extract_identity(replace_claims(data, claims))[0] == (
        codex_session.extract_identity(auth())[0]
    )


@pytest.mark.parametrize("field", ["chatgpt_user_id", "chatgpt_account_id"])
def test_untrimmed_ids(field):
    """Reject padded identifiers even when the workspace agrees with the envelope."""
    data = auth(**{field: " padded "})
    if field == "chatgpt_account_id":
        data["tokens"]["account_id"] = " padded "
    with pytest.raises(SessionError):
        codex_session.extract_identity(data)


def test_contradictory_email():
    """Reject conflicting top-level and profile email claims."""
    data = auth()
    claims = {"email": "one@example.org", "https://api.openai.com/profile": {
        "email": "two@example.org"}, "https://api.openai.com/auth": {
        "chatgpt_user_id": "user", "chatgpt_account_id": "workspace"}}
    with pytest.raises(SessionError):
        codex_session.extract_identity(replace_claims(data, claims))


@pytest.mark.parametrize("token", [
    ".e30.sig", "e30.e30.", "e30..sig", "!.e30.sig", "e30.e30.!",
    "e30.e30=.sig", "e30.e31.sig", "e30.e30.sig.extra",
])
def test_jwt_shape(token):
    """Reject empty, invalid, padded and noncanonical JWT segments."""
    data = auth()
    data["tokens"]["id_token"] = token
    with pytest.raises(SessionError):
        codex_session.extract_identity(data)


@pytest.mark.parametrize("location", ["record", "jwt", "fingerprint"])
def test_deep_json(location, tmp_path, monkeypatch):
    """Translate parser and serializer recursion failures to safe session errors."""
    deeply_nested = "[" * 10000 + "0" + "]" * 10000
    with pytest.raises(SessionError):
        if location == "record":
            path = tmp_path / "deep.json"
            path.write_text(deeply_nested)
            codex_session.load_record(path, "manifest")
        elif location == "jwt":
            data = auth()
            payload = base64.urlsafe_b64encode(deeply_nested.encode()).decode().rstrip("=")
            data["tokens"]["id_token"] = f"e30.{payload}.sig"
            codex_session.extract_identity(data)
        else:
            document = {}
            current = document
            for _ in range(10000):
                current["next"] = {}
                current = current["next"]
            def recursion_failure(*args, **kwargs):
                raise RecursionError()

            monkeypatch.setattr(codex_session.json, "dumps", recursion_failure)
            codex_session.auth_fingerprint(document)


@pytest.mark.parametrize("value", [None, 1, "bad", "2026-02-30T12:00:00Z"])
def test_invalid_last_refresh(value):
    """Reject timestamps Codex cannot deserialize."""
    data = dict(auth(), last_refresh=value)
    with pytest.raises(SessionError):
        codex_session.extract_identity(data)


@pytest.mark.parametrize("value", [None, [], {}, {"issuer": 1, "client_id": "c"}])
def test_invalid_provenance(value):
    """Require the complete typed OAuth provenance object when present."""
    data = auth()
    data["tokens"]["oauth_provenance"] = value
    with pytest.raises(SessionError):
        codex_session.extract_identity(data)


def test_valid_auth_metadata():
    """Accept a valid timestamp and complete provenance without dropping unknown fields."""
    data = dict(auth(), last_refresh="2026-10-25T12:34:56.123Z")
    data["tokens"]["oauth_provenance"] = {"issuer": "https://example.org", "client_id": "c"}
    assert codex_session.extract_identity(data) == codex_session.extract_identity(auth())


@pytest.mark.parametrize("destination", [
    "config.toml", "AGENTS.md", "AGENTS.override.md", "rules", "prompts", "skills/example",
])
def test_shareable_destinations(destination, tmp_path):
    """Allow exactly the supported definitions and individual user skills."""
    data = record("manifest", tmp_path)
    data["sharedItems"] = {destination: {"source": str(tmp_path / "source"), "mode": "copy"}}
    assert codex_session.validate_record(data, "manifest") == data


@pytest.mark.parametrize("destination", [
    "auth.json", "skills", "skills/.system", "skills/example/nested", "plugins", "sessions",
])
def test_unshareable_destinations(destination, tmp_path):
    """Reject private state and unsupported skill destinations."""
    data = record("manifest", tmp_path)
    data["sharedItems"] = {destination: {"source": str(tmp_path / "source"), "mode": "copy"}}
    with pytest.raises(SessionError):
        codex_session.validate_record(data, "manifest")


@pytest.mark.parametrize("kind", ["manifest", "stale"])
def test_leading_zero_account(kind, tmp_path):
    """Reject noncanonical account numbers in both custody schemas."""
    data = record(kind, tmp_path)
    data["accountNumber"] = "01"
    with pytest.raises(SessionError):
        codex_session.validate_record(data, kind)


@pytest.mark.parametrize("exit_code", [-(2**31), 0, 2**31 - 1, 2**32 - 1, -(2**31) - 1, 2**32])
def test_windows_run_ranges(exit_code, tmp_path):
    """Validate a positive Windows record and both signed and native exit boundaries."""
    data = record("run", tmp_path)
    data["supervisor"] = {"pid": 2**32 - 1, "start": {
        "kind": "windows", "creationTime100ns": "123"}}
    data["jobName"] = "Local\\ccswap-codex-" + data["runId"]
    data["exitCode"] = exit_code
    if -(2**31) <= exit_code <= 2**32 - 1:
        assert codex_session.validate_record(data, "run", run_id=data["runId"]) == data
    else:
        with pytest.raises(SessionError):
            codex_session.validate_record(data, "run", run_id=data["runId"])


@pytest.mark.parametrize("kind", ["directory", "fifo", "symlink"])
def test_nonordinary_open(kind, tmp_path):
    """Reject nonordinary files without blocking or following a link to valid content."""
    path = tmp_path / "record.json"
    if kind == "directory":
        path.mkdir()
    elif kind == "fifo":
        if os.name != "posix":
            pytest.skip("POSIX FIFO")
        os.mkfifo(path)
    else:
        target = tmp_path / "valid.json"
        target.write_text(json.dumps(record("manifest", tmp_path)))
        path.symlink_to(target)
    with pytest.raises(SessionError, match="Malformed"):
        codex_session.load_record(path, "manifest")


@pytest.mark.parametrize("plan", [
    "free", "go", "plus", "pro", "prolite", "promax", "team",
    "self_serve_business_prolite", "self_serve_business_usage_based",
])
def test_all_allowed_plans(plan):
    """Accept every known plan outside the cloud-configuration eligibility set."""
    assert codex_session.session_plan_supported(plan)
    assert not codex_session.session_plan_supported(plan.upper())


@pytest.mark.parametrize("plan", [
    "business", "ent26", "enterprise_cbp_automation", "enterprise_cbp_usage_based",
    "enterprise", "hc", "edu", "education", "edu_plus", "edu_pro", "sci", "sci_plus",
    "Free", "PLUS", "Team", "unknown", None,
])
def test_all_cloud_and_unknown_plans(plan):
    """Reject cloud-eligible plans, aliases and unrecognized case variants."""
    assert not codex_session.session_plan_supported(plan)


def test_fingerprint_golden_and_key_order():
    """Pin canonical UTF-8 serialization and prove key-order independence."""
    expected = "688218a4131df1c29ea57a1903c5145932e1db0b48a723a989731375ab11da9b"
    assert codex_session.auth_fingerprint({"z": "ä", "a": 1}) == expected
    assert codex_session.auth_fingerprint({"a": 1, "z": "ä"}) == expected


@pytest.mark.parametrize("change", ["padding", "trailing-bits"])
def test_noncanonical_valid_payload(change):
    """Reject noncanonical encoding even when the decoded claims are valid."""
    data = auth()
    payload = data["tokens"]["id_token"].split(".")[1]
    decoded = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
    while len(decoded) % 3 != 1:
        decoded += b" "
    payload = base64.urlsafe_b64encode(decoded).decode().rstrip("=")
    if change == "padding":
        payload += "=="
    else:
        alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
        payload = payload[:-1] + alphabet[alphabet.index(payload[-1]) + 1]
    data["tokens"]["id_token"] = f"e30.{payload}.sig"
    with pytest.raises(SessionError):
        codex_session.extract_identity(data)


def test_matching_fallback_and_owner_whitespace():
    """Accept agreeing user claims and reject padded optional owner identifiers."""
    assert codex_session.extract_identity(auth(user_id="user")) == (
        codex_session.extract_identity(auth())
    )
    data = auth()
    data["tokens"]["login_owner_id"] = " padded "
    with pytest.raises(SessionError):
        codex_session.extract_identity(data)


@pytest.fixture(params=[False, True], ids=["direct-parent", "linked-parent"])
def bootstrap(tmp_path, request):
    parent = tmp_path
    if request.param:
        parent = tmp_path / "alias"
        parent.symlink_to(tmp_path, target_is_directory=True)
    root, source = parent / "backup", parent / "default"
    root.mkdir()
    source.mkdir()
    home = root.resolve() / "codex" / "sessions" / "1-user_example.org"
    stage = home.with_name(f".{home.name}.preparing")
    return root, source, home, stage


def test_bootstrap_complete_and_existing_destination(bootstrap):
    root, source, home, stage = bootstrap
    document = dict(auth(), unknown={"preserve": True})
    assert codex_session.bootstrap_home(root, source, "1", document) == home
    assert not stage.exists()
    assert json.loads((home / "auth.json").read_text()) == document
    manifest = codex_session.load_record(home / ".ccswap-session.json", "manifest")
    assert manifest["bootstrapState"] == "ready"
    assert manifest["baselineFingerprint"] == codex_session.auth_fingerprint(document)
    assert manifest["home"] == str(home) and manifest["sourceHome"] == str(source.resolve())
    assert manifest["sharedItems"] == {}
    assert sorted(p.name for p in home.iterdir()) == [
        ".ccswap-session.json", "archived_sessions", "auth.json", "log", "sessions", "skills",
    ]
    if os.name == "posix":
        assert home.stat().st_mode & 0o777 == 0o700
        for entry in home.iterdir():
            assert entry.stat().st_mode & 0o777 == (0o700 if entry.is_dir() else 0o600)
    with pytest.raises(SessionError):
        codex_session.bootstrap_home(root, source, "1", document)
    assert json.loads((home / "auth.json").read_text()) == document
    assert list(source.iterdir()) == []


@pytest.mark.parametrize("failure", ["manifest", "auth", "ready", "publish"])
def test_bootstrap_interruption(bootstrap, monkeypatch, failure):
    root, source, home, stage = bootstrap
    write, publish = codex_session._session_write, codex_session._publish_home

    def denied_write(tree, path, data):
        if ((failure == "manifest" and path.name == ".ccswap-session.json")
                or (failure == "auth" and path.name == "auth.json")
                or (failure == "ready" and data.get("bootstrapState") == "ready")):
            raise PermissionError()
        write(tree, path, data)

    def denied_publish(*args):
        raise PermissionError()

    monkeypatch.setattr(codex_session, "_session_write", denied_write)
    if failure == "publish":
        monkeypatch.setattr(codex_session, "_publish_home", denied_publish)
    with pytest.raises(SessionError, match="evidence retained"):
        codex_session.bootstrap_home(root, source, "1", auth())
    assert stage.is_dir() and not home.exists()
    monkeypatch.setattr(codex_session, "_session_write", write)
    monkeypatch.setattr(codex_session, "_publish_home", publish)
    assert codex_session.bootstrap_home(root, source, "1", auth()) == home


def interrupted_stage(bootstrap, monkeypatch):
    root, source, home, stage = bootstrap
    with monkeypatch.context() as patch:
        def deny(*args):
            raise PermissionError()
        patch.setattr(codex_session, "_publish_home", deny)
        with pytest.raises(SessionError):
            codex_session.bootstrap_home(root, source, "1", auth())
    return root, source, home, stage


@pytest.mark.parametrize("conflict", [
    "runs", "runs-link", "manifest-link", "auth-link", "auth-dir", "skills-link",
    "stage-link", "identity", "account", "home", "source", "malformed", "unknown-schema",
])
def test_bootstrap_refuses_staging_conflicts(bootstrap, monkeypatch, tmp_path, conflict):
    root, source, home, stage = interrupted_stage(bootstrap, monkeypatch)
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_text("untouched")
    if conflict == "runs":
        (stage / "runs").mkdir()
    elif conflict == "runs-link":
        (stage / "runs").symlink_to(outside / "missing")
    elif conflict == "stage-link":
        stage.rename(outside / "stage")
        stage.symlink_to(outside / "stage", target_is_directory=True)
    elif conflict == "skills-link":
        (stage / "skills").rmdir()
        (stage / "skills").symlink_to(outside, target_is_directory=True)
    elif conflict in ("manifest-link", "auth-link", "auth-dir"):
        path = stage / ("auth.json" if conflict.startswith("auth") else ".ccswap-session.json")
        path.unlink()
        if conflict == "auth-dir":
            path.mkdir()
        else:
            path.symlink_to(sentinel)
    else:
        path = stage / ".ccswap-session.json"
        data = json.loads(path.read_text())
        if conflict == "identity":
            data["identity"]["userId"] = "other"
        elif conflict == "account":
            data["accountNumber"] = "2"
        elif conflict in ("home", "source"):
            data["home" if conflict == "home" else "sourceHome"] = str(outside)
        elif conflict == "unknown-schema":
            data["schemaVersion"] = 2
        path.write_text("{" if conflict == "malformed" else json.dumps(data))
    with pytest.raises(SessionError):
        codex_session.bootstrap_home(root, source, "1", auth())
    assert os.path.lexists(stage) and not home.exists()
    assert sentinel.read_text() == "untouched"


@pytest.mark.parametrize("destination", ["directory", "file", "dangling-link"])
def test_publish_never_overwrites(bootstrap, monkeypatch, destination):
    root, source, home, stage = bootstrap
    publish = codex_session._publish_home

    def competing_destination(stage, home):
        if destination == "directory":
            home.mkdir()  # Even an empty directory must survive.
        elif destination == "file":
            home.write_text("untouched")
        else:
            home.symlink_to(home.parent / "missing")
        publish(stage, home)

    monkeypatch.setattr(codex_session, "_publish_home", competing_destination)
    with pytest.raises(SessionError):
        codex_session.bootstrap_home(root, source, "1", auth())
    assert stage.is_dir() and os.path.lexists(home)
    if destination == "file":
        assert home.read_text() == "untouched"


def test_bootstrap_parent_link_and_alias(bootstrap, tmp_path):
    root, source, home, stage = bootstrap
    with pytest.raises(SessionError):
        codex_session.bootstrap_home(root, root, "1", auth())
    assert list(root.iterdir()) == []
    alias = tmp_path / "root-alias"
    alias.symlink_to(root, target_is_directory=True)
    source_alias = tmp_path / "source-alias"
    source_alias.symlink_to(source, target_is_directory=True)
    assert codex_session.bootstrap_home(alias, source_alias, "1", auth()) == home


def test_bootstrap_atomic_writer_failure(bootstrap, monkeypatch):
    from ccswap import fsutil

    root, source, home, stage = bootstrap
    replace = fsutil.replace_with_retry

    def deny_auth(src, dest, **kwargs):
        if dest.name == "auth.json":
            raise PermissionError()
        replace(src, dest, **kwargs)

    monkeypatch.setattr(fsutil, "replace_with_retry", deny_auth)
    with pytest.raises(SessionError):
        codex_session.bootstrap_home(root, source, "1", auth())
    assert not (stage / "auth.json").exists()
    assert codex_session.load_record(stage / ".ccswap-session.json", "manifest")[
        "bootstrapState"] == "preparing"
    assert not list(stage.glob("*.tmp"))
    monkeypatch.setattr(fsutil, "replace_with_retry", replace)
    assert codex_session.bootstrap_home(root, source, "1", auth()) == home


def test_bootstrap_manifest_read_denied(bootstrap, monkeypatch):
    root, source, home, stage = interrupted_stage(bootstrap, monkeypatch)
    original = codex_session.os.open

    def deny(path, *args, **kwargs):
        if os.fspath(path).endswith(".ccswap-session.json"):
            raise PermissionError()
        return original(path, *args, **kwargs)

    monkeypatch.setattr(codex_session.os, "open", deny)
    before = (stage / "auth.json").read_bytes()
    with pytest.raises(SessionError):
        codex_session.bootstrap_home(root, source, "1", auth())
    assert (stage / "auth.json").read_bytes() == before
    assert not home.exists()


def test_session_writer_rejects_escape_and_linked_parent(tmp_path):
    tree, outside = tmp_path / "tree", tmp_path / "outside"
    tree.mkdir()
    outside.mkdir()
    (tree / "link").symlink_to(outside, target_is_directory=True)
    for destination in (tree / ".." / "escape", outside / "escape", tree / "link" / "escape"):
        with pytest.raises(SessionError):
            codex_session._session_write(tree, destination, {})
    assert list(outside.iterdir()) == []
    assert not (tmp_path / "escape").exists()


def test_bootstrap_refuses_private_history_on_resume(bootstrap, monkeypatch):
    root, source, home, stage = interrupted_stage(bootstrap, monkeypatch)
    history = stage / "sessions" / "history"
    history.write_text("retained")
    with pytest.raises(SessionError, match=re.escape(str(stage))):
        codex_session.bootstrap_home(root, source, "1", auth())
    assert history.read_text() == "retained"
    assert not home.exists()


def test_bootstrap_directory_creation_denied(bootstrap, monkeypatch):
    from pathlib import Path

    root, source, home, stage = bootstrap
    mkdir = Path.mkdir

    def deny(path, *args, **kwargs):
        if path.name == "skills":
            raise PermissionError()
        return mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", deny)
    with pytest.raises(SessionError):
        codex_session.bootstrap_home(root, source, "1", auth())
    assert stage.is_dir() and not home.exists()
    assert codex_session.load_record(stage / ".ccswap-session.json", "manifest")[
        "bootstrapState"] == "preparing"


def test_ordinary_rejects_reparse_point(tmp_path, monkeypatch):
    from pathlib import Path
    from types import SimpleNamespace

    monkeypatch.setattr(Path, "lstat", lambda self: SimpleNamespace(
        st_mode=0o100600, st_file_attributes=0x400,
    ))
    with pytest.raises(SessionError):
        codex_session._ordinary(tmp_path / "redirected")


def test_bootstrap_publication_crash_keeps_ready_home(bootstrap, monkeypatch):
    root, source, home, stage = bootstrap
    publish = codex_session._publish_home

    def crash_after_publish(stage, home):
        publish(stage, home)
        raise KeyboardInterrupt()

    monkeypatch.setattr(codex_session, "_publish_home", crash_after_publish)
    with pytest.raises(KeyboardInterrupt):
        codex_session.bootstrap_home(root, source, "1", auth())
    assert not stage.exists()
    assert codex_session.load_record(home / ".ccswap-session.json", "manifest")[
        "bootstrapState"] == "ready"
    assert json.loads((home / "auth.json").read_text()) == auth()
    with pytest.raises(SessionError):
        codex_session.bootstrap_home(root, source, "1", auth())


@pytest.mark.parametrize("state", ["preparing", "ready"])
def test_bootstrap_resume_rotated_backup(bootstrap, monkeypatch, state):
    root, source, home, stage = interrupted_stage(bootstrap, monkeypatch)
    path = stage / ".ccswap-session.json"
    manifest = json.loads(path.read_text())
    manifest["bootstrapState"] = state
    path.write_text(json.dumps(manifest))
    current = auth()
    current["tokens"]["refresh_token"] = "synthetic-rotated"
    assert codex_session.bootstrap_home(root, source, "1", current) == home
    assert json.loads((home / "auth.json").read_text()) == current
    assert codex_session.load_record(home / path.name, "manifest")[
        "baselineFingerprint"] == codex_session.auth_fingerprint(current)


@pytest.mark.parametrize("content", [None, "untracked", ".ccswap-session.json"])
def test_bootstrap_stage_without_receipt(bootstrap, content):
    root, source, home, stage = bootstrap
    stage.mkdir(parents=True)
    if content is not None:
        (stage / content).write_text("evidence")
        with pytest.raises(SessionError):
            codex_session.bootstrap_home(root, source, "1", auth())
        assert (stage / content).read_text() == "evidence"
        assert not home.exists()
    else:
        assert codex_session.bootstrap_home(root, source, "1", auth()) == home


@pytest.mark.parametrize("component", ["codex", "sessions"])
def test_bootstrap_owned_parent_link_refused(bootstrap, tmp_path, component):
    root, source, home, stage = bootstrap
    outside = tmp_path / "outside"
    outside.mkdir()
    if component == "codex":
        (root / "codex").symlink_to(outside, target_is_directory=True)
    else:
        (root / "codex").mkdir()
        (root / "codex" / "sessions").symlink_to(outside, target_is_directory=True)
    with pytest.raises(SessionError):
        codex_session.bootstrap_home(root, source, "1", auth())
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("result", [0, -1])
def test_macos_publish_exclusive_abi(tmp_path, monkeypatch, result):
    from types import SimpleNamespace

    stage, home = tmp_path / "stage", tmp_path / "home"
    calls = []

    def rename(*args):
        calls.append(args)
        return result

    def library(name, *, use_errno):
        assert name == "/usr/lib/libSystem.B.dylib" and use_errno is True
        return SimpleNamespace(renamex_np=rename)

    monkeypatch.setattr(codex_session, "os", SimpleNamespace(
        name="posix", fsencode=os.fsencode, uname=lambda: SimpleNamespace(sysname="Darwin")))
    # The ABI follows the real OS even when tests steer the process platform elsewhere.
    monkeypatch.setattr(codex_session.sys, "platform", "linux")
    monkeypatch.setattr(codex_session.ctypes, "CDLL", library)
    monkeypatch.setattr(codex_session.ctypes, "get_errno", lambda: 17)
    if result:
        with pytest.raises(OSError) as error:
            codex_session._publish_home(stage, home)
        assert error.value.errno == 17
    else:
        codex_session._publish_home(stage, home)
    assert calls == [(os.fsencode(stage), os.fsencode(home), 0x00000004)]
    assert rename.argtypes == [codex_session.ctypes.c_char_p,
                              codex_session.ctypes.c_char_p, codex_session.ctypes.c_uint]
    assert rename.restype is codex_session.ctypes.c_int


@pytest.mark.parametrize("system", ["Linux", "FreeBSD"])
def test_publish_missing_primitive_refuses(tmp_path, monkeypatch, system):
    from types import SimpleNamespace

    monkeypatch.setattr(codex_session, "os", SimpleNamespace(
        name="posix", uname=lambda: SimpleNamespace(sysname=system)))
    monkeypatch.setattr(codex_session.ctypes, "CDLL", lambda *args, **kwargs: SimpleNamespace())
    with pytest.raises(SessionError):
        codex_session._publish_home(tmp_path / "stage", tmp_path / "home")


def test_bootstrap_imports_without_cycle(tmp_path):
    import subprocess
    import sys
    from pathlib import Path

    source = str(Path(codex_session.__file__).resolve().parents[1])
    result = subprocess.run([
        sys.executable, "-c",
        "import sys; sys.path.insert(0, sys.argv[1]); "
        "from ccswap import codex_session as c; "
        "from ccswap.fsutil import atomic_write_text; "
        "from ccswap.session import slugify_email; "
        "assert c.atomic_write_text is atomic_write_text; "
        "assert c.slugify_email is slugify_email; assert c.ctypes and c.sys",
        source,
    ], cwd=tmp_path, capture_output=True, timeout=10)
    assert result.returncode == 0


@pytest.mark.parametrize("relation", ["equal", "source-inside-stage", "stage-inside-source"])
def test_bootstrap_source_stage_overlap_before_mutation(bootstrap, monkeypatch, relation):
    root, source, home, stage = bootstrap
    if relation == "equal":
        source = stage
    elif relation == "source-inside-stage":
        source = stage / "nested"
    else:
        source = stage.parent
    source.mkdir(parents=True, exist_ok=True)

    def no_mutation(*args, **kwargs):
        pytest.fail("overlap must be rejected before directory mutation")

    monkeypatch.setattr(codex_session, "_private_directory", no_mutation)
    with pytest.raises(SessionError):
        codex_session.bootstrap_home(root, source, "1", auth())
    assert not home.exists()
    assert list(source.iterdir()) == []


@pytest.mark.parametrize("foreign", ["stage", "auth.json"])
def test_bootstrap_foreign_owner_refused(bootstrap, monkeypatch, foreign):
    if os.name != "posix":
        pytest.skip("POSIX euid ownership")
    from pathlib import Path
    from types import SimpleNamespace

    root, source, home, stage = interrupted_stage(bootstrap, monkeypatch)
    target = stage if foreign == "stage" else stage / foreign
    original = Path.lstat

    def foreign_owner(path, *args, **kwargs):
        info = original(path, *args, **kwargs)
        if path == target:
            return SimpleNamespace(st_mode=info.st_mode, st_uid=os.geteuid() + 1)
        return info

    monkeypatch.setattr(Path, "lstat", foreign_owner)
    with pytest.raises(SessionError, match=re.escape(str(stage))):
        codex_session.bootstrap_home(root, source, "1", auth())
    assert stage.is_dir() and not home.exists()


@pytest.mark.parametrize("entry", [
    "skills/.system", "skills/link", "extra-link", "extra-file", "runs", "extra-dir",
    "auth.json.abcdefgh.tmp.extra", "auth.json.abcdefg.tmp", "auth.json.ABCDEFGH.tmp",
    "auth.json.abcdefghi.tmp", "other.json.abcdefgh.tmp", "auth.json.abcdefg-.tmp",
])
def test_bootstrap_allowlist_retains_all_on_conflict(bootstrap, monkeypatch, entry):
    root, source, home, stage = interrupted_stage(bootstrap, monkeypatch)
    leftover = stage / "auth.json.abcdefgh.tmp"
    leftover.write_text("synthetic-leftover")
    # Probe this directory rather than assuming the host OS dictates case sensitivity.
    if (stage / "auth.json.ABCDEFGH.tmp").exists():
        distinct = stage / "auth.json.ijklmnop.tmp"
        leftover.rename(distinct)
        leftover = distinct
    path = stage / entry
    if entry.endswith("link"):
        path.symlink_to(stage / "missing")
    elif entry in ("skills/.system", "extra-dir", "runs"):
        path.mkdir()
    else:
        path.write_text("evidence")
    with pytest.raises(SessionError, match=re.escape(str(stage))):
        codex_session.bootstrap_home(root, source, "1", auth())
    assert os.path.lexists(path) and leftover.exists()
    assert not home.exists()


@pytest.mark.parametrize("name", ["auth.json", ".ccswap-session.json"])
def test_bootstrap_removes_exact_writer_leftovers(bootstrap, monkeypatch, name):
    import tempfile

    root, source, home, stage = interrupted_stage(bootstrap, monkeypatch)
    descriptor, temporary = tempfile.mkstemp(dir=stage, prefix=f"{name[:40]}.", suffix=".tmp")
    os.close(descriptor)
    assert codex_session.bootstrap_home(root, source, "1", auth()) == home
    assert not os.path.lexists(temporary)
    assert sorted(path.name for path in home.iterdir()) == [
        ".ccswap-session.json", "archived_sessions", "auth.json", "log", "sessions", "skills",
    ]


def test_bootstrap_directory_fsync_order(bootstrap, monkeypatch):
    root, source, home, stage = bootstrap
    events = []
    sync, publish = codex_session._sync_directory, codex_session._publish_home

    def checked_sync(path):
        manifest_home = stage if path == stage else home
        assert codex_session.load_record(manifest_home / ".ccswap-session.json", "manifest")[
            "bootstrapState"] == "ready"
        events.append(path)
        sync(path)

    def checked_publish(stage, home):
        events.append("publish")
        publish(stage, home)

    monkeypatch.setattr(codex_session, "_sync_directory", checked_sync)
    monkeypatch.setattr(codex_session, "_publish_home", checked_publish)
    codex_session.bootstrap_home(root, source, "1", auth())
    assert events == [stage, "publish", home.parent]


@pytest.mark.parametrize("point", ["stage", "sessions"])
def test_bootstrap_directory_fsync_failure(bootstrap, monkeypatch, point):
    root, source, home, stage = bootstrap

    def denied(path):
        if path == (stage if point == "stage" else home.parent):
            raise OSError(5, "not exposed")

    monkeypatch.setattr(codex_session, "_sync_directory", denied)
    with pytest.raises(SessionError, match="EIO"):
        codex_session.bootstrap_home(root, source, "1", auth())
    assert stage.exists() is (point == "stage")
    assert home.exists() is (point == "sessions")


def test_bootstrap_publication_errno_redacted(bootstrap, monkeypatch):
    root, source, home, stage = bootstrap

    def unsupported(*args):
        raise OSError(22, "synthetic-sensitive-value")

    monkeypatch.setattr(codex_session, "_publish_home", unsupported)
    with pytest.raises(SessionError, match="EINVAL") as caught:
        codex_session.bootstrap_home(root, source, "1", auth())
    assert str(stage) in str(caught.value)
    assert "synthetic-sensitive-value" not in str(caught.value)


def test_bootstrap_does_not_chmod_existing_directories(bootstrap, monkeypatch):
    from pathlib import Path

    root, source, home, stage = interrupted_stage(bootstrap, monkeypatch)
    existing = {root.resolve(), root.resolve() / "codex", home.parent, stage,
                *(stage / name for name in codex_session._BOOTSTRAP_DIRS)}
    original = Path.chmod

    def checked(path, *args, **kwargs):
        assert path not in existing
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "chmod", checked)
    assert codex_session.bootstrap_home(root, source, "1", auth()) == home


@pytest.mark.parametrize("error", [RecursionError, RuntimeError])
def test_bootstrap_safe_runtime_errors(bootstrap, monkeypatch, error):
    from pathlib import Path

    root, source, home, stage = bootstrap

    def fail(*args, **kwargs):
        raise error("synthetic-sensitive-value")

    monkeypatch.setattr(Path, "resolve", fail)
    with pytest.raises(SessionError) as caught:
        codex_session.bootstrap_home(root, source, "1", auth())
    assert "synthetic-sensitive-value" not in str(caught.value)


@pytest.mark.parametrize("denied", [False, True])
def test_windows_publish_and_directory_sync(tmp_path, monkeypatch, denied):
    from types import SimpleNamespace

    stage, home = tmp_path / "stage", tmp_path / "home"
    calls = []

    def rename(source, destination):
        calls.append((source, destination))
        if denied:
            raise FileExistsError()

    monkeypatch.setattr(codex_session, "os", SimpleNamespace(name="nt", rename=rename))
    if denied:
        with pytest.raises(FileExistsError):
            codex_session._publish_home(stage, home)
    else:
        codex_session._publish_home(stage, home)
    codex_session._sync_directory(tmp_path)  # No Windows directory open/fsync.
    assert calls == [(stage, home)]


@pytest.fixture
def sharing(bootstrap):
    root, source, home, _ = bootstrap
    codex_session.bootstrap_home(root, source, "1", auth())
    manifest = codex_session.load_record(home / ".ccswap-session.json", "manifest")
    return root.resolve(), source.resolve(), home, manifest


def assert_link(path, target):
    if os.name == "nt":
        assert codex_session._same_link(os.readlink(path), str(target))
    else:
        assert os.readlink(path) == str(target)


def share(state):
    root, source, home, manifest = state
    result, warnings = codex_session.sync_sharing(
        root, home, source, manifest,
        lambda data: codex_session._session_write(root, home / ".ccswap-session.json", data),
    )
    return result


def test_sharing_first_repeat_added_removed_private(sharing):
    root, source, home, original = sharing
    for name in codex_session._SHARED_FILES:
        (source / name).write_text(name)
    for name in codex_session._SHARED_DIRS:
        (source / name).mkdir()
        (source / name / "definition").write_text("definition")
    private = ["auth.json", ".credentials.json", "history.jsonl", "state_5.sqlite",
               "memories_1.sqlite", "models_cache.json"]
    for name in private:
        (source / name).write_text("source-private")
        if name != "auth.json":
            (home / name).write_text("home-private")
    result = share(sharing)
    assert original["sharedItems"] == {}
    assert set(result["sharedItems"]) == set((*codex_session._SHARED_FILES,
                                              *codex_session._SHARED_DIRS))
    for name, item in result["sharedItems"].items():
        assert item == {"source": str(source / name), "mode": "symlink"}
        assert_link(home / name, source / name)
    assert share((root, source, home, result)) == result
    (source / "AGENTS.md").unlink()
    removed = share((root, source, home, result))
    assert not os.path.lexists(home / "AGENTS.md")
    assert "AGENTS.md" not in removed["sharedItems"]
    (source / "AGENTS.md").write_text("added")
    assert "AGENTS.md" in share((root, source, home, removed))["sharedItems"]
    assert json.loads((home / "auth.json").read_text()) == auth()
    assert all((home / name).read_text() == "home-private" for name in private[1:])
    assert codex_session.load_record(home / ".ccswap-session.json", "manifest")[
        "sharedItems"] == share((root, source, home, removed))["sharedItems"]


@pytest.mark.parametrize("kind", ["file", "directory", "link", "dangling"])
def test_sharing_unmanaged_conflict(sharing, kind):
    _, source, home, _ = sharing
    (source / "rules").mkdir()
    destination = home / "rules"
    if kind == "file":
        destination.write_text("foreign")
    elif kind == "directory":
        destination.mkdir()
    else:
        destination.symlink_to(source / "missing")
    with pytest.raises(SessionError):
        share(sharing)
    assert os.path.lexists(destination)


@pytest.mark.parametrize("removed", [False, True])
def test_sharing_retargeted_link_refused(sharing, removed):
    root, source, home, _ = sharing
    (source / "config.toml").write_text("original")
    result = share(sharing)
    destination = home / "config.toml"
    destination.unlink()
    destination.symlink_to(source / "foreign")
    if removed:
        (source / "config.toml").unlink()
    with pytest.raises(SessionError):
        share((root, source, home, result))
    assert_link(destination, source / "foreign")


def test_sharing_skills_entries_system_private(sharing):
    root, source, home, _ = sharing
    (source / "skills").mkdir()
    (source / "skills" / ".system").mkdir()
    (source / "skills" / ".system" / "source").write_text("private")
    (source / "skills" / "user").mkdir()
    (source / "skills" / "single.md").write_text("user")
    (home / "skills" / ".system").mkdir()
    (home / "skills" / ".system" / "local").write_text("private")
    result = share(sharing)
    assert set(result["sharedItems"]) == {"skills/user", "skills/single.md"}
    assert not (home / "skills").is_symlink()
    assert (home / "skills" / ".system" / "local").read_text() == "private"
    (source / "skills" / "single.md").unlink()
    assert "skills/single.md" not in share((root, source, home, result))["sharedItems"]


@pytest.fixture
def windows_copy(monkeypatch):
    from pathlib import Path

    def unavailable(*args, **kwargs):
        raise OSError(1, "synthetic-sensitive-value")

    monkeypatch.setattr(codex_session.sys, "platform", "win32")
    monkeypatch.setattr(Path, "symlink_to", unavailable)


def test_sharing_windows_refresh_and_remove(sharing, windows_copy):
    root, source, home, _ = sharing
    (source / "config.toml").write_bytes(b"first\x00")
    (source / "rules").mkdir()
    (source / "rules" / "old").write_text("old")
    result = share(sharing)
    assert {item["mode"] for item in result["sharedItems"].values()} == {"copy"}
    (home / "config.toml").write_text("user edit")
    (home / "rules" / "local").write_text("user edit")
    (source / "config.toml").write_bytes(b"second\x00")
    (source / "rules" / "old").unlink()
    (source / "rules" / "new").write_text("new")
    result = share((root, source, home, result))
    assert (home / "config.toml").read_bytes() == b"second\x00"
    assert [p.name for p in (home / "rules").iterdir()] == ["new"]
    (source / "config.toml").unlink()
    (source / "rules" / "new").unlink()
    (source / "rules").rmdir()
    assert share((root, source, home, result))["sharedItems"] == {}
    assert not (home / "rules").exists() and not (home / "config.toml").exists()


@pytest.mark.parametrize("kind", ["safe-link", "credential", "cycle", "runtime"])
def test_sharing_source_redirections(sharing, monkeypatch, kind):
    _, source, home, _ = sharing
    (source / "rules").mkdir()
    if kind == "safe-link":
        target = source / "rules" / "ordinary"
        target.write_text("definition")
    elif kind == "credential":
        target = source / "auth.json"
        target.write_text("private")
    elif kind == "runtime":
        target = home / "sessions"
    else:
        target = source / "rules"
    (source / "rules" / "link").symlink_to(target)
    share(sharing)  # Symlink mode deliberately does not inspect directory contents.
    (home / "rules").unlink()
    from pathlib import Path
    monkeypatch.setattr(codex_session.sys, "platform", "win32")
    monkeypatch.setattr(Path, "symlink_to", lambda *a, **k: (_ for _ in ()).throw(OSError()))
    with pytest.raises(SessionError):
        share(sharing)
    assert not os.path.lexists(home / "rules")


@pytest.mark.parametrize("removed", [False, True])
def test_sharing_copy_foreign_child_retained(sharing, windows_copy, monkeypatch, removed):
    from pathlib import Path

    root, source, home, _ = sharing
    (source / "rules").mkdir()
    result = share(sharing)
    monkeypatch.undo()
    (home / "rules" / "foreign").symlink_to(source / "rules")
    if removed:
        (source / "rules").rmdir()
    with pytest.raises(SessionError):
        share((root, source, home, result))
    assert (home / "rules" / "foreign").is_symlink()


def test_sharing_copy_failure_preserves_previous(sharing, windows_copy, monkeypatch):
    root, source, home, _ = sharing
    (source / "rules").mkdir()
    (source / "rules" / "item").write_text("old")
    result = share(sharing)
    (source / "rules" / "item").write_text("new")

    def denied(*args, **kwargs):
        raise PermissionError(13, "synthetic-sensitive-value")

    monkeypatch.setattr(codex_session.shutil, "copytree", denied)
    with pytest.raises(SessionError) as caught:
        share((root, source, home, result))
    assert "synthetic-sensitive-value" not in str(caught.value)
    assert (home / "rules" / "item").read_text() == "old"
    assert not list(home.glob(".ccswap-sharing-*"))


def test_sharing_receipt_wrong_source_refused(sharing):
    root, source, home, manifest = sharing
    manifest["sharedItems"]["config.toml"] = dict(source=str(source / "auth.json"), mode="symlink")
    (home / "config.toml").symlink_to(source / "auth.json")
    with pytest.raises(SessionError):
        share(sharing)
    assert (home / "config.toml").is_symlink()


@pytest.mark.parametrize("kind", ["denied", "reparse"])
def test_sharing_source_unknown_never_absent(sharing, monkeypatch, kind):
    from pathlib import Path
    from types import SimpleNamespace

    _, source, home, _ = sharing
    target = source / "config.toml"
    target.write_text("definition")
    original = Path.lstat

    def altered(path, *args, **kwargs):
        if path == target:
            if kind == "denied":
                raise PermissionError(13, "synthetic-sensitive-value")
            return SimpleNamespace(st_mode=0o100600, st_file_attributes=0x400)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", altered)
    with pytest.raises(SessionError) as caught:
        share(sharing)
    assert "synthetic-sensitive-value" not in str(caught.value)
    assert not os.path.lexists(home / "config.toml")


def test_sharing_posix_symlink_failure_no_copy(sharing, monkeypatch):
    from pathlib import Path

    _, source, home, _ = sharing
    (source / "config.toml").write_text("definition")
    monkeypatch.setattr(codex_session.sys, "platform", "linux")

    def denied(*args, **kwargs):
        raise PermissionError()

    monkeypatch.setattr(Path, "symlink_to", denied)
    with pytest.raises(SessionError):
        share(sharing)
    assert not (home / "config.toml").exists()


def test_sharing_windows_system_case_alias_skipped(sharing, windows_copy):
    _, source, home, _ = sharing
    (source / "skills").mkdir()
    (source / "skills" / ".SYSTEM").mkdir()
    assert share(sharing)["sharedItems"] == {}
    assert list((home / "skills").iterdir()) == []


def test_sharing_missing_recorded_destination_recreated(sharing):
    root, source, home, _ = sharing
    (source / "config.toml").write_text("definition")
    result = share(sharing)
    (home / "config.toml").unlink()
    assert share((root, source, home, result)) == result
    assert (home / "config.toml").is_symlink()


def test_sharing_copy_file_atomic_failure(sharing, windows_copy, monkeypatch):
    root, source, home, _ = sharing
    (source / "config.toml").write_text("first")
    result = share(sharing)
    (source / "config.toml").write_text("second")

    def denied(*args, **kwargs):
        raise PermissionError()

    monkeypatch.setattr(codex_session, "atomic_write_bytes", denied)
    with pytest.raises(SessionError):
        share((root, source, home, result))
    assert (home / "config.toml").read_text() == "first"


def test_sharing_copy_foreign_owner(sharing, windows_copy, monkeypatch):
    from pathlib import Path
    from types import SimpleNamespace

    if os.name != "posix":
        pytest.skip("POSIX euid ownership")
    root, source, home, _ = sharing
    target = home / "config.toml"
    (source / "config.toml").write_text("definition")
    result = share(sharing)
    original = Path.lstat

    def foreign(path, *args, **kwargs):
        info = original(path, *args, **kwargs)
        if path == target:
            return SimpleNamespace(st_mode=info.st_mode, st_uid=os.geteuid() + 1)
        return info

    monkeypatch.setattr(Path, "lstat", foreign)
    with pytest.raises(SessionError):
        share((root, source, home, result))
    assert target.read_text() == "definition"


def sharing_call(state, persist=None):
    root, source, home, manifest = state
    if persist is None:
        persist = lambda data: codex_session._session_write(
            root, home / ".ccswap-session.json", data)
    return codex_session.sync_sharing(root, home, source, manifest, persist)


@pytest.mark.parametrize("copying", [False, True])
def test_sharing_failure_second_item_retry(sharing, monkeypatch, copying):
    from pathlib import Path

    root, source, home, original = sharing
    for name in ("config.toml", "AGENTS.md"):
        (source / name).write_text(name)
    if copying:
        monkeypatch.setattr(codex_session.sys, "platform", "win32")

        def unavailable(*args, **kwargs):
            raise OSError()

        monkeypatch.setattr(Path, "symlink_to", unavailable)
    writes = []

    def persist(data):
        writes.append(copy.deepcopy(data))
        # Copies fail after the second write-ahead; symlinks after creation.
        if "AGENTS.md" in data["sharedItems"]:
            raise OSError(5, "synthetic-sensitive-value")
        codex_session._session_write(root, home / ".ccswap-session.json", data)

    with pytest.raises(SessionError):
        sharing_call(sharing, persist)
    assert (home / "config.toml").read_text() == "config.toml"
    receipt = codex_session.load_record(home / ".ccswap-session.json", "manifest")
    if not copying:
        assert (home / "AGENTS.md").is_symlink()  # Unrecorded exact link is adopted.
    result, warnings = sharing_call((root, source, home, receipt))
    assert not warnings
    assert set(result["sharedItems"]) == {"config.toml", "AGENTS.md"}
    assert original["sharedItems"] == {}
    assert writes[0]["sharedItems"]["config.toml"]["mode"] == (
        "copy" if copying else "symlink")


def test_sharing_copy_write_ahead_and_publish_rollback(sharing, windows_copy, monkeypatch):
    root, source, home, _ = sharing
    (source / "rules").mkdir()
    (source / "rules" / "item").write_text("old")
    result, _ = sharing_call(sharing)
    (source / "rules" / "item").write_text("new")
    replace = codex_session.replace_with_retry
    events = []

    def interrupted(src, dst):
        src, dst = codex_session.Path(src), codex_session.Path(dst)
        assert codex_session.load_record(home / ".ccswap-session.json", "manifest")[
            "sharedItems"]["rules"]["mode"] == "copy"
        if src.name.startswith(".ccswap-new-") and dst == home / "rules":
            assert not dst.exists()
            old = list((home / ".ccswap-tmp").glob(".ccswap-old-*"))
            assert len(old) == 1 and (old[0] / "item").read_text() == "old"
            events.append("publish-failed")
            raise OSError(5, "interrupted")
        events.append((src, dst))
        return replace(src, dst)

    monkeypatch.setattr(codex_session, "replace_with_retry", interrupted)
    with pytest.raises(SessionError):
        sharing_call((root, source, home, result))
    assert "publish-failed" in events
    assert (home / "rules" / "item").read_text() == "old"
    monkeypatch.setattr(codex_session, "replace_with_retry", replace)
    result, _ = sharing_call((root, source, home, result))
    assert (home / "rules" / "item").read_text() == "new"
    # Simulate a process crash after moving the old copy, before publication.
    temporary = home / ".ccswap-tmp"
    (home / "rules").rename(temporary / ".ccswap-old-crashed")
    (temporary / ".ccswap-new-crashed").mkdir()
    (temporary / ".ccswap-new-crashed" / "partial").write_text("partial")
    sharing_call((root, source, home, result))
    assert (home / "rules" / "item").read_text() == "new"
    assert list(temporary.iterdir()) == []
    assert not list((home / "skills").glob(".ccswap-*"))


@pytest.mark.parametrize("target", ["source", "destination"])
def test_sharing_os_lstat_denied_nothing_removed(sharing, monkeypatch, target):
    root, source, home, _ = sharing
    (source / "config.toml").write_text("definition")
    result, _ = sharing_call(sharing)
    (source / "config.toml").unlink()
    denied = (source if target == "source" else home) / "config.toml"
    lstat = os.lstat
    intercepted = []
    receipt = (home / ".ccswap-session.json").read_bytes()

    def forbidden(path, *args, **kwargs):
        if codex_session._same_link(os.fspath(path), str(denied)):
            intercepted.append(path)
            raise PermissionError(13, "synthetic-sensitive-value")
        return lstat(path, *args, **kwargs)

    monkeypatch.setattr(os, "lstat", forbidden)
    with pytest.raises(SessionError):
        sharing_call((root, source, home, result))
    assert intercepted
    assert (home / ".ccswap-session.json").read_bytes() == receipt
    assert_link(home / "config.toml", source / "config.toml")


@pytest.mark.skipif(os.name == "nt", reason="Backslash cannot be a Windows filename")
def test_sharing_invalid_skill_warns_before_mutation(sharing, monkeypatch):
    _, source, home, _ = sharing
    (source / "skills").mkdir()
    (source / "skills" / "bad\\name").write_text("invalid")
    (source / "skills" / "valid").write_text("definition")
    (source / "config.toml").write_text("definition")
    result, warnings = sharing_call(sharing)
    assert warnings == ["Skipping skill entry: bad\\name"]
    assert set(result["sharedItems"]) == {"config.toml", "skills/valid"}
    assert not (home / "skills" / "bad\\name").exists()


@pytest.mark.parametrize("kind", ["external", "dangling", "auth", "credentials", "runtime"])
def test_sharing_skill_link_policy(sharing, tmp_path, kind):
    root, source, home, _ = sharing
    (source / "skills").mkdir()
    target = tmp_path / "external"
    if kind == "auth":
        target = tmp_path / "auth.json"
    elif kind == "credentials":
        target = tmp_path / ".credentials.json"
    elif kind == "runtime":
        target = source / "sessions"
    if kind != "dangling":
        target.mkdir()
    (source / "skills" / "linked").symlink_to(target)
    result, warnings = sharing_call(sharing)
    assert bool(warnings) == (kind != "external")
    assert ("skills/linked" in result["sharedItems"]) == (kind == "external")
    assert (home / "skills" / "linked").is_symlink() == (kind == "external")


@pytest.mark.parametrize("name", ["config.toml", "rules"])
@pytest.mark.parametrize("allowed", [False, True])
def test_sharing_required_external_link_policy(sharing, tmp_path, name, allowed):
    _, source, home, _ = sharing
    target = tmp_path / ("definitions" if allowed else "auth.json")
    if name == "rules":
        target.mkdir()
    else:
        target.write_text("definition")
    (source / name).symlink_to(target)
    if allowed:
        result, warnings = sharing_call(sharing)
        assert not warnings and name in result["sharedItems"]
    else:
        with pytest.raises(SessionError):
            sharing_call(sharing)
        assert not os.path.lexists(home / name)


@pytest.mark.parametrize("platform", ["darwin", "win32"])
def test_sharing_system_case_and_identity(sharing, monkeypatch, platform):
    _, source, home, _ = sharing
    skills = source / "skills"
    skills.mkdir()
    (skills / ".system").mkdir()
    (skills / ".SYSTEM").mkdir(exist_ok=os.name == "nt" or sys.platform == "darwin")
    (skills / "alias").symlink_to(skills / ".system")
    (skills / "identity-alias").mkdir()
    samefile = os.path.samefile

    def identical(left, right):
        if codex_session.Path(left) == skills / "identity-alias":
            return True
        return samefile(left, right)

    monkeypatch.setattr(codex_session.sys, "platform", platform)
    monkeypatch.setattr(os.path, "samefile", identical)
    result, warnings = sharing_call(sharing)
    assert not result["sharedItems"]
    assert set(warnings) == {"Skipping skill entry: alias", "Skipping skill entry: identity-alias"}
    assert list((home / "skills").iterdir()) == []


def test_sharing_tmp_foreign_link_retained(sharing):
    _, source, home, _ = sharing
    temporary = home / ".ccswap-tmp"
    temporary.mkdir()
    (temporary / ".ccswap-new-foreign").symlink_to(source)
    with pytest.raises(SessionError):
        sharing_call(sharing)
    assert (temporary / ".ccswap-new-foreign").is_symlink()


def test_sharing_lines_within_budget():
    from pathlib import Path
    text = Path(codex_session.__file__).read_text()
    addition = text[text.index("_SHARED_FILES ="):]
    assert all(len(line) <= 100 for line in addition.splitlines())


def test_sharing_second_copy_interrupted_receipt_durable(sharing, windows_copy, monkeypatch):
    root, source, home, _ = sharing
    for name in ("config.toml", "AGENTS.md"):
        (source / name).write_text(name)
    original = codex_session._copy_shared

    def interrupted(src, dst, *args):
        receipt = codex_session.load_record(home / ".ccswap-session.json", "manifest")
        assert receipt["sharedItems"][src.name]["mode"] == "copy"
        if src.name == "AGENTS.md":
            assert not dst.exists()
            raise OSError(5, "interrupted")
        return original(src, dst, *args)

    monkeypatch.setattr(codex_session, "_copy_shared", interrupted)
    with pytest.raises(SessionError):
        sharing_call(sharing)
    receipt = codex_session.load_record(home / ".ccswap-session.json", "manifest")
    assert set(receipt["sharedItems"]) == {"config.toml", "AGENTS.md"}
    monkeypatch.setattr(codex_session, "_copy_shared", original)
    sharing_call((root, source, home, receipt))
    assert (home / "AGENTS.md").read_text() == "AGENTS.md"


@pytest.mark.parametrize("kind", ["dangling", "backup", "private-default"])
def test_sharing_required_link_refuses_before_removal(sharing, kind):
    root, source, home, _ = sharing
    (source / "AGENTS.md").write_text("original")
    result, _ = sharing_call(sharing)
    (source / "AGENTS.md").unlink()
    if kind == "dangling":
        target = source / "missing"
    elif kind == "backup":
        target = root / "definition"
        target.write_text("private")
    else:
        target = source / "history.jsonl"
        target.write_text("private")
    (source / "config.toml").symlink_to(target)
    with pytest.raises(SessionError):
        sharing_call((root, source, home, result))
    assert_link(home / "AGENTS.md", source / "AGENTS.md")


def test_sharing_nonrecursive_symlink_directory(sharing, monkeypatch):
    from pathlib import Path

    _, source, _, _ = sharing
    (source / "rules").mkdir()
    original = Path.iterdir

    def forbidden(path):
        if path == source / "rules":
            raise AssertionError("symlink mode must not recurse")
        return original(path)

    monkeypatch.setattr(Path, "iterdir", forbidden)
    result, _ = sharing_call(sharing)
    assert result["sharedItems"]["rules"]["mode"] == "symlink"


def test_sharing_skill_link_cycle_skipped(sharing):
    _, source, home, _ = sharing
    (source / "skills").mkdir()
    (source / "skills" / "cycle").symlink_to(source / "skills" / "cycle")
    result, warnings = sharing_call(sharing)
    assert result["sharedItems"] == {}
    assert warnings == ["Skipping skill entry: cycle"]
    assert list((home / "skills").iterdir()) == []


@pytest.mark.parametrize("left,right,equal", [
    (r"\\?\C:\Definitions\config.toml", r"c:\definitions\config.toml", True),
    (r"C:\Definitions\config.toml", r"\\?\c:\definitions\config.toml", True),
    (r"\\?\C:\Definitions\other.toml", r"C:\Definitions\config.toml", False),
    (r"\\?\UNC\host\share\file", r"\\host\share\file", False),
    (r"\\?\Volume{example}\file", r"Volume{example}\file", False),
    (r"C:config.toml", r"C:\config.toml", False),
    (r"\config.toml", r"\config.toml", False),
])
def test_windows_link_spelling(left, right, equal, monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(codex_session, "os", SimpleNamespace(name="nt"))
    assert codex_session._same_link(left, right) is equal


@pytest.mark.parametrize("initial_directory", [False, True])
def test_sharing_copy_source_type_change(sharing, windows_copy, monkeypatch, initial_directory):
    root, source, home, _ = sharing
    (source / "skills").mkdir()
    source_item = source / "skills" / "changing"
    destination = home / "skills" / "changing"
    if initial_directory:
        source_item.mkdir()
        (source_item / "item").write_text("old")
    else:
        source_item.write_text("old")
    result, _ = sharing_call(sharing)
    if initial_directory:
        (source_item / "item").unlink()
        source_item.rmdir()
        source_item.write_text("new")
    else:
        source_item.unlink()
        source_item.mkdir()
        (source_item / "item").write_text("new")
    replace = codex_session.replace_with_retry

    def fail_publish(src, dst):
        if dst == destination:
            assert not destination.exists()
            raise OSError(5, "publish interrupted")
        return replace(src, dst)

    # Fail just once so rollback can restore the existing destination type.
    failed = False

    def fail_once(src, dst):
        nonlocal failed
        if dst == destination and not failed:
            failed = True
            return fail_publish(src, dst)
        return replace(src, dst)

    monkeypatch.setattr(codex_session, "replace_with_retry", fail_once)
    with pytest.raises(SessionError):
        sharing_call((root, source, home, result))
    assert destination.is_dir() == initial_directory
    old_content = destination / "item" if initial_directory else destination
    assert old_content.read_text() == "old"
    sharing_call((root, source, home, result))
    assert destination.is_dir() != initial_directory
    new_content = destination if initial_directory else destination / "item"
    assert new_content.read_text() == "new"


@pytest.mark.parametrize("source_removed", [False, True])
def test_sharing_orphan_old_retained_until_success(sharing, windows_copy, monkeypatch,
                                                  source_removed):
    root, source, home, _ = sharing
    (source / "rules").mkdir()
    (source / "rules" / "item").write_text("old")
    result, _ = sharing_call(sharing)
    orphan = home / ".ccswap-tmp" / ".ccswap-old-crashed"
    (home / "rules").rename(orphan)
    (source / "rules" / "item").write_text("new")
    original = codex_session.shutil.copytree

    def denied(*args, **kwargs):
        raise PermissionError()

    monkeypatch.setattr(codex_session.shutil, "copytree", denied)
    with pytest.raises(SessionError):
        sharing_call((root, source, home, result))
    assert (orphan / "item").read_text() == "old"
    assert not (home / "rules").exists()
    monkeypatch.setattr(codex_session.shutil, "copytree", original)
    if source_removed:
        (source / "rules" / "item").unlink()
        (source / "rules").rmdir()
    sharing_call((root, source, home, result))
    assert not orphan.exists()
    if source_removed:
        assert not (home / "rules").exists()
    else:
        assert (home / "rules" / "item").read_text() == "new"


@pytest.mark.parametrize("lineage", [None, [], "a" * 64, [True], ["A" * 64],
                                    ["b" * 64], ["a" * 64] * 65])
def test_manifest_lineage_validation(tmp_path, lineage):
    manifest = record("manifest", tmp_path)
    manifest["lineage"] = lineage
    with pytest.raises(SessionError):
        codex_session.validate_record(manifest, "manifest")


def test_manifest_lineage_required(tmp_path):
    manifest = record("manifest", tmp_path)
    del manifest["lineage"]
    with pytest.raises(SessionError):
        codex_session.validate_record(manifest, "manifest")


def test_baseline_lineage_bounded_and_idempotent(tmp_path):
    manifest = record("manifest", tmp_path)
    seed = manifest["baselineFingerprint"]
    entries = [f"{n:064x}" for n in range(70)]
    for fingerprint in entries:
        previous = manifest
        manifest = codex_session.with_baseline(manifest, fingerprint)
        assert previous["lineage"][-1] == previous["baselineFingerprint"]
        codex_session.validate_record(manifest, "manifest")
    assert manifest["lineage"] == entries[-64:] and seed not in manifest["lineage"]
    assert codex_session.with_baseline(manifest, entries[-1]) == manifest


@pytest.mark.parametrize("timestamp", [None, "bad", "2026-10-25T12:00:00", "2026-02-30T12:00:00Z"])
@pytest.mark.parametrize("side", ["default", "stored"])
def test_default_write_decision_missing_or_invalid_timestamp(tmp_path, timestamp, side):
    d = dict(auth(), rotation=1, last_refresh="2026-10-25T12:01:00Z")
    s = dict(auth(), last_refresh="2026-10-25T12:00:00Z")
    target = d if side == "default" else s
    if timestamp is None:
        del target["last_refresh"]
    else:
        target["last_refresh"] = timestamp
    manifest = codex_session._new_manifest(tmp_path / "home", tmp_path / "default", "1", auth())
    manifest = codex_session.with_baseline(manifest, codex_session.auth_fingerprint(s))
    assert codex_session.default_write_decision(d, s, s, manifest) == "conflict"


def test_default_write_decision_is_pure_and_checks_history_before_time(tmp_path):
    seed = dict(auth(), last_refresh="2026-10-25T12:02:00Z")
    current = dict(auth(), rotation=2, last_refresh="2026-10-25T12:00:00Z")
    manifest = codex_session._new_manifest(tmp_path / "home", tmp_path / "default", "1", seed)
    manifest = codex_session.with_baseline(manifest, codex_session.auth_fingerprint(current))
    before = copy.deepcopy((seed, current, manifest))
    assert codex_session.default_write_decision(seed, current, current, manifest) == "skip"
    assert (seed, current, manifest) == before


def test_resumed_bootstrap_preserves_previous_seed_lineage(bootstrap, monkeypatch):
    root, source, home, stage = bootstrap
    publish = codex_session._publish_home

    def interrupted(*args):
        raise OSError()

    monkeypatch.setattr(codex_session, "_publish_home", interrupted)
    seed = auth()
    with pytest.raises(SessionError):
        codex_session.bootstrap_home(root, source, "1", seed)
    rotated = dict(seed, rotation=2)
    monkeypatch.setattr(codex_session, "_publish_home", publish)
    codex_session.bootstrap_home(root, source, "1", rotated)
    manifest = codex_session.load_record(home / ".ccswap-session.json", "manifest")
    assert manifest["lineage"] == list(map(codex_session.auth_fingerprint, (seed, rotated)))


def test_evicted_ancestor_without_timestamp_is_conflict(tmp_path):
    seed = auth()
    manifest = codex_session._new_manifest(tmp_path / "home", tmp_path / "default", "1", seed)
    for number in range(65):
        current = dict(seed, rotation=number)
        manifest = codex_session.with_baseline(manifest, codex_session.auth_fingerprint(current))
    assert codex_session.default_write_decision(seed, current, current, manifest) == "conflict"
