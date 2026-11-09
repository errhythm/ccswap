"""A fork build must never be 'updated' to upstream, which lacks the vendor integrations."""

from __future__ import annotations

from unittest.mock import patch

from claude_swap import update_check


def test_fork_build_gets_no_update_notice_and_makes_no_lookup():
    with patch.object(update_check, "_latest_version") as lookup:
        assert update_check.check_for_update("0.48.0+vendors.1") is None
    lookup.assert_not_called()


def test_fork_build_upgrade_is_refused_with_a_reason():
    reason = update_check.fork_upgrade_refusal("0.48.0+vendors.1")
    assert reason and "fork build" in reason and "Reinstall" in reason


def test_cli_upgrade_flag_never_reaches_the_package_manager_on_a_fork(capsys, monkeypatch):
    import pytest

    from claude_swap import cli

    monkeypatch.setattr("sys.argv", ["ccswap", "--upgrade"])
    monkeypatch.setattr(cli, "__version__", "0.48.0+vendors.1")
    with (
        patch("claude_swap.update_check.run_self_upgrade") as upgrade,
        pytest.raises(SystemExit) as exc,
    ):
        cli.main()
    upgrade.assert_not_called()
    assert exc.value.code == 1


def test_upstream_versions_are_unaffected():
    assert update_check.fork_upgrade_refusal("0.47.0") is None
