"""``ccswap statusline``: a Claude Code ``statusLine`` command in ccswap's theme.

Claude Code pipes a JSON session snapshot to stdin and prints whatever we
write. Everything here is a local file read: no network, no keychain, no
account switcher. The ccswap half (active account, best other account, usage
fallback) comes from ``sequence.json`` + the ``cache/usage.json`` table the
collectors already keep warm.

Fields used (https://code.claude.com/docs/en/statusline): ``model.display_name``,
``workspace.current_dir`` / ``cwd``, ``context_window.{context_window_size,
current_usage}``, ``cost.total_duration_ms``, ``effort.level``,
``rate_limits.{five_hour,seven_day}.{used_percentage,resets_at}``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ccswap import paths
from ccswap.settings import load_ui_settings
from ccswap.tui.data import present_alias, present_email
from ccswap.usage_store import UsageStore

# Palette: tui/theme.py (hex constants copied; that module imports textual,
# which a statusline must never pay for). Keep in sync with it.
WARN_PCT = 70.0
CRIT_PCT = 90.0


@dataclass(frozen=True)
class _Palette:
    accent: str
    fg: str
    muted: str
    ok: str
    warn: str
    crit: str
    track: str

    def severity(self, pct: float) -> str:
        if pct >= CRIT_PCT:
            return self.crit
        if pct >= WARN_PCT:
            return self.warn
        return self.ok


DARK = _Palette("#d7875f", "#e8e4de", "#8a8a8a", "#87af87", "#d7af5f", "#d75f5f", "#3a3a3a")
LIGHT = _Palette("#954c2a", "#2b2723", "#635d55", "#3d6b3d", "#795911", "#ad3128", "#cec7ba")

# Bar glyphs: tui/widgets.py (_BAR_FILLED / _BAR_HALF / _BAR_EMPTY).
_FILLED, _HALF, _EMPTY = "━", "╸", "─"
_BAR_WIDTH = 10
_RESET = "\033[0m"


def _c(hex_color: str, text: str) -> str:
    r, g, b = (int(hex_color[i : i + 2], 16) for i in (1, 3, 5))
    return f"\033[38;2;{r};{g};{b}m{text}{_RESET}"


def _bar(pct: float, p: _Palette) -> str:
    cells = min(max(pct, 0.0), 100.0) / 100.0 * _BAR_WIDTH
    full = int(cells)
    half = (cells - full) >= 0.5 and full < _BAR_WIDTH
    color = p.severity(pct)
    out = _c(color, _FILLED * full + (_HALF if half else ""))
    rest = _BAR_WIDTH - full - (1 if half else 0)
    return out + _c(p.track, _EMPTY * rest)


def _num(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _epoch(value: object) -> float | None:
    """Unix seconds from an epoch number or an ISO-8601 string."""
    n = _num(value)
    if n is not None:
        return n or None
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def _clock(epoch: float, with_date: bool) -> str:
    # Hand-formatted: the strftime no-padding flags are POSIX-only (ValueError on Windows).
    dt = datetime.fromtimestamp(epoch)
    text = f"{dt.hour % 12 or 12}:{dt.minute:02d}{'am' if dt.hour < 12 else 'pm'}"
    return f"{dt.strftime('%b').lower()} {dt.day}, {text}" if with_date else text


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _git(cwd: str) -> tuple[str, bool] | None:
    """``(branch, dirty)`` from one short-timeout ``git status``; None on any failure."""
    try:
        out = subprocess.run(
            ["git", "-C", cwd, "--no-optional-locks", "status", "--porcelain=v1", "-b"],
            capture_output=True, text=True, timeout=1.0, check=True,
        ).stdout.splitlines()
    except (OSError, subprocess.SubprocessError):
        return None
    if not out or not out[0].startswith("## "):
        return None
    branch = out[0][3:].split("...")[0]
    if branch.startswith("No commits yet on "):
        branch = branch[len("No commits yet on "):]
    elif branch.startswith("HEAD (no branch)"):
        branch = "HEAD"
    return branch, len(out) > 1


def _duration(ms: object) -> str | None:
    n = _num(ms)
    if n is None or n <= 0:
        return None
    s = int(n // 1000)
    if s >= 3600:
        return f"{s // 3600}h{s % 3600 // 60}m"
    return f"{s // 60}m" if s >= 60 else f"{s}s"


def _effort(data: dict) -> str:
    level = (data.get("effort") or {}).get("level")
    if not isinstance(level, str) or not level:
        settings = _read_json(paths.get_claude_config_home() / "settings.json")
        level = settings.get("effortLevel")
    return level if isinstance(level, str) and level else "default"


@dataclass
class _Account:
    num: str
    label: str
    usage: dict | None  # cached last-good usage dict


def _accounts(backup_root: Path, mask: bool = False) -> tuple[_Account | None, _Account | None]:
    """``(active, best other)`` from sequence.json + the cached usage table.

    The active slot is the one ccswap last recorded; resolving it live would
    read the keychain, which a statusline must not.
    """
    seq = _read_json(backup_root / "sequence.json")
    recs = seq.get("accounts")
    if not isinstance(recs, dict):
        return None, None
    identities = {
        str(n): (recs[str(n)].get("email", ""), recs[str(n)].get("organizationUuid", "") or "")
        for n in seq.get("sequence", ())
        if isinstance(recs.get(str(n)), dict)
    }
    entries = UsageStore(backup_root / "cache").entries(identities)
    active_num = str(seq.get("activeAccountNumber"))
    active = best = None
    best_pct = 101.0
    for num in identities:
        rec = recs[num]
        usage = entries[num].last_good
        alias = present_alias(rec.get("alias") or "", mask=mask)
        acct = _Account(num, alias or present_email(rec.get("email") or "", mask=mask), usage)
        if num == active_num:
            active = acct
        elif not rec.get("disabled") and usage:
            pct = _worst_pct(usage)
            if pct is not None and pct < best_pct:
                best, best_pct = acct, pct
    return active, best


def _worst_pct(usage: dict) -> float | None:
    pcts = [
        p for key in ("five_hour", "seven_day")
        if isinstance(usage.get(key), dict) and (p := _num(usage[key].get("pct"))) is not None
    ]
    return max(pcts) if pcts else None


def _rate_line(label: str, pct: float, reset: float | None, with_date: bool, p: _Palette) -> str:
    line = f"{_c(p.fg, label)} {_bar(pct, p)} {_c(p.severity(pct), f'{pct:3.0f}%')}"
    if reset:
        line += f" {_c(p.muted, '⟳')} {_c(p.fg, _clock(reset, with_date))}"
    return line


def _spend_line(live: object, cached: object, p: _Palette) -> str | None:
    """Extra-usage bar: stdin ``rate_limits.spend_limit`` first, else the cached
    ``spend``. Returns None (line dropped) when the numbers aren't usable."""
    if isinstance(live, dict) and _num(live.get("used_percentage")) is not None:
        pct, used, limit = (_num(live.get(k)) for k in ("used_percentage", "used_usd", "limit_usd"))
        reset, sym = _epoch(live.get("resets_at")), "$"
    elif isinstance(cached, dict):
        pct, used, limit = (_num(cached.get(k)) for k in ("pct", "used", "limit"))
        reset = _epoch(cached.get("resets_at"))
        sym = "$" if cached.get("currency", "USD") == "USD" else ""
    else:
        return None
    if pct is None:
        return None
    line = f"{_c(p.fg, 'xtra')} {_bar(pct, p)} "
    if used is None or limit is None:
        # Claude Code omits the amounts for its first few minutes.
        line += _c(p.severity(pct), f"{pct:3.0f}%")
    else:
        line += (
            f"{_c(p.severity(pct), f'{sym}{used:.2f}')}"
            f"{_c(p.muted, '/')}{_c(p.fg, f'{sym}{limit:.2f}')}"
        )
    if reset:
        line += f" {_c(p.muted, '⟳')} {_c(p.fg, _clock(reset, True))}"
    return line


def render(
    data: dict, p: _Palette = DARK, backup_root: Path | None = None, mask: bool = False
) -> str:
    """The full statusline text for one stdin snapshot."""
    sep = f" {_c(p.muted, '│')} "
    model = (data.get("model") or {}).get("display_name") or "Claude"

    ctx = data.get("context_window") or {}
    size = _num(ctx.get("context_window_size")) or 200000
    cur = ctx.get("current_usage") or {}
    used = sum(
        _num(cur.get(k)) or 0.0
        for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
    )
    ctx_pct = int(used * 100 / size)

    cwd = (data.get("workspace") or {}).get("current_dir") or data.get("cwd") or os.getcwd()
    where = _c(p.fg, os.path.basename(cwd.rstrip("/")) or cwd)
    git = _git(cwd)
    if git:
        branch, dirty = git
        where += " " + _c(p.ok, "(" + branch) + (_c(p.crit, "*") if dirty else "") + _c(p.ok, ")")

    parts = [_c(p.accent, model), _c(p.severity(ctx_pct), f"✍️ {ctx_pct}%"), where]
    if dur := _duration((data.get("cost") or {}).get("total_duration_ms")):
        parts.append(_c(p.muted, "⏱ ") + _c(p.fg, dur))
    effort = _effort(data)
    parts.append(_c(p.accent if effort in ("high", "xhigh", "max") else p.muted, f"◑ {effort}"))
    lines = [sep.join(parts)]

    active, best = _accounts(backup_root or paths.get_backup_root(), mask)
    acct_parts = []
    if active:
        acct_parts.append(_c(p.accent, f"#{active.num}") + " " + _c(p.fg, active.label))
    if best and (pct := _worst_pct(best.usage or {})) is not None:
        acct_parts.append(
            _c(p.muted, "next: ") + _c(p.fg, f"#{best.num}") + " " + _c(p.severity(pct), f"{pct:.0f}%")
        )
    if acct_parts:
        lines.append(sep.join(acct_parts))

    # Rate windows: Claude Code's own numbers first, the cached ones per missing window.
    cached = (active.usage if active else None) or {}
    rates = data.get("rate_limits") or {}
    bars = []
    for label, key, with_date in (("5h", "five_hour", False), ("7d", "seven_day", True)):
        live = rates.get(key) or {}
        pct, reset = _num(live.get("used_percentage")), _epoch(live.get("resets_at"))
        if pct is None:
            win = cached.get(key) or {}
            pct, reset = _num(win.get("pct")), _epoch(win.get("resets_at"))
        if pct is not None:
            bars.append(_rate_line(label, pct, reset, with_date, p))
    if line := _spend_line(rates.get("spend_limit"), cached.get("spend"), p):
        bars.append(line)
    if bars:
        lines.append("")
        lines += bars
    return "\n".join(lines)


def main() -> int:
    """Entry point for ``ccswap statusline``. Never raises, always exits 0."""
    raw = ""
    data: dict = {}
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            raise ValueError("empty stdin")
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ValueError("stdin is not a JSON object")
        data = parsed
        ui = load_ui_settings(paths.get_backup_root())
        # `auto` can't probe the terminal from a pipe: default to dark.
        text = render(data, LIGHT if ui.theme == "light" else DARK, mask=ui.mask)
    except Exception:
        model = (data.get("model") or {}) if isinstance(data, dict) else {}
        name = model.get("display_name") if isinstance(model, dict) else None
        text = name if isinstance(name, str) and name else "Claude"
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    print(text, end="")
    return 0
