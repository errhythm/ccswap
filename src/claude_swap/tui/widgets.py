"""Shared render widgets: usage bars, account cards, and the accounts panel.

``bar_cells``/``usage_bar`` are custom renderers rather than Textual's
``ProgressBar`` because the design needs three things the stock widget
doesn't do: a severity color ramp, an optional threshold tick mark (the
auto-switch trigger line), and stale-measurement dimming.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Callable

from rich.text import Text
from textual.widgets import ListItem, ListView, Static

from claude_swap import pace
from claude_swap.json_output import USAGE_API_KEY
from claude_swap.models import AccountSnapshot
from claude_swap.reset_credit_display import ResetCreditDisplay, reset_credit_display
from claude_swap.switcher import ERROR_NOTES
from claude_swap.tui import data
from claude_swap.tui.data import PROVIDER_LABELS, PROVIDERS
from claude_swap.tui.theme import Palette
from claude_swap.usage_store import STALE_OK_S

if TYPE_CHECKING:
    from claude_swap.tui.app import CswapApp

_BAR_FILLED = "━"
_BAR_HALF = "╸"
_BAR_EMPTY = "─"
_BAR_TICK = "┃"


def bar_cells(
    pct: float | None,
    width: int,
    *,
    stale: bool = False,
    threshold: float | None = None,
    palette: Palette = Palette.DARK,
) -> Text:
    """Just the bar glyphs: severity-colored fill, track, optional tick."""
    text = Text()
    if pct is None:
        text.append(_BAR_EMPTY * width, style=palette.track)
        return text
    frac = min(max(pct, 0.0), 100.0) / 100.0
    cells = frac * width
    full = int(cells)
    half = (cells - full) >= 0.5 and full < width
    tick_at: int | None = None
    if threshold is not None:
        tick_at = min(width - 1, max(0, round(threshold / 100.0 * width)))
    color = palette.severity(pct)
    fill_style = f"{color} dim" if stale else color
    for i in range(width):
        if tick_at is not None and i == tick_at:
            text.append(_BAR_TICK, style=palette.sev_warn)
        elif i < full:
            text.append(_BAR_FILLED, style=fill_style)
        elif i == full and half:
            text.append(_BAR_HALF, style=fill_style)
        else:
            text.append(_BAR_EMPTY, style=palette.track)
    return text


def usage_bar(
    label: str,
    pct: float | None,
    suffix: str | None,
    width: int,
    *,
    stale: bool = False,
    threshold: float | None = None,
    palette: Palette = Palette.DARK,
) -> Text:
    """One full bar line: ``5h ━━━━╸────┃──  47%  resets 2h 13m · 20:39``."""
    text = Text()
    text.append(f"{label} ", style=palette.muted)
    text.append(
        bar_cells(pct, width, stale=stale, threshold=threshold, palette=palette)
    )
    if pct is None:
        text.append("  usage unknown", style=palette.muted)
    else:
        color = palette.severity(pct)
        text.append(f" {pct:3.0f}%", style=f"{color} dim" if stale else color)
    if suffix:
        text.append(f"  {suffix}", style=palette.muted)
    return text


def _reset_parts(
    window: dict, now: float, *, tr: Callable[[str], str] = str
) -> tuple[str | None, str | None]:
    """Countdown suffix and its clock-extended variant for one window.

    ``("resets 2h 13m", "resets 2h 13m · 20:39")`` — the second form is what
    a row shows when it has the width for it. Equal when no clock is known.
    """
    reset = data.reset_text(window, now, tr=tr)
    if not reset:
        return None, None
    clock = data.reset_clock(window, now)
    return reset, f"{reset} · {clock}" if clock else reset


def _pace_suffix(
    window: dict, fetched_at: float | None, *, tr: Callable[[str], str] = str
) -> str:
    """ "(ahead of pace)" when a weekly window is meaningfully ahead, else ""."""
    result = pace.compute_pace(window, fetched_at=fetched_at)
    return tr("(ahead of pace)") if result and result.ahead else ""


def usage_rows(
    last_good: dict | None,
    now: float,
    fetched_at: float | None = None,
    *,
    tr: Callable[[str], str] = str,
) -> list[tuple[str, float, str, str]]:
    """(label, pct, suffix, suffix_full) rows mirroring the CLI's
    ``_format_usage_lines``.

    ``suffix_full`` extends the reset countdown with the absolute clock time
    (``resets 2h 13m · 20:39``) for rows that have room; otherwise it equals
    ``suffix``. Only windows the account actually has produce a row — an
    annual plan without a 7-day window simply has no 7d line. Order matches
    the CLI: spend, the provider's quota windows (5h, 7d, Weekly), then
    per-model scoped windows (e.g. "Fable"), the latter marked ``(!)`` at/over
    their limit. The weekly (7d/Weekly) and scoped rows also carry an
    "(ahead of pace)" marker when meaningfully ahead of the week's expected
    usage (issue #125) — never the 5h row.
    """
    if not isinstance(last_good, dict):
        return []
    rows: list[tuple[str, float, str, str]] = []
    spend = last_good.get("spend")
    if spend:
        amounts = f"${spend['used']:,.2f} / ${spend['limit']:,.2f}"
        reset, reset_full = _reset_parts(spend, now, tr=tr)
        suffix = f"{reset}  {amounts}" if reset else amounts
        suffix_full = f"{reset_full}  {amounts}" if reset_full else amounts
        rows.append(("$$", float(spend["pct"]), suffix, suffix_full))
    for key, label in (
        ("five_hour", "5h"),
        ("seven_day", "7d"),
        ("weekly", "Weekly"),
    ):
        window = last_good.get(key)
        if window:
            reset, reset_full = _reset_parts(window, now, tr=tr)
            suffix, suffix_full = reset or "", reset_full or ""
            if key != "five_hour":
                marker = _pace_suffix(window, fetched_at, tr=tr)
                if marker:
                    suffix = f"{suffix}  {marker}" if suffix else marker
                    suffix_full = f"{suffix_full}  {marker}" if suffix_full else marker
            rows.append(
                (
                    label,
                    float(window["pct"]),
                    suffix,
                    suffix_full,
                )
            )
    for window in last_good.get("scoped") or []:
        pct = float(window["pct"])
        suffix, suffix_full = _reset_parts(window, now, tr=tr)
        suffix, suffix_full = suffix or "", suffix_full or ""
        if pct >= 100:
            suffix = f"{suffix}  (!)" if suffix else "(!)"
            suffix_full = f"{suffix_full}  (!)" if suffix_full else "(!)"
        else:
            marker = _pace_suffix(window, fetched_at, tr=tr)
            if marker:
                suffix = f"{suffix}  {marker}" if suffix else marker
                suffix_full = f"{suffix_full}  {marker}" if suffix_full else marker
        rows.append((window["name"], pct, suffix, suffix_full))
    return rows


def reset_credits_text(last_good: dict | None, now: float) -> str | None:
    """Compact Codex banked-reset count and earliest local expiry."""
    display = reset_credit_display(last_good, now)
    return display.text if display else None


def _reset_style(display: ResetCreditDisplay, palette: Palette) -> str:
    if display.severity in {"critical", "expired"}:
        return f"bold {palette.sev_crit}"
    return f"bold {palette.sev_warn}"


def credits_text(
    last_good: dict | None,
    now: float,
    *,
    compact: bool = False,
    tr: Callable[[str], str] = str,
) -> str | None:
    """Codex workspace/purchased credit balance."""
    del now
    if not isinstance(last_good, dict):
        return None
    credits = last_good.get("credits")
    if not isinstance(credits, dict):
        return None
    if credits.get("limit_reached") is True:
        return tr("limit reached")
    if credits.get("unlimited") is True:
        return tr("unlimited")
    if credits.get("has_credits") is False:
        return tr("none")

    balance = credits.get("balance")
    if isinstance(balance, (int, float)):
        return tr("{balance:,.2f} available").format(balance=balance)
    if credits.get("has_credits") is True:
        return tr("available") if compact else tr("available · balance not reported")
    return None


def credit_allowance_text(
    last_good: dict | None,
    now: float,
    *,
    compact: bool = False,
    tr: Callable[[str], str] = str,
) -> str | None:
    """Per-user monthly workspace allowance, separate from credit balance."""
    if not isinstance(last_good, dict):
        return None
    allowance = last_good.get("credit_allowance")
    if not isinstance(allowance, dict):
        return None

    remaining = allowance.get("remaining")
    limit = allowance.get("limit")
    if allowance.get("limit_reached") is True:
        text = tr("limit reached")
    elif isinstance(remaining, (int, float)):
        text = tr("{remaining:,.2f} remaining").format(remaining=remaining)
    elif isinstance(limit, (int, float)):
        text = tr("{limit:,.2f} limit").format(limit=limit)
    else:
        return None
    if (
        not compact
        and isinstance(limit, (int, float))
        and isinstance(remaining, (int, float))
    ):
        text += tr(" · {limit:,.2f} limit").format(limit=limit)
    reset = data.reset_text({"resets_at": allowance.get("resets_at")}, now, tr=tr)
    if reset and not compact:
        text += f" · {reset}"
    return text


def _identity(acc: AccountSnapshot, *, mask: bool) -> tuple[str, str, str]:
    """Alias, email, and org tag as they should be drawn."""
    return (
        data.present_alias(acc.alias, mask=mask),
        data.present_email(acc.email, mask=mask),
        data.present_tag(acc.display_tag, mask=mask),
    )


def account_card_text(
    acc: AccountSnapshot,
    width: int,
    *,
    threshold: float | None = None,
    threshold_for: Callable[[str], float | None] | None = None,
    tag: str | None = None,
    now: float | None = None,
    palette: Palette = Palette.DARK,
    mask: bool = False,
    tr: Callable[[str], str] = str,
) -> Text:
    """The full account card: header line + per-window bar rows."""
    now = now if now is not None else time.time()
    alias, email, org = _identity(acc, mask=mask)

    text = Text()
    text.append(f"{acc.number:>2}  ", style=f"bold {palette.foreground}")
    if alias:
        text.append(alias, style=f"bold {palette.accent}")
        text.append(f" ({email})", style=palette.foreground)
    else:
        text.append(email, style=palette.foreground)
    text.append(f"  [{org}]", style=palette.muted)
    if tag:
        text.append(f"  {tag}", style=palette.muted)
    if acc.is_active:
        text.append(tr("   ● active"), style=f"bold {palette.accent}")
    if acc.disabled:
        text.append(tr("   (disabled)"), style=palette.muted)
    age = data.format_age(acc.usage.age_s, tr=tr)
    if age:
        text.append(f"   {age}", style=palette.muted)

    reset_display = reset_credit_display(acc.usage.last_good, now)
    if reset_display and reset_display.urgent:
        text.append("\n    ⚠ " + tr("Resets "), style=_reset_style(reset_display, palette))
        text.append(reset_display.text, style=_reset_style(reset_display, palette))
        if acc.usage.sentinel or acc.usage.last_error:
            text.append(" · last known", style=palette.muted)

    sentinel = acc.usage.sentinel
    if sentinel is not None:
        text.append("\n    ")
        style = palette.muted if sentinel == USAGE_API_KEY else palette.sev_warn
        marker = "·" if sentinel == USAGE_API_KEY else "⚠"
        text.append(f"{marker} {data.sentinel_label(sentinel)}", style=style)
        # Same supplementary line `cswap list` prints: the last good
        # measurement behind the sentinel (API-key accounts have no quota to
        # have "seen").
        if sentinel != USAGE_API_KEY:
            last_seen = data.last_seen_note(acc.usage)
            if last_seen is not None:
                text.append("\n    ")
                text.append(f"└ {last_seen}", style=palette.muted)
        return text

    rows = usage_rows(acc.usage.last_good, now, acc.usage.fetched_at, tr=tr)
    credits = credits_text(acc.usage.last_good, now, tr=tr)
    allowance = credit_allowance_text(acc.usage.last_good, now, tr=tr)
    resets = reset_display.text if reset_display else None
    if not rows and not credits and not allowance and not resets:
        text.append("\n    ")
        text.append(tr("usage unavailable"), style=palette.muted)
        if acc.usage.last_error:
            # Same wording as the CLI detail line: error KINDS with a
            # friendly note render it, so both surfaces describe the state
            # identically.
            note = ERROR_NOTES.get(acc.usage.last_error, acc.usage.last_error)
            text.append(f" · {note}", style=palette.muted)
        return text

    if rows:
        stale = acc.usage.age_s is not None and acc.usage.age_s > STALE_OK_S
        display_labels = {
            label: tr("Weekly") if label == "Weekly" else label
            for label, _pct, _suffix, _full in rows
        }
        label_width = max(len(label) for label in display_labels.values())
        bar_width = max(12, min(30, width - 42 - label_width))
        # everything on a row except the suffix: indent, label, bar, " NNN%", gap
        row_overhead = 4 + label_width + 1 + bar_width + 5 + 2
        for label, pct, suffix, suffix_full in rows:
            # per-row: show the absolute clock only where it fits, so a long
            # spend row degrading doesn't cost the 5h/7d rows their clocks
            if suffix_full != suffix and row_overhead + len(suffix_full) <= width:
                suffix = suffix_full
            text.append("\n    ")
            row_threshold = (
                threshold_for(label) if threshold_for is not None else threshold
            )
            text.append(
                usage_bar(
                    f"{display_labels[label]:<{label_width}}",
                    pct,
                    suffix or None,
                    bar_width,
                    stale=stale,
                    threshold=row_threshold,
                    palette=palette,
                )
            )
    if credits:
        text.append("\n    ")
        text.append(tr("Credits "), style=palette.muted)
        text.append(credits, style=palette.foreground)
    if allowance:
        text.append("\n    ")
        text.append(tr("Allowance "), style=palette.muted)
        text.append(allowance, style=palette.foreground)
    if resets and not (reset_display and reset_display.urgent):
        text.append("\n    ")
        text.append(tr("Resets "), style=palette.muted)
        text.append(resets, style=palette.foreground)
    return text


def mini_account_text(
    acc: AccountSnapshot,
    now: float,
    *,
    palette: Palette = Palette.DARK,
    mask: bool = False,
    tr: Callable[[str], str] = str,
) -> Text:
    """One minimized line for an inactive account.

    ``2  work@acme.dev [personal]   5h 92% · 7d 63%`` — pcts only, severity
    colored; a window at/over 100% brings its reset countdown along, and a
    maxed per-model window shows as ``Fable (!)``. Sentinel states show
    their label instead.
    """
    alias, email, org = _identity(acc, mask=mask)
    text = Text(no_wrap=True, overflow="ellipsis")
    text.append(f"{acc.number:>2}  ", style=f"bold {palette.muted}")
    reset_display = reset_credit_display(acc.usage.last_good, now)
    if reset_display and reset_display.urgent:
        text.no_wrap = False
        text.overflow = "fold"
        text.append("⚠ " + tr("Resets {resets}").format(resets=reset_display.text) + "\n    ", style=_reset_style(reset_display, palette))
        if acc.usage.sentinel or acc.usage.last_error:
            text.append("last known · ", style=palette.muted)
    if alias:
        text.append(alias, style=f"bold {palette.accent}")
        text.append(f" ({email})", style=palette.foreground)
    else:
        text.append(email, style=palette.foreground)
    text.append(f"  [{org}]", style=palette.muted)
    if acc.disabled:
        text.append(tr("  (disabled)"), style=palette.muted)
    text.append("   ")

    sentinel = acc.usage.sentinel
    if sentinel is not None:
        style = palette.muted if sentinel == USAGE_API_KEY else palette.sev_warn
        text.append(data.sentinel_label(sentinel), style=style)
        return text

    last_good = acc.usage.last_good
    fetched_at = acc.usage.fetched_at
    stale = acc.usage.age_s is not None and acc.usage.age_s > STALE_OK_S
    parts = 0
    for key, label in (
        ("five_hour", "5h"),
        ("seven_day", "7d"),
        ("weekly", "Weekly"),
    ):
        window = last_good.get(key) if isinstance(last_good, dict) else None
        if not window:
            continue
        pct = float(window["pct"])
        if parts:
            text.append(" · ", style=palette.track)
        color = palette.severity(pct)
        display_label = tr("Weekly") if label == "Weekly" else label
        text.append(f"{display_label} ", style=palette.muted)
        text.append(f"{pct:.0f}%", style=f"{color} dim" if stale else color)
        if pct >= 100:
            reset = data.reset_text(window, now, tr=tr)
            if reset:
                text.append(f" ({reset})", style=palette.muted)
        elif key == "seven_day":
            result = pace.compute_pace(window, fetched_at=fetched_at)
            if result and result.ahead:
                text.append(tr(" (ahead)"), style=palette.sev_warn)
        parts += 1
    maxed = [
        w["name"]
        for w in (last_good.get("scoped") or [] if isinstance(last_good, dict) else [])
        if float(w["pct"]) >= 100
    ]
    for name in maxed:
        if parts:
            text.append(" · ", style=palette.track)
        text.append(f"{name} (!)", style=palette.sev_crit)
        parts += 1
    credits = credits_text(last_good, now, compact=True, tr=tr)
    if credits:
        if parts:
            text.append(" · ", style=palette.track)
        text.append(
            tr("Credits {credits}").format(credits=credits), style=palette.muted
        )
        parts += 1
    allowance = credit_allowance_text(last_good, now, compact=True, tr=tr)
    if allowance:
        if parts:
            text.append(" · ", style=palette.track)
        text.append(
            tr("Allowance {allowance}").format(allowance=allowance), style=palette.muted
        )
        parts += 1
    resets = reset_display.text if reset_display else None
    if resets and not (reset_display and reset_display.urgent):
        if parts:
            text.append(" · ", style=palette.track)
        text.append(tr("Resets {resets}").format(resets=resets), style=palette.muted)
        parts += 1
    if not parts and not (reset_display and reset_display.urgent):
        text.append(tr("usage unknown"), style=palette.muted)
    return text


def section_header_text(label: str, width: int, *, palette: Palette) -> Text:
    """Muted provider label followed by a width-clamped horizontal rule."""
    text = Text()
    text.append(label, style=palette.muted)
    rule_width = max(0, width - len(label) - 1)
    if rule_width:
        text.append(" ")
        text.append("─" * rule_width, style=palette.track)
    return text


class AccountsPanel(Static):
    """Static account overview: the active account full-size, others as
    one-line minis (in slot order, expanded in place). The dashboard's — and
    with ``show_minis=False`` the auto screen's — always-visible monitor."""

    def __init__(
        self,
        *,
        show_minis: bool = True,
        provider: str | None = None,
        id: str | None = None,
    ) -> None:
        super().__init__(id=id)
        self._show_minis = show_minis
        self.provider = provider

    def on_mount(self) -> None:
        self.watch(self.app, "snapshots", lambda _snap: self.refresh(layout=True))
        self.watch(self.app, "theme", lambda _t: self.refresh(layout=True))
        self.watch(self.app, "mask_accounts", lambda _on: self.refresh(layout=True))
        self.set_interval(30.0, lambda: self.refresh(layout=True))

    def render(self) -> Text:
        app: "CswapApp" = self.app  # type: ignore[assignment]
        palette = Palette.from_theme(app.current_theme)
        providers = (self.provider,) if self.provider is not None else PROVIDERS
        if all(
            app.snapshots[provider] is not None and not app.snapshots[provider].accounts
            for provider in providers
        ):
            return Text(
                self.app.gettext(
                    "No managed accounts yet.\nUse the menu below to add a Claude Code or Codex login."
                ),
                style=palette.muted,
            )
        now = time.time()
        width = (self.size.width or 80) - 2
        text = Text()
        rendered_sections = 0
        for provider in providers:
            snap = app.snapshots[provider]
            error = app._last_refresh_error[provider]
            # A configured provider with zero accounts stays out of the way;
            # the Add-account menu remains the discovery path.
            if snap is not None and not snap.accounts:
                continue
            if rendered_sections:
                text.append("\n\n")
            label = PROVIDER_LABELS[provider]
            text.append(section_header_text(label, width, palette=palette))
            text.append("\n")
            if snap is None:
                text.append(error or self.app.gettext("loading…"), style=palette.muted)
                rendered_sections += 1
                continue

            blocks: list[Text] = []
            for acc in snap.accounts:
                if acc.is_active:
                    blocks.append(
                        account_card_text(
                            acc,
                            width,
                            threshold_for=app.bar_threshold,
                            now=now,
                            palette=palette,
                            mask=app.mask_accounts,
                            tr=app.gettext,
                        )
                    )
                elif self._show_minis:
                    blocks.append(
                        mini_account_text(
                            acc,
                            now,
                            palette=palette,
                            mask=app.mask_accounts,
                            tr=app.gettext,
                        )
                    )
            if not blocks:
                text.append(
                    self.app.gettext("no active managed login"), style=palette.muted
                )
            previous_multiline = False
            for i, block in enumerate(blocks):
                multiline = "\n" in block.plain
                if i:
                    text.append("\n\n" if (multiline or previous_multiline) else "\n")
                text.append(block)
                previous_multiline = multiline
            rendered_sections += 1
        return text


class AccountCard(Static):
    """One account rendered full-size (used by the switch screen's list)."""

    def __init__(
        self,
        acc: AccountSnapshot,
        *,
        threshold: float | None = None,
        tag: str | None = None,
    ) -> None:
        super().__init__()
        self._acc = acc
        self._threshold = threshold
        self._tag = tag

    def set_account(self, acc: AccountSnapshot) -> None:
        self._acc = acc
        self.refresh(layout=True)

    def on_mount(self) -> None:
        self.watch(self.app, "mask_accounts", lambda _on: self.refresh(layout=True))
        self.set_interval(30.0, lambda: self.refresh(layout=True))

    def render(self) -> Text:
        return account_card_text(
            self._acc,
            self.size.width or 80,
            threshold=self._threshold,
            tag=self._tag,
            palette=Palette.from_theme(self.app.current_theme),
            mask=bool(getattr(self.app, "mask_accounts", False)),
            tr=self.app.gettext,
        )


class AccountItem(ListItem):
    """ListView row wrapping an :class:`AccountCard`; remembers its slot."""

    def __init__(self, acc: AccountSnapshot, provider: str) -> None:
        super().__init__(AccountCard(acc, tag=PROVIDER_LABELS[provider]))
        self.provider = provider
        self.number = acc.number
        self.email = acc.email

    def set_account(self, acc: AccountSnapshot) -> None:
        self.number = acc.number
        self.email = acc.email
        self.query_one(AccountCard).set_account(acc)


class ProviderDivider(ListItem):
    """A non-selectable provider heading inside an account list."""

    def __init__(self, provider: str) -> None:
        super().__init__(disabled=True)
        self.provider = provider

    def render(self) -> Text:
        return section_header_text(
            PROVIDER_LABELS[self.provider],
            self.size.width or 80,
            palette=Palette.from_theme(self.app.current_theme),
        )


class CyclingListView(ListView):
    """A ListView whose cursor wraps at both ends."""

    def _selectable_from(self, start: int, step: int) -> int | None:
        """Find the next enabled child while walking circularly from ``start``."""
        for offset in range(len(self.children)):
            index = (start + offset * step) % len(self.children)
            if not self.children[index].disabled:
                return index
        return None

    def action_cursor_up(self) -> None:
        if not self.children:
            return
        start = len(self.children) - 1 if self.index is None else self.index - 1
        # ListView's stock one-way movement skips disabled rows, but wrapping
        # here bypasses it; provider headings must never become a cursor stop.
        self.index = self._selectable_from(start, -1)

    def action_cursor_down(self) -> None:
        if not self.children:
            return
        start = 0 if self.index is None else self.index + 1
        self.index = self._selectable_from(start, 1)


class MenuItem(ListItem):
    """One menu row: a label plus an action id the screen dispatches on."""

    def __init__(
        self, label: str, action_id: str, *, muted: bool = False, help_text: str | None = None
    ) -> None:
        item = Static(label, markup=False)
        if muted:
            item.add_class("menu-item-muted")
        super().__init__(item)
        self.action_id = action_id
        self.tooltip = Text(help_text) if help_text else None
