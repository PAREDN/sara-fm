#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 PAREDN (Philippine Amateur Radio Emergency Data Network) / DW1IRF
"""
kiss.py — KISS protocol framing, encode and decode.

Pure byte-level framing, nothing else: no network, no AX.25 parsing, no
hardware. This is what BPQ32 speaks over the TCP connection it makes to
us (see bpq_bridge.py for the TCP side, and BPQ32.cfg.example for the
port config BPQ32 itself needs).

KISS frame shape:  FEND [cmd_byte] [data, escaped] FEND
    FEND  = 0xC0  frame delimiter
    FESC  = 0xDB  escape marker
    TFEND = 0xDC  stands in for a literal 0xC0 inside the data
    TFESC = 0xDD  stands in for a literal 0xDB inside the data

cmd_byte's high nibble is the KISS port number (always 0 here — BPQ32's
config in this project uses a single port), low nibble is the command:
0x00 = data frame (the only one this module produces or acts on).
"""

from __future__ import annotations

FEND = 0xC0
FESC = 0xDB
TFEND = 0xDC
TFESC = 0xDD

CMD_DATA = 0x00
# v0.9.0: KISS "ACKMODE" (BPQ32 KISSOPTIONS=ACKMODE). The host sends
#   [cmd 0x0C][2-byte id][AX.25 frame]
# and the TNC answers [cmd 0x0C][the same 2 bytes] once that frame has
# actually been transmitted -- so BPQ32 starts its FRACK timer then, not
# when it handed the frame over (which, behind a queue and a DCD wait,
# could be many seconds earlier).
CMD_ACKMODE = 0x0C


def encode_frame(data: bytes, port: int = 0, command: int = CMD_DATA) -> bytes:
    """Wrap data as one complete KISS frame, escaping FEND/FESC inside it."""
    cmd_byte = ((port & 0x0F) << 4) | (command & 0x0F)
    body = bytearray([cmd_byte])
    for b in data:
        if b == FEND:
            body += bytes([FESC, TFEND])
        elif b == FESC:
            body += bytes([FESC, TFESC])
        else:
            body.append(b)
    return bytes([FEND]) + bytes(body) + bytes([FEND])


class KissDecoder:
    """
    Streaming KISS decoder. Feed raw bytes as they arrive (e.g. from a
    TCP socket, in whatever chunk sizes the network hands you) via
    feed(); complete decoded frames come back as a list of
    (port, command, payload) tuples. Partial frames are held across
    calls — a frame split across two socket reads decodes correctly
    once the rest arrives.
    """

    def __init__(self):
        self._buf = bytearray()
        self._in_frame = False
        self._escaped = False
        self._current = bytearray()

    def feed(self, data: bytes) -> list[tuple[int, int, bytes]]:
        out = []
        for b in data:
            if b == FEND:
                if self._in_frame and self._current:
                    frame = self._finish_frame()
                    if frame is not None:
                        out.append(frame)
                self._in_frame = True
                self._escaped = False
                self._current = bytearray()
                continue
            if not self._in_frame:
                continue   # junk between frames, ignore
            if self._escaped:
                if b == TFEND:
                    self._current.append(FEND)
                elif b == TFESC:
                    self._current.append(FESC)
                else:
                    # invalid escape sequence -- drop the frame being built
                    self._in_frame = False
                    self._current = bytearray()
                self._escaped = False
                continue
            if b == FESC:
                self._escaped = True
                continue
            self._current.append(b)
        return out

    def _finish_frame(self):
        if not self._current:
            return None
        cmd_byte = self._current[0]
        port = (cmd_byte >> 4) & 0x0F
        command = cmd_byte & 0x0F
        payload = bytes(self._current[1:])
        return (port, command, payload)
