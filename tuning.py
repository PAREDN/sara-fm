#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 PAREDN (Philippine Amateur Radio Emergency Data Network) / DW1IRF
"""
tuning.py -- level/S-N guidance for Auto-Tune, the two-way ping report,
and the RX meter. Pure (no GUI, no audio), testable on its own.

TWO DIFFERENT NUMBERS, deliberately kept apart (v0.7.0):
- S/N (dB) is the LINK: measured from the repeated preamble, unaffected by
  any volume or gain setting. It says whether the radio path is good. No
  knob in SARA-PP improves it; antenna, distance, and interference do.
- Level (dBFS) is the VOLUME the sound card delivered. It only needs to be
  in a sensible window: not clipping, not buried in the noise floor. RX
  gain moves it; it does not change S/N.
Before v0.7.0 SARA-PP showed only "quality", which was really a level
reading, and it pushed users to turn levels up (into VARA FM's red zone on
a shared sound card) chasing a better-looking number that wasn't better.
"""

from __future__ import annotations

import math

# Where received frames should land AFTER RX gain: comfortably above noise
# and the receiver's own thresholds, with headroom below full scale.
TARGET_LEVEL_DBFS = -12.0
TARGET_WINDOW_DB = 4.0
RX_GAIN_MIN_DB = -20.0
RX_GAIN_MAX_DB = 40.0
CLIP_RAW_PEAK = 0.97          # raw sample peak (fraction of full scale)
RAW_HOT_DBFS = -3.0           # far end's RAW level above this: near clipping
RAW_QUIET_DBFS = -45.0        # far end's RAW level below this: very quiet
CS_MARGIN_MIN_DB = 12.0       # signal-over-noise needed for reliable carrier sense


def snr_verdict(snr_db: float | None) -> str:
    if snr_db is None:
        return "unknown"
    if snr_db >= 20:
        return "excellent"
    if snr_db >= 12:
        return "good"
    if snr_db >= 8:
        return "marginal (expect FEC repairs and some retries)"
    return "poor (link unreliable)"


def in_target(level_dbfs: float | None) -> bool:
    return level_dbfs is not None and abs(level_dbfs - TARGET_LEVEL_DBFS) <= TARGET_WINDOW_DB


def level_verdict(level_dbfs: float | None) -> str:
    if level_dbfs is None:
        return "unknown"
    if in_target(level_dbfs):
        return "in target"
    return "too low" if level_dbfs < TARGET_LEVEL_DBFS else "too high"


def recommend_rx_gain(level_after_gain_dbfs: float, current_gain_db: float) -> tuple[float, str]:
    """RX gain (dB) that would put frames at TARGET_LEVEL_DBFS, clamped to
    the control's range, plus a note when the range isn't enough -- which
    means the RADIO volume (or Windows recording level) has to change."""
    raw = level_after_gain_dbfs - current_gain_db
    ideal = TARGET_LEVEL_DBFS - raw
    new = max(RX_GAIN_MIN_DB, min(RX_GAIN_MAX_DB, round(ideal)))
    note = ""
    # Only advise touching the radio when the clamp actually leaves frames
    # OUTSIDE the target window -- hitting the limit while still landing in
    # the window needs no action.
    if ideal > RX_GAIN_MAX_DB + TARGET_WINDOW_DB:
        note = ("RX gain is at its maximum and frames are still quiet: raise the "
                "radio's volume (or the Windows recording level).")
    elif ideal < RX_GAIN_MIN_DB - TARGET_WINDOW_DB:
        note = ("RX gain is at its minimum and frames are still hot: lower the "
                "radio's volume (or the Windows recording level).")
    return float(new), note


def report_advice(their_snr_db: float | None, their_raw_level_dbfs: float | None,
                  peer: str = "them", carrier_db: float | None = None) -> list[str]:
    """Plain-language advice from the far end's two-way ping report: how
    THEY received YOUR ping. v0.9.79: clean data carriers at their end
    (carrier_db) mean a quiet input is fine -- no "raise your TX level"."""
    out = []
    clean = carrier_db is not None and carrier_db >= CARRIER_LIGHT_GREEN_DB
    if their_raw_level_dbfs is not None:
        if their_raw_level_dbfs > RAW_HOT_DBFS:
            out.append(f"Your signal is near clipping {peer}'s sound card: lower your "
                       f"TX level (or {peer} lowers their radio volume).")
        elif their_raw_level_dbfs < RAW_QUIET_DBFS and not clean:
            out.append(f"Your signal arrives very quietly at {peer}'s sound card: raise "
                       f"your TX level, or {peer} raises their radio volume.")
    if their_snr_db is not None and their_snr_db < 12:
        if their_raw_level_dbfs is not None and their_raw_level_dbfs < -30:
            # v0.9.41: on FM the audio level sets the deviation, and deviation
            # sets the S/N after the receiver -- quiet AND noisy = too little
            out.append(f"{peer} hears you quietly AND with {snr_verdict(their_snr_db)} S/N: raise "
                       f"your TX level (or Tune TX) -- on FM, more audio means more deviation and "
                       f"a better S/N at their end. If that doesn't help, it's the radio path.")
        else:
            out.append(f"{peer} hears you with {snr_verdict(their_snr_db)} S/N -- that's the "
                       f"radio path (antenna, distance, interference), not a level setting.")
    return out


def carrier_sense_margin_db(level_dbfs: float | None, noise_floor_rms: float | None) -> float | None:
    if level_dbfs is None or not noise_floor_rms or noise_floor_rms <= 0:
        return None
    return level_dbfs - 20 * math.log10(noise_floor_rms)


def carrier_sense_advice(margin_db: float | None) -> str:
    if margin_db is None or margin_db >= CS_MARGIN_MIN_DB:
        return ""
    if margin_db <= 0:
        head = (f"Background noise between transmissions is as loud as received signals "
                f"({-margin_db:.0f} dB louder)")
    else:
        head = f"Background noise is only {margin_db:.0f} dB below received signals"
    return (head + ": DCD 'Auto' can't tell a station from noise (squelch open, or a "
            "noisy audio path). Close the squelch, or set DCD Mode to 'SARA-PP signals "
            "only'. Raising gain won't help -- it lifts both.")


def dbfs(rms: float) -> float:
    return 20 * math.log10(rms) if rms and rms > 0 else -120.0


# ── colours for the graphical ping window (v0.7.3) ───────────────────────
# Same thresholds as the text verdicts above, so the boxes and the log
# always agree. Returned as (fill, text) colour pairs.
GREEN = ("#1f9d3a", "white")
LIGHT_GREEN = ("#7cc242", "black")
YELLOW = ("#f2c94c", "black")
RED = ("#d64545", "white")
GREY = ("#9aa0a6", "black")


def snr_colors(snr_db: float | None):
    if snr_db is None:
        return GREY
    if snr_db >= 20:
        return GREEN
    if snr_db >= 12:
        return LIGHT_GREEN
    if snr_db >= 8:
        return YELLOW
    return RED


# v0.9.52: the data-carrier S/N (what Tune TX measures and the speed
# depends on). Overall S/N is taken from the repeated preamble, and a CLIPPED
# signal still repeats perfectly -- so it keeps rising as the TX level goes
# up, even past the point where the radio distorts the data. The carriers
# show the distortion. Thresholds from the speed table: ~11 dB carries
# 16QAM Wide C, 16+ the fastest modes; below ~7 only QPSK/BPSK work well.
CARRIER_GREEN_DB = 16
CARRIER_LIGHT_GREEN_DB = 11
CARRIER_YELLOW_DB = 7


def carrier_colors(car_db: float | None):
    if car_db is None:
        return GREY
    if car_db >= CARRIER_GREEN_DB:
        return GREEN
    if car_db >= CARRIER_LIGHT_GREEN_DB:
        return LIGHT_GREEN
    if car_db >= CARRIER_YELLOW_DB:
        return YELLOW
    return RED


def carrier_verdict(car_db: float | None) -> str:
    if car_db is None:
        return "unknown"
    if car_db >= CARRIER_GREEN_DB:
        return "clean -- fastest modes"
    if car_db >= CARRIER_LIGHT_GREEN_DB:
        return "good -- 16QAM"
    if car_db >= CARRIER_YELLOW_DB:
        return "fair -- QPSK"
    return "distorted or noisy -- slow modes only"


def level_colors(level_dbfs: float | None):
    """Your received level AFTER RX gain: green in the target zone, red if
    hot enough to risk clipping, yellow otherwise (Auto-Tune fixes it)."""
    if level_dbfs is None:
        return GREY
    if level_dbfs > RAW_HOT_DBFS:
        return RED
    return GREEN if in_target(level_dbfs) else YELLOW


def raw_input_colors(raw_dbfs: float | None):
    """The far end's RAW input level for your signal (from its report):
    red near clipping, yellow very quiet, green otherwise."""
    if raw_dbfs is None:
        return GREY
    if raw_dbfs > RAW_HOT_DBFS:
        return RED
    if raw_dbfs < RAW_QUIET_DBFS:
        return YELLOW
    return GREEN


# ── one verdict at the end of Tune (v0.9.78) ─────────────────────────────
# Field (volunteers): Ping/Auto-Tune/Tune TX gave three panels of numbers
# and several "-> raise this, or they lower that" lines, sometimes pulling
# opposite ways ("data carriers 16 dB -- clean" next to "arrives very
# quietly: raise your TX level"). VARA FM's drive calibration ends with one
# answer. Tune now does too: what happened, and the ONE thing to do next.

TUNE_TX_HIGH_PCT = 80          # at or above: "turn the interface up", not "raise TX level"
TUNE_TX_LOW_PCT = 8            # at or below: "turn the interface down"


def tune_verdict(target: str, *, reply: bool, their_snr=None, their_raw=None, data_db=None,
                 data_flat: bool = False, tx_pct=None, rx_gain_db=None, my_clip: bool = False,
                 their_replies_clipped: bool = False, rx_note: str = "",
                 gateway_self: bool = False) -> tuple:
    """(state, headline, action) -- state is 'done' (green), 'warn' (yellow)
    or 'failed' (red). `data_db` is the data-carrier S/N at the far end at the
    TX level kept (None when it couldn't be measured)."""
    t = target or "the other station"
    if not reply:
        return ("failed", f"NO REPLY FROM {t}",
                f"Same frequency? Is SARA FM started at {t}? Send a Test tone (Settings > Devices) and ask {t} "
                f"to watch its RX meter -- no movement means your signal isn't reaching them.")
    if my_clip:
        return ("warn", "TURN DOWN YOUR RADIO'S VOLUME",
                f"{t}'s signal clips your sound card input. Lower the radio's audio (or the "
                f"Windows recording level), then press Auto-Tune again.")
    if their_replies_clipped:
        return ("warn", f"YOUR LEVELS ARE FINE -- {t} TRANSMITS TOO LOUD",
                f"{t}'s replies arrive overdriven. {t} should press Auto-Tune to you "
                f"(if {t} is a gateway, tell its sysop).")
    if their_raw is not None and their_raw > RAW_HOT_DBFS and tx_pct is not None and tx_pct <= TUNE_TX_LOW_PCT:
        return ("warn", "TURN DOWN YOUR TX AUDIO",
                f"Your signal clips {t}'s sound card even at TX {tx_pct}%. Turn down the audio "
                f"into your radio (the interface's TX trimpot), then press Auto-Tune again.")
    if data_db is not None and data_db < CARRIER_LIGHT_GREEN_DB:
        if their_raw is not None and their_raw < RAW_QUIET_DBFS and tx_pct is not None \
                and tx_pct >= TUNE_TX_HIGH_PCT:
            return ("warn", "TURN UP YOUR TX AUDIO",
                    f"Too quiet at {t} even at TX {tx_pct}%. Turn up the audio into your radio "
                    f"(the interface's TX trimpot, or the radio's data input level), then press "
                    f"Tune again.")
        if their_raw is not None and their_raw < RAW_QUIET_DBFS and tx_pct is not None:
            up = min(100, max(tx_pct + 10, tx_pct * 2))
            return ("warn", "RAISE YOUR TX LEVEL",
                    f"Too quiet at {t} (data carriers {data_db:.0f} dB). Set TX level to {up}% "
                    f"and press Auto-Tune again.")
        if data_flat:
            return ("warn", "WEAK LINK -- NOT YOUR LEVELS",
                    f"Data carriers only {data_db:.0f} dB at {t} at every TX level, so a level "
                    f"setting won't fix it: antenna, distance or the radio's audio path. It works, "
                    f"at the slower speeds.")
        return ("warn", "WORKS, AT SLOWER SPEEDS",
                f"Data carriers {data_db:.0f} dB at {t}: good enough for the slower modes. "
                f"Saved -- press Auto-Tune again another time to recheck.")
    if their_snr is not None and data_db is None and their_snr < 12:
        return ("warn", "WEAK LINK",
                f"{t} hears you at S/N {their_snr:+.0f} dB -- that's the radio path, not a level "
                f"setting. It works, at the slower speeds.")
    if rx_note:
        return ("warn", "ADJUST YOUR RADIO'S VOLUME", rx_note)
    bits = []
    if tx_pct is not None:
        bits.append(f"TX {tx_pct}%")
    if rx_gain_db is not None:
        bits.append(f"RX gain {rx_gain_db:+.0f} dB")
    heard = []
    if their_snr is not None:
        heard.append(f"{t} hears you at S/N {their_snr:+.0f} dB")
    if data_db is not None:
        heard.append(f"data {data_db:.0f} dB")
    action = (f"Saved for {t}: " + ", ".join(bits) if bits else "Levels saved") \
        + (". " + ", ".join(heard) + "." if heard else ".")
    if gateway_self:
        action += " As a gateway, these are now the levels for every caller."
    return ("done", "APPROVED", action)
