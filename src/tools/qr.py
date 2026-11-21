"""Minimal pure-Python ISO 18004 QR Code encoder (Byte mode, versions 1 to 10, ECC level L).

No external dependencies: returns a 2D boolean module grid (`True` = dark module)
ready to paint with a 4-module quiet zone in Qt dialogs.
"""

from __future__ import annotations

# Versions 1..10 (single-block or equal-block ECC level L capacities and EC codewords per block)
# (version, total_codewords, ec_per_block, group1_blocks, group1_data_cw, group2_blocks, group2_data_cw, align_centers)
_VERSIONS: tuple[tuple[int, int, int, int, int, int, int, tuple[int, ...]], ...] = (
    (1, 26, 7, 1, 19, 0, 0, ()),
    (2, 44, 10, 1, 34, 0, 0, (6, 18)),
    (3, 70, 15, 1, 55, 0, 0, (6, 22)),
    (4, 100, 20, 1, 80, 0, 0, (6, 26)),
    (5, 134, 26, 1, 108, 0, 0, (6, 30)),
    (6, 172, 18, 2, 68, 0, 0, (6, 34)),
    (7, 196, 20, 2, 78, 0, 0, (6, 22, 38)),
    (8, 242, 24, 2, 97, 0, 0, (6, 24, 42)),
    (9, 292, 30, 2, 116, 0, 0, (6, 26, 46)),
    (10, 346, 18, 2, 68, 2, 69, (6, 28, 50)),
)

# GF(256) with primitive polynomial 0x11D (285)
_EXP = [0] * 512
_LOG = [0] * 256
_v = 1
for _i in range(255):
    _EXP[_i] = _v
    _EXP[_i + 255] = _v
    _LOG[_v] = _i
    _v = (_v << 1) ^ 0x11D if _v & 0x80 else _v << 1


def _gf_mul(a: int, b: int) -> int:
    return 0 if a == 0 or b == 0 else _EXP[_LOG[a] + _LOG[b]]


def _rs_gen_poly(degree: int) -> list[int]:
    poly = [1]
    for i in range(degree):
        next_p = [0] * (len(poly) + 1)
        root = _EXP[i]
        for j, coef in enumerate(poly):
            next_p[j] ^= coef
            next_p[j + 1] ^= _gf_mul(coef, root)
        poly = next_p
    return poly


def _rs_remainder(data: list[int], ec_len: int) -> list[int]:
    gen = _rs_gen_poly(ec_len)
    res = list(data) + [0] * ec_len
    for i in range(len(data)):
        coef = res[i]
        if coef != 0:
            for j in range(1, len(gen)):
                res[i + j] ^= _gf_mul(gen[j], coef)
    return res[len(data):]


def _pick_version(byte_len: int) -> tuple[int, int, int, int, int, int, int, tuple[int, ...]]:
    for spec in _VERSIONS:
        ver, _tot, _ec, g1b, g1d, g2b, g2d, _align = spec
        data_cap = g1b * g1d + g2b * g2d
        count_bits = 8 if ver < 10 else 16
        needed_bytes = (4 + count_bits + 8 * byte_len + 7) // 8
        if needed_bytes <= data_cap:
            return spec
    raise ValueError("Payload too long for QR v1..10")


def _encode_data_bits(payload: bytes, ver: int, data_cap: int) -> list[int]:
    bits: list[int] = []

    def push(val: int, width: int) -> None:
        for shift in range(width - 1, -1, -1):
            bits.append((val >> shift) & 1)

    push(0b0100, 4)  # Byte mode
    push(len(payload), 8 if ver < 10 else 16)
    for b in payload:
        push(b, 8)
    cap_bits = data_cap * 8
    term = min(4, cap_bits - len(bits))
    push(0, term)
    while len(bits) % 8:
        bits.append(0)
    codewords = [int("".join(str(b) for b in bits[i:i + 8]), 2) for i in range(0, len(bits), 8)]
    pads = (0xEC, 0x11)
    pidx = 0
    while len(codewords) < data_cap:
        codewords.append(pads[pidx & 1])
        pidx += 1
    return codewords


def _interleave(codewords: list[int], spec: tuple[int, int, int, int, int, int, int, tuple[int, ...]]) -> list[int]:
    _ver, _tot, ec_len, g1b, g1d, g2b, g2d, _align = spec
    blocks: list[list[int]] = []
    ecs: list[list[int]] = []
    pos = 0
    for count, size in ((g1b, g1d), (g2b, g2d)):
        for _ in range(count):
            blk = codewords[pos:pos + size]
            pos += size
            blocks.append(blk)
            ecs.append(_rs_remainder(blk, ec_len))
    out: list[int] = []
    for i in range(max(g1d, g2d)):
        for blk in blocks:
            if i < len(blk):
                out.append(blk[i])
    for i in range(ec_len):
        for ec in ecs:
            out.append(ec[i])
    bits: list[int] = []
    for cw in out:
        for shift in range(7, -1, -1):
            bits.append((cw >> shift) & 1)
    return bits


def _bch_format(fmt: int) -> int:
    d = fmt << 10
    g = 0x537
    for shift in range(14, 9, -1):
        if (d >> shift) & 1:
            d ^= g << (shift - 10)
    return ((fmt << 10) | d) ^ 0x5412


def _bch_version(ver: int) -> int:
    d = ver << 12
    g = 0x1F25
    for shift in range(17, 11, -1):
        if (d >> shift) & 1:
            d ^= g << (shift - 12)
    return (ver << 12) | d


def _build_base_matrix(ver: int, align: tuple[int, ...]) -> tuple[list[list[bool]], list[list[bool]]]:
    n = 17 + 4 * ver
    grid = [[False] * n for _ in range(n)]
    func = [[False] * n for _ in range(n)]

    def set_fn(r: int, c: int, dark: bool) -> None:
        if 0 <= r < n and 0 <= c < n:
            grid[r][c] = dark
            func[r][c] = True

    def draw_finder(r0: int, c0: int) -> None:
        for dr in range(-1, 8):
            for dc in range(-1, 8):
                r, c = r0 + dr, c0 + dc
                if not (0 <= r < n and 0 <= c < n):
                    continue
                if 0 <= dr <= 6 and 0 <= dc <= 6:
                    dark = dr in (0, 6) or dc in (0, 6) or (2 <= dr <= 4 and 2 <= dc <= 4)
                else:
                    dark = False
                set_fn(r, c, dark)

    draw_finder(0, 0)
    draw_finder(0, n - 7)
    draw_finder(n - 7, 0)

    for i in range(8, n - 8):
        set_fn(6, i, i % 2 == 0)
        set_fn(i, 6, i % 2 == 0)

    for cr in align:
        for cc in align:
            if (cr <= 8 and cc <= 8) or (cr <= 8 and cc >= n - 9) or (cr >= n - 9 and cc <= 8):
                continue
            for dr in range(-2, 3):
                for dc in range(-2, 3):
                    set_fn(cr + dr, cc + dc, max(abs(dr), abs(dc)) != 1)

    # Format bits placeholders + dark module
    for i in range(9):
        if i != 6:
            set_fn(8, i, False)
            set_fn(i, 8, False)
    for i in range(8):
        set_fn(8, n - 1 - i, False)
        set_fn(n - 1 - i, 8, False)
    set_fn(4 * ver + 9, 8, True)

    if ver >= 7:
        vbits = _bch_version(ver)
        for i in range(18):
            bit = bool((vbits >> i) & 1)
            r, c = i // 3, n - 11 + (i % 3)
            set_fn(r, c, bit)
            set_fn(c, r, bit)

    return grid, func


def _apply_format(grid: list[list[bool]], mask: int) -> None:
    n = len(grid)
    bits = _bch_format((0b01 << 3) | mask)  # ECC Level L = 01
    coords1 = [(8, 0), (8, 1), (8, 2), (8, 3), (8, 4), (8, 5), (8, 7), (8, 8),
               (7, 8), (5, 8), (4, 8), (3, 8), (2, 8), (1, 8), (0, 8)]
    coords2 = [(n - 1 - i, 8) for i in range(7)] + [(8, n - 8 + i) for i in range(8)]
    for i in range(15):
        val = bool((bits >> (14 - i)) & 1)
        r1, c1 = coords1[i]
        r2, c2 = coords2[i]
        grid[r1][c1] = val
        grid[r2][c2] = val


def _mask_fn(mask: int, r: int, c: int) -> bool:
    if mask == 0:
        return (r + c) % 2 == 0
    if mask == 1:
        return r % 2 == 0
    if mask == 2:
        return c % 3 == 0
    if mask == 3:
        return (r + c) % 3 == 0
    if mask == 4:
        return (r // 2 + c // 3) % 2 == 0
    if mask == 5:
        return ((r * c) % 2 + (r * c) % 3) == 0
    if mask == 6:
        return (((r * c) % 2 + (r * c) % 3) % 2) == 0
    return (((r + c) % 2 + (r * c) % 3) % 2) == 0


def _penalty(grid: list[list[bool]]) -> int:
    n = len(grid)
    score = 0
    for line in (*grid, *([grid[r][c] for r in range(n)] for c in range(n))):
        run = 1
        for i in range(1, n):
            if line[i] == line[i - 1]:
                run += 1
            else:
                if run >= 5:
                    score += 3 + (run - 5)
                run = 1
        if run >= 5:
            score += 3 + (run - 5)
    for r in range(n - 1):
        for c in range(n - 1):
            v = grid[r][c]
            if v == grid[r][c + 1] == grid[r + 1][c] == grid[r + 1][c + 1]:
                score += 3
    dark = sum(1 for row in grid for v in row if v)
    pct = (dark * 100) // (n * n)
    score += min(abs(pct - 50) // 5, 20) * 10
    return score


def qr_matrix(text: str) -> list[list[bool]]:
    """Return a square 2D boolean matrix (`True` = dark module) encoding `text` in QR Byte mode."""
    payload = text.encode("utf-8")
    spec = _pick_version(len(payload))
    ver, _tot, _ec, g1b, g1d, g2b, g2d, align = spec
    data_cap = g1b * g1d + g2b * g2d
    bits = _interleave(_encode_data_bits(payload, ver, data_cap), spec)
    base, func = _build_base_matrix(ver, align)
    n = len(base)

    # Fill data bits zigzag once, then evaluate all 8 masks
    data_coords: list[tuple[int, int]] = []
    col = n - 1
    up = True
    while col > 0:
        if col == 6:
            col -= 1
        rows = range(n - 1, -1, -1) if up else range(n)
        for r in rows:
            for dc in (0, -1):
                c = col + dc
                if not func[r][c]:
                    data_coords.append((r, c))
        col -= 2
        up = not up

    best_grid: list[list[bool]] | None = None
    best_score = 1 << 30
    for mask in range(8):
        cand = [row[:] for row in base]
        for idx, (r, c) in enumerate(data_coords):
            bit = bits[idx] if idx < len(bits) else 0
            if _mask_fn(mask, r, c):
                bit ^= 1
            cand[r][c] = bool(bit)
        _apply_format(cand, mask)
        sc = _penalty(cand)
        if sc < best_score:
            best_score = sc
            best_grid = cand
    assert best_grid is not None
    return best_grid
