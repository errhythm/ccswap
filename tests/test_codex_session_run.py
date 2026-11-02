"""Supervisor transaction tests: fake writers, native processes and policy sources."""

import json
import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from claude_swap import codex_binary, codex_session as cs
from claude_swap.codex_session import slot_custody as observe_custody
from tests.test_codex_reconcile import custody
from tests.test_codex_session import auth


@pytest.fixture
def run(custody, monkeypatch, tmp_path):
    switcher, home, baseline = custody
    # This fixture models POSIX Popen; Windows job tests override it explicitly.
    class PosixSys:
        platform = "linux"
        def __getattr__(self, name):
            return getattr(sys, name)
    monkeypatch.setattr(cs, "sys", PosixSys())
    switcher.lock_file = switcher.backup_dir / ".lock"
    switcher.auth_file = switcher.codex_home / "auth.json"
    cwd = tmp_path / "project"
    cwd.mkdir()
    state = SimpleNamespace(default=None, aliases=[], before="quiescent", after="quiescent",
                            spawned=[], code=0, fail=False, prepared=False)
    monkeypatch.setattr(codex_binary, "resolve_codex_binary", lambda env: ["/fake/codex"])
    monkeypatch.setattr(codex_binary, "probe_codex", lambda binary: None)
    monkeypatch.setattr(cs, "_system_policy_paths", lambda: [])
    # Windows temp roots can be below the real user's .codex project layer.
    project_paths = cs._project_policy_paths
    monkeypatch.setattr(cs, "_project_policy_paths", lambda cwd: [
        path for path in project_paths(cwd) if path.is_relative_to(tmp_path)])
    monkeypatch.setattr(cs, "_run_subreaper", lambda: None)
    monkeypatch.setattr(cs, "_run_reap", lambda: None)
    import signal
    monkeypatch.setattr(signal, "SIGHUP", 1, raising=False)
    handlers = {}
    def install(signum, handler):
        previous = handlers.get(signum, signal.SIG_DFL)
        handlers[signum] = handler
        return previous
    monkeypatch.setattr(signal, "signal", install)
    monkeypatch.setattr(cs, "_run_identity", lambda pid: {
        "pid": pid, "start": {"kind": "linux", "bootId": "boot", "ticks": "1"}})
    monkeypatch.setattr(switcher, "_read_switch_auth", lambda: state.default)
    monkeypatch.setattr(switcher, "_default_session_slots", lambda default, data: state.aliases)
    def scan(*args, **kwargs):
        return cs.Custody(state.after if state.spawned else state.before, "fake")
    monkeypatch.setattr(cs, "slot_custody", scan)
    class Process:
        pid = 54321
        def wait(self):
            if not state.aliases and not getattr(state, "killed", False):
                record = cs.load_record(next((home / "runs").iterdir()), "run")
                assert record["phase"] == "running" and record["child"]["pid"] == self.pid
            return state.code
        def send_signal(self, signum):
            state.signal = signum
        def kill(self):
            state.killed = True
    def spawn(argv, **kwargs):
        if not state.aliases:
            record = cs.load_record(next((home / "runs").iterdir()), "run")
            assert record["phase"] == "starting" and record["child"] is None
        state.spawned.append((argv, kwargs))
        if state.fail:
            raise OSError("secret prompt must not leak")
        return Process()
    monkeypatch.setattr("subprocess.Popen", spawn)
    def invoke(identifier="1", **overrides):
        options = dict(require_session=False, shared=False, codex_args=["exec", "hello"],
                       env={"PATH": "/fake"}, cwd=cwd)
        options.update(overrides)
        return cs.run_session(switcher, identifier, **options)
    state.invoke, state.home, state.switcher, state.cwd = invoke, home, switcher, cwd
    return state


@pytest.mark.parametrize("code,expected", [(0, 0), (7, 7), (-15, 143)])
def test_record_order_and_exit(run, capsys, code, expected):
    run.code = code
    assert run.invoke() == expected
    record = cs.load_record(next((run.home / "runs").iterdir()), "run")
    assert record["phase"] == "finished" and record["exitCode"] == expected
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("require,expected", [(False, 0), (True, 1)])
def test_default_and_omitted(run, capsys, require, expected):
    run.default, run.aliases = auth(), ["1"]
    env = {"PATH": "/fake", "OPENAI_API_KEY": "synthetic secret"}
    args = ["exec", "hello"]
    assert run.invoke(None, require_session=require, env=env, codex_args=args) == expected
    if not require:
        argv, kwargs = run.spawned[0]
        assert argv == ["/fake/codex", *args] and kwargs["env"] is env
        assert "not isolated" in capsys.readouterr().err
    else:
        assert not run.spawned
        assert "--require-session refuses" in capsys.readouterr().err
    assert not (run.home / "runs").exists()


@pytest.mark.parametrize("state,shared,expected", [
    ("live", False, 1), ("live", True, 0), ("uncertain", False, 1),
    ("uncertain", True, 1), ("conflict", True, 1),
])
def test_custody_refusal_and_shared_join(run, monkeypatch, capsys, state, shared, expected):
    run.before = state
    if shared and state == "live":
        monkeypatch.setattr(run.switcher, "_guard_session_slot",
            lambda n: pytest.fail("reconcile on join"))
        monkeypatch.setattr(cs, "reconcile_session", lambda *a,
            **kw: cs.ReconcileResult("unchanged")
                            if run.spawned else pytest.fail("reconcile on join"))
    assert run.invoke(shared=shared) == expected
    assert bool(run.spawned) == (expected == 0)
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("state", ["live", "uncertain", "conflict"])
def test_default_never_bypasses_existing_session(run, state):
    run.default, run.aliases, run.before = auth(), ["1"], state
    assert run.invoke(shared=True) == 1
    assert not run.spawned


@pytest.mark.parametrize("after,expected", [("live", 9), ("uncertain", 1)])
def test_post_exit_scan(run, monkeypatch, capsys, after, expected):
    run.after, run.code = after, 9
    monkeypatch.setattr(cs.time, "monotonic", iter([0, 0, 2, 4, 6]).__next__)
    assert run.invoke() == expected
    output = capsys.readouterr()
    assert output.out == ""
    assert ("capture pending" in output.err) == (after == "live")


def test_spawn_failure_finished_and_redacted(run, capsys):
    run.fail = True
    assert run.invoke() == 1
    record = cs.load_record(next((run.home / "runs").iterdir()), "run")
    assert record["phase"] == "finished" and record["exitCode"] == 1
    assert "secret prompt" not in capsys.readouterr().err


@pytest.mark.parametrize("kind", ["policy", "plan", "syntax", "disabled", "unmanaged"])
def test_preflight_refuses_without_record(run, kind):
    if kind == "policy":
        (run.cwd / "config.toml").write_text('forced_login_method = "api"')
    elif kind == "plan":
        (run.home / "auth.json").write_text(json.dumps(auth(chatgpt_plan_type="business")))
    elif kind == "disabled":
        data = run.switcher._read_sequence()
        data["accounts"]["1"]["disabled"] = True
        run.switcher.sequence_file.write_text(json.dumps(data))
    result = run.invoke(None if kind == "unmanaged" else "1",
                        codex_args=["login"] if kind == "syntax" else ["exec", "hello"])
    assert result == (2 if kind == "syntax" else 1)
    assert not run.spawned and not (run.home / "runs").exists()


def test_new_home_bootstrap(run, monkeypatch):
    # A distinct synthetic slot exercises the real bootstrap and sharing helpers.
    data = run.switcher._read_sequence()
    data["accounts"]["2"] = data["accounts"].pop("1")
    run.switcher.sequence_file.write_text(json.dumps(data))
    run.switcher._credential_path("2").write_text(json.dumps(auth()))
    run.home = cs.slot_home(run.switcher, "2")
    run.before = "none"
    monkeypatch.setattr(cs, "slot_custody", lambda *a, **kw:
                        cs.Custody("quiescent" if run.spawned else "none", "fake"))
    # The fake process's record observer follows the new home through this reference.
    monkeypatch.setattr("subprocess.Popen", lambda *a, **kw: _new_process(run))
    assert run.invoke("2") == 0
    assert cs.load_record(run.home / ".ccswap-session.json", "manifest")[
        "bootstrapState"] == "ready"


def _new_process(run):
    record = cs.load_record(next((run.home / "runs").iterdir()), "run")
    assert record["phase"] == "starting"
    run.spawned.append(True)
    return SimpleNamespace(pid=54321, wait=lambda: 0, send_signal=lambda s: None, kill=lambda: None)


def test_env_names_only_and_parent_unchanged(run, capsys):
    env = {"OPENAI_API_KEY": "never-print-this", "CODEX_SQLITE_HOME": "/outside",
           "CODEX_GUARDIAN_DECISIONS_API_KEY": "preserved"}
    assert run.invoke(env=env) == 0
    child = run.spawned[0][1]["env"]
    assert child["CODEX_HOME"] == str(run.home)
    assert "OPENAI_API_KEY" not in child and "CODEX_SQLITE_HOME" not in child
    assert child["CODEX_GUARDIAN_DECISIONS_API_KEY"] == "preserved"
    output = capsys.readouterr()
    assert "Ignoring OPENAI_API_KEY, CODEX_SQLITE_HOME" in output.err
    assert "never-print-this" not in output.err and output.out == ""
    assert env["OPENAI_API_KEY"] == "never-print-this"


def test_inherited_home_contract(run, monkeypatch):
    assert "before constructing switcher" in cs.run_session.__doc__
    monkeypatch.setattr(run.switcher, "_read_sequence", lambda: pytest.fail("account discovery"))
    assert run.invoke(env={"CODEX_HOME": "/foreign"}) == 1


def test_all_config_candidates_and_managed_flags(run, monkeypatch):
    system = run.cwd / "system"
    system.mkdir()
    config, requirements = system / "config.toml", system / "requirements.toml"
    config.write_text('model_provider = "external"')
    requirements.write_text('sqlite_home = "/outside"')
    project = run.cwd / ".codex"
    project.mkdir()
    (project / "config.toml").write_text('log_dir = "logs"')
    monkeypatch.setattr(cs, "_system_policy_paths", lambda: [config, requirements])
    layers = cs._run_layers(run.home, run.cwd)
    assert (config, {"model_provider": "external"}, False) in layers
    assert (requirements, {"sqlite_home": "/outside"}, True) in layers
    assert any(source == project / "config.toml" for source, _, _ in layers)
    assert run.invoke() == 1 and not run.spawned


@pytest.mark.parametrize("phase", ["starting", "running", "finished"])
def test_record_write_failure(run, monkeypatch, phase):
    original = cs._session_write
    def persist(tree, path, value):
        if path.parent.name == "runs" and value["phase"] == phase:
            raise OSError("redacted")
        return original(tree, path, value)
    monkeypatch.setattr(cs, "_session_write", persist)
    assert run.invoke() == 1
    assert bool(run.spawned) == (phase != "starting")


def test_binary_gate_before_record(run, monkeypatch):
    from claude_swap.exceptions import SessionError
    def probe(binary):
        raise SessionError("unsupported binary")
    monkeypatch.setattr(codex_binary, "probe_codex", probe)
    assert run.invoke() == 1
    assert not run.spawned and not (run.home / "runs").exists()


def test_default_never_captures(run):
    run.default, run.aliases = auth(), ["1"]
    rotated = dict(auth(), rotation="pending")
    (run.home / "auth.json").write_text(json.dumps(rotated))
    before = run.switcher._credential_path("1").read_bytes()
    assert run.invoke() == 0
    assert run.switcher._credential_path("1").read_bytes() == before


def test_final_capture_and_conflict(run, monkeypatch):
    original = cs._run_wait
    def wait(process):
        result = original(process)
        (run.home / "auth.json").write_text(json.dumps(dict(auth(), rotated=True)))
        return result
    monkeypatch.setattr(cs, "_run_wait", wait)
    assert run.invoke() == 0
    assert run.switcher._read_session_backup("1")["rotated"] is True


def test_shared_generation_conflict_preserves_files(run):
    run.before = "live"
    run.switcher._credential_path("1").write_text(json.dumps(dict(auth(), other_generation=True)))
    before = (run.home / "auth.json").read_bytes()
    assert run.invoke(shared=True) == 1
    assert not run.spawned and (run.home / "auth.json").read_bytes() == before


def test_signal_forwarding_and_restore(monkeypatch):
    import signal
    monkeypatch.setattr(cs, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(signal, "SIGHUP", 1, raising=False)
    handlers, sent = {}, []
    def install(signum, handler):
        previous = handlers.get(signum, "original")
        handlers[signum] = handler
        return previous
    monkeypatch.setattr(signal, "signal", install)
    def wait():
        # The terminal delivers SIGINT to the child directly; the supervisor ignores it.
        assert handlers[signal.SIGINT] is signal.SIG_IGN
        for signum in (signal.SIGTERM, signal.SIGHUP):
            handlers[signum](signum, None)
        return -2
    process = SimpleNamespace(wait=wait, send_signal=sent.append)
    assert cs._run_wait(process) == 130
    assert sent == [signal.SIGTERM, signal.SIGHUP]
    assert set(handlers.values()) == {"original"}


def test_child_cwd_drives_discovery(run):
    sub = run.cwd / "child"
    sub.mkdir()
    (sub / "config.toml").write_text('model_provider = "external"')
    assert run.invoke(codex_args=["-C", "child", "exec", "hello"]) == 1
    assert not run.spawned


def test_unknown_supervisor_identity_before_record(run, monkeypatch):
    from claude_swap.exceptions import SessionError
    monkeypatch.setattr(cs, "_run_identity",
        lambda pid: (_ for _ in ()).throw(SessionError("unknown")))
    assert run.invoke() == 1
    assert not run.spawned and not (run.home / "runs").exists()


def test_inherited_preconstruction_example():
    from claude_swap.exceptions import SessionError
    constructed = []
    def caller(environment):
        cs.refuse_inherited_home(environment)
        constructed.append(True)
    with pytest.raises(SessionError):
        caller({"CODEX_HOME": "/foreign"})
    assert not constructed


def test_mismatching_identity_token_collision_refused(run):
    run.default = auth(chatgpt_user_id="other")
    assert run.invoke() == 1 and not run.spawned


def test_subreaper_failure_before_record(run, monkeypatch):
    from claude_swap.exceptions import SessionError
    def fail():
        raise SessionError("subreaper unavailable")
    monkeypatch.setattr(cs, "_run_subreaper", fail)
    assert run.invoke() == 1 and not run.spawned
    assert not (run.home / "runs").exists()


def test_windows_job_callback_and_wait(run, monkeypatch):
    from claude_swap import windows_job
    events = []
    monkeypatch.setattr(cs, "sys",
        SimpleNamespace(platform="win32", stderr=__import__("sys").stderr))
    monkeypatch.setattr(cs, "_windows_requirements_path", lambda: None)
    monkeypatch.setattr(cs, "_run_identity", lambda pid: {
        "pid": pid, "start": {"kind": "windows", "creationTime100ns": "1"}})
    def launch(binary, args, env, cwd, run_id, callback):
        record = cs.load_record(run.home / "runs" / (run_id + ".json"), "run")
        assert record["phase"] == "starting"
        assert record["jobName"] == windows_job.job_name(run_id)
        callback(54321, {"kind": "windows", "creationTime100ns": "2"})
        assert cs.load_record(run.home / "runs" / (run_id + ".json"), "run")["phase"] == "running"
        events.append("resume")
        run.spawned.append(True)
        return SimpleNamespace(wait=lambda: events.append("whole-job-wait") or 42,
                               close=lambda: events.append("close"))
    monkeypatch.setattr(windows_job, "launch", launch)
    assert run.invoke() == 42
    assert events == ["resume", "whole-job-wait", "close"]


@pytest.mark.parametrize("child_state,expected", [("gone", "quiescent"), ("unknown", "uncertain")])
def test_windows_completed_reservation_still_checks_child(run, monkeypatch, child_state, expected):
    from uuid import uuid4
    from claude_swap import windows_job
    run_id = str(uuid4())
    supervisor = {"pid": 123, "start": {"kind": "windows", "creationTime100ns": "1"}}
    child = {"pid": 456, "start": {"kind": "windows", "creationTime100ns": "2"}}
    record = dict(schemaVersion=1, runId=run_id, home=str(run.home), supervisor=supervisor,
                  child=child, phase="finished", shared=False,
                  jobName=windows_job.job_name(run_id), exitCode=0)
    cs._private_directory(run.home / "runs", run.switcher.backup_dir)
    cs._session_write(run.switcher.backup_dir, run.home / "runs" / (run_id + ".json"), record)
    monkeypatch.setattr(cs, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(windows_job, "query_job", lambda rid: "gone")
    def state(pid, start):
        assert pid == child["pid"], "only the completed reservation is excluded"
        return child_state
    monkeypatch.setattr(windows_job, "process_state", state)
    monkeypatch.setattr(windows_job, "wait_identity_gone", lambda *a: child_state)
    assert observe_custody(run.switcher, "1", completed=supervisor).state == expected


@pytest.mark.parametrize("tokens", [None, [], "invalid"])
def test_default_malformed_tokens_never_crash(run, tokens):
    run.default = {"tokens": tokens}
    assert run.invoke() == (0 if tokens is None else 1)


def test_shared_email_drift_refused(run):
    import base64
    run.before = "live"
    document = auth()
    token = document["tokens"]["id_token"]
    payload = json.loads(base64.urlsafe_b64decode(token.split(".")[1] + "=="))
    payload["email"] = "different@example.org"
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    document["tokens"]["id_token"] = f"e30.{encoded}.sig"
    (run.home / "auth.json").write_text(json.dumps(document))
    assert run.invoke(shared=True) == 1 and not run.spawned


@pytest.mark.parametrize("identifier", [None, "1", "user@example.org"])
def test_real_default_identity_resolution(run, monkeypatch, identifier):
    from claude_swap.codex import CodexAccountSwitcher
    run.default, run.aliases = auth(), ["1"]
    monkeypatch.setattr(run.switcher, "_default_session_slots",
                        CodexAccountSwitcher._default_session_slots.__get__(run.switcher))
    assert run.invoke(identifier) == 0
    assert not (run.home / "runs").exists()


def test_final_generation_conflict_keeps_evidence(run, monkeypatch):
    original = cs._run_wait
    def wait(process):
        result = original(process)
        (run.home / "auth.json").write_text(json.dumps(dict(auth(), rotated="home")))
        run.switcher._credential_path("1").write_text(json.dumps(dict(auth(), rotated="backup")))
        return result
    monkeypatch.setattr(cs, "_run_wait", wait)
    assert run.invoke() == 1
    assert cs.load_document(run.home / "auth.json")["rotated"] == "home"
    assert run.switcher._read_session_backup("1")["rotated"] == "backup"


def test_last_orphan_exit_allows_capture(run, monkeypatch, capsys):
    scans = iter(["quiescent", "quiescent", "live", "quiescent"])
    monkeypatch.setattr(cs, "slot_custody", lambda *a, **kw: cs.Custody(next(scans), "fake"))
    monkeypatch.setattr(cs, "time", SimpleNamespace(monotonic=lambda: 0, sleep=lambda delay: None))
    assert run.invoke() == 0
    assert "capture pending" not in capsys.readouterr().err


@pytest.mark.parametrize("result", [0, -1])
def test_linux_subreaper_prctl(monkeypatch, result):
    from claude_swap.exceptions import SessionError
    calls = []
    # Retrieve the implementation, rather than the run fixture's injected seam.
    monkeypatch.setattr(cs, "sys",
        SimpleNamespace(platform="linux", stderr=__import__("sys").stderr))
    def prctl(*args):
        calls.append(args)
        return result
    monkeypatch.setattr(cs.ctypes, "CDLL", lambda *a, **kw: SimpleNamespace(prctl=prctl))
    if result:
        with pytest.raises(SessionError):
            cs._run_subreaper()
    else:
        cs._run_subreaper()
    assert calls == [(36, 1, 0, 0, 0)]


@pytest.mark.parametrize("original", [None, "original"])
def test_windows_wait_ignores_console_interrupts(monkeypatch, original):
    import signal
    handlers = {}
    def install(signum, handler):
        previous = handlers.get(signum, original)
        handlers[signum] = handler
        return previous
    monkeypatch.setattr(signal, "signal", install)
    monkeypatch.setattr(signal, "SIGBREAK", 21, raising=False)
    monkeypatch.setattr(cs, "sys", SimpleNamespace(platform="win32"))
    def wait():
        # Ctrl-C reaches Codex through the console; no KeyboardInterrupt may skip the finish.
        assert handlers[signal.SIGINT] is signal.SIG_IGN and handlers[21] is signal.SIG_IGN
        return 3
    assert cs._run_wait(SimpleNamespace(wait=wait)) == 3
    assert set(handlers.values()) == {signal.SIG_DFL if original is None else original}


def test_live_default_alias_does_not_block_other_account(run, monkeypatch):
    other = auth(chatgpt_user_id="other-user")
    other["tokens"].update(access_token="other-access", refresh_token="other-refresh")
    run.default, run.aliases = other, ["2"]
    original = run.switcher._read_session_backup
    monkeypatch.setattr(run.switcher, "_read_session_backup",
                        lambda n: other if n == "2" else original(n))
    checked = []
    real = cs._run_custody
    monkeypatch.setattr(cs, "_run_custody",
                        lambda s, n, shared: checked.append(n) or real(s, n, shared))
    assert run.invoke("1") == 0
    assert checked == ["1"]


def test_capture_failure_after_exit_is_not_reported_as_unstarted(run, monkeypatch, capsys):
    def busy(*args, **kwargs):
        raise cs.SessionError("Codex run refused.")
    monkeypatch.setattr(cs, "_run_finish", busy)
    assert run.invoke() == 1
    err = capsys.readouterr().err
    assert "Codex exited, but the session capture failed" in err and "not started" not in err


@pytest.mark.parametrize("args", [
    ["--worktree"], ["--worktree", "resume", "--last"],
    ["resume", "--last", "-c", 'sqlite_home="db"'],
    ["fork", "thread", "-c", 'log_dir="logs"'],
    ["exec", "resume", "--last", "-c", 'sqlite_home="db"'],
])
def test_unsupported_syntax_has_no_side_effects(run, monkeypatch, args):
    before = {p: p.read_bytes() for p in run.home.rglob("*") if p.is_file()}
    def unexpected(*args, **kwargs):
        pytest.fail("syntax rejection must precede probes and bootstrap")
    monkeypatch.setattr(codex_binary, "resolve_codex_binary", unexpected)
    monkeypatch.setattr(cs, "bootstrap_home", unexpected)
    assert run.invoke(codex_args=args) == 2
    assert not run.spawned
    assert {p: p.read_bytes() for p in run.home.rglob("*") if p.is_file()} == before


def test_run_layers_orders_project_configs_root_first(run, monkeypatch):
    outer = run.cwd / ".codex" / "config.toml"
    inner_dir = run.cwd / "nested"
    inner = inner_dir / ".codex" / "config.toml"
    for path, model in ((outer, "outer"), (inner, "inner")):
        path.parent.mkdir(parents=True)
        path.write_text(f'model = "{model}"')
    monkeypatch.setattr(cs, "_system_policy_paths", lambda: [])
    layers = cs._run_layers(run.home, inner_dir)
    assert [(path, table, managed) for path, table, managed in layers
            if path in {outer, inner}] == [
        (outer, {"model": "outer"}, False), (inner, {"model": "inner"}, False),
    ]


def test_finish_custody_conflict_reports_retained_evidence(run, capsys):
    run.after = "conflict"
    before = (run.home / "auth.json").read_bytes()
    assert run.invoke() == 1
    err = capsys.readouterr().err
    assert "session/backup identity or generation conflict" in err
    assert f"files retained at {run.home}" in err
    assert "Reconcile the login explicitly before retrying" in err
    assert "liveness is uncertain" not in err
    assert (run.home / "auth.json").read_bytes() == before


@pytest.mark.parametrize("previous", [None, "original"])
@pytest.mark.parametrize("fail", [False, True])
def test_interrupt_guard_covers_finish_and_restores(run, monkeypatch, previous, fail):
    import signal

    handlers = {}
    def install(signum, handler):
        old = handlers.get(signum, previous)
        handlers[signum] = handler
        return old
    monkeypatch.setattr(signal, "signal", install)
    monkeypatch.setattr(signal, "SIGBREAK", 21, raising=False)
    monkeypatch.setattr(cs, "_run_identity", lambda pid: {
        "pid": pid, "start": {"kind": "windows", "creationTime100ns": "1"}})
    monkeypatch.setattr(cs, "_run_notice", lambda message: None)
    checked = []
    def finish(*args):
        assert handlers[signal.SIGINT] is signal.SIG_IGN
        assert handlers[signal.SIGBREAK] is signal.SIG_IGN
        checked.append(True)
        if fail:
            raise cs.SessionError("synthetic finish failure")
        return 0
    monkeypatch.setattr(cs, "_run_finish", finish)
    from claude_swap import windows_job
    def launch(binary, args, env, cwd, run_id, callback):
        callback(54321, {"kind": "windows", "creationTime100ns": "2"})
        run.spawned.append(True)
        return SimpleNamespace(wait=lambda: 0, close=lambda: None)
    monkeypatch.setattr(cs, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(cs, "_windows_requirements_path", lambda: None)
    monkeypatch.setattr(windows_job, "launch", launch)
    assert run.invoke() == (1 if fail else 0)
    assert checked == [True]
    restored = signal.SIG_DFL if previous is None else previous
    assert handlers == {signal.SIGINT: restored, signal.SIGBREAK: restored}


@pytest.mark.parametrize("name", ["requirements.toml", "managed_config.toml"])
@pytest.mark.parametrize("text,refused", [
    ('chatgpt_base_url = "https://collector.example"', True),
    ('model_provider = "external"', True),
    ('[model_providers.x]\nbase_url = "https://collector.example"', True),
    ('cli_auth_credentials_store = "keyring"', True),
    ('cli_auth_credentials_store = "auto"', True),
    ('mcp_oauth_credentials_store = "keyring"', True),
    ('cli_auth_credentials_store = "file"', False),
])
def test_managed_requirements_cannot_reroute_child(run, monkeypatch, capsys, name, text,
                                                    refused):
    # Managed layers override the child's -c values, so incompatible ones refuse the run.
    requirements = run.cwd / name
    requirements.write_text(text)
    monkeypatch.setattr(cs, "_system_policy_paths", lambda: [requirements])
    assert run.invoke() == (1 if refused else 0)
    assert bool(run.spawned) is not refused
    if refused:
        assert "No new session writer started" in capsys.readouterr().err


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission modes")
def test_reused_home_credentials_made_private(run):
    run.home.chmod(0o755)
    (run.home / "auth.json").chmod(0o644)
    assert run.invoke() == 0
    assert stat.S_IMODE(run.home.stat().st_mode) == 0o700
    assert stat.S_IMODE((run.home / "auth.json").stat().st_mode) == 0o600


def test_shared_config_explicit_file_stores_launch_isolated_child(run):
    config = run.switcher.codex_home / "config.toml"
    config.write_text('cli_auth_credentials_store = "file"\n'
                      'mcp_oauth_credentials_store = "file"\n')
    assert run.invoke(require_session=True) == 0
    argv, kwargs = run.spawned[0]
    assert kwargs["env"]["CODEX_HOME"] == str(run.home)
    assert argv.count('cli_auth_credentials_store="file"') == 1
    assert argv.count('mcp_oauth_credentials_store="file"') == 1
    assert (run.home / "config.toml").read_text() == config.read_text()


def test_hard_linked_session_credentials_refused(run, tmp_path, capsys):
    os.link(run.home / "auth.json", tmp_path / "copy.json")
    assert run.invoke() == 1 and not run.spawned
    assert "another hard link" in capsys.readouterr().err


@pytest.mark.parametrize('platform', ['linux', 'darwin', 'win32'])
def test_plain_start_skips_session_probe(run, monkeypatch, platform):
    run.default, run.aliases = auth(), ['1']
    monkeypatch.setattr(cs.sys, 'platform', platform)
    monkeypatch.setattr(codex_binary, 'probe_codex', lambda *_: pytest.fail('session probe'))
    assert run.invoke() == 0


def test_macos_isolated_refusal_precedes_binary_resolution(run, monkeypatch, capsys):
    monkeypatch.setattr(cs.sys, 'platform', 'darwin')
    monkeypatch.setattr(codex_binary, 'resolve_codex_binary', lambda *_: pytest.fail('resolver'))
    assert run.invoke() == 1
    assert 'macOS session supervision' in capsys.readouterr().err


def test_account_disabled_during_probe_is_rechecked(run, monkeypatch):
    def probe(_):
        data = run.switcher._read_sequence()
        data['accounts']['1']['disabled'] = True
        run.switcher.sequence_file.write_text(json.dumps(data))
    monkeypatch.setattr(codex_binary, 'probe_codex', probe)
    assert run.invoke() == 1
    assert not run.spawned


def test_default_changed_during_probe_has_retry_message(run, monkeypatch, capsys):
    def probe(_):
        run.default, run.aliases = auth(), ['1']
    monkeypatch.setattr(codex_binary, 'probe_codex', probe)
    assert run.invoke() == 1
    assert not run.spawned
    assert 'state changed during preflight; retry' in capsys.readouterr().err
