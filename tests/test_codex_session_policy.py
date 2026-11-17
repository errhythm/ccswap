"""Local policy tests never read installed policy or account homes."""

import ctypes
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from ccswap import codex_session as session
from ccswap.exceptions import SessionError


@pytest.fixture
def gate(tmp_path, monkeypatch):
    home = tmp_path / "default"
    home.mkdir()
    cwd = tmp_path / "project"
    cwd.mkdir()
    monkeypatch.setattr(session, "_project_policy_paths", lambda cwd: [cwd / ".codex/config.toml"])
    identity = dict(userId="user", workspaceId="workspace", email="a@example.org",
        loginOwnerId=None)

    def check(plan="plus", **kwargs):
        return session.check_login_policy(home, cwd, identity, plan,
                                          system_paths=kwargs.pop("system_paths", []),
                                          windows_policy=kwargs.pop("windows_policy", lambda: None),
                                          **kwargs)

    return home, cwd, check


@pytest.mark.parametrize("text", [
    '', 'other = "synthetic-secret"', 'forced_login_method = "chatgpt"',
    'forced_chatgpt_workspace_id = " workspace "',
    'forced_chatgpt_workspace_id = ["", "  "]', 'forced_chatgpt_workspace_id = []',
    'forced_chatgpt_workspace_id = ["other", " workspace "]',
    'allowed_login_methods = ["api", "chatgpt"]',
    'allowed_chatgpt_workspaces = ["", " workspace "]',
    'forced_chatgpt_workspace_id = "workspace"\nallowed_chatgpt_workspaces = ["workspace"]',
    '[profiles.unselected.deep]\nforced_login_method = "chatgpt"',
])
def test_compatible(gate, text):
    home, _, check = gate
    (home / "config.toml").write_text(text)
    assert check() is None


@pytest.mark.parametrize("key,value", [
    ("forced_login_method", '"api"'), ("forced_login_method", '"unknown"'),
    ("forced_login_method", '[]'), ("forced_login_method", 'true'),
    ("forced_chatgpt_workspace_id", '"workspace,other"'),
    ("forced_chatgpt_workspace_id", '["other"]'),
    ("forced_chatgpt_workspace_id", '["workspace", 1]'),
    ("forced_chatgpt_workspace_id", '{}'),
    ("allowed_login_methods", '[]'), ("allowed_login_methods", '["api"]'),
    ("allowed_login_methods", '"chatgpt"'),
    ("allowed_login_methods", '["chatgpt", "unknown"]'),
    ("allowed_chatgpt_workspaces", '[]'), ("allowed_chatgpt_workspaces", '[" "]'),
    ("allowed_chatgpt_workspaces", '"workspace"'),
    ("allowed_chatgpt_workspaces", '["other"]'),
    ("allowed_chatgpt_workspaces", '["workspace", false]'),
])
@pytest.mark.parametrize("prefix", ['', '[profiles.unselected.deep]\n', '[[layers]]\n'])
def test_incompatible_anywhere(gate, key, value, prefix):
    home, _, check = gate
    source = home / "config.toml"
    source.write_text(prefix + key + ' = ' + value + '\nsecret = "synthetic-secret"')
    with pytest.raises(SessionError) as error:
        check()
    assert str(source) in str(error.value) and key in str(error.value)
    assert "synthetic-secret" not in str(error.value)
    assert value not in str(error.value)


@pytest.mark.parametrize("plan", [None, "unknown", "business", "enterprise", "edu", "sci_plus"])
def test_plan_refuses_before_discovery(gate, plan, monkeypatch):
    _, _, check = gate
    monkeypatch.setattr(session, "_project_policy_paths", lambda _: pytest.fail("discovery"))
    with pytest.raises(SessionError, match=r"^Session mode does not support workspace-managed "
                       r"\(business/enterprise/education\) accounts yet\.$"):
        check(plan)


@pytest.mark.parametrize("source", ["config.toml", "requirements.toml", "managed_config.toml",
                                    "OpenAI/Codex/config.toml", "OpenAI/Codex/requirements.toml",
                                    "registry-requirements.toml", ".codex/config.toml"])
def test_each_source(gate, tmp_path, source):
    _, cwd, check = gate
    path = (cwd if source.startswith(".codex") else tmp_path / "system") / source
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('forced_login_method = "api"')
    options = {"system_paths": [path]}
    if source.startswith("registry"):
        options = {"windows_policy": lambda: path}
    with pytest.raises(SessionError, match="forced_login_method") as error:
        check(**options)
    assert str(path) in str(error.value)


@pytest.mark.parametrize("failure", ["parse", "utf8", "directory", "denied", "dangling", "deep"])
def test_source_failures(gate, tmp_path, monkeypatch, failure):
    _, _, check = gate
    path = tmp_path / "policy.toml"
    if failure == "directory":
        path.mkdir()
    elif failure == "dangling":
        path.symlink_to(tmp_path / "absent")
    else:
        path.write_bytes(b'key = "synthetic-secret"\ninvalid' if failure == "parse" else b'\xff')
    if failure in ("denied", "deep"):
        original = Path.open

        def fail(selected, *args, **kwargs):
            if selected == path:
                raise PermissionError(
                    "synthetic-secret") if failure == "denied" else RecursionError()
            return original(selected, *args, **kwargs)

        monkeypatch.setattr(Path, "open", fail)
    with pytest.raises(SessionError) as error:
        check(system_paths=[path])
    assert str(path) in str(error.value) and "/source" in str(error.value)
    assert "synthetic-secret" not in str(error.value)


@pytest.mark.parametrize("user", [False, True])
def test_managed_plist_existence(gate, tmp_path, user):
    _, _, check = gate
    path = tmp_path / ("user" if user else "managed") / "com.openai.codex.plist"
    path.parent.mkdir()
    assert check(system_paths=[path]) is None
    path.write_text("synthetic-secret")
    with pytest.raises(SessionError, match="managed Codex preferences are not supported yet"):
        check(system_paths=[path])


def test_intersection_and_conservative_layers(gate, tmp_path):
    home, _, check = gate
    (home / "config.toml").write_text('forced_chatgpt_workspace_id = ["workspace", "other"]')
    policy = tmp_path / "requirements.toml"
    policy.write_text('allowed_chatgpt_workspaces = ["other"]')
    with pytest.raises(SessionError, match="allowed_chatgpt_workspaces"):
        check(system_paths=[policy])
    policy.write_text('allowed_chatgpt_workspaces = ["workspace"]')
    assert check(system_paths=[policy]) is None
    (home / "config.toml").write_text('forced_login_method = "api"')
    policy.write_text('forced_login_method = "chatgpt"')
    with pytest.raises(SessionError, match="forced_login_method"):
        check(system_paths=[policy])


def test_project_discovery(tmp_path):
    cwd = tmp_path / "a/b"
    cwd.mkdir(parents=True)
    assert session._project_policy_paths(cwd) == [
        parent / ".codex/config.toml" for parent in (cwd, *cwd.parents)]
    alias = tmp_path / "alias"
    alias.symlink_to(cwd, target_is_directory=True)
    assert session._project_policy_paths(alias) == session._project_policy_paths(cwd)
    with pytest.raises(SessionError, match="project discovery"):
        session._project_policy_paths(tmp_path / "missing")


def test_system_discovery(tmp_path, monkeypatch):
    monkeypatch.setattr(session.sys, "platform", "linux")
    assert session._system_policy_paths() == [Path('/etc/codex') / name for name in
                                             ('config.toml', 'requirements.toml',
                                                 'managed_config.toml')]
    monkeypatch.setattr(session.sys, "platform", "win32")
    monkeypatch.setattr(session, "_program_data", lambda: tmp_path)
    assert session._system_policy_paths() == [tmp_path / 'OpenAI/Codex' / name for name in
                                             ('config.toml', 'requirements.toml')]
    monkeypatch.setattr(session.sys, "platform", "darwin")
    monkeypatch.setattr(session.os, "getuid", lambda: 501, raising=False)
    monkeypatch.setitem(sys.modules, "pwd", SimpleNamespace(
        getpwuid=lambda _: SimpleNamespace(pw_name="test-user")))
    assert session._system_policy_paths()[-2:] == [
        Path('/Library/Managed Preferences/com.openai.codex.plist'),
        Path('/Library/Managed Preferences/test-user/com.openai.codex.plist')]


@pytest.mark.parametrize("mode", ["present", "missing", "denied", "relative", "wrong-type"])
def test_registry_reader(tmp_path, monkeypatch, mode):
    calls = []
    class Key:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
    def open_key(*args):
        calls.append(args)
        if mode == "missing":
            raise FileNotFoundError()
        if mode == "denied":
            raise PermissionError("synthetic-secret")
        return Key()
    registry = SimpleNamespace(HKEY_LOCAL_MACHINE=1, KEY_READ=2, KEY_WOW64_64KEY=4,
        REG_SZ=1, OpenKey=open_key, QueryValueEx=lambda *args: (
            'relative' if mode == 'relative' else str(tmp_path / 'requirements.toml'),
            2 if mode == 'wrong-type' else 1))
    monkeypatch.setitem(sys.modules, 'winreg', registry)
    if mode in ('denied', 'relative', 'wrong-type'):
        with pytest.raises(SessionError, match='RequirementsPath') as error:
            session._windows_requirements_path()
        assert 'synthetic-secret' not in str(error.value)
    else:
        assert session._windows_requirements_path() == (
            None if mode == 'missing' else tmp_path / 'requirements.toml')
    assert calls[0][-1] == 6


@pytest.mark.parametrize("failure", [PermissionError, ValueError])
def test_injected_registry_failure(gate, failure):
    def fail():
        raise failure('synthetic-secret')
    with pytest.raises(SessionError, match='RequirementsPath') as error:
        gate[2](windows_policy=fail)
    assert 'synthetic-secret' not in str(error.value)


@pytest.mark.parametrize("result", [0, -1, "null", "unavailable"])
def test_known_folder(tmp_path, monkeypatch, result):
    freed = []
    buffer = ctypes.create_unicode_buffer(str(tmp_path))
    class Function:
        def __init__(self, call):
            self.call = call
        def __call__(self, *args):
            return self.call(*args)
    def query(guid, flags, token, pointer):
        assert bytes(guid._obj) == session.UUID('62ab5d82-fdc1-4dc3-a9dd-070d1d495d97').bytes_le
        if result != 'null':
            pointer._obj.value = ctypes.addressof(buffer)
        return -1 if result == -1 else 0
    def library(name):
        if result == 'unavailable':
            raise OSError()
        return SimpleNamespace(SHGetKnownFolderPath=Function(query),
                               CoTaskMemFree=Function(lambda p: freed.append(p.value)))
    monkeypatch.setattr(ctypes, 'WinDLL', library, raising=False)
    assert session._program_data() == (tmp_path if result == 0 else Path(r'C:\ProgramData'))
    assert bool(freed) == (result in (0, -1))


@pytest.mark.parametrize('kind', ['stat-denied', 'lookup-denied', 'missing'])
def test_source_lookup(gate, tmp_path, monkeypatch, kind):
    path = tmp_path / 'policy.toml'
    original_stat, original_lstat = Path.stat, Path.lstat

    def selected_stat(selected, *args, **kwargs):
        if selected == path and kind == 'stat-denied':
            raise PermissionError('synthetic-secret')
        return original_stat(selected, *args, **kwargs)

    def selected_lstat(selected, *args, **kwargs):
        if selected == path and kind == 'lookup-denied':
            raise PermissionError('synthetic-secret')
        return original_lstat(selected, *args, **kwargs)

    monkeypatch.setattr(Path, 'stat', selected_stat)
    monkeypatch.setattr(Path, 'lstat', selected_lstat)
    if kind == 'missing':
        assert gate[2](system_paths=[path]) is None
    else:
        with pytest.raises(SessionError, match='/source') as error:
            gate[2](system_paths=[path])
        assert 'synthetic-secret' not in str(error.value)


@pytest.mark.parametrize('path', ['relative', 1])
def test_invalid_registry_path(gate, path):
    with pytest.raises(SessionError, match='RequirementsPath'):
        gate[2](windows_policy=lambda: path)


@pytest.mark.parametrize('source', [
    'default', 'system-config', 'system-requirements', 'legacy-managed',
    'windows-config', 'windows-requirements', 'registry', 'project', 'managed-plist',
])
@pytest.mark.parametrize('setting', [
    'config_server = "synthetic-secret"',
    'config_server = false',
    'config_server = {}',
    '[config_server]\nbase_url = "https://synthetic-secret.invalid"',
    '[profiles.unselected.config_server]\nbase_url = "https://synthetic-secret.invalid"',
    '[[layers]]\nconfig_server = {}',
])
def test_unresolved_config_server(gate, tmp_path, source, setting):
    home, cwd, check = gate
    paths = {
        'default': home / 'config.toml',
        'project': cwd / '.codex/config.toml',
        'system-config': tmp_path / 'etc/config.toml',
        'system-requirements': tmp_path / 'etc/requirements.toml',
        'legacy-managed': tmp_path / 'etc/managed_config.toml',
        'windows-config': tmp_path / 'ProgramData/OpenAI/Codex/config.toml',
        'windows-requirements': tmp_path / 'ProgramData/OpenAI/Codex/requirements.toml',
        'registry': tmp_path / 'registry/requirements.toml',
        'managed-plist': tmp_path / 'managed/com.openai.codex.plist',
    }
    path = paths[source]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(setting)
    options = {'windows_policy': lambda: path} if source == 'registry' else {'system_paths': [path]}
    with pytest.raises(SessionError) as error:
        check(**options)
    message = str(error.value)
    assert str(path) in message and 'synthetic-secret' not in message
    assert ('managed Codex preferences' if source == 'managed-plist'
            else 'config_server (unresolved managed source)') in message


@pytest.mark.parametrize('key', ['profile', 'experimental_thread_store_endpoint'])
@pytest.mark.parametrize('prefix', ['', '[profiles.unselected.deep]\n', '[[layers]]\n'])
def test_rejected_legacy_startup_keys(gate, key, prefix):
    home, _, check = gate
    path = home / 'config.toml'
    path.write_text(prefix + key + ' = "synthetic-secret"')
    with pytest.raises(SessionError) as error:
        check()
    assert str(path) in str(error.value) and key in str(error.value)
    assert 'synthetic-secret' not in str(error.value)
