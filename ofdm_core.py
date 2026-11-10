#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 PAREDN (Philippine Amateur Radio Emergency Data Network) / DW1IRF
"""
ofdm_core.py — SARA-PP OFDM waveform: encode / detect / decode.

Written from scratch for the simplified "ping/pong" link tester.
No audio hardware, no GUI, no BPQ32/KISS — pure signal processing,
so it can be fully unit-tested offline (see test_offline.py) without
ever touching a soundcard or a radio.

DESIGN RULE THAT MATTERS MOST HERE (read this before touching TX shaping):

    This module NEVER applies silence, keying delay, squelch tone, or
    PTT settle time. It hands back one thing: a burst of audio that
    starts with the preamble and ends with the last data symbol,
    with only a small internal edge taper (see TAPER_MS below) to
    avoid a hard-edge "click". All the silence / squelch-tone / PTT
    timing belongs in audio_io.py's transmit chain, wrapped AROUND
    this burst — never mixed inside it.

    The old SARA FM had a 5 ms amplitude ramp that landed 37.5% of
    the way into the FIRST preamble repetition (640-sample symbol,
    240-sample ramp), cutting that repetition's average energy by
    roughly half — right when the receiver most needs a strong,
    undistorted reference. That bug lived precisely because TX
    shaping (ramps, tone, silence) and the OFDM payload were built
    in the same function. Keeping them apart is the fix, not just a
    smaller number.

    TAPER_MS here is deliberately tiny (1.5 ms = ~11% of one 13.3 ms
    symbol) — just enough to stop spectral splatter from a hard edge,
    never enough to meaningfully weaken a preamble repetition. A
    regression test in test_offline.py asserts this stays small.
"""

from __future__ import annotations
import struct

import fec
import numpy as np
from scipy.signal import butter, sosfilt, sosfilt_zi, lfilter, lfilter_zi
from scipy.signal import sosfiltfilt as _scipy_sosfiltfilt   # reference, for tests

# v0.9.29: the receiver band-pass filters its buffer forward and backward
# (zero phase) on every audio block. scipy's sosfiltfilt recomputes the
# filter's initial conditions and pads through a generic helper on every
# call -- over half its cost here. The filters never change, so the initial
# conditions are computed once; the result is identical. Field (v0.9.28,
# DX1FLR gateway): its PC fell 3-9 s behind on an idle channel and skipped
# audio -- the first connect request (SABM) of a session was lost each time.
_ZI_CACHE: dict = {}


def sosfiltfilt(sos, x):
    """Same result as scipy.signal.sosfiltfilt(sos, x) (odd padding, default
    pad length), with the filter's initial conditions cached."""
    key = id(sos)
    ent = _ZI_CACHE.get(key)
    if ent is None or ent[0] is not sos:
        n = sos.shape[0]
        ntaps = 2 * n + 1 - min(int((sos[:, 2] == 0).sum()), int((sos[:, 5] == 0).sum()))
        ent = (sos, sosfilt_zi(sos), 3 * ntaps)
        _ZI_CACHE[key] = ent
    _, zi, padlen = ent
    x = np.asarray(x, dtype=np.float64)
    if x.shape[-1] <= padlen:
        return _scipy_sosfiltfilt(sos, x)
    left = 2 * x[0] - x[padlen:0:-1]
    right = 2 * x[-1] - x[-2:-padlen - 2:-1]
    ext = np.concatenate([left, x, right])
    y, _ = sosfilt(sos, ext, zi=zi * ext[0])
    y = y[::-1]
    y, _ = sosfilt(sos, y, zi=zi * y[0])
    return y[::-1][padlen:-padlen]

# ─────────────────────────────────────────────────────────────────────────
# Waveform parameters — one fixed mode, no negotiation, no mode switching.
# ─────────────────────────────────────────────────────────────────────────

SAMPLE_RATE = 48000          # matches CM108 / USB audio interfaces used on-site
N_FFT       = 512
N_CP        = 128            # 25% cyclic prefix — generous guard interval
N_SYM       = N_FFT + N_CP   # 640 samples = 13.333 ms / symbol

NUM_CARRIERS = 16
CARRIER_BINS = np.arange(5, 5 + NUM_CARRIERS)          # bins 5-20 -> ~469-1875 Hz
PILOT_POS    = [0, 5, 10, 15]                            # positions within CARRIER_BINS
DATA_POS     = [i for i in range(NUM_CARRIERS) if i not in PILOT_POS]
PILOT_VAL    = 1.0 + 0j

MOD_BPSK = 0
MOD_QPSK = 1
MOD_QAM16_WIDE = 2    # v0.9.4 -- see the 16-QAM note below (was the retired narrow 8PSK)
MOD_QPSK_WIDE = 3     # v0.8.0 -- see WIDE_BINS
# v0.9.18 (experimental): 64-QAM Wide. All four 2-bit header codes are in
# use, so on the air it carries header code 2 (16QAM Wide) with a DIFFERENT
# wide training symbol; the receiver tells the two apart by which known
# pattern gives a smooth channel estimate (see _training_is_qam64). Never
# sent to a station that hasn't said it decodes it (CAP_QAM64): an older
# receiver would take it for a damaged 16QAM frame.
MOD_QAM64_WIDE = 4
# v0.9.24: "coded" Wide modes -- the same constellations with an inner
# convolutional code (fec_cc.py: K=7, soft-decision Viterbi) inside the
# Reed-Solomon code, interleaved across carriers and symbols. Several dB
# more robust, so a faster constellation works on a weaker link. Signalled
# like 64QAM: the header code of the base mode plus a training-symbol
# pattern of their own (see WIDE_TRAINING_BITS_BY_MODE), and only sent to a
# station announcing it decodes them (CAP_CODED).
MOD_QAM16_WIDE_C = 5
MOD_QAM64_WIDE_C = 6
# (A coded QPSK Wide was tried and dropped: plain QPSK Wide already decodes
# down to ~9 dB data carriers, and the simulations gave it no useful gain.)
CODED = {MOD_QAM16_WIDE_C: (MOD_QAM16_WIDE, "2/3"),
         MOD_QAM64_WIDE_C: (MOD_QAM64_WIDE, "3/4")}
BITS_PER_CARRIER = {MOD_BPSK: 1, MOD_QPSK: 2, MOD_QAM16_WIDE: 4, MOD_QPSK_WIDE: 2, MOD_QAM64_WIDE: 6,
                    MOD_QAM16_WIDE_C: 4, MOD_QAM64_WIDE_C: 6}
WIDE_MODS = (MOD_QPSK_WIDE, MOD_QAM16_WIDE, MOD_QAM64_WIDE, MOD_QAM16_WIDE_C, MOD_QAM64_WIDE_C)
HEADER_CODE = {MOD_QAM64_WIDE: MOD_QAM16_WIDE, MOD_QAM16_WIDE_C: MOD_QAM16_WIDE,
               MOD_QAM64_WIDE_C: MOD_QAM16_WIDE}   # on-air header code
# which internal modes share each on-air header code (told apart by training symbol)
HEADER_FAMILY = {MOD_QAM16_WIDE: (MOD_QAM16_WIDE, MOD_QAM64_WIDE, MOD_QAM16_WIDE_C, MOD_QAM64_WIDE_C)}


def base_mod(modulation: int) -> int:
    """The constellation a mode uses (a coded mode's uncoded twin)."""
    return CODED[modulation][0] if modulation in CODED else modulation


def cc_rate(modulation: int) -> str | None:
    return CODED[modulation][1] if modulation in CODED else None


def is_wide(modulation: int) -> bool:
    """Modes using the wide carrier layout (training symbol, WIDE_BINS)."""
    return modulation in WIDE_MODS

# ─────────────────────────────────────────────────────────────────────────
# 16-QAM Wide (v0.9.4), the next step up from QPSK Wide: the same 22 wide
# carriers, training symbol, pilots and tracking, but 4 bits per carrier
# instead of 2 (72 bits/symbol, 2x QPSK Wide), Gray-coded as two
# independent 4-level axes (I and Q), so -- like QPSK -- a decision that
# slips to its nearest neighbour costs one bit. It needs the AMPLITUDE as
# well as the phase, which the training symbol (per carrier) and the
# pilots (common gain, every symbol) provide.
# Why now, when narrow 8PSK failed on air in v0.5.12: that was before
# per-carrier tracking, the robust phase-line tracker, the wide band and
# the decoder thread, and the v0.9.3 field carrier checks measure 18-26 dB
# per-carrier S/N (error-vector based, so real radio distortion included)
# on every Wide carrier -- 16-QAM's decisions need ~16-18 dB for the
# Reed-Solomon code to clean up. Header code 2 was 8PSK's: receivers from
# v0.7.1 to v0.9.3 reject it instantly (invalid_modulation), and Auto only
# uses it with a station that says (in its link report) it can decode it.
# ─────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────
# QPSK Wide (v0.8.0), Phase 1 of "use more of the audio band" (VARA FM
# adds carriers as conditions allow). Same QPSK per carrier -- unlike 8PSK,
# which needed a cleaner channel than FM audio gives -- just more carriers:
# originally 25 on bins 4-28 (375-2625 Hz) vs 16 on bins 5-20 (469-1875 Hz) --
# see the v0.8.3 note below for the current band. An FM
# channel simulation (speaker audio with pre/de-emphasis, and the 9600
# port) showed per-carrier S/N within a few dB of the current band out to
# ~2.7 kHz; real radios' TX audio filters/limiters aren't modelled, so the
# top edge stays at 2.625 kHz pending field tests.
#   [preamble x4][header][WIDE training symbol][wide data symbols...]
# Preamble and header are UNCHANGED (narrow), so detection and every
# header check work as before; the header's modulation value 3 tells a
# v0.8.0 receiver to expect the wide layout, and makes an older receiver
# reject the frame instantly (invalid_modulation) -- never misread it. The
# preamble only covers the narrow bins, so one known training symbol on all
# wide carriers gives the channel estimate for the new outer carriers.
# 5 pilots (both edges included) leave 20 data carriers: 40 bits/symbol vs
# 24. Drift tracking fits phase vs frequency across the pilots (a sample-
# clock mismatch rotates higher carriers more) instead of narrow mode's
# single common correction -- the wider span would make that residual
# slope larger.
# ─────────────────────────────────────────────────────────────────────────
#
# v0.8.3 -- band moved up, smaller FEC blocks. The v0.8.1 per-carrier check
# on the K6IRF -> DW1IRF path showed the LOW end failing, not the top:
# 469 Hz ~2% bit errors at S/N ~4 dB and 562 Hz ~5.5% at ~5 dB, while
# 656 Hz and up were clean (2531 Hz 0.3-0.8%). Radios commonly high-pass
# the audio below a few hundred Hz (keeping CTCSS tones out). Wide now
# spans 656-2625 Hz: 22 carriers, pilots at both edges + every 7th, 18
# data carriers = 36 bits/symbol (1.5x QPSK). And a 6000-byte upload
# choked every time on its final 211-byte frame: at <= 231 data bytes a
# frame got ONE Reed-Solomon block (12 repairs) while the 275-byte frames
# before it got two (24), and with those weak carriers it needed more than
# 12 -- the identical retransmission failing the same way each time. Wide
# frames now use blocks of at most WIDE_MAX_BLOCK_DATA bytes, so mid-size
# frames get two blocks; short and long frames are unchanged.
WIDE_BINS = np.arange(7, 29)                     # 656-2625 Hz, 22 carriers
WIDE_PILOT_POS = [0, 7, 14, 21]
WIDE_DATA_POS = [i for i in range(len(WIDE_BINS)) if i not in WIDE_PILOT_POS]
WIDE_MAX_BLOCK_DATA = 160
WIDE_TRAINING_BITS = [int(b) for b in np.random.RandomState(0x5A1A).randint(0, 2, len(WIDE_BINS))]
# v0.9.18: 64-QAM Wide's training symbol -- the 16QAM pattern with every
# other carrier inverted, so the two differ on exactly half the carriers
# (and estimating with the wrong one flips the channel's sign on those).
WIDE_TRAINING_BITS_64 = [b ^ (i % 2) for i, b in enumerate(WIDE_TRAINING_BITS)]
# v0.9.24: four patterns in all, each differing from every other on half the
# carriers (Walsh-like flips of the 16QAM pattern).
_WALSH = {0: lambda i: 0, 1: lambda i: i % 2, 2: lambda i: (i // 2) % 2,
          3: lambda i: (i % 2) ^ ((i // 2) % 2)}
_TRAINING_WALSH = {2: 0, 3: 0, 4: 1, 5: 2, 6: 3}   # internal mode -> flip pattern
# Data symbols can be BPSK (1 bit/carrier, 12 bits/symbol), QPSK (2
# bits/carrier, 24 bits/symbol), or 8-PSK (3 bits/carrier, 36
# bits/symbol) -- same 12 data carriers each time, more payload per
# symbol at the cost of less noise/drift margin as the constellation
# gets denser. The HEADER symbol is ALWAYS BPSK, regardless of the data
# modulation -- the receiver has to be able to read the header before
# it knows which modulation the data uses, so the header itself can't
# be mode-dependent. Two of its 12 bits are the modulation field (not
# one -- three live modes now need 2 bits to select, with one value
# held in reserve), so a frame is self-describing: the two stations
# never have to be manually set to matching modes, each frame just
# says what it is.
HEADER_BITS = 12                 # 2 modulation bits + 10 payload-length bits
HEADER_MOD_BITS = 2
HEADER_LEN_BITS = HEADER_BITS - HEADER_MOD_BITS
MAX_PAYLOAD_LEN = (1 << HEADER_LEN_BITS) - 1   # 1023 bytes -- ample above this
                                                 # project's real coalescing cap

TAPER_MS   = 1.5
TAPER_LEN  = int(SAMPLE_RATE * TAPER_MS / 1000)   # ~72 samples, ~11% of one symbol

PREAMBLE_REPS = 4

VERSION = "sara-fm-0.9.96"

# Fixed, deterministic preamble bit pattern (known to both ends).
_rng = np.random.RandomState(1337)
PREAMBLE_BITS = _rng.randint(0, 2, size=NUM_CARRIERS).tolist()


# ─────────────────────────────────────────────────────────────────────────
# Symbol-level helpers
# ─────────────────────────────────────────────────────────────────────────

def bits_per_symbol(modulation: int) -> int:
    if is_wide(modulation):
        return len(WIDE_DATA_POS) * BITS_PER_CARRIER[modulation]
    return len(DATA_POS) * BITS_PER_CARRIER[modulation]


def fec_block(modulation: int) -> int | None:
    """Max data bytes per Reed-Solomon block for this modulation (None =
    the standard largest block, narrow modes)."""
    return WIDE_MAX_BLOCK_DATA if is_wide(modulation) else None


def extra_symbols(modulation: int) -> int:
    """Symbols between the header and the data (the wide training symbol)."""
    return 1 if is_wide(modulation) else 0


def _bits_to_bpsk(bits) -> np.ndarray:
    """0 -> +1, 1 -> -1 (standard BPSK mapping)."""
    b = np.asarray(bits, dtype=np.float64)
    return 1.0 - 2.0 * b


def _bits_to_qpsk(bits) -> np.ndarray:
    """
    Two bits per symbol, consumed as (b0, b1) pairs: b0 -> sign of I,
    b1 -> sign of Q, normalized to unit energy. This is exactly two
    independent BPSK streams carried on the same carrier's I and Q —
    the simplest correct QPSK, and because each axis is an independent
    BPSK decision, a single bit error on receive only ever comes from
    noise on that one axis, not from a constellation-distance subtlety.
    """
    b = np.asarray(bits, dtype=np.float64).reshape(-1, 2)
    i = 1.0 - 2.0 * b[:, 0]
    q = 1.0 - 2.0 * b[:, 1]
    return (i + 1j * q) / np.sqrt(2)


# Added as a throughput experiment: 8-PSK packs 3 bits/carrier (36
# bits/symbol) vs QPSK's 2 (24 bits/symbol), and a denser constellation
# sends a given payload in less time -- less time for clock drift to
# accumulate, which was hypothesized to allow a LARGER safe coalescing
# cap than QPSK's established 250 bytes, not just faster transmission
# of the same size.
#
# Rigorous multi-trial drift testing (same methodology used to validate
# every other drift-sensitive constant in this file) did not bear that
# out. 8-PSK's constellation points are 45 degrees apart (vs QPSK's 90),
# roughly halving the angular margin before a decision crosses into the
# wrong point -- confirmed empirically: at the SAME 254-byte payload,
# QPSK's failure cliff sits around 170-175ppm while 8-PSK's sits around
# 150-155ppm, and at QPSK's own validated 250-byte cap specifically,
# 8-PSK only held reliably (5/5 multi-trial) through 100ppm, dropping to
# 1/5 by 130ppm -- a real, measured reliability loss, not a hypothetical
# one. 8-PSK's OWN genuinely safe cap (reliable 5/5 through 150ppm, the
# same rigor QPSK's 250-byte cap was held to) measured around 150 bytes
# -- smaller than QPSK's, not larger. Net real-world throughput
# including realistic fixed per-transmission overhead (TX lead + tail):
# QPSK @ 250B ~129 bytes/sec per transmission vs 8-PSK @ its own 150B
# cap ~118 bytes/sec -- 8-PSK comes out slightly BEHIND, not ahead. And
# since this project's actual observed real-world throughput (~18.7
# bytes/sec) sits far below either number, the real bottleneck is
# protocol-level round trips, not modulation speed -- meaning 8-PSK
# would not have meaningfully helped real transfers even had it won
# this comparison.
#
# UPDATE (v0.5.12): real hardware testing confirmed it outright, beyond
# the synthetic margin finding above. In Ping/Pong mode, zero Pong
# replies ever came back; in BPQ32 bridge mode, real exchanges simply
# stopped and the session disconnected. Not occasional unreliability --
# apparently unable to survive this radio path's real, non-drift
# impairments (limiting, compression, frequency-response rolloff,
# general noise) at all, on top of the angular margin it was already
# short on. Adaptive modulation (start robust, step up once the link
# proves itself, the way VARA FM varies its speed) was considered and
# declined for this specifically: that architecture rescues a mode that
# works but is merely riskier under some conditions, not one that fails
# every single time regardless of link quality -- switching into 8-PSK
# mid-session wouldn't trade some risk for more speed, it would just
# break the link at that exact moment.
#
# Retired from the GUI's selectable choices (sara_pingpong.py) so it
# can't be accidentally picked again -- this DSP core (constellation,
# encode/decode) stays in place and tested, so a received 8-PSK frame,
# however it got sent, is still decoded and labeled correctly. Kept for
# that reason, and because the underlying math could still favor it on
# a cleaner link with less real drift AND fewer other impairments than
# this project's hardware has shown -- not currently recommended for
# use on this hardware. Not used as bpq_bridge.py's default, and
# MAX_BATCH_BYTES was not raised on the strength of it.
#
# Gray-coded 8-PSK: 8 points equally spaced at k*45 degrees, with the
# standard Gray sequence assigned around them in angular order so that
# ADJACENT points -- the most likely confusion under noise or drift,
# since they're the nearest neighbors on the circle -- differ by
# exactly one bit, not two or three. A symbol that drifts just past its
# own decision boundary into a neighboring point therefore costs one
# bit, the same single-bit cost BPSK/QPSK's independent-axis decisions
# already have, rather than a disproportionate multi-bit error.
_PSK8_GRAY_BITS = [
    (0, 0, 0), (0, 0, 1), (0, 1, 1), (0, 1, 0),
    (1, 1, 0), (1, 1, 1), (1, 0, 1), (1, 0, 0),
]
_PSK8_ANGLES = np.array([k * np.pi / 4 for k in range(8)])
_PSK8_BITS_TO_INDEX = {bits: i for i, bits in enumerate(_PSK8_GRAY_BITS)}


def _bits_to_8psk(bits) -> np.ndarray:
    """Three bits per symbol -- see _PSK8_GRAY_BITS for the mapping."""
    b = np.asarray(bits, dtype=int).reshape(-1, 3)
    out = np.empty(len(b), dtype=complex)
    for i, triple in enumerate(b):
        idx = _PSK8_BITS_TO_INDEX[tuple(int(x) for x in triple)]
        angle = _PSK8_ANGLES[idx]
        out[i] = np.cos(angle) + 1j * np.sin(angle)
    return out


def _8psk_values_to_bits(values) -> list[int]:
    """Inverse of _bits_to_8psk: nearest-angle (minimum angular distance)
    decoding, not a simple sign check -- 8-PSK's points aren't aligned to
    simple real/imaginary axis boundaries the way BPSK/QPSK's are."""
    bits = []
    for v in values:
        angle = np.angle(v) % (2 * np.pi)
        idx = int(np.round(angle / (np.pi / 4))) % 8
        bits.extend(_PSK8_GRAY_BITS[idx])
    return bits


# 16-QAM (v0.9.4): per axis, Gray-coded 4 levels  00:-3  01:-1  11:+1  10:+3
# (first bit = sign, second = inner/outer), scaled to unit average energy.
_QAM16_SCALE = 1 / np.sqrt(10)
_QAM16_LEVEL = {(0, 0): -3.0, (0, 1): -1.0, (1, 1): 1.0, (1, 0): 3.0}
QAM16_THRESHOLD = 2 * _QAM16_SCALE      # inner/outer decision boundary


def _bits_to_qam16(bits) -> np.ndarray:
    b = np.asarray(bits, dtype=int).reshape(-1, 4)
    i = np.array([_QAM16_LEVEL[(int(x[0]), int(x[1]))] for x in b])
    q = np.array([_QAM16_LEVEL[(int(x[2]), int(x[3]))] for x in b])
    return (i + 1j * q) * _QAM16_SCALE


def _qam16_axis_bits(v: float):
    return (1 if v >= 0 else 0), (1 if abs(v) < QAM16_THRESHOLD else 0)


# 64-QAM (v0.9.18): per axis 3 bits -- sign, then a Gray code on the
# magnitude: 1:(1,0) 3:(1,1) 5:(0,1) 7:(0,0) -- scaled to unit average energy.
_QAM64_SCALE = 1 / np.sqrt(42)
_QAM64_MAG = {(1, 0): 1.0, (1, 1): 3.0, (0, 1): 5.0, (0, 0): 7.0}


def _bits_to_qam64(bits) -> np.ndarray:
    b = np.asarray(bits, dtype=int).reshape(-1, 6)
    ax = lambda s, a, c: (1.0 if s else -1.0) * _QAM64_MAG[(int(a), int(c))]
    i = np.array([ax(x[0], x[1], x[2]) for x in b])
    q = np.array([ax(x[3], x[4], x[5]) for x in b])
    return (i + 1j * q) * _QAM64_SCALE


def _qam64_axis_bits(v: float):
    m = abs(v) / _QAM64_SCALE
    return (1 if v >= 0 else 0), (1 if m < 4.0 else 0), (1 if 2.0 < m < 6.0 else 0)


_MODULATION_MAPPERS = {MOD_BPSK: _bits_to_bpsk, MOD_QPSK: _bits_to_qpsk,
                       MOD_QAM16_WIDE: _bits_to_qam16, MOD_QPSK_WIDE: _bits_to_qpsk,
                       MOD_QAM64_WIDE: _bits_to_qam64,
                       MOD_QAM16_WIDE_C: _bits_to_qam16, MOD_QAM64_WIDE_C: _bits_to_qam64}


def _symbol_to_bits(values: np.ndarray, modulation: int) -> list[int]:
    """Inverse of _bits_to_bpsk / _bits_to_qpsk / _bits_to_8psk for a
    vector of equalized carrier values."""
    bits = []
    modulation = base_mod(modulation)
    if modulation == MOD_BPSK:
        for v in values:
            bits.append(0 if v.real >= 0 else 1)
    elif modulation in (MOD_QPSK, MOD_QPSK_WIDE):
        for v in values:
            bits.append(0 if v.real >= 0 else 1)
            bits.append(0 if v.imag >= 0 else 1)
    elif modulation == MOD_QAM16_WIDE:
        for v in values:
            bits.extend(_qam16_axis_bits(v.real))
            bits.extend(_qam16_axis_bits(v.imag))
    elif modulation == MOD_QAM64_WIDE:
        for v in values:
            bits.extend(_qam64_axis_bits(v.real))
            bits.extend(_qam64_axis_bits(v.imag))
    else:
        raise ValueError(f"unknown modulation: {modulation}")
    return bits


def _ofdm_symbol_tx(carrier_symbols: dict, bins=None) -> np.ndarray:
    """
    carrier_symbols: {carrier_index_within_bins: complex value}; bins
    defaults to CARRIER_BINS (narrow), WIDE_BINS for QPSK Wide.
    Builds one real time-domain OFDM symbol (with cyclic prefix) via IFFT,
    using conjugate symmetry so the result is real.
    """
    if bins is None:
        bins = CARRIER_BINS
    spec = np.zeros(N_FFT, dtype=complex)
    for pos, bin_idx in enumerate(bins):
        val = carrier_symbols.get(pos, 0j)
        spec[bin_idx] = val
        spec[N_FFT - bin_idx] = np.conj(val)   # Hermitian mirror -> real IFFT
    td = np.fft.ifft(spec).real
    td = td / (np.max(np.abs(td)) or 1.0)
    cp = td[-N_CP:]
    return np.concatenate([cp, td]).astype(np.float32)


def _build_known_symbol(bits) -> dict:
    bpsk = _bits_to_bpsk(bits)
    return {i: complex(v, 0) for i, v in enumerate(bpsk)}


PREAMBLE_CARRIERS = _build_known_symbol(PREAMBLE_BITS)
PREAMBLE_SYMBOL   = _ofdm_symbol_tx(PREAMBLE_CARRIERS)   # (N_SYM,) float32, one rep
WIDE_TRAINING_CARRIERS = _build_known_symbol(WIDE_TRAINING_BITS)
WIDE_TRAINING_SYMBOL = _ofdm_symbol_tx(WIDE_TRAINING_CARRIERS, WIDE_BINS)
WIDE_TRAINING_CARRIERS_64 = _build_known_symbol(WIDE_TRAINING_BITS_64)
WIDE_TRAINING_SYMBOL_64 = _ofdm_symbol_tx(WIDE_TRAINING_CARRIERS_64, WIDE_BINS)
WIDE_TRAINING_CARRIERS_BY_MODE = {}
WIDE_TRAINING_SYMBOL_BY_MODE = {}
for _m, _w in _TRAINING_WALSH.items():
    _bits = [b ^ _WALSH[_w](i) for i, b in enumerate(WIDE_TRAINING_BITS)]
    WIDE_TRAINING_CARRIERS_BY_MODE[_m] = _build_known_symbol(_bits)
    WIDE_TRAINING_SYMBOL_BY_MODE[_m] = _ofdm_symbol_tx(WIDE_TRAINING_CARRIERS_BY_MODE[_m], WIDE_BINS)


def _payload_bits(n_bits_needed: int, bits_available: list[int]) -> list[int]:
    pad = (-len(bits_available)) % n_bits_needed
    return bits_available + [0] * pad


def _bytes_to_bits(data: bytes) -> list[int]:
    out = []
    for byte in data:
        out.extend([(byte >> (7 - i)) & 1 for i in range(8)])
    return out


def _bits_to_bytes(bits: list[int]) -> bytes:
    out = bytearray()
    for i in range(0, len(bits) - 7, 8):
        byte = 0
        for b in bits[i:i + 8]:
            byte = (byte << 1) | int(b)
        out.append(byte)
    return bytes(out)


def crc16(data: bytes) -> int:
    """CRC-16/CCITT-FALSE — same polynomial family as the original modem."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if (crc & 0x8000) else (crc << 1) & 0xFFFF
    return crc


# ─────────────────────────────────────────────────────────────────────────
# TX: frame_encode — the ONLY function that builds transmit audio.
# ─────────────────────────────────────────────────────────────────────────

def frame_encode(payload: bytes, modulation: int = MOD_BPSK) -> np.ndarray:
    """
    payload -> float32 audio burst: [preamble x4][header][data symbols...]
    A CRC-16 is appended to the payload before packing. No silence, no
    keying delay, no squelch tone — see module docstring.

    modulation: MOD_BPSK (default), MOD_QPSK, MOD_QPSK_WIDE or MOD_QAM16_WIDE. Encoded as a
    2-bit field in the header (always BPSK itself — see the modulation
    comment above), so the receiver reads it off each frame rather than
    needing to be told out of band. The two stations never have to
    agree on a setting.
    """
    if modulation not in BITS_PER_CARRIER:
        raise ValueError(f"unknown modulation: {modulation}")
    # CRC first, then Reed-Solomon FEC over payload+CRC (see fec.py). The
    # CRC stays as the final integrity check AFTER correction on receive,
    # so a rare RS mis-correction is still caught rather than forwarded.
    # The header's length field carries the ON-AIR (encoded) byte count;
    # the receiver recovers the data length from it via fec.decoded_len().
    data = fec.encode(payload + struct.pack('>H', crc16(payload)), fec_block(modulation))
    if len(data) > MAX_PAYLOAD_LEN:
        raise ValueError(f"payload too large: {len(data)} bytes on air after FEC (max {MAX_PAYLOAD_LEN})")

    code = HEADER_CODE.get(modulation, modulation)
    mod_bits = [(code >> (HEADER_MOD_BITS - 1 - i)) & 1 for i in range(HEADER_MOD_BITS)]
    hdr_bits = mod_bits + [(len(data) >> (HEADER_LEN_BITS - 1 - i)) & 1
                            for i in range(HEADER_LEN_BITS)]
    hdr_carriers = {DATA_POS[k]: complex(v, 0) for k, v in enumerate(_bits_to_bpsk(hdr_bits))}
    for p in PILOT_POS:
        hdr_carriers[p] = PILOT_VAL
    hdr_sym = _ofdm_symbol_tx(hdr_carriers)

    syms_per_frame = bits_per_symbol(modulation)
    mapper = _MODULATION_MAPPERS[modulation]
    wide = is_wide(modulation)
    data_pos, pilot_pos, bins = ((WIDE_DATA_POS, WIDE_PILOT_POS, WIDE_BINS) if wide
                                 else (DATA_POS, PILOT_POS, CARRIER_BINS))
    if modulation in CODED:
        import fec_cc
        coded = fec_cc.encode(_bytes_to_bits(data), cc_rate(modulation))
        d_bits = _payload_bits(syms_per_frame, [int(b) for b in fec_cc.interleave(coded, syms_per_frame)])
    else:
        d_bits = _payload_bits(syms_per_frame, _bytes_to_bits(data))
    data_syms = [WIDE_TRAINING_SYMBOL_BY_MODE[modulation]] if wide else []
    for i in range(0, len(d_bits), syms_per_frame):
        chunk = d_bits[i:i + syms_per_frame]
        carriers = {data_pos[k]: complex(v) for k, v in enumerate(mapper(chunk))}
        for p in pilot_pos:
            carriers[p] = PILOT_VAL
        data_syms.append(_ofdm_symbol_tx(carriers, bins))

    burst = np.concatenate([PREAMBLE_SYMBOL] * PREAMBLE_REPS + [hdr_sym] + data_syms)
    burst = burst.astype(np.float32)
    peak = np.max(np.abs(burst)) or 1.0
    burst = burst / peak

    if len(burst) > TAPER_LEN * 2:
        ramp = np.linspace(0, 1, TAPER_LEN, dtype=np.float32)
        burst[:TAPER_LEN]  *= ramp
        burst[-TAPER_LEN:] *= ramp[::-1]
    return burst


# ─────────────────────────────────────────────────────────────────────────
# RX: streaming OFDM receiver
# ─────────────────────────────────────────────────────────────────────────

class DecodedFrame:
    __slots__ = ("payload", "quality", "modulation", "drift_deg", "fec_corrected",
                 "snr_db", "level_dbfs", "carrier_stats")

    def __init__(self, payload: bytes, quality: float, modulation: int = MOD_BPSK,
                 drift_deg: float = 0.0, fec_corrected: int = 0,
                 snr_db: float | None = None, level_dbfs: float | None = None):
        # v0.7.0 tuning figures, like VARA's S/N and VU:
        # snr_db -- signal-to-noise ratio measured from the repeated
        #   preamble (see OFDMReceiver.preamble_snr_db). Independent of
        #   volume/gain: it measures the radio LINK, not the level setting.
        # level_dbfs -- received preamble RMS in dB relative to full scale,
        #   as seen by the receiver (i.e. after any RX gain).
        self.snr_db = snr_db
        self.level_dbfs = level_dbfs
        # v0.8.1 per-carrier diagnostic (see OFDMReceiver._carrier_stats):
        # (freqs_hz, bit_errors, bits, evm_sum, evm_n) per data carrier, or None
        self.carrier_stats = None
        # Bytes repaired by FEC in this frame. Anything > 0 is a frame that
        # would have been DROPPED before v0.6.0 (CRC fail -> BPQ32 retry,
        # a full round trip) -- the direct on-air measure of FEC's value.
        self.fec_corrected = fec_corrected
        self.payload = payload
        self.quality = quality   # 0..1-ish signal-quality estimate
        self.modulation = modulation   # MOD_BPSK or MOD_QPSK, read from this frame's header
        # Total pilot-measured phase drift (degrees) accumulated from the
        # preamble to the last data symbol. Diagnostic only -- already
        # corrected for by the time this frame is returned; lets a caller
        # see how much real TX/RX clock drift this specific link actually
        # has, rather than only knowing a frame succeeded or failed.
        self.drift_deg = drift_deg


# v0.9.24: Wide FFT windows starting this many samples early (negative) are
# left alone -- inside the 128-sample prefix with margin; the A/B tests under
# noise showed no gain from moving them. Outside it they are moved.
WIDE_TIMING_OK = (-100, -8)


class OFDMReceiver:
    """
    Feed raw float32 samples via push(); pull decoded frames via poll().
    Streaming-safe: keeps a rolling buffer, trims itself as frames (or
    junk) are consumed, works with arbitrarily sized input chunks.

    Filtering is a fresh sosfiltfilt() pass over the whole buffer on
    every poll() call -- the exact approach field-proven across every
    BPSK/QPSK ping/pong test this project ran (repeated 30/30 results
    both ways, in both modulations). An incremental (stateful) filter
    was tried briefly to support a much larger buffer; it introduced a
    real regression (persistent filter-ringing state after a burst
    produced occasional false preamble detections that never showed up
    in short synthetic tests but broke real over-air ping/pong) and was
    reverted here. Benchmarked: sosfiltfilt on a buffer sized per
    MAX_BUF_SYMS below takes single-digit milliseconds, comfortably
    inside the ~20ms real-time budget per audio callback -- there was no
    actual need to change the filtering approach, only the buffer size.

    BUFFER SIZING: must comfortably hold the longest frame this receiver
    is realistically asked to decode, not just the short ping/pong
    payloads this was originally tested against. Every ping/pong test
    frame (22 bytes) fit in well under a second, so an undersized buffer
    went completely unnoticed through extensive over-air testing -- it
    only surfaced once real BPQ32 traffic sent a full-size (254-byte)
    AX.25 frame: the link still connected (short SABM/UA control frames
    decoded fine), but no actual data ever got through, because push()
    was silently discarding the front of every such frame before poll()
    had accumulated enough of it to decode.

    This happened a SECOND time, worse: once bpq_bridge.py's frame
    coalescing (bundling several AX.25 frames into one OFDM transmission)
    started working alongside BPQ32 actually using its full MAXFRAME=7
    window (after a BPQ32.cfg SLOTTIME/PERSIST fix), real transmissions
    reached 1780 bytes -- needing ~8s at QPSK, ~16s at BPSK. The buffer
    at the time was sized for "any realistic BPQ32 PACLEN" (~700 bytes,
    493 symbols, 6.6s) on the theory that the full 2047-byte header
    maximum was a theoretical ceiling "no real client sends." That
    theory was wrong: coalescing can combine enough small frames to
    approach it, and the failure mode is silent and total -- not
    corruption, not an occasional drop, but NO receive log entry at all,
    because the transmission is longer than the buffer can ever hold, so
    the front is discarded before a complete decode is possible. Sized
    now to the full MAX_PAYLOAD_LEN at BPSK (the slower mode) with
    margin, eliminating this class of mismatch regardless of what
    bpq_bridge.py's own batching limits are set to. Benchmarked at this
    size: ~12-16ms per sosfiltfilt call, comfortably inside the ~20ms
    real-time budget, and this size is only reached transiently while
    genuinely receiving a very long frame, not as a steady-state cost.
    See test_offline.py's test_long_bpq_style_frame and
    test_coalesced_batch_survives_full_maxframe_window for the
    regression guards.
    """

    CORR_THRESHOLD = 0.5
    # v0.7.3: the band-pass filter (sosfiltfilt over the whole buffer) rings
    # at the buffer's freshly trimmed oldest edge: measured +10 dB in the
    # first 64 samples, fading to ~0 by one symbol. Every poll re-filters a
    # newly trimmed buffer, so that edge looked like the start of a frame:
    # in 4 minutes of simulated white hiss, ALL noise candidates that got
    # past the energy gate were at buffer position 0, 12-15 dB above the
    # true in-band noise. Field: K6IRF's +31 dB RX gain on sound-card hiss;
    # one such candidate claimed a plausible 382-byte BPSK frame, the
    # receiver waited for it, and a real BBS frame inside that span was
    # lost (then BPQ32 waited its whole FRACK, ~26 s, to resend).
    # Fix: every trim keeps EDGE_GUARD samples of PRE-ROLL before the point
    # it used to cut at, and the preamble search starts after them -- the
    # same audio position as before, so no real preamble is missed or
    # misaligned; the filter just warms up on audio that is never searched.
    EDGE_GUARD = N_SYM
    # See _header_confidence's docstring. Calibrated empirically below.
    HEADER_CONFIDENCE_MIN = 0.15
    # See the header-energy-ratio check in poll(). Clean headers measured
    # 0.48-1.32 across 80 varied trials (p99 1.295); a strong enough
    # overlapping signal to confidently (not just ambiguously) flip
    # header bits -- the specific case HEADER_CONFIDENCE_MIN alone
    # doesn't catch -- measured 2.38, a comfortable gap above.
    HEADER_ENERGY_RATIO_MAX = 2.05     # v0.9.48: 1.8 -> 2.05; with exact preamble timing one clean header measured 1.81
    # See the weak-signal-long-wait check in poll(). Every real signal
    # this project has ever observed in the field measured quality 0.18
    # or higher (see MIN_ENERGY's own comment); a real field session
    # showed a recurring false candidate at quality 0.035-0.051 -- a
    # comfortable gap below 0.18, comfortably above the bare noise floor
    # MIN_ENERGY alone requires (~0.05) -- claiming a huge, implausible
    # length (1659 bytes at BPSK, a 14.84s wait) fourteen times across
    # one session, over 3 cumulative minutes of the receiver blinded to
    # every genuine frame arriving during each wait.
    WEAK_SIGNAL_QUALITY_MAX = 0.12
    # Comfortable margin above a real, full-size coalesced frame's own
    # duration (~1.65s for 250 bytes at QPSK, ~3.2s at BPSK -- see
    # bpq_bridge.py's MAX_BATCH_BYTES) -- short real frames at ANY
    # quality, even a weak one, are unaffected; only a weak candidate
    # ALSO claiming a long wait gets the extra scrutiny.
    MAX_WAIT_FOR_WEAK_SIGNAL_S = 4.0
    WEAK_SIGNAL_SNR_DB = 10.0
    # v0.7.1 header plausibility (see poll()). Instance-overridable so the
    # 8PSK DSP and the weak-signal rule can still be tested directly.
    RX_MODULATIONS = (MOD_BPSK, MOD_QPSK, MOD_QPSK_WIDE, MOD_QAM16_WIDE)
    # Largest on-air size a real frame can have: one maximal AX.25 frame
    # (PACLEN up to 256 + a 72-byte header with 8 digipeaters) alone in a
    # batch, + batch wrapper + CRC + FEC parity = ~382 bytes; a coalesced
    # batch is capped at 250 bytes of frames (~320 on air). 400 leaves
    # margin; test_offline.py checks the bridge can never exceed it.
    MAX_RX_DATA_BYTES = 400
    # v0.9.0: QPSK Wide uses smaller FEC blocks (more parity per byte), and a
    # frame may now carry a 9-byte link report: one maximal AX.25 frame +
    # batch wrapper + report + CRC is 343 bytes -> 391 on air in BPSK/QPSK,
    # but 415 in Wide (3 blocks). The Wide limit covers that.
    MAX_RX_DATA_BYTES_WIDE = 420
    # v0.9.4: 16-QAM Wide frames may carry TWO full AX.25 frames (see
    # bpq_bridge.MAX_BATCH_BYTES_QAM16): 2 x 343 + report = ~700 bytes, 830
    # on air with FEC -- still only ~1.2 s, shorter than one QPSK frame.
    MAX_RX_DATA_BYTES_QAM16 = 860

    def _max_rx_bytes(self, modulation: int) -> int:
        modulation = base_mod(modulation)
        if modulation in (MOD_QAM16_WIDE, MOD_QAM64_WIDE):
            return self.MAX_RX_DATA_BYTES_QAM16
        return self.MAX_RX_DATA_BYTES_WIDE if modulation == MOD_QPSK_WIDE else self.MAX_RX_DATA_BYTES
    # Noise-relative lowering of the low-energy gate (v0.7.0): a quiet but
    # clean frame -- e.g. Windows recording level set low for VARA FM --
    # is accepted if it's at least RX_GATE_MARGIN_DB above the engine's
    # noise floor, down to a hard floor of RX_GATE_ABS_MIN_DBFS.
    RX_GATE_MARGIN_DB = 12.0
    RX_GATE_ABS_MIN_DBFS = -60.0

    def effective_min_energy(self) -> float:
        """v0.9.41: the energy gate no longer depends on input level. Before,
        a frame below ~-30 dBFS was dropped unless the noise floor was quiet
        -- so with RX gain 0 dB and a weak input (VARA FM decodes the same
        station at a -35 dB VU) nothing was heard and Auto-Tune had nothing
        to tune from. What the old fixed gate protected against -- noise or a
        steady tone looking like a preamble -- is now rejected by shape: the
        preamble's repetition S/N (RX_MIN_PREAMBLE_SNR_DB) and its spread
        over the carriers (PREAMBLE_SPREAD_MIN_DB). Only near-silence below
        RX_GATE_ABS_MIN_DBFS is still gated."""
        abs_rms = 10 ** (self.RX_GATE_ABS_MIN_DBFS / 20)
        return N_SYM * abs_rms ** 2

    # v0.9.41: a real preamble puts energy on all 16 carriers; a whistle
    # (which, at a multiple of 75 Hz, repeats exactly and passes the S/N
    # check) peaks in one or two. Two readings, median carrier vs strongest:
    #   raw       -- real frames, even through a radio with de-emphasis and
    #                no pre-emphasis (12 dB tilt) at CNR 6 dB: >= -16 dB;
    #                repeating in-band tones: <= -19 dB
    #   detrended -- after removing a straight-line tilt (dB per carrier):
    #                real frames >= -6 dB; repeating in-band tones <= -10 dB
    # A candidate is a tone if EITHER reading says so (v0.9.42: the raw
    # reading alone, at -15, also turned away some real tilted frames).
    PREAMBLE_SPREAD_MIN_DB = -19.0
    PREAMBLE_SPREAD_DETRENDED_MIN_DB = -8.0

    @staticmethod
    def preamble_spread(reps):
        """(raw, detrended) median-vs-strongest preamble-carrier power, dB."""
        avg = np.mean(np.stack([np.asarray(r, dtype=np.float64) for r in reps]), axis=0)
        spec = np.abs(np.fft.fft(avg[N_CP:N_CP + N_FFT])) ** 2
        pdb = 10 * np.log10(spec[CARRIER_BINS] + 1e-30)
        raw = float(np.median(pdb) - np.max(pdb))
        k = np.arange(len(pdb))
        a, b = np.polyfit(k, pdb, 1)
        r = pdb - (a * k + b)
        return raw, float(np.median(r) - np.max(r))

    @classmethod
    def preamble_spread_db(cls, reps) -> float:
        return cls.preamble_spread(reps)[0]

    def is_tone(self, reps) -> tuple:
        raw, det = self.preamble_spread(reps)
        return (raw < self.PREAMBLE_SPREAD_MIN_DB or det < self.PREAMBLE_SPREAD_DETRENDED_MIN_DB), raw, det
    # Sized to this protocol's own absolute ceiling (MAX_PAYLOAD_LEN,
    # the largest payload the 11-bit header length field can even
    # express), not a guess at what any particular client "realistically"
    # sends -- see the class docstring for why that guess failed once
    # frame coalescing entered the picture.
    MAX_BUF_SYMS = PREAMBLE_REPS + 1 + (-(-(MAX_PAYLOAD_LEN + 2) * 8 // bits_per_symbol(MOD_BPSK))) + 20

    # Absolute energy floor for a preamble candidate window. Equivalent to
    # quality ~0.05 (energy = 0.05^2 * reference preamble energy) -- about
    # 3.5x below the lowest quality this project has ever observed working
    # in the field (0.18), so real signals (including weak ones) clear it
    # with margin, while still rejecting the lowest-energy false hits.
    MIN_ENERGY = float(np.sum(PREAMBLE_SYMBOL.astype(np.float64) ** 2)) * 0.0025

    # After a successful decode, don't search for a new preamble for this
    # long. Field logs (real over-air QPSK traffic, v0.4.4/0.4.5's verbose
    # diagnostics) showed an implausible_length or crc_fail rejection,
    # with substantial real energy and a DIFFERENT, essentially random
    # decoded length each time, immediately after nearly every successful
    # decode -- not a deterministic residual of our own signal (which
    # would look similar each time), but something external arriving in a
    # narrow, consistent window right after the remote station's
    # transmission ends. The likely source: a squelch-tail/PTT-release
    # noise burst from the remote radio, a common FM artifact -- nothing
    # about OUR OWN trim margin can fix noise that isn't our own signal.
    # Since our own TX chain enforces hundreds of ms between any two
    # distinct transmissions (TX_LEAD_MS + burst + TX_TAIL_MS), a brief
    # cooldown after a decode costs nothing against genuine traffic.
    POST_DECODE_COOLDOWN_S = 0.35
    # v0.7.4: during the cooldown the receiver no longer goes deaf -- it
    # keeps searching but only ACCEPTS a candidate whose preamble S/N is at
    # least COOLDOWN_ACCEPT_SNR_DB, and stays silent about anything else.
    # The cooldown exists to ride through squelch-tail noise bursts (no
    # repeating structure: preamble S/N near or below 0 dB); the next frame
    # of a keyed burst repeats its preamble exactly (field: +20..+40 dB).
    # That lets the transmitter's inter-frame gap drop from 0.5 s (cooldown
    # + margin) to audio_io.BURST_GAP_S -- ~0.4 s saved per frame, ~2.3 s
    # per 7-frame window. Both stations need v0.7.4: an older receiver
    # stays deaf for the full cooldown and would miss frames 2..7.
    # Measured: 643 squelch-tail-like noise candidates (white/radio hiss,
    # clicks, hum, fill-tone tails) never scored above -1.2 dB; the weakest
    # QPSK frames that still decode score ~8 dB. At 10 dB, weak-link bursts
    # lost 3 of 28 frames the old deaf cooldown delivered; 4 dB sits ~5 dB
    # above the worst noise and ~4 dB below the weakest usable frame.
    COOLDOWN_ACCEPT_SNR_DB = 4.0
    # v0.7.7: minimum preamble S/N for ANY candidate. Simulated FM voice on
    # the channel made the receiver commit to four bogus "frames" (48-370
    # bytes, each leaving it deaf for that long) whose preamble S/N was
    # -12.9 to -60 dB -- the weak-signal rule only covered claims over 4 s.
    # Measured: the weakest real frames that still decode score ~4.8 dB
    # (BPSK; QPSK ~9.5); noise candidates never above -1.2 dB; voice
    # candidates -12.9 dB or lower. 2 dB sits ~3 dB under the weakest real
    # frame and well above all of them.
    RX_MIN_PREAMBLE_SNR_DB = 2.0
    # v0.9.11: below these preamble S/N figures a frame in that mode can't
    # decode (measured cliffs: QPSK ~9, QPSK Wide ~12, 16QAM Wide ~19 dB),
    # so a header claiming it is noise. Margins of 4-5 dB under the cliffs.
    MODE_MIN_SNR_DB = {MOD_QPSK: 5.0, MOD_QPSK_WIDE: 8.0, MOD_QAM16_WIDE: 14.0, MOD_QAM64_WIDE: 18.0,
                       MOD_QAM16_WIDE_C: 9.0, MOD_QAM64_WIDE_C: 13.0}

    def _mode_min_snr(self, header_code: int) -> float:
        """The lowest S/N any mode sharing this header code needs."""
        fam = HEADER_FAMILY.get(header_code, (header_code,))
        return min(self.MODE_MIN_SNR_DB.get(m, 0.0) for m in fam)

    def __init__(self):
        self.buf: np.ndarray = np.array([], dtype=np.float32)
        nyq = SAMPLE_RATE / 2
        hi = min(CARRIER_BINS[-1] * SAMPLE_RATE / N_FFT + 300, SAMPLE_RATE / 2 - 100)
        self.sos = butter(4, [50 / nyq, hi / nyq], btype='band', output='sos')
        # v0.8.0: QPSK Wide data symbols are demodulated from a separately
        # filtered copy wide enough for the outer carriers. Detection and the
        # header checks keep using the narrow filter above, so their
        # calibrated thresholds are untouched.
        hi_w = min(WIDE_BINS[-1] * SAMPLE_RATE / N_FFT + 300, SAMPLE_RATE / 2 - 100)
        self.sos_wide = butter(4, [50 / nyq, hi_w / nyq], btype='band', output='sos')
        self._total_pushed = 0      # cumulative samples ever pushed -- a sample-based clock
        self._cooldown_until = 0    # _total_pushed value at which searching may resume
        self._guard = 0             # samples at the buffer start not searched (see EDGE_GUARD)
        self._in_cooldown = False
        # Optional diagnostic hook: set to a callable(reason: str, info: dict)
        # to observe every rejected candidate (why, and with what energy/
        # length/etc) without changing any decoding behavior. Never allowed
        # to crash the receiver if it raises.
        self.on_reject = None
        # v0.9.46: sound-card clock correction (see CLOCK_FIX_MIN_PPM)
        self._ratio = 1.0           # incoming audio is read at this rate (1 + ppm*1e-6)
        self._rs_in = np.array([], dtype=np.float64)   # input not yet resampled
        self._rs_pos = 0.0          # fractional read position in _rs_in
        self.on_clock_fix = None    # callable(total_ppm) when the correction changes
        self._clock_trial = None    # v0.9.54: a correction not yet confirmed by a decode
        self.clock_reverts = 0
        # v0.9.49: cheap streaming pre-detector (see GATE_M)
        self._g_zi = None
        self._g_hist = np.array([], dtype=np.float64)
        # the gate looks only where the preamble is (its 16 carriers,
        # 469-1875 Hz): de-emphasised hiss below that repeats by chance
        self._g_ba = butter(2, [400 / nyq, 2000 / nyq], btype='band')   # low order: lfilter is cheap
        self._g_hot_until = 0
        self.gate_skips = 0

    # v0.9.46, field (DW3TDJ <-> DU3FS, 0.9.45): VARA FM pings both ways while
    # SARA FM decoded nothing -- DU3FS turned away 19 candidates at the header
    # / FEC stage for DW3TDJ's 19 pings (low_header_confidence 15,
    # fec_uncorrectable 4) with a clean preamble each time, while DU3FS and
    # DW1IRF worked fine. That is the signature of a sound card whose clock is
    # off: cheap "USB PnP Sound Device" dongles can run 0.2-1% fast or slow,
    # and the receiver tolerates only about +-1500 ppm (0.15%). The preamble's
    # four identical repetitions show the offset directly (each carrier's
    # phase slides by an amount proportional to its frequency), measured to
    # about +-100 ppm. Above CLOCK_FIX_MIN_PPM the incoming audio is resampled
    # to undo it -- for the frame being received and everything after, until
    # another station's preamble says otherwise.
    CLOCK_FIX_MIN_PPM = 500.0
    CLOCK_FIX_MAX_PPM = 20000.0
    # v0.9.54, field (DW3TDJ, 0.9.53): a noise candidate (preamble S/N 2-3 dB,
    # "offset" 13000-23000 ppm) repeated 1 dB more cleanly once resampled --
    # by chance -- and a 1.59% correction was applied with nobody on the air.
    # Everything received after that was warped: DW3TDJ went deaf to DU3FS
    # (which kept hearing and answering DW3TDJ) until SARA FM was restarted.
    # Now the corrected preamble must be clean in its own right, and the
    # correction is only a TRIAL until a header or frame decodes with it;
    # otherwise it is undone after CLOCK_TRIAL_S.
    CLOCK_FIX_MIN_SNR_DB = 8.0
    CLOCK_TRIAL_S = 3.0
    # v0.9.59, field (DV1UK digipeater, 0.9.58): a burst of weak junk
    # candidates (clock estimates scattered -6000..+12000 ppm) set a -8841 ppm
    # trial; one HEADER then passed -- the frame itself failed FEC -- and that
    # confirmed it. DV1UK went deaf to both ends and the session died. Now only
    # a whole frame decoded (CRC good) confirms a correction; a header passing
    # just keeps the trial going until that frame is in. And a confirmed
    # correction is dropped again if real-looking frames keep failing with no
    # good decode for CLOCK_WATCHDOG_S -- a digipeater hears several stations,
    # and a correction right for one is wrong for the others.
    CLOCK_WATCHDOG_S = 15.0
    CLOCK_WATCHDOG_FAILS = 3

    @staticmethod
    def preamble_ppm(reps) -> float:
        """Sample-clock offset (ppm) of the transmitter relative to us, from
        the preamble repetitions: positive = the frame arrives stretched."""
        specs = [np.fft.fft(np.asarray(r, dtype=np.float64)[N_CP:N_CP + N_FFT]) for r in reps]
        k = CARRIER_BINS.astype(np.float64)
        # coarse: adjacent repetitions (unambiguous to ~+-2%)
        z = sum(specs[i + 1][CARRIER_BINS] * np.conj(specs[i][CARRIER_BINS]) for i in range(len(specs) - 1))
        w = np.abs(z)
        if not np.any(w):
            return 0.0
        slope = np.sum(w * k * np.angle(z)) / np.sum(w * k * k)
        dt1 = -slope * N_FFT / (2 * np.pi)                     # samples per repetition
        # fine: first vs last repetition, unwrapped with the coarse estimate
        span = len(specs) - 1
        z2 = specs[-1][CARRIER_BINS] * np.conj(specs[0][CARRIER_BINS])
        pred = -2 * np.pi * k * dt1 * span / N_FFT
        ph = pred + np.angle(z2 * np.exp(-1j * pred))
        w2 = np.abs(z2)
        if np.any(w2):
            slope2 = np.sum(w2 * k * ph) / np.sum(w2 * k * k)
            dt = -slope2 * N_FFT / (2 * np.pi) / span
        else:
            dt = dt1
        return float(dt / N_SYM * 1e6)

    def _clock_rescue(self, filtered, start, need_snr_db: float) -> bool:
        """v0.9.46: beyond ~0.6% clock offset the preamble's repetitions no
        longer line up well enough for the S/N check. If undoing the offset
        the preamble itself suggests makes them repeat cleanly (S/N at least
        3 dB over the threshold), it was a real preamble: correct and look
        again. Noise never cleans up this way, so it can't set a false
        correction."""
        if getattr(self, "_clock_trial", None) is not None:
            return False                         # v0.9.54: one trial at a time
        span = N_SYM * (PREAMBLE_REPS + 1)
        if start + span > len(filtered):
            return False
        reps = [filtered[start + r * N_SYM: start + (r + 1) * N_SYM] for r in range(PREAMBLE_REPS)]
        try:
            ppm = self.preamble_ppm(reps)
        except Exception:
            return False
        if not (self.CLOCK_FIX_MIN_PPM <= abs(ppm) <= self.CLOCK_FIX_MAX_PPM):
            return False
        r = 1.0 + ppm * 1e-6
        seg = filtered[start:start + span].astype(np.float64)
        n = int(np.floor((len(seg) - 1) / r)) + 1
        fixed = np.interp(np.arange(n) * r, np.arange(len(seg)), seg)
        if len(fixed) < N_SYM * PREAMBLE_REPS:
            return False
        reps2 = [fixed[i * N_SYM:(i + 1) * N_SYM] for i in range(PREAMBLE_REPS)]
        if self.preamble_snr_db(reps2) < max(need_snr_db, self.CLOCK_FIX_MIN_SNR_DB):
            return False
        # v0.9.54: a steady TONE can be stretched to repeat perfectly at the
        # preamble lag (the DW3TDJ case: a -24 dBFS tone, "15918 ppm") -- a real
        # preamble, corrected, still has its energy on all 16 carriers
        try:
            if self.is_tone(reps2)[0]:
                return False
        except Exception:
            return False
        prev = self._ratio
        abs_a = self._apply_clock_fix(start, ppm)
        self._clock_trial = {"prev": prev, "r": 1.0 + ppm * 1e-6, "abs": abs_a,
                             "until": self._total_pushed + int(self.CLOCK_TRIAL_S * SAMPLE_RATE)}
        return True

    def _confirm_clock(self):
        """A header/frame decoded with the trial correction: keep it."""
        if self._clock_trial is None:
            return
        self._clock_trial = None
        if self.on_clock_fix is not None:
            try:
                self.on_clock_fix(self.clock_ppm)
            except Exception:
                pass

    def _revert_clock(self):
        """Nothing decoded with the trial correction: undo it, and un-warp the
        audio buffered since it was applied."""
        t, self._clock_trial = self._clock_trial, None
        if t is None:
            return
        idx = max(0, t["abs"] - (self._total_pushed - len(self.buf)))
        seg = self.buf[idx:].astype(np.float64)
        r = t["r"]
        if len(seg) > 1:
            n = int(np.floor((len(seg) - 1) * r)) + 1
            back = np.interp(np.arange(n) / r, np.arange(len(seg)), seg).astype(np.float32)
            self.buf = np.concatenate([self.buf[:idx], back])
        self._ratio = t["prev"]
        self._pending_until = None
        self._guard = self.EDGE_GUARD
        self.clock_reverts += 1

    # v0.9.48, field (DW3TDJ <-> DU3FS, recordings): clean preambles (S/N 26
    # dB) yet garbled headers both ways. The recordings showed the detector
    # firing 260-370 samples EARLY -- more than the 128-sample guard -- so
    # the header was read across a symbol boundary. The audio before the
    # preamble (the fill tone, plus its 2nd harmonic from an overdriven
    # path) already correlated at the repetition lag. The plateau can't
    # tell where the preamble really starts; the known preamble waveform can.
    REFINE_SPAN = N_SYM * 3 // 4           # search +-480 samples around the detector's guess
    _tmpl_cache = {}

    def _preamble_template(self) -> np.ndarray:
        key = id(self.sos)
        t = OFDMReceiver._tmpl_cache.get(key)
        if t is None:
            raw = np.tile(PREAMBLE_SYMBOL.astype(np.float64), PREAMBLE_REPS)
            pad = np.concatenate([np.zeros(N_SYM), raw, np.zeros(N_SYM)])
            t = sosfiltfilt(self.sos, pad)[N_SYM:N_SYM + len(raw)]
            OFDMReceiver._tmpl_cache[key] = t
        return t

    def _refine_start(self, filtered, start):
        """Start of the first preamble repetition, from cross-correlation
        with the (receive-filtered) preamble around the detector's guess.
        None if the buffer doesn't reach far enough yet."""
        tmpl = self._preamble_template()
        a = max(0, start - self.REFINE_SPAN)
        b = start + self.REFINE_SPAN + len(tmpl)
        if b > len(filtered):
            return None
        seg = filtered[a:b].astype(np.float64)
        from scipy.signal import fftconvolve
        xc = fftconvolve(seg, tmpl[::-1], mode="valid")
        # energy-normalised, so a louder stretch can't win on level alone
        e = np.concatenate([[0.0], np.cumsum(seg ** 2)])
        en = np.sqrt(np.maximum(e[len(tmpl):] - e[:-len(tmpl)], 1e-20))[:len(xc)]
        score = np.abs(xc) / en
        best = int(np.argmax(score))
        new = a + best
        # keep the detector's guess when it is already within the guard interval
        return start if abs(new - start) <= N_CP // 4 else new

    @property
    def clock_ppm(self) -> float:
        """The clock correction in use (0 = none)."""
        return (self._ratio - 1.0) * 1e6

    def _resample_in(self, samples: np.ndarray) -> np.ndarray:
        """Streaming linear-interpolation resampler at self._ratio."""
        inp = np.concatenate([self._rs_in, samples.astype(np.float64)])
        if len(inp) < 2:
            self._rs_in = inp
            return np.array([], dtype=np.float32)
        n = int(np.floor((len(inp) - 1 - self._rs_pos) / self._ratio)) + 1
        if n <= 0:
            self._rs_in = inp
            return np.array([], dtype=np.float32)
        pos = self._rs_pos + np.arange(n) * self._ratio
        out = np.interp(pos, np.arange(len(inp)), inp)
        nxt = pos[-1] + self._ratio
        cut = int(np.floor(nxt))
        self._rs_in = inp[cut:]
        self._rs_pos = nxt - cut
        return out.astype(np.float32)

    def _apply_clock_fix(self, start: int, ppm: float):
        """Undo a further `ppm` from buffer position `start` on, and read all
        later audio the same way."""
        r = 1.0 + ppm * 1e-6
        a = max(0, start - self.EDGE_GUARD)
        seg = self.buf[a:].astype(np.float64)
        n = int(np.floor((len(seg) - 1) / r)) + 1 if len(seg) > 1 else 0
        fixed = (np.interp(np.arange(n) * r, np.arange(len(seg)), seg).astype(np.float32)
                 if n > 0 else np.array([], dtype=np.float32))
        abs_a = self._total_pushed - len(self.buf) + a
        self.buf = np.concatenate([self.buf[:a], fixed])
        self._ratio *= r
        return abs_a

    RECEIVING_HOLD_S = 0.3

    def receiving_frame(self) -> bool:
        """True while a frame that passed every header check is still
        arriving (and for RECEIVING_HOLD_S after the last such poll) -- used
        by the SARA-PP-signal DCD (v0.7.7): proof a SARA-PP station is on the
        air that no amount of hiss can fake."""
        mark = getattr(self, "_receiving_mark", None)
        return mark is not None and self._total_pushed - mark <= self.RECEIVING_HOLD_S * SAMPLE_RATE

    QUIET_REJECTS = ("low_energy", "not_preamble", "low_preamble_snr")

    def _report_reject(self, reason: str, **info):
        if getattr(self, "_in_cooldown", False):
            return     # cooldown stays silent, as it was when it didn't search at all
        if reason not in self.QUIET_REJECTS and abs(self._ratio - 1.0) > 1e-9:
            pre = (getattr(self, "_cand_info", None) or {}).get("pre_snr_db", info.get("snr_db"))
            if pre is not None and pre >= self.CLOCK_FIX_MIN_SNR_DB:
                self._wd_fails = getattr(self, "_wd_fails", 0) + 1     # v0.9.59
        if reason not in self.QUIET_REJECTS:
            for k, v in (getattr(self, "_cand_info", None) or {}).items():
                info.setdefault(k, v)
        if self.on_reject is not None:
            try:
                self.on_reject(reason, info)
            except Exception:
                pass

    # v0.9.8: how far to move on after a candidate whose "preamble" doesn't
    # repeat (low_preamble_snr). Was a full PREAMBLE_REPS symbols, like the
    # other rejections -- which matters for a REAL repeated block (crawling
    # through it a symbol at a time). A candidate that doesn't repeat isn't
    # one, and skipping 4 symbols past it could jump over a real preamble
    # starting just after it. Field (v0.9.6/v0.9.7, K6IRF -> DW1IRF): 20+
    # frames per test never reached the decoder -- often the first data
    # frame of a window -- with low_preamble_snr rejections logged right
    # where their preamble arrived. Half a symbol keeps that from happening.
    LOW_SNR_SKIP = N_SYM // 2

    def _skip_to(self, pos: int):
        """Drop the buffer up to pos, keeping EDGE_GUARD samples of filter
        lead-in that are not searched."""
        cut = max(0, pos - self.EDGE_GUARD)
        self.buf = self.buf[cut:]
        self._guard = pos - cut

    def drop_buffer(self):
        """Forget all buffered audio (v0.9.25+: the audio thread dropped a
        decode backlog, so what is buffered no longer joins up with what
        comes next)."""
        self.buf = self.buf[:0]
        self._guard = 0
        self._pending_until = None

    # v0.9.49: idle CPU. Every audio block used to re-filter (zero-phase) and
    # re-search the whole ~0.15 s search buffer, even with nothing on the air
    # -- ~8x the audio actually received, 22-24% of a core idle on a slow PC
    # (DW3TDJ). Now a causal copy of the same band filter runs on each new
    # block only, and the repetition metric (the detector's own test, which
    # any linear filter leaves intact on a repeating preamble) is computed
    # just for the windows the new audio completes. Only when it comes near
    # the detector's threshold -- or a frame is under way -- does the full
    # search run, for GATE_HOLD_S afterwards.
    GATE_M = 0.25                # vs CORR_THRESHOLD 0.5 (plateau sustain 0.375)
    GATE_RUN = 24                # ...held for 24 points x 8 = 192 samples (detector: 640)
    GATE_HOLD_S = 1.5

    def _gate_update(self, samples: np.ndarray):
        if len(samples) == 0:
            return
        x = samples.astype(np.float64)
        b, a = self._g_ba
        if self._g_zi is None:
            self._g_zi = lfilter_zi(b, a) * x[0]
        y, self._g_zi = lfilter(b, a, x, zi=self._g_zi)
        L = N_SYM
        h = np.concatenate([self._g_hist, y])
        keep = 2 * L + len(y) + 8 * self.GATE_RUN + 16
        if len(h) > keep:
            h = h[-keep:]
        span = 8 * self.GATE_RUN
        self._g_hist = h[-(2 * L + span + 16):]
        if len(h) < 2 * L + 1:
            return
        prod = h[:-L] * h[L:]
        sq = h[L:] ** 2
        cp = np.concatenate([[0.0], np.cumsum(prod)])
        cs = np.concatenate([[0.0], np.cumsum(sq)])
        last = len(prod) - L
        first = max(0, last - len(y) - span)
        d = np.arange(last, first - 1, -8)[::-1]          # aligned to the newest window
        if len(d) == 0:
            return
        P = np.maximum(cp[d + L] - cp[d], 0.0)
        R = cs[d + L] - cs[d]
        above = (P * P / (R * R + 1e-12)) >= self.GATE_M
        # longest run of consecutive windows above the gate level
        if not above.any():
            return
        edges = np.diff(np.concatenate([[0], above.astype(np.int8), [0]]))
        best = int(np.max(np.flatnonzero(edges == -1) - np.flatnonzero(edges == 1)))
        if best >= min(self.GATE_RUN, len(above)):
            self._g_hot_until = self._total_pushed + int(self.GATE_HOLD_S * SAMPLE_RATE)

    def push(self, samples: np.ndarray):
        if self._ratio != 1.0 or len(self._rs_in):
            samples = self._resample_in(samples)     # v0.9.46: clock correction
        self.buf = np.concatenate([self.buf, samples.astype(np.float32)])
        self._total_pushed += len(samples)
        self._gate_update(samples)
        max_len = N_SYM * self.MAX_BUF_SYMS
        if len(self.buf) > max_len:
            self.buf = self.buf[-max_len:]
            self._guard = self.EDGE_GUARD

    def _find_preamble(self, sig: np.ndarray) -> int:
        """
        Delayed-autocorrelation ("Schmidl-Cox style") timing metric.

        We deliberately repeat the SAME preamble symbol PREAMBLE_REPS times
        back-to-back, so x[d] and x[d+N_SYM] are identical (up to noise)
        for any d that falls inside the repeated block. That self-similarity
        is what we detect — we do NOT cross-correlate against a stored
        template.

        This matters: a fixed-template cross-correlation is extremely
        sensitive to sub-symbol timing offset for a multi-tone OFDM
        waveform (a few samples of misalignment decorrelates the carriers'
        phases and the match collapses). Delayed autocorrelation instead
        gives a wide, flat plateau across the whole repeated region —
        robust to exactly the kind of arbitrary alignment a real capture
        (leading silence of unknown length, unknown RX start point) hands
        you. We take the EARLIEST sample where the plateau begins, which
        marks the start of the first repetition.
        """
        L = N_SYM
        if len(sig) < 2 * L + 1:
            return -1

        x = sig.astype(np.float64)
        prod = x[:-L] * x[L:]                    # x[n] * x[n+L]
        sq   = x[L:] ** 2                          # x[n+L]^2

        # moving sums of length L via cumulative sum (fast, exact)
        cs_prod = np.concatenate([[0.0], np.cumsum(prod)])
        cs_sq   = np.concatenate([[0.0], np.cumsum(sq)])

        n_d = len(prod) - L + 1
        if n_d <= 0:
            return -1
        step = 8   # sample resolution; well within the N_CP=128 guard tolerance
        # v0.7.3: after a trim, never search the first EDGE_GUARD samples
        # (filter edge ringing) -- see EDGE_GUARD. Before any trim the buffer
        # starts at the genuine beginning of the audio, with no cut edge, and
        # a frame starting at sample 0 is legitimate.
        d = np.arange(getattr(self, "_guard", 0), n_d, step)
        if len(d) == 0:
            return -1

        P = cs_prod[d + L] - cs_prod[d]
        R = cs_sq[d + L] - cs_sq[d]
        # v0.9.48: a real preamble repeats, so x[n] and x[n+N_SYM] agree in
        # SIGN (P > 0). A tone that is half a cycle out at that lag (e.g. the
        # fill tone's 2nd harmonic, 2062.5 Hz, from a slightly overdriven
        # radio) gives P < 0 and used to raise the plateau early.
        P = np.maximum(P, 0.0)
        M = (P ** 2) / (R ** 2 + 1e-12)

        hits = np.where(M > self.CORR_THRESHOLD)[0]
        if len(hits) == 0:
            return -1

        # Require the plateau to sustain across a FULL symbol's worth of
        # points at a stricter level, not just half a symbol at a looser
        # one. A genuine preamble's plateau is wide (spans most of
        # (PREAMBLE_REPS-1)*N_SYM samples) so this costs it nothing; real
        # continuous background noise (every microphone has some -- this
        # is not the same as the clean digital silence early offline
        # tests used) occasionally clears CORR_THRESHOLD for a brief
        # window by chance, but sustaining a stricter level across a full
        # symbol is measurably less likely by chance.
        sustain_pts = max(2, N_SYM // step)
        for h in hits:
            if h + sustain_pts < len(M) and np.all(M[h:h + sustain_pts] > self.CORR_THRESHOLD * 0.75):
                return int(d[h])
        return int(d[hits[0]])

    _AGAIN = object()          # _poll_once: a candidate was turned away -- search on
    MAX_CANDIDATES_PER_POLL = 64

    def poll(self):
        """Try to decode one frame from the buffer. Returns DecodedFrame or None.

        v0.9.45: a candidate turned away (noise, a tone, a low-S/N look-alike)
        now leads straight to a search of the REST of the buffer, instead of
        waiting for the next block of audio. Field (DW1IRF, 0.9.44, RX gain
        +20 dB, idle channel): ~44 such candidates a second -- one per audio
        block -- each skipping only half a symbol while a block adds two, so
        the buffer grew to its 9 s ceiling and every block re-filtered all of
        it: the decoder at 100% of a core with nothing on the air, falling
        behind. Before v0.9.41 those quiet candidates were trimmed away by
        the level gate; loud open-squelch hiss could always do this."""
        trial = getattr(self, "_clock_trial", None)
        if (trial is not None and self._total_pushed > trial["until"]
                and getattr(self, "_pending_until", None) is None):
            self._revert_clock()                 # v0.9.54: nothing decoded with it
        self._clock_watchdog()
        if (getattr(self, "_pending_until", None) is None
                and self._total_pushed > self._g_hot_until
                and len(self.buf) >= N_SYM * (PREAMBLE_REPS + 2)):
            # v0.9.49: nothing that could be a preamble arrived -- just keep the
            # tail the full search would keep (see GATE_M)
            keep = N_SYM * (PREAMBLE_REPS * 2 + 2) + self.EDGE_GUARD
            if len(self.buf) > keep:
                self.buf = self.buf[-keep:]
                self._guard = self.EDGE_GUARD
            self.gate_skips += 1
            return None
        for _ in range(self.MAX_CANDIDATES_PER_POLL):
            r = self._poll_once()
            if r is not self._AGAIN:
                if r is not None:
                    self._last_good_at = self._total_pushed      # v0.9.59: clock watchdog
                    self._wd_fails = 0
                    if getattr(self, "_clock_trial", None) is not None:
                        self._confirm_clock()
                return r
        return None

    def _clock_watchdog(self):
        """v0.9.59: drop a confirmed clock correction that no longer works."""
        if abs(self._ratio - 1.0) < 1e-9 or getattr(self, "_clock_trial", None) is not None:
            return
        if getattr(self, "_wd_fails", 0) < self.CLOCK_WATCHDOG_FAILS:
            return
        if self._total_pushed - getattr(self, "_last_good_at", 0) < self.CLOCK_WATCHDOG_S * SAMPLE_RATE:
            return
        r = self._ratio
        self._clock_trial = {"prev": 1.0, "r": r, "abs": self._total_pushed - len(self.buf),
                             "until": self._total_pushed}
        self._revert_clock()                      # un-warps the buffer, ratio back to 1.0
        self._wd_fails = 0
        self._last_good_at = self._total_pushed
        self.clock_drops = getattr(self, "clock_drops", 0) + 1
        if self.on_clock_fix is not None:
            try:
                self.on_clock_fix(0.0)
            except Exception:
                pass

    def _poll_once(self):
        need = N_SYM * (PREAMBLE_REPS + 2)
        if len(self.buf) < need:
            return None
        # v0.9.1: a frame that passed every header check is still arriving --
        # don't re-filter and re-search the whole buffer on every audio block
        # until all of it is here (that work grew with the frame's length and
        # starved the sound card on long BPSK frames; see audio_io._rx_worker).
        # The full search below then runs exactly as before.
        pend = getattr(self, "_pending_until", None)
        if pend is not None:
            if self._total_pushed < pend:
                self._receiving_mark = self._total_pushed
                return None
            self._pending_until = None

        # Inside the post-decode cooldown (see POST_DECODE_COOLDOWN_S and
        # COOLDOWN_ACCEPT_SNR_DB): search as usual, but rejections are
        # silent and only a high-S/N preamble is accepted (checked below).
        self._in_cooldown = self._total_pushed < self._cooldown_until

        filtered = sosfiltfilt(self.sos, self.buf).astype(np.float32)
        start = self._find_preamble(filtered)
        if start < 0:
            # nothing found yet; keep a short tail in case a preamble
            # straddles this chunk boundary
            keep = N_SYM * (PREAMBLE_REPS * 2 + 2) + self.EDGE_GUARD
            if len(self.buf) > keep:
                self.buf = self.buf[-keep:]
                self._guard = self.EDGE_GUARD
            return None

        # A high correlation RATIO alone isn't enough: real continuous
        # background noise (every microphone has some -- room noise, hum,
        # self-noise; this is NOT the same as the clean digital silence
        # offline tests originally used) can occasionally self-correlate
        # at lag N_SYM by chance strongly enough to clear CORR_THRESHOLD,
        # despite carrying no real signal. Require genuine absolute energy
        # too. MIN_ENERGY is a fixed, conservative floor -- chosen well
        # below the lowest real quality this project has ever observed
        # working in the field (0.18, energy ~3.9), with a comfortable
        # margin, rather than an adaptive estimate: an adaptive floor that
        # samples "ambient" from the live buffer was tried and reverted --
        # it can accidentally sample real signal energy right at the edge
        # of a transmission and get contaminated upward over a session
        # until genuine signals can no longer clear it.
        #
        # v0.7.0: the gate may now be LOWERED (never raised) when the audio
        # engine supplies a noise floor -- see effective_min_energy(). That
        # floor is NOT sampled from the live buffer (the reverted approach
        # above): it's the quietest 1-second average over a full minute,
        # which can't creep upward unless a whole minute passes with no
        # quiet second. Worst case is exactly the old fixed gate.
        window_energy = float(np.sum(filtered[start:start + N_SYM].astype(np.float64) ** 2))
        min_energy = self.effective_min_energy()
        if window_energy < min_energy:
            ref_energy = float(np.sum(PREAMBLE_SYMBOL.astype(np.float64) ** 2))
            self._report_reject("low_energy", energy=window_energy, threshold=min_energy,
                                 quality_equiv=(window_energy / ref_energy) ** 0.5)
            self.buf = self.buf[start + N_SYM * PREAMBLE_REPS - self.EDGE_GUARD:]
            self._guard = self.EDGE_GUARD
            return self._AGAIN

        frame_start = start + N_SYM * PREAMBLE_REPS
        # need header + at least a little data beyond it before we try
        if len(filtered) < frame_start + N_SYM * 2:
            return None

        # --- channel estimate from the preamble reps (averaged for noise robustness) ---
        reps = [filtered[start + r * N_SYM: start + (r + 1) * N_SYM] for r in range(PREAMBLE_REPS)]
        tone, spread, spread_det = self.is_tone(reps)
        if tone:
            # v0.9.41: a tone (whistle, hum), not a preamble -- handled exactly
            # like the old low-energy rejection, which used to catch quiet ones
            self._report_reject("not_preamble", spread_db=spread, detrended_db=spread_det)
            self.buf = self.buf[start + N_SYM * PREAMBLE_REPS - self.EDGE_GUARD:]
            self._guard = self.EDGE_GUARD
            return self._AGAIN
        quality = self._preamble_quality(reps)
        snr_db = self.preamble_snr_db(reps)
        level_dbfs = self.preamble_level_dbfs(reps)
        if snr_db < self.RX_MIN_PREAMBLE_SNR_DB and not self._in_cooldown and self._clock_rescue(
                filtered, start, self.RX_MIN_PREAMBLE_SNR_DB + 3.0):
            return self._AGAIN      # v0.9.46: a preamble smeared by a far-off sound-card clock
        if snr_db < self.RX_MIN_PREAMBLE_SNR_DB and not self._in_cooldown:
            # v0.7.7: not a real SARA-PP preamble (a real one repeats
            # exactly). Rejected before reading the header at all, so no
            # garbled header can make the receiver wait -- see
            # RX_MIN_PREAMBLE_SNR_DB.
            self._report_reject("low_preamble_snr", snr_db=snr_db, energy=window_energy)
            self._skip_to(start + self.LOW_SNR_SKIP)
            return self._AGAIN
        if self._in_cooldown and snr_db < self.COOLDOWN_ACCEPT_SNR_DB:
            # Cooldown: not a clean repeated preamble -- most likely the
            # squelch-tail burst this window exists for. Skip it silently.
            self._skip_to(start + self.LOW_SNR_SKIP)
            return self._AGAIN

        # v0.9.48: fine timing against the known preamble (see _refine_start)
        ref = self._refine_start(filtered, start)
        if ref is None:
            return None                         # need a little more audio
        if ref != start:
            start = ref
            frame_start = start + N_SYM * PREAMBLE_REPS
            if len(filtered) < frame_start + N_SYM * 2:
                return None
            reps = [filtered[start + r * N_SYM: start + (r + 1) * N_SYM] for r in range(PREAMBLE_REPS)]
            quality = self._preamble_quality(reps)
            snr_db = self.preamble_snr_db(reps)
            level_dbfs = self.preamble_level_dbfs(reps)
            window_energy = float(np.sum(filtered[start:start + N_SYM].astype(np.float64) ** 2))

        # v0.9.46: undo the transmitter's sound-card clock offset, then look again
        # (only when undoing it makes the preamble repeat more cleanly -- at a
        # weak signal a noisy estimate must not mis-correct a good frame)
        if self._clock_rescue(filtered, start, snr_db + 1.0):
            return self._AGAIN
        # v0.9.47: what the preamble looked like, attached to any later rejection
        # of this frame (header, length, FEC, CRC) for the log
        try:
            est = self.preamble_ppm(reps)
        except Exception:
            est = 0.0
        self._cand_info = {"pre_snr_db": round(float(snr_db), 1), "level_dbfs": round(float(level_dbfs), 1),
                           "clock_ppm_est": int(round(est)), "clock_fix_ppm": int(round(self.clock_ppm)),
                           "spread_raw_db": round(float(spread), 1), "spread_det_db": round(float(spread_det), 1)}

        h_est = self._estimate_channel(reps)

        # --- header symbol ---
        hdr_start = frame_start
        hdr_end = hdr_start + N_SYM
        if hdr_end > len(filtered):
            return None
        # header is ALWAYS BPSK, regardless of the data modulation -- see
        # the MOD_BPSK/MOD_QPSK comment near the top of the file.
        # Energy-consistency sanity check: a header symbol carrying
        # noticeably MORE energy than the preamble reps did is a sign
        # something else is ALSO being received at exactly that moment
        # (two real signals sum to more power than either alone) --
        # catches a case the confidence check below cannot: a strong
        # enough overlap can flip header bits CONFIDENTLY to a wrong
        # value, not just ambiguously, which looks fine to a check that
        # only measures distance from the decision boundary. Checked
        # first since it's cheaper and independent of the bit decisions
        # themselves. See HEADER_ENERGY_RATIO_MAX's own comment for the
        # calibration data (clean headers: 0.48-1.32; the specific
        # confidently-wrong case this exists for: 2.38).
        preamble_energies = [float(np.sum(r.astype(np.float64) ** 2)) for r in reps]
        hdr_energy = float(np.sum(filtered[hdr_start:hdr_end].astype(np.float64) ** 2))
        mean_preamble_energy = np.mean(preamble_energies)
        header_energy_ratio = (hdr_energy / mean_preamble_energy) if mean_preamble_energy > 0 else 0.0
        if header_energy_ratio > self.HEADER_ENERGY_RATIO_MAX:
            self._report_reject("header_energy_mismatch", header_energy_ratio=header_energy_ratio,
                                 energy=window_energy)
            self.buf = self.buf[start + N_SYM * PREAMBLE_REPS - self.EDGE_GUARD:]
            self._guard = self.EDGE_GUARD
            return None

        hdr_bits = self._demod_symbol(filtered[hdr_start:hdr_end], h_est, DATA_POS, MOD_BPSK)

        # Soft-decision sanity check BEFORE committing to anything below:
        # a header whose bit decisions are wildly uneven in confidence is
        # a strong sign of localized corruption (e.g. a brief overlap
        # landing on exactly this one symbol), even when the preamble
        # and the rest of the frame are clean enough to give a high
        # overall quality score. Catching this HERE, before computing
        # n_data_bytes or total_end, matters because a corrupted header
        # can easily decode to a large, structurally-valid-looking
        # length (not caught by the invalid_length or implausible_length
        # checks below, which only catch out-of-range or buffer-
        # exceeding claims) -- committing to that claim would mean
        # waiting out its entire (possibly many-second) duration before
        # ever attempting a real decode, during which every genuine
        # frame that arrives in the meantime is missed entirely. See
        # _header_confidence's own docstring for the field evidence.
        header_conf = self._header_confidence(filtered[hdr_start:hdr_end], h_est)
        if header_conf < self.HEADER_CONFIDENCE_MIN:
            self._report_reject("low_header_confidence", header_confidence=header_conf,
                                 energy=window_energy)
            self.buf = self.buf[start + N_SYM * PREAMBLE_REPS - self.EDGE_GUARD:]
            self._guard = self.EDGE_GUARD
            return None

        modulation = 0
        for b in hdr_bits[:HEADER_MOD_BITS]:
            modulation = (modulation << 1) | int(b)
        # Rejection skips below advance by a full PREAMBLE_REPS worth of
        # symbols, not just one: the 4 repetitions are each self-similar,
        # so skipping only N_SYM after rejecting a candidate just lands on
        # the NEXT repetition, re-triggering the same false detection and
        # crawling through an entire bogus block one symbol at a time
        # (found via a dense back-to-back stress test -- a corrupted/
        # misdetected burst took 20 rejection cycles to clear instead of
        # one, corrupting detection of every real frame after it in the
        # process).
        # v0.7.1: only modulations a current SARA-PP actually TRANSMITS.
        # 8PSK left the TX choices in v0.5.12, so a header claiming it (or
        # the unused value 3) can only be garbage -- field log: a garbled
        # header claiming 836 bytes of 8PSK made the receiver wait ~2.5s and
        # swallowed a real frame arriving inside that span.
        if modulation not in self.RX_MODULATIONS:
            self._report_reject("invalid_modulation", modulation_bit=modulation, energy=window_energy)
            self.buf = self.buf[start + N_SYM * PREAMBLE_REPS - self.EDGE_GUARD:]
            self._guard = self.EDGE_GUARD
            return None
        n_data_bytes = 0
        for b in hdr_bits[HEADER_MOD_BITS:HEADER_BITS]:
            n_data_bytes = (n_data_bytes << 1) | int(b)
        if not (0 < n_data_bytes <= MAX_PAYLOAD_LEN):
            self._report_reject("invalid_length", n_data_bytes=n_data_bytes, energy=window_energy)
            self.buf = self.buf[start + N_SYM * PREAMBLE_REPS - self.EDGE_GUARD:]
            self._guard = self.EDGE_GUARD
            return None
        # v0.7.1: no real SARA-PP frame is longer than MAX_RX_DATA_BYTES on
        # the air, and some lengths can't be produced by FEC encoding at
        # all. Rejecting both HERE, before committing to wait for the
        # claimed length, closes the blind window a garbled header opens.
        # (Field: bogus claims of 629-999 bytes; one 'invalid FEC frame
        # length 18' was only caught after waiting for it.)
        if n_data_bytes > self._max_rx_bytes(modulation) or fec.decoded_len(n_data_bytes, fec_block(modulation)) is None:
            self._report_reject("implausible_length", n_data_bytes=n_data_bytes,
                                 modulation=modulation, energy=window_energy,
                                 detail="over max frame size" if n_data_bytes > self._max_rx_bytes(modulation)
                                 else "not a valid FEC length")
            self.buf = self.buf[start + N_SYM * PREAMBLE_REPS - self.EDGE_GUARD:]
            self._guard = self.EDGE_GUARD
            return None

        # v0.9.11: a header claiming a mode this candidate's preamble is far
        # too weak to carry is noise, not a frame -- reject it now instead of
        # going deaf for the length it claims. Field (DV1UK, v0.9.10): 17
        # false "16QAM Wide" headers at preamble S/N 3-11 dB, each claiming
        # 450-810 bytes (~1 s of waiting); 16QAM needs ~19 dB to decode.
        if snr_db < self._mode_min_snr(modulation) and not self._in_cooldown:
            self._report_reject("snr_too_low_for_mode", modulation=modulation, snr_db=snr_db,
                                n_data_bytes=n_data_bytes, energy=window_energy)
            self._skip_to(start + self.LOW_SNR_SKIP)
            return None
        # v0.9.18/v0.9.24: a header code shared by several modes (16QAM / 64QAM
        # / their coded twins; QPSK Wide / coded) -- the training symbol right
        # after the header says which (see _identify_training).
        if modulation in HEADER_FAMILY:
            if len(self.buf) < hdr_end + 2 * N_SYM:
                self._receiving_mark = self._total_pushed
                self._pending_until = self._total_pushed - len(self.buf) + hdr_end + 2 * N_SYM
                return None
            lo = max(0, hdr_end - N_SYM)
            seg = sosfiltfilt(self.sos_wide, self.buf[lo:hdr_end + 2 * N_SYM]).astype(np.float32)
            modulation = self._identify_training(seg[hdr_end - lo:hdr_end - lo + N_SYM],
                                                 self.RX_FAMILY.get(modulation, HEADER_FAMILY[modulation]),
                                                 h_ref=h_est)
            if snr_db < self.MODE_MIN_SNR_DB.get(modulation, 0.0) and not self._in_cooldown:
                self._report_reject("snr_too_low_for_mode", modulation=modulation, snr_db=snr_db,
                                    n_data_bytes=n_data_bytes, energy=window_energy)
                self._skip_to(start + self.LOW_SNR_SKIP)
                return None
        n_data_bits = n_data_bytes * 8
        syms_per_frame = bits_per_symbol(modulation)
        n_coded_bits = n_data_bits
        if modulation in CODED:
            import fec_cc
            n_coded_bits = fec_cc.coded_len(n_data_bits, cc_rate(modulation))
        n_syms = -(-n_coded_bits // syms_per_frame)  # ceil div
        data_start = hdr_end + extra_symbols(modulation) * N_SYM   # after the wide training symbol
        total_end = data_start + n_syms * N_SYM

        # Weak-signal-claims-long-wait check: every real signal this
        # project has ever observed in the field measured quality 0.18
        # or higher. A candidate this weak claiming a long frame is far
        # more likely to be noise that happened to self-correlate at the
        # preamble lag than a genuine transmission -- found via a real
        # field session where this exact pattern (quality 0.035-0.051,
        # claiming a 14.84s-long BPSK frame) recurred 14 times, costing
        # over 3 cumulative minutes of the receiver blind to every real
        # frame arriving during each wait. Checked before the broader
        # implausible_length check below (which only catches claims the
        # buffer could never hold at all) since this is a tighter,
        # earlier screen -- a short claim at this same weak quality is
        # left alone; only the combination of weak AND long is rejected.
        implied_wait_s = (total_end - hdr_end) / SAMPLE_RATE
        # v0.7.0: also by S/N, not just level. "quality" is a LEVEL reading,
        # so with enough RX gain amplified noise reads above
        # WEAK_SIGNAL_QUALITY_MAX and slipped past this guard. preamble S/N
        # doesn't change with gain. No real frame comes close to the 4s wait
        # threshold (the bridge's 250-byte cap is ~2.8s even at BPSK with
        # FEC), so a long claim at low S/N is noise.
        weak = quality < self.WEAK_SIGNAL_QUALITY_MAX or snr_db < self.WEAK_SIGNAL_SNR_DB
        if weak and implied_wait_s > self.MAX_WAIT_FOR_WEAK_SIGNAL_S:
            self._report_reject("weak_signal_long_claim", n_data_bytes=n_data_bytes,
                                 modulation=modulation, quality=quality, snr_db=snr_db,
                                 implied_wait_s=implied_wait_s, energy=window_energy)
            self.buf = self.buf[start + N_SYM * PREAMBLE_REPS - self.EDGE_GUARD:]
            self._guard = self.EDGE_GUARD
            return None

        # A candidate that claims a length our own buffer cap could NEVER
        # hold cannot be legitimate -- push()'s MAX_BUF_SYMS retention cap
        # means len(buf) will never exceed that size no matter how long we
        # wait, so if total_end exceeds it, this candidate's required data
        # can mathematically never fully arrive. Without this check, a
        # false preamble-detector hit (random data can occasionally
        # self-correlate at lag N_SYM by chance, especially with QPSK's
        # denser constellation) that happens to decode a large bogus
        # header length would wait here FOREVER, permanently blocking
        # every subsequent real frame -- the receiver would never crash
        # or error, just silently stop decoding anything, ever, for the
        # rest of the session. Found via a 40-frame continuous-session
        # stress test; see test_offline.py's
        # test_implausible_header_length_does_not_stall_receiver.
        if total_end > N_SYM * self.MAX_BUF_SYMS:
            self._report_reject("implausible_length", n_data_bytes=n_data_bytes,
                                 modulation=modulation, energy=window_energy)
            self.buf = self.buf[start + N_SYM * PREAMBLE_REPS - self.EDGE_GUARD:]
            self._guard = self.EDGE_GUARD
            return None
        # v0.9.24: fine timing for the Wide modes. The preamble detector's
        # FFT window lands up to ~90-128 samples early (inside the 128-sample
        # cyclic prefix -- harmless for QPSK, at the edge for 64-QAM: offline,
        # the first frame of each 64QAM burst failed at 46 dB). The preamble's
        # channel estimate shows that offset as a steady phase step between
        # neighbouring carriers (0.703 degrees per sample); the data windows
        # are moved to sit WIDE_TIMING_TARGET samples into the prefix.
        shift = self._wide_timing_shift(h_est) if is_wide(modulation) else 0
        if total_end + max(0, shift) > len(filtered):
            # v0.7.7: mark "a frame is arriving" for the SARA-PP-signal DCD
            # (see receiving_frame); refreshed on every poll while waiting.
            self._receiving_mark = self._total_pushed
            self._pending_until = self._total_pushed - len(self.buf) + total_end + max(0, shift)
            return None   # genuinely wait for more audio -- this candidate IS satisfiable

        # Per-symbol pilot-based tracking: h_est above is a single snapshot
        # from the preamble, at the very start of the frame. That's fine
        # for a short frame (ping/pong's 22-byte payload needs only ~8
        # QPSK symbols, ~107ms) but NOT for a long one -- BPQ32's real
        # 254-byte frames need ~85 QPSK symbols, over a full second. Two
        # independent, unsynchronized sound card clocks drift against each
        # other continuously; over ~100ms that drift is negligible, but
        # over a full second it accumulates enough to flip bits in the
        # later symbols even on an otherwise clean, strong link -- found
        # via real BPQ32 traffic (crc_fail on exactly 254-byte frames at
        # quality ~0.87-0.95, the real link's normal quality, while
        # ping/pong's always-short frames never showed this). Reproduced
        # directly: a 252-byte payload with a simulated 80+ ppm clock
        # offset (realistic for two different sound cards) fails CRC with
        # this exact signature; under 80ppm it's fine. The pilot carriers
        # are transmitted specifically to make this correctable -- each
        # symbol's own pilots reveal how far the channel has drifted
        # since the preamble, and that correction is applied before
        # demodulating that symbol's data carriers.
        all_bits = []
        all_vals = []
        self._frame_snr_db = snr_db        # selects narrow-mode tracking (v0.8.6)
        if is_wide(modulation):
            wide = sosfiltfilt(self.sos_wide, self.buf).astype(np.float32)
            h0 = self._estimate_wide_channel(wide[hdr_end + shift:hdr_end + shift + N_SYM], modulation)
            tracked_h = h0
            for s in range(n_syms):
                sym = wide[data_start + shift + s * N_SYM: data_start + shift + (s + 1) * N_SYM]
                tracked_h = self._track_pilots_wide(sym, tracked_h)
                vals = self._equalized(sym, tracked_h, WIDE_BINS, WIDE_DATA_POS)
                all_vals.append(vals)
                all_bits.extend(_symbol_to_bits(vals, modulation))
            ref_h = h0
        else:
            tracked_h = h_est
            for s in range(n_syms):
                sym = filtered[hdr_end + s * N_SYM: hdr_end + (s + 1) * N_SYM]
                tracked_h = self._track_pilots(sym, tracked_h)
                vals = self._equalized(sym, tracked_h, CARRIER_BINS, DATA_POS)
                all_vals.append(vals)
                all_bits.extend(_symbol_to_bits(vals, modulation))
            ref_h = h_est

        # Diagnostic only: how much the channel drifted from the preamble
        # (or wide training symbol) to the last data symbol, in degrees --
        # see _track_pilots. Lets a real session reveal its actual drift
        # magnitude directly, rather than only knowing a long frame
        # succeeded or failed.
        with np.errstate(invalid="ignore", divide="ignore"):
            ratio = np.where(ref_h != 0, tracked_h / ref_h, 1.0)
        drift_deg = float(np.degrees(np.angle(np.mean(ratio))))

        if modulation in CODED:
            data_bytes = self._soft_decode(all_vals, modulation, n_data_bits, syms_per_frame, n_coded_bits)
        else:
            data_bytes = _bits_to_bytes(all_bits[:n_data_bits])
        # Trim a little PAST total_end, not exactly at it: _find_preamble's
        # detected 'start' can be imprecise -- naturally absorbed by the CP
        # for demodulation itself (that's what it's there for), but NOT
        # accounted for here, since total_end is computed directly from
        # that same possibly-imprecise 'start'. Left untrimmed, a real
        # residual tail of the frame we just decoded can remain in the
        # buffer and get mistaken for the start of the NEXT frame,
        # corrupting its header decode. The margin only ever discards
        # buffer content immediately after a frame we've already consumed
        # (or failed) -- never part of a frame not yet decoded.
        #
        # A margin of one N_CP was tried first and was NOT enough: field
        # logs (real over-air QPSK traffic) showed an implausible_length
        # or crc_fail rejection, with substantial real signal energy
        # (0.7-2.3, far above noise), immediately after nearly every
        # successful decode -- a residual tail still surviving the N_CP
        # margin and getting mistaken for a new candidate every single
        # time, not an occasional fluke. A full extra symbol's worth
        # (N_SYM) is used instead.
        self.buf = self.buf[total_end + N_SYM - self.EDGE_GUARD:]
        self._guard = self.EDGE_GUARD

        try:
            data_bytes, fec_corrected = fec.decode(data_bytes, fec_block(modulation))
        except fec.FecError as e:
            # Damaged beyond what FEC can repair -- the same outcome a
            # crc_fail always was before v0.6.0 (dropped; BPQ32 retries).
            self._report_reject("fec_uncorrectable", n_data_bytes=n_data_bytes,
                                 modulation=modulation, energy=window_energy,
                                 quality=quality, snr_db=snr_db, drift_deg=drift_deg, detail=str(e))
            self.buf = self.buf[start + N_SYM * PREAMBLE_REPS - self.EDGE_GUARD:]
            self._guard = self.EDGE_GUARD
            return None

        if len(data_bytes) < 2:
            self._report_reject("too_short", n_data_bytes=n_data_bytes, energy=window_energy)
            self.buf = self.buf[start + N_SYM * PREAMBLE_REPS - self.EDGE_GUARD:]
            self._guard = self.EDGE_GUARD
            return None
        payload, rx_crc = data_bytes[:-2], struct.unpack('>H', data_bytes[-2:])[0]
        if crc16(payload) != rx_crc:
            # Bad frame, silently dropped (as intended for a link test) --
            # but the buffer MUST still advance past this failed attempt.
            # Without this, a spurious preamble-detector hit that never
            # produces a valid CRC gets re-found at the exact same position
            # on every subsequent poll() forever, permanently blocking any
            # real frame that arrives after it from ever being searched for.
            self._report_reject("crc_fail", n_data_bytes=n_data_bytes, modulation=modulation,
                                 energy=window_energy, quality=quality, drift_deg=drift_deg)
            self.buf = self.buf[start + N_SYM * PREAMBLE_REPS - self.EDGE_GUARD:]
            self._guard = self.EDGE_GUARD
            return None

        self._cooldown_until = self._total_pushed + int(self.POST_DECODE_COOLDOWN_S * SAMPLE_RATE)
        frame = DecodedFrame(payload=payload, quality=quality, modulation=modulation,
                             drift_deg=drift_deg, fec_corrected=fec_corrected,
                             snr_db=snr_db, level_dbfs=level_dbfs)
        try:
            frame.carrier_stats = self._carrier_stats(payload, rx_crc, modulation, all_bits,
                                                      all_vals, n_data_bits)
        except Exception:
            frame.carrier_stats = None    # diagnostics must never cost a frame
        return frame

    def _preamble_quality(self, reps) -> float:
        rms = [float(np.sqrt(np.mean(r ** 2))) for r in reps]
        ref_rms = float(np.sqrt(np.mean(PREAMBLE_SYMBOL ** 2)))
        if ref_rms == 0:
            return 0.0
        return float(np.clip(np.mean(rms) / ref_rms, 0.0, 1.5))

    @staticmethod
    def preamble_snr_db(reps) -> float:
        """
        Signal-to-noise ratio from the repeated preamble, in dB.

        The preamble sends the SAME symbol PREAMBLE_REPS times, so between
        repetitions the signal is identical and the noise is not. The
        normalized correlation rho between two repetitions is S/(S+N),
        giving SNR = rho/(1-rho). Uses repetitions 1..3 only: repetition 0
        carries the anti-click edge taper, which would read as "noise".
        Normalized, so it does not change with volume or RX gain -- it
        measures the radio link, like VARA's S/N. Validated against known
        in-band SNR: within ~1-2 dB from 10 to ~37 dB, identical at full
        level and at 1/20th level; saturates near ~42 dB on a very clean
        signal (residual preamble/filter mismatch), which is still plainly
        "excellent".
        """
        rhos = []
        for a, b in ((1, 2), (2, 3), (1, 3)):
            ra = np.asarray(reps[a], dtype=np.float64)
            rb = np.asarray(reps[b], dtype=np.float64)
            denom = np.sqrt(np.dot(ra, ra) * np.dot(rb, rb))
            if denom > 0:
                rhos.append(np.dot(ra, rb) / denom)
        if not rhos:
            return -30.0
        rho = min(max(float(np.mean(rhos)), 1e-6), 0.999999)
        return float(10 * np.log10(rho / (1 - rho)))

    @staticmethod
    def preamble_level_dbfs(reps) -> float:
        x = np.concatenate([np.asarray(r, dtype=np.float64) for r in reps[1:]])
        rms = float(np.sqrt(np.mean(x ** 2))) if len(x) else 0.0
        return float(20 * np.log10(rms)) if rms > 0 else -120.0

    def _estimate_channel(self, reps) -> np.ndarray:
        """Average per-carrier complex channel response across preamble reps."""
        specs = []
        for r in reps:
            body = r[N_CP:]
            spec = np.fft.fft(body)
            specs.append(np.array([spec[b] for b in CARRIER_BINS]))
        avg = np.mean(specs, axis=0)
        known = np.array([PREAMBLE_CARRIERS[i] for i in range(NUM_CARRIERS)])
        h = avg / np.where(known != 0, known, 1)
        return h

    # v0.8.6: per-symbol tracking -- see _robust_track
    TRACK_MAX_RESID_RAD = 0.6       # pilots must fit a straight phase line this well to use its slope
    TRACK_SLOPE_MIN_SNR_DB = 15.0   # narrow modes use the phase line only on frames this strong

    @classmethod
    def _robust_track(cls, spec: np.ndarray, h_est: np.ndarray, bins, pilot_pos) -> np.ndarray:
        """Phase-line tracking (v0.8.6), used by Wide always and by narrow
        modes on strong frames (see _track_pilots).

        Field (v0.8.5): one Wide frame from K6IRF reached DW1IRF at S/N 28.7
        dB but failed FEC with a drift reading of +150 deg where its
        neighbours read about -34 -- a ~180 deg jump mid-frame. Simulated:
        dropping or repeating a few audio samples mid-frame (a USB sound card
        or Windows audio hiccup) shifts every later symbol's timing; each
        sample becomes ~12 deg of phase SLOPE across the band. Up to 8
        samples decoded; 16 killed every frame, QPSK and Wide alike (the
        field jump ~= 15 samples). Narrow mode's single common correction
        can't represent a slope at all, and Wide's line fit used phase
        angles that wrap at +-180 deg. With AX.25's go-back-N, that one lost
        frame cost several resent ones (a slower send).

        Each pilot is weighted by its channel magnitude; pilot phases are
        UNWRAPPED across frequency and a straight line (common phase +
        slope) fitted and applied to every carrier, so a timing jump becomes
        a clean slope (Wide: up to ~32 samples, narrow ~48). The symbol that
        straddles a glitch (or is hit by a burst) doesn't fit a line: its
        update is skipped, keeping the previous estimate; FEC repairs that
        symbol's few bytes."""
        idx = np.array(pilot_pos)
        exp_ = h_est[idx] * PILOT_VAL
        ok = exp_ != 0
        if ok.sum() < 2:
            return h_est
        r = spec[bins[idx]][ok] / exp_[ok]
        if not np.all(np.isfinite(r)):
            return h_est
        x = bins[idx][ok].astype(float)
        w = np.abs(exp_[ok])
        ph = np.unwrap(np.angle(r))
        slope, icpt = np.polyfit(x, ph, 1, w=w)
        resid = ph - (icpt + slope * x)
        wr = float(np.sqrt(np.sum((w * resid) ** 2) / np.sum(w ** 2)))
        gain = float(np.sum(np.abs(r) * w ** 2) / np.sum(w ** 2))
        if wr > cls.TRACK_MAX_RESID_RAD or not (0.25 < gain < 4.0):
            return h_est
        return h_est * (gain * np.exp(1j * (icpt + slope * np.asarray(bins, dtype=float))))

    def _track_pilots(self, sym: np.ndarray, h_est: np.ndarray) -> np.ndarray:
        """
        Returns an updated per-carrier channel estimate for THIS symbol,
        correcting for drift since h_est was last computed (either from
        the preamble, for the first data symbol, or from the previous
        symbol's tracked estimate) using this symbol's own pilot carriers
        as a reference. A single common complex correction (phase and
        amplitude together) is derived by averaging across the pilots and
        applied uniformly to every carrier -- adequate for the slow,
        common-to-all-carriers drift a sample-clock mismatch produces;
        not meant to track frequency-selective fading, which isn't the
        mechanism here.
        """
        spec = np.fft.fft(sym[N_CP:])
        # v0.8.6: on strong frames, the phase-line tracker (rides out audio
        # glitches, and far more clock drift). On weaker ones, 4 noisy pilots
        # can't measure a slope reliably: measured at the decoding edge, any
        # slope term cost frames (BPSK 8 -> 4-7 of 16, QPSK 10 -> 5-7), so
        # below TRACK_SLOPE_MIN_SNR_DB this is exactly the pre-v0.8.6
        # single common correction.
        if getattr(self, "_frame_snr_db", 0.0) >= self.TRACK_SLOPE_MIN_SNR_DB:
            return self._robust_track(spec, h_est, CARRIER_BINS, PILOT_POS)
        ratios = []
        for p in PILOT_POS:
            raw = spec[CARRIER_BINS[p]]
            expected = h_est[p] * PILOT_VAL
            if expected != 0:
                ratios.append(raw / expected)
        if not ratios:
            return h_est
        drift = np.mean(ratios)
        if drift == 0 or not np.isfinite(drift):
            return h_est
        return h_est * drift

    def _header_confidence(self, sym: np.ndarray, h_est: np.ndarray) -> float:
        """
        Soft-decision confidence for the header's 12 bit decisions: the
        ratio of the WEAKEST carrier's decision magnitude to the average
        across all 12. A cleanly-received header has fairly uniform
        magnitude across its carriers (ratio close to 1.0) -- a header
        corrupted by a brief collision landing specifically on that one
        symbol typically shows one or more carriers pulled sharply
        toward zero (the BPSK decision boundary) while the rest stay
        strong, dragging this ratio down. A hard bit decision alone
        can't see this: it only asks which side of zero a value landed
        on, not how close to zero it was -- two very different carrier
        magnitudes can produce the identical hard-decision bit.

        Found via real field data: a header claiming an implausible but
        structurally-valid length (768 bytes at BPSK, when the real
        traffic was 256-byte QPSK frames) with excellent overall quality
        (~0.95) -- a brief overlap (most likely the other station's own
        short transmission clipping this frame's header specifically)
        had corrupted just enough of the header's bits to produce a
        bogus modulation+length, while the REST of the frame, received
        cleanly, is what gave it that high overall quality score. Energy
        and preamble-quality checks don't catch this: both measure the
        preamble, which arrived fine: only the header symbol itself was
        hit.
        """
        body = sym[N_CP:]
        spec = np.fft.fft(body)
        mags = []
        for pos in DATA_POS:
            h = h_est[pos] if h_est[pos] != 0 else 1.0
            eq = spec[CARRIER_BINS[pos]] / h
            mags.append(abs(eq.real))
        mags = np.array(mags)
        if mags.mean() == 0:
            return 0.0
        return float(mags.min() / mags.mean())

    # ── per-carrier diagnostic (v0.8.1) ──────────────────────────────
    @staticmethod
    def _equalized(sym: np.ndarray, h: np.ndarray, bins, positions) -> np.ndarray:
        spec = np.fft.fft(sym[N_CP:])
        out = np.empty(len(positions), dtype=complex)
        for k, pos in enumerate(positions):
            hk = h[pos] if h[pos] != 0 else 1.0
            out[k] = spec[bins[pos]] / hk
        return out

    @staticmethod
    def _carrier_stats(payload, rx_crc, modulation, rx_bits, rx_vals, n_data_bits):
        """After a frame decodes (FEC + CRC good), the exact transmitted bits
        are known: re-encoding the corrected data reproduces them. Compare
        with what each data carrier delivered: per-carrier bit errors (before
        FEC) and error-vector power against the ideal constellation point,
        which gives a per-carrier S/N. Field (v0.8.0): QPSK Wide frames from
        K6IRF arrived at DW1IRF with 100% needing FEC repair (median 15
        bytes, up to the 24-byte limit) at +29 dB overall S/N, while the
        other direction was clean -- some specific carriers were being
        received badly, and this shows which."""
        tx = fec.encode(payload + struct.pack('>H', rx_crc), fec_block(modulation))
        bps = bits_per_symbol(modulation)
        bpc = BITS_PER_CARRIER[modulation]
        if modulation in CODED:
            import fec_cc
            cb = fec_cc.encode(_bytes_to_bits(tx), cc_rate(modulation))
            n_data_bits = len(cb)
            exp_bits = _payload_bits(bps, [int(b) for b in fec_cc.interleave(cb, bps)])
        else:
            exp_bits = _payload_bits(bps, _bytes_to_bits(tx))
        wide = is_wide(modulation)
        bins, positions = (WIDE_BINS, WIDE_DATA_POS) if wide else (CARRIER_BINS, DATA_POS)
        ncar = len(positions)
        errs = np.zeros(ncar)
        bits = np.zeros(ncar)
        evm = np.zeros(ncar)
        evm_n = np.zeros(ncar)
        mapper = _MODULATION_MAPPERS[modulation]
        for s_, vals in enumerate(rx_vals):
            chunk = exp_bits[s_ * bps:(s_ + 1) * bps]
            if len(chunk) < bps:
                break
            ideal = mapper(chunk)
            evm += np.abs(np.asarray(vals) - ideal) ** 2
            evm_n += 1
        for i in range(min(n_data_bits, len(rx_bits))):
            c = (i % bps) // bpc
            bits[c] += 1
            errs[c] += rx_bits[i] != exp_bits[i]
        freqs = np.array([bins[p] * SAMPLE_RATE / N_FFT for p in positions])
        return freqs, errs, bits, evm, evm_n

    # ── QPSK Wide (v0.8.0) ───────────────────────────────────────────
    @staticmethod
    def _estimate_wide_channel(sym: np.ndarray, mode=MOD_QPSK_WIDE) -> np.ndarray:
        spec = np.fft.fft(sym[N_CP:])
        if mode is True:
            mode = MOD_QAM64_WIDE
        elif mode is False or mode is None:
            mode = MOD_QPSK_WIDE
        tc = WIDE_TRAINING_CARRIERS_BY_MODE.get(mode, WIDE_TRAINING_CARRIERS)
        known = np.array([tc[i] for i in range(len(WIDE_BINS))])
        return spec[WIDE_BINS] / known

    # modes this receiver will consider for each header code (a test can
    # narrow it to imitate an older receiver)
    RX_FAMILY: dict = {}

    WIDE_TIMING_TARGET = -56          # samples: where a window too near an edge is moved to
    _DEG_PER_SAMPLE = 360.0 / N_FFT    # phase step between neighbouring bins per sample of offset

    @staticmethod
    def _wide_timing_shift(h_est) -> int:
        """v0.9.24: samples to move the Wide FFT windows by (see the caller)."""
        try:
            h = np.asarray(h_est)
            d = h[1:] * np.conj(h[:-1])
            if float(np.abs(np.sum(d))) < 0.5 * float(np.sum(np.abs(d)) or 1):
                return 0                                  # no steady step: leave it
            k = float(np.degrees(np.angle(np.sum(d)))) / OFDMReceiver._DEG_PER_SAMPLE
            if WIDE_TIMING_OK[0] <= k <= WIDE_TIMING_OK[1]:
                return 0                                  # comfortably inside the prefix
            return int(np.clip(round(OFDMReceiver.WIDE_TIMING_TARGET - k), -40, 110))
        except Exception:
            return 0

    # wide-layout positions of the bins the narrow preamble also covers
    _OVERLAP_BINS = [b for b in WIDE_BINS if b in set(CARRIER_BINS)]

    @staticmethod
    def _identify_training(sym: np.ndarray, candidates, h_ref=None) -> int:
        """v0.9.18/v0.9.24: which wide training pattern this symbol carries.
        With the right pattern the channel estimate turns by the SAME small
        angle from each carrier to the next (a timing offset is a steady
        phase ramp); a wrong pattern flips its sign on half of them, so the
        carrier-to-carrier steps cancel out. v0.9.24: was "smoothest
        estimate", which a timing offset of a few samples could fool --
        offline, the first frame of 16QAM/64QAM bursts was misread (v0.9.23:
        every 64QAM burst frame)."""
        # With h_ref (the preamble's channel estimate, same timing), the
        # right pattern is simply the one that matches it on the bins both
        # cover -- immune to any timing offset.
        if h_ref is not None:
            wpos = [list(WIDE_BINS).index(b) for b in OFDMReceiver._OVERLAP_BINS]
            npos = [list(CARRIER_BINS).index(b) for b in OFDMReceiver._OVERLAP_BINS]
            ref = np.asarray(h_ref)[npos]

            def match(m):
                h = OFDMReceiver._estimate_wide_channel(sym, m)[wpos]
                return float(np.real(np.sum(h * np.conj(ref)))) / (
                    float(np.sum(np.abs(h) * np.abs(ref))) or 1e-12)
            return max(candidates, key=match)

        def coherence(m):
            h = OFDMReceiver._estimate_wide_channel(sym, m)
            d = h[1:] * np.conj(h[:-1])
            return float(np.abs(np.sum(d))) / (float(np.sum(np.abs(d))) or 1e-12)
        return max(candidates, key=coherence)

    def _training_is_qam64(self, sym: np.ndarray) -> bool:
        return self._identify_training(sym, (MOD_QAM16_WIDE, MOD_QAM64_WIDE)) == MOD_QAM64_WIDE

    @staticmethod
    def _soft_decode(all_vals, modulation, n_data_bits, syms_per_frame, n_coded_bits) -> bytes:
        """v0.9.24: coded modes -- per-carrier noise from decision-directed
        error vectors, max-log bit LLRs, de-interleave, soft Viterbi."""
        import fec_cc
        vals = np.array(all_vals)                       # (n_syms, n_carriers)
        base = base_mod(modulation)
        hard_bits = _symbol_to_bits(vals.reshape(-1), base)
        ideal = _MODULATION_MAPPERS[base](hard_bits).reshape(vals.shape)
        nv = np.mean(np.abs(vals - ideal) ** 2, axis=0)
        nv = np.maximum(nv, 1e-4 + 0.02 * float(np.median(nv)))
        nv_full = np.broadcast_to(nv, vals.shape).reshape(-1)
        flat = vals.reshape(-1)
        if base == MOD_QAM16_WIDE:
            llr = fec_cc.llr_qam16(flat, nv_full)
        elif base == MOD_QAM64_WIDE:
            llr = fec_cc.llr_qam64(flat, nv_full)
        else:
            llr = fec_cc.llr_qpsk(flat, nv_full)
        llr = fec_cc.deinterleave(llr[:n_coded_bits], syms_per_frame)
        bits = fec_cc.decode(llr, n_data_bits, cc_rate(modulation))
        return _bits_to_bytes([int(b) for b in bits])

    @staticmethod
    def _track_pilots_wide(sym: np.ndarray, h_est: np.ndarray) -> np.ndarray:
        """Wide-mode tracking: the shared robust phase-line tracker over the
        wide pilots (see _robust_track)."""
        return OFDMReceiver._robust_track(np.fft.fft(sym[N_CP:]), h_est, WIDE_BINS, WIDE_PILOT_POS)

    @staticmethod
    def _demod_wide(sym: np.ndarray, h_est: np.ndarray) -> list[int]:
        spec = np.fft.fft(sym[N_CP:])
        values = []
        for pos in WIDE_DATA_POS:
            h = h_est[pos] if h_est[pos] != 0 else 1.0
            values.append(spec[WIDE_BINS[pos]] / h)
        return _symbol_to_bits(values, MOD_QPSK_WIDE)

    def _demod_symbol(self, sym: np.ndarray, h_est: np.ndarray, positions, modulation: int) -> list[int]:
        body = sym[N_CP:]
        spec = np.fft.fft(body)
        values = []
        for pos in positions:
            raw = spec[CARRIER_BINS[pos]]
            h = h_est[pos] if h_est[pos] != 0 else 1.0
            values.append(raw / h)
        return _symbol_to_bits(values, modulation)
