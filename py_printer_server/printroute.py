"""Decides which external path prints a given file.

Author: Pandiyaraj Karuppasamy
Date: Sep-14-2026

With no Pillow, no ReportLab and no bundled PDF renderer, there is no
in-process rendering to a page image. The question this module answers is not
"how do I turn this into a PDF" but "which mechanism on this machine already
knows how to print this file type":

- plain text/code: we write it to the spooler ourselves (full control over
  pagination, but only this one format).
- PDF and images: handed to whatever program Windows has associated with that
  extension, via the shell's ``printto`` verb.
- Office documents: same shell verb, letting Word/Excel/PowerPoint do the
  printing. No COM automation, so no ability to set duplex/colour on a
  per-job basis for these; they print with the printer's standing defaults.

This module contains pure logic, no I/O, so it is fully unit-testable without
Windows, a printer, or a file on disk.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path

TEXT_SUFFIXES = frozenset({
    ".txt", ".log", ".csv", ".md", ".py", ".json", ".xml", ".ini",
    ".yaml", ".yml", ".cfg", ".conf", ".ps1", ".sh", ".bat",
})
SHELL_IMAGE_SUFFIXES = frozenset({
    ".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".gif",
})
OFFICE_SUFFIXES = frozenset({
    ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
})

# Read this many bytes when guessing whether an unknown extension is text.
_SNIFF_BYTES = 4096


class PrintRoute(Enum):
    RAW_TEXT = "raw_text"
    SHELL_VERB = "shell_verb"
    OFFICE_COM = "office_com"  # handled via the same shell verb, kept as a
                                # distinct label so the UI can explain the
                                # duplex/colour caveat for these files.
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class RouteDecision:
    route: PrintRoute
    reason: str = ""


def _looks_like_text(path: Path) -> bool:
    """Best-effort sniff: does this file decode as UTF-8 without a NUL byte
    in the first chunk? Used only for unknown extensions; known extensions
    never reach this check."""
    try:
        with open(path, "rb") as f:
            chunk = f.read(_SNIFF_BYTES)
    except OSError:
        return False
    if b"\x00" in chunk:
        return False
    try:
        chunk.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def route(src: Path) -> RouteDecision:
    """Decide how to print `src`. Does not open the file except to sniff an
    unknown extension's content."""
    suffix = src.suffix.lower()

    if suffix in TEXT_SUFFIXES:
        return RouteDecision(PrintRoute.RAW_TEXT, "recognised text/code extension")

    if suffix in SHELL_IMAGE_SUFFIXES:
        return RouteDecision(PrintRoute.SHELL_VERB, "PDF/image, printed via the shell handler")

    if suffix in OFFICE_SUFFIXES:
        return RouteDecision(
            PrintRoute.OFFICE_COM,
            "Office document, printed via the shell handler (no per-job duplex/colour control)",
        )

    if suffix == "":
        if _looks_like_text(src):
            return RouteDecision(PrintRoute.RAW_TEXT, "no extension, sniffed as text")
        return RouteDecision(PrintRoute.UNSUPPORTED, "no extension and not text")

    if _looks_like_text(src):
        return RouteDecision(PrintRoute.RAW_TEXT, f"unrecognised extension {suffix!r}, sniffed as text")

    return RouteDecision(
        PrintRoute.UNSUPPORTED,
        f"no program on this PC is known to print {suffix!r} files",
    )
