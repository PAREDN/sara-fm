# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 PAREDN (Philippine Amateur Radio Emergency Data Network) / DW1IRF
"""
link_adapt.py -- Phase 2: automatic speed selection (v0.9.0).

SARA-PP sits UNDER AX.25: BPQ32 / Winlink Express own the acknowledgments
and retries, so -- unlike VARA, which runs its own ARQ -- a sending station
does not automatically learn how well the other station hears it. And the
field showed it can't guess from how well IT hears THEM (K6IRF heard DW1IRF
cleanly while DW1IRF struggled with K6IRF's Wide frames). So:

1. LINK REPORTS. Every bridge transmission carries, after its AX.25 frames,
   a 9-byte report for the station it is addressed to: "your frames reach
   me in mode M at preamble S/N S, using up to F% of FEC capacity, N ok,
   B lost". Batches carry explicit frame lengths, and every receiver since
   v0.5 unpacks exactly the counted frames -- so the trailer is invisible
   to older versions (and never reaches BPQ32).
2. SPEED CONTROLLER per peer callsign (a gateway serves several stations):
   ladder BPSK -> QPSK -> QPSK Wide. Down one step at once on any lost frame,
   heavy FEC use or low S/N; up one step only after consecutive clean
   reports with margin (hysteresis: the up and down S/N thresholds differ by
   4-6 dB). A step down holds off further downs for a moment (the rest of
   the same window was sent in the old mode) and further ups for longer.
3. RETRANSMISSION DETECTION, for peers without reports (older versions):
   BPQ32 sending the same AX.25 I-frame again means the other side missed
   it -> step down. Ignored while recent clean reports say our frames
   arrive fine (then it was their acknowledgment that got lost).

Every frame's header already names its modulation, so the receiver needs
no handshake -- only the sender decides.
"""

from __future__ import annotations

import functools
import math
import statistics
import struct
import threading
import time
from dataclasses import dataclass, field

import fec
import ofdm_core as ofdm

def _locked(fn):
    """Serialize a method on self._lock (used from audio, bridge, TX and GUI threads)."""
    @functools.wraps(fn)
    def w(self, *a, **k):
        with self._lock:
            return fn(self, *a, **k)
    return w


# ── AX.25 helpers ─────────────────────────────────────────────────────────


def _call(addr7: bytes) -> str:
    call = "".join(chr(b >> 1) for b in addr7[:6]).strip()
    ssid = (addr7[6] >> 1) & 0x0F
    return f"{call}-{ssid}" if ssid else call


def ax25_parse(frame: bytes):
    """(dest, src, control, info) or None if it doesn't look like AX.25."""
    if len(frame) < 15:
        return None
    end = None
    for i in range(6, min(len(frame), 70), 7):         # dest, src, up to 8 digis
        if frame[i] & 0x01:
            end = i
            break
    if end is None or end < 13 or end + 1 >= len(frame):
        return None
    control = frame[end + 1]
    info_start = end + 2
    if (control & 0x01) == 0:                           # I-frame: control + PID
        info_start = end + 3
    return _call(frame[0:7]), _call(frame[7:14]), control, frame[info_start:]


# ── digipeater paths (v0.9.37) ───────────────────────────────────────────
# AX.25 address field: destination, source, then up to 8 digipeaters. A
# digipeater sets the H ("has been repeated") bit, 0x80 in the SSID byte of
# its own entry, when it repeats the frame. So on the air:
#   next hop   = the first digipeater not yet repeated, else the destination
#                (the station that is meant to hear THIS transmission)
#   heard from = the last digipeater already repeated, else the source
#                (the station that actually transmitted it)
# Auto speed, link reports and saved levels are about the RADIO link, so they
# follow the hop, not the end-to-end callsigns.

H_BIT = 0x80


def norm_call(call: str) -> str:
    """'dx1cw-0' -> 'DX1CW'; 'DX1CW-10' unchanged."""
    c = (call or "").strip().upper()
    return c[:-2] if c.endswith("-0") else c


def base_call(call: str) -> str:
    return norm_call(call).split("-")[0]


def ax25_addresses(frame: bytes):
    """[(call, h_bit)] for destination, source and each digipeater, plus the
    index of the last address byte; None if it doesn't look like AX.25."""
    if len(frame) < 15:
        return None
    out = []
    for i in range(0, min(len(frame), 70), 7):
        a = frame[i:i + 7]
        if len(a) < 7:
            return None
        out.append((_call(a), bool(a[6] & H_BIT)))
        if a[6] & 0x01:
            if len(out) < 2:
                return None
            return out, i + 6
    return None


def ax25_hops(frame: bytes):
    """(next_hop, heard_from, digis) -- digis as [(call, repeated)] -- or
    (None, None, []) if not AX.25."""
    a = ax25_addresses(frame)
    if a is None:
        return None, None, []
    addrs = a[0]
    dest, src, digis = addrs[0][0], addrs[1][0], addrs[2:]
    nxt = next((c for c, h in digis if not h), dest)
    heard = next((c for c, h in reversed(digis) if h), src)
    return nxt, heard, digis


def is_relayed(frame: bytes) -> bool:
    """True if a digipeater has already repeated this frame (any H bit set)."""
    return any(h for _, h in ax25_hops(frame)[2])


def in_transit(frame: bytes) -> bool:
    """True if the frame still has a digipeater to go through."""
    return any(not h for _, h in ax25_hops(frame)[2])


def frame_trace(frame: bytes) -> str:
    """v0.9.60: one AX.25 frame in a few characters, for the log --
    'I3/5 #a1f2', 'RR5', 'REJ2', 'SABM', 'UA', 'DISC', 'UI #..'. I = info frame
    N(S)/N(R) and a 16-bit checksum of its data, so the same frame can be
    followed through every station's log (and a duplicate or a changed one
    spotted)."""
    import zlib
    a = ax25_addresses(frame)
    if a is None:
        return "?"
    n = 7 * len(a[0])
    if len(frame) <= n:
        return "?"
    c = frame[n]
    # v0.9.65: P/F bit shown as "p" on a command (poll) and "f" on a response
    # (final), from the C bits in the destination / source SSID bytes; "p?"
    # when they don't say (old AX.25 v1 style, both bits equal)
    pf = ""
    if c & 0x10:
        cmd, resp = bool(frame[6] & 0x80), bool(frame[13] & 0x80)
        pf = "p" if cmd and not resp else "f" if resp and not cmd else "p?"
    if c & 0x01 == 0:                                   # I frame
        info = frame[n + 2:]
        return f"I{(c >> 1) & 7}/{(c >> 5) & 7}{pf} #{zlib.crc32(info) & 0xFFFF:04x}"
    if c & 0x03 == 0x01:                                # S frame
        kind = ("RR", "RNR", "REJ", "SREJ")[(c >> 2) & 3]
        return f"{kind}{(c >> 5) & 7}{pf}"
    u = {0x2F: "SABM", 0x6F: "SABME", 0x43: "DISC", 0x63: "UA", 0x0F: "DM", 0x87: "FRMR",
         0x03: "UI", 0xAF: "XID", 0xE3: "TEST"}.get(c & 0xEF, f"U{c:02x}")
    if u == "UI":
        return f"UI #{zlib.crc32(frame[n + 2:]) & 0xFFFF:04x}"
    if u == "FRMR":
        return u + pf + frmr_detail(frame[n + 1:n + 4])
    return u + pf


def _ctl_name(c: int) -> str:
    if c & 0x01 == 0:
        return f"I{(c >> 1) & 7}/{(c >> 5) & 7}"
    if c & 0x03 == 0x01:
        return ("RR", "RNR", "REJ", "SREJ")[(c >> 2) & 3] + str((c >> 5) & 7)
    return {0x2F: "SABM", 0x6F: "SABME", 0x43: "DISC", 0x63: "UA", 0x0F: "DM", 0x87: "FRMR",
            0x03: "UI", 0xAF: "XID", 0xE3: "TEST"}.get(c & 0xEF, f"U{c:02x}")


def frmr_detail(info: bytes) -> str:
    """v0.9.65: what a frame reject (FRMR) says it rejected, and why:
    '[rejected I6/6, their V(S)=6 V(R)=6: Z bad N(R)]'. W = control field not
    understood, X = info not allowed in that frame, Y = info too long,
    Z = N(R) acknowledges a frame never sent."""
    if len(info) < 3:
        return "[?]"
    why = [t for bit, t in ((0x01, "W bad control"), (0x02, "X info not allowed"),
                            (0x04, "Y info too long"), (0x08, "Z bad N(R)")) if info[2] & bit]
    return (f"[rejected {_ctl_name(info[0])}, their V(S)={(info[1] >> 1) & 7} V(R)={(info[1] >> 5) & 7}"
            f"{', it was a response' if info[1] & 0x10 else ''}: {', '.join(why) or 'no reason given'}]")


def batch_trace(frames: list) -> str:
    return " ".join(frame_trace(f) for f in frames)


# ── stale resends from the client (v0.9.61) ──────────────────────────────
# Field (0.9.60, DW1IRF Winlink Express -> DV1UK-10 direct, BPSK): Winlink
# Express times FRACK from when it hands a frame to the KISS port, not from
# when it goes on the air. A 4-frame BPSK window takes ~13 s to air, so its
# timer ran out while the window was still being sent and it handed the same
# frames over again -- SARA FM aired them straight behind the originals, in
# the same key-up (I5 I6 I7 I0 I5 I6 I7 I0 ...). DV1UK-10 never got a gap to
# answer in (it waited 30 s for a clear channel, three times), the session
# looped for 2 minutes and disconnected. Earlier, a resend queued just before
# DV1UK-10's acknowledgment arrived was aired anyway (20 s of BPSK), and
# DV1UK-10 answered each stale copy with REJ5.
# So a resent I frame is skipped when it can't help: the other end has
# already acknowledged it, or it was already sent since the other end last
# had a chance to answer. This sends nothing of its own and changes no AX.25
# timing -- the client's own timers still do every real retry.

def ack_info(frame: bytes):
    """(from, to, N(R)) for a frame that acknowledges (I, RR, RNR, REJ), else
    None. Modulo 8."""
    a = ax25_addresses(frame)
    if a is None:
        return None
    n = 7 * len(a[0])
    if len(frame) <= n:
        return None
    c = frame[n]
    if c & 0x01 == 0 or (c & 0x03 == 0x01 and (c >> 2) & 3 != 3):
        return norm_call(a[0][1][0]), norm_call(a[0][0][0]), (c >> 5) & 7
    return None


def is_link_reset(frame: bytes) -> bool:
    """SABM / DISC / UA / DM: numbering starts over."""
    p = ax25_parse(frame)
    return p is not None and (p[2] & 0x03) == 0x03 and (p[2] & 0xEF) in (0x2F, 0x6F, 0x43, 0x63, 0x0F)


def iframe_key(frame: bytes):
    """(link, N(S), info) for an I frame -- the same frame resent, whatever
    its N(R) or P bit -- else None. link = (from, to)."""
    p = ax25_parse(frame)
    if p is None or not is_iframe(p[2]):
        return None
    dest, src, control, info = p
    return (norm_call(src), norm_call(dest)), (control >> 1) & 7, bytes(info)


class StaleFrames:
    """Tracks our own I frames on the air and the other end's acknowledgments.
    Thread-safe (bridge, transmit and GUI threads)."""
    KEEP_S = 300.0

    def __init__(self):
        self._lock = threading.Lock()
        self._aired: dict = {}        # key -> time it last went out
        self._last_ns: dict = {}      # link -> N(S) of the newest I frame aired
        self._acked: dict = {}        # (link, ns) -> time acknowledged
        self._heard: dict = {}        # link -> time the other end was last heard
        self._queued: dict = {}       # key -> copies waiting to go out
        self.skipped = 0

    def _prune(self, now):
        for d in (self._aired, self._acked, self._heard):
            for k in [k for k, t in d.items() if now - t > self.KEEP_S]:
                del d[k]

    def _reset(self, link):
        for d in (self._aired, self._queued):
            for k in [k for k in d if k[0] == link]:
                del d[k]
        for k in [k for k in self._acked if k[0] == link]:
            del self._acked[k]
        self._last_ns.pop(link, None)

    @_locked
    def on_queued(self, frames, now=None):
        for f in frames:
            k = iframe_key(f)
            if k is not None:
                self._queued[k] = self._queued.get(k, 0) + 1

    def _unqueue(self, k):
        n = self._queued.get(k, 0) - 1
        if n > 0:
            self._queued[k] = n
        else:
            self._queued.pop(k, None)

    @_locked
    def on_dropped(self, frames):
        for f in frames:
            k = iframe_key(f)
            if k is not None:
                self._unqueue(k)

    @_locked
    def clear_queued(self):
        self._queued.clear()

    @_locked
    def on_aired(self, frames, now=None):
        now = time.time() if now is None else now
        self._prune(now)
        for f in frames:
            if is_link_reset(f):
                p = ax25_parse(f)
                self._reset((norm_call(p[1]), norm_call(p[0])))
                self._reset((norm_call(p[0]), norm_call(p[1])))
                continue
            k = iframe_key(f)
            if k is None:
                continue
            self._unqueue(k)
            self._aired[k] = now
            self._last_ns[k[0]] = k[1]

    @_locked
    def on_heard(self, frames, now=None):
        """Frames decoded off the air (any copy: direct or relayed)."""
        now = time.time() if now is None else now
        for f in frames:
            if is_link_reset(f):
                p = ax25_parse(f)
                self._reset((norm_call(p[1]), norm_call(p[0])))
                self._reset((norm_call(p[0]), norm_call(p[1])))
                continue
            a = ack_info(f)
            if a is None:
                continue
            frm, to, nr = a
            link = (to, frm)                 # our frames to them
            self._heard[link] = now
            last = self._last_ns.get(link)
            if last is None:
                continue
            d_r = (last - (nr - 1)) % 8      # how far back from the newest the last one acked is
            for d in range(d_r, 7):
                self._acked[(link, (last - d) % 8)] = now

    @_locked
    def why_stale(self, frame: bytes, turn_t: float = 0.0):
        """'acked' / 'unanswered' / None. turn_t: when our transmitter last
        went quiet long enough for the other end to answer."""
        k = iframe_key(frame)
        if k is None:
            return None
        ta = self._aired.get(k)
        if ta is None:
            return None
        tk = self._acked.get((k[0], k[1]))
        if tk is not None and tk > ta:
            return "acked"
        if ta > max(turn_t, self._heard.get(k[0], 0.0)):
            return "unanswered"
        return None

    @_locked
    def is_queued(self, frame: bytes) -> bool:
        k = iframe_key(frame)
        return k is not None and self._queued.get(k, 0) > 0


# ── the digipeater goes first (v0.9.61) ──────────────────────────────────
# Field (0.9.60, DW1IRF -> DX1FLR-10 via DV1UK): DW1IRF sent a 7-frame BPSK
# window; DV1UK and DX1FLR both heard it and both waited for it to end. The
# moment it did, DV1UK started relaying and DX1FLR (its BPQ32 retry timer had
# run out meanwhile) keyed up too -- DX1FLR could not hear the first relayed
# frame while it transmitted. That frame was I6 every time; DX1FLR answered
# REJ6 six times and the message never went through. A station that has just
# heard frames still on their way through a digipeater now leaves the channel
# to that digipeater: after the channel clears it waits RELAY_YIELD_S for the
# relay to start, and while it is running, until the relayed copies are in.

def relay_key(frame: bytes) -> bytes:
    """The frame with every digipeater 'repeated' bit cleared: the copy heard
    direct and the relayed copy give the same key."""
    a = ax25_addresses(frame)
    if a is None:
        return bytes(frame)
    b = bytearray(frame)
    for k in range(2, len(a[0])):
        b[7 * k + 6] &= ~H_BIT & 0xFF
    return bytes(b)


class RelayWatch:
    EXPECT_S = 60.0          # a relay not heard by then isn't coming

    def __init__(self):
        self._lock = threading.Lock()
        self._pending: dict = {}

    @_locked
    def expect(self, frames, now=None):
        now = time.time() if now is None else now
        for f in frames:
            if in_transit(f):
                self._pending[relay_key(f)] = now

    @_locked
    def heard(self, frames):
        for f in frames:
            if is_relayed(f):
                self._pending.pop(relay_key(f), None)

    SEEN_MARGIN_S = 0.3

    @_locked
    def pending(self, now=None, seen=None) -> bool:
        """seen: when another SARA FM station was last heard on the air
        (v0.9.70). Field (0.9.69, DX1FLR via DW1IRF): DW1IRF relayed DX1FLR's
        own frames in 16QAM -- right for its next hop, DV1UK-10, but too fast
        for DX1FLR to decode -- so DX1FLR never "heard the relay", and every
        transmission it made waited 4.5 s for one, the first connect request
        included. Something heard on the air after a frame went out means its
        relay has happened, decoded or not."""
        now = time.time() if now is None else now
        for k in [k for k, t in self._pending.items()
                  if now - t > self.EXPECT_S or (seen is not None and seen > t + self.SEEN_MARGIN_S)]:
            del self._pending[k]
        return bool(self._pending)

    @_locked
    def give_up(self):
        self._pending.clear()


def path_str(frame: bytes) -> str:
    """'DW1IRF>DX1FLR-10 via DX1CW*' (* = already repeated), or '' with no
    digipeaters."""
    a = ax25_addresses(frame)
    if a is None or len(a[0]) < 3:
        return ""
    addrs = a[0]
    return (f"{addrs[1][0]}>{addrs[0][0]} via "
            + ",".join(c + ("*" if h else "") for c, h in addrs[2:]))


def _encode_call(call: str, ssid_byte_low: int) -> bytes:
    """Address bytes for call (with optional -SSID); ssid_byte_low keeps the
    caller's H/C, reserved and end-of-address bits."""
    c = norm_call(call)
    name, _, ssid = c.partition("-")
    try:
        n = int(ssid) if ssid else 0
    except ValueError:
        n = 0
    name = (name + "      ")[:6]
    return bytes(ord(ch) << 1 for ch in name) + bytes([(ssid_byte_low & 0xE1) | ((n & 0x0F) << 1)])


def digipeat(frame: bytes, my_call: str, aliases=()):
    """The frame as this station repeats it, or None if it isn't ours to
    repeat: the first not-yet-repeated digipeater must be my_call (SSID
    included) or one of the aliases. Sets that entry's H bit; an alias is
    replaced by my_call so the path shows who really repeated it."""
    a = ax25_addresses(frame)
    mine = norm_call(my_call)
    if a is None or not mine:
        return None
    addrs, _ = a
    for k in range(2, len(addrs)):
        call, h = addrs[k]
        if h:
            continue
        c = norm_call(call)
        names = {mine} | {norm_call(x) for x in aliases if x and x.strip()}
        if c not in names:
            return None
        off = 7 * k
        low = frame[off + 6] | H_BIT                 # keeps reserved + end-of-address bits
        entry = frame[off:off + 6] + bytes([low]) if c == mine else _encode_call(mine, low)
        return frame[:off] + entry + frame[off + 7:]
    return None


# ── gateway: digipeated callers shown to BPQ32 as direct (v0.9.71) ────────
# Field (0.9.55-0.9.70, both PAREDN gateways): a Winlink message through
# SARA FM's digipeater failed the CMS B2 checksum every time -- DW1IRF ->
# DX1FLR-10 via DV1UK and DX1PRD-2 -> DV1UK-10 via DW1IRF -- while the SAME
# message, the SAME AX.25 frames (identical content, accepted in the same
# order), went through when sent direct. The only thing BPQ32 saw differently
# was the digipeater in the address field. So, at the station that is
# connected TO (the gateway), a session that came in through a digipeater is
# handed to BPQ32 as a plain direct one, and SARA FM puts the path back on
# BPQ32's replies. Nothing else changes: same frames, same order, same timers.

def strip_digis(frame: bytes) -> bytes:
    """The frame without its digipeater entries (source marked last)."""
    a = ax25_addresses(frame)
    if a is None or len(a[0]) < 3:
        return frame
    end = a[1]                                    # last address byte
    src = bytearray(frame[7:14])
    src[6] |= 0x01
    return frame[:7] + bytes(src) + frame[end + 1:]


def add_digis(frame: bytes, digis: list) -> bytes:
    """A frame with no digipeaters, with `digis` (raw 7-byte entries, H bit
    cleared) inserted after the source."""
    a = ax25_addresses(frame)
    if a is None or len(a[0]) != 2 or not digis:
        return frame
    src = bytearray(frame[7:14])
    src[6] &= 0xFE
    out = bytearray(frame[:7]) + src
    for i, d in enumerate(digis):
        e = bytearray(d[:7])
        e[6] = (e[6] & ~(H_BIT | 0x01)) & 0xFF
        if i == len(digis) - 1:
            e[6] |= 0x01
        out += e
    return bytes(out) + frame[14:]


def _raw_digis(frame: bytes) -> list:
    a = ax25_addresses(frame)
    if a is None:
        return []
    return [bytes(frame[7 * k:7 * k + 7]) for k in range(2, len(a[0]))]


def _is_connect(control: int) -> bool:
    return (control & 0xEF) in (0x2F, 0x6F)          # SABM, SABME


class ViaMap:
    """Per (remote, local) pair: the digipeater path a session came in on."""
    IDLE_S = 900.0

    def __init__(self):
        self._lock = threading.Lock()
        self._paths: dict = {}       # (remote, local) -> [digis calls, reply path raw, last used]

    @_locked
    def to_client(self, frame: bytes, now=None):
        """RX, a frame about to be passed to BPQ32: (frame to pass, note or
        None). The note is said once per new digipeated connection."""
        now = time.time() if now is None else now
        p = ax25_parse(frame)
        if p is None:
            return frame, None
        dest, src, control, _ = p
        key = (norm_call(src), norm_call(dest))
        hops = ax25_hops(frame)[2]
        if _is_connect(control):
            if not hops:
                self._paths.pop(key, None)
                return frame, None
            if all(h for _, h in hops):
                calls = tuple(norm_call(c) for c, _ in hops)
                new = key not in self._paths or self._paths[key][0] != calls
                self._paths[key] = [calls, list(reversed(_raw_digis(frame))), now]
                note = (f"{key[0]} connected to {key[1]} via {', '.join(calls)}: BPQ32 is shown a direct "
                        f"connection, and SARA FM adds the path back to its replies") if new else None
                return strip_digis(frame), note
        e = self._paths.get(key)
        if e is None or not hops or not all(h for _, h in hops):
            return frame, None
        if now - e[2] > self.IDLE_S or tuple(norm_call(c) for c, _ in hops) != e[0]:
            return frame, None
        e[2] = now
        return strip_digis(frame), None

    @_locked
    def from_client(self, frame: bytes, now=None) -> bytes:
        """TX, a frame from BPQ32: the path put back when it answers a
        caller that came in through a digipeater."""
        now = time.time() if now is None else now
        p = ax25_parse(frame)
        if p is None or ax25_hops(frame)[2]:
            return frame
        dest, src, _, _ = p
        e = self._paths.get((norm_call(dest), norm_call(src)))
        if e is None or now - e[2] > self.IDLE_S:
            return frame
        e[2] = now
        return add_digis(frame, e[1])

    @_locked
    def active(self) -> bool:
        return bool(self._paths)


def mark_repeated(frame: bytes) -> bytes:
    """The frame as it looks once every digipeater has repeated it."""
    a = ax25_addresses(frame)
    if a is None:
        return frame
    b = bytearray(frame)
    for k in range(2, len(a[0])):
        b[7 * k + 6] |= H_BIT
    return bytes(b)


class DirectFallback:
    """v0.9.72: frames for this station's client heard DIRECT while still on
    their way through the digipeater. They are held (v0.9.57: passing both
    copies doubled every frame) -- but field (0.9.71, DX1FLR): eight times in
    one session DX1FLR heard DV1UK-10's answer direct, clean, and then could
    not decode DW1IRF's relay of it, so Winlink Express waited out its retry
    timer (up to 25 s) for an answer that was already here. Now a held copy
    is passed on (as the relay would have delivered it) when the relay
    doesn't bring it: at once if the relay brings LATER frames of the same
    session (relays keep the order), else FALLBACK_S after the last one was
    heard. The relayed copy of a frame already passed on is then dropped."""
    FALLBACK_S = 6.0
    DONE_KEEP_S = 60.0

    def __init__(self):
        self._lock = threading.Lock()
        self._held: dict = {}        # relay_key -> (pair, seq, frame as relayed)
        self._seq = 0
        self._last: dict = {}        # pair -> time of the last held frame
        self._done: dict = {}        # relay_key -> time passed on from the direct copy

    @staticmethod
    def _pair(frame):
        p = ax25_parse(frame)
        return (norm_call(p[1]), norm_call(p[0])) if p else None

    @_locked
    def hold(self, frame: bytes, now=None):
        now = time.time() if now is None else now
        pair = self._pair(frame)
        if pair is None:
            return
        k = relay_key(frame)
        self._done.pop(k, None)
        self._held.pop(k, None)            # a repeat goes to the back: order heard
        self._seq += 1
        self._held[k] = (pair, self._seq, mark_repeated(frame))
        self._last[pair] = now

    def _release(self, keys, now):
        out = []
        for k in keys:
            e = self._held.pop(k, None)
            if e is not None:
                out.append(e[2])
                self._done[k] = now
        return out

    @_locked
    def relayed(self, frame: bytes, now=None):
        """A relayed copy is about to go to the client: (held frames to pass
        on first, pass this one on?)."""
        now = time.time() if now is None else now
        for k in [k for k, t in self._done.items() if now - t > self.DONE_KEEP_S]:
            del self._done[k]
        k = relay_key(frame)
        if k in self._done:
            del self._done[k]
            return [], False
        if k not in self._held:
            return [], True
        # v0.9.73: strictly the frames heard BEFORE this one. 0.9.72 compared
        # decode times, and frames of one burst share a time: the relay of
        # {UA I0} passed I0 on ahead of the UA, and {I0..I4} put I3 first --
        # out of order, so Winlink Express dropped them and stalled.
        pair, seq, _ = self._held.pop(k)
        earlier = [kk for kk, e in sorted(self._held.items(), key=lambda it: it[1][1])
                   if e[0] == pair and e[1] < seq]
        return self._release(earlier, now), True

    @_locked
    def due(self, now=None) -> list:
        now = time.time() if now is None else now
        ready = {p for p, t in self._last.items() if now - t >= self.FALLBACK_S}
        keys = [k for k, e in sorted(self._held.items(), key=lambda it: it[1][1]) if e[0] in ready]
        for p in ready:
            self._last.pop(p, None)
        return self._release(keys, now)

    @_locked
    def waiting(self) -> bool:
        return bool(self._held)


def first_frame_hops(payload: bytes):
    """(next_hop, heard_from) of the first AX.25 frame in a batch, or
    (None, None)."""
    end = _frames_end(payload)
    if end is None or payload[1] < 1:
        return None, None
    n = struct.unpack(">H", payload[2:4])[0]
    nxt, heard, _ = ax25_hops(payload[4:4 + n])
    return nxt, heard


def is_iframe(control: int) -> bool:
    return (control & 0x01) == 0


def is_ui(control: int) -> bool:
    """AX.25 unnumbered information frame (beacons, APRS, ID) -- not part of a
    connected session."""
    return (control & 0xEF) == 0x03


def session_frames(frames) -> bool:
    """v0.9.48: True if any frame belongs to a connected AX.25 session (not
    only UI beacons). Field (DW3TDJ): BPQ32's APRS beacon every 5 minutes made
    SARA FM think a Winlink session was running, so pings went unanswered."""
    for f in frames:
        p = ax25_parse(f)
        if p is None or not is_ui(p[2]):
            return True
    return False


def is_sabm(control: int) -> bool:
    """AX.25 connect request (SABM, P bit either way)."""
    return (control & 0xEF) == 0x2F


# ── link report trailer ──────────────────────────────────────────────────

REPORT_MAGIC = b"\xA5\x52"
REPORT_VERSION = 1
REPORT_LEN = 9          # magic(2) ver mode n_ok n_bad snr(int8) fec_max% fec_avg%
# v0.9.4: one more byte after those 9: what this station can DECODE. Older
# versions read exactly 9 bytes and ignore the rest, so the format stays
# version 1; a 9-byte report means "no extras".
CAP_QAM16 = 0x01
CAP_QAM64 = 0x02        # v0.9.18: decodes 64-QAM Wide
CAP_CODED = 0x04        # v0.9.24: decodes the coded Wide modes (ofdm_core.CODED)
MY_CAPS = CAP_QAM16 | CAP_QAM64 | CAP_CODED
# v0.9.18: one more byte after caps: the median DATA-CARRIER S/N of the
# frames reported on (int8, 0x80 = not measured). Versions up to v0.9.17
# read the caps byte and ignore the rest. 64-QAM needs clean carriers, and
# the S/N figure alone (min of preamble and carriers + offset) can't show
# them: a distorted-but-strong link and a clean one can report the same.
BATCH_MARKER = 0x01     # == bpq_bridge.BATCH_MARKER (not imported: avoid a cycle)


@dataclass
class Report:
    mode: int           # modulation of the frames being reported on
    n_ok: int
    n_bad: int
    snr_db: int         # median preamble S/N of those frames
    fec_max_pct: int    # worst frame: bytes repaired / FEC capacity
    fec_avg_pct: int
    caps: int = 0       # v0.9.4: the SENDER's receive capabilities (CAP_*)
    carrier_snr_db: int | None = None   # v0.9.18: median data-carrier S/N

    def pack(self) -> bytes:
        c = lambda v, lo, hi: max(lo, min(hi, int(round(v))))
        car = 0x80 if self.carrier_snr_db is None else (c(self.carrier_snr_db, -127, 127) & 0xFF)
        return REPORT_MAGIC + struct.pack("BBBBbBB", REPORT_VERSION, self.mode & 0xFF,
                                          c(self.n_ok, 0, 255), c(self.n_bad, 0, 255),
                                          c(self.snr_db, -128, 127), c(self.fec_max_pct, 0, 100),
                                          c(self.fec_avg_pct, 0, 100)) + bytes([self.caps & 0xFF, car])


def _frames_end(payload: bytes) -> int | None:
    """Byte offset just past the counted frames of a batch, or None."""
    if len(payload) < 2 or payload[0] != BATCH_MARKER:
        return None
    pos = 2
    for _ in range(payload[1]):
        if pos + 2 > len(payload):
            return None
        n = struct.unpack(">H", payload[pos:pos + 2])[0]
        pos += 2 + n
        if pos > len(payload):
            return None
    return pos


def _split_counted(payload: bytes) -> list:
    """The counted AX.25 frames of a batch (report trailer ignored)."""
    if _frames_end(payload) is None:
        return []
    out, pos = [], 2
    for _ in range(payload[1]):
        n = struct.unpack(">H", payload[pos:pos + 2])[0]
        out.append(payload[pos + 2:pos + 2 + n])
        pos += 2 + n
    return out


def attach_report(payload: bytes, report: Report | None) -> bytes:
    """Batch payload + report trailer. Non-batches, or no report: unchanged."""
    if report is None or _frames_end(payload) != len(payload):
        return payload
    return payload + report.pack()


def split_report(payload: bytes) -> Report | None:
    end = _frames_end(payload)
    if end is None or len(payload) - end < REPORT_LEN:
        return None
    return parse_report(payload[end:])


def parse_report(t: bytes) -> Report | None:
    """A packed Report (Report.pack()) -> Report, or None. v0.9.39: also
    carried by SARA Chat receipts."""
    if len(t) < REPORT_LEN or t[:2] != REPORT_MAGIC or t[2] != REPORT_VERSION:
        return None
    _, mode, n_ok, n_bad, snr, fmax, favg = struct.unpack("BBBBbBB", t[2:REPORT_LEN])
    caps = t[REPORT_LEN] if len(t) > REPORT_LEN else 0
    car = None
    if len(t) > REPORT_LEN + 1 and t[REPORT_LEN + 1] != 0x80:
        car = struct.unpack("b", t[REPORT_LEN + 1:REPORT_LEN + 2])[0]
    return Report(mode, n_ok, n_bad, snr, fmax, favg, caps, car)


def first_frame_calls(payload: bytes):
    """(dest, src) of the first AX.25 frame in a batch, or (None, None)."""
    end = _frames_end(payload)
    if end is None or payload[1] < 1:
        return None, None
    n = struct.unpack(">H", payload[2:4])[0]
    p = ax25_parse(payload[4:4 + n])
    return (p[0], p[1]) if p else (None, None)


# ── receive-side statistics (what we report back) ────────────────────────

def fec_capacity(modulation: int, payload_len: int) -> int:
    """Bytes FEC could have repaired in a frame carrying payload_len bytes."""
    return (fec.NSYM // 2) * fec._n_blocks(payload_len + 2, ofdm.fec_block(modulation))


@dataclass
class _PeerRx:
    mode: int = -1
    n_ok: int = 0
    n_bad: int = 0
    snrs: list = field(default_factory=list)
    fec_pcts: list = field(default_factory=list)
    cars: list = field(default_factory=list)      # v0.9.18: data-carrier S/N per frame
    last_seen: float = 0.0


# Receiver rejections that mean "a real frame from the other station was
# lost" (v0.9.11: a header too weak for the mode it claims too -- at a real
# link fade that is a frame in a mode the link no longer carries).
LOST_REASONS = ("fec_uncorrectable", "crc_fail", "snr_too_low_for_mode")


class RxStats:
    """How we hear each peer, since we last reported to it."""
    ACTIVE_PEER_S = 20.0      # an undecodable frame is blamed on the peer heard this recently
    MIN_BAD_SNR_DB = 5.0      # ...and only if it was a real frame, not noise

    def __init__(self):
        self._lock = threading.RLock()
        self.peers: dict[str, _PeerRx] = {}
        self._last_peer = None

    @_locked
    def on_frame(self, src: str, modulation: int, snr_db, fec_corrected: int, payload_len: int,
                 now: float | None = None, carrier_snr_db=None):
        now = time.time() if now is None else now
        p = self.peers.setdefault(src, _PeerRx())
        if p.mode != modulation:          # report on one mode at a time
            p.n_ok, p.n_bad, p.snrs, p.fec_pcts, p.cars = 0, 0, [], [], []
            p.mode = modulation
        p.n_ok += 1
        if snr_db is not None:
            p.snrs.append(float(snr_db))
        if carrier_snr_db is not None:
            p.cars.append(float(carrier_snr_db))
        cap = max(1, fec_capacity(modulation, payload_len))
        p.fec_pcts.append(100.0 * fec_corrected / cap)
        p.last_seen = now
        self._last_peer = src

    @_locked
    def on_lost(self, snr_db, now: float | None = None):
        """A frame that failed to decode (FEC/CRC) -- charged to the peer
        we've been hearing, if any."""
        now = time.time() if now is None else now
        if snr_db is None or snr_db < self.MIN_BAD_SNR_DB or self._last_peer is None:
            return
        p = self.peers.get(self._last_peer)
        if p is None or now - p.last_seen > self.ACTIVE_PEER_S:
            return
        p.n_bad += 1

    @_locked
    def reset_peer(self, peer: str):
        """Forget how we heard this peer (new connection, v0.9.1)."""
        self.peers.pop(peer, None)

    @_locked
    def take_report(self, peer: str) -> Report | None:
        p = self.peers.get(peer)
        if p is None or (p.n_ok + p.n_bad) == 0 or p.mode < 0:
            return None
        r = Report(mode=p.mode, n_ok=p.n_ok, n_bad=p.n_bad, caps=MY_CAPS,
                   snr_db=int(round(statistics.median(p.snrs))) if p.snrs else -128,
                   fec_max_pct=int(round(max(p.fec_pcts))) if p.fec_pcts else 0,
                   fec_avg_pct=int(round(sum(p.fec_pcts) / len(p.fec_pcts))) if p.fec_pcts else 0,
                   carrier_snr_db=int(round(statistics.median(p.cars))) if p.cars else None)
        p.n_ok, p.n_bad, p.snrs, p.fec_pcts, p.cars = 0, 0, [], [], []
        return r


# v0.9.6: the S/N we REPORT. Field (DV1UK -> DU3FS, v0.9.5): frames arrived
# with a 27-31 dB preamble S/N while the data carriers measured only 1-11 dB
# (carrier check) -- 52 of ~100 QPSK frames lost, yet Auto kept "trying
# faster again: S/N 28 dB is strong enough". The preamble S/N compares the
# four identical preamble repetitions, so a signal distorted the SAME way
# each time (TX overdrive, a steady tone) still reads "clean". The data
# carriers' error-vector S/N sees it. On healthy links it runs ~8 dB below
# the preamble figure (K6IRF/DW1IRF: preamble ~30, carriers median ~22), so
# the report uses min(preamble, carriers + offset): unchanged on a clean
# link, honest on a distorted one.
# v0.9.7: offset 8 -> 14. Field (v0.9.6, K6IRF -> DW1IRF): that path's
# narrow carriers run ~14 dB below the preamble yet decode cleanly; with 8
# its reports dropped to 21-23 dB, below STRONG_SNR_DB, so resent frames
# walked Auto down to BPSK and sends got slower (9.7 kB: 0:52 -> 1:15-2:07).
# DV1UK's distorted signal (gap ~20) is still reported ~22 dB, not 28.
CARRIER_SNR_OFFSET_DB = 14.0


def carrier_snr_db(frame):
    """Median per-carrier error-vector S/N (dB) of a decoded frame, or None."""
    cs = getattr(frame, "carrier_stats", None)
    if not cs:
        return None
    try:
        evm, evm_n = cs[3], cs[4]
        vals = sorted(10 * math.log10(n / e) for e, n in zip(evm, evm_n) if n > 0 and e > 0)
    except Exception:
        return None
    return vals[len(vals) // 2] if vals else None


def link_snr_db(frame):
    """The S/N to report for a frame: see CARRIER_SNR_OFFSET_DB."""
    s = getattr(frame, "snr_db", None)
    c = carrier_snr_db(frame)
    if c is None:
        return s
    if s is None:
        return c + CARRIER_SNR_OFFSET_DB
    return min(s, c + CARRIER_SNR_OFFSET_DB)


# ── speed controller (what we send with) ─────────────────────────────────

# v0.9.24: coded rungs (see ofdm_core.CODED) between the plain ones, by
# simulated throughput (250-byte frames, FM channel): QPSK Wide ~2.1 kbps
# (data carriers >= ~9 dB), 16QAM-C ~2.6 (>= ~13), 16QAM ~3.8 (>= ~16),
# 64QAM-C ~4.2 (>= ~18), 64QAM ~5.2 (>= ~21).
LADDER = (ofdm.MOD_BPSK, ofdm.MOD_QPSK, ofdm.MOD_QPSK_WIDE, ofdm.MOD_QAM16_WIDE_C, ofdm.MOD_QAM16_WIDE,
          ofdm.MOD_QAM64_WIDE_C, ofdm.MOD_QAM64_WIDE)
NAMES = {ofdm.MOD_BPSK: "BPSK", ofdm.MOD_QPSK: "QPSK", ofdm.MOD_QPSK_WIDE: "QPSK Wide",
         ofdm.MOD_QAM16_WIDE: "16QAM Wide", ofdm.MOD_QAM64_WIDE: "64QAM Wide",
         ofdm.MOD_QAM16_WIDE_C: "16QAM Wide C", ofdm.MOD_QAM64_WIDE_C: "64QAM Wide C"}

# Preamble S/N as REPORTED by the receiver of our frames. From measurement:
# weakest decodable frames ~5 dB (BPSK), ~9.5-10 (QPSK); Wide needs ~1-3 dB
# more than QPSK and was fine in the field at +29 dB with every carrier clean.
# v0.9.4, 16-QAM Wide (simulated, field-shaped and FM channels): decodes
# every frame from ~22 dB preamble S/N, fails below ~19; field links run
# 28-32 dB. Up from Wide at 27 (clean, FEC <= 25%), down below 23.
# v0.9.24: step-up thresholds are by the mode stepped up TO (the ladder now
# skips rungs a station can't decode, so "from" no longer says "to").
UP_SNR_TO = {ofdm.MOD_QPSK: 15, ofdm.MOD_QPSK_WIDE: 21, ofdm.MOD_QAM16_WIDE_C: 20,
             ofdm.MOD_QAM16_WIDE: 27, ofdm.MOD_QAM64_WIDE_C: 30, ofdm.MOD_QAM64_WIDE: 33}
DOWN_SNR_DB = {ofdm.MOD_QPSK: 11, ofdm.MOD_QPSK_WIDE: 16, ofdm.MOD_QAM16_WIDE_C: 16,
               ofdm.MOD_QAM16_WIDE: 20, ofdm.MOD_QAM64_WIDE_C: 24,
               ofdm.MOD_QAM64_WIDE: 28}  # step down below this
# v0.9.18, 64-QAM Wide (simulated FM channels, 548-byte frames): decodes from
# ~29 dB preamble S/N / ~23 dB data-carrier S/N, clean from ~32 / ~25. Up to
# it only with the receiver's carrier report at UP_CARRIER_TO or better, and
# down from it when carriers fall under DOWN_CARRIER_DB.
UP_CARRIER_TO = {ofdm.MOD_QAM16_WIDE_C: 15, ofdm.MOD_QAM16_WIDE: 19,
                 ofdm.MOD_QAM64_WIDE_C: 20, ofdm.MOD_QAM64_WIDE: 24}
# v0.9.22: QPSK Wide -> 16QAM Wide also needs clean carriers when the report
# carries them (v0.9.18+; older reports still step up on S/N alone). Field
# (v0.9.21, DV1UK-10 -> DW1IRF): QPSK Wide was spotless at 21-23 dB with
# carriers 13-18 dB, so the spotless probe kept trying 16QAM -- which then
# needed 42-92% of its FEC, and one lost 16QAM acknowledgment cost a send a
# 33 s stall. The other direction (carriers 21-22) runs 16QAM at FEC 4-17%.
DOWN_CARRIER_DB = {ofdm.MOD_QAM16_WIDE_C: 12, ofdm.MOD_QAM16_WIDE: 14,
                   ofdm.MOD_QAM64_WIDE_C: 17, ofdm.MOD_QAM64_WIDE: 20}
# (v0.9.24: with the coded rungs in between, 64QAM is back to carriers 24 up
# / 20 down -- field: its one try at carriers 24 used 88% of FEC.)
# v0.9.23 (experiment): 64QAM up at carriers 21 (was 24), down under 18 (was
# 21). Field (v0.9.22, DW1IRF -> DV1UK): 16QAM ran at reported carriers
# 18-23 dB (mostly 20-22, FEC 0-17%) -- never 24 -- and DV1UK decoded manual
# 64QAM pings at ~22 dB with 5 bytes of FEC. Auto tries 64QAM only on a
# spotless 16QAM run (FEC <= 4%, S/N 29+) or S/N 33; a failed try puts it on
# hold, doubling, and losses or heavy FEC in 64QAM step straight back.
# (16QAM Wide: was 23 until v0.9.16 -- see CLEAN_PROBE_* below; its decode
# cliff is ~19 dB, and losses or heavy FEC still step down at any S/N)
UP_FEC_MAX_PCT = 25      # every frame in the report used at most this much FEC
DOWN_FEC_PCT = 60        # any frame this close to the FEC limit -> step down
UP_CLEAN_REPORTS = 2
DOWN_HOLDOFF_S = 8.0     # after a step down, ignore more downs (rest of the old window)
UP_HOLDOFF_S = 30.0      # ...and don't step back up for this long
NO_REPORT_RECOVER_S = 90.0   # no reports (older peer): step back up after this long clean
PEER_EXPIRE_S = 600.0

# v0.9.1 -- field (v0.9.0, run 12): frames were lost while the receiver
# reported S/N 32-36 dB. Losses at a signal that strong aren't the radio
# path's noise -- a slower mode can't help, and BPSK's 3x longer frames
# were hit harder (Auto sat in BPSK for 5 minutes; reconnecting in QPSK
# Wide sent the same message in under a minute). So:
STRONG_SNR_DB = 24       # losses reported at or above this S/N: never step below QPSK
PROBE_AFTER_S = 45.0     # in a slower mode this long with a strong signal: try one step up
PROBE_MAX_S = 180.0      # a probe that fails at once doubles the wait, up to this
PROBE_FAIL_S = 30.0      # "fails at once": stepped back down within this long

# v0.9.10 -- climb faster (field v0.9.9: uploads spent most of each session
# climbing; a report arrives only once per window, ~6-15 s apart).
FAST_UP_MARGIN_DB = 2    # report this far above the step-up S/N, and...
FAST_UP_FEC_PCT = 10     # ...FEC this low: ONE clean report steps up (else UP_CLEAN_REPORTS)
RESUME_WINDOW_S = 6 * 3600.0   # a new connection within this long of a clean session resumes there
# (v0.9.19: was 30 min. Field, v0.9.18: after a ~40 min break between test
# sets, DV1UK started every receive in narrow QPSK again -- 43 of 47 frames
# -- having forgotten that QPSK Wide works better with DW1IRF. A station
# pair's radio path doesn't change in 30 minutes; the first report still
# corrects a wrong start.)

# v0.9.16 -- field (v0.9.14 after Tune TX, DV1UK-10 -> DW1IRF): DV1UK's QPSK
# Wide frames arrived spotless (every carrier clean at 17-19 dB, FEC 0%,
# 0 lost) but at 22-25 dB preamble S/N -- under the 27 dB needed to step up,
# so every receive stayed in QPSK Wide (16QAM decodes from ~22 dB, fails
# below ~19). Now a run of spotless reports at or above CLEAN_PROBE_TO
# tries the next mode up, on the probe timer (a failed try doubles the wait
# and, below STRONG_SNR_DB, puts the faster mode on hold as usual).
CLEAN_PROBE_TO = {ofdm.MOD_QPSK_WIDE: 17, ofdm.MOD_QAM16_WIDE_C: 18, ofdm.MOD_QAM16_WIDE: 22,
                  ofdm.MOD_QAM64_WIDE_C: 27, ofdm.MOD_QAM64_WIDE: 29}   # by mode tried
CLEAN_PROBE_FEC_PCT = 4
CLEAN_PROBE_REPORTS = 3
# v0.9.17 -- field (v0.9.16, DV1UK-10 -> DW1IRF, a weaker day: 19-21 dB):
# QPSK reports were spotless (FEC 0%, 7 ok / 0 lost) but under the 21 dB
# step-up, so sessions sat in narrow QPSK for 45-80 s before a probe moved
# them to QPSK Wide (which decodes from ~12 dB). A spotless QPSK run at 17 dB+
# now tries Wide; spotless tries wait CLEAN_PROBE_AFTER_S (scaled up like the
# probe wait after failed tries), and a session that ENDED spotless in a
# mode starts the next connection in that mode, not one below.
CLEAN_PROBE_AFTER_S = 15.0

# v0.9.20 -- field (v0.9.19, DV1UK-10 -> DW1IRF, receives 3 and 4): every
# lost frame was a long one (306-317 bytes on the air) at the link's normal
# S/N (~20 dB), with every data carrier clean (11-19 dB) on the frames that
# did arrive -- in BPSK as well as QPSK Wide. That is interference hitting
# frames, not a weak signal; yet Auto stepped down to BPSK, whose 3 s frames
# are hit even more often, and the receives took 3:19-3:24 instead of ~1:10.
# Losses while the receiver reports clean carriers (at least
# CLEAN_CARRIER_DB) and light FEC now stop at QPSK Wide (which decodes from
# carriers well under that). Heavy FEC, low S/N or poor carriers still step
# down as before.
CLEAN_CARRIER_DB = 12
CLEAN_CARRIER_FEC_PCT = 25

# v0.9.12 -- remember a mode that just FAILED. Field (v0.9.11, DW1IRF ->
# DV1UK-10, distorted TX audio): BPSK windows arrived clean, QPSK windows
# never did (FEC 67-92% used, 2 of 9 decoded). Every clean BPSK report
# stepped straight back up to QPSK (FAST_UP), the next 7-frame QPSK window
# was lost, and the session sat ~25-35 s in a FRACK wait -- 5 times in one
# message, and every new connection started at QPSK and failed first. Now
# a mode we stepped down FROM because of a failure is off-limits for
# FAIL_HOLD_S (doubling on each repeat failure, up to FAIL_HOLD_MAX_S):
# no step up, probe, creep or new-connection start into it until then.
# Only a mode that failed BEFORE it delivered one clean report (a climb or a
# start that didn't work), at a signal under STRONG_SNR_DB (strong-signal
# losses are handled by the probe logic above), counts. A clean report in
# that mode clears its record.
FAIL_HOLD_S = 90.0
FAIL_HOLD_MAX_S = 600.0

# v0.9.13 -- QPSK (narrow, carriers ~560-1780 Hz) is not always easier than
# QPSK Wide (~750-2620 Hz). Field (v0.9.12, DV1UK-10 -> DW1IRF): DW1IRF heard
# DV1UK's 562/656 Hz carriers at only 5-7 dB S/N, so narrow QPSK frames came
# in with FEC 40-100% used or were lost (often the very first reply of a
# session: a ~20 s Winlink retry each time), while QPSK Wide -- which doesn't
# use those carriers -- reported FEC 0-8%. Auto never got from BPSK to Wide
# without passing through the failing QPSK, and QPSK's holds kept it in BPSK.
# So we keep a short history of each mode's reports from this station; when
# narrow QPSK has been clearly more troubled than Wide, the ladder skips it
# (BPSK <-> QPSK Wide), and a new connection starts in Wide.
MODE_HIST_S = 6 * 3600.0  # reports remembered this long (across connections; v0.9.19: was 30 min)
TROUBLE_FEC_PCT = 40      # a report with a loss or FEC at least this high is "troubled"
SKIP_NARROW_MIN_REPORTS = 2
SKIP_NARROW_GAP = 0.25    # QPSK's troubled share this much above Wide's


@dataclass
class _PeerTx:
    idx: int
    clean: int = 0
    last_down: float = -1e9
    last_report: float = -1e9
    last_report_clean: bool = False
    last_retx: float = -1e9
    last_tx: float = -1e9       # when we last chose a mode for (i.e. sent to) this peer
    last_change: float = -1e9   # v0.9.1: when the mode last changed (or the connection started)
    last_up: float = -1e9
    probe_wait: float = PROBE_AFTER_S
    probed: bool = False        # the last step up was a probe
    last_snr: int | None = None  # S/N in their latest report
    caps: int = 0               # v0.9.4: what they said they can decode
    fail_until: dict = field(default_factory=dict)   # v0.9.12: ladder idx -> held until
    fail_hold: dict = field(default_factory=dict)    # v0.9.12: ladder idx -> last hold length
    mode_ok: bool = False       # v0.9.12: a clean report arrived since entering this mode
    hist: list = field(default_factory=list)   # v0.9.13: (time, ladder idx, troubled)
    spotless: int = 0           # v0.9.16: consecutive spotless reports in this mode
    last_carrier: int | None = None  # v0.9.20: carrier S/N in their latest report
    last_fec: int = 100              # v0.9.20: FEC max % in their latest report
    last_spotless_idx: int = -1 # v0.9.17: mode of the latest report, if it was spotless


STATE_VERSION = 2       # v0.9.24 (the ladder gained the coded rungs)
_STATE_FIELDS = ("idx", "last_report", "last_report_clean", "last_spotless_idx", "probe_wait",
                 "last_snr", "caps", "fail_until", "fail_hold", "hist")


class SpeedController:
    # ── v0.9.19: what Auto learned survives a restart (gateways restart) ──
    @_locked
    def export_state(self, now: float | None = None) -> dict:
        """JSON-friendly memory of each station, for saving to disk."""
        now = time.time() if now is None else now
        out = {"_v": STATE_VERSION}
        for peer, p in self.peers.items():
            if now - max(p.last_report, p.last_change) > RESUME_WINDOW_S:
                continue
            d = {k: getattr(p, k) for k in _STATE_FIELDS}
            d["fail_until"] = {str(k): v for k, v in p.fail_until.items()}
            d["fail_hold"] = {str(k): v for k, v in p.fail_hold.items()}
            d["hist"] = [list(h) for h in p.hist if now - h[0] <= MODE_HIST_S]
            out[peer] = d
        return out

    @_locked
    def import_state(self, state: dict, now: float | None = None):
        """Inverse of export_state. Bad entries are skipped, never fatal."""
        now = time.time() if now is None else now
        if not isinstance(state, dict) or state.get("_v") != STATE_VERSION:
            return            # v0.9.24: ladder indices changed -- an older file is ignored
        for peer, d in state.items():
            if peer == "_v":
                continue
            try:
                p = _PeerTx(idx=max(0, min(len(LADDER) - 1, int(d["idx"]))), last_change=now)
                for k in _STATE_FIELDS:
                    if k in ("idx", "fail_until", "fail_hold", "hist"):
                        continue
                    if k in d:
                        setattr(p, k, d[k])
                p.fail_until = {int(k): float(v) for k, v in d.get("fail_until", {}).items()}
                p.fail_hold = {int(k): float(v) for k, v in d.get("fail_hold", {}).items()}
                p.hist = [(float(h[0]), int(h[1]), bool(h[2])) for h in d.get("hist", [])
                          if now - float(h[0]) <= MODE_HIST_S]
                self.peers[str(peer)] = p
            except Exception:
                continue

    def __init__(self, start=ofdm.MOD_QPSK, ceiling=ofdm.MOD_QAM64_WIDE, on_change=None):
        self.start_idx = LADDER.index(start)
        self.max_idx = LADDER.index(ceiling)
        self.on_change = on_change          # on_change(peer, old_mod, new_mod, reason)
        self.peers: dict[str, _PeerTx] = {}
        self._lock = threading.RLock()

    def _peer(self, peer: str, now: float) -> _PeerTx:
        p = self.peers.get(peer)
        if p is None or now - max(p.last_report, p.last_down, p.last_retx, p.last_change) > PEER_EXPIRE_S and p.idx != self.start_idx:
            old = p
            p = _PeerTx(idx=min(self.start_idx, self.max_idx), last_change=now)
            if old is not None:     # v0.9.12/13: what we learned about this station stays
                p.fail_until, p.fail_hold, p.hist = dict(old.fail_until), dict(old.fail_hold), list(old.hist)
            self.peers[peer] = p
        return p

    # ── v0.9.13: skip narrow QPSK when Wide does better with this station ──
    _QI = LADDER.index(ofdm.MOD_QPSK)
    _WI = LADDER.index(ofdm.MOD_QPSK_WIDE)

    def _skip_narrow(self, p: _PeerTx, now: float) -> bool:
        if self._max_for(p) < self._WI:
            return False
        n = {self._QI: 0, self._WI: 0}
        t = {self._QI: 0, self._WI: 0}
        for (when, idx, troubled) in p.hist:
            if now - when <= MODE_HIST_S and idx in n:
                n[idx] += 1
                t[idx] += troubled
        nq, nw = n[self._QI], n[self._WI]
        if nq < SKIP_NARROW_MIN_REPORTS or nw < SKIP_NARROW_MIN_REPORTS or not t[self._QI]:
            return False
        return t[self._QI] / nq - t[self._WI] / nw >= SKIP_NARROW_GAP

    def _allowed(self, p: _PeerTx, idx: int) -> bool:
        """v0.9.24: can this station decode LADDER[idx]?"""
        m = LADDER[idx]
        if m in ofdm.CODED:
            return bool(p.caps & CAP_CODED)
        if m == ofdm.MOD_QAM64_WIDE:
            return bool(p.caps & CAP_QAM64)
        if m == ofdm.MOD_QAM16_WIDE:
            return bool(p.caps & CAP_QAM16)
        return True

    def _below(self, p: _PeerTx, idx: int) -> int:
        """The highest rung at or under idx this station can decode."""
        idx = max(0, min(idx, len(LADDER) - 1))
        while idx > 0 and not self._allowed(p, idx):
            idx -= 1
        return idx

    def _up_idx(self, p: _PeerTx, now: float) -> int:
        n = p.idx + 1
        if n == self._QI and self._skip_narrow(p, now) and now >= p.fail_until.get(self._WI, -1e9):
            n = self._WI
        top = self._max_for(p)
        while n < top and not self._allowed(p, n):
            n += 1
        return min(n, top) if n <= top else p.idx

    def _down_idx(self, p: _PeerTx, now: float) -> int:
        n = p.idx - 1
        if n == self._QI and self._skip_narrow(p, now):
            n = self._QI - 1
        return self._below(p, n)

    def _target(self, p: _PeerTx, now: float):
        """The mode a step up would go to, or None at the top."""
        if p.idx >= self._max_for(p):
            return None
        n = self._up_idx(p, now)
        return LADDER[n] if n > p.idx else None

    @_locked
    def new_connection(self, peer: str, now: float | None = None) -> int:
        """v0.9.1: an AX.25 connect to/from this peer -- start fresh at the
        start mode, forgetting the last session's slowdowns (VARA FM users
        reconnect when stuck at a low speed for exactly this). Returns the
        mode it starts at."""
        now = time.time() if now is None else now
        old = self.peers.get(peer)
        start = min(self.start_idx, self.max_idx)
        reason = "new connection: starting fresh"
        caps = 0
        # v0.9.10: a station that finished its last session cleanly, recently,
        # starts ONE step below where that session ended (still a fresh
        # reading: the first report decides). Field (v0.9.9): every session
        # climbed QPSK -> Wide -> 16QAM again, ~25-45 s of each send spent
        # below 16QAM; uploads used 16QAM for only 45 of 135 data frames.
        if old is not None:
            caps = old.caps
            if (old.last_report_clean and now - old.last_report <= RESUME_WINDOW_S
                    and old.last_spotless_idx == old.idx and old.idx > start):
                start = self._below(old, min(old.idx, self._max_for(old)))   # v0.9.17: ended spotless
                reason = f"new connection: last session ended spotless in {NAMES[LADDER[old.idx]]}"
            elif (old.last_report_clean and now - old.last_report <= RESUME_WINDOW_S
                    and old.idx - 1 > start):
                start = self._below(old, min(old.idx - 1, self._max_for(old)))
                reason = f"new connection: last session ended clean in {NAMES[LADDER[old.idx]]}"
        # v0.9.13: QPSK Wide has done better than narrow QPSK with this station
        if (old is not None and start == self._QI and self._skip_narrow(old, now)
                and old.fail_until.get(self._WI, -1e9) <= now):
            start = self._WI
            reason = "new connection: QPSK Wide has worked better than QPSK with this station"
        # v0.9.12: don't start in a mode that failed recently (see FAIL_HOLD_S)
        if old is not None:
            while start > 0 and old.fail_until.get(start, -1e9) > now:
                failed_name = NAMES[LADDER[start]]
                start -= 1
                if start == self._QI and self._skip_narrow(old, now):
                    start -= 1
                reason = f"new connection: {failed_name} failed recently"
        p = _PeerTx(idx=start, last_change=now, caps=caps)
        if old is not None:
            p.fail_until, p.fail_hold, p.hist = dict(old.fail_until), dict(old.fail_hold), list(old.hist)
        self.peers[peer] = p
        if old is not None and old.idx != start and self.on_change is not None:
            try:
                self.on_change(peer, LADDER[old.idx], LADDER[start], reason)
            except Exception:
                pass
        return LADDER[start]

    @_locked
    def mode_for(self, peer: str | None, now: float | None = None) -> int:
        now = time.time() if now is None else now
        if not peer:
            return LADDER[min(self.start_idx, self.max_idx)]
        p = self._peer(peer, now)
        p.last_tx = now
        if p.idx > self._max_for(p) or not self._allowed(p, p.idx):
            self._set(peer, p, self._below(p, min(p.idx, self._max_for(p))), now,
                      f"they can't decode {NAMES[LADDER[p.idx]]}")
        # older peer (never reports): creep back up after a long clean spell
        if (p.idx < min(self.start_idx, self.max_idx) and now - p.last_report > NO_REPORT_RECOVER_S
                and now - max(p.last_down, p.last_retx) > NO_REPORT_RECOVER_S and self._up_ok(p, now)):
            self._set(peer, p, self._up_idx(p, now), now, "no losses for a while")
        return LADDER[p.idx]

    def _max_for(self, p: _PeerTx) -> int:
        """v0.9.4: 16-QAM Wide only for a station that said it decodes it;
        v0.9.18: 64-QAM Wide likewise (CAP_QAM64); v0.9.24: coded modes
        (CAP_CODED). The highest rung it can decode, under the ceiling."""
        return self._below(p, self.max_idx)

    @staticmethod
    def _carrier_ok_up(tgt, r: Report) -> bool:
        """v0.9.18: stepping up to tgt needs clean data carriers, if tgt needs
        them. Only 16QAM Wide may be reached without a carrier figure (the
        reports of v0.9.4-v0.9.17 stations don't carry one)."""
        need = UP_CARRIER_TO.get(tgt)
        if need is None:
            return True
        if r.carrier_snr_db is None:
            return tgt == ofdm.MOD_QAM16_WIDE
        return r.carrier_snr_db >= need

    def _up_ok(self, p: _PeerTx, now: float) -> bool:
        """v0.9.12: the next mode up isn't on hold after a recent failure."""
        return now >= p.fail_until.get(self._up_idx(p, now), -1e9)

    def _set(self, peer, p, idx, now, reason, failed=False):
        idx = self._below(p, max(0, min(self._max_for(p), idx)))
        if idx == p.idx:
            return
        old = LADDER[p.idx]
        if failed:
            p.last_down = now
            if not p.mode_ok:
                # v0.9.12: hold the failed mode off (see FAIL_HOLD_S); a repeat
                # failure soon after the last hold ended doubles the hold
                m = p.idx
                prev, until = p.fail_hold.get(m, 0.0), p.fail_until.get(m, -1e9)
                hold = (min(FAIL_HOLD_MAX_S, prev * 2) if prev and now - until <= FAIL_HOLD_MAX_S
                        else FAIL_HOLD_S)
                p.fail_hold[m], p.fail_until[m] = hold, now + hold
                reason += f" -- {NAMES[old]} on hold {hold:.0f} s"
        if idx < p.idx:
            p.last_down = now
            if p.probed and now - p.last_up <= PROBE_FAIL_S:
                p.probe_wait = min(PROBE_MAX_S, p.probe_wait * 2)   # that probe failed at once
            p.probed = False
        else:
            p.last_up = now
        p.idx, p.clean, p.spotless = idx, 0, 0
        p.mode_ok = False
        p.last_change = now
        if self.on_change is not None:
            try:
                self.on_change(peer, old, LADDER[idx], reason)
            except Exception:
                pass

    @_locked
    def on_report(self, peer: str, r: Report, now: float | None = None):
        now = time.time() if now is None else now
        p = self._peer(peer, now)
        p.last_report = now
        p.last_snr = r.snr_db
        p.caps = r.caps
        p.last_carrier, p.last_fec = r.carrier_snr_db, r.fec_max_pct
        if r.mode in LADDER:        # v0.9.13: every report is evidence about its mode
            p.hist.append((now, LADDER.index(r.mode), r.n_bad > 0 or r.fec_max_pct >= TROUBLE_FEC_PCT))
            p.hist = [h for h in p.hist if now - h[0] <= MODE_HIST_S][-200:]
        cur = LADDER[p.idx]
        if r.mode != cur:
            return                      # about frames sent in another mode: not evidence for this one
        bad = r.n_bad > 0
        heavy = r.fec_max_pct >= DOWN_FEC_PCT
        low = ((cur in DOWN_SNR_DB and r.snr_db < DOWN_SNR_DB[cur])
               or (cur in DOWN_CARRIER_DB and r.carrier_snr_db is not None
                   and r.carrier_snr_db < DOWN_CARRIER_DB[cur]))
        p.last_report_clean = not (bad or heavy or low)
        strong = r.snr_db >= STRONG_SNR_DB
        if bad or heavy or low:
            floor = LADDER.index(ofdm.MOD_QPSK) if strong and not low else 0
            if (not low and not heavy and r.carrier_snr_db is not None
                    and r.carrier_snr_db >= CLEAN_CARRIER_DB and r.fec_max_pct <= CLEAN_CARRIER_FEC_PCT):
                floor = max(floor, min(self._WI, self._max_for(p)))   # v0.9.20: interference
            target = self._down_idx(p, now)
            sideways = (p.idx == self._QI and not low and self._skip_narrow(p, now)
                        and now >= p.fail_until.get(self._WI, -1e9))
            if sideways:
                target = self._WI   # v0.9.13: Wide has done better than QPSK with them
            if now - p.last_down >= DOWN_HOLDOFF_S and (sideways or (p.idx > floor and target >= floor)):
                why = (f"{r.n_bad} lost" if bad else f"FEC {r.fec_max_pct}% used" if heavy
                       else f"S/N {r.snr_db} dB")
                if sideways:
                    why += "; QPSK Wide has done better with them"
                self._set(peer, p, target, now, f"they report {why}", failed=not strong or sideways)
            elif self._probe_due(p, cur, r, now):
                self._probe(peer, p, r, now)
            p.clean = 0
            return
        if p.probed:
            p.probe_wait = PROBE_AFTER_S        # the faster mode is working
        p.fail_hold.pop(p.idx, None)            # v0.9.12: and this mode works
        p.mode_ok = True
        tgt = self._target(p, now)
        ok_up = (tgt in UP_SNR_TO and r.snr_db >= UP_SNR_TO[tgt] and r.fec_max_pct <= UP_FEC_MAX_PCT
                 and self._carrier_ok_up(tgt, r))
        p.clean = p.clean + 1 if ok_up else 0
        # v0.9.10: one clean report is enough with a clear margin (see FAST_UP_*)
        needed = (1 if ok_up and r.snr_db >= UP_SNR_TO[tgt] + FAST_UP_MARGIN_DB
                  and r.fec_max_pct <= FAST_UP_FEC_PCT else UP_CLEAN_REPORTS)
        spotless = (tgt in CLEAN_PROBE_TO and r.snr_db >= CLEAN_PROBE_TO[tgt]
                    and r.fec_max_pct <= CLEAN_PROBE_FEC_PCT and self._carrier_ok_up(tgt, r))
        p.spotless = p.spotless + 1 if spotless else 0
        p.last_spotless_idx = p.idx if (r.fec_max_pct <= CLEAN_PROBE_FEC_PCT) else -1
        if (ok_up and p.clean >= needed and p.idx < self._max_for(p)
                and now - p.last_down >= UP_HOLDOFF_S and self._up_ok(p, now)):
            p.probed = False
            self._set(peer, p, self._up_idx(p, now), now, f"they report S/N {r.snr_db} dB, FEC {r.fec_max_pct}%, 0 lost")
        elif self._probe_due(p, cur, r, now):
            self._probe(peer, p, r, now)
        elif (p.spotless >= CLEAN_PROBE_REPORTS and p.idx < self._max_for(p)
              and now - p.last_change >= CLEAN_PROBE_AFTER_S * p.probe_wait / PROBE_AFTER_S
              and now - p.last_down >= UP_HOLDOFF_S
              and self._up_ok(p, now)):
            self._set(peer, p, self._up_idx(p, now), now,
                      f"trying faster: {NAMES[cur]} spotless (FEC {r.fec_max_pct}%, 0 lost) "
                      f"at S/N {r.snr_db} dB")
            p.probed = True

    def _probe_due(self, p: _PeerTx, cur: int, r: Report, now: float) -> bool:
        """v0.9.1: stuck in a slower mode for a while although the signal is
        strong enough for the next one up -- worth trying faster again."""
        tgt = self._target(p, now)
        return (tgt in UP_SNR_TO and r.snr_db >= UP_SNR_TO[tgt]
                and self._carrier_ok_up(tgt, r)
                and now - p.last_change >= p.probe_wait and now - p.last_down >= UP_HOLDOFF_S
                and self._up_ok(p, now))

    def _probe(self, peer, p: _PeerTx, r: Report, now: float):
        self._set(peer, p, self._up_idx(p, now), now,
                  f"trying faster again: S/N {r.snr_db} dB is strong enough")
        p.probed = True

    @_locked
    def on_retransmit(self, peer: str, now: float | None = None):
        now = time.time() if now is None else now
        p = self._peer(peer, now)
        p.last_retx = now
        if p.last_report_clean and p.last_report > p.last_tx:
            # A clean report arrived AFTER our last transmission: they heard
            # that window fine, so it was their acknowledgment that got lost.
            # (v0.9.0 simulation: trusting any recent clean report -- one from
            # BEFORE the failed window -- delayed the step down by 3 windows.)
            return
        # v0.9.8: their latest report was clean AND strong -> frames are
        # arriving fine as far as they can tell; a resend then means a frame
        # never reached their decoder at all (or an acknowledgment got lost),
        # which a slower mode doesn't fix. Field (v0.9.7, K6IRF -> DW1IRF):
        # Auto stepped 16QAM -> QPSK Wide 30 ms after stepping UP, on a frame
        # resent from the previous window, then on to QPSK -- each slower
        # window took longer and the sends ran 2:10-2:38 instead of ~0:52.
        if (p.last_report_clean and p.last_snr is not None and p.last_snr >= STRONG_SNR_DB
                and now - p.last_report <= PROBE_MAX_S):
            return
        # v0.9.1: their latest report said the signal is strong -- not a
        # noise problem, so not a reason for BPSK (see STRONG_SNR_DB)
        strong = (p.last_snr is not None and p.last_snr >= STRONG_SNR_DB
                  and now - p.last_report <= PROBE_MAX_S)
        floor = LADDER.index(ofdm.MOD_QPSK) if strong else 0
        if (p.last_carrier is not None and p.last_carrier >= CLEAN_CARRIER_DB
                and p.last_fec <= CLEAN_CARRIER_FEC_PCT and now - p.last_report <= PROBE_MAX_S):
            floor = max(floor, min(self._WI, self._max_for(p)))       # v0.9.20: interference
        target = self._down_idx(p, now)
        if now - p.last_down >= DOWN_HOLDOFF_S and p.idx > floor and target >= floor:
            self._set(peer, p, target, now, "they missed a frame (BPQ32 resent it)", failed=not strong)


class RetransmitDetector:
    """Spots BPQ32 sending the same AX.25 I-frame (same N(S) and contents)
    again within WINDOW_S."""
    WINDOW_S = 90.0

    def __init__(self):
        self._seen: dict = {}
        self._lock = threading.Lock()

    @_locked
    def check(self, frame: bytes, now: float | None = None):
        """Returns the destination callsign if this is a retransmitted
        I-frame, else None."""
        now = time.time() if now is None else now
        p = ax25_parse(frame)
        if p is None or not is_iframe(p[2]):
            return None
        dest, src, control, info = p
        key = (dest, src, (control >> 1) & 0x07, bytes(info))
        for k in [k for k, t in self._seen.items() if now - t > self.WINDOW_S]:
            del self._seen[k]
        seen = key in self._seen
        self._seen[key] = now
        return dest if seen else None
