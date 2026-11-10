from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

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


# ---- Kiro profiles (one Identity Center login, several AWS accounts) ---------------

ARN_A = "arn:aws:codewhisperer:us-east-1:276673691073:profile/A93MWV3YUKXQ"
ARN_B = "arn:aws:codewhisperer:us-east-1:151869951925:profile/QXHDHVXUCRC4"


def whoami_with_profile(who, arn, name="KiroProfile-us-east-1"):
    return ok(json.dumps(who) + f"\n\nProfile:\n{name}\n{arn}\n")


def test_parse_whoami_reads_the_profile_kiro_31_prints():
    from claude_swap.vendors.kiro import parse_whoami

    who = parse_whoami(json.dumps(IDC) + f"\n\nProfile:\nKiroProfile-us-east-1\n{ARN_A}\n")
    assert who["profileArn"] == ARN_A and who["profileName"] == "KiroProfile-us-east-1"
    assert "profileArn" not in parse_whoami(json.dumps(IDC) + "\n\nProfile:\nnot-an-arn\n")


def profile_world(active_arn):
    state = {"arn": active_arn}
    r = FakeRunner({"kiro-cli": KIRO})
    r.on(KIRO, "--version", result=ok("kiro-cli 3.1.0"))
    r.on(KIRO, "whoami", result=lambda c: whoami_with_profile(IDC, state["arn"]))
    return r, state


def test_two_profiles_of_one_login_are_separate_accounts():
    r, _ = profile_world(ARN_A)
    adapter = KiroAdapter(r)
    a, _ = adapter.register("acct-a")
    b, _ = adapter.register("acct-b", profile_arn=ARN_B)
    assert a.identity["profileArn"] == ARN_A and b.identity["profileArn"] == ARN_B
    assert b.identity["email"] == IDC["email"] and "profileName" not in b.identity
    views = {v.alias: v.active for v in adapter.observe([a, b])}
    assert views == {"acct-a": True, "acct-b": False}
    assert not any(c.interactive for c in r.calls)  # registering never switches


def test_invalid_profile_arn_is_rejected():
    r, _ = profile_world(ARN_A)
    with pytest.raises(VendorError, match="Invalid Kiro profile ARN"):
        KiroAdapter(r).register("x", profile_arn="arn:aws:iam::123:role/evil; rm -rf /")


def test_profile_switch_uses_kiros_picker_without_logout_and_verifies():
    r, state = profile_world(ARN_A)
    r.on_interactive(KIRO, "profile", code=lambda c: (state.__setitem__("arn", ARN_B), 0)[1])
    adapter = KiroAdapter(r)
    b, _ = adapter.register("acct-b", profile_arn=ARN_B)
    outcome = adapter.switch(b, None, assume_yes=False, interactive=True, context=[b])
    assert outcome.verified and "151869951925" in outcome.message
    assert [c.argv for c in r.calls if c.interactive] == [(KIRO, "profile")]  # no logout


def test_profile_switch_to_the_wrong_profile_is_reported():
    r, _ = profile_world(ARN_A)
    r.on_interactive(KIRO, "profile", code=0)  # user picked nothing new
    adapter = KiroAdapter(r)
    b, _ = adapter.register("acct-b", profile_arn=ARN_B)
    with pytest.raises(VendorError, match="276673691073"):
        adapter.switch(b, None, assume_yes=False, interactive=True, context=[b])


def _iso_in(minutes: float) -> str:
    moment = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def seed_kiro_db(path: Path, arn: str, expires_at: str, token: str = "fake-token") -> None:
    """A small stand-in for Kiro's login tables.  No live database and no real token."""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE migrations (
            id INTEGER PRIMARY KEY,
            version INTEGER NOT NULL,
            migration_time INTEGER NOT NULL
        );
        CREATE TABLE auth_kv (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE state (key TEXT PRIMARY KEY, value BLOB);
        CREATE TABLE history (id INTEGER PRIMARY KEY, command TEXT);
        """
    )
    con.execute("INSERT INTO migrations VALUES (1, 1, 0)")
    con.execute(
        "INSERT INTO auth_kv VALUES ('kirocli:odic:token', ?)",
        (json.dumps({"expires_at": expires_at, "access_token": token}),),
    )
    con.execute(
        "INSERT INTO state VALUES ('api.codewhisperer.profile', ?)",
        (
            json.dumps(
                {"arn": arn, "profile_name": "P", "device_code_disabled": False}
            ).encode(),
        ),
    )
    for key, value in (
        ("auth.idc.region", b'"us-east-1"'),
        ("profile.Migrated", b"1"),
        ("migration.kiro.completed", b"1"),
    ):
        con.execute("INSERT INTO state VALUES (?, ?)", (key, value))
    con.execute("INSERT INTO history VALUES (1, 'do-not-copy')")
    con.commit()
    con.close()


ACTIVE_REPORT = (
    "Estimated Usage | resets on 2099-12-01 | KIRO PRO+\n"
    "Credits (100.00 of 2000 covered in plan), 5%\n"
)
OTHER_REPORT = (
    "Estimated Usage | resets on 2099-12-01 | KIRO PRO+\n"
    "Credits (0.00 of 2000 covered in plan), 0%\n"
)


def test_profile_usage_is_measured_for_each_profile(tmp_path):
    from claude_swap.vendors.registry import ToolRegistry
    from claude_swap.vendors.service import VendorService
    from claude_swap.vendors.store import VendorStore

    r, _ = profile_world(ARN_A)
    r.on(KIRO, "chat", "--help", result=ok("--no-interactive --v2"))

    def report(call):
        text = OTHER_REPORT if call.env.get("HOME") else ACTIVE_REPORT
        return ok(text)

    r.on(KIRO, "chat", "--no-interactive", "--v2", "/usage", result=report)
    live = tmp_path / "live.sqlite3"
    seed_kiro_db(live, ARN_A, "2099-01-01T00:00:00.000000Z")
    before = live.read_bytes()
    reg = ToolRegistry.default(r)
    reg.get("kiro").probe_dir = tmp_path / "probe"
    reg.get("kiro").live_db = live
    svc = VendorService(reg, VendorStore(tmp_path / "v"))
    a, _ = reg.get("kiro").register("acct-a")
    b, _ = reg.get("kiro").register("acct-b", profile_arn=ARN_B)
    svc.store.add(a)
    svc.store.add(b)
    assert svc.usage("kiro").attributed_to == "acct-a"
    assert svc.usage("kiro", "acct-a").windows[0].used == 100.0
    other = svc.usage("kiro", "acct-b")
    assert other.attributed_to == "acct-b" and other.windows[0].used == 0.0
    assert live.read_bytes() == before


def test_profile_switch_without_a_terminal_uses_kiros_picker_and_verifies():
    r, state = profile_world(ARN_A)
    picked = []

    def picker(exe, arn):
        picked.append(arn)
        state["arn"] = arn
        return True

    adapter = KiroAdapter(r, profile_picker=picker)
    b, _ = adapter.register("acct-b", profile_arn=ARN_B)
    outcome = adapter.switch(b, None, assume_yes=False, interactive=False, context=[b])
    assert picked == [ARN_B] and outcome.verified
    assert not any(c.interactive for c in r.calls)  # no terminal, no logout


def test_picker_refusal_is_reported():
    r, _ = profile_world(ARN_A)
    adapter = KiroAdapter(r, profile_picker=lambda exe, arn: "that profile is not offered")
    b, _ = adapter.register("acct-b", profile_arn=ARN_B)
    with pytest.raises(VendorError, match="not offered"):
        adapter.switch(b, None, assume_yes=False, interactive=False, context=[b])


def test_inactive_profile_is_measured_in_an_isolated_home(tmp_path):
    runner, _ = profile_world(ARN_A)
    runner.on(KIRO, "chat", "--help", result=ok("--no-interactive --v2"))
    seen = {}

    def probe(call):
        home = Path(call.env["HOME"])
        seen["home"] = home
        seen["unset"] = call.unset_env
        db = home / "Library/Application Support/kiro-cli/data.sqlite3"
        con = sqlite3.connect(db)
        raw = con.execute(
            "SELECT value FROM state WHERE key = 'api.codewhisperer.profile'"
        ).fetchone()[0]
        profile = json.loads(raw)
        seen["arn"] = profile["arn"]
        seen["name"] = profile["profile_name"]
        seen["auth"] = con.execute(
            "SELECT value FROM auth_kv WHERE key = 'kirocli:odic:token'"
        ).fetchone()[0]
        seen["history"] = con.execute("SELECT COUNT(*) FROM history").fetchone()[0]
        con.close()
        return ok(OTHER_REPORT)

    isolated = FakeRunner()
    isolated.on(KIRO, "chat", "--no-interactive", "--v2", "/usage", result=probe)
    live = tmp_path / "live.sqlite3"
    seed_kiro_db(live, ARN_A, "2099-01-01T00:00:00.000000Z", token="fake-token")
    before = live.read_bytes()
    adapter = KiroAdapter(
        runner, probe_dir=tmp_path / "probe", live_db=live, isolated_runner=isolated
    )
    record = login_rec("acct-b", {**IDC, "profileArn": ARN_B})
    result = adapter.usage(record, None)
    assert result.state is UsageState.AVAILABLE
    assert result.attributed_to == "acct-b"
    assert result.identity["profileArn"] == ARN_B
    assert result.identity["email"] == IDC["email"]
    assert result.windows[0].used == 0.0
    assert seen["arn"] == ARN_B and seen["name"] == "P"
    assert json.loads(seen["auth"])["access_token"] == "fake-token"
    assert seen["history"] == 0
    assert seen["unset"] == (ENV_KEY,)
    assert not seen["home"].exists()
    assert live.read_bytes() == before
    assert not any("/usage" in call.argv for call in runner.calls)


def test_near_expiry_refreshes_the_active_profile_before_copying(tmp_path):
    runner, _ = profile_world(ARN_A)
    runner.on(KIRO, "chat", "--help", result=ok("--v2"))
    seen = {}

    def probe(call):
        seen.setdefault("order", []).append("home" if call.env.get("HOME") else "live")
        if call.env.get("HOME"):
            seen["home"] = Path(call.env["HOME"])
            db = seen["home"] / "Library/Application Support/kiro-cli/data.sqlite3"
            con = sqlite3.connect(db)
            raw = con.execute(
                "SELECT value FROM state WHERE key = 'api.codewhisperer.profile'"
            ).fetchone()[0]
            seen["arn"] = json.loads(raw)["arn"]
            con.close()
            return ok(OTHER_REPORT)
        return ok(ACTIVE_REPORT)

    runner.on(KIRO, "chat", "--no-interactive", "--v2", "/usage", result=probe)
    live = tmp_path / "live.sqlite3"
    seed_kiro_db(live, ARN_A, _iso_in(4))
    before = live.read_bytes()
    adapter = KiroAdapter(runner, probe_dir=tmp_path / "probe", live_db=live)
    result = adapter.usage(login_rec("acct-b", {**IDC, "profileArn": ARN_B}), None)
    assert result.attributed_to == "acct-b" and result.windows[0].used == 0.0
    assert seen["order"] == ["live", "home"] and seen["arn"] == ARN_B
    assert not seen["home"].exists()
    assert live.read_bytes() == before


def test_a_different_login_is_not_read_from_the_database(tmp_path):
    runner, _ = make(IDC)
    runner.on(KIRO, "chat", "--help", result=ok("--v2"))
    isolated = FakeRunner()
    isolated.on(KIRO, "chat", "--no-interactive", "--v2", "/usage", result=ok(OTHER_REPORT))
    live = tmp_path / "missing.sqlite3"
    adapter = KiroAdapter(
        runner, probe_dir=tmp_path / "probe", live_db=live, isolated_runner=isolated
    )
    result = adapter.usage(login_rec("b", OTHER), None)
    assert result.state is UsageState.UNAVAILABLE
    assert "not the signed-in" in result.reason
    assert isolated.calls == []
    assert not live.exists()


def test_register_rejects_a_different_email_and_keeps_same_login_profiles():
    runner, _ = profile_world(ARN_A)
    adapter = KiroAdapter(runner)
    with pytest.raises(VendorError, match="kiro-cli login"):
        adapter.register("other", email="b@corp.com")
    with pytest.raises(VendorError, match="--profile-arn"):
        adapter.register("other", email="b@corp.com", profile_arn=ARN_B)
    same, _ = adapter.register("same", email="A@Corp.com")
    assert same.identity["email"] == IDC["email"]
    profile, _ = adapter.register("acct-b", profile_arn=ARN_B)
    assert profile.identity["email"] == IDC["email"]
    assert profile.identity["profileArn"] == ARN_B


def test_picker_that_lands_elsewhere_is_not_success():
    r, _ = profile_world(ARN_A)
    adapter = KiroAdapter(
        r, profile_picker=lambda exe, arn: True
    )  # claims success, nothing changed
    b, _ = adapter.register("acct-b", profile_arn=ARN_B)
    with pytest.raises(VendorError, match="276673691073"):
        adapter.switch(b, None, assume_yes=False, interactive=False, context=[b])
