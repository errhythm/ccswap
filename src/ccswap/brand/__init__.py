"""ccswap brand: the Swap CC mark (two Cs joined into one loop).

``mark.svg`` (white → coral blend), ``mark-mono.svg`` (template glyph) and
``icon.svg`` (1024 app icon on the Dusk tile) are the source files, drawn from
the Figma "Swap CC · seamless join" board. Contract for the menu bar shells
(menu bar v2 phase); ``glyph_image`` is implemented by the brand implementer.
"""

from __future__ import annotations

from pathlib import Path

BRAND_DIR = Path(__file__).parent
MARK_SVG = BRAND_DIR / "mark.svg"
MARK_MONO_SVG = BRAND_DIR / "mark-mono.svg"
ICON_SVG = BRAND_DIR / "icon.svg"

# Geometry of the mark in its 113.4 x 113.4 box (the Figma frame).
BOX = 113.4
STROKE_WIDTH = 11.907


def glyph_image(used_pct: float | None, size: float = 18.0):
    """The live menu bar glyph as an NSImage template.

    The loop drawn at about 35% alpha as the track; the left C (the active
    account) drawn solid along ``used_pct`` percent of its length. None draws
    the plain mark. AppKit is imported inside, so this module imports anywhere.
    """
    raise NotImplementedError
