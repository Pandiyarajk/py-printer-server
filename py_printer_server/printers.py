"""Printer discovery and the hardware/virtual split.

Author: Pandiyaraj Karuppasamy
Date: Sep-14-2026
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from py_printer_server import winspool

# Heuristic, not a hardcoded name list: OEM virtual-printer drivers vary too
# much (PDFCreator, CutePDF, Foxit, novaPDF, ...) to enumerate exhaustively,
# but they consistently either use a "prompt"/null/software port, or carry
# one of these words in their driver or printer name.
_VIRTUAL_PORT_PREFIXES = ("PORTPROMPT:", "NUL:", "SHRFAX:", "XPSPORT", "ONENOTE")
_VIRTUAL_NAME_PATTERN = re.compile(
    r"print to pdf|xps document writer|onenote|fax|pdfcreator|cutepdf|"
    r"foxit.*pdf|novapdf|document writer|send to.*driver",
    re.IGNORECASE,
)
# A real USB/parallel/serial/network printer reports one of these port shapes.
_HARDWARE_PORT_PATTERN = re.compile(r"^(USB|LPT|COM|WSD-|\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})", re.IGNORECASE)


@dataclass(frozen=True)
class PrinterInfo:
    name: str
    port: str
    driver: str
    is_virtual: bool
    is_default: bool
    supports_color: bool


def is_virtual_printer(name: str, port: str, driver: str) -> bool:
    """Decide whether a printer is a virtual/software sink rather than
    physical hardware, so the UI can hide "Microsoft Print to PDF" etc. by
    default without a maintained blocklist of every OEM driver name."""
    port_upper = (port or "").upper()
    if any(port_upper.startswith(p) for p in _VIRTUAL_PORT_PREFIXES):
        return True
    if _HARDWARE_PORT_PATTERN.match(port or ""):
        return False
    haystack = f"{name} {driver}"
    return bool(_VIRTUAL_NAME_PATTERN.search(haystack))


def supports_color(name: str, port: str) -> bool:
    """Return True if the driver reports colour capability.

    Used to grey out the colour toggle for a mono-only printer rather than
    silently ignoring the setting -- DeviceCapabilitiesW returns -1 on error,
    which is treated as "assume colour" (the safer default: a toggle that
    exists but has no effect is less confusing than one that is missing when
    it should be there).
    """
    result = winspool.device_capabilities(name, port, winspool.DC_COLORDEVICE)
    if result < 0:
        return True
    # Nonzero means colour-capable. There used to be a note here claiming some
    # drivers return "other nonzero values"; that was this function reading
    # DC_BINS (6) instead of DC_COLORDEVICE (32) and seeing a paper-bin count.
    return result != 0


def list_printers(show_virtual: bool = False) -> list[PrinterInfo]:
    """Enumerate installed printers, hardware first, alphabetically.

    Args:
        show_virtual: include software sinks (Print to PDF, XPS, OneNote,
            Fax, ...) in the result. Default False, matching the UI's
            "hardware only" default.
    """
    buf, count = winspool.enum_printers_raw(
        winspool.PRINTER_ENUM_LOCAL | winspool.PRINTER_ENUM_CONNECTIONS
    )
    raw = winspool.parse_printer_info_2w(buf, count)
    default_name = winspool.get_default_printer()

    printers = []
    for p in raw:
        name = p.pPrinterName or ""
        port = p.pPortName or ""
        driver = p.pDriverName or ""
        if not name:
            continue
        virtual = is_virtual_printer(name, port, driver)
        if virtual and not show_virtual:
            continue
        printers.append(
            PrinterInfo(
                name=name,
                port=port,
                driver=driver,
                is_virtual=virtual,
                is_default=(name == default_name),
                supports_color=supports_color(name, port),
            )
        )

    printers.sort(key=lambda p: (p.is_virtual, p.name.lower()))
    return printers


def get_default_printer_name() -> str | None:
    """Return the system default printer's name, or None if unset."""
    return winspool.get_default_printer()
