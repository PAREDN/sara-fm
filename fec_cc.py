# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 PAREDN (Philippine Amateur Radio Emergency Data Network) / DW1IRF
"""
fec_cc.py -- inner convolutional code with soft-decision Viterbi decoding
(v0.9.24).

Why: the Reed-Solomon code in fec.py works on hard bit decisions -- every
bit is "0" or "1", however unsure the receiver was. Field (v0.9.23 vs VARA
FM over the same DV1UK <-> DW1IRF path): VARA moved data 1.8x faster on the
weak direction (4040-7185 bps at the same ~20 dB), because a code that uses
the receiver's CONFIDENCE in each bit ("soft decisions") gains several dB.

Here: the industry-standard K=7 convolutional code (generators 171/133
octal, the one in 802.11a/g, DVB, many satellite links), punctured to rate
2/3 or 3/4, decoded with a soft-decision Viterbi decoder. The existing
Reed-Solomon code stays as the OUTER code: it cleans up the short error
bursts a Viterbi decoder leaves behind, and its CRC check still guards
every frame. Coded bits are interleaved across carriers and symbols so
that a weak carrier (field: 562/1031 Hz) or a short noise burst becomes
scattered single errors the Viterbi decoder can fix.

Pure numpy: ~5,000-bit frames decode in well under a second on a gateway PC.
"""

from __future__ import annotations

import numpy as np

K = 7
G = (0o171, 0o133)
N_STATES = 1 << (K - 1)
TAIL = K - 1

# Puncturing patterns (802.11a): per pair of mother-code outputs, 1 = sent.
PUNCTURE = {
    "1/2": np.array([[1], [1]], dtype=bool),
    "2/3": np.array([[1, 1], [1, 0]], dtype=bool),
    "3/4": np.array([[1, 1, 0], [1, 0, 1]], dtype=bool),
}
RATES = {"1/2": 0.5, "2/3": 2 / 3, "3/4": 0.75}


def _parity(x: int) -> int:
    return bin(x).count("1") & 1


# Output bits for (state, input): the register is (input << 6) | state,
# newest bit first; state = the previous 6 inputs.
_OUT = np.zeros((N_STATES, 2, 2), dtype=np.int8)
for s in range(N_STATES):
    for b in range(2):
        reg = (b << (K - 1)) | s
        _OUT[s, b, 0] = _parity(reg & G[0])
        _OUT[s, b, 1] = _parity(reg & G[1])


def _next_state(s: int, b: int) -> int:
    return ((b << (K - 1)) | s) >> 1


# Predecessors of each next state: ns = (b << 5) | (s >> 1), so the input
# bit is ns's top bit and the two predecessors differ in their lowest bit.
_PRED = np.zeros((N_STATES, 2), dtype=np.int64)
_PRED_OUT = np.zeros((N_STATES, 2, 2), dtype=np.int8)
for ns in range(N_STATES):
    b = ns >> (K - 2)
    for j in range(2):
        s = ((ns << 1) & (N_STATES - 1)) | j
        assert _next_state(s, b) == ns
        _PRED[ns, j] = s
        _PRED_OUT[ns, j] = _OUT[s, b]
_INPUT_OF = (np.arange(N_STATES) >> (K - 2)).astype(np.uint8)
# +1 / -1 form of the expected outputs (bit 0 -> +1), for correlation metrics
_SGN = (1 - 2 * _PRED_OUT.astype(np.float64))      # (64, 2 preds, 2 outputs)


def coded_len(n_bits: int, rate: str) -> int:
    """Bits on the air for n_bits info bits (tail included) at this rate."""
    pat = PUNCTURE[rate]
    steps = n_bits + TAIL
    cols = pat.shape[1]
    full, rem = divmod(steps, cols)
    return int(full * pat.sum() + pat[:, :rem].sum())


def encode(bits, rate: str = "1/2") -> np.ndarray:
    """Info bits -> punctured coded bits (zero-tail terminated)."""
    bits = np.asarray(bits, dtype=np.uint8)
    seq = np.concatenate([bits, np.zeros(TAIL, dtype=np.uint8)])
    out = np.empty((len(seq), 2), dtype=np.uint8)
    s = 0
    for i, b in enumerate(seq):
        out[i] = _OUT[s, b]
        s = _next_state(s, int(b))
    pat = PUNCTURE[rate]
    mask = np.tile(pat.T, (-(-len(seq) // pat.shape[1]), 1))[:len(seq)]
    return out[mask]


def decode(llr, n_bits: int, rate: str = "1/2") -> np.ndarray:
    """Soft Viterbi. llr: one value per coded bit, POSITIVE = bit 0 more
    likely (larger = surer). Returns the n_bits info bits."""
    pat = PUNCTURE[rate]
    steps = n_bits + TAIL
    mask = np.tile(pat.T, (-(-steps // pat.shape[1]), 1))[:steps]
    full = np.zeros((steps, 2))
    llr = np.asarray(llr, dtype=np.float64)
    need = int(mask.sum())
    if len(llr) < need:
        llr = np.concatenate([llr, np.zeros(need - len(llr))])
    full[mask] = llr[:need]                       # punctured positions stay 0 (no information)
    pm = np.full(N_STATES, -1e18)
    pm[0] = 0.0
    dec = np.empty((steps, N_STATES), dtype=np.uint8)
    p0, p1 = _PRED[:, 0], _PRED[:, 1]
    s0, s1 = _SGN[:, 0, :], _SGN[:, 1, :]
    for t in range(steps):
        l = full[t]
        m0 = pm[p0] + s0 @ l
        m1 = pm[p1] + s1 @ l
        choose1 = m1 > m0
        dec[t] = choose1
        pm = np.where(choose1, m1, m0)
        pm -= pm.max()
    # zero-terminated: trace back from state 0
    s = 0
    out = np.empty(steps, dtype=np.uint8)
    for t in range(steps - 1, -1, -1):
        out[t] = _INPUT_OF[s]
        s = int(_PRED[s, dec[t, s]])
    return out[:n_bits]


# ── interleaver ──────────────────────────────────────────────────────────
def interleave_perm(n: int, row_len: int) -> np.ndarray:
    """Block interleaver: write bits in rows of row_len (= coded bits per
    OFDM symbol), read by columns -- neighbours in the code end up on
    different carriers AND different symbols. Returns perm with
    tx[i] = coded[perm[i]]."""
    rows = -(-n // row_len)
    idx = np.arange(rows * row_len).reshape(rows, row_len)
    # spread: read column-major, skipping padding positions
    order = idx.T.reshape(-1)
    return order[order < n]


def interleave(bits, row_len: int) -> np.ndarray:
    bits = np.asarray(bits)
    # inverse sense on purpose: coded neighbours -> far apart on the air
    perm = interleave_perm(len(bits), row_len)
    out = np.empty_like(bits)
    out[perm] = bits
    return out


def deinterleave(vals, row_len: int) -> np.ndarray:
    vals = np.asarray(vals)
    perm = interleave_perm(len(vals), row_len)
    return vals[perm]


# ── soft demapping (max-log) ─────────────────────────────────────────────
def llr_qpsk(v: np.ndarray, noise_var: np.ndarray) -> np.ndarray:
    """Bits per carrier value: (real<0, imag<0) as in ofdm_core's QPSK."""
    A = 1 / np.sqrt(2)           # bit 0 -> +A on that axis
    w = 4 * A / noise_var
    return np.stack([v.real * w, v.imag * w], axis=-1).reshape(-1)


def _axis_llr16(x, nv):
    from ofdm_core import _QAM16_SCALE as A
    # bits (sign, inner): sign: 1 if x >= 0 ; inner: 1 if |x| < 2A
    # LLR convention: positive -> bit 0. sign bit is 1 for x>=0 -> LLR = -x*...
    l_sign = -4 * A * x / nv
    l_inner = 4 * A * (np.abs(x) - 2 * A) / nv
    return l_sign, l_inner


def llr_qam16(v: np.ndarray, noise_var: np.ndarray) -> np.ndarray:
    a, b = _axis_llr16(v.real, noise_var)
    c, d = _axis_llr16(v.imag, noise_var)
    return np.stack([a, b, c, d], axis=-1).reshape(-1)


def _axis_llr64(x, nv):
    from ofdm_core import _QAM64_SCALE as A
    m = np.abs(x)
    l_sign = -4 * A * x / nv                     # bit 1 for x >= 0
    l_a = 4 * A * (m - 4 * A) / nv               # bit 1 for |x| < 4A
    l_b = 4 * A * (np.abs(m - 4 * A) - 2 * A) / nv   # bit 1 for 2A < |x| < 6A
    return l_sign, l_a, l_b


def llr_qam64(v: np.ndarray, noise_var: np.ndarray) -> np.ndarray:
    a, b, c = _axis_llr64(v.real, noise_var)
    d, e, f = _axis_llr64(v.imag, noise_var)
    return np.stack([a, b, c, d, e, f], axis=-1).reshape(-1)
