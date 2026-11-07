# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 PAREDN (Philippine Amateur Radio Emergency Data Network) / DW1IRF
"""
session_stats.py -- how much of a Winlink/BPQ32 session went to waiting on
AX.25 retry timers (v0.9.30).

Field (v0.9.28, DX1CW / DX1FLR on 145.220): the biggest cost in slow
sessions was not slow modes but silence -- 12-15 s stretches with no
traffic, each ended by BPQ32 or Winlink Express giving up on a lost frame
(FRACK) and polling or resending. The log showed them, but only to someone
lining up timestamps by hand.

A "recovery" is a stretch of at least STALL_S with no SARA-PP traffic either
way, ended by something that only happens after a retry timer runs out:
  - an I-frame sent again (same N(S), same contents) -- by us or by them,
  - an AX.25 supervisory frame (RR/RNR/REJ) with the poll/final bit set,
  - a connect request (SABM) repeated.
A quiet stretch ended by ordinary traffic (someone composing a message, a
session that simply paused) is not counted.
"""

from __future__ import annotations

import threading
import time

import link_adapt

STALL_S = 4.0          # shorter gaps are normal turn-taking
REPEAT_WINDOW_S = 90.0


def _is_poll(control: int) -> bool:
    """S-frame (RR/RNR/REJ/SREJ) with the P/F bit set (modulo-8 AX.25)."""
    return (control & 0x03) == 0x01 and bool(control & 0x10)


class SessionRetryStats:
    def __init__(self):
        self._lock = threading.Lock()
        self.reset()

    def reset(self, now: float | None = None):
        with getattr(self, "_lock", threading.Lock()):
            self.started = time.time() if now is None else now
            self.last_activity = None
            self.recoveries = 0
            self.wait_s = 0.0
            self.resent_by_us = 0
            self.resent_by_them = 0
            self._seen = {}            # (direction, key) -> time

    def _classify(self, direction: str, frames, now: float):
        """(kind or None, peer) for a batch of AX.25 frames."""
        kind, peer = None, None
        for k in [k for k, t in self._seen.items() if now - t > REPEAT_WINDOW_S]:
            del self._seen[k]
        for f in frames:
            p = link_adapt.ax25_parse(f)
            if p is None:
                continue
            dest, src, control, info = p
            other = dest if direction == "tx" else src
            if link_adapt.is_iframe(control):
                key = (direction, dest, src, (control >> 1) & 0x07, bytes(info))
                if key in self._seen:
                    if direction == "tx":
                        self.resent_by_us += 1
                    else:
                        self.resent_by_them += 1
                    kind, peer = kind or "resent", peer or other
                self._seen[key] = now
            elif link_adapt.is_sabm(control):
                key = (direction, "sabm", dest, src)
                if key in self._seen:
                    kind, peer = kind or "reconnect", peer or other
                self._seen[key] = now
            elif _is_poll(control):
                kind, peer = kind or "poll", peer or other
        return kind, peer

    def on_traffic(self, direction: str, frames, now: float | None = None):
        """direction 'tx' (sent to air) or 'rx' (received from air). Returns a
        one-line log note when this traffic ended a recovery, else None."""
        now = time.time() if now is None else now
        with self._lock:
            kind, peer = self._classify(direction, frames, now)
            gap = None if self.last_activity is None else now - self.last_activity
            self.last_activity = now
            if gap is None or gap < STALL_S or kind is None:
                return None
            self.recoveries += 1
            self.wait_s += gap
        who = "we" if direction == "tx" else (peer or "they")
        what = {"resent": "resent a frame", "poll": "polled", "reconnect": "repeated the connect request"}[kind]
        if direction == "tx":
            text = f"this station {what}" + (f" to {peer}" if peer else "")
        else:
            text = f"{who} {what}"
        return f"Retry: {gap:.1f}s with no traffic, then {text} (AX.25 retry timer)"

    def summary(self, now: float | None = None) -> str:
        """Short text for the session summary line, or '' if nothing to say."""
        now = time.time() if now is None else now
        with self._lock:
            if not self.recoveries and not self.resent_by_us and not self.resent_by_them:
                return "   retries: none"
            total = max(1e-9, now - self.started)
            m, s = divmod(int(round(self.wait_s)), 60)
            return (f"   retries: {self.recoveries} (frames resent: {self.resent_by_us} by us, "
                    f"{self.resent_by_them} by them), {m}:{s:02d} waiting on retry timers "
                    f"({100 * self.wait_s / total:.0f}% of the session)")
