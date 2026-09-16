"""Round-trip tests for qrcode_ascii.py.

There is no decoder library in this zero-dependency project (and none is
being added just to test with), so these tests decode the encoder's own
output independently: re-deriving version and mask from the matrix itself
rather than trusting the encoder's internal state, then RS-verifying every
block's syndrome before trusting the recovered bytes. A placement or
interleaving bug in the encoder would make this decoder fail (wrong bits,
non-zero syndromes) even though both sides share the same module.
"""
from __future__ import annotations

import pytest

from py_printer_server import qrcode_ascii as qr


def _decode(matrix: list[list[int]]) -> str:
    size = len(matrix)
    version = (size - 17) // 4

    fmt_positions = [(8, 0), (8, 1), (8, 2), (8, 3), (8, 4), (8, 5), (8, 7), (8, 8),
                      (7, 8), (5, 8), (4, 8), (3, 8), (2, 8), (1, 8), (0, 8)]
    raw = 0
    for r, c in fmt_positions:
        raw = (raw << 1) | matrix[r][c]
    raw ^= qr._FORMAT_MASK
    assert qr._bch_remainder(raw, qr._FORMAT_GENERATOR) == 0, "format info BCH check failed"
    ec_mask_bits = raw >> 10
    mask = ec_mask_bits & 0b111
    assert (ec_mask_bits >> 3) == qr._EC_LEVEL_L

    is_fn = [[False] * size for _ in range(size)]
    scratch = [[0] * size for _ in range(size)]
    qr._draw_finder(scratch, is_fn, 0, 0, size)
    qr._draw_finder(scratch, is_fn, 0, size - 7, size)
    qr._draw_finder(scratch, is_fn, size - 7, 0, size)
    for i in range(8, size - 8):
        is_fn[6][i] = True
        is_fn[i][6] = True
    coords = qr._ALIGNMENT_COORDS[version]
    skip = (
        {(coords[0], coords[0]), (coords[0], coords[-1]), (coords[-1], coords[0])}
        if coords else set()
    )
    for r in coords:
        for c in coords:
            if (r, c) in skip:
                continue
            for dr in range(-2, 3):
                for dc in range(-2, 3):
                    is_fn[r + dr][c + dc] = True
    is_fn[size - 8][8] = True
    for c in list(range(0, 6)) + [7, 8]:
        is_fn[8][c] = True
    for r in list(range(0, 6)) + [7]:
        is_fn[r][8] = True
    for c in range(size - 8, size):
        is_fn[8][c] = True
    for r in range(size - 7, size):
        is_fn[r][8] = True
    if version >= 7:
        for i in range(18):
            a = size - 11 + i % 3
            b = i // 3
            is_fn[b][a] = True
            is_fn[a][b] = True

    mask_fn = qr._MASK_FNS[mask]
    unmasked = [
        [matrix[r][c] ^ (1 if (not is_fn[r][c] and mask_fn(r, c)) else 0) for c in range(size)]
        for r in range(size)
    ]

    bits: list[int] = []
    col = size - 1
    upward = True
    while col > 0:
        if col == 6:
            col -= 1
        for i in range(size):
            row = (size - 1 - i) if upward else i
            for c in (col, col - 1):
                if is_fn[row][c]:
                    continue
                bits.append(unmasked[row][c])
        upward = not upward
        col -= 2

    remainder_bits = qr._REMAINDER_BITS[version]
    if remainder_bits:
        bits = bits[:-remainder_bits]

    codewords = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]

    _, ecc_per_block, group1, group2 = qr._BLOCK_TABLE[version]
    blocks_meta = []
    for blocks_g, data_per_g in (group1, group2):
        blocks_meta.extend([data_per_g] * blocks_g)
    max_data = max(blocks_meta)

    data_parts: list[list[int]] = [[] for _ in blocks_meta]
    idx = 0
    for i in range(max_data):
        for b, dlen in enumerate(blocks_meta):
            if i < dlen:
                data_parts[b].append(codewords[idx])
                idx += 1
    ecc_parts: list[list[int]] = [[] for _ in blocks_meta]
    for _ in range(ecc_per_block):
        for b in range(len(blocks_meta)):
            ecc_parts[b].append(codewords[idx])
            idx += 1

    for data_block, ecc_block in zip(data_parts, ecc_parts):
        full = data_block + ecc_block
        for i in range(ecc_per_block):
            root = qr._GF_EXP[i]
            val = 0
            for coef in full:
                val = qr._gf_mul(val, root) ^ coef
            assert val == 0, "RS syndrome check failed"

    all_data = [cw for part in data_parts for cw in part]
    bitstream = [b for cw in all_data for b in ((cw >> i) & 1 for i in range(7, -1, -1))]

    pos = 0
    mode = int("".join(map(str, bitstream[pos:pos + 4])), 2)
    pos += 4
    assert mode == 0b0100, "expected byte mode"
    count_bits = 8 if version < 10 else 16
    length = int("".join(map(str, bitstream[pos:pos + count_bits])), 2)
    pos += count_bits
    out = bytearray()
    for _ in range(length):
        out.append(int("".join(map(str, bitstream[pos:pos + 8])), 2))
        pos += 8
    return bytes(out).decode("ascii")


@pytest.mark.parametrize("url", [
    "http://192.168.1.100:8000",
    "http://10.0.0.5:9999",
    "http://localhost:8080",
    "http://255.255.255.255:65535/very/long/path/segment/for/testing",
    "http://a.b.c.d:1/" + "x" * 200,  # forces a version high enough to exercise version-info bits
])
def test_round_trip(url: str) -> None:
    matrix = qr.generate_matrix(url)
    assert _decode(matrix) == url


def test_matrix_is_square_with_expected_size() -> None:
    matrix = qr.generate_matrix("http://192.168.1.1:8114")
    size = len(matrix)
    assert all(len(row) == size for row in matrix)
    assert (size - 17) % 4 == 0


def test_too_large_payload_raises() -> None:
    with pytest.raises(qr.QrTooLargeError):
        qr.generate_matrix("x" * 1000)


def test_render_ascii_has_quiet_zone_border() -> None:
    text = qr.qr_ascii("http://192.168.1.1:8114")
    lines = text.splitlines()
    assert lines[0].strip(" ") == ""
    assert all(c == " " for c in lines[0][:2])
