"""ccswap brand: the Swap CC mark: two Cs joined into one loop, a mint track at 40% with 30% of it filled in flat near-white, on the flat Relay green tile.

``mark.svg`` (dark backgrounds) and ``mark-light.svg`` (light backgrounds) are cropped to
the loop; ``mark-mono.svg`` is the loop geometry for the template glyph, in its square
113.4 box; ``icon.svg`` is the 1024 app icon. Drawn from the Figma "Recommendation: F" board.
"""

from __future__ import annotations

import math
import re
from pathlib import Path

BRAND_DIR = Path(__file__).parent
MARK_SVG = BRAND_DIR / "mark.svg"
MARK_MONO_SVG = BRAND_DIR / "mark-mono.svg"
ICON_SVG = BRAND_DIR / "icon.svg"

# Geometry of the mark in its 113.4 x 113.4 box (the Figma frame).
BOX = 113.4
STROKE_WIDTH = 11.907


def mark_segments() -> tuple[tuple[float, float], list[tuple[tuple[float, float], ...]]]:
    """The loop of ``mark-mono.svg`` as (start point, cubic segments), no AppKit.

    Each segment is ``(c1, c2, end)`` in SVG coordinates (y down). The path is
    closed: the last segment ends at the start. The first half of the segments
    is the left C, the second half the right C.
    """
    d = re.search(r' d="([^"]+)"', MARK_MONO_SVG.read_text(encoding="utf-8")).group(1)
    nums = [float(n) for n in re.findall(r"-?\d+(?:\.\d+)?", d)]
    start = (nums[0], nums[1])
    pts = list(zip(nums[2::2], nums[3::2]))
    return start, [tuple(pts[i:i + 3]) for i in range(0, len(pts), 3)]


def _cubic_length(p0, c1, c2, p3, steps: int = 32) -> float:
    total, prev = 0.0, p0
    for i in range(1, steps + 1):
        t = i / steps
        u = 1 - t
        pt = tuple(
            u**3 * a + 3 * u * u * t * b + 3 * u * t * t * c + t**3 * d
            for a, b, c, d in zip(p0, c1, c2, p3)
        )
        total += math.dist(prev, pt)
        prev = pt
    return total


def glyph_image(used_pct: float | None, size: float = 18.0):
    """The live menu bar glyph as an NSImage template.

    The loop drawn at 40% alpha as the track; the left C (the active
    account) drawn solid along ``used_pct`` percent of its length. None draws
    the plain mark. AppKit is imported inside, so this module imports anywhere.
    """
    from AppKit import NSAffineTransform, NSBezierPath, NSColor, NSImage

    start, segs = mark_segments()
    scale = size / BOX

    def path(segments, begin, close=False):
        bp = NSBezierPath.bezierPath()
        bp.moveToPoint_((begin[0], BOX - begin[1]))  # AppKit's y axis points up
        for c1, c2, end in segments:
            bp.curveToPoint_controlPoint1_controlPoint2_(
                (end[0], BOX - end[1]), (c1[0], BOX - c1[1]), (c2[0], BOX - c2[1])
            )
        if close:
            bp.closePath()
        bp.setLineWidth_(STROKE_WIDTH)
        bp.setLineJoinStyle_(1)  # round
        return bp

    half = len(segs) // 2
    loop, left = path(segs, start, close=True), path(segs[:half], start)
    ends = [start, *(s[2] for s in segs[:half])]
    total = sum(_cubic_length(a, *s) for a, s in zip(ends, segs[:half]))

    img = NSImage.alloc().initWithSize_((size, size))
    img.lockFocus()
    try:
        xf = NSAffineTransform.transform()
        xf.scaleBy_(scale)
        xf.concat()
        if used_pct is None:
            NSColor.blackColor().set()
            loop.stroke()
        else:
            NSColor.colorWithCalibratedWhite_alpha_(0.0, 0.40).set()
            loop.stroke()
            used = total * max(0.0, min(float(used_pct), 100.0)) / 100
            if used > 0:
                NSColor.blackColor().set()
                left.setLineDash_count_phase_([used, total + 1], 2, 0.0)
                left.stroke()
    finally:
        img.unlockFocus()
    img.setTemplate_(True)
    return img


# Provider logos from each company's own published files (sources and hashes in
# docs/provider-logos.md), one file per provider for both light and dark menu bars.
# The scale makes up for transparent padding in the official files, which stay unmodified.
_PROVIDER_LOGOS = {
    "claude": ("claude.svg", 1.0),
    "codex": ("codex.png", 1.15),
    "agy": ("antigravity.png", 1.38),
}
_LOGO_DIR = BRAND_DIR.parent / "menubar_web" / "logos"


def provider_logo(provider_id: str, size: float = 14.0):
    """The provider's logo in its own colours, drawn about ``size`` points across, or None."""
    entry = _PROVIDER_LOGOS.get(provider_id)
    if entry is None:
        return None
    from AppKit import NSImage

    img = NSImage.alloc().initWithContentsOfFile_(str(_LOGO_DIR / entry[0]))
    if img is None:
        return None
    w, h = img.size()
    side = size * entry[1]
    img.setSize_((side * w / max(w, h), side * h / max(w, h)))
    return img


def status_image(used_pct: float | None, provider_id: str | None = None, size: float = 18.0):
    """The ccswap glyph, followed by the title provider's logo in colour when there is one.

    Without a logo this is the plain template glyph. With one, the image draws itself on every
    paint, with the glyph in the menu bar's text colour, so a light/dark switch needs no
    repaint from us. It does not invert while the item is clicked.
    """
    from AppKit import NSAppearance, NSColor, NSCompositingOperationSourceAtop, NSImage, NSRectFillUsingOperation

    glyph = glyph_image(used_pct, size)
    if provider_id not in _PROVIDER_LOGOS:
        return glyph
    slot = size * 0.78
    logo = provider_logo(provider_id, slot)
    if logo is None:
        return glyph

    def tinted(white: float):  # the template glyph in the menu bar's text colour, drawn offscreen
        img = NSImage.alloc().initWithSize_((size, size))
        img.lockFocus()
        try:
            glyph.drawInRect_(((0, 0), (size, size)))
            NSColor.colorWithWhite_alpha_(white, 0.85).set()
            NSRectFillUsingOperation(((0, 0), (size, size)), NSCompositingOperationSourceAtop)
        finally:
            img.unlockFocus()
        return img

    glyphs = {False: tinted(0.0), True: tinted(1.0)}
    gap = size * 0.28
    lw, lh = logo.size()

    def draw(_rect):
        dark = NSAppearance.currentDrawingAppearance().bestMatchFromAppearancesWithNames_(["NSAppearanceNameAqua", "NSAppearanceNameDarkAqua"]) == "NSAppearanceNameDarkAqua"
        glyphs[dark].drawInRect_(((0, 0), (size, size)))
        # centred in its slot; a scaled-up logo only spills its own transparent padding
        logo.drawInRect_(((size + gap + (slot - lw) / 2, (size - lh) / 2), (lw, lh)))
        return True

    return NSImage.imageWithSize_flipped_drawingHandler_((size + gap + slot, size), False, draw)
