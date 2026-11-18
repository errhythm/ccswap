"""ccswap brand: the Swap CC mark (two Cs joined into one loop).

``mark.svg`` (white → coral blend), ``mark-mono.svg`` (template glyph) and
``icon.svg`` (1024 app icon on the Dusk tile) are the source files, drawn from
the Figma "Swap CC · seamless join" board. Contract for the menu bar shells
(menu bar v2 phase); ``glyph_image`` is implemented by the brand implementer.
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

    The loop drawn at about 35% alpha as the track; the left C (the active
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
            NSColor.colorWithCalibratedWhite_alpha_(0.0, 0.35).set()
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
