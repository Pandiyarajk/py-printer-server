"""DEVMODE field constants and the pure logic that fills one in.

Author: Pandiyaraj Karuppasamy
Date: Sep-22-2026

Split out of winspool.py so it imports on any OS: it touches no ctypes and no
Windows DLL, which is what lets the settings logic be unit-tested off Windows.
winspool re-exports every constant here, so `winspool.DM_COLOR` keeps working.

The hard-won rule this module exists to protect: a DEVMODE field written
WITHOUT its matching bit in dmFields is silently ignored by the driver. Writing
the field is the obvious half; the bit is the half that gets forgotten.

And the rule that cost a day: a DEVMODE that is built but never attached to
anything has no effect at all. It has to reach either OpenPrinterW (via
PRINTER_DEFAULTS.pDevMode, for the spooler path) or CreateDCW's lpInitData (for
the GDI path). No assertion about dmColor's value can detect that it went
nowhere, which is exactly how this went unnoticed.
"""

from __future__ import annotations

# dmFields bits (wingdi.h)
DM_ORIENTATION = 0x00000001
DM_PAPERSIZE = 0x00000002
DM_COPIES = 0x00000100
DM_PRINTQUALITY = 0x00000400
DM_COLOR = 0x00000800
DM_DUPLEX = 0x00001000

DMCOLOR_MONOCHROME = 1
DMCOLOR_COLOR = 2

DMDUP_SIMPLEX = 1
DMDUP_VERTICAL = 2

DMPAPER_LETTER = 1
DMPAPER_A4 = 9

DMORIENT_PORTRAIT = 1

# Paper names as the web UI offers them.
_PAPER_SIZES = {
    "A4": DMPAPER_A4,
    "LETTER": DMPAPER_LETTER,
}


def paper_code(paper: str) -> int | None:
    """Map a UI paper name to its DMPAPER_* code, or None if unrecognised.

    Letter used to fall through silently: only A4 was handled, so choosing
    Letter left the driver default. That looked correct on a Letter-default
    printer and printed A4 on an A4-default one.
    """
    return _PAPER_SIZES.get(paper.strip().upper())


def apply_print_options(dm, options) -> None:
    """Write `options` into the DEVMODE `dm`, setting each field's dmFields bit.

    `dm` is duck-typed, so a plain object with the same attributes works and the
    tests need no ctypes.

    dmCopies is the ONLY place copies are applied. Nothing may also loop the
    pages, or the two multiply: an earlier version did both and 3 copies of a
    2-page file emitted 18 pages.
    """
    fields = 0

    dm.dmColor = DMCOLOR_COLOR if options.color else DMCOLOR_MONOCHROME
    fields |= DM_COLOR

    code = paper_code(options.paper)
    if code is not None:
        dm.dmPaperSize = code
        fields |= DM_PAPERSIZE

    dm.dmCopies = max(1, int(options.copies))
    fields |= DM_COPIES

    dm.dmDuplex = DMDUP_VERTICAL if options.duplex else DMDUP_SIMPLEX
    fields |= DM_DUPLEX

    # OR in, never overwrite: bits the driver already set for fields we do not
    # touch have to survive.
    dm.dmFields |= fields


def unapplied_settings(dm, options) -> list[str]:
    """Which requested settings the driver did NOT accept.

    Read back AFTER DocumentPropertiesW has merged and validated the devmode,
    which is where a driver clamps what its hardware cannot do. This is the
    difference between "we asked for mono" and "mono will happen", and it is
    what lets the UI tell the user which settings were honoured instead of
    quietly printing something else.
    """
    missed: list[str] = []

    want_colour = DMCOLOR_COLOR if options.color else DMCOLOR_MONOCHROME
    if not (dm.dmFields & DM_COLOR) or dm.dmColor != want_colour:
        missed.append("colour")

    want_copies = max(1, int(options.copies))
    if want_copies > 1 and (not (dm.dmFields & DM_COPIES) or dm.dmCopies != want_copies):
        missed.append("copies")

    want_duplex = DMDUP_VERTICAL if options.duplex else DMDUP_SIMPLEX
    if options.duplex and (not (dm.dmFields & DM_DUPLEX) or dm.dmDuplex != want_duplex):
        missed.append("duplex")

    code = paper_code(options.paper)
    if code is not None and (not (dm.dmFields & DM_PAPERSIZE) or dm.dmPaperSize != code):
        missed.append("paper size")

    return missed
