"""Tests for the fit-to-page geometry.

Author: Pandiyaraj Karuppasamy
Date: Sep-23-2026

Pure arithmetic, so this runs anywhere. The fixture is the real geometry
measured from an HP Smart Tank 520/540 at A4/600dpi, so these tests document
the device as well as the maths.
"""

from __future__ import annotations

import pytest

from py_printer_server.pagelayout import PageGeometry, fit_image_to_page

HP_A4_600 = PageGeometry(
    phys_width=4961,
    phys_height=7016,
    offset_x=70,
    offset_y=70,
    printable_width=4821,
    printable_height=6876,
    dpi_x=600,
    dpi_y=600,
)

# An inkjet with a deeper bottom margin, which is where sheet-centring and
# printable-area-centring stop agreeing.
ASYMMETRIC = PageGeometry(
    phys_width=4961,
    phys_height=7016,
    offset_x=70,
    offset_y=70,
    printable_width=4821,
    printable_height=6546,   # 400 units lost at the bottom
    dpi_x=600,
    dpi_y=600,
)


class TestFit:
    def test_landscape_photo_on_a4(self):
        """Regression lock on the real arithmetic: a 4:3 photo fills the page
        width and is centred vertically."""
        assert fit_image_to_page(4000, 3000, HP_A4_600) == (0, 1630, 4821, 3616)

    @pytest.mark.parametrize(
        "img_w,img_h", [(4000, 3000), (3000, 4000), (1920, 1080), (1000, 1000), (6000, 1200)]
    )
    def test_aspect_ratio_is_preserved(self, img_w, img_h):
        _, _, w, h = fit_image_to_page(img_w, img_h, HP_A4_600)
        assert abs((w / h) - (img_w / img_h)) < 0.01

    @pytest.mark.parametrize(
        "img_w,img_h",
        [(4000, 3000), (3000, 4000), (100, 100), (6000, 1200), (1, 5000), (5000, 1)],
    )
    def test_never_leaves_the_printable_area(self, img_w, img_h):
        """The no-clipping guarantee. A driver crops silently rather than
        complaining, so an out-of-bounds rect would lose part of the image with
        no error anywhere."""
        for page in (HP_A4_600, ASYMMETRIC):
            x, y, w, h = fit_image_to_page(img_w, img_h, page)
            assert x >= 0 and y >= 0
            assert x + w <= page.printable_width
            assert y + h <= page.printable_height

    def test_small_images_are_scaled_up(self):
        """Fit-to-page means up as well as down. The alternative is a postage
        stamp in the middle of a sheet."""
        _, _, w, h = fit_image_to_page(100, 100, HP_A4_600)
        assert w == h == 4821

    def test_margin_shrinks_the_available_box(self):
        _, _, w, _ = fit_image_to_page(1000, 1000, HP_A4_600, margin_inch=0.5)
        # half an inch each side at 600dpi removes 600 units
        assert w == 4821 - 600

    @pytest.mark.parametrize("img_w,img_h", [(0, 100), (100, 0), (-5, 10)])
    def test_degenerate_images_raise(self, img_w, img_h):
        with pytest.raises(ValueError):
            fit_image_to_page(img_w, img_h, HP_A4_600)

    def test_asymmetric_margins_still_fit(self):
        x, y, w, h = fit_image_to_page(4000, 3000, ASYMMETRIC)
        assert y >= 0
        assert y + h <= ASYMMETRIC.printable_height
