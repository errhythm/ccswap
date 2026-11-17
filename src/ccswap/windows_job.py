"""Lazy Windows containment; named-job absence needs a proven-gone supervisor."""

from __future__ import annotations

import ctypes
import math
import os
import re
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Literal
from uuid import UUID

DWORD = ctypes.c_uint32
BOOL = ctypes.c_int32
HANDLE = ctypes.c_void_p
SIZE_T = ctypes.c_size_t
ULONG_PTR = ctypes.c_size_t
State = Literal["alive", "gone", "unknown"]
CREATE_SUSPENDED = 0x4
CREATE_UNICODE_ENVIRONMENT = 0x400
EXTENDED_STARTUPINFO_PRESENT = 0x80000
PROC_THREAD_ATTRIBUTE_HANDLE_LIST = 0x20002
PROC_THREAD_ATTRIBUTE_JOB_LIST = 0x2000D
STARTF_USESTDHANDLES = 0x100
DUPLICATE_SAME_ACCESS = 0x2
STD_INPUT_HANDLE = -10
STD_OUTPUT_HANDLE = -11
STD_ERROR_HANDLE = -12
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SYNCHRONIZE = 0x100000
JOB_OBJECT_QUERY = 0x4
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
JobObjectBasicAccountingInformation = 1
JobObjectExtendedLimitInformation = 9
ERROR_FILE_NOT_FOUND = 2
ERROR_INVALID_PARAMETER = 87
ERROR_ALREADY_EXISTS = 183
ERROR_INSUFFICIENT_BUFFER = 122
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 258
INFINITE = 0xFFFFFFFF
TERMINATION_TIMEOUT_MS = 30000
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
CURRENT_PROCESS = HANDLE(-1)


class FILETIME(ctypes.Structure):
    _fields_ = [("low", DWORD), ("high", DWORD)]


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
                ("flags", DWORD), ("minimum", SIZE_T), ("maximum", SIZE_T),
                ("active", DWORD), ("affinity", ULONG_PTR),
                ("priority", DWORD), ("scheduling", DWORD)]


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in
                ("reads", "writes", "other", "read_bytes", "write_bytes", "other_bytes")]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [("basic", JOBOBJECT_BASIC_LIMIT_INFORMATION), ("io", IO_COUNTERS),
                ("process_memory", SIZE_T), ("job_memory", SIZE_T),
                ("peak_process", SIZE_T), ("peak_job", SIZE_T)]


class JOBOBJECT_BASIC_ACCOUNTING_INFORMATION(ctypes.Structure):
    _fields_ = [(name, ctypes.c_int64) for name in
                ("user", "kernel", "period_user", "period_kernel")] + [
                    (name, DWORD) for name in ("faults", "total", "active", "terminated")]


class STARTUPINFOW(ctypes.Structure):
    _fields_ = [("size", DWORD), ("reserved", ctypes.c_wchar_p),
                ("desktop", ctypes.c_wchar_p), ("title", ctypes.c_wchar_p)] + [
        (name, DWORD) for name in
        ("x", "y", "width", "height", "chars_x", "chars_y", "fill", "flags")
    ] + [("show", ctypes.c_uint16), ("reserved_size", ctypes.c_uint16),
         ("reserved_bytes", ctypes.c_void_p), ("stdin", HANDLE), ("stdout", HANDLE),
         ("stderr", HANDLE)]


class STARTUPINFOEXW(ctypes.Structure):
    _fields_ = [("startup", STARTUPINFOW), ("attributes", ctypes.c_void_p)]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [("process", HANDLE), ("thread", HANDLE), ("pid", DWORD), ("tid", DWORD)]


def _kernel32():
    if os.name != "nt":
        raise OSError("Windows APIs unavailable")
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    pointer = ctypes.c_void_p
    text = ctypes.c_wchar_p
    signatures = {
        "OpenProcess": (HANDLE, [DWORD, BOOL, DWORD]),
        "GetProcessTimes": (BOOL, [HANDLE] + [ctypes.POINTER(FILETIME)] * 4),
        "CloseHandle": (BOOL, [HANDLE]),
        "GetStdHandle": (HANDLE, [DWORD]),
        "DuplicateHandle": (BOOL, [HANDLE, HANDLE, HANDLE, ctypes.POINTER(HANDLE),
                                    DWORD, BOOL, DWORD]),
        "CreateJobObjectW": (HANDLE, [pointer, text]),
        "OpenJobObjectW": (HANDLE, [DWORD, BOOL, text]),
        "SetInformationJobObject": (BOOL, [HANDLE, ctypes.c_int, pointer, DWORD]),
        "QueryInformationJobObject": (BOOL, [HANDLE, ctypes.c_int, pointer, DWORD,
                                             ctypes.POINTER(DWORD)]),
        "CreateProcessW": (BOOL, [text, text, pointer, pointer, BOOL, DWORD, pointer, text,
                                  ctypes.POINTER(STARTUPINFOEXW),
                                  ctypes.POINTER(PROCESS_INFORMATION)]),
        "InitializeProcThreadAttributeList": (BOOL, [pointer, DWORD, DWORD,
                                                     ctypes.POINTER(SIZE_T)]),
        "UpdateProcThreadAttribute": (BOOL, [pointer, DWORD, SIZE_T, pointer, SIZE_T,
                                              pointer, pointer]),
        "DeleteProcThreadAttributeList": (None, [pointer]),
        "TerminateJobObject": (BOOL, [HANDLE, DWORD]),
        "ResumeThread": (DWORD, [HANDLE]),
        "TerminateProcess": (BOOL, [HANDLE, DWORD]),
        "WaitForSingleObject": (DWORD, [HANDLE, DWORD]),
        "GetExitCodeProcess": (BOOL, [HANDLE, ctypes.POINTER(DWORD)]),
    }
    for name, (result, args) in signatures.items():
        function = getattr(api, name)
        function.restype, function.argtypes = result, args
    return api


def _error(operation: str) -> OSError:
    return OSError(ctypes.get_last_error(), f"Windows {operation} failed")


def _creation(api, process) -> dict[str, str]:
    stamps = [FILETIME() for _ in range(4)]
    if not api.GetProcessTimes(process, *(ctypes.byref(stamp) for stamp in stamps)):
        raise _error("process identity query")
    return {"kind": "windows", "creationTime100ns": str(stamps[0].high << 32 | stamps[0].low)}


def process_identity(pid: int, *, api=None) -> tuple[State, dict[str, str] | None]:
    """Return exact creation time, proven absence, or uncertainty."""
    if type(pid) is not int or not 0 < pid <= INFINITE:
        return "unknown", None
    try:
        api = api if api is not None else _kernel32()
    except OSError:
        return "unknown", None
    process = api.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE, False, pid)
    if not process:
        return ("gone" if ctypes.get_last_error() == ERROR_INVALID_PARAMETER else "unknown"), None
    state, identity = "unknown", None
    try:
        result = api.WaitForSingleObject(process, 0)
        if result == WAIT_OBJECT_0:
            state = "gone"
        elif result == WAIT_TIMEOUT:
            identity = _creation(api, process)
            state = "alive"
    except OSError:
        pass
    finally:
        if not api.CloseHandle(process):
            state, identity = "unknown", None
    return state, identity


def process_state(pid: int, start: dict[str, str], *, api=None) -> State:
    """Compare a validated identity without mistaking query failures for death."""
    if (not isinstance(start, dict) or set(start) != {"kind", "creationTime100ns"}
            or start["kind"] != "windows" or not isinstance(start["creationTime100ns"], str)
            or re.fullmatch(r"[0-9]+", start["creationTime100ns"]) is None):
        return "unknown"
    state, current = process_identity(pid, api=api)
    return "gone" if state == "alive" and current != start else state


def wait_identity_gone(pid: int, start: dict[str, str], timeout_s: float, *, api=None) -> State:
    """Wait on one verified process handle; timeout is alive, API failures unknown."""
    if (not isinstance(start, dict) or set(start) != {"kind", "creationTime100ns"}
            or start["kind"] != "windows" or not isinstance(start["creationTime100ns"], str)
            or re.fullmatch(r"[0-9]+", start["creationTime100ns"]) is None
            or type(pid) is not int or not 0 < pid <= INFINITE
            or isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float))
            or not math.isfinite(timeout_s) or not 0 <= timeout_s <= (INFINITE - 1) / 1000):
        return "unknown"
    try:
        api = api if api is not None else _kernel32()
        process = api.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE, False, pid)
    except OSError:
        return "unknown"
    if not process:
        return "gone" if ctypes.get_last_error() == ERROR_INVALID_PARAMETER else "unknown"
    state: State = "unknown"
    try:
        if _creation(api, process) != start:
            state = "gone"
        else:
            result = api.WaitForSingleObject(process, math.ceil(timeout_s * 1000))
            if result == WAIT_OBJECT_0:
                state = "gone"
            elif result == WAIT_TIMEOUT:
                state = "alive"
    except OSError:
        pass
    finally:
        if not api.CloseHandle(process):
            state = "unknown"
    return state


def job_name(run_id: str) -> str:
    """Return the job name for a canonical UUID4 run identifier."""
    value = UUID(run_id)
    if value.version != 4 or str(value) != run_id:
        raise ValueError("Invalid run identifier")
    return f"Local\\ccswap-codex-{run_id}"


def _active(api, job) -> int:
    accounting, size = JOBOBJECT_BASIC_ACCOUNTING_INFORMATION(), DWORD()
    if not api.QueryInformationJobObject(
        job, JobObjectBasicAccountingInformation, ctypes.byref(accounting),
        ctypes.sizeof(accounting), ctypes.byref(size),
    ):
        raise _error("job query")
    if size.value != ctypes.sizeof(accounting):
        raise OSError("Incomplete Windows job query")
    return accounting.active


def query_job(run_id: str, *, api=None) -> State:
    """Query a validated run and close its query handle before deciding custody.

    A6: quiescence requires job gone AND supervisor identity gone AND recorded
    child identity gone, checked within a bounded deadline; timeout is uncertain.
    "gone" (name absent) alone is insufficient: termination is asynchronous.
    The Local\\ namespace is per logon session. Unrecorded grandchildren are
    covered by kernel termination: no further user-mode code runs once initiated.
    """
    name = job_name(run_id)
    try:
        api = api if api is not None else _kernel32()
    except OSError:
        return "unknown"
    job = api.OpenJobObjectW(JOB_OBJECT_QUERY, False, name)
    if not job:
        return "gone" if ctypes.get_last_error() == ERROR_FILE_NOT_FOUND else "unknown"
    state: State = "unknown"
    try:
        state = "alive" if _active(api, job) else "gone"
    except OSError:
        pass
    finally:
        if not api.CloseHandle(job):
            state = "unknown"
    return state


def _terminate_job(api, handle) -> None:
    if not api.TerminateJobObject(handle, 1):
        raise _error("job termination")
    deadline = time.monotonic() + 30
    while _active(api, handle):
        if time.monotonic() >= deadline:
            raise OSError("Windows job termination timed out; handle retained")
        time.sleep(0.02)


class Job:
    """Own containment until wait completes or close kills descendants."""

    def __init__(self, api, handle, process, pid: int):
        self.api, self.handle, self.process, self.pid = api, handle, process, pid

    def wait(self) -> int:
        """Wait for every descendant and return the initial child's exit code."""
        while _active(self.api, self.handle):
            time.sleep(0.02)
        if self.api.WaitForSingleObject(self.process, INFINITE) != WAIT_OBJECT_0:
            raise _error("child wait")
        code = DWORD()
        if not self.api.GetExitCodeProcess(self.process, ctypes.byref(code)):
            raise _error("child exit query")
        return code.value

    def close(self) -> None:
        """Close owned handles, retaining failed handles so closing can be retried."""
        if self.handle:
            _terminate_job(self.api, self.handle)
        failed = False
        for attribute in ("handle", "process"):
            handle = getattr(self, attribute)
            if handle:
                if self.api.CloseHandle(handle):
                    setattr(self, attribute, None)
                else:
                    failed = True
        if failed:
            raise _error("handle close")

    def __enter__(self) -> Job:
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def launch(executable: str, args: Sequence[str], env: Mapping[str, str], cwd: str,
           run_id: str, record_child: Callable[[int, dict[str, str]], None], *, api=None) -> Job:
    """Assign a suspended child, durably record via callback, then resume it.
    """
    name = job_name(run_id)
    if (re.match(r"^(?:\\\\\?\\)?[A-Za-z]:\\", executable) is None
            or not executable.lower().endswith(".exe")
            or not os.path.isfile(executable)):
        raise ValueError("An absolute native executable is required")
    if any("\0" in arg for arg in [executable, cwd, *args]):
        raise ValueError("Invalid child arguments")
    if len({key.casefold() for key in env}) != len(env):
        raise ValueError("Ambiguous child environment")
    if any(not key or "=" in key or "\0" in key or "\0" in value for key, value in env.items()):
        raise ValueError("Invalid child environment")
    api = api if api is not None else _kernel32()
    ctypes.set_last_error(0)
    job = api.CreateJobObjectW(None, name)  # NULL security attributes: noninherited.
    if not job:
        raise _error("job creation")
    child, extended = PROCESS_INFORMATION(), STARTUPINFOEXW()
    startup = extended.startup
    startup.size = ctypes.sizeof(extended)
    startup.flags = STARTF_USESTDHANDLES
    duplicates = []
    attributes = None
    try:
        if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
            raise OSError("Windows job name already exists")
        limits = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        limits.basic.flags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not api.SetInformationJobObject(
            job, JobObjectExtendedLimitInformation, ctypes.byref(limits), ctypes.sizeof(limits),
        ):
            raise _error("job limits")
        for attribute, selector in zip(
            ("stdin", "stdout", "stderr"), (STD_INPUT_HANDLE, STD_OUTPUT_HANDLE, STD_ERROR_HANDLE),
        ):
            source, duplicate = api.GetStdHandle(selector), HANDLE()
            if not source or source == INVALID_HANDLE_VALUE:
                raise _error("standard handle query")
            if not api.DuplicateHandle(CURRENT_PROCESS, source, CURRENT_PROCESS,
                                       ctypes.byref(duplicate), 0, True, DUPLICATE_SAME_ACCESS):
                raise _error("standard handle duplication")
            duplicates.append(duplicate.value)
            setattr(startup, attribute, duplicate.value)
        size = SIZE_T()
        if (api.InitializeProcThreadAttributeList(None, 2, 0, ctypes.byref(size))
                or ctypes.get_last_error() != ERROR_INSUFFICIENT_BUFFER or not size.value):
            raise _error("attribute list sizing")
        buffer = ctypes.create_string_buffer(size.value)
        if not api.InitializeProcThreadAttributeList(buffer, 2, 0, ctypes.byref(size)):
            raise _error("attribute list initialization")
        attributes = buffer
        extended.attributes = ctypes.addressof(buffer)
        handles = (HANDLE * 3)(*duplicates)
        jobs = (HANDLE * 1)(job)
        for key, values in ((PROC_THREAD_ATTRIBUTE_HANDLE_LIST, handles),
                            (PROC_THREAD_ATTRIBUTE_JOB_LIST, jobs)):
            if not api.UpdateProcThreadAttribute(buffer, 0, key, values,
                                                  ctypes.sizeof(values), None, None):
                raise _error("containment attribute update")
        command = ctypes.create_unicode_buffer(subprocess.list2cmdline([executable, *args]))
        entries = sorted(env.items(), key=lambda item: item[0].casefold())
        environment = ctypes.create_unicode_buffer(
            "\0".join(f"{key}={value}" for key, value in entries) + "\0\0")
        flags = CREATE_SUSPENDED | CREATE_UNICODE_ENVIRONMENT | EXTENDED_STARTUPINFO_PRESENT
        if not api.CreateProcessW(executable, command, None, None, True, flags,
                                  environment, cwd, ctypes.byref(extended), ctypes.byref(child)):
            raise _error("suspended child creation")
        while duplicates:
            if not api.CloseHandle(duplicates[-1]):
                raise _error("standard handle close")
            duplicates.pop()
        record_child(child.pid, _creation(api, child.process))
        if api.ResumeThread(child.thread) != 1:
            raise _error("child resume")
        if not api.CloseHandle(child.thread):
            raise _error("thread close")
        child.thread = None
        return Job(api, job, child.process, child.pid)
    except BaseException:
        failed = False
        if child.process:
            terminated = api.TerminateProcess(child.process, 1)
            timeout = TERMINATION_TIMEOUT_MS if terminated else 0
            if api.WaitForSingleObject(child.process, timeout) != WAIT_OBJECT_0:
                failed = True
            try:
                _terminate_job(api, job)
            except OSError:
                for handle in (*duplicates, child.thread):
                    if handle:
                        api.CloseHandle(handle)
                retained = Job(api, job, child.process, child.pid)
                error = _error("launch cleanup; job handle retained")
                error.job = retained
                raise error
        for handle in (*duplicates, child.thread, child.process, job):
            if handle and not api.CloseHandle(handle):
                failed = True
        if failed:
            raise _error("launch cleanup")
        raise
    finally:
        if attributes is not None:
            api.DeleteProcThreadAttributeList(attributes)
