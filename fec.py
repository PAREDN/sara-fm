#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 PAREDN (Philippine Amateur Radio Emergency Data Network) / DW1IRF
"""
fec.py — Reed-Solomon forward error correction for SARA-PP frames.

Pure, hardware-free (like kiss.py): bytes in, bytes out.

WHY: standard AX.25 (and SARA-PP before v0.6.0) is detect-and-retry only.
A frame with even one bad bit fails its CRC, is dropped, and BPQ32's ARQ
has to retransmit it -- costing a full round trip (often ~10 seconds on
this link, the real throughput bottleneck measured in the field). FEC
lets the receiver REPAIR a frame with a limited number of damaged bytes
instead of throwing it away. FX.25 and IL2P add the same kind of
Reed-Solomon FEC underneath AX.25 for the same reason; like them, this
lives entirely in the modem, below the KISS boundary, so BPQ32 and
Winlink Express never see it.

CODE: Reed-Solomon over GF(2^8) (primitive polynomial 0x11d, generator
alpha=2, first consecutive root alpha^0), systematic, shortened as needed.
Each block carries NSYM parity bytes and can correct up to NSYM // 2
byte errors anywhere in that block -- a byte is the RS "symbol", so a
burst of corrupted bits that stays within a few bytes costs only those
few bytes of correction budget, however many bits inside them are bad.

FRAME LAYOUT: the data (payload + CRC-16) is split into as few blocks as
fit (at most 255 - NSYM data bytes each), sized evenly, each RS-encoded,
then BYTE-INTERLEAVED across blocks: on-air byte order is block0[0],
block1[0], ..., block0[1], block1[1], ... A burst of consecutive damaged
bytes on the air (a collision, a fade, accumulated drift late in a frame)
is therefore spread across all blocks instead of exhausting one.

The receiver only knows the on-air byte count (from the frame header).
encoded_len() is strictly increasing in the data length, so
decoded_len() can always recover the data length unambiguously.
"""

from __future__ import annotations

# Parity bytes per RS block (corrects up to NSYM // 2 = 12 bad bytes per
# block) -- chosen by measurement, not guesswork. Calibrated on full
# 256-byte QPSK frames, 30 trials per condition, comparing 16/24/32:
#   noise sigma=0.40:     16 -> 23/30, 24 -> 28/30, 32 -> 30/30 (old: 0/30)
#   16-symbol burst:      16 -> 26/30, 24 -> 30/30, 32 -> 30/30 (old: 0/30)
#   22-symbol burst:      16 -> 15/30, 24 -> 30/30, 32 -> 30/30 (old: 0/30)
# (Milder noise and 2-12 symbol bursts: all three levels 30/30, vs as
# low as 0/30 with no FEC.) 24 over 16 costs ~3% more airtime per frame
# (~67ms on a 256-byte frame); one rescued frame saves a full BPQ32
# retry round trip (~10s on this link), so it pays for itself if it
# rescues even ~1 extra frame in 100. 32 adds another ~3% mainly to
# survive extreme noise where the link is barely usable anyway.
NSYM = 24
MAX_BLOCK = 255


class FecError(Exception):
    """Too many errors to correct (or an inconsistent/invalid length)."""


# ── GF(2^8) arithmetic ────────────────────────────────────────────────────
_PRIM = 0x11d
GF_EXP = [0] * 512
GF_LOG = [0] * 256
_x = 1
for _i in range(255):
    GF_EXP[_i] = _x
    GF_LOG[_x] = _i
    _x <<= 1
    if _x & 0x100:
        _x ^= _PRIM
for _i in range(255, 512):
    GF_EXP[_i] = GF_EXP[_i - 255]


def _mul(a: int, b: int) -> int:
    if a == 0 or b == 0:
        return 0
    return GF_EXP[GF_LOG[a] + GF_LOG[b]]


def _div(a: int, b: int) -> int:
    if b == 0:
        raise ZeroDivisionError
    if a == 0:
        return 0
    return GF_EXP[(GF_LOG[a] + 255 - GF_LOG[b]) % 255]


def _pow(a: int, p: int) -> int:
    return GF_EXP[(GF_LOG[a] * p) % 255]


def _inv(a: int) -> int:
    return GF_EXP[255 - GF_LOG[a]]


def _poly_scale(p, x):
    return [_mul(c, x) for c in p]


def _poly_add(p, q):
    r = [0] * max(len(p), len(q))
    for i, c in enumerate(p):
        r[i + len(r) - len(p)] = c
    for i, c in enumerate(q):
        r[i + len(r) - len(q)] ^= c
    return r


def _poly_mul(p, q):
    r = [0] * (len(p) + len(q) - 1)
    for j, qc in enumerate(q):
        if qc == 0:
            continue
        lq = GF_LOG[qc]
        for i, pc in enumerate(p):
            if pc:
                r[i + j] ^= GF_EXP[GF_LOG[pc] + lq]
    return r


def _poly_eval(poly, x):
    y = poly[0]
    for c in poly[1:]:
        y = _mul(y, x) ^ c
    return y


def _poly_div(dividend, divisor):
    out = list(dividend)
    for i in range(len(dividend) - (len(divisor) - 1)):
        coef = out[i]
        if coef != 0:
            for j in range(1, len(divisor)):
                if divisor[j] != 0:
                    out[i + j] ^= _mul(divisor[j], coef)
    sep = -(len(divisor) - 1)
    return out[:sep], out[sep:]


_GEN_CACHE: dict[int, list[int]] = {}


def _generator(nsym: int) -> list[int]:
    g = _GEN_CACHE.get(nsym)
    if g is None:
        g = [1]
        for i in range(nsym):
            g = _poly_mul(g, [1, _pow(2, i)])
        _GEN_CACHE[nsym] = g
    return g


# ── single RS block ───────────────────────────────────────────────────────
def rs_encode_block(msg: bytes, nsym: int | None = None) -> bytes:
    """Systematic encode: returns msg + nsym parity bytes."""
    nsym = NSYM if nsym is None else nsym
    if len(msg) + nsym > MAX_BLOCK:
        raise ValueError("block too long")
    gen = _generator(nsym)
    out = list(msg) + [0] * nsym
    for i in range(len(msg)):
        coef = out[i]
        if coef != 0:
            lc = GF_LOG[coef]
            for j in range(1, len(gen)):
                if gen[j]:
                    out[i + j] ^= GF_EXP[GF_LOG[gen[j]] + lc]
    return bytes(msg) + bytes(out[len(msg):])


def _syndromes(msg, nsym):
    return [0] + [_poly_eval(msg, _pow(2, i)) for i in range(nsym)]


def _error_locator(synd, nsym):
    """Berlekamp-Massey."""
    err_loc = [1]
    old_loc = [1]
    shift = len(synd) - nsym
    for i in range(nsym):
        k = i + shift
        delta = synd[k]
        for j in range(1, len(err_loc)):
            delta ^= _mul(err_loc[-(j + 1)], synd[k - j])
        old_loc = old_loc + [0]
        if delta != 0:
            if len(old_loc) > len(err_loc):
                new_loc = _poly_scale(old_loc, delta)
                old_loc = _poly_scale(err_loc, _inv(delta))
                err_loc = new_loc
            err_loc = _poly_add(err_loc, _poly_scale(old_loc, delta))
    while err_loc and err_loc[0] == 0:
        del err_loc[0]
    if (len(err_loc) - 1) * 2 > nsym:
        raise FecError("too many errors")
    return err_loc


def _find_errors(err_loc_rev, n):
    """Chien search -- only positions inside this (possibly shortened)
    block are valid; a locator pointing anywhere else means the block
    was damaged beyond what can be corrected."""
    errs = len(err_loc_rev) - 1
    pos = []
    for i in range(n):
        if _poly_eval(err_loc_rev, _pow(2, i)) == 0:
            pos.append(n - 1 - i)
    if len(pos) != errs:
        raise FecError("error locator inconsistent with block length")
    return pos


def _correct_errata(msg, synd, err_pos):
    """Forney algorithm."""
    coef_pos = [len(msg) - 1 - p for p in err_pos]
    err_loc = [1]
    for i in coef_pos:
        err_loc = _poly_mul(err_loc, _poly_add([1], [_pow(2, i), 0]))
    _, rem = _poly_div(_poly_mul(synd[::-1], err_loc), [1] + [0] * len(err_loc))
    err_eval = rem[::-1]
    X = [_pow(2, -(255 - p)) for p in coef_pos]
    E = [0] * len(msg)
    for i, Xi in enumerate(X):
        Xi_inv = _inv(Xi)
        loc_prime = 1
        for j, Xj in enumerate(X):
            if j != i:
                loc_prime = _mul(loc_prime, 1 ^ _mul(Xi_inv, Xj))
        if loc_prime == 0:
            raise FecError("Forney failed")
        y = _mul(Xi, _poly_eval(err_eval[::-1], Xi_inv))
        E[err_pos[i]] = _div(y, loc_prime)
    return _poly_add(msg, E)


def rs_decode_block(block: bytes, nsym: int | None = None) -> tuple[bytes, int]:
    """Returns (data, number_of_bytes_corrected). Raises FecError if the
    block can't be corrected."""
    nsym = NSYM if nsym is None else nsym
    msg = list(block)
    synd = _syndromes(msg, nsym)
    if max(synd) == 0:
        return bytes(msg[:-nsym]), 0
    err_loc = _error_locator(synd, nsym)
    err_pos = _find_errors(err_loc[::-1], len(msg))
    msg = _correct_errata(msg, synd, err_pos)
    if max(_syndromes(msg, nsym)) != 0:
        raise FecError("correction did not converge")
    return bytes(msg[:-nsym]), len(err_pos)


# ── frame level: split, encode, interleave ────────────────────────────────
def _n_blocks(data_len: int, max_block_data: int | None = None) -> int:
    # Read NSYM at call time (not a frozen module constant) so the parity
    # level can be varied for calibration without stale derived values.
    # max_block_data (v0.8.3): smaller blocks for QPSK Wide -- see
    # ofdm_core.WIDE_MAX_BLOCK_DATA. Default = largest block (narrow modes,
    # unchanged, bit-for-bit compatible with v0.6.0+).
    m = (MAX_BLOCK - NSYM) if max_block_data is None else max_block_data
    return max(1, -(-data_len // m))


def _block_data_sizes(data_len: int, max_block_data: int | None = None) -> list[int]:
    b = _n_blocks(data_len, max_block_data)
    base, extra = divmod(data_len, b)
    return [base + 1 if i < extra else base for i in range(b)]


def encoded_len(data_len: int, max_block_data: int | None = None) -> int:
    return data_len + NSYM * _n_blocks(data_len, max_block_data)


def decoded_len(on_air_len: int, max_block_data: int | None = None) -> int | None:
    """Inverse of encoded_len(); None if on_air_len isn't a length any
    real frame could have (encoded_len is strictly increasing, so at most
    one data length maps to any given on-air length)."""
    for b in range(1, on_air_len // NSYM + 1):
        d = on_air_len - NSYM * b
        if d >= 1 and _n_blocks(d, max_block_data) == b:
            return d
    return None


def _interleave_order(block_lens: list[int]):
    """(block_index, position) for each on-air byte, in on-air order."""
    order = []
    for pos in range(max(block_lens)):
        for bi, blen in enumerate(block_lens):
            if pos < blen:
                order.append((bi, pos))
    return order


def encode(data: bytes, max_block_data: int | None = None) -> bytes:
    if not data:
        raise ValueError("nothing to encode")
    sizes = _block_data_sizes(len(data), max_block_data)
    blocks, p = [], 0
    for s in sizes:
        blocks.append(rs_encode_block(data[p:p + s]))
        p += s
    order = _interleave_order([len(b) for b in blocks])
    return bytes(blocks[bi][pos] for bi, pos in order)


def decode(on_air: bytes, max_block_data: int | None = None) -> tuple[bytes, int]:
    """Returns (data, total_bytes_corrected). Raises FecError if any block
    is beyond repair or the length is invalid."""
    d = decoded_len(len(on_air), max_block_data)
    if d is None:
        raise FecError(f"invalid FEC frame length {len(on_air)}")
    sizes = _block_data_sizes(d, max_block_data)
    lens = [s + NSYM for s in sizes]
    blocks = [bytearray(n) for n in lens]
    for byte, (bi, pos) in zip(on_air, _interleave_order(lens)):
        blocks[bi][pos] = byte
    out, corrected = bytearray(), 0
    for blk in blocks:
        data, n = rs_decode_block(bytes(blk))
        out += data
        corrected += n
    return bytes(out), corrected
