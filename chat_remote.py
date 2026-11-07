# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 PAREDN (Philippine Amateur Radio Emergency Data Network) / DW1IRF
"""
chat_remote.py -- v0.9.75: SARA Chat from another computer.

Like VARA Chat reaching a VARA FM modem over TCP: an operator with no
radio runs SARA FM on their own PC, sets "SARA Chat through a remote SARA FM
station" (host, port), and chats under THEIR OWN callsign through that
station's radio -- the same way Winlink Express uses a SARA FM modem over
the KISS port. The station keeps its own chat, and its BPQ32 keeps the KISS
port: remote chat has its own TCP port and serves several operators at once.

Everything that runs a chat (receipts, resends, held messages, files, the
NET room, the heard list) stays in the operator's own ChatEngine; the
station only carries frames:

  operator -> station   hello {call, ver}
                        tx    {id, p (base64 chat frame), peer, attempt}
                        report {peer, x (base64 link report)}  -- a receipt's
                              link report: how the far station hears the radio
  station -> operator   welcome {station, ver}
                        state {can_send}           -- on change and every 2 s
                        sent  {id, mode}           -- that frame is on the air
                        rx    {p, snr, mode}       -- a chat frame heard
                        heard {call, snr, mode, how} -- ping / packet traffic

Each message is one line of JSON. Frames between people on the same station
(its own operator and the remote operators) are passed locally, not aired.
Access uses the KISS port's network rule ("private" = Tailscale, AREDN,
LAN by default). KISS-style: no password -- anyone allowed in can transmit.
"""

from __future__ import annotations

import base64
import json
import socket
import sys
import threading
import time

import bpq_bridge

DEFAULT_PORT = 8180
PROTO = 1


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def _unb64(s: str) -> bytes:
    return base64.b64decode(s.encode("ascii"))


class _Conn:
    def __init__(self, sock: socket.socket, addr):
        self.sock = sock
        self.addr = addr
        self.call = ""
        self.lock = threading.Lock()

    def send(self, msg: dict) -> bool:
        data = (json.dumps(msg, separators=(",", ":")) + "\n").encode("utf-8")
        try:
            with self.lock:
                self.sock.sendall(data)
            return True
        except OSError:
            return False


def _lines(sock: socket.socket, running):
    """Yield decoded JSON objects from a socket until it closes."""
    buf = b""
    sock.settimeout(1.0)
    while running():
        try:
            chunk = sock.recv(8192)
        except socket.timeout:
            continue
        except OSError:
            return
        if not chunk:
            return
        buf += chunk
        if len(buf) > 1_000_000:
            return
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            if not line.strip():
                continue
            try:
                yield json.loads(line.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                continue


class ChatServer:
    """At the station with the radio. Callbacks run on the server's threads;
    the app queues them to its window thread."""

    def __init__(self, on_tx, on_report=None, on_status=None, on_client=None):
        self.on_tx = on_tx              # (conn, id, payload, peer, attempt)
        self.on_report = on_report      # (peer, bytes)
        self.on_status = on_status or (lambda m: None)
        self.on_client = on_client or (lambda conn, joined: None)
        self.station = ""
        self.can_send = False
        self._conns: list = []
        self._lock = threading.Lock()
        self._running = False
        self._sock = None
        self._allow = None

    def start(self, port: int = DEFAULT_PORT, host: str = "0.0.0.0", allow=None):
        self._allow = allow
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if not sys.platform.startswith("win"):
            # POSIX: rebind right after a Stop. Not on Windows, where
            # SO_REUSEADDR lets a SECOND program take a port that is in use
            # (two SARA FM copies set to the same chat port would both
            # "open" it and split the operators between them).
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((host, port))
        s.listen(8)
        self._sock = s
        self._running = True
        self._acc = threading.Thread(target=self._accept, daemon=True)
        self._acc.start()
        threading.Thread(target=self._state_loop, daemon=True).start()

    def stop(self):
        """Closes the port before returning, so a quick Stop / Start can
        open it again."""
        self._running = False
        acc = getattr(self, "_acc", None)
        if acc is not None and acc is not threading.current_thread():
            acc.join(2.0)
        try:
            if self._sock:
                self._sock.close()
        except OSError:
            pass
        with self._lock:
            for c in self._conns:
                try:
                    c.sock.close()
                except OSError:
                    pass
            self._conns = []

    def calls(self) -> set:
        with self._lock:
            return {c.call for c in self._conns if c.call}

    def clients(self) -> list:
        with self._lock:
            return [(c.call, c.addr[0]) for c in self._conns]

    def broadcast(self, msg: dict, skip=None):
        with self._lock:
            conns = [c for c in self._conns if c is not skip and c.call]
        for c in conns:
            c.send(msg)

    def send_to(self, call: str, msg: dict):
        with self._lock:
            conns = [c for c in self._conns if c.call == call]
        for c in conns:
            c.send(msg)

    def set_can_send(self, v: bool):
        v = bool(v)
        if v != self.can_send:
            self.can_send = v
            self.broadcast({"t": "state", "can_send": v})

    def _state_loop(self):
        while self._running:
            time.sleep(2.0)
            self.broadcast({"t": "state", "can_send": self.can_send})

    def _accept(self):
        while self._running:
            try:
                self._sock.settimeout(1.0)
                try:
                    sock, addr = self._sock.accept()
                except socket.timeout:
                    continue
            except OSError:
                return
            if self._allow is not None and not bpq_bridge.ip_allowed(addr[0], self._allow):
                try:
                    sock.close()
                except OSError:
                    pass
                self.on_status(f"Remote chat: refused {addr[0]} (not in the allowed list)")
                continue
            c = _Conn(sock, addr)
            threading.Thread(target=self._serve, args=(c,), daemon=True).start()

    def _serve(self, c: _Conn):
        try:
            for m in _lines(c.sock, lambda: self._running):
                t = m.get("t")
                if t == "hello":
                    call = str(m.get("call", "")).strip().upper()[:12]
                    if not call:
                        continue
                    c.call = call
                    with self._lock:
                        if c not in self._conns:
                            self._conns.append(c)
                    c.send({"t": "welcome", "station": self.station, "ver": PROTO})
                    c.send({"t": "state", "can_send": self.can_send})
                    self.on_client(c, True)
                elif not c.call:
                    continue
                elif t == "tx":
                    try:
                        p = _unb64(m["p"])
                    except (KeyError, ValueError, TypeError):
                        continue
                    self.on_tx(c, m.get("id"), p, m.get("peer"), int(m.get("attempt") or 1))
                elif t == "report" and self.on_report:
                    try:
                        self.on_report(str(m.get("peer", "")), _unb64(m["x"]))
                    except (KeyError, ValueError, TypeError):
                        pass
        finally:
            with self._lock:
                was = c in self._conns
                if was:
                    self._conns.remove(c)
            try:
                c.sock.close()
            except OSError:
                pass
            if was:
                self.on_client(c, False)


class ChatClient:
    """At the operator's PC. Keeps reconnecting while enabled."""
    RETRY_S = 10.0

    def __init__(self, call_fn, on_rx, on_heard=None, on_status=None):
        # v0.9.77: call_fn is read on the CALLER's thread only (start /
        # set_call). Field (Windows build, 0.9.76): reading a Tk variable on
        # this client's own thread raised "main thread is not in main loop",
        # the thread died, and the operator never connected.
        self.call_fn = call_fn
        self.call = ""
        self.on_rx = on_rx              # (payload, snr, mode)
        self.on_heard = on_heard or (lambda call, snr, mode, how: None)
        self.on_status = on_status or (lambda m: None)
        self.host, self.port = "", DEFAULT_PORT
        self.connected = False
        self.can_send = False
        self.station = ""
        self._conn = None
        self._running = False
        self._next_id = 1
        self._pending: dict = {}        # id -> on_sent
        self._lock = threading.Lock()

    def start(self, host: str, port: int, call: str | None = None):
        self.stop()
        self.host, self.port = host, port
        self.call = (call if call is not None else (self.call_fn() or "")).strip().upper()
        self._running = True
        threading.Thread(target=self._loop, daemon=True).start()

    def stop(self):
        self._running = False
        c = self._conn
        if c is not None:
            try:
                c.sock.close()
            except OSError:
                pass

    def ready(self) -> bool:
        return self.connected and self.can_send

    def transmit(self, payload: bytes, peer, attempt: int, on_sent) -> bool:
        c = self._conn
        if c is None or not self.connected:
            return False
        with self._lock:
            i = self._next_id
            self._next_id += 1
            self._pending[i] = on_sent
        ok = c.send({"t": "tx", "id": i, "p": _b64(payload), "peer": peer, "attempt": attempt})
        if not ok:
            with self._lock:
                self._pending.pop(i, None)
        return ok

    def report(self, peer: str, extra: bytes):
        c = self._conn
        if c is not None and self.connected:
            c.send({"t": "report", "peer": peer, "x": _b64(extra)})

    def _loop(self):
        while self._running:
            try:
                s = socket.create_connection((self.host, self.port), timeout=8)
            except OSError as e:
                self.on_status(f"can't reach {self.host}:{self.port} ({e.__class__.__name__}) -- retrying")
                self._sleep(self.RETRY_S)
                continue
            c = _Conn(s, (self.host, self.port))
            self._conn = c
            c.send({"t": "hello", "call": self.call, "ver": PROTO})
            try:
                for m in _lines(s, lambda: self._running):
                    self._handle(m)
            finally:
                self._conn = None
                was = self.connected
                self.connected = self.can_send = False
                with self._lock:
                    self._pending.clear()
                try:
                    s.close()
                except OSError:
                    pass
                if was:
                    self.on_status(f"disconnected from {self.station or self.host}")
            self._sleep(self.RETRY_S if self._running else 0)

    def _sleep(self, s):
        end = time.time() + s
        while self._running and time.time() < end:
            time.sleep(0.2)

    def _handle(self, m: dict):
        t = m.get("t")
        if t == "welcome":
            self.connected = True
            self.station = str(m.get("station", ""))
            self.on_status(f"connected to {self.station or self.host} ({self.host}:{self.port})")
        elif t == "state":
            self.can_send = bool(m.get("can_send"))
        elif t == "sent":
            with self._lock:
                fn = self._pending.pop(m.get("id"), None)
            if fn:
                fn()
        elif t == "rx":
            try:
                self.on_rx(_unb64(m["p"]), m.get("snr"), str(m.get("mode", "")))
            except (KeyError, ValueError, TypeError):
                pass
        elif t == "heard":
            self.on_heard(str(m.get("call", "")), m.get("snr"), str(m.get("mode", "")), str(m.get("how", "")))
