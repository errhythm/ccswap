"""The "cswap-dark" / "cswap-light" Textual themes and shared color constants.

The dark theme uses the Figma design's tokens: near-black background, one
terracotta accent (also Claude's provider colour), a colour per provider, and a
green/amber/red severity ramp. The light theme keeps the same roles, tuned for a
warm near-white base.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from textual.theme import Theme

# Core palette: the Figma "TUI" page tokens (file VBQgujeuhCWkriwLE9ouzm,
# frame 18:2). Widgets import these for Rich renderables; the Theme below maps
# them onto Textual's design tokens.
ACCENT = "#e07a5f"  # brand terracotta, also Claude's provider colour
FOREGROUND = "#e6e3de"  # text
MUTED = "#8c8984"  # secondary text
DIM = "#55524e"  # tertiary text
BACKGROUND = "#121212"
SURFACE = "#1a1918"  # highlighted row
PANEL = "#262422"
LINE = "#3a3734"  # panel borders, header rule, gauge track
CODEX = "#d6d3ce"
ANTIGRAVITY = "#8ab4f8"
MARK = "#9BE0B6"  # the brand mark's mint (src/ccswap/brand/mark.svg)

# Usage severity ramp.
SEV_OK = "#8cbf88"  # < 70%
SEV_WARN = "#e2b45a"  # 70-89%
SEV_CRIT = "#e8655a"  # >= 90%
TRACK = LINE  # unfilled gauge track

# Severity band edges. WARN mirrors where a user starts caring; CRIT mirrors
# the auto-switch default threshold so bar color and switch behavior agree.
WARN_PCT = 70.0
CRIT_PCT = 90.0


@dataclass(frozen=True)
class Palette:
    """Resolved colors for Rich renderables, keyed to a Textual theme.

    Rich renderables bake color into styles at render time, so they can't read
    Textual's ``$variables`` the way the .tcss layer does. A Palette carries the
    active theme's colors so the same render code paints correctly in either
    theme. Resolve from the Theme object (never App.theme_variables, which lags
    the deferred CSS refresh)."""

    accent: str
    foreground: str
    muted: str
    sev_ok: str
    sev_warn: str
    sev_crit: str
    track: str
    dim: str = DIM
    line: str = LINE
    codex: str = CODEX
    antigravity: str = ANTIGRAVITY
    mark: str = MARK
    hover: str = SURFACE  # block-hover-background
    cursor: str = "#3F2822"  # block-cursor-background flattened on the background

    DARK: ClassVar["Palette"]

    def severity(self, pct: float | None) -> str:
        if pct is None:
            return self.muted
        if pct >= CRIT_PCT:
            return self.sev_crit
        if pct >= WARN_PCT:
            return self.sev_warn
        return self.sev_ok

    @classmethod
    def from_theme(cls, theme: Theme) -> "Palette":
        return cls(
            accent=theme.primary,
            foreground=theme.foreground,
            muted=theme.secondary,
            sev_ok=theme.success,
            sev_warn=theme.warning,
            sev_crit=theme.error,
            track=theme.variables.get("track", TRACK),
            dim=theme.variables.get("dim", DIM),
            line=theme.variables.get("line", LINE),
            codex=theme.variables.get("codex", CODEX),
            antigravity=theme.variables.get("antigravity", ANTIGRAVITY),
            mark=theme.variables.get("mark", MARK),
            hover=theme.variables.get("block-hover-background", SURFACE),
            cursor=_flatten(theme.variables.get("block-cursor-background", SURFACE), theme.background),
        )

    def provider(self, provider: str) -> str:
        """The provider's own colour (panel titles, tab marks, active dots)."""
        return {"codex": self.codex, "agy": self.antigravity}.get(provider, self.accent)


def _flatten(color: str, background: str | None) -> str:
    """A "#rrggbb NN%" theme colour as the solid colour it paints on the
    background (Rich styles have no alpha)."""
    from textual.color import Color

    value, _space, pct = color.partition(" ")
    alpha = float(pct.rstrip("%")) / 100 if pct else 1.0
    return Color.parse(background or BACKGROUND).blend(Color.parse(value), alpha).hex


CSWAP_DARK = Theme(
    name="cswap-dark",
    primary=ACCENT,
    secondary=MUTED,
    accent=ACCENT,
    foreground=FOREGROUND,
    background=BACKGROUND,
    surface=SURFACE,
    panel=PANEL,
    success=SEV_OK,
    warning=SEV_WARN,
    error=SEV_CRIT,
    dark=True,
    variables={
        # Footer keys pick up the accent instead of the default blue.
        "footer-key-foreground": ACCENT,
        # The highlighted row: a soft full-width surface band (Figma 16:3).
        # the focused row must be obvious: an accent tint plus bold (SURFACE alone was invisible)
        "block-cursor-background": f"{ACCENT} 22%",
        "block-cursor-foreground": FOREGROUND,
        "block-cursor-text-style": "bold",
        "block-cursor-blurred-background": SURFACE,
        "block-cursor-blurred-foreground": FOREGROUND,
        "block-cursor-blurred-text-style": "none",
        "block-hover-background": SURFACE,
        "track": TRACK,
        "dim": DIM,
        "line": LINE,
        "codex": CODEX,
        "antigravity": ANTIGRAVITY,
        "mark": MARK,
        "footer-background": BACKGROUND,
        "footer-description-foreground": MUTED,
    },
)

# Light companion palette (same intent, tuned for a warm near-white base).
ACCENT_LIGHT = "#954c2a"  # burnt sienna — deepened for AA on panel (worst-case row bg)
FOREGROUND_LIGHT = "#2b2723"
MUTED_LIGHT = "#635d55"
BACKGROUND_LIGHT = "#faf7f2"
SURFACE_LIGHT = "#efeae1"
PANEL_LIGHT = "#e2dbcf"  # most-elevated = darkest (inverted from dark)
SEV_OK_LIGHT = "#3d6b3d"  # forest green — deepened for AA on panel
SEV_WARN_LIGHT = "#795911"  # deep ochre — deepened for AA on panel
SEV_CRIT_LIGHT = "#ad3128"  # brick red — deepened for AA on panel
TRACK_LIGHT = "#cec7ba"
DIM_LIGHT = "#8a8378"
LINE_LIGHT = TRACK_LIGHT
CODEX_LIGHT = "#3f3a34"
ANTIGRAVITY_LIGHT = "#2a5db0"
MARK_LIGHT = "#256b49"  # darker mint: AA (4.5:1) on background, surface and panel

CSWAP_LIGHT = Theme(
    name="cswap-light",
    primary=ACCENT_LIGHT,
    secondary=MUTED_LIGHT,
    accent=ACCENT_LIGHT,
    foreground=FOREGROUND_LIGHT,
    background=BACKGROUND_LIGHT,
    surface=SURFACE_LIGHT,
    panel=PANEL_LIGHT,
    success=SEV_OK_LIGHT,
    warning=SEV_WARN_LIGHT,
    error=SEV_CRIT_LIGHT,
    dark=False,
    variables={
        "footer-key-foreground": ACCENT_LIGHT,
        # the focused row must be obvious: an accent tint plus bold (SURFACE alone was invisible)
        "block-cursor-background": f"{ACCENT_LIGHT} 22%",
        "block-cursor-foreground": FOREGROUND_LIGHT,
        "block-cursor-text-style": "bold",
        "block-cursor-blurred-background": SURFACE_LIGHT,
        "block-cursor-blurred-foreground": FOREGROUND_LIGHT,
        "block-cursor-blurred-text-style": "none",
        "block-hover-background": SURFACE_LIGHT,
        "track": TRACK_LIGHT,
        "dim": DIM_LIGHT,
        "line": LINE_LIGHT,
        "codex": CODEX_LIGHT,
        "antigravity": ANTIGRAVITY_LIGHT,
        "mark": MARK_LIGHT,
        "footer-background": BACKGROUND_LIGHT,
        "footer-description-foreground": MUTED_LIGHT,
    },
)

Palette.DARK = Palette(
    accent=ACCENT, foreground=FOREGROUND, muted=MUTED,
    sev_ok=SEV_OK, sev_warn=SEV_WARN, sev_crit=SEV_CRIT, track=TRACK,
)
