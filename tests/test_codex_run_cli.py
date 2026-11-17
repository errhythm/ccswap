"""CLI boundary tests; all launches are mocked and accounts use temporary homes."""

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from ccswap import cli, codex_session
from ccswap.appearance import cli_should_probe
from tests.test_codex_reconcile import custody


@pytest.fixture
def launch(monkeypatch):
    monkeypatch.delenv("CODEX_HOME", raising=False)
    constructor = Mock()
    runner = Mock(return_value=0)
    monkeypatch.setattr(cli, "CodexAccountSwitcher", constructor)
    monkeypatch.setattr(codex_session, "run_session", runner)
    return constructor, runner


@pytest.mark.parametrize("wrapper,identifier,required,shared", [
    ([], None, False, False),
    (["2"], "2", False, False),
    (["user@example.org", "--require-session"], "user@example.org", True, False),
    (["--shared", "2", "--require-session"], "2", True, True),
    (["--require-session", "--shared"], None, True, True),
])
def test_wrapper_and_untouched_tail(launch, capsys, wrapper, identifier, required, shared):
    constructor, runner = launch
    tail = ["exec", "--json", "-c", 'model="-value"', "--", "--shared", "-prompt"]
    with pytest.raises(SystemExit) as exited:
        cli._codex_command(["run", *wrapper, "--", *tail])
    assert exited.value.code == 0
    constructor.assert_called_once_with(debug=False)
    positional, keywords = runner.call_args
    assert positional == (constructor.return_value, identifier)
    assert keywords == dict(require_session=required, shared=shared, codex_args=tail,
                            env=os.environ, cwd=Path.cwd())
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("tail", [[], ["--require-session"], ["--shared", "--json", "--help"],
                                  ["exec", "--output-schema", "-schema", "--", "--"]])
def test_tail_never_parsed_as_wrapper(launch, tail):
    constructor, runner = launch
    with pytest.raises(SystemExit) as exited:
        cli._codex_command(["run", "--", *tail])
    assert exited.value.code == 0
    assert runner.call_args.kwargs["codex_args"] == tail
    assert runner.call_args.args[1] is None
    assert not runner.call_args.kwargs["shared"]
    assert not runner.call_args.kwargs["require_session"]


@pytest.mark.parametrize("arguments", [
    ["--json"], ["--strategy", "best"], ["--unknown", "private-prompt"],
    ["--sha"], ["--recover"], ["2", "exec"], ["--profile", "secret-profile"],
])
def test_invalid_wrapper_flags_exit_two_without_launch(launch, capsys, arguments):
    constructor, runner = launch
    with pytest.raises(SystemExit) as exited:
        cli._codex_command(["run", *arguments, "--", "private-child-prompt"])
    assert exited.value.code == 2
    constructor.assert_not_called()
    runner.assert_not_called()
    output = capsys.readouterr()
    assert output.out == "" and "Invalid Codex run arguments" in output.err
    assert "private-prompt" not in output.err and "private-child-prompt" not in output.err
    assert "secret-profile" not in output.err


def test_inherited_home_refused_before_construction(launch, monkeypatch, capsys):
    constructor, runner = launch
    monkeypatch.setenv("CODEX_HOME", "/private-inherited-home")
    with pytest.raises(SystemExit) as exited:
        cli._codex_command(["run", "2"])
    assert exited.value.code == 1
    constructor.assert_not_called()
    runner.assert_not_called()
    output = capsys.readouterr()
    assert output.out == "" and "Inherited CODEX_HOME" in output.err
    assert "/private-inherited-home" not in output.err


def test_empty_inherited_home_allowed(launch, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", "")
    with pytest.raises(SystemExit) as exited:
        cli._codex_command(["run"])
    assert exited.value.code == 0
    launch[0].assert_called_once()


@pytest.mark.parametrize("code", [0, 1, 2, 7, 130, 143, 4294967295])
def test_supervisor_exit_code_passthrough(launch, capsys, code):
    launch[1].return_value = code
    with pytest.raises(SystemExit) as exited:
        cli._codex_command(["run", "2"])
    assert exited.value.code == code
    assert capsys.readouterr().out == ""


def test_run_help_on_stderr_without_launch(launch, capsys):
    with pytest.raises(SystemExit) as exited:
        cli._codex_command(["run", "--help"])
    assert exited.value.code == 0
    launch[0].assert_not_called()
    output = capsys.readouterr()
    assert output.out == "" and "--require-session" in output.err and "--shared" in output.err


@pytest.mark.parametrize("flag", ["--debug", "--deb"])
def test_root_debug_and_main_predispatch(launch, monkeypatch, flag):
    monkeypatch.setattr(sys, "argv", ["ccswap", "codex", flag, "run", "2", "--", "--json"])
    monkeypatch.setattr(cli, "_use_native_tls", lambda: None)
    monkeypatch.setattr(cli, "load_ui_settings", lambda *a: SimpleNamespace(theme="dark"))
    with pytest.raises(SystemExit) as exited:
        cli.main()
    assert exited.value.code == 0
    launch[0].assert_called_once_with(debug=True)
    assert launch[1].call_args.kwargs["codex_args"] == ["--json"]


@pytest.mark.parametrize("argv",
    [["codex", "run"], ["codex", "--debug", "run", "2"], ["codex", "--deb", "run", "2"],
                                  ["codex", "--debug", "--debug", "run", "2", "--", "prompt"]])
def test_no_terminal_probe_before_codex_run(argv):
    assert not cli_should_probe(argv, colors_enabled=True)


def test_existing_remove_delimiter_unchanged(launch):
    cli._codex_command(["remove", "--", "-email@example.org"])
    launch[0].return_value.remove_account.assert_called_once_with("-email@example.org",
        assume_yes=False)
    launch[1].assert_not_called()


def test_list_dispatch_preserves_session_fields(launch):
    session = dict(state="uncertain", capturePending=True, runCount=0, shared=False)
    payload = {"provider": "codex", "accounts": [{"number": 1, "session": session}]}
    launch[0].return_value.list_payload.return_value = payload
    args = SimpleNamespace(provider="codex", debug=False, json=True, token_status=False)
    assert cli._list_dispatch(Mock(), args) is payload
    args.provider = "all"
    assert cli._list_dispatch(Mock(), args)["codex"] is payload


@pytest.mark.parametrize("command", ["list", "status"])
def test_real_session_json_through_cli(custody, monkeypatch, capsys, command):
    switcher, _, _ = custody
    switcher.lock_file = switcher.backup_dir / ".lock"
    switcher.auth_file = switcher.codex_home / "auth.json"
    monkeypatch.setattr(cli, "CodexAccountSwitcher", lambda **kw: switcher)
    monkeypatch.setattr(switcher, "current_account_number", lambda: None)
    monkeypatch.setattr(switcher, "_account_plan", lambda *a: "plus")
    monkeypatch.setattr(codex_session, "slot_custody",
        lambda *a: codex_session.Custody("uncertain", "fake"))
    cli._codex_command([command, "--json"])
    payload = json.loads(capsys.readouterr().out)
    rows = payload["accounts" if command == "list" else "sessions"]
    assert rows[0]["session"] == {
        "state": "uncertain", "capturePending": True, "runCount": 0, "shared": False,
    }


@pytest.mark.parametrize("failure", ["constructor", "cwd"])
def test_setup_oserror_is_redacted_wrapper_failure(launch, monkeypatch, capsys, failure):
    def fail(*args, **kwargs):
        raise OSError("private-setup-detail")
    if failure == "constructor":
        launch[0].side_effect = fail
    else:
        monkeypatch.setattr(Path, "cwd", fail)
    with pytest.raises(SystemExit) as exited:
        cli._codex_command(["run"])
    assert exited.value.code == 1
    launch[1].assert_not_called()
    output = capsys.readouterr()
    assert output.out == "" and "Cannot initialize the Codex run" in output.err
    assert "private-setup-detail" not in output.err


def test_codex_json_stdout_is_untouched(launch, capsys):
    def child(*args, **kwargs):
        print("Shared session: refresh tokens may race.", file=sys.stderr)
        print('{"type":"fake-child-event"}')
        return 7
    launch[1].side_effect = child
    with pytest.raises(SystemExit) as exited:
        cli._codex_command(["run", "2", "--", "exec", "--json"])
    assert exited.value.code == 7
    output = capsys.readouterr()
    assert output.out == '{"type":"fake-child-event"}\n'
    assert output.err == "Shared session: refresh tokens may race.\n"
