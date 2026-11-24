"""Binary discovery and probes use synthetic installations and empty homes."""

import io
import json
import os
import signal
import sys
import time
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from ccswap import codex_binary as binary
from ccswap.exceptions import SessionError


def touch(path, content=''):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def metadata(root, name='@openai/codex', version='0.164.0'):
    touch(root / 'package.json', json.dumps({'name': name, 'version': version}))


def node_pe(path, machine=0x8664):
    header = bytearray(64)
    header[:2] = b'MZ'
    header[60:64] = (128).to_bytes(4, 'little')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(header + bytes(64) + b'PE\0\0' + machine.to_bytes(2, 'little'))
    return path


@pytest.fixture
def installation(tmp_path, monkeypatch):
    shim = touch(tmp_path / 'npm' / 'codex.cmd')
    node_pe(shim.parent / 'node.exe')
    package = shim.parent / 'node_modules' / '@openai' / 'codex'
    metadata(package)
    touch(package / 'bin' / 'codex.js')
    monkeypatch.setattr(binary, 'os', SimpleNamespace(name='nt'))
    monkeypatch.setattr(binary.shutil, 'which', lambda name,
        path: str(shim) if name == 'codex' else None)
    return shim, package


def platform(package, arch='x64', where='hoisted', executable=True):
    root = (package.parent if where == 'hoisted' else
            package / ('bin/node_modules/@openai' if where == 'bin' else 'node_modules/@openai'))
    root = root / f'codex-win32-{arch}'
    metadata(root, '@openai/codex', f'0.164.0-win32-{arch}')
    exe = root / 'vendor' / binary._TARGETS[arch] / 'bin' / 'codex.exe'
    if executable:
        touch(exe)
    return exe


def resolve(**env):
    return binary.resolve_codex_binary(
        {'PATH': 'synthetic', 'PROCESSOR_ARCHITECTURE': 'AMD64', **env})


@pytest.mark.parametrize('where', ['hoisted', 'nested', 'bin'])
def test_node_search(installation, where):
    _, package = installation
    expected = platform(package, where=where)
    if where != 'hoisted':
        platform(package, where='hoisted')
    assert resolve() == [str(expected.resolve())]


def test_legacy(installation):
    _, package = installation
    expected = touch(package / 'vendor' / binary._TARGETS['x64'] / 'bin' / 'codex.exe')
    assert resolve() == [str(expected.resolve())]
    platform(package, executable=False)
    with pytest.raises(SessionError, match='Put the native codex.exe on PATH'):
        resolve()


@pytest.mark.parametrize('arch,wow', [('ARM64', ''), ('AMD64', 'ARM64'), ('x86', 'ARM64')])
def test_os_arm64(installation, arch, wow):
    shim, package = installation
    node_pe(shim.parent / 'node.exe', 0xAA64)
    expected = platform(package, 'arm64')
    assert resolve(PROCESSOR_ARCHITECTURE=arch, PROCESSOR_ARCHITEW6432=wow) == [
        str(expected.resolve())]
    platform(package, 'x64')
    assert resolve(PROCESSOR_ARCHITECTURE=arch, PROCESSOR_ARCHITEW6432=wow) == [
        str(expected.resolve())]


def test_node_arch_overrides_os(installation):
    shim, package = installation
    expected = platform(package, 'x64')
    node_pe(shim.parent / 'node.exe', 0xAA64)
    with pytest.raises(SessionError):
        resolve(PROCESSOR_ARCHITECTURE='ARM64')
    node_pe(shim.parent / 'node.exe', 0x8664)
    assert resolve(PROCESSOR_ARCHITECTURE='ARM64') == [str(expected.resolve())]
    platform(package, 'arm64')
    assert resolve(PROCESSOR_ARCHITECTURE='ARM64') == [str(expected.resolve())]


@pytest.mark.parametrize('name,version', [('other', '0.164.0'), ('@openai/codex', 'not-a-version')])
def test_wrong_metadata(installation, name, version):
    metadata(installation[1], name, version)
    with pytest.raises(SessionError):
        resolve()


@pytest.mark.parametrize('failure',
    ['layout', 'missing-js', 'no-binary', 'bad-json', 'node-missing', 'denied'])
def test_unsupported(installation, monkeypatch, failure):
    shim, package = installation
    if failure == 'layout':
        monkeypatch.setattr(binary.shutil, 'which', lambda *a,
            **kw: str(touch(shim.with_suffix('.js'))))
    elif failure == 'missing-js':
        (package / 'bin/codex.js').unlink()
    elif failure == 'node-missing':
        (shim.parent / 'node.exe').unlink()
    elif failure == 'bad-json':
        (package / 'package.json').write_text('{')
    elif failure == 'denied':
        monkeypatch.setattr(binary, '_metadata',
            lambda *a: (_ for _ in ()).throw(PermissionError()))
    with pytest.raises(SessionError):
        resolve()


@pytest.mark.parametrize('windows', [False, True])
def test_direct(tmp_path, monkeypatch, windows):
    executable = touch(tmp_path / ('codex.exe' if windows else 'codex'))
    seen = []
    monkeypatch.setattr(binary, 'os', SimpleNamespace(name='nt' if windows else 'posix'))
    monkeypatch.setattr(binary.shutil, 'which', lambda name,
        path: seen.append((name, path)) or str(executable))
    assert resolve() == [str(executable.resolve())]
    assert seen == [('codex', 'synthetic')]


def test_missing(monkeypatch):
    monkeypatch.setattr(binary.shutil, 'which', lambda *a, **kw: None)
    with pytest.raises(SessionError):
        resolve()


class Probe:
    def __init__(self, output, result=0, timeout=False):
        self.stdout = io.BytesIO(output)
        self.result = result
        self.timeout = timeout
        self.killed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.stdout.close()

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        if self.timeout and not self.killed:
            raise subprocess.TimeoutExpired('synthetic', timeout)
        return self.result


def fake_probe(monkeypatch, outputs, **options):
    calls = []
    def popen(argv, **kwargs):
        home = Path(kwargs['env']['CODEX_HOME'])
        calls.append((argv, kwargs, home, list(home.iterdir())))
        return Probe(outputs[len(calls) - 1], **options)
    monkeypatch.setattr(binary.subprocess, 'Popen', popen)
    monkeypatch.setattr(binary, '_kill_probe', lambda process: process.kill())
    return calls


def test_probe_isolation(monkeypatch):
    inherited = {'PATH': 'synthetic', 'HOME': '/real', 'USERPROFILE': '/real',
                 'CODEX_HOME': '/real', 'TOKEN': 'synthetic-secret', 'NODE_OPTIONS': 'bad',
                 'SYSTEMROOT': 'win', 'TEMP': 'temp'}
    monkeypatch.setattr(binary, 'os', SimpleNamespace(environ=inherited, name='posix'))
    calls = fake_probe(monkeypatch, [b'codex-cli 0.164.0\n', b'Usage:\n  --no-daemon\n'])
    binary.probe_codex(['/native path/codex'])
    assert len(calls) == 2
    for argv, kwargs, home, contents in calls:
        assert argv[0] == '/native path/codex'
        assert contents == []
        assert kwargs == dict(env={'PATH': 'synthetic', 'SYSTEMROOT': 'win', 'TEMP': 'temp',
                                   'HOME': str(home), 'USERPROFILE': str(home),
                                       'CODEX_HOME': str(home)},
                              stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, shell=False, bufsize=0,
                                  start_new_session=True)
        assert not home.exists()
    assert calls[0][0][-1] == '--version'
    assert calls[1][0][-1] == '--help'
    assert inherited['HOME'] == '/real'


@pytest.mark.parametrize('output', [b'codex-cli 0.163.0', b'codex-cli 0.164.00',
                                    b'codex-cli 0.165.0-alpha.1', b'0.164.0', b'noise\ncodex-cli 0.164.0', b'\xff'])
def test_version_refused(monkeypatch, output):
    calls = fake_probe(monkeypatch, [output])
    with pytest.raises(SessionError, match='requires Codex 0.164.0'):
        binary.probe_codex(['codex.exe'])
    assert not calls[0][2].exists()


@pytest.mark.parametrize('help_output', [b'help', b'--no-daemon-extra', b'x--no-daemon'])
def test_capability_refused(monkeypatch, help_output):
    fake_probe(monkeypatch, [b'codex-cli 0.164.0', help_output])
    with pytest.raises(SessionError, match='--no-daemon'):
        binary.probe_codex(['codex.exe'])


@pytest.mark.parametrize('failure', ['timeout', 'exit', 'oversized', 'spawn'])
def test_probe_failure(monkeypatch, failure):
    if failure == 'spawn':
        monkeypatch.setattr(binary.subprocess, 'Popen', lambda *a,
            **kw: (_ for _ in ()).throw(OSError('synthetic-secret')))
    else:
        calls = fake_probe(monkeypatch,
            [b'x' * (binary._LIMIT + 1) if failure == 'oversized' else b''],
                           timeout=failure == 'timeout', result=1 if failure == 'exit' else 0)
    with pytest.raises(SessionError) as error:
        binary.probe_codex(['codex.exe'])
    assert 'synthetic-secret' not in str(error.value)
    if failure != 'spawn':
        assert not calls[0][2].exists()


@pytest.mark.parametrize('suffix', ['.cmd', '.ps1', '.bat'])
def test_shim_extensions(installation, monkeypatch, suffix):
    shim, package = installation
    expected = platform(package)
    shim = touch(shim.with_suffix(suffix))
    monkeypatch.setattr(binary.shutil, 'which', lambda *a, **kw: str(shim))
    assert resolve() == [str(expected.resolve())]


@pytest.mark.parametrize('name,version', [('wrong', '0.164.0'), ('@openai/codex', '0.165.0')])
def test_metadata_failures_keep_diagnostics_private(installation, name, version):
    metadata(installation[1], name, version)
    with pytest.raises(SessionError) as error:
        resolve()
    assert name not in str(error.value)
    assert version not in str(error.value)


@pytest.mark.skipif(os.name == 'nt', reason='Windows probes require native codex.exe')
@pytest.mark.parametrize('scenario', ['ok', 'oversized', 'timeout', 'exit'])
def test_real_probe_with_synthetic_program(tmp_path, monkeypatch, scenario):
    import sys

    script = touch(tmp_path / 'synthetic_probe.py', '''
import os, sys, time
from pathlib import Path
home = Path(os.environ['CODEX_HOME'])
assert home == Path(os.environ['HOME']) == Path(os.environ['USERPROFILE'])
assert list(home.iterdir()) == []
assert not {'OPENAI_API_KEY', 'NODE_OPTIONS', 'SYNTHETIC_SECRET'} & os.environ.keys()
scenario = sys.argv[1]
if scenario == 'timeout':
    time.sleep(10)
if scenario == 'exit':
    sys.exit(3)
if scenario == 'oversized':
    print('x' * 200000)
else:
    print('codex-cli 0.164.0' if sys.argv[-1] == '--version' else '--no-daemon')
''')
    monkeypatch.setenv('SYNTHETIC_SECRET', 'synthetic')
    monkeypatch.setenv('NODE_OPTIONS', 'synthetic')
    monkeypatch.setattr(binary, '_TIMEOUT', 0.5)
    if scenario == 'ok':
        binary.probe_codex([sys.executable, str(script), scenario])
    else:
        with pytest.raises(SessionError):
            binary.probe_codex([sys.executable, str(script), scenario])


@pytest.mark.parametrize('damage',
    ['unknown', 'mz', 'short', 'pe', 'offset-low', 'offset-high', 'truncated'])
def test_invalid_node_pe(installation, damage):
    shim, package = installation
    platform(package)
    node = shim.parent / 'node.exe'
    data = bytearray(node.read_bytes())
    if damage == 'unknown':
        data[-2:] = b'\x4c\x01'  # Unsupported x86 Node.
    elif damage == 'mz':
        data[:2] = b'XX'
    elif damage == 'short':
        data = data[:63]
    elif damage == 'pe':
        data[128:132] = b'XXXX'
    elif damage.startswith('offset'):
        data[60:64] = (63 if damage == 'offset-low' else 1024 * 1024 + 1).to_bytes(4, 'little')
    else:
        data = data[:-1]
    node.write_bytes(data)
    with pytest.raises(SessionError, match='Put the native codex.exe on PATH'):
        resolve()


def test_oversized_help(monkeypatch):
    fake_probe(monkeypatch, [b'codex-cli 0.164.0', b'--no-daemon\n' + b'x' * binary._LIMIT])
    with pytest.raises(SessionError):
        binary.probe_codex(['codex.exe'])


@pytest.mark.parametrize('prefix', [[], [''], [None]])
def test_invalid_prefix(prefix):
    with pytest.raises(SessionError):
        binary.probe_codex(prefix)


def test_probe_ignores_stderr_warnings(monkeypatch):
    # Real Codex 0.164.0 warns on stderr about helper aliases in a temporary home.
    seen = []

    class Fake:
        def __init__(self, argv, **kwargs):
            seen.append(kwargs["stderr"])
            text = b"codex-cli 0.164.0\n" if argv[-1] == "--version" else b"  --no-daemon\n"
            self.stdout = io.BytesIO(text)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pass

    monkeypatch.setattr(binary.subprocess, "Popen", Fake)
    binary.probe_codex(["codex.exe"])
    assert seen == [binary.subprocess.DEVNULL] * 2


@pytest.mark.parametrize('arch,machine', [('x64', 0x8664), ('arm64', 0xAA64)])
def test_node_from_path(installation, tmp_path, monkeypatch, arch, machine):
    shim, package = installation
    (shim.parent / 'node.exe').unlink()
    node = node_pe(tmp_path / 'path-node' / 'node.exe', machine)
    seen = []
    def which(name, path):
        seen.append((name, path))
        return str(shim if name == 'codex' else node)
    monkeypatch.setattr(binary.shutil, 'which', which)
    expected = platform(package, arch)
    assert resolve() == [str(expected.resolve())]
    assert seen == [('codex', 'synthetic'), ('node.exe', 'synthetic')]


def test_local_node_is_authoritative(installation, monkeypatch):
    shim, package = installation
    platform(package)
    (shim.parent / 'node.exe').write_bytes(b'broken')
    def which(name, path):
        assert name == 'codex'  # Never substitute another PATH installation.
        return str(shim)
    monkeypatch.setattr(binary.shutil, 'which', which)
    with pytest.raises(SessionError):
        resolve()


@pytest.mark.parametrize('suffix', ['.cmd', '.bat', '.ps1'])
def test_windows_resolves_only_native_probe_prefix(installation, monkeypatch, suffix):
    shim, package = installation
    shim = touch(shim.with_suffix(suffix))
    monkeypatch.setattr(binary.shutil, 'which', lambda *a, **kw: str(shim))
    expected = platform(package)
    prefix = resolve()
    assert prefix == [str(expected.resolve())]
    assert Path(prefix[0]).name == 'codex.exe'
    with pytest.raises(SessionError, match='Put the native codex.exe on PATH'):
        binary.probe_codex([str(shim)])


@pytest.mark.skipif(os.name != 'posix', reason='POSIX process groups')
@pytest.mark.parametrize('failure', ['timeout', 'oversized'])
def test_probe_kills_wrapper_and_child(tmp_path, monkeypatch, failure):
    child_pid = tmp_path / 'child.pid'
    wrapper_pid = tmp_path / 'wrapper.pid'
    wrapper = touch(tmp_path / 'wrapper.py', '''
import os, subprocess, sys, time
from pathlib import Path
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
Path(sys.argv[1]).write_text(str(child.pid))
Path(sys.argv[2]).write_text(str(os.getpid()))
if sys.argv[3] == 'oversized':
    sys.stdout.write('x' * 200000)
    sys.stdout.flush()
time.sleep(60)
''')
    # Long enough for interpreter startup under a loaded parallel run.
    monkeypatch.setattr(binary, '_TIMEOUT', 3)
    try:
        with pytest.raises(SessionError):
            binary.probe_codex(
                [sys.executable, str(wrapper), str(child_pid), str(wrapper_pid), failure])
        assert child_pid.exists(), 'Wrapper must spawn the child before timeout'
        pid = int(child_pid.read_text())
        # A killed orphan may remain a zombie until PID 1 reaps it; it cannot run.
        def running():
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return False
            stat = Path(f'/proc/{pid}/stat')
            if stat.exists():
                return stat.read_text().rsplit(')', 1)[1].split()[0] != 'Z'
            return True
        deadline = time.monotonic() + 2
        while running() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not running()
        with pytest.raises(ProcessLookupError):
            os.kill(int(wrapper_pid.read_text()), 0)
    finally:
        if wrapper_pid.exists():
            try:
                os.killpg(int(wrapper_pid.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_windows_probe_native_without_process_group(monkeypatch):
    monkeypatch.setattr(binary, 'os', SimpleNamespace(name='nt', environ={}))
    calls = fake_probe(monkeypatch, [b'codex-cli 0.164.0', b'--no-daemon'])
    binary.probe_codex(['codex.exe'])
    assert all(not kwargs['start_new_session'] for _, kwargs, _, _ in calls)
    assert all(argv == ['codex.exe', flag]
               for (argv, _, _, _), flag in zip(calls, ['--version', '--help']))


def test_windows_kill_is_direct(monkeypatch):
    monkeypatch.setattr(binary, 'os', SimpleNamespace(name='nt'))
    process = Probe(b'')
    binary._kill_probe(process)
    assert process.killed


def test_node_read_denied(installation, monkeypatch):
    shim, package = installation
    platform(package)
    original = Path.open
    def denied(path, *args, **kwargs):
        if path == shim.parent / 'node.exe':
            raise PermissionError('synthetic-private-detail')
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', denied)
    with pytest.raises(SessionError, match='Put the native codex.exe on PATH') as error:
        resolve()
    assert 'synthetic-private-detail' not in str(error.value)


@pytest.mark.parametrize('version', ['0.153.4', '0.165.0', '1.0.0', '0.165.0-alpha.1'])
def test_resolver_is_independent_of_session_minimum(installation, version):
    _, package = installation
    metadata(package, version=version)
    expected = touch(package / 'vendor' / binary._TARGETS['x64'] / 'bin' / 'codex.exe')
    assert resolve() == [str(expected.resolve())]


@pytest.mark.parametrize('version', ['0.165.0', '1.0.0'])
def test_newer_stable_with_required_capability(monkeypatch, version):
    fake_probe(monkeypatch, [f'codex-cli {version}\n'.encode(), b'--no-daemon'])
    binary.probe_codex(['codex.exe'])


def test_platform_package_must_match_selected_installation(installation):
    _, package = installation
    platform(package)
    metadata(package, version='0.165.0')
    with pytest.raises(SessionError):
        resolve()
