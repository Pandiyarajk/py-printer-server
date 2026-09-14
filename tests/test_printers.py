"""Tests for the hardware/virtual printer heuristic.

Author: Pandiyaraj Karuppasamy
Date: Sep-14-2026

Fixtures below are captured from a real EnumPrintersW(level=2) call on a
development machine (see winspool.py's docstring for why the raw buffer
itself cannot be trivially fixture-ed -- these are just the (name, port,
driver) triples that matter to is_virtual_printer, not a live buffer).
"""

from __future__ import annotations

import pytest

from py_printer_server.printers import is_virtual_printer

# (name, port, driver, expected_is_virtual)
CAPTURED_PRINTERS = [
    ("HP Smart Tank 520_540 series", "USB001", "HP Smart Tank 520_540 series PCL-3 (V4)", False),
    ("RICOH MP 2001 PCL 6", "10.151.20.6", "RICOH MP 2001 PCL 6", False),
    ("Snagit 10", r"C:\ProgramData\TechSmith\Snagit 10\PrinterPortFile", "Snagit 10 Printer", False),
    ("OneNote (Desktop)", "nul:", "Send to Microsoft OneNote 16 Driver", True),
    ("Microsoft XPS Document Writer", "PORTPROMPT:", "Microsoft XPS Document Writer v4", True),
    ("Microsoft Print to PDF", "PORTPROMPT:", "Microsoft Print To PDF", True),
    ("Fax", "SHRFAX:", "Microsoft Shared Fax Driver", True),
]


@pytest.mark.parametrize("name,port,driver,expected", CAPTURED_PRINTERS)
def test_is_virtual_printer_matches_captured_fixtures(name, port, driver, expected) -> None:
    assert is_virtual_printer(name, port, driver) is expected


def test_network_printer_by_ip_port_is_hardware() -> None:
    assert is_virtual_printer("Office Printer", "192.168.1.50", "Generic PCL6") is False


def test_wsd_port_is_hardware() -> None:
    assert is_virtual_printer("Some Printer", "WSD-abc123", "Some Driver") is False


def test_unrecognised_pdf_creator_name_is_virtual() -> None:
    # Regression guard for the "heuristic, not a hardcoded list" requirement:
    # an OEM virtual driver never seen before should still be caught by name.
    assert is_virtual_printer("PDFCreator", "SoftPrinterPort:", "PDFCreator v5") is True
