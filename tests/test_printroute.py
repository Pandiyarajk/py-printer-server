"""Tests for printroute.py -- pure suffix logic, no I/O beyond a sniff.

Author: Pandiyaraj Karuppasamy
Date: Sep-14-2026
"""

from __future__ import annotations

from pathlib import Path

from py_printer_server.printroute import PrintRoute, route


def test_text_extension_routes_raw_text(tmp_path: Path) -> None:
    f = tmp_path / "notes.txt"
    f.write_text("hello", encoding="utf-8")
    assert route(f).route == PrintRoute.RAW_TEXT


def test_pdf_routes_shell_verb(tmp_path: Path) -> None:
    f = tmp_path / "doc.pdf"
    f.write_bytes(b"%PDF-1.4\n")
    assert route(f).route == PrintRoute.SHELL_VERB


def test_image_routes_shell_verb(tmp_path: Path) -> None:
    f = tmp_path / "photo.jpg"
    f.write_bytes(b"\xff\xd8\xff")
    assert route(f).route == PrintRoute.SHELL_VERB


def test_docx_routes_office_com(tmp_path: Path) -> None:
    f = tmp_path / "report.docx"
    f.write_bytes(b"PK\x03\x04")
    assert route(f).route == PrintRoute.OFFICE_COM


def test_unknown_binary_is_unsupported(tmp_path: Path) -> None:
    f = tmp_path / "data.bin"
    f.write_bytes(b"\x00\x01\x02\xff")
    assert route(f).route == PrintRoute.UNSUPPORTED


def test_unknown_extension_sniffed_as_text(tmp_path: Path) -> None:
    f = tmp_path / "notes.weird"
    f.write_text("plain ascii content", encoding="utf-8")
    assert route(f).route == PrintRoute.RAW_TEXT


def test_no_extension_sniffed_as_text(tmp_path: Path) -> None:
    f = tmp_path / "README"
    f.write_text("plain ascii content", encoding="utf-8")
    assert route(f).route == PrintRoute.RAW_TEXT


def test_no_extension_binary_is_unsupported(tmp_path: Path) -> None:
    f = tmp_path / "BINARY"
    f.write_bytes(b"\x00\x01\x02\xff")
    assert route(f).route == PrintRoute.UNSUPPORTED


def test_case_insensitive_extension(tmp_path: Path) -> None:
    f = tmp_path / "PHOTO.JPG"
    f.write_bytes(b"\xff\xd8\xff")
    assert route(f).route == PrintRoute.SHELL_VERB
