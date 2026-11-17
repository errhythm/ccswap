"""Ask for a GitHub star, rarely: after a switch off an account at 95%+ usage.

Shows at most once every 60 days, only in an interactive terminal, and never
again once the user opens the repo or picks "never"; disabled by ``ui.starPrompt=false``.
Never raises.
"""

from __future__ import annotations

import json
import sys
import time
import webbrowser
from pathlib import Path

from ccswap.fsutil import atomic_write_text
from ccswap.printer import accent, muted
from ccswap.usage_store import UsageStore

REPO_URL = "https://github.com/errhythm/ccswap"
STATE_NAME = "star_prompt.json"
INTERVAL_S = 60 * 86400
THRESHOLD_PCT = 95.0
# Cached usage older than one 5h window says little about the switch just made.
MAX_USAGE_AGE_S = 5 * 3600


def _worst_pct(usage: dict | None) -> float | None:
    if not isinstance(usage, dict):
        return None
    windows = [usage.get("five_hour"), usage.get("seven_day"), *(usage.get("scoped") or [])]
    pcts = [
        float(w["pct"]) for w in windows
        if isinstance(w, dict) and isinstance(w.get("pct"), (int, float))
    ]
    return max(pcts) if pcts else None


def _left_near_limit(backup_dir: Path, seq: dict, num: str, now: float) -> bool:
    rec = (seq.get("accounts") or {}).get(num)
    if not isinstance(rec, dict):
        return False
    identity = (rec.get("email", ""), rec.get("organizationUuid", "") or "")
    entry = UsageStore(backup_dir / "cache").entries({num: identity})[num]
    if entry.fetched_at is None or now - entry.fetched_at > MAX_USAGE_AGE_S:
        return False
    pct = _worst_pct(entry.last_good)
    return pct is not None and pct >= THRESHOLD_PCT


def _read_state(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def maybe_ask(switcher, from_num: str | None) -> None:
    """Called after a successful human-facing switch away from ``from_num``."""
    try:
        if from_num is None or not _interactive():
            return
        seq = switcher._get_sequence_data() or {}
        if str(seq.get("activeAccountNumber")) == str(from_num):
            return  # no switch happened
        path = Path(switcher.backup_dir) / "cache" / STATE_NAME
        state = _read_state(path)
        now = time.time()
        if state.get("never") or now - float(state.get("lastShownAt") or 0) < INTERVAL_S:
            return
        if not _left_near_limit(Path(switcher.backup_dir), seq, str(from_num), now):
            return

        print("\nIf ccswap saved you from a rate limit, a ⭐ on GitHub helps other people find it.")
        answer = None
        while answer not in ("", "n", "l"):
            try:
                answer = input(muted("[Enter] open GitHub  [n] never show again  [l] later: "))
            except (EOFError, KeyboardInterrupt):
                answer = "l"  # cancelled: later, never open the browser
            answer = answer.strip().lower()
        state["lastShownAt"] = now
        if answer == "":
            webbrowser.open(REPO_URL)
            print(accent("Thanks!"))
            state["never"] = True
        elif answer == "n":
            state["never"] = True
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, json.dumps(state))
    except Exception:
        pass
