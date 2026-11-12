"""Containment ordering, failure handling and native Windows release gates."""

import ctypes
import ntpath
import os
import subprocess
import sys
import time
from textwrap import dedent
from uuid import uuid4

import pytest

from claude_swap import windows_job as w

RUN = str(uuid4())


class Kernel:
    def __init__(self, fail=None, error=5):
        self.fail, self.error, self.calls = fail, error, []
        self.closed = []
        self.counts = [1, 0]
        self.duplicates = []
        self.attributes = {}
        self.last_error_cleared = False

    def __getattr__(self, name):
        def call(*args):
            self.calls.append((name, args))
            if name == self.fail:
                return 0xFFFFFFFF if name == "ResumeThread" else 0
            if name == "InitializeProcThreadAttributeList":
                args[3]._obj.value = 128
                if args[0] is None:
                    self.error = w.ERROR_INSUFFICIENT_BUFFER
                    return 0
            if name == "UpdateProcThreadAttribute":
                self.attributes[args[2]] = list(args[3])
            if name == "CreateJobObjectW":
                assert self.last_error_cleared
                assert args[0] is None
                assert args[1] == w.job_name(RUN)
                return 0x123456789
            if name == "SetInformationJobObject":
                assert args[1] == 9
                assert args[2]._obj.basic.flags == 0x2000
            if name == "GetStdHandle":
                return 200 - args[0]
            if name == "DuplicateHandle":
                assert args[4:] == (0, True, w.DUPLICATE_SAME_ACCESS)
                value = 100 + len(self.duplicates)
                self.duplicates.append(value)
                args[3]._obj.value = value
            if name == "CreateProcessW":
                assert args[4] is True
                assert args[5] == (w.CREATE_SUSPENDED | w.CREATE_UNICODE_ENVIRONMENT
                                   | w.EXTENDED_STARTUPINFO_PRESENT)
                assert args[2] is None and args[3] is None
                assert args[8]._obj.startup.flags == w.STARTF_USESTDHANDLES
                startup = args[8]._obj.startup
                assert [startup.stdin, startup.stdout, startup.stderr] == self.duplicates
                assert 0x123456789 not in self.duplicates
                assert self.attributes == {
                    w.PROC_THREAD_ATTRIBUTE_HANDLE_LIST: self.duplicates,
                    w.PROC_THREAD_ATTRIBUTE_JOB_LIST: [0x123456789],
                }
                assert args[8]._obj.startup.size == ctypes.sizeof(w.STARTUPINFOEXW)
                assert args[1].value == subprocess.list2cmdline([args[0], "a b", "x\"y"])
                assert args[6][:].endswith("\u0000\u0000")
                child = args[-1]._obj
                child.process, child.thread, child.pid = 22, 33, 44
            if name == "GetProcessTimes":
                args[1]._obj.low, args[1]._obj.high = 7, 2
            if name == "QueryInformationJobObject":
                args[2]._obj.active = self.counts.pop(0) if self.counts else 0
                size = ctypes.sizeof(w.JOBOBJECT_BASIC_ACCOUNTING_INFORMATION)
                args[4]._obj.value = 0 if self.fail == "short_query" else size
            if name == "CloseHandle":
                self.closed.append(args[0])
            if name == "GetExitCodeProcess":
                args[1]._obj.value = 0xC0000005
            if name == "OpenJobObjectW":
                assert args[:2] == (4, False)
            if name == "WaitForSingleObject":
                if self.fail == "exited":
                    return 0
                return 0xFFFFFFFF if self.fail == "wait" else (258 if args[1] == 0 else 0)
            return 1
        return call


@pytest.fixture
def api(monkeypatch):
    kernel = Kernel()
    monkeypatch.setattr(ctypes, "get_last_error", lambda: kernel.error, raising=False)
    monkeypatch.setattr(
        ctypes, "set_last_error",
        lambda value: setattr(kernel, "last_error_cleared", value == 0), raising=False,
    )
    return kernel


@pytest.fixture
def executable(tmp_path, monkeypatch):
    path = tmp_path / "codex.exe"
    path.touch()
    monkeypatch.setattr(w.os.path, "isfile", lambda path: True)
    return "C:\\test\\codex.exe"


def launch(api, executable, callback=lambda *_: None):
    return w.launch(executable, ["a b", "x\"y"], {"CODEX_HOME": "private home"},
                    os.path.dirname(executable), RUN, callback, api=api)


def test_order_and_wait(api, executable):
    def record(pid, start):
        assert pid == 44 and start == {"kind": "windows", "creationTime100ns": str((2 << 32) + 7)}
        assert api.closed == [102, 101, 100]
        api.calls.append(("record", ()))
    with launch(api, executable, record) as job:
        assert job.wait() == 0xC0000005
    names = [name for name, _ in api.calls]
    assert names.index("CreateProcessW") < names.index("record")
    assert names.index("record") < names.index("ResumeThread")
    assert names.count("QueryInformationJobObject") == 3
    assert api.closed == [102, 101, 100, 33, 0x123456789, 22]


@pytest.mark.parametrize("failure,child_created", [
    ("CreateJobObjectW", False), ("SetInformationJobObject", False),
    ("GetStdHandle", False), ("DuplicateHandle", False),
    ("CreateProcessW", False), ("UpdateProcThreadAttribute", False),
    ("GetProcessTimes", True), ("ResumeThread", True), ("CloseHandle", True),
])
def test_launch_failure(api, executable, failure, child_created):
    api.fail = failure
    with pytest.raises(OSError):
        launch(api, executable)
    names = [name for name, _ in api.calls]
    assert ("TerminateProcess" in names) == child_created
    if failure != "CloseHandle":
        expected = {22, 33, 0x123456789} if child_created else {0x123456789}
        if failure == "CreateJobObjectW":
            expected.clear()
        assert set(api.closed) == expected | set(api.duplicates)
    if failure in ("UpdateProcThreadAttribute", "GetProcessTimes"):
        assert "ResumeThread" not in names


def test_record_failure_never_resumes(api, executable):
    def record(*_):
        raise RuntimeError("persistence failed")
    with pytest.raises(RuntimeError):
        launch(api, executable, record)
    assert "ResumeThread" not in [name for name, _ in api.calls]
    assert set(api.closed) == {22, 33, 0x123456789, 100, 101, 102}


@pytest.mark.parametrize("cleanup_failure", ["TerminateProcess", "wait", "CloseHandle"])
def test_cleanup_failures_are_reported(api, executable, cleanup_failure):
    def record(*_):
        api.fail = cleanup_failure
        raise RuntimeError("record failed")
    with pytest.raises(OSError, match="cleanup"):
        launch(api, executable, record)
    assert "CloseHandle" in [name for name, _ in api.calls]


def test_existing_job_refused(api, executable):
    api.error = 183
    with pytest.raises(OSError, match="already exists"):
        launch(api, executable)
    assert api.closed == [0x123456789]
    assert "CreateProcessW" not in [name for name, _ in api.calls]


@pytest.mark.parametrize("operation", ["GetStdHandle", "DuplicateHandle"])
@pytest.mark.parametrize("index", [1, 2])
def test_partial_standard_handle_failure(api, executable, monkeypatch, operation, index):
    original = getattr(api, operation)
    count = 0

    def fail_after_partial_copy(*args):
        nonlocal count
        count += 1
        if count == index + 1:
            return 0
        return original(*args)

    monkeypatch.setattr(api, operation, fail_after_partial_copy)
    with pytest.raises(OSError):
        launch(api, executable)
    assert set(api.closed) == {0x123456789, *api.duplicates}
    assert len(api.duplicates) == index
    assert "CreateProcessW" not in [name for name, _ in api.calls]


def test_invalid_standard_handle(api, executable, monkeypatch):
    monkeypatch.setattr(api, "GetStdHandle", lambda *_: w.INVALID_HANDLE_VALUE)
    with pytest.raises(OSError, match="standard handle query"):
        launch(api, executable)
    assert api.closed == [0x123456789]


@pytest.mark.parametrize("failure,error,expected", [
    (None, 0, "alive"), ("OpenProcess", 87, "gone"), ("OpenProcess", 5, "unknown"),
    ("GetProcessTimes", 5, "unknown"), ("CloseHandle", 5, "unknown"),
    ("wait", 5, "unknown"), ("exited", 0, "gone"),
])
def test_process_identity(api, failure, error, expected):
    api.fail, api.error = failure, error
    state, start = w.process_identity(123, api=api)
    assert state == expected
    if expected == "alive":
        assert w.process_state(123, start, api=api) == "alive"
        reused = {"kind": "windows", "creationTime100ns": "8"}
        assert w.process_state(123, reused, api=api) == "gone"
    else:
        assert start is None
    assert ("CloseHandle" in [name for name, _ in api.calls]) == (failure != "OpenProcess")


@pytest.mark.parametrize("pid", [0, -1, True, 2**32])
def test_invalid_pid_is_unknown(api, pid):
    assert w.process_identity(pid, api=api) == ("unknown", None)
    assert not api.calls


@pytest.mark.parametrize("failure,error,count,expected", [
    (None, 0, 1, "alive"), (None, 0, 0, "gone"),
    ("OpenJobObjectW", 2, 0, "gone"), ("OpenJobObjectW", 5, 0, "unknown"),
    ("OpenJobObjectW", 6, 0, "unknown"), ("QueryInformationJobObject", 5, 0, "unknown"),
    ("short_query", 0, 0, "unknown"), ("CloseHandle", 5, 0, "unknown"),
])
def test_named_query(api, failure, error, count, expected):
    api.fail, api.error, api.counts = failure, error, [count]
    assert w.query_job(RUN, api=api) == expected
    assert ("CloseHandle" in [name for name, _ in api.calls]) == (failure != "OpenJobObjectW")


@pytest.mark.parametrize("failure", [
    "QueryInformationJobObject", "short_query", "wait", "GetExitCodeProcess",
])
def test_wait_failure_retains_handle(api, executable, failure):
    job = launch(api, executable)
    api.fail, api.counts = failure, [0]
    with pytest.raises(OSError):
        job.wait()
    api.fail = None
    job.close()
    assert set(api.closed) == {22, 33, 0x123456789, 100, 101, 102}


def test_close_can_retry(api, executable):
    job = launch(api, executable)
    api.fail = "CloseHandle"
    with pytest.raises(OSError):
        job.close()
    api.fail = None
    job.close()
    before = len(api.closed)
    job.close()
    assert len(api.closed) == before


@pytest.mark.parametrize("run_id", [
    "bad", "A1234567-1234-4123-8123-123456789ABC", "00000000-0000-0000-0000-000000000000",
])
def test_invalid_job_name(api, run_id):
    with pytest.raises(ValueError):
        w.query_job(run_id, api=api)
    assert not api.calls


def test_invalid_launch_inputs(api, executable):
    for path, env in [("C:\\test\\codex.bat", {}), ("C:\\test\\codex.cmd", {}),
                      (executable, {"A=B": "value"}), (executable, {"A": "\u0000"}),
                      (executable, {"a": "1", "A": "2"})]:
        with pytest.raises(ValueError):
            w.launch(path, [], env, ".", RUN, lambda *_: None, api=api)
    assert not api.calls


def test_import_does_not_load_dll(monkeypatch):
    import importlib
    monkeypatch.setattr(
        ctypes, "WinDLL", lambda *_a, **_k: pytest.fail("DLL loaded on import"), raising=False,
    )
    importlib.reload(w)


def test_typed_api_and_layout(monkeypatch):
    from types import SimpleNamespace
    functions = {}
    class Library:
        def __getattr__(self, name):
            functions[name] = lambda: None
            return functions[name]
    monkeypatch.setattr(w, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(ctypes, "WinDLL", lambda *a, **k: Library(), raising=False)
    w._kernel32()
    assert len(functions) == 18
    assert all(function.argtypes for function in functions.values())
    assert functions["OpenProcess"].restype is ctypes.c_void_p
    assert functions["CreateProcessW"].argtypes[5] is ctypes.c_uint32
    assert ctypes.sizeof(w.DWORD) == 4 and ctypes.sizeof(w.FILETIME) == 8
    assert ctypes.sizeof(w.JOBOBJECT_BASIC_ACCOUNTING_INFORMATION) == 48
    wide = ctypes.sizeof(ctypes.c_void_p) == 8
    assert ctypes.sizeof(w.JOBOBJECT_EXTENDED_LIMIT_INFORMATION) == (144 if wide else 112)
    assert ctypes.sizeof(w.STARTUPINFOW) == (104 if wide else 68)


def test_unavailable_apis_are_unknown(monkeypatch):
    def unavailable():
        raise OSError("unavailable")
    monkeypatch.setattr(w, "_kernel32", unavailable)
    assert w.process_identity(123) == ("unknown", None)
    assert w.query_job(RUN) == "unknown"


def test_named_query_termination_race(api):
    api.counts = [1, 0]
    assert w.query_job(RUN, api=api) == "alive"
    assert api.closed == [1]
    assert w.query_job(RUN, api=api) == "gone"
    assert api.closed == [1, 1]


@pytest.mark.skipif(sys.platform != "win32", reason="native Windows redirection gate")
def test_native_redirected_stdout(tmp_path):
    run_id = str(uuid4())
    supervisor = dedent(f"""
        import os
        import sys
        from claude_swap.windows_job import launch

        with launch(sys.executable, ["-c", "print('redirected child')"], dict(os.environ),
                    {str(tmp_path)!r}, {run_id!r}, lambda *_: None) as job:
            sys.exit(job.wait())
    """)
    output = tmp_path / "stdout.txt"
    env = dict(os.environ, PYTHONPATH=os.path.dirname(os.path.dirname(w.__file__)))
    with output.open("wb") as stream:
        result = subprocess.run(
            [sys.executable, "-c", supervisor], env=env, stdin=subprocess.DEVNULL,
            stdout=stream, stderr=subprocess.PIPE, timeout=15,
        )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    assert output.read_text().strip() == "redirected child"


@pytest.mark.skipif(sys.platform != "win32", reason="native Windows containment gate")
def test_native_wait_for_grandchild(tmp_path):
    signal = tmp_path / "done"
    grandchild = f"import time,pathlib;time.sleep(0.4);pathlib.Path({str(signal)!r}).touch()"
    child = f"import subprocess,sys;subprocess.Popen([sys.executable,'-c',{grandchild!r}])"
    with w.launch(sys.executable, ["-c", child], dict(os.environ), str(tmp_path),
                  str(uuid4()), lambda *_: None) as job:
        assert job.wait() == 0
        assert signal.exists()


@pytest.mark.skipif(sys.platform != "win32", reason="native Windows containment gate")
@pytest.mark.parametrize("parent_death", [False, True])
def test_native_kill_completion(tmp_path, parent_death):
    import json
    marker = tmp_path / "identities.json"
    run_id = str(uuid4())
    child_script = tmp_path / "child.py"
    child_script.write_text(dedent(f"""
        import json
        import os
        import pathlib
        import subprocess
        import sys
        import time
        from claude_swap.windows_job import process_identity

        grandchild = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(60)"])
        identities = [(pid, process_identity(pid)[1]) for pid in (grandchild.pid, os.getpid())]
        path = pathlib.Path({str(marker)!r})
        path.with_suffix(".tmp").write_text(json.dumps(identities))
        path.with_suffix(".tmp").replace(path)
        time.sleep(60)
    """))
    supervisor = dedent(f"""
        import os
        import time
        from claude_swap.windows_job import launch

        job = launch({sys.executable!r}, [{str(child_script)!r}], dict(os.environ),
                     {str(tmp_path)!r}, {run_id!r}, lambda *_: None)
        time.sleep(60)
    """)
    env = dict(os.environ, PYTHONPATH=os.path.dirname(os.path.dirname(w.__file__)))
    owner = None
    job = None
    try:
        if parent_death:
            owner = subprocess.Popen([sys.executable, "-c", supervisor], env=env)
        else:
            job = w.launch(
                sys.executable, [str(child_script)], env, str(tmp_path), run_id, lambda *_: None,
            )
        deadline = time.monotonic() + 15
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        identities = json.loads(marker.read_text())
        assert w.query_job(run_id) == "alive"
        if owner:
            supervisor_start = w.process_identity(owner.pid)[1]
            identities.append((owner.pid, supervisor_start))
            owner.kill()
            owner.wait(timeout=10)
        else:
            job.close()
        deadline = time.monotonic() + 10
        while w.query_job(run_id) != "gone" and time.monotonic() < deadline:
            time.sleep(0.02)
        assert w.query_job(run_id) == "gone"
        for pid, start in identities:
            assert w.wait_identity_gone(pid, start, max(0, deadline - time.monotonic())) == "gone"
    finally:
        if owner and owner.poll() is None:
            owner.kill()
            owner.wait(timeout=10)
        if job:
            job.close()


@pytest.mark.parametrize("start", [None, {}, {"kind": "linux", "creationTime100ns": "1"},
    {"kind": "windows", "creationTime100ns": 1},
    {"kind": "windows", "creationTime100ns": "١"},
    {"kind": "windows", "creationTime100ns": ""},
    {"kind": "windows", "creationTime100ns": "1", "extra": True}])
def test_invalid_start_is_unknown(api, start):
    assert w.process_state(123, start, api=api) == "unknown"
    assert not api.calls


@pytest.mark.parametrize("path", ["\\\\server\\share\\codex.exe", "\\codex.exe",
    "C:codex.exe", "\\\\?\\UNC\\server\\share\\codex.exe"])
def test_reject_non_drive_absolute(api, executable, path):
    with pytest.raises(ValueError):
        w.launch(path, [], {}, ".", RUN, lambda *_: None, api=api)
    assert not api.calls


def test_extended_drive_path(api, executable):
    job = launch(api, "\\\\?\\C:\\test\\codex.exe")
    job.close()


@pytest.mark.parametrize("key", [w.PROC_THREAD_ATTRIBUTE_JOB_LIST,
                                  w.PROC_THREAD_ATTRIBUTE_HANDLE_LIST])
def test_unsupported_attribute_refuses(api, executable, monkeypatch, key):
    original = api.UpdateProcThreadAttribute
    monkeypatch.setattr(api, "UpdateProcThreadAttribute",
                        lambda *args: 0 if args[2] == key else original(*args))
    with pytest.raises(OSError):
        launch(api, executable)
    names = [name for name, _ in api.calls]
    assert "CreateProcessW" not in names
    assert "DeleteProcThreadAttributeList" in names
    assert set(api.closed) == {0x123456789, 100, 101, 102}


@pytest.mark.parametrize("phase", ["sizing", "initialize"])
def test_attribute_initialization_failure(api, executable, monkeypatch, phase):
    original = api.InitializeProcThreadAttributeList
    def initialize(*args):
        if phase == "sizing" or args[0] is not None:
            return 0
        return original(*args)
    monkeypatch.setattr(api, "InitializeProcThreadAttributeList", initialize)
    with pytest.raises(OSError):
        launch(api, executable)
    assert "CreateProcessW" not in [name for name, _ in api.calls]
    assert set(api.closed) == {0x123456789, 100, 101, 102}


def test_resume_two_fails_closed(api, executable, monkeypatch):
    monkeypatch.setattr(api, "ResumeThread", lambda *_: 2)
    with pytest.raises(OSError):
        launch(api, executable)
    names = [name for name, _ in api.calls]
    assert names.index("TerminateProcess") < names.index("WaitForSingleObject")
    assert names.index("TerminateJobObject") < names.index("QueryInformationJobObject")
    job_close = next(i for i, (name, args) in enumerate(api.calls)
                     if name == "CloseHandle" and args[0] == 0x123456789)
    assert names.index("QueryInformationJobObject") < job_close


def test_terminate_already_exited_is_not_cleanup_failure(api, executable, monkeypatch):
    monkeypatch.setattr(api, "TerminateProcess", lambda *_: 0)
    monkeypatch.setattr(api, "WaitForSingleObject", lambda *_: w.WAIT_OBJECT_0)
    def record(*_):
        raise RuntimeError("record failed")
    with pytest.raises(RuntimeError, match="record failed"):
        launch(api, executable, record)


@pytest.mark.parametrize("failure", ["TerminateJobObject", "QueryInformationJobObject", "timeout"])
def test_close_retains_job_on_uncertain_termination(api, executable, monkeypatch, failure):
    job = launch(api, executable)
    if failure == "timeout":
        api.counts = [1]
        ticks = iter([0, 31])
        monkeypatch.setattr(w.time, "monotonic", lambda: next(ticks))
    else:
        api.fail = failure
    with pytest.raises(OSError):
        job.close()
    assert job.handle == 0x123456789
    assert job.handle not in api.closed
    api.fail = None
    api.counts = [0]
    monkeypatch.setattr(w.time, "monotonic", lambda: 0)
    job.close()


def test_launch_retains_job_on_cleanup_timeout(api, executable, monkeypatch):
    def record(*_):
        api.fail = "TerminateJobObject"
        raise RuntimeError("record failed")
    with pytest.raises(OSError) as caught:
        launch(api, executable, record)
    retained = caught.value.job
    assert retained.handle not in api.closed
    api.fail = None
    retained.close()


@pytest.mark.parametrize("environment", [{}, {"CODEX_HOME": "私有 空間", "Z": "ä"}])
def test_environment_block(api, executable, monkeypatch, environment):
    original = api.CreateProcessW
    def create(*args):
        expected = "\0".join(f"{key}={value}" for key, value in sorted(environment.items()))
        assert args[6][:] == expected + "\0\0\0"
        return original(*args)
    monkeypatch.setattr(api, "CreateProcessW", create)
    with w.launch(executable, ["a b", "x\"y"], environment, ".", RUN, lambda *_: None, api=api):
        pass


@pytest.mark.skipif(sys.platform != "win32", reason="native handle inheritance gate")
def test_native_extra_inheritable_handle_is_excluded(tmp_path):
    import msvcrt
    output = tmp_path / "result.txt"
    child = tmp_path / "probe.py"
    child.write_text(dedent("""
        import ctypes
        import sys
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.GetHandleInformation.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
        api.GetHandleInformation.restype = ctypes.c_int32
        api.GetFileType.argtypes = [ctypes.c_void_p]
        api.GetFinalPathNameByHandleW.argtypes = [
            ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32]
        handle = int(sys.argv[1])
        flags = ctypes.c_uint32()
        # Handle values are per process: the child may own an unrelated handle
        # with the same number, so only a handle to extra.txt is a leak.
        if api.GetHandleInformation(handle, ctypes.byref(flags)) and api.GetFileType(handle) == 1:
            path = ctypes.create_unicode_buffer(1024)
            assert not (api.GetFinalPathNameByHandleW(handle, path, 1024, 0)
                        and path.value.lower().endswith("\\\\extra.txt")), path.value
        print("excluded")
    """))
    with (tmp_path / "extra.txt").open("wb") as extra:
        handle = msvcrt.get_osfhandle(extra.fileno())
        os.set_handle_inheritable(handle, True)
        supervisor = dedent(f"""
            import os
            import sys
            os.get_handle_inheritable({handle})
            from claude_swap.windows_job import launch
            with launch(sys.executable, [{str(child)!r}, {str(handle)!r}], dict(os.environ),
                        {str(tmp_path)!r}, {str(uuid4())!r}, lambda *_: None) as job:
                sys.exit(job.wait())
        """)
        env = dict(os.environ, PYTHONPATH=os.path.dirname(os.path.dirname(w.__file__)))
        with output.open("wb") as stream:
            result = subprocess.run([sys.executable, "-c", supervisor], env=env,
                                    close_fds=False, stdout=stream, stdin=subprocess.DEVNULL,
                                    stderr=subprocess.PIPE, timeout=15)
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    assert output.read_text().strip() == "excluded"


@pytest.mark.parametrize("result,expected", [
    (w.WAIT_OBJECT_0, "gone"), (w.WAIT_TIMEOUT, "alive"), (0xFFFFFFFF, "unknown"),
])
def test_wait_identity_gone(api, monkeypatch, result, expected):
    start = {"kind": "windows", "creationTime100ns": str((2 << 32) + 7)}
    def wait(handle, timeout):
        assert handle == 1
        assert timeout == 1250
        assert api.calls[-1][0] == "GetProcessTimes"
        return result
    monkeypatch.setattr(api, "WaitForSingleObject", wait)
    assert w.wait_identity_gone(123, start, 1.25, api=api) == expected
    assert api.calls[0] == (
        "OpenProcess", (w.PROCESS_QUERY_LIMITED_INFORMATION | w.SYNCHRONIZE, False, 123),
    )
    assert api.closed == [1]


def test_wait_identity_mismatch_is_gone(api):
    start = {"kind": "windows", "creationTime100ns": "8"}
    assert w.wait_identity_gone(123, start, 10, api=api) == "gone"
    assert "WaitForSingleObject" not in [name for name, _ in api.calls]
    assert api.closed == [1]


@pytest.mark.parametrize("failure,error,expected", [
    ("OpenProcess", 5, "unknown"), ("OpenProcess", 87, "gone"),
    ("GetProcessTimes", 5, "unknown"), ("CloseHandle", 5, "unknown"),
])
def test_wait_identity_failures(api, failure, error, expected):
    api.fail, api.error = failure, error
    start = {"kind": "windows", "creationTime100ns": str((2 << 32) + 7)}
    assert w.wait_identity_gone(123, start, 0, api=api) == expected
    names = [name for name, _ in api.calls]
    assert ("CloseHandle" in names) == (failure != "OpenProcess")


@pytest.mark.parametrize("timeout", [-1, float("inf"), float("nan"), True, "10", w.INFINITE])
def test_wait_identity_invalid_timeout(api, timeout):
    start = {"kind": "windows", "creationTime100ns": "8"}
    assert w.wait_identity_gone(123, start, timeout, api=api) == "unknown"
    assert not api.calls


def test_wait_identity_invalid_start(api):
    assert w.wait_identity_gone(123, {}, 10, api=api) == "unknown"
    assert not api.calls


def test_wait_identity_unavailable_api(monkeypatch):
    def unavailable():
        raise OSError("unavailable")
    monkeypatch.setattr(w, "_kernel32", unavailable)
    start = {"kind": "windows", "creationTime100ns": "8"}
    assert w.wait_identity_gone(123, start, 10) == "unknown"
