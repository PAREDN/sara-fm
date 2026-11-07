#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 PAREDN (Philippine Amateur Radio Emergency Data Network) / DW1IRF
"""
fm_channel.py -- FM radio channel simulator (test helper, not used on air).

Models what a real FM receiver hands the sound card, including OPEN
SQUELCH: the transmitter frequency-modulates the audio; RF noise is added
at a chosen carrier-to-noise ratio (CNR) in the receiver's IF bandwidth;
an IF filter; a phase-difference discriminator; then the radio's audio
low-pass. With no carrier, the discriminator turns RF noise into loud hiss
(what an open-squelch gateway radio outputs between transmissions); a
carrier "quiets" it, more so the stronger it is -- the FM threshold effect
appears naturally below ~10 dB CNR.

Typical narrow-band FM numbers are used: 5 kHz peak deviation for
full-scale audio, ~16 kHz IF, ~3 kHz audio bandwidth, flat audio (data
port style, no pre/de-emphasis).
"""

from __future__ import annotations

import numpy as np
from scipy.signal import butter, sosfilt

SR = 48000
DEV_HZ_FULL_SCALE = 5000.0     # peak deviation for audio amplitude 1.0
IF_HALF_BW_HZ = 8000.0         # ~16 kHz IF
AUDIO_LO_HZ, AUDIO_HI_HZ = 100.0, 3000.0

_IF_SOS = butter(6, IF_HALF_BW_HZ / (SR / 2), output="sos")
_AUDIO_SOS = butter(4, [AUDIO_LO_HZ / (SR / 2), AUDIO_HI_HZ / (SR / 2)], btype="band", output="sos")


# Radio audio paths (v0.7.7). Real gateways differ, and that changes what
# open-squelch hiss looks like:
#   "flat"    -- discriminator, flat, ~3 kHz (the v0.7.6 model)
#   "speaker" -- mic in / speaker or accessory out: TX pre-emphasis and RX
#                de-emphasis (6 dB/octave, ~300 Hz corner, as NBFM radios
#                do), ~3 kHz audio. The usual RA-Board / CM108 hookup.
#   "speaker_narrow" -- the same with a ~2.5 kHz audio roll-off
#   "data9600" -- the rear 9600 data port: flat discriminator, ~6 kHz wide
PATHS = ("flat", "speaker", "speaker_narrow", "data9600")
_DEEMPH_CORNER_HZ = 300.0


def _emph_filters():
    from scipy.signal import bilinear_zpk, zpk2sos
    w = 2 * np.pi * _DEEMPH_CORNER_HZ
    # de-emphasis: 1st-order low-pass at the corner; pre-emphasis: its inverse
    # (shelved at 10x the corner so it stays stable/bounded)
    z, p, k = bilinear_zpk([], [-w], w, SR)
    de = zpk2sos(z, p, k)
    z2, p2, k2 = bilinear_zpk([-w], [-10 * w], 10.0, SR)
    pre = zpk2sos(z2, p2, k2)
    return pre, de


_PRE_SOS, _DE_SOS = _emph_filters()


def receive(tx_audio, carrier_on, cnr_db: float, rng, out_gain: float = 0.1, path: str = "flat"):
    """tx_audio: transmitter audio (float, |x|<=1), same length as
    carrier_on (bool array: True where the far transmitter is keyed).
    Returns the receiver's audio output (open squelch), scaled by
    out_gain (stands in for the radio's volume / sound-card level)."""
    tx_audio = np.asarray(tx_audio, dtype=np.float64)
    if path in ("speaker", "speaker_narrow"):
        tx_audio = sosfilt(_PRE_SOS, tx_audio) / 10.0 * 2.0   # pre-emphasis, level-normalised
    n = len(tx_audio)
    phase = 2 * np.pi * DEV_HZ_FULL_SCALE * np.cumsum(tx_audio) / SR
    carrier = np.exp(1j * phase) * np.asarray(carrier_on, dtype=np.float64)
    # complex noise whose power, after the IF filter, is 1/CNR of the carrier
    noise = (rng.normal(0, 1, n) + 1j * rng.normal(0, 1, n)) / np.sqrt(2)
    noise_if_power = (2 * IF_HALF_BW_HZ / SR)
    noise *= np.sqrt(10 ** (-cnr_db / 10) / noise_if_power)
    x = sosfilt(_IF_SOS, carrier + noise)
    d = np.angle(x[1:] * np.conj(x[:-1])) * SR / (2 * np.pi)      # instantaneous freq, Hz
    d = np.concatenate([[0.0], d]) / DEV_HZ_FULL_SCALE
    if path == "flat":
        out = sosfilt(_AUDIO_SOS, d)
    elif path == "data9600":
        out = sosfilt(_WIDE_SOS, d)
    else:
        out = sosfilt(_DE_SOS, d) * 5.0
        out = sosfilt(_NARROW_SOS if path == "speaker_narrow" else _AUDIO_SOS, out)
    return (out * out_gain).astype(np.float32)


_WIDE_SOS = butter(4, [AUDIO_LO_HZ / (SR / 2), 6000.0 / (SR / 2)], btype="band", output="sos")
_NARROW_SOS = butter(6, [AUDIO_LO_HZ / (SR / 2), 2500.0 / (SR / 2)], btype="band", output="sos")
