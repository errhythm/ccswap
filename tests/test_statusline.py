"""``ccswap statusline``: local-only rendering, cache fallback, never crashes."""

import io
import json
import re
import time
from unittest.mock import patch

import pytest

from ccswap import statusline

ANSI = re.compile(r"\x1b\[[0-9;]*m")
NOW = time.time()
SAMPLE = {
    "model": {"display_name": "Opus"},
    "cwd": "/nonexistent-dir",
    "context_window": {
        "context_window_size": 200000,
        "current_usage": {
            "input_tokens": 1000,
            "cache_creation_input_tokens": 20000,
            "cache_read_input_tokens": 60000,
        },
    },
    "cost": {"total_duration_ms": 4980000},
    "effort": {"level": "high"},
    "rate_limits": {
        "five_hour": {"used_percentage": 23.5, "resets_at": NOW + 3600},
        "seven_day": {"used_percentage": 95, "resets_at": NOW + 86400},
    },
}


@pytest.fixture
def backup(tmp_path, monkeypatch):
    """A backup root with two accounts, #2 active, usage cached for both."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    ident = {"organizationUuid": "o"}
    (tmp_path / "sequence.json").write_text(json.dumps({
        "activeAccountNumber": 2,
        "sequence": [1, 2],
        "accounts": {
            "1": {"email": "a@x.io", **ident},
            "2": {"email": "b@x.io", **ident},
        },
    }))
    (tmp_path / "cache").mkdir()
    row = lambda email, five, seven: {  # noqa: E731
        "email": email, "organizationUuid": "o", "fetchedAt": NOW,
        "lastGood": {
            "five_hour": {"pct": five, "resets_at": "2030-01-01T00:00:00+00:00"},
            "seven_day": {"pct": seven, "resets_at": "2030-01-02T00:00:00+00:00"},
            "spend": {"used": 5.0, "limit": 20.0, "pct": 25.0, "currency": "USD"},
        },
    }
    (tmp_path / "cache" / "usage.json").write_text(json.dumps({
        "schemaVersion": 2,
        "accounts": {"1": row("a@x.io", 12, 8), "2": row("b@x.io", 40, 60)},
    }))
    return tmp_path


def plain(s):
    return ANSI.sub("", s)


def test_render_dark_with_rate_limits(backup):
    out = statusline.render(SAMPLE, statusline.DARK, backup)
    text = plain(out)
    assert "Opus" in text and "40%" in text and "1h23m" in text and "high" in text
    assert "#2 b@x.io" in text and "next: #1 12%" in text
    assert re.search(r"5h .*  24%", text) and re.search(r"7d .*  95%", text)
    assert "38;2;215;135;95mOpus" in out  # dark accent on the model
    assert "38;2;215;95;95m" in out  # 95% weekly is critical
    assert "$5.00/$20.00" in text  # extra usage comes from the cache


def test_mask_hides_the_active_email(backup):
    text = plain(statusline.render(SAMPLE, statusline.DARK, backup, mask=True))
    assert "b@x.io" not in text
    assert "#2 •@•.io" in text


def test_render_light_uses_light_palette(backup):
    out = statusline.render(SAMPLE, statusline.LIGHT, backup)
    assert "38;2;149;76;42mOpus" in out and "215;135;95" not in out


def test_falls_back_to_cache_without_rate_limits(backup):
    data = {k: v for k, v in SAMPLE.items() if k != "rate_limits"}
    text = plain(statusline.render(data, statusline.DARK, backup))
    assert re.search(r"5h .*  40%", text) and re.search(r"7d .*  60%", text)


def test_no_ccswap_state_still_renders(tmp_path):
    text = plain(statusline.render({"model": {"display_name": "Opus"}}, statusline.DARK, tmp_path))
    assert text.startswith("Opus") and "#" not in text


@pytest.mark.parametrize("stdin", ["", "   ", "not json", "[1, 2]", '{"model": 5}'])
def test_garbage_stdin_exits_zero(stdin, capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    assert statusline.main() == 0
    assert plain(capsys.readouterr().out) == "Claude"


def test_render_error_falls_back_to_model_name(capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO('{"model": {"display_name": "Opus"}}'))
    with patch.object(statusline, "render", side_effect=RuntimeError):
        assert statusline.main() == 0
    assert capsys.readouterr().out == "Opus"


def test_never_touches_network_or_keychain(backup, capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(SAMPLE)))
    boom = AssertionError("statusline must be local-only")
    with (
        patch("urllib.request.urlopen", side_effect=boom),
        patch("ccswap.oauth.request_usage_data", side_effect=boom),
        patch("socket.socket.connect", side_effect=boom),
        patch("ccswap.paths.get_backup_root", return_value=backup),
    ):
        assert statusline.main() == 0
    assert "Opus" in plain(capsys.readouterr().out)


def test_clock_has_no_posix_only_strftime_flags():
    import inspect

    assert "%-" not in inspect.getsource(statusline._clock)
    t = time.mktime((2030, 1, 5, 0, 7, 0, 0, 0, -1))
    assert statusline._clock(t, True) == "jan 5, 12:07am"
    assert statusline._clock(t + 13 * 3600, False) == "1:07pm"


def test_spend_prefers_stdin_spend_limit(backup):
    data = {**SAMPLE, "rate_limits": {**SAMPLE["rate_limits"], "spend_limit": {
        "used_percentage": 50, "resets_at": NOW + 86400, "used_usd": 7.5, "limit_usd": 15,
    }}}
    assert "$7.50/$15.00" in plain(statusline.render(data, statusline.DARK, backup))


def test_bad_spend_numbers_only_touch_that_line(backup):
    row = json.loads((backup / "cache" / "usage.json").read_text())
    row["accounts"]["2"]["lastGood"]["spend"] = {"used": "x", "limit": None, "pct": 1}
    (backup / "cache" / "usage.json").write_text(json.dumps(row))
    text = plain(statusline.render(SAMPLE, statusline.DARK, backup))
    assert "$" not in text and "7d" in text

    row["accounts"]["2"]["lastGood"]["spend"] = {"used": 1, "limit": 2, "pct": "x"}
    (backup / "cache" / "usage.json").write_text(json.dumps(row))
    text = plain(statusline.render(SAMPLE, statusline.DARK, backup))
    assert "xtra" not in text and "7d" in text


def test_spend_without_amounts_shows_percentage():
    from ccswap.statusline import DARK, _spend_line

    line = _spend_line({"used_percentage": 40, "resets_at": None}, None, DARK)
    assert line is not None and "40%" in line
