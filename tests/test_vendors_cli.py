"""CLI + service behaviour through the real argparse layer with a fake process boundary."""

from __future__ import annotations

import io
import json
import sys

import pytest

from claude_swap.vendors.cli import tool_command, vendors_command
from claude_swap.vendors.registry import ToolRegistry
from claude_swap.vendors.service import VendorService
from claude_swap.vendors.store import VendorStore
from tests.vendor_helpers import FakeRunner, ok

MUSE, KIRO, OC = "/b/muse", "/b/kiro-cli", "/b/opencode"
KEY = "mk_live_0123456789abcdef"
IDC = {
    "accountType": "IamIdentityCenter",
    "startUrl": "https://c.awsapps.com/start",
    "region": "us-east-1",
    "email": "a@c.com",
}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("META_API_KEY", raising=False)
    monkeypatch.delenv("KIRO_API_KEY", raising=False)


@pytest.fixture
def runner():
    r = FakeRunner({"muse": MUSE, "kiro-cli": KIRO, "opencode": OC})
    r.on(MUSE, "--version", result=ok("Muse Code 1.4.14\n"))
    r.on(KIRO, "--version", result=ok("kiro-cli 3.1.0\n"))
    r.on(OC, "--version", result=ok("1.18.35\n"))
    r.on(KIRO, "whoami", result=ok(json.dumps(IDC) + "\n\nProfile:\nX\n"))
    r.on(OC, "auth", "--help", result=ok("opencode auth list\n"))
    r.on(OC, "auth", "list", "--help", result=ok("none"))
    r.on(OC, "auth", "login", "--help", result=ok("none"))
    return r


@pytest.fixture
def service(runner, tmp_path):
    return VendorService(ToolRegistry.default(runner), VendorStore(tmp_path / "v"))


def run(tool, argv, service, capsys, stdin=None, monkeypatch=None):
    if stdin is not None:
        monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    code = tool_command(tool, argv, service)
    out = capsys.readouterr()
    return code, out.out, out.err


def test_vendors_json_contract(service, capsys):
    assert vendors_command(["--json"], service) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["schemaVersion"] == 1
    assert [v["tool"] for v in doc["vendors"]] == ["muse", "opencode", "kiro"]
    for v in doc["vendors"]:
        assert {"installed", "version", "capabilities", "accounts", "error"} <= v.keys()


def test_vendors_status_isolates_a_failing_vendor(service, monkeypatch):
    from claude_swap.vendors.core import VendorError

    def boom():
        raise VendorError("muse state unreadable")

    monkeypatch.setattr(service.adapter("muse"), "auth_summary", boom)
    doc = service.vendors_payload()
    by = {v["tool"]: v for v in doc["vendors"]}
    assert by["muse"]["error"] == "muse state unreadable"
    assert by["kiro"]["error"] is None and by["kiro"]["installed"] is True


def test_unexpected_adapter_bugs_are_not_hidden(service, monkeypatch):
    monkeypatch.setattr(
        service.adapter("muse"), "detect", lambda: (_ for _ in ()).throw(RuntimeError)
    )
    with pytest.raises(RuntimeError):
        service.vendors_payload()


def test_capabilities_command(service, capsys):
    assert vendors_command(["capabilities", "kiro", "--json"], service) == 0
    caps = json.loads(capsys.readouterr().out)["vendors"]["kiro"]
    assert caps["usage"]["support"] == "unsupported"


def test_muse_add_reads_key_from_stdin_and_never_prints_it(service, capsys, monkeypatch, runner):
    code, out, err = run(
        "muse", ["add", "work", "--api-key-stdin"], service, capsys, KEY + "\n", monkeypatch
    )
    assert code == 0 and KEY not in out + err
    code, out, _ = run("muse", ["list", "--json"], service, capsys)
    assert KEY not in out and json.loads(out)["accounts"][0]["alias"] == "work"
    assert all(KEY not in " ".join(c.argv) for c in runner.calls)


def test_muse_switch_end_to_end(service, capsys, monkeypatch, runner):
    runner.on(MUSE, "auth", "set", result=ok())
    run("muse", ["add", "work", "--api-key-stdin"], service, capsys, KEY + "\n", monkeypatch)
    code, _, err = run("muse", ["switch", "work"], service, capsys)
    assert code == 1 and "--yes" in err
    code, out, _ = run("muse", ["switch", "work", "--yes", "--json"], service, capsys)
    payload = json.loads(out)
    assert code == 0 and payload["restartRequired"] and payload["verified"] is False
    assert service.store.active("muse") == "work"
    assert runner.calls[-1].input_text == KEY + "\n"


def test_muse_run_isolated_env_and_exit_code(service, capsys, monkeypatch, runner):
    runner.on_interactive(MUSE, code=7)
    run("muse", ["add", "w", "--api-key-stdin"], service, capsys, KEY + "\n", monkeypatch)
    code, _, _ = run("muse", ["run", "w", "--", "exec", "hi"], service, capsys)
    assert code == 7
    call = runner.calls[-1]
    assert call.argv == (MUSE, "exec", "hi") and call.env == {"META_API_KEY": KEY}


def test_remove_leaves_vendor_untouched(service, capsys, monkeypatch, runner):
    run("muse", ["add", "w", "--api-key-stdin"], service, capsys, KEY + "\n", monkeypatch)
    n = len(runner.calls)
    code, out, _ = run("muse", ["remove", "w"], service, capsys)
    assert code == 0 and "not touched" in out
    assert not any(c.argv[1:2] in (("logout",), ("auth",)) for c in runner.calls[n:])
    assert service.store.records("muse") == []


def test_usage_cache_and_refresh(service, capsys, runner):
    calls = []
    runner.on(
        MUSE, "usage", "--json", result=lambda c: (calls.append(1), ok('{"allowance": null}'))[1]
    )
    for _ in range(3):
        service.usage("muse")
    assert len(calls) == 1
    service.usage("muse", refresh=True)
    assert len(calls) == 2


def test_usage_cache_expires(runner, tmp_path):
    now = [1000.0]
    svc = VendorService(
        ToolRegistry.default(runner), VendorStore(tmp_path / "v"), clock=lambda: now[0]
    )
    calls = []
    runner.on(
        MUSE,
        "usage",
        "--json",
        result=lambda c: (calls.append(1), ok('{"allowance": {"a": 1}}'))[1],
    )
    svc.usage("muse")
    now[0] += 30
    cached = svc.usage("muse")
    assert len(calls) == 1 and cached.observations[0].cached is True
    now[0] += 61
    svc.usage("muse")
    assert len(calls) == 2


def test_unsupported_exit_code_and_json_envelope(service, capsys, runner):
    code, out, _ = run("opencode", ["switch", "anthropic", "work", "--json"], service, capsys)
    assert code == 3
    err = json.loads(out)["error"]
    assert err["type"] == "UnsupportedCapability" and "auth switch" in err["message"]


def test_opencode_list_human_and_provider_awareness(service, capsys, runner, tmp_path, monkeypatch):
    data = tmp_path / "d" / "opencode"
    data.mkdir(parents=True)
    (data / "auth.json").write_text(
        json.dumps({"anthropic": {"type": "oauth", "refresh": "SECRET"}})
    )
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "d"))
    code, out, _ = run("opencode", ["list"], service, capsys)
    assert code == 0 and "anthropic/default" in out and "SECRET" not in out


def test_kiro_status_and_list(service, capsys):
    code, out, _ = run("kiro", ["add", "work"], service, capsys)
    assert code == 0
    code, out, _ = run("kiro", ["list", "--json"], service, capsys)
    (acct,) = json.loads(out)["accounts"]
    assert acct["active"] is True and acct["identity"]["email"] == "a@c.com"


def test_kiro_switch_non_interactive_exit_3(service, capsys, runner):
    run("kiro", ["add", "a"], service, capsys)
    other = {**IDC, "email": "z@c.com"}
    service.store.add(
        __import__("claude_swap.vendors.core", fromlist=["AccountRecord"]).AccountRecord(
            "kiro", "z", "login", identity=other
        )
    )
    code, _, err = run("kiro", ["switch", "z"], service, capsys)
    assert code == 3 and "--login" in err


def test_kiro_usage_unsupported_exit_0_with_reason(service, capsys):
    code, out, _ = run("kiro", ["usage", "--json"], service, capsys)
    assert code == 0 and json.loads(out)["state"] == "unsupported"


def test_missing_binary_is_actionable(tmp_path, capsys):
    svc = VendorService(ToolRegistry.default(FakeRunner()), VendorStore(tmp_path / "v"))
    code, _, err = run("muse", ["switch", "x"], svc, capsys)
    assert code == 1 and "not installed" in err and "Traceback" not in err


def test_ambiguous_and_numeric_selectors(service, capsys, monkeypatch):
    run("muse", ["add", "Work", "--api-key-stdin"], service, capsys, KEY + "\n", monkeypatch)
    run("muse", ["add", "play", "--browser"], service, capsys)
    assert service.resolve("muse", "2").alias == "play"
    assert service.resolve("muse", "work").alias == "Work"
    assert service.resolve("muse", "1").alias == "Work"


def test_auto_switch_is_disabled_with_reason_for_every_vendor(service):
    for tool in ("muse", "opencode", "kiro"):
        state = service.auto_switch_state(tool)
        assert state["available"] is False and state["reason"]


def test_argparse_rejects_secret_on_command_line(service, capsys):
    with pytest.raises(SystemExit) as exc:
        tool_command("muse", ["add", "w", "--api-key", KEY], service)
    assert exc.value.code == 2


def test_corrupt_store_gives_clean_error(service, capsys):
    service.store.root.mkdir(parents=True, exist_ok=True)
    service.store.file.write_text("{oops")
    code, _out, err = run("muse", ["list"], service, capsys)
    assert code == 1 and "untouched" in err and "Traceback" not in err
    assert service.store.file.read_text() == "{oops"
