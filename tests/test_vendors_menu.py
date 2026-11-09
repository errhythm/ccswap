from __future__ import annotations

import json

from claude_swap.vendors.core import AccountRecord, VendorError
from claude_swap.vendors.menu import collect, switchable
from claude_swap.vendors.registry import ToolRegistry
from claude_swap.vendors.service import VendorService
from claude_swap.vendors.store import VendorStore
from tests.vendor_helpers import FakeRunner, ok

OC = "/b/opencode"


def service(tmp_path, runner):
    return VendorService(ToolRegistry.default(runner), VendorStore(tmp_path / "v"))


def test_only_inactive_known_opencode_accounts_are_switchable():
    assert switchable("opencode", False, "anthropic")
    assert not switchable("opencode", True, "anthropic")
    assert not switchable("opencode", None, "anthropic")
    assert not switchable("muse", False, None)
    assert not switchable("kiro", False, "x")


def test_collect_marks_missing_vendors_and_isolates_failures(tmp_path):
    r = FakeRunner({"opencode": OC})
    r.on(OC, "--version", result=ok("1.99.0"))
    r.on(OC, "auth", "--help", result=ok("opencode auth switch"))
    r.on(OC, "auth", "list", "--help", result=ok("--format"))
    r.on(OC, "auth", "login", "--help", result=ok(""))
    rows = [{"provider": "anthropic", "id": "c1", "label": "work", "type": "oauth", "active": True},
            {"provider": "anthropic", "id": "c2", "label": "alt", "type": "oauth", "active": False}]
    r.on(OC, "auth", "list", "--format", "json", result=ok(json.dumps(rows)))
    out = collect(service(tmp_path, r))
    assert out["muse"][0].label.endswith("not installed")
    assert out["kiro"][0].label.endswith("not installed")
    labels = [x.label for x in out["opencode"]]
    assert labels[0].startswith("OpenCode 1.99.0")
    switchable_rows = [x for x in out["opencode"] if x.selector]
    assert [(x.provider, x.selector) for x in switchable_rows] == [("anthropic", "c2")]


def test_collect_survives_a_vendor_error(tmp_path):
    r = FakeRunner({"opencode": OC})
    r.on(OC, "--version", result=ok("1.99.0"))
    r.on(OC, "auth", "--help", result=ok("opencode auth switch"))
    r.on(OC, "auth", "list", "--help", result=ok("--format"))
    r.on(OC, "auth", "login", "--help", result=ok(""))
    r.on(OC, "auth", "list", "--format", "json", result=ok("garbage"))
    out = collect(service(tmp_path, r))
    assert "malformed" in out["opencode"][-1].label
    assert "muse" in out and "kiro" in out


def test_muse_and_kiro_rows_are_never_switchable_from_the_menu(tmp_path):
    r = FakeRunner({"muse": "/b/muse"})
    r.on("/b/muse", "--version", result=ok("Muse Code 1.4.14"))
    svc = service(tmp_path, r)
    svc.store.add(AccountRecord("muse", "work", "api-key"), "key-aaaaaaaa")
    rows = collect(svc)["muse"]
    assert all(x.selector is None for x in rows)
    assert any("switch in terminal" in x.label or "?" in x.label for x in rows)
