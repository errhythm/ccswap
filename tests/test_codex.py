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
