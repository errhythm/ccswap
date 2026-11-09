"""Real process-boundary tests against fake vendor executables (POSIX only)."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import textwrap
import time

import pytest

from claude_swap.vendors.core import SubprocessRunner

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="fake executables are POSIX scripts"
)

KEY = "mk_live_0123456789abcdef"


def script(path, body):
    path.write_text(f"#!{sys.executable}\n" + textwrap.dedent(body))
    path.chmod(0o755)


@pytest.fixture
def world(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    log = tmp_path / "log.jsonl"
    script(
        bin_dir / "muse",
        f"""
        import json, os, sys
        rec = {{"argv": sys.argv[1:], "key": os.environ.get("META_API_KEY"),
                "stdin": None if sys.stdin.isatty() else sys.stdin.read()}}
        open({str(log)!r}, "a").write(json.dumps(rec) + "\\n")
        if sys.argv[1:2] == ["--version"]:
            print("Muse Code 9.9.9")
        elif sys.argv[1:2] == ["exec"]:
            sys.exit(7)
        elif sys.argv[1:3] == ["usage", "--json"]:
            print(json.dumps({{"schemaVersion": 1, "allowance": None, "reason": "none"}}))
    """,
    )
    env = {
        **os.environ,
        "HOME": str(home),
        "USERPROFILE": str(home),
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "XDG_CONFIG_HOME": str(home / "cfg"),
    }
    env.pop("META_API_KEY", None)
    return env, log


def ccswap(env, *argv, stdin=None):
    return subprocess.run(
        [sys.executable, "-c", "from claude_swap.cli import main; main()", *argv],
        env=env,
        input=stdin,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def calls(log):
    return [json.loads(line) for line in log.read_text().splitlines()]


def test_end_to_end_add_switch_run_through_real_processes(world):
    env, log = world
    done = ccswap(env, "muse", "add", "work", "--api-key-stdin", stdin=KEY + "\n")
    assert done.returncode == 0, done.stderr
    assert KEY not in done.stdout + done.stderr

    done = ccswap(env, "muse", "switch", "work", "--yes", "--json")
    assert done.returncode == 0, done.stderr
    assert KEY not in done.stdout + done.stderr
    set_call = next(c for c in calls(log) if c["argv"][:2] == ["auth", "set"])
    assert set_call["stdin"].strip() == KEY and KEY not in " ".join(set_call["argv"])
    assert set_call["key"] is None  # the env var is not used for the global switch

    done = ccswap(env, "muse", "run", "work", "--", "exec", "hello")
    assert done.returncode == 7  # exit status propagated from the child
    run_call = calls(log)[-1]
    assert run_call["argv"] == ["exec", "hello"] and run_call["key"] == KEY

    secrets = [
        p for p in (env["HOME"] and __import__("pathlib").Path(env["HOME"]).rglob("*.secret"))
    ]
    assert secrets and all(oct(p.stat().st_mode & 0o777) == "0o600" for p in secrets)

    done = ccswap(env, "muse", "usage", "--json")
    assert json.loads(done.stdout)["state"] == "unavailable"


def test_vendors_cli_reports_missing_binaries_without_traceback(world, tmp_path):
    env, _ = world
    empty = tmp_path / "empty"
    empty.mkdir()
    env = {**env, "PATH": f"{empty}{os.pathsep}/usr/bin{os.pathsep}/bin"}
    done = ccswap(env, "vendors", "--json")
    assert done.returncode == 0 and "Traceback" not in done.stderr
    assert {v["tool"]: v["installed"] for v in json.loads(done.stdout)["vendors"]}["muse"] is False


def test_runner_timeout_and_missing(tmp_path):
    slow = tmp_path / "slow"
    script(slow, "import time; time.sleep(30)\n")
    runner = SubprocessRunner()
    started = time.monotonic()
    result = runner.run([str(slow)], timeout=0.5)
    assert result.timed_out and time.monotonic() - started < 10
    assert runner.run([str(tmp_path / "nope")]).missing


def test_runner_passes_stdin_and_env_not_argv(tmp_path):
    echo = tmp_path / "echo"
    script(
        echo,
        "import os,sys,json; print(json.dumps([os.environ.get('SECRET_X'), sys.stdin.read()]))\n",
    )
    result = SubprocessRunner().run([str(echo)], env={"SECRET_X": "e"}, input_text="in")
    assert json.loads(result.stdout) == ["e", "in"]


def test_runner_unset_env(tmp_path, monkeypatch):
    monkeypatch.setenv("KIRO_API_KEY", "should-vanish")
    probe = tmp_path / "probe"
    script(probe, "import os; print(os.environ.get('KIRO_API_KEY'))\n")
    result = SubprocessRunner().run([str(probe)], unset_env=("KIRO_API_KEY",))
    assert result.stdout.strip() == "None"


def test_interactive_exit_code_and_signal_forwarding(tmp_path):
    child = tmp_path / "child"
    script(child, "import time; time.sleep(60)\n")
    parent = tmp_path / "parent.py"
    parent.write_text(
        textwrap.dedent(f"""
        import sys
        from claude_swap.vendors.core import SubprocessRunner
        print("ready", flush=True)
        sys.exit(SubprocessRunner().run_interactive([{str(child)!r}]))
    """)
    )
    proc = subprocess.Popen([sys.executable, str(parent)], stdout=subprocess.PIPE, text=True)
    assert proc.stdout.readline().strip() == "ready"
    time.sleep(0.5)
    proc.send_signal(signal.SIGTERM)
    assert proc.wait(timeout=15) == 143  # child was terminated, not orphaned
    assert SubprocessRunner().run_interactive([str(tmp_path / "missing")]) == 127
