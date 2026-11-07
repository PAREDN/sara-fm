# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 PAREDN (Philippine Amateur Radio Emergency Data Network) / DW1IRF
"""
chat.py -- SARA Chat (v0.9.38): keyboard-to-keyboard chat between SARA FM
stations, built into SARA FM so it shares the radio with a running BPQ32
gateway (chat frames are never passed to BPQ32).

What it does, beyond VARA Chat:
  - one-to-one chats with delivery receipts: every message shows sending /
    sent / delivered / held / failed, and is resent automatically;
  - a NET room everyone on frequency sees, with CQ and check-ins;
  - leave a message: a message that can't be delivered now is held and sent
    the next time that station is heard (on chat, a ping or Winlink traffic);
  - small file / picture transfer inside a chat;
  - a heard list (callsign, last heard, S/N, how).

This module is pure logic (no Tk, no audio): ChatEngine gets a transmit
callback and the clock, so it is tested without a radio.

Wire format (one OFDM frame each; all integers big-endian):
  magic 0xC5 0x43 | version | type | left | len+from | len+to | body
  'left' = how many more chat frames follow in this burst, so the receiver
  acknowledges as soon as the last one arrives.
  0xC5 can't start an AX.25 frame (address bytes are ASCII << 1, always
  even), a SARA batch (0x01) or a ping/pong (1 or 2).
"""

from __future__ import annotations

import json
import os
import struct
import time
import zlib

MAGIC = b"\xC5\x43"
VERSION = 1

T_MSG = 1        # reliable (direct) or one-shot (NET) text
T_ACK = 2
T_OFFER = 3      # file: name, size, chunks, crc
T_CHUNK = 4      # file: one piece
T_CHUNKS = 5     # v0.9.40: several file pieces in one frame (fast modes only)

# v0.9.40: our receipts end with this, so the other station knows we take
# T_CHUNKS frames (0.9.38/39 receipts don't, and get one piece per frame)
FEATURES = b"\xC5\x56" + bytes([2])

F_ZLIB = 0x01
F_NET = 0x02
F_CQ = 0x04
F_CHECKIN = 0x08
F_HELD = 0x10    # was held for a while before delivery

NET = "*NET"            # the NET room's "callsign"
NET_LABEL = "NET (everyone)"

MAX_TEXT_BYTES = 240    # per frame after compression (whole frame stays < 300 B)
CHUNK_BYTES = 280       # v0.9.40: one piece fits every mode with margin (0.9.39: 300)
MAX_FILE_BYTES = 32 * 1024
WINDOW = 4              # reliable frames per burst (burst_plan may allow more)
# v0.9.40: the biggest frame (bytes, header included) the receiver decodes
# reliably with 200 ppm sound-card clock drift, measured per mode family
FRAME_CAP_NARROW = 320  # BPSK, QPSK, QPSK Wide (fail from ~350)
FRAME_CAP_FAST = 680    # 16QAM / 64QAM Wide (fail from ~760)
RTO_S = 12.0            # no acknowledgment this long after a burst -> resend
ACK_IDLE_S = 3.5        # acknowledge this long after the last frame, if 'left' never hit 0
MAX_ATTEMPTS = 4        # then: messages are held...
MAX_FILE_ATTEMPTS = 8   # ...and a file fails (each piece gets more tries: a file has many)
HELD_RETRY_GAP_S = 60.0
HELD_MAX_AGE_S = 7 * 24 * 3600
DEDUPE_S = 3600.0
HISTORY_KEEP = 500


def is_chat(payload: bytes) -> bool:
    return len(payload) >= 8 and payload[:2] == MAGIC and payload[2] == VERSION


def _s(b: str) -> bytes:
    e = b.encode("utf-8")[:255]
    return bytes([len(e)]) + e


def _rs(buf: bytes, pos: int):
    n = buf[pos]
    return buf[pos + 1:pos + 1 + n].decode("utf-8", "replace"), pos + 1 + n


def norm(call: str) -> str:
    c = (call or "").strip().upper()
    return c[:-2] if c.endswith("-0") else c


def pack(ftype: int, frm: str, to: str, body: bytes, left: int = 0) -> bytes:
    return MAGIC + bytes([VERSION, ftype, max(0, min(255, left))]) + _s(frm) + _s(to) + body


def unpack(payload: bytes):
    """dict with type, left, frm, to and the body fields; None if malformed."""
    try:
        if not is_chat(payload):
            return None
        ftype, left = payload[3], payload[4]
        frm, pos = _rs(payload, 5)
        to, pos = _rs(payload, pos)
        b = payload[pos:]
        d = {"type": ftype, "left": left, "frm": norm(frm), "to": norm(to)}
        if ftype == T_MSG:
            mid, flags, ts = struct.unpack(">HBI", b[:7])
            raw = b[7:]
            text = (zlib.decompress(raw) if flags & F_ZLIB else raw).decode("utf-8", "replace")
            d.update(id=mid, flags=flags, ts=ts, text=text)
        elif ftype == T_ACK:
            n, snr = b[0], struct.unpack(">b", b[1:2])[0]
            # v0.9.39: a link report (how we hear the sender) may follow the ids;
            # 0.9.38 reads only the ids and ignores it
            d.update(ids=list(struct.unpack(f">{n}H", b[2:2 + 2 * n])), snr=snr, extra=bytes(b[2 + 2 * n:]))
        elif ftype == T_OFFER:
            mid, fid, size, nch, crc = struct.unpack(">HHIHI", b[:14])
            name, _ = _rs(b, 14)
            d.update(id=mid, fid=fid, size=size, n=nch, crc=crc, name=name)
        elif ftype == T_CHUNK:
            mid, fid, idx = struct.unpack(">HHH", b[:6])
            d.update(id=mid, fid=fid, idx=idx, data=bytes(b[6:]))
        elif ftype == T_CHUNKS:
            n, pos, pieces = b[0], 1, []
            for _ in range(n):
                mid, fid, idx, ln = struct.unpack(">HHHH", b[pos:pos + 8])
                pieces.append((mid, fid, idx, bytes(b[pos + 8:pos + 8 + ln])))
                pos += 8 + ln
            if pos != len(b):
                return None
            d.update(pieces=pieces)
        else:
            return None
        return d
    except Exception:
        return None


def msg_body(mid: int, flags: int, ts: int, text: str) -> bytes:
    raw = text.encode("utf-8")
    z = zlib.compress(raw, 9)
    if len(z) < len(raw):
        raw, flags = z, flags | F_ZLIB
    return struct.pack(">HBI", mid & 0xFFFF, flags, int(ts) & 0xFFFFFFFF) + raw


def split_text(text: str, limit: int = MAX_TEXT_BYTES) -> list:
    """Long text -> pieces that each fit one frame (compression not assumed)."""
    out, cur = [], ""
    for ch in text:
        if len((cur + ch).encode("utf-8")) > limit:
            out.append(cur)
            cur = ""
        cur += ch
    if cur or not out:
        out.append(cur)
    return out


class _Unit:
    """One reliable frame waiting for its acknowledgment."""
    __slots__ = ("peer", "id", "kind", "build", "entry", "attempts", "sent_at", "in_flight", "fid",
                 "queued_at", "piece")

    def __init__(self, peer, mid, kind, build, entry, fid=None):
        self.peer, self.id, self.kind, self.build, self.entry = peer, mid, kind, build, entry
        self.attempts, self.sent_at, self.in_flight, self.fid = 0, None, False, fid
        self.queued_at = None
        self.piece = None                # (fid, idx, data) for file pieces


class ChatEngine:
    """
    transmit(payload, peer, attempt, on_sent) -> bool   puts one frame on the
        air queue (peer None = NET); returns False if it can't right now.
    can_send() -> bool   False while a Winlink session is moving data (chat
        waits, like pings do) or the modem is stopped.
    on_event(kind, data)  'message', 'status', 'heard', 'file' -- for the GUI.
    """

    def __init__(self, my_call_fn, transmit, can_send=lambda: True, on_event=None,
                 store_path=None, files_dir=None, now_fn=time.time):
        self.my_call_fn = my_call_fn
        self.transmit = transmit
        self.can_send = can_send
        self.on_event = on_event or (lambda kind, data: None)
        # v0.9.39 Auto speed for chat (set by SARA FM): report_fn(peer) -> bytes
        # to ride on our receipts; on_report(peer, bytes) for theirs;
        # on_resend(peer) when a window has to be resent
        self.report_fn = None
        self.on_report = None
        self.on_resend = None
        # v0.9.40: burst_plan(peer) -> (frame cap bytes, frames per burst) for
        # the speed in use; multi-piece frames only to stations that said so
        self.burst_plan = None
        self.multi_ok: set = set()
        self.store_path = store_path
        self.files_dir = files_dir
        self.now = now_fn
        self.convs: dict = {}            # peer (or NET) -> [entry]
        self.unread: dict = {}
        self.heard: dict = {}            # call -> {last, snr, mode, how, checkin}
        self.queue: list = []            # _Unit, in order
        self.held: list = []             # entries (direct messages) waiting for the station
        self.next_id = 1
        self._rx_seen: dict = {}         # (peer, id) -> time
        self._ack_due: dict = {}         # peer -> {"ids": set, "last": t, "snr": int, "now": bool}
        self._files_in: dict = {}        # (peer, fid) -> {...}
        self._dirty = False
        self._last_save = 0.0
        self.load()

    # ── helpers ───────────────────────────────────────────────────────
    def me(self) -> str:
        return norm(self.my_call_fn())

    def _new_id(self) -> int:
        i = self.next_id
        self.next_id = (self.next_id % 0xFFFF) + 1
        return i

    def _add(self, peer: str, entry: dict, unread: bool = False) -> dict:
        lst = self.convs.setdefault(peer, [])
        pos = len(lst)
        if entry.get("dir") == "in" and entry.get("kind") == "msg" and "id" in entry:
            # a resent message can arrive after a later one: keep the sender's order
            while pos > 0:
                prev = lst[pos - 1]
                if (prev.get("dir") == "in" and prev.get("frm") == entry["frm"] and prev.get("kind") == "msg"
                        and "id" in prev and 0 < prev["id"] - entry["id"] < 1000):
                    pos -= 1
                else:
                    break
        lst.insert(pos, entry)
        if len(lst) > HISTORY_KEEP:
            del lst[:len(lst) - HISTORY_KEEP]
        if unread:
            self.unread[peer] = self.unread.get(peer, 0) + 1
        self._dirty = True
        self.on_event("message", {"peer": peer, "entry": entry})
        return entry

    def _status(self, entry: dict, status: str, **extra):
        entry["status"] = status
        entry.update(extra)
        self._dirty = True
        self.on_event("status", {"entry": entry})

    def system(self, peer: str, text: str):
        return self._add(peer, {"dir": "sys", "text": text, "ts": self.now()})

    # ── sending ───────────────────────────────────────────────────────
    def send_text(self, to: str, text: str) -> list:
        """A direct message (reliable). Long text goes as several messages."""
        to = norm(to)
        text = text.strip()
        if not text or not to or to == NET:
            return []
        entries = []
        for part in split_text(text):
            mid = self._new_id()
            e = self._add(to, {"dir": "out", "frm": self.me(), "to": to, "text": part,
                               "ts": self.now(), "id": mid, "status": "queued", "kind": "msg"})
            ts = int(e["ts"])
            self.queue.append(_Unit(to, mid, "msg",
                                    lambda left, m=mid, t=part, s=ts, ee=e: pack(
                                        T_MSG, self.me(), to,
                                        msg_body(m, F_HELD if ee.get("was_held") else 0, s, t), left), e))
            entries.append(e)
        self.pump()
        return entries

    def send_net(self, text: str, kind: str = "msg") -> dict | None:
        """NET room: one frame, everyone on frequency, no acknowledgment."""
        flags = F_NET | {"cq": F_CQ, "checkin": F_CHECKIN}.get(kind, 0)
        me = self.me()
        if kind == "cq" and not text:
            text = f"CQ CQ CQ de {me} -- SARA Chat, anyone around?"
        elif kind == "checkin" and not text:
            text = f"{me} checking in"
        text = text.strip()
        if not text:
            return None
        text = split_text(text)[0]
        e = self._add(NET, {"dir": "out", "frm": me, "to": NET, "text": text, "ts": self.now(),
                            "status": "queued", "kind": kind})
        payload = pack(T_MSG, me, NET, msg_body(0, flags, int(e["ts"]), text))
        if self.can_send() and self.transmit(payload, None, 1, lambda: self._status(e, "sent")):
            self._status(e, "sending")
        else:
            self._status(e, "failed", note="modem stopped or Winlink session busy -- try again")
        return e

    def send_file(self, to: str, name: str, data: bytes) -> dict:
        to = norm(to)
        if len(data) > MAX_FILE_BYTES:
            raise ValueError(f"file is {len(data) // 1024} kB; SARA Chat sends up to "
                             f"{MAX_FILE_BYTES // 1024} kB (shrink the picture first)")
        if not to or to == NET:
            raise ValueError("files go to one station, not the NET")
        fid = self._new_id()
        chunks = [data[i:i + CHUNK_BYTES] for i in range(0, len(data), CHUNK_BYTES)] or [b""]
        crc = zlib.crc32(data) & 0xFFFFFFFF
        name = os.path.basename(name)[:80] or "file"
        e = self._add(to, {"dir": "out", "frm": self.me(), "to": to, "kind": "file", "name": name,
                           "size": len(data), "n": len(chunks), "done": 0, "ts": self.now(),
                           "status": "queued", "fid": fid})
        mid = self._new_id()
        self.queue.append(_Unit(to, mid, "offer", lambda left, m=mid: pack(
            T_OFFER, self.me(), to, struct.pack(">HHIHI", m, fid, len(data), len(chunks), crc) + _s(name), left),
            e, fid))
        for i, c in enumerate(chunks):
            cid = self._new_id()
            u = _Unit(to, cid, "chunk", lambda left, m=cid, k=i, cc=c: pack(
                T_CHUNK, self.me(), to, struct.pack(">HHH", m, fid, k) + cc, left), e, fid)
            u.piece = (fid, i, c)
            self.queue.append(u)
        self.pump()
        return e

    def pump(self):
        """Send the next window for each station that has nothing in flight."""
        if not self.queue or not self.can_send():
            return
        peers = []
        for u in self.queue:
            if u.peer not in peers:
                peers.append(u.peer)
        for peer in peers:
            units = [u for u in self.queue if u.peer == peer]
            if any(u.in_flight for u in units):
                continue
            frames = self._plan_frames(peer, units)
            for i, group in enumerate(frames):
                for u in group:
                    u.attempts += 1
                    u.in_flight = True
                    u.sent_at = None
                    u.queued_at = self.now()
                left = len(frames) - 1 - i
                payload = group[0].build(left) if len(group) == 1 else self._multi_payload(peer, group, left)
                ok = self.transmit(payload, peer, max(u.attempts for u in group),
                                   lambda gg=group: [self._unit_sent(uu) for uu in gg])
                if not ok:
                    for u in group:
                        u.in_flight = False
                        u.attempts -= 1
                    return
                for u in group:
                    if u.entry.get("status") in ("queued", "held", "retrying"):
                        self._status(u.entry, "sending")

    def _plan_frames(self, peer: str, units: list) -> list:
        """This burst as a list of frames, each a list of units. File pieces
        share a frame when the speed allows and the station takes it."""
        cap, max_frames = FRAME_CAP_NARROW, WINDOW
        if self.burst_plan is not None:
            try:
                cap, max_frames = self.burst_plan(peer)
            except Exception:
                pass
        multi = peer in self.multi_ok and cap > FRAME_CAP_NARROW
        hdr = len(pack(T_CHUNKS, self.me(), peer, b"\x00"))
        frames = []
        for u in units:
            if frames and self._fits(frames[-1], u, hdr, cap, multi):
                frames[-1].append(u)
            elif len(frames) < max_frames:
                frames.append([u])
            else:
                break
        return frames

    @staticmethod
    def _fits(group: list, u, hdr: int, cap: int, multi: bool) -> bool:
        if not multi or u.piece is None or any(x.piece is None for x in group):
            return False
        return hdr + sum(8 + len(x.piece[2]) for x in group + [u]) <= cap

    def _multi_payload(self, peer: str, group: list, left: int) -> bytes:
        body = bytes([len(group)])
        for u in group:
            fid, idx, data = u.piece
            body += struct.pack(">HHHH", u.id, fid, idx, len(data)) + data
        return pack(T_CHUNKS, self.me(), peer, body, left)

    def _unit_sent(self, u: _Unit):
        u.sent_at = self.now()
        e = u.entry
        if u.kind == "msg" and e.get("status") == "sending":
            self._status(e, "sent")

    def _acked(self, peer: str, ids: list):
        done = [u for u in self.queue if u.peer == peer and u.id in ids]
        for u in done:
            self.queue.remove(u)
            e = u.entry
            if u.kind == "msg":
                self._status(e, "delivered", delivered_at=self.now())
            else:
                if u.kind == "chunk":
                    e["done"] = e.get("done", 0) + 1
                rest = [x for x in self.queue if x.entry is e]
                if not rest:
                    self._status(e, "delivered", delivered_at=self.now())
                else:
                    self._status(e, "sending")
        if done:
            self.pump()

    # ── receiving ─────────────────────────────────────────────────────
    def on_heard(self, call: str, snr=None, mode: str = "", how: str = ""):
        """Any frame heard from this station (chat, ping, Winlink)."""
        call = norm(call)
        if not call or call == self.me() or call == NET:
            return
        h = self.heard.setdefault(call, {})
        h.update(last=self.now(), how=how or h.get("how", ""))
        self._dirty = True
        if snr is not None:
            h["snr"] = int(round(snr))
        if mode:
            h["mode"] = mode
        self.on_event("heard", {"call": call})
        self._release_held(call)

    def _release_held(self, call: str):
        now = self.now()
        back = [e for e in self.held if e["to"] == call
                and now - e.get("held_tried", 0) >= HELD_RETRY_GAP_S]
        for e in back:
            self.held.remove(e)
            e["was_held"] = True
            e["held_tried"] = now
            mid = e["id"]
            ts, text = int(e["ts"]), e["text"]
            self.queue.append(_Unit(call, mid, "msg", lambda left, m=mid, t=text, s=ts: pack(
                T_MSG, self.me(), call, msg_body(m, F_HELD, s, t), left), e))
            self._status(e, "retrying", note=f"{call} heard -- delivering")
        if back:
            self.pump()

    def on_frame(self, payload: bytes, snr=None, mode: str = ""):
        """A decoded chat frame. Returns the parsed dict (or None)."""
        d = unpack(payload)
        if d is None:
            return None
        me = self.me()
        frm, to = d["frm"], d["to"]
        if not frm or frm == me:
            return d
        self.on_heard(frm, snr, mode, "chat")
        t = d["type"]
        if t == T_MSG and to == NET and d["flags"] & F_NET:
            kind = "cq" if d["flags"] & F_CQ else ("checkin" if d["flags"] & F_CHECKIN else "msg")
            if kind == "checkin":
                self.heard[frm]["checkin"] = self.now()
            self._add(NET, {"dir": "in", "frm": frm, "to": NET, "text": d["text"], "ts": self.now(),
                            "sent_ts": d["ts"], "kind": kind, "snr": snr}, unread=True)
            return d
        if to != me:
            return d                            # someone else's conversation
        if t == T_ACK:
            extra = d.get("extra") or b""
            if len(extra) >= 3 and extra[-3:-1] == FEATURES[:2] and extra[-1] >= 2:
                self.multi_ok.add(frm)
            if d.get("extra") and self.on_report is not None:
                try:
                    self.on_report(frm, d["extra"])
                except Exception:
                    pass
            self._acked(frm, d["ids"])
            if d.get("snr") is not None and d["snr"] > -128:
                self.heard[frm]["they_hear"] = d["snr"]
            return d
        # reliable: acknowledge (duplicates too -- our acknowledgment was lost)
        due = self._ack_due.setdefault(frm, {"ids": set(), "last": 0.0, "snr": -128, "now": False})
        if t == T_CHUNKS:
            for mid, fid, idx, data in d["pieces"]:
                due["ids"].add(mid)
                key = (frm, mid)
                if not (key in self._rx_seen and self.now() - self._rx_seen[key] < DEDUPE_S):
                    f = self._file_rec(frm, fid)
                    f["chunks"][idx] = data
                    self._file_progress(frm, fid)
                self._rx_seen[key] = self.now()
            due["last"] = self.now()
            if snr is not None:
                due["snr"] = int(max(-127, min(127, round(snr))))
            if d["left"] == 0:
                due["now"] = True
            if due["now"]:
                self._send_ack(frm)
            return d
        due["ids"].add(d["id"])
        due["last"] = self.now()
        if snr is not None:
            due["snr"] = int(max(-127, min(127, round(snr))))
        if d["left"] == 0:
            due["now"] = True
        key = (frm, d["id"])
        now = self.now()
        dup = key in self._rx_seen and now - self._rx_seen[key] < DEDUPE_S
        self._rx_seen[key] = now
        if not dup:
            if t == T_MSG:
                e = {"dir": "in", "frm": frm, "to": me, "text": d["text"], "ts": now,
                     "sent_ts": d["ts"], "kind": "msg", "snr": snr, "id": d["id"]}
                if d["flags"] & F_HELD:
                    e["held_note"] = True
                self._add(frm, e, unread=True)
            elif t == T_OFFER:
                f = self._file_rec(frm, d["fid"])
                f.update(name=d["name"], size=d["size"], n=d["n"], crc=d["crc"])
                self._file_progress(frm, d["fid"])
            elif t == T_CHUNK:
                f = self._file_rec(frm, d["fid"])
                f["chunks"][d["idx"]] = d["data"]
                self._file_progress(frm, d["fid"])
        if due["now"]:
            self._send_ack(frm)
        return d

    def _file_rec(self, frm, fid):
        return self._files_in.setdefault((frm, fid), {"chunks": {}, "entry": None, "name": None})

    def _file_progress(self, frm, fid):
        f = self._files_in[(frm, fid)]
        if f.get("name") is None:
            return                              # chunks before the offer: wait for it
        if f["entry"] is None:
            f["entry"] = self._add(frm, {"dir": "in", "frm": frm, "to": self.me(), "kind": "file",
                                         "name": f["name"], "size": f["size"], "n": f["n"], "done": 0,
                                         "ts": self.now(), "status": "receiving"}, unread=True)
        e = f["entry"]
        e["done"] = len(f["chunks"])
        if len(f["chunks"]) >= f["n"] and e.get("status") != "received":
            data = b"".join(f["chunks"].get(i, b"") for i in range(f["n"]))
            if (zlib.crc32(data) & 0xFFFFFFFF) != f["crc"] or len(data) != f["size"]:
                self._status(e, "failed", note="file arrived damaged")
                return
            path = self._save_file(frm, f["name"], data)
            self._status(e, "received", path=path)
            self.on_event("file", {"peer": frm, "entry": e})
        else:
            self._status(e, "receiving")

    def _save_file(self, frm, name, data) -> str | None:
        if not self.files_dir:
            return None
        os.makedirs(self.files_dir, exist_ok=True)
        safe = "".join(c for c in name if c.isalnum() or c in "._- ") or "file"
        base, ext = os.path.splitext(safe)
        path = os.path.join(self.files_dir, f"{frm}_{base}{ext}")
        k = 1
        while os.path.exists(path):
            k += 1
            path = os.path.join(self.files_dir, f"{frm}_{base}_{k}{ext}")
        with open(path, "wb") as fh:
            fh.write(data)
        return path

    def _send_ack(self, peer: str):
        due = self._ack_due.pop(peer, None)
        if not due or not due["ids"]:
            return
        ids = sorted(due["ids"])[-100:]
        body = bytes([len(ids)]) + struct.pack(">b", due["snr"]) + struct.pack(f">{len(ids)}H", *ids)
        if self.report_fn is not None:
            try:
                body += self.report_fn(peer) or b""
            except Exception:
                pass
        body += FEATURES                        # v0.9.40: we take multi-piece frames
        # acknowledgments go even during a Winlink session: they are short and
        # answer a station that is transmitting chat right now
        self.transmit(pack(T_ACK, self.me(), peer, body), peer, 1, lambda: None)

    # ── timers ───────────────────────────────────────────────────────
    def tick(self):
        now = self.now()
        resent_peers = set()
        for peer, due in list(self._ack_due.items()):
            if due["ids"] and (due["now"] or now - due["last"] >= ACK_IDLE_S):
                self._send_ack(peer)
        for u in list(self.queue):
            if u.in_flight and u.sent_at is None and u.queued_at and now - u.queued_at > 120.0:
                u.in_flight = False             # never went out (modem stopped?) -- try again
                u.attempts -= 1
                continue
            if u.in_flight and u.sent_at is not None and now - u.sent_at >= RTO_S + 2.0 * (u.attempts - 1):
                u.in_flight = False
                if u.attempts >= (MAX_ATTEMPTS if u.kind == "msg" else MAX_FILE_ATTEMPTS):
                    self._give_up(u)
                else:
                    resent_peers.add(u.peer)
                    if u.kind == "msg":
                        self._status(u.entry, "retrying",
                                     note=f"no reply, resending ({u.attempts + 1}/{MAX_ATTEMPTS})")
        if self.on_resend is not None:
            for peer in resent_peers:
                try:
                    self.on_resend(peer)
                except Exception:
                    pass
        for e in list(self.held):
            if now - e["ts"] > HELD_MAX_AGE_S:
                self.held.remove(e)
                self._status(e, "failed", note="not delivered in 7 days")
        for k in [k for k, t in self._rx_seen.items() if now - t > DEDUPE_S]:
            del self._rx_seen[k]
        self.pump()
        if self._dirty and now - self._last_save >= 10.0:
            self.save()

    def _give_up(self, u: _Unit):
        e = u.entry
        if u.kind == "msg":
            self.queue.remove(u)
            e["held_tried"] = self.now()
            self.held.append(e)
            self._status(e, "held", note=f"{u.peer} not answering -- will deliver when {u.peer} is heard")
        else:
            for x in [x for x in self.queue if x.entry is e]:
                self.queue.remove(x)
            self._status(e, "failed", note=f"{u.peer} stopped answering")

    def cancel(self, entry: dict):
        """Stop trying to deliver a queued, held or failed outgoing item."""
        for x in [x for x in self.queue if x.entry is entry]:
            self.queue.remove(x)
        if entry in self.held:
            self.held.remove(entry)
        self._status(entry, "cancelled")

    def mark_read(self, peer: str):
        if self.unread.pop(peer, None):
            self._dirty = True

    def held_for(self, call: str) -> int:
        return sum(1 for e in self.held if e["to"] == norm(call))

    # ── persistence ──────────────────────────────────────────────────
    def save(self):
        self._dirty = False
        self._last_save = self.now()
        if not self.store_path:
            return
        # messages still being sent are kept as held, so a restart doesn't lose them
        pending = [u.entry for u in self.queue if u.kind == "msg"]
        data = {"v": 1, "next_id": self.next_id, "convs": self.convs, "unread": self.unread,
                "heard": self.heard,
                "held": [self.convs_index(e) for e in self.held + pending]}
        tmp = self.store_path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False)
            os.replace(tmp, self.store_path)
        except OSError:
            pass

    def convs_index(self, e: dict):
        lst = self.convs.get(e.get("to"), [])
        for i, x in enumerate(lst):
            if x is e:
                return [e.get("to"), i]
        return None

    def load(self):
        if not self.store_path or not os.path.exists(self.store_path):
            return
        try:
            with open(self.store_path, encoding="utf-8") as fh:
                d = json.load(fh)
            self.convs = {k: list(v) for k, v in d.get("convs", {}).items()}
            self.unread = dict(d.get("unread", {}))
            self.heard = dict(d.get("heard", {}))
            self.next_id = int(d.get("next_id", 1)) or 1
            for ref in d.get("held", []):
                if not ref:
                    continue
                e = self.convs.get(ref[0], [])[ref[1]]
                if e.get("status") not in ("delivered", "cancelled"):
                    e["status"] = "held"
                    e.setdefault("held_tried", 0)
                    self.held.append(e)
            for lst in self.convs.values():           # anything mid-send at exit
                for e in lst:
                    if e.get("dir") == "out" and e.get("status") in ("queued", "sending", "retrying", "sent") \
                            and e not in self.held:
                        e["status"] = "failed" if e.get("kind") == "file" else e["status"]
        except Exception:
            self.convs, self.held = {}, []
