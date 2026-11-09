from __future__ import annotations

import json

import pytest

from claude_swap.vendors.core import RunResult, UnsupportedCapability, UsageState, VendorError
from claude_swap.vendors.opencode import OpenCodeAdapter, valid_selector
from tests.vendor_helpers import FakeRunner, fail, ok

OC = "/usr/local/bin/opencode"
GROUP_NEW = "Commands:\n  opencode auth list\n  opencode auth switch <provider> <account>\n"
GROUP_OLD = "Commands:\n  opencode auth list\n  opencode auth login [url]\n  opencode auth logout [provider]\n"


def make(*, new: bool, listing=None):
    r = FakeRunner({"opencode": OC})
    r.on(OC, "--version", result=ok("1.99.0\n" if new else "1.18.35\n"))
    r.on(OC, "auth", "--help", result=ok(GROUP_NEW if new else GROUP_OLD))
    r.on(OC, "auth", "list", "--help", result=ok("--format  json|table" if new else "--print-logs"))
    r.on(OC, "auth", "login", "--help", result=ok("-p, --provider"))
    if listing is not None:
        r.on(OC, "auth", "list", "--format", "json", result=listing)
    return r


ROWS = [
    {"provider": "anthropic", "id": "c1", "label": "work", "type": "oauth", "active": True},
    {"provider": "anthropic", "id": "c2", "label": "personal", "type": "oauth", "active": False},
    {"provider": "openai", "id": "c3", "label": "main", "type": "api", "active": True},
]


def test_probe_detects_switch_from_help_not_version():
    assert OpenCodeAdapter(make(new=True)).probe()["switch"] is True
    old = OpenCodeAdapter(make(new=False))
    assert old.probe()["switch"] is False and old.probe()["json"] is False


def test_json_listing_preserves_provider_account_relationships():
    adapter = OpenCodeAdapter(make(new=True, listing=ok(json.dumps(ROWS))))
    found = adapter.discover()
    assert [(r["provider"], r["account"], r["active"]) for r in found] == [
        ("anthropic", "work", True),
        ("anthropic", "personal", False),
        ("openai", "main", True),
    ]


def test_json_listing_alternate_shapes():
    nested = {"accounts": {"anthropic": [{"id": "c1", "name": "work", "active": True}]}}
    adapter = OpenCodeAdapter(make(new=True, listing=ok(json.dumps(nested))))
    assert adapter.discover()[0]["account"] == "work"


@pytest.mark.parametrize("bad", ["nope", '"str"', "42"])
def test_malformed_listing_raises(bad):
    adapter = OpenCodeAdapter(make(new=True, listing=ok(bad)))
    with pytest.raises(VendorError):
        adapter.discover()


def test_empty_object_listing_means_no_accounts():
    assert OpenCodeAdapter(make(new=True, listing=ok("{}"))).discover() == []


def test_listing_failure_is_reported():
    adapter = OpenCodeAdapter(make(new=True, listing=fail("db locked")))
    with pytest.raises(VendorError, match="auth list"):
        adapter.discover()


def test_legacy_auth_json_read_only_no_secret_values(tmp_path, monkeypatch):
    data = tmp_path / "data" / "opencode"
    data.mkdir(parents=True)
    (data / "auth.json").write_text(
        json.dumps({"opencode-go": {"type": "api", "key": "SECRET-VALUE"}})
    )
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    adapter = OpenCodeAdapter(make(new=False))
    rows = adapter.discover()
    assert rows == [
        {"provider": "opencode-go", "account": "default", "id": None, "type": "api", "active": True}
    ]
    assert "SECRET-VALUE" not in json.dumps([v.to_json() for v in adapter.observe([])])


def test_legacy_without_auth_file_is_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    assert OpenCodeAdapter(make(new=False)).discover() == []


def test_switch_success_is_verified_by_relisting():
    runner = make(new=True)
    states = iter([ROWS, [{**ROWS[0], "active": False}, {**ROWS[1], "active": True}, ROWS[2]]])
    runner.on(OC, "auth", "list", "--format", "json", result=lambda c: ok(json.dumps(next(states))))
    runner.on(OC, "auth", "switch", result=ok())
    outcome = OpenCodeAdapter(runner).switch_account("anthropic", "personal")
    assert outcome.verified and outcome.restart_required
    assert (OC, "auth", "switch", "anthropic", "c2") in runner.argvs()


def test_switch_that_does_not_stick_is_restored_and_reported():
    runner = make(new=True, listing=ok(json.dumps(ROWS)))  # never changes
    runner.on(OC, "auth", "switch", result=ok())
    with pytest.raises(VendorError, match="still reports"):
        OpenCodeAdapter(runner).switch_account("anthropic", "personal")
    switches = [a for a in runner.argvs() if a[:3] == (OC, "auth", "switch")]
    assert switches[-1][-1] == "c1"  # restored to the previous account


def test_switch_unknown_account_does_not_invoke_vendor():
    runner = make(new=True, listing=ok(json.dumps(ROWS)))
    with pytest.raises(VendorError, match="no account"):
        OpenCodeAdapter(runner).switch_account("anthropic", "ghost")
    assert not any(a[:3] == (OC, "auth", "switch") for a in runner.argvs())


def test_switch_unsupported_on_legacy(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    with pytest.raises(UnsupportedCapability, match="1.18.35"):
        OpenCodeAdapter(make(new=False)).switch_account("anthropic", "work")


@pytest.mark.parametrize("bad", ["--help", "x;rm -rf", "$(id)", "", "a\nb", "-p"])
def test_selectors_cannot_become_flags_or_shell(bad):
    with pytest.raises(VendorError):
        valid_selector(bad)


def test_switch_rejects_injection_before_any_process():
    runner = make(new=True, listing=ok(json.dumps(ROWS)))
    with pytest.raises(VendorError):
        OpenCodeAdapter(runner).switch_account("anthropic", "--yes")
    assert runner.calls == []


def test_register_requires_disambiguation():
    adapter = OpenCodeAdapter(make(new=True, listing=ok(json.dumps(ROWS))))
    with pytest.raises(VendorError, match="--account"):
        adapter.register("a", provider="anthropic")
    record, secret = adapter.register("work", provider="anthropic", account="work")
    assert secret is None and record.provider == "anthropic" and record.identity["id"] == "c1"
    assert adapter.register("o", provider="openai")[0].identity["account"] == "main"


def test_observe_marks_registered_and_missing():
    adapter = OpenCodeAdapter(make(new=True, listing=ok(json.dumps(ROWS))))
    reg, _ = adapter.register("w", provider="anthropic", account="work")
    from claude_swap.vendors.core import AccountRecord

    ghost = AccountRecord("opencode", "g", "provider-account", "google", {"account": "x"})
    views = adapter.observe([reg, ghost])
    by = {v.alias: v for v in views}
    assert by["w"].registered and by["w"].active is True
    assert by["anthropic/personal"].registered is False and by["anthropic/personal"].active is False
    assert "no longer listed" in by["g"].note


def test_usage_never_fabricates_a_quota():
    runner = make(new=True)
    runner.on(OC, "db", "--help", result=RunResult(0, stderr="query  --format json|tsv"))
    runner.on(OC, "db", result=ok("[]"))
    result = OpenCodeAdapter(runner).usage(None, None)
    assert result.windows == () and result.authoritative is False
    assert result.state is UsageState.UNAVAILABLE


def test_local_stats_are_labelled_non_authoritative():
    runner = make(new=True)
    runner.on(OC, "stats", result=ok("\x1b[1mOVERVIEW\x1b[0m\nSessions 3\n"))
    (obs,) = OpenCodeAdapter(runner).local_stats().observations
    assert obs.authoritative is False and obs.scope == "local" and obs.kind.value == "local-tokens"
    assert "\x1b" not in obs.data["text"]


def test_provider_config_is_separate_and_names_only(tmp_path, monkeypatch):
    cfg = tmp_path / "opencode.json"
    cfg.write_text(
        json.dumps(
            {"model": "anthropic/x", "provider": {"acme": {"options": {"apiKey": "SECRETKEY"}}}}
        )
    )
    monkeypatch.setenv("OPENCODE_CONFIG", str(cfg))
    summary = OpenCodeAdapter(make(new=True)).provider_config()
    assert summary == {"readable": True, "model": "anthropic/x", "configuredProviders": ["acme"]}
    assert "SECRETKEY" not in json.dumps(summary)


def test_login_uses_provider_flag_when_available():
    runner = make(new=False)
    runner.on_interactive(OC, "auth", "login", code=0)
    OpenCodeAdapter(runner).login("anthropic")
    assert runner.calls[-1].argv == (OC, "auth", "login", "--provider", "anthropic")


def test_logout_passes_validated_arguments_only():
    runner = make(new=True)
    runner.on_interactive(OC, "auth", "logout", code=0)
    OpenCodeAdapter(runner).logout("anthropic", "work")
    assert runner.calls[-1].argv == (OC, "auth", "logout", "anthropic", "work")
    with pytest.raises(VendorError):
        OpenCodeAdapter(runner).logout("anthropic", "--all")


def test_labels_with_spaces_are_single_safe_arguments():
    assert valid_selector("Team Work") == "Team Work"
    for bad in ("-x", " lead", "a;b"):
        with pytest.raises(VendorError):
            valid_selector(bad)


DUP = [
    {"provider": "anthropic", "id": "c1", "label": "work", "type": "oauth", "active": True},
    {"provider": "anthropic", "id": "c2", "label": "work", "type": "oauth", "active": False},
]


def test_duplicate_labels_require_the_id():
    adapter = OpenCodeAdapter(make(new=True, listing=ok(json.dumps(DUP))))
    with pytest.raises(VendorError, match="account id"):
        adapter.register("w", provider="anthropic", account="work")
    record, _ = adapter.register("w2", provider="anthropic", account="c2")
    assert record.identity["id"] == "c2"
    with pytest.raises(VendorError, match="account id"):
        adapter.switch_account("anthropic", "work")


def test_observe_distinguishes_same_label_accounts_by_id():
    adapter = OpenCodeAdapter(make(new=True, listing=ok(json.dumps(DUP))))
    record, _ = adapter.register("w2", provider="anthropic", account="c2")
    views = adapter.observe([record])
    assert [(v.registered, v.alias == "w2") for v in views] == [(False, False), (True, True)]


def test_listing_without_active_info_cannot_verify_but_does_not_claim_failure():
    rows = [{k: v for k, v in r.items() if k != "active"} for r in ROWS]
    runner = make(new=True, listing=ok(json.dumps(rows)))
    runner.on(OC, "auth", "switch", result=ok())
    outcome = OpenCodeAdapter(runner).switch_account("anthropic", "personal")
    assert outcome.verified is False and "cannot be verified" in outcome.message


def test_relist_failure_after_a_successful_switch_is_reported_as_unverified():
    runner = make(new=True)
    answers = iter([ok(json.dumps(ROWS)), fail("db locked")])
    runner.on(OC, "auth", "list", "--format", "json", result=lambda c: next(answers))
    runner.on(OC, "auth", "switch", result=ok())
    outcome = OpenCodeAdapter(runner).switch_account("anthropic", "personal")
    assert outcome.verified is False and outcome.warnings


def test_failed_restore_is_reported_not_claimed():
    runner = make(new=True, listing=ok(json.dumps(ROWS)))  # the switch never sticks

    def switch(call):
        return ok() if call.argv[-1] == "c2" else fail("cannot restore")

    runner.on(OC, "auth", "switch", result=switch)
    with pytest.raises(VendorError, match="could not restore"):
        OpenCodeAdapter(runner).switch_account("anthropic", "personal")


def test_legacy_logout_never_passes_an_account(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    runner = make(new=False)
    runner.on_interactive(OC, "auth", "logout", code=0)
    OpenCodeAdapter(runner).logout("anthropic", "work")
    assert runner.calls[-1].argv == (OC, "auth", "logout", "anthropic")


# ---- OpenCode Go: dollar-equivalent usage per model, in Go's own windows ------------


def go_rows():
    def row(model, h5, d7, d30, n30):
        base = {"provider": "opencode-go", "model": model, "last": 1}
        for p, (n, c) in {
            "5h": (1 if h5 else 0, h5),
            "24h": (0, 0.0),
            "7d": (1, d7),
            "30d": (n30, d30),
            "all": (n30, d30),
        }.items():
            base[f"n_{p}"], base[f"cost_{p}"] = n, c
        return base

    return [
        row("muse-spark-1.3", 0.5, 4.0, 38.48, 51000),
        row("glm-5.3", 0.0, 0.0, 0.03, 2),
        {"provider": "anthropic", "model": "x", "n_30d": 1, "cost_30d": 2.0},
    ]


def test_go_usage_is_per_model_in_gos_windows_not_credits():
    from claude_swap.vendors.opencode import go_usage

    go = go_usage(summarize_rows())
    assert go["unit"] == "usd-equivalent" and "20%" in go["rule"] and "50%" in go["rule"]
    top = go["models"][0]
    assert (top["model"], top["fiveHourUsd"], top["weekUsd"], top["thirtyDayUsd"]) == (
        "muse-spark-1.3",
        0.5,
        4.0,
        38.48,
    )


def summarize_rows():
    from claude_swap.vendors.opencode import summarize

    return summarize(go_rows())


def test_go_headline_and_refused_request_are_shown():
    from claude_swap.vendors.core import UsageKind, UsageObservation, UsageResult, UsageState
    from claude_swap.vendors.opencode import go_usage
    from claude_swap.vendors.overview import go_lines, usage_headline

    providers = summarize_rows()
    data = {
        "providers": providers,
        "go": go_usage(providers),
        "lastErrors": {
            "opencode-go": {"message": "Workspace access denied", "status": 403, "at": 1.0}
        },
    }
    result = UsageResult(
        UsageState.AVAILABLE,
        observations=(
            UsageObservation(
                UsageKind.LOCAL_TOKENS, "opencode db", False, scope="local", data=data
            ),
        ),
    )
    assert (
        usage_headline(result) == "Go · 5h $0.50 · wk $4.00 (muse-spark-1.3) · last request refused"
    )
    text = "\n".join(go_lines(result))
    assert (
        "dollar-equivalent" in text
        and "not credits" in text
        and "403: Workspace access denied" in text
    )
    assert "credit" not in usage_headline(result)


def test_last_error_query_is_constant_and_read_only():
    from claude_swap.vendors.opencode import _LAST_ERROR_SQL

    sql = _LAST_ERROR_SQL.lower()
    assert sql.startswith("select ") and " from message " in sql
    for word in ("insert", "update", "delete", "drop", "credential", "account", "attach", "pragma"):
        assert word not in sql.replace("json_extract", "")


def test_usage_queries_last_errors_and_reports_go(tmp_path):
    runner = make(new=True)
    runner.on(OC, "db", "--help", result=RunResult(0, stderr="query --format json"))
    from claude_swap.vendors.opencode import _LAST_ERROR_SQL

    def db(call):
        if call.argv[2] == _LAST_ERROR_SQL:
            return ok(
                json.dumps(
                    [
                        {
                            "provider": "opencode-go",
                            "message": "Workspace access denied",
                            "status": 403,
                            "at": 1793856631125,
                        }
                    ]
                )
            )
        return ok(json.dumps(go_rows()))

    runner.on(OC, "db", result=db)
    result = OpenCodeAdapter(runner).usage(None, None)
    data = result.observations[0].data
    assert data["lastErrors"]["opencode-go"]["status"] == 403
    assert data["go"]["models"][0]["model"] == "muse-spark-1.3"
