# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 PAREDN (Philippine Amateur Radio Emergency Data Network) / DW1IRF
"""Test helper. Channel matching DW1IRF's measured per-carrier S/N (K6IRF -> DW1IRF, v0.8.1 carrier check)."""
import numpy as np, ofdm_core as o
SR=o.SAMPLE_RATE
# measured per-carrier EVM S/N (dB) at DW1IRF, averaged over the five reports
PROFILE_HZ = [375,  469, 562, 656, 750, 844, 1031, 1219, 1406, 1688, 1875, 2156, 2344, 2531, 2625, 2800]
PROFILE_DB = [3,    5,   3.5, 7.5, 8,   10,  10,   12,   13.5, 13.5, 11,   10,   8.5,  7.5,  7,    6]
def impair(x, rng, extra_db=5.0):
    """Add frequency-shaped noise so per-carrier S/N follows PROFILE (+extra_db)."""
    x=np.asarray(x,dtype=np.float64); n=len(x)
    S=np.abs(np.fft.rfft(x))**2/n; f=np.fft.rfftfreq(n,1/SR)
    band=(f>350)&(f<2900); sig_psd=np.median(S[band & (S>np.percentile(S[band],60))])   # per-carrier signal power level
    snr=np.interp(f, PROFILE_HZ, PROFILE_DB)+extra_db
    w=np.fft.rfft(rng.normal(0,1,n)); w*=np.sqrt(sig_psd/10**(snr/10))
    return (x+np.fft.irfft(w,n)).astype(np.float32)
