"""Tests for Claude Code process detection."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

import pytest

from ccswap.process_detection import (
    PID_REUSE_SLACK_S,
    ClaudeSession,
    IdeInstance,
    _lstart_seconds,
    _stat_start_ticks,
    get_claude_dir,
    get_running_instances,
    is_codex_running,
    is_pid_alive,
    list_ide_instances,
    list_sessions,
    pid_matches_record,
    process_is_claude,
    process_start_ticks,
    process_started_at,
    scan_sessions,
)
from ccswap import process_detection as detection
from ccswap.printer import abbreviate_path, entrypoint_label, format_age


# --- get_claude_dir ---


class TestGetClaudeDir:
    def test_default_path(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
            result = get_claude_dir()
            assert result == Path.home() / ".claude"

    def test_respects_env_var(self, tmp_path):
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(tmp_path)}):
            assert get_claude_dir() == tmp_path


# --- is_codex_running ---


class TestIsCodexRunning:
    @pytest.mark.parametrize(
        "stdout,expected",
        [
            ("INFORMATION: Es werden keine Aufgaben mit den angegebenen Kriterien ausgeführt.\n".encode("cp850"), False),
            (b"Codex.exe                    1234 Console                    1     10,000 K\n", True),
        ],
        ids=["localized_no_tasks", "codex_running"],
    )
    def test_windows_bytes(self, stdout, expected):
        with patch("ccswap.process_detection.sys.platform", "win32"), \
             patch("ccswap.process_detection.subprocess.run", return_value=CompletedProcess(
                 args=[], returncode=0, stdout=stdout,
             )) as run:
            assert is_codex_running() is expected

        run.assert_called_once_with(
            ["tasklist", "/FI", "IMAGENAME eq codex.exe", "/NH"],
            capture_output=True,
            timeout=5,
        )


# --- is_pid_alive ---


class TestIsPidAlive:
    # The os.kill(pid, 0) semantics below are the POSIX branch; on Windows
    # is_pid_alive() dispatches to _is_pid_alive_windows() and never calls
    # os.kill. Pin the platform so these exercise the intended path on any host.
    def test_alive_pid(self):
        with patch("ccswap.process_detection.sys.platform", "linux"), \
             patch("os.kill") as mock_kill:
            mock_kill.return_value = None
            assert is_pid_alive(12345) is True
            mock_kill.assert_called_once_with(12345, 0)

    def test_dead_pid(self):
        with patch("ccswap.process_detection.sys.platform", "linux"), \
             patch("os.kill", side_effect=OSError("No such process")):
            assert is_pid_alive(12345) is False

    def test_permission_error_means_alive(self):
        with patch("ccswap.process_detection.sys.platform", "linux"), \
             patch("os.kill", side_effect=PermissionError("Operation not permitted")):
            assert is_pid_alive(12345) is True

    def test_windows_dispatches_to_ctypes_impl(self):
        """On win32, is_pid_alive() delegates to the ctypes-based helper."""
        with patch("ccswap.process_detection.sys.platform", "win32"), \
             patch(
                 "ccswap.process_detection._is_pid_alive_windows",
                 return_value=True,
             ) as mock_win:
            assert is_pid_alive(12345) is True
            mock_win.assert_called_once_with(12345)

    def test_current_process_is_alive(self):
        """Smoke test on the real platform branch (win32 or POSIX)."""
        assert is_pid_alive(os.getpid()) is True

    def test_invalid_pid_zero(self):
        assert is_pid_alive(0) is False

    def test_invalid_pid_one(self):
        assert is_pid_alive(1) is False

    def test_negative_pid(self):
        assert is_pid_alive(-1) is False


# --- process start time / pid reuse ---


LSTART = "Wed Sep  2 20:35:59 2026"
LSTART_S = 1788381359
TICKS = "11485"
# /proc/<pid>/stat: pid, (comm), then the numeric fields; start time is 22nd.
STAT = (
    "4242 (claude) S 1 4242 4242 0 -1 4194560 1234 0 0 0 10 5 0 0 20 0 8 0 "
    f"{TICKS} 123456789 4321 18446744073709551615 1 1 0 0 0 0 0 0 0 0 0 0 0 "
    "17 3 0 0 0 0 0 0 0 0 0 0 0 0 0\n"
)


class TestLstartSeconds:
    @pytest.mark.parametrize(
        "text, expected",
        [
            (LSTART, LSTART_S),
            ("Thu Jan  1 00:00:00 1970", 0),
            ("Tue Nov 14 22:13:20 2023", 1_700_000_000),
        ],
    )
    def test_parses_ps_lstart(self, text, expected):
        assert _lstart_seconds(text) == expected

    @pytest.mark.parametrize(
        "text",
        ["", "abc", "Wed Sep 2 20:35 2026", "Wed Foo  2 20:35:59 2026",
         "Wed Sep  2 20:35:59", "Wed Sep  2 20:35:xx 2026"],
    )
    def test_rejects_garbage(self, text):
        with pytest.raises(ValueError):
            _lstart_seconds(text)


class TestStatStartTicks:
    def test_reads_field_22(self):
        assert _stat_start_ticks(STAT) == TICKS

    def test_counts_from_the_last_parenthesis(self):
        """The command name may contain spaces and parentheses of its own."""
        assert _stat_start_ticks(STAT.replace("(claude)", "(my (odd) name)")) == TICKS

    @pytest.mark.parametrize(
        "text", ["", "4242 (claude) S 1 2 3", STAT.replace(TICKS, "x")]
    )
    def test_rejects_garbage(self, text):
        assert _stat_start_ticks(text) is None


class TestProcessStartTicks:
    def test_unreadable_is_unknowable(self):
        with patch.object(Path, "read_text", side_effect=OSError("no /proc")):
            assert process_start_ticks(4242) is None

    def test_reads_proc_stat(self):
        with patch.object(Path, "read_text", autospec=True, return_value=STAT) as read:
            assert process_start_ticks(4242) == TICKS
        assert read.call_args[0][0] == Path("/proc/4242/stat")

    @pytest.mark.skipif(sys.platform != "linux", reason="/proc is Linux")
    def test_own_process_matches_proc_self(self):
        expected = _stat_start_ticks(Path("/proc/self/stat").read_text())
        assert process_start_ticks(os.getpid()) == expected


class TestProcessStartedAt:
    def test_windows_is_unknowable(self):
        with patch("ccswap.process_detection.sys.platform", "win32"), \
             patch("ccswap.process_detection.subprocess.run") as run:
            assert process_started_at(1234) is None
        run.assert_not_called()

    def test_ps_failure_is_unknowable(self):
        with patch("ccswap.process_detection.sys.platform", "linux"), \
             patch("ccswap.process_detection.subprocess.run",
                   side_effect=OSError("no ps")):
            assert process_started_at(1234) is None

    def test_unknown_pid_is_unknowable(self):
        proc = subprocess_result(returncode=1, stdout="")
        with patch("ccswap.process_detection.sys.platform", "linux"), \
             patch("ccswap.process_detection.subprocess.run", return_value=proc):
            assert process_started_at(1234) is None

    def test_garbage_is_unknowable(self):
        proc = subprocess_result(returncode=0, stdout="??\n")
        with patch("ccswap.process_detection.sys.platform", "linux"), \
             patch("ccswap.process_detection.subprocess.run", return_value=proc):
            assert process_started_at(1234) is None

    def test_reads_lstart_the_way_claude_does(self):
        """The record's ``procStart`` is claude's own ``LC_ALL=C TZ=UTC ps -o
        lstart=`` output; the live reading must match it to the second."""
        proc = subprocess_result(returncode=0, stdout=f"{LSTART}    \n")
        with patch("ccswap.process_detection.sys.platform", "linux"), \
             patch("ccswap.process_detection.subprocess.run",
                   return_value=proc) as run:
            assert process_started_at(1234) == LSTART_S
        args, kwargs = run.call_args
        assert args[0] == ["ps", "-o", "lstart=", "-p", "1234"]
        assert kwargs["env"]["LC_ALL"] == "C"
        assert kwargs["env"]["TZ"] == "UTC"

    @pytest.mark.skipif(os.name != "posix", reason="ps is POSIX")
    def test_own_process_started_in_the_past(self):
        started = process_started_at(os.getpid())
        assert started is not None
        assert started <= time.time()


def subprocess_result(returncode: int, stdout: str):
    from subprocess import CompletedProcess

    return CompletedProcess(["ps"], returncode, stdout=stdout, stderr="")


class TestProcessIsClaude:
    def test_ps_failure_is_unknowable(self):
        with patch("ccswap.process_detection.sys.platform", "linux"), \
             patch("ccswap.process_detection.subprocess.run",
                   side_effect=OSError("no ps")):
            assert process_is_claude(1234) is None

    @pytest.mark.parametrize(
        "line, expected",
        [
            ("claude           claude --resume 2d6cbe5d", True),
            ("node             node /usr/lib/node_modules/@anthropic-ai/claude-code/cli.js", True),
            ("2.1.258          /home/u/.local/share/claude/versions/2.1.258", True),
            ("vim              vim notes.md", False),
        ],
    )
    def test_judges_comm_and_args(self, line, expected):
        proc = subprocess_result(returncode=0, stdout=f"{line}\n")
        with patch("ccswap.process_detection.sys.platform", "linux"), \
             patch("ccswap.process_detection.subprocess.run",
                   return_value=proc) as run:
            assert process_is_claude(1234) is expected
        assert run.call_args[0][0] == ["ps", "-o", "comm=,args=", "-p", "1234"]


class TestPidMatchesRecord:
    @pytest.mark.parametrize("proc_start", [None, "", "garbage"])
    def test_unstamped_or_garbage_record_passes(self, proc_start):
        with patch("ccswap.process_detection.process_started_at") as started:
            assert pid_matches_record(1234, proc_start) is True
        started.assert_not_called()

    def test_unknowable_start_passes(self):
        with patch("ccswap.process_detection.process_started_at",
                   return_value=None), \
             patch("ccswap.process_detection.process_is_claude") as is_claude:
            assert pid_matches_record(1234, LSTART) is True
        is_claude.assert_not_called()

    def test_same_start_ticks_is_the_recorded_process(self):
        """A Linux record stamps the /proc start time in ticks since boot,
        which the process keeps for life: equality is the whole test."""
        with patch("ccswap.process_detection.process_start_ticks",
                   return_value=TICKS), \
             patch("ccswap.process_detection.process_started_at") as started:
            assert pid_matches_record(1234, TICKS) is True
        started.assert_not_called()

    def test_other_start_ticks_is_a_recycled_pid(self):
        with patch("ccswap.process_detection.process_start_ticks",
                   return_value="998877"), \
             patch("ccswap.process_detection.process_is_claude") as is_claude:
            assert pid_matches_record(1234, TICKS) is False
        is_claude.assert_not_called()

    def test_unreadable_ticks_pass(self):
        """A FILETIME on Windows, or /proc hidden: no comparison is possible."""
        with patch("ccswap.process_detection.process_start_ticks",
                   return_value=None):
            assert pid_matches_record(1234, "134332352612628209") is True

    @pytest.mark.parametrize(
        "started", [LSTART_S, LSTART_S - 3600, LSTART_S + PID_REUSE_SLACK_S // 2]
    )
    def test_process_not_younger_than_record_passes(self, started):
        with patch("ccswap.process_detection.process_started_at",
                   return_value=started), \
             patch("ccswap.process_detection.process_is_claude") as is_claude:
            assert pid_matches_record(1234, LSTART) is True
        is_claude.assert_not_called()

    def test_stranger_younger_than_record_is_a_recycled_pid(self):
        with patch("ccswap.process_detection.process_started_at",
                   return_value=LSTART_S + 86400), \
             patch("ccswap.process_detection.process_is_claude",
                   return_value=False):
            assert pid_matches_record(1234, LSTART) is False

    def test_claude_younger_than_record_is_kept(self):
        """Linux ps derives start times from a boot time that moves with the
        wall clock, so after a clock step a live session reads as younger
        than its own record. A claude at the pid is never a recycled pid."""
        with patch("ccswap.process_detection.process_started_at",
                   return_value=LSTART_S + 86400), \
             patch("ccswap.process_detection.process_is_claude",
                   return_value=True):
            assert pid_matches_record(1234, LSTART) is True

    def test_unknowable_identity_is_kept(self):
        with patch("ccswap.process_detection.process_started_at",
                   return_value=LSTART_S + 86400), \
             patch("ccswap.process_detection.process_is_claude",
                   return_value=None):
            assert pid_matches_record(1234, LSTART) is True


# --- list_sessions ---


def _write_session(sessions_dir: Path, pid: int, **overrides) -> Path:
    """Write a session PID file with sensible defaults."""
    data = {
        "pid": pid,
        "sessionId": f"session-{pid}",
        "cwd": "/home/user/project",
        "startedAt": int(time.time() * 1000),
        "kind": "interactive",
        "entrypoint": "cli",
    }
    data.update(overrides)
    path = sessions_dir / f"{pid}.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


class TestListSessions:
    def test_reads_valid_sessions(self, tmp_path):
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        _write_session(sessions_dir, 1001, entrypoint="cli", cwd="/home/user/app")
        _write_session(sessions_dir, 1002, entrypoint="claude-vscode", cwd="/home/user/web")

        with patch("ccswap.process_detection.is_pid_alive", return_value=True):
            result = list_sessions(tmp_path)

        assert len(result) == 2
        pids = {s.pid for s in result}
        assert pids == {1001, 1002}

    def test_filters_dead_pids(self, tmp_path):
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        _write_session(sessions_dir, 1001)
        _write_session(sessions_dir, 1002)

        def alive(pid):
            return pid == 1001

        with patch("ccswap.process_detection.is_pid_alive", side_effect=alive):
            result = list_sessions(tmp_path)

        assert len(result) == 1
        assert result[0].pid == 1001

    def test_filters_recycled_pids(self, tmp_path):
        """A crashed claude's record survives it; the OS may hand its pid to
        something else. Only a process that started when the record says its
        claude did is the recorded one."""
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        _write_session(sessions_dir, 1001, procStart=LSTART)
        _write_session(sessions_dir, 1002, procStart=LSTART)

        def started(pid):
            return LSTART_S if pid == 1002 else LSTART_S + 86400

        with patch("ccswap.process_detection.is_pid_alive", return_value=True), \
             patch("ccswap.process_detection.process_started_at",
                   side_effect=started), \
             patch("ccswap.process_detection.process_is_claude",
                   return_value=False):
            result = list_sessions(tmp_path)

        assert [s.pid for s in result] == [1002]

    def test_filters_recycled_pids_by_start_ticks(self, tmp_path):
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        _write_session(sessions_dir, 1001, procStart=TICKS)
        _write_session(sessions_dir, 1002, procStart=TICKS)

        def ticks(pid):
            return TICKS if pid == 1002 else "998877"

        with patch("ccswap.process_detection.is_pid_alive", return_value=True), \
             patch("ccswap.process_detection.process_start_ticks",
                   side_effect=ticks):
            result = list_sessions(tmp_path)

        assert [s.pid for s in result] == [1002]

    def test_unknowable_start_time_keeps_session(self, tmp_path):
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        _write_session(sessions_dir, 1001, procStart=LSTART)

        with patch("ccswap.process_detection.is_pid_alive", return_value=True), \
             patch("ccswap.process_detection.process_started_at",
                   return_value=None):
            result = list_sessions(tmp_path)

        assert [s.pid for s in result] == [1001]

    def test_missing_sessions_dir(self, tmp_path):
        assert list_sessions(tmp_path) == []

    @pytest.mark.parametrize(
        "write_bad_file",
        [
            lambda p: p.write_text("not json{{{", encoding="utf-8"),
            lambda p: p.write_bytes(b"\xff\xfe{\"pid\": 1}"),
            lambda p: p.write_text(json.dumps({"pid": 2**31}), encoding="utf-8"),
            lambda p: p.write_text("[" * 2000 + "]" * 2000, encoding="utf-8"),
            lambda p: p.write_text("[]", encoding="utf-8"),
        ],
        ids=["invalid_json", "invalid_utf8", "pid_overflows_c_long",
             "json_nested_too_deep", "json_is_an_array"],
    )
    def test_corrupt_session_file_is_skipped(self, tmp_path, write_bad_file):
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        write_bad_file(sessions_dir / "9999.json")

        result = list_sessions(tmp_path)
        assert result == []

    def test_missing_pid_field(self, tmp_path):
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        (sessions_dir / "9999.json").write_text(
            json.dumps({"sessionId": "abc", "cwd": "/tmp"}), encoding="utf-8"
        )

        result = list_sessions(tmp_path)
        assert result == []

    def test_optional_status_field(self, tmp_path):
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        _write_session(sessions_dir, 1001, status="busy")

        with patch("ccswap.process_detection.is_pid_alive", return_value=True):
            result = list_sessions(tmp_path)

        assert result[0].status == "busy"

    def test_status_absent(self, tmp_path):
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        _write_session(sessions_dir, 1001)

        with patch("ccswap.process_detection.is_pid_alive", return_value=True):
            result = list_sessions(tmp_path)

        assert result[0].status is None

    def test_session_fields(self, tmp_path):
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        _write_session(
            sessions_dir, 5000,
            sessionId="sess-abc",
            cwd="/projects/foo",
            startedAt=1700000000000,
            kind="bg",
            entrypoint="claude-desktop",
        )

        with patch("ccswap.process_detection.is_pid_alive", return_value=True):
            result = list_sessions(tmp_path)

        s = result[0]
        assert s.pid == 5000
        assert s.session_id == "sess-abc"
        assert s.cwd == "/projects/foo"
        assert s.started_at == 1700000000000
        assert s.kind == "bg"
        assert s.entrypoint == "claude-desktop"


# --- scan_sessions ---


class TestScanSessions:
    """The guard-shaped view, where "0 live" and "0 readable" must not
    collapse into the same answer."""

    @pytest.mark.skipif(
        sys.platform == "win32" or os.geteuid() == 0,
        reason="needs POSIX permission semantics (non-root)",
    )
    def test_an_unlistable_sessions_dir_counts_as_unreadable(self, tmp_path):
        """A directory whose listing fails hides live records exactly the way
        an unparseable record does, so it must not answer "0 unreadable": the
        callers read that as "nobody there" and re-bootstrap the profile under
        a running claude.

        The REAL state, not a patched scan. `Path.glob` SUPPRESSES the
        listing's own OSError and answers an empty list, so a test that fakes
        the raise cannot see that the raise never happens.
        """
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        _write_session(sessions_dir, os.getpid())
        assert scan_sessions(tmp_path) != ([], 0), "test premise"

        os.chmod(sessions_dir, 0o300)  # searchable and writable, not listable
        try:
            sessions, unreadable = scan_sessions(tmp_path)
        finally:
            os.chmod(sessions_dir, 0o700)

        assert sessions == []
        assert unreadable > 0

    @pytest.mark.skipif(
        sys.platform == "win32" or os.geteuid() == 0,
        reason="needs POSIX permission semantics (non-root)",
    )
    def test_an_unreachable_sessions_dir_counts_as_unreadable(self, tmp_path):
        """The same conflation one level up: `Path.is_dir()` answers False
        when the stat FAILS, not only when the directory is absent, so a
        profile root that cannot be traversed reads as a profile that never
        ran."""
        profile = tmp_path / "profile"
        sessions_dir = profile / "sessions"
        sessions_dir.mkdir(parents=True)
        _write_session(sessions_dir, os.getpid())

        os.chmod(profile, 0o600)  # readable, not searchable: the stat fails
        try:
            sessions, unreadable = scan_sessions(profile)
        finally:
            os.chmod(profile, 0o700)

        assert sessions == []
        assert unreadable > 0

    def test_a_missing_sessions_dir_is_provably_no_records(self, tmp_path):
        """The one scan failure that stays at 0: a profile that has never been
        run has no sessions directory, and refusing on that would block every
        destructive step on it permanently."""
        assert scan_sessions(tmp_path) == ([], 0)

    def test_record_name_casing_follows_the_platform_like_glob_did(self, tmp_path):
        """The listing used to be `glob("*.json")`, which is case-insensitive
        on Windows and case-sensitive elsewhere; the `iterdir` filter must
        keep that, so an upper-cased record is still seen where it used to
        be (the guard errs toward seeing records) and ignored where it was."""
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        record = _write_session(sessions_dir, os.getpid())
        record.rename(sessions_dir / f"{os.getpid()}.JSON")

        sessions, unreadable = scan_sessions(tmp_path)

        assert unreadable == 0
        assert len(sessions) == (1 if sys.platform == "win32" else 0)


# --- list_ide_instances ---


def _write_ide_lock(ide_dir: Path, port: int, **overrides) -> Path:
    """Write an IDE lockfile with sensible defaults."""
    data = {
        "pid": port + 1000,
        "workspaceFolders": ["/home/user/project"],
        "ideName": "Visual Studio Code",
        "transport": "ws",
    }
    data.update(overrides)
    path = ide_dir / f"{port}.lock"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


class TestListIdeInstances:
    def test_reads_valid_lockfiles(self, tmp_path):
        ide_dir = tmp_path / "ide"
        ide_dir.mkdir()
        _write_ide_lock(ide_dir, 45000, ideName="Visual Studio Code")
        _write_ide_lock(ide_dir, 45001, ideName="Cursor")

        with patch("ccswap.process_detection.is_pid_alive", return_value=True):
            result = list_ide_instances(tmp_path)

        assert len(result) == 2
        names = {i.ide_name for i in result}
        assert names == {"Visual Studio Code", "Cursor"}

    def test_filters_dead_pids(self, tmp_path):
        ide_dir = tmp_path / "ide"
        ide_dir.mkdir()
        _write_ide_lock(ide_dir, 45000, pid=2001)
        _write_ide_lock(ide_dir, 45001, pid=2002)

        with patch("ccswap.process_detection.is_pid_alive", side_effect=lambda p: p == 2001):
            result = list_ide_instances(tmp_path)

        assert len(result) == 1
        assert result[0].pid == 2001

    def test_missing_ide_dir(self, tmp_path):
        assert list_ide_instances(tmp_path) == []

    @pytest.mark.parametrize(
        "write_bad_file",
        [
            lambda p: p.write_text("broken", encoding="utf-8"),
            lambda p: p.write_text(json.dumps({"pid": 2**31}), encoding="utf-8"),
            lambda p: p.write_text("[" * 2000 + "]" * 2000, encoding="utf-8"),
            lambda p: p.write_text("[]", encoding="utf-8"),
        ],
        ids=["invalid_json", "pid_overflows_c_long", "json_nested_too_deep",
             "json_is_an_array"],
    )
    def test_corrupt_json(self, tmp_path, write_bad_file):
        ide_dir = tmp_path / "ide"
        ide_dir.mkdir()
        write_bad_file(ide_dir / "9999.lock")

        assert list_ide_instances(tmp_path) == []

    def test_missing_pid_field(self, tmp_path):
        ide_dir = tmp_path / "ide"
        ide_dir.mkdir()
        (ide_dir / "9999.lock").write_text(
            json.dumps({"ideName": "VS Code"}), encoding="utf-8"
        )

        assert list_ide_instances(tmp_path) == []

    def test_port_from_filename(self, tmp_path):
        ide_dir = tmp_path / "ide"
        ide_dir.mkdir()
        _write_ide_lock(ide_dir, 12345)

        with patch("ccswap.process_detection.is_pid_alive", return_value=True):
            result = list_ide_instances(tmp_path)

        assert result[0].port == 12345

    def test_workspace_folders(self, tmp_path):
        ide_dir = tmp_path / "ide"
        ide_dir.mkdir()
        _write_ide_lock(ide_dir, 45000, workspaceFolders=["/a", "/b"])

        with patch("ccswap.process_detection.is_pid_alive", return_value=True):
            result = list_ide_instances(tmp_path)

        assert result[0].workspace_folders == ["/a", "/b"]


# --- get_running_instances ---


class TestGetRunningInstances:
    def test_returns_both(self, tmp_path):
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        _write_session(sessions_dir, 1001)

        ide_dir = tmp_path / "ide"
        ide_dir.mkdir()
        _write_ide_lock(ide_dir, 45000)

        with patch("ccswap.process_detection.is_pid_alive", return_value=True):
            sessions, ides = get_running_instances(tmp_path)

        assert len(sessions) == 1
        assert len(ides) == 1

    def test_empty_when_no_dirs(self, tmp_path):
        sessions, ides = get_running_instances(tmp_path)
        assert sessions == []
        assert ides == []


# --- Display helpers (in switcher.py) ---


class TestEntrypointLabel:
    @pytest.mark.parametrize(
        "entrypoint,expected",
        [
            ("cli", "CLI"),
            ("claude-vscode", "VS Code"),
            ("claude-desktop", "Desktop"),
            ("sdk-cli", "SDK"),
            ("mcp", "MCP"),
            ("unknown-thing", "unknown-thing"),
        ],
    )
    def test_known_and_unknown(self, entrypoint, expected):
        assert entrypoint_label(entrypoint) == expected


class TestAbbreviatePath:
    def test_replaces_home(self):
        home = str(Path.home())
        assert abbreviate_path(f"{home}/projects/foo") == "~/projects/foo"

    def test_non_home_path_unchanged(self):
        assert abbreviate_path("/opt/data/bar") == "/opt/data/bar"

    def test_home_root(self):
        home = str(Path.home())
        assert abbreviate_path(home) == "~"


class TestFormatAge:
    def test_just_now(self):
        now_ms = int(time.time() * 1000)
        assert format_age(now_ms) == "just now"

    def test_minutes(self):
        ms = int((time.time() - 300) * 1000)  # 5 minutes ago
        assert format_age(ms) == "5m ago"

    def test_hours(self):
        ms = int((time.time() - 7200) * 1000)  # 2 hours ago
        assert format_age(ms) == "2h ago"

    def test_days(self):
        ms = int((time.time() - 172800) * 1000)  # 2 days ago
        assert format_age(ms) == "2d ago"


# --- Codex session liveness ---

@pytest.fixture
def codex_proc(tmp_path, monkeypatch):
    if not hasattr(os, "getuid"):
        pytest.skip("fake /proc needs POSIX uids; the Linux backend is not built for Windows")
    monkeypatch.setattr(detection.sys, "platform", "linux")
    root = tmp_path / "proc"
    boot = root / "sys/kernel/random"
    boot.mkdir(parents=True)
    (boot / "boot_id").write_text("12345678-1234-1234-1234-123456789abc")

    own_ns = root / "self/ns"
    own_ns.mkdir(parents=True)
    (own_ns / "mnt").symlink_to("mnt:[1]")

    def process(pid=42, env=b"OTHER=value\0", ticks="11485", state="S", uid=None):
        directory = root / str(pid)
        directory.mkdir(exist_ok=True)
        (directory / "ns").mkdir(exist_ok=True)
        if not (directory / "ns/mnt").is_symlink():
            (directory / "ns/mnt").symlink_to("mnt:[1]")
        (directory / "task" / str(pid)).mkdir(parents=True, exist_ok=True)
        (directory / "stat").write_text(
            STAT.replace("4242 (claude) S", f"{pid} (shell (worker)) {state}")
                                         .replace(TICKS, ticks))
        (directory / "environ").write_bytes(env)
        uid = os.getuid() if uid is None else uid
        (directory / "status").write_text(f"Uid:\t{uid}\t{uid}\t{uid}\t{uid}\n")
        return directory

    return root, process


def test_codex_identity_and_reuse(codex_proc):
    root, process = codex_proc
    directory = process()
    start = detection.codex_process_identity(42, root)
    assert start == {"kind": "linux", "bootId": "12345678-1234-1234-1234-123456789abc",
        "ticks": TICKS}
    assert detection.codex_process_state(42, start, root) == "alive"
    process(ticks="999")
    assert detection.codex_process_state(42, start, root) == "gone"
    (directory / "stat").unlink()
    assert detection.codex_process_identity(42, root) is None
    assert detection.codex_process_state(42, start, root) == "unknown"
    import shutil
    shutil.rmtree(directory)
    assert detection.codex_process_state(42, start, root) == "gone"
    assert detection.codex_process_state(42, start, root / "missing") == "unknown"


@pytest.mark.parametrize("env, expected", [
    (b"CODEX_HOME={home}\0", "live"),
    (b"PREFIX_CODEX_HOME={home}\0", "quiescent"),
    (b"ARGV=CODEX_HOME={home}\0", "quiescent"),
    (b"CODEX_HOME={home}/../session home\0", "live"),
    (b"CODEX_HOME=relative\0", "uncertain"),
    (b"CODEX_HOME=\0", "quiescent"),
    (b"CODEX_HOME={home}", "uncertain"),
    (b"OTHER=x", "quiescent"),
    (b"broken\0", "quiescent"),
    (b"", "quiescent"),
])
def test_codex_environment_bytes(codex_proc, tmp_path, env, expected):
    root, process = codex_proc
    home = tmp_path / "session home"
    process(env=env.replace(b"{home}", os.fsencode(home)))
    result = detection.scan_codex_home(home, [], proc_root=root)
    assert result["state"] == expected
    assert str(home) not in str(result["issues"])


def test_codex_alias_descendants_and_orphans(codex_proc, tmp_path):
    root, process = codex_proc
    home = tmp_path / "home with spaces"
    home.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(home, target_is_directory=True)
    # No name or parent relationship is required: shim, grandchild, orphan.
    for pid in (42, 43, 44):
        process(pid, env=b"CODEX_HOME=" + os.fsencode(alias) + b"\0")
    result = detection.scan_codex_home(home, [], proc_root=root)
    assert result == {"state": "live", "pids": [42, 43, 44], "issues": [], "unreadable": 0}


def test_codex_starting_reservation_and_invalid_records(codex_proc, tmp_path):
    root, process = codex_proc
    process()
    start = detection.codex_process_identity(42, root)
    result = detection.scan_codex_home(tmp_path, [(42, start)], 1, root)
    assert result["state"] == "live" and result["issues"]
    assert detection.scan_codex_home(tmp_path, [], 1, root)["state"] == "uncertain"
    assert detection.scan_codex_home(tmp_path, [(42, {**start, "ticks": "9"})],
                                     proc_root=root)["state"] == "quiescent"


@pytest.mark.parametrize("filename", ["status", "stat", "environ", "boot_id"])
@pytest.mark.parametrize("failure", [PermissionError, FileNotFoundError, ValueError])
def test_codex_unavailable_data_is_uncertain(codex_proc, tmp_path, monkeypatch, filename, failure):
    root, process = codex_proc
    process()
    original = Path.open

    def denied(path, *args, **kwargs):
        if path.name == filename:
            raise failure("sensitive data must not appear")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", denied)
    result = detection.scan_codex_home(tmp_path, [], proc_root=root)
    assert result["state"] == (
        "quiescent" if filename == "environ" and failure is PermissionError else "uncertain")
    assert "sensitive" not in str(result)
    assert detection.codex_process_state(42, {}, root) in ("gone", "unknown")


def test_codex_other_uid_and_zombie(codex_proc, tmp_path):
    root, process = codex_proc
    process(uid=os.getuid() + 1, env=b"")
    assert detection.scan_codex_home(tmp_path, [], proc_root=root)["state"] == "quiescent"
    process(env=b"", state="Z")
    assert detection.scan_codex_home(tmp_path, [], proc_root=root)["state"] == "quiescent"


def test_codex_conflicting_environment_keeps_positive_evidence(codex_proc, tmp_path):
    root, process = codex_proc
    process(env=b"CODEX_HOME=" + os.fsencode(tmp_path) + b"\0CODEX_HOME=/other\0")
    result = detection.scan_codex_home(tmp_path, [], proc_root=root)
    assert result["state"] == "live" and result["issues"]


def test_codex_identity_race_retries(codex_proc, tmp_path, monkeypatch):
    root, process = codex_proc
    process(env=b"CODEX_HOME=" + os.fsencode(tmp_path) + b"\0")
    original = detection._codex_linux_snapshot
    calls = 0

    def changed(pid, proc_root, boot=None):
        nonlocal calls
        calls += 1
        identity, *rest = original(pid, proc_root, boot)
        if calls == 2:
            identity["ticks"] = "999"
        return (identity, *rest)

    monkeypatch.setattr(detection, "_codex_linux_snapshot", changed)
    assert detection.scan_codex_home(tmp_path, [], proc_root=root)["state"] == "live"
    assert calls == 6
    calls = 0

    def unstable(pid, proc_root, boot=None):
        nonlocal calls
        calls += 1
        identity, *rest = original(pid, proc_root, boot)
        identity["ticks"] = str(calls)
        return (identity, *rest)

    monkeypatch.setattr(detection, "_codex_linux_snapshot", unstable)
    assert detection.scan_codex_home(tmp_path, [], proc_root=root)["state"] == "uncertain"


def test_codex_disappearance_confirmed(codex_proc, tmp_path, monkeypatch):
    root, process = codex_proc
    directory = process()
    original = Path.read_bytes

    def disappear(path):
        if path == directory / "environ":
            import shutil
            shutil.rmtree(directory)
            raise FileNotFoundError
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", disappear)
    assert detection.scan_codex_home(tmp_path, [], proc_root=root)["state"] == "quiescent"


def test_codex_enumeration_failure_and_unsupported(codex_proc, tmp_path, monkeypatch):
    root, _ = codex_proc
    with patch.object(Path, "iterdir", side_effect=PermissionError):
        assert detection.scan_codex_home(tmp_path, [], proc_root=root)["state"] == "uncertain"
    monkeypatch.setattr(detection.sys, "platform", "freebsd")
    assert detection.codex_process_identity(42, root) is None
    assert detection.codex_process_state(42, {}, root) == "unknown"
    assert detection.scan_codex_home(tmp_path, [], proc_root=root) == {
        "state": "uncertain", "pids": [], "issues": ["unsupported platform"], "unreadable": 0}


@pytest.mark.skipif(sys.platform != "linux", reason="Linux /proc smoke test")
def test_codex_real_child_detection_and_quiescence(tmp_path):
    import subprocess

    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                             env={**os.environ, "CODEX_HOME": str(tmp_path)})
    try:
        result = detection.scan_codex_home(tmp_path, [])
        assert result["state"] == "live" and child.pid in result["pids"]
    finally:
        child.terminate()
        child.wait(timeout=5)
    result = detection.scan_codex_home(tmp_path, [])
    assert result["state"] == "quiescent", result["issues"]


@pytest.mark.parametrize("filename, content", [
    ("stat", "malformed"), ("status", "Uid: invalid"),
    ("boot_id", ""), ("boot_id", "invalid-boot"),
])
def test_codex_malformed_process_metadata(codex_proc, tmp_path, filename, content):
    root, process = codex_proc
    directory = process()
    path = root / "sys/kernel/random/boot_id" if filename == "boot_id" else directory / filename
    path.write_text(content)
    assert detection.scan_codex_home(tmp_path, [], proc_root=root)["state"] == "uncertain"
    if filename != "status":
        assert detection.codex_process_identity(42, root) is None
        assert detection.codex_process_state(42, {}, root) == "unknown"


def test_codex_unresolvable_home_and_unknown_supervisor(codex_proc, tmp_path):
    root, process = codex_proc
    directory = process()
    start = detection.codex_process_identity(42, root)
    (directory / "stat").unlink()
    result = detection.scan_codex_home(tmp_path, [(42, start)], proc_root=root)
    assert result["state"] == "uncertain"
    assert any("supervisor" in issue for issue in result["issues"])
    loop = tmp_path / "loop"
    loop.symlink_to(loop)
    process(env=b"CODEX_HOME=" + os.fsencode(loop) + b"\0")
    assert detection.scan_codex_home(tmp_path, [], proc_root=root)["state"] == "uncertain"
    assert detection.scan_codex_home(loop, [], proc_root=root)["state"] == "uncertain"
    assert detection.scan_codex_home(Path("relative"), [], proc_root=root)["state"] == "uncertain"


@pytest.mark.parametrize("pid", [0, -1, True])
def test_codex_invalid_pid_is_unknown(codex_proc, pid):
    root, _ = codex_proc
    assert detection.codex_process_identity(pid, root) is None
    assert detection.codex_process_state(pid, {}, root) == "unknown"


def test_codex_empty_home_is_unset_without_cwd_probe(codex_proc, tmp_path):
    root, process = codex_proc
    process(env=b"CODEX_HOME=\0")
    with patch.object(Path, "readlink", side_effect=AssertionError("cwd must not be read")):
        assert detection.scan_codex_home(tmp_path, [], proc_root=root) == {
            "state": "quiescent", "pids": [], "issues": [], "unreadable": 0}


@pytest.mark.parametrize("relative", ["home with spaces", "alias", "other"])
def test_codex_relative_home_is_uncertain_even_with_cwd(codex_proc, tmp_path, relative):
    root, process = codex_proc
    directory = process(env=b"CODEX_HOME=" + os.fsencode(relative) + b"\0")
    (directory / "cwd").symlink_to(tmp_path, target_is_directory=True)
    result = detection.scan_codex_home(tmp_path, [], proc_root=root)
    assert result["state"] == "uncertain"
    assert result["issues"] == ["PID 42: relative CODEX_HOME"]


def test_codex_kernel_thread_skipped_before_environment_read(codex_proc, tmp_path, monkeypatch):
    root, process = codex_proc
    monkeypatch.setattr(os, "getuid", lambda: 0)
    directory = process(env=b"")
    stat = directory / "stat"
    prefix, _, rest = stat.read_text().rpartition(")")
    fields = rest.split()
    fields[6] = str(int(fields[6]) | 0x00200000)
    stat.write_text(prefix + ") " + " ".join(fields))
    with patch.object(Path, "read_bytes", side_effect=AssertionError("kernel environment read")):
        assert detection.scan_codex_home(tmp_path, [], proc_root=root) == {
            "state": "quiescent", "pids": [], "issues": [], "unreadable": 0}
    # An ordinary process with an empty environment is attributed like an unreadable one.
    fields[6] = str(int(fields[6]) & ~0x00200000)
    stat.write_text(prefix + ") " + " ".join(fields))
    result = detection.scan_codex_home(tmp_path, [], proc_root=root)
    assert result["state"] == "quiescent" and result["unreadable"] == 1


def test_codex_boot_id_read_once_for_scan_and_supervisors(codex_proc, tmp_path, monkeypatch):
    root, process = codex_proc
    process()
    process(43, env=b"CODEX_HOME=" + os.fsencode(tmp_path) + b"\0")
    start = detection.codex_process_identity(42, root)
    original = Path.read_text
    reads = 0

    def read_text(path, *args, **kwargs):
        nonlocal reads
        if path.name == "boot_id":
            reads += 1
            assert reads == 1
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    result = detection.scan_codex_home(tmp_path, [(42, start)], proc_root=root)
    assert result == {"state": "live", "pids": [42, 43], "issues": [], "unreadable": 0}
    assert reads == 1


def _codex_edit_stat(directory, *, parent=None, comm=None):
    path = directory / "stat"
    prefix, _, rest = path.read_bytes().rpartition(b")")
    fields = rest.split()
    if parent is not None:
        fields[1] = str(parent).encode()
    if comm is not None:
        prefix = str(directory.name).encode() + b" (" + comm
    path.write_bytes(prefix + b") " + b" ".join(fields))


def test_codex_non_utf8_comm_and_status(codex_proc, tmp_path):
    root, process = codex_proc
    directory = process(env=b"CODEX_HOME=" + os.fsencode(tmp_path) + b"\0")
    _codex_edit_stat(directory, comm=b"worker\xff")
    status = directory / "status"
    status.write_bytes(b"Name:\tworker\xff\n" + status.read_bytes())
    assert detection.codex_process_identity(42, root) is not None
    assert detection.scan_codex_home(tmp_path, [], proc_root=root)["state"] == "live"


@pytest.mark.parametrize("kind",
    ["unrelated", "codex", "ancestor", "supervisor", "cycle", "missing"])
@pytest.mark.parametrize("error_number", [1, 13])
def test_codex_denied_environ_attribution(codex_proc, tmp_path, monkeypatch, kind, error_number):
    root, process = codex_proc
    directory = process(44)
    supervisors = []
    if kind == "codex":
        _codex_edit_stat(directory, comm=b"codex-helper")
    if kind in ("ancestor", "supervisor"):
        process(42, env=(b"CODEX_HOME=" + os.fsencode(tmp_path) + b"\0")
                if kind == "ancestor" else b"OTHER=x\0")
        middle = process(43)
        _codex_edit_stat(middle, parent=42)
        _codex_edit_stat(directory, parent=43)
        if kind == "supervisor":
            supervisors = [(42, detection.codex_process_identity(42, root))]
    elif kind == "cycle":
        _codex_edit_stat(directory, parent=44)
    elif kind == "missing":
        _codex_edit_stat(directory, parent=999)
    original = Path.read_bytes

    def denied(path):
        if path == directory / "environ":
            raise PermissionError(error_number, "hidden environment")
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", denied)
    result = detection.scan_codex_home(tmp_path, supervisors, proc_root=root)
    assert result["unreadable"] == 1
    assert result["state"] == ("live" if kind in ("ancestor", "supervisor") else
                               "uncertain" if kind == "codex" else "quiescent")
    assert ("PID 44: environment unreadable" in result["issues"]) == (
        kind in ("codex", "ancestor", "supervisor"))


def test_codex_ancestry_bound_is_conservative(codex_proc, tmp_path, monkeypatch):
    root, _ = codex_proc
    calls = 0

    def endless(pid, proc_root, boot):
        nonlocal calls
        calls += 1
        return ({}, "S", 0, pid + 1, "helper")

    monkeypatch.setattr(detection, "_codex_linux_snapshot", endless)
    assert detection._codex_related_unreadable(({}, "S", 0, 1, "helper"), set(), root, "boot")
    assert calls == 4096


def test_codex_second_enumeration_finds_new_pid(codex_proc, tmp_path, monkeypatch):
    root, process = codex_proc
    directory = process(43, env=b"CODEX_HOME=" + os.fsencode(tmp_path) + b"\0")
    original = Path.iterdir
    passes = 0

    def enumerate_proc(path):
        nonlocal passes
        entries = list(original(path))
        if path == root:
            passes += 1
            if passes == 1:
                entries.remove(directory)
        return iter(entries)

    monkeypatch.setattr(Path, "iterdir", enumerate_proc)
    result = detection.scan_codex_home(tmp_path, [], proc_root=root)
    assert result["pids"] == [43]
    assert passes == 2


@pytest.mark.parametrize("namespace", ["different", "missing", "denied"])
def test_codex_foreign_mount_namespace(codex_proc, tmp_path, monkeypatch, namespace):
    root, process = codex_proc
    directory = process(env=b"CODEX_HOME=" + os.fsencode(tmp_path) + b"\0")
    link = directory / "ns/mnt"
    link.unlink()
    if namespace == "different":
        link.symlink_to("mnt:[2]")
    original = Path.readlink

    def readlink(path):
        if namespace == "denied" and path == link:
            raise PermissionError
        return original(path)

    monkeypatch.setattr(Path, "readlink", readlink)
    result = detection.scan_codex_home(tmp_path, [], proc_root=root)
    assert result["state"] == "uncertain"
    assert result["issues"] == ["PID 42: foreign mount namespace"]


def test_codex_home_inode_alias(codex_proc, tmp_path, monkeypatch):
    root, process = codex_proc
    home, alias = tmp_path / "home", tmp_path / "bind-mount"
    home.mkdir()
    alias.mkdir()
    process(env=b"CODEX_HOME=" + os.fsencode(alias) + b"\0")
    original = Path.stat

    def bind_stat(path, *args, **kwargs):
        return original(home if path == alias else path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", bind_stat)
    assert detection.scan_codex_home(home, [], proc_root=root)["state"] == "live"


@pytest.mark.parametrize("boot", [None, "invalid"])
def test_codex_missing_pid_with_bad_boot_is_unknown(codex_proc, boot):
    root, process = codex_proc
    process()
    start = detection.codex_process_identity(42, root)
    path = root / "sys/kernel/random/boot_id"
    if boot is None:
        path.unlink()
    else:
        path.write_text(boot)
    assert detection.codex_process_state(999, start, root) == "unknown"


@pytest.mark.parametrize("start", [None, [], {}, {"kind": "linux"},
    {"kind": "macos", "bootId": "boot", "ticks": "1"},
    {"kind": "linux", "bootId": 1, "ticks": "1"},
    {"kind": "linux", "bootId": "boot", "ticks": 1},
    {"kind": "linux", "bootId": "boot", "ticks": "١"},
    {"kind": "linux", "bootId": "boot", "ticks": ""},
    {"kind": "linux", "bootId": "boot", "ticks": "1", "extra": True},
])
def test_codex_invalid_start_is_unknown_even_for_missing_pid(codex_proc, start):
    root, _ = codex_proc
    assert detection.codex_process_state(999, start, root) == "unknown"


def test_codex_pid_reuse_during_uid_read_is_retried(codex_proc, tmp_path, monkeypatch):
    root, process = codex_proc
    directory = process(uid=os.getuid() + 1)
    original = Path.read_text
    reads = 0

    def replaced(path, *args, **kwargs):
        nonlocal reads
        text = original(path, *args, **kwargs)
        if path == directory / "status":
            reads += 1
            if reads == 1:
                process(ticks="999", env=b"CODEX_HOME=" + os.fsencode(tmp_path) + b"\0")
        return text

    monkeypatch.setattr(Path, "read_text", replaced)
    assert detection.scan_codex_home(tmp_path, [], proc_root=root)["state"] == "live"
    assert reads == 3


@pytest.mark.parametrize("thread_state, expected", [("Z", "quiescent"), ("S", "uncertain")])
def test_codex_zombie_leader_threads(codex_proc, tmp_path, thread_state, expected):
    root, process = codex_proc
    directory = process(env=b"", state="Z")
    leader = directory / "task/42/stat"
    leader.write_bytes((directory / "stat").read_bytes())
    thread = directory / "task/43"
    thread.mkdir()
    thread.joinpath("stat").write_text(
        STAT.replace("4242 (claude) S", f"43 (worker) {thread_state}"))
    result = detection.scan_codex_home(tmp_path, [], proc_root=root)
    assert result["state"] == expected
    assert bool(result["issues"]) == (expected == "uncertain")


@pytest.mark.parametrize("filename", ["stat", "status", "environ"])
def test_codex_esrch_disappearance(codex_proc, tmp_path, monkeypatch, filename):
    import shutil

    root, process = codex_proc
    directory = process()
    original = Path.open

    def disappear(path, *args, **kwargs):
        if path == directory / filename:
            shutil.rmtree(directory)
            raise ProcessLookupError
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", disappear)
    assert detection.scan_codex_home(tmp_path, [], proc_root=root)["state"] == "quiescent"


def test_codex_two_pass_union_retains_prior_matches_and_issues(codex_proc, tmp_path, monkeypatch):
    root, process = codex_proc
    process(42, env=b"CODEX_HOME=" + os.fsencode(tmp_path) + b"\0")
    process(43, env=b"CODEX_HOME=relative\0")
    original = Path.iterdir
    passes = 0

    def enumerate_proc(path):
        nonlocal passes
        if path == root:
            passes += 1
            if passes == 2:
                return iter([])
        return original(path)

    monkeypatch.setattr(Path, "iterdir", enumerate_proc)
    result = detection.scan_codex_home(tmp_path, [], proc_root=root)
    assert result == {"state": "live", "pids": [42],
                      "issues": ["PID 43: relative CODEX_HOME"], "unreadable": 0}


def test_codex_removed_start_alias_stays_uncertain(codex_proc, tmp_path):
    """Codex canonicalizes CODEX_HOME at start; a since-removed alias may still point at H."""
    root, process = codex_proc
    home = tmp_path / "home"
    home.mkdir()
    process(env=b"CODEX_HOME=" + os.fsencode(tmp_path / "removed-alias") + b"\0")
    assert detection.scan_codex_home(home, [], proc_root=root)["state"] == "uncertain"


def test_codex_unreadable_name_change_between_passes(codex_proc, tmp_path, monkeypatch):
    """A codex-named unreadable process stays an issue even if renamed before pass two."""
    root, process = codex_proc
    process()
    real = detection._codex_linux_snapshot
    names = iter(["codex-helper"] * 3 + ["worker"] * 100)

    def snapshot(pid, proc_root, boot=None):
        current = real(pid, proc_root, boot)
        return pid == 42 and (*current[:4], next(names)) or current

    monkeypatch.setattr(detection, "_codex_linux_snapshot", snapshot)
    original = Path.read_bytes
    monkeypatch.setattr(Path, "read_bytes", lambda path: (_ for _ in ()).throw(
        PermissionError()) if path.name == "environ" else original(path))
    result = detection.scan_codex_home(tmp_path, [], proc_root=root)
    assert result["state"] == "uncertain"


@pytest.mark.parametrize("env", [b"OTHER=x", b"broken\0", b"", b"title rewritten\0\0"])
def test_codex_garbled_environment_follows_attribution(codex_proc, tmp_path, env):
    """setproctitle-style environments only count for codex-related processes."""
    root, process = codex_proc
    home = tmp_path / "home"
    home.mkdir()
    process(42, env=env)
    result = detection.scan_codex_home(home, [], proc_root=root)
    assert result["state"] == "quiescent" and result["unreadable"] == 1
    stat_file = root / "42" / "stat"
    stat_file.write_text(stat_file.read_text().replace("(shell (worker))", "(codex)"))
    assert detection.scan_codex_home(home, [], proc_root=root)["state"] == "uncertain"
