"""Brand mark geometry (pure) and the AppKit glyph."""

import math
import subprocess
import sys

import pytest

from ccswap import brand


def test_mark_segments_form_a_closed_loop_with_meeting_tips():
    start, segs = brand.mark_segments()
    assert len(segs) == 16 and all(len(s) == 3 for s in segs)
    assert segs[-1][2] == start  # closed
    tip = segs[len(segs) // 2 - 1][2]  # where the left C ends...
    assert tip[0] == start[0] and tip[1] < start[1]  # ...above the start tip, same x
    xs = [p[0] for s in segs for p in s]
    ys = [p[1] for s in segs for p in s]
    assert 0 < min(xs) and max(xs) < brand.BOX and 0 < min(ys) and max(ys) < brand.BOX


def test_cubic_length_of_a_straight_cubic():
    assert math.isclose(brand._cubic_length((0, 0), (1, 0), (2, 0), (3, 0)), 3.0)


def test_import_does_not_load_appkit():
    code = "import sys, ccswap.brand; sys.exit('AppKit' in sys.modules)"
    assert subprocess.run([sys.executable, "-c", code]).returncode == 0


@pytest.mark.parametrize("pct", [None, 0, 50, 100, 140])
def test_glyph_image_is_a_template_of_the_requested_size(pct):
    pytest.importorskip("AppKit")
    img = brand.glyph_image(pct, size=22.0)
    assert img.isTemplate()
    assert (img.size().width, img.size().height) == (22.0, 22.0)
