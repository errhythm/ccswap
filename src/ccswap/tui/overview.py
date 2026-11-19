"""Figma "TUI" page look (frames 14:3, 16:3) for main's account views.

Pure render helpers (an account and a clock in, a Rich ``Text`` out) plus two
widgets: :class:`HeaderBar` (brand and the ``ui.view`` filter drawn as tabs)
and :class:`ProviderPanel` (one ``border: round`` panel per provider).
Lines and boxes come from Textual borders, never from repeated ``─`` text.
"""

from __future__ import annotations

import textwrap
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Callable

from rich.text import Text
from textual.widgets import Static

from ccswap import oauth, pace
from ccswap.json_output import USAGE_API_KEY
from ccswap.models import AccountSnapshot
from ccswap.reset_credit_display import reset_credit_display
from ccswap.subscription import plan_label
from ccswap.switcher import ERROR_NOTES
from ccswap.tui import data
from ccswap.tui.data import PROVIDER_LABELS, PROVIDERS, capabilities
from ccswap.tui.theme import CRIT_PCT, Palette
from ccswap.usage_store import STALE_OK_S

if TYPE_CHECKING:
    from ccswap.tui.app import CswapApp


def clock() -> float:
    """The account views' clock. Snapshot tests replace it."""
    return time.time()


# Screen classes from HORIZONTAL_BREAKPOINTS (set on every account screen).
BREAKPOINTS = [(0, "-narrow"), (100, "-medium"), (120, "-wide")]

GAUGE_W = 20  # widest gauge (Figma wide view: ~20 cells)
GROUP_GAUGE_W = 10  # Antigravity model-group gauges
MIN_GAUGE_W = 6  # Figma compact view

# Textual's own bar glyphs (textual/renderables/bar.py) and Rule's heavy vertical.
_FILLED, _HALF, _TRACK, _TICK = "━", "╸", "─", "┃"
_WINDOWS = (("five_hour", "5h"), ("seven_day", "7d"), ("weekly", "wk"))


# ---------------------------------------------------------------------------
# small text helpers
# ---------------------------------------------------------------------------


def _ts(resets_at: object) -> float | None:
    if not isinstance(resets_at, str) or not resets_at:
        return None
    try:
        return datetime.fromisoformat(resets_at.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def clock_at(ts: float, now: float) -> str:
    """Local clock for an epoch: "20:39" today, "Nov 26 02:00" otherwise."""
    return oauth.reset_clock_string(
        datetime.fromtimestamp(ts, tz=timezone.utc),
        datetime.fromtimestamp(now, tz=timezone.utc),
    )


def _fit(text: Text, width: int) -> Text:
    """One line, never wider than ``width`` (ellipsis instead of a wrap)."""
    if text.cell_len > width:
        text = text.copy()
        text.truncate(max(1, width), overflow="ellipsis")
    return text


def short_label(provider: str) -> str:
    """Narrow tab name: "Claude", "Codex", "Antigravity"."""
    return PROVIDER_LABELS[provider].split()[0]


def plan_text(acc: AccountSnapshot) -> str:
    """The plan column, the same rule as the menu bar's card chip: Claude's
    stored plan record, else a Codex plan tag with "Codex" dropped."""
    record = acc.subscription_plan
    if isinstance(record, dict) and isinstance(record.get("plan"), dict):
        label = plan_label(record["plan"])
        if label:
            return label
    if acc.org_name.startswith("Codex "):
        return acc.org_name[len("Codex "):]
    return ""


def tick_color(pct: float | None, tick: float | None, palette: Palette) -> str:
    """Below its gate the tick is the dim track colour; at or past the gate it
    turns bad (usage >= 90%) or warn."""
    if pct is None or tick is None or pct < tick:
        return palette.dim
    return palette.sev_crit if pct >= CRIT_PCT else palette.sev_warn


def gauge(
    pct: float | None,
    width: int,
    palette: Palette,
    *,
    tick: float | None = None,
    stale: bool = False,
) -> Text:
    """``━━━━╸─┃──``: used quota heavy (half-cell end), the rest a dim light
    track, ┃ at the auto-switch threshold (main's tick, same rounding)."""
    text = Text()
    mark = None if tick is None else min(width - 1, max(0, round(tick / 100 * width)))
    cells = 0.0 if pct is None else min(max(pct, 0.0), 100.0) / 100 * width
    full = int(cells)
    half = cells - full >= 0.5 and full < width
    color = palette.severity(pct) if pct is not None else palette.dim
    fill = f"{color} dim" if stale else color
    tick_style = tick_color(pct, tick, palette)
    for i in range(width):
        if i == mark:
            text.append(_TICK, style=tick_style)
        elif i < full:
            text.append(_FILLED, style=fill)
        elif i == full and half:
            text.append(_HALF, style=fill)
        else:
            text.append(_TRACK, style=palette.dim)
    return text


def _pct_text(pct: float | None, palette: Palette, *, stale: bool = False, width: int = 4) -> Text:
    if pct is None:
        return Text("?".rjust(width), style=palette.muted)
    color = palette.severity(pct)
    return Text(f"{pct:.0f}%".rjust(width), style=f"{color} dim" if stale else color)


# ---------------------------------------------------------------------------
# windows
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Window:
    label: str
    pct: float | None
    raw: dict
    weekly: bool  # pace applies (never to a 5h window)
    extra: str = ""  # spend amounts, "(!)"


def account_windows(last_good: dict | None) -> list[Window]:
    """Every window the account reports, Antigravity groups excepted."""
    if not isinstance(last_good, dict):
        return []
    out: list[Window] = []
    spend = last_good.get("spend")
    if isinstance(spend, dict):
        amounts = f"${spend['used']:,.2f} / ${spend['limit']:,.2f}"
        out.append(Window("$$", float(spend["pct"]), spend, False, amounts))
    for key, label in _WINDOWS:
        window = last_good.get(key)
        if isinstance(window, dict):
            pct = window.get("pct")
            out.append(
                Window(
                    label,
                    float(pct) if isinstance(pct, (int, float)) else None,
                    window,
                    key != "five_hour",
                )
            )
    for window in last_good.get("scoped") or []:
        if "group" in window:
            continue
        pct = float(window["pct"])
        out.append(
            Window(
                window["name"],
                pct,
                window,
                window.get("window") != "5h",
                "(!)" if pct >= 100 else "",
            )
        )
    return out


def model_groups(last_good: dict | None) -> list[tuple[str, dict[str, dict]]]:
    """Antigravity's per-group buckets: ``[(group, {"5h": w, "weekly": w})]``."""
    if not isinstance(last_good, dict):
        return []
    groups: dict[str, dict[str, dict]] = {}
    for window in last_good.get("scoped") or []:
        if "group" in window:
            groups.setdefault(window["group"], {})[window.get("window", "")] = window
    return list(groups.items())


def _pace(window: Window, fetched_at: float | None) -> pace.PaceResult | None:
    return pace.compute_pace(window.raw, fetched_at=fetched_at) if window.weekly else None


# ---------------------------------------------------------------------------
# one account block
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Columns:
    """Header-line columns shared by every account in one panel."""

    name: int = 0
    plan: int = 0


def name_text(acc: AccountSnapshot, palette: Palette, *, mask: bool, show_tag: bool) -> Text:
    alias = data.present_alias(acc.alias, mask=mask)
    email = data.present_email(acc.email, mask=mask)
    text = Text()
    if alias:
        text.append(alias, style=f"bold {palette.accent}")
        text.append(f" ({email})", style=f"bold {palette.foreground}")
    else:
        text.append(email, style=f"bold {palette.foreground}")
    if show_tag:
        text.append(f" [{data.present_tag(acc.display_tag, mask=mask)}]", style=palette.muted)
    return text


def _window_line(
    window: Window,
    *,
    label_w: int,
    gauge_w: int,
    width: int,
    now: float,
    palette: Palette,
    fetched_at: float | None,
    stale: bool,
    narrow: bool,
    tick: float | None = None,
) -> Text:
    result = _pace(window, fetched_at)
    line = Text("  ")
    line.append(window.label.ljust(label_w), style=palette.muted)
    line.append("  ")
    line.append(
        gauge(
            window.pct,
            gauge_w,
            palette,
            tick=tick,
            stale=stale,
        )
    )
    if window.pct is None:
        line.append("  usage unknown", style=palette.muted)  # main's wording
    else:
        line.append("  ")
        line.append(_pct_text(window.pct, palette, stale=stale))
    parts: list[str] = []
    if result is not None and result.ahead and window.pct is not None and window.pct < 100:
        parts.append("ahead of pace")
    reset = data.reset_text(window.raw, now)
    if reset:
        clock_text = data.reset_clock(window.raw, now)
        if clock_text and not narrow:
            reset += f" · {clock_text}"
        parts.append(reset)
    if window.extra:
        parts.append(window.extra)
    if parts:
        line.append("  " + " · ".join(parts), style=palette.muted)
    return _fit(line, width)


def _group_lines(
    groups: list[tuple[str, dict[str, dict]]],
    *,
    width: int,
    now: float,
    palette: Palette,
    stale: bool,
    threshold_for: Callable[[str], float | None] | None = None,
) -> list[Text]:
    """``Gemini models  5h ──── 4%  4h 44m   wk ━─── 22%  6d 10h``

    ``threshold_for`` gets each window's own name, as main's rows did."""
    name_w = min(24, max(len(name) for name, _ in groups))
    count_w = 7

    def cell_w(bar: int, counts: bool) -> int:
        return 3 + bar + 5 + (2 + count_w if counts else 0)

    for bar, counts in ((GROUP_GAUGE_W, True), (MIN_GAUGE_W, True), (MIN_GAUGE_W, False)):
        if 2 + name_w + 2 * (2 + cell_w(bar, counts)) <= width:
            break
    lines = []
    for name, buckets in groups:
        line = Text("  ")
        shown = name if len(name) <= name_w else name[: name_w - 1] + "…"
        line.append(shown.ljust(name_w), style=palette.muted)
        for key, label in (("5h", "5h"), ("weekly", "wk")):
            window = buckets.get(key)
            line.append("  ")
            if window is None:
                line.append(" " * cell_w(bar, counts))
                continue
            pct = float(window["pct"])
            line.append(f"{label} ", style=palette.dim)
            tick = threshold_for(window["name"]) if threshold_for is not None else None
            line.append(gauge(pct, bar, palette, tick=tick, stale=stale))
            line.append(_pct_text(pct, palette, stale=stale, width=5))
            if counts:
                left = data.reset_countdown(window, now) or ""
                line.append(("  " + left).ljust(2 + count_w), style=palette.dim)
        lines.append(_fit(line.rstrip() or line, width))
    return lines




def _head(
    acc: AccountSnapshot,
    provider: str,
    *,
    cols: Columns,
    width: int,
    palette: Palette,
    mask: bool,
    show_tag: bool,
    narrow: bool,
    marker: str | None,
    tag: str | None = None,
) -> Text:
    """``● 1  john@x.com   Max 5x   ACTIVE``. Narrow drops the plan first."""
    head = Text()
    if marker is not None:
        head.append(marker, style=f"bold {palette.accent}")
    if acc.is_active:
        head.append("● ", style=palette.provider(provider))
    else:
        head.append("○ ", style=palette.dim)
    head.append(f"{acc.number}  ", style=palette.muted)
    name_at = head.cell_len
    head.append(name_text(acc, palette, mask=mask, show_tag=show_tag))
    if tag:
        head.append(f"  {tag}", style=palette.muted)
    status = Text()
    if acc.is_active:
        status.append("ACTIVE", style=f"bold {palette.provider(provider)}")
    if acc.disabled:
        status.append(f"{' ' if status else ''}(disabled)", style=palette.muted)
    age = data.format_age(acc.usage.age_s)
    if age:
        status.append(f"{' ' if status else ''}{age}", style=palette.dim)
    plan = "" if narrow else plan_text(acc)
    plan_w = max(cols.plan, len(plan))
    if not narrow and name_at + cols.name + 2 + plan_w + 2 + status.cell_len <= width:
        head.pad_right(max(0, name_at + cols.name - head.cell_len))
        if plan_w:
            head.append("  ")
            head.append(plan.ljust(plan_w), style=palette.muted)
    elif plan:
        head.append(f"  {plan}", style=palette.muted)
    if status:
        head.append("  ")
        head.append(status)
    return head


class _Whole(Text):
    """A line that folds instead of being cut (warnings whose text is the point)."""


def _status_lines(acc: AccountSnapshot, now: float, palette: Palette, width: int = 200) -> list[Text]:
    """Recovery and urgent banked-reset warnings, as main shows them.

    The recovery line is left whole (Rich folds it): its file path is the one
    thing the user needs to recover, so it is never cut."""
    lines: list[Text] = []
    recovery = acc.usage.recovery
    if recovery is not None:
        message = recovery.instruction
        if recovery.path and message.endswith(recovery.path):
            # The path on a line of its own, indented, so a fold stays inside it.
            message = message[: -len(recovery.path)].rstrip()
            for i, part in enumerate(textwrap.wrap(message, max(20, width - 4))):
                lines.append(_Whole(("  ⚠ " if i == 0 else "    ") + part, style=palette.sev_warn))
            lines.append(_Whole("    " + recovery.path, style=palette.sev_warn))
        else:
            lines.append(_Whole("  ⚠ " + message, style=palette.sev_warn))
    reset_display = reset_credit_display(acc.usage.last_good, now)
    if reset_display and reset_display.urgent:
        style = palette.sev_crit if reset_display.severity in {"critical", "expired"} else palette.sev_warn
        line = _Whole(f"  ⚠ Resets {reset_display.text}", style=f"bold {style}")
        if acc.usage.sentinel or acc.usage.last_error:
            line.append(" · last known", style=palette.muted)
        lines.append(line)
    return lines


def _sentinel_lines(acc: AccountSnapshot, palette: Palette) -> list[Text]:
    sentinel = acc.usage.sentinel
    if sentinel is None:
        return []
    api_key = sentinel == USAGE_API_KEY
    lines = [
        _Whole(
            f"  {'·' if api_key else '⚠'} {data.sentinel_label(sentinel)}",
            style=palette.muted if api_key else palette.sev_warn,
        )
    ]
    if not api_key and (last_seen := data.last_seen_note(acc.usage)) is not None:
        lines.append(Text(f"  └ {last_seen}", style=palette.muted))
    return lines


def _money_lines(last_good: dict | None, now: float, palette: Palette, *, compact: bool) -> list[Text]:
    from ccswap.tui.widgets import credit_allowance_text, credits_text

    reset_display = reset_credit_display(last_good, now)
    lines = []
    for label, value in (
        ("Credits", credits_text(last_good, now, compact=compact)),
        ("Allowance", credit_allowance_text(last_good, now, compact=compact)),
        ("Resets", reset_display.text if reset_display and not reset_display.urgent else None),
    ):
        if value:
            line = _Whole(f"  {label} ", style=palette.muted)  # amounts fold, never cut
            line.append(value, style=palette.foreground)
            lines.append(line)
    return lines


def _join(lines: list[Text], width: int) -> Text:
    """One Text; every line but a recovery warning is cut to ``width``."""
    out = Text(overflow="fold")
    for i, line in enumerate(lines):
        if i:
            out.append("\n")
        # Recovery text is never cut: the user needs that whole path.
        out.append(line if isinstance(line, _Whole) else _fit(line, width))
    return out


def account_block(
    acc: AccountSnapshot,
    provider: str,
    *,
    width: int,
    cols: Columns = Columns(),
    now: float,
    palette: Palette,
    mask: bool = False,
    show_tag: bool = False,
    narrow: bool = False,
    marker: str | None = None,
    tag: str | None = None,
    threshold_for: Callable[[str], float | None] | None = None,
) -> Text:
    """The full account: header line, then one gauge row per window.
    ``threshold_for`` (label -> pct) draws main's auto-switch tick."""
    if marker is not None:
        # Rows follow the name under a cursor marker.
        body = account_block(
            acc, provider, width=width - len(marker), cols=cols, now=now,
            palette=palette, mask=mask, show_tag=show_tag, narrow=narrow, tag=tag,
            threshold_for=threshold_for,
        )
        lines = body.split("\n", allow_blank=True)
        out = Text(overflow="fold")
        for i, line in enumerate(lines):
            if i:
                out.append("\n" + " " * len(marker))
            else:
                out.append(marker, style=f"bold {palette.accent}")
            out.append(line)
        return out
    lines = [
        _head(
            acc, provider, cols=cols, width=width, palette=palette, mask=mask,
            show_tag=show_tag, narrow=narrow, marker=None, tag=tag,
        )
    ]
    lines += _status_lines(acc, now, palette, width)
    if acc.usage.sentinel is not None:
        return _join(lines + _sentinel_lines(acc, palette), width)
    last_good = acc.usage.last_good
    windows = account_windows(last_good)
    groups = model_groups(last_good)
    money = _money_lines(last_good, now, palette, compact=False)
    if not windows and not groups and not money:
        reset_display = reset_credit_display(last_good, now)
        if not (reset_display and reset_display.urgent):
            line = Text("  usage unavailable", style=palette.muted)
            if acc.usage.last_error:
                note = ERROR_NOTES.get(acc.usage.last_error, acc.usage.last_error)
                line.append(f" · {note}", style=palette.muted)
            lines.append(line)
        return _join(lines, width)
    stale = acc.usage.age_s is not None and acc.usage.age_s > STALE_OK_S
    if windows:
        label_w = max(2, *(len(w.label) for w in windows))
        # Gauge from the room left after label, %, and a short reset note.
        gauge_w = max(MIN_GAUGE_W, min(GAUGE_W, width - label_w - 34))
        for window in windows:
            lines.append(
                _window_line(
                    window, label_w=label_w, gauge_w=gauge_w, width=width, now=now,
                    palette=palette, fetched_at=acc.usage.fetched_at, stale=stale, narrow=narrow,
                    tick=threshold_for("Weekly" if window.label == "wk" else window.label)
                    if threshold_for is not None
                    else None,
                )
            )
    if groups:
        lines += _group_lines(
            groups, width=width, now=now, palette=palette, stale=stale,
            threshold_for=threshold_for,
        )
    lines += money
    return _join(lines, width)


def compact_block(
    acc: AccountSnapshot,
    provider: str,
    *,
    width: int,
    cols: Columns = Columns(),
    now: float,
    palette: Palette,
    mask: bool = False,
    show_tag: bool = False,
    narrow: bool = False,
) -> Text:
    """An inactive account on one line (Figma compact rows): its 5h/7d/wk
    windows as short gauges after the name. Extra lines only for what main's
    one-line form also adds: warnings, groups, and per-model windows when
    there is no account-wide one."""
    head = _head(
        acc, provider, cols=cols, width=width, palette=palette, mask=mask,
        show_tag=show_tag, narrow=narrow, marker=None,
    )
    extra = _status_lines(acc, now, palette, width)
    if acc.usage.sentinel is not None:
        sentinel = acc.usage.sentinel
        head.append("   ")
        head.append(
            data.sentinel_label(sentinel),
            style=palette.muted if sentinel == USAGE_API_KEY else palette.sev_warn,
        )
        return _join([head] + extra, width)
    last_good = acc.usage.last_good
    stale = acc.usage.age_s is not None and acc.usage.age_s > STALE_OK_S
    main = [w for w in account_windows(last_good) if w.label in ("5h", "7d", "wk")]
    scoped = [w for w in account_windows(last_good) if w.label not in ("5h", "7d", "wk", "$$")]
    inline = Text()
    for window in main:
        inline.append("   ")
        inline.append(f"{window.label} ", style=palette.dim)
        inline.append(gauge(window.pct, MIN_GAUGE_W, palette, stale=stale))
        inline.append(_pct_text(window.pct, palette, stale=stale, width=5))
        if window.pct is not None and window.pct >= 100:
            reset = data.reset_text(window.raw, now)
            if reset:
                inline.append(f" ({reset})", style=palette.muted)
        elif window.label == "7d":
            result = _pace(window, acc.usage.fetched_at)
            if result and result.ahead:
                inline.append(" (ahead)", style=palette.sev_warn)
    sub: list[Text] = []
    if main:
        for window in scoped:
            if window.pct is not None and window.pct >= 100:
                inline.append(f"   {window.label} (!)", style=palette.sev_crit)
    else:
        for window in scoped:
            line = Text(f"  {window.label} ", style=palette.muted)
            line.append(gauge(window.pct, MIN_GAUGE_W, palette, stale=stale))
            line.append(_pct_text(window.pct, palette, stale=stale, width=5))
            if window.pct is not None and window.pct >= 100:
                line.append(" (!)", style=palette.sev_crit)
            sub.append(line)
    groups = model_groups(last_good)
    if groups:
        sub += _group_lines(groups, width=width, now=now, palette=palette, stale=stale)
    money = _money_lines(last_good, now, palette, compact=True)
    if head.cell_len + inline.cell_len <= width:
        head.append(inline)
    elif inline:
        sub.insert(0, Text(" ") + inline)
    if not main and not sub and not money:
        reset_display = reset_credit_display(last_good, now)
        if not (reset_display and reset_display.urgent):
            head.append("   usage unknown", style=palette.muted)
    return _join([head] + extra + sub + money, width)


# ---------------------------------------------------------------------------
# widgets
# ---------------------------------------------------------------------------


def tabs() -> tuple[str, ...]:
    """The ``ui.view`` choices, drawn as tabs: all providers, then each one."""
    return ("combined", *PROVIDERS)


def account_count_note(provider: str, count: int) -> str:
    """Border subtitle: "2 accounts · restart after switch"."""
    if not count:
        return ""
    note = f"{count} account{'s' if count != 1 else ''}"
    if capabilities(provider).restart_after_switch:
        note += " · restart after switch"
    return note


class HeaderBar(Static):
    """Brand mark and the ``ui.view`` filter as tabs, over a border-bottom
    rule. The right side carries the snapshot-age / long-refresh note.

    On screens with ``action_view_tab`` (dashboard, Switch, Watch) a click on
    a tab switches the view for this session; hovering one tints it."""

    def __init__(self, *, id: str | None = "header", tab: str | None = None) -> None:
        super().__init__(id=id)
        self.tab = tab  # a screen bound to one provider (the auto view) marks it
        self._spans: list[tuple[int, int, str]] = []  # (start, end, tab) last drawn
        self._hover: str | None = None

    def on_mount(self) -> None:
        self.watch(self.app, "refresh_status", lambda _s: self.refresh())
        self.watch(self.app, "theme", lambda _t: self.refresh())

    def _clickable(self) -> bool:
        return self.tab is None and hasattr(self.screen, "action_view_tab")

    def _tab_at(self, event) -> str | None:
        offset = event.get_content_offset(self)
        if offset is None or offset.y != 0 or not self._clickable():
            return None
        return next((tab for start, end, tab in self._spans if start <= offset.x < end), None)

    def on_mouse_move(self, event) -> None:
        hover = self._tab_at(event)
        if hover != self._hover:
            self._hover = hover
            self.refresh()

    def on_leave(self, _event) -> None:
        if self._hover is not None:
            self._hover = None
            self.refresh()

    def on_click(self, event) -> None:
        tab = self._tab_at(event)
        if tab is not None and tab != self.app._view:
            order = tabs()
            self.app.step_view(order.index(tab) - order.index(self.app._view))

    def render(self) -> Text:
        app: "CswapApp" = self.app  # type: ignore[assignment]
        palette = Palette.from_theme(app.current_theme)
        width = self.content_size.width or 80
        current = self.tab or (app._view if app._view in tabs() else "combined")
        status = Text(app.refresh_status, style=palette.sev_warn)
        for short in (False, True):
            text = Text()
            spans: list[tuple[int, int, str]] = []
            text.append("∞", style=f"bold {palette.mark}")
            text.append(" ")
            text.append("ccswap", style=f"bold {palette.foreground}")
            for tab in tabs():
                text.append("  " if short else "   ")
                if tab == "combined":
                    label = "All" if short else "Overview"
                else:
                    label = short_label(tab) if short else PROVIDER_LABELS[tab]
                hover = f" on {palette.hover}" if tab == self._hover else ""
                start = text.cell_len
                if tab == current:
                    text.append(label, style=f"bold underline {palette.accent}{hover}")
                else:
                    text.append(label, style=f"{palette.muted}{hover}")
                spans.append((start, text.cell_len, tab))
            if text.cell_len <= width:
                break
        self._spans = spans
        if status and text.cell_len + 3 + status.cell_len <= width:
            text.append(" " * (width - text.cell_len - status.cell_len))
            text.append(status)
        return _fit(text, width)


class ProviderPanel(Static):
    """One provider's accounts in a ``border: round`` panel: the name is the
    border title, the account count the border subtitle."""

    def __init__(self, provider: str, *, show_minis: bool = True) -> None:
        super().__init__(id=f"panel-{provider}", classes="provider-panel")
        self.provider = provider
        self.show_minis = show_minis
        self._width = 0
        # (first row, last row + 1, account number) of each block last drawn.
        self.rows: list[tuple[int, int, str]] = []

    def get_content_height(self, container, viewport, width: int) -> int:
        # Lines are cut or folded to the width, so measure at the width
        # being laid out, not the last one drawn.
        self._width = width
        return super().get_content_height(container, viewport, width)

    def on_resize(self) -> None:
        self._width = self.content_size.width

    def accounts(self) -> tuple[AccountSnapshot, ...] | None:
        snap = self.app.snapshots.get(self.provider)
        return None if snap is None else snap.accounts

    def update_border(self) -> None:
        self.border_title = PROVIDER_LABELS[self.provider]
        self.border_subtitle = account_count_note(self.provider, len(self.accounts() or ()))

    def render(self) -> Text:
        app: "CswapApp" = self.app  # type: ignore[assignment]
        palette = Palette.from_theme(app.current_theme)
        accounts = self.accounts()
        if accounts is None:
            error = app._last_refresh_error.get(self.provider, "")
            return Text(error or app.gettext("loading…"), style=palette.muted)
        width = max(20, self._width or self.content_size.width or 76)
        narrow = self.screen.has_class("-narrow")
        mask = app.mask_accounts
        now = clock()
        shown = [a for a in accounts if a.is_active or self.show_minis]
        shown.sort(key=lambda a: not a.is_active)  # active first, then slot order
        # Columns align the one-line rows with each other; the full card
        # above them has its own line and needs no padding.
        rows = [a for a in shown if not a.is_active]
        cols = Columns(
            name=max(
                (name_text(a, palette, mask=mask, show_tag=app.tag_shown(a)).cell_len for a in rows),
                default=0,
            ),
            plan=max((len(plan_text(a)) for a in rows), default=0),
        )
        cursor = getattr(self.parent, "cursor", None)
        blocks = []
        for acc in shown:
            common = dict(
                width=width, cols=cols, now=now, palette=palette,
                mask=mask, show_tag=app.tag_shown(acc), narrow=narrow,
            )
            if acc.is_active:
                common["cols"] = Columns()
                blocks.append(account_block(acc, self.provider, threshold_for=app.bar_threshold, **common))
            else:
                blocks.append(compact_block(acc, self.provider, **common))
        if not blocks:
            self.rows = []
            return Text(app.gettext("no active managed login"), style=palette.muted)
        text = Text(overflow="fold")
        rows: list[tuple[int, int, str]] = []
        row = 0
        for i, (acc, block) in enumerate(zip(shown, blocks)):
            if i:
                # A blank line around multi-line blocks; one-line rows stack.
                multi = "\n" in block.plain or "\n" in blocks[i - 1].plain
                text.append("\n\n" if multi else "\n")
                row += 2 if multi else 1
            lines = block.split("\n", allow_blank=True)
            height = sum(max(1, -(-line.cell_len // width)) for line in lines)
            rows.append((row, row + height, acc.number))
            row += height - 1
            if cursor == (self.provider, acc.number):
                # The dashboard cursor: the menu's accent tint, the whole block.
                block = Text(overflow="fold")
                for j, line in enumerate(lines):
                    if j:
                        block.append("\n")
                    line.pad_right(max(0, width - line.cell_len))
                    line.stylize(f"bold on {palette.cursor}")
                    block.append(line)
            text.append(block)
        self.rows = rows
        return text

    def number_at(self, y: int) -> str | None:
        """The account drawn on content row ``y``, if any."""
        return next((number for start, end, number in self.rows if start <= y < end), None)

    def on_click(self, event) -> None:
        offset = event.get_content_offset(self)
        number = None if offset is None else self.number_at(offset.y)
        handler = getattr(self.screen, "account_clicked", None)
        if number is not None and handler is not None:
            handler(self.provider, number)
