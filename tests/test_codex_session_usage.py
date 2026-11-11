"""Session usage authority with synthetic homes and credentials only."""
import copy

import pytest

from claude_swap import codex as module, codex_session as cs
from claude_swap.codex_usage import CodexUsageError
from claude_swap.locking import FileLock
from tests.test_codex_reconcile import custody, write


@pytest.fixture
def usage(custody, monkeypatch):
    switcher, home, baseline = custody
    switcher.provider_dir = switcher.backup_dir / 'codex'
    switcher.lock_file = switcher.backup_dir / '.lock'
    switcher.auth_file = switcher.codex_home / 'auth.json'
    switcher._usage_cache.clear()
    monkeypatch.setattr(switcher, 'current_account_number', lambda: None)
    monkeypatch.setattr(cs, 'slot_custody', lambda *_: cs.Custody('live', 'writers_present'))
    return switcher, home, baseline


@pytest.mark.parametrize('entrypoint', ['accounts_snapshot', 'usage_status'])
@pytest.mark.parametrize('unauthorized', [False, True])
def test_live_rotated_read_only(usage, monkeypatch, entrypoint, unauthorized):
    switcher, home, baseline = usage
    rotated = copy.deepcopy(baseline)
    rotated['tokens']['access_token'] = 'synthetic-rotated'
    write(home / 'auth.json', rotated)
    calls = []

    def fetch(auth, **kwargs):
        calls.append(auth)
        if unauthorized:
            raise CodexUsageError('unauthorized', status_code=401)
        return {'five_hour': {'pct': 10}}

    monkeypatch.setattr(module, 'fetch_codex_usage', fetch)
    monkeypatch.setattr(switcher, '_refresh_inactive_auth', lambda *_: pytest.fail('refresh'))
    result = getattr(switcher, entrypoint)()
    entry = switcher._usage_cache['1']
    assert calls == [rotated]
    assert bool(entry.last_error) == unauthorized
    if entrypoint == 'accounts_snapshot':
        assert not result.accounts[0].switchable


@pytest.mark.parametrize('state', ['uncertain', 'conflict'])
def test_unavailable_never_uses_backup(usage, monkeypatch, state):
    switcher, _, _ = usage
    monkeypatch.setattr(cs, 'slot_custody', lambda *_: cs.Custody(state, 'fixed_reason'))
    monkeypatch.setattr(module, 'fetch_codex_usage', lambda *_args, **_kw: pytest.fail('fetch'))
    snapshot = switcher.accounts_snapshot()
    assert snapshot.accounts[0].usage.last_error == ({
        'uncertain': 'Codex session liveness is uncertain',
        'conflict': 'Codex session credentials are unavailable',
    }[state])
    assert not snapshot.accounts[0].switchable


@pytest.mark.parametrize('failures', [2, 3])
def test_bounded_strict_read(usage, monkeypatch, failures):
    switcher, home, _ = usage
    original = cs.load_document
    calls = []

    def read(path):
        if path == home / 'auth.json':
            calls.append(path)
            if len(calls) <= failures:
                from claude_swap.exceptions import SessionError
                raise SessionError('Malformed session record.')
        return original(path)

    monkeypatch.setattr(cs, 'load_document', read)
    with FileLock(switcher.lock_file):
        effective = switcher._effective_account_auth('1', allow_refresh=True)
    assert len(calls) == 3
    assert (effective['auth'] is None) == (failures == 3)


def test_authority_change_invalidates_cache(usage, monkeypatch):
    switcher, home, baseline = usage
    seen = []
    monkeypatch.setattr(module, 'fetch_codex_usage', lambda auth, **_: seen.append(auth) or {})
    switcher.accounts_snapshot()
    rotated = dict(baseline, rotation=1)
    write(home / 'auth.json', rotated)
    switcher.accounts_snapshot()
    assert seen == [baseline, rotated]


def test_idle_adopts_before_refresh(usage, monkeypatch):
    switcher, home, baseline = usage
    rotated = dict(baseline, rotation=1)
    write(home / 'auth.json', rotated)
    monkeypatch.setattr(cs, 'slot_custody', lambda *_: cs.Custody('quiescent', 'no_writers'))
    calls = []

    def fetch(auth, **_):
        calls.append(auth)
        if len(calls) == 1:
            raise CodexUsageError('unauthorized', status_code=401)
        return {}

    def refresh(number, expected):
        assert expected == rotated == cs.load_document(switcher._credential_path(number))
        return dict(expected, refreshed=True)

    monkeypatch.setattr(module, 'fetch_codex_usage', fetch)
    monkeypatch.setattr(switcher, '_refresh_inactive_auth', refresh)
    switcher.accounts_snapshot()
    assert len(calls) == 2 and calls[1]['refreshed']


def test_display_read_only_schema(usage, monkeypatch):
    switcher, home, baseline = usage
    write(home / 'auth.json', dict(baseline, rotation=1))
    before = switcher._credential_path('1').read_bytes()
    monkeypatch.setattr(cs, 'slot_custody', lambda *_: cs.Custody('quiescent', 'no_writers'))
    row = switcher.list_payload()['accounts'][0]
    assert list(row) == ['number', 'email', 'authMode', 'planType', 'label', 'active',
                         'disabled', 'session', 'usageStatus', 'usage']
    assert row['session'] == {'state': 'idle', 'capturePending': True,
                              'runCount': 0, 'shared': False}
    assert switcher.status()['sessions'] == [{'number': 1, 'session': row['session']}]
    assert switcher._credential_path('1').read_bytes() == before


def test_copied_routing_never_receives_session_token(usage, monkeypatch):
    switcher, home, _ = usage
    (home / 'config.toml').write_text('chatgpt_base_url = "https://invalid.test"')
    monkeypatch.setattr(module, 'fetch_codex_usage', lambda *_args, **_kw: pytest.fail('fetch'))
    assert switcher.accounts_snapshot().accounts[0].usage.last_error


def test_live_divergence_is_unavailable(usage, monkeypatch):
    switcher, home, baseline = usage
    write(home / 'auth.json', dict(baseline, rotation=1))
    write(switcher._credential_path('1'), dict(baseline, rotation=2))
    monkeypatch.setattr(module, 'fetch_codex_usage', lambda *_args, **_kw: pytest.fail('fetch'))
    assert switcher.accounts_snapshot().accounts[0].usage.last_error


def test_default_remains_active_and_metadata_can_change_live(usage, monkeypatch):
    switcher, _, baseline = usage
    write(switcher.auth_file, baseline)
    monkeypatch.setattr(switcher, 'current_account_number', lambda: '1')
    switcher.set_account_disabled('1', True)
    row = switcher.list_payload()['accounts'][0]
    assert row['active'] and row['disabled']
    assert row['session']['state'] == 'live'
    assert row['session']['capturePending']


def test_usage_entry_resolves_instead_of_trusting_caller(usage, monkeypatch):
    switcher, home, baseline = usage
    rotated = dict(baseline, rotation=1)
    write(home / 'auth.json', rotated)
    seen = []
    monkeypatch.setattr(module, 'fetch_codex_usage', lambda auth, **_: seen.append(auth) or {})
    switcher._usage_entry('1', baseline, True, 100, is_active=False)
    assert seen == [rotated]


def test_missing_live_auth_is_unavailable(usage, monkeypatch):
    switcher, home, _ = usage
    (home / 'auth.json').unlink()
    monkeypatch.setattr(module, 'fetch_codex_usage', lambda *_args, **_kw: pytest.fail('fetch'))
    assert switcher.accounts_snapshot().accounts[0].usage.last_error


@pytest.mark.parametrize('state', ['none', 'quiescent'])
@pytest.mark.parametrize('entrypoint', ['accounts_snapshot', 'usage_status'])
def test_actual_default_rotation_is_authority(usage, monkeypatch, state, entrypoint):
    switcher, _, baseline = usage
    rotated = copy.deepcopy(baseline)
    rotated['tokens']['access_token'] = 'synthetic-default-rotation'
    write(switcher.auth_file, rotated)
    monkeypatch.setattr(cs, 'slot_custody', lambda *_: cs.Custody(state, 'no_writers'))
    seen = []

    def fetch(auth, **_):
        seen.append(auth)
        raise CodexUsageError('unauthorized', status_code=401)

    monkeypatch.setattr(module, 'fetch_codex_usage', fetch)
    monkeypatch.setattr(module, 'refresh_codex_auth', lambda *_: pytest.fail('refresh'))
    getattr(switcher, entrypoint)()
    assert seen == [rotated]
    assert switcher._usage_cache['1'].last_error == 'unauthorized'


@pytest.mark.parametrize('failure', ['parse', 'permission'])
@pytest.mark.parametrize('state', ['none', 'quiescent'])
def test_unknown_default_never_authorizes_refresh(usage, monkeypatch, failure, state):
    from pathlib import Path

    switcher, _, _ = usage
    original = Path.read_text
    reads = []

    def read(path, *args, **kwargs):
        if path == switcher.auth_file:
            reads.append(path)
            if failure == 'permission':
                raise PermissionError('synthetic denied')
            return '{"tokens":'
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'read_text', read)
    monkeypatch.setattr(cs, 'slot_custody', lambda *_: cs.Custody(state, 'no_writers'))
    monkeypatch.setattr(module, 'fetch_codex_usage', lambda *_a, **_k: {'five_hour': {'pct': 10}})
    monkeypatch.setattr(module, 'refresh_codex_auth', lambda *_: pytest.fail('refresh'))
    snapshot = switcher.accounts_snapshot()
    assert reads
    assert snapshot.accounts[0].usage.last_good == {'five_hour': {'pct': 10}}
    assert snapshot.accounts[0].switchable
    with FileLock(switcher.lock_file):
        assert not switcher._effective_account_auth('1', allow_refresh=True)['refresh']


def test_session_start_before_401_blocks_real_refresh(custody, monkeypatch):
    from types import SimpleNamespace

    # Exercise the POSIX scanner race; Windows job custody has separate tests.
    monkeypatch.setattr(cs, "sys", SimpleNamespace(platform="linux"))
    from claude_swap import process_detection

    switcher, _, _ = custody
    switcher.lock_file = switcher.backup_dir / '.lock'
    switcher.auth_file = switcher.codex_home / 'auth.json'
    switcher._usage_cache.clear()
    state = ['quiescent']
    scans = []

    def scan(*_):
        scans.append(state[0])
        return {'state': state[0], 'pids': [], 'issues': []}

    def fetch(*_, **__):
        state[0] = 'live'
        raise CodexUsageError('unauthorized', status_code=401)

    monkeypatch.setattr(process_detection, 'scan_codex_home', scan)
    monkeypatch.setattr(module, 'fetch_codex_usage', fetch)
    monkeypatch.setattr(module, 'refresh_codex_auth', lambda *_: pytest.fail('refresh'))
    snapshot = switcher.accounts_snapshot()
    assert scans[0] == 'quiescent' and scans[-1] == 'live'
    assert snapshot.accounts[0].usage.last_error == 'unauthorized'


@pytest.mark.parametrize('entrypoint', ['status', 'list_payload', 'list_accounts',
                                        'accounts_snapshot', 'usage_status'])
def test_broken_config_is_per_account_unavailable(usage, monkeypatch, entrypoint):
    switcher, _, baseline = usage
    write(switcher.auth_file, baseline)
    (switcher.codex_home / 'config.toml').write_text('broken = [')
    monkeypatch.setattr(switcher, 'current_account_number', lambda: '1')
    monkeypatch.setattr(cs, 'slot_custody', lambda *_: cs.Custody('none', 'no_session_home'))
    getattr(switcher, entrypoint)()
    with FileLock(switcher.lock_file):
        effective = switcher._effective_account_auth('1', allow_refresh=True)
    assert effective['auth'] is None and not effective['refresh']
    assert effective['reason'] == 'Codex configuration is unreadable or invalid'


@pytest.mark.parametrize('process_state,expected', [('alive', 'live'), ('unknown', 'uncertain')])
def test_windows_status_uses_job_process_state(usage, monkeypatch, process_state, expected):
    from types import SimpleNamespace
    from claude_swap import process_detection, windows_job
    from tests.test_codex_session import record

    switcher, home, _ = usage
    run = record('run', home.parent)
    run.update(home=str(home), shared=True)
    runs = home / 'runs'
    runs.mkdir()
    write(runs / (run['runId'] + '.json'), run)
    monkeypatch.setattr(module, 'sys', SimpleNamespace(platform='win32'))
    calls = []
    monkeypatch.setattr(windows_job, 'process_state',
                        lambda pid, start: calls.append((pid, start)) or process_state)
    monkeypatch.setattr(process_detection, 'codex_process_state', lambda *_: pytest.fail('POSIX'))
    status = switcher._session_status('1')
    assert calls == [(run['supervisor']['pid'], run['supervisor']['start'])]
    assert status['state'] == expected
    assert status['runCount'] == (process_state == 'alive')
    assert status['shared'] == (process_state == 'alive')


@pytest.mark.parametrize('state,expected', [('live', 'live'), ('uncertain', 'uncertain'),
                                          ('quiescent', 'stale')])
def test_intent_never_downgrades_custody(usage, monkeypatch, state, expected):
    from tests.test_codex_session import record

    switcher, home, _ = usage
    intent = record('stale', home.parent)
    intent['home'] = str(home)
    write(home.parent / f'.{home.name}.stale.json', intent)
    monkeypatch.setattr(cs, 'slot_custody', lambda *_: cs.Custody(state, 'intent', intent=True))
    status = switcher._session_status('1')
    assert status['state'] == expected and status['capturePending']


@pytest.mark.parametrize('failures', [2, 3])
def test_display_partial_auth_retries_then_uncertain(usage, monkeypatch, failures):
    switcher, home, _ = usage
    original = cs.load_document
    calls = []

    def read(path):
        if path == home / 'auth.json':
            calls.append(path)
            if len(calls) <= failures:
                from claude_swap.exceptions import SessionError
                raise SessionError('Malformed session record.')
        return original(path)

    monkeypatch.setattr(cs, 'load_document', read)
    status = switcher._session_status('1')
    assert len(calls) == 3
    assert status['state'] == ('live' if failures == 2 else 'uncertain')


def test_display_validated_divergence_is_conflict(usage):
    switcher, home, baseline = usage
    write(home / 'auth.json', dict(baseline, rotation=1))
    write(switcher._credential_path('1'), dict(baseline, rotation=2))
    assert switcher._session_status('1')['state'] == 'conflict'


def test_interrupted_adoption_is_capture_pending(usage, monkeypatch):
    switcher, home, baseline = usage
    rotated = dict(baseline, rotation=1)
    write(home / 'auth.json', rotated)
    write(switcher._credential_path('1'), rotated)
    monkeypatch.setattr(cs, 'slot_custody', lambda *_: cs.Custody('quiescent', 'no_writers'))
    before = (home / '.ccswap-session.json').read_bytes()
    assert switcher._session_status('1') == {
        'state': 'idle', 'capturePending': True, 'runCount': 0, 'shared': False}
    assert (home / '.ccswap-session.json').read_bytes() == before


@pytest.mark.parametrize('entrypoint', ['status', 'list_payload', 'list_accounts'])
def test_display_lock_busy_is_uncertain(usage, monkeypatch, entrypoint):
    from claude_swap.exceptions import LockError

    switcher, _, baseline = usage
    write(switcher.auth_file, baseline)
    monkeypatch.setattr(switcher, 'current_account_number', lambda: '1')
    timeouts = []

    class BusyLock:
        def __init__(self, path, timeout):
            timeouts.append(timeout)

        def __enter__(self):
            raise LockError('busy')

        def __exit__(self, *_):
            pass

    monkeypatch.setattr(module, 'FileLock', BusyLock)
    payload = getattr(switcher, entrypoint)()
    rows = payload.get('accounts', payload.get('sessions'))
    assert rows[0]['session']['state'] == 'uncertain'
    assert rows[0]['session']['capturePending']
    assert timeouts and all(timeout == 2 for timeout in timeouts)


def test_missing_backup_keeps_human_message(usage, monkeypatch):
    switcher, _, _ = usage
    monkeypatch.setattr(cs, 'slot_custody', lambda *_: cs.Custody('none', 'no_session_home'))
    switcher._credential_path('1').unlink()
    assert switcher.accounts_snapshot().accounts[0].usage.last_error == (
        'stored Codex auth.json is missing')


@pytest.mark.parametrize('failure', ['parse', 'permission'])
def test_display_real_read_failures_are_uncertain(usage, monkeypatch, failure):
    from pathlib import Path

    switcher, home, _ = usage
    if failure == 'parse':
        (home / 'auth.json').write_text('{"tokens":')
    original = Path.lstat
    reads = []

    def lstat(path, *args, **kwargs):
        if path == home / 'auth.json':
            reads.append(path)
            if failure == 'permission':
                raise PermissionError('synthetic denied')
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'lstat', lstat)
    status = switcher._session_status('1')
    # lstat calls per read differ between Python versions; the outcome must not.
    assert reads
    assert status['state'] == 'uncertain' and status['capturePending']


def test_snapshot_reports_busy_store_instead_of_failing(usage, monkeypatch):
    from claude_swap import codex
    from claude_swap.exceptions import LockError

    switcher, _, _ = usage

    class Busy:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            raise LockError("synthetic busy")

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(codex, "FileLock", Busy)
    snapshot = switcher.accounts_snapshot(fetch=None)
    assert all(not account.switchable for account in snapshot.accounts)


def test_no_sessions_observation_never_takes_store_lock(usage, monkeypatch):
    switcher, home, _ = usage
    # Move the synthetic home aside; no session directory has ever been used here.
    home.parent.rename(home.parent.with_name('unused-test-homes'))
    monkeypatch.setattr(module, 'FileLock', lambda *_a, **_k: pytest.fail('store lock'))
    monkeypatch.setattr(module, 'fetch_codex_usage', lambda *_a, **_k: {})
    monkeypatch.setattr(cs, 'slot_custody', lambda *_a, **_k: pytest.fail('custody scan'))
    assert switcher._session_status('1') == {
        'state': 'absent', 'capturePending': False, 'runCount': 0, 'shared': False}
    assert switcher._account_plan('1', {'planType': 'plus'}, None) == 'plus'
    assert switcher.accounts_snapshot().accounts[0].switchable


@pytest.mark.parametrize('kind', ['symlink', 'file', 'permission'])
def test_session_absence_requires_valid_readable_parents(usage, monkeypatch, kind):
    switcher, home, _ = usage
    sessions = home.parent
    sessions.rename(sessions.with_name('retained-test-homes'))
    if kind == 'symlink':
        try:
            sessions.symlink_to(sessions.with_name('retained-test-homes'), target_is_directory=True)
        except OSError:
            pytest.skip('directory symlinks unavailable')
    elif kind == 'file':
        sessions.write_text('malformed')
    else:
        from pathlib import Path
        original = Path.lstat
        def denied(path, *args, **kwargs):
            if path == sessions:
                raise PermissionError('synthetic')
            return original(path, *args, **kwargs)
        monkeypatch.setattr(Path, 'lstat', denied)
    assert not cs.sessions_absent(switcher)


@pytest.mark.parametrize('unauthorized', [False, True])
def test_unknown_default_idle_rotation_stays_read_only(usage, monkeypatch, unauthorized):
    from claude_swap.exceptions import ConfigError

    switcher, home, baseline = usage
    rotated = dict(baseline, rotation=1)
    write(home / 'auth.json', rotated)
    monkeypatch.setattr(cs, 'slot_custody', lambda *_: cs.Custody('quiescent', 'no_writers'))
    def unreadable():
        raise ConfigError('synthetic default unreadable')
    monkeypatch.setattr(switcher, '_read_switch_auth', unreadable)
    monkeypatch.setattr(switcher, '_guard_session_slot', lambda *_: pytest.fail('reconcile'))
    monkeypatch.setattr(switcher, '_refresh_inactive_auth', lambda *_: pytest.fail('refresh'))
    seen = []
    def fetch(auth, **_):
        seen.append(auth)
        if unauthorized:
            raise CodexUsageError('unauthorized', status_code=401)
        return {}
    monkeypatch.setattr(module, 'fetch_codex_usage', fetch)
    before = switcher._credential_path('1').read_bytes()
    snapshot = switcher.accounts_snapshot()
    assert seen == [rotated]
    assert bool(snapshot.accounts[0].usage.last_error) == unauthorized
    assert switcher._credential_path('1').read_bytes() == before


@pytest.mark.parametrize('state', ['live', 'uncertain', 'conflict'])
def test_redeem_session_preflight_401_never_rotates_or_posts(usage, monkeypatch, state):
    from claude_swap.exceptions import SwitchError

    switcher, _, _ = usage
    monkeypatch.setattr(cs, 'slot_custody', lambda *_a, **_k: cs.Custody(state, 'synthetic'))
    def reject(*_a, **_k):
        raise CodexUsageError('unauthorized', status_code=401)
    monkeypatch.setattr(module, 'fetch_codex_usage', reject)
    monkeypatch.setattr(module, 'refresh_codex_auth', lambda *_a, **_k: pytest.fail('refresh'))
    monkeypatch.setattr(module, 'consume_reset_credit', lambda *_a, **_k: pytest.fail('POST'))
    before = switcher._credential_path('1').read_bytes()
    with pytest.raises(SwitchError):
        switcher.redeem_reset('1', True, True)
    assert switcher._credential_path('1').read_bytes() == before


def test_refresh_unknown_default_checks_before_reconcile(usage, monkeypatch):
    from claude_swap.exceptions import ConfigError

    switcher, _, baseline = usage
    def unreadable():
        raise ConfigError('synthetic')
    monkeypatch.setattr(switcher, '_read_switch_auth', unreadable)
    monkeypatch.setattr(switcher, '_guard_session_slot', lambda *_: pytest.fail('reconcile'))
    monkeypatch.setattr(module, 'refresh_codex_auth', lambda *_a, **_k: pytest.fail('refresh'))
    assert switcher._refresh_inactive_auth('1', baseline) is None


@pytest.mark.parametrize('state,accepted', [('live', True), ('quiescent', True),
                                          ('uncertain', False), ('conflict', False)])
def test_reset_final_custody_gate(usage, monkeypatch, state, accepted):
    from claude_swap.exceptions import SwitchError

    switcher, _, _ = usage
    monkeypatch.setattr(cs, 'slot_custody', lambda *_a, **_k: cs.Custody(state, 'synthetic'))
    calls = []
    monkeypatch.setattr(module, 'consume_reset_credit', lambda *_a, **_k:
                        calls.append(1) or {'code': 'reset'})
    record = switcher._read_sequence()['accounts']['1']
    if accepted:
        assert switcher._consume_reset('1', record, 'synthetic', 'card')[0]['code'] == 'reset'
    else:
        with pytest.raises(SwitchError):
            switcher._consume_reset('1', record, 'synthetic', 'card')
    assert len(calls) == int(accepted)


def test_reset_final_gate_refuses_changed_backup_identity(usage, monkeypatch):
    from claude_swap.exceptions import SwitchError

    switcher, _, baseline = usage
    other = copy.deepcopy(baseline)
    other['tokens']['account_id'] = 'other'
    write(switcher._credential_path('1'), other)
    monkeypatch.setattr(module, 'consume_reset_credit', lambda *_a, **_k: pytest.fail('POST'))
    with pytest.raises(SwitchError, match='identity changed'):
        switcher._consume_reset('1', switcher._read_sequence()['accounts']['1'], 'request', 'card')


@pytest.mark.parametrize('failure', ['intent', 'divergence'])
def test_reset_never_posts_with_pending_or_diverged_credentials(usage, monkeypatch, failure):
    from claude_swap.exceptions import SwitchError

    switcher, home, baseline = usage
    monkeypatch.setattr(cs, 'slot_custody', lambda *_a, **_k:
                        cs.Custody('quiescent', 'synthetic', intent=failure == 'intent'))
    if failure == 'divergence':
        write(home / 'auth.json', dict(baseline, rotation=1))
        write(switcher._credential_path('1'), dict(baseline, rotation=2))
    monkeypatch.setattr(module, 'consume_reset_credit', lambda *_a, **_k: pytest.fail('POST'))
    with pytest.raises(SwitchError):
        switcher._consume_reset('1', switcher._read_sequence()['accounts']['1'], 'request', 'card')
