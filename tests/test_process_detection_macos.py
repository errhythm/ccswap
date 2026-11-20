"""Darwin ABI mocks use synthetic process data, never real profile directories."""

from __future__ import annotations

import ctypes
import errno
import os
import signal
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from ccswap import process_detection as detection

BOOT = "12345678-1234-1234-1234-123456789abc"


def procargs(env=b"OTHER=value\0", argv=(b"worker",), executable=b"/usr/bin/worker",
             pointer_size=8):
    path = executable + b"\0"
    padding = -(len(b"executable_path=") + len(path)) % pointer_size
    return (bytes(ctypes.c_int32(len(argv))) + path + b"\0" * padding
            + b"\0".join(argv) + b"\0" + env)


class SysctlCall:
    def __init__(self, backend, named=False):
        self.backend, self.named = backend, named

    def __call__(self, *args):
        if self.named:
            name, output, length, new, newlen = args
            key = name.decode("ascii")
        else:
            name, count, output, length, new, newlen = args
            key = tuple(name[:count])
        assert new is None and newlen == 0
        size = ctypes.cast(length, ctypes.POINTER(ctypes.c_size_t))
        phase = "size" if output is None else "read"
        self.backend.calls.append((key, phase, size[0]))
        value = self.backend.response(key, phase)
        if isinstance(value, int):
            ctypes.set_errno(value)
            return -1
        if output is not None:
            ctypes.memmove(output, value, min(size[0], len(value)))
        size[0] = len(value)
        return 0


@pytest.fixture
def mac(monkeypatch):
    monkeypatch.setattr(os, "getuid", lambda: 1000, raising=False)
    monkeypatch.setattr(detection.sys, "platform", "darwin")
    monkeypatch.setattr(detection.platform, "machine", lambda: "x86_64")
    abi = detection._codex_macos_abi("x86_64")
    state = SimpleNamespace(rows={}, environments={}, calls=[], overrides={}, include_self=True)

    def add(pid=42, *, parent=1, comm=b"worker", seconds=100, micros=123,
            uid=None, ruid=None, status=b"\3", env=b"OTHER=value\0"):
        uid = os.getuid() if uid is None else uid
        ruid = uid if ruid is None else ruid
        row = abi()
        row.kp_proc.p_pid, row.kp_proc.p_comm, row.kp_proc.p_stat = pid, comm, status
        start = getattr(row.kp_proc.p_un, "__p_starttime")
        start.tv_sec, start.tv_usec = seconds, micros
        row.kp_eproc.e_ppid = parent
        row.kp_proc.p_flag = 4
        row.kp_eproc.e_ucred.cr_uid = uid
        row.kp_eproc.e_pcred.p_ruid = ruid
        state.rows[pid], state.environments[pid] = row, procargs(env)
        return row

    def response(key, phase):
        override = state.overrides.get((key, phase), state.overrides.get(key))
        if override is not None:
            value = override() if callable(override) else override
            if key == (1, 14, 0, 0) and isinstance(value, bytes) and state.include_self:
                value += bytes(state.control)
            return value
        if key == "kern.bootsessionuuid":
            return BOOT.encode() + b"\0"
        if key == (1, 8):
            return bytes(ctypes.c_int32(1024 * 1024))
        if key == (1, 14, 0, 0):
            return (b"".join(bytes(row) for row in state.rows.values())
                    + (bytes(state.control) if state.include_self else b""))
        if key[:3] == (1, 14, 1):
            if key[3] == os.getpid():
                return bytes(state.control)
            return bytes(state.rows[key[3]]) if key[3] in state.rows else b""
        if key[:2] == (1, 49):
            return state.environments[key[2]]
        raise AssertionError(key)

    state.response, state.add = response, add
    state.libc = SimpleNamespace(sysctl=SysctlCall(state), sysctlbyname=SysctlCall(state, True))
    monkeypatch.setattr(detection.ctypes, "CDLL", lambda *a, **kw: state.libc)
    state.control = add(os.getpid(), parent=0)
    del state.rows[os.getpid()]
    add()
    return state


@pytest.mark.parametrize("arch", ["x86_64", "arm64"])
def test_macos_complete_abi(arch):
    abi = detection._codex_macos_abi(arch)
    assert ctypes.sizeof(abi) == 648
    assert [name for name, _ in abi._fields_] == ["kp_proc", "kp_eproc"]
    proc = abi._fields_[0][1]
    assert ctypes.sizeof(proc) == 296
    boolean = dict(proc._fields_)["sigwait"]
    assert boolean(-1).value == (2**32 - 1 if arch == "x86_64" else -1)
    assert ctypes.alignment(abi) == 8


def test_macos_exact_identity_and_pid_reuse(mac):
    start = detection.codex_process_identity(42)
    assert start == {"kind": "macos", "bootId": BOOT, "uniqueId": "100000123"}
    assert detection.codex_process_state(42, start) == "alive"
    mac.add(micros=124)
    assert detection.codex_process_state(42, start) == "gone"
    del mac.rows[42]
    assert detection.codex_process_state(42, start) == "gone"


@pytest.mark.parametrize("pid", [0, -1, True, 2**32 + 42])
def test_macos_bad_pid(mac, pid):
    assert detection.codex_process_identity(pid) is None
    assert detection.codex_process_state(pid, {}) == "unknown"


@pytest.mark.parametrize("start", [None, [], {}, {"kind": "macos"},
    {"kind": "linux", "bootId": BOOT, "uniqueId": "1"},
    {"kind": "macos", "bootId": BOOT, "uniqueId": 1},
    {"kind": "macos", "bootId": BOOT, "uniqueId": "١"},
    {"kind": "macos", "bootId": BOOT, "uniqueId": ""},
    {"kind": "macos", "bootId": 1, "uniqueId": "1"},
    {"kind": "macos", "bootId": BOOT, "uniqueId": "1", "extra": 1}])
def test_macos_bad_start_even_missing_pid(mac, start):
    assert detection.codex_process_state(999, start) == "unknown"


@pytest.mark.parametrize("env", [b"CODEX_HOME=/path with spaces/'quotes'\\escapes\nline\0",
                                  b"LONG=" + b"x" * 200000 + b"\0CODEX_HOME=/tmp/home\0"],
                         ids=["special-characters", "large-environment"])
def test_macos_procargs_preserves_values(env):
    assert detection._codex_macos_environment(
        procargs(env, (b"worker", b"arg with spaces"))) == (env, b"")


@pytest.mark.parametrize("data", [b"", b"\0" * 4, bytes(ctypes.c_int32(-1)),
    bytes(ctypes.c_int32(9999)) + b"/exec\0arg\0",
    bytes(ctypes.c_int32(1)) + b"/exec", bytes(ctypes.c_int32(1)) + b"\0arg\0",
    bytes(ctypes.c_int32(2)) + b"/exec\0arg\0",
])
def test_macos_malformed_procargs(mac, tmp_path, data):
    with pytest.raises(ValueError):
        detection._codex_macos_environment(data)
    mac.environments[42] = data
    assert detection.scan_codex_home(tmp_path, [])["state"] == "quiescent"
    assert detection.scan_codex_home(tmp_path, [])["unreadable"] == 1


@pytest.mark.parametrize("env", [b"", b"\0", b"\0\0\0"])
def test_macos_hidden_environment_is_empty(env):
    assert detection._codex_macos_environment(procargs(env)) == (b"", b"")


def test_macos_scan_paths_and_argv_are_distinct(mac, tmp_path):
    mac.environments[42] = procargs(b"OTHER=x\0",
        (b"worker", b"CODEX_HOME=" + os.fsencode(tmp_path)))
    assert detection.scan_codex_home(tmp_path, [])["state"] == "quiescent"
    alias = tmp_path / "alias with spaces"
    alias.symlink_to(tmp_path, target_is_directory=True)
    mac.add(env=b"CODEX_HOME=" + os.fsencode(alias) + b"\0")
    result = detection.scan_codex_home(tmp_path, [])
    assert result == {"state": "live", "pids": [42], "issues": [], "unreadable": 0}


@pytest.mark.parametrize("env, expected", [
    (b"CODEX_HOME=relative\0", "uncertain"),
    (b"missing-absolute-home", "uncertain"),
    (b"CODEX_HOME=\0", "quiescent"),
    (b"MALFORMED\0", "quiescent"),
])
def test_macos_ambiguous_environment(mac, tmp_path, env, expected):
    if env == b"missing-absolute-home":
        missing = tmp_path / "missing-ccswap-alias"
        assert missing.is_absolute() and not missing.exists()
        env = b"CODEX_HOME=" + os.fsencode(missing) + b"\0"
    mac.add(env=env)
    assert detection.scan_codex_home(tmp_path, [])["state"] == expected


def test_macos_positive_evidence_keeps_issues(mac, tmp_path):
    mac.add(env=b"CODEX_HOME=" + os.fsencode(tmp_path) + b"\0CODEX_HOME=/other\0")
    result = detection.scan_codex_home(tmp_path, [], invalid_records=1)
    assert result["state"] == "live" and result["issues"]
    assert "invalid or unreadable run records" in result["issues"]


@pytest.mark.parametrize("key", [(1, 14, 1, 42), (1, 14, 0, 0), (1, 49, 42),
                                  "kern.bootsessionuuid"])
@pytest.mark.parametrize("phase", ["size", "read"])
@pytest.mark.parametrize("error", [errno.EINVAL, errno.EPERM, errno.ESRCH])
def test_macos_sysctl_errors_fail_closed(mac, tmp_path, key, phase, error):
    mac.add(comm=b"codex-worker")
    mac.overrides[key, phase] = error
    result = detection.scan_codex_home(tmp_path, [])
    assert result["state"] == "uncertain", result
    assert detection.codex_process_state(42, {}) == "unknown"
    assert "CODEX_HOME=" not in str(result)


@pytest.mark.parametrize("data", [b"x", b"x" * 647, b"x" * 649])
def test_macos_bad_row_size(mac, tmp_path, data):
    mac.overrides[(1, 14, 1, 42)] = data
    assert detection.codex_process_identity(42) is None
    assert detection.scan_codex_home(tmp_path, [])["state"] == "uncertain"


@pytest.mark.parametrize("data", [b"", b"x", BOOT.encode(), b"invalid" + b"\0" * 30])
def test_macos_bad_boot_is_unknown(mac, tmp_path, data):
    mac.overrides["kern.bootsessionuuid"] = data
    assert detection.codex_process_identity(42) is None
    assert detection.codex_process_state(999,
        {"kind": "macos", "bootId": BOOT, "uniqueId": "1"}) == "unknown"
    assert detection.scan_codex_home(tmp_path, [])["state"] == "uncertain"


def test_macos_boot_uuid_change_is_exact_mismatch(mac):
    start = detection.codex_process_identity(42)
    mac.overrides["kern.bootsessionuuid"] = b"87654321-1234-1234-1234-123456789abc\0"
    assert detection.codex_process_state(42, start) == "gone"


# XNU refuses KERN_PROCARGS2 for some live same-uid processes with EINVAL, not EPERM.
@pytest.mark.parametrize("error", [errno.EPERM, errno.EINVAL])
@pytest.mark.parametrize("kind",
    ["unrelated", "codex", "ancestor", "supervisor", "cycle", "missing"])
def test_macos_unreadable_attribution(mac, tmp_path, kind, error):
    mac.rows.clear()
    mac.add(44, comm=b"codex-worker" if kind == "codex" else b"worker")
    supervisors = []
    if kind in ("ancestor", "supervisor"):
        mac.add(42, env=(b"CODEX_HOME=" + os.fsencode(tmp_path) + b"\0")
                if kind == "ancestor" else b"OTHER=x\0")
        mac.add(43, parent=42)
        mac.rows[44].kp_eproc.e_ppid = 43
        if kind == "supervisor":
            supervisors = [(42, detection.codex_process_identity(42))]
    elif kind in ("cycle", "missing"):
        mac.rows[44].kp_eproc.e_ppid = 44 if kind == "cycle" else 999
    mac.overrides[(1, 49, 44)] = error
    result = detection.scan_codex_home(tmp_path, supervisors)
    assert result["state"] == ("live" if kind in ("ancestor", "supervisor") else
                               "uncertain" if kind == "codex" else "quiescent")
    assert result["unreadable"] == 1
    assert ("PID 44: environment unreadable" in result["issues"]) == (
        kind in ("codex", "ancestor", "supervisor"))


def test_macos_zombie_and_other_uid(mac, tmp_path):
    mac.add(uid=os.getuid() + 1)
    mac.add(43, status=b"\5", env=b"")
    assert detection.scan_codex_home(tmp_path, [])["state"] == "quiescent"
    assert not any(key == (1, 49, 43) for key, _, _ in mac.calls)


def test_macos_inode_alias(mac, tmp_path, monkeypatch):
    from pathlib import Path

    alias = tmp_path / "alias"
    alias.mkdir()
    mac.add(env=b"CODEX_HOME=" + os.fsencode(alias) + b"\0")
    real = Path.stat
    monkeypatch.setattr(Path, "stat", lambda path, *a,
        **kw: real(tmp_path if path == alias else path, *a, **kw))
    assert detection.scan_codex_home(tmp_path, [])["state"] == "live"


def test_macos_identity_race_retries(mac, tmp_path):
    mac.add(env=b"CODEX_HOME=" + os.fsencode(tmp_path) + b"\0")
    calls = 0

    def reuse():
        nonlocal calls
        calls += 1
        if calls == 2:
            mac.add(micros=999, env=b"CODEX_HOME=" + os.fsencode(tmp_path) + b"\0")
        return mac.environments[42]

    mac.overrides[(1, 49, 42)] = reuse
    assert detection.scan_codex_home(tmp_path, [])["state"] == "live"
    assert calls == 6  # query/read, retry, second pass


def test_macos_continuously_reused_pid_is_uncertain(mac, tmp_path):
    count = 0

    def reuse():
        nonlocal count
        count += 1
        mac.add(micros=count)
        return mac.environments[42]

    mac.overrides[(1, 49, 42)] = reuse
    assert detection.scan_codex_home(tmp_path, [])["state"] == "uncertain"


def test_macos_second_pass_union(mac, tmp_path):
    mac.add(env=b"CODEX_HOME=" + os.fsencode(tmp_path) + b"\0")
    calls = 0

    def enumerate_rows():
        nonlocal calls
        calls += 1
        if calls == 3:
            mac.add(43, env=b"CODEX_HOME=relative\0")
        return b"".join(bytes(row) for row in mac.rows.values())

    mac.overrides[(1, 14, 0, 0)] = enumerate_rows
    result = detection.scan_codex_home(tmp_path, [])
    assert result["pids"] == [42] and result["issues"] == ["PID 43: relative CODEX_HOME"]
    assert calls == 4


def test_macos_missing_enumerated_row_is_gone(mac, tmp_path):
    mac.overrides[(1, 14, 1, 42)] = b""
    assert detection.scan_codex_home(tmp_path, [])["state"] == "quiescent"


def test_macos_supervisor_reserves_launch(mac, tmp_path):
    start = detection.codex_process_identity(42)
    assert detection.scan_codex_home(tmp_path, [(42, start)])["pids"] == [42]
    mac.overrides[(1, 14, 1, 42)] = errno.EPERM
    assert detection.scan_codex_home(tmp_path, [(42, start)])["state"] == "uncertain"


def test_macos_procargs_uses_argmax_capacity(mac):
    detection._codex_macos_sysctl((1, 49, 42))
    assert ((1, 49, 42), "read", 1024 * 1024 + 4) in mac.calls
    assert mac.libc.sysctl.restype is ctypes.c_int
    assert len(mac.libc.sysctl.argtypes) == 6


def test_macos_growing_buffer_is_rejected(mac):
    mac.overrides[((1, 14, 0, 0), "read")] = b"x" * 1296
    with pytest.raises(ValueError, match="truncated"):
        detection._codex_macos_pids()


@pytest.mark.skipif(sys.platform != "darwin", reason="native macOS release gate")
@pytest.mark.xfail(sys.platform == "darwin", strict=False,
                   reason="native macOS scan not validated yet; `codex run` refuses macOS")
def test_macos_native_child_and_orphaned_grandchild(tmp_path):
    import select

    marker = tmp_path / "parent-exit"
    grandchild_code = "import time; time.sleep(60)"
    child_code = (
        "import pathlib,subprocess,sys,time; "
        f"p=subprocess.Popen([sys.executable,'-c',{grandchild_code!r}],stdout=subprocess.DEVNULL); "
        "print(p.pid,flush=True); "
        f"marker=pathlib.Path({str(marker)!r}); "
        "\nwhile not marker.exists(): time.sleep(.02)"
    )
    child = subprocess.Popen([sys.executable, "-c", child_code], stdout=subprocess.PIPE,
                             text=True, env={**os.environ, "CODEX_HOME": str(tmp_path)})
    grandchild = None
    try:
        assert select.select([child.stdout], [], [], 5)[0], "child startup timed out"
        grandchild = int(child.stdout.readline())
        result = detection.scan_codex_home(tmp_path, [])
        assert result["state"] == "live" and {child.pid, grandchild} <= set(result["pids"]), (
            result.get("issues"))
        marker.touch()
        child.wait(timeout=5)
        result = detection.scan_codex_home(tmp_path, [])
        assert result["state"] == "live" and grandchild in result["pids"]
    finally:
        if child.poll() is None:
            child.terminate()
        child.wait(timeout=5)
        if grandchild is not None:
            try:
                os.kill(grandchild, signal.SIGTERM)
            except ProcessLookupError:
                pass
    deadline = time.monotonic() + 5
    while True:
        result = detection.scan_codex_home(tmp_path, [])
        if result["state"] == "quiescent" or time.monotonic() >= deadline:
            break
        time.sleep(.02)
    assert result["state"] == "quiescent", result["issues"]


@pytest.mark.parametrize("arch", ["i386", "unknown"])
def test_macos_unsupported_abi_refuses(mac, tmp_path, monkeypatch, arch):
    monkeypatch.setattr(detection.platform, "machine", lambda: arch)
    assert detection.codex_process_identity(42) is None
    assert detection.scan_codex_home(tmp_path, [])["state"] == "uncertain"
    assert not mac.calls


def test_macos_abi_size_mismatch_refuses(mac, tmp_path, monkeypatch):
    real = ctypes.sizeof
    monkeypatch.setattr(detection.ctypes, "sizeof", lambda value: 647 if
                        getattr(value, "__name__", "") == "KinfoProc" else real(value))
    detection._codex_macos_abi.cache_clear()
    try:
        assert detection.codex_process_identity(42) is None
        assert detection.scan_codex_home(tmp_path, [])["state"] == "uncertain"
        assert not mac.calls
    finally:
        detection._codex_macos_abi.cache_clear()


def test_macos_missing_symbol_and_library(mac, tmp_path, monkeypatch):
    monkeypatch.setattr(detection.ctypes, "CDLL", lambda *a, **kw: SimpleNamespace())
    assert detection.codex_process_identity(42) is None
    assert detection.scan_codex_home(tmp_path, [])["state"] == "uncertain"

    def missing_library(*args, **kwargs):
        raise OSError("unavailable")

    monkeypatch.setattr(detection.ctypes, "CDLL", missing_library)
    assert detection.codex_process_identity(42) is None
    assert detection.scan_codex_home(tmp_path, [])["state"] == "uncertain"


@pytest.mark.parametrize("field, value", [("p_pid", 99), ("p_stat", b"\0"),
                                         ("tv_sec", 0), ("tv_usec", 1000000)])
def test_macos_invalid_snapshot(mac, tmp_path, field, value):
    row = type(mac.rows[42]).from_buffer_copy(bytes(mac.rows[42]))
    proc = row.kp_proc
    target = getattr(proc.p_un, "__p_starttime") if field.startswith("tv_") else proc
    setattr(target, field, value)
    mac.overrides[(1, 14, 1, 42)] = bytes(row)
    assert detection.codex_process_identity(42) is None
    assert detection.scan_codex_home(tmp_path, [])["state"] == "uncertain"


def test_macos_duplicate_pid_rows_are_unknown(mac, tmp_path):
    mac.overrides[(1, 14, 1, 42)] = bytes(mac.rows[42]) * 2
    assert detection.codex_process_identity(42) is None
    assert detection.scan_codex_home(tmp_path, [])["state"] == "uncertain"


@pytest.mark.parametrize("data", [b"x", bytes(ctypes.c_int32(0)), bytes(ctypes.c_int32(-1))])
def test_macos_bad_argmax(mac, tmp_path, data):
    mac.overrides[(1, 8)] = data
    assert detection.scan_codex_home(tmp_path, [])["state"] == "uncertain"


def test_macos_hidden_codex_environment_is_uncertain(mac, tmp_path):
    mac.add(comm=b"codex", env=b"")
    result = detection.scan_codex_home(tmp_path, [])
    assert result == {"state": "uncertain", "pids": [],
                      "issues": ["PID 42: environment unreadable"], "unreadable": 1}


def test_macos_confirmed_disappearance_during_env_read(mac, tmp_path):
    def disappear():
        mac.rows.pop(42, None)
        return errno.EINVAL

    mac.overrides[((1, 49, 42), "read")] = disappear
    assert detection.scan_codex_home(tmp_path, [])["state"] == "quiescent"


def test_macos_unreadable_name_change_preserves_first_pass(mac, tmp_path):
    mac.add(comm=b"codex-helper")
    calls = 0

    def denied():
        nonlocal calls
        calls += 1
        if calls == 2:
            mac.rows[42].kp_proc.p_comm = b"worker"
        return errno.EPERM

    mac.overrides[(1, 49, 42)] = denied
    assert detection.scan_codex_home(tmp_path, [])["state"] == "uncertain"


def test_macos_related_unreadable_ancestor_failure(mac, tmp_path):
    mac.add(42, parent=43)
    mac.overrides[(1, 49, 42)] = errno.EPERM
    mac.overrides[(1, 14, 1, 43)] = errno.EINVAL
    assert detection.scan_codex_home(tmp_path, [])["state"] == "uncertain"


def test_macos_first_pass_evidence_retained_when_second_pass_empty(mac, tmp_path):
    mac.add(env=b"CODEX_HOME=" + os.fsencode(tmp_path) + b"\0")
    calls = 0

    def rows():
        nonlocal calls
        calls += 1
        return bytes(mac.rows[42]) if calls <= 2 else b""

    mac.overrides[(1, 14, 0, 0)] = rows
    assert detection.scan_codex_home(tmp_path, [])["pids"] == [42]


@pytest.mark.parametrize("kind", ["pid", "size"])
def test_macos_invalid_enumeration_is_uncertain(mac, tmp_path, kind):
    if kind == "pid":
        mac.rows[42].kp_proc.p_pid = 0
    data = bytes(mac.rows[42]) + (b"x" if kind == "size" else b"")
    mac.overrides[(1, 14, 0, 0)] = data
    result = detection.scan_codex_home(tmp_path, [])
    assert result["state"] == "uncertain"
    assert result["issues"] == ["process enumeration unavailable"]


def test_macos_oversized_sysctl_buffer_is_refused(mac, tmp_path):
    original = mac.libc.sysctl

    def oversized(name, count, output, length, new, newlen):
        if tuple(name[:count]) == (1, 14, 0, 0) and output is None:
            ctypes.cast(length, ctypes.POINTER(ctypes.c_size_t))[0] = 64 * 1024 * 1024 + 1
            return 0
        return original(name, count, output, length, new, newlen)

    mac.libc.sysctl = oversized
    assert detection.scan_codex_home(tmp_path, [])["state"] == "uncertain"


def test_macos_home_resolution_failures(mac, tmp_path):
    from pathlib import Path

    assert detection.scan_codex_home(Path("relative"), [])["state"] == "uncertain"
    loop = tmp_path / "loop"
    loop.symlink_to(loop)
    mac.add(env=b"CODEX_HOME=" + os.fsencode(loop) + b"\0")
    assert detection.scan_codex_home(tmp_path, [])["state"] == "uncertain"
    assert detection.scan_codex_home(loop, [])["state"] == "uncertain"


@pytest.mark.parametrize("kind", ["zombie", "other_uid"])
def test_macos_reuse_while_excluding_process_is_retried(mac, tmp_path, kind):
    old = mac.add(status=b"\5" if kind == "zombie" else b"\3",
                  uid=os.getuid() + 1 if kind == "other_uid" else os.getuid())
    calls = 0

    def row():
        nonlocal calls
        calls += 1
        if calls == 2:
            mac.add(micros=999, env=b"CODEX_HOME=" + os.fsencode(tmp_path) + b"\0")
        return bytes(mac.rows[42])

    # Keep the enumeration's own-uid scope while simulating a recycled foreign PID.
    listed = type(old).from_buffer_copy(bytes(old))
    listed.kp_eproc.e_ucred.cr_uid = os.getuid()
    mac.overrides[(1, 14, 0, 0)] = bytes(listed)
    mac.overrides[((1, 14, 1, 42), "read")] = row
    result = detection.scan_codex_home(tmp_path, [])
    assert result["state"] == "live" and result["pids"] == [42]


def test_macos_capacity_filled_procargs_is_uncertain(mac, tmp_path):
    mac.overrides[(1, 8)] = bytes(ctypes.c_int32(len(mac.environments[42]) - 4))
    assert detection.scan_codex_home(tmp_path, [])["state"] == "uncertain"


def test_macos_wall_clock_changes_do_not_change_identity(mac, monkeypatch):
    start = detection.codex_process_identity(42)
    for seconds in (0, 2000000000, 1):
        monkeypatch.setattr(time, "time", lambda: seconds)
        assert detection.codex_process_identity(42) == start
        assert detection.codex_process_state(42, start) == "alive"


@pytest.mark.parametrize("arch", ["x86_64", "arm64"])
def test_macos_literal_lp64_offsets(arch):
    # user64_kinfo_proc, sysctl.h:1133–1157; extern_proc, proc.h:98–152.
    abi = detection._codex_macos_abi(arch)
    proc, extra = [kind for _, kind in abi._fields_]
    start = dict(proc._fields_)["p_un"]
    credentials = dict(extra._fields_)["e_ucred"]
    assert proc.p_un.offset + getattr(start, "__p_starttime").offset == 0
    assert proc.p_pid.offset == 40
    assert proc.p_comm.offset == 243
    assert abi.kp_eproc.offset + extra.e_ppid.offset == 560
    assert abi.kp_eproc.offset + extra.e_ucred.offset == 416
    assert abi.kp_eproc.offset + extra.e_ucred.offset + credentials.cr_uid.offset == 420
    assert ctypes.sizeof(dict(extra._fields_)["e_jobc"]) == 4
    assert abi.kp_eproc.offset + extra.e_jobc.offset == 568
    assert ctypes.sizeof(abi) == 648


def test_macos_named_mib_constants():
    assert (detection.CTL_KERN, detection.KERN_ARGMAX, detection.KERN_PROC,
            detection.KERN_PROCARGS2, detection.KERN_PROC_ALL,
            detection.KERN_PROC_PID, detection.P_LP64) == (1, 8, 14, 49, 0, 1, 4)


@pytest.mark.parametrize("env", [b"MALFORMED\0", b"OTHER=unterminated", b"", None])
@pytest.mark.parametrize("related", [False, True])
def test_macos_a8_garbled_omitted_denied_attribution(mac, tmp_path, env, related):
    mac.add(comm=b"codex-helper" if related else b"worker", env=env or b"")
    if env is None:
        mac.overrides[(1, 49, 42)] = errno.EPERM
    result = detection.scan_codex_home(tmp_path, [])
    assert result["state"] == ("uncertain" if related else "quiescent")
    assert result["unreadable"] == 1
    assert bool(result["issues"]) == related


def test_macos_a8_garbled_descendant_attribution(mac, tmp_path):
    mac.add(42, env=b"CODEX_HOME=" + os.fsencode(tmp_path) + b"\0")
    mac.add(43, parent=42, env=b"rewritten title without equals\0")
    result = detection.scan_codex_home(tmp_path, [])
    assert result["state"] == "live"
    assert result["unreadable"] == 1
    assert result["issues"] == ["PID 43: environment unreadable"]


@pytest.mark.parametrize("tail", [b"CODEX_HOME", b"CODEX_HOME=/truncated"])
def test_macos_a8_truncated_home_is_always_issue(mac, tmp_path, tail):
    mac.add(env=b"OTHER=value\0" + tail)
    result = detection.scan_codex_home(tmp_path, [])
    assert result["state"] == "uncertain" and result["unreadable"] == 1
    assert result["issues"] == ["PID 42: truncated CODEX_HOME"]


@pytest.mark.parametrize("pointer_size", [4, 8])
@pytest.mark.parametrize("argv", [(b"", b"x"), (b"", b""), (b"x", b"")])
def test_macos_empty_argv_strings_preserve_home(mac, tmp_path, pointer_size, argv):
    env = b"CODEX_HOME=" + os.fsencode(tmp_path) + b"\0"
    mac.environments[42] = procargs(env, argv, pointer_size=pointer_size)
    mac.rows[42].kp_proc.p_flag = 4 if pointer_size == 8 else 0
    assert detection._codex_macos_environment(mac.environments[42], pointer_size) == (env, b"")
    result = detection.scan_codex_home(tmp_path, [])
    assert result["state"] == "live" and result["pids"] == [42]


def test_macos_garbled_argv_tail_retains_home(mac, tmp_path):
    mac.environments[42] = (bytes(ctypes.c_int32(-1)) + b"/exec\0\0x\0CODEX_HOME="
                            + os.fsencode(tmp_path) + b"\0")
    result = detection.scan_codex_home(tmp_path, [])
    assert result["state"] == "live" and result["pids"] == [42]


def apple_procargs(env, *, aligned=False, apple=b"pfz=0x123\0ptr_munge=0x42\0"):
    # kern_exec.c:6906–6919: only alignment bytes separate env[] from applev[].
    for length in range(8):
        candidate = b"PAD=" + b"x" * length + b"\0" + env
        data = procargs(candidate)
        padding = -(len(data) - 4 + len(b"executable_path=")) % 8
        if (padding == 0) == aligned:
            return data + b"\0" * padding + apple, candidate
    raise AssertionError("alignment case not constructed")


def test_macos_padded_apple_strings_are_not_environment(mac, tmp_path):
    apple = (b"pfz=0x123\0ptr_munge=0x42\0executable_path=/binary\0not-an-env-entry\0"
             b"CODEX_HOME=" + os.fsencode(tmp_path) + b"\0")
    data, env = apple_procargs(b"OTHER=value\0", apple=apple)
    mac.environments[42] = data
    assert detection._codex_macos_environment(data) == (env, b"")
    result = detection.scan_codex_home(tmp_path, [])
    assert result == {"state": "quiescent", "pids": [], "issues": [], "unreadable": 0}


@pytest.mark.parametrize("related", [False, True])
def test_macos_zero_padding_apple_boundary_is_attributed(mac, tmp_path, related):
    data, env = apple_procargs(b"OTHER=value\0", aligned=True)
    mac.add(comm=b"codex-helper" if related else b"worker")
    mac.environments[42] = data
    parsed, tail = detection._codex_macos_environment(data)
    assert parsed == env and tail.startswith(b"pfz=")
    result = detection.scan_codex_home(tmp_path, [])
    assert result["state"] == ("uncertain" if related else "quiescent")
    assert result["unreadable"] == 1


def test_macos_zero_padding_apple_boundary_retains_possible_home(mac, tmp_path):
    data, _ = apple_procargs(b"CODEX_HOME=" + os.fsencode(tmp_path) + b"\0", aligned=True)
    mac.environments[42] = data
    assert detection.scan_codex_home(tmp_path, [])["pids"] == [42]


def test_macos_missing_enumeration_positive_control_is_uncertain(mac, tmp_path):
    mac.include_self = False
    result = detection.scan_codex_home(tmp_path, [])
    assert result == {"state": "uncertain", "pids": [],
                      "issues": ["process enumeration unavailable"], "unreadable": 0}


@pytest.mark.parametrize("owner", ["real", "effective", "neither"])
def test_macos_real_or_effective_uid_scope(mac, tmp_path, owner):
    other = os.getuid() + 1
    mac.add(uid=os.getuid() if owner == "effective" else other,
            ruid=os.getuid() if owner == "real" else other,
            env=b"CODEX_HOME=" + os.fsencode(tmp_path) + b"\0")
    result = detection.scan_codex_home(tmp_path, [])
    assert result["state"] == ("quiescent" if owner == "neither" else "live")
    if owner == "neither":
        assert not any(key == (1, 49, 42) for key, _, _ in mac.calls)


def test_macos_ambiguous_pfz_tail_cannot_hide_environment_home(mac, tmp_path):
    # The same bytes could be a user-supplied env pfz= entry followed by HOME.
    # Without envc/pointers this cannot be excluded, so keep positive evidence.
    tail = b"pfz=0x123\0CODEX_HOME=" + os.fsencode(tmp_path) + b"\0"
    data, _ = apple_procargs(b"OTHER=value\0", aligned=True, apple=tail)
    mac.environments[42] = data
    result = detection.scan_codex_home(tmp_path, [])
    assert result["state"] == "live" and result["pids"] == [42]
