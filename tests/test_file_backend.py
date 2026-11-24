"""``credentials.backend = file``: an explicit, user-declared file backend on macOS.

Over SSH (or any non-GUI session) the login Keychain is locked, and Claude Code
falls back to the plaintext ``~/.claude/.credentials.json``. ccswap's default
``auto`` backend infers Keychain usability from failed ``security`` calls and
then — correctly, for a machine that ALSO runs Claude Code from a GUI session —
treats the plaintext file as possibly superseded (``degraded``), refusing to
capture or consume it. On a machine where Claude Code only ever runs headless,
that file IS the authority, and the user can say so: ``file`` makes macOS behave
like Linux for credential storage — the Keychain is never consulted, the
plaintext file is authoritative, and backups live in ``.enc`` files.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ccswap import macos_keychain
from ccswap.credentials import ActiveCredentials, CLAUDE_CODE_KEYCHAIN_SERVICE
from ccswap.exceptions import ConfigError
from ccswap.macos_keychain import KeychainError
from ccswap.models import Platform
from ccswap.paths import get_backup_root, get_credentials_path
from ccswap.settings import SETTING_SPECS, set_setting
from ccswap.switcher import ClaudeAccountSwitcher


ACTIVE_CREDS = json.dumps({
    "claudeAiOauth": {
        "accessToken": "sk-active",
        "refreshToken": "rt-active",
        "expiresAt": 9_999_999_999_000,
        "scopes": ["user:inference", "user:profile"],
    }
})


class _LockedKeychain:
    """Every secret-touching ``security`` op fails as on a locked SSH keychain,
    and every call is recorded so tests can assert the Keychain was never asked."""

    def __init__(self):
        self.calls: list[str] = []

    def _fail(self, name):
        def op(*args, **kwargs):
            self.calls.append(name)
            raise KeychainError("User interaction is not allowed.")
        return op


@pytest.fixture
def locked_keychain(monkeypatch, block_real_keychain):
    lk = _LockedKeychain()
    for name in ("get_password", "set_password", "delete_password"):
        monkeypatch.setattr(macos_keychain, name, lk._fail(name))
    monkeypatch.setattr("ccswap.credentials._ACTIVE_READ_RETRY_DELAY", 0)
    return lk


def _use_file_backend() -> None:
    set_setting(get_backup_root(), "credentials.backend", "file")


def _macos_switcher() -> ClaudeAccountSwitcher:
    s = ClaudeAccountSwitcher()
    s.platform = Platform.MACOS
    s._setup_directories()
    return s


def _write_active_file(text: str = ACTIVE_CREDS) -> Path:
    cred = get_credentials_path()
    cred.parent.mkdir(parents=True, exist_ok=True)
    cred.write_text(text)
    return cred


# --------------------------------------------------------------------------
# The setting itself
# --------------------------------------------------------------------------


def load_credential_settings(root: Path):
    from ccswap.settings import load_credential_settings as load

    return load(root)


class TestCredentialBackendSetting:
    def test_spec_is_registered_with_auto_default(self):
        spec = SETTING_SPECS["credentials.backend"]
        assert spec.kind == "choice"
        assert spec.choices == ("auto", "file")
        assert spec.default == "auto"

    def test_missing_file_loads_auto(self, temp_home: Path):
        assert load_credential_settings(get_backup_root()).backend == "auto"

    def test_set_file_round_trips(self, temp_home: Path):
        _use_file_backend()
        assert load_credential_settings(get_backup_root()).backend == "file"
        raw = json.loads((get_backup_root() / "settings.json").read_text())
        assert raw["credentials"]["backend"] == "file"

    def test_set_rejects_unknown_value(self, temp_home: Path):
        with pytest.raises(ConfigError):
            set_setting(get_backup_root(), "credentials.backend", "keychain-only")

    def test_corrupt_value_on_disk_loads_auto(self, temp_home: Path):
        root = get_backup_root()
        root.mkdir(parents=True, exist_ok=True)
        (root / "settings.json").write_text(
            json.dumps({"schemaVersion": 1, "credentials": {"backend": "nonsense"}})
        )
        assert load_credential_settings(root).backend == "auto"

    def test_switcher_exposes_loaded_backend(self, temp_home: Path):
        _use_file_backend()
        assert ClaudeAccountSwitcher().credential_backend == "file"


# --------------------------------------------------------------------------
# File backend on macOS: the Keychain is never consulted
# --------------------------------------------------------------------------


class TestFileBackendOnMacOS:
    def test_active_read_is_authoritative_not_degraded(
        self, temp_home: Path, locked_keychain
    ):
        _use_file_backend()
        s = _macos_switcher()
        _write_active_file()

        got = s._read_active_credentials()

        assert got.value == ACTIVE_CREDS
        assert got.degraded is False
        assert got.keychain_unavailable is False
        assert locked_keychain.calls == []

    def test_store_reports_no_keychain(self, temp_home: Path, locked_keychain):
        _use_file_backend()
        s = _macos_switcher()
        assert s._store._use_keychain() is False
        assert s._store._keychain_unreadable is False
        assert s._store._uses_file_backup_backend() is True

    def test_empty_active_store_reads_as_empty_not_unavailable(
        self, temp_home: Path, locked_keychain
    ):
        _use_file_backend()
        s = _macos_switcher()
        got = s._read_active_credentials()
        assert got.value == ""
        assert got.keychain_unavailable is False
        assert got.degraded is False

    def test_capture_guard_passes(self, temp_home: Path, locked_keychain):
        _use_file_backend()
        s = _macos_switcher()
        _write_active_file()
        assert s._refuse_degraded_capture() == ACTIVE_CREDS
        assert locked_keychain.calls == []

    def test_active_write_goes_to_file_only(self, temp_home: Path, locked_keychain):
        _use_file_backend()
        s = _macos_switcher()

        s._write_credentials(ACTIVE_CREDS)

        assert get_credentials_path().read_text() == ACTIVE_CREDS
        assert locked_keychain.calls == []
        assert s._last_active_credentials_backend == "file"

    def test_backup_round_trip_uses_enc_files_only(
        self, temp_home: Path, locked_keychain
    ):
        _use_file_backend()
        s = _macos_switcher()

        s._write_account_credentials("1", "a@example.com", ACTIVE_CREDS)

        assert s._read_account_credentials("1", "a@example.com") == ACTIVE_CREDS
        assert list(s.credentials_dir.glob("*.enc")), "backup must land in an .enc file"
        assert locked_keychain.calls == []

    def test_add_account_succeeds_with_locked_keychain(
        self, temp_home: Path, mock_claude_config: Path, locked_keychain
    ):
        _use_file_backend()
        s = _macos_switcher()
        _write_active_file()

        s.add_account()

        data = s._get_sequence_data()
        assert data["accounts"]["1"]["email"] == "test@example.com"
        assert s._read_account_credentials("1", "test@example.com") == ACTIVE_CREDS
        assert locked_keychain.calls == []


# --------------------------------------------------------------------------
# Residual Keychain item warning
# --------------------------------------------------------------------------


class TestResidualKeychainItemWarning:
    """Claude Code reads the Keychain BEFORE the plaintext file. A leftover
    ``Claude Code-credentials`` item is harmless while the Keychain stays locked,
    but would shadow ccswap's file the moment Claude Code runs where it is
    unlocked (a GUI session). The attribute-only existence probe never decrypts,
    so it works on a locked Keychain; status must surface it."""

    def _seed_residual(self, block_real_keychain) -> None:
        acct = macos_keychain.keychain_account_name()
        block_real_keychain.data[(CLAUDE_CODE_KEYCHAIN_SERVICE, acct)] = "stale"

    def test_status_warns_about_residual_item(
        self, temp_home: Path, mock_claude_config: Path, block_real_keychain,
        locked_keychain, capsys,
    ):
        self._seed_residual(block_real_keychain)
        _use_file_backend()
        s = _macos_switcher()
        _write_active_file()

        s.status()

        captured = capsys.readouterr()
        assert CLAUDE_CODE_KEYCHAIN_SERVICE in captured.out + captured.err

    def test_status_json_stays_clean(
        self, temp_home: Path, mock_claude_config: Path, block_real_keychain,
        locked_keychain, capsys,
    ):
        self._seed_residual(block_real_keychain)
        _use_file_backend()
        s = _macos_switcher()
        _write_active_file()

        s.status(json_output=True)

        assert CLAUDE_CODE_KEYCHAIN_SERVICE not in capsys.readouterr().out

    def test_no_warning_without_residual(
        self, temp_home: Path, mock_claude_config: Path, locked_keychain, capsys,
    ):
        _use_file_backend()
        s = _macos_switcher()
        _write_active_file()

        s.status()

        captured = capsys.readouterr()
        assert CLAUDE_CODE_KEYCHAIN_SERVICE not in captured.out + captured.err

    def test_no_warning_under_auto_backend(
        self, temp_home: Path, mock_claude_config: Path, block_real_keychain, capsys,
    ):
        self._seed_residual(block_real_keychain)
        s = _macos_switcher()

        s.status()

        captured = capsys.readouterr()
        assert "credentials.backend" not in captured.out + captured.err


# --------------------------------------------------------------------------
# Unchanged behaviour
# --------------------------------------------------------------------------


class TestDefaultsUnchanged:
    def test_auto_backend_still_flags_locked_keychain_as_degraded(
        self, temp_home: Path, locked_keychain
    ):
        s = _macos_switcher()
        _write_active_file()
        got = s._read_active_credentials()
        assert got.value == ACTIVE_CREDS
        assert got.degraded is True

    def test_auto_backend_still_refuses_capture(self, temp_home: Path, locked_keychain):
        from ccswap.exceptions import CredentialReadError

        s = _macos_switcher()
        _write_active_file()
        with pytest.raises(CredentialReadError):
            s._refuse_degraded_capture()

    def test_file_backend_is_a_no_op_off_macos(self, temp_home: Path):
        _use_file_backend()
        s = ClaudeAccountSwitcher()
        s.platform = Platform.LINUX
        s._setup_directories()
        _write_active_file()
        got = s._read_active_credentials()
        assert got == ActiveCredentials(ACTIVE_CREDS, False, False)


class TestLegacyKeychainMigration:
    def test_file_backend_leaves_keychain_migration_pending(
        self, temp_home: Path, locked_keychain
    ):
        """The legacy keyring → security migration reads and writes Keychain
        backups directly, outside CredentialStore's routing. Explicit file mode
        must skip it without marking it done, so it still runs under ``auto``."""
        from ccswap.migrations import migrate_macos_keyring_to_security

        _use_file_backend()
        s = _macos_switcher()
        s._init_sequence_file()

        assert migrate_macos_keyring_to_security(s) is False
        assert locked_keychain.calls == []


# --------------------------------------------------------------------------
# Round 2 (adversarial review): switching INTO file mode must not strand
# Keychain-only backups, and the warnings cover every residual Claude Code item.
# --------------------------------------------------------------------------

import sys
from unittest.mock import patch

from ccswap import cli
from ccswap.credentials import CLAUDE_CODE_MANAGED_KEYCHAIN_SERVICE


def _config(argv: list[str], capsys) -> tuple[int, str, str]:
    """Run `ccswap config <argv>` as the CLI does; returns (code, out, err)."""
    with patch("ccswap.claude_integration.mod_installed", return_value=False), \
         patch("os.geteuid", return_value=1000, create=True), \
         patch.object(sys, "argv", ["claude-swap", "config", *argv]):
        code = 0
        try:
            cli.main()
        except SystemExit as e:
            code = e.code or 0
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _seed_accounts(emails: list[str], with_enc: set[str]) -> None:
    """Managed accounts in sequence.json; only ``with_enc`` get an .enc backup
    (the rest exist only in the Keychain, as under the ``auto`` backend)."""
    s = _macos_switcher()
    s._init_sequence_file()
    data = s._get_sequence_data()
    for i, email in enumerate(emails, start=1):
        data["accounts"][str(i)] = {
            "email": email, "uuid": f"uuid-{i}", "added": "2024-01-01T00:00:00Z",
        }
        data["sequence"].append(i)
        if email in with_enc:
            s._store._write_backup_enc(str(i), email, ACTIVE_CREDS)
    s._write_json(s.sequence_file, data)


def _backend_on_disk() -> str | None:
    path = get_backup_root() / "settings.json"
    if not path.exists():
        return None
    return json.loads(path.read_text()).get("credentials", {}).get("backend")


@pytest.fixture
def on_macos():
    with patch.object(Platform, "detect", return_value=Platform.MACOS):
        yield


class TestSwitchingIntoFileMode:
    def test_refuses_when_an_account_has_no_file_backup(
        self, temp_home: Path, on_macos, capsys
    ):
        _seed_accounts(["a@example.com", "b@example.com"], with_enc={"a@example.com"})

        code, out, err = _config(["set", "credentials.backend", "file"], capsys)

        assert code != 0
        assert _backend_on_disk() is None, "a refused switch must not be persisted"
        text = out + err
        assert "b@example.com" in text           # names the stranded account
        assert "a@example.com" not in text       # ...and only that one
        assert "export" in text and "import" in text   # recovery recipe

    def test_allows_when_every_account_has_a_file_backup(
        self, temp_home: Path, on_macos, capsys
    ):
        _seed_accounts(["a@example.com"], with_enc={"a@example.com"})
        code, _, _ = _config(["set", "credentials.backend", "file"], capsys)
        assert code == 0
        assert _backend_on_disk() == "file"

    def test_allows_with_no_managed_accounts(self, temp_home: Path, on_macos, capsys):
        code, _, _ = _config(["set", "credentials.backend", "file"], capsys)
        assert code == 0
        assert _backend_on_disk() == "file"

    def test_setting_auto_is_never_refused(self, temp_home: Path, on_macos, capsys):
        _seed_accounts(["a@example.com"], with_enc=set())
        code, _, _ = _config(["set", "credentials.backend", "auto"], capsys)
        assert code == 0
        assert _backend_on_disk() == "auto"

    def test_no_check_off_macos(self, temp_home: Path, capsys):
        _seed_accounts(["a@example.com"], with_enc=set())
        with patch.object(Platform, "detect", return_value=Platform.LINUX):
            code, _, _ = _config(["set", "credentials.backend", "file"], capsys)
        assert code == 0

    def test_success_reminds_to_restart_long_running_instances(
        self, temp_home: Path, on_macos, capsys
    ):
        code, out, err = _config(["set", "credentials.backend", "file"], capsys)
        assert code == 0
        assert "restart" in (out + err).lower()

    def test_success_warns_about_residual_keychain_item(
        self, temp_home: Path, on_macos, block_real_keychain, capsys
    ):
        acct = macos_keychain.keychain_account_name()
        block_real_keychain.data[(CLAUDE_CODE_KEYCHAIN_SERVICE, acct)] = "stale"
        code, out, err = _config(["set", "credentials.backend", "file"], capsys)
        assert code == 0
        assert f'"{CLAUDE_CODE_KEYCHAIN_SERVICE}"' in out + err


class TestResidualWarningCoversManagedKeyItem:
    def test_status_names_the_managed_key_item(
        self, temp_home: Path, mock_claude_config: Path, block_real_keychain,
        locked_keychain, capsys,
    ):
        acct = macos_keychain.keychain_account_name()
        block_real_keychain.data[(CLAUDE_CODE_MANAGED_KEYCHAIN_SERVICE, acct)] = "k"
        _use_file_backend()
        s = _macos_switcher()
        _write_active_file()

        s.status()

        captured = capsys.readouterr()
        # Quoted, so "Claude Code" is not satisfied by "Claude Code-credentials".
        assert f'"{CLAUDE_CODE_MANAGED_KEYCHAIN_SERVICE}"' in captured.out + captured.err


class TestSwitchGuardRound3:
    """Round-2 review fixes: the guard decodes like the file-mode read, the
    documented recovery recipe actually completes, and unset reminds too."""

    def test_corrupt_enc_counts_as_missing(self, temp_home: Path, on_macos, capsys):
        _seed_accounts(["a@example.com"], with_enc=set())
        s = _macos_switcher()
        s._backup_enc_path("1", "a@example.com").write_text("!!!! not base64 !!!!")

        code, out, err = _config(["set", "credentials.backend", "file"], capsys)

        assert code != 0
        assert "a@example.com" in out + err
        assert _backend_on_disk() is None

    def test_unset_reminds_to_restart(self, temp_home: Path, on_macos, capsys):
        assert _config(["set", "credentials.backend", "file"], capsys)[0] == 0
        code, out, err = _config(["unset", "credentials.backend"], capsys)
        assert code == 0
        assert "restart" in (out + err).lower()

    def test_documented_recovery_recipe_completes(
        self, temp_home: Path, mock_claude_config: Path, on_macos,
        block_real_keychain, capsys,
    ):
        """auto mode with a Keychain-only backup → export → remove → set file →
        import must end with a usable .enc backup that file mode reads."""
        from ccswap.transfer import export_accounts, import_accounts

        email = "test@example.com"  # mock_claude_config's identity
        acct = macos_keychain.keychain_account_name()
        block_real_keychain.data[(CLAUDE_CODE_KEYCHAIN_SERVICE, acct)] = ACTIVE_CREDS
        s = _macos_switcher()
        s.add_account()
        assert not s._backup_enc_path("1", email).exists(), (
            "premise: under auto with a usable Keychain the backup is Keychain-only"
        )
        assert _config(["set", "credentials.backend", "file"], capsys)[0] != 0

        export_path = temp_home / "backup.cswap"
        export_accounts(s, str(export_path))
        s.remove_account(email, assume_yes=True)
        assert _config(["set", "credentials.backend", "file"], capsys)[0] == 0

        s2 = _macos_switcher()
        assert s2.credential_backend == "file"
        import_accounts(s2, str(export_path))

        slot = next(
            num for num, info in s2._get_sequence_data()["accounts"].items()
            if info["email"] == email
        )
        assert s2._backup_enc_path(slot, email).exists()
        assert s2._read_account_credentials(slot, email) == ACTIVE_CREDS


# --------------------------------------------------------------------------
# Review fixes: the live login, the way back to auto, hand-edited file mode,
# session mode, and profile-scoped Keychain names
# --------------------------------------------------------------------------


def _creds(refresh: str, expires_at: int) -> str:
    return json.dumps({
        "claudeAiOauth": {
            "accessToken": f"sk-{refresh}",
            "refreshToken": refresh,
            "expiresAt": expires_at,
            "scopes": ["user:inference", "user:profile"],
        }
    })


OLD = _creds("rt-old", 1_000)
NEW = _creds("rt-new", 2_000)


def _seed_keychain_login(block_real_keychain, creds: str) -> None:
    acct = macos_keychain.keychain_account_name()
    block_real_keychain.data[(CLAUDE_CODE_KEYCHAIN_SERVICE, acct)] = creds


class TestLiveLoginGuard:
    """Entering file mode: a readable Keychain with a NEWER login than the
    plaintext file means Claude Code rotates tokens in the Keychain here, so
    the file is an older generation a file-mode switch would back up."""

    def test_refused_when_keychain_login_is_newer(
        self, temp_home: Path, on_macos, block_real_keychain, capsys
    ):
        _seed_keychain_login(block_real_keychain, NEW)
        _write_active_file(OLD)
        code, out, err = _config(["set", "credentials.backend", "file"], capsys)
        assert code != 0
        assert _backend_on_disk() is None
        assert "security delete-generic-password" in out + err

    def test_allowed_when_file_is_newer_or_matches(
        self, temp_home: Path, on_macos, block_real_keychain, capsys
    ):
        for keychain in (OLD, NEW):
            _seed_keychain_login(block_real_keychain, keychain)
            _write_active_file(NEW)
            code, _, _ = _config(["set", "credentials.backend", "file"], capsys)
            assert code == 0
            assert _backend_on_disk() == "file"
            _config(["unset", "credentials.backend"], capsys)

    def test_allowed_when_keychain_is_locked(
        self, temp_home: Path, on_macos, block_real_keychain, locked_keychain, capsys
    ):
        _seed_keychain_login(block_real_keychain, NEW)
        _write_active_file(OLD)
        code, _, _ = _config(["set", "credentials.backend", "file"], capsys)
        assert code == 0
        assert _backend_on_disk() == "file"


class TestLeavingFileMode:
    def test_refused_when_file_is_newer_than_keychain(
        self, temp_home: Path, on_macos, block_real_keychain, capsys
    ):
        _use_file_backend()
        _seed_keychain_login(block_real_keychain, OLD)
        _write_active_file(NEW)
        for argv in (["set", "credentials.backend", "auto"], ["unset", "credentials.backend"]):
            code, out, err = _config(argv, capsys)
            assert code != 0
            assert _backend_on_disk() == "file"
            assert "security delete-generic-password" in out + err

    def test_allowed_when_keychain_is_newer(
        self, temp_home: Path, on_macos, block_real_keychain, capsys
    ):
        """settings.json hand-edited to file on a GUI Mac: nothing was written
        in file mode, the Keychain login is the newest, and auto should use it."""
        _use_file_backend()
        _seed_keychain_login(block_real_keychain, NEW)
        _write_active_file(OLD)
        code, _, _ = _config(["set", "credentials.backend", "auto"], capsys)
        assert code == 0
        assert _backend_on_disk() == "auto"

    @pytest.mark.parametrize("text", ["{}", "not json", "[1]"])
    def test_file_without_a_login_does_not_block(
        self, temp_home: Path, on_macos, block_real_keychain, capsys, text
    ):
        _use_file_backend()
        _seed_keychain_login(block_real_keychain, OLD)
        _write_active_file(text)
        code, _, _ = _config(["set", "credentials.backend", "auto"], capsys)
        assert code == 0
        assert _backend_on_disk() == "auto"

    def test_allowed_without_a_keychain_login(self, temp_home: Path, on_macos, capsys):
        _use_file_backend()
        _write_active_file(NEW)
        code, _, _ = _config(["set", "credentials.backend", "auto"], capsys)
        assert code == 0
        assert _backend_on_disk() == "auto"

    def test_locked_keychain_warns_but_allows(
        self, temp_home: Path, on_macos, block_real_keychain, locked_keychain, capsys
    ):
        _use_file_backend()
        _seed_keychain_login(block_real_keychain, OLD)
        _write_active_file(NEW)
        code, out, err = _config(["unset", "credentials.backend"], capsys)
        assert code == 0
        assert f'"{CLAUDE_CODE_KEYCHAIN_SERVICE}"' in out + err


class TestUnorderableLogins:
    """Two different logins with the same expiry can't be ordered."""

    def test_entering_refuses_and_leaving_warns(
        self, temp_home: Path, on_macos, block_real_keychain, capsys
    ):
        _seed_keychain_login(block_real_keychain, _creds("rt-a", 1_000))
        _write_active_file(_creds("rt-b", 1_000))
        code, _, _ = _config(["set", "credentials.backend", "file"], capsys)
        assert code != 0

        _use_file_backend()
        code, out, err = _config(["set", "credentials.backend", "auto"], capsys)
        assert code == 0
        assert f'"{CLAUDE_CODE_KEYCHAIN_SERVICE}"' in out + err

    def test_leaving_with_newer_keychain_still_warns(
        self, temp_home: Path, on_macos, block_real_keychain, capsys
    ):
        _use_file_backend()
        _seed_keychain_login(block_real_keychain, NEW)
        _write_active_file(OLD)
        code, out, err = _config(["set", "credentials.backend", "auto"], capsys)
        assert code == 0
        assert f'"{CLAUDE_CODE_KEYCHAIN_SERVICE}"' in out + err


class TestHandEditedFileMode:
    """settings.json edited by hand skips `config set`; a switch onto a
    Keychain-only account must still refuse, while `config set ... auto`
    stays the way back."""

    def test_switch_to_keychain_only_account_refuses(
        self, temp_home: Path, on_macos, capsys
    ):
        _seed_accounts(["a@example.com", "b@example.com"], with_enc={"a@example.com"})
        _use_file_backend()
        s = _macos_switcher()
        with pytest.raises(ConfigError, match="b@example.com"):
            s._perform_switch("2")

        code, _, _ = _config(["set", "credentials.backend", "auto"], capsys)
        assert code == 0
        assert _backend_on_disk() == "auto"

    def test_a_bad_slot_does_not_block_switching_to_a_healthy_one(
        self, temp_home: Path, on_macos
    ):
        _seed_accounts(["a@example.com", "b@example.com"], with_enc={"a@example.com"})
        _use_file_backend()
        s = _macos_switcher()
        s._require_file_backup("1")  # no raise: only the target is checked


class TestCaptureWithCustomProfile:
    """`ccswap add` under CLAUDE_CONFIG_DIR reads the profile's credentials
    through session.read_config_dir_credentials, which must honor file mode."""

    def _work_profile(self, temp_home: Path, monkeypatch) -> Path:
        work = temp_home / "work"
        work.mkdir()
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(work))
        monkeypatch.delenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", raising=False)
        (work / ".claude.json").write_text(json.dumps({
            "oauthAccount": {"emailAddress": "w@example.com", "accountUuid": "u1"},
        }))
        (work / ".credentials.json").write_text(ACTIVE_CREDS)
        return work

    def test_add_never_asks_a_locked_keychain(
        self, temp_home: Path, on_macos, locked_keychain, monkeypatch
    ):
        self._work_profile(temp_home, monkeypatch)
        _use_file_backend()
        s = _macos_switcher()
        s.add_account()
        assert locked_keychain.calls == []
        assert s._read_account_credentials("1", "w@example.com") == ACTIVE_CREDS

    def test_add_ignores_a_stale_keychain_item(
        self, temp_home: Path, on_macos, block_real_keychain, monkeypatch
    ):
        from ccswap.session import keychain_service_name

        work = self._work_profile(temp_home, monkeypatch)
        acct = macos_keychain.keychain_account_name()
        block_real_keychain.data[(keychain_service_name(str(work)), acct)] = OLD
        _use_file_backend()
        s = _macos_switcher()
        s.add_account()
        assert s._read_account_credentials("1", "w@example.com") == ACTIVE_CREDS


class TestNonObjectKeychainValue:
    def test_config_set_does_not_crash(
        self, temp_home: Path, on_macos, block_real_keychain, capsys
    ):
        _seed_keychain_login(block_real_keychain, "[1]")
        _write_active_file(ACTIVE_CREDS)
        code, out, err = _config(["set", "credentials.backend", "file"], capsys)
        assert code != 0  # can't compare: refused, not crashed
        assert "Traceback" not in out + err
        assert "Cannot set credentials.backend to file" in out + err


class TestSessionModeRefused:
    def test_run_refuses_in_file_mode(self, temp_home: Path, on_macos, capsys):
        _seed_accounts(["a@example.com"], with_enc={"a@example.com"})
        _use_file_backend()
        with patch("os.geteuid", return_value=1000, create=True), \
             patch("ccswap.session.SessionManager.run") as run:
            with pytest.raises(SystemExit) as exc:
                cli._run_command(["2"])
        assert exc.value.code == 1
        run.assert_not_called()
        assert "session mode" in capsys.readouterr().err


class TestResidualUsesProfileServiceName:
    def test_hashed_item_is_reported_under_custom_config_dir(
        self, temp_home: Path, mock_claude_config: Path, block_real_keychain,
        locked_keychain, monkeypatch, capsys,
    ):
        from ccswap.session import keychain_service_name

        config_dir = str(temp_home / "work-profile")
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", config_dir)
        monkeypatch.delenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", raising=False)
        hashed = keychain_service_name(config_dir)
        acct = macos_keychain.keychain_account_name()
        block_real_keychain.data[(hashed, acct)] = "stale"
        _use_file_backend()
        s = _macos_switcher()

        assert hashed in s._residual_keychain_items()
