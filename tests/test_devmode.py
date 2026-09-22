"""Tests for the pure DEVMODE settings logic.

Author: Pandiyaraj Karuppasamy
Date: Sep-23-2026

Runs on any OS: devmode.py touches no ctypes, which is the point of the split.

Read the docstring on test_mono_sets_the_field_AND_its_bit before adding to
this file. These tests guard the second-most-likely way colour breaks; the
most likely way is not observable from here at all.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from py_printer_server import devmode as d


@dataclass
class FakeOptions:
    printer: str = "X"
    color: bool = True
    paper: str = "A4"
    copies: int = 1
    duplex: bool = False


class FakeDevmode:
    """Duck-types DEVMODEW for the fields apply_print_options touches.

    dmFields starts non-zero on purpose: a real driver hands one back with bits
    already set, and the mutation has to OR into that, never replace it.
    0x201bf43 is a real mask read from an HP Smart Tank 520/540.
    """

    def __init__(self, dmFields: int = 0x201BF43) -> None:
        self.dmFields = dmFields
        self.dmColor = d.DMCOLOR_COLOR
        self.dmPaperSize = d.DMPAPER_LETTER
        self.dmCopies = 1
        self.dmDuplex = d.DMDUP_SIMPLEX


class TestApplyPrintOptions:
    def test_mono_sets_the_field_AND_its_bit(self):
        """The trap: a field written without its dmFields bit is silently
        ignored by the driver, so asserting dmColor alone proves nothing.

        Worth being clear about what this test does NOT cover. It would have
        passed against the original bug, because _apply was always correct: the
        devmode it produced was simply never attached to a printer handle or a
        DC. That failure is invisible from here and is covered by
        test_printing.py's wiring test instead.
        """
        dm = FakeDevmode()
        d.apply_print_options(dm, FakeOptions(color=False))
        assert dm.dmColor == d.DMCOLOR_MONOCHROME
        assert dm.dmFields & d.DM_COLOR

    def test_colour_sets_the_field_and_its_bit(self):
        dm = FakeDevmode()
        dm.dmColor = d.DMCOLOR_MONOCHROME
        d.apply_print_options(dm, FakeOptions(color=True))
        assert dm.dmColor == d.DMCOLOR_COLOR
        assert dm.dmFields & d.DM_COLOR

    def test_existing_dmfields_bits_survive(self):
        """Guards against `=` replacing `|=`: bits the driver set for fields we
        never touch must not be cleared."""
        original = 0x201BF43
        dm = FakeDevmode(dmFields=original)
        d.apply_print_options(dm, FakeOptions())
        assert dm.dmFields & original == original

    @pytest.mark.parametrize("requested,expected", [(0, 1), (-3, 1), (1, 1), (5, 5)])
    def test_copies_are_clamped_to_at_least_one(self, requested, expected):
        dm = FakeDevmode()
        d.apply_print_options(dm, FakeOptions(copies=requested))
        assert dm.dmCopies == expected
        assert dm.dmFields & d.DM_COPIES

    @pytest.mark.parametrize(
        "duplex,expected", [(True, d.DMDUP_VERTICAL), (False, d.DMDUP_SIMPLEX)]
    )
    def test_duplex(self, duplex, expected):
        dm = FakeDevmode()
        d.apply_print_options(dm, FakeOptions(duplex=duplex))
        assert dm.dmDuplex == expected
        assert dm.dmFields & d.DM_DUPLEX

    @pytest.mark.parametrize(
        "paper,expected",
        [("A4", d.DMPAPER_A4), ("a4", d.DMPAPER_A4), ("Letter", d.DMPAPER_LETTER)],
    )
    def test_paper_size(self, paper, expected):
        """Letter used to fall through silently: only A4 was handled, so
        choosing Letter left whatever the driver defaulted to."""
        dm = FakeDevmode()
        d.apply_print_options(dm, FakeOptions(paper=paper))
        assert dm.dmPaperSize == expected
        assert dm.dmFields & d.DM_PAPERSIZE

    def test_unknown_paper_is_left_to_the_driver(self):
        dm = FakeDevmode()
        dm.dmPaperSize = 42
        d.apply_print_options(dm, FakeOptions(paper="Tabloid"))
        assert dm.dmPaperSize == 42


class TestUnappliedSettings:
    def test_nothing_missed_when_the_driver_took_everything(self):
        dm = FakeDevmode()
        options = FakeOptions(color=False, copies=3, duplex=True)
        d.apply_print_options(dm, options)
        assert d.unapplied_settings(dm, options) == []

    def test_colour_reported_when_the_driver_strips_the_bit(self):
        """The exact shape of a silently-ignored setting: the value is right
        but the enabling bit is gone, so the driver will disregard it."""
        dm = FakeDevmode()
        options = FakeOptions(color=False)
        d.apply_print_options(dm, options)
        dm.dmFields &= ~d.DM_COLOR
        assert "colour" in d.unapplied_settings(dm, options)

    def test_colour_reported_when_the_driver_clamps_the_value(self):
        dm = FakeDevmode()
        options = FakeOptions(color=False)
        d.apply_print_options(dm, options)
        dm.dmColor = d.DMCOLOR_COLOR          # a mono-incapable driver clamping
        assert "colour" in d.unapplied_settings(dm, options)

    def test_duplex_only_reported_when_it_was_requested(self):
        dm = FakeDevmode()
        options = FakeOptions(duplex=False)
        d.apply_print_options(dm, options)
        dm.dmFields &= ~d.DM_DUPLEX
        assert "duplex" not in d.unapplied_settings(dm, options)
