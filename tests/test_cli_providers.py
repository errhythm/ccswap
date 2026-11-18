"""CLI routing through the provider registry."""

from __future__ import annotations

import json
import sys
from unittest.mock import patch

import pytest

from ccswap import cli, providers
from ccswap.providers import Capabilities, ProviderSpec

CLAUDE = {"schemaVersion": 1, "activeAccountNumber": 1, "accounts": [{"number": 1}]}


class Fake:
    def __init__(self, debug=False):
        self.switched = []

    def list_payload(self):
        return {"schemaVersion": 1, "provider": "fake", "activeAccountNumber": None, "accounts": []}

    def list_accounts(self, json_output=False):
        print(json.dumps(self.list_payload()) if json_output else "fake accounts")

    def switch_to(self, identifier, json_output=False):
        return {"switched": True}


@pytest.fixture
def fake_provider(monkeypatch):
    spec = ProviderSpec("fake", "Fake AI", Capabilities(), lambda debug=False: Fake(debug))
    monkeypatch.setitem(providers.PROVIDERS, "fake", spec)
    return spec


def run_main(*argv):
    with patch("ccswap.cli.ClaudeAccountSwitcher") as claude_cls, \
         patch.object(sys, "argv", ["ccswap", *argv]), \
         patch("os.geteuid", return_value=1000, create=True), \
         patch("ccswap.update_check.check_for_update", return_value=None):
        claude_cls.return_value.list_accounts.return_value = CLAUDE
        cli.main()


def test_claude_prefix_matches_provider_flag(capsys):
    run_main("claude", "list", "--json")
    prefixed = capsys.readouterr().out
    run_main("list", "--provider", "claude", "--json")
    assert prefixed == capsys.readouterr().out
    assert json.loads(prefixed) == CLAUDE


def test_agy_uses_generic_parser(monkeypatch, capsys):
    calls = []

    class Agy:
        def list_accounts(self, json_output=False):
            calls.append(json_output)
            print("agy list")

    monkeypatch.setattr(cli, "AgyAccountSwitcher", Agy)
    run_main("agy", "list")
    assert capsys.readouterr().out == "agy list\n" and calls == [False]
    assert providers.get("agy").cli is None


def test_fake_provider_in_list_json_and_own_list(fake_provider, capsys):
    with patch("ccswap.cli.CodexAccountSwitcher") as codex_cls, \
         patch("ccswap.cli.AgyAccountSwitcher") as agy_cls:
        codex_cls.return_value.list_payload.return_value = {"accounts": []}
        agy_cls.return_value.list_payload.return_value = {"accounts": []}
        run_main("list", "--json")
    out = json.loads(capsys.readouterr().out)
    assert out["fake"]["provider"] == "fake"
    assert "fake" in [p["id"] for p in out["providers"]]
    run_main("fake", "list")
    assert capsys.readouterr().out == "fake accounts\n"


def test_provider_choice_comes_from_registry(fake_provider, capsys):
    run_main("list", "--provider", "fake", "--json")
    assert json.loads(capsys.readouterr().out)["provider"] == "fake"


def test_targetless_switch_refused_without_strategies(fake_provider, capsys):
    with pytest.raises(SystemExit) as raised:
        run_main("fake", "switch")
    assert raised.value.code == 2
    assert "NUM|EMAIL" in capsys.readouterr().err


def test_disable_only_when_capability(fake_provider):
    with pytest.raises(SystemExit) as raised:
        run_main("fake", "disable", "1")
    assert raised.value.code == 2
