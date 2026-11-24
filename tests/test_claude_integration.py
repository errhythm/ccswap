"""Tests for the opt-in Claude Code statusline and mod (claude_integration)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from ccswap import claude_integration as ci
from ccswap import cli
from ccswap.exceptions import ConfigError
from ccswap.settings import ACTION_KEYS, effective_settings, set_setting

OURS = {"type": "command", "command": "ccswap statusline"}
CUSTOM = {"type": "command", "command": "~/.claude/statusline.sh", "padding": 2}


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Temp CLAUDE_CONFIG_DIR and backup root; no real ~/.claude is touched."""
    config = tmp_path / "claude"
    config.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    root = tmp_path / "backup"
    root.mkdir()
    return config / "settings.json", root


def _write(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _read(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


class TestStatusline:
    def test_round_trip_restores_custom_statusline_exactly(self, env):
        path, root = env
        _write(path, {"model": "opus", "statusLine": CUSTOM, "env": {"A": "1"}})
        os.chmod(path, 0o644)

        ci.install_statusline(root)
        data = _read(path)
        assert data["statusLine"] == OURS
        assert data["model"] == "opus" and data["env"] == {"A": "1"}
        assert ci.statusline_installed()
        if sys.platform != "win32":
            assert path.stat().st_mode & 0o777 == 0o644

        ci.uninstall_statusline(root)
        assert _read(path) == {"model": "opus", "statusLine": CUSTOM, "env": {"A": "1"}}
        assert list(_read(path)) == ["model", "statusLine", "env"]
        assert not ci.statusline_installed()
        assert "claude" not in _read(root / "settings.json")

    def test_round_trip_without_previous_removes_key(self, env):
        path, root = env
        _write(path, {"model": "opus"})
        ci.install_statusline(root)
        assert _read(path)["statusLine"] == OURS
        ci.uninstall_statusline(root)
        assert _read(path) == {"model": "opus"}

    def test_creates_missing_file(self, env):
        path, root = env
        ci.install_statusline(root)
        assert _read(path) == {"statusLine": OURS}

    def test_corrupt_json_errors_and_leaves_file_untouched(self, env):
        path, root = env
        path.write_text('{"model": "opus",', encoding="utf-8")
        with pytest.raises(ConfigError, match="not valid JSON"):
            ci.install_statusline(root)
        assert path.read_text(encoding="utf-8") == '{"model": "opus",'
        with pytest.raises(ConfigError):
            ci.uninstall_statusline(root)
        assert path.read_text(encoding="utf-8") == '{"model": "opus",'

    def test_install_when_already_ours_keeps_saved_previous(self, env):
        path, root = env
        _write(path, {"statusLine": CUSTOM})
        ci.install_statusline(root)
        before = (root / "settings.json").read_text(encoding="utf-8")
        assert ci.install_statusline(root) == "Claude statusline is already on"
        assert (root / "settings.json").read_text(encoding="utf-8") == before
        ci.uninstall_statusline(root)
        assert _read(path)["statusLine"] == CUSTOM

    def test_two_config_dirs_keep_separate_previous_values(self, env, tmp_path, monkeypatch):
        # Fix 1: install A, install B, uninstall A, uninstall B restores each.
        _, root = env
        dirs = {}
        for name in ("A", "B"):
            d = tmp_path / name
            d.mkdir()
            _write(d / "settings.json", {"statusLine": {"type": "command", "command": f"{name}.sh"}})
            dirs[name] = d
        for name in ("A", "B", "A", "B"):
            monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(dirs[name]))
            if ci.statusline_installed():
                ci.uninstall_statusline(root)
            else:
                ci.install_statusline(root)
        for name in ("A", "B"):
            assert _read(dirs[name] / "settings.json")["statusLine"]["command"] == f"{name}.sh"
        assert "claude" not in _read(root / "settings.json")

    def test_corrupt_ccswap_settings_blocks_uninstall_before_any_change(self, env):
        # Fix 2: the strict read fails before Claude's file is touched.
        path, root = env
        _write(path, {"statusLine": CUSTOM})
        ci.install_statusline(root)
        (root / "settings.json").write_text("{oops", encoding="utf-8")
        before = path.read_text(encoding="utf-8")
        with pytest.raises(ConfigError, match="not valid JSON"):
            ci.uninstall_statusline(root)
        assert path.read_text(encoding="utf-8") == before

    def test_explicit_null_statusline_round_trips(self, env):
        # Fix 3: a saved null is restored as null, not removed.
        path, root = env
        _write(path, {"statusLine": None})
        ci.install_statusline(root)
        ci.uninstall_statusline(root)
        assert _read(path) == {"statusLine": None}

    def test_uninstall_leaves_foreign_statusline_alone(self, env):
        path, root = env
        _write(path, {"statusLine": CUSTOM})
        before = path.read_text(encoding="utf-8")
        ci.uninstall_statusline(root)
        assert path.read_text(encoding="utf-8") == before

    def test_manual_removal_reads_as_off(self, env):
        path, root = env
        ci.install_statusline(root)
        _write(path, {})
        with patch.object(ci, "mod_installed", return_value=False):
            rows = {spec.dotted: value for spec, value, _ in effective_settings(root)
                    if spec.dotted in ACTION_KEYS}
        assert rows == {"claude.statusline": False, "claude.mod": False}


def _done(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


class TestMod:
    def test_install_argv(self):
        with patch("shutil.which", return_value="/bin/claude"), \
             patch("subprocess.run", return_value=_done()) as run:
            msg = ci.install_mod()
        argvs = [c.args[0] for c in run.call_args_list]
        assert argvs == [
            ["/bin/claude", "plugin", "list", "--json"],
            ["/bin/claude", "plugin", "marketplace", "add", "errhythm/ccswap"],
            ["/bin/claude", "plugin", "install", "ccswap@ccswap", "--scope", "user"],
        ]
        assert all(c.kwargs["timeout"] for c in run.call_args_list)
        assert "/reload-plugins" in msg

    def test_marketplace_add_failure_is_not_swallowed(self):
        # Fix 5: a repeat add exits 0, so a nonzero exit mentioning "already"
        # is a real error (e.g. a different source under the same name).
        results = [
            _done(stdout="[]"),
            _done(1, stderr="Marketplace 'ccswap' already exists with a different source"),
        ]
        with patch("shutil.which", return_value="/bin/claude"), \
             patch("subprocess.run", side_effect=results) as run:
            with pytest.raises(ConfigError, match="different source"):
                ci.install_mod()
        assert run.call_count == 2

    def test_install_enables_a_disabled_user_install(self):
        listing = json.dumps([{"id": "ccswap@ccswap", "scope": "user", "enabled": False}])
        with patch("shutil.which", return_value="/bin/claude"), \
             patch("subprocess.run", side_effect=[_done(stdout=listing), _done()]) as run:
            ci.install_mod()
        assert run.call_args.args[0] == [
            "/bin/claude", "plugin", "enable", "ccswap@ccswap", "--scope", "user",
        ]

    def test_install_failure_raises(self):
        with patch("shutil.which", return_value="/bin/claude"), \
             patch("subprocess.run",
                   side_effect=[_done(stdout="[]"), _done(), _done(1, stderr="boom")]):
            with pytest.raises(ConfigError, match="boom"):
                ci.install_mod()

    def test_uninstall_argv(self):
        with patch("shutil.which", return_value="/bin/claude"), \
             patch("subprocess.run", return_value=_done()) as run:
            ci.uninstall_mod()
        assert run.call_args.args[0] == [
            "/bin/claude", "plugin", "uninstall", "ccswap@ccswap", "--scope", "user",
        ]

    def test_claude_missing(self):
        with patch("shutil.which", return_value=None), \
             patch("subprocess.run") as run:
            with pytest.raises(ConfigError, match="Claude Code CLI not found on PATH"):
                ci.install_mod()
            with pytest.raises(ConfigError, match="not found"):
                ci.uninstall_mod()
            assert ci.mod_installed() is False
        run.assert_not_called()

    def test_mod_installed_reads_plugin_list_json(self):
        listing = json.dumps([
            {"id": "other@x", "scope": "user", "enabled": True},
            {"id": "ccswap@ccswap", "scope": "user", "enabled": True},
        ])
        with patch("shutil.which", return_value="/bin/claude"), \
             patch("subprocess.run", return_value=_done(stdout=listing)) as run:
            assert ci.mod_installed() is True
        assert run.call_args.args[0] == ["/bin/claude", "plugin", "list", "--json"]
        with patch("shutil.which", return_value="/bin/claude"), \
             patch("subprocess.run", return_value=_done(stdout="[]")):
            assert ci.mod_installed() is False

    @pytest.mark.parametrize("entry", [
        {"id": "ccswap@ccswap", "scope": "project", "enabled": True},
        {"id": "ccswap@ccswap", "scope": "user", "enabled": False},
    ])
    def test_mod_installed_only_counts_enabled_user_scope(self, entry):
        # Fix 4: project scope isn't ours to manage; a disabled install is off.
        with patch("shutil.which", return_value="/bin/claude"), \
             patch("subprocess.run", return_value=_done(stdout=json.dumps([entry]))):
            assert ci.mod_installed() is False


class TestConfigSurface:
    def _config(self, argv, capsys):
        with patch("os.geteuid", return_value=1000, create=True), \
             patch.object(sys, "argv", ["ccswap", "config", *argv]):
            code = 0
            try:
                cli.main()
            except SystemExit as e:
                code = e.code or 0
        return code, capsys.readouterr()

    def test_config_set_statusline_on_routes_to_install(self, temp_home, capsys):
        with patch.object(ci, "install_statusline", return_value="installed!") as inst:
            code, out = self._config(["set", "claude.statusline", "on"], capsys)
        assert code == 0
        inst.assert_called_once()
        assert "installed!" in out.out

    def test_config_set_mod_off_and_unset_route_to_uninstall(self, temp_home, capsys):
        with patch.object(ci, "uninstall_mod", return_value="gone") as un:
            assert self._config(["set", "claude.mod", "false"], capsys)[0] == 0
            assert self._config(["unset", "claude.mod"], capsys)[0] == 0
        assert un.call_count == 2

    def test_set_setting_never_stores_action_keys(self, tmp_path):
        with patch.object(ci, "install_mod", return_value="ok") as inst:
            assert set_setting(tmp_path, "claude.mod", "on") is True
        inst.assert_called_once()
        assert not (tmp_path / "settings.json").exists()

    def test_config_set_error_exits_nonzero(self, temp_home, capsys):
        with patch("shutil.which", return_value=None):
            code, out = self._config(["set", "claude.mod", "on"], capsys)
        assert code == 1
        assert "not found on PATH" in out.err + out.out


@pytest.mark.asyncio
class TestTuiToggle:
    async def test_settings_rows_toggle_off_thread(self, tmp_path):
        from textual.widgets import ListView, Static

        from ccswap.tui.widgets import MenuItem
        from tests.test_tui import (
            FakeSwitcher, make_account, make_app, menu_select, setting_values, settle,
        )

        state = {"claude.statusline": False, "claude.mod": False}
        calls = []

        def fake_set(root, key, on):
            calls.append((root, key, on))
            state[key] = on
            return f"{key} -> {on}"

        fake = FakeSwitcher([make_account(1, active=True)], tmp_path)
        app = make_app(fake)
        with patch.object(ci, "is_enabled", side_effect=state.__getitem__), \
             patch.object(ci, "set_enabled", side_effect=fake_set):
            async with app.run_test() as pilot:
                await settle(pilot)
                await menu_select(pilot, "settings-menu")
                await settle(pilot)

                def labels():
                    return setting_values(app.screen)

                assert labels()["claude:claude.statusline"] == "off"
                assert labels()["claude:claude.mod"] == "off"
                await menu_select(pilot, "claude:claude.mod")
                await settle(pilot)
                assert calls == [(tmp_path, "claude.mod", True)]
                assert labels()["claude:claude.mod"] == "on"
