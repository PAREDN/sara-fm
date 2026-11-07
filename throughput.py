#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 PAREDN (Philippine Amateur Radio Emergency Data Network) / DW1IRF
"""
throughput.py -- link speed meter for BPQ32/KISS bridge mode.

Pure (no GUI, no audio), so it's testable on its own like kiss.py/fec.py.

WHAT IS COUNTED: AX.25 frame bytes actually delivered to BPQ32 (RX) or
actually put on the air (TX) -- after batch unpacking, and only frames
that passed FEC + CRC. Not raw audio, not FEC parity, not retries that
failed. That makes it an honest "useful link rate," comparable to the
speed VARA FM displays. It includes AX.25 headers (~16 bytes per
236-byte frame), so it reads a few percent above the message-only
bytes/minute figure Winlink Express prints at the end of a transfer.

TWO NUMBERS:
- rate_bps(): live rate over the last `window_s` seconds -- what the
  link is doing right now. While a session is younger than the window,
  it divides by the actual elapsed time so early readings aren't diluted.
- session_bps(): average since reset() (the KISS client connecting) --
  the figure to compare between test runs and versions.
"""

from __future__ import annotations

import collections
import time


class ThroughputMeter:
    def __init__(self, window_s: float = 30.0):
        self.window_s = window_s
        self.reset()

    def reset(self, now: float | None = None):
        self.start = time.time() if now is None else now
        self.total_bytes = 0
        self._events: collections.deque = collections.deque()  # (t, n_bytes)

    def add(self, n_bytes: int, now: float | None = None):
        now = time.time() if now is None else now
        self.total_bytes += n_bytes
        self._events.append((now, n_bytes))
        self._prune(now)

    def _prune(self, now: float):
        while self._events and now - self._events[0][0] > self.window_s:
            self._events.popleft()

    def rate_bps(self, now: float | None = None) -> float:
        now = time.time() if now is None else now
        self._prune(now)
        span = min(self.window_s, max(now - self.start, 1e-6))
        return sum(n for _, n in self._events) * 8 / span

    def elapsed(self, now: float | None = None) -> float:
        now = time.time() if now is None else now
        return max(now - self.start, 0.0)

    def session_bps(self, now: float | None = None) -> float:
        e = self.elapsed(now)
        return self.total_bytes * 8 / e if e > 0 else 0.0


def fmt_bytes(n: int) -> str:
    return f"{n} B" if n < 1024 else f"{n / 1024:.1f} kB"


def fmt_elapsed(sec: float) -> str:
    sec = int(sec)
    return f"{sec // 60}:{sec % 60:02d}"
