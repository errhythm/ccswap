"""Tests for file-backed Codex account switching."""

from __future__ import annotations

import base64
import json

import pytest

from claude_swap import codex
from claude_swap.codex_usage import CodexUsageError
from claude_swap.exceptions import ConfigError, SwitchError


def _jwt(email: str, account_id: str) -> str:
    payload = base64.urlsafe_b64encode(
        json.dumps({"email": email, "chatgpt_account_id": account_id}).encode()
    ).decode().rstrip("=")
    return f"eyJhbGciOiJub25lIn0.{payload}.signature"


def _auth(email: str, account_id: str) -> dict:
    return {
        "auth_mode": "chatgpt",
        "tokens": {
            "id_token": _jwt(email, account_id),
            "access_token": f"access-{account_id}",
            "refresh_token": f"refresh-{account_id}",
            "account_id": account_id,
        },
    }


@pytest.fixture
def switcher(tmp_path, monkeypatch):
    backup = tmp_path / "backup"
    home = tmp_path / "codex-home"
    home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.setattr(codex, "get_backup_root", lambda: backup)
    instance = codex.CodexAccountSwitcher()
    return instance, home


def _write_live(home, auth: dict) -> None:
    (home / "auth.json").write_text(json.dumps(auth), encoding="utf-8")


def test_add_and_switch_preserves_other_codex_files(switcher):
    instance, home = switcher
    (home / "config.toml").write_text('model = "gpt-test"\n', encoding="utf-8")
    _write_live(home, _auth("one@example.com", "account-one"))
    instance.add_account(assume_yes=True)

    _write_live(home, _auth("two@example.com", "account-two"))
    instance.add_account(assume_yes=True)

    result = instance.switch_to("1", json_output=True)

    assert result["switched"]
    assert json.loads((home / "auth.json").read_text()) == _auth(
        "one@example.com", "account-one"
    )
    assert (home / "config.toml").read_text(encoding="utf-8") == 'model = "gpt-test"\n'
    assert instance.current_account_number() == "1"


def test_add_refreshes_existing_account_in_place(switcher):
    instance, home = switcher
    _write_live(home, _auth("one@example.com", "account-one"))
    instance.add_account(assume_yes=True)

    refreshed = _auth("one@example.com", "account-one")
    refreshed["tokens"]["refresh_token"] = "fresh-refresh-token"
    _write_live(home, refreshed)
    instance.add_account(assume_yes=True)

    listed = instance.list_accounts()
    assert [account["number"] for account in listed["accounts"]] == [1]
    assert json.loads((instance.credentials_dir / "account-1.json").read_text()) == refreshed


def test_switch_saves_the_currently_refreshed_auth_before_activating_target(switcher):
    instance, home = switcher
    _write_live(home, _auth("one@example.com", "account-one"))
    instance.add_account(assume_yes=True)
    _write_live(home, _auth("two@example.com", "account-two"))
    instance.add_account(assume_yes=True)

    refreshed = _auth("two@example.com", "account-two")
    refreshed["tokens"]["refresh_token"] = "rotated-refresh-token"
    _write_live(home, refreshed)
    instance.switch_to("1", json_output=True)

    assert json.loads((instance.credentials_dir / "account-2.json").read_text()) == refreshed


def test_disabled_account_is_skipped_by_rotation_but_stays_explicit_target(switcher):
    instance, home = switcher
    for name in ("one", "two", "three"):
        _write_live(home, _auth(f"{name}@example.com", f"account-{name}"))
        instance.add_account(assume_yes=True)
    instance.switch_to("1", json_output=True)

    instance.set_account_disabled("2", True)
    assert instance.is_account_disabled("2")
    assert instance.accounts_snapshot(fetch=set()).accounts[1].disabled

    assert instance.switch(json_output=True)["to"]["number"] == 3  # 2 skipped
    assert instance.switch_to("2", json_output=True)["switched"]  # still explicit

    instance.set_account_disabled("2", False)
    instance.switch_to("1", json_output=True)
    assert instance.switch(json_output=True)["to"]["number"] == 2


def test_switch_raises_when_every_codex_account_is_disabled(switcher):
    instance, home = switcher
    _write_live(home, _auth("one@example.com", "account-one"))
    instance.add_account(assume_yes=True)
    instance.set_account_disabled("1", True)

    with pytest.raises(SwitchError, match="disabled"):
        instance.switch(json_output=True)


def test_switch_refuses_to_overwrite_an_unmanaged_live_login(switcher):
    instance, home = switcher
    _write_live(home, _auth("one@example.com", "account-one"))
    instance.add_account(assume_yes=True)
    _write_live(home, _auth("two@example.com", "account-two"))
    instance.add_account(assume_yes=True)
    unmanaged = _auth("outside@example.com", "outside-account")
    _write_live(home, unmanaged)

    with pytest.raises(SwitchError, match="unmanaged"):
        instance.switch_to("1", json_output=True)

    assert json.loads((home / "auth.json").read_text()) == unmanaged


def test_seats_sharing_a_workspace_keep_their_own_slots(switcher):
    instance, home = switcher
    _write_live(home, _auth("one@example.com", "shared-workspace"))
    instance.add_account(assume_yes=True)

    _write_live(home, _auth("two@example.com", "shared-workspace"))
    instance.add_account(assume_yes=True)

    listed = instance.list_accounts()
    assert [account["email"] for account in listed["accounts"]] == [
        "one@example.com",
        "two@example.com",
    ]
    assert json.loads((instance.credentials_dir / "account-1.json").read_text()) == _auth(
        "one@example.com", "shared-workspace"
    )


def test_live_seat_is_identified_by_email_not_just_workspace(switcher):
    instance, home = switcher
    _write_live(home, _auth("one@example.com", "shared-workspace"))
    instance.add_account(assume_yes=True)
    _write_live(home, _auth("two@example.com", "shared-workspace"))
    instance.add_account(slot=2, assume_yes=True)

    assert instance.current_account_number() == "2"


def test_switch_away_from_a_shared_workspace_seat_keeps_both_logins(switcher):
    instance, home = switcher
    _write_live(home, _auth("one@example.com", "shared-workspace"))
    instance.add_account(assume_yes=True)
    _write_live(home, _auth("two@example.com", "shared-workspace"))
    instance.add_account(slot=2, assume_yes=True)

    result = instance.switch_to("1", json_output=True)

    assert result["switched"]
    assert json.loads((home / "auth.json").read_text()) == _auth(
        "one@example.com", "shared-workspace"
    )
    assert json.loads((instance.credentials_dir / "account-1.json").read_text()) == _auth(
        "one@example.com", "shared-workspace"
    )
    assert json.loads((instance.credentials_dir / "account-2.json").read_text()) == _auth(
        "two@example.com", "shared-workspace"
    )


def test_unregistered_sibling_seat_is_not_mistaken_for_a_managed_one(switcher):
    instance, home = switcher
    _write_live(home, _auth("one@example.com", "shared-workspace"))
    instance.add_account(assume_yes=True)
    _write_live(home, _auth("two@example.com", "shared-workspace"))

    assert instance.current_account_number() is None


def test_switching_away_from_an_unregistered_sibling_seat_is_refused(switcher):
    instance, home = switcher
    _write_live(home, _auth("one@example.com", "shared-workspace"))
    instance.add_account(assume_yes=True)
    _write_live(home, _auth("elsewhere@example.com", "other-workspace"))
    instance.add_account(assume_yes=True)
    _write_live(home, _auth("two@example.com", "shared-workspace"))

    with pytest.raises(SwitchError, match="unmanaged"):
        instance.switch_to("2", json_output=True)

    assert json.loads((instance.credentials_dir / "account-1.json").read_text()) == _auth(
        "one@example.com", "shared-workspace"
    )


def test_the_same_email_in_two_workspaces_keeps_separate_slots(switcher):
    instance, home = switcher
    _write_live(home, _auth("same@example.com", "workspace-one"))
    instance.add_account(assume_yes=True)

    _write_live(home, _auth("same@example.com", "workspace-two"))
    instance.add_account(assume_yes=True)

    assert [account["number"] for account in instance.list_accounts()["accounts"]] == [1, 2]
    assert json.loads((instance.credentials_dir / "account-1.json").read_text()) == _auth(
        "same@example.com", "workspace-one"
    )


def test_a_renamed_login_is_not_claimed_by_its_old_slot(switcher):
    instance, home = switcher
    _write_live(home, _auth("old@example.com", "account-one"))
    instance.add_account(assume_yes=True)

    _write_live(home, _auth("new@example.com", "account-one"))

    assert instance.current_account_number() is None


def test_an_unreadable_live_identity_matches_nothing(switcher):
    instance, home = switcher
    _write_live(home, _auth("one@example.com", "account-one"))
    instance.add_account(assume_yes=True)
    _write_live(home, _auth("two@example.com", "account-two"))
    instance.add_account(assume_yes=True)

    # Legacy metadata with blank fields must not be matched by a login whose
    # own identity failed to decode; switch_to reads auth.json directly and so
    # never sees the validation _read_live_auth applies.
    sequence = json.loads(instance.sequence_file.read_text())
    sequence["accounts"]["1"]["email"] = ""
    sequence["accounts"]["1"]["accountId"] = ""
    instance.sequence_file.write_text(json.dumps(sequence), encoding="utf-8")
    _write_live(home, {"auth_mode": "chatgpt", "tokens": {"id_token": "not-a-jwt"}})

    with pytest.raises(SwitchError, match="unmanaged"):
        instance.switch_to("2", json_output=True)

    assert json.loads((instance.credentials_dir / "account-1.json").read_text()) == _auth(
        "one@example.com", "account-one"
    )


def test_api_key_accounts_get_a_stable_non_secret_label(switcher):
    instance, home = switcher
    _write_live(
        home,
        {"auth_mode": "api_key", "OPENAI_API_KEY": "sk-test-not-a-real-key"},
    )

    instance.add_account(assume_yes=True)

    account = instance.list_accounts()["accounts"][0]
    assert account["email"].startswith("api-key-")
    assert account["email"].endswith("@codex.local")
    assert "sk-test" not in account["email"]


def test_keyring_only_configuration_is_rejected_without_writing(switcher):
    instance, home = switcher
    (home / "config.toml").write_text(
        'cli_auth_credentials_store = "keyring"\n', encoding="utf-8"
    )

    with pytest.raises(ConfigError, match="OS keyring"):
        instance.add_account(assume_yes=True)

    assert not instance.sequence_file.exists()


def test_snapshot_fetches_codex_usage_and_reuses_fresh_result(switcher, monkeypatch):
    instance, home = switcher
    _write_live(home, _auth("one@example.com", "account-one"))
    instance.add_account(assume_yes=True)
    fetched: list[dict] = []

    def fetch(auth, *, base_url):
        fetched.append(auth)
        assert base_url == "https://chatgpt.com/backend-api"
        return {"five_hour": {"pct": 25}, "seven_day": {"pct": 50}}

    monkeypatch.setattr(codex, "fetch_codex_usage", fetch)

    snapshot = instance.accounts_snapshot()
    cached = instance.accounts_snapshot(fetch=set())

    assert snapshot.active_number == "1"
    assert snapshot.accounts[0].usage.last_good == {
        "five_hour": {"pct": 25},
        "seven_day": {"pct": 50},
    }
    assert cached.accounts[0].usage.last_good == snapshot.accounts[0].usage.last_good
    assert len(fetched) == 1


def test_snapshot_marks_api_key_usage_as_not_applicable(switcher):
    instance, home = switcher
    _write_live(home, {"auth_mode": "api_key", "OPENAI_API_KEY": "sk-test"})
    instance.add_account(assume_yes=True)

    snapshot = instance.accounts_snapshot()

    assert snapshot.accounts[0].kind == "api_key"
    assert snapshot.accounts[0].usage.sentinel == "api key"


def test_snapshot_refreshes_an_inactive_codex_login_after_usage_401(switcher, monkeypatch):
    instance, home = switcher
    _write_live(home, _auth("one@example.com", "account-one"))
    instance.add_account(assume_yes=True)
    _write_live(home, _auth("two@example.com", "account-two"))
    instance.add_account(assume_yes=True)

    refreshed = _auth("one@example.com", "account-one")
    refreshed["tokens"]["access_token"] = "fresh-access"
    refreshed["tokens"]["refresh_token"] = "fresh-refresh"
    refresh_calls = []

    def fetch(auth, *, base_url):
        if auth["tokens"]["access_token"] != "fresh-access":
            raise CodexUsageError("expired", status_code=401)
        return {"five_hour": {"pct": 25}}

    def refresh(auth):
        refresh_calls.append(auth)
        return refreshed

    monkeypatch.setattr(codex, "fetch_codex_usage", fetch)
    monkeypatch.setattr(codex, "refresh_codex_auth", refresh)

    snapshot = instance.accounts_snapshot(fetch={"1"})

    assert snapshot.accounts[0].usage.last_good == {"five_hour": {"pct": 25}}
    assert refresh_calls == [_auth("one@example.com", "account-one")]
    assert json.loads((instance.credentials_dir / "account-1.json").read_text()) == refreshed


def test_snapshot_never_refreshes_the_active_codex_login(switcher, monkeypatch):
    instance, home = switcher
    _write_live(home, _auth("one@example.com", "account-one"))
    instance.add_account(assume_yes=True)

    monkeypatch.setattr(
        codex,
        "fetch_codex_usage",
        lambda auth, *, base_url: (_ for _ in ()).throw(
            CodexUsageError("expired", status_code=401)
        ),
    )
    monkeypatch.setattr(
        codex,
        "refresh_codex_auth",
        lambda auth: pytest.fail("the active Codex auth must not be refreshed"),
    )

    snapshot = instance.accounts_snapshot()

    assert snapshot.accounts[0].usage.last_good is None
    assert snapshot.accounts[0].usage.last_error == "expired"


def test_snapshot_never_refreshes_a_token_the_live_login_still_holds(switcher, monkeypatch):
    instance, home = switcher
    _write_live(home, _auth("old@example.com", "account-one"))
    instance.add_account(assume_yes=True)

    # The login was renamed, so it no longer matches the slot that stores it and
    # reads as inactive -- but Codex is still using the token in that backup.
    _write_live(home, _auth("new@example.com", "account-one"))
    assert instance.current_account_number() is None

    monkeypatch.setattr(
        codex,
        "fetch_codex_usage",
        lambda auth, *, base_url: (_ for _ in ()).throw(
            CodexUsageError("expired", status_code=401)
        ),
    )
    monkeypatch.setattr(
        codex,
        "refresh_codex_auth",
        lambda auth: pytest.fail("rotating a live refresh token logs that session out"),
    )

    snapshot = instance.accounts_snapshot(fetch={"1"})

    assert snapshot.accounts[0].usage.last_error == "expired"
    assert json.loads((instance.credentials_dir / "account-1.json").read_text()) == _auth(
        "old@example.com", "account-one"
    )


def _jwt_with_plan(email: str, account_id: str, plan: str) -> str:
    payload = base64.urlsafe_b64encode(
        json.dumps(
            {
                "email": email,
                "chatgpt_account_id": account_id,
                "https://api.openai.com/auth": {"chatgpt_plan_type": plan},
            }
        ).encode()
    ).decode().rstrip("=")
    return f"eyJhbGciOiJub25lIn0.{payload}.signature"


def _auth_with_plan(email: str, account_id: str, plan: str) -> dict:
    auth = _auth(email, account_id)
    auth["tokens"]["id_token"] = _jwt_with_plan(email, account_id, plan)
    return auth


@pytest.mark.parametrize(
    "plan,expected",
    [
        ("team", "Codex Team"),
        ("pro", "Codex Pro"),
        ("plus", "Codex Plus"),
        ("business", "Codex Business"),
        ("enterprise", "Codex Enterprise"),
        ("", "Codex"),
        (None, "Codex"),
        ("go_pro", "Codex Go Pro"),
    ],
)
def test_codex_org_label_maps_plan_to_display(plan, expected):
    assert codex.codex_org_label(plan) == expected


def test_plan_type_read_from_token_and_falls_back_to_access_token():
    assert codex._plan_type_from_auth(_auth_with_plan("a@b.co", "acct", "team")) == "team"
    # id_token without a plan claim -> read the access_token namespace instead.
    auth = _auth("a@b.co", "acct")
    auth["tokens"]["access_token"] = _jwt_with_plan("a@b.co", "acct", "pro")
    assert codex._plan_type_from_auth(auth) == "pro"
    assert codex._plan_type_from_auth(_auth("a@b.co", "acct")) == ""


def test_add_records_plan_and_list_shows_team_label(switcher):
    instance, home = switcher
    _write_live(home, _auth_with_plan("team@example.com", "acct-team", "team"))
    instance.add_account(assume_yes=True)

    stored = json.loads(instance.sequence_file.read_text())
    assert stored["accounts"]["1"]["planType"] == "team"

    listed = instance.list_accounts()["accounts"][0]
    assert listed["planType"] == "team"
    assert listed["label"] == "Codex Team"


def test_list_derives_plan_for_accounts_saved_without_plan_type(switcher):
    """Accounts stored before planType existed still show their plan."""
    instance, home = switcher
    _write_live(home, _auth_with_plan("team@example.com", "acct-team", "team"))
    instance.add_account(assume_yes=True)

    # Simulate a legacy backup: drop the recorded planType from the sequence.
    data = json.loads(instance.sequence_file.read_text())
    del data["accounts"]["1"]["planType"]
    instance.sequence_file.write_text(json.dumps(data), encoding="utf-8")

    listed = instance.list_accounts()["accounts"][0]
    assert listed["planType"] == "team"
    assert listed["label"] == "Codex Team"


def test_snapshot_org_name_reflects_plan(switcher, monkeypatch):
    instance, home = switcher
    _write_live(home, _auth_with_plan("team@example.com", "acct-team", "team"))
    instance.add_account(assume_yes=True)
    monkeypatch.setattr(
        codex, "fetch_codex_usage", lambda auth, *, base_url: {"five_hour": {"pct": 1}}
    )

    snapshot = instance.accounts_snapshot()

    assert snapshot.accounts[0].org_name == "Codex Team"
    assert snapshot.accounts[0].display_tag == "Codex Team"


def test_usage_text_output_includes_reset_countdown(switcher, monkeypatch, capsys):
    instance, home = switcher
    _write_live(home, _auth_with_plan("team@example.com", "acct-team", "team"))
    instance.add_account(assume_yes=True)
    monkeypatch.setattr(
        codex,
        "fetch_codex_usage",
        lambda auth, *, base_url: {
            "five_hour": {"pct": 47, "resets_at": "2999-01-01T00:00:00Z"},
            "credits": {
                "has_credits": True,
                "balance": 2500.0,
            },
            "credit_allowance": {
                "remaining": 4678.44787979126,
                "limit": 5000.0,
                "resets_at": "2999-01-01T00:00:00Z",
            },
        },
    )

    instance.usage_status()

    out = capsys.readouterr().out
    assert "47% used" in out
    assert "resets in" in out
    assert "Credits: 2,500.00 available" in out
    assert "Credit allowance: 4,678.45 remaining · 5,000.00 limit" in out


def test_usage_text_output_prefers_no_credits_to_zero_balance(
    switcher, monkeypatch, capsys
):
    instance, home = switcher
    _write_live(home, _auth_with_plan("team@example.com", "acct-team", "team"))
    instance.add_account(assume_yes=True)
    monkeypatch.setattr(
        codex,
        "fetch_codex_usage",
        lambda auth, *, base_url: {
            "credits": {"has_credits": False, "balance": 0.0}
        },
    )

    instance.usage_status()

    out = capsys.readouterr().out
    assert "Credits: none" in out
    assert "available" not in out


@pytest.mark.parametrize(
    ("credits", "expected"),
    [
        ({"has_credits": False, "limit_reached": True}, "Credits: limit reached"),
        ({"has_credits": False, "unlimited": True}, "Credits: unlimited"),
    ],
)
def test_usage_text_output_credit_state_precedence(
    switcher, monkeypatch, capsys, credits, expected
):
    instance, home = switcher
    _write_live(home, _auth_with_plan("team@example.com", "acct-team", "team"))
    instance.add_account(assume_yes=True)
    monkeypatch.setattr(
        codex,
        "fetch_codex_usage",
        lambda auth, *, base_url: {"credits": credits},
    )

    instance.usage_status()

    assert expected in capsys.readouterr().out


@pytest.fixture
def strategy_accounts(switcher, monkeypatch):
    from dataclasses import replace

    from claude_swap.models import AccountsSnapshot
    from claude_swap.usage_store import UsageEntry

    instance, home = switcher
    for number in range(1, 5):
        _write_live(home, _auth(f"{number}@example.com", f"account-{number}"))
        instance.add_account(assume_yes=True)
    instance.switch_to("1", json_output=True)
    base = instance.accounts_snapshot(fetch=set())

    def configure(
        pcts, *, disabled=(), api_key=(), sentinel=(), resets=None,
        active="1", unswitchable=(),
    ):
        accounts = []
        for account, pct in zip(base.accounts, pcts):
            usage = None if pct is None else {"weekly": {"pct": pct}}
            if usage and resets and account.number in resets:
                usage["weekly"]["resets_at"] = resets[account.number]
            entry = UsageEntry(
                last_good=usage,
                sentinel="error" if account.number in sentinel else None,
            )
            accounts.append(replace(
                account,
                kind="api_key" if account.number in api_key else "oauth",
                disabled=account.number in disabled,
                switchable=account.number not in unswitchable,
                usage=entry,
            ))
        snapshot = AccountsSnapshot(active_number=active, accounts=tuple(accounts), taken_at=0)
        monkeypatch.setattr(instance, "accounts_snapshot", lambda: snapshot)
        return instance

    return configure


@pytest.mark.parametrize("pcts,reason", [
    ([20, 20, 50, 70], "already-best"),
    ([10, 20, 50, 70], "already-best"),
    ([None, 20, 50, 70], "usage-unavailable"),
    ([100, 100, 110, 100], "candidates-exhausted"),
    ([10, None, 50, 70], "usage-unavailable"),
    ([10, None, None, None], "usage-unavailable"),
])
def test_strategy_best_stays(strategy_accounts, pcts, reason, capsys, monkeypatch):
    instance = strategy_accounts(pcts)
    capsys.readouterr()
    result = instance.switch(strategy="best", json_output=True)
    assert result == {
        "switched": False, "from": {"number": 1, "email": "1@example.com"},
        "to": {"number": 1, "email": "1@example.com"},
        "reason": reason, "strategy": "best", "message": result["message"],
    }
    assert result["message"]
    assert capsys.readouterr().out == ""
    monkeypatch.setattr(codex, "accent", lambda text: f"accent:{text}")
    monkeypatch.setattr(codex, "dimmed", lambda text: f"dimmed:{text}")
    monkeypatch.setattr(codex, "warning", lambda text: print(f"warning:{text}"))
    instance.switch(strategy="best")
    output = capsys.readouterr().out
    assert len(output.splitlines()) == 1
    style = {
        "already-best": "accent", "candidates-exhausted": "warning",
        "usage-unavailable": "dimmed",
    }[reason]
    assert output.startswith(f"{style}:")
    if reason == "usage-unavailable":
        assert output.rstrip().endswith("Run 'ccswap codex switch' to rotate.")
    assert instance.current_account_number() == "1"


def test_strategy_best_switches_to_most_headroom(strategy_accounts):
    instance = strategy_accounts([90, 50, 10, 30])
    result = instance.switch(strategy="best", json_output=True)
    assert result["switched"]
    assert result["strategy"] == "best"
    assert result["from"]["number"] == 1
    assert result["to"]["number"] == 3
    assert instance.current_account_number() == "3"


@pytest.mark.parametrize("excluded", ["disabled", "api_key", "unswitchable", "sentinel"])
def test_strategy_best_excludes_ineligible_usage(strategy_accounts, excluded):
    instance = strategy_accounts([80, 0, 50, 90], **{excluded: ("2",)})
    assert instance.switch(strategy="best", json_output=True)["to"]["number"] == 3


@pytest.mark.parametrize("pcts,kwargs,target", [
    ([50, 100, 30, 10], {"disabled": ("3",)}, 4),
    ([50, None, 30, 10], {}, 2),
    ([50, 0, 30, 10], {"api_key": ("2",)}, 3),
])
def test_strategy_next_available(strategy_accounts, pcts, kwargs, target):
    instance = strategy_accounts(pcts, **kwargs)
    assert instance.switch(strategy="next-available", json_output=True)["to"]["number"] == target


def test_strategy_next_available_all_exhausted(strategy_accounts):
    instance = strategy_accounts([50, 100, 100, 100])
    result = instance.switch(strategy="next-available", json_output=True)
    assert not result["switched"]
    assert result["reason"] == "candidates-exhausted"


@pytest.mark.parametrize("strategy", ["best", "consume-first", "next-available"])
def test_strategy_without_managed_current_stays(strategy_accounts, strategy):
    instance = strategy_accounts([50, 10, 20, 30], active=None)
    result = instance.switch(strategy=strategy, json_output=True)
    assert not result["switched"]
    assert result["reason"] == "usage-unavailable"
    assert result["from"]["number"] is None


@pytest.mark.parametrize("resets,pcts,target", [
    ({"1": 400, "2": 100, "3": 200}, [20, 90, 10, 50], 2),
    ({"1": 400, "2": -100, "3": 200}, [20, 0, 10, 50], 3),
    ({"1": 400, "2": 100, "3": 200}, [20, 100, 10, 50], 3),
    ({}, [80, 50, 10, 90], 3),
    ({"1": 100, "2": 100}, [80, 10, 20, 30], 1),
    ({"2": 100, "3": 100}, [80, 50, 10, 30], 2),
])
def test_strategy_consume_first(strategy_accounts, monkeypatch, resets, pcts, target):
    from datetime import datetime, timezone

    now = 1800000000
    monkeypatch.setattr(codex.time, "time", lambda: now)
    instance = strategy_accounts(pcts, resets={
        number: datetime.fromtimestamp(now + offset, timezone.utc).isoformat()
        for number, offset in resets.items()
    })
    result = instance.switch(strategy="consume-first", json_output=True)
    assert result["to"]["number"] == target
    assert result["switched"] is (target != 1)
    if target == 1:
        assert result["reason"] == "already-best"
        assert "weekly" in result["message"]


def test_strategy_only_one_account(switcher, monkeypatch):
    instance, home = switcher
    _write_live(home, _auth("one@example.com", "account-one"))
    instance.add_account(assume_yes=True)
    snapshot = instance.accounts_snapshot(fetch=set())
    monkeypatch.setattr(instance, "accounts_snapshot", lambda: snapshot)
    result = instance.switch(strategy="best", json_output=True)
    assert not result["switched"]
    assert result["reason"] == "only-one-account"


def test_strategy_rotation_anchors_on_live_login(strategy_accounts):
    instance = strategy_accounts([10, 20, 30, 40])
    instance.switch_to("3", json_output=True)
    snapshot = instance.accounts_snapshot()
    from dataclasses import replace

    instance.accounts_snapshot = lambda: replace(snapshot, active_number="3")
    result = instance.switch(strategy="next-available", json_output=True)
    assert result["from"]["number"] == 3
    assert result["to"]["number"] == 4


def test_plain_rotation_does_not_collect_usage(strategy_accounts, monkeypatch):
    instance = strategy_accounts([10, 100, 0, 0])

    def unexpected_snapshot():
        pytest.fail("Plain rotation must not collect usage")

    monkeypatch.setattr(instance, "accounts_snapshot", unexpected_snapshot)
    assert instance.switch(json_output=True) == {
        "switched": True, "from": {"number": 1, "email": ""},
        "to": {"number": 2, "email": "2@example.com"}, "reason": "requested",
    }


@pytest.fixture
def redemption(switcher, monkeypatch, capsys):
    instance, home = switcher
    _write_live(home, _auth("one@example.com", "one"))
    instance.add_account(assume_yes=True)
    capsys.readouterr()
    usage = {"five_hour": {"pct": 80}, "weekly": {"pct": 90}, "reset_credits": {"available": 2}}
    monkeypatch.setattr(codex, "fetch_codex_usage", lambda *a, **k: usage)
    monkeypatch.setattr(codex, "list_reset_credits", lambda *a, **k: [{"id": "earliest"}])
    return instance, home, usage


@pytest.mark.parametrize(
    "code", ["reset", "nothing_to_reset", "no_credit", "already_redeemed", "unexpected"]
)
def test_redeem_outcomes(redemption, monkeypatch, capsys, code):
    instance, _, usage = redemption
    calls = []
    after = {**usage, "reset_credits": {"available": 1}}
    readings = iter((usage, after))
    monkeypatch.setattr(codex, "fetch_codex_usage", lambda *a, **k: next(readings))

    def consume(*args, **kwargs):
        calls.append(kwargs)
        return {"code": code, "windows_reset": 2}
    monkeypatch.setattr(codex, "consume_reset_credit", consume)
    if code in ("reset", "nothing_to_reset"):
        result = instance.redeem_reset("one@example.com", True, True)
        assert result["confirmed"] == (code == "reset")
        assert result["usageAfter"] == (after if code == "reset" else None)
    else:
        with pytest.raises(SwitchError, match=code if code == "unexpected" else "card"):
            instance.redeem_reset("1", True, True)
    result = json.loads(capsys.readouterr().out)
    assert result["code"] == code
    assert result["creditId"] == "earliest"
    assert len(calls) == 1


@pytest.mark.parametrize("mode", ["zero", "api", "noninteractive", "no", "json"])
def test_redeem_refusals_never_post(redemption, monkeypatch, mode):
    instance, home, usage = redemption
    def forbidden(*a, **k):
        pytest.fail("must not spend a card")
    monkeypatch.setattr(codex, "consume_reset_credit", forbidden)
    monkeypatch.setattr(codex.sys.stdin, "isatty", lambda: mode == "no")
    if mode == "zero":
        usage["reset_credits"]["available"] = 0
    if mode == "api":
        _write_live(home, {"auth_mode": "api_key", "OPENAI_API_KEY": "test"})
        monkeypatch.setattr(instance, "current_account_number", lambda: "1")
    monkeypatch.setattr("builtins.input", lambda _: "n")
    with pytest.raises(SwitchError):
        instance.redeem_reset("1", mode in ("zero", "api"), mode == "json")


@pytest.mark.parametrize("json_output", [False, True])
def test_redeem_reread_failure(redemption, monkeypatch, capsys, json_output):
    instance, _, usage = redemption
    calls = []
    def fetch(*a, **k):
        calls.append(1)
        if len(calls) > 1:
            raise CodexUsageError("offline")
        return usage
    monkeypatch.setattr(codex, "fetch_codex_usage", fetch)
    monkeypatch.setattr(codex, "consume_reset_credit", lambda *a, **k: {"code": "reset"})
    warnings = []
    monkeypatch.setattr(codex, "warning", warnings.append)
    result = instance.redeem_reset("1", True, json_output)
    assert result["confirmed"] is False
    output = capsys.readouterr().out
    if json_output:
        assert json.loads(output)["confirmed"] is False
        assert warnings == []
    else:
        assert warnings == [
            "Reset accepted but could not be confirmed; run 'ccswap codex usage' to check"
        ]
    assert len(calls) == 2


def test_redeem_inactive_401_does_not_retry_or_refresh(redemption, monkeypatch):
    instance, home, _ = redemption
    _write_live(home, _auth("two@example.com", "two"))
    instance.add_account(assume_yes=True)
    calls = []

    def consume(auth, **kwargs):
        calls.append(kwargs)
        raise CodexUsageError("unauthorized", status_code=401)

    monkeypatch.setattr(codex, "consume_reset_credit", consume)
    monkeypatch.setattr(instance, "_refresh_inactive_auth", lambda *a: pytest.fail("POST retry"))
    with pytest.raises(SwitchError, match="nothing was redeemed"):
        instance.redeem_reset("1", True)
    assert len(calls) == 1


@pytest.mark.parametrize("status", [500, None, 401])
def test_redeem_json_reports_unknown_outcome(redemption, monkeypatch, capsys, status):
    instance, _, _ = redemption

    def consume(*a, **k):
        raise CodexUsageError("timed out", status_code=status)

    monkeypatch.setattr(codex, "consume_reset_credit", consume)
    with pytest.raises(SwitchError):
        instance.redeem_reset("1", True, True)
    out = capsys.readouterr().out
    if status == 401:
        assert out == ""
    else:
        assert json.loads(out) == {
            "account": {"number": 1, "email": "one@example.com"},
            "code": "unknown", "creditId": "earliest", "confirmed": False,
            "error": "timed out",
        }


@pytest.mark.parametrize("status", [500, None, 401])
def test_redeem_active_never_retries(redemption, monkeypatch, status):
    instance, _, _ = redemption
    calls = []
    def consume(*a, **k):
        calls.append(1)
        raise CodexUsageError("outcome is unknown", status_code=status)
    monkeypatch.setattr(codex, "consume_reset_credit", consume)
    message = "nothing was redeemed" if status == 401 else "unknown"
    with pytest.raises(SwitchError, match=message):
        instance.redeem_reset("1", True)
    assert len(calls) == 1


def test_redeem_preflight_inactive_refresh(redemption, monkeypatch):
    instance, home, usage = redemption
    _write_live(home, _auth("two@example.com", "two"))
    instance.add_account(assume_yes=True)
    calls = []
    refreshed = _auth("one@example.com", "one")
    refreshed["tokens"]["access_token"] = "new"
    def fetch(auth, **kwargs):
        calls.append(auth)
        if len(calls) == 1:
            raise CodexUsageError("unauthorized", status_code=401)
        return usage
    monkeypatch.setattr(codex, "fetch_codex_usage", fetch)
    monkeypatch.setattr(instance, "_refresh_inactive_auth", lambda *a: refreshed)
    monkeypatch.setattr(codex, "consume_reset_credit", lambda *a, **k: {"code": "nothing_to_reset"})
    instance.redeem_reset("1", True)
    assert calls[1] is refreshed


@pytest.mark.parametrize("failure", [EOFError, KeyboardInterrupt])
def test_redeem_interrupted_prompt_never_posts(redemption, monkeypatch, failure):
    instance, _, _ = redemption
    monkeypatch.setattr(codex.sys.stdin, "isatty", lambda: True)
    def interrupt(_):
        raise failure()
    def forbidden(*a, **k):
        pytest.fail("must not spend a card")
    monkeypatch.setattr("builtins.input", interrupt)
    monkeypatch.setattr(codex, "consume_reset_credit", forbidden)
    with pytest.raises(SwitchError, match="cancelled"):
        instance.redeem_reset("1")


def test_redeem_changed_slot_never_posts(redemption, monkeypatch):
    instance, _, _ = redemption
    monkeypatch.setattr(codex.sys.stdin, "isatty", lambda: True)
    def confirm(_):
        data = instance._read_sequence()
        data["accounts"]["1"]["email"] = "changed@example.com"
        instance._write_json(instance.sequence_file, data)
        return "y"
    monkeypatch.setattr("builtins.input", confirm)
    monkeypatch.setattr(
        codex, "consume_reset_credit", lambda *a, **k: pytest.fail("changed account")
    )
    with pytest.raises(SwitchError, match="changed"):
        instance.redeem_reset("1")


def test_redeem_usage_formats_windows_and_expiry(switcher, monkeypatch, capsys):
    instance, _ = switcher
    calls = []

    def format_time(value):
        calls.append(value)
        return "2h", "14:00"

    monkeypatch.setattr(codex, "format_reset", format_time)
    instance._print_redeem_usage({
        "five_hour": {"pct": 12.6, "resets_at": "reset-time"},
        "reset_credits": {"available": 2, "expires_at": "expiry-time"},
    })
    assert capsys.readouterr().out.splitlines() == [
        "  5h: 13% used · resets in 2h (14:00)",
        "  Banked resets: 2 · earliest expires in 2h (14:00)",
    ]
    assert calls == ["reset-time", "expiry-time"]


@pytest.mark.parametrize("code,message", [
    ("no_credit", "No reset card is available."),
    ("already_redeemed", "The reset card was already redeemed."),
    ("unexpected", "Unknown reset response code: unexpected"),
])
def test_redeem_human_errors_are_not_printed_twice(
    redemption, monkeypatch, capsys, code, message
):
    instance, _, _ = redemption
    monkeypatch.setattr(codex, "consume_reset_credit", lambda *a, **k: {"code": code})
    with pytest.raises(SwitchError, match=message):
        instance.redeem_reset("1", True)
    assert message not in capsys.readouterr().out


@pytest.mark.parametrize("json_output", [False, True])
@pytest.mark.parametrize("available", [2, 3, None])
def test_redeem_not_yet_visible(redemption, monkeypatch, capsys, json_output, available):
    instance, _, before = redemption
    after = {**before}
    after["reset_credits"] = {} if available is None else {"available": available}
    readings = iter((before, after))
    monkeypatch.setattr(codex, "fetch_codex_usage", lambda *a, **k: next(readings))
    monkeypatch.setattr(codex, "consume_reset_credit", lambda *a, **k: {"code": "reset"})
    warnings = []
    monkeypatch.setattr(codex, "warning", warnings.append)

    result = instance.redeem_reset("1", True, json_output)

    assert result["confirmed"] is False
    assert result["usageAfter"] == after
    if json_output:
        assert json.loads(capsys.readouterr().out)["confirmed"] is False
        assert warnings == []
    else:
        assert warnings == [
            "Reset accepted, but usage does not show it yet; "
            "run 'ccswap codex usage' to check"
        ]


@pytest.mark.parametrize("json_output", [False, True])
def test_redeem_without_card_details_never_posts(redemption, monkeypatch, json_output):
    instance, _, _ = redemption
    monkeypatch.setattr(codex, "list_reset_credits", lambda *a, **k: [])
    monkeypatch.setattr(
        codex, "consume_reset_credit", lambda *a, **k: pytest.fail("unknown card")
    )
    with pytest.raises(SwitchError, match="card details unavailable"):
        instance.redeem_reset("1", True, json_output)


def test_redeem_prompt_names_expiry_and_posts_exact_selected_card(
    redemption, monkeypatch
):
    from claude_swap.exceptions import LockError

    instance, _, _ = redemption
    monkeypatch.setattr(codex.sys.stdin, "isatty", lambda: True)
    expiry = "2026-11-01T12:30:00Z"
    lists = []
    prompts = []
    posts = []

    def cards(*a, **k):
        lists.append(1)
        return [{"id": "selected", "expires_at": expiry}]

    def confirm(prompt):
        # Waiting for consent must not block switches in another terminal.
        with codex.FileLock(instance.lock_file, timeout=0):
            prompts.append(prompt)
        return "y"

    def consume(*a, **kwargs):
        with pytest.raises(LockError):
            with codex.FileLock(instance.lock_file, timeout=0):
                pass
        posts.append(kwargs)
        return {"code": "nothing_to_reset"}

    monkeypatch.setattr(codex, "list_reset_credits", cards)
    monkeypatch.setattr("builtins.input", confirm)
    monkeypatch.setattr(codex, "consume_reset_credit", consume)
    instance.redeem_reset("1")
    assert len(lists) == 1
    assert len(prompts) == 1 and expiry in prompts[0] and "[y/N]" in prompts[0]
    assert len(posts) == 1 and posts[0]["credit_id"] == "selected"


@pytest.mark.parametrize("failure", ["500", "timeout", "401", "success"])
def test_redeem_inactive_post_safety(redemption, monkeypatch, failure):
    import io
    import urllib.error

    from claude_swap import codex_usage

    instance, home, _ = redemption
    _write_live(home, _auth("two@example.com", "two"))
    instance.add_account(assume_yes=True)
    posts = []
    listed = []
    refreshed = []

    def list_cards(*args, **kwargs):
        # Listing must happen before the POST lock is taken.
        with codex.FileLock(instance.lock_file, timeout=0):
            listed.append(1)
        return [{"id": "chosen-card"}]

    def refresh(auth):
        # A redemption POST must never trigger an inactive refresh.
        refreshed.append(auth)
        return {**auth, "tokens": {**auth["tokens"], "access_token": "refreshed-access"}}

    def urlopen(request, **kwargs):
        posts.append((json.loads(request.data), request.get_header("Authorization")))
        if failure == "timeout":
            raise TimeoutError()
        if failure == "500":
            raise urllib.error.HTTPError(request.full_url, 500, "error", {}, None)
        if failure == "401":
            raise urllib.error.HTTPError(request.full_url, 401, "unauthorized", {}, None)
        return io.BytesIO(b'{"code": "nothing_to_reset"}')

    monkeypatch.setattr(codex, "list_reset_credits", list_cards)
    monkeypatch.setattr(codex, "consume_reset_credit", codex_usage.consume_reset_credit)
    monkeypatch.setattr(codex_usage.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(codex, "refresh_codex_auth", refresh)

    if failure == "success":
        instance.redeem_reset("1", True)
    else:
        message = "nothing was redeemed" if failure == "401" else (
            "outcome is unknown"
        )
        with pytest.raises(SwitchError, match=message) as exc:
            instance.redeem_reset("1", True)
        if failure in ("500", "timeout"):
            assert "Check before retrying: another attempt may spend a second card." in str(
                exc.value
            )
        else:
            assert "Check the login before trying again." in str(exc.value)
            assert "unknown" not in str(exc.value)

    assert listed == [1]
    assert len(posts) == 1
    assert posts[0][1] == "Bearer access-one"
    assert posts[0][0]["credit_id"] == "chosen-card"
    assert refreshed == []


@pytest.mark.parametrize("failure", ["lock", "write", "http"])
def test_redeem_accepted_survives_confirmation_storage_errors(
    redemption, monkeypatch, capsys, failure
):
    from claude_swap.exceptions import LockError

    instance, _, usage = redemption
    reads = []

    def fetch(number):
        reads.append(number)
        if len(reads) > 1:
            if failure == "http":
                import http.client

                raise http.client.IncompleteRead(b"partial response")
            raise LockError("busy") if failure == "lock" else OSError("disk full")
        return usage

    monkeypatch.setattr(instance, "_fetch_redeem_usage", fetch)
    monkeypatch.setattr(codex, "consume_reset_credit", lambda *a, **k: {"code": "reset"})
    result = instance.redeem_reset("1", True, True)
    assert result["code"] == "reset" and result["confirmed"] is False
    assert result["usageAfter"] is None
    assert json.loads(capsys.readouterr().out) == result


@pytest.mark.parametrize("status", [None, "token_expired"])
def test_usage_expiry_warning_preserves_json_and_survives_status(switcher, monkeypatch, capsys, status):
    from types import SimpleNamespace
    from claude_swap.usage_store import UsageEntry
    instance, _ = switcher
    now = 1_700_000_000.0
    usage = {"reset_credits": {"available": 1, "expires_at": "2023-11-14T22:43:20Z"}}
    account = SimpleNamespace(number="1", email="fake@example.com", is_active=True,
                              usage=UsageEntry(last_good=usage, sentinel=status))
    monkeypatch.setattr(instance, "accounts_snapshot", lambda fetch: SimpleNamespace(accounts=[account]))
    monkeypatch.setattr(codex.time, "time", lambda: now)
    instance.usage_status()
    text = capsys.readouterr().out
    assert "⚠ Resets: 1 banked · earliest expires in 30m" in text
    assert bool("last known" in text) == bool(status)
    payload = instance.usage_status(json_output=True)
    assert payload["accounts"][0]["usage"] == usage
    assert "⚠" not in capsys.readouterr().out
