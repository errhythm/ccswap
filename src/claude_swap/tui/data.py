"""Data service for the TUI: snapshots, blocking actions, display helpers.

The TUI never parses printed CLI output — it consumes
``ClaudeAccountSwitcher.accounts_snapshot`` (one collect pass, see
switcher.py) and renders structured data. Fetch pacing lives in
``claude_swap.snapshot_source.SnapshotSource`` (shared with any GUI shell);
this module re-exports it for the TUI's use.

Everything here is blocking (file locks, keychain subprocesses, network) and
must be called from a thread worker, never the UI event loop.
"""

from __future__ import annotations

import contextlib
import io
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from claude_swap import oauth, printer, usage_store
from claude_swap.exceptions import ClaudeSwitchError
from claude_swap.models import AccountSnapshot, AccountsSnapshot
from claude_swap.snapshot_source import SnapshotSource
from claude_swap.switcher import SENTINEL_NOTES, last_seen_note


PROVIDERS = ("claude", "codex")
PROVIDER_LABELS = {"claude": "Claude Code", "codex": "Codex"}

# Plan labels and the personal fallback are not an account's identity.
# Organization names are, so masking leaves only the generic tags alone.
_PLAIN_TAGS = frozenset({"personal"})
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def mask_name(name: str) -> str:
    """Mask a display name, keeping only each word's first character."""
    parts: list[str] = []
    for part in name.split():
        if len(part) <= 1:
            parts.append("•")
        else:
            parts.append(part[0] + "•" * min(3, len(part) - 1))
    return " ".join(parts) if parts else name


def _mask_local(local: str) -> str:
    n = len(local)
    if n <= 1:
        return "•"
    if n == 2:
        return local[0] + "•"
    return local[0] + "•" * min(3, n - 2) + local[-1]


def _mask_domain(domain: str) -> str:
    if not domain:
        return "•"
    head, dot, tail = domain.partition(".")
    if len(head) <= 1:
        masked = "•"
    else:
        masked = head[0] + "•" * min(3, len(head) - 1)
    return f"{masked}.{tail}" if dot else masked


def mask_email(email: str) -> str:
    """Redact a mailbox but keep enough shape to tell accounts apart.

    ``alice@acme.com`` becomes ``a•••e@a•••.com``: the local part keeps its
    first and last character (so ``user1`` and ``user2`` still differ) and
    only the first domain label is hidden. A value with no ``@`` is masked
    as a name.
    """
    raw = email.strip()
    if not raw:
        return raw
    if "@" not in raw:
        return mask_name(raw)
    local, domain = raw.rsplit("@", 1)
    return f"{_mask_local(local)}@{_mask_domain(domain)}"


def mask_emails_in_text(text: str) -> str:
    """Replace email addresses in free text (event log, captured CLI output)."""
    return _EMAIL_RE.sub(lambda match: mask_email(match.group(0)), text)


def present_email(email: str, *, mask: bool) -> str:
    return mask_email(email) if mask and email else email


def present_alias(alias: str, *, mask: bool) -> str:
    """Aliases stay visible — they are the local nickname — unless one is an email."""
    if mask and alias and "@" in alias:
        return mask_email(alias)
    return alias


def present_tag(tag: str, *, mask: bool) -> str:
    """Mask organization names; leave ``personal`` and Codex plan labels."""
    if not mask or not tag:
        return tag
    lowered = tag.lower()
    if lowered in _PLAIN_TAGS or lowered == "codex" or lowered.startswith("codex "):
        return tag
    return mask_name(tag)


def account_label(alias: str, email: str, *, mask: bool) -> str:
    """``alias (email)`` or just the email, both sides already masked."""
    shown_email = present_email(email, mask=mask)
    shown_alias = present_alias(alias, mask=mask)
    if shown_alias:
        return f"{shown_alias} ({shown_email})"
    return shown_email


def iter_accounts(
    snapshots: dict[str, AccountsSnapshot | None],
    provider: str | None = None,
) -> list[tuple[str, AccountSnapshot]]:
    """Flatten provider sections without discarding row identity."""
    return [
        (provider_name, account)
        for provider_name in ((provider,) if provider is not None else PROVIDERS)
        if (snapshot := snapshots.get(provider_name)) is not None
        for account in snapshot.accounts
    ]


# ---------------------------------------------------------------------------
# Blocking actions (switch/add/remove) run captured, off the UI thread
# ---------------------------------------------------------------------------


@dataclass
class ActionResult:
    """Outcome of a captured switcher action."""

    ok: bool
    output: str  # captured stdout+stderr, ANSI-colored (render with Text.from_ansi)
    payload: dict | None = None  # structured result for json-capable actions

    @property
    def first_line(self) -> str:
        """First non-empty output line, ANSI-stripped — notification material."""
        from rich.text import Text

        for line in self.output.splitlines():
            plain = Text.from_ansi(line).plain.strip()
            if plain:
                return plain
        return ""


def run_action(fn: Callable[[], dict | None]) -> ActionResult:
    """Run a switcher action capturing stdout+stderr (color forced on).

    ``sys.stdin`` is swapped for an empty stream so an unexpected ``input()``
    raises ``EOFError`` instead of freezing the app (in-scope actions never
    prompt once ``assume_yes``/explicit identifiers are used; this is
    defensive). The redirect is process-global for the duration — fine here
    because the TUI owns the terminal and nothing else prints while it runs.
    """
    buf = io.StringIO()
    payload: dict | None = None
    saved_stdin = sys.stdin
    sys.stdin = io.StringIO()
    try:
        with (
            printer.force_color(),
            contextlib.redirect_stdout(buf),
            contextlib.redirect_stderr(buf),
        ):
            try:
                payload = fn()
            except ClaudeSwitchError as e:
                print(f"Error: {e}")
                return ActionResult(False, buf.getvalue())
            except EOFError:
                print("Error: interactive input is not available here.")
                return ActionResult(False, buf.getvalue())
    finally:
        sys.stdin = saved_stdin
    return ActionResult(
        True, buf.getvalue(), payload if isinstance(payload, dict) else None
    )


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------


def sentinel_label(sentinel: str) -> str:
    """The same wording ``cswap list`` prints for this sentinel state."""
    return SENTINEL_NOTES.get(sentinel, sentinel)


def window_pct(last_good: dict | None, key: str) -> float | None:
    """Utilization pct of one window ("five_hour"/"seven_day"), if known."""
    if not isinstance(last_good, dict):
        return None
    window = last_good.get(key)
    if not isinstance(window, dict):
        return None
    pct = window.get("pct")
    return float(pct) if isinstance(pct, (int, float)) else None


def reset_countdown(
    window: dict | None, now: float, *, tr: Callable[[str], str] = str
) -> str | None:
    """Live countdown without a language-dependent prefix, if known.

    Computed from ``resets_at`` at render time — the countdown the API sent
    was correct at *fetch* time and drifts as the measurement ages.
    """
    if not isinstance(window, dict):
        return None
    resets_at = window.get("resets_at")
    if not resets_at:
        return None
    try:
        ts = datetime.fromisoformat(str(resets_at).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None
    remaining = ts - now
    if remaining <= 0:
        return tr("now")
    return format_duration(remaining)


def reset_text(
    window: dict | None, now: float, *, tr: Callable[[str], str] = str
) -> str | None:
    countdown = reset_countdown(window, now)
    if countdown == "now":
        return tr("resets now")
    return tr("resets {duration}").format(duration=countdown) if countdown else None


def reset_clock(window: dict | None, now: float) -> str | None:
    """Absolute local reset time ("20:39" / "Jul 14 09:00"), if known.

    None once the reset has elapsed — "resets now" needs no clock.
    """
    if not isinstance(window, dict):
        return None
    resets_at = window.get("resets_at")
    if not resets_at:
        return None
    try:
        reset_utc = datetime.fromisoformat(str(resets_at).replace("Z", "+00:00"))
    except ValueError:
        return None
    if reset_utc.timestamp() - now <= 0:
        return None
    return oauth.reset_clock_string(
        reset_utc, datetime.fromtimestamp(now, tz=timezone.utc)
    )


def window_reset_text(last_good: dict | None, key: str, now: float) -> str | None:
    """`reset_text` for one of the top-level 5h/7d windows."""
    if not isinstance(last_good, dict):
        return None
    return reset_text(last_good.get(key), now)


def format_duration(seconds: float) -> str:
    """Compact duration: "45s", "12m", "2h 13m", "3d 4h"."""
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        h, m = divmod(s // 60, 60)
        return f"{h}h {m}m" if m else f"{h}h"
    d, h = divmod(s // 3600, 24)
    return f"{d}d {h}h" if h else f"{d}d"


def format_age(age_s: float | None, *, tr: Callable[[str], str] = str) -> str | None:
    """Measurement age note ("· 2m ago"); None while comfortably fresh."""
    if age_s is None or age_s < usage_store.SERVE_TTL_S:
        return None
    return tr("· {age} ago").format(age=format_duration(age_s))


def clock_stamp() -> str:
    """HH:MM:SS local-time stamp for the event log."""
    return time.strftime("%H:%M:%S")


__all__ = [
    "ActionResult",
    "SnapshotSource",
    "account_label",
    "format_age",
    "format_duration",
    "last_seen_note",
    "mask_email",
    "mask_emails_in_text",
    "mask_name",
    "present_alias",
    "present_email",
    "present_tag",
    "reset_clock",
    "reset_countdown",
    "reset_text",
    "run_action",
    "sentinel_label",
    "clock_stamp",
    "window_pct",
    "window_reset_text",
]
