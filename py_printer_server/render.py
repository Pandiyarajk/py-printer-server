"""Printing images and PDFs ourselves, so print settings actually apply.

Author: Pandiyaraj Karuppasamy
Date: Sep-22-2026

Everything here exists because ShellExecuteW's "printto" verb cannot carry a
devmode. The handler it launches builds its own, so colour, copies and duplex
followed the printer's standing defaults however the job was submitted. The fix
is to stop delegating: decode the page with Pillow, create a device context
from our own devmode, and draw onto it.

Windows-only, like the rest of the print path. Imported lazily from printing.py
so a non-Windows machine can still collect the test suite.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PIL import Image, ImageOps, ImageSequence, ImageWin

from py_printer_server import winspool
from py_printer_server.pagelayout import IMAGE_MARGIN_INCH, fit_image_to_page

logger = logging.getLogger("printer_server")

# Rendering resolution for PDF pages. 200dpi is a deliberate compromise: 600
# would match the printer exactly but a single A4 page becomes a ~100MB bitmap,
# and the visible difference on an inkjet is small.
PDF_RENDER_DPI = 200


class RenderError(Exception):
    """A page could not be decoded or drawn."""


def to_printable(image: Image.Image, colour: bool) -> Image.Image:
    """Normalise a decoded image for printing.

    Converted to greyscale when mono is requested, as well as setting
    dmColor=DMCOLOR_MONOCHROME on the devmode. Belt and braces on purpose: the
    devmode is the correct mechanism, but the whole reason this code exists is
    that a setting was being ignored somewhere downstream, and a greyscale
    raster cannot come out coloured whatever the driver does. It can never turn
    a colour request INTO mono, so the redundancy is free.

    RGBA is flattened onto white first. A printer DC has no backdrop, so an
    alpha channel would otherwise composite against black and print a solid
    dark rectangle.
    """
    if image.mode in ("RGBA", "LA") or (
        image.mode == "P" and "transparency" in image.info
    ):
        flattened = Image.new("RGB", image.size, (255, 255, 255))
        rgba = image.convert("RGBA")
        flattened.paste(rgba, mask=rgba.split()[-1])
        image = flattened

    if not colour:
        # "L" is a luminance conversion, which is what greyscale should mean.
        return image.convert("L")
    return image.convert("RGB")


def _pages_from_image(path: Path) -> list[Image.Image]:
    """Every page of an image file.

    Multi-page TIFFs matter: they used to go to the Windows handler, which
    printed all of them. Printing only the first and reporting success would be
    a worse regression than the bug being fixed here.

    GIF frames are deliberately NOT enumerated. They are animation frames, and
    printing 40 sheets of a meme is not what anyone wanted.
    """
    with Image.open(path) as opened:
        multi_page = opened.format == "TIFF" and getattr(opened, "n_frames", 1) > 1
        if not multi_page:
            return [ImageOps.exif_transpose(opened).copy()]
        return [frame.copy() for frame in ImageSequence.Iterator(opened)]


def _pages_from_pdf(path: Path) -> list[Image.Image]:
    """Render every page of a PDF to a bitmap."""
    import pypdfium2

    scale = PDF_RENDER_DPI / 72.0  # PDF user space is 72dpi
    pages: list[Image.Image] = []
    document = pypdfium2.PdfDocument(str(path))
    try:
        for page in document:
            bitmap = page.render(scale=scale)
            pages.append(bitmap.to_pil().copy())
            page.close()
    finally:
        document.close()
    if not pages:
        raise RenderError(f"{path.name} has no pages")
    return pages


def load_pages(path: Path) -> list[Image.Image]:
    """Decode `path` into one PIL image per printed page."""
    try:
        if path.suffix.lower() == ".pdf":
            return _pages_from_pdf(path)
        return _pages_from_image(path)
    except RenderError:
        raise
    except Exception as exc:
        # Pillow and pdfium raise a wide spread of types, and the useful thing
        # for the user is the file name plus what went wrong, not the class.
        raise RenderError(f"could not read {path.name}: {exc}") from exc


def _resample_for(image: Image.Image, width: int, height: int) -> Image.Image:
    """Downscale in Pillow so GDI never has to shrink the bitmap.

    This is not an optimisation, it is a correctness fix. GDI's default stretch
    mode is BLACKONWHITE, which DISCARDS pixels when shrinking rather than
    averaging them, and Pillow's Dib.draw never calls SetStretchBltMode. A
    12-megapixel photo squeezed into a page rect by GDI comes out as noise.
    Resampling here with LANCZOS means the bitmap handed to GDI is never larger
    than its destination, so GDI only ever enlarges, which discards nothing.

    Also caps the raster. A4 at 600dpi is about 4800x6900, which is ~96MB as
    RGB, in a long-lived worker thread. The destination rect is the cap, and
    upscaling from it is visually indistinguishable on an inkjet.
    """
    if image.width <= width and image.height <= height:
        return image
    return image.resize((max(1, width), max(1, height)), Image.Resampling.LANCZOS)


def print_rendered(src: Path, session, colour: bool) -> int:
    """Print `src` through a device context built from the session's devmode.

    Returns the spooler job id, which StartDocW hands back, so the existing
    completion tracking works unchanged.
    """
    pages = load_pages(src)
    hdc = winspool.create_printer_dc(session.printer_name, session.devmode)
    try:
        geometry = winspool.get_page_geometry(hdc)

        info = winspool.DOCINFOW(
            cbSize=winspool.sizeof(winspool.DOCINFOW),
            lpszDocName=src.name,
            lpszOutput=None,
            lpszDatatype=None,
            fwType=0,
        )
        job_id = winspool.gdi32.StartDocW(hdc, winspool.byref(info))
        if job_id <= 0:
            raise RenderError(
                f"StartDocW failed for {src.name}: {winspool.get_last_error()}"
            )

        try:
            for page in pages:
                prepared = to_printable(page, colour)
                x, y, width, height = fit_image_to_page(
                    prepared.width, prepared.height, geometry, IMAGE_MARGIN_INCH
                )
                prepared = _resample_for(prepared, width, height)
                if winspool.gdi32.StartPage(hdc) <= 0:
                    raise RenderError(f"StartPage failed: {winspool.get_last_error()}")
                # Dib.draw takes the raw HDC as an int.
                ImageWin.Dib(prepared).draw(hdc.value, (x, y, x + width, y + height))
                if winspool.gdi32.EndPage(hdc) <= 0:
                    raise RenderError(f"EndPage failed: {winspool.get_last_error()}")
        except BaseException:
            # AbortDoc, not EndDoc. EndDoc on the failure path commits a
            # half-rendered document to the queue, so a crash mid-page still
            # produces paper.
            winspool.gdi32.AbortDoc(hdc)
            raise

        if winspool.gdi32.EndDoc(hdc) <= 0:
            raise RenderError(f"EndDoc failed: {winspool.get_last_error()}")
        logger.info(
            "rendered %s as %d page(s) in %s",
            src.name, len(pages), "colour" if colour else "mono",
        )
        return job_id
    finally:
        winspool.gdi32.DeleteDC(hdc)
        for page in pages:
            page.close()
