"""Fitting an image onto a printed page.

Author: Pandiyaraj Karuppasamy
Date: Sep-22-2026

Pure geometry: no ctypes, no Windows, so the arithmetic that decides where ink
lands can be tested on any machine.
"""

from __future__ import annotations

from dataclasses import dataclass

# The printable area already is the margin the hardware imposes. Adding more
# shrinks the picture for no reason, so the default border is none.
IMAGE_MARGIN_INCH = 0.0


@dataclass(frozen=True)
class PageGeometry:
    """Device-unit measurements of one sheet, from GetDeviceCaps.

    Measured on an HP Smart Tank 520/540 at A4/600dpi:
    phys 4961x7016, offset 70x70, printable 4821x6876.
    """

    phys_width: int         # PHYSICALWIDTH, the whole sheet
    phys_height: int        # PHYSICALHEIGHT
    offset_x: int           # PHYSICALOFFSETX, sheet edge to printable area
    offset_y: int           # PHYSICALOFFSETY
    printable_width: int    # HORZRES
    printable_height: int   # VERTRES
    dpi_x: int              # LOGPIXELSX
    dpi_y: int              # LOGPIXELSY


def fit_image_to_page(
    img_w: int,
    img_h: int,
    page: PageGeometry,
    margin_inch: float = IMAGE_MARGIN_INCH,
) -> tuple[int, int, int, int]:
    """Return (x, y, w, h) in printer-DC coordinates, aspect preserved.

    The DC's origin is the top-left of the PRINTABLE area, not of the paper.
    That is the whole reason PHYSICALOFFSETX/Y exist: to centre on the sheet you
    work in sheet coordinates and then subtract the offset to get back into DC
    coordinates. Skipping that centres in the printable area instead, which is
    the same answer only when the margins are symmetric. They are on this HP
    (70 and 70), but inkjets commonly have a deeper bottom margin, and there the
    difference is visible on the page.

    The final clamp is what prevents clipping: on an asymmetric-margin device,
    sheet-centring can push an edge outside the printable area, and the driver
    crops it silently rather than complaining.

    Images are scaled up as well as down. A small image blown up to A4 looks
    soft, but that is what pressing Print asked for; the alternative is a
    postage stamp in the middle of a sheet.
    """
    if img_w <= 0 or img_h <= 0:
        raise ValueError(f"image has no area: {img_w}x{img_h}")

    margin_x = int(round(margin_inch * page.dpi_x))
    margin_y = int(round(margin_inch * page.dpi_y))
    avail_w = max(1, page.printable_width - 2 * margin_x)
    avail_h = max(1, page.printable_height - 2 * margin_y)

    scale = min(avail_w / img_w, avail_h / img_h)
    draw_w = max(1, int(round(img_w * scale)))
    draw_h = max(1, int(round(img_h * scale)))

    # Centre on the sheet, then translate into the DC's coordinate space.
    x = (page.phys_width - draw_w) // 2 - page.offset_x
    y = (page.phys_height - draw_h) // 2 - page.offset_y

    x = min(max(x, 0), max(0, page.printable_width - draw_w))
    y = min(max(y, 0), max(0, page.printable_height - draw_h))
    return x, y, draw_w, draw_h
