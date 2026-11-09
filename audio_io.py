#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 PAREDN (Philippine Amateur Radio Emergency Data Network) / DW1IRF
"""
audio_io.py — soundcard I/O and the transmit chain.

This is the ONLY place PTT timing, keying silence, and the burst from
ofdm_core.frame_encode() come together. ofdm_core itself stays pure DSP
(see its module docstring for why that separation is the actual fix for
the old fade-window bug) — this file is where "real world" timing lives,
in one obvious spot instead of scattered across files.

DEFAULT_TX_LEAD_MS / DEFAULT_TX_TAIL_MS are plain silence: enough time
for PTT (VOX or CM108) to key up and for the radio's relay/PA to fully
settle before the OFDM burst starts, and to release cleanly after.
This is exactly the AX.25 TXDELAY idea — every radio's actual keying
latency is different, so it's a per-station setting (AudioEngine.tx_lead_ms),
not a fixed constant. Too short and the front of the burst gets
physically clipped by a relay that hasn't closed yet (decode can still
succeed on the later preamble repetitions even when this happens, which
is why it can go unnoticed until someone is monitoring the transmission
directly). If squelch-opening turns out to be a separate issue on the
air (it might not be, discriminator audio into an already-keyed
transmitter is usually fine) the right fix is a short tone burst added
HERE, in the silence gap — never inside frame_encode().
"""

from __future__ import annotations
import collections
import queue
import random
import threading
import sys
import time
import numpy as np
import pyaudio

import ofdm_core as ofdm
from ptt import PTTController, PTT_VOX

DEFAULT_TX_LEAD_MS = 600   # silence before the burst — PTT keying / radio settle time
DEFAULT_TX_TAIL_MS = 150   # silence after the burst — clean PTT release
DEFAULT_TX_LEVEL = 0.4     # transmit amplitude (adjustable per station, v0.6.5)

# ─────────────────────────────────────────────────────────────────────────
# Carrier sense: without this, two independently-operating half-duplex
# stations (the real BPQ32/Winlink scenario, unlike ping/pong where one
# side's own transmit lock already prevents self-collision) have no way
# to know not to key up while the other is already transmitting. Found
# via field reports of one station's TX audibly stepping on the other's
# -- exactly the kind of collision that garbles both signals and forces
# BPQ32 into an ARQ retry/timeout cycle, which costs far more time than
# the brief wait below ever does.
#
# This is a simple RMS-level carrier sense (closer to real-world DCD than
# full AX.25 p-persistent CSMA): busy is "is there meaningfully more
# energy arriving than the ambient noise floor", not a protocol-aware
# decision. CARRIER_BUSY_RMS is calibrated to quality ~0.12 -- comfortably
# above the ambient-noise territory ofdm_core's own MIN_ENERGY guards
# against (~quality 0.05), comfortably below any real signal this project
# has ever observed in the field (0.87+).
CARRIER_BUSY_RMS = float(np.sqrt(np.mean(ofdm.PREAMBLE_SYMBOL.astype(np.float64) ** 2))) * 0.12
# Give up and transmit anyway rather than starve forever on a stuck
# "busy" reading. Was 4.0s; raised in v0.6.1 because burst mode (below)
# legitimately keeps the channel busy for a whole BPQ32 window -- ~13s
# for 7 QPSK frames, ~22s at BPSK. A 4s cap would make a waiting station
# give up mid-burst and transmit straight over it, recreating exactly
# the collision burst mode exists to prevent.
CARRIER_SENSE_MAX_WAIT_S = 30.0
# v0.9.12: while a session is running (a SARA-PP frame decoded within
# SESSION_ACTIVE_S), "busy" that carries no SARA-PP signal at all -- no
# fill tone, no frame arriving, nothing decoded -- for NON_SARA_WAIT_MAX_S
# is local noise or interference, not the other station: transmit anyway
# instead of waiting the full 30 s. The other station is waiting for us,
# and FM capture usually carries our frame through. Field (v0.9.11,
# DV1UK-10): waits of 10-30 s with nobody transmitting (noise floor 1 dB
# under the signal, "carrier detected by spectrum"), each one stalling the
# Winlink session into a FRACK timeout. Real traffic is unaffected: a
# station sending a burst is decoded every few seconds and its fill tone
# and arriving frames read as SARA-PP the whole time.
NON_SARA_WAIT_MAX_S = 8.0
SESSION_ACTIVE_S = 60.0

# ─────────────────────────────────────────────────────────────────────────
# Noise-relative carrier sense (v0.7.0). Until now "busy" meant RMS above
# a FIXED level (CARRIER_BUSY_RMS, -25.6 dBFS). That forced hot input
# levels: sharing a sound card with VARA FM at VARA's comfortable level,
# the fill tone arrived only ~0.3-2 dB above that fixed threshold, so the
# far end often didn't hear a keyed station and the step-on returned; and
# raising RX gain to compensate lifted the background noise toward the
# same fixed threshold. Now the engine tracks the noise floor (the
# quietest 1-second average over the last minute -- a whole BPQ32 burst
# is ~14-23s, so a minute always contains quiet) and "busy" means clearly
# above THAT. Changing the volume or RX gain moves signal and noise
# together, so the decision no longer depends on the level setting.
# CARRIER_BUSY_RMS remains the fallback for the first second after start,
# before any floor is known.
# ─────────────────────────────────────────────────────────────────────────
NOISE_BLOCK_S = 1.0
NOISE_HISTORY_BLOCKS = 60
BUSY_MARGIN_DB = 6.0
BUSY_ABS_MIN_RMS = 10 ** (-50 / 20)   # -50 dBFS: never call near-digital-silence "busy"

# Signal-anchored threshold (v0.7.2). Field: DW1IRF's carrier sense held a
# reply for 26.4 s while only short connect requests (with ~3 s silent gaps)
# were on the air. Both stations' idle input sat 1-3 dB below the busy
# threshold, because "floor + 6 dB" is measured from the QUIETEST second of
# the last minute, and real background noise is usually steady a few dB
# above its quietest moments. Reproduced as a cliff: idle noise steady at
# -50 dBFS over a -57 floor read busy 46% of the time; 1 dB more, 96%.
# Fix: once real frames have been received, put the threshold midway (in
# dB) between the noise floor and the WEAKEST recently received station,
# never closer than BUSY_MARGIN_DB to the noise and never closer than
# SIGNAL_MARGIN_DB to that station (so its fill tone, ~2.5 dB under its
# preamble level, is still heard). With DW1IRF's numbers (-55 floor,
# -20 dBFS frames) that's ~-37 dBFS: ~18 dB of margin on each side
# instead of ~1 dB above idle noise.
SIGNAL_MARGIN_DB = 10.0

# ─────────────────────────────────────────────────────────────────────────
# Spectral carrier detect (v0.7.6) -- for OPEN-SQUELCH radios. Many PAREDN
# gateways run squelch at zero (so VARA FM's input gauge shows activity).
# Then the radio outputs loud hiss whenever nobody transmits, and a carrier
# QUIETS it: measured in an FM channel simulation, idle hiss arrived ~9 dB
# LOUDER than a real SARA-PP transmission, and the loudness-based carrier
# sense above saw the other station's burst as busy 0% of the time (strong
# and weak signals alike) -- every open-squelch gateway would step on
# incoming bursts. The fix is the radio's own noise-squelch principle:
# FM hiss is strongest at high audio frequencies, so with no carrier the
# band ABOVE SARA-PP's signal (2.3-2.9 kHz) is full of hiss; any carrier
# quiets it while the signal fills 0.4-1.95 kHz. The in-band / above-band
# power ratio, measured (median): open-squelch hiss ~+2 dB; SARA-PP bursts
# +17..+27 dB at CNR 12-30 dB, +10..+13 at CNR 6-8; squelch-closed sound-
# card hiss ~0; mains hum ~+16 (signal-like!). So the ratio is judged
# against the station's own idle baseline (lowest 1-second average over the
# last minute, like the noise floor), and this rule is OR'd with the
# loudness rule -- squelch-closed stations behave exactly as before.
SPECTRAL_INBAND_HZ = (400.0, 1950.0)
SPECTRAL_ABOVE_HZ = (2300.0, 2900.0)
SPECTRAL_MARGIN_DB = 8.0       # ratio must rise this far above the idle baseline
SPECTRAL_MIN_RATIO_DB = 6.0    # ...and be clearly in-band-heavy in absolute terms

# ─────────────────────────────────────────────────────────────────────────
# DCD modes (v0.7.7), modelled on VARA FM. Field: with squelch open, a
# v0.7.6 test station would not transmit -- DCD read busy -- though a
# simulation of four radio audio paths (flat, speaker with de-emphasis,
# narrow speaker, 9600 data port) showed no false busy; the real cause is
# something the model doesn't capture. So, like VARA:
#   "auto" -- level OR spectrum OR a SARA-PP signal (v0.7.6 behaviour, plus)
#   "sara" -- busy ONLY when a SARA-PP station is actually heard: its fill
#             tone (lead-in, gaps, tail), or a frame that passed every header
#             check still arriving. Like VARA's DCD reacting to VARA signals:
#             hiss of any shape can't trip it. Won't hold off for voice or
#             other modes.
#   "off"  -- never wait (VARA's "connect anyway").
# Plus a one-shot override ("Transmit anyway") and, as in VARA, pings /
# pongs / Auto-Tune / TUNE ignore DCD altogether.
# ─────────────────────────────────────────────────────────────────────────
DCD_MODES = ("auto", "sara", "off")

# ─────────────────────────────────────────────────────────────────────────
# Yield to the reply (v0.8.4). AX.25 takes turns: when one station stops
# transmitting, the other usually answers ~0.5-1 s later. If the first
# station gets ANOTHER frame to send in that gap, the channel still reads
# clear (the reply hasn't keyed up yet), the short CSMA jitter can't help
# (both stations are reacting to the same moment), and it transmits on top
# of the reply. Field (v0.8.3): at the end of a download K6IRF sent a
# second short frame at the same moment the BBS sent its DISC -- both
# lost, and the BBS's 23 s timeout made the hang-up take ~45 s; the logs
# showed this pattern ~46 times in one test, each one a 15-23 s FRACK
# stall when it hit a control frame. So a NEW transmission waits until
# REPLY_YIELD_S after our own last transmission ended, giving the other
# station's reply time to start (DCD then holds us as usual). Bulk
# transfers are unaffected (the next window follows the other side's
# acknowledgment, long after our own TX). Pings/pongs (ignore_dcd) skip it.
# ─────────────────────────────────────────────────────────────────────────
REPLY_YIELD_S = 2.5
CONTROL_REPLY_YIELD_S = 6.5
# v0.9.3: two yields, by what we are about to send (see reply_yield_s).
# Field (v0.9.2): 4.5 s fixed the downloads' wrap-up (33-39 s -> 14-16 s)
# but one upload still collided: BPQ32 answered K6IRF's last frames ~4.3 s
# after they ended, and Winlink Express's next frame (a 19-byte control
# frame, like every collision seen so far) went out at 4.5 s. Meanwhile the
# gateway's own back-to-back text lines (BPQ32 I-frames, never seen to
# collide) were each held ~2 s longer. So a burst of control frames only
# (acks, polls, DISC/UA -- never urgent) yields CONTROL_REPLY_YIELD_S;
# one carrying data (I-frames) keeps the v0.8.7-v0.9.1 REPLY_YIELD_S.
# Both still end as soon as the other station is heard.
#
# v0.9.2: was 2.5 s. Field (v0.9.1): ALL 5 downloads ended the same way --
# K6IRF acknowledged BPQ32's last frame, Winlink Express queued its DISC at
# once, and it went out when the 2.5 s yield ran out; BPQ32 answered the
# acknowledgment ~3.1 s after K6IRF's transmission ended. Both keyed within
# ~0.1 s, both frames were lost, and the disconnect waited ~20 s for a
# retry. Measured reply starts there were up to ~3.1 s, so 4.5 s leaves a
# margin. It costs nothing when the other station does reply: the yield
# ends as soon as its carrier is heard or a frame from it is decoded.
# v0.8.7: was 1.5 s. Field (v0.8.6, 22:37:28): DW1IRF's 19-byte frame went
# out 2.0 s after its previous transmission -- just past the 1.5 s yield --
# at the same moment K6IRF keyed up its next 7-frame window; the window's
# first frame was lost and AX.25 resent the rest. A longer FIXED wait would
# slow every turn, so the yield now ends as soon as a complete frame from the
# other station has been RECEIVED since our transmission ended (the reply
# has come; DCD still holds us while the rest of their burst is on air).
# Only when nothing has been heard yet does it wait the full yield.
YIELD_SELF_TAIL_S = 1.2       # v0.9.9: our own squelch tail can't end the yield before this
REPLY_HEARD_MARGIN_S = 0.5   # frames decoded this soon after our TX may be our own echo

_NOTHING = object()    # "no item held" marker for the burst loop (v0.8.5)
_SKIPPED = object()    # tx_filter dropped the item (v0.9.61)
# v0.9.61: clear channel left to a digipeater before transmitting, when frames
# just heard or sent are still to be relayed (see link_adapt.RelayWatch). The
# digipeater's own DCD can hold up to SARA_CONTINUE_S after the sender stops,
# then its jitter and TX delay -- 4.5 s covers that.
RELAY_YIELD_S = 4.5
# v0.9.61: a pause this long after our own transmission gave the other
# station its turn to answer (see AudioEngine.turn_time).
TURN_IDLE_S = 1.5
# v0.9.66: unbroken clear channel a digipeater needs before relaying (see
# AudioEngine._hold_clear); gaps inside a sender's burst are BURST_GAP_S.
DIGI_MIN_CLEAR_S = 1.2
# ─────────────────────────────────────────────────────────────────────────
# Slow PCs (v0.9.26). Field (v0.9.24, gateway DX1PAR): "sound card input
# lost" 5-13 times every 10 s through whole sessions, and right after Start
# the decoder fell 19 s behind -- frames lost, replies late, the far end
# keying up over it. The modem itself needs ~4% of one core of a modern PC
# on any input (hiss, packet, voice); a busy or low-power PC still misses
# the sound card's 21 ms deadlines now and then.
# - The sound card now hands audio over in RX_BUFFER_FRAMES (85 ms) pieces,
#   four times the slack; they are cut back into RX_BLOCK pieces inside, so
#   level, DCD and decoding behave exactly as before.
# - A decoder more than RX_BACKLOG_DROP_S behind skips ahead to live audio:
#   frames that late are useless (the sender has already timed out), and a
#   late decoder also leaves DCD deaf to the other station.
# - The process asks Windows for "above normal" priority.
RX_BLOCK = 1024
RX_BUFFER_FRAMES = 4096
# v0.9.83: "Slow PC: larger audio buffer". Field (DV1OTT, 0.9.79): an old PC
# lost sound-card input 1-6 times every 10 s while 16QAM / QPSK Wide frames
# were arriving (decoder 30-48% busy, window freezes up to 0.6 s), so frames
# arrived with S/N 25-30 dB and still failed -- phase jumps of 50-175 degrees
# where audio was missing. Four times the buffer (341 ms of slack) rides out
# stalls like that. Cost: level meter and carrier sense see audio up to a
# third of a second later; nothing on the air changes.
RX_BUFFER_FRAMES_LARGE = 16384
RX_BACKLOG_DROP_S = 3.0


def _raise_priority():
    """Windows only: ABOVE_NORMAL_PRIORITY_CLASS for this process, so a busy
    PC gives the audio its turn first. Harmless if refused."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        k = ctypes.windll.kernel32
        k.SetPriorityClass(k.GetCurrentProcess(), 0x00008000)
    except Exception:
        pass


FILL_DETECT_DB = 15.0          # fill-tone bin vs neighbouring bins, smoothed
# v0.9.27: SARA_CONTINUE_S. Field (v0.9.26, DX1CW -> DW1IRF, BPSK): DX1CW
# sent 6-frame bursts (2.8 s frames, 19 s keyed). Every second or third
# frame DW1IRF's DCD ("SARA-PP signals only") read clear right after a
# decode -- the next frame's header had not been recognised yet -- and
# DW1IRF keyed its acknowledgment into the middle of DX1CW's burst,
# destroying the next frame each time. Simulated: one missed header leaves
# that DCD deaf for a whole BPSK frame. Now, after direct proof of a
# SARA-PP station (fill tone, a frame arriving or decoded), its CARRIER
# also counts for SARA_CONTINUE_S: the level detector, which is anchored
# between the noise floor and the weakest station heard (so open-squelch
# hiss does not trip it). Only proof heard since our own last transmission
# counts, so our own squelch tail never extends it.
SARA_CONTINUE_S = 3.5
# v0.9.28: ...and only at that station's own received level. Field (v0.9.27,
# DX1CW): DW1IRF's frames arrived at -30 dBFS, but when its carrier dropped
# DX1CW's radio put out noise at -20/-21 dBFS for seconds (squelch tail /
# open hiss), loud enough for the level detector -- every turn DX1CW waited
# out the whole 3.5 s before answering. A carrier still on the air reads
# within a few dB of the frames it carried; noise louder or quieter than
# that by more than SARA_LEVEL_MATCH_DB is not that station.
# v0.9.31: 6 -> 3 dB. Field (v0.9.30, DX1FLR <- DW1IRF): DW1IRF's frames at
# -12 dBFS, DX1FLR's radio noise after each transmission at -6 dBFS -- just
# inside 6 dB, so DX1FLR held DCD ~3.3 s after every DW1IRF transmission
# (v0.9.26, without the hold, ran the same path visibly quicker). A carrier
# still carrying a burst reads within ~1-2 dB of its frames.
SARA_LEVEL_MATCH_DB = 3.0

# v0.9.25: a STEADY tone near the fill frequency is not a SARA-PP station.
# Field (v0.9.24, DV1UK, DCD "SARA-PP signals only"): the lamp sat on
# "busy -- SARA-PP signal" for minutes with nobody transmitting -- the
# fill-tone detector read +23..+28 dB at an input of -48 dBFS (something
# at the input whistling near 1 kHz: hum harmonic, sound-card whine or an
# interfering carrier; the 1024-sample check spans ~0.96-1.1 kHz). Every
# transmission then waited the full 30 s: an 8-minute session moved 135 B.
# A real fill tone never lasts long unbroken -- it fills the lead-in, the
# gaps between frames and the tail (about a second at most), or a far-end
# TUNE (10 s). So a tone heard unbroken for FILL_STEADY_S is marked
# foreign, and for FILL_FOREIGN_HOLD_S afterwards the fill tone only
# counts for its first FILL_FOREIGN_MAX_S each time it appears (enough to
# catch a real lead-in when the steady tone returns after the other
# station's transmission).
FILL_STEADY_S = 12.0
FILL_FOREIGN_HOLD_S = 1800.0
FILL_FOREIGN_MAX_S = 2.5
SIGNAL_LEVEL_HISTORY = 20          # recent received frames remembered
SIGNAL_LEVEL_MAX_AGE_S = 600.0     # forget levels older than this
# A real collision was directly OBSERVED on the radio (not just inferred
# from logs): K6IRF keyed up and transmitted a 19-byte frame while
# DW1IRF was still mid-transmission on a 256-byte frame, stepping on it.
# This is a real, structural gap in "check once, then go" carrier sense,
# not a sensitivity problem: the single is_channel_busy() check happens
# BEFORE the TX_LEAD silence (300-600ms, needed for PTT/relay settling),
# not after it. During that silent lead-in, this station is COMMITTED to
# transmitting but produces no audible signal yet -- so if the OTHER
# station's own carrier-sense check happens to land in that exact
# window, the channel still looks clear to it too, and it commits as
# well. Both stations end up committed before either one's actual
# signal is audible to the other -- the classic CSMA blind-window
# collision, the same reason real AX.25 systems use randomized backoff
# (this project's own BPQ32 configs already carry PERSIST/SLOTTIME for
# exactly this) rather than a single check-then-go.
# CSMA_JITTER_MAX_S adds a short random delay immediately before PTT,
# with a re-check of the channel afterward: two stations that both saw
# "clear" at the same instant (e.g. both reacting to the same prior
# transmission ending) now commit at slightly different, randomized
# moments instead of the identical one, giving whichever one's jitter
# is shorter a real head start -- audible to the other before its own
# commit point, which the re-check then correctly detects as busy.
CSMA_JITTER_MAX_S = 0.3

# ─────────────────────────────────────────────────────────────────────────
# Occupancy fill (v0.6.1). Found via a field session where K6IRF keyed up
# MID-BURST between two of DW1IRF's 256-byte frames, every burst, and the
# frame after the collision was always lost. Cause: each frame was its
# own key-up with a lead-in of pure SILENCE. On FM, a keyed transmitter
# sending silence is inaudible, so for ~0.75s between frames (tail +
# re-key + 600ms lead-in) the channel looked free to the other station's
# energy-based carrier sense. Winlink Express sends mid-burst acks on its
# own response timer; K6IRF's carrier sense saw "clear" in that gap and
# keyed up; DW1IRF, already committed to its next frame, talked over it.
#
# Real packet TNCs avoid this by sending flags during TXDELAY, so a keyed
# station is audible (and DCD-detectable) from the moment it keys. The
# equivalent here: lead-in, tail, and inter-frame gaps carry a steady
# tone instead of silence.
#
# The tone frequency is NOT arbitrary. The receiver finds frames by
# looking for a signal that repeats exactly every N_SYM (640) samples; a
# tone with a whole number of cycles in 640 samples would look like an
# endless preamble. Verified: 1200 Hz (exactly 16 cycles per 640 samples)
# DID trigger false frame detections. For a tone at f, the correlation at
# that lag is proportional to cos(2*pi*f*N_SYM/SR), which is zero when
# f = 18.75 + 37.5*k Hz; 1031.25 Hz (k=27) sits mid-passband and produced
# zero false triggers in 6s, with frames inside a filled burst decoding
# 4/4. Amplitude 0.3 x tx_gain lands at ~4x the far end's busy threshold
# at normal received levels, yet below the data's own RMS (no extra
# deviation).
# ─────────────────────────────────────────────────────────────────────────
FILL_TONE_HZ = 1031.25
# Raised 0.3 -> 0.45 in v0.7.0: at 0.3 the fill arrived ~6 dB below the
# data, and at moderate (VARA-friendly) receive levels it sat barely above
# the far end's busy threshold -- the step-on came back. 0.45 x tx_gain
# puts the tone's RMS level with the data's own RMS (no extra average
# deviation; its peaks stay well below the frame's peaks).
FILL_AMPLITUDE = 0.45


def fill_tone(n_samples: int, tx_gain: float = 0.8) -> np.ndarray:
    n = np.arange(n_samples)
    return (FILL_AMPLITUDE * tx_gain *
            np.sin(2 * np.pi * FILL_TONE_HZ * n / ofdm.SAMPLE_RATE)).astype(np.float32)


# Burst mode (v0.6.1): when several frames are queued (a BPQ32 MAXFRAME
# window), key ONCE and send them all, with tone-filled gaps between, so
# the channel never goes quiet mid-burst and the other station's carrier
# sense holds off until the whole window is out. The gap must outlast the
# receiver's post-decode cooldown (it discards audio for that long after
# each decode) with margin, or the next frame's preamble is thrown away.
# v0.7.4: was POST_DECODE_COOLDOWN_S + 0.15 (0.5 s), because the receiver
# went deaf for the whole cooldown after each decode. It now accepts a
# clean (high-S/N) preamble during the cooldown -- see
# OFDMReceiver.COOLDOWN_ACCEPT_SNR_DB. Gap chosen by measurement at the
# edge of usability (S/N ~9 dB, 336 frames each): 0.5 s 232, 0.3 s 250,
# 0.2 s 241, 0.12 s 223 decoded (run-to-run spread ~+/-10). 0.2 s keeps
# weak-link performance and saves 0.3 s per frame (~1.8 s per 7-frame
# window). Still tone-filled, so far-end carrier sense stays busy.
BURST_GAP_S = 0.2
BURST_MAX_FRAMES = 8
CARRIER_SENSE_POLL_S = 0.05


def reply_yield_s(payload: bytes | None) -> float:
    """How long a burst starting with this payload yields to the other
    station's reply (v0.9.3): CONTROL_REPLY_YIELD_S if it carries only AX.25
    control frames (S/U: acks, polls, DISC, UA...), else REPLY_YIELD_S."""
    try:
        if not payload or payload[0] != 0x01:      # not a bridge batch
            return REPLY_YIELD_S
        import link_adapt                           # lazy: avoid import order issues
        frames = link_adapt._split_counted(payload)
        if not frames:
            return REPLY_YIELD_S
        for f in frames:
            p = link_adapt.ax25_parse(f)
            if p is None or link_adapt.is_iframe(p[2]):
                return REPLY_YIELD_S
        return CONTROL_REPLY_YIELD_S
    except Exception:
        return REPLY_YIELD_S


def wait_for_clear_channel(is_busy_fn, max_wait_s: float = CARRIER_SENSE_MAX_WAIT_S,
                            poll_s: float = CARRIER_SENSE_POLL_S, sleep_fn=time.sleep) -> float:
    """
    Polls is_busy_fn() and sleeps while it returns True, up to max_wait_s
    total. Returns how long it actually waited. A free function (not an
    AudioEngine method) specifically so the wait-loop logic itself is
    testable without any audio hardware -- pass a fake is_busy_fn/sleep_fn
    in a test, no pyaudio or real device involved.
    """
    waited = 0.0
    while is_busy_fn() and waited < max_wait_s:
        sleep_fn(poll_s)
        waited += poll_s
    return waited


def list_devices(all_apis: bool = True):
    """Returns [(index, name, max_input_channels, max_output_channels, default_rate), ...].
    v0.9.32: all_apis=False keeps only the system's default audio API (MME on
    Windows). Windows lists every sound card once per API (MME, DirectSound,
    WASAPI, WDM-KS), so the same interface appeared 3-4 times under
    different numbers -- field: after plugging in a second USB interface
    DW1IRF ended up on the wrong copy and decoded nothing."""
    pa = pyaudio.PyAudio()
    out = []
    try:
        default_api = pa.get_default_host_api_info().get("index") if not all_apis else None
    except Exception:
        default_api = None
    for i in range(pa.get_device_count()):
        d = pa.get_device_info_by_index(i)
        if default_api is not None and d.get("hostApi", default_api) != default_api:
            continue
        out.append((i, d['name'], d['maxInputChannels'], d['maxOutputChannels'],
                     int(d['defaultSampleRate'])))
    pa.terminate()
    return out


class AudioEngine:
    """
    Owns the input/output PyAudio streams, the PTT controller, and a
    background thread that feeds captured audio into an OFDMReceiver.
    Decoded frames land on `frame_queue`; RX level (for a simple meter)
    lands on `level_queue`.
    """

    def __init__(self, input_device: int, output_device: int,
                 ptt_method: str = PTT_VOX, ptt_kwargs: dict | None = None,
                 tx_lead_ms: int = DEFAULT_TX_LEAD_MS, tx_tail_ms: int = DEFAULT_TX_TAIL_MS,
                 rx_buffer_frames: int = RX_BUFFER_FRAMES):
        self.input_device = input_device
        self.rx_buffer_frames = int(rx_buffer_frames)
        self.output_device = output_device
        self.pa = pyaudio.PyAudio()
        self.rx_stream = None
        self.tx_stream = None
        self.running = False
        # plain instance attributes, not constructor-only: the GUI can adjust
        # these live (e.g. a "TX delay" field) without recreating the engine.
        self.tx_lead_ms = tx_lead_ms
        self.tx_tail_ms = tx_tail_ms

        self.receiver = ofdm.OFDMReceiver()
        self.frame_queue: "queue.Queue[ofdm.DecodedFrame]" = queue.Queue()
        self.level_queue: "queue.Queue[float]" = queue.Queue(maxsize=4)

        self.ptt = PTTController(method=ptt_method, **(ptt_kwargs or {}))
        self._tx_lock = threading.Lock()
        # Optional diagnostic hook: set to a callable(seconds_waited) to
        # observe meaningful carrier-sense delays (see transmit() below).
        # Added specifically because carrier sense (v0.5.0) had no
        # visibility at all -- if one station's local noise floor sits
        # persistently above CARRIER_BUSY_RMS, every one of its
        # transmissions could be eating the full wait before anything
        # obviously wrong shows up anywhere in the log.
        self.on_carrier_wait = None
        # Optional hook: callable(payload) fired right as a transmission
        # actually BEGINS (after any carrier-sense wait, after acquiring
        # _tx_lock, right before audio starts playing) -- not when
        # transmit() was merely called/queued. Added because bridge mode
        # spawns a new thread per transmission; once several queue up
        # behind the same _tx_lock (a full BPQ32 MAXFRAME burst, now that
        # the smaller coalescing cap means real frames rarely combine),
        # a later frame's transmit() call can happen long before its
        # actual turn to transmit. Anything timestamping "when was this
        # sent" (e.g. SelfEchoFilter) needs the real broadcast moment,
        # not the queue-entry moment, or its window measures the wrong
        # interval entirely.
        self.on_tx_start = None
        # Per-program audio levels (v0.6.5), so SARA-PP can share one sound
        # card with VARA FM: both read the SAME Windows recording level and
        # write to the SAME playback level. Set Windows levels for VARA,
        # then match SARA-PP with these instead of moving the shared ones.
        # rx_gain: linear multiplier on incoming audio, applied before the
        # modem, the level meter, and carrier sense all see it.
        # tx_level: transmit amplitude (was a fixed 0.8).
        self._rx_gain = 1.0
        self.tx_level = DEFAULT_TX_LEVEL
        # noise floor tracking -- see BUSY_MARGIN_DB
        self._noise_blocks = collections.deque(maxlen=NOISE_HISTORY_BLOCKS)
        self._noise_acc = 0.0
        self._noise_acc_n = 0
        self._noise_acc_samples = 0
        self.noise_floor_rms = None
        self.raw_peak = 0.0          # largest |sample| before RX gain, for clip warnings
        self._sig_levels = collections.deque(maxlen=SIGNAL_LEVEL_HISTORY)  # (time, level dBFS)
        self.last_wait_info = None   # (median level, threshold, floor) dBFS during last long wait
        self.last_wait_by = None     # which rule held the last long wait: "level"/"spectrum"/"both"
        # spectral carrier detect (v0.7.6) -- see SPECTRAL_MARGIN_DB
        self._spec_ema = None
        self._spec_blocks = collections.deque(maxlen=NOISE_HISTORY_BLOCKS)
        self._spec_acc = 0.0
        self._spec_acc_n = 0
        self._spec_acc_samples = 0
        self.spec_baseline_db = None
        self.dcd_mode = "auto"           # see DCD_MODES
        self._dcd_override_until = 0.0   # "Transmit anyway"
        self._fill_ema = None
        self._fill_on_since = None       # v0.9.25: steady-tone check -- see FILL_STEADY_S
        self._steady_tone_at = None
        self.steady_tone_count = 0
        # Fast-reacting smoothed RX level, for carrier sense -- see
        # wait_for_clear_channel() and is_channel_busy() below. Updated
        # every audio callback regardless of what the OFDM receiver itself
        # is doing (decoding, in cooldown, etc.), since carrier sense needs
        # to know "is there a signal on the channel right now", which is
        # independent of our own decode pipeline's internal state.
        self._rx_rms_ema = 0.0

    def start(self):
        self.ptt.open()
        _raise_priority()
        self.rx_stream = self.pa.open(
            format=pyaudio.paInt16, channels=1, rate=ofdm.SAMPLE_RATE,
            input=True, input_device_index=self.input_device,
            frames_per_buffer=self.rx_buffer_frames, stream_callback=self._rx_callback)
        self.tx_stream = self.pa.open(
            format=pyaudio.paFloat32, channels=1, rate=ofdm.SAMPLE_RATE,
            output=True, output_device_index=self.output_device,
            frames_per_buffer=1024)
        self.running = True
        # v0.9.1: decoding runs on its own thread, not inside the audio
        # callback -- see _rx_worker.
        self._rx_q = queue.Queue()
        self._rx_async = True
        self._rx_thread = threading.Thread(target=self._rx_worker, name="sara-rx", daemon=True)
        self._rx_thread.start()
        self.rx_stream.start_stream()

    def stop(self):
        self.running = False
        if getattr(self, "_tx_queue", None) is not None:
            self._tx_queue.put(None)
        if getattr(self, "_rx_q", None) is not None:
            self._rx_q.put(None)
        for s in (self.rx_stream, self.tx_stream):
            if s:
                try:
                    s.stop_stream()
                    s.close()
                except Exception:
                    pass
        self.ptt.close()
        self.pa.terminate()

    # v0.9.1: field (v0.9.0, K6IRF): the gateway's long BPSK frames reached
    # K6IRF at S/N 33-36 dB, yet 17 of 27 failed FEC or vanished outright
    # (every other frame of a window missing, survivors showing random
    # +-180 deg "drift"), while the same station decoded 236 of 237 Wide
    # frames and the gateway decoded K6IRF's BPSK frames fine. The decoder
    # ran INSIDE the sound card's input callback, and while a long frame
    # was arriving every callback re-filtered and re-searched the whole
    # growing buffer: work that grows with the frame's length, worst for
    # BPSK's 2.8 s frames. On a busy PC the callback falls behind and the
    # sound card drops input -- missing samples look exactly like that
    # damage. Now the callback only measures the level (DCD stays live)
    # and hands the audio to this thread; and the receiver no longer
    # re-processes a frame it is still waiting for (OFDMReceiver.poll).
    def _rx_worker(self):
        q = self._rx_q
        while True:
            samples = q.get()
            if samples is None:
                return
            backlog = q.qsize() * RX_BLOCK / ofdm.SAMPLE_RATE
            if backlog > getattr(self, "rx_backlog_max_s", 0.0):
                self.rx_backlog_max_s = backlog
            if backlog > RX_BACKLOG_DROP_S:
                # v0.9.26: see RX_BACKLOG_DROP_S
                dropped = 0
                try:
                    while q.qsize() > 1:
                        item = q.get_nowait()
                        if item is None:
                            return
                        dropped += 1
                except queue.Empty:
                    pass
                self.rx_backlog_dropped_s = (getattr(self, "rx_backlog_dropped_s", 0.0)
                                             + dropped * RX_BLOCK / ofdm.SAMPLE_RATE)
                try:
                    self.receiver.drop_buffer()
                except Exception:
                    pass
                continue
            t0 = time.perf_counter()
            c0 = time.thread_time()
            try:
                self._decode_samples(samples)
            except Exception:
                pass
            # v0.9.43: how long the decoder itself works, for the health warning
            dt = time.perf_counter() - t0
            self.decode_busy_s = getattr(self, "decode_busy_s", 0.0) + dt
            self.decode_busy_total_s = getattr(self, "decode_busy_total_s", 0.0) + dt   # v0.9.44: CPU meter
            # v0.9.53: ...and how much of that was its OWN CPU work. On a shared
            # PC (Wyse 3040 + 4 VARA FM + BPQ32) the rest is waiting for a core.
            dc = time.thread_time() - c0
            self.decode_cpu_s = getattr(self, "decode_cpu_s", 0.0) + dc
            self.decode_cpu_total_s = getattr(self, "decode_cpu_total_s", 0.0) + dc

    def start_recording(self, seconds: float):
        """v0.9.47: keep the next `seconds` of received audio exactly as the
        sound card delivers it (before RX gain), for the sysop to analyse."""
        self._rec_done = None
        self._rec_left = int(seconds * ofdm.SAMPLE_RATE)
        self._rec = []

    def recording_left_s(self) -> float:
        return max(0, getattr(self, "_rec_left", 0)) / ofdm.SAMPLE_RATE if getattr(self, "_rec", None) is not None else 0.0

    def take_recording(self):
        d = getattr(self, "_rec_done", None)
        self._rec_done = None
        return d

    def take_decode_cpu(self) -> float:
        """Decoder thread CPU seconds since the last call (v0.9.53)."""
        d = getattr(self, "decode_cpu_s", 0.0)
        self.decode_cpu_s = 0.0
        return d

    def take_decode_busy(self) -> float:
        """Seconds the decoder spent working since the last call (v0.9.43)."""
        d = getattr(self, "decode_busy_s", 0.0)
        self.decode_busy_s = 0.0
        return d

    def _decode_samples(self, samples: np.ndarray):
        self.receiver.push(samples)
        frame = self.receiver.poll()
        while frame is not None:
            lvl = getattr(frame, "level_dbfs", None)
            if lvl is not None and getattr(self, "_sig_levels", None) is not None:
                self._sig_levels.append((time.time(), lvl))
            self._last_rx_frame_time = time.time()     # yield-to-reply (v0.8.7)
            self._last_sara_evidence = self._last_rx_frame_time   # v0.9.27
            if lvl is not None:
                self._last_sara_level_db = lvl                     # v0.9.28
            self.frame_queue.put(frame)
            frame = self.receiver.poll()

    # PortAudio's code for "the sound card ran out of audio to play"
    PA_OUTPUT_UNDERFLOWED = -9980

    def _tx_write(self, data: bytes):
        """Write transmit audio, counting output underruns (v0.9.7): when
        this PC can't feed the sound card in time, the transmitted audio gets
        a gap. Field (v0.9.6): 23 of K6IRF's frames never reached DW1IRF's
        decoder at all (the preamble wasn't recognised), with K6IRF's PC
        also losing input audio -- a gap on the TRANSMIT side would explain
        it, and wasn't visible until now. The audio is still written in
        full; underflow is only reported."""
        stream = self.tx_stream
        try:
            stream.write(data, exception_on_underflow=True)
        except TypeError:                    # a stream without that option
            stream.write(data)
        except OSError as e:
            code = e.args[1] if len(e.args) > 1 else (e.args[0] if e.args else None)
            if code == self.PA_OUTPUT_UNDERFLOWED or "underflow" in str(e).lower():
                self.output_underruns = getattr(self, "output_underruns", 0) + 1
            else:
                raise

    def take_output_underruns(self) -> int:
        n = getattr(self, "output_underruns", 0)
        self.output_underruns = 0
        return n

    def take_audio_health(self):
        """(input overruns, worst decode backlog in seconds) since the last
        call -- for the GUI to warn when this PC can't keep up (v0.9.1)."""
        n, b = getattr(self, "input_overruns", 0), getattr(self, "rx_backlog_max_s", 0.0)
        self.input_overruns, self.rx_backlog_max_s = 0, 0.0
        return n, b

    def take_backlog_dropped(self) -> float:
        """Seconds of input audio skipped since the last call because decoding
        had fallen too far behind (v0.9.26, see RX_BACKLOG_DROP_S)."""
        d = getattr(self, "rx_backlog_dropped_s", 0.0)
        self.rx_backlog_dropped_s = 0.0
        return d

    def _rx_callback(self, in_data, frame_count, time_info, status):
        if status and (status & getattr(pyaudio, "paInputOverflow", 2)):
            self.input_overruns = getattr(self, "input_overruns", 0) + 1
        # v0.9.26: the sound card now hands over RX_BUFFER_FRAMES at a time;
        # everything downstream (level, DCD, decoder) still sees RX_BLOCK
        # pieces, exactly as before.
        step = RX_BLOCK * 2                  # int16 bytes
        if len(in_data) <= step:
            self._rx_block(in_data)
        else:
            for i in range(0, len(in_data), step):
                self._rx_block(in_data[i:i + step])
        return (None, pyaudio.paContinue)

    def _rx_block(self, in_data):
        samples = np.frombuffer(in_data, dtype=np.int16).astype(np.float32) / 32768.0
        rec = getattr(self, "_rec", None)
        if rec is not None:                       # v0.9.47: Log > Record received audio
            take = samples[:max(0, self._rec_left)]
            rec.append(take.copy())
            self._rec_left -= len(take)
            if self._rec_left <= 0:
                self._rec_done = np.concatenate(rec)
                self._rec = None
        block_peak = float(np.max(np.abs(samples))) if len(samples) else 0.0
        gain = getattr(self, "_rx_gain", 1.0)
        if gain != 1.0:
            # Digital boost/cut before EVERYTHING downstream (modem, level
            # meter, carrier sense), so all thresholds see a normalized
            # level. It can't restore detail lost to a too-quiet or clipped
            # sound card -- it only rescales what arrived.
            samples = samples * gain
        if getattr(self, "_rx_async", False):
            self._rx_q.put(samples)
        else:                       # no audio thread (offline tests): decode inline
            self._decode_samples(samples)
        rms = float(np.sqrt(np.mean(samples ** 2))) if len(samples) else 0.0
        self._rx_rms_ema = 0.3 * rms + 0.7 * self._rx_rms_ema
        self._track_spectrum(samples)
        # v0.8.0: remember if a SARA-PP signal was present at any point in the
        # current 1-second block, so the noise floor never learns from it --
        # see _track_noise.
        try:
            if self._sara_direct():
                self._last_sara_evidence = time.time()
            self._block_sara = getattr(self, "_block_sara", False) or self.sara_busy()
        except Exception:
            pass
        self._track_noise(rms, len(samples))
        # Clipping only matters while a carrier is present (v0.7.6): with an
        # open-squelch radio, loud idle hiss may clip harmlessly, and "lower
        # the radio volume" advice would then also weaken real signals.
        peak = getattr(self, "raw_peak", 0.0) * 0.999
        try:
            carrier = self.is_channel_busy()
        except Exception:
            carrier = True
        self.raw_peak = max(peak, block_peak) if carrier else peak
        try:
            self.level_queue.put_nowait(rms)
        except queue.Full:
            pass

    @property
    def rx_gain(self) -> float:
        return getattr(self, "_rx_gain", 1.0)

    @rx_gain.setter
    def rx_gain(self, g: float):
        old = getattr(self, "_rx_gain", 1.0)
        self._rx_gain = g
        levels = getattr(self, "_sig_levels", None)
        if levels and old > 0 and g > 0:
            shift = 20 * np.log10(g / old)
            for i in range(len(levels)):
                t, lv = levels[i]
                levels[i] = (t, lv + shift)
        # Keep the noise history consistent with the new gain instead of
        # waiting a minute for it to roll over.
        blocks = getattr(self, "_noise_blocks", None)
        if blocks and old > 0:
            ratio = g / old
            for i in range(len(blocks)):
                blocks[i] *= ratio
            self.noise_floor_rms = min(blocks)
            rx = getattr(self, "receiver", None)
            if rx is not None:
                rx.noise_floor_rms = self.noise_floor_rms

    def _track_noise(self, rms: float, n: int):
        if getattr(self, "_noise_blocks", None) is None:
            return
        self._noise_acc += rms * n
        self._noise_acc_n += n
        self._noise_acc_samples += n
        if self._noise_acc_samples >= NOISE_BLOCK_S * ofdm.SAMPLE_RATE:
            # v0.7.6: the noise floor learns only from seconds that SOUND IDLE
            # (spectrum near its idle baseline). With open squelch a carrier
            # QUIETS the hiss, so the quietest seconds are received
            # transmissions; a floor learned from them put ordinary idle hiss
            # above the busy threshold (simulated: 75% false busy after the
            # first burst heard). Squelch closed: idle seconds are the quiet
            # ones anyway, so nothing changes.
            spec = getattr(self, "_last_spec_block_db", None)
            base = getattr(self, "spec_baseline_db", None)
            idle_like = spec is None or base is None or spec <= base + SPECTRAL_MARGIN_DB / 2
            # v0.8.0: QPSK Wide carriers reach into the band the spectrum check
            # listens to (2.3-2.9 kHz), so a Wide transmission can LOOK idle;
            # with open squelch the floor then learned the (quieted)
            # transmission and idle hiss read busy 100% of the time
            # (simulated, speaker audio). A second with a SARA-PP signal in it
            # -- fill tone or a frame arriving -- is never idle.
            if getattr(self, "_block_sara", False):
                idle_like = False
            self._block_sara = False
            if not idle_like:
                self._noise_acc = 0.0
                self._noise_acc_n = 0
                self._noise_acc_samples = 0
                return
            self._noise_blocks.append(self._noise_acc / max(self._noise_acc_n, 1))
            self._noise_acc = 0.0
            self._noise_acc_n = 0
            self._noise_acc_samples = 0
            self.noise_floor_rms = min(self._noise_blocks)
            rx = getattr(self, "receiver", None)
            if rx is not None:
                rx.noise_floor_rms = self.noise_floor_rms   # see effective_min_energy()

    def weakest_signal_dbfs(self, now: float | None = None) -> float | None:
        levels = getattr(self, "_sig_levels", None)
        if not levels:
            return None
        now = time.time() if now is None else now
        recent = [lv for t, lv in levels if now - t <= SIGNAL_LEVEL_MAX_AGE_S]
        return min(recent) if recent else None

    _SPEC_MASKS: dict = {}

    @classmethod
    def _block_metrics(cls, samples: np.ndarray):
        """(spectral ratio dB or None, fill-tone prominence dB or None) for
        one audio block, from a single Hann-windowed FFT."""
        n = len(samples)
        if n < 256:
            return None, None
        m = cls._SPEC_MASKS.get(n)
        if m is None:
            f = np.fft.rfftfreq(n, 1 / ofdm.SAMPLE_RATE)
            k = int(round(FILL_TONE_HZ * n / ofdm.SAMPLE_RATE))
            nb = np.r_[k - 8:k - 3, k + 4:k + 9]
            m = (np.hanning(n),
                 (f >= SPECTRAL_INBAND_HZ[0]) & (f <= SPECTRAL_INBAND_HZ[1]),
                 (f >= SPECTRAL_ABOVE_HZ[0]) & (f <= SPECTRAL_ABOVE_HZ[1]),
                 slice(k - 1, k + 2), nb)
            cls._SPEC_MASKS[n] = m
        win, inb, above, tone, nb = m
        p = np.abs(np.fft.rfft(np.asarray(samples, dtype=np.float64) * win)) ** 2
        a, b = float(p[inb].mean()), float(p[above].mean())
        ratio = 10 * np.log10(a / b) if a > 0 and b > 0 else None
        t, r = float(p[tone].max()), float(p[nb].mean())
        fill = 10 * np.log10(t / r) if t > 0 and r > 0 else None
        return ratio, fill

    @classmethod
    def spectral_ratio_db(cls, samples: np.ndarray) -> float | None:
        """In-band / above-band power ratio of one audio block, dB (per Hz,
        Hann-windowed). None if the block is too short or silent."""
        return cls._block_metrics(samples)[0]

    def _track_spectrum(self, samples: np.ndarray):
        r, fill = self._block_metrics(samples)
        if fill is not None:
            fe = getattr(self, "_fill_ema", None)
            self._fill_ema = fill if fe is None else 0.3 * fill + 0.7 * fe
        self._track_steady_tone()
        if getattr(self, "_spec_blocks", None) is None:
            return
        if r is None:
            return
        self._spec_ema = r if self._spec_ema is None else 0.3 * r + 0.7 * self._spec_ema
        self._spec_acc += r * len(samples)
        self._spec_acc_n += len(samples)
        self._spec_acc_samples += len(samples)
        if self._spec_acc_samples >= NOISE_BLOCK_S * ofdm.SAMPLE_RATE:
            self._last_spec_block_db = self._spec_acc / self._spec_acc_n
            self._spec_blocks.append(self._last_spec_block_db)
            self._spec_acc = 0.0
            self._spec_acc_n = 0
            self._spec_acc_samples = 0
            self.spec_baseline_db = min(self._spec_blocks)

    def spectral_busy(self) -> bool:
        """Carrier present by spectrum shape (open-squelch quieting) -- see
        SPECTRAL_MARGIN_DB. Off until a baseline exists."""
        ema = getattr(self, "_spec_ema", None)
        base = getattr(self, "spec_baseline_db", None)
        if ema is None or base is None:
            return False
        return (ema - base >= SPECTRAL_MARGIN_DB and ema >= SPECTRAL_MIN_RATIO_DB
                and getattr(self, "_rx_rms_ema", 0.0) >= BUSY_ABS_MIN_RMS)

    def level_busy(self) -> bool:
        return self._rx_rms_ema > self.busy_threshold_rms()

    def _fill_heard(self) -> bool:
        fe = getattr(self, "_fill_ema", None)
        return (fe is not None and fe >= FILL_DETECT_DB
                and getattr(self, "_rx_rms_ema", 0.0) >= BUSY_ABS_MIN_RMS)

    def _track_steady_tone(self, now: float | None = None):
        """Note how long the fill tone has been heard unbroken; past
        FILL_STEADY_S it is a foreign steady tone -- see FILL_STEADY_S."""
        now = time.time() if now is None else now
        if not self._fill_heard():
            self._fill_on_since = None
            return
        lvl = getattr(self, "_rx_rms_ema", 0.0)
        if (getattr(self, "_fill_on_since", None) is None
                or lvl > 3.0 * getattr(self, "_fill_on_level", lvl)):
            # newly heard -- or 10 dB louder than when it began: a station's
            # fill tone over the steady one counts as a new tone
            self._fill_on_since, self._fill_on_level = now, lvl
        elif now - self._fill_on_since >= FILL_STEADY_S:
            prev = getattr(self, "_steady_tone_at", None)
            if prev is None or now - prev >= FILL_FOREIGN_HOLD_S:
                self.steady_tone_count = getattr(self, "steady_tone_count", 0) + 1
            self._steady_tone_at = now

    def steady_tone_ignored(self, now: float | None = None) -> bool:
        """The fill-tone detector hears a tone, but it is the steady foreign
        one (see FILL_STEADY_S), so DCD doesn't count it."""
        now = time.time() if now is None else now
        since = getattr(self, "_fill_on_since", None)
        if since is None or not self._fill_heard():
            return False
        at = getattr(self, "_steady_tone_at", None)
        foreign = at is not None and now - at < FILL_FOREIGN_HOLD_S
        return now - since >= (FILL_FOREIGN_MAX_S if foreign else FILL_STEADY_S)

    def _key_up(self):
        self.turn_t = self.turn_time()    # v0.9.61: before this key-up starts
        self._ptt_keyed = True            # v0.9.33: TX lamp
        self.ptt.ptt_on()

    def turn_time(self) -> float:
        """v0.9.61: when our transmitter last went quiet for at least
        TURN_IDLE_S -- the other station's latest chance to answer what we had
        sent before it (see link_adapt.StaleFrames)."""
        t = getattr(self, "turn_t", 0.0)
        end = getattr(self, "_last_tx_end", 0.0)
        if not getattr(self, "_ptt_keyed", False) and time.time() - end >= TURN_IDLE_S:
            return max(t, end)
        return t

    def _key_down(self):
        try:
            self.ptt.ptt_off()
        finally:
            self._ptt_keyed = False

    def _sara_direct(self) -> bool:
        """Direct proof of a SARA-PP station on the air: its fill tone, or a
        frame still arriving that passed every header check."""
        if self._fill_heard() and not self.steady_tone_ignored():
            return True
        rx = getattr(self, "receiver", None)
        try:
            return bool(rx is not None and rx.receiving_frame())
        except Exception:
            return False

    def sara_busy(self) -> bool:
        """A SARA-PP station is audibly on the air: its fill tone, or a frame
        still arriving that passed every header check. See DCD_MODES. A
        steady foreign tone near the fill frequency doesn't count (v0.9.25).
        v0.9.27: also its carrier, for a short while after the last direct
        proof -- see SARA_CONTINUE_S."""
        if self._sara_direct():
            return True
        ev = getattr(self, "_last_sara_evidence", None)
        if ev is None or ev <= getattr(self, "_last_tx_end", 0.0):
            return False
        lv = getattr(self, "_last_sara_level_db", None)
        try:
            if lv is None or time.time() - ev > SARA_CONTINUE_S or not self.level_busy():
                return False
            now_db = 20 * np.log10(max(getattr(self, "_rx_rms_ema", 0.0), 1e-9))
            return abs(now_db - lv) <= SARA_LEVEL_MATCH_DB      # v0.9.28
        except Exception:
            return False

    def override_dcd(self, seconds: float = 3.0):
        """'Transmit anyway': treat the channel as clear for a few seconds, so
        whatever is waiting on DCD goes out now (VARA's manual take-over)."""
        self._dcd_override_until = time.time() + seconds

    def dcd_state(self) -> tuple[bool, str]:
        """(busy, reason) for the GUI lamp -- reason is "clear", "off",
        "override", or which detector(s) fired."""
        mode = getattr(self, "dcd_mode", "auto")
        if mode == "off":
            return False, "off"
        if time.time() < getattr(self, "_dcd_override_until", 0.0):
            return False, "override"
        why = []
        if self.sara_busy():
            why.append("SARA-PP signal")
        if mode == "auto":
            if self.level_busy():
                why.append("level")
            if self.spectral_busy():
                why.append("spectrum")
        if why:
            return True, " + ".join(why)
        try:
            if self.steady_tone_ignored():
                return False, "tone ignored"
        except Exception:
            pass
        return False, "clear"

    def busy_threshold_rms(self, now: float | None = None) -> float:
        floor = getattr(self, "noise_floor_rms", None)
        if floor is None:
            return CARRIER_BUSY_RMS
        floor_db = 20 * np.log10(max(floor, 1e-9))
        thr_db = floor_db + BUSY_MARGIN_DB
        sig_db = self.weakest_signal_dbfs(now)
        if sig_db is not None:
            # midway between noise and the weakest station, but never closer
            # than SIGNAL_MARGIN_DB to that station -- see SIGNAL_MARGIN_DB
            thr_db = max(thr_db, min((floor_db + sig_db) / 2, sig_db - SIGNAL_MARGIN_DB))
        return max(BUSY_ABS_MIN_RMS, 10 ** (thr_db / 20))

    def _yield_to_reply(self, payload: bytes | None = None):
        """Hold a new transmission until reply_yield_s(payload) after our
        own last one ended, or until the other station's reply shows up on
        DCD (whichever comes first) -- see REPLY_YIELD_S. Not counted as a
        carrier-sense wait in the log: it's deliberate, not traffic."""
        tx_end = getattr(self, "_last_tx_end", 0.0)
        end = tx_end + reply_yield_s(payload)
        while time.time() < end:
            if getattr(self, "_last_rx_frame_time", 0.0) > tx_end + REPLY_HEARD_MARGIN_S:
                return              # v0.8.7: the reply has been heard -- no need to wait longer
            try:
                # v0.9.9: right after our own transmission the channel reads
                # busy from OUR radio (squelch tail, PTT release noise), not
                # the other station. Field (v0.9.8, DW1IRF): that cut the
                # 6.5 s control yield to ~0.5 s twice -- BPQ32's late 19-byte
                # frame went out just as K6IRF began its next window; the
                # second time it hit the message's last frames and the
                # session stalled 34 s. A SARA-PP signal (their fill tone or a
                # frame) still ends the yield at once; anything else only
                # after YIELD_SELF_TAIL_S.
                if self.sara_busy() or (time.time() - tx_end >= YIELD_SELF_TAIL_S
                                        and self.is_channel_busy()):
                    return          # the reply has started: normal DCD wait takes over
            except Exception:
                return
            time.sleep(0.05)

    def _wait_clear(self, payload: bytes | None = None) -> float:
        """wait_for_clear_channel, also noting what the channel read while
        it waited (median level vs threshold vs floor) so a long wait in
        the field log can be told apart: real traffic or noise near the
        threshold. Preceded by the yield-to-reply pause (v0.8.4)."""
        self._yield_to_reply(payload)
        seen = []
        by = {"level": 0, "spectrum": 0}
        start = time.time()
        in_session = start - getattr(self, "_last_rx_frame_time", -1e9) <= SESSION_ACTIVE_S
        last_sara = [-1e9]
        self.last_wait_gave_up = False

        def busy():
            b = self.is_channel_busy()
            if b and in_session and getattr(self, "dcd_mode", "auto") == "auto":
                now = time.time()
                try:
                    if self.sara_busy():
                        last_sara[0] = now
                except Exception:
                    pass
                heard = max(last_sara[0], getattr(self, "_last_rx_frame_time", -1e9))
                if now - start >= NON_SARA_WAIT_MAX_S and now - heard >= NON_SARA_WAIT_MAX_S:
                    self.last_wait_gave_up = True      # v0.9.12: noise, not a station
                    return False
            if b:
                seen.append(getattr(self, "_rx_rms_ema", 0.0))
                try:
                    by["level"] += self.level_busy()
                    by["spectrum"] += self.spectral_busy()
                except Exception:
                    pass
            return b
        waited = wait_for_clear_channel(busy)
        waited += self._hold_clear(busy, start, payload)
        waited += self._yield_to_relay(busy, start)
        if seen:
            try:
                db = lambda x: 20 * np.log10(max(x, 1e-9))
                floor = getattr(self, "noise_floor_rms", None)
                self.last_wait_info = (db(float(np.median(seen))), db(self.busy_threshold_rms()),
                                       db(floor) if floor else None)
            except Exception:
                self.last_wait_info = None
        else:
            self.last_wait_info = None
        self.last_wait_by = (None if not seen else
                             "both" if by["level"] and by["spectrum"] else
                             "spectrum" if by["spectrum"] else "level")
        return waited

    def _hold_clear(self, busy, start: float, payload) -> float:
        """v0.9.66: some transmissions (a digipeater's relay) need the channel
        clear for min_clear_s(payload) without a break first -- not just one
        clear reading. Field (0.9.65, DX1FLR -> DV1UK-10 via DW1IRF, BPSK):
        DX1FLR sent a 7-frame BPSK window (3.4 s a frame, 0.2 s gaps); DW1IRF's
        DCD read clear for a moment between two frames and it keyed up to
        relay the frames so far -- in the middle of DX1FLR's window, missing
        every other frame. A gap inside a burst is 0.2 s; DIGI_MIN_CLEAR_S of
        quiet means the burst is over. Returns time spent waiting on traffic."""
        fn = getattr(self, "min_clear_s", None)
        if fn is None or getattr(self, "dcd_mode", "auto") == "off":
            return 0.0
        try:
            need = float(fn(payload) or 0.0)
        except Exception:
            need = 0.0
        if need <= 0:
            return 0.0
        waited = 0.0
        t_clear = time.time()
        while time.time() - start < CARRIER_SENSE_MAX_WAIT_S:
            if busy():
                waited += wait_for_clear_channel(
                    busy, max_wait_s=max(CARRIER_SENSE_POLL_S, CARRIER_SENSE_MAX_WAIT_S - (time.time() - start)))
                t_clear = time.time()
                continue
            if time.time() - t_clear >= need:
                break
            time.sleep(CARRIER_SENSE_POLL_S)
        return waited

    def _yield_to_relay(self, busy, start: float) -> float:
        """v0.9.61: frames we just heard (or sent) are still to be repeated by
        a digipeater -- leave it the channel first (see link_adapt.RelayWatch).
        Waits up to RELAY_YIELD_S of clear channel for the relay to begin, and
        while it runs, until every expected copy is heard. Returns the extra
        time spent waiting on traffic (the yield itself is deliberate, not
        counted, but kept in last_relay_yield)."""
        self.last_relay_yield = 0.0
        pending = getattr(self, "relay_pending", None)
        if pending is None or getattr(self, "dcd_mode", "auto") == "off":
            return 0.0
        t0 = time.time()
        t_clear = t0
        busy_waited = 0.0
        while time.time() - start < CARRIER_SENSE_MAX_WAIT_S:
            try:
                if not pending():
                    break
            except Exception:
                break
            if busy():
                busy_waited += wait_for_clear_channel(
                    busy, max_wait_s=max(CARRIER_SENSE_POLL_S, CARRIER_SENSE_MAX_WAIT_S - (time.time() - start)))
                t_clear = time.time()
                continue
            if time.time() - t_clear >= RELAY_YIELD_S:
                give_up = getattr(self, "relay_give_up", None)
                if give_up is not None:
                    try:
                        give_up()
                    except Exception:
                        pass
                break
            time.sleep(CARRIER_SENSE_POLL_S)
        self.last_relay_yield = max(0.0, time.time() - t0 - busy_waited)
        return busy_waited

    def _tx_filtered(self, item):
        """v0.9.61: tx_filter(payload) -> payload (maybe fewer frames) or None
        to skip it -- applied as each queued item is taken to go on the air."""
        f = getattr(self, "tx_filter", None)
        if f is None or item is None or item is _NOTHING:
            return item
        try:
            p = f(item[0])
        except Exception:
            return item
        if p is None:
            return _SKIPPED
        return item if p is item[0] else (p,) + tuple(item[1:])

    def is_channel_busy(self) -> bool:
        """True if someone (most likely the other station) is transmitting:
        recent RX audio clearly above the tracked noise floor (see
        BUSY_MARGIN_DB), OR -- for open-squelch radios, where idle hiss is
        louder than a transmission -- the spectrum shows a carrier quieting
        the hiss (see SPECTRAL_MARGIN_DB)."""
        return self.dcd_state()[0]

    def transmit(self, payload: bytes, tx_gain: float | None = None, modulation: int = ofdm.MOD_BPSK,
                 ignore_dcd: bool = False):
        """Blocking: keys PTT, plays lead-in silence + burst + tail silence, unkeys.
        Lead/tail duration comes from self.tx_lead_ms / self.tx_tail_ms, settable
        at any time (this is the TX delay setting — tune it per radio: too short
        clips the front of the burst on relays/PAs slower to key up).
        modulation is MOD_BPSK or MOD_QPSK -- encoded in the frame's own header,
        so the receiving station doesn't need to be told or set to match.

        Waits for a clear channel first (see wait_for_clear_channel) --
        without this, two independently-operating half-duplex stations
        have no way to avoid keying up over each other, which garbles
        both signals and forces an ARQ retry rather than just a brief
        wait here."""
        if tx_gain is None:
            tx_gain = getattr(self, "tx_level", DEFAULT_TX_LEVEL)
        # v0.7.7: pings/pongs ignore DCD, as in VARA FM (ignore_dcd=True)
        waited = 0.0 if ignore_dcd else self._wait_clear(payload)
        if (waited > 0.3 or getattr(self, "last_relay_yield", 0.0) >= 0.5) and self.on_carrier_wait is not None:
            try:
                self.on_carrier_wait(waited)
            except Exception:
                pass
        burst = ofdm.frame_encode(payload, modulation=modulation) * tx_gain
        # Lead-in and tail carry the occupancy fill tone, not silence (see
        # FILL_TONE_HZ): a keyed-but-silent FM transmitter is invisible
        # to the other station's carrier sense.
        lead = fill_tone(int(ofdm.SAMPLE_RATE * self.tx_lead_ms / 1000), tx_gain)
        tail = fill_tone(int(ofdm.SAMPLE_RATE * self.tx_tail_ms / 1000), tx_gain)
        with self._tx_lock:
            # This is the real broadcast moment -- this thread has won
            # its turn at the lock and is about to key PTT. Fire
            # on_tx_start here, not earlier: a caller timestamping "when
            # was this sent" (e.g. SelfEchoFilter) needs this moment, not
            # when transmit() was merely called, which can be much
            # earlier if several transmissions queued up ahead of it.
            # Randomized jitter + re-check, immediately before PTT -- see
            # CSMA_JITTER_MAX_S's own comment for why this exists and
            # what it fixes (a real, observed on-air collision, not a
            # hypothetical one). Deliberately placed here, as close to
            # PTT as the code gets, and re-checks right after the jitter
            # rather than trusting the earlier wait_for_clear_channel()
            # result to still hold -- that is exactly the gap being
            # closed.
            if not ignore_dcd:
                time.sleep(random.uniform(0, CSMA_JITTER_MAX_S))
                if self.is_channel_busy():
                    extra_wait = self._wait_clear(payload)
                    if extra_wait > 0.3 and self.on_carrier_wait is not None:
                        try:
                            self.on_carrier_wait(extra_wait)
                        except Exception:
                            pass
            if self.on_tx_start is not None:
                try:
                    self.on_tx_start(payload)
                except Exception:
                    pass
            self._key_up()
            try:
                self._tx_write(lead.tobytes())
                self._tx_write(burst.astype(np.float32).tobytes())
                self._tx_write(tail.tobytes())
            finally:
                self._key_down()
                self._last_tx_end = time.time()   # yield-to-reply reference (v0.8.4)


    # ── TUNE (v0.7.0) ─────────────────────────────────────────────────
    def transmit_tone(self, seconds: float = 10.0, tx_gain: float | None = None):
        """Blocking: key up and send a steady tone for `seconds`, so the far
        end can set its levels while watching its RX meter. Uses the fill
        tone (never mistaken for a frame -- see FILL_TONE_HZ) at the same
        RMS as real data at this TX level, so the far end's meter reads
        what an actual frame would read. Ignores DCD (v0.7.7)."""
        if tx_gain is None:
            tx_gain = getattr(self, "tx_level", DEFAULT_TX_LEVEL)
        # TUNE ignores DCD (v0.7.7), as VARA's does: it's a deliberate,
        # operator-initiated 10 s test.
        with self._tx_lock:
            self._key_up()
            try:
                self._tx_write(fill_tone(int(ofdm.SAMPLE_RATE * seconds), tx_gain).tobytes())
            finally:
                self._key_down()
                self._last_tx_end = time.time()   # yield-to-reply reference (v0.8.4)

    # ── burst mode (v0.6.1) ───────────────────────────────────────────
    def queue_transmit(self, payload: bytes, modulation: int = ofdm.MOD_BPSK,
                       tx_gain: float | None = None, on_sent=None, on_error=None):
        """Non-blocking: queue a frame for the burst transmitter. Frames
        queued close together (a BPQ32 window) go out under ONE key-up
        with tone-filled gaps -- see BURST_GAP_S. on_sent(payload,
        modulation) fires after each frame is written; on_error(exc) if
        the burst fails."""
        if tx_gain is None:
            tx_gain = getattr(self, "tx_level", DEFAULT_TX_LEVEL)
        self._ensure_tx_worker()
        self._tx_queue.put((payload, modulation, tx_gain, on_sent, on_error))

    def _ensure_tx_worker(self):
        if getattr(self, "_tx_worker", None) is not None and self._tx_worker.is_alive():
            return
        if getattr(self, "_tx_queue", None) is None:
            self._tx_queue = queue.Queue()
        self._tx_worker = threading.Thread(target=self._tx_worker_loop, daemon=True)
        self._tx_worker.start()

    def _requeue_front(self, item):
        """Return an item taken for merging to the FRONT of the TX queue, so
        frame order is preserved when a burst stops at BURST_MAX_FRAMES."""
        with self._tx_queue.mutex:
            self._tx_queue.queue.appendleft(item)
            self._tx_queue.unfinished_tasks += 1
            self._tx_queue.not_empty.notify()

    def _tx_worker_loop(self):
        while True:
            item = self._tx_queue.get()
            if item is None:
                return
            try:
                self._send_burst(item)
            except Exception as e:
                on_error = item[4]
                if on_error is not None:
                    try:
                        on_error(e)
                    except Exception:
                        pass

    def _send_burst(self, first):
        waited = self._wait_clear(first[0])
        if not getattr(self, "running", True):
            # v0.9.25: Stop pressed while this burst waited for DCD -- the
            # streams are closed now (field: "OSError: Stream closed").
            return
        if (waited > 0.3 or getattr(self, "last_relay_yield", 0.0) >= 0.5) and self.on_carrier_wait is not None:
            try:
                self.on_carrier_wait(waited)
            except Exception:
                pass
        with self._tx_lock:
            # Same jitter + re-check as transmit() -- see CSMA_JITTER_MAX_S.
            time.sleep(random.uniform(0, CSMA_JITTER_MAX_S))
            if self.is_channel_busy():
                extra = self._wait_clear(first[0])
                if extra > 0.3 and self.on_carrier_wait is not None:
                    try:
                        self.on_carrier_wait(extra)
                    except Exception:
                        pass
            first = self._tx_filtered(first)
            if first is _SKIPPED:
                return
            tx_gain = first[2]
            self._key_up()
            try:
                self._tx_write(fill_tone(int(ofdm.SAMPLE_RATE * self.tx_lead_ms / 1000),
                                               tx_gain).tobytes())
                item, sent = first, 0
                held = _NOTHING           # taken from the queue but not merged
                merge = getattr(self, "merge_payloads", None)

                def take_next():
                    nonlocal held
                    if held is not _NOTHING:
                        n, held = held, _NOTHING
                        return n
                    while True:
                        n = self._tx_filtered(self._tx_queue.get_nowait())   # raises queue.Empty
                        if n is not _SKIPPED:
                            return n
                while True:
                    callbacks = [item[3]]
                    if merge is not None:
                        # v0.8.5: fold following small queued payloads into this
                        # frame (see bpq_bridge.merge_batches) -- one preamble and
                        # no gaps for a run of acknowledgments.
                        while True:
                            try:
                                nxt = take_next()
                            except queue.Empty:
                                break
                            if nxt is None:
                                held = None
                                break
                            merged = None
                            if nxt[1] == item[1] and nxt[2] == item[2]:
                                try:
                                    # v0.9.4: the mode sets the size cap (16-QAM
                                    # Wide carries two full AX.25 frames)
                                    merged = merge(item[0], nxt[0], item[1])
                                except TypeError:
                                    try:
                                        merged = merge(item[0], nxt[0])
                                    except Exception:
                                        merged = None
                                except Exception:
                                    merged = None
                            if merged is None:
                                held = nxt
                                break
                            item = (merged, item[1], item[2], item[3], item[4])
                            # == not "is": the GUI passes a bound method, and
                            # each reference to it is a NEW object -- with "is"
                            # every merged item kept its own copy, so a merged
                            # frame was logged (and counted in the speed meter)
                            # once per item merged (v0.8.6 fix; field v0.8.5
                            # showed "TX 265 bytes (7 frames)" three times).
                            if all(nxt[3] != c for c in callbacks):
                                callbacks.append(nxt[3])
                    payload, modulation, gain, on_sent, _ = item
                    fin = getattr(self, "finalize_payload", None)
                    if fin is not None:
                        # v0.9.0: e.g. append the link report for the frame's
                        # destination -- after merging, before encoding, so the
                        # echo filter and ACKMODE see exactly what went out.
                        try:
                            payload = fin(payload, modulation)
                        except Exception:
                            pass
                    burst = ofdm.frame_encode(payload, modulation=modulation) * gain
                    if self.on_tx_start is not None:
                        try:
                            self.on_tx_start(payload)
                        except Exception:
                            pass
                    self._tx_write(burst.astype(np.float32).tobytes())
                    sent += 1
                    for cb in callbacks:
                        if cb is not None:
                            try:
                                cb(payload, modulation)
                            except Exception:
                                pass
                    if sent >= BURST_MAX_FRAMES:
                        if held is not _NOTHING:          # put back what we took
                            self._requeue_front(held)
                        break
                    try:
                        nxt = take_next()
                    except queue.Empty:
                        break
                    if nxt is None:            # stop requested mid-burst
                        self._tx_queue.put(None)
                        break
                    self._tx_write(fill_tone(int(ofdm.SAMPLE_RATE * BURST_GAP_S),
                                                   tx_gain).tobytes())
                    item = nxt
                self._tx_write(fill_tone(int(ofdm.SAMPLE_RATE * self.tx_tail_ms / 1000),
                                               tx_gain).tobytes())
            finally:
                self._key_down()
                self._last_tx_end = time.time()   # yield-to-reply reference (v0.8.4)
