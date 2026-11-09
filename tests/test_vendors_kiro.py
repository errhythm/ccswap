from __future__ import annotations

import json

import pytest

from claude_swap.vendors.core import (
    AccountRecord,
    RunResult,
    UnsupportedCapability,
    UsageState,
    VendorError,
)
from claude_swap.vendors.kiro import ENV_KEY, KiroAdapter, parse_whoami
from tests.vendor_helpers import FakeRunner, fail, ok

KIRO = "/usr/local/bin/kiro-cli"
IDC = {
    "accountType": "IamIdentityCenter",
    "startUrl": "https://corp.awsapps.com/start",
    "region": "ap-northeast-2",
    "email": "a@corp.com",
}
OTHER = {**IDC, "email": "b@corp.com"}
TRAILER = "\n\nProfile:\nKiroProfile-us-east-1\narn:aws:codewhisperer:us-east-1:1:profile/X\n"


@pytest.fixture(autouse=True)
def _no_env(monkeypatch):
    monkeypatch.delenv(ENV_KEY, raising=False)


class Login:
    """Mutable fake of the machine-wide login."""

    def __init__(self, who):
        self.who = who

    def whoami(self, call):
        return ok(json.dumps(self.who) + TRAILER) if self.who else fail("Not logged in")


def make(who=IDC):
    state = Login(who)
    r = FakeRunner({"kiro-cli": KIRO})
    r.on(KIRO, "--version", result=ok("kiro-cli 3.1.0\n"))
    r.on(KIRO, "whoami", result=state.whoami)
    return r, state


def login_rec(alias="a", ident=IDC, hint=None):
    return AccountRecord(
        "kiro",
        alias,
        "login",
        identity=ident,
        login_hint=hint
        if hint is not None
        else {"license": "pro", "identityProvider": IDC["startUrl"], "region": IDC["region"]},
    )


def test_parse_whoami_ignores_trailing_text():
    assert parse_whoami(json.dumps(IDC) + TRAILER)["email"] == "a@corp.com"
    assert parse_whoami("Not logged in") is None
    assert parse_whoami("") is None and parse_whoami("[1]") is None


def test_detect_and_state():
    runner, _ = make()
    adapter = KiroAdapter(runner)
    assert adapter.detect().version == "3.1.0"
    summary = adapter.auth_summary()
    assert summary["loggedIn"] and summary["accountType"] == "IamIdentityCenter"


def test_detect_does_not_confuse_ide_launcher():
    r = FakeRunner({"kiro": "/usr/bin/kiro"})
    assert KiroAdapter(r).detect().installed is False


def test_logged_out_state():
    runner, _ = make(None)
    assert KiroAdapter(runner).auth_summary()["loggedIn"] is False


def test_register_login_captures_identity_without_tokens():
    runner, _ = make()
    record, secret = KiroAdapter(runner).register("work")
    assert secret is None and record.kind == "login"
    assert record.identity["email"] == "a@corp.com"
    assert record.login_hint["identityProvider"] == IDC["startUrl"]
    assert not any(c.argv[1] in ("login", "logout") for c in runner.calls)


def test_register_requires_a_login():
    runner, _ = make(None)
    with pytest.raises(VendorError, match="kiro-cli login"):
        KiroAdapter(runner).register("work")


def test_register_social_and_validation():
    runner, _ = make({"accountType": "Social", "email": "me@gmail.com"})
    record, _ = KiroAdapter(runner).register("g", social="google")
    assert record.login_hint == {"social": "google"}
    with pytest.raises(VendorError):
        KiroAdapter(runner).register("g", social="facebook")
    runner2, _ = make()
    with pytest.raises(VendorError, match="Invalid Identity Center"):
        KiroAdapter(runner2).register("x", license="pro", identity_provider="http://evil; rm -rf /")


def test_register_api_key():
    record, secret = KiroAdapter(make()[0]).register(
        "ci", kind="api-key", api_key="ksk_abcdef123456"
    )
    assert record.kind == "api-key" and secret == "ksk_abcdef123456"
    with pytest.raises(VendorError):
        KiroAdapter(make()[0]).register("ci", kind="api-key", api_key="x")


def test_observe_marks_active_by_identity_not_alias():
    runner, _ = make(IDC)
    adapter = KiroAdapter(runner)
    recs = [login_rec("a"), login_rec("b", OTHER)]
    views = {v.alias: v.active for v in adapter.observe(recs)}
    assert views == {"a": True, "b": False}


def test_observe_surfaces_unregistered_current_login():
    runner, _ = make(OTHER)
    views = KiroAdapter(runner).observe([login_rec("a")])
    assert views[-1].registered is False and views[-1].active is True


def test_switch_already_active_is_noop():
    runner, _ = make(IDC)
    outcome = KiroAdapter(runner).switch(
        login_rec(), None, assume_yes=False, interactive=False, context=[login_rec()]
    )
    assert outcome.verified and not outcome.restart_required
    assert not any(c.interactive for c in runner.calls)


def test_switch_non_interactive_is_unsupported_and_changes_nothing():
    runner, _ = make(OTHER)
    with pytest.raises(UnsupportedCapability, match="--login"):
        KiroAdapter(runner).switch(
            login_rec(),
            None,
            assume_yes=True,
            interactive=False,
            context=[login_rec(), login_rec("b", OTHER)],
        )
    assert not any(c.interactive for c in runner.calls)


def test_switch_refuses_when_current_login_is_unregistered():
    runner, _ = make(OTHER)
    with pytest.raises(VendorError, match="not registered"):
        KiroAdapter(runner).switch(
            login_rec(), None, assume_yes=True, interactive=True, context=[login_rec()]
        )
    assert not any(c.interactive for c in runner.calls)  # logout was never run


def test_switch_requires_confirmation_before_logout():
    runner, _ = make(OTHER)
    ctx = [login_rec(), login_rec("b", OTHER)]
    with pytest.raises(VendorError, match="--yes"):
        KiroAdapter(runner).switch(
            login_rec(), None, assume_yes=False, interactive=True, context=ctx
        )
    assert not any(c.interactive for c in runner.calls)


def test_switch_full_flow_logs_out_logs_in_and_verifies():
    runner, state = make(OTHER)
    runner.on_interactive(KIRO, "logout", code=lambda c: (setattr(state, "who", None), 0)[1])
    runner.on_interactive(KIRO, "login", code=lambda c: (setattr(state, "who", IDC), 0)[1])
    ctx = [login_rec(), login_rec("b", OTHER)]
    outcome = KiroAdapter(runner).switch(
        login_rec(), None, assume_yes=True, interactive=True, context=ctx
    )
    assert outcome.verified and outcome.restart_required
    interactive = [c.argv for c in runner.calls if c.interactive]
    assert interactive == [
        (KIRO, "logout"),
        (
            KIRO,
            "login",
            "--license",
            "pro",
            "--identity-provider",
            IDC["startUrl"],
            "--region",
            IDC["region"],
        ),
    ]


def test_switch_login_cancelled_reports_recovery_command():
    runner, state = make(OTHER)
    runner.on_interactive(KIRO, "logout", code=lambda c: (setattr(state, "who", None), 0)[1])
    runner.on_interactive(KIRO, "login", code=1)
    ctx = [login_rec(), login_rec("b", OTHER)]
    with pytest.raises(VendorError, match="logged out.*kiro-cli login"):
        KiroAdapter(runner).switch(
            login_rec(), None, assume_yes=True, interactive=True, context=ctx
        )


def test_switch_wrong_account_after_login_is_not_reported_as_success():
    runner, _ = make(OTHER)
    runner.on_interactive(KIRO, "logout", code=0)
    runner.on_interactive(KIRO, "login", code=0)  # user signs in as b again
    ctx = [login_rec(), login_rec("b", OTHER)]
    with pytest.raises(VendorError, match="b@corp.com"):
        KiroAdapter(runner).switch(
            login_rec(), None, assume_yes=True, interactive=True, context=ctx
        )


def test_logout_failure_leaves_login_alone():
    runner, _ = make(OTHER)
    runner.on_interactive(KIRO, "logout", code=1)
    ctx = [login_rec(), login_rec("b", OTHER)]
    with pytest.raises(VendorError, match="left as it was"):
        KiroAdapter(runner).switch(
            login_rec(), None, assume_yes=True, interactive=True, context=ctx
        )
    assert [c.argv for c in runner.calls if c.interactive] == [(KIRO, "logout")]


def test_api_key_accounts_cannot_be_switched_only_run():
    rec = AccountRecord("kiro", "ci", "api-key", secret_ref="kiro-" + "0" * 32)
    with pytest.raises(UnsupportedCapability, match="ccswap kiro run"):
        KiroAdapter(make()[0]).switch(rec, "ksk_x", assume_yes=True, interactive=True)


def test_launch_spec_api_key_uses_env_only():
    rec = AccountRecord("kiro", "ci", "api-key", secret_ref="kiro-" + "0" * 32)
    spec = KiroAdapter(make()[0]).launch_spec(rec, "ksk_abcdef123456", ["--no-interactive", "hi"])
    assert spec.argv == (KIRO, "chat", "--no-interactive", "hi")
    assert spec.env_overlay == {ENV_KEY: "ksk_abcdef123456"}
    assert "ksk_abcdef123456" not in " ".join(spec.argv)


def test_launch_spec_login_requires_active_and_ignores_stray_env_key():
    runner, _ = make(IDC)
    spec = KiroAdapter(runner).launch_spec(login_rec(), None, [])
    assert spec.unset_env == (ENV_KEY,)
    with pytest.raises(VendorError, match="not the active"):
        KiroAdapter(make(OTHER)[0]).launch_spec(login_rec(), None, [])


def test_usage_without_v2_harness_is_never_sent_to_the_model(tmp_path):
    runner, _ = make()
    runner.on(KIRO, "chat", "--help", result=ok("--no-interactive  --v3"))
    result = KiroAdapter(runner, probe_dir=tmp_path).usage(None, None)
    assert result.state is UsageState.UNSUPPORTED
    assert not any("/usage" in c.argv for c in runner.calls)


def test_capability_table_marks_auto_switch_unsupported():
    adapter = KiroAdapter(make()[0])
    caps = {c.value: s.support.value for c, s in adapter.capabilities(adapter.detect()).items()}
    assert caps["auto-switch"] == "unsupported" and caps["usage"] == "unsupported"


def test_whoami_failure_is_not_mistaken_for_logged_out():
    runner = FakeRunner({"kiro-cli": KIRO})
    runner.on(KIRO, "--version", result=ok("kiro-cli 3.1.0"))
    runner.on(KIRO, "whoami", result=RunResult(124, timed_out=True))
    assert KiroAdapter(runner).whoami_state() == ("error", None)
    runner.on(KIRO, "whoami", result=fail("segfault"))
    assert KiroAdapter(runner).whoami_state() == ("error", None)
    runner.on(KIRO, "whoami", result=ok("garbage"))
    assert KiroAdapter(runner).whoami_state() == ("error", None)
    runner.on(KIRO, "whoami", result=fail("Not logged in"))
    assert KiroAdapter(runner).whoami_state() == ("out", None)


def test_switch_aborts_without_logout_when_whoami_is_unreadable():
    runner = FakeRunner({"kiro-cli": KIRO})
    runner.on(KIRO, "--version", result=ok("kiro-cli 3.1.0"))
    runner.on(KIRO, "whoami", result=RunResult(124, timed_out=True))
    runner.on_interactive(KIRO, "logout", code=0)
    with pytest.raises(VendorError, match="nothing was changed"):
        KiroAdapter(runner).switch(
            login_rec(), None, assume_yes=True, interactive=True, context=[login_rec()]
        )
    assert not any(c.interactive for c in runner.calls)


def test_switch_when_logged_out_skips_logout_and_logs_in():
    runner, state = make(None)
    runner.on(
        KIRO, "whoami", result=lambda c: state.whoami(c) if state.who else fail("Not logged in")
    )
    runner.on_interactive(KIRO, "login", code=lambda c: (setattr(state, "who", IDC), 0)[1])
    outcome = KiroAdapter(runner).switch(
        login_rec(), None, assume_yes=False, interactive=True, context=[login_rec()]
    )
    assert outcome.verified
    assert [c.argv[1] for c in runner.calls if c.interactive] == ["login"]


def test_identity_includes_account_type_and_ignores_email_case():
    from claude_swap.vendors.kiro import _same_login

    assert _same_login({**IDC, "email": "A@Corp.com"}, IDC)
    assert not _same_login(
        {**IDC, "accountType": "BuilderId", "startUrl": ""}, {**IDC, "startUrl": ""}
    )
    assert not _same_login(IDC, {**IDC, "startUrl": "https://other.awsapps.com/start"})


def test_ctrl_c_during_login_names_the_recovery_command():
    runner, state = make(OTHER)
    runner.on_interactive(KIRO, "logout", code=lambda c: (setattr(state, "who", None), 0)[1])

    def interrupted(call):
        raise KeyboardInterrupt

    runner.on_interactive(KIRO, "login", code=interrupted)
    ctx = [login_rec(), login_rec("b", OTHER)]
    with pytest.raises(VendorError, match="Recover with: kiro-cli login"):
        KiroAdapter(runner).switch(
            login_rec(), None, assume_yes=True, interactive=True, context=ctx
        )
