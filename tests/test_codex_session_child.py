"""Child construction uses synthetic arguments, environments and temporary homes only."""

import json
from pathlib import Path

import pytest

from claude_swap import codex_session as session
from claude_swap.exceptions import SessionError


STORES = ['-c', 'cli_auth_credentials_store="file"',
          '-c', 'mcp_oauth_credentials_store="file"']


@pytest.mark.parametrize('value', ['', None, 'private-parent'])
def test_inherited_home(value):
    env = {} if value is None else {'CODEX_HOME': value}
    if value:
        with pytest.raises(SessionError, match='CODEX_HOME'):
            session.refuse_inherited_home(env)
    else:
        session.refuse_inherited_home(env)


@pytest.mark.parametrize('name', session._CHILD_DROP)
@pytest.mark.parametrize('present', [False, True])
def test_environment_names(tmp_path, name, present):
    env = {'ORDINARY': 'unchanged', **({name: 'synthetic-secret'} if present else {})}
    before = env.copy()
    result, warnings = session.child_env(env, tmp_path)
    assert env == before
    assert result == {'ORDINARY': 'unchanged', 'CODEX_HOME': str(tmp_path)}
    assert warnings == ([f'Removed {name}.'] if present else [])


def pinned():
    # Without --cd the invocation cwd is checked, so resume/fork pin exactly that one.
    return ['--no-daemon', '--cd', str(Path.cwd().resolve())]


@pytest.mark.parametrize('args,root', [
    ([], 'tui'), (['hello world'], 'tui'), (['resume', '--last'], 'pinned'),
    (['fork', 'thread', 'prompt'], 'pinned'), (['exec', 'prompt'], 'exec'),
    (['exec', 'resume', '--last', 'prompt'], 'exec'), (['e', 'resume', 'id'], 'exec'),
    (['-m', 'exec', 'resume'], 'pinned'),
])
def test_supported_order(tmp_path, args, root):
    original = args.copy()
    prefix = {'tui': ['--no-daemon'], 'exec': [], 'pinned': pinned()}[root]
    assert session.child_argv(args, tmp_path, config_layers=[]) == prefix + STORES + args
    assert args == original


@pytest.mark.parametrize('command', sorted(session._CHILD_COMMANDS))
def test_refused_commands(tmp_path, command):
    with pytest.raises(SessionError, match=command):
        session.child_argv(['--model=ordinary', command], tmp_path, config_layers=[])


@pytest.mark.parametrize('args', [
    ['exec', 'fork', 'id'], ['exec', 'review'], ['--profile', 'x'], ['-px'],
    ['--profile=x'], ['-p', 'x'], ['--oss'], ['--local-provider=ollama'],
    ['--remote', 'endpoint'], ['resume', '--remote=endpoint'],
    ['--remote-auth-token-env=X'], ['--unknown'], ['-c'], ['-c', 'missing-equals'],
    ['--model'], ['-c', '=value'],
])
def test_refused_options(tmp_path, args):
    with pytest.raises(SessionError):
        session.child_argv(args, tmp_path, config_layers=[])


@pytest.mark.parametrize('key', sorted(session._CHILD_FORBIDDEN))
@pytest.mark.parametrize('form', ['short', 'long', 'equal', 'attached'])
def test_protected_overrides(tmp_path, key, form):
    raw = key + '.nested={ child = "synthetic-secret" }'
    args = {'short': ['-c', raw], 'long': ['--config', raw],
            'equal': ['--config=' + raw], 'attached': ['-c' + raw]}[form]
    with pytest.raises(SessionError) as error:
        session.child_argv(['exec', 'resume', '--last'] + args, tmp_path, config_layers=[])
    assert key in str(error.value)
    assert 'synthetic-secret' not in str(error.value)


@pytest.mark.parametrize('raw', [
    'model_providers={ custom = { base_url = "synthetic-secret" } }',
    'cli_auth_credentials_store="file"', 'mcp_oauth_credentials_store=keyring',
])
def test_parent_table_and_store_conflicts(tmp_path, raw):
    with pytest.raises(SessionError):
        session.child_argv(['-c', raw], tmp_path, config_layers=[])


@pytest.mark.parametrize('raw,expected', [
    ('model="quoted=value"', ('model', 'quoted=value')),
    ('model=raw words', ('model', 'raw words')),
    ('model=\'broken"', ('model', 'broken')),
    ('other={ nested = true }', ('other', {'nested': True})),
    (' model = 42 ', ('model', 42)),
])
def test_codex_toml_fallback(raw, expected):
    assert session._child_override(raw) == expected


@pytest.mark.parametrize('args', [
    ['--', '--profile', 'login', '-c', 'model_provider=secret'],
    ['--model', 'login', '--sandbox=workspace-write', '-aon-request', '-C', 'exec',
     '--add-dir=resume', '--search', 'ordinary prompt'],
    ['--image', 'login.png', 'exec', 'resume', '--search'],
    ['exec', 'resume', '--image', 'image.png', '--', '--remote'],
])
def test_ordinary_arguments_unchanged(tmp_path, args):
    result = session.child_argv(args, tmp_path, config_layers=[])
    assert result[-len(args):] == args


@pytest.mark.parametrize('private', [False, True])
def test_sqlite_env(tmp_path, private):
    home = tmp_path / 'home'
    value = str(home / 'db' if private else tmp_path / 'outside')
    result, warnings = session.child_env({'CODEX_SQLITE_HOME': value}, home)
    assert result.get('CODEX_SQLITE_HOME') == (value if private else None)
    assert warnings == ([] if private else ['Removed CODEX_SQLITE_HOME.'])


@pytest.mark.parametrize('key', ['sqlite_home', 'log_dir'])
@pytest.mark.parametrize('managed', [False, True])
@pytest.mark.parametrize('private', [False, True])
def test_config_paths(tmp_path, key, managed, private):
    home = tmp_path / 'home'
    value = str(home / 'private' if private else tmp_path / 'escape')
    layers = [(tmp_path / 'config.toml', {key: value}, managed)]
    if managed and not private:
        with pytest.raises(SessionError, match=key):
            session.child_argv([], home, config_layers=layers)
        return
    result = session.child_argv([], home, config_layers=layers)
    default = home if key == 'sqlite_home' else home / 'log'
    expected = [] if private else ['-c', key + '=' + json.dumps(str(default.resolve()))]
    assert result == ['--no-daemon'] + STORES + expected


@pytest.mark.parametrize('key', ['sqlite_home', 'log_dir'])
def test_last_override_and_delimiter(tmp_path, key):
    home = tmp_path / 'home'
    private = f'{key}={json.dumps(str(home / "private"))}'
    escape = f'{key}={json.dumps(str(tmp_path / "escape"))}'
    args = ['exec', 'resume', '--last', '-c', escape, '--config=' + private, '--', 'prompt']
    assert session.child_argv(args, home, config_layers=[]) == STORES + args
    args = ['exec', '-c', private, '--config=' + escape, '--', 'prompt']
    result = session.child_argv(args, home, config_layers=[])
    default = home if key == 'sqlite_home' else home / 'log'
    expected = ['-c', key + '=' + json.dumps(str(default.resolve()))]
    assert result == STORES + args[:-2] + expected + args[-2:]


def test_relative_config_layer_and_precedence(tmp_path):
    home = tmp_path / 'home'
    layers = [(tmp_path / 'config.toml', {'sqlite_home': 'home/db'}, False),
              (home / 'config.toml', {'log_dir': 'logs'}, False)]
    assert session.child_argv([], home, config_layers=layers) == ['--no-daemon'] + STORES
    layers.append((tmp_path / 'config.toml', {'sqlite_home': 'outside'}, False))
    assert 'sqlite_home=' in session.child_argv([], home, config_layers=layers)[-1]


def test_symlink_escape(tmp_path):
    home = tmp_path / 'home'
    home.mkdir()
    outside = tmp_path / 'outside'
    outside.mkdir()
    (home / 'link').symlink_to(outside, target_is_directory=True)
    value = str(home / 'link' / 'missing' / 'db')
    result, warnings = session.child_env({'CODEX_SQLITE_HOME': value}, home)
    assert 'CODEX_SQLITE_HOME' not in result
    assert warnings == ['Removed CODEX_SQLITE_HOME.']
    layers = [(home / 'config.toml', {'sqlite_home': value}, False)]
    expected = 'sqlite_home=' + json.dumps(str(home.resolve()))
    assert session.child_argv([], home, config_layers=layers)[-1] == expected
    with pytest.raises(SessionError):
        session.child_argv([], home,
            config_layers=[(home / 'config.toml', {'log_dir': value}, True)])


@pytest.mark.parametrize('raw', ['sqlite_home={x=1}', 'log_dir=false', 'sqlite_home.child="x"'])
def test_invalid_path_override(tmp_path, raw):
    with pytest.raises(SessionError):
        session.child_argv(['-c', raw], tmp_path, config_layers=[])


def test_path_resolution_failure(tmp_path, monkeypatch):
    def denied(*args, **kwargs):
        raise PermissionError('synthetic-secret')
    monkeypatch.setattr(type(tmp_path), 'resolve', denied)
    with pytest.raises(SessionError, match='Cannot resolve') as error:
        session.child_env({'CODEX_SQLITE_HOME': str(tmp_path)}, tmp_path)
    assert 'synthetic-secret' not in str(error.value)


def test_relative_override_uses_child_cd(tmp_path):
    home = tmp_path / 'home'
    args = ['--cd', str(home), '-c', 'sqlite_home="db"', 'exec', 'prompt']
    assert session.child_argv(args, home, config_layers=[]) == STORES + args
    args = ['--cd', str(tmp_path), '-c', 'sqlite_home="db"', 'resume', '--last']
    with pytest.raises(SessionError, match='Relative child path'):
        session.child_argv(args, home, config_layers=[])


def test_relative_sqlite_env_pinned_before_cd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    home = tmp_path / 'home'
    result, warnings = session.child_env({'CODEX_SQLITE_HOME': ' home/db '}, home)
    assert result['CODEX_SQLITE_HOME'] == str((home / 'db').resolve())
    assert warnings == []


@pytest.mark.parametrize('value', ['', '   ', ' /outside '])
def test_empty_or_trimmed_sqlite_env(tmp_path, value):
    result, warnings = session.child_env({'CODEX_SQLITE_HOME': value}, tmp_path)
    assert 'CODEX_SQLITE_HOME' not in result
    assert warnings == ['Removed CODEX_SQLITE_HOME.']


@pytest.mark.parametrize('where', ['env', 'config', 'override', 'cd'])
def test_home_expansion_refused_without_home_lookup(tmp_path, where):
    with pytest.raises(SessionError, match='Home expansion'):
        if where == 'env':
            session.child_env({'CODEX_SQLITE_HOME': '~/db'}, tmp_path)
        else:
            args = {'config': [], 'override': ['-c', 'sqlite_home="~/db"'],
                    'cd': ['--cd=~/project']}[where]
            layers = [
                (tmp_path / 'config.toml', {'sqlite_home': '~/db'},
                    False)] if where == 'config' else []
            session.child_argv(args, tmp_path, config_layers=layers)


def test_default_log_symlink_escape_refused(tmp_path):
    home = tmp_path / 'home'
    home.mkdir()
    outside = tmp_path / 'outside'
    outside.mkdir()
    (home / 'log').symlink_to(outside, target_is_directory=True)
    with pytest.raises(SessionError, match='log_dir'):
        session.child_argv([], home, config_layers=[])


@pytest.mark.parametrize('args', [['prompt', 'login'], ['exec', 'prompt', 'review']])
def test_ambiguous_subcommand_refused(tmp_path, args):
    with pytest.raises(SessionError, match='Unexpected child positional'):
        session.child_argv(args, tmp_path, config_layers=[])


def test_managed_escape_not_hidden_by_runtime_override(tmp_path):
    layers = [(tmp_path / 'config.toml', {'sqlite_home': '/outside'}, True)]
    with pytest.raises(SessionError, match='Managed'):
        session.child_argv(['-c', 'sqlite_home=' + json.dumps(str(tmp_path))], tmp_path,
            config_layers=layers)


def test_symlink_loop_refused(tmp_path):
    (tmp_path / 'loop').symlink_to(tmp_path / 'loop')
    with pytest.raises(SessionError, match='Cannot resolve'):
        session.child_env({'CODEX_SQLITE_HOME': str(tmp_path / 'loop')}, tmp_path)


@pytest.mark.parametrize('args', [
    ['--image=a.png', 'login'], ['-ia.png', 'logout'],
    ['exec', '--image=a', 'review'], ['exec', '-ia', 'review'],
])
def test_attached_images_leave_subcommand_visible(tmp_path, args):
    with pytest.raises(SessionError, match=args[-1]):
        session.child_argv(args, tmp_path, config_layers=[])


def test_unused_default_log_escape_does_not_replace_private_config(tmp_path):
    home = tmp_path / 'home'
    home.mkdir()
    (home / 'log').symlink_to(tmp_path / 'outside', target_is_directory=True)
    layers = [(home / 'config.toml', {'log_dir': str(home / 'private-log')}, False)]
    assert session.child_argv([], home, config_layers=layers) == ['--no-daemon'] + STORES


@pytest.mark.parametrize('key', sorted(session._CHILD_FORBIDDEN))
def test_nonmanaged_config_routing_refused(tmp_path, key):
    source = tmp_path / 'shared' / 'config.toml'
    table = {key: 'synthetic-secret'}
    with pytest.raises(SessionError) as error:
        session.child_argv([], tmp_path, config_layers=[(source, table, False)])
    assert str(source) in str(error.value)
    assert key in str(error.value)
    assert 'synthetic-secret' not in str(error.value)


@pytest.mark.parametrize('key', ['cli_auth_credentials_store', 'mcp_oauth_credentials_store'])
@pytest.mark.parametrize('value', ['file', 'keyring', 'auto', 'ephemeral', None, True, {}])
def test_nonmanaged_credential_store_requires_file(tmp_path, key, value):
    source = tmp_path / 'shared' / 'config.toml'
    layers = [(source, {key: value}, False)]
    if value == 'file':
        assert session.child_argv(['exec', 'hello'], tmp_path, config_layers=layers) == (
            STORES + ['exec', 'hello'])
    else:
        with pytest.raises(SessionError, match=key):
            session.child_argv(['exec', 'hello'], tmp_path, config_layers=layers)


def test_managed_keys_are_left_to_policy_gate(tmp_path):
    layers = [(tmp_path / 'managed.toml', {'forced_login_method': 'chatgpt'}, True)]
    assert session.child_argv([], tmp_path, config_layers=layers) == ['--no-daemon'] + STORES


@pytest.mark.parametrize('args', [
    ['exec', '--ignore-user-config'], ['exec', 'resume', '--ignore-user-config'],
])
def test_ignore_user_config_refused_even_with_private_layer(tmp_path, args):
    home = tmp_path / 'home'
    home.mkdir()
    (home / 'log').symlink_to(tmp_path / 'outside', target_is_directory=True)
    layers = [(home / 'config.toml', {'log_dir': str(home / 'private')}, False)]
    with pytest.raises(SessionError, match='--ignore-user-config'):
        session.child_argv(args, home, config_layers=layers)


@pytest.mark.parametrize('form', [['resume'], ['fork'], ['exec', 'resume']])
@pytest.mark.parametrize('key', ['sqlite_home', 'log_dir'])
@pytest.mark.parametrize('value', ['private', '../outside'])
def test_recorded_cwd_relative_path_override_refused(tmp_path, form, key, value):
    with pytest.raises(SessionError, match='Relative child path'):
        session.child_argv(form + ['-c', key + '=' + json.dumps(value)],
                           tmp_path, config_layers=[])


@pytest.mark.parametrize('form', [['resume'], ['fork'], ['exec', 'resume']])
@pytest.mark.parametrize('key', ['sqlite_home', 'log_dir'])
def test_recorded_cwd_absolute_private_override_preserved(tmp_path, form, key):
    args = form + ['-c', key + '=' + json.dumps(str(tmp_path / 'private'))]
    assert session.child_argv(args, tmp_path, config_layers=[]) == (
        ([] if form[0] == 'exec' else pinned()) + STORES + args)


@pytest.mark.parametrize('key', sorted(session._CHILD_FORBIDDEN | {'sqlite_home', 'log_dir'}))
@pytest.mark.parametrize('value', [r'"/private/\x2e\x2e/outside"', 'unquoted/path'])
def test_sensitive_toml_parse_failure_refused(tmp_path, key, value):
    with pytest.raises(SessionError) as error:
        session.child_argv(['-c', key + '=' + value], tmp_path, config_layers=[])
    assert value not in str(error.value)


@pytest.mark.parametrize('word', ['daemon', 'service', 'remote', 'clear-memories'])
def test_nonexistent_command_names_remain_prompts(tmp_path, word):
    assert session.child_argv([word], tmp_path, config_layers=[]) == [
        '--no-daemon'] + STORES + [word]


@pytest.mark.parametrize('args', [['hi', 'exec'], ['exec', 'hi', 'resume'], ['hi', 'fork']])
def test_second_root_positional_is_not_interactive_launch(tmp_path, args):
    with pytest.raises(SessionError, match='Unexpected child positional'):
        session.child_argv(args, tmp_path, config_layers=[])


@pytest.mark.parametrize('args', [['help'], ['exec', 'help'], ['help', 'login']])
def test_help_subcommand_refused(tmp_path, args):
    with pytest.raises(SessionError, match='help'):
        session.child_argv(args, tmp_path, config_layers=[])


def test_space_images_still_consume_command_words(tmp_path):
    args = ['--image', 'a.png', 'login', 'exec', '--search']
    assert session.child_argv(args, tmp_path, config_layers=[]) == ['--no-daemon'] + STORES + args


@pytest.mark.parametrize('name', [
    'CODEX_REVOKE_TOKEN_URL_OVERRIDE', 'CODEX_AUTHAPI_BASE_URL',
    'CODEX_AGENT_IDENTITY_AUTHAPI_BASE_URL', 'CODEX_TUI_RECORD_SESSION',
    'CODEX_TUI_SESSION_LOG_PATH', 'CODEX_AGENT_IDENTITY_JWKS_BASE_URL',
    'CODEX_APP_SERVER_LOGIN_ISSUER', 'CODEX_ROLLOUT_TRACE_ROOT',
    'CODEX_CONNECTORS_TOKEN', 'CODEX_DAEMON_SHUTDOWN_FILE',
])
def test_additional_unsafe_env_removed(tmp_path, name):
    result, warnings = session.child_env({name: 'synthetic-secret'}, tmp_path)
    assert name not in result
    assert warnings == [f'Removed {name}.']


def test_token_endpoint_keys_are_protected():
    assert {
        'openai_base_url', 'chatgpt_base_url', 'model_provider', 'model_providers',
        'profile', 'profiles', 'experimental_realtime_webrtc_call_base_url',
        'experimental_realtime_ws_base_url',
    } <= session._CHILD_FORBIDDEN


@pytest.mark.parametrize('key', ['notify', 'otel', 'responses_api_metadata'])
def test_ordinary_user_settings_do_not_block(tmp_path, key):
    layer = (tmp_path / 'config.toml', {key: ['synthetic'], 'tui': {'profile': 'x'}}, False)
    assert session.child_argv([], tmp_path, config_layers=[layer]) == ['--no-daemon'] + STORES


def test_command_word_inside_single_prompt_is_unchanged(tmp_path):
    args = ['hi exec']
    assert session.child_argv(args, tmp_path, config_layers=[]) == ['--no-daemon'] + STORES + args


def test_child_env_explicit_spawn_cwd(tmp_path):
    from claude_swap.codex_session import child_env

    home = tmp_path / "home"
    home.mkdir()
    env, warnings = child_env({"CODEX_SQLITE_HOME": "home"}, home, cwd=tmp_path)
    assert env["CODEX_SQLITE_HOME"] == str(home.resolve())
    assert warnings == []


def test_child_argv_explicit_spawn_cwd(tmp_path):
    from claude_swap.codex_session import child_argv

    home = tmp_path / "home"
    home.mkdir()
    result = child_argv(["-c", 'sqlite_home="home"', "exec", "hi"], home,
                        config_layers=[], cwd=tmp_path)
    assert not any(item.startswith('sqlite_home="/') for item in result)


@pytest.mark.parametrize('form', [['resume', '--last'], ['fork', 'thread']])
@pytest.mark.parametrize('cd', [['--cd', 'x'], ['-C', 'x'], ['--cd=x'], ['-Cx']])
def test_explicit_cd_not_pinned_twice(tmp_path, form, cd):
    args = cd + form
    assert session.child_argv(args, tmp_path, config_layers=[]) == (
        ['--no-daemon'] + STORES + args)


@pytest.mark.parametrize('args,has_cd', [
    (['resume', '--last', '--cd', 'x'], True),
    (['resume', '--', '-C', 'x'], False),
])
def test_resume_cd_option_and_prompt_boundary(tmp_path, args, has_cd):
    assert session._child_has_cd(args) is has_cd
    prefix = ['--no-daemon'] if has_cd else pinned()
    assert session.child_argv(args, tmp_path, config_layers=[]) == prefix + STORES + args


@pytest.mark.parametrize('key', ['sqlite_home', 'log_dir'])
def test_nearest_private_layer_cannot_hide_outer_escape(tmp_path, key):
    # Codex ranks project layers root first and nearest last; any escaping layer
    # value must still be overridden, whichever layer this parser ranks highest.
    home = tmp_path / 'home'
    outer = (tmp_path / '.codex' / 'config.toml', {key: str(tmp_path / 'outside')}, False)
    inner = (tmp_path / 'p' / '.codex' / 'config.toml', {key: str(home / 'private')}, False)
    for layers in ([outer, inner], [inner, outer]):
        assert (key + '=') in session.child_argv([], home, config_layers=layers)[-1]
