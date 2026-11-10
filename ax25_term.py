# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 PAREDN (Philippine Amateur Radio Emergency Data Network) / DW1IRF
"""AX.25 connected-mode terminal session (v0.9.90).

A small AX.25 v2.0 link layer (modulo 8) for the SARA Chat terminal, so an
operator can connect to a BPQ32 node -- e.g. DX1PRD-12 -- and use its Chat
Server, the way EasyTerm does over the UZ7HO soundmodem. On the air the
frames travel in SARA FM's OFDM modes like any Winlink session's frames.

This is the ONLY place SARA FM makes AX.25 frames of its own, and only for
the terminal's own connection under the terminal's own callsign (e.g.
DW1IRF-7). Frames of other sessions -- BPQ32's, Winlink Express's -- are
never created, changed or answered here.

The class is pure logic: frames go out through `send(list_of_frames)`,
received frames come in through `receive(frame)`, and `tick(now)` runs the
timers. Text arrives through `on_text`, state changes through `on_status`.
"""
from __future__ import annotations

import time

# ── frame encoding ──────────────────────────────────────────────────
PID_NONE = 0xF0
RR, RNR, REJ = 0x01, 0x05, 0x09
SABM, DISC, UA, DM, FRMR, UI = 0x2F, 0x43, 0x63, 0x0F, 0x87, 0x03
PF = 0x10


def norm(call: str) -> str:
    c = (call or "").strip().upper()
    if c.endswith("-0"):
        c = c[:-2]
    return c


def split_call(call: str):
    c = norm(call)
    if "-" in c:
        base, ssid = c.split("-", 1)
        return base, int(ssid)
    return c, 0


def valid_call(call: str) -> bool:
    try:
        base, ssid = split_call(call)
    except ValueError:
        return False
    return 1 <= len(base) <= 6 and base.isalnum() and 0 <= ssid <= 15


def enc_addr(call: str, c_bit: bool, last: bool) -> bytes:
    base, ssid = split_call(call)
    b = bytes((ord(ch) << 1) for ch in base.ljust(6)[:6])
    return b + bytes([0x60 | (ssid << 1) | (0x80 if c_bit else 0) | (0x01 if last else 0)])


def dec_addr(b: bytes):
    base = "".join(chr(x >> 1) for x in b[:6]).strip()
    ssid = (b[6] >> 1) & 0x0F
    return (f"{base}-{ssid}" if ssid else base), bool(b[6] & 0x80), bool(b[6] & 0x01)


def build(dest: str, src: str, control: int, command: bool, info: bytes | None = None,
          digis=()) -> bytes:
    """One AX.25 frame. command=True sets the v2 command bits (dest C=1)."""
    digis = list(digis)
    out = enc_addr(dest, command, False) + enc_addr(src, not command, not digis)
    for i, d in enumerate(digis):
        out += enc_addr(d, False, i == len(digis) - 1)
    out += bytes([control])
    if info is not None:
        out += bytes([PID_NONE]) + info
    return out


def parse(frame: bytes):
    """-> dict(dest, src, digis, command, control, info) or None."""
    if len(frame) < 15:
        return None
    dest, dc, last = dec_addr(frame[0:7])
    src, sc, last = dec_addr(frame[7:14])
    pos, digis = 14, []
    while not last:
        if len(frame) < pos + 8:
            return None
        d, _h, last = dec_addr(frame[pos:pos + 7])
        digis.append(d)
        pos += 7
    if len(frame) <= pos:
        return None
    control = frame[pos]
    info = b""
    if control & 0x01 == 0 or (control & 0xEF) == UI:     # I or UI: PID then info
        info = frame[pos + 2:]
    return {"dest": norm(dest), "src": norm(src), "digis": digis, "command": dc and not sc,
            "control": control, "info": info}


# ── the session ────────────────────────────────────────────────────
DISCONNECTED, CONNECTING, CONNECTED, DISCONNECTING = "disconnected", "connecting", "connected", "disconnecting"


class Session:
    # SARA FM frames take seconds on the air, and the other station waits for
    # a clear channel before it answers: generous timers, like FRACK=12000 on
    # the gateways' BPQ32 ports.
    T1 = 12.0          # no answer -> resend / poll
    T2 = 1.0           # wait this long to acknowledge, in case more frames come
    T3 = 300.0         # quiet link -> check it's still there
    N2 = 6             # tries before giving up
    K = 4              # frames outstanding
    PACLEN = 200

    def __init__(self, my_call: str, send, on_text=None, on_status=None, clock=time.monotonic):
        self.my_call = norm(my_call)
        self.send = send
        self.on_text = on_text or (lambda s: None)
        self.on_status = on_status or (lambda s: None)
        self.clock = clock
        self.state = DISCONNECTED
        self.peer = ""
        self.digis = []
        self._reset()

    def _reset(self):
        self.vs = self.vr = self.va = 0
        self.unacked = {}              # ns -> info
        self.txbuf = b""
        self.t1 = None
        self.t2 = None
        self.t3 = None
        self.retries = 0
        self.rej_sent = False
        self.polling = False
        self.peer_busy = False

    # ── helpers ──
    def _now(self):
        return self.clock()

    def _out(self, control: int, command: bool, info: bytes | None = None):
        self.send([build(self.peer, self.my_call, control, command, info, self.digis)])

    def _start_t1(self):
        self.t1 = self._now() + self.T1

    def _set_status(self, s: str, msg: str):
        self.state = s
        self.on_status(msg)

    def wants(self, frame: bytes) -> bool:
        """Is this received frame for the terminal's callsign?"""
        p = parse(frame)
        return bool(p) and p["dest"] == self.my_call and not self._in_transit(frame)

    @staticmethod
    def _in_transit(frame: bytes) -> bool:
        """Still on its way through a digipeater (some H bit not set)?"""
        pos, n = 14, 0
        if len(frame) < 14 or frame[13] & 0x01:
            return False
        while pos + 7 <= len(frame):
            h = frame[pos + 6] & 0x80
            last = frame[pos + 6] & 0x01
            if not h:
                return True
            n += 1
            pos += 7
            if last:
                break
        return False

    # ── operator actions ──
    def connect(self, peer: str, digis=()):
        if self.state != DISCONNECTED:
            return
        self.peer = norm(peer)
        self.digis = [norm(d) for d in digis]
        self._reset()
        self.retries = 0
        self._set_status(CONNECTING, f"*** Connecting to {self.peer}" +
                         (f" via {', '.join(self.digis)}" if self.digis else "") + " ...")
        self._out(SABM | PF, True)
        self._start_t1()

    def disconnect(self):
        if self.state in (CONNECTED, CONNECTING):
            self.retries = 0
            self.txbuf = b""
            self._set_status(DISCONNECTING, f"*** Disconnecting from {self.peer} ...")
            self._out(DISC | PF, True)
            self._start_t1()
        elif self.state == DISCONNECTING:
            self._finish(f"*** Disconnected from {self.peer}")

    def send_text(self, text: str):
        """A line typed by the operator (CR is the end-of-line on AX.25 nodes)."""
        if self.state != CONNECTED:
            return False
        data = text.replace("\r\n", "\r").replace("\n", "\r")
        if not data.endswith("\r"):
            data += "\r"
        self.txbuf += data.encode("utf-8", "replace")
        self._pump()
        return True

    def _finish(self, msg: str):
        self._reset()
        self._set_status(DISCONNECTED, msg)

    # ── sending I frames ──
    def _outstanding(self):
        return (self.vs - self.va) % 8

    def _pump(self):
        if self.state != CONNECTED or self.peer_busy or self.polling:
            return
        frames = []
        while self.txbuf and self._outstanding() < self.K:
            chunk, self.txbuf = self.txbuf[:self.PACLEN], self.txbuf[self.PACLEN:]
            self.unacked[self.vs] = chunk
            frames.append((self.vs, chunk))
            self.vs = (self.vs + 1) % 8
        if not frames:
            return
        out = []
        for i, (ns, chunk) in enumerate(frames):
            p = PF if i == len(frames) - 1 else 0          # last one asks for a quick answer
            out.append(build(self.peer, self.my_call, (self.vr << 5) | p | (ns << 1), True, chunk,
                             self.digis))
        self.t2 = None                                      # N(R) rides on the I frames
        self.send(out)
        self._start_t1()

    def _resend_unacked(self):
        ns_list = [(self.va + i) % 8 for i in range(self._outstanding())]
        out = []
        for i, ns in enumerate(ns_list):
            p = PF if i == len(ns_list) - 1 else 0
            out.append(build(self.peer, self.my_call, (self.vr << 5) | p | (ns << 1), True,
                             self.unacked[ns], self.digis))
        if out:
            self.t2 = None
            self.send(out)
        self._start_t1()

    def _ack_upto(self, nr: int) -> bool:
        """Frames up to N(R) are acknowledged. False if N(R) is out of range."""
        if (nr - self.va) % 8 > self._outstanding():
            return False
        while self.va != nr:
            self.unacked.pop(self.va, None)
            self.va = (self.va + 1) % 8
        if self._outstanding() == 0:
            self.t1 = None
            self.retries = 0
        else:
            self._start_t1()
        return True

    # ── receiving ──
    def receive(self, frame: bytes):
        p = parse(frame)
        if not p or p["dest"] != self.my_call:
            return
        c, cmd = p["control"], p["command"]
        pf = bool(c & PF)
        from_peer = p["src"] == self.peer
        base = c & ~PF & 0xFF

        if self.state == DISCONNECTED or not from_peer:
            # not in a session with this station: refuse connects, answer polls
            if cmd and (base == SABM or pf):
                self.send([build(p["src"], self.my_call, DM | (PF if pf else 0), False,
                                 None, list(reversed(p["digis"])))])
                if base == SABM:
                    self.on_status(f"*** {p['src']} tried to connect to the terminal -- refused "
                                   f"(it only makes outgoing connections)")
            return

        if self.state == CONNECTING:
            if base == UA:
                self._reset()
                self._set_status(CONNECTED, f"*** Connected to {self.peer}")
                self.t3 = self._now() + self.T3
            elif base == DM:
                self._finish(f"*** {self.peer} is busy or refused the connection")
            return

        if self.state == DISCONNECTING:
            if base in (UA, DM):
                self._finish(f"*** Disconnected from {self.peer}")
            elif base == DISC:
                self._out(UA | (PF if pf else 0), False)
                self._finish(f"*** Disconnected from {self.peer}")
            return

        # CONNECTED
        self.t3 = self._now() + self.T3
        if base == DISC:
            self._out(UA | (PF if pf else 0), False)
            self._finish(f"*** Disconnected by {self.peer}")
            return
        if base == DM:
            self._finish(f"*** {self.peer} ended the connection")
            return
        if base == SABM:                     # the node restarted the link
            self._out(UA | (PF if pf else 0), False)
            self._reset()
            self._set_status(CONNECTED, f"*** {self.peer} reset the link -- still connected")
            self.t3 = self._now() + self.T3
            return
        if base == FRMR:
            self._set_status(CONNECTING, f"*** {self.peer} rejected a frame -- reconnecting")
            self._reset()
            self.retries = 0
            self._out(SABM | PF, True)
            self._start_t1()
            return
        if base == UA:
            return

        if c & 0x01 == 0:                    # I frame
            ns, nr = (c >> 1) & 7, (c >> 5) & 7
            self._ack_upto(nr)
            if ns == self.vr:
                self.vr = (self.vr + 1) % 8
                self.rej_sent = False
                if p["info"]:
                    self.on_text(p["info"].decode("utf-8", "replace").replace("\r\n", "\n")
                                 .replace("\r", "\n"))
                if pf:
                    self._out((self.vr << 5) | PF | RR, False)
                    self.t2 = None
                elif self.t2 is None:
                    self.t2 = self._now() + self.T2
            else:
                if not self.rej_sent:
                    self.rej_sent = True
                    self._out((self.vr << 5) | (PF if pf else 0) | REJ, False)
                    self.t2 = None
                elif pf:
                    self._out((self.vr << 5) | PF | RR, False)
            self._pump()
            return

        if c & 0x03 == 0x01:                 # S frame
            stype, nr = c & 0x0F, (c >> 5) & 7
            self.peer_busy = stype == RNR
            ok = self._ack_upto(nr)
            if cmd and pf:                   # they poll us
                self._out((self.vr << 5) | PF | RR, False)
                self.t2 = None
            if not cmd and pf and self.polling:
                self.polling = False
                self.retries = 0
                if ok and self._outstanding():
                    self._resend_unacked()
            if stype == REJ and ok and self._outstanding():
                self.vs = self.va
                pending = [self.unacked[(self.va + i) % 8] for i in range(len(self.unacked))
                           if (self.va + i) % 8 in self.unacked]
                self.unacked = {}
                self.txbuf = b"".join(pending) + self.txbuf
            self._pump()

    # ── timers ──
    def tick(self, now: float | None = None):
        now = self._now() if now is None else now
        if self.t2 is not None and now >= self.t2 and self.state == CONNECTED:
            self.t2 = None
            self._out((self.vr << 5) | RR, False)
        if self.t1 is not None and now >= self.t1:
            self.t1 = None
            self.retries += 1
            if self.state == CONNECTING:
                if self.retries >= self.N2:
                    self._finish(f"*** No answer from {self.peer}")
                else:
                    self.on_status(f"*** No answer yet from {self.peer} -- trying again "
                                   f"({self.retries + 1} of {self.N2})")
                    self._out(SABM | PF, True)
                    self._start_t1()
            elif self.state == DISCONNECTING:
                if self.retries >= 3:
                    self._finish(f"*** Disconnected from {self.peer} (no answer to the disconnect)")
                else:
                    self._out(DISC | PF, True)
                    self._start_t1()
            elif self.state == CONNECTED:
                if self.retries >= self.N2:
                    self._finish(f"*** Link to {self.peer} lost (no answer after {self.N2} tries)")
                    return
                # ask where they are; resend from what they report
                self.polling = True
                self._out((self.vr << 5) | PF | RR, True)
                self._start_t1()
        if self.t3 is not None and now >= self.t3 and self.state == CONNECTED \
                and self.t1 is None and not self.polling:
            self.t3 = now + self.T3
            self.polling = True
            self.retries = 0
            self._out((self.vr << 5) | PF | RR, True)
            self._start_t1()
