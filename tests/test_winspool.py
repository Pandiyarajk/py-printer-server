"""Layout and sanity checks for the ctypes bindings.

Author: Pandiyaraj Karuppasamy
Date: Sep-14-2026

A misdeclared ctypes Structure fails far away from its cause -- usually as a
setting that appears to have no effect, or an access violation deep inside a
Win32 call. These tests catch a struct layout regression at the cheapest
possible point: before any function is actually called.
"""

from __future__ import annotations

import ctypes

from py_printer_server import winspool

# Measured empirically on 64-bit Windows (see winspool.py's DEVMODEW
# docstring). This is not the same as the deprecated `sizeof(DEVMODE)` figure
# quoted in older documentation -- Windows headers have grown the struct
# over time, so this asserts internal consistency (the layout doesn't drift
# once fixed) rather than a canonical constant.
EXPECTED_DEVMODEW_SIZE = 220


def test_devmodew_size_matches_measured_layout() -> None:
    assert ctypes.sizeof(winspool.DEVMODEW) == EXPECTED_DEVMODEW_SIZE


def test_printer_defaults_struct_has_three_fields() -> None:
    assert len(winspool.PRINTER_DEFAULTS._fields_) == 3


def test_enum_printers_returns_real_printers() -> None:
    """Live check: requires Windows with at least one printer driver
    installed (true of any Windows box with even a virtual PDF printer)."""
    buf, count = winspool.enum_printers_raw(
        winspool.PRINTER_ENUM_LOCAL | winspool.PRINTER_ENUM_CONNECTIONS
    )
    printers = winspool.parse_printer_info_2w(buf, count)
    assert len(printers) == count
    if count:
        assert all(isinstance(p.pPrinterName, str) and p.pPrinterName for p in printers)


def test_get_default_printer_returns_string_or_none() -> None:
    result = winspool.get_default_printer()
    assert result is None or isinstance(result, str)
