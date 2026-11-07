# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 PAREDN (Philippine Amateur Radio Emergency Data Network) / DW1IRF
"""
b2check.py -- v0.9.71: check the Winlink B2 messages passing through the
bridge, so a CMS "Check sum failure" can be pinned on the right link.

SARA FM sits under AX.25 and never changes the data, but when the CMS says
"failed to assemble correct binary image" nobody can tell from the radio
log whether the bytes BPQ32 was given were already wrong. This follows each
AX.25 session the way a receiving AX.25 station does (an I-frame counts
only if its N(S) is the next one expected -- repeats and out-of-order copies
are dropped, as BPQ32 drops them), reads the B2 forwarding protocol out of
the data, and checks each binary message as it completes:

    SOH len title NUL offset NUL  |  STX len data ... (blocks)  |  EOT checksum

The checksum is the two's complement of the sum of all data bytes; the data
length must match the compressed size the sender proposed ("FC EM id usize
csize offset"), and the compressed image starts with the CRC (2 bytes) and
the uncompressed size (4 bytes, little-endian).

Read-only: it never changes, holds or adds a frame.
"""

from __future__ import annotations

import struct
import threading

import link_adapt

SOH, STX, EOT = 0x01, 0x02, 0x04
MAX_TEXT = 4096


class _Stream:
    """One direction of one AX.25 session."""

    def __init__(self):
        self.vr = 0
        self.reset_b2()

    def reset_b2(self):
        self.mode = "text"
        self.line = bytearray()
        self.proposals = []          # [(mid, usize, csize, offset)] from this sender
        self.hdr = bytearray()
        self.need = 0
        self.cur = None
        self.data = bytearray()
        self.blocks = 0
        self.stage = None


class B2Watch:
    def __init__(self):
        self._lock = threading.Lock()
        self._streams: dict = {}

    def feed(self, frame: bytes) -> list:
        """One AX.25 frame as BPQ32 / Winlink Express sent or was given it.
        Returns notes (strings) for messages that completed or broke."""
        p = link_adapt.ax25_parse(frame)
        if p is None:
            return []
        dest, src, control, info = p
        key = (link_adapt.norm_call(src), link_adapt.norm_call(dest))
        with self._lock:
            if (control & 0xEF) in (0x2F, 0x6F):            # SABM(E): new session both ways
                self._streams[key] = _Stream()
                self._streams[(key[1], key[0])] = _Stream()
                return []
            if control & 0x01:
                return []
            st = self._streams.get(key)
            if st is None:
                return []                                   # joined mid-session: can't follow it
            ns = (control >> 1) & 7
            if ns != st.vr:
                return []
            st.vr = (st.vr + 1) & 7
            return self._data(st, key, info)

    # ── B2 parsing ──────────────────────────────────────────────────────
    def _data(self, st: _Stream, key, data: bytes) -> list:
        notes = []
        i = 0
        while i < len(data):
            b = data[i]
            if st.mode == "text":
                if b == SOH and not st.line and st.proposals:
                    st.mode, st.stage, st.hdr, st.need = "bin", "soh_len", bytearray(), 1
                    i += 1
                    continue
                if b == 0x0D:
                    self._text_line(st, bytes(st.line), self._streams.get((key[1], key[0])))
                    st.line = bytearray()
                elif b != 0x0A:
                    st.line.append(b)
                    if len(st.line) > MAX_TEXT:
                        st.line = bytearray()
                i += 1
                continue
            # binary
            if st.stage == "soh_len":
                st.need = b
                st.hdr = bytearray()
                st.stage = "hdr"
                i += 1
                if st.need == 0:
                    st.stage = "blk"
                continue
            if st.stage == "hdr":
                take = min(st.need - len(st.hdr), len(data) - i)
                st.hdr += data[i:i + take]
                i += take
                if len(st.hdr) >= st.need:
                    st.cur = st.proposals.pop(0) if st.proposals else None
                    st.data, st.blocks, st.stage = bytearray(), 0, "blk"
                continue
            if st.stage == "blk":
                if b == STX:
                    st.stage = "stx_len"
                elif b == EOT:
                    st.stage = "eot"
                else:
                    notes.append(self._broken(st, key, f"expected a data block (STX) or the end "
                                                       f"(EOT), got byte 0x{b:02x}"))
                i += 1
                continue
            if st.stage == "stx_len":
                st.need = b or 256
                st.stage = "stx_data"
                st.blocks += 1
                st.blk_got = 0
                i += 1
                continue
            if st.stage == "stx_data":
                take = min(st.need - st.blk_got, len(data) - i)
                st.data += data[i:i + take]
                st.blk_got += take
                i += take
                if st.blk_got >= st.need:
                    st.stage = "blk"
                continue
            if st.stage == "eot":
                notes.append(self._done(st, key, b))
                i += 1
                continue
        return notes

    @staticmethod
    def _fs(other: _Stream, answers: str):
        """The other side's 'FS' answer to its proposals: keep the accepted
        ones (Y/+, or A/! with a resume offset), drop the rest."""
        keep, props, i = [], other.proposals, 0
        k = 0
        while i < len(answers) and k < len(props):
            c = answers[i]
            i += 1
            mid, usize, csize, off = props[k]
            k += 1
            if c in "Y+":
                keep.append((mid, usize, csize, off))
            elif c in "A!":
                j = i
                while j < len(answers) and answers[j].isdigit():
                    j += 1
                keep.append((mid, usize, csize, int(answers[i:j] or 0)))
                i = j
        other.proposals = keep

    @staticmethod
    def _text_line(st: _Stream, line: bytes, other=None):
        try:
            s = line.decode("ascii", "replace").strip()
        except Exception:
            return
        parts = s.split()
        if len(parts) >= 5 and parts[0] == "FC":
            try:
                usize, csize = int(parts[3]), int(parts[4])
                off = int(parts[5]) if len(parts) > 5 else 0
            except ValueError:
                return
            st.proposals.append((parts[2], usize, csize, off))
        elif parts[:1] == ["FS"] and len(parts) > 1 and other is not None:
            B2Watch._fs(other, parts[1])
        elif parts[:1] in (["FF"], ["FQ"]):
            st.proposals = []

    def _label(self, st: _Stream, key) -> str:
        title = bytes(st.hdr).split(b"\0")[0].decode("latin-1", "replace")[:40]
        mid = st.cur[0] if st.cur else "?"
        return f"B2 message {mid} ({title!r}) {key[0]} -> {key[1]}"

    def _done(self, st: _Stream, key, chk: int) -> str:
        total = (sum(st.data) + chk) & 0xFF
        n = len(st.data)
        problems = []
        if total != 0:
            problems.append(f"checksum WRONG (sum 0x{total:02x}, should be 0)")
        if st.cur is not None:
            mid, usize, csize, off = st.cur
            want = csize - off
            if n != want:
                problems.append(f"{n} bytes, but {want} were proposed ({csize} compressed, offset {off})")
            if off == 0 and n >= 6:
                if struct.unpack("<I", bytes(st.data[2:6]))[0] != usize:
                    problems.append(f"size field {struct.unpack('<I', bytes(st.data[2:6]))[0]} "
                                    f"!= {usize} proposed")
        label = self._label(st, key)
        st.stage, st.hdr, st.cur, st.mode = None, bytearray(), None, "text"
        if problems:
            return f"{label}: {n} bytes in {st.blocks} blocks -- " + "; ".join(problems)
        return f"{label}: {n} bytes in {st.blocks} blocks, checksum OK -- intact"

    def _broken(self, st: _Stream, key, why: str) -> str:
        label = self._label(st, key)
        n = len(st.data)
        st.reset_b2()
        return f"{label}: data stream broken after {n} bytes -- {why}"
