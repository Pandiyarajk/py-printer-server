"""Tests for image normalisation before printing.

Author: Pandiyaraj Karuppasamy
Date: Sep-23-2026

Each of these guards a failure measured against Pillow's ImageWin.Dib, which is
what actually blits onto the printer DC. All three would reach the user as a
ruined sheet rather than an exception.
"""

from __future__ import annotations

import sys

import pytest

pytest.importorskip("PIL")
from PIL import Image  # noqa: E402

pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="render imports winspool, which is Windows-only"
)


def _to_printable(*args, **kwargs):
    from py_printer_server.render import to_printable

    return to_printable(*args, **kwargs)


class TestNormalisation:
    def test_palette_images_are_converted(self):
        """ImageWin.Dib raises ValueError on a P-mode image. Palette GIFs and
        PNGs are routine phone and web content, so without this the print
        worker takes an exception on an ordinary file."""
        assert _to_printable(Image.new("P", (8, 8)), colour=True).mode == "RGB"

    def test_transparency_is_flattened_onto_white(self):
        """RGBA.convert("RGB") drops alpha WITHOUT compositing, so a fully
        transparent pixel becomes black. A logo with a transparent background
        would print as a solid black rectangle."""
        transparent = Image.new("RGBA", (4, 4), (0, 0, 0, 0))
        assert _to_printable(transparent, colour=True).getpixel((0, 0)) == (255, 255, 255)

    def test_mono_produces_greyscale(self):
        out = _to_printable(Image.new("RGB", (4, 4), (255, 0, 0)), colour=False)
        assert out.mode == "L"

    def test_mono_uses_luminance_not_a_channel_average(self):
        """A flat average would give 85 for pure red; luminance gives 76."""
        red = _to_printable(Image.new("RGB", (4, 4), (255, 0, 0)), colour=False)
        green = _to_printable(Image.new("RGB", (4, 4), (0, 255, 0)), colour=False)
        assert red.getpixel((0, 0)) == 76
        assert green.getpixel((0, 0)) == 150

    def test_colour_request_stays_rgb(self):
        assert _to_printable(Image.new("RGB", (4, 4), (10, 20, 30)), colour=True).mode == "RGB"


class TestResampling:
    def test_oversized_images_are_downscaled_here_not_by_gdi(self):
        """GDI's default stretch mode is BLACKONWHITE, which DISCARDS pixels
        when shrinking instead of averaging them, and Pillow's Dib.draw never
        calls SetStretchBltMode. A 12MP photo left for GDI to shrink prints as
        noise, so the bitmap must never be larger than its destination."""
        from py_printer_server.render import _resample_for

        out = _resample_for(Image.new("RGB", (6000, 4500)), 4821, 3616)
        assert out.size == (4821, 3616)

    def test_smaller_images_are_left_for_gdi_to_enlarge(self):
        """Upscaling discards nothing, so there is no reason to spend memory
        doing it here."""
        from py_printer_server.render import _resample_for

        out = _resample_for(Image.new("RGB", (400, 300)), 4821, 3616)
        assert out.size == (400, 300)
