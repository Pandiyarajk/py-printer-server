"""Tests for server.py's pure helper functions.

Author: Pandiyaraj Karuppasamy
Date: Sep-14-2026
"""

from __future__ import annotations

from pathlib import Path

from py_printer_server.server import _dedupe_name, human


def test_human_formats_bytes() -> None:
    assert human(500) == "500.0 B"
    assert human(1536) == "1.5 KB"
    assert human(1024 * 1024 * 2) == "2.0 MB"


def test_dedupe_name_returns_original_when_free(tmp_path: Path) -> None:
    assert _dedupe_name(str(tmp_path), "report.pdf") == "report.pdf"


def test_dedupe_name_suffixes_on_collision(tmp_path: Path) -> None:
    (tmp_path / "report.pdf").write_text("x", encoding="utf-8")
    assert _dedupe_name(str(tmp_path), "report.pdf") == "report (2).pdf"


def test_dedupe_name_increments_past_existing_suffixes(tmp_path: Path) -> None:
    (tmp_path / "report.pdf").write_text("x", encoding="utf-8")
    (tmp_path / "report (2).pdf").write_text("x", encoding="utf-8")
    assert _dedupe_name(str(tmp_path), "report.pdf") == "report (3).pdf"
