"""Brand mark geometry (pure) and the AppKit glyph."""

import math
import subprocess
import sys

import pytest
from pathlib import Path

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


@pytest.mark.parametrize("pid", ["claude", "codex", "agy"])
def test_status_image_adds_the_provider_logo_in_colour(pid):
    pytest.importorskip("AppKit")
    plain = brand.glyph_image(40, 18)
    img = brand.status_image(40, pid, 18)
    assert not img.isTemplate() and img.size()[0] > plain.size()[0]  # glyph plus the logo beside it
    assert brand.provider_logo(pid) is not None


def test_provider_logos_are_the_pinned_official_files():
    # docs/provider-logos.md lists each file's source; a changed or added file must update that table
    import hashlib
    doc = (Path(brand.__file__).parents[3] / "docs" / "provider-logos.md").read_text()
    files = sorted(brand._LOGO_DIR.iterdir())
    assert {name for name, _ in brand._PROVIDER_LOGOS.values()} <= {f.name for f in files}
    for f in files:
        assert f"`{f.name}`" in doc and hashlib.sha256(f.read_bytes()).hexdigest() in doc, f.name


def test_status_image_without_a_known_provider_is_the_glyph():
    pytest.importorskip("AppKit")
    assert brand.status_image(40, None, 18).size()[0] == 18 and brand.status_image(40, None, 18).isTemplate()
    assert brand.status_image(40, "unknown", 18).size()[0] == 18


def test_glyph_fill_runs_along_the_whole_loop():
    pytest.importorskip("AppKit")
    from AppKit import NSBitmapImageRep

    def alpha_at_right_edge(pct):
        img = brand.glyph_image(pct, size=brand.BOX)  # 1 point per mark unit
        rep = NSBitmapImageRep.imageRepWithData_(img.TIFFRepresentation())
        sx = rep.pixelsWide() / brand.BOX
        # the loop's rightmost point (98.7, 56.7), inside the stroke; bitmap rows count from the top
        return rep.colorAtX_y_(int((98.748 - 3) * sx), int(56.7 * sx)).alphaComponent()

    assert alpha_at_right_edge(40) < 0.6   # 40% only reaches the left C: track here
    assert alpha_at_right_edge(100) > 0.9  # a full window closes the loop
