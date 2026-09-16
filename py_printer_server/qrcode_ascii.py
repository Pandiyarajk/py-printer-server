"""Minimal, hand-rolled QR code generator for printing a LAN URL to the console.

Zero third-party dependency, matching the rest of this project (winspool.py
hand-rolls ctypes bindings instead of pulling in pywin32; this hand-rolls the
same category of primitive -- Reed-Solomon over GF(256) and BCH format/version
bits -- instead of pulling in `qrcode`).

Scope is deliberately narrow: byte-mode encoding only, error-correction level
L, versions 1-10 (up to 271 bytes of payload -- far more than an
"http://192.168.x.x:port" URL needs). That keeps the block/alignment tables
small enough to hand-transcribe and check arithmetically against the known
per-version codeword totals, rather than reproducing the full 40-version x
4-level spec table from memory.

Author: Pandiyaraj Karuppasamy
Date: Sep-16-2026
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# GF(256) arithmetic for Reed-Solomon, primitive polynomial 0x11D (the one the
# QR spec uses -- NOT AES's 0x11B).
# ---------------------------------------------------------------------------

_GF_EXP = [0] * 512
_GF_LOG = [0] * 256


def _init_gf() -> None:
    x = 1
    for i in range(255):
        _GF_EXP[i] = x
        _GF_LOG[x] = i
        x <<= 1
        if x & 0x100:
            x ^= 0x11D
    for i in range(255, 512):
        _GF_EXP[i] = _GF_EXP[i - 255]


_init_gf()


def _gf_mul(a: int, b: int) -> int:
    if a == 0 or b == 0:
        return 0
    return _GF_EXP[_GF_LOG[a] + _GF_LOG[b]]


def _rs_generator_poly(nsym: int) -> list[int]:
    g = [1]
    for i in range(nsym):
        new_g = [0] * (len(g) + 1)
        root = _GF_EXP[i]
        for j, coef in enumerate(g):
            new_g[j] ^= coef
            new_g[j + 1] ^= _gf_mul(coef, root)
        g = new_g
    return g


def _rs_encode(data: list[int], nsym: int) -> list[int]:
    """Return the `nsym` error-correction codewords for `data`."""
    gen = _rs_generator_poly(nsym)
    remainder = data[:] + [0] * nsym
    for i in range(len(data)):
        coef = remainder[i]
        if coef == 0:
            continue
        for j, gc in enumerate(gen):
            remainder[i + j] ^= _gf_mul(gc, coef)
    return remainder[len(data):]


# ---------------------------------------------------------------------------
# BCH encoding for format (15 bit) and version (18 bit) info strings. Both are
# plain polynomial division over GF(2) -- computed rather than looked up, so
# there is no 40-entry lookup table to transcribe wrong.
# ---------------------------------------------------------------------------

_FORMAT_GENERATOR = 0b10100110111  # x^10+x^8+x^5+x^4+x^2+x+1
_FORMAT_MASK = 0b101010000010010
_VERSION_GENERATOR = 0b1111100100101  # x^12+x^11+x^10+x^9+x^8+x^5+x^2+1
_EC_LEVEL_L = 0b01


def _bch_remainder(value: int, generator: int) -> int:
    g_len = generator.bit_length()
    v_len = value.bit_length()
    while v_len >= g_len:
        value ^= generator << (v_len - g_len)
        v_len = value.bit_length()
    return value


def _format_bits(mask: int) -> list[int]:
    data = (_EC_LEVEL_L << 3) | mask  # 5 bits
    shifted = data << 10
    bits = shifted | _bch_remainder(shifted, _FORMAT_GENERATOR)
    bits ^= _FORMAT_MASK
    return [(bits >> (14 - i)) & 1 for i in range(15)]


def _version_bits(version: int) -> list[int]:
    shifted = version << 12
    bits = shifted | _bch_remainder(shifted, _VERSION_GENERATOR)
    return [(bits >> (17 - i)) & 1 for i in range(18)]


# ---------------------------------------------------------------------------
# Per-version block layout, error-correction level L only. Each entry is
# (data_codewords, ecc_per_block, (blocks_g1, data_per_g1), (blocks_g2, data_per_g2)).
# Verified by hand: data_codewords + ecc_per_block * (blocks_g1 + blocks_g2)
# equals the version's total codeword count (26, 44, 70, 100, 134, 172, 196,
# 242, 292, 346 for versions 1-10), which is fixed regardless of EC level.
# ---------------------------------------------------------------------------

_BLOCK_TABLE = {
    1: (19, 7, (1, 19), (0, 0)),
    2: (34, 10, (1, 34), (0, 0)),
    3: (55, 15, (1, 55), (0, 0)),
    4: (80, 20, (1, 80), (0, 0)),
    5: (108, 26, (1, 108), (0, 0)),
    6: (136, 18, (2, 68), (0, 0)),
    7: (156, 20, (2, 78), (0, 0)),
    8: (194, 24, (2, 97), (0, 0)),
    9: (232, 30, (2, 116), (0, 0)),
    10: (274, 18, (2, 68), (2, 69)),
}

# Alignment pattern center coordinates by version (empty for version 1, which
# has none). Combinations overlapping a finder pattern corner are skipped when
# drawing.
_ALIGNMENT_COORDS = {
    1: [],
    2: [6, 18],
    3: [6, 22],
    4: [6, 26],
    5: [6, 30],
    6: [6, 34],
    7: [6, 22, 38],
    8: [6, 24, 42],
    9: [6, 26, 46],
    10: [6, 28, 50],
}

# Remainder bits after interleaving, before the bitstream is placed into the
# matrix (versions 1, 7-10 have none; 2-6 have 7).
_REMAINDER_BITS = {1: 0, 2: 7, 3: 7, 4: 7, 5: 7, 6: 7, 7: 0, 8: 0, 9: 0, 10: 0}


class QrTooLargeError(ValueError):
    """Raised when the payload does not fit in any supported version (1-10)."""


def _byte_capacity(version: int) -> int:
    data_codewords = _BLOCK_TABLE[version][0]
    count_bits = 8 if version < 10 else 16
    overhead_bits = 4 + count_bits  # mode indicator + character count
    return (data_codewords * 8 - overhead_bits) // 8


def _choose_version(payload_len: int) -> int:
    for version in range(1, 11):
        if payload_len <= _byte_capacity(version):
            return version
    raise QrTooLargeError(
        f"{payload_len} bytes is too long for a version 1-10 QR code at error "
        "correction level L (max is 271 bytes)"
    )


def _build_codewords(data: bytes, version: int) -> list[int]:
    data_codewords, ecc_per_block, group1, group2 = _BLOCK_TABLE[version]
    count_bits = 8 if version < 10 else 16

    bits: list[int] = []
    for i in range(3, -1, -1):
        bits.append((0b0100 >> i) & 1)  # byte-mode indicator
    for i in range(count_bits - 1, -1, -1):
        bits.append((len(data) >> i) & 1)
    for byte in data:
        for i in range(7, -1, -1):
            bits.append((byte >> i) & 1)

    # Terminator (up to 4 zero bits), then pad to a byte boundary.
    bits.extend([0] * min(4, data_codewords * 8 - len(bits)))
    while len(bits) % 8:
        bits.append(0)

    codewords = [
        int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)
    ]
    pad_bytes = (0xEC, 0x11)
    i = 0
    while len(codewords) < data_codewords:
        codewords.append(pad_bytes[i % 2])
        i += 1

    blocks: list[list[int]] = []
    pos = 0
    for blocks_g, data_per_g in (group1, group2):
        for _ in range(blocks_g):
            blocks.append(codewords[pos:pos + data_per_g])
            pos += data_per_g

    ecc_blocks = [_rs_encode(block, ecc_per_block) for block in blocks]

    interleaved: list[int] = []
    for i in range(max(len(b) for b in blocks)):
        for block in blocks:
            if i < len(block):
                interleaved.append(block[i])
    for i in range(ecc_per_block):
        for ecc in ecc_blocks:
            interleaved.append(ecc[i])

    return interleaved


def _codewords_to_bits(codewords: list[int], version: int) -> list[int]:
    bits = [b for cw in codewords for b in ((cw >> i) & 1 for i in range(7, -1, -1))]
    bits.extend([0] * _REMAINDER_BITS[version])
    return bits


# ---------------------------------------------------------------------------
# Matrix construction.
# ---------------------------------------------------------------------------

_MASK_FNS = [
    lambda r, c: (r + c) % 2 == 0,
    lambda r, c: r % 2 == 0,
    lambda r, c: c % 3 == 0,
    lambda r, c: (r + c) % 3 == 0,
    lambda r, c: (r // 2 + c // 3) % 2 == 0,
    lambda r, c: (r * c) % 2 + (r * c) % 3 == 0,
    lambda r, c: ((r * c) % 2 + (r * c) % 3) % 2 == 0,
    lambda r, c: ((r + c) % 2 + (r * c) % 3) % 2 == 0,
]


def _draw_finder(matrix: list[list[int]], is_fn: list[list[bool]], top: int, left: int, size: int) -> None:
    for dr in range(-1, 8):
        for dc in range(-1, 8):
            r, c = top + dr, left + dc
            if not (0 <= r < size and 0 <= c < size):
                continue
            is_fn[r][c] = True
            if 0 <= dr <= 6 and 0 <= dc <= 6 and (dr in (0, 6) or dc in (0, 6) or (2 <= dr <= 4 and 2 <= dc <= 4)):
                matrix[r][c] = 1


def _draw_alignment(matrix: list[list[int]], is_fn: list[list[bool]], row: int, col: int) -> None:
    for dr in range(-2, 3):
        for dc in range(-2, 3):
            r, c = row + dr, col + dc
            is_fn[r][c] = True
            if max(abs(dr), abs(dc)) != 1:
                matrix[r][c] = 1


def _build_matrix(version: int, mask: int, data_bits: list[int]) -> list[list[int]]:
    size = 17 + 4 * version
    matrix = [[0] * size for _ in range(size)]
    is_fn = [[False] * size for _ in range(size)]

    _draw_finder(matrix, is_fn, 0, 0, size)
    _draw_finder(matrix, is_fn, 0, size - 7, size)
    _draw_finder(matrix, is_fn, size - 7, 0, size)

    for i in range(8, size - 8):
        matrix[6][i] = 1 if i % 2 == 0 else 0
        is_fn[6][i] = True
        matrix[i][6] = 1 if i % 2 == 0 else 0
        is_fn[i][6] = True

    coords = _ALIGNMENT_COORDS[version]
    skip = {(coords[0], coords[0]), (coords[0], coords[-1] if coords else 0), (coords[-1] if coords else 0, coords[0])} if coords else set()
    for r in coords:
        for c in coords:
            if (r, c) in skip:
                continue
            _draw_alignment(matrix, is_fn, r, c)

    matrix[size - 8][8] = 1
    is_fn[size - 8][8] = True

    # Reserve format-info modules (values filled in after masking).
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

    # Zigzag data placement.
    bit_index = 0
    n = len(data_bits)
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
                if bit_index < n:
                    matrix[row][c] = data_bits[bit_index]
                    bit_index += 1
        upward = not upward
        col -= 2

    # Mask data modules only.
    mask_fn = _MASK_FNS[mask]
    for r in range(size):
        for c in range(size):
            if not is_fn[r][c] and mask_fn(r, c):
                matrix[r][c] ^= 1

    # Format info (two copies) + fixed dark module.
    fmt = _format_bits(mask)
    fmt_positions_a = [(8, 0), (8, 1), (8, 2), (8, 3), (8, 4), (8, 5), (8, 7), (8, 8),
                        (7, 8), (5, 8), (4, 8), (3, 8), (2, 8), (1, 8), (0, 8)]
    for (r, c), bit in zip(fmt_positions_a, fmt):
        matrix[r][c] = bit
    for i, bit in enumerate(fmt[:7]):
        matrix[size - 1 - i][8] = bit
    for i, bit in enumerate(fmt[8:]):
        matrix[8][size - 7 + i] = bit
    matrix[size - 8][8] = 1

    if version >= 7:
        ver_bits_list = _version_bits(version)
        for i, bit in enumerate(ver_bits_list):
            a = size - 11 + i % 3
            b = i // 3
            matrix[b][a] = bit
            matrix[a][b] = bit

    return matrix


def _penalty(matrix: list[list[int]]) -> int:
    size = len(matrix)
    penalty = 0

    def run_penalty(line: list[int]) -> int:
        p = 0
        run = 1
        for i in range(1, len(line)):
            if line[i] == line[i - 1]:
                run += 1
            else:
                if run >= 5:
                    p += 3 + (run - 5)
                run = 1
        if run >= 5:
            p += 3 + (run - 5)
        return p

    for r in range(size):
        penalty += run_penalty(matrix[r])
    for c in range(size):
        penalty += run_penalty([matrix[r][c] for r in range(size)])

    for r in range(size - 1):
        for c in range(size - 1):
            block = matrix[r][c] + matrix[r][c + 1] + matrix[r + 1][c] + matrix[r + 1][c + 1]
            if block in (0, 4):
                penalty += 3

    pattern_dark_light = [1, 0, 1, 1, 1, 0, 1, 0, 0, 0, 0]
    pattern_light_dark = [0, 0, 0, 0, 1, 0, 1, 1, 1, 0, 1]

    def finder_like_penalty(line: list[int]) -> int:
        p = 0
        for i in range(len(line) - 10):
            window = line[i:i + 11]
            if window == pattern_dark_light or window == pattern_light_dark:
                p += 40
        return p

    for r in range(size):
        penalty += finder_like_penalty(matrix[r])
    for c in range(size):
        penalty += finder_like_penalty([matrix[r][c] for r in range(size)])

    dark = sum(sum(row) for row in matrix)
    percent = dark * 100 // (size * size)
    penalty += (abs(percent - 50) // 5) * 10

    return penalty


def generate_matrix(text: str) -> list[list[int]]:
    """Encode `text` (ASCII) as a byte-mode QR code and return its module grid.

    1 = dark module, 0 = light. Includes no quiet zone -- add one when
    rendering.
    """
    data = text.encode("ascii")
    version = _choose_version(len(data))
    codewords = _build_codewords(data, version)
    data_bits = _codewords_to_bits(codewords, version)

    best_matrix = None
    best_penalty = None
    for mask in range(8):
        candidate = _build_matrix(version, mask, data_bits)
        score = _penalty(candidate)
        if best_penalty is None or score < best_penalty:
            best_penalty = score
            best_matrix = candidate
    assert best_matrix is not None
    return best_matrix


def render_ascii(matrix: list[list[int]], quiet_zone: int = 2) -> str:
    """Render a module grid as half-block Unicode text for a terminal.

    Each output line covers two module rows using U+2588/U+2580/U+2584/space,
    so the printed QR code has roughly square modules instead of the tall
    rectangles a naive one-row-per-line rendering would produce.
    """
    size = len(matrix)
    padded_size = size + quiet_zone * 2
    padded = [[0] * padded_size for _ in range(padded_size)]
    for r in range(size):
        for c in range(size):
            padded[r + quiet_zone][c + quiet_zone] = matrix[r][c]

    blocks = {(0, 0): " ", (0, 1): "▄", (1, 0): "▀", (1, 1): "█"}
    lines = []
    for r in range(0, padded_size, 2):
        top = padded[r]
        bottom = padded[r + 1] if r + 1 < padded_size else [0] * padded_size
        lines.append("".join(blocks[(top[c], bottom[c])] for c in range(padded_size)))
    return "\n".join(lines)


def qr_ascii(text: str) -> str:
    """Convenience: encode `text` and render it in one call."""
    return render_ascii(generate_matrix(text))
