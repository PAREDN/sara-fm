#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 PAREDN (Philippine Amateur Radio Emergency Data Network) / DW1IRF
"""
protocol.py — the PING/PONG payload carried inside one ofdm_core frame.

Deliberately tiny: this is a link test, not a general data protocol.
Fixed-width fields, no variable-length parsing, nothing to get subtly
wrong on one end and not the other.

Layout (20 bytes total, before ofdm_core appends its own CRC-16):
    byte 0      : msg type   (1 = PING, 2 = PONG)
    byte 1      : sequence number (0-255, wraps)
    bytes 2-10  : from-callsign, 9 bytes, ASCII, space-padded
    bytes 11-19 : to-callsign,   9 bytes, ASCII, space-padded ('*' = broadcast)

v0.7.0: a PONG may carry 2 more bytes (22 total) -- the responder's
measurement of the PING it is answering, like VARA's two-way ping report:
    byte 20     : S/N of the received ping, dB (signed int8)
    byte 21     : raw input level of the received ping, dBFS, BEFORE the
                  responder's RX gain (signed int8)
So one ping tells you how the far end hears you AND how you hear them.
A 20-byte PONG (older v0.6.x responder) still parses, just without them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

MSG_PING = 1
MSG_PONG = 2

CALLSIGN_FIELD_LEN = 9
PING_FLAG_CARRIER = 0x01   # v0.9.14: "report the data-carrier S/N in your PONG"
# v0.9.50: "your last reply reached me strong but clipped -- answer quieter".
# Sent only after such a reply, so an older station (which doesn't know the
# flag and won't answer this one ping) was already unreadable anyway.
PING_FLAG_QUIETER = 0x02
_PING_FLAGS = PING_FLAG_CARRIER | PING_FLAG_QUIETER
BROADCAST = "*"


def _pack_callsign(call: str) -> bytes:
    call = call.strip().upper()[:CALLSIGN_FIELD_LEN]
    return call.encode("ascii").ljust(CALLSIGN_FIELD_LEN, b" ")


def _int8(v: float) -> bytes:
    return int(max(-128, min(127, round(v)))).to_bytes(1, "big", signed=True)


def _unpack_callsign(raw: bytes) -> str:
    return raw.decode("ascii", errors="replace").strip()


@dataclass
class PingPongMessage:
    msg_type: int
    seq: int
    from_call: str
    to_call: str
    report_snr_db: int | None = None
    report_level_dbfs: int | None = None
    # v0.9.14 (Tune TX): a PING may ask for the data-carrier S/N; the PONG
    # then carries it as a third report byte. Older versions never send
    # either form and don't recognise them.
    want_carrier: bool = False
    report_carrier_snr_db: int | None = None
    ask_quieter: bool = False          # v0.9.50

    def pack(self) -> bytes:
        out = (
            bytes([self.msg_type & 0xFF, self.seq & 0xFF])
            + _pack_callsign(self.from_call)
            + _pack_callsign(self.to_call)
        )
        if self.msg_type == MSG_PING and (self.want_carrier or self.ask_quieter):
            out += bytes([(PING_FLAG_CARRIER if self.want_carrier else 0)
                          | (PING_FLAG_QUIETER if self.ask_quieter else 0)])
        elif self.report_snr_db is not None and self.report_level_dbfs is not None:
            out += _int8(self.report_snr_db) + _int8(self.report_level_dbfs)
            if self.report_carrier_snr_db is not None:
                out += _int8(self.report_carrier_snr_db)
        return out

    @staticmethod
    def unpack(data: bytes) -> "PingPongMessage":
        base = 2 + 2 * CALLSIGN_FIELD_LEN
        if len(data) not in (base, base + 1, base + 2, base + 3):
            raise ValueError(f"bad payload length: {len(data)}")
        msg_type = data[0]
        seq = data[1]
        from_call = _unpack_callsign(data[2:2 + CALLSIGN_FIELD_LEN])
        to_call = _unpack_callsign(data[2 + CALLSIGN_FIELD_LEN:base])
        snr = lvl = car = None
        want = quiet = False
        if len(data) == base + 1:
            want = bool(data[base] & PING_FLAG_CARRIER)
            quiet = bool(data[base] & PING_FLAG_QUIETER)
        if len(data) >= base + 2:
            snr = int.from_bytes(data[base:base + 1], "big", signed=True)
            lvl = int.from_bytes(data[base + 1:base + 2], "big", signed=True)
        if len(data) == base + 3 and data[base + 2] != 0x80:
            car = int.from_bytes(data[base + 2:base + 3], "big", signed=True)
        return PingPongMessage(msg_type, seq, from_call, to_call, snr, lvl, want, car, quiet)

    def is_for(self, my_call: str) -> bool:
        return self.to_call == BROADCAST or self.to_call.upper() == my_call.strip().upper()


def make_ping(from_call: str, to_call: str, seq: int, want_carrier: bool = False,
              ask_quieter: bool = False) -> bytes:
    return PingPongMessage(MSG_PING, seq, from_call, to_call, want_carrier=want_carrier,
                           ask_quieter=ask_quieter).pack()


def make_pong(from_call: str, to_call: str, seq: int,
              snr_db: float | None = None, level_dbfs: float | None = None,
              carrier_snr_db: float | None = None, want_carrier: bool = False) -> bytes:
    """With snr_db/level_dbfs (the received PING's measurements), the PONG
    carries the two-way report; without them it's the plain 20-byte form.
    v0.9.14: when the PING asked (want_carrier), a third byte carries the
    data-carrier S/N (0x80 = couldn't measure)."""
    if snr_db is None or level_dbfs is None:
        return PingPongMessage(MSG_PONG, seq, from_call, to_call).pack()
    msg = PingPongMessage(MSG_PONG, seq, from_call, to_call,
                          int(round(snr_db)), int(round(level_dbfs)))
    if not want_carrier:
        return msg.pack()
    out = msg.pack()
    return out + (_int8(carrier_snr_db) if carrier_snr_db is not None else bytes([0x80]))


# v0.7.3: bridge mode answers pings like VARA FM does while connected to
# BPQ32, so it must recognise a ping/pong among AX.25 traffic WITHOUT ever
# mistaking a data frame for one (a misread data frame would silently drop
# out of a Winlink session). The bridge's batch format also starts with
# 0x01 (== MSG_PING), so the first byte alone can't tell them apart. They
# diverge right after: in a batch, byte 2 is the high byte of the first
# frame's length -- 0x00 or 0x01 for any real frame -- while in a ping it
# is the first character of a callsign. Requiring both callsign fields to
# be genuine, space-padded callsigns makes the check unambiguous.
_CALL_RE = re.compile(rb"^[A-Z0-9][A-Z0-9/\-]{0,8} *$")
_TO_RE = re.compile(rb"^(\* {8}|[A-Z0-9][A-Z0-9/\-]{0,8} *)$")


def is_pingpong(payload: bytes) -> bool:
    base = 2 + 2 * CALLSIGN_FIELD_LEN
    if len(payload) not in (base, base + 1, base + 2, base + 3):
        return False
    if payload[0] not in (MSG_PING, MSG_PONG):
        return False
    if len(payload) == base + 1 and (payload[0] != MSG_PING or payload[base] == 0
                                     or payload[base] & ~_PING_FLAGS):
        return False
    if len(payload) >= base + 2 and payload[0] != MSG_PONG:
        return False
    frm = payload[2:2 + CALLSIGN_FIELD_LEN]
    to = payload[2 + CALLSIGN_FIELD_LEN:base]
    return bool(_CALL_RE.match(frm)) and bool(_TO_RE.match(to))
