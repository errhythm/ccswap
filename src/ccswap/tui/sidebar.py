"""Dashboard sidebar at >= 120 cols (Figma 14:3): "Up next" and "Recent switches".

Read-only, built from data ccswap already has. Up next asks the same
selectors ``ccswap switch --strategy <saved strategy>`` runs which account
it would pick, over the usage the dashboard shows, and never switches.
Recent switches parses the switch log the menu bar reads. Both run in a
thread worker: the Claude selector reads the sequence file.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from rich.text import Text
from textual.containers import Vertical
from textual.widgets import Static

from ccswap.models import AccountsSnapshot
from ccswap.tui import overview
from ccswap.tui.data import PROVIDERS, capabilities
from ccswap.tui.overview import account_windows, clock_at, short_label
from ccswap.tui.theme import Palette

if TYPE_CHECKING:
    from ccswap.tui.app import CswapApp

DEFAULT_GATE_PCT = 90.0  # the auto-switch default threshold
RECENT_LIMIT = 5
LOG_NAME = "claude-swap.log"  # the switcher's log (menubar.py reads the same file)


@dataclass(frozen=True)
class UpNext:
    provider: str
    manual: bool = False  # no auto-switch for this provider
    active: str | None = None
    target: str | None = None  # None: the strategy stays on the active account
    window: str = ""  # the window closest to its gate
    pct: float | None = None
    gate: float | None = None
    eta: float | None = None  # projectedExhaustionAt, only when JSON carries it
    note: str = ""  # nothing to rank: "loading…", "no accounts yet", …


def rank_target(
    provider: str, switcher, snap: AccountsSnapshot, strategy: str, models: tuple[str, ...]
) -> str | None:
    """The account ``ccswap [codex] switch --strategy <strategy>`` would pick
    now, or None when it would stay. Calls the switchers' own selectors."""
    active = next(a for a in snap.accounts if a.is_active)
    if provider == "claude":
        usage = {a.number: a.usage.decision_value() for a in snap.accounts}
        select = (
            switcher._select_consume_first_switchable
            if strategy == "consume-first"
            else switcher._select_best_switchable
        )
        return select(active.number, models, usage)[0]
    # Codex: the same candidate filter as CodexAccountSwitcher._switch_with_strategy.
    others = [
        a for a in sorted(snap.accounts, key=lambda a: int(a.number))
        if a.number != active.number and a.switchable and not a.disabled and a.kind == "oauth"
    ]
    select = switcher._select_consume_first if strategy == "consume-first" else switcher._select_best
    return select(active, others)[0]


def _eta(raw: dict, fetched_at: float | None) -> float | None:
    """``projectedExhaustionAt`` exactly as ``list --json`` emits it, shown only
    where the menu bar shows it (before the reset, not lasting to it)."""
    from ccswap.json_output import _pace_fields

    fields = _pace_fields(raw, fetched_at)
    at, resets = overview._ts(fields.get("projectedExhaustionAt")), overview._ts(raw.get("resets_at"))
    if at is None or resets is None or at >= resets or fields.get("willLastToReset") is True:
        return None
    return at


def up_next(
    provider: str,
    switcher,
    snap: AccountsSnapshot | None,
    *,
    strategy: str,
    models: tuple[str, ...],
    gate_for: Callable[[str], float | None],
) -> UpNext:
    if not capabilities(provider).auto_switch:
        return UpNext(provider, manual=True)
    if snap is None:
        return UpNext(provider, note="loading…")
    if not snap.accounts:
        return UpNext(provider, note="no accounts yet")
    active = next((a for a in snap.accounts if a.is_active), None)
    if active is None:
        return UpNext(provider, note="no active managed login")
    tightest = None  # (pct - gate, window, gate)
    for window in account_windows(active.usage.last_good):
        if window.pct is None or window.label == "$$":
            continue
        gate = gate_for("Weekly" if window.label == "wk" else window.label)
        gate = DEFAULT_GATE_PCT if gate is None else gate
        if tightest is None or window.pct - gate > tightest[0]:
            tightest = (window.pct - gate, window, gate)
    try:
        target = rank_target(provider, switcher, snap, strategy, models)
    except Exception:  # read-only aid: never take the dashboard down
        return UpNext(provider, active=active.number, note="unavailable")
    if tightest is None:
        return UpNext(provider, active=active.number, target=target)
    _slack, window, gate = tightest
    return UpNext(
        provider,
        active=active.number,
        target=target,
        window=window.label,
        pct=window.pct,
        gate=gate,
        eta=_eta(window.raw, active.usage.fetched_at) if window.weekly else None,
    )


@dataclass(frozen=True)
class RecentSwitch:
    at: float  # local epoch seconds
    provider: str
    source: str
    target: str


def recent_switches(log_path: Path, limit: int = RECENT_LIMIT) -> list[RecentSwitch]:
    """The newest switches in the switch log, newest first. The log records
    Claude Code switches only, with no reason."""
    from ccswap.menubar import parse_switch_history

    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out = []
    for line in parse_switch_history(text, limit):  # "3 → 1   2026-06-27 02:06"
        pair, _sep, stamp = line.partition("   ")
        source, _arrow, target = pair.partition(" → ")
        try:
            at = datetime.strptime(stamp.strip(), "%Y-%m-%d %H:%M").timestamp()
        except ValueError:
            continue
        out.append(RecentSwitch(at, "claude", source, target))
    return out


def when(at: float, now: float) -> str:
    """HH:MM today, the weekday within the week, else the date."""
    then, today = datetime.fromtimestamp(at), datetime.fromtimestamp(now)
    if then.date() == today.date():
        return then.strftime("%H:%M")
    if 0 < (today.date() - then.date()).days < 7:
        return then.strftime("%a")
    return then.strftime("%b %d")


def up_next_text(rows: list[UpNext], palette: Palette, tr: Callable[[str], str] = str) -> Text:
    name_w = max((len(short_label(r.provider)) for r in rows), default=0)
    text = Text(no_wrap=True, overflow="ellipsis")
    for i, row in enumerate(rows):
        if i:
            text.append("\n")
        text.append(short_label(row.provider).ljust(name_w), style=palette.provider(row.provider))
        text.append("  ")
        if row.manual:
            text.append(tr("manual switching only"), style=palette.dim)
        elif row.note:
            text.append(tr(row.note), style=palette.muted)
        elif row.target is not None and row.target != row.active:
            text.append(f"#{row.active} → #{row.target}", style=palette.foreground)
            if row.gate is not None:
                text.append(
                    tr(" at {gate}% of {window}").format(gate=f"{row.gate:g}", window=row.window),
                    style=palette.muted,
                )
        else:
            text.append(tr("stays on #{n}").format(n=row.active), style=palette.foreground)
            if row.pct is not None:
                text.append(f" · {row.pct:.0f}% {row.window}", style=palette.muted)
        if row.eta is not None:
            text.append("\n" + " " * (name_w + 2))
            text.append(
                tr("expected around {clock}").format(clock=clock_at(row.eta, overview.clock())),
                style=palette.muted,
            )
    return text


def recent_text(
    rows: list[RecentSwitch],
    palette: Palette,
    now: float,
    tr: Callable[[str], str] = str,
    *,
    empty: str = "no switches logged yet",
) -> Text:
    text = Text(no_wrap=True, overflow="ellipsis")
    if not rows:
        text.append(tr(empty), style=palette.dim)
        return text
    stamps = [when(r.at, now) for r in rows]
    stamp_w = max(len(s) for s in stamps)
    for i, (row, stamp) in enumerate(zip(rows, stamps)):
        if i:
            text.append("\n")
        text.append(stamp.ljust(stamp_w), style=palette.muted)
        text.append("  ")
        text.append(short_label(row.provider), style=palette.provider(row.provider))
        text.append(f"  #{row.source} → #{row.target}", style=palette.foreground)
    return text


class Sidebar(Vertical):
    """The two read-only panels; shown by TCSS on -wide dashboards only."""

    _computing = False
    _rerun = False

    def compose(self):
        yield Static("", id="up-next", classes="side-panel")
        yield Static("", id="recent-switches", classes="side-panel")

    def on_mount(self) -> None:
        tr = self.app.gettext
        self.query_one("#up-next").border_title = tr("Up next")
        self.query_one("#recent-switches").border_title = tr("Recent switches")
        self.watch(self.app, "snapshots", lambda _s: self.update_panels())
        self.watch(self.app, "theme", lambda _t: self.update_panels())
        self.set_interval(30.0, self.update_panels)

    def update_panels(self) -> None:
        """Recompute off the UI thread; skipped while the sidebar is hidden."""
        if not self.is_mounted or not self.screen.has_class("-wide"):
            return
        if self._computing:  # one at a time; the newest request runs next
            self._rerun = True
            return
        app: "CswapApp" = self.app  # type: ignore[assignment]
        providers = tuple(PROVIDERS) if app._view == "combined" else (app._view,)
        self._computing, self._rerun = True, False
        self.run_worker(
            partial(self._compute, providers, dict(app.snapshots)),
            thread=True,
            group="sidebar",
            exit_on_error=False,
        )

    def _compute(self, providers: tuple[str, ...], snapshots: dict) -> None:
        from ccswap.settings import load_settings, parse_model_names

        app: "CswapApp" = self.app  # type: ignore[assignment]
        root = app.switcher_for("claude").backup_dir
        try:
            models = parse_model_names(load_settings(root).model)
        except Exception:
            models = ()
        rows = [
            up_next(
                provider,
                app.switcher_for(provider),
                snapshots.get(provider),
                strategy=app._strategy_name,
                models=models if provider == "claude" else (),
                gate_for=app.bar_threshold,
            )
            for provider in providers
        ]
        logged = recent_switches(root / LOG_NAME)
        recent = [r for r in logged if r.provider in providers]
        empty = "no switches logged yet" if not logged else "only Claude Code switches are logged"
        try:
            app.call_from_thread(self._show, rows, recent, empty)
        except RuntimeError:  # the app closed meanwhile
            self._computing = False

    def _show(self, rows: list[UpNext], recent: list[RecentSwitch], empty: str) -> None:
        self._computing = False
        if not self.is_attached:
            return
        if self._rerun:
            self.update_panels()
        palette = Palette.from_theme(self.app.current_theme)
        tr = self.app.gettext
        self.query_one("#up-next", Static).update(up_next_text(rows, palette, tr))
        self.query_one("#recent-switches", Static).update(
            recent_text(recent, palette, overview.clock(), tr, empty=empty)
        )
