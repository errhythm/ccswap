"""Detect running Claude Code instances.

Reads session PID files (~/.claude/sessions/{pid}.json) and IDE lockfiles
(~/.claude/ide/{port}.lock) to determine which Claude Code instances are
currently running. Uses the same mechanism Claude Code itself uses internally.
"""

from __future__ import annotations

import calendar
import ctypes
import functools
import json
import logging
import os
import platform
import subprocess
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from ccswap import spawn
from ccswap.paths import get_claude_config_home

logger = logging.getLogger(__name__)

# A session record names a pid, and the OS recycles pids: once the claude
# that wrote the record is gone, the number can belong to anything. The
# record also carries claude's reading of its own start (``procStart``): on
# Linux the ``/proc/<pid>/stat`` start time in clock ticks since boot, fixed
# for the process's lifetime, and elsewhere ``ps -o lstart``, a wall-clock
# time. Only the latter needs slack, for the small clock steps that move a
# ``ps`` start time on some platforms.
PID_REUSE_SLACK_S = 120

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


@dataclass
class ClaudeSession:
    """A running Claude Code session from ~/.claude/sessions/{pid}.json."""

    pid: int
    session_id: str
    cwd: str
    started_at: int  # epoch milliseconds
    kind: str  # "interactive", "bg", "daemon", "daemon-worker"
    entrypoint: str  # "cli", "claude-vscode", "claude-desktop", "sdk-cli", "mcp"
    status: str | None = None  # "busy", "idle", "waiting"


@dataclass
class IdeInstance:
    """A running IDE instance from ~/.claude/ide/{port}.lock."""

    port: int  # from filename
    pid: int
    ide_name: str  # "Visual Studio Code", "Cursor", "Windsurf"
    workspace_folders: list[str] = field(default_factory=list)


def get_claude_dir() -> Path:
    """Return the Claude config directory, respecting CLAUDE_CONFIG_DIR."""
    return get_claude_config_home()


def is_pid_alive(pid: int) -> bool:
    """Check whether a process with the given PID is running.

    Cross-platform:
    - macOS/Linux/WSL: os.kill(pid, 0)
    - Windows: ctypes OpenProcess
    """
    if pid <= 1:
        return False

    if sys.platform == "win32":
        return _is_pid_alive_windows(pid)

    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        # EPERM means the process exists but we lack permission
        return True
    except OSError:
        return False


def _is_pid_alive_windows(pid: int) -> bool:
    """Windows-specific PID liveness check using ctypes."""
    try:
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if handle:
            kernel32.CloseHandle(handle)
            return True
        return False
    except Exception:
        return False


def _ps(pid: int, *columns: str) -> str | None:
    """``ps -o`` ``columns`` for ``pid``, or None when unknowable.

    POSIX only, under ``LC_ALL=C TZ=UTC`` like claude's own reading. Windows
    and every failure answer None: not knowing must never be read as "not
    the recorded process".
    """
    if sys.platform == "win32":
        return None
    try:
        proc = spawn.run(
            ["ps", "-o", ",".join(f"{c}=" for c in columns), "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=5,
            env={**os.environ, "LC_ALL": "C", "TZ": "UTC"},
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    text = proc.stdout.strip()
    if proc.returncode != 0 or not text:
        return None
    return text


def process_started_at(pid: int) -> int | None:
    """Epoch seconds at which ``pid`` started, or None when unknowable.

    Read the way claude stamps ``procStart`` into its record, ``ps -o
    lstart=`` under ``LC_ALL=C TZ=UTC``, so the two agree to the second for
    the same process.
    """
    text = _ps(pid, "lstart")
    if text is None:
        return None
    try:
        return _lstart_seconds(text)
    except ValueError:
        return None


def _lstart_seconds(text: str) -> int:
    """``Wed Sep  2 20:35:59 2026``, the ``ps -o lstart`` format under
    ``LC_ALL=C TZ=UTC``, as epoch seconds. Parsed by hand because ``strptime``
    reads month names in the process locale."""
    parts = text.split()
    if len(parts) != 5 or parts[1] not in _MONTHS:
        raise ValueError(text)
    _, month, day, clock, year = parts
    hours, minutes, seconds = (int(p) for p in clock.split(":"))
    return calendar.timegm(
        (int(year), _MONTHS.index(month) + 1, int(day), hours, minutes, seconds, 0, 0, 0)
    )


def process_start_ticks(pid: int) -> str | None:
    """``/proc/<pid>/stat``'s start time, in clock ticks since boot, or None
    when unreadable. Linux by construction: the file exists nowhere else."""
    try:
        text = Path(f"/proc/{pid}/stat").read_text(encoding="ascii", errors="replace")
    except OSError:
        return None
    return _stat_start_ticks(text)


def _stat_start_ticks(text: str) -> str | None:
    """Field 22 of a ``/proc/<pid>/stat`` line, as the string claude stamps
    into ``procStart``. The command name sits in parentheses before the
    numeric fields and may itself contain spaces or parentheses, so the
    fields are counted from the last closing one."""
    _, _, rest = text.rpartition(")")
    fields = rest.split()
    if len(fields) <= 19 or not fields[19].isdigit():
        return None
    return fields[19]


def process_is_claude(pid: int) -> bool | None:
    """Does the process at ``pid`` look like a claude, or None when unknowable.

    Judged from ``ps -o comm=,args=``: the native binary and the symlink to
    it are named ``claude``, and an npm install runs ``cli.js`` out of a
    ``claude-code`` package directory.
    """
    text = _ps(pid, "comm", "args")
    if text is None:
        return None
    return "claude" in text.lower()


def pid_matches_record(pid: int, proc_start: str | None) -> bool:
    """Is the live process at ``pid`` the one that wrote a record stamped
    ``proc_start``, claude's reading of its own start?

    On Linux the stamp is the ``/proc/<pid>/stat`` start time in clock ticks
    since boot, which a process keeps for life and no two processes at one
    pid share, so equality is the whole test and no clock domain is
    involved. Elsewhere it is ``ps -o lstart``, a wall-clock time: a
    recycled pid belongs to a process that started after the recorded claude
    did, and only that direction disqualifies, and only a stranger. Some
    ``ps`` builds derive every start time from a boot time that moves with
    each wall-clock step (a WSL2 resume re-syncing the clock steps it by the
    whole sleep), so a live session can read as younger than its own record;
    a claude at the pid is kept either way, and a pid genuinely recycled by
    another claude lingers only for that process's lifetime, which is what
    happened before this check. Everything unknowable (Windows, whose stamp
    is a FILETIME with no ``/proc`` to check it against, ``ps`` unavailable,
    an unstamped or unparseable record) passes, because "cannot tell" must
    never turn a live session into "nobody there".
    """
    if not proc_start:
        return True
    if proc_start.isdigit():
        ticks = process_start_ticks(pid)
        return ticks is None or ticks == proc_start
    try:
        recorded = _lstart_seconds(proc_start)
    except ValueError:
        return True
    started = process_started_at(pid)
    if started is None or started <= recorded + PID_REUSE_SLACK_S:
        return True
    return process_is_claude(pid) is not False


def scan_sessions(claude_dir: Path | None = None) -> tuple[list[ClaudeSession], int]:
    """Live sessions, and how many records could NOT be read.

    A record counts as live only when its pid is alive AND still belongs to
    the claude that wrote it (see ``pid_matches_record``): a crashed claude
    leaves its record behind, and the OS can hand the number to something
    else.

    Two kinds of caller read this directory and they need opposite things from
    an unparseable record:

    - A SCAN (a listing, a status display) wants it skipped. One bad file must
      not take out the whole listing.
    - A GUARD wants to know. ``0 live`` and ``0 readable`` are the same list,
      and only the first is safe to act on -- the callers gate ``_bootstrap``
      (which deletes a profile's Keychain entry and overwrites
      ``.credentials.json``) and account removal, so reading "could not tell"
      as "nobody there" runs them underneath a live instance.

    So the count is returned rather than swallowed, and ``list_sessions``
    below is the scan-shaped view that drops it.

    ``iterdir``, NOT ``glob`` behind an ``is_dir`` check: both of those
    suppress the ``OSError`` they hit, so a sessions directory that cannot be
    listed -- or one whose stat fails because the profile root cannot be
    traversed -- arrived at the GUARD as an empty directory rather than an
    unknown one. Such a directory contributes exactly 1, which makes the count
    a lower bound on the records at stake rather than a tally of them; every
    caller only asks whether it is zero.

    A MISSING directory is the one scan failure that still counts 0: a profile
    that has never been run holds no records, and refusing on it would block
    every guarded step against it permanently.
    """
    sessions_dir = (claude_dir or get_claude_dir()) / "sessions"
    try:
        # glob/is_dir may suppress filesystem errors: uncertainty must block guards.
        paths = [path for path in sessions_dir.iterdir() if path.match("*.json")]
    except FileNotFoundError:
        return [], 0
    except OSError as exc:
        logger.debug("Cannot enumerate session directory %s: %s", sessions_dir, exc)
        return [], 1

    sessions = []
    unreadable = 0
    for path in paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            pid = data["pid"]
            if not is_pid_alive(pid):
                continue
            if not pid_matches_record(pid, data.get("procStart")):
                logger.debug(
                    "Skipping session file %s: pid %s was recycled", path, pid
                )
                continue
            sessions.append(ClaudeSession(
                pid=pid,
                session_id=data.get("sessionId", ""),
                cwd=data.get("cwd", ""),
                started_at=data.get("startedAt", 0),
                kind=data.get("kind", ""),
                entrypoint=data.get("entrypoint", ""),
                status=data.get("status"),
            ))
        except (
            json.JSONDecodeError,   # malformed JSON
            KeyError,               # required field missing
            TypeError,              # field has the wrong type (e.g. pid not an int)
            AttributeError,         # valid JSON that is not an object: a
                                    # top-level array reaches `.get` as a list.
                                    # Also how a too-deep nesting lands where
                                    # the parser's recursion limit is high
                                    # enough not to raise -- it differs per
                                    # machine, so BOTH outcomes must be inert.
            ValueError,             # includes UnicodeDecodeError from read_text
            OverflowError,          # pid too large for os.kill's C long (is_pid_alive)
            RecursionError,         # pathologically nested JSON in json.loads
            OSError,
        ) as exc:
            unreadable += 1
            logger.debug("Skipping session file %s: %s", path, exc)
    return sessions, unreadable


def list_sessions(claude_dir: Path | None = None) -> list[ClaudeSession]:
    """Live sessions. A record that cannot be read is SKIPPED.

    SCAN USE ONLY. The returned list cannot distinguish "no live sessions"
    from "no readable records", so anything gating a destructive step must
    call :func:`scan_sessions` and treat a non-zero count as live.
    """
    return scan_sessions(claude_dir)[0]


def list_ide_instances(claude_dir: Path | None = None) -> list[IdeInstance]:
    """Read IDE lockfiles and return only those with alive processes."""
    ide_dir = (claude_dir or get_claude_dir()) / "ide"
    if not ide_dir.is_dir():
        return []

    instances = []
    for path in ide_dir.glob("*.lock"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            pid = data.get("pid")
            if pid is None or not is_pid_alive(pid):
                continue
            port = int(path.stem)
            instances.append(IdeInstance(
                port=port,
                pid=pid,
                ide_name=data.get("ideName", "Unknown IDE"),
                workspace_folders=data.get("workspaceFolders", []),
            ))
        except (
            json.JSONDecodeError,   # malformed JSON
            KeyError,               # required field missing
            TypeError,              # field has the wrong type (e.g. pid not an int)
            AttributeError,         # valid JSON that is not an object: a
                                    # top-level array reaches `.get` as a list.
                                    # Also how a too-deep nesting lands where
                                    # the parser's recursion limit is high
                                    # enough not to raise -- it differs per
                                    # machine, so BOTH outcomes must be inert.
            ValueError,             # includes UnicodeDecodeError from read_text
            OverflowError,          # pid too large for os.kill's C long (is_pid_alive)
            RecursionError,         # pathologically nested JSON in json.loads
            OSError,
        ) as exc:
            logger.debug("Skipping IDE lockfile %s: %s", path, exc)
    return instances


def get_running_instances(
    claude_dir: Path | None = None,
) -> tuple[list[ClaudeSession], list[IdeInstance]]:
    """Return all running Claude Code sessions and IDE instances."""
    resolved = claude_dir or get_claude_dir()
    return list_sessions(resolved), list_ide_instances(resolved)


def is_codex_running() -> bool:
    """Best-effort check for a live Codex CLI process.

    Codex writes no PID or lock file we can trust for liveness, so this scans
    the OS process table by executable name. It is only used to phrase the
    post-switch reminder ("Codex is running, restart it" vs "ready on next
    launch"); any failure returns ``False`` so detection never blocks a switch.
    """
    try:
        if sys.platform == "win32":
            # Keep bytes to avoid decoding localized tasklist output with the wrong code page.
            completed = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq codex.exe", "/NH"],
                capture_output=True,
                timeout=5,
            )
            return b"codex.exe" in completed.stdout.lower()
        # pgrep -x matches the executable name exactly, so it won't fire on
        # ccswap itself or on unrelated paths that merely contain "codex".
        completed = subprocess.run(
            ["pgrep", "-x", "codex"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return completed.returncode == 0 and bool(completed.stdout.strip())
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("Codex process detection failed: %s", exc)
        return False


def _codex_boot_id(proc_root: Path) -> str:
    boot = (proc_root / "sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    if str(uuid.UUID(boot)) != boot:
        raise ValueError("invalid boot identity")
    return boot


def _codex_linux_snapshot(pid: int, proc_root: Path, boot: str) -> tuple:
    text = (proc_root / str(pid) / "stat").read_text(encoding="ascii", errors="surrogateescape")
    ticks = _stat_start_ticks(text)
    prefix, _, rest = text.rpartition(")")
    fields = rest.split()
    if (ticks is None or not ticks.isascii() or not text.startswith(f"{pid} (")
            or fields[0] not in set("RSDZTtXxKWPI")
            or not fields[1].isascii() or not fields[1].isdecimal()
            or not fields[6].isascii() or not fields[6].isdecimal()):
        raise ValueError("invalid process identity")
    return ({"kind": "linux", "bootId": boot, "ticks": ticks}, fields[0],
            int(fields[6]), int(fields[1]), prefix.partition(" (")[2])


def _codex_pid_gone(pid: int, proc_root: Path) -> bool:
    # A missing stat/environ alone does not prove disappearance.
    try:
        (proc_root / str(pid)).stat()
    except (FileNotFoundError, ProcessLookupError):
        try:
            proc_root.stat()
            return True
        except OSError:
            pass
    except OSError:
        pass
    return False


def codex_process_identity(pid: int, proc_root: Path = Path("/proc")) -> dict | None:
    """Exact process start identity, or None when it cannot be acquired."""
    if sys.platform not in ("linux", "darwin") or type(pid) is not int or pid <= 0:
        return None
    try:
        if sys.platform == "darwin":
            return _codex_macos_snapshot(pid, proc_root, _codex_macos_boot())[0]
        return _codex_linux_snapshot(pid, proc_root, _codex_boot_id(proc_root))[0]
    except (OSError, ValueError):
        return None


def codex_process_state(pid: int, start: dict, proc_root: Path = Path("/proc")) -> str:
    """Return alive, gone or unknown without process-name or clock heuristics."""
    if sys.platform not in ("linux", "darwin") or type(pid) is not int or pid <= 0:
        return "unknown"
    try:
        boot = _codex_macos_boot() if sys.platform == "darwin" else _codex_boot_id(proc_root)
    except (OSError, ValueError):
        return "unknown"
    return _codex_process_state(pid, start, proc_root, boot)


def _codex_process_state(pid: int, start: dict, proc_root: Path, boot: str) -> str:
    if sys.platform == "darwin":
        return _codex_macos_state(pid, start, proc_root, boot)
    if (not isinstance(start, dict) or set(start) != {"kind", "bootId", "ticks"}
            or start["kind"] != "linux" or not isinstance(start["bootId"], str)
            or not isinstance(start["ticks"], str) or not start["ticks"].isascii()
            or not start["ticks"].isdecimal()):
        return "unknown"
    try:
        current = _codex_linux_snapshot(pid, proc_root, boot)[0]
    except (FileNotFoundError, ProcessLookupError):
        return "gone" if _codex_pid_gone(pid, proc_root) else "unknown"
    except (OSError, ValueError):
        return "unknown"
    return "alive" if current == start else "gone"


def _codex_canonical_path(path: Path) -> Path:
    try:
        return path.resolve(strict=True)
    except FileNotFoundError:
        return path.resolve()


def _codex_garbled_env(data: bytes) -> bool:
    """Programs that rewrite their environ area (setproctitle) leave it unparsable."""
    return (not data.endswith(b"\0")
            or any(b"=" not in item for item in data.split(b"\0")[:-1]))


def _codex_env_home(data: bytes, home: Path, directory: Path, proc_root: Path) -> tuple:
    issues = []
    values = []
    for item in data.split(b"\0")[:-1]:
        key, separator, value = item.partition(b"=")
        if key == b"CODEX_HOME" and separator:
            values.append(value)
    if not data.endswith(b"\0") and data.rpartition(b"\0")[2].startswith(b"CODEX_HOME"):
        issues.append("truncated CODEX_HOME")
    if any(value and not os.path.isabs(value) for value in values):
        issues.append("relative CODEX_HOME")
    if any(values) and sys.platform == "linux":
        try:
            if (directory / "ns/mnt").readlink() != (proc_root / "self/ns/mnt").readlink():
                raise ValueError("different namespace")
        except (OSError, ValueError):
            return False, issues + ["foreign mount namespace"]
    matched = False
    for value in values:
        if not value:
            continue
        path = Path(os.fsdecode(value))
        if not path.is_absolute():
            continue
        try:
            path = _codex_canonical_path(path)
            if path == home:
                matched = True
            else:
                source, target = path.stat(), home.stat()
                matched |= (source.st_dev, source.st_ino) == (target.st_dev, target.st_ino)
        except (OSError, ValueError, RuntimeError):
            issues.append("unresolvable CODEX_HOME")
    if len(set(values)) > 1:
        issues.append("contradictory CODEX_HOME")
    return matched, issues


def _codex_zombie_issues(pid: int, proc_root: Path, boot: str) -> list[str]:
    task_root = proc_root / str(pid) / "task"
    tids = [int(path.name) for path in task_root.iterdir() if path.name.isdecimal()]
    if tids == [pid]:
        return []
    if not tids or any(_codex_linux_snapshot(tid, task_root, boot)[1] != "Z" for tid in tids):
        return ["zombie leader with live threads"]
    return []


def _scan_codex_linux_pid(pid: int, home: Path, proc_root: Path, boot: str) -> tuple:
    directory = proc_root / str(pid)
    for _ in range(2):
        try:
            before = _codex_linux_snapshot(pid, proc_root, boot)
            if before[2] & 0x00200000:  # PF_KTHREAD
                return False, [], None
            status = (directory / "status").read_text(encoding="ascii", errors="surrogateescape")
            rows = [row.split()[1:] for row in status.splitlines() if row.startswith("Uid:")]
            if (len(rows) != 1 or len(rows[0]) != 4
                    or any(not uid.isascii() or not uid.isdecimal() for uid in rows[0])):
                raise ValueError("invalid uid")
            denied = False
            result = (False, [])
            if os.getuid() in [int(uid) for uid in rows[0]]:
                try:
                    data = (directory / "environ").read_bytes()
                except PermissionError:
                    denied = True
                else:
                    if data:
                        result = _codex_env_home(data, home, directory, proc_root)
                        # Garbled without a match: attribute like an unreadable one.
                        denied = not result[0] and _codex_garbled_env(data)
                    elif before[1] == "Z":
                        result = (False, _codex_zombie_issues(pid, proc_root, boot))
                    else:
                        denied = True
            after = _codex_linux_snapshot(pid, proc_root, boot)
            if before[0] != after[0]:
                continue
            return result[0], result[1], before if denied else None
        except (FileNotFoundError, ProcessLookupError):
            if _codex_pid_gone(pid, proc_root):
                return False, [], None
            return False, ["incomplete process data"], None
        except (OSError, ValueError):
            return False, ["process data unavailable"], None
    return False, ["process identity changed during scan"], None


def _codex_related_unreadable(snapshot: tuple, pids: set[int], proc_root: Path, boot: str) -> bool:
    if snapshot[4].startswith("codex"):
        return True
    parent, seen = snapshot[3], set()
    for _ in range(4096):
        if parent in pids:
            return True
        if not parent or parent in seen:
            return False
        seen.add(parent)
        try:
            reader = _codex_macos_snapshot if sys.platform == "darwin" else _codex_linux_snapshot
            parent = reader(parent, proc_root, boot)[3]
        except (FileNotFoundError, ProcessLookupError):
            return False
        except (OSError, ValueError):
            return True
    return True  # Incomplete ancestry cannot exclude a writer.


def scan_codex_home(
    home: Path,
    supervisors: list[tuple[int, dict]],
    invalid_records: int = 0,
    proc_root: Path = Path("/proc"),
) -> dict:
    """Scan writers twice; callers validate records and hold the store lock.

    Repeat after reaping and immediately before capture under that lock.
    Residual risk: an orphaned, non-dumpable writer not named codex* cannot
    be attributed; the Linux supervisor will set PR_SET_CHILD_SUBREAPER.
    Exec after the environ read keeps the environment unless replaced;
    clearing CODEX_HOME via execve takes a process outside this home protocol.
    Unreadable counts distinct PIDs with denied, empty or garbled environments
    (e.g. rewritten by setproctitle) across both passes.
    All invocations share one PID namespace; foreign-namespace records are
    out of scope. Never reads run files, manifests or credentials.
    macOS remains gated (no CLI) until the native macOS CI test passes.
    Its denied-environment attribution has the same orphan/name limitation.
    """
    result = {"state": "uncertain", "pids": [], "issues": [], "unreadable": 0}
    if sys.platform not in ("linux", "darwin"):
        result["issues"] = ["unsupported platform"]
        return result
    pids, issues, unreadable = set(), set(), {}
    if invalid_records:
        issues.add("invalid or unreadable run records")
    try:
        home = Path(home)
        if not home.is_absolute():
            raise ValueError("relative home")
        home = _codex_canonical_path(home)
        boot = _codex_macos_boot() if sys.platform == "darwin" else _codex_boot_id(proc_root)
    except (OSError, ValueError, RuntimeError):
        result["issues"] = sorted(issues | {"home or boot identity unavailable"})
        return result
    for pid, start in supervisors:
        state = _codex_process_state(pid, start, proc_root, boot)
        if state == "alive":
            pids.add(pid)
        elif state == "unknown":
            issues.add(f"PID {pid}: supervisor identity unavailable")
    for _ in range(2):
        try:
            entries = (_codex_macos_pids() if sys.platform == "darwin" else
                       [int(p.name) for p in proc_root.iterdir()
                        if p.name.isascii() and p.name.isdecimal()])
        except (OSError, ValueError):
            issues.add("process enumeration unavailable")
            continue
        for pid in entries:
            scanner = _scan_codex_macos_pid if sys.platform == "darwin" else _scan_codex_linux_pid
            matched, errors, denied = scanner(pid, home, proc_root, boot)
            if matched:
                pids.add(pid)
            if denied is not None:
                unreadable.setdefault(pid, []).append(denied)
            issues.update(f"PID {pid}: {error}" for error in errors)
    for pid, snapshots in unreadable.items():
        if any(_codex_related_unreadable(snapshot, pids, proc_root, boot)
               for snapshot in snapshots):
            issues.add(f"PID {pid}: environment unreadable")
    return {"state": "live" if pids else "uncertain" if issues else "quiescent",
            "pids": sorted(pids), "issues": sorted(issues), "unreadable": len(unreadable)}


CTL_KERN, KERN_ARGMAX, KERN_PROC, KERN_PROCARGS2 = 1, 8, 14, 49
KERN_PROC_ALL, KERN_PROC_PID, P_LP64 = 0, 1, 0x4


class _MacTimeval(ctypes.Structure):
    _fields_ = [("tv_sec", ctypes.c_int64), ("tv_usec", ctypes.c_int32)]


@functools.cache
def _codex_macos_abi(arch: str) -> type[ctypes.Structure]:
    """XNU xnu-12377.121.6 LP64 field order; no guessed offsets/packing.

    bsd/sys/proc.h:extern_proc; sys/sysctl.h:1133–1157:user64_kinfo_proc,
    the LP64 copyout selected in kern/kern_sysctl.c:874–875 (e_jobc is int).
    sys/vm.h:vmspace; sys/time.h:itimerval; sys/_types/_timeval.h.
    sys/types.h and sys/_types.h supply integer widths; param.h/syslimits.h
    give MAXCOMLEN=16, NGROUPS=16. i386/arm/_types.h both use long time_t;
    neither adds LP64 packing. mach/{i386,arm}/boolean.h differs in signedness.
    """
    if arch not in ("x86_64", "arm64"):
        raise ValueError("unsupported macOS ABI")
    I, U, P, C = ctypes.c_int32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_char
    H, B, Q = ctypes.c_int16, ctypes.c_ubyte, ctypes.c_uint64

    class Links(ctypes.Structure):
        _fields_ = [("__p_forw", P), ("__p_back", P)]

    class Start(ctypes.Union):
        _fields_ = [("p_st1", Links), ("__p_starttime", _MacTimeval)]

    class Timer(ctypes.Structure):
        _fields_ = [("it_interval", _MacTimeval), ("it_value", _MacTimeval)]

    class ExternProc(ctypes.Structure):
        _fields_ = [
            ("p_un", Start), ("p_vmspace", P), ("p_sigacts", P), ("p_flag", I),
            ("p_stat", C), ("p_pid", I), ("p_oppid", I), ("p_dupfd", I),
            ("user_stack", P), ("exit_thread", P), ("p_debugger", I),
            ("sigwait", U if arch == "x86_64" else I), ("p_estcpu", U),
            ("p_cpticks", I), ("p_pctcpu", U), ("p_wchan", P), ("p_wmesg", P),
            ("p_swtime", U), ("p_slptime", U), ("p_realtimer", Timer),
            ("p_rtime", _MacTimeval), ("p_uticks", Q), ("p_sticks", Q),
            ("p_iticks", Q), ("p_traceflag", I), ("p_tracep", P), ("p_siglist", I),
            ("p_textvp", P), ("p_holdcnt", I), ("p_sigmask", U), ("p_sigignore", U),
            ("p_sigcatch", U), ("p_priority", B), ("p_usrpri", B), ("p_nice", C),
            ("p_comm", C * 17), ("p_pgrp", P), ("p_addr", P),
            ("p_xstat", ctypes.c_uint16), ("p_acflag", ctypes.c_uint16), ("p_ru", P)]

    class Pcred(ctypes.Structure):
        _fields_ = [("pc_lock", C * 72), ("pc_ucred", P), ("p_ruid", U),
                    ("p_svuid", U), ("p_rgid", U), ("p_svgid", U), ("p_refcnt", I)]

    class Ucred(ctypes.Structure):
        _fields_ = [("cr_ref", I), ("cr_uid", U), ("cr_ngroups", H), ("cr_groups", U * 16)]

    class Vmspace(ctypes.Structure):
        _fields_ = [("vm_refcnt", I), ("vm_shm", P), ("vm_rssize", I), ("vm_swrss", I),
                    ("vm_tsize", I), ("vm_dsize", I), ("vm_ssize", I),
                    ("vm_taddr", P), ("vm_daddr", P), ("vm_maxsaddr", P)]

    class Eproc(ctypes.Structure):
        _fields_ = [("e_paddr", P), ("e_sess", P), ("e_pcred", Pcred), ("e_ucred", Ucred),
                    ("e_vm", Vmspace), ("e_ppid", I), ("e_pgid", I), ("e_jobc", I),
                    ("e_tdev", I), ("e_tpgid", I), ("e_tsess", P), ("e_wmesg", C * 8),
                    ("e_xsize", I), ("e_xrssize", H), ("e_xccount", H), ("e_xswrss", H),
                    ("e_flag", I), ("e_login", C * 12), ("e_spare", I * 4)]

    class KinfoProc(ctypes.Structure):
        _fields_ = [("kp_proc", ExternProc), ("kp_eproc", Eproc)]

    if ctypes.sizeof(P) != 8 or ctypes.sizeof(KinfoProc) != 648:
        raise ValueError("macOS ABI size mismatch")
    return KinfoProc


def _codex_macos_sysctl(mib: tuple[int, ...] | str) -> bytes:
    _codex_macos_abi(platform.machine())  # Refuse unsupported ABI before calling libc.
    try:
        libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
        call = libc.sysctlbyname if isinstance(mib, str) else libc.sysctl
    except AttributeError:
        raise OSError("sysctl unavailable") from None
    tail = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p, ctypes.c_size_t]
    call.argtypes = ([ctypes.c_char_p] if isinstance(mib, str) else
                     [ctypes.POINTER(ctypes.c_int), ctypes.c_uint]) + tail
    call.restype = ctypes.c_int
    args = (
        [mib.encode("ascii")] if isinstance(mib, str)
        else [(ctypes.c_int * len(mib))(*mib), len(mib)]
    )
    size = ctypes.c_size_t()
    if call(*args, None, ctypes.byref(size), None, 0):
        raise OSError(ctypes.get_errno(), "sysctl unavailable")
    if not isinstance(mib, str) and mib[:2] == (CTL_KERN, KERN_PROCARGS2):
        maximum = _codex_macos_sysctl((CTL_KERN, KERN_ARGMAX))
        if len(maximum) != 4:
            raise ValueError("invalid argument limit")
        limit = ctypes.c_int32.from_buffer_copy(maximum).value
        if limit <= 0 or size.value > limit + 4:
            raise ValueError("invalid argument limit")
        size.value = limit + 4
    if not 0 <= size.value <= 64 * 1024 * 1024:
        raise ValueError("invalid sysctl size")
    buffer = ctypes.create_string_buffer(size.value)
    capacity = size.value
    if call(*args, buffer, ctypes.byref(size), None, 0):
        raise OSError(ctypes.get_errno(), "sysctl unavailable")
    if size.value > capacity or (not isinstance(mib, str) and mib[:2] == (CTL_KERN, KERN_PROCARGS2)
                                 and size.value == capacity):
        raise ValueError("truncated sysctl buffer")
    return buffer.raw[:size.value]


def _codex_macos_rows(data: bytes) -> list:
    row = _codex_macos_abi(platform.machine())
    if len(data) % ctypes.sizeof(row):
        raise ValueError("macOS ABI size mismatch")
    return [row.from_buffer_copy(data, i) for i in range(0, len(data), ctypes.sizeof(row))]


def _codex_macos_boot() -> str:
    # kern_sysctl.c:SYSCTL_STRING(_kern, OID_AUTO, bootsessionuuid) exposes the
    # boot UUID. Prefer it to kern.boottime, which can move with clock changes.
    data = _codex_macos_sysctl("kern.bootsessionuuid")
    if len(data) != 37 or data[-1:] != b"\0":
        raise ValueError("invalid boot identity")
    boot = data[:-1].decode("ascii")
    if str(uuid.UUID(boot)) != boot:
        raise ValueError("invalid boot identity")
    return boot


def _codex_macos_pids() -> list[int]:
    # ALL avoids omitting setuid processes: accept real OR effective own uid.
    rows = _codex_macos_rows(_codex_macos_sysctl((CTL_KERN, KERN_PROC, KERN_PROC_ALL, 0)))
    pids = [r.kp_proc.p_pid for r in rows if os.getuid() in
            (r.kp_eproc.e_ucred.cr_uid, r.kp_eproc.e_pcred.p_ruid)]
    if any(pid <= 0 for pid in pids) or os.getpid() not in pids:
        raise ValueError("invalid process enumeration")
    return pids


def _codex_macos_snapshot(pid: int, proc_root: Path, boot: str) -> tuple:
    if type(pid) is not int or not 0 < pid <= 2147483647:
        raise ValueError("invalid pid")
    try:
        data = _codex_macos_sysctl((CTL_KERN, KERN_PROC, KERN_PROC_PID, pid))
    except OSError:
        raise ValueError("process query unavailable") from None
    rows = _codex_macos_rows(data)
    if not rows:
        raise ProcessLookupError
    if len(rows) != 1 or rows[0].kp_proc.p_pid != pid:
        raise ValueError("invalid process identity")
    proc, extra = rows[0].kp_proc, rows[0].kp_eproc
    tv = getattr(proc.p_un, "__p_starttime")  # proc.h p_starttime union alias.
    if tv.tv_sec <= 0 or not 0 <= tv.tv_usec < 1000000 or proc.p_stat not in b"\1\2\3\4\5":
        raise ValueError("invalid process identity")
    # A3 uniqueId encodes p_starttime in integer microseconds, without clock slack.
    return ({"kind": "macos", "bootId": boot, "uniqueId": str(tv.tv_sec * 1000000 + tv.tv_usec)},
            proc.p_stat, (extra.e_ucred.cr_uid, extra.e_pcred.p_ruid), extra.e_ppid,
            os.fsdecode(proc.p_comm), 8 if proc.p_flag & P_LP64 else 4)


def _codex_macos_state(pid: int, start: dict, proc_root: Path, boot: str) -> str:
    if (not isinstance(start, dict) or set(start) != {"kind", "bootId", "uniqueId"}
            or start["kind"] != "macos" or not isinstance(start["bootId"], str)
            or not isinstance(start["uniqueId"], str) or not start["uniqueId"].isascii()
            or not start["uniqueId"].isdecimal()):
        return "unknown"
    try:
        current = _codex_macos_snapshot(pid, proc_root, boot)[0]
    except ProcessLookupError:
        return "gone"
    except (OSError, ValueError):
        return "unknown"
    return "alive" if current == start else "gone"


def _codex_macos_environment(data: bytes, pointer_size: int = 8) -> tuple[bytes, bytes]:
    """Return env and any ambiguous tail, without consuming alignment as argv.

    kern_exec.c:6741–6751 aligns path+executable_path= before argv; 6906–6919
    aligns argv+env before apple (possibly ZERO padding); 7070–7084 adds pfz
    first. An aligned pfz without padding is ambiguous: retain the tail only
    as possible-home evidence, never as env or a malformed Apple string.
    """
    if len(data) < 4:
        raise ValueError("truncated procargs")
    argc = ctypes.c_int32.from_buffer_copy(data).value
    if argc <= 0 or argc > len(data):
        raise ValueError("invalid argc")
    end = data.find(b"\0", 4)
    if end <= 4:
        raise ValueError("invalid executable path")
    pos = end + 1
    pos += -(pos - 4 + len(b"executable_path=")) % pointer_size
    for _ in range(argc):
        end = data.find(b"\0", pos)
        if end < 0:
            raise ValueError("truncated argv")
        pos = end + 1
    start = pos
    while pos < len(data):
        if data[pos] == 0:  # Alignment between env[] and applev[].
            return data[start:pos], b""
        if (
            (pos - 4 + len(b"executable_path=")) % pointer_size == 0
            and data[pos:].startswith(b"pfz=")
        ):
            return data[start:pos], data[pos:]
        end = data.find(b"\0", pos)
        if end < 0:
            break
        pos = end + 1
    return data[start:], b""


def _scan_codex_macos_pid(pid: int, home: Path, proc_root: Path, boot: str) -> tuple:
    for _ in range(2):
        try:
            before = _codex_macos_snapshot(pid, proc_root, boot)
            if os.getuid() not in before[2] or before[1] == b"\5":
                if before[0] != _codex_macos_snapshot(pid, proc_root, boot)[0]:
                    continue
                return False, [], None
            denied, result = False, (False, [])
            try:
                data = _codex_macos_sysctl((CTL_KERN, KERN_PROCARGS2, pid))
                try:
                    env, tail = _codex_macos_environment(data, before[5])
                except ValueError:
                    env, tail = b"", data  # Rewritten argv: retain possible home evidence.
                result = _codex_env_home(env, home, proc_root, proc_root)
                if tail:
                    matched, issues = _codex_env_home(tail, home, proc_root, proc_root)
                    result = (result[0] or matched, result[1] + issues)
                denied = not result[0] and (bool(tail) or _codex_garbled_env(env))
            except PermissionError:
                denied = True
            except OSError:
                # XNU answers EINVAL for some surviving processes (e.g. set-id); like denied
                # data, only Codex-related ones count. A vanished pid raises below.
                _codex_macos_snapshot(pid, proc_root, boot)
                denied = True
            after = _codex_macos_snapshot(pid, proc_root, boot)
            if before[0] != after[0]:
                continue
            return result[0], result[1], before if denied else None
        except ProcessLookupError:
            return False, [], None
        except (OSError, ValueError):
            return False, ["process data unavailable"], None
    return False, ["process identity changed during scan"], None
