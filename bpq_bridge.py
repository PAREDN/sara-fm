#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 PAREDN (Philippine Amateur Radio Emergency Data Network) / DW1IRF
"""
bpq_bridge.py — TCP server BPQ32 connects to; bridges KISS <-> the OFDM
physical layer (ofdm_core, via audio_io.AudioEngine).

Per the original project's BPQ32 port config (see BPQ32.cfg.example),
BPQ32 is configured as TYPE=ASYNC / PROTOCOL=KISS with an IPADDR/TCPPORT
-- meaning BPQ32 makes the TCP connection TO us, not the other way
around. So this is a server that accepts one connection and holds it;
if BPQ32 reconnects (e.g. after a restart), we accept the new connection
and drop the old one.

This module has no GUI and no direct knowledge of ping/pong vs KISS
traffic -- it just moves bytes: KISS-framed bytes arriving from the
client get unwrapped and handed to a callback (normally
AudioEngine.transmit); bytes decoded off the air get KISS-wrapped and
written back to the client. The GUI decides when this bridge is
running and which AudioEngine it's attached to.

"BPQ32" in this file's naming reflects the primary intended use, but
any KISS-speaking client works the same way -- Winlink Express's own
Packet Winlink session, for instance, can connect directly to this
same port without BPQ32 in the picture at all, dialing a station whose
BPQ32 node is on the other end of the air link.
"""

from __future__ import annotations
import collections
import ipaddress
import socket
import struct
import threading
import time
from typing import Callable, Optional

import kiss

DEFAULT_PORT = 8105   # matches TCPPORT in the original BPQ32_SARA_FM.cfg

# ─────────────────────────────────────────────────────────────────────────
# Frame coalescing: BPQ32's own handshakes (Winlink B2F, connect setup)
# send several small AX.25 frames in quick succession -- a few bytes each,
# one after another a fraction of a second apart. Every SARA-PP
# transmission pays the same fixed overhead (TX delay + burst + TX tail)
# regardless of payload size, so sending each of those small frames as
# its own transmission means paying that fixed cost many times over for
# almost no data. Bundling several into one OFDM transmission pays that
# fixed cost once instead.
#
# This is purely an internal SARA-PP-to-SARA-PP optimization on the air
# link -- BPQ32 on both ends only ever sees normal, individual KISS
# frames; the bundling and unbundling happen entirely inside this module,
# transparently. Both ends need to be running a version that understands
# the batch format for this to work (same as any other version bump in
# this project) -- an older peer would misinterpret a batch's marker/
# length-prefix bytes as raw AX.25 content.
# ─────────────────────────────────────────────────────────────────────────

COALESCE_WINDOW_S = 0.12   # wait this long for more frames before sending
MAX_BATCH_FRAMES = 8
# MAX_BATCH_BYTES caps transmission DURATION, not just size -- this is the
# real reason it's small, not ofdm_core's buffer capacity (which is sized
# separately, and deliberately larger, to hold whatever legitimately
# arrives). Found via real field data: BPQ32 using its full MAXFRAME=7
# window, coalesced into one 1780-byte transmission (~8s at QPSK), failed
# CRC repeatedly with large, erratic drift_deg (-110 to 170 degrees across
# retries) despite excellent quality (~0.95) -- not corruption, not noise,
# but genuine real clock drift between the two stations' independent sound
# cards. Direct testing confirmed this hardware pair shows real drift
# around 20-30ppm, and that at THIS payload size, even 30ppm pushes
# accumulated phase error past the ~90 degree point where QPSK
# demodulation starts flipping into the wrong constellation quadrant --
# despite per-symbol pilot tracking (ofdm_core.py) correctly measuring
# that drift the whole way (verified by direct instrumentation: smooth,
# linear, accurate tracking right up to the failure point). The tracking
# isn't broken; accumulated drift over a transmission this long genuinely
# exceeds what any phase-based correction can recover once it crosses that
# boundary. 500 bytes was the first value chosen here, keeping even BPSK
# reliable through 80ppm in single-trial testing -- a real margin above
# the ~20-30ppm this hardware had shown at the time.
#
# UPDATE: 500 bytes was not enough margin. The next real field test
# (both stations on that fix) showed a frame matching DW1IRF's actual
# 510-byte transmission exactly (512 = 510 + the 2-byte frame CRC)
# still failing, with drift_deg=-89.975 -- right on the same ~90 degree
# cliff, meaning real drift on this hardware occasionally reaches or
# exceeds the ~80ppm that size was validated against. The same test
# also showed clear evidence of real over-the-air COLLISIONS (crc_fail
# candidates claiming sizes -- 1008, 1633, 1231 bytes -- that DW1IRF
# never actually transmitted; the signature of two stations' audio
# mixing together, the garbled result occasionally resembling a valid
# preamble+header). Carrier sense reduces collisions but can't
# eliminate them -- it reacts to energy that has already arrived, so a
# narrow window around the exact moment both stations decide to
# transmit remains a real gap, the same reason real AX.25 systems lean
# on retry/ARQ rather than expecting zero collisions.
#
# Re-validated more rigorously this time: multiple trials per size/ppm
# combination, each with a different sub-symbol timing offset, since a
# single deterministic resample() trial can land luckily or unluckily
# right at a boundary. 250 bytes passed EVERY trial (5/5) through
# 150ppm in both modulations -- a real, multi-trial-confirmed margin,
# roughly 2x the ~80-100ppm the field data now suggests this hardware
# can actually produce, rather than a single untested data point.
MAX_BATCH_BYTES = 250
BATCH_MARKER = 0x01


def pack_batch(frames: list) -> bytes:
    """One or more AX.25 frames -> a single wire payload: [marker][count]
    then [2-byte length][frame bytes] for each. Used even for a single
    frame, so the receiver never has to guess which format it's looking at."""
    out = bytearray([BATCH_MARKER, len(frames)])
    for f in frames:
        out += struct.pack('>H', len(f))
        out += f
    return bytes(out)


def unpack_batch(payload: bytes) -> list:
    """Inverse of pack_batch(). Falls back to treating the whole payload as
    one frame if it doesn't look like a batch we produced (e.g. a peer
    running an older version) -- best-effort, not officially supported;
    both ends should run matching versions."""
    if len(payload) < 2 or payload[0] != BATCH_MARKER:
        return [payload]
    count = payload[1]
    frames = []
    pos = 2
    try:
        for _ in range(count):
            length = struct.unpack('>H', payload[pos:pos + 2])[0]
            pos += 2
            frames.append(payload[pos:pos + length])
            pos += length
    except Exception:
        return [payload]   # malformed -- fail safe rather than raise
    return frames


# v0.9.4: a 16-QAM Wide frame airs 2x the data of a QPSK Wide one in the
# same time, so at burst time two full AX.25 frames (2 x 256 + headers)
# may share one OFDM frame: one preamble, no 0.2 s gap. ~1.2 s on air, still
# shorter than one QPSK frame (ofdm_core.MAX_RX_DATA_BYTES_QAM16 covers it).
MAX_BATCH_BYTES_QAM16 = 560


def merge_cap(modulation: int | None) -> int:
    import ofdm_core
    if modulation is not None:
        modulation = ofdm_core.base_mod(modulation)
    return (MAX_BATCH_BYTES_QAM16 if modulation in (ofdm_core.MOD_QAM16_WIDE, ofdm_core.MOD_QAM64_WIDE)
            else MAX_BATCH_BYTES)


def pack_for_mode(frames: list, modulation: int | None) -> list:
    """v0.9.58: AX.25 frames -> one or more batch payloads, each within what
    `modulation` may carry (merge_cap, counted like the coalescer). A frame
    bigger than the cap on its own still goes alone, as it always has.
    Field (DV1UK digipeater, 0.9.57): DW1IRF sent two 282-byte frames per
    16QAM transmission (562 bytes); DV1UK relayed each pair as ONE frame in
    QPSK Wide, its speed to DX1FLR -- over the QPSK Wide size limit, so
    every receiver turned it away ("implausible_length ... over max frame
    size") and the session died on retries."""
    cap = merge_cap(modulation)
    out, cur, size = [], [], 0
    for f in frames:
        if cur and (size + len(f) > cap or len(cur) >= 255):
            out.append(pack_batch(cur))
            cur, size = [], 0
        cur.append(f)
        size += len(f)
    if cur:
        out.append(pack_batch(cur))
    return out


def merge_batches(a: bytes, b: bytes, modulation: int | None = None) -> bytes | None:
    """Two batch payloads -> one, if both are batches we produced and their
    AX.25 frames together stay within MAX_BATCH_BYTES (the same cap, counted
    the same way, as the coalescer -- so a merged batch is never longer on
    the air than one the drift tests already validated); else None.

    v0.8.5 (acknowledgment batching): Winlink Express acknowledges each data
    frame separately. Those small RR frames pile up in the TX queue while
    DCD holds the station during the other side's window, then went out as
    SEVEN separate OFDM frames, each with its own preamble and gap: ~3.4 s
    in QPSK Wide vs ~1.3 s as one frame -- ~2 s of every ~13 s transfer
    cycle (field, v0.8.4). The engine merges consecutive queued batches at
    burst time with this. Receivers have always unpacked multi-frame
    batches, so this needs no change at the other end."""
    if not a or not b or a[0] != BATCH_MARKER or b[0] != BATCH_MARKER:
        return None
    fa, fb = unpack_batch(a), unpack_batch(b)
    if fa == [a] or fb == [b]:          # not well-formed batches: leave alone
        return None
    frames = fa + fb
    if len(frames) > 255 or sum(len(f) for f in frames) > merge_cap(modulation):
        return None
    return pack_batch(frames)


class BpqBridge:
    """
    on_air_frame(ax25_bytes): called from the bridge's own thread when a
    complete KISS data frame arrives from BPQ32 -- hand this straight to
    AudioEngine.transmit() (in its own thread; transmit() is blocking).

    Call deliver_from_air(payload) whenever a frame is decoded off the
    air, to KISS-wrap it and send it on to BPQ32 if a client is
    connected. If nothing is connected, it's just dropped -- there is no
    queueing of air traffic for a BPQ32 that isn't there to receive it,
    deliberately: KISS/AX.25 has its own retry layer above this for
    exactly that situation.
    """

    def __init__(self, on_air_frame: Callable[[bytes], None],
                 on_status: Optional[Callable[[str], None]] = None):
        self.on_air_frame = on_air_frame
        self.on_status = on_status or (lambda msg: None)
        self._server_sock: Optional[socket.socket] = None
        self._client_sock: Optional[socket.socket] = None
        self._client_lock = threading.Lock()
        self._accept_thread: Optional[threading.Thread] = None
        self._running = False
        # v0.9.0 ACKMODE: frame bytes -> FIFO of (ack id, port) awaiting transmission
        self._ack_lock = threading.Lock()
        self._ack_pending: dict = {}
        self.ackmode_seen = False
        # optional hook: called with each AX.25 frame the client asks us to send
        self.on_client_frame: Optional[Callable[[bytes], None]] = None
        # v0.9.83: a second program on this PC taking over the KISS port
        self._client_port = None
        self._replaced_socks: set = set()
        self._takeovers: list = []          # times of recent takeovers

    def _safe_status(self, msg: str):
        # A GUI callback raising here (e.g. a widget torn down mid-call)
        # must never be allowed to silently kill the accept/client thread
        # and take the whole bridge down with it.
        try:
            self.on_status(msg)
        except Exception:
            pass

    def start(self, port: int = DEFAULT_PORT, host: str = "127.0.0.1", allow=None):
        """host "127.0.0.1" (default) = this PC only; "0.0.0.0" = every
        network interface (v0.7.5, remote testing). allow = list of
        ipaddress networks a client must come from (None = no filter,
        appropriate only for localhost). See parse_allowlist()."""
        self._allow = allow
        self._client_ip = None
        with self._ack_lock:
            self._ack_pending.clear()
        self.ackmode_seen = False
        self._server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server_sock.bind((host, port))
        self._server_sock.listen(1)
        self._running = True
        self._accept_thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._accept_thread.start()
        self._safe_status(f"Listening on {host}:{port} for a KISS client (BPQ32, Winlink Express, etc.)...")

    def stop(self):
        self._running = False
        with self._client_lock:
            if self._client_sock:
                try:
                    self._client_sock.close()
                except OSError:
                    pass
                self._client_sock = None
            self._client_ip = None
        if self._server_sock:
            try:
                self._server_sock.close()
            except OSError:
                pass
            self._server_sock = None
        self._safe_status("Stopped.")

    def deliver_from_air(self, payload: bytes):
        """KISS-wrap a decoded frame (or batch of frames, see pack_batch)
        and send it on to BPQ32, if connected. A batch is unbundled first
        -- BPQ32 only ever sees normal, individual KISS frames; it never
        knows the air link coalesced several into one transmission."""
        with self._client_lock:
            sock = self._client_sock
        if sock is None:
            return
        for frame in unpack_batch(payload):
            try:
                sock.sendall(kiss.encode_frame(frame))
            except OSError as e:
                self._safe_status(f"Send to BPQ32 failed: {e}")
                return

    # ── ACKMODE (v0.9.0) ─────────────────────────────────────────────
    def _send_acks(self, acks):
        with self._client_lock:
            sock = self._client_sock
        if sock is None:
            return
        for ack_id, port in acks:
            try:
                sock.sendall(kiss.encode_frame(ack_id, port=port, command=kiss.CMD_ACKMODE))
            except OSError:
                return

    def frames_sent(self, payload: bytes):
        """The batch carrying these frames has just been transmitted:
        acknowledge each ACKMODE frame in it (merged batches included --
        frames are matched by content, oldest id first)."""
        acks = []
        with self._ack_lock:
            for f in unpack_batch(payload):
                q = self._ack_pending.get(bytes(f))
                if q:
                    acks.append(q.popleft())
                    if not q:
                        del self._ack_pending[bytes(f)]
        if acks:
            self._send_acks(acks)

    def ack_all_pending(self):
        """A transmission failed: acknowledge everything outstanding so
        BPQ32's own FRACK timers run and it retries, rather than waiting
        for acknowledgments that would never come."""
        with self._ack_lock:
            acks = [a for q in self._ack_pending.values() for a in q]
            self._ack_pending.clear()
        if acks:
            self._send_acks(acks)

    def _accept_loop(self):
        while self._running:
            try:
                self._server_sock.settimeout(1.0)
                try:
                    client, addr = self._server_sock.accept()
                except socket.timeout:
                    continue
            except OSError:
                break
            ip = addr[0]
            allow = getattr(self, "_allow", None)
            if allow is not None and not ip_allowed(ip, allow):
                self._refuse(client, f"Refused KISS client from {ip}: not in the allowed list.")
                continue
            took_over_from = None
            with self._client_lock:
                current_ip = getattr(self, "_client_ip", None)
                if self._client_sock is not None and current_ip is not None and ip != current_ip:
                    # v0.7.5: never let a DIFFERENT address take over -- over a
                    # network that would silently disconnect the station's own
                    # BPQ32 (gateway off the air). A reconnect from the SAME
                    # address still replaces the old socket, as before (it may
                    # be half-dead after a client restart).
                    busy = True
                else:
                    busy = False
                    if self._client_sock is not None:
                        took_over_from = getattr(self, "_client_port", None)
                        self._replaced_socks.add(self._client_sock)
                        try:
                            self._client_sock.close()
                        except OSError:
                            pass
                    self._client_sock = client
                    self._client_ip = ip
                    self._client_port = addr[1]
            if busy:
                self._refuse(client, f"Refused KISS client from {ip}: already serving {current_ip}.")
                continue
            self._safe_status(f"KISS client connected from {addr[0]}:{addr[1]}")
            if took_over_from is not None:
                self._note_takeover(addr, took_over_from)
            threading.Thread(target=self._client_loop, args=(client,), daemon=True).start()

    TAKEOVER_WINDOW_S = 120.0

    def _note_takeover(self, addr, old_port):
        """v0.9.83: field (DU3FS, 0.9.79): two programs on one PC -- BPQ32 and
        Winlink Express, or two copies of SARA FM / BPQ32 -- kept connecting
        to the same KISS port. Each new connection silently replaced the
        last, so the program running a Winlink session lost its link mid-
        session and the far end's polls went unanswered. Say so plainly."""
        now = time.time()
        self._takeovers = [t for t in self._takeovers if now - t < self.TAKEOVER_WINDOW_S] + [now]
        n = len(self._takeovers)
        port = None
        try:
            port = self._server_sock.getsockname()[1]
        except Exception:
            pass
        msg = (f"NOTE: a new connection ({addr[0]}:{addr[1]}) took over SARA FM's KISS "
               f"port{f' {port}' if port else ''} from the one connected before (:{old_port}) -- that "
               f"one is now cut off. Normal if BPQ32 just restarted; otherwise another program on this "
               f"computer uses the same port. Only ONE program may use a KISS port.")
        if n >= 2:
            msg += (f" This happened {n} times in the last {self.TAKEOVER_WINDOW_S / 60:.0f} minutes: two "
                    f"programs are taking turns (e.g. BPQ32 and Winlink Express both set to this port, or "
                    f"a second copy of SARA FM / BPQ32), and Winlink sessions will stall. Point only one "
                    f"of them at this port; Winlink Express can connect through BPQ32 instead.")
        self._safe_status(msg)

    def _refuse(self, client: socket.socket, msg: str):
        try:
            client.close()
        except OSError:
            pass
        self._safe_status(msg)

    def _rewrite(self, frame: bytes) -> bytes:
        """v0.9.71: a frame from the client as it goes on the air (the
        gateway puts a digipeater path back -- link_adapt.ViaMap). Applied
        before the ACKMODE id is filed, so the id still matches when aired."""
        fn = getattr(self, "rewrite_from_client", None)
        if fn is None:
            return frame
        try:
            return fn(bytes(frame))
        except Exception:
            return frame

    def _client_loop(self, sock: socket.socket):
        decoder = kiss.KissDecoder()
        # Short timeout so the coalescing window (COALESCE_WINDOW_S, well
        # under a second) gets checked promptly even when no new data has
        # arrived -- not because we need to poll the socket itself quickly.
        sock.settimeout(0.05)
        pending: list = []
        pending_since: Optional[float] = None
        pending_bytes = 0

        def flush():
            nonlocal pending, pending_since, pending_bytes
            if not pending:
                return
            batch = pack_batch(pending)
            pending, pending_since, pending_bytes = [], None, 0
            try:
                self.on_air_frame(batch)
            except Exception as e:
                self._safe_status(f"TX from BPQ32 frame failed: {e}")

        while self._running:
            try:
                data = sock.recv(4096)
            except socket.timeout:
                data = None   # nothing arrived this cycle; connection still alive
            except OSError:
                break
            if data is not None:
                if not data:
                    break   # BPQ32 closed the connection (real EOF, not a timeout)
                for port, command, payload in decoder.feed(data):
                    if command == kiss.CMD_ACKMODE and len(payload) > 2:
                        # v0.9.0: remember the id; acknowledged in frames_sent()
                        ack_id, payload = bytes(payload[:2]), bytes(payload[2:])
                        payload = self._rewrite(payload)
                        with self._ack_lock:
                            self._ack_pending.setdefault(payload, collections.deque()).append((ack_id, port))
                        if not self.ackmode_seen:
                            self.ackmode_seen = True
                            self._safe_status("KISS ACKMODE in use: BPQ32 times each frame from when it is actually sent.")
                        command = kiss.CMD_DATA
                    elif command == kiss.CMD_DATA and payload:
                        payload = self._rewrite(payload)
                    if command == kiss.CMD_DATA and payload:
                        hook = getattr(self, "on_client_frame", None)
                        if hook is not None:
                            try:
                                hook(payload)
                            except Exception:
                                pass
                        # v0.7.1: flush BEFORE adding a frame that would push
                        # the batch past MAX_BATCH_BYTES. Checking only after
                        # appending let batches reach ~2x the cap (field logs:
                        # "386 bytes (2 frames)", "510 bytes (2 frames)") --
                        # exactly the ~510-byte size the cap exists to prevent
                        # (clock drift past ~90 degrees). A single frame larger
                        # than the cap still goes out alone, as it always has.
                        if pending and pending_bytes + len(payload) > MAX_BATCH_BYTES:
                            flush()
                        pending.append(payload)
                        pending_bytes += len(payload)
                        if pending_since is None:
                            pending_since = time.time()
                        if len(pending) >= MAX_BATCH_FRAMES or pending_bytes >= MAX_BATCH_BYTES:
                            flush()
            if pending_since is not None and (time.time() - pending_since) >= COALESCE_WINDOW_S:
                flush()

        flush()   # don't drop a partially-collected batch on disconnect
        with self._ack_lock:
            self._ack_pending.clear()
        with self._client_lock:
            if self._client_sock is sock:
                self._client_sock = None
                self._client_ip = None      # free for a client from any allowed address
                self._client_port = None
            replaced = sock in self._replaced_socks
            self._replaced_socks.discard(sock)
        self._safe_status("KISS client cut off (replaced by the newer connection)." if replaced
                          else "KISS client disconnected.")


# ── network KISS access (v0.7.5) ─────────────────────────────────────────
# KISS has no authentication: whoever can connect can transmit on the radio
# under any callsign. So network listening is off by default, and when on,
# clients must come from an allowed address. "private" = this PC plus the
# address ranges remote-testing setups actually use: Tailscale (CGNAT
# 100.64.0.0/10), AREDN mesh and LANs (10/8, 172.16/12, 192.168/16) --
# public internet addresses are refused unless listed explicitly.
PRIVATE_NETS = ["127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10"]


def parse_allowlist(text: str):
    """'private', 'any', or comma/space-separated IPs and CIDR ranges (may
    be mixed with 'private'). Returns (networks, error_message_or_None)."""
    items = [t for t in text.replace(",", " ").split() if t]
    if not items:
        items = ["private"]
    nets = []
    for t in items:
        if t.lower() == "private":
            nets += [ipaddress.ip_network(n) for n in PRIVATE_NETS]
        elif t.lower() == "any":
            nets += [ipaddress.ip_network("0.0.0.0/0")]
        else:
            try:
                nets.append(ipaddress.ip_network(t, strict=False))
            except ValueError:
                return None, f"'{t}' is not an IP address or range (e.g. 100.101.102.103 or 192.168.1.0/24)"
    return nets, None


def ip_allowed(ip: str, nets) -> bool:
    """v0.9.5: this PC itself (127.x / ::1 -- the station's own BPQ32 or
    Winlink Express) is ALWAYS allowed. Field (DV1UK): with "Allow network
    KISS clients" on and only a Tailscale address listed, the gateway's own
    BPQ32 on 127.0.0.1 was refused every 30 s -- the gateway off the air.
    The list only controls OTHER computers."""
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if a.is_loopback or (getattr(a, "ipv4_mapped", None) is not None and a.ipv4_mapped.is_loopback):
        return True
    return any(a in n for n in nets)
