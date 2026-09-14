"""Tests for the multipart parser and upload name handling.

Author: Pandiyaraj Karuppasamy
Date: Sep-14-2026

These cover bugs that silently corrupted or destroyed uploaded files, so each
test names the concrete failure it guards against.
"""

from __future__ import annotations

from pathlib import Path

from py_printer_server.server import _dedupe_name, _iter_multipart_files

BOUNDARY = "----WebKitFormBoundaryTEST"


def _build_body(parts: list[tuple[str, bytes]]) -> bytes:
    """Assemble a multipart body exactly as a browser would."""
    chunks = []
    for filename, data in parts:
        chunks.append(
            b"--" + BOUNDARY.encode() + b"\r\n"
            b'Content-Disposition: form-data; name="file"; filename="'
            + filename.encode() + b'"\r\n'
            b"Content-Type: application/octet-stream\r\n\r\n"
            + data + b"\r\n"
        )
    chunks.append(b"--" + BOUNDARY.encode() + b"--\r\n")
    return b"".join(chunks)


def test_simple_file_round_trips() -> None:
    body = _build_body([("hello.txt", b"hello world")])
    files = list(_iter_multipart_files(body, BOUNDARY))
    assert files == [("hello.txt", b"hello world")]


def test_multiple_files_round_trip() -> None:
    body = _build_body([("a.txt", b"AAA"), ("b.txt", b"BBB")])
    files = list(_iter_multipart_files(body, BOUNDARY))
    assert files == [("a.txt", b"AAA"), ("b.txt", b"BBB")]


def test_trailing_hyphens_are_preserved() -> None:
    """Regression: rstrip(b'\\r\\n--') stripped a character SET, so a file
    ending in hyphens lost real bytes."""
    payload = b"%%EOF\n--"
    body = _build_body([("doc.pdf", payload)])
    files = list(_iter_multipart_files(body, BOUNDARY))
    assert files[0][1] == payload


def test_trailing_newlines_are_preserved() -> None:
    """Only the single CRLF belonging to the delimiter may be removed."""
    payload = b"line1\r\nline2\r\n\r\n"
    body = _build_body([("notes.txt", payload)])
    files = list(_iter_multipart_files(body, BOUNDARY))
    assert files[0][1] == payload


def test_binary_content_is_byte_exact() -> None:
    payload = bytes(range(256)) * 4
    body = _build_body([("blob.bin", payload)])
    files = list(_iter_multipart_files(body, BOUNDARY))
    assert files[0][1] == payload


def test_content_containing_boundary_text_is_not_split() -> None:
    """Regression: splitting on the bare boundary also split inside file
    content. The real delimiter is CRLF + '--' + boundary."""
    payload = b"before " + BOUNDARY.encode() + b" after"
    body = _build_body([("tricky.txt", payload)])
    files = list(_iter_multipart_files(body, BOUNDARY))
    assert len(files) == 1
    assert files[0][1] == payload


def test_empty_file_is_yielded() -> None:
    body = _build_body([("empty.txt", b"")])
    files = list(_iter_multipart_files(body, BOUNDARY))
    assert files == [("empty.txt", b"")]


def test_path_in_filename_is_reduced_to_basename() -> None:
    body = _build_body([("../../etc/passwd", b"x")])
    files = list(_iter_multipart_files(body, BOUNDARY))
    assert files[0][0] == "passwd"


def test_same_name_twice_in_one_batch_does_not_collide(tmp_path: Path) -> None:
    """Regression: _dedupe_name only checked the filesystem, and nothing is
    committed until the batch finishes, so two same-named parts in one
    request both resolved to the same name and the second overwrote the
    first."""
    claimed: set[str] = set()
    first = _dedupe_name(str(tmp_path), "IMG_0001.JPG", claimed)
    claimed.add(first.lower())
    second = _dedupe_name(str(tmp_path), "IMG_0001.JPG", claimed)
    claimed.add(second.lower())
    third = _dedupe_name(str(tmp_path), "IMG_0001.JPG", claimed)

    assert first == "IMG_0001.JPG"
    assert second == "IMG_0001 (2).JPG"
    assert third == "IMG_0001 (3).JPG"
    assert len({first, second, third}) == 3


def test_dedupe_is_case_insensitive_like_the_filesystem(tmp_path: Path) -> None:
    (tmp_path / "Report.PDF").write_text("x", encoding="utf-8")
    assert _dedupe_name(str(tmp_path), "report.pdf") == "report (2).pdf"
