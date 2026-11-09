#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 PAREDN (Philippine Amateur Radio Emergency Data Network) / DW1IRF
"""
sara_pingpong.py — SARA-PP: OFDM link tester, now also a BPQ32 TNC.

A deliberately small VARA-FM-style GUI: pick your audio devices, enter
your callsign, hit Ping, see whether the other station's Pong comes
back. Two modes, selected by radio button, sharing the same audio
engine, PTT, GPIO pin, TX delay, and modulation settings:

- Ping/Pong Test: this file's original purpose. Manual or batch pings,
  pass/fail/RTT, nothing else.
- BPQ32/KISS Bridge: starts a TCP server (bpq_bridge.py) that a real
  BPQ32 node connects to as a KISS TNC. Frames BPQ32 sends go out over
  the OFDM physical layer; frames decoded off the air go back to BPQ32.
  See README.md and BPQ32.cfg.example for the BPQ32-side port config.

Every ping/pong test result and every bridge TX/RX event is appended to
sara_pingpong_log.csv next to this script, so a session leaves a record
you can scan for patterns afterward instead of relying on memory.

Run:
    python sara_pingpong.py
"""

from __future__ import annotations
import os
# v0.9.86: numpy's maths library (OpenBLAS) starts one worker thread per core
# and lets idle workers spin for a while. On a slow 4-core gateway PC (DX1FLR,
# 0.9.85: decoder peaking at 108% of a core, "decoder waited for a free core")
# that spinning competes with the sound card. SARA FM's matrices are small,
# so one thread is as fast and leaves the other cores free. Must be set
# before numpy is first imported.
for _v in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(_v, "1")
import collections
import csv
import json
import glob
import sys
import os
import queue
import threading
import time
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox

import ofdm_core as ofdm
import protocol as proto
import audio_io
import bpq_bridge
import throughput
import tuning
import ping_window
import log_bundle
import link_adapt
import b2check
import session_stats
import gauges
import chat
import chat_window
import web_ui
import chat_remote
from audio_io import AudioEngine, list_devices
from bpq_bridge import BpqBridge
from ptt import (PTT_VOX, PTT_CM108, PTT_RTS, PTT_DTR, PTT_PIGPIO, PI_GPIO_DEFAULT_PIN,
                 PTTController, enumerate_ra_boards)

def app_dir() -> str:
    """Folder to keep logs in: next to SARA-FM.exe when running as the
    standalone build (where __file__ points inside the bundle's internal
    folder, which nobody would ever look in), next to this script otherwise."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


LOG_CSV = os.path.join(app_dir(), "sara_pingpong_log.csv")
PONG_REPLY_DELAY_S = 0.4     # let the sender release PTT and switch to RX
PING_TIMEOUT_S = 6.0

CSV_FIELDS = ["timestamp", "version", "my_call", "peer_call", "seq",
              "event", "rtt_ms", "quality", "note"]


def log_csv_row(**fields):
    is_new = not os.path.exists(LOG_CSV)
    with open(LOG_CSV, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        if is_new:
            w.writeheader()
        row = {k: fields.get(k, "") for k in CSV_FIELDS}
        row["timestamp"] = time.strftime("%Y-%m-%d %H:%M:%S")
        row["version"] = ofdm.VERSION
        w.writerow(row)


class SelfEchoFilter:
    """
    Tracks what we've recently transmitted in BPQ32/KISS Bridge mode, so a
    self-heard echo of our own transmission (local audio bleed-back --
    the same phenomenon Ping/Pong mode already filters by comparing
    callsigns) can be recognized and dropped instead of being forwarded
    to BPQ32 as if it were real incoming data from the other station.

    Bridge mode has no callsign concept of its own (it only ever sees raw
    AX.25 bytes), so this works differently and more generally: an echo
    is, by definition, an exact byte-for-byte copy of something we just
    sent, so comparing raw payload bytes against a short recent-TX
    history catches it regardless of what protocol is actually inside
    those bytes. Found via a field log where every single TX was
    immediately followed by an RX of the identical byte count, at a
    distinctly lower quality (local bleed) than genuine remote traffic.

    window_s was originally 2.0s, validated against short ping/pong-
    style traffic -- but a real field log then showed it silently
    failing specifically for larger real data frames (256 bytes): small
    control frames (19-63 bytes) were correctly tagged as self-heard,
    while every 256-byte one was decoded and forwarded to BPQ32 as if
    real. The transmission itself (lead-in + burst + tail) for a frame
    that size takes ~1.96s -- almost the entire 2.0s window consumed
    before the echo is even fully received and decoded, leaving no
    margin at all, and none for carrier sense (v0.5.0) potentially
    adding up to another 4s of wait before the transmission even starts.
    Resized to 10.0s: comfortably above the worst realistic case (a
    maximal coalesced batch at BPSK, the slower modulation, plus the
    full carrier-sense wait, plus lead/tail) which computes to under 8s.
    """

    def __init__(self, window_s: float = 10.0):
        self.window_s = window_s
        self._recent: list[tuple] = []   # [(timestamp, payload), ...]
        # Bridge mode spawns a new thread per transmission (on_air_frame
        # in sara_pingpong.py's _connect), so multiple _bridge_transmit
        # calls can be alive concurrently once BPQ32 sends frames close
        # together -- which real bursts do throughout, not just rarely.
        # Found via a field log: the first several frames in each burst
        # were correctly tagged as self-heard, but consistently the LAST
        # one or two were not -- matching a read-modify-write race on
        # self._recent (one thread's record_sent() appending while
        # another thread's _prune() is mid-rebuild loses that append the
        # moment the rebuild is reassigned). Without this lock, the
        # filter silently becomes less reliable exactly when a burst is
        # busiest -- the opposite of when it matters most.
        self._lock = threading.Lock()

    def record_sent(self, payload: bytes, now: float = None):
        now = time.time() if now is None else now
        with self._lock:
            self._recent.append((now, payload))
            self._prune_locked(now)

    def is_echo(self, payload: bytes, now: float = None) -> bool:
        now = time.time() if now is None else now
        with self._lock:
            self._prune_locked(now)
            return any(p == payload for (_, p) in self._recent)

    def _prune_locked(self, now: float):
        """Caller must hold self._lock."""
        self._recent = [(t, p) for (t, p) in self._recent if now - t < self.window_s]


class App(tk.Tk):
    # v0.9.68: field (0.9.66, DX1FLR): the window froze for good ("Not
    # Responding") right as the KISS client reconnected. With Tcl built for
    # threads (Windows), a Tk call such as after() made from another thread
    # is handed to the window's thread and the caller WAITS for it. The bridge
    # thread did that while holding the Auto-speed lock (the "New connection"
    # log line), and the window's thread was waiting for that same lock to
    # update the speed display: each waited for the other forever. after()
    # from any other thread now just queues the call; the window's own loop
    # runs it (every XQ_POLL_MS).
    XQ_POLL_MS = 40

    def after(self, ms, func=None, *args):
        if func is None or threading.current_thread() is threading.main_thread():
            return super().after(ms, func, *args)
        q = getattr(self, "_xq", None)
        if q is None:
            return super().after(ms, func, *args)
        q.put((time.time() + max(0, int(ms or 0)) / 1000.0, func, args))
        return None

    def _drain_xq(self):
        q = self._xq
        later = []
        now = time.time()
        try:
            while True:
                due, func, args = q.get_nowait()
                if due > now:
                    later.append((due, func, args))
                    continue
                try:
                    func(*args)
                except Exception as e:
                    try:
                        import traceback
                        traceback.print_exc()
                    except Exception:
                        pass
        except queue.Empty:
            pass
        for item in later:
            q.put(item)
        try:
            super().after(self.XQ_POLL_MS, self._drain_xq)
        except tk.TclError:
            pass

    def __init__(self):
        super().__init__()
        self._xq = queue.Queue()          # v0.9.68: calls from other threads, see after()
        super().after(self.XQ_POLL_MS, self._drain_xq)
        self.title(f"SARA FM  ({ofdm.VERSION})")
        self.geometry("880x720")
        self.resizable(True, True)

        self.engine: AudioEngine | None = None
        self.seq_counter = 0
        self.outstanding: dict[int, float] = {}   # seq -> send_time
        self.own_devices = []

        self.batch_running = False     # v0.9.36: Auto-ping removed; kept for the ping guards

        self.bridge: BpqBridge | None = None
        self.self_echo_filter = SelfEchoFilter()
        # Link speed meters (v0.6.2), bridge mode -- see throughput.py for
        # exactly what is counted. Only touched on the GUI thread.
        self.rx_meter = throughput.ThroughputMeter()
        self.retry_stats = session_stats.SessionRetryStats()   # v0.9.30
        self.tx_meter = throughput.ThroughputMeter()
        self._speed_tick = 0
        self.autotune = None           # Auto-Tune state (v0.7.0), None when idle
        self._last_session_activity = 0.0   # last BPQ32 data in either direction (v0.7.3)
        self.ping_win = None                # graphical ping window (v0.7.3)
        # v0.8.8: every log line also goes to a file in logs/ -- v0.9.2: named
        # sara_pp_<CALL>_YYYY-MM-DD_HHMMSS.log (see log_bundle.SessionLog)
        self.session_log = log_bundle.SessionLog(
            app_dir(), ofdm.VERSION,
            call_fn=lambda: self.call_var.get() if hasattr(self, "call_var") else "")
        # v0.9.0 automatic speed selection (see link_adapt.py)
        self.link_stats = link_adapt.RxStats()
        self.speed = link_adapt.SpeedController(on_change=self._on_speed_change)
        self.retx = link_adapt.RetransmitDetector()
        self.stale = link_adapt.StaleFrames()          # v0.9.61
        self._start_freeze_watch()                     # v0.9.67
        self.relay_watch = link_adapt.RelayWatch()     # v0.9.61
        self.via_map = link_adapt.ViaMap()             # v0.9.71
        self.b2watch = b2check.B2Watch()               # v0.9.71
        self.direct_fb = link_adapt.DirectFallback()   # v0.9.72
        self._client_calls = set()                     # v0.9.72: callsigns the KISS client sends as
        self._own_relay_heard = {}                     # v0.9.72: relay_key -> when we heard it repeated
        self._verbose_rx = False
        self._mod_choice = "BPSK"            # thread-safe copy of the Modulation setting
        self._last_tx_peer = None
        self._dcd_busy_since = None         # DCD stuck-busy diagnostic (v0.7.7)
        self._dcd_frames_at_start = 0
        self._dcd_logged = False
        self._frames_seen = 0
        self._carrier_acc = {}              # per-carrier check (v0.8.1)
        self._sent_seqs = collections.deque(maxlen=64)   # (seq, time) of our recent PINGs
        self._dup_call_warned = False
        self._ping_win_seq = None

        self._build_ui()
        # v0.9.38: SARA Chat (chat.py) -- shares the radio with BPQ32
        self.chat = chat.ChatEngine(lambda: self.call_var.get(), self._chat_transmit,
                                    can_send=self._chat_can_send, on_event=self._on_chat_event,
                                    store_path=os.path.join(self._chat_dir(), "sara_chat.json"),
                                    files_dir=os.path.join(self._chat_dir(), "chat_files"))
        self.chat_win = None
        # v0.9.39: chat drives Auto speed like Winlink traffic does
        self.chat.report_fn = self._chat_report
        self.chat.on_report = self._chat_on_report
        self.chat.on_resend = lambda peer: self._auto() and self.speed.on_retransmit(peer)
        self.chat.burst_plan = self._chat_burst_plan              # v0.9.40
        # v0.9.75: SARA Chat through a remote SARA FM station (chat_remote)
        self.chat_server = None
        self.chat_client = chat_remote.ChatClient(
            lambda: self.call_var.get(),
            on_rx=lambda p, snr, mode: self.after(0, lambda: self._on_remote_chat_rx(p, snr, mode)),
            on_heard=lambda c, snr, mode, how: self.after(0, lambda: self.chat.on_heard(c, snr, mode, how)),
            on_status=lambda m: self.after(0, lambda: self._on_remote_chat_status(m)))
        self._chat_remote_on = False
        self._mod_choice = self.modulation_var.get()   # v0.9.33: the trace only fires on change
        self._update_mode_controls()
        self._refresh_devices()
        self._load_settings()
        self._load_speed_memory()      # v0.9.19
        self.after(100, self._poll_engine)

    # ── UI layout ─────────────────────────────────────────────────────
    def _build_ui(self):
        """v0.9.33: VARA FM-style layout. The main window holds what is used
        while operating (target, gauge, lamps, buttons, status, log); setup
        lives in Settings > SARA Setup and Settings > Devices, logs and
        diagnostics under Log, documents under Help. Every control keeps
        its name, so the rest of the program is unchanged."""
        pad = {"padx": 8, "pady": 4}
        self._build_menu()
        self.setup_win = self._make_dialog("SARA Setup")
        self.devices_win = self._make_dialog("Devices")

        # ── SARA Setup dialog ────────────────────────────────────────
        sw = self.setup_win
        top = ttk.LabelFrame(sw, text="Station")
        top.pack(fill="x", **pad)
        ttk.Label(top, text="My callsign:").grid(row=0, column=0, sticky="w", padx=4, pady=3)
        self.call_var = tk.StringVar(value="")
        ttk.Entry(top, textvariable=self.call_var, width=12).grid(row=0, column=1, sticky="w")
        # v0.9.36: a title line shown above the gauges in the Gauges-only view
        ttk.Label(top, text="Gauge title:").grid(row=0, column=2, sticky="w", padx=(16, 4))
        self.gauge_title_var = tk.StringVar(value="")
        ttk.Entry(top, textvariable=self.gauge_title_var, width=30).grid(row=0, column=3, sticky="w")
        ttk.Label(top, text="(shown above the gauges in View > Gauges only)", foreground="#555").grid(
            row=1, column=2, columnspan=2, sticky="w", padx=(16, 4))

        # v0.9.36: always the BPQ32/KISS bridge (the Ping/Pong Test mode is gone;
        # PING and Auto-Tune work while bridged, as in VARA FM)
        self.mode_var = tk.StringVar(value="bpq")

        bridge = ttk.LabelFrame(sw, text="BPQ32 / Winlink Express (KISS TCP)")
        bridge.pack(fill="x", **pad)
        row = ttk.Frame(bridge)
        row.pack(fill="x", padx=4, pady=3)
        ttk.Label(row, text="KISS TCP port:").pack(side="left")
        self.kiss_port_var = tk.StringVar(value=str(bpq_bridge.DEFAULT_PORT))
        ttk.Entry(row, textvariable=self.kiss_port_var, width=8).pack(side="left", padx=4)
        self.bridge_status_var = tk.StringVar(value="Bridge starts with Start.")
        ttk.Label(row, textvariable=self.bridge_status_var).pack(side="left", padx=8)
        row2 = ttk.Frame(bridge)
        row2.pack(fill="x", padx=4, pady=3)
        self.remote_kiss_var = tk.BooleanVar(value=False)
        self.remote_kiss_chk = ttk.Checkbutton(row2, text="Allow network KISS clients",
                                               variable=self.remote_kiss_var)
        self.remote_kiss_chk.pack(side="left")
        ttk.Label(row2, text="   Allow from:").pack(side="left")
        self.kiss_allow_var = tk.StringVar(value="private")
        self.kiss_allow_entry = ttk.Entry(row2, textvariable=self.kiss_allow_var, width=20)
        self.kiss_allow_entry.pack(side="left", padx=4)
        ttk.Label(bridge, text="The bridge runs whenever the modem is started; port and network "
                               "changes take effect at the next Start.", foreground="#555").pack(
            anchor="w", padx=4, pady=(0, 3))
        # v0.9.78: a gateway keeps one set of audio levels for every caller
        self.gateway_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(bridge, text="This station is a gateway: one set of audio levels for every "
                                     "caller (callers tune to it)", variable=self.gateway_var,
                        command=self._on_gateway_change).pack(anchor="w", padx=4, pady=(0, 3))

        # v0.9.75: SARA Chat over the network, like VARA Chat with VARA FM
        rc = ttk.LabelFrame(sw, text="SARA Chat over the network")
        rc.pack(fill="x", **pad)
        row = ttk.Frame(rc)
        row.pack(fill="x", padx=4, pady=3)
        self.chat_serve_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(row, text="Let operators on other computers chat through this station's radio",
                        variable=self.chat_serve_var, command=self._save_settings).pack(side="left")
        ttk.Label(row, text="  port:").pack(side="left")
        self.chat_port_var = tk.StringVar(value=str(chat_remote.DEFAULT_PORT))
        ttk.Entry(row, textvariable=self.chat_port_var, width=7).pack(side="left", padx=4)
        row = ttk.Frame(rc)
        row.pack(fill="x", padx=4, pady=3)
        self.chat_use_remote_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(row, text="Chat through a remote SARA FM station:", variable=self.chat_use_remote_var,
                        command=self._apply_remote_chat).pack(side="left")
        self.chat_remote_host_var = tk.StringVar(value="")
        ttk.Entry(row, textvariable=self.chat_remote_host_var, width=18).pack(side="left", padx=4)
        ttk.Label(row, text="port:").pack(side="left")
        self.chat_remote_port_var = tk.StringVar(value=str(chat_remote.DEFAULT_PORT))
        ttk.Entry(row, textvariable=self.chat_remote_port_var, width=7).pack(side="left", padx=4)
        ttk.Label(rc, text="Station: allowed from the addresses in KISS \"Allow from\" (private = Tailscale, AREDN, "
                           "LAN); starts with Start. Operator: no radio needed -- you chat under this window's "
                           "callsign through that station's radio.", foreground="#555", wraplength=640,
                  justify="left").pack(anchor="w", padx=4, pady=(0, 3))

        link = ttk.LabelFrame(sw, text="Link")
        link.pack(fill="x", **pad)
        row = ttk.Frame(link)
        row.pack(fill="x", padx=4, pady=3)
        ttk.Label(row, text="DCD mode:").pack(side="left")
        self.dcd_mode_var = tk.StringVar(value=self.DCD_LABELS["sara"])   # v0.9.32: field default
        ttk.Combobox(row, textvariable=self.dcd_mode_var, state="readonly", width=22,
                     values=[self.DCD_LABELS[m] for m in audio_io.DCD_MODES]).pack(side="left", padx=4)
        self.dcd_mode_var.trace_add("write", lambda *a: self._apply_dcd_mode())
        # v0.9.18: let Auto climb to the experimental 64QAM Wide
        self.allow64_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(row, text="Auto may use 64QAM (test)", variable=self.allow64_var,
                        command=self._apply_allow64).pack(side="left", padx=(16, 0))
        # v0.9.83: more slack for slow PCs that lose sound-card input
        row = ttk.Frame(link)
        row.pack(fill="x", padx=4, pady=(0, 3))
        self.slow_pc_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(row, text="Slow PC mode", variable=self.slow_pc_var,
                        command=self._on_slow_pc_change).pack(side="left")
        ttk.Label(row, text="  larger audio buffer + lighter window (buffer takes effect at Start)",
                  foreground="#555").pack(side="left")
        row = ttk.Frame(link)
        row.pack(fill="x", padx=4, pady=(0, 3))
        self.no_coded_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(row, text="No C modes", variable=self.no_coded_var,
                        command=self._on_no_coded_change).pack(side="left")
        ttk.Label(row, text="  ask other stations not to send 16QAM / 64QAM Wide C here -- only if Slow PC "
                  "mode is on and 'sound card input lost' still appears", foreground="#555").pack(side="left")

        # v0.9.37: SARA FM's own digipeater
        digi = ttk.LabelFrame(sw, text="Digipeater")
        digi.pack(fill="x", **pad)
        row = ttk.Frame(digi)
        row.pack(fill="x", padx=4, pady=3)
        self.digi_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(row, text="Repeat frames sent via this station", variable=self.digi_var,
                        command=self._on_digi_change).pack(side="left")
        ttk.Label(row, text="   Also answer to alias:").pack(side="left")
        self.digi_alias_var = tk.StringVar(value="")
        ttk.Entry(row, textvariable=self.digi_alias_var, width=14).pack(side="left", padx=4)
        ttk.Label(digi, text="Other stations connect \"<gateway> via <this callsign>\". Leave this off "
                             "if BPQ32 on this PC digipeats (DIGIFLAG=1) -- not both.",
                  foreground="#555").pack(anchor="w", padx=4, pady=(0, 3))
        # v0.9.71: see link_adapt.ViaMap
        self.gw_direct_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(digi, text="Gateway: show callers that come through a digipeater to BPQ32 as "
                                   "direct (SARA FM keeps the path)",
                        variable=self.gw_direct_var, command=self._save_settings).pack(anchor="w", padx=4, pady=(0, 3))
        ttk.Button(sw, text="Close", command=lambda: self._close_dialog(sw)).pack(anchor="e", **pad)

        # ── Devices dialog ───────────────────────────────────────────
        dw = self.devices_win
        dev = ttk.LabelFrame(dw, text="Sound card and PTT")
        dev.pack(fill="x", **pad)
        ttk.Label(dev, text="Input device:").grid(row=0, column=0, sticky="w", padx=4, pady=2)
        self.in_dev_var = tk.StringVar()
        self.in_dev_combo = ttk.Combobox(dev, textvariable=self.in_dev_var, width=44, state="readonly")
        self.in_dev_combo.grid(row=0, column=1, columnspan=2, sticky="w")
        ttk.Label(dev, text="Output device:").grid(row=1, column=0, sticky="w", padx=4, pady=2)
        self.out_dev_var = tk.StringVar()
        self.out_dev_combo = ttk.Combobox(dev, textvariable=self.out_dev_var, width=44, state="readonly")
        self.out_dev_combo.grid(row=1, column=1, columnspan=2, sticky="w")
        ttk.Label(dev, text="PTT:").grid(row=2, column=0, sticky="w", padx=4, pady=2)
        self.ptt_var = tk.StringVar(value=PTT_VOX)
        ptt_combo = ttk.Combobox(dev, textvariable=self.ptt_var, width=12, state="readonly",
                                 values=[PTT_VOX, PTT_CM108, PTT_RTS, PTT_DTR, PTT_PIGPIO])
        ptt_combo.grid(row=2, column=1, sticky="w")
        ptt_combo.bind("<<ComboboxSelected>>", lambda e: self._on_ptt_method_change())
        ttk.Label(dev, text="RA-Board:").grid(row=3, column=0, sticky="w", padx=4, pady=2)
        self.ra_board_var = tk.StringVar()
        self.ra_board_combo = ttk.Combobox(dev, textvariable=self.ra_board_var, width=30, state="disabled")
        self.ra_board_combo.grid(row=3, column=1, sticky="w")
        self._ra_boards: list[tuple[str, object, str]] = []
        ttk.Button(dev, text="Detect", command=self._refresh_ra_boards).grid(row=3, column=2, sticky="w", padx=4)
        # GPIO pin varies by board -- GPIO3 (0x04) on the RA-Board / DX1PRD interface.
        ttk.Label(dev, text="GPIO pin:").grid(row=4, column=0, sticky="w", padx=4, pady=2)
        self.gpio_pin_var = tk.StringVar(value="GPIO3 (0x04)")
        self.gpio_pin_combo = ttk.Combobox(dev, textvariable=self.gpio_pin_var, width=20, state="disabled",
                                           values=["GPIO4 (0x08)", "GPIO3 (0x04)", "GPIO2 (0x02)", "GPIO1 (0x01)"])
        self.gpio_pin_combo.grid(row=4, column=1, sticky="w")
        ttk.Button(dev, text="Test PTT (2s)", command=self._test_ptt).grid(row=4, column=2, sticky="w", padx=4)
        # v0.9.81: the serial port for RTS / DTR keying (e.g. a Digirig: COM5 on
        # Windows, /dev/ttyUSB0 on Linux) -- it was never passed on before, so
        # RTS / DTR could not key anything -- and the Raspberry Pi GPIO pin.
        ttk.Label(dev, text="Serial port:").grid(row=5, column=0, sticky="w", padx=4, pady=2)
        self.serial_port_var = tk.StringVar(value="")
        self.serial_port_combo = ttk.Combobox(dev, textvariable=self.serial_port_var, width=20,
                                              postcommand=self._refresh_serial_ports)
        self.serial_port_combo.grid(row=5, column=1, sticky="w")
        ttk.Label(dev, text="for RTS / DTR", foreground="#555").grid(row=5, column=2, sticky="w", padx=4)
        ttk.Label(dev, text="Pi GPIO pin:").grid(row=6, column=0, sticky="w", padx=4, pady=2)
        self.pi_gpio_var = tk.StringVar(value=str(PI_GPIO_DEFAULT_PIN))
        ttk.Entry(dev, textvariable=self.pi_gpio_var, width=6).grid(row=6, column=1, sticky="w")
        ttk.Label(dev, text="BCM number, for PTT = Pi GPIO", foreground="#555").grid(
            row=6, column=2, sticky="w", padx=4)

        lv = ttk.LabelFrame(dw, text="Levels and timing")
        lv.pack(fill="x", **pad)
        # TX delay: fill tone before the burst so the radio has keyed up.
        ttk.Label(lv, text="TX delay (ms):").grid(row=0, column=0, sticky="w", padx=4, pady=2)
        self.tx_delay_var = tk.StringVar(value=str(audio_io.DEFAULT_TX_LEAD_MS))
        tx_delay_entry = ttk.Entry(lv, textvariable=self.tx_delay_var, width=8)
        tx_delay_entry.grid(row=0, column=1, sticky="w")
        tx_delay_entry.bind("<FocusOut>", lambda e: self._apply_tx_delay())
        tx_delay_entry.bind("<Return>", lambda e: self._apply_tx_delay())
        ttk.Label(lv, text="600 for the DX1PRD interface; raise it if the radio clips the start").grid(
            row=0, column=2, sticky="w", padx=8)
        # Per-program audio levels (v0.6.5) -- for sharing a sound card with VARA FM.
        ttk.Label(lv, text="RX gain (dB):").grid(row=1, column=0, sticky="w", padx=4, pady=2)
        self.rx_gain_db_var = tk.StringVar(value="0")
        ttk.Spinbox(lv, from_=-20, to=40, increment=1, width=6,
                    textvariable=self.rx_gain_db_var).grid(row=1, column=1, sticky="w")
        ttk.Label(lv, text=f"Auto-Tune sets this; frames should land near {tuning.TARGET_LEVEL_DBFS:.0f} dBFS "
                           f"(leave the Windows level set for VARA)").grid(row=1, column=2, sticky="w", padx=8)
        ttk.Label(lv, text="TX level (%):").grid(row=2, column=0, sticky="w", padx=4, pady=2)
        self.tx_level_var = tk.StringVar(value=str(int(audio_io.DEFAULT_TX_LEVEL * 100)))
        ttk.Spinbox(lv, from_=5, to=100, increment=5, width=6,
                    textvariable=self.tx_level_var).grid(row=2, column=1, sticky="w")
        ttk.Label(lv, text="Auto-Tune (Tune TX) sets this from the other station's report").grid(
            row=2, column=2, sticky="w", padx=8)
        self.rx_gain_db_var.trace_add("write", lambda *a: self._apply_levels())
        self.tx_level_var.trace_add("write", lambda *a: self._apply_levels())

        # RX level bar (v0.7.0) -- here for setting levels while watching it
        meter_frame = ttk.LabelFrame(dw, text="RX level now")
        meter_frame.pack(fill="x", **pad)
        self.meter = tk.Canvas(meter_frame, width=self.METER_W, height=16, bg="black", highlightthickness=0)
        self.meter.pack(side="left", padx=8, pady=4)
        lo = self._meter_x(tuning.TARGET_LEVEL_DBFS - tuning.TARGET_WINDOW_DB)
        hi = self._meter_x(tuning.TARGET_LEVEL_DBFS + tuning.TARGET_WINDOW_DB)
        self.meter.create_rectangle(lo, 0, hi, 16, fill="#123d12", outline="")
        self.meter.create_rectangle(self._meter_x(-3), 0, self.METER_W, 16, fill="#4a1010", outline="")
        self.meter_bar = self.meter.create_rectangle(0, 4, 0, 12, fill="green", outline="")
        self.rx_meter_var = tk.StringVar(value="")
        ttk.Label(meter_frame, textvariable=self.rx_meter_var, width=42).pack(side="left")
        ttk.Button(dw, text="Close", command=lambda: self._close_dialog(dw)).pack(anchor="e", **pad)

        # ── Main window ──────────────────────────────────────────────
        # v0.9.34: two views, like VARA FM. "Gauges only" is a short strip
        # (meters, lamps, status bar) that sits above or below VARA FM's own
        # window; "Full" adds the target row, buttons, Auto-Tune bar and log.
        self._full_only = []            # (widget, pack options), in screen order
        head = ttk.Frame(self)
        # v0.9.32: Target is a list of stations (callsign + location)
        ttk.Label(head, text="Target:").grid(row=0, column=0, sticky="w")
        self.target_var = tk.StringVar(value="*")
        self.targets: list = [[c, "", None] for c in self.DEFAULT_TARGETS]
        self.target_combo = ttk.Combobox(head, textvariable=self.target_var, width=14)
        self.target_combo.grid(row=0, column=1, sticky="w", padx=(4, 0))
        self.target_combo.bind("<<ComboboxSelected>>", lambda e: self._on_target_selected())
        self.target_var.trace_add("write", lambda *a: self._show_target_location())
        self.target_loc_var = tk.StringVar(value="")
        ttk.Label(head, text="Location:").grid(row=0, column=2, sticky="w", padx=(12, 0))
        ttk.Entry(head, textvariable=self.target_loc_var, width=22).grid(row=0, column=3, sticky="w", padx=4)
        ttk.Button(head, text="Save", width=6, command=self._save_target).grid(row=0, column=4, padx=(0, 2))
        ttk.Button(head, text="Remove", width=8, command=self._remove_target).grid(row=0, column=5)
        ttk.Label(head, text="Modulation:").grid(row=0, column=6, sticky="w", padx=(16, 0))
        self.modulation_var = tk.StringVar(value="Auto")      # v0.9.32: default Auto
        ttk.Combobox(head, textvariable=self.modulation_var, width=12, state="readonly",
                     values=["Auto", "BPSK", "QPSK", "QPSK Wide", "16QAM Wide C", "16QAM Wide",
                             "64QAM Wide C", "64QAM Wide"]).grid(row=0, column=7, sticky="w", padx=4)
        self.modulation_var.trace_add("write", lambda *a: setattr(self, "_mod_choice", self.modulation_var.get()))
        # v0.9.35: this station's saved audio levels (Auto-Tune saves them)
        self.target_prof_var = tk.StringVar(value="")
        ttk.Label(head, textvariable=self.target_prof_var, foreground="#555").grid(
            row=1, column=1, columnspan=7, sticky="w", padx=(4, 0), pady=(2, 0))
        self._refresh_target_list()
        self._full_only.append((head, dict(fill="x", **pad)))

        # gauge row (both views)
        self.gauge_row = ttk.Frame(self)
        self.gauge_row.pack(fill="x", padx=6, pady=(4, 2))
        # v0.9.36: title line above the gauges (Gauges-only view; SARA Setup sets it)
        self.gauge_title_lbl = tk.Label(self, textvariable=self.gauge_title_var,
                                        font=("TkDefaultFont", 13, "bold"), anchor="center")
        self.gauge_title_var.trace_add("write", lambda *a: self._apply_gauge_title())
        G = gauges.AnalogMeter
        self.gauge = G(self.gauge_row, width=210, height=140, target_db=tuning.TARGET_LEVEL_DBFS,
                       target_window_db=tuning.TARGET_WINDOW_DB, title="RX LEVEL", unit="dBFS",
                       caption="Last frame: ---")
        self.gauge.pack(side="left", padx=(0, 4))
        self.snr_gauge = G(self.gauge_row, width=210, height=140, lo_db=0, hi_db=40, title="S/N",
                           unit="dB", majors=(0, 10, 20, 30, 40),
                           zones=[(0, 8, G.RED), (8, 12, G.AMBER), (12, 40, G.GREEN)],
                           caption="S/N ---")
        self.snr_gauge.pack(side="left", padx=4)
        self.speed_gauge = G(self.gauge_row, width=210, height=140, lo_db=0, hi_db=3000, title="SPEED",
                             unit="bps", majors=(0, 500, 1000, 1500, 2000, 2500, 3000),
                             zones=[], label_fmt=lambda v: f"{v / 1000:g}k" if v else "0",
                             caption="idle")
        self.speed_gauge.pack(side="left", padx=4)
        lamps = ttk.Frame(self.gauge_row)
        lamps.pack(side="left", fill="y", padx=(8, 0))
        self.tx_lamp = gauges.Lamp(lamps, "TX", "#e03131")
        self.tx_lamp.pack(anchor="w", pady=2)
        self.rx_lamp = gauges.Lamp(lamps, "RX", "#2f9e44")
        self.rx_lamp.pack(anchor="w", pady=2)
        dcd_l = gauges.Lamp(lamps, "DCD", "#3c3")
        dcd_l.pack(anchor="w", pady=2)
        # DCD (v0.7.7): the existing code colours self._dcd_dot on self.dcd_lamp
        self.dcd_lamp, self._dcd_dot = dcd_l, dcd_l._dot
        self.tcp_lamp = gauges.Lamp(lamps, "TCP", "#3c3")      # a KISS client is connected
        self.tcp_lamp.pack(anchor="w", pady=2)
        self.dcd_override_btn = ttk.Button(lamps, text="TX anyway", width=10, command=self._dcd_override,
                                           state="disabled")
        self.dcd_override_btn.pack(anchor="w", pady=(4, 0))

        info = ttk.Frame(self)
        self.signal_var = tk.StringVar(value="Last frame: (none yet)")
        ttk.Label(info, textvariable=self.signal_var).pack(side="left")
        self.clip_var = tk.StringVar(value="")
        ttk.Label(info, textvariable=self.clip_var, foreground="red").pack(side="left", padx=8)
        self._full_only.append((info, dict(fill="x", padx=8, pady=(0, 2))))
        self.snr_big_var = tk.StringVar(value="")     # (v0.9.33 big S/N -- now the S/N gauge)

        speed = ttk.Frame(self)
        self.speed_var = tk.StringVar(value="Speed: (starts when a KISS client connects)")
        ttk.Label(speed, textvariable=self.speed_var, font=("TkDefaultFont", 10, "bold")).pack(side="left")
        self._full_only.append((speed, dict(fill="x", **pad)))

        ctrl = ttk.Frame(self)
        self.connect_btn = ttk.Button(ctrl, text="Start", command=self._toggle_connect)
        self.connect_btn.pack(side="left")
        self.ping_btn = ttk.Button(ctrl, text="PING", command=self._send_ping, state="disabled")
        self.ping_btn.pack(side="left", padx=8)
        # v0.9.78: ONE Tune button (RX gain, then TX level) ending in one
        # verdict, like VARA FM's drive calibration. "Tune TX level only" is
        # in the Settings menu.
        self.autotune_btn = ttk.Button(ctrl, text="Tune", command=self._start_autotune, state="disabled")
        self.autotune_btn.pack(side="left", padx=(0, 8))
        # v0.9.14: find the loudest TX level the other station still receives clean
        self.txtune_btn = ttk.Button(ctrl, text="Tune TX", command=self._start_txtune, state="disabled")
        self.tune_btn = ttk.Button(ctrl, text="TUNE 10s", command=self._start_tune, state="disabled")
        self.tune_btn.pack(side="left", padx=(0, 8))
        self.chat_btn = ttk.Button(ctrl, text="Chat", command=self.open_chat)     # v0.9.38
        self.chat_btn.pack(side="right")
        self._full_only.append((ctrl, dict(fill="x", **pad)))

        # v0.9.32: Auto-Tune / Tune TX status in large type
        self.tune_status_var = tk.StringVar(value="")
        try:
            import tkinter.font as tkfont
            big = tkfont.nametofont("TkDefaultFont").copy()
            big.configure(size=14, weight="bold")
        except Exception:
            big = ("Segoe UI", 14, "bold")
        self._tune_font = big
        self.tune_status_lbl = tk.Label(self, textvariable=self.tune_status_var, anchor="w",
                                        font=big, padx=10, pady=6, justify="left", wraplength=880,
                                        bg=self.TUNE_COLORS["idle"][0], fg=self.TUNE_COLORS["idle"][1])
        self._full_only.append((self.tune_status_lbl, dict(fill="x", padx=8, pady=(0, 4))))
        self._set_tune_status("idle", "Tune: not run yet -- pick a Target and press Tune.")

        self.verbose_rx_var = tk.BooleanVar(value=False)     # Log menu
        self.log_box = scrolledtext.ScrolledText(self, height=12, state="disabled", font=("Consolas", 9))
        self._full_only.append((self.log_box, dict(fill="both", expand=True, **pad)))

        # status bar (both views), like VARA FM's bottom line
        sb = tk.Frame(self, bd=1, relief="sunken")
        sb.pack(side="bottom", fill="x", before=self.gauge_row)   # packed first: always visible
        self.status_bar = sb
        self.status_var = tk.StringVar(value="Not started")
        tk.Label(sb, textvariable=self.status_var, anchor="w", width=18).pack(side="left", padx=(6, 0))
        ttk.Separator(sb, orient="vertical").pack(side="left", fill="y", padx=4)
        self.dcd_text_var = tk.StringVar(value="(audio not started)")
        tk.Label(sb, textvariable=self.dcd_text_var, anchor="w", width=24).pack(side="left")
        ttk.Separator(sb, orient="vertical").pack(side="left", fill="y", padx=4)
        self.sb_tune_var = tk.StringVar(value="")
        tk.Label(sb, textvariable=self.sb_tune_var, anchor="w", width=1).pack(side="left", fill="x", expand=True)
        self.sb_mode_var = tk.StringVar(value="")
        tk.Label(sb, textvariable=self.sb_mode_var, anchor="e").pack(side="right", padx=6)
        # v0.9.44: CPU meter (like VARA FM's), in both views
        ttk.Separator(sb, orient="vertical").pack(side="right", fill="y", padx=4)
        self.cpu_bar = gauges.CpuBar(sb)
        self.cpu_bar.pack(side="right")
        self._view = "full"
        self._apply_view("full", resize=False)

    # ── menus and dialogs (v0.9.33) ──────────────────────────────────
    def _build_menu(self):
        mb = tk.Menu(self)
        st = tk.Menu(mb, tearoff=0)
        st.add_command(label="SARA Setup...", command=lambda: self._show_dialog(self.setup_win))
        st.add_command(label="Devices...", command=lambda: self._show_dialog(self.devices_win))
        st.add_command(label="Tune TX level only", command=self._start_txtune)     # v0.9.78
        mb.add_cascade(label="Settings", menu=st)
        vw = tk.Menu(mb, tearoff=0)
        self.view_var = tk.StringVar(value="full")
        vw.add_radiobutton(label="Full (gauges, buttons and log)", value="full", variable=self.view_var,
                           command=lambda: self._apply_view("full"))
        vw.add_radiobutton(label="Gauges only (to sit next to VARA FM)", value="gauges",
                           variable=self.view_var, command=lambda: self._apply_view("gauges"))
        mb.add_cascade(label="View", menu=vw)
        lg = tk.Menu(mb, tearoff=0)
        lg.add_command(label="Save Report (one file for a test run)", command=self._save_report)
        lg.add_command(label="Save Logs (zip of everything)", command=self._save_logs)
        lg.add_command(label="Open log folder", command=self._open_log_folder)
        lg.add_command(label="Record received audio (60 s)", command=self._start_rx_recording)   # v0.9.47
        lg.add_separator()
        lg.add_checkbutton(label="Verbose RX diagnostics", command=self._toggle_verbose_menu,
                           variable=tk.BooleanVar(value=False))
        lg.add_command(label="Clear window", command=self._clear_log_window)
        mb.add_cascade(label="Log", menu=lg)
        self._log_menu = lg
        ch = tk.Menu(mb, tearoff=0)                                  # v0.9.38
        ch.add_command(label="Open SARA Chat", command=self.open_chat)
        ch.add_command(label="Call CQ on the NET", command=lambda: self._chat_net("cq"))
        ch.add_command(label="Check in to the NET", command=lambda: self._chat_net("checkin"))
        ch.add_separator()
        ch.add_command(label="Open received files folder",
                       command=lambda: chat_window._open_path(self._chat_files_dir()))
        mb.add_cascade(label="Chat", menu=ch)
        hp = tk.Menu(mb, tearoff=0)
        hp.add_command(label="Quick Start (gateway)", command=lambda: self._open_doc("QUICKSTART_GATEWAY.txt"))
        hp.add_command(label="README", command=lambda: self._open_doc("README.md"))
        hp.add_command(label="Version history", command=lambda: self._open_doc("HISTORY.md"))
        hp.add_separator()
        hp.add_command(label="About SARA FM", command=self._about)
        mb.add_cascade(label="Help", menu=hp)
        self.config(menu=mb)

    def _apply_gauge_title(self):
        """v0.9.36: show the title line above the gauges in the Gauges-only
        view when one is set; hide it otherwise."""
        lbl = getattr(self, "gauge_title_lbl", None)
        if lbl is None:
            return
        want = getattr(self, "_view", "full") == "gauges" and bool(self.gauge_title_var.get().strip())
        if want and lbl.winfo_manager() != "pack":
            lbl.pack(before=self.gauge_row, fill="x", padx=6, pady=(4, 0))
        elif not want:
            lbl.pack_forget()

    def _apply_view(self, view: str, resize: bool = True):
        """v0.9.34: 'full' or 'gauges' (only the meter strip and status bar)."""
        for w, _ in self._full_only:
            w.pack_forget()
        if view == "full":
            prev = None
            for w, opts in self._full_only:
                if prev is None:                   # the target row sits above the gauges
                    w.pack(before=self.gauge_row, **opts)
                    prev = self.gauge_row
                else:
                    w.pack(after=prev, **opts)
                    prev = w
        self._view = view
        self._apply_gauge_title()
        if hasattr(self, "view_var"):
            self.view_var.set(view)
        if resize:
            self.update_idletasks()
            if view == "gauges":
                self._full_geometry = self.geometry().split("+")[0]
                self.geometry("")             # natural size of the strip (menu included)
            else:
                self.geometry(getattr(self, "_full_geometry", "880x720"))
            self.resizable(True, view == "full")

    def _make_dialog(self, title: str) -> tk.Toplevel:
        w = tk.Toplevel(self)
        w.title(f"SARA FM -- {title}")
        w.withdraw()
        w.resizable(False, False)
        w.protocol("WM_DELETE_WINDOW", lambda: self._close_dialog(w))
        return w

    def _show_dialog(self, w: tk.Toplevel):
        w.deiconify()
        w.lift()
        w.focus_set()

    def _close_dialog(self, w: tk.Toplevel):
        w.withdraw()
        try:
            self._apply_tx_delay()
            self._save_settings()
        except Exception:
            pass

    def _toggle_verbose_menu(self):
        self.verbose_rx_var.set(not self.verbose_rx_var.get())
        self._on_verbose_rx_change()

    def _clear_log_window(self):
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")

    def _open_path(self, path: str):
        try:
            if sys.platform == "win32":
                os.startfile(path)            # noqa -- Windows only
            elif sys.platform == "darwin":
                os.system(f'open "{path}"')
            else:
                os.system(f'xdg-open "{path}" >/dev/null 2>&1 &')
        except Exception as e:
            self._log(f"Could not open {path}: {e}")

    def _open_log_folder(self):
        folder = os.path.join(app_dir(), "logs")
        os.makedirs(folder, exist_ok=True)
        self._open_path(folder)

    def _open_doc(self, name: str):
        for base in (app_dir(), os.path.dirname(os.path.abspath(__file__)),
                     getattr(sys, "_MEIPASS", "")):
            p = os.path.join(base, name) if base else ""
            if p and os.path.exists(p):
                self._open_path(p)
                return
        messagebox.showinfo("SARA FM", f"{name} was not found next to the program.")

    def _about(self):
        messagebox.showinfo("About SARA FM",
                            f"SARA FM  ({ofdm.VERSION})\n\nOFDM FM modem for Winlink over BPQ32 / "
                            f"Winlink Express (KISS TCP).\nPAREDN -- Philippine Amateur Radio "
                            f"Emergency Data Network.\n\n"
                            f"Copyright (c) 2026 PAREDN / DW1IRF. Free software under the GNU "
                            f"General Public License v3 or later (see LICENSE.txt); it comes "
                            f"with NO WARRANTY. The on-air protocol is documented in the "
                            f"SARA FM Protocol Specification (CC BY 4.0).")


    # ── Target list (v0.9.32) + saved audio levels per station (v0.9.35) ──
    # Each entry is [callsign, location, levels or None]; levels is
    # {"rx_gain_db": int, "tx_level_pct": int, "saved": "YYYY-MM-DD"}.
    # Auto-Tune / Tune TX save the levels they found for their Target;
    # picking a station from the list (or a new Winlink connection with it)
    # puts those levels back.
    DEFAULT_TARGETS = ("DV1UK", "DX1CW", "DX1FLR", "DX1PAR")

    @staticmethod
    def _base_call(call: str) -> str:
        return call.strip().upper().split("-", 1)[0]

    @staticmethod
    def _target_display(call: str, loc: str, prof: dict | None = None) -> str:
        s = f"{call}  --  {loc}" if loc else call
        if prof:
            s += f"   [RX {int(prof.get('rx_gain_db', 0)):+d} dB, TX {int(prof.get('tx_level_pct', 0))}%]"
        return s

    def _norm_targets(self):
        for t in self.targets:
            while len(t) < 3:
                t.append("" if len(t) == 1 else None)

    def _refresh_target_list(self):
        self._norm_targets()
        self.target_combo["values"] = [self._target_display(c, l, p) for c, l, p in self.targets]
        self._show_target_location()

    def _find_target(self, call: str, base_ok: bool = False):
        call = call.strip().upper()
        for t in self.targets:
            if t[0].upper() == call:
                return t
        if base_ok:
            b = self._base_call(call)
            for t in self.targets:
                if self._base_call(t[0]) == b:
                    return t
        return None

    def _target_lookup(self, call: str) -> str | None:
        t = self._find_target(call)
        return t[1] if t else None

    def _is_gateway(self) -> bool:
        v = getattr(self, "gateway_var", None)
        try:
            return bool(v is not None and v.get())
        except Exception:
            return False

    def _on_gateway_change(self):
        if self._is_gateway():
            p = self._current_profile()
            self._log(f"Gateway: one set of audio levels for every caller -- RX gain {p['rx_gain_db']:+d} dB, "
                      f"TX level {p['tx_level_pct']}%. Callers tune to this station and save their own "
                      f"levels for it; this station no longer switches levels per caller.")
        else:
            self._log("Gateway levels off: saved levels per station are used again.")
        self._save_settings()

    def _on_target_selected(self):
        call = self.target_var.get().split("  --  ", 1)[0].split("   [", 1)[0].strip()
        self.target_var.set(call)
        t = self._find_target(call)
        if t and t[2] and not self._is_gateway():       # v0.9.78: a gateway keeps its levels
            self._apply_profile(call, t[2], why="selected")

    def _show_target_location(self):
        t = self._find_target(self.target_var.get())
        if t is not None:
            self.target_loc_var.set(t[1])
        pv = getattr(self, "target_prof_var", None)
        if pv is not None:
            p = t[2] if t is not None and len(t) > 2 else None
            pv.set(f"Saved levels: RX {int(p['rx_gain_db']):+d} dB, TX {int(p['tx_level_pct'])}%"
                   f"  ({p.get('saved', '')})" if p else "No saved levels for this station yet")

    def _current_profile(self) -> dict:
        try:
            rx = int(round(float(self.rx_gain_db_var.get())))
        except ValueError:
            rx = 0
        try:
            tx = int(round(float(self.tx_level_var.get())))
        except ValueError:
            tx = int(audio_io.DEFAULT_TX_LEVEL * 100)
        return {"rx_gain_db": rx, "tx_level_pct": tx, "saved": time.strftime("%Y-%m-%d")}

    def _store_profile(self, call: str, why: str = "Auto-Tune"):
        """Remember the current RX gain / TX level for this station."""
        call = call.split("  --  ", 1)[0].strip().upper()
        if not call or call == "*":
            return
        t = self._find_target(call)
        if t is None:
            t = [call, "", None]
            self.targets.append(t)
            self.targets.sort(key=lambda x: x[0])
        self._norm_targets()
        t[2] = self._current_profile()
        getattr(self, "_pong_level", {}).pop(call, None)   # v0.9.50: tuned now -- no reply override
        self._refresh_target_list()
        self._save_settings()
        self._log(f"Levels saved for {call} ({why}): RX gain {t[2]['rx_gain_db']:+d} dB, "
                  f"TX level {t[2]['tx_level_pct']}%")

    def _apply_profile(self, call: str, prof: dict, why: str):
        rx, tx = int(prof.get("rx_gain_db", 0)), int(prof.get("tx_level_pct", 40))
        cur = self._current_profile()
        if cur["rx_gain_db"] == rx and cur["tx_level_pct"] == tx:
            return
        self.rx_gain_db_var.set(str(rx))
        self.tx_level_var.set(str(tx))
        self._log(f"Levels for {call} ({why}): RX gain {cur['rx_gain_db']:+d} -> {rx:+d} dB, "
                  f"TX level {cur['tx_level_pct']} -> {tx}%  (saved {prof.get('saved', '?')})")

    def _apply_profile_for_peer(self, peer: str, incoming: bool = True):
        """New Winlink connection: use that station's saved levels, if any.
        v0.9.87: a gateway keeps one set of levels for its CALLERS only; when
        this station makes the call itself it uses the levels saved for the
        station it calls, and goes back to its gateway levels afterwards."""
        gw = self._is_gateway()
        if gw and incoming:
            self._restore_gateway_levels(why=f"{peer} calling in")
            return          # v0.9.78: the same levels for every caller
        t = self._find_target(peer, base_ok=True)
        prof = t[2] if t and t[2] else None
        src = ""
        if prof is None:
            prof, src = self._levels_from_other_installs(peer)
            if prof is not None:
                if t is None:
                    t = [peer.upper(), "", None]
                    self.targets.append(t)
                    self.targets.sort(key=lambda x: x[0])
                t[2] = dict(prof)
                self._refresh_target_list()
                self._save_settings()
                self._log(f"Saved levels for {peer} copied from your other SARA FM folder {src} "
                          f"(same sound card): RX gain {int(prof['rx_gain_db']):+d} dB, "
                          f"TX level {int(prof['tx_level_pct'])}%")
        if prof is None:
            cur = self._current_profile()
            self._log(f"No saved levels for {peer} on this PC -- using the current ones "
                      f"(RX gain {cur['rx_gain_db']:+d} dB, TX level {cur['tx_level_pct']}%). "
                      f"If the speed stays low, pick {self._base_call(peer)} as Target and run "
                      f"Auto-Tune once; the levels are then used every time you connect.")
            return
        if gw and getattr(self, "_gw_levels", None) is None:
            self._gw_levels = self._current_profile()
        self._apply_profile(peer, prof, why="new connection" + (", called from this gateway" if gw else ""))

    def _restore_gateway_levels(self, why: str):
        """v0.9.87: back to the gateway's own levels after it called out."""
        prof = getattr(self, "_gw_levels", None)
        self._gw_levels = None
        if prof is not None and self._is_gateway():
            self._apply_profile("gateway", prof, why=why)

    def _levels_from_other_installs(self, peer: str):
        """v0.9.87: each new version is usually unzipped into a new folder with
        its own settings file. When this one has no saved levels for `peer`,
        look in the other SARA FM folders next to it (same sound card only)."""
        try:
            here = os.path.normcase(os.path.abspath(self._settings_path()))
            mine_in = self._dev_name(self.in_dev_var.get())
            mine_out = self._dev_name(self.out_dev_var.get())
        except Exception:
            return None, ""
        base = os.path.dirname(app_dir())
        roots = [base, os.path.dirname(base)]
        found = []
        for r in roots:
            for pat in ("*", os.path.join("*", "*")):
                try:
                    found += glob.glob(os.path.join(r, pat, self._SETTINGS_FILE))
                except Exception:
                    pass
        best = None
        for f in set(os.path.normcase(os.path.abspath(x)) for x in found):
            if f == here:
                continue
            try:
                with open(f) as fh:
                    st = json.load(fh)
                mt = os.path.getmtime(f)
            except (OSError, ValueError):
                continue
            if (st.get("input_device", "") or "") != mine_in or (st.get("output_device", "") or "") != mine_out:
                continue
            b = self._base_call(peer)
            for t in st.get("targets") or []:
                if (isinstance(t, (list, tuple)) and len(t) > 2 and isinstance(t[2], dict)
                        and self._base_call(str(t[0])) == b
                        and "rx_gain_db" in t[2] and "tx_level_pct" in t[2]):
                    if best is None or mt > best[0]:
                        best = (mt, t[2], os.path.basename(os.path.dirname(os.path.dirname(f)))
                                if os.path.basename(os.path.dirname(f)).lower() == "sara-fm"
                                else os.path.basename(os.path.dirname(f)))
        if best is None:
            return None, ""
        return best[1], best[2]

    def _save_target(self):
        call = self.target_var.get().split("  --  ", 1)[0].split("   [", 1)[0].strip().upper()
        if not call or call == "*":
            return
        loc = self.target_loc_var.get().strip()
        t = self._find_target(call)
        if t is None:
            self.targets.append([call, loc, None])
        else:
            t[1] = loc
        self.targets.sort(key=lambda x: x[0])
        self.target_var.set(call)
        self._store_profile(call, why="Save")     # also refreshes the list and saves settings
        self._log(f"Target list: saved {self._target_display(call, loc)}")

    def _remove_target(self):
        call = self.target_var.get().split("  --  ", 1)[0].split("   [", 1)[0].strip().upper()
        before = len(self.targets)
        self.targets = [t for t in self.targets if t[0].upper() != call]
        if len(self.targets) != before:
            self._refresh_target_list()
            self.target_loc_var.set("")
            self._save_settings()
            self._log(f"Target list: removed {call}")

    # ── Auto-Tune status banner (v0.9.32) ────────────────────────────
    TUNE_COLORS = {"idle": ("#e6e6e6", "#333333"), "running": ("#f2c94c", "#1a1a1a"),
                   "warn": ("#f2994a", "#1a1a1a"),
                   "done": ("#2e8b57", "#ffffff"), "failed": ("#c0392b", "#ffffff")}

    def _set_tune_status(self, state: str, text: str):
        lbl = getattr(self, "tune_status_lbl", None)
        if lbl is None:
            return
        if getattr(self, "sb_tune_var", None) is not None:
            self.sb_tune_var.set("" if state == "idle" else text.replace("  --  ", ": "))
        bg, fg = self.TUNE_COLORS.get(state, self.TUNE_COLORS["idle"])
        self.tune_status_var.set(text)
        lbl.configure(bg=bg, fg=fg)

    def _refresh_devices(self):
        try:
            self._all_devices = list_devices()
            devices = list_devices(all_apis=False) or self._all_devices
        except Exception as e:
            self._log(f"Could not list audio devices: {e}")
            return
        in_list = [f"{i}: {name}" for i, name, ic, oc, rate in devices if ic > 0]
        out_list = [f"{i}: {name}" for i, name, ic, oc, rate in devices if oc > 0]
        self.in_dev_combo["values"] = in_list
        self.out_dev_combo["values"] = out_list
        if in_list:
            self.in_dev_combo.current(0)
        if out_list:
            self.out_dev_combo.current(0)

    def _on_ptt_method_change(self):
        if self.ptt_var.get() == PTT_CM108:
            self.ra_board_combo.configure(state="readonly")
            self.gpio_pin_combo.configure(state="readonly")
            self._refresh_ra_boards()
        else:
            self.ra_board_combo.configure(state="disabled")
            self.gpio_pin_combo.configure(state="disabled")

    def _refresh_serial_ports(self):
        try:
            self.serial_port_combo["values"] = PTTController.list_serial_ports()
        except Exception:
            pass

    def _serial_ptt_kwargs(self, method: str) -> dict | None:
        """v0.9.81: PTT settings for the RTS / DTR / Pi GPIO methods, or None
        (after telling the user) if one is missing."""
        if method in (PTT_RTS, PTT_DTR):
            port = self.serial_port_var.get().strip()
            if not port:
                messagebox.showwarning("Serial port", "Pick the serial port for RTS / DTR PTT "
                                       "(Settings > Devices > Serial port).")
                return None
            return {"port": port}
        if method == PTT_PIGPIO:
            try:
                return {"pi_gpio_pin": int(self.pi_gpio_var.get().strip())}
            except ValueError:
                messagebox.showwarning("Pi GPIO pin", "Enter the GPIO pin number (BCM), e.g. 17.")
                return None
        return {}

    def _refresh_ra_boards(self):
        try:
            self._ra_boards = enumerate_ra_boards()
        except Exception as e:
            self._log(f"RA-Board detection failed: {e}")
            self._ra_boards = []
        if self._ra_boards:
            self.ra_board_combo["values"] = [f"{name} ({chip})" for name, path, chip in self._ra_boards]
            self.ra_board_combo.current(0)
            self._log(f"Detected {len(self._ra_boards)} RA-Board(s): "
                       + ", ".join(n for n, p, c in self._ra_boards))
        else:
            self.ra_board_combo["values"] = []
            self.ra_board_var.set("")
            hint = ("" if getattr(sys, "frozen", False)
                    else ", or 'pip install hidapi' if this is a fresh install")
            self._log(f"No RA-Board (CM108) detected. Check USB connection{hint}.")

    # ── logging helper ───────────────────────────────────────────────
    LOG_MAX_LINES = 3000
    LOG_KEEP_LINES = 2000

    def _log(self, msg: str):
        self.log_box.configure(state="normal")
        # v0.8.7: milliseconds, so two stations' logs can be lined up closely
        # enough to see collisions (1-s stamps couldn't separate them).
        now = time.time()
        stamp = time.strftime('%H:%M:%S', time.localtime(now)) + f".{int((now % 1) * 1000):03d}"
        self.log_box.insert("end", f"{stamp}  {msg}\n")
        # v0.9.26: keep the on-screen log short -- a Text widget with tens of
        # thousands of lines (hours of verbose diagnostics) slows the whole
        # program on a low-power PC. The daily log file keeps everything.
        try:
            n = int(self.log_box.index("end-1c").split(".")[0])
            lite = getattr(self, "_lite", False)
            if n > (self.LITE_LOG_MAX if lite else self.LOG_MAX_LINES):
                self.log_box.delete("1.0", f"{n - (self.LITE_LOG_KEEP if lite else self.LOG_KEEP_LINES)}.0")
        except Exception:
            pass
        sl = getattr(self, "session_log", None)
        if sl is not None:
            sl.write(f"{stamp}  {msg}")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _report_error(self, context: str, exc: BaseException):
        """Log an exception from ANY thread (v0.8.9).

        Field (v0.8.8): transmissions failed and the radio stayed keyed, but
        the console showed only "NameError: cannot access free variable 'e'":
        the error was logged with  self.after(0, lambda: ...{e}...)  and
        Python deletes 'e' when the except block ends -- by the time the
        lambda ran, the real error was gone. The message and traceback are
        now formatted HERE, immediately, then handed to the GUI thread; the
        traceback also goes to the daily session log file."""
        import traceback
        msg = f"{context}: {type(exc).__name__}: {exc}"
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)).rstrip()

        def show(m=msg, t=tb):
            self._log(m)
            sl = getattr(self, "session_log", None)
            if sl is not None:
                sl.write("    " + t.replace("\n", "\n    "))
        try:
            self.after(0, show)
        except Exception:
            pass

    PTT_WATCHDOG_EVERY_S = 1.0
    PTT_WARN_EVERY_S = 15.0

    def _ptt_watchdog(self):
        """If a PTT release failed (see ptt.PTTController.release_failed),
        keep retrying about once a second while nothing is transmitting, and
        warn clearly until it succeeds (v0.8.9)."""
        e = self.engine
        p = getattr(e, "ptt", None)
        if getattr(p, "release_failed", False) is not True:
            return
        now = time.time()
        if now - getattr(self, "_ptt_wd_last", 0.0) < self.PTT_WATCHDOG_EVERY_S:
            return
        self._ptt_wd_last = now
        lock = getattr(e, "_tx_lock", None)
        if lock is not None and lock.locked():
            return                       # a transmission is in progress
        if p.ensure_released():
            self._log("PTT released (an earlier PTT release had failed -- see the error above).")
            self._ptt_wd_warned = 0.0
        elif now - getattr(self, "_ptt_wd_warned", 0.0) >= self.PTT_WARN_EVERY_S:
            self._ptt_wd_warned = now
            self._log("WARNING: PTT release is FAILING -- the radio may be stuck transmitting. "
                      "Check the RA-Board USB cable (RF getting into it?). Retrying every second; "
                      "if it doesn't clear, press Stop, unplug/replug the RA-Board, and Start again.")

    AUDIO_HEALTH_EVERY_S = 10.0
    # v0.9.53: was 0.5. Field (DX1BBC, Wyse 3040 + 4 VARA FM + BPQ32): 0.5-0.9 s
    # behind while 275-byte frames arrived -- warned every 10 s through the whole
    # transfer, though no audio was lost (that is RX_BACKLOG_DROP_S, 3 s, and is
    # always reported) and both messages went through.
    AUDIO_BACKLOG_WARN_S = 1.0

    # v0.9.67: field (0.9.66, DX1FLR): SARA FM showed "Not Responding" after a
    # dropped session and had to be closed -- no report could be saved, so
    # nothing showed what it was stuck on. A watchdog thread notices when the
    # window's loop stops for FREEZE_DUMP_S and writes what every thread is
    # doing to logs/sara_pp_freeze_<CALL>_<time>.txt (it never touches the
    # window itself, so it works while the window is frozen).
    FREEZE_DUMP_S = 8.0
    FREEZE_CHECK_S = 1.0

    def _start_freeze_watch(self):
        def run():
            dumped_for = None
            while True:
                time.sleep(self.FREEZE_CHECK_S)
                try:
                    tick = getattr(self, "_health_tick", None)
                    if tick is None or getattr(self, "engine", None) is None:
                        continue
                    stuck = time.time() - tick
                    if stuck >= self.FREEZE_DUMP_S and dumped_for != tick:
                        dumped_for = tick
                        App.write_freeze_dump(app_dir(), getattr(self, "_call_cache", ""), stuck)
                except Exception:
                    pass
        threading.Thread(target=run, daemon=True, name="freeze-watch").start()

    @staticmethod
    def write_freeze_dump(base_dir: str, call: str, stuck_s: float) -> str | None:
        """Every thread's current stack, to a file in logs/. Returns the path."""
        import traceback
        try:
            names = {t.ident: t.name for t in threading.enumerate()}
            out = [f"SARA FM window not responding for {stuck_s:.0f} s -- {ofdm.VERSION}",
                   time.strftime("%Y-%m-%d %H:%M:%S"), ""]
            for ident, frame in sys._current_frames().items():
                out.append(f"--- thread {names.get(ident, ident)} ---")
                out.extend(l.rstrip() for l in traceback.format_stack(frame))
                out.append("")
            d = os.path.join(base_dir, log_bundle.LOG_DIR_NAME)
            os.makedirs(d, exist_ok=True)
            path = os.path.join(d, f"sara_pp_freeze_{log_bundle.safe_call(call or 'NOCALL')}_"
                                   f"{time.strftime('%Y-%m-%d_%H%M%S')}.txt")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("\n".join(out))
            return path
        except Exception:
            return None

    def _report_slow_hid(self):
        """v0.9.69: log RA-Board (HID) calls that held the whole program up."""
        try:
            import ptt as _ptt
            ev = _ptt.take_slow_hid()
        except Exception:
            return
        if not ev:
            return
        worst = max(ev, key=lambda e: e[2])
        self._log(f"WARNING: the RA-Board answered slowly -- {worst[1]} took {worst[2]:.1f} s"
                  + (f" ({len(ev)} slow calls)" if len(ev) > 1 else "")
                  + ". The whole program waits for Windows meanwhile: the window freezes ('Not "
                  "Responding'), audio is lost and transmissions go out late. Usually another "
                  "program using the same RA-Board (VARA FM, BPQ32), a USB hub or a second RA-Board "
                  "that is misbehaving: unplug RA-Boards you don't use, and plug the one you use "
                  "straight into the PC.")

    def _audio_health(self):
        """v0.9.1: say so in the log when this PC can't keep up with the
        sound card -- lost input audio damages frames (long BPSK frames
        most), and a late decoder hears the channel late. See
        audio_io._rx_worker."""
        now = time.time()
        App._report_slow_hid(self)                                    # v0.9.69
        try:
            self._call_cache = self.call_var.get().strip().upper()    # v0.9.67: for the freeze watch
        except Exception:
            pass
        # v0.9.43: longest pause of this window's own loop (100 ms when idle)
        last_tick = getattr(self, "_health_tick", None)
        if last_tick is not None:
            self._gui_stall_max = max(getattr(self, "_gui_stall_max", 0.0), now - last_tick)
        self._health_tick = now
        if now - getattr(self, "_audio_health_last", 0.0) < self.AUDIO_HEALTH_EVERY_S:
            return
        span = now - getattr(self, "_audio_health_last", now - self.AUDIO_HEALTH_EVERY_S)
        self._audio_health_last = now
        cpu_now = time.process_time()
        cpu_used = cpu_now - getattr(self, "_health_cpu", cpu_now)
        self._health_cpu = cpu_now
        try:
            busy = self.engine.take_decode_busy()
        except Exception:
            busy = None
        try:
            dcpu = self.engine.take_decode_cpu()           # v0.9.53
        except Exception:
            dcpu = None
        stall, self._gui_stall_max = getattr(self, "_gui_stall_max", 0.0), 0.0
        take = getattr(self.engine, "take_audio_health", None)
        if take is None:
            return
        try:
            overruns, backlog = take()
        except Exception:
            return
        out_take = getattr(self.engine, "take_output_underruns", None)
        try:
            underruns = out_take() if out_take else 0
        except Exception:
            underruns = 0
        try:
            dropped = self.engine.take_backlog_dropped()
        except Exception:
            dropped = 0.0
        if overruns or underruns or dropped or backlog >= self.AUDIO_BACKLOG_WARN_S:
            parts = []
            if dropped:
                parts.append(f"decoding fell over {audio_io.RX_BACKLOG_DROP_S:.0f}s behind, "
                             f"so {dropped:.1f}s of received audio was skipped to catch up")
            if overruns:
                parts.append(f"sound card input lost {overruns} time(s)")
            if underruns:
                parts.append(f"transmit audio ran dry {underruns} time(s) -- gaps in what was sent")
            if backlog >= self.AUDIO_BACKLOG_WARN_S and not dropped:
                parts.append(f"decoding fell {backlog:.1f}s behind")
            # v0.9.43: say WHERE the time went, so the cause is visible in a report
            diag = []
            if busy is not None and span > 0:
                diag.append(f"decoder working {100 * busy / span:.0f}% of the time"
                            + (f" (its own CPU work {100 * dcpu / span:.0f}%)" if dcpu is not None else ""))
            if span > 0:
                diag.append(f"SARA FM used {100 * cpu_used / span:.0f}% of one CPU core")
            if stall:
                diag.append(f"longest window freeze {stall:.1f}s")
            hint = App._health_hint(busy, cpu_used, stall, span, dcpu)
            if overruns and getattr(self, "engine", None) is not None and \
                    getattr(self.engine, "rx_buffer_frames", audio_io.RX_BUFFER_FRAMES) < audio_io.RX_BUFFER_FRAMES_LARGE:
                hint += (" TIP: tick Settings > SARA Setup > 'Slow PC mode', then Stop and Start -- "
                         "4x more slack for the sound card and a lighter window.")
            elif overruns and not (getattr(self, "no_coded_var", None) is not None and self.no_coded_var.get()):
                hint += (" Slow PC mode is already on: if this keeps happening while 'C' mode frames "
                         "arrive, also tick 'No C modes' (Settings > SARA Setup > Link).")
            self._log("WARNING: this PC is not keeping up with the audio (" + ", ".join(parts)
                      + " in the last 10 s). Frames can be lost. [" + "; ".join(diag) + "] " + hint)

    def _on_clock_fix(self, total_ppm: float):
        """Decode thread: the receiver is now undoing a sound-card clock offset."""
        def gui():
            last = getattr(self, "_clock_logged", 0.0)
            self._clock_ppm = total_ppm
            if abs(total_ppm - last) < 300:
                return
            self._clock_logged = total_ppm
            if abs(total_ppm) < 300:
                self._log("Sound-card clock: correction dropped -- frames kept failing with it "
                          "(v0.9.59 watchdog)." if abs(last) >= 300 else
                          "Sound-card clock: no correction needed for this station.")
                return
            self._log(f"Sound-card clock: the station on the air runs {abs(total_ppm) / 10000:.2f}% "
                      f"{'slow' if total_ppm > 0 else 'fast'} relative to this PC ({total_ppm:+.0f} ppm) "
                      f"-- corrected. (Cheap USB sound dongles do this; VARA FM copes too. Beyond "
                      f"about 0.6-0.8% a better USB interface is the fix.)")
        try:
            self.after(0, gui)
        except Exception:
            pass

    RX_RECORD_S = 60.0

    def _start_rx_recording(self):
        """v0.9.47: save a WAV of what the sound card receives, so a link that
        works in VARA FM but not here can be analysed exactly (clock, level,
        distortion, filtering) instead of guessed at."""
        if self.engine is None or not hasattr(self.engine, "start_recording"):
            messagebox.showinfo("Record received audio", "Press Start first.")
            return
        self.engine.start_recording(self.RX_RECORD_S)
        self._log(f"Recording {self.RX_RECORD_S:.0f} s of received audio -- ask the other station to "
                  f"PING you (or press TUNE 10s) now. The file goes to the log folder.")

    def _check_rx_recording(self):
        e = self.engine
        if e is None or not hasattr(e, "take_recording"):
            return
        data = e.take_recording()
        if data is None:
            return
        import wave
        import numpy as np
        call = self.call_var.get().strip().upper() or "NOCALL"
        d = getattr(self.session_log, "dir", None) or app_dir()
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, f"SARA-FM_rx_{log_bundle.safe_call(call)}_{time.strftime('%Y-%m-%d_%H%M%S')}.wav")
        try:
            with wave.open(path, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(ofdm.SAMPLE_RATE)
                w.writeframes((np.clip(data, -1, 1) * 32767).astype(np.int16).tobytes())
            self._log(f"Received audio saved: {path}  ({len(data) / ofdm.SAMPLE_RATE:.0f} s) -- send it to the sysop.")
        except OSError as ex:
            self._log(f"Could not save the recording: {ex}")

    CPU_EVERY_S = 1.0
    CPU_CORES = max(1, os.cpu_count() or 1)

    def _update_cpu(self, now: float | None = None):
        """v0.9.44: SARA FM's share of this PC (all cores, like VARA FM's CPU
        gauge) and the decoder's load (it runs on one core: near 100% means
        it is about to fall behind). Smoothed over a few seconds; the peak of
        the last minute goes into reports."""
        now = time.time() if now is None else now
        last = getattr(self, "_cpu_last", None)
        cpu = time.process_time()
        dec = getattr(self.engine, "decode_busy_total_s", None) if self.engine is not None else None
        dcpu = getattr(self.engine, "decode_cpu_total_s", None) if self.engine is not None else None
        gui = time.thread_time()          # v0.9.53: this (window) thread's own CPU
        if last is None or len(last) < 5:
            self._cpu_last = (now, cpu, dec, dcpu, gui)
            return
        t0, c0, d0, dc0, g0 = last
        span = now - t0
        if span < self.CPU_EVERY_S:
            return
        self._cpu_last = (now, cpu, dec, dcpu, gui)
        brk = getattr(self, "_cpu_brk", None)
        if brk is None:
            brk = self._cpu_brk = collections.deque(maxlen=60)
        brk.append((span, cpu - c0,
                    (dec - d0) if (dec is not None and d0 is not None) else 0.0,
                    (dcpu - dc0) if (dcpu is not None and dc0 is not None) else 0.0,
                    gui - g0))
        pct = max(0.0, 100.0 * (cpu - c0) / span / self.CPU_CORES)
        dpct = (max(0.0, 100.0 * (dec - d0) / span) if (dec is not None and d0 is not None) else None)
        ema = getattr(self, "_cpu_pct", None)
        self._cpu_pct = pct if ema is None else 0.5 * pct + 0.5 * ema
        if dpct is not None:
            de = getattr(self, "_dec_pct", None)
            self._dec_pct = dpct if de is None else 0.5 * dpct + 0.5 * de
        else:
            self._dec_pct = None
        hist = getattr(self, "_cpu_hist", None)
        if hist is None:
            hist = self._cpu_hist = collections.deque(maxlen=60)
        hist.append((pct, dpct or 0.0))
        bar = getattr(self, "cpu_bar", None)
        if bar is not None:
            try:
                bar.set(self._cpu_pct, self._dec_pct)
            except tk.TclError:
                pass

    def _cpu_summary(self) -> str:
        hist = getattr(self, "_cpu_hist", None)
        if not hist:
            return "not measured yet"
        cur = getattr(self, "_cpu_pct", 0.0) or 0.0
        dec = getattr(self, "_dec_pct", None)
        out = (f"SARA FM {cur:.0f}% of this PC now (peak {max(h[0] for h in hist):.0f}% in the last "
               f"minute, {self.CPU_CORES} cores)"
               + (f"; decoder {dec:.0f}% of one core (peak {max(h[1] for h in hist):.0f}%)"
                  if dec is not None else ""))
        b = App.cpu_breakdown(getattr(self, "_cpu_brk", None))
        return out + ("\n              " + b if b else "")

    @staticmethod
    def cpu_breakdown(brk) -> str:
        """v0.9.53: where SARA FM's CPU went over the last minute, in % of one core."""
        if not brk:
            return ""
        span = sum(b[0] for b in brk)
        if span <= 0:
            return ""
        proc, dbusy, dcpu, gui = (sum(b[i] for b in brk) for i in (1, 2, 3, 4))
        if dcpu <= 0 and dbusy > 0:
            return ""
        other = max(0.0, proc - dcpu - gui)
        wait = max(0.0, dbusy - dcpu)
        pc = lambda v: 100 * v / span
        return (f"last minute, % of one core: decoder work {pc(dcpu):.0f}%, window {pc(gui):.0f}%, "
                f"audio/bridge/other {pc(other):.0f}%; decoder waited for a free core {pc(wait):.0f}%")

    def _on_slow_pc_change(self):
        on = self.slow_pc_var.get()
        self._apply_lite(on)
        self._save_settings()
        self._log(f"Slow PC mode {'ON' if on else 'off'}: audio buffer "
                  f"{audio_io.RX_BUFFER_FRAMES_LARGE if on else audio_io.RX_BUFFER_FRAMES} samples"
                  + (", meters 4x a second without needle swing, shorter on-screen log" if on else "")
                  + (" -- press Stop, then Start, for the buffer." if self.engine is not None else "."))

    # v0.9.83: what Slow PC mode lightens in the window. Field (DV1OTT,
    # 0.9.79): its window used 13-23% of a core -- more than decoding -- and
    # froze for up to 0.6 s, while sound-card input was being lost.
    LITE_POLL_EVERY = 3          # meters, DCD text, status: every 3rd 100 ms poll
    LITE_LOG_MAX, LITE_LOG_KEEP = 500, 300

    def _apply_caps(self):
        """v0.9.85: 'No C modes' asks other stations not to send the coded
        modes here (the most work to decode); they use QPSK Wide / 16QAM
        Wide instead. Only for PCs that still lose sound-card input."""
        ls = getattr(self, "link_stats", None)
        nc = getattr(self, "no_coded_var", None)
        if ls is not None:
            off = nc is not None and nc.get()
            ls.my_caps = link_adapt.MY_CAPS & ~(link_adapt.CAP_CODED if off else 0)

    def _on_no_coded_change(self):
        self._apply_caps()
        self._save_settings()
        self._log("Other stations are now asked NOT to send the coded (C) modes here -- their next "
                  "link report from this station tells them." if self.no_coded_var.get() else
                  "Other stations may send the coded (C) modes here again.")

    def _apply_lite(self, on: bool):
        gauges.AnalogMeter.LITE = bool(on)
        self._lite = bool(on)
        self._apply_caps()
        # Python hands the interpreter between threads every 5 ms by default;
        # 1 ms lets the sound-card callback in sooner while the decoder or the
        # window is busy (the callback itself needs well under 1 ms).
        try:
            sys.setswitchinterval(0.001 if on else 0.005)
        except Exception:
            pass
        if on:
            try:
                n = int(self.log_box.index("end-1c").split(".")[0])
                if n > self.LITE_LOG_KEEP:
                    self.log_box.configure(state="normal")
                    self.log_box.delete("1.0", f"{n - self.LITE_LOG_KEEP}.0")
                    self.log_box.configure(state="disabled")
            except Exception:
                pass

    def _rx_buffer_frames(self) -> int:
        return (audio_io.RX_BUFFER_FRAMES_LARGE if getattr(self, "slow_pc_var", None) is not None
                and self.slow_pc_var.get() else audio_io.RX_BUFFER_FRAMES)

    @staticmethod
    def _health_hint(busy, cpu_used, stall, span, dcpu=None) -> str:
        """v0.9.43: the likely cause, from where the time went.
        v0.9.53: `dcpu` = the decoder thread's own CPU time. When the decoder
        was "busy" far longer than it actually computed, it was waiting for a
        free core -- the PC is shared out, not SARA FM being slow."""
        if span <= 0:
            return ""
        if stall is not None and stall >= 1.0:
            return ("The SARA FM window itself froze -- something in it was slow; please send "
                    "this report to the sysop.")
        if (busy is not None and dcpu is not None and busy / span >= 0.25
                and dcpu < 0.6 * busy):
            return (f"Most of the decoder's time was spent WAITING for a free CPU core "
                    f"({100 * (busy - dcpu) / span:.0f}% waiting, {100 * dcpu / span:.0f}% working): other "
                    f"programs (VARA FM, BPQ32, Windows) were using the cores. Fewer programs on this PC, "
                    f"or a faster PC, fixes it; SARA FM settings won't.")
        if busy is not None and busy / span >= 0.6:
            return ("The decoder itself is busy. If frames were arriving in 16QAM/64QAM 'C' modes, "
                    "untick 'Auto may use 64QAM' or use QPSK Wide; if nothing was on the air, "
                    "please send this report to the sysop.")
        if cpu_used / span < 0.5:
            return ("SARA FM wasn't busy -- other programs or Windows held the PC up (antivirus "
                    "scan, updates, browser, VARA/BPQ32). Close what you can; on a laptop, plug in "
                    "the charger and use a High performance power plan.")
        return ("Close other busy programs; on a laptop, plug in the charger and use a High "
                "performance power plan.")

    def _test_ptt(self):
        """Key PTT for 2 seconds, independent of Start/engine — lets you try
        each GPIO pin against the radio's PTT LED without a full connect."""
        method = self.ptt_var.get()
        kwargs = {}
        if method == PTT_CM108:
            if not self._ra_boards:
                self._refresh_ra_boards()
            if not self._ra_boards:
                messagebox.showerror("RA-Board not found", "No CM108/RA-Board detected.")
                return
            sel = self.ra_board_combo.current()
            sel = sel if sel >= 0 else 0
            _, path, _ = self._ra_boards[sel]
            pin_label = self.gpio_pin_var.get()
            kwargs["ra_board_path"] = path
            kwargs["gpio_pin"] = int(pin_label.split("(")[1].rstrip(")"), 16)
            self._log(f"Testing PTT: {pin_label} on {self._ra_boards[sel][0]} — "
                       f"watch the radio/LED for 2s...")
        else:
            extra = self._serial_ptt_kwargs(method)
            if extra is None:
                return
            kwargs.update(extra)
            where = extra.get("port") or (f"GPIO {extra['pi_gpio_pin']}" if "pi_gpio_pin" in extra else "")
            self._log(f"Testing PTT via {method}{' on ' + where if where else ''} for 2s...")

        def worker():
            ptt = PTTController(method=method, **kwargs)
            try:
                ptt.open()
                ptt.ptt_on()
                time.sleep(2.0)
                ptt.ptt_off()
                self.after(0, lambda: (self._log("PTT test done."), self._report_slow_hid()))
            except Exception as e:
                self._report_error("PTT test failed", e)
            finally:
                try:
                    ptt.close()
                except Exception:
                    pass

        threading.Thread(target=worker, daemon=True).start()

    def _apply_tx_delay(self) -> int:
        """Validate the TX delay field and, if the engine is running, apply it
        live (no reconnect needed). Returns the value to use either way."""
        raw = self.tx_delay_var.get().strip()
        try:
            ms = int(raw)
            if ms < 0:
                raise ValueError
        except ValueError:
            self._log(f"TX delay '{raw}' is not valid; keeping "
                       f"{self.engine.tx_lead_ms if self.engine else audio_io.DEFAULT_TX_LEAD_MS} ms")
            ms = self.engine.tx_lead_ms if self.engine else audio_io.DEFAULT_TX_LEAD_MS
            self.tx_delay_var.set(str(ms))
            return ms
        if self.engine:
            self.engine.tx_lead_ms = ms
            self._log(f"TX delay set to {ms} ms")
        return ms

    # ── connect / disconnect ─────────────────────────────────────────
    # ── audio levels (v0.6.5) ────────────────────────────────────────
    def _levels(self) -> tuple[float, float]:
        """(rx_gain_linear, tx_level) from the GUI fields, clamped; falls
        back to safe values on a half-typed or invalid entry."""
        try:
            db = max(-20.0, min(40.0, float(self.rx_gain_db_var.get())))
        except (ValueError, tk.TclError):
            db = 0.0
        try:
            pct = max(5.0, min(100.0, float(self.tx_level_var.get())))
        except (ValueError, tk.TclError):
            pct = audio_io.DEFAULT_TX_LEVEL * 100
        return 10 ** (db / 20.0), pct / 100.0

    def _apply_levels(self):
        if self.engine:
            self.engine.rx_gain, self.engine.tx_level = self._levels()

    # ── saved settings (v0.6.5) ──────────────────────────────────────
    # Without these, every start (and every gateway reboot) meant
    # re-selecting devices, PTT, GPIO, TX delay, mode and levels by hand.
    # Stored next to the program (see app_dir()). Audio devices are
    # matched by NAME, not list position: Windows often renumbers them
    # after a reboot or USB replug.
    _SETTINGS_FILE = "sara_pp_settings.json"

    def _settings_path(self) -> str:
        return os.path.join(app_dir(), self._SETTINGS_FILE)

    @staticmethod
    def _dev_name(entry: str) -> str:
        """'2: Microphone (USB PnP Sound Devic' -> 'Microphone (USB PnP Sound Devic'"""
        return entry.split(":", 1)[1].strip() if ":" in entry else entry.strip()

    def _settings_dict(self) -> dict:
        return {
            "callsign": self.call_var.get().strip(),
            "target": self.target_var.get().strip(),
            "modulation": self.modulation_var.get(),
            "mode": self.mode_var.get(),
            "kiss_port": self.kiss_port_var.get().strip(),
            "remote_kiss": bool(self.remote_kiss_var.get()),
            "dcd_mode": self._dcd_mode_code(),
            "kiss_allow": self.kiss_allow_var.get().strip(),
            "input_device": self._dev_name(self.in_dev_var.get()),
            "output_device": self._dev_name(self.out_dev_var.get()),
            "ptt": self.ptt_var.get(),
            "ra_board": self.ra_board_var.get(),
            "gpio_pin": self.gpio_pin_var.get(),
            "serial_port": self.serial_port_var.get().strip() if hasattr(self, "serial_port_var") else "",
            "pi_gpio_pin": self.pi_gpio_var.get().strip() if hasattr(self, "pi_gpio_var") else "",
            "tx_delay_ms": self.tx_delay_var.get().strip(),
            "rx_gain_db": self.rx_gain_db_var.get().strip(),
            "tx_level_pct": self.tx_level_var.get().strip(),
            "allow_qam64": bool(self.allow64_var.get()) if hasattr(self, "allow64_var") else True,
            "slow_pc_buffer": bool(self.slow_pc_var.get()) if hasattr(self, "slow_pc_var") else False,
            "no_coded_modes": bool(self.no_coded_var.get()) if hasattr(self, "no_coded_var") else False,
            "targets": [list(t) for t in getattr(self, "targets", [])],
            "view": getattr(self, "_view", "full"),
            "gauge_title": self.gauge_title_var.get().strip() if hasattr(self, "gauge_title_var") else "",
            "digipeater": bool(self.digi_var.get()) if hasattr(self, "digi_var") else False,
            "digi_alias": self.digi_alias_var.get().strip() if hasattr(self, "digi_alias_var") else "",
            "gateway_direct": bool(self.gw_direct_var.get()) if hasattr(self, "gw_direct_var") else True,
            "gateway": bool(self.gateway_var.get()) if hasattr(self, "gateway_var") else False,
            "chat_serve": bool(self.chat_serve_var.get()) if hasattr(self, "chat_serve_var") else False,
            "chat_port": self.chat_port_var.get().strip() if hasattr(self, "chat_port_var") else "",
            "chat_use_remote": bool(self.chat_use_remote_var.get()) if hasattr(self, "chat_use_remote_var") else False,
            "chat_remote_host": self.chat_remote_host_var.get().strip() if hasattr(self, "chat_remote_host_var") else "",
            "chat_remote_port": self.chat_remote_port_var.get().strip() if hasattr(self, "chat_remote_port_var") else "",
        }

    def _save_settings(self):
        path = self._settings_path()
        try:
            tmp = path + ".tmp"
            with open(tmp, "w") as fh:
                json.dump(self._settings_dict(), fh, indent=2)
            os.replace(tmp, path)
        except OSError as e:
            self._log(f"Could not save settings to {path}: {e}")

    def _select_device(self, combo, var, saved_name: str, label: str):
        if not saved_name:
            return
        values = list(combo["values"])
        for v in values:
            if self._dev_name(v) == saved_name:
                var.set(v)
                return
        # v0.9.32: saved from another audio API's list -- still offer it
        want_in = label == "input"
        for i, name, ic, oc, rate in getattr(self, "_all_devices", []):
            if name.strip() == saved_name and (ic > 0 if want_in else oc > 0):
                v = f"{i}: {name}"
                combo["values"] = values + [v]
                var.set(v)
                return
        self._log(f"Saved {label} device '{saved_name}' not found -- "
                   f"check the USB audio interface, then pick it manually.")

    def _load_settings(self):
        path = self._settings_path()
        try:
            with open(path) as fh:
                st = json.load(fh)
        except FileNotFoundError:
            return
        except (OSError, ValueError) as e:
            self._log(f"Settings file unreadable ({e}); using defaults.")
            return
        simple = [("callsign", self.call_var), ("target", self.target_var),
                  ("modulation", self.modulation_var), ("kiss_port", self.kiss_port_var),
                  ("tx_delay_ms", self.tx_delay_var), ("rx_gain_db", self.rx_gain_db_var),
                  ("tx_level_pct", self.tx_level_var), ("gpio_pin", self.gpio_pin_var)]
        simple += [(k, getattr(self, a)) for k, a in (("serial_port", "serial_port_var"),   # v0.9.81
                                                      ("pi_gpio_pin", "pi_gpio_var")) if hasattr(self, a)]
        for key, var in simple:
            if st.get(key) not in (None, ""):
                var.set(str(st[key]))
        if st.get("dcd_mode") in self.DCD_LABELS:
            self.dcd_mode_var.set(self.DCD_LABELS[st["dcd_mode"]])
        if isinstance(st.get("remote_kiss"), bool):
            self.remote_kiss_var.set(st["remote_kiss"])
        if isinstance(st.get("allow_qam64"), bool) and hasattr(self, "allow64_var"):
            self.allow64_var.set(st["allow_qam64"])
        if isinstance(st.get("slow_pc_buffer"), bool) and hasattr(self, "slow_pc_var"):
            self.slow_pc_var.set(st["slow_pc_buffer"])
            self._apply_lite(st["slow_pc_buffer"])
        if isinstance(st.get("no_coded_modes"), bool) and hasattr(self, "no_coded_var"):
            self.no_coded_var.set(st["no_coded_modes"])
            self._apply_caps()
            self._apply_allow64()
        if st.get("kiss_allow"):
            self.kiss_allow_var.set(st["kiss_allow"])
        if st.get("view") in ("full", "gauges") and hasattr(self, "gauge_row"):
            self.after(200, lambda v=st["view"]: self._apply_view(v))
        tl = st.get("targets")
        if isinstance(tl, list) and hasattr(self, "target_combo"):
            self.targets = [[str(t[0]).upper(), str(t[1]) if len(t) > 1 else "",
                             t[2] if len(t) > 2 and isinstance(t[2], dict) else None]
                            for t in tl if isinstance(t, (list, tuple)) and t]
            self._refresh_target_list()
            self._show_target_location()
        self.mode_var.set("bpq")          # v0.9.36: an old "pingpong" setting is ignored
        if isinstance(st.get("gauge_title"), str) and hasattr(self, "gauge_title_var"):
            self.gauge_title_var.set(st["gauge_title"])
        if hasattr(self, "digi_var"):
            self.digi_var.set(bool(st.get("digipeater", False)))
            self.digi_alias_var.set(str(st.get("digi_alias", "") or ""))
        if hasattr(self, "gw_direct_var"):
            self.gw_direct_var.set(bool(st.get("gateway_direct", True)))
        if hasattr(self, "gateway_var"):
            self.gateway_var.set(bool(st.get("gateway", False)))
        if hasattr(self, "chat_serve_var"):
            self.chat_serve_var.set(bool(st.get("chat_serve", False)))
            self.chat_port_var.set(str(st.get("chat_port") or chat_remote.DEFAULT_PORT))
            self.chat_remote_host_var.set(str(st.get("chat_remote_host", "") or ""))
            self.chat_remote_port_var.set(str(st.get("chat_remote_port") or chat_remote.DEFAULT_PORT))
            self.chat_use_remote_var.set(bool(st.get("chat_use_remote", False)))
            if self.chat_use_remote_var.get():
                self.after(500, self._apply_remote_chat)
        self._select_device(self.in_dev_combo, self.in_dev_var, st.get("input_device", ""), "input")
        self._select_device(self.out_dev_combo, self.out_dev_var, st.get("output_device", ""), "output")
        if st.get("ptt") in (PTT_VOX, PTT_CM108, PTT_RTS, PTT_DTR, PTT_PIGPIO):
            self.ptt_var.set(st["ptt"])
            self._on_ptt_method_change()          # runs RA-Board detection for CM108
            saved_board = st.get("ra_board", "")
            if saved_board and saved_board in list(self.ra_board_combo["values"]):
                self.ra_board_var.set(saved_board)
        self._log(f"Settings restored from {path}")

    def _toggle_connect(self):
        if self.engine is None:
            self._connect()
        else:
            self._disconnect()

    def _connect(self):
        if not self.call_var.get().strip():
            messagebox.showwarning("Callsign needed", "Enter your callsign first.")
            return
        try:
            in_idx = int(self.in_dev_var.get().split(":")[0])
            out_idx = int(self.out_dev_var.get().split(":")[0])
        except Exception:
            messagebox.showwarning("Devices", "Pick an input and output device.")
            return
        ptt_kwargs = {}
        if self.ptt_var.get() == PTT_CM108:
            if not self._ra_boards:
                self._refresh_ra_boards()
            if not self._ra_boards:
                messagebox.showerror("RA-Board not found",
                                      "No CM108/RA-Board detected. Check the USB "
                                      "connection and click Detect, or pick a "
                                      "different PTT method.")
                return
            sel = self.ra_board_combo.current()
            sel = sel if sel >= 0 else 0
            _, path, _ = self._ra_boards[sel]
            ptt_kwargs["ra_board_path"] = path
            if len(self._ra_boards) > 1:
                # v0.9.42: field (DU3FS, two interfaces): with two boards it's easy
                # to send audio to one radio and key the other -- nothing goes out
                self._log(f"NOTE: {len(self._ra_boards)} RA-Boards connected; PTT uses "
                          f"{self.ra_board_var.get() or 'RA-Board-' + str(sel + 1)} and audio goes to "
                          f"'{self._dev_name(self.out_dev_var.get())}'. They must be the SAME interface: "
                          f"Settings > Devices > Test PTT (2s) and check that the right radio keys, and "
                          f"TUNE 10s to check its audio reaches the other station.")
            # "GPIO4 (0x08)" -> 0x08
            pin_label = self.gpio_pin_var.get()
            ptt_kwargs["gpio_pin"] = int(pin_label.split("(")[1].rstrip(")"), 16)
        else:
            extra = self._serial_ptt_kwargs(self.ptt_var.get())
            if extra is None:
                return
            ptt_kwargs.update(extra)
        tx_lead_ms = self._apply_tx_delay()
        try:
            t_start = time.time()
            self.engine = AudioEngine(in_idx, out_idx, ptt_method=self.ptt_var.get(),
                                       ptt_kwargs=ptt_kwargs, tx_lead_ms=tx_lead_ms,
                                       rx_buffer_frames=self._rx_buffer_frames())
            if self._rx_buffer_frames() != audio_io.RX_BUFFER_FRAMES:
                self._log(f"Audio buffer: large ({self._rx_buffer_frames()} samples, "
                          f"{self._rx_buffer_frames() / 48:.0f} ms) -- Slow PC setting.")
            t_ptt = time.time()
            self.engine.start()
            t_audio = time.time()
            if t_audio - t_start >= 3.0:          # v0.9.69: why Start "hangs"
                self._log(f"NOTE: Start took {t_audio - t_start:.0f} s -- PTT / RA-Board "
                          f"{t_ptt - t_start:.1f} s, sound card {t_audio - t_ptt:.1f} s. The window "
                          f"can't respond meanwhile (Windows may say 'Not Responding').")
            self._report_slow_hid()
        except Exception as e:
            messagebox.showerror("Audio error", str(e))
            self.engine = None
            return
        self._on_verbose_rx_change()  # apply current checkbox state to the new receiver
        try:
            self.engine.receiver.on_clock_fix = self._on_clock_fix     # v0.9.46
        except Exception:
            pass
        self._apply_levels()
        self._apply_dcd_mode()
        self._save_settings()          # a working configuration is worth keeping
        self.engine.on_carrier_wait = self._on_carrier_wait
        # Fired at the real broadcast moment (see AudioEngine.transmit),
        # not when _bridge_transmit was merely called -- a transmission
        # that queued up behind others at _tx_lock needs its self-echo
        # recorded against when it actually went out, not when it was
        # first requested. Safe to wire unconditionally: in Ping/Pong
        # mode self_echo_filter is simply never consulted.
        self.engine.on_tx_start = self.self_echo_filter.record_sent
        self.engine.tx_filter = self._tx_filter                 # v0.9.61
        self.engine.min_clear_s = self._min_clear_s             # v0.9.66
        self.engine.relay_pending = lambda: self.relay_watch.pending(     # v0.9.61 / v0.9.70
            seen=getattr(self.engine, "_last_sara_evidence", None))
        self.engine.relay_give_up = self.relay_watch.give_up
        self.status_var.set(f"Running — {ofdm.SAMPLE_RATE} Hz")
        self.connect_btn.configure(text="Stop")
        self._update_mode_controls()
        if not self.bridge:
            self._toggle_bridge()        # v0.9.36: the KISS bridge is always on while running
        self._log(f"Started. {ofdm.NUM_CARRIERS} subcarriers, "
                   f"{ofdm.GROSS_BPS if hasattr(ofdm, 'GROSS_BPS') else ''}"
                   f" version {ofdm.VERSION}")

    def _disconnect(self):
        if self.autotune:
            self._log("Auto-Tune cancelled: audio stopped.")
            self.autotune = None
        if self.bridge:
            self._stop_bridge()
        if self.engine:
            self.engine.stop()
            self.engine = None
        self.status_var.set("Stopped")
        self.connect_btn.configure(text="Start")
        self._update_mode_controls()
        self._log("Stopped.")

    # ── mode switching (Ping/Pong Test vs BPQ32/KISS Bridge) ──────────
    def _on_mode_change(self):
        self._update_mode_controls()

    def _update_mode_controls(self):
        connected = self.engine is not None
        # v0.7.3: PING and Auto-Tune also work while bridged (not mid-session)
        self.ping_btn.configure(state="normal" if connected else "disabled")
        self.autotune_btn.configure(state="normal" if connected else "disabled")
        self.tune_btn.configure(state="normal" if connected else "disabled")
        self.txtune_btn.configure(state="normal" if connected and not getattr(self, "txtune", None)
                                  else "disabled")
        net_state = "disabled" if self.bridge is not None else "normal"
        self.remote_kiss_chk.configure(state=net_state)
        self.kiss_allow_entry.configure(state=net_state)

    # ── BPQ32/KISS bridge ──────────────────────────────────────────────
    def _toggle_bridge(self):
        """Start the KISS bridge (stop it if running). v0.9.36: called by _connect."""
        if self.bridge:
            self._stop_bridge()
            return
        if not self.engine:
            return
        try:
            port = int(self.kiss_port_var.get().strip())
        except ValueError:
            messagebox.showwarning("KISS port", "Enter a valid TCP port number.")
            return

        def on_status(msg):
            self.after(0, lambda: self._on_bridge_status(msg))

        def on_air_frame(payload: bytes):
            # Queued to the engine's burst transmitter (v0.6.1), not a
            # thread per frame: frames that BPQ32 hands over together (a
            # MAXFRAME window) now go out under ONE key-up with tone-filled
            # gaps, so the channel never goes quiet mid-burst for the other
            # station to key up into. Non-blocking, so the bridge's
            # socket-reading loop is never held for the TX duration.
            self.stale.on_queued(bpq_bridge.unpack_batch(payload))      # v0.9.61
            self.engine.queue_transmit(payload, modulation=self._bridge_modulation(payload),
                                       on_sent=self._bridge_on_sent,
                                       on_error=self._bridge_on_error)

        host, allow = "127.0.0.1", None
        if self.remote_kiss_var.get():
            allow, err = bpq_bridge.parse_allowlist(self.kiss_allow_var.get())
            if err:
                messagebox.showwarning("Allow from", err)
                return
            host = "0.0.0.0"

        # v0.8.5: merge queued acknowledgments into one frame per burst
        self.engine.merge_payloads = bpq_bridge.merge_batches
        # v0.9.0: link report for the destination rides on each burst
        self.engine.finalize_payload = self._finalize_payload
        self.bridge = BpqBridge(on_air_frame=on_air_frame, on_status=on_status)
        self.bridge.on_client_frame = self._on_client_frame
        self.bridge.rewrite_from_client = self._rewrite_from_client      # v0.9.71
        try:
            self.bridge.start(port=port, host=host, allow=allow)
        except OSError as e:
            messagebox.showerror("Bridge error", f"Could not start KISS server on port {port}: {e}")
            self.bridge = None
            return
        if self._modulation() == ofdm.MOD_BPSK:
            # v0.8.4: field (v0.8.3) -- a BBS bridged in BPSK stalled two
            # downloads: a 7 x 256-byte BPSK window is ~21 s on the air, longer
            # than BPQ32's FRACK (20 s) and Winlink Express's (15 s max), so
            # every window timed out, was resent, and the resends crowded out
            # the acknowledgments (one download ran 17 minutes).
            f = len(ofdm.frame_encode(bytes(252), modulation=ofdm.MOD_BPSK)) / ofdm.SAMPLE_RATE
            self._log(f"WARNING: BPSK bridge -- a full 7-frame window takes ~{0.75 + 7 * f + 6 * audio_io.BURST_GAP_S:.0f} s "
                      f"on the air, longer than FRACK (BPQ32 20 s, Winlink Express 15 s): expect timeouts "
                      f"and stalls. Use QPSK unless the link is too weak for it; for BPSK set MAXFRAME 3.")
        if allow is not None:
            self._log(f"Network KISS access ON (port {port}). Connect from another PC with "
                      f"KISS-over-TCP host {', '.join(self._local_ipv4()) or 'this PC'} port {port}.")
            self._log(f"  Allowed from: this PC (always) + {', '.join(str(n) for n in allow)}")
            self._log("  KISS has no password: anyone allowed to connect can transmit on this "
                      "radio. If Windows Firewall asks, allow Private networks only.")
        self._update_mode_controls()
        self._start_chat_server()
        self._save_settings()

    @staticmethod
    def _local_ipv4() -> list:
        """This PC's IPv4 addresses (incl. Tailscale/AREDN), to tell the remote
        tester what to connect to. Best effort."""
        import socket as _s
        ips = set()
        try:
            for ip in _s.gethostbyname_ex(_s.gethostname())[2]:
                if not ip.startswith("127."):
                    ips.add(ip)
        except OSError:
            pass
        return sorted(ips)

    def _stop_bridge(self):
        self._stop_chat_server()
        if self.bridge:
            self.bridge.stop()
            self.bridge = None
        self.bridge_status_var.set("Bridge starts with Start.")
        self._update_mode_controls()

    def _on_bridge_status(self, msg: str):
        """GUI thread. Speed meters reset when a KISS client connects (a new
        Winlink/BPQ32 session) and log a one-line summary when it leaves."""
        if msg.startswith("KISS client connected"):
            self._kiss_connected = True
            self.rx_meter.reset()
            self.tx_meter.reset()
            self.retry_stats.reset()
        elif msg.startswith("KISS client disconnected") or msg.startswith("Stopped"):
            self._kiss_connected = False
            self._restore_gateway_levels(why="call finished")     # v0.9.87
        if msg.startswith("KISS client disconnected") and (
                self.rx_meter.total_bytes or self.tx_meter.total_bytes):
            self._log(self._speed_summary())
            self._save_speed_memory()      # v0.9.19: survives a crash or power cut too
        # A refusal (v0.7.5) goes in the log only: the status line keeps
        # showing who is actually connected.
        if not msg.startswith("Refused"):
            self.bridge_status_var.set(msg)
        self._log(f"Bridge: {msg}")

    def _speed_summary(self) -> str:
        e = throughput.fmt_elapsed(self.rx_meter.elapsed())
        return (f"Session summary ({e}):  RX {throughput.fmt_bytes(self.rx_meter.total_bytes)} "
                f"= {self.rx_meter.session_bps():.0f} bps   "
                f"TX {throughput.fmt_bytes(self.tx_meter.total_bytes)} "
                f"= {self.tx_meter.session_bps():.0f} bps"
                + (self.retry_stats.summary() if getattr(self, "retry_stats", None) else ""))

    def _update_speed_display(self):
        r, t = self.rx_meter, self.tx_meter
        if not (r.total_bytes or t.total_bytes):
            return
        self.speed_var.set(
            f"Speed  RX {r.rate_bps():.0f} bps (last {r.window_s:.0f}s)  ·  "
            f"session {r.session_bps():.0f} bps, {throughput.fmt_bytes(r.total_bytes)} "
            f"in {throughput.fmt_elapsed(r.elapsed())}     "
            f"TX {t.rate_bps():.0f} bps  ·  {throughput.fmt_bytes(t.total_bytes)}"
            + self._auto_note())

    def _auto_note(self) -> str:
        """'   ·  Auto: QPSK Wide to K6IRF-10' while Auto is on (v0.9.0)."""
        peer = getattr(self, "_last_tx_peer", None)
        if not self._auto() or not peer:
            return ""
        return f"     Auto: {link_adapt.NAMES.get(self.speed.mode_for(peer), '?')} to {peer}"

    def _bridge_on_sent(self, payload: bytes, modulation: int):
        """Called from the engine's burst transmitter after each frame is
        written (self-echo recording still happens via engine.on_tx_start
        at the real broadcast moment)."""
        mod_name = self._mod_name(modulation)
        n_frames = len(bpq_bridge.unpack_batch(payload))
        frames_note = f" ({n_frames} frames)" if n_frames != 1 else ""
        if link_adapt.session_frames(bpq_bridge.unpack_batch(payload)):
            self._last_session_activity = time.time()      # v0.9.48: not for UI beacons
        br = self.bridge
        if br is not None:
            br.frames_sent(payload)          # v0.9.0 KISS ACKMODE
        ax_frames = bpq_bridge.unpack_batch(payload)
        own = [f for f in ax_frames if not self._is_relayed_tx(f)]     # v0.9.37
        try:
            self._note_we_sent(own)                         # v0.9.63
            self.stale.on_aired(own)                        # v0.9.61
            self.relay_watch.expect(own)
        except Exception:
            pass
        ax25_bytes = sum(len(f) for f in own)
        try:
            retry_note = self.retry_stats.on_traffic("tx", own) if own else None
        except Exception:
            retry_note = None
        n_relay = len(ax_frames) - len(own)
        path = link_adapt.path_str(ax_frames[0]) if ax_frames else ""
        relay_note = ""
        if n_relay:
            relay_note = (f"  [digipeated{'' if n_relay == len(ax_frames) else f' {n_relay} of them'}"
                          f"  {link_adapt.path_str(next(f for f in ax_frames if self._is_relayed_tx(f)))}]")
        elif path:
            relay_note = f"  {path}"

        def on_gui_thread():
            if retry_note:
                self._log(retry_note)
            # Tk variables (callsign) are read here, on the GUI thread -- this
            # method itself runs on the transmit worker thread.
            self.tx_meter.add(ax25_bytes)
            src = "DIGI" if n_relay == len(ax_frames) and n_relay else "BPQ32"
            self._log(f"TX ({src}->air)  {len(payload)} bytes{frames_note}  [{mod_name}]{relay_note}"
                      f"  {{{link_adapt.batch_trace(ax_frames)}}}")
            log_csv_row(my_call=self.call_var.get().strip().upper(), event="BPQ_TX",
                        note=f"{len(payload)} bytes {mod_name} ({n_frames}f)")
        self.after(0, on_gui_thread)

    def _bridge_on_error(self, e: Exception):
        self._report_error("TX (BPQ32->air) error", e)
        self.stale.clear_queued()
        br = self.bridge
        if br is not None:
            br.ack_all_pending()             # let BPQ32's own timers retry

    def _bridge_transmit(self, payload: bytes, modulation: int):
        try:
            # record_sent() happens via engine.on_tx_start (wired in
            # _connect), fired from inside transmit() at the actual
            # broadcast moment -- not here, before transmit() is even
            # called, which can be well before this specific
            # transmission's real turn if others are queued ahead of it.
            self.engine.transmit(payload, modulation=modulation)
            mod_name = self._mod_name(modulation)
            n_frames = len(bpq_bridge.unpack_batch(payload))
            frames_note = f" ({n_frames} frames)" if n_frames != 1 else ""
            self.after(0, lambda: self._log(f"TX (BPQ32->air)  {len(payload)} bytes{frames_note}  [{mod_name}]"))
            log_csv_row(my_call=self.call_var.get().strip().upper(), event="BPQ_TX",
                        note=f"{len(payload)} bytes {mod_name} ({n_frames}f)")
        except Exception as e:
            self._report_error("TX (BPQ32->air) error", e)

    # ── RX diagnostics ──────────────────────────────────────────────
    def _on_verbose_rx_change(self):
        if not self.engine:
            return
        # v0.9.0: rejects always feed the link reports; logged only when verbose
        self._verbose_rx = bool(self.verbose_rx_var.get())
        self.engine.receiver.on_reject = self._rx_reject_dispatch
        if self._verbose_rx:
            self._log("Verbose RX diagnostics ON — every rejected candidate will be logged.")

    def _on_carrier_wait(self, seconds: float):
        # Called from a transmit thread, not the GUI thread -- hop over.
        # Always on (not gated behind Verbose RX): a station waiting out
        # a meaningful chunk of the carrier-sense timeout before every
        # transmission is directly actionable (e.g. a persistently high
        # local noise floor) and easy to miss if it only shows up buried
        # in a verbose log.
        msg = f"  [waited {seconds:.1f}s for clear channel before transmitting]"
        info = getattr(self.engine, "last_wait_info", None) if self.engine else None
        if info and seconds >= 3.0:
            # v0.7.2: say what the channel read during a long wait, so a log
            # shows whether it was real traffic (level well above the
            # threshold) or background noise sitting near it.
            lvl, thr, floor = info
            msg += f"  (channel read {lvl:.0f} dBFS vs busy above {thr:.0f}"
            msg += f", noise floor {floor:.0f}" if floor is not None else ""
            by = getattr(self.engine, "last_wait_by", None)
            # v0.7.6: open-squelch radios are detected by spectrum (carrier
            # quieting the hiss), not loudness -- say so, so the log reads right.
            msg += "; carrier detected by spectrum)" if by in ("spectrum", "both") else ")"
        if self.engine is not None and getattr(self.engine, "last_relay_yield", 0.0) >= 0.5:
            msg += f"  (and {self.engine.last_relay_yield:.1f}s leaving the channel to the digipeater's relay first)"
        if self.engine is not None and getattr(self.engine, "last_wait_gave_up", False):
            msg += ("  -- busy but no SARA-PP signal: local noise or interference? "
                    "Transmitted anyway (v0.9.12)")
        self.after(0, lambda: self._log(msg))

    def _rx_reject_dispatch(self, reason: str, info: dict):
        """Audio thread. A real frame lost to FEC/CRC counts against the peer
        we're hearing (its link report will say so); verbose logs everything."""
        # v0.9.42: always counted (the report shows them even without Verbose RX)
        rc = getattr(self, "_reject_counts", None)
        if rc is None:
            rc = self._reject_counts = collections.Counter()
        rc[reason] += 1
        if reason in link_adapt.LOST_REASONS:
            lock = getattr(self.engine, "_tx_lock", None) if self.engine else None
            if lock is None or not lock.locked():         # not our own transmission
                self.link_stats.on_lost(info.get("snr_db"))
        App._note_garbled(self, reason, info)
        if reason not in ofdm.OFDMReceiver.QUIET_REJECTS:
            lock = getattr(self.engine, "_tx_lock", None) if self.engine else None
            if lock is None or not lock.locked():
                self._last_lost_rx_t = time.time()        # v0.9.55: see _txtune_on_timeout
        if self._verbose_rx or reason not in ofdm.OFDMReceiver.QUIET_REJECTS:
            # v0.9.47: a frame lost after its preamble was recognised is always
            # logged (rare, and the clue when two stations can't hear each other)
            self._on_rx_reject(reason, info)

    # v0.9.50: a STRONG, clean preamble whose header/data still won't decode
    # is the mark of an overdriven transmitter (the radio's mic limiter /
    # ALC clips the OFDM peaks) -- a weak signal would show a weak preamble.
    # Field (DX1BBC -> DW1IRF, 0.9.49): at TX 40-85% every ping arrived with
    # preamble S/N 20-25 dB, -5.5 dBFS, and failed FEC/header (even BPSK);
    # at 20% the same pings arrived 1.5 dB quieter and decoded at once.
    GARBLED_MIN_PRE_SNR_DB = 18.0
    GARBLED_REASONS = ("fec_uncorrectable", "crc_fail", "low_header_confidence")
    GARBLED_HINT_EVERY_S = 120.0
    GARBLED_AFTER_OWN_TX_S = 4.0      # v0.9.88: longest frame (BPSK, ~3.4 s) + margin

    def _note_garbled(self, reason: str, info: dict):
        """Audio thread. Count strong-but-undecodable frames; hint once in a while."""
        pre = info.get("pre_snr_db")
        if reason not in App.GARBLED_REASONS or pre is None or pre < App.GARBLED_MIN_PRE_SNR_DB:
            return
        lock = getattr(self.engine, "_tx_lock", None) if self.engine else None
        if lock is not None and lock.locked():
            return                                        # our own transmission
        now = time.time()
        # v0.9.88: a frame that was already arriving while this station was
        # still transmitting lost its start to our own signal -- a collision,
        # not an overdriven sender. Field (0.9.87, DX1FLR -> DW1IRF-10): every
        # "won't decode" frame followed one of DW1IRF's own acknowledgments.
        if now - getattr(self.engine, "_last_tx_end", -1e9) < App.GARBLED_AFTER_OWN_TX_S:
            return
        hist = getattr(self, "_garbled_t", None)
        if hist is None:
            hist = self._garbled_t = collections.deque(maxlen=8)
        hist.append(now)
        self._garbled_last_t = now                        # v0.9.50: see _check_timeout
        self._garbled_total = getattr(self, "_garbled_total", 0) + 1
        recent = [t for t in hist if now - t <= 60.0]
        if len(recent) >= 2 and now - getattr(self, "_garbled_hint_t", 0.0) >= App.GARBLED_HINT_EVERY_S:
            self._garbled_hint_t = now
            msg = (f"NOTE: SARA FM signals arrive here strong (preamble S/N {pre:.0f} dB) but won't "
                   f"decode -- {len(recent)} in the last minute. That is what an OVERDRIVEN transmitter "
                   f"looks like: the sending station's radio clips the signal. Ask them to LOWER their "
                   f"TX level (e.g. 20%) and ping again, then run Tune TX.")
            self.after(0, lambda m=msg: self._log(m))

    def _on_rx_reject(self, reason: str, info: dict):
        # called from the audio callback thread -- hop to the GUI thread
        parts = ", ".join(f"{k}={v:.3f}" if isinstance(v, float) else f"{k}={v}"
                           for k, v in info.items())
        self.after(0, lambda: self._log(f"  [rejected: {reason}  {parts}]"))

    # ── ping / pong ───────────────────────────────────────────────────
    # Also used to LABEL a received frame's modulation (_mod_name). v0.9.4:
    # header code 2 is 16QAM Wide (it was the retired narrow 8PSK).
    _MOD_NAME_TO_CODE = {"BPSK": ofdm.MOD_BPSK, "QPSK": ofdm.MOD_QPSK, "16QAM Wide": ofdm.MOD_QAM16_WIDE,
                         "QPSK Wide": ofdm.MOD_QPSK_WIDE,   # v0.8.0
                         "64QAM Wide": ofdm.MOD_QAM64_WIDE,   # v0.9.18 (experimental)
                         "16QAM Wide C": ofdm.MOD_QAM16_WIDE_C,   # v0.9.24 coded
                         "64QAM Wide C": ofdm.MOD_QAM64_WIDE_C}
    _MOD_CODE_TO_NAME = {v: k for k, v in _MOD_NAME_TO_CODE.items()}

    def _modulation(self) -> int:
        """The fixed modulation chosen, or for "Auto" (v0.9.0) the start
        mode -- pings, pongs, TUNE and Auto-Tune use QPSK; bridge traffic
        goes through _bridge_modulation()."""
        choice = getattr(self, "_mod_choice", None) or self.modulation_var.get()
        if choice == "Auto":
            return ofdm.MOD_QPSK
        return self._MOD_NAME_TO_CODE.get(choice, ofdm.MOD_BPSK)

    def _apply_allow64(self):
        top = ofdm.MOD_QAM64_WIDE if self.allow64_var.get() else ofdm.MOD_QAM16_WIDE
        self.speed.max_idx = link_adapt.LADDER.index(top)

    def _auto(self) -> bool:
        return getattr(self, "_mod_choice", "") == "Auto"

    def _bridge_modulation(self, payload: bytes) -> int:
        """Bridge thread: the mode for a batch from BPQ32 -- per destination
        station when Auto (v0.9.0)."""
        if not self._auto():
            return self._modulation()
        # v0.9.37: per radio hop (the next digipeater, else the destination);
        # a batch for several hops goes at the slowest of their speeds
        hops = []
        for f in bpq_bridge.unpack_batch(payload):
            h = link_adapt.ax25_hops(f)[0]
            if h and h not in hops:
                hops.append(h)
        if not hops:
            return self.speed.mode_for(None)
        self._last_tx_peer = hops[0]
        modes = [self.speed.mode_for(h) for h in hops]
        mode = min(modes, key=link_adapt.LADDER.index)
        # v0.9.56: a connect request (SABM) is tiny and losing it costs a whole
        # retry timer -- send it no faster than QPSK Wide (not narrow QPSK: Auto
        # skips that for stations whose low carriers are weak, v0.9.13). Field (DW1IRF via DV1UK,
        # 0.9.55): a new connection warm-started at 16QAM Wide, the SABM failed
        # at DV1UK (S/N 29 dB, FEC failed) and the connect waited 15 s for nothing.
        if any(App._is_sabm_frame(f) for f in bpq_bridge.unpack_batch(payload)):
            if link_adapt.LADDER.index(mode) > link_adapt.LADDER.index(ofdm.MOD_QPSK_WIDE):
                mode = ofdm.MOD_QPSK_WIDE
        return mode

    @staticmethod
    def _is_sabm_frame(f: bytes) -> bool:
        a = link_adapt.ax25_addresses(f)
        if a is None:
            return False
        n = 7 * len(a[0])
        return len(f) > n and link_adapt.is_sabm(f[n])

    @classmethod
    def _mod_name(cls, modulation: int) -> str:
        return cls._MOD_CODE_TO_NAME.get(modulation, "BPSK")

    @staticmethod
    def _fec_note(frame) -> str:
        """Visible only when FEC actually repaired something: each such
        frame is one that would have been DROPPED before v0.6.0 (CRC fail
        -> a BPQ32 retry, a full round trip). Silent otherwise, so normal
        clean traffic doesn't get noisier."""
        n = getattr(frame, "fec_corrected", 0)
        return f"  [FEC repaired {n} byte{'s' if n != 1 else ''}]" if n else ""

    # ── graphical ping window (v0.7.3) ───────────────────────────────
    def _ping_window(self, open_it: bool):
        """The VARA-style ping window: returns it (creating/raising it when
        open_it), or None if it isn't open and shouldn't be opened."""
        w = self.ping_win
        if w is not None:
            try:
                if not w.winfo_exists():
                    w = None
            except tk.TclError:
                w = None
        if w is None and open_it:
            w = ping_window.PingWindow(self, on_ping=self._send_ping, on_autotune=self._start_autotune)
            try:  # open beside the main window, kept on screen
                self.update_idletasks()
                x = min(self.winfo_rootx() + self.winfo_width() + 10,
                        max(0, self.winfo_screenwidth() - ping_window.PingWindow.W - 40))
                w.geometry(f"+{x}+{max(0, self.winfo_rooty())}")
            except tk.TclError:
                pass
        self.ping_win = w
        if w is not None:
            w.set_peers(self.call_var.get().strip().upper(),
                        self.target_var.get().strip().upper() or proto.BROADCAST)
            if open_it:
                w.lift()
        return w

    def _send_ping(self, modulation: int | None = None, want_carrier: bool = False):
        if not self.engine or not self._ping_allowed():
            return
        my_call = self.call_var.get().strip().upper()
        target = self.target_var.get().strip().upper() or "*"
        self.seq_counter = (self.seq_counter + 1) % 256
        seq = self.seq_counter
        if modulation is None and self._auto():
            # v0.9.41: with Auto, PING and Auto-Tune use BPSK -- the most
            # sensitive mode (decodes ~4 dB below QPSK), like VARA FM's ping.
            # The pong comes back in the same mode.
            modulation = ofdm.MOD_BPSK
        mod = self._modulation() if modulation is None else modulation
        quieter = App._wants_quieter(self, target)
        if quieter:
            # v0.9.62: ask once per clipped reply seen, not on every ping for a minute
            getattr(self, "_quieter_ask", {}).pop(target.upper(), None)
        # v0.9.52: every ping asks for the data-carrier S/N (v0.9.14+ answer it):
        # the Ping window shows it -- it's what Tune TX goes by
        payload = proto.make_ping(my_call, target, seq, want_carrier=True,
                                  ask_quieter=quieter)
        self.outstanding[seq] = time.time()
        self._ping_sent_t = getattr(self, "_ping_sent_t", {})
        self._ping_sent_t[seq] = time.time()
        self._sent_seqs.append((seq, time.time()))   # duplicate-callsign check (v0.8.2)
        mod_label = self.modulation_var.get() if modulation is None else self._mod_name(mod)
        if self._auto() and not want_carrier and mod == ofdm.MOD_BPSK:
            mod_label = "Auto: BPSK"
        if want_carrier:
            pct = int(round(getattr(self.engine, "tx_level", 0) * 100))
            mod_label += f", TX level {pct}%"
        if quieter:
            mod_label += ", asks for a quieter reply"
        self._log(f"TX PING  seq={seq}  {my_call} -> {target}  [{mod_label}]")
        log_csv_row(my_call=my_call, peer_call=target, seq=seq, event="TX_PING",
                    note=self.modulation_var.get())
        threading.Thread(target=self._transmit_thread, args=(payload, mod), daemon=True).start()
        self.after(int(PING_TIMEOUT_S * 1000), lambda: self._check_timeout(seq, my_call, target))
        w = self._ping_window(open_it=not self.batch_running)
        if w is not None:
            self._ping_win_seq = seq
            w.set_pinging(seq)

    def _transmit_thread(self, payload: bytes, modulation: int):
        try:
            self.engine.transmit(payload, modulation=modulation, ignore_dcd=True)  # pings ignore DCD, as in VARA (v0.7.7)
        except Exception as e:
            self._report_error("TX error", e)

    # v0.9.50: the far station's REPLY can be what's overdriven. Field
    # (0.9.50, fresh install at DX1BBC, TX 40%): DX1BBC heard every one of
    # DW1IRF's Auto-Tune pings and answered; each pong reached DW1IRF strong
    # (preamble 25 dB) but clipped -- and Auto-Tune, seeing "no reply",
    # changed DW1IRF's OWN level, which couldn't help. Now a strong clipped
    # frame arriving while a ping waits for its pong means "they answered":
    # our TX level stays, and the next ping asks them to answer quieter
    # (PING_FLAG_QUIETER); the responder halves its reply level for us.
    QUIETER_ASK_S = 60.0
    QUIETER_MIN_LEVEL = 0.05

    def _check_clipped_reply(self, seq: int, target: str):
        sent = getattr(self, "_ping_sent_t", {}).pop(seq, None)
        last = getattr(self, "_garbled_last_t", 0.0)
        if sent is None or last < sent + 0.3 or target in ("", "*"):
            return False
        asks = getattr(self, "_quieter_ask", None)
        if asks is None:
            asks = self._quieter_ask = {}
        asks[target.upper()] = time.time()
        self._log(f"  ...but a strong, CLIPPED reply came back: {target} hears you and answered, its "
                  f"radio is overdriven. The next ping asks {target} to answer quieter "
                  f"(and {target} should run Auto-Tune / Tune TX to you, or lower its TX level).")
        at = getattr(self, "autotune", None)
        if at is not None and seq == at.get("seq"):
            at["reply_clipped"] = True
        return True

    def _wants_quieter(self, target: str) -> bool:
        t = getattr(self, "_quieter_ask", {}).get((target or "").upper())
        return t is not None and time.time() - t <= App.QUIETER_ASK_S

    def _pong_level_for(self, call: str, current: float) -> float:
        lv = getattr(self, "_pong_level", {}).get((call or "").upper())
        return current if lv is None else min(lv, current)

    # v0.9.62: at most -12 dB below our own level. Field (0.9.61, DX1FLR ->
    # DW1IRF): DX1FLR asked on every ping for a minute; DW1IRF went 14% -> 7%
    # -> 5% and DX1FLR still said "clipped" -- so it wasn't DW1IRF's level --
    # but at 5% DW1IRF's SignaLink VOX barely keyed and DX1FLR then read it as
    # too quiet (-47 dBFS). Still "clipped" at the floor: not our level, so
    # we answer normally again and ignore that station's asks for a while.
    QUIETER_MAX_CUT = 0.25
    QUIETER_IGNORE_S = 600.0

    def _on_quieter_request(self, call: str, current: float) -> float:
        """Responder: the pinging station says our replies arrive clipped.
        Returns the level to answer at; `current` means "back to normal"."""
        pl = getattr(self, "_pong_level", None)
        if pl is None:
            pl = self._pong_level = {}
        ign = getattr(self, "_quieter_ignore", None)
        if ign is None:
            ign = self._quieter_ignore = {}
        key = call.upper()
        if time.time() - ign.get(key, -1e9) < App.QUIETER_IGNORE_S:
            return App._pong_level_for(self, call, current)
        floor = max(App.QUIETER_MIN_LEVEL, round(current * App.QUIETER_MAX_CUT, 2))
        cur = App._pong_level_for(self, call, current)
        if cur <= floor + 1e-6:
            pl.pop(key, None)
            ign[key] = time.time()
            return current
        new = max(floor, round(cur * 0.5, 2))
        pl[key] = new
        return new

    def _check_timeout(self, seq, my_call, target):
        if seq in self.outstanding:
            del self.outstanding[seq]
            self._log(f"TIMEOUT  seq={seq} — no pong from {target}")
            App._check_clipped_reply(self, seq, target)
            w = self._ping_window(open_it=False)
            if w is not None and seq == self._ping_win_seq:
                w.set_timeout()
            at = getattr(self, "autotune", None)
            if at and seq == at.get("seq"):
                self._autotune_on_timeout()
            tt = getattr(self, "txtune", None)
            if tt and seq == tt.get("seq"):
                self._txtune_on_timeout()
            log_csv_row(my_call=my_call, peer_call=target, seq=seq, event="TIMEOUT")

    QUALITY_CLIP_HINT = 1.0
    # Quality is the ratio of received signal level to the reference waveform's
    # own level. Near 1.0 is a strong, clean signal. Above 1.0 means the
    # received audio is louder than the digital reference itself -- which
    # physically means analog gain somewhere (RX input level, most likely) has
    # been pushed past unity, toward clipping. It is common to see occasional
    # frame failures start exactly when quality climbs past this line, even
    # though most frames still limp through -- that pattern means back off
    # gain, not push it further.

    def _quality_str(self, quality: float) -> str:
        s = f"quality={quality:.2f}"
        if quality > self.QUALITY_CLIP_HINT:
            s += "  (>1.0 — likely overdriven; try lowering RX/TX gain)"
        return s

    # ── DCD (v0.7.7) ────────────────────────────────────────────────
    DCD_LABELS = {"auto": "Auto", "sara": "SARA-PP signals only", "off": "Off (never wait)"}
    DCD_STUCK_LOG_S = 15.0

    def _dcd_mode_code(self) -> str:
        label = self.dcd_mode_var.get()
        for code, lab in self.DCD_LABELS.items():
            if lab == label:
                return code
        return "auto"

    def _apply_dcd_mode(self):
        if self.engine:
            self.engine.dcd_mode = self._dcd_mode_code()

    def _dcd_override(self):
        if self.engine:
            self.engine.override_dcd(3.0)
            self._log("DCD override: transmitting anyway.")

    def _update_status_gauges(self):
        """v0.9.34: speed gauge, TCP lamp and the status bar's mode field."""
        r, t = self.rx_meter, self.tx_meter
        bps = max(r.rate_bps(), t.rate_bps()) if (r.total_bytes or t.total_bytes) else 0.0
        peer = getattr(self, "_last_tx_peer", None)
        mode = link_adapt.NAMES.get(self.speed.mode_for(peer), "") if (self._auto() and peer) else \
            ("" if self._auto() else self.modulation_var.get())
        sg = getattr(self, "speed_gauge", None)
        if sg is not None:
            sg.set_value(bps, (f"{bps:.0f} bps" + (f"  {mode}" if mode else "")) if bps else
                         ("idle" + (f"  {mode}" if mode else "")))
        connected = bool(getattr(self, "_kiss_connected", False))
        if getattr(self, "tcp_lamp", None) is not None:
            self.tcp_lamp.set(connected)
        if getattr(self, "sb_mode_var", None) is not None:
            m = "BPQ32 bridge" + ("  ·  DIGI on" if getattr(self, "digi_var", None) is not None
                                  and self.digi_var.get() else "")
            self.sb_mode_var.set(f"{m}  ·  {'Auto' if self._auto() else self.modulation_var.get()}"
                                 + (f" ({mode})" if self._auto() and mode else ""))

    def _update_lamps(self):
        """v0.9.33: TX / RX lamps (keyed; a SARA frame arriving)."""
        e = self.engine
        tx = bool(e is not None and getattr(e, "_ptt_keyed", False))
        rx = False
        try:
            rx = bool(e is not None and e.receiver.receiving_frame())
        except Exception:
            pass
        if getattr(self, "tx_lamp", None) is not None:
            self.tx_lamp.set(tx)
            self.rx_lamp.set(rx and not tx)

    def _update_dcd(self):
        self._update_lamps()
        e = self.engine
        if e is None:
            return
        try:
            busy, why = e.dcd_state()
        except Exception:
            return
        color = "#f2c94c" if busy else ("#888" if why == "off" else "#3c3")
        self.dcd_lamp.itemconfigure(self._dcd_dot, fill=color)
        self.dcd_text_var.set(f"busy -- {why}" if busy else
                              {"off": "off -- never waits", "override": "override (transmitting anyway)",
                               "tone ignored": "clear (ignoring a steady ~1 kHz tone)"}.get(why, "clear"))
        # v0.9.25: say once when a steady tone at the input gets ignored
        n = getattr(self.engine, "steady_tone_count", 0)
        if n != getattr(self, "_steady_tone_logged", 0):
            self._steady_tone_logged = n
            self._log(f"DCD: a steady tone near 1 kHz has been at the input for "
                      f"{audio_io.FILL_STEADY_S:.0f}s with no SARA-PP frame -- not a SARA-PP station, "
                      f"so DCD now ignores it. (It looks like SARA-PP's fill tone; likely hum or whine "
                      f"from the sound card or cabling, or a carrier on the frequency.)")
        self.dcd_override_btn.configure(state="normal" if busy else "disabled")
        # Stuck-busy diagnostic: what each detector read, so a field log shows
        # why DCD held (and whether "SARA-PP signals only" would help).
        now = time.time()
        if busy:
            if self._dcd_busy_since is None:
                self._dcd_busy_since, self._dcd_frames_at_start = now, self._frames_seen
                self._dcd_logged = False
            elif (not self._dcd_logged and now - self._dcd_busy_since >= self.DCD_STUCK_LOG_S
                  and self._frames_seen == self._dcd_frames_at_start):
                self._dcd_logged = True
                self._log(self._dcd_diagnostic(why))
        else:
            self._dcd_busy_since = None

    def _dcd_diagnostic(self, why: str) -> str:
        e = self.engine
        db = tuning.dbfs
        parts = [f"DCD busy {self.DCD_STUCK_LOG_S:.0f}s with no SARA-PP frames heard -- {why}."]
        try:
            parts.append(f"level {db(e._rx_rms_ema):.0f} dBFS vs busy above {db(e.busy_threshold_rms()):.0f}"
                         + (f" (noise floor {db(e.noise_floor_rms):.0f})" if e.noise_floor_rms else ""))
            if getattr(e, "_spec_ema", None) is not None and getattr(e, "spec_baseline_db", None) is not None:
                parts.append(f"spectrum ratio {e._spec_ema:+.0f} dB vs idle baseline {e.spec_baseline_db:+.0f} "
                             f"(busy at +{audio_io.SPECTRAL_MARGIN_DB:.0f})")
            if getattr(e, "_fill_ema", None) is not None:
                parts.append(f"fill tone {e._fill_ema:+.0f} dB (busy at +{audio_io.FILL_DETECT_DB:.0f})")
        except Exception:
            pass
        parts.append("If nobody is transmitting, set DCD Mode to 'SARA-PP signals only' "
                     "and please send this log.")
        return "  ".join(parts)

    # ── signal display (v0.7.0) ─────────────────────────────────────
    METER_W = 300

    def _meter_x(self, db: float) -> int:
        return int(max(0.0, min(1.0, (db + 60.0) / 60.0)) * self.METER_W)

    def _update_meter(self, rms: float):
        db = tuning.dbfs(rms)
        g = getattr(self, "gauge", None)
        if g is not None:
            g.set_level(db)
        color = "red" if db > -3 else ("#3c3" if tuning.in_target(db) else "#cc3")
        self.meter.coords(self.meter_bar, 0, 4, self._meter_x(db), 12)
        self.meter.itemconfigure(self.meter_bar, fill=color)
        e = self.engine
        floor = getattr(e, "noise_floor_rms", None) if e else None
        txt = f"{db:5.0f} dBFS"
        if floor:
            txt += f"   noise {tuning.dbfs(floor):.0f}   busy above {tuning.dbfs(e.busy_threshold_rms()):.0f}"
        self.rx_meter_var.set(txt)
        clipping = e is not None and getattr(e, "raw_peak", 0.0) >= tuning.CLIP_RAW_PEAK
        if clipping:
            # v0.9.14: our own transmit audio leaking into the input (common
            # with RA-Board setups) isn't receive clipping -- the receiver
            # ignores its input while transmitting. Field: DX1BBC showed red
            # on every TX.
            lock = getattr(e, "_tx_lock", None)
            if (lock is not None and lock.locked()) or time.time() - getattr(e, "_last_tx_end", 0.0) < 1.0:
                clipping = False
        self.clip_var.set("CLIP: lower radio volume / Windows level" if clipping else "")

    def _gain_db(self) -> float:
        return 20 * __import__("math").log10(self._levels()[0])

    def _signal_str(self, frame) -> str:
        snr, lvl = getattr(frame, "snr_db", None), getattr(frame, "level_dbfs", None)
        if snr is None or lvl is None:
            return self._quality_str(frame.quality)
        s = f"S/N {snr:+.0f} dB  level {lvl:.0f} dBFS"
        if lvl > -3:
            s += "  (hot -- lower RX gain)"
        return s

    def _note_frame(self, frame):
        snr, lvl = getattr(frame, "snr_db", None), getattr(frame, "level_dbfs", None)
        if snr is None or lvl is None:
            return
        self.signal_var.set(f"Last frame: S/N {snr:+.0f} dB ({tuning.snr_verdict(snr)}),  "
                            f"level {lvl:.0f} dBFS ({tuning.level_verdict(lvl)})")
        g = getattr(self, "gauge", None)
        if g is not None:
            g.set_frame_level(lvl)
        sg = getattr(self, "snr_gauge", None)
        if sg is not None:
            v = tuning.snr_verdict(snr).split()[0]
            color = {"excellent": sg.GREEN_LIT, "good": sg.GREEN_LIT, "marginal": sg.AMBER_LIT}.get(v, sg.RED_LIT)
            sg.set_value(snr, f"S/N {snr:+.0f} dB  {v}", color)

    # ── automatic speed (v0.9.0) ────────────────────────────────────
    def _finalize_payload(self, payload: bytes, modulation: int) -> bytes:
        """TX worker thread: add the link report for this batch's destination
        (bridge traffic only; pings aren't batches and pass unchanged)."""
        if self.bridge is None or chat.is_chat(payload):
            return payload
        hop, _ = link_adapt.first_frame_hops(payload)      # v0.9.37: the radio hop
        if not hop:
            return payload
        return link_adapt.attach_report(payload, self.link_stats.take_report(hop))

    def _note_link(self, frame):
        """GUI thread, bridge frames from the other station: record how we
        hear them, and act on the report they sent about how they hear us."""
        hop, src = link_adapt.first_frame_hops(frame.payload)   # v0.9.37: src = who transmitted it
        if not src:
            return
        for f in bpq_bridge.unpack_batch(frame.payload):
            self._check_new_connection(f, incoming=True)
        try:
            self._chat_heard(src, link_adapt.link_snr_db(frame), self._mod_name(frame.modulation), "packet")
        except Exception:
            pass
        self.link_stats.on_frame(src, frame.modulation, link_adapt.link_snr_db(frame), frame.fec_corrected,
                                 len(frame.payload), carrier_snr_db=link_adapt.carrier_snr_db(frame))
        rep = link_adapt.split_report(frame.payload)
        if rep is not None and not self._report_for_me(frame.payload, hop):
            rep = None             # v0.9.37: a report on another hop (digipeated traffic)
        if rep is not None:
            if self._verbose_rx:
                self._log(f"  [link report from {src}: our {link_adapt.NAMES.get(rep.mode, rep.mode)} frames "
                          f"arrive at S/N {rep.snr_db} dB"
                          + (f" (carriers {rep.carrier_snr_db} dB)" if rep.carrier_snr_db is not None else "")
                          + f", FEC up to {rep.fec_max_pct}%, {rep.n_ok} ok / {rep.n_bad} lost]")
            if self._auto():
                self.speed.on_report(src, rep)

    def _my_calls(self) -> set:
        """This station's callsign and digipeater alias, normalized."""
        out = {link_adapt.norm_call(self.call_var.get())}
        alias = getattr(self, "digi_alias_var", None)
        if alias is not None:
            out |= {link_adapt.norm_call(a) for a in alias.get().replace(",", " ").split()}
        return {c for c in out if c}

    def _report_for_me(self, payload: bytes, hop) -> bool:
        """v0.9.37: a batch's link report is about the hop it was sent to.
        Direct (no digipeater) traffic keeps the old rule -- the report is
        ours -- so two-station links behave exactly as before; digipeated
        traffic counts only when this station is the hop."""
        try:
            frames = bpq_bridge.unpack_batch(payload)
            if not frames or not link_adapt.ax25_hops(frames[0])[2]:
                return True
        except Exception:
            return True
        mine = self._my_calls()
        if not mine or not hop:
            return True
        n = link_adapt.norm_call(hop)
        # v0.9.72: or the KISS client's own callsign -- field (0.9.71): DW1IRF
        # relays to DX1PRD-2 (Winlink Express at DX1FLR) carried reports for
        # DX1FLR's hop, and DX1FLR ignored them all: stuck at QPSK at 21 dB
        return n in mine or n in getattr(self, "_client_calls", ()) \
            or link_adapt.base_call(n) in {link_adapt.base_call(c) for c in mine}

    def _gw_direct(self) -> bool:
        v = getattr(self, "gw_direct_var", None)
        try:
            return True if v is None else bool(v.get())
        except Exception:
            return True

    def _rewrite_from_client(self, frame: bytes) -> bytes:
        """Bridge thread (v0.9.71): the digipeater path put back on BPQ32's
        replies to a caller it was shown as direct."""
        if not self._gw_direct():
            return frame
        return self.via_map.from_client(frame)

    def _b2_notes(self, notes, given: bool):
        """v0.9.71: B2 message checks (b2check) to the log."""
        for n in notes or ():
            ok = n.endswith("intact")
            where = ("as handed to BPQ32 / Winlink Express on this PC" if given
                     else "as BPQ32 / Winlink Express on this PC sent it")
            msg = f"{n} ({where})"
            if given and ok:
                msg += (". If the CMS still reports a checksum failure, the data was changed after "
                        "SARA FM handed it over")
            self.after(0, lambda m=msg: self._log(m))

    def _on_client_frame(self, frame: bytes):
        """Bridge thread: a frame from BPQ32 / Winlink Express. A repeated
        I-frame means the other station missed it."""
        if not link_adapt.is_relayed(frame):
            try:
                self._b2_notes(self.b2watch.feed(frame), given=False)
            except Exception:
                pass
        if link_adapt.is_relayed(frame):
            # v0.9.37: BPQ32 digipeating someone else's frame. If SARA FM's
            # digipeater already repeated this one, both are on: say so once.
            if frame in getattr(self, "_relayed", {}) and not getattr(self, "_warned_double_digi", False):
                self._warned_double_digi = True
                self.after(0, lambda: self._log(
                    "WARNING: BPQ32 is digipeating too (DIGIFLAG=1) -- every frame goes out twice. "
                    "Turn off one of them: SARA Setup > Digipeater, or DIGIFLAG=0 in bpq32.cfg."))
            return
        p = link_adapt.ax25_parse(frame)
        if p is not None and p[1]:
            try:
                self._client_calls.add(link_adapt.norm_call(p[1]))  # v0.9.72
            except AttributeError:
                pass
        self._check_new_connection(frame, incoming=False)
        dest = self.retx.check(frame)
        if dest and self._auto():
            # v0.9.61: a resend of a frame still waiting to go out, already
            # acknowledged, or sent since they last had a chance to answer
            # says the client's timer ran out -- not that they missed it
            turn = self.engine.turn_time() if self.engine else 0.0
            if self.stale.is_queued(frame) or self.stale.why_stale(frame, turn):
                return
            hop = link_adapt.ax25_hops(frame)[0]
            t = getattr(self, "_own_relay_heard", {}).get(link_adapt.relay_key(frame))
            if hop and t is not None and time.time() - t < 120:
                # v0.9.72: we heard the digipeater repeat this very frame, so
                # the first hop delivered it -- it was lost beyond the
                # digipeater. Field (0.9.71, DX1FLR): this dropped the hop to
                # DW1IRF to BPSK for the last 2:20 of a 14 kB message.
                if not getattr(self, "_noted_far_loss", False):
                    self._noted_far_loss = True
                    self.after(0, lambda h=hop: self._log(
                        f"  [a resend of a frame {h} DID repeat: lost after the digipeater, so the "
                        f"speed to {h} stays]"))
                return
            self.speed.on_retransmit(hop or dest)

    def _min_clear_s(self, payload: bytes) -> float:
        """v0.9.66: a relay (frames we repeat as digipeater) waits for the
        sender's whole burst to end -- see AudioEngine._hold_clear.
        v0.9.88: so does any AX.25 transmission while another station is in
        the middle of sending a window (see _peer_mid_window)."""
        if not payload or payload[0] != bpq_bridge.BATCH_MARKER:
            return 0.0
        frames = bpq_bridge.unpack_batch(payload)
        if frames == [payload] or not frames:
            return 0.0
        if any(f in getattr(self, "_relayed", {}) for f in frames):
            return audio_io.DIGI_MIN_CLEAR_S
        return audio_io.DIGI_MIN_CLEAR_S if App._peer_mid_window(self) else 0.0

    # v0.9.88: field (0.9.87, Winlink Express on DX1FLR -> DW1IRF-10, BPSK):
    # DX1FLR sent 7-frame windows, 3.3 s a frame with 0.2-0.4 s gaps. BPQ32 on
    # DW1IRF acknowledged each frame as it arrived; SARA FM's carrier sense
    # read the channel clear in a gap and sent the acknowledgment -- on top of
    # DX1FLR's next frame, which was lost. Every window needed a REJ and a
    # resend, Auto dropped DX1FLR to BPSK (longer windows, more gaps), and
    # both sessions ended before the message got through. The last frame of
    # a window polls (P bit) or the window ends in silence: until then the
    # sender isn't done, and a 0.2 s gap isn't the end of its turn.
    MID_WINDOW_HOLD_S = 6.0          # longest frame (BPSK ~3.4 s) + gap + margin

    def _note_peer_burst(self, ax_frames):
        """Air frame heard from another station: is its window still going?
        (an I frame and no poll/final bit on anything in it)"""
        more = False
        for f in ax_frames:
            p = link_adapt.ax25_parse(f)
            if p is None:
                continue
            ctrl = p[2]
            if ctrl & 0x10:
                more = False
                break
            if ctrl & 0x01 == 0:
                more = True
        self._peer_burst = (time.time(), more)

    def _peer_mid_window(self) -> bool:
        t, more = getattr(self, "_peer_burst", (0.0, False))
        return more and time.time() - t <= App.MID_WINDOW_HOLD_S

    def _tx_filter(self, payload: bytes):
        """Transmit thread, as each queued item is taken to go on the air
        (v0.9.61): skip I frames the client resent that can't help -- see
        link_adapt.StaleFrames. Returns the payload, a smaller batch, or None."""
        if (not payload or payload[0] != bpq_bridge.BATCH_MARKER or chat.is_chat(payload)
                or proto.is_pingpong(payload)):
            return payload
        frames = bpq_bridge.unpack_batch(payload)
        if frames == [payload]:
            return payload
        br_ = self.bridge
        if br_ is not None and getattr(br_, "_client_sock", True) is None:
            # v0.9.70: the KISS client (Winlink Express / BPQ32) has gone --
            # field (DX1FLR): two queued BPSK data frames (7 s of air) still
            # went out after the session ended. Its data and acknowledgments
            # are for a session that no longer exists; DISC/DM/UA still go.
            gone = [f for f in frames if not (link_adapt.is_relayed(f) or self._is_relayed_tx(f))
                    and (link_adapt.ax25_parse(f) or (0, 0, 0x03))[2] & 0x03 != 0x03]
            if gone:
                frames = [f for f in frames if f not in gone]
                self.after(0, lambda n=len(gone): self._log(
                    f"  [dropped {n} queued frame{'s' if n > 1 else ''}: the KISS client disconnected "
                    f"-- that session is over]"))
                if not frames:
                    return None
                payload = bpq_bridge.pack_batch(frames)
        turn = self.engine.turn_time() if self.engine else 0.0
        keep, drop, whys = [], [], set()
        for f in frames:
            why = None
            if not (link_adapt.is_relayed(f) or self._is_relayed_tx(f)):
                why = self.stale.why_stale(f, turn)
            if why:
                drop.append(f)
                whys.add(why)
            else:
                keep.append(f)
        if not drop:
            return payload
        self.stale.on_dropped(drop)
        self.stale.skipped += len(drop)
        br = self.bridge
        if br is not None:
            br.frames_sent(bpq_bridge.pack_batch(drop))     # KISS ACKMODE: done with them
        to = link_adapt.ax25_hops(drop[0])[0] or ""
        why_s = ("already acknowledged by " + to if whys == {"acked"} else
                 f"sent moments ago -- {to} hasn't had a chance to answer yet" if whys == {"unanswered"} else
                 f"already acknowledged or just sent -- {to} hasn't answered yet")
        msg = (f"  [skipped {len(drop)} resent frame{'s' if len(drop) > 1 else ''} "
               f"{{{link_adapt.batch_trace(drop)}}}: {why_s}. The client's retry timer ran out "
               f"while they were still waiting to go out.]")
        self.after(0, lambda: self._log(msg))
        if not keep:
            return None
        return bpq_bridge.pack_batch(keep)

    def _check_new_connection(self, frame: bytes, incoming: bool):
        """v0.9.1: an AX.25 connect request (SABM) either way starts Auto
        fresh for that station -- like reconnecting in VARA FM to get a new
        reading instead of staying stuck at a low speed."""
        p = link_adapt.ax25_parse(frame)
        if p is None or not link_adapt.is_sabm(p[2]):
            return
        # v0.9.37: a connect still on its way through a digipeater isn't a
        # connection here; speed and levels are for the radio hop
        if incoming and link_adapt.in_transit(frame):
            return
        nxt, heard, digis = link_adapt.ax25_hops(frame)
        peer = (heard if incoming else nxt) or (p[1] if incoming else p[0])
        end = p[1] if incoming else p[0]
        via = f" (via {peer})" if digis and link_adapt.norm_call(end) != link_adapt.norm_call(peer) else ""
        key = (peer, incoming)
        now = time.time()
        last = getattr(self, "_last_sabm", {})
        self._last_sabm = last
        if now - last.get(key, 0.0) < 30.0:
            return                       # SABM retries of the same connect
        last[key] = now
        self.link_stats.reset_peer(peer)
        try:
            self.after(0, lambda p=peer, i=incoming: self._apply_profile_for_peer(p, i))   # v0.9.35/0.9.87
        except Exception:
            pass
        if not self._auto():
            return
        mode = self.speed.new_connection(peer)
        msg = (f"New connection with {end}{via}: Auto starts at {link_adapt.NAMES.get(mode, mode)}"
               + (f" for the hop to {peer}" if via else ""))
        try:
            self.after(0, lambda m=msg: self._log(m))
        except Exception:
            pass

    def _on_speed_change(self, peer: str, old: int, new: int, reason: str):
        msg = (f"Speed to {peer}: {link_adapt.NAMES.get(old, old)} -> {link_adapt.NAMES.get(new, new)}"
               f"  ({reason})")
        if new == ofdm.MOD_BPSK:
            msg += ("  -- BPSK windows are long: KISS ACKMODE on BPQ32 keeps FRACK from expiring "
                    "(see QUICKSTART 14)")
        try:
            self.after(0, lambda m=msg: self._log(m))
        except Exception:
            pass

    # ── duplicate callsign (v0.8.2) ─────────────────────────────────
    def _sent_it(self, seq: int, window_s: float = 20.0) -> bool:
        now = time.time()
        return any(s == seq and now - t <= window_s for s, t in self._sent_seqs)

    def _warn_duplicate_callsign(self, my_call: str):
        msg = (f"Another station is transmitting with YOUR callsign ({my_call}). Pings between "
               f"two stations with the same callsign are never answered. Check 'My callsign' on "
               f"both stations (a SARA-PP folder copied from another PC brings its settings, "
               f"including the callsign).")
        self._log("WARNING: " + msg)
        if not self._dup_call_warned:
            self._dup_call_warned = True
            try:
                messagebox.showwarning("Duplicate callsign", msg)
            except Exception:
                pass

    # ── per-carrier check (v0.8.1) ──────────────────────────────────
    CARRIER_REPORT_EVERY = 20
    DISTORTION_GAP_DB = 18.0     # healthy links: 8-15 dB gap (preamble vs carriers); DV1UK ~20

    def _note_carriers(self, frame):
        """Accumulate the per-carrier diagnostic (ofdm_core._carrier_stats)
        for frames from OTHER stations, and every CARRIER_REPORT_EVERY frames
        of a modulation log which carriers this radio path is losing. Field
        (v0.8.0): QPSK Wide from K6IRF reached DW1IRF needing FEC on every
        large frame (median 15 bytes, up to the 24-byte limit) at +29 dB,
        while the other direction was clean -- this shows where in the band."""
        cs = getattr(frame, "carrier_stats", None)
        if cs is None:
            return
        freqs, errs, bits, evm, evm_n = cs
        acc = self._carrier_acc.setdefault(frame.modulation, {"n": 0, "freqs": freqs,
                                                              "errs": 0, "bits": 0, "evm": 0, "evm_n": 0})
        if len(acc["freqs"]) != len(freqs):
            return
        acc["n"] += 1
        if getattr(frame, "snr_db", None) is not None:
            acc.setdefault("pre", []).append(frame.snr_db)
        acc["errs"] = acc["errs"] + errs
        acc["bits"] = acc["bits"] + bits
        acc["evm"] = acc["evm"] + evm
        acc["evm_n"] = acc["evm_n"] + evm_n
        if acc["n"] >= self.CARRIER_REPORT_EVERY:
            self._log_carriers(frame.modulation, acc)
            del self._carrier_acc[frame.modulation]

    def _log_carriers(self, modulation, acc):
        import numpy as _np
        with _np.errstate(divide="ignore", invalid="ignore"):
            err_pct = 100 * acc["errs"] / _np.maximum(acc["bits"], 1)
            snr = 10 * _np.log10(acc["evm_n"] / _np.maximum(acc["evm"], 1e-12))
        cells = "  ".join(f"{f:.0f}:{e:.1f}%/{q:.0f}" for f, e, q in zip(acc["freqs"], err_pct, snr))
        self._log(f"Carrier check ({self._mod_name(modulation)}, last {acc['n']} frames) "
                  f"-- Hz: bit errors / S/N dB:")
        self._log("  " + cells)
        worst = _np.argsort(-err_pct)[:3]
        bad = [f"{acc['freqs'][i]:.0f} Hz ({err_pct[i]:.1f}%, S/N {snr[i]:.0f} dB)" for i in worst if err_pct[i] >= 1.0]
        self._log("  -> weakest: " + (", ".join(bad) if bad else "none -- every carrier clean"))
        pre = sorted(acc.get("pre") or [])
        med_c = float(_np.median(snr[_np.isfinite(snr)])) if _np.isfinite(snr).any() else None
        if pre and med_c is not None:
            med_p = pre[len(pre) // 2]
            if med_p - med_c >= getattr(self, "DISTORTION_GAP_DB", 18.0):
                # v0.9.6, field (DV1UK -> DU3FS): preamble 27-31 dB, carriers 1-11 dB
                self._log(f"  -> WARNING: these frames read S/N {med_p:+.0f} dB on the preamble but only "
                          f"{med_c:.0f} dB on the data carriers. The signal is DISTORTED, not weak: most "
                          f"often the sending station's transmit audio is too loud (clipping in the radio) "
                          f"or a steady tone/interference sits in the band. Ask that station to lower its "
                          f"TX level (and Windows playback level) and run Auto-Tune.")

    # ── TUNE (v0.7.0) ───────────────────────────────────────────────
    def _start_tune(self):
        if not self.engine:
            return
        self.tune_btn.configure(state="disabled")
        pct = int(round(self._levels()[1] * 100))
        self._log(f"TUNE: sending a 10 s test tone at TX level {pct}% -- the other station "
                  f"can set its RX gain while watching its RX meter (target "
                  f"{tuning.TARGET_LEVEL_DBFS:.0f} dBFS).")

        def run():
            try:
                self.engine.transmit_tone(10.0)
                self.after(0, lambda: self._log("TUNE: done."))
            except Exception as e:
                self._report_error("TUNE error", e)
            finally:
                self.after(0, lambda: self.tune_btn.configure(
                    state="normal" if self.engine else "disabled"))
        threading.Thread(target=run, daemon=True).start()

    # ── Auto-Tune (v0.7.0) ──────────────────────────────────────────
    AUTOTUNE_MAX_PINGS = 6
    AUTOTUNE_MAX_MISSES = 3

    def _start_autotune(self):
        if not self.engine or self.batch_running or getattr(self, "autotune", None):
            return
        if not self._ping_allowed():
            return
        target = self.target_var.get().strip().upper()
        if not target or target == proto.BROADCAST:
            messagebox.showwarning("Auto-Tune", "Enter the other station's callsign as Target "
                                                "first (it must be running SARA-PP v0.7.0+, in Ping/Pong mode or, "
                                                "from v0.7.3, bridged to BPQ32 while idle).")
            return
        self.autotune = {"target": target, "remaining": self.AUTOTUNE_MAX_PINGS, "seq": None,
                         "good": 0, "misses": 0, "report": None, "frame": None,
                         "contact": False, "start_tx": self._levels()[1], "raised": []}
        self.ping_btn.configure(state="disabled")
        self.autotune_btn.configure(state="disabled")
        w = self._ping_window(open_it=True)
        if w is not None:
            w.set_advice([f"Auto-Tune running: pinging {target} and setting your RX gain..."])
        self._autotune_chain = True
        self._set_tune_status("running", f"AUTO-TUNE RUNNING  (1 of 2: RX gain)  --  pinging {target}...")
        self._log(f"Auto-Tune: pinging {target} to set RX gain (target "
                  f"{tuning.TARGET_LEVEL_DBFS:.0f} dBFS) and collect the two-way report...")
        self._autotune_step()

    def _autotune_step(self):
        at = getattr(self, "autotune", None)
        if not at:
            return
        if not self.engine:
            self._finish_autotune("audio engine stopped")
            return
        if at["remaining"] <= 0:
            self._finish_autotune()
            return
        at["remaining"] -= 1
        n = self.AUTOTUNE_MAX_PINGS - at["remaining"]
        self._set_tune_status("running", f"AUTO-TUNE RUNNING  (1 of 2: RX gain)  --  ping {n} "
                                         f"to {at['target']}, RX gain {self._gain_db():+.0f} dB...")
        self._send_ping()
        at["seq"] = self.seq_counter

    def _autotune_on_pong(self, msg, frame):
        at = self.autotune
        if not at.get("contact") and at.get("raised"):
            self._log(f"Auto-Tune: {at['target']} answers at TX level {self._levels()[1] * 100:.0f}% "
                      f"(no reply at {at['start_tx'] * 100:.0f}%) -- keeping it; Tune TX fine-tunes it next.")
        at["contact"] = True
        at["frame"] = frame
        if msg.report_snr_db is not None:
            at["report"] = (msg.report_snr_db, msg.report_level_dbfs)
        if frame.level_dbfs is not None:
            cur = self._gain_db()
            new, note = tuning.recommend_rx_gain(frame.level_dbfs, cur)
            if tuning.in_target(frame.level_dbfs):
                at["good"] += 1
            else:
                at["good"] = 0
                if round(new) != round(cur):
                    self._log(f"Auto-Tune: frames at {frame.level_dbfs:.0f} dBFS -> RX gain "
                              f"{cur:+.0f} dB -> {new:+.0f} dB")
                    self.rx_gain_db_var.set(str(int(new)))
            if note:
                at["note"] = note
        if at["good"] >= 2:
            self._finish_autotune()
        else:
            self.after(1500, self._autotune_step)

    # v0.9.41: before the first reply, a miss may mean they hear us too
    # weakly (FM: too little deviation), so TX level steps up between tries
    AUTOTUNE_CONTACT_TX = (0.55, 0.70, 0.85)
    # v0.9.50: ...but just as often the miss means TOO MUCH audio: the radio
    # clips the signal and the other station hears it strong yet garbled
    # (DX1BBC, 0.9.49: 40/55/70/85% all failed at DW1IRF, 20% worked; both
    # earlier tuned pairs settled at 14%). So the first retry goes DOWN
    # to about half the starting level, then the climb as before.
    AUTOTUNE_MAX_CLIPPED = 3      # v0.9.50: quieter-reply requests before going on as usual
    AUTOTUNE_LOWER_FACTOR = 0.5
    AUTOTUNE_LOWER_MIN = 0.15

    @classmethod
    def autotune_contact_plan(cls, start: float) -> list:
        """TX levels Auto-Tune tries, in order, after a miss at `start`."""
        plan = []
        low = max(cls.AUTOTUNE_LOWER_MIN, round(start * cls.AUTOTUNE_LOWER_FACTOR, 2))
        if low < start - 0.04:
            plan.append(low)
        plan += [lv for lv in cls.AUTOTUNE_CONTACT_TX if lv > start + 0.04]
        return plan

    def _autotune_on_timeout(self):
        at = self.autotune
        at["misses"] += 1
        if not at.get("contact") and at.pop("reply_clipped", False):
            # v0.9.50: they answered -- it's THEIR level that's too high; ours stays
            at["clipped"] = at.get("clipped", 0) + 1
            if at["clipped"] <= self.AUTOTUNE_MAX_CLIPPED:
                at["remaining"] = max(at["remaining"], 2)
                at["misses"] -= 1
                self._log(f"Auto-Tune: {at['target']} answered, but clipped -- keeping your TX level "
                          f"and asking {at['target']} to answer quieter...")
                self._set_tune_status("running", f"AUTO-TUNE RUNNING  --  {at['target']} answers too "
                                                 f"loud, asking it to answer quieter...")
                self.after(500, self._autotune_step)
                return
        if not at.get("contact"):
            cur = self._levels()[1]
            plan = at.get("plan")
            if plan is None:
                plan = at["plan"] = self.autotune_contact_plan(at.get("start_tx", cur))
            nxt = plan[len(at["raised"])] if len(at["raised"]) < len(plan) else None
            if nxt is not None:
                at["raised"].append(nxt)
                at["remaining"] = max(at["remaining"], 2)
                self._set_tx_level_pct(int(round(nxt * 100)))
                why = ("an overdriven radio clips the signal -- they hear it strong but garbled"
                       if nxt < cur else
                       "on FM, more audio = more deviation = better S/N at their end")
                self._log(f"Auto-Tune: no reply from {at['target']} at TX level {int(round(cur * 100))}% "
                          f"-- trying {int(round(nxt * 100))}% ({why})")
                self._set_tune_status("running", f"AUTO-TUNE RUNNING  --  no reply yet, trying TX level "
                                                 f"{int(round(nxt * 100))}%...")
                self.after(500, self._autotune_step)
                return
        if at["misses"] >= self.AUTOTUNE_MAX_MISSES + len(at.get("raised", [])):
            self._finish_autotune("no reply")
        else:
            self.after(500, self._autotune_step)

    def _set_tx_level_pct(self, pct: int):
        self.tx_level_var.set(str(pct))
        try:
            self._apply_levels()
        except Exception:
            if self.engine is not None:
                self.engine.tx_level = pct / 100.0

    def _finish_autotune(self, problem: str = ""):
        at, self.autotune = self.autotune, None
        if self.engine:
            self.ping_btn.configure(state="normal")
            self.autotune_btn.configure(state="normal")
        lines = [f"Auto-Tune result ({at['target']}):"]
        f = at.get("frame")
        if (problem == "no reply" or f is None) and at.get("clipped"):
            lines.append(f"  {at['target']} HEARS you and answers, but its replies arrive here strong and "
                         f"CLIPPED -- its radio is overdriven, even after asking it to answer quieter "
                         f"(it may run a version older than 0.9.50). At {at['target']}: lower the TX level "
                         f"to about 20% (or less), then run Auto-Tune to you. Your levels are fine.")
            if at.get("raised") and self.engine is not None:
                self._set_tx_level_pct(int(round(at["start_tx"] * 100)))
        elif problem == "no reply" or f is None:
            tried = ", ".join(f"{int(round(x * 100))}%" for x in [at.get("start_tx", 0)] + at.get("raised", []))
            lines.append(f"  No reply from {at['target']} (tried TX level {tried}, BPSK). Check: same "
                         f"frequency, {at['target']} has SARA FM started and idle, and your radio keys "
                         f"(TX lamp, radio's TX light). Ask them to watch their RX meter while you press "
                         f"TUNE 10s: no movement = they don't hear you at all.")
            if at.get("raised") and self.engine is not None:
                self._set_tx_level_pct(int(round(at["start_tx"] * 100)))
                lines.append(f"  (TX level put back to {int(round(at['start_tx'] * 100))}%.)")
        else:
            gain = self._gain_db()
            lines.append(f"  You hear {at['target']}: S/N {f.snr_db:+.0f} dB "
                         f"({tuning.snr_verdict(f.snr_db)}), level {f.level_dbfs:.0f} dBFS "
                         f"({tuning.level_verdict(f.level_dbfs)}) with RX gain {gain:+.0f} dB")
            if at.get("clipped"):
                lines.append(f"  -> {at['target']}'s replies first arrived CLIPPED (overdriven) and only "
                             f"got through once it was asked to answer quieter. At {at['target']}: run "
                             f"Auto-Tune to you (or lower its TX level to about 20%).")
            if at.get("report"):
                snr, raw = at["report"]
                lines.append(f"  {at['target']} hears you: S/N {snr:+d} dB ({tuning.snr_verdict(snr)}), "
                             f"input {raw:d} dBFS before their RX gain")
                for adv in tuning.report_advice(snr, raw, at["target"]):
                    lines.append("  -> " + adv)
            else:
                lines.append(f"  ({at['target']} runs an older SARA-PP without the two-way report.)")
            if at.get("note"):
                lines.append("  -> " + at["note"])
            if self.engine and getattr(self.engine, "raw_peak", 0.0) >= tuning.CLIP_RAW_PEAK:
                lines.append("  -> Your sound card input is CLIPPING: lower the radio's volume "
                             "(or the Windows recording level). RX gain can't undo clipping.")
            cs = tuning.carrier_sense_advice(tuning.carrier_sense_margin_db(
                f.level_dbfs, getattr(self.engine, "noise_floor_rms", None) if self.engine else None))
            if cs:
                lines.append("  -> " + cs)
            if not any(l.startswith("  ->") for l in lines):
                lines.append("  -> Levels look good. RX gain saved.")
            self._save_settings()
        for l in lines:
            self._log(l)
        w = self._ping_window(open_it=False)
        if w is not None:
            w.set_advice([l.strip() for l in lines[1:]])
        # v0.9.24: one button for both -- after the RX gain, tune the TX level
        # too (the other station needs v0.9.14+ to answer; an older one just
        # leaves the TX level as it was, with a note in the log).
        # v0.9.78: what the final verdict needs from this half
        rep_ = at.get("report")
        self._tune_ctx = {
            "target": at["target"], "reply": f is not None and problem != "no reply",
            "their_snr": rep_[0] if rep_ else None, "their_raw": rep_[1] if rep_ else None,
            "clipped": bool(at.get("clipped")) and (f is None or problem == "no reply"),
            "my_clip": isinstance(getattr(self.engine, "raw_peak", None), (int, float))
                       and self.engine.raw_peak >= tuning.CLIP_RAW_PEAK,
            "rx_note": at.get("note") or "",
        }
        if not problem and f is not None and self.engine:
            self._log("Auto-Tune: now tuning the TX level (Tune TX)...")
            self._autotune_rx_note = f"RX gain {self._gain_db():+.0f} dB"
            self._set_tune_status("running", f"AUTO-TUNE RUNNING  (2 of 2: TX level)  --  "
                                             f"{self._autotune_rx_note} set, now testing TX levels...")
            self.after(1500, self._chain_txtune)
        else:
            self._autotune_chain = False
            ctx = self._tune_ctx
            if problem not in ("", "no reply") and f is not None:
                self._show_tune_verdict(("failed", "TUNE STOPPED", problem))
            else:
                self._show_tune_verdict(tuning.tune_verdict(
                    at["target"], reply=False if not ctx["clipped"] else True,
                    their_replies_clipped=ctx["clipped"], my_clip=ctx["my_clip"]))

    def _verdict_from(self, ctx: dict, data_db=None, data_flat: bool = False):
        try:
            tx = int(round(float(self.tx_level_var.get())))
        except ValueError:
            tx = None
        return tuning.tune_verdict(
            ctx.get("target") or self.target_var.get().strip().upper(),
            reply=ctx.get("reply", True), their_snr=ctx.get("their_snr"), their_raw=ctx.get("their_raw"),
            data_db=data_db, data_flat=data_flat, tx_pct=tx, rx_gain_db=self._gain_db(),
            my_clip=ctx.get("my_clip", False), their_replies_clipped=ctx.get("clipped", False),
            rx_note=ctx.get("rx_note", ""), gateway_self=self._is_gateway())

    def _show_tune_verdict(self, verdict, target: str | None = None):
        """v0.9.78: the one answer, in the big banner, the Ping window and the log."""
        state, head, action = verdict
        self._set_tune_status(state, f"TUNE: {head}  --  {action}")
        self._log(f"TUNE RESULT: {head} -- {action}")
        # v0.9.79: the ping sent right after Tune shows this answer too, instead
        # of the old Tune TX note + generic advice (field: volunteers only ever
        # saw "Tune TX: TX level 10% -- ... S/N reads lower here ...")
        tgt = (target or self.target_var.get()).strip().upper()
        self._tune_note = (tgt, time.time(), [f"{head}: {action}"])
        w = self._ping_window(open_it=False)
        if w is not None:
            try:
                w.set_advice([f"{head}: {action}", "(The numbers behind it are in the log.)"])
            except Exception:
                pass

    def _chain_txtune(self):
        self._start_txtune()
        if not getattr(self, "txtune", None):       # couldn't start (busy, stopped...)
            self._autotune_chain = False
            ctx = getattr(self, "_tune_ctx", {}) or {}
            self._show_tune_verdict(self._verdict_from(ctx, data_db=None))
            tgt = self.target_var.get().strip().upper()
            self.after(50, lambda c=tgt: self._store_profile(c, why="Auto-Tune"))

    # ── Tune TX (v0.9.14) ──────────────────────────────────────────
    # Field (v0.9.12-13): DV1UK read DW1IRF's frames at 30-38 dB on the
    # preamble but only 9-13 dB on the data carriers (the DISTORTED warning)
    # -- 16QAM Wide sat at the edge and 32/64QAM was out of reach. The data
    # carriers suffer when the transmit audio overdrives the radio (or the
    # interface). Tune TX pings the other station in QPSK Wide at several TX
    # levels -- the current one, one step up when there's room, then down in
    # 3 dB steps -- and the PONG reports the carrier S/N at each (both
    # stations need v0.9.14). The loudest level within TXTUNE_CLEAN_MARGIN_DB
    # of the best carrier S/N is kept.
    TXTUNE_STEPS_DB = (0.0, 3.0, -3.0, -6.0, -9.0, -12.0)
    TXTUNE_PINGS_PER_STEP = 2
    TXTUNE_CLEAN_MARGIN_DB = 1.0
    TXTUNE_MIN_GAIN_DB = 2.0      # less than this better than now: keep the current level
    TXTUNE_MAX_MISSES = 3
    TXTUNE_MAX_PAUSES = 2         # v0.9.61: times the other station may go quiet mid-sweep
    TXTUNE_PAUSE_MS = 8000

    def _start_txtune(self):
        if (not self.engine or self.batch_running or getattr(self, "autotune", None)
                or getattr(self, "txtune", None)):
            return
        if not self._ping_allowed():
            return
        target = self.target_var.get().strip().upper()
        if not target or target == proto.BROADCAST:
            messagebox.showwarning("Tune TX", "Enter the other station's callsign as Target first "
                                              "(it must be running SARA-PP v0.9.14 or newer).")
            return
        start = self._levels()[1]
        levels = []
        for d in self.TXTUNE_STEPS_DB:
            lv = round(start * 10 ** (d / 20.0), 2)
            if 0.05 <= lv <= 1.0 and lv not in levels:
                levels.append(lv)
        self.txtune = {"target": target, "start": start, "levels": levels, "i": 0, "n": 0,
                       "seq": None, "results": {lv: [] for lv in levels}, "pre": {lv: [] for lv in levels},
                       "misses": 0, "any_reply": False, "missed": {lv: 0 for lv in levels},
                       "stopped_low": None,
                       # v0.9.41: QPSK Wide only when they hear us well; a weak
                       # link is tuned in BPSK so the pings get through at all
                       "mod": App.txtune_mode(
                           getattr(self, "_far_snr", {}).get(target, getattr(self, "_far_snr", {}).get(
                               target.split("-")[0])),
                           getattr(self, "_near_q", {}).get(target, getattr(self, "_near_q", {}).get(
                               target.split("-")[0]))),
                       "back_lost": 0}
        for b in (self.ping_btn, self.autotune_btn, self.txtune_btn):
            b.configure(state="disabled")
        if not getattr(self, "_autotune_chain", False):
            self._set_tune_status("running", f"TUNE TX RUNNING  --  testing TX levels with {target}...")
        self._log(f"Tune TX: pinging {target} in {self._mod_name(self.txtune['mod'])} at TX levels "
                  f"{', '.join(f'{int(round(lv * 100))}%' for lv in levels)} "
                  f"(now {int(round(start * 100))}%) -- {target} reports how clean each one arrives.")
        self._txtune_step()

    # v0.9.55: the test pings AND their replies travel in the test mode, so
    # QPSK Wide needs BOTH directions strong. Field (DU3FS -> DW3TDJ, 0.9.54):
    # DW3TDJ heard DU3FS at 28 dB, so the sweep ran in QPSK Wide -- but DU3FS
    # hears DW3TDJ at 16 dB with data carriers ~7 dB, the replies failed to
    # decode (fec_uncorrectable), and Tune TX reported "57%: overdriven?,
    # 28%: too quiet" and kept a distorted 40%.
    TXTUNE_WIDE_MIN_SNR_DB = 16
    TXTUNE_WIDE_MIN_CARRIER_DB = 10
    TXTUNE_BACK_LOST_SWITCH = 2

    @classmethod
    def txtune_mode(cls, far_snr, near) -> int:
        """Test mode: QPSK Wide only when they hear us AND we hear them well."""
        if far_snr is None or far_snr < cls.TXTUNE_WIDE_MIN_SNR_DB:
            return ofdm.MOD_BPSK
        if near is not None:
            n_snr, n_car = near
            if n_snr is not None and n_snr < cls.TXTUNE_WIDE_MIN_SNR_DB:
                return ofdm.MOD_BPSK
            if n_car is not None and n_car < cls.TXTUNE_WIDE_MIN_CARRIER_DB:
                return ofdm.MOD_BPSK
        return ofdm.MOD_QPSK_WIDE

    def _txtune_step(self):
        tt = getattr(self, "txtune", None)
        if not tt:
            return
        tt["sent_t"] = time.time()
        if not self.engine:
            self._finish_txtune("audio engine stopped")
            return
        if tt["i"] >= len(tt["levels"]):
            self._finish_txtune()
            return
        self.engine.tx_level = tt["levels"][tt["i"]]
        head = ("AUTO-TUNE RUNNING  (2 of 2: TX level)" if getattr(self, "_autotune_chain", False)
                else "TUNE TX RUNNING")
        self._set_tune_status("running", f"{head}  --  TX {int(round(tt['levels'][tt['i']] * 100))}%  "
                                         f"(level {tt['i'] + 1} of {len(tt['levels'])}) to {tt['target']}...")
        self._send_ping(modulation=tt.get("mod", ofdm.MOD_QPSK_WIDE), want_carrier=True)
        tt["seq"] = self.seq_counter

    def _txtune_next(self):
        tt = self.txtune
        tt["n"] += 1
        if tt["n"] >= self.TXTUNE_PINGS_PER_STEP:
            tt["i"], tt["n"] = tt["i"] + 1, 0
        self.after(1200, self._txtune_step)

    def _txtune_on_pong(self, msg, frame):
        tt = self.txtune
        tt["any_reply"] = True
        tt["misses"] = 0
        lv = tt["levels"][tt["i"]]
        ctl = tt.pop("control", None)
        if msg.report_carrier_snr_db is not None:
            tt["results"][lv].append(msg.report_carrier_snr_db)
            if msg.report_snr_db is not None:
                tt["pre"][lv].append(msg.report_snr_db)
        elif msg.report_snr_db is not None and len(frame.payload) == 22:
            self._finish_txtune("old version")   # answered without the carrier byte
            return
        if ctl is not None:
            # v0.9.61: they answer at a level that worked before, so the
            # quieter one really got no reply -- the sweep ends there
            tt["stopped_low"] = ctl["dead"]
            self._finish_txtune()
            return
        self._txtune_next()

    def _txtune_on_timeout(self):
        tt = self.txtune
        ctl = tt.pop("control", None)
        if ctl is not None and tt.get("sent_t") and getattr(self, "_last_lost_rx_t", 0.0) > tt["sent_t"] + 0.3:
            tt["stopped_low"] = ctl["dead"]          # a reply came, it just didn't decode
            self._finish_txtune()
            return
        if ctl is not None:
            # v0.9.61: no reply at a level that DID work a moment ago either --
            # the other station stopped answering (busy, its PC falling
            # behind), not our level. Field (0.9.60, DW1IRF -> DV1UK): DV1UK's
            # PC fell 3 s behind its audio during the sweep; 57% and 28% got
            # no reply and Tune TX kept a distorted 40% ("as clean as it gets"
            # at data carriers 6 dB). Forget those misses and test them again.
            tt["pauses"] = tt.get("pauses", 0) + 1
            for l in tt["levels"]:
                if not tt["results"][l]:
                    tt["missed"][l] = 0
            if tt["pauses"] > self.TXTUNE_MAX_PAUSES:
                self._finish_txtune()
                return
            redo = [k for k, l in enumerate(tt["levels"]) if not tt["results"][l]]
            tt["i"], tt["n"], tt["misses"] = (redo[0] if redo else ctl["dead_i"]), 0, 0
            self._log(f"Tune TX: {tt['target']} stopped answering even at "
                      f"{int(round(ctl['level'] * 100))}%, which worked before -- busy, or its PC "
                      f"fell behind. Waiting {self.TXTUNE_PAUSE_MS // 1000} s, then testing the "
                      f"levels that got no reply again.")
            self.after(self.TXTUNE_PAUSE_MS, self._txtune_step)
            return
        # v0.9.55: a reply arrived but didn't decode -- the way BACK can't carry
        # the test mode. Restart the sweep in BPSK instead of blaming the level.
        if (tt.get("mod") != ofdm.MOD_BPSK and tt.get("sent_t")
                and getattr(self, "_last_lost_rx_t", 0.0) > tt["sent_t"] + 0.3):
            tt["back_lost"] = tt.get("back_lost", 0) + 1
            if tt["back_lost"] >= self.TXTUNE_BACK_LOST_SWITCH:
                self._log(f"Tune TX: {tt['target']}'s replies arrive but don't decode in "
                          f"{self._mod_name(tt['mod'])} -- the way back is weaker. Starting again in BPSK.")
                tt["mod"] = ofdm.MOD_BPSK
                tt.update(i=0, n=0, misses=0, any_reply=False, stopped_low=None, back_lost=0,
                          results={lv: [] for lv in tt["levels"]}, pre={lv: [] for lv in tt["levels"]},
                          missed={lv: 0 for lv in tt["levels"]})
                if self.engine:
                    self.engine.tx_level = tt["start"]
                self.after(1200, self._txtune_step)
                return
            self._txtune_next()                  # resend at this level; not the level's fault
            return
        tt["misses"] += 1
        lv = tt["levels"][tt["i"]]
        tt["missed"][lv] += 1
        if not tt["any_reply"]:
            if tt["misses"] >= self.TXTUNE_MAX_MISSES:
                self._finish_txtune("no reply")
                return
        elif (tt["missed"][lv] >= self.TXTUNE_PINGS_PER_STEP and lv < tt["start"]
              and not tt["results"][lv]):
            # v0.9.15: field (DW1IRF, SignaLink VOX): at 9% the VOX never keyed,
            # three misses ended Tune TX and threw away the good measurements.
            # A quieter level with no reply at all ends the sweep instead --
            # quieter still won't key either -- and the results so far count.
            # v0.9.61: ...once one ping at a level that answered before shows
            # they're still listening (see the control check above).
            good = [l for l in tt["levels"] if tt["results"][l]]
            if good and tt.get("pauses", 0) <= self.TXTUNE_MAX_PAUSES:
                ref = min(good, key=lambda l: abs(l - lv))
                tt["control"] = {"dead": lv, "dead_i": tt["i"], "level": ref}
                tt["i"], tt["n"] = tt["levels"].index(ref), self.TXTUNE_PINGS_PER_STEP - 1
                self.after(1200, self._txtune_step)
                return
            tt["stopped_low"] = lv
            self._finish_txtune()
            return
        self._txtune_next()

    @staticmethod
    def txtune_choose(results: dict, start: float, margin_db: float = 1.0,
                      min_gain_db: float = 2.0, dead=()):
        """(chosen level, {level: mean carrier S/N}) from the measurements;
        chosen is None when nothing was measured. The loudest level within
        margin_db of the best wins -- but the current level stays unless that
        is at least min_gain_db cleaner than it (measurement noise).

        v0.9.50: `dead` = quieter levels that got no reply at all (VOX didn't
        key). The level just above one of those is on the edge: field
        (DW1IRF VOX, 0.9.49): 10% never keyed, 14% was kept, and DX1BBC then
        missed most of DW1IRF's pongs. If the next level up is about as
        clean, it is taken instead."""
        means = {lv: sum(v) / len(v) for lv, v in results.items() if v}
        if not means:
            return None, means
        best = max(means.values())
        chosen = max(lv for lv, m in means.items() if m >= best - margin_db)
        if start in means and means[chosen] - means[start] < min_gain_db:
            chosen = start
        below = [lv for lv in means if lv < chosen]
        if any(d < chosen for d in dead) and not below:
            up = sorted(lv for lv in means if lv > chosen)
            if up and means[up[0]] >= means[chosen] - margin_db:
                chosen = up[0]
        return chosen, means

    @staticmethod
    def tune_window_note(tt: dict, chosen: float, means: dict, target: str) -> list:
        """v0.9.52: what the Ping window says after Tune TX. Field (DX1BBC /
        DW1IRF): mid-tune pings showed S/N +25 dB, all green; the final one at
        the level chosen showed +16..20, partly light green -- it looked like
        Auto-Tune had picked something worse. Overall S/N comes from the
        repeated preamble, which a clipped signal still repeats perfectly, so
        it rises with TX level; the data carriers show the clipping."""
        pct = int(round(chosen * 100))
        out = [f"Tune TX: TX level {pct}% -- data carriers {means[chosen]:.0f} dB at {target} "
               f"({tuning.carrier_verdict(means[chosen])})."]
        pre = {lv: sum(v) / len(v) for lv, v in tt.get("pre", {}).items() if v}
        if chosen in pre and pre:
            loud = max(pre, key=lambda lv: pre[lv])
            if loud > chosen and pre[loud] - pre[chosen] >= 2 and means.get(loud) is not None:
                out.append(f"S/N reads lower here than at {int(round(loud * 100))}% "
                           f"({pre[loud]:.0f} -> {pre[chosen]:.0f} dB): less FM deviation. S/N can't see "
                           f"clipping -- the data carriers can ({means[loud]:.0f} dB at "
                           f"{int(round(loud * 100))}%, {means[chosen]:.0f} dB at {pct}%), and speed "
                           f"follows the carriers.")
        return out

    def _finish_txtune(self, problem: str = ""):
        tt, self.txtune = self.txtune, None
        if self.engine:
            for b in (self.ping_btn, self.autotune_btn, self.txtune_btn):
                b.configure(state="normal")
        target, start = tt["target"], tt["start"]
        lines = [f"Tune TX result ({target}):"]
        dead = [l for l, n in tt["missed"].items() if n and not tt["results"].get(l)]
        chosen, means = self.txtune_choose(tt["results"], start, self.TXTUNE_CLEAN_MARGIN_DB,
                                           self.TXTUNE_MIN_GAIN_DB, dead=dead)
        plain, _ = self.txtune_choose(tt["results"], start, self.TXTUNE_CLEAN_MARGIN_DB,
                                      self.TXTUNE_MIN_GAIN_DB)
        vox_edge = chosen is not None and plain is not None and chosen != plain
        if problem == "old version":
            lines.append(f"  {target} answers, but runs an older SARA-PP that doesn't report the "
                         f"carrier S/N -- Tune TX needs v0.9.14 or newer at both stations.")
        elif problem or chosen is None:
            lines.append(f"  No usable replies from {target} ({problem or 'no carrier reports'}). "
                         f"Check that it runs v0.9.14 or newer (older versions don't answer a "
                         f"Tune TX ping) and is idle, then try again.")
        else:
            row = []
            for lv in sorted(set(means) | {l for l, n in tt["missed"].items() if n}, reverse=True):
                if lv not in means:
                    row.append(f"  TX {int(round(lv * 100)):3d}%: no reply"
                               + ("  (too quiet to key VOX, or to be heard)" if lv < start else
                                  "  (overdriven?)"))
                    continue
                pre = tt["pre"].get(lv) or []
                pre_s = f", overall {sum(pre) / len(pre):.0f}" if pre else ""
                mark = "  <- now" if lv == start else ""
                row.append(f"  TX {int(round(lv * 100)):3d}%: data carriers {means[lv]:.0f} dB{pre_s}{mark}")
            lines += row
            best = max(means.values())
            if chosen == start and len(means) == 1:
                # v0.9.61: one level measured is no comparison
                lines.append(f"  -> only {int(round(start * 100))}% could be measured, so nothing to compare "
                             f"it with -- TX level unchanged. Run Tune TX again while {target} is idle"
                             + (f"; data carriers {means[start]:.0f} dB are low, so try a lower TX level "
                                f"(e.g. 20%) if it keeps failing." if means[start] < tuning.CARRIER_LIGHT_GREEN_DB
                                else "."))
            elif chosen == start:
                if means.get(start, best) >= best - self.TXTUNE_MIN_GAIN_DB:
                    lines.append(f"  -> TX level {int(round(start * 100))}% is already as clean as it gets"
                                 + (" -- the carriers stay about the same at every level, so TX level isn't "
                                    "what limits them (look at the audio path: mic input vs data port, "
                                    "the other station's receive side)." if best - min(means.values()) < 3
                                    else "."))
            else:
                lines.append(f"  -> TX level {int(round(start * 100))}% -> {int(round(chosen * 100))}%: "
                             + (f"data carriers {means[start]:.0f} -> {means[chosen]:.0f} dB. Saved."
                                if start in means else
                                f"data carriers {means[chosen]:.0f} dB ({int(round(start * 100))}% got no "
                                f"reply). Saved.")
                             + (f" ({int(round(plain * 100))}% is right above the level that got no reply "
                                f"at all -- VOX may key late there, so the next level up, just as clean, "
                                f"is safer.)" if vox_edge else ""))
                self.tx_level_var.set(str(int(round(chosen * 100))))
        # v0.9.32: status banner
        chain = getattr(self, "_autotune_chain", False)
        self._autotune_chain = False
        head = "AUTO-TUNE" if chain else "TUNE TX"
        rx = (getattr(self, "_autotune_rx_note", "") + "  ·  ") if chain else ""
        # v0.9.78: one verdict
        ctx = dict(getattr(self, "_tune_ctx", None) or {}) if chain else {"target": target}
        if not chain:
            ctx["reply"] = bool(tt.get("any_reply"))
        if chosen is not None and not problem:
            pre = tt["pre"].get(chosen) or []
            if pre:
                ctx["their_snr"] = sum(pre) / len(pre)
            flat = len(means) > 1 and max(means.values()) - min(means.values()) < 3
            verdict = self._verdict_from(ctx, data_db=means[chosen], data_flat=flat)
        elif chain:
            verdict = self._verdict_from(ctx, data_db=None)
        else:
            verdict = (tuning.tune_verdict(target, reply=False)
                                    if not tt.get("any_reply") else
                                    ("failed", "TUNE TX STOPPED", f"No usable replies from {target}: "
                                     f"it needs SARA FM 0.9.14 or newer and must be idle. Try again."))
        if chain or (not problem and chosen is not None):
            self.after(50, lambda c=target, h=head: self._store_profile(c, why=h.title()))
        # restore / apply the GUI level to the engine
        self._apply_levels()
        if chosen is not None and not problem:
            self._save_settings()
        for l in lines:
            self._log(l)
        self._show_tune_verdict(verdict, target)  # v0.9.78: after the details, so it's the last word
        self._update_mode_controls()
        if chosen is not None and not problem:
            for l in App.tune_window_note(tt, chosen, means, target):
                self._log("  " + l)                   # v0.9.79: the details stay in the log
        if self.engine and chosen is not None and not problem:
            # v0.9.16: the Ping window showed the LAST test ping (e.g. a
            # deliberately quiet 13% one: "input -48 dBFS", yellow). One
            # ordinary ping at the level kept shows the real reading.
            self.after(1500, self._send_ping)

    def _handle_frame(self, frame: ofdm.DecodedFrame):
        my_call = self.call_var.get().strip().upper()
        try:
            msg = proto.PingPongMessage.unpack(frame.payload)
        except Exception:
            self._log(f"RX: undecodable payload ({len(frame.payload)} bytes), "
                       f"{self._signal_str(frame)}")
            return

        mod_name = self._mod_name(frame.modulation)
        self._note_frame(frame)
        if not (my_call and msg.from_call.upper() == my_call):
            self._note_carriers(frame)     # only the OTHER station's frames, never our echo
            try:
                self._chat_heard(msg.from_call, frame.snr_db, mod_name, "ping")   # v0.9.38
            except Exception:
                pass

        if msg.msg_type == proto.MSG_PING:
            self_heard = bool(my_call) and msg.from_call.upper() == my_call
            tag = "  [own TX heard locally — not your real link quality]" if self_heard else ""
            if self_heard and not self._sent_it(msg.seq):
                # v0.8.2: a PING "from us" with a sequence number we never sent
                # can't be our own echo -- another station is using our
                # callsign (field: a copied settings file gave both test
                # stations "K6IRF"; neither answered, each taking the other's
                # pings for its own echo).
                tag = "  [ANOTHER station is using YOUR callsign]"
                self._warn_duplicate_callsign(my_call)
            self._log(f"RX PING  seq={msg.seq}  {msg.from_call} -> {msg.to_call}  "
                       f"{self._signal_str(frame)}  [{mod_name}]{tag}{self._fec_note(frame)}")
            log_csv_row(my_call=my_call, peer_call=msg.from_call, seq=msg.seq,
                        event="RX_PING", quality=f"{frame.quality:.2f}",
                        note="own_tx_heard_locally" if self_heard else mod_name)
            if my_call and msg.is_for(my_call) and msg.from_call.upper() != my_call:
                raw = (frame.level_dbfs - self._gain_db()) if frame.level_dbfs is not None else None
                car = link_adapt.carrier_snr_db(frame) if msg.want_carrier else None
                cur = getattr(self.engine, "tx_level", None) if self.engine else None
                lvl = None
                if cur is not None:
                    if getattr(msg, "ask_quieter", False):
                        before = App._pong_level_for(self, msg.from_call, cur)
                        lvl = App._on_quieter_request(self, msg.from_call, cur)
                        if abs(lvl - cur) < 1e-6 and before < cur - 1e-6:
                            self._log(f"  {msg.from_call} still says our replies arrive CLIPPED, even at "
                                      f"{before * 100:.0f}% -- so it isn't our TX level (look at "
                                      f"{msg.from_call}'s RX side). Answering at our normal "
                                      f"{cur * 100:.0f}% again.")
                            lvl = None
                        elif abs(lvl - before) < 1e-6:
                            lvl = None if abs(lvl - cur) < 1e-6 else lvl
                        else:
                            self._log(f"  {msg.from_call} says our replies reach it strong but CLIPPED "
                                      f"(overdriven): answering {msg.from_call} at TX level "
                                      f"{lvl * 100:.0f}% (was {before * 100:.0f}%). To fix it for good, "
                                      f"run Auto-Tune to {msg.from_call}.")
                    else:
                        lvl = App._pong_level_for(self, msg.from_call, cur)
                        if abs(lvl - cur) < 1e-6:
                            lvl = None
                threading.Thread(target=self._reply_pong,
                                  args=(msg.from_call, msg.seq, frame.modulation,
                                        frame.snr_db, raw, my_call, msg.want_carrier, car, lvl),
                                  daemon=True).start()

        elif msg.msg_type == proto.MSG_PONG:
            self_heard = bool(my_call) and msg.from_call.upper() == my_call
            tag = "  [own TX heard locally]" if self_heard else ""
            self._log(f"RX PONG  seq={msg.seq}  {msg.from_call} -> {msg.to_call}  "
                       f"{self._signal_str(frame)}  [{mod_name}]{tag}{self._fec_note(frame)}")
            if msg.seq in self.outstanding:
                getattr(self, "_quieter_ask", {}).pop(msg.from_call.upper(), None)   # v0.9.50
                getattr(self, "_ping_sent_t", {}).pop(msg.seq, None)
                rtt_ms = (time.time() - self.outstanding.pop(msg.seq)) * 1000
                self._log(f"  -> round trip {rtt_ms:.0f} ms  (PASS)")
                nq = getattr(self, "_near_q", None)            # v0.9.55: how WE hear them
                if nq is None:
                    nq = self._near_q = {}
                for k in (msg.from_call.upper(), msg.from_call.upper().split("-")[0]):
                    nq[k] = (frame.snr_db, link_adapt.carrier_snr_db(frame))
                if msg.report_snr_db is not None:
                    self._far_snr = getattr(self, "_far_snr", {})
                    self._far_snr[msg.from_call.upper()] = msg.report_snr_db      # v0.9.41
                    self._far_snr[msg.from_call.upper().split("-")[0]] = msg.report_snr_db
                    self._log(f"     {msg.from_call} hears you: S/N {msg.report_snr_db:+d} dB "
                              f"({tuning.snr_verdict(msg.report_snr_db)}), input "
                              f"{msg.report_level_dbfs:d} dBFS before their RX gain")
                w = self._ping_window(open_it=False)
                if w is not None and msg.seq == self._ping_win_seq:
                    report = ((msg.report_snr_db, msg.report_level_dbfs)
                              if msg.report_snr_db is not None else None)
                    w.set_result((frame.snr_db, frame.level_dbfs), report, rtt_ms / 1000.0,
                                 carriers=(link_adapt.carrier_snr_db(frame), msg.report_carrier_snr_db))
                    adv = (["-> " + a for a in tuning.report_advice(
                        *(report or (None, None)), msg.from_call,
                        carrier_db=msg.report_carrier_snr_db)] or ["Link looks good."])
                    tn = getattr(self, "_tune_note", None)      # v0.9.52: keep Tune's answer
                    if tn and tn[0] == msg.from_call.upper() and time.time() - tn[1] < 30:
                        adv = tn[2]                             # v0.9.79: only the answer
                    w.set_advice(adv)
                at = getattr(self, "autotune", None)
                if at and msg.seq == at.get("seq"):
                    self._autotune_on_pong(msg, frame)
                tt = getattr(self, "txtune", None)
                if tt and msg.seq == tt.get("seq"):
                    self._txtune_on_pong(msg, frame)
                log_csv_row(my_call=my_call, peer_call=msg.from_call, seq=msg.seq,
                            event="RX_PONG_MATCHED", rtt_ms=f"{rtt_ms:.0f}",
                            quality=f"{frame.quality:.2f}")
            else:
                log_csv_row(my_call=my_call, peer_call=msg.from_call, seq=msg.seq,
                            event="RX_PONG_UNMATCHED", quality=f"{frame.quality:.2f}")

    def _reply_pong(self, to_call: str, seq: int, modulation: int = None,
                    snr_db: float | None = None, raw_level_dbfs: float | None = None,
                    my_call: str | None = None, want_carrier: bool = False,
                    carrier_snr_db: float | None = None, tx_gain: float | None = None):
        # Echo back in the SAME modulation the ping arrived in, not whatever
        # this station's own dropdown happens to be set to -- that way one
        # station can be set to QPSK and tested in both directions without
        # needing the other station's dropdown touched at all.
        if modulation is None:
            modulation = self._modulation()
        time.sleep(PONG_REPLY_DELAY_S)
        # v0.7.3: callsign read on the GUI thread by the caller and passed in;
        # reading Tk variables from this worker thread is fragile.
        if my_call is None:
            my_call = self.call_var.get().strip().upper()
        mod_name = self._mod_name(modulation)
        # v0.7.0: the PONG carries how we heard their PING (two-way report)
        payload = proto.make_pong(my_call, to_call, seq, snr_db, raw_level_dbfs,
                                  carrier_snr_db, want_carrier)   # v0.9.14: Tune TX
        try:
            if tx_gain is None:
                self.engine.transmit(payload, modulation=modulation, ignore_dcd=True)  # as in VARA (v0.7.7)
            else:                     # v0.9.50: a quieter reply was asked for
                self.engine.transmit(payload, tx_gain=tx_gain, modulation=modulation, ignore_dcd=True)
            lv = f", TX level {tx_gain * 100:.0f}%" if tx_gain is not None else ""
            self.after(0, lambda: self._log(f"TX PONG  seq={seq}  {my_call} -> {to_call}  [{mod_name}{lv}]"))
            log_csv_row(my_call=my_call, peer_call=to_call, seq=seq, event="TX_PONG", note=mod_name)
        except Exception as e:
            self._report_error("TX PONG error", e)

    def _handle_air_frame_for_bridge(self, frame: ofdm.DecodedFrame):
        """BPQ32 mode: a decoded frame off the air is a raw AX.25 frame, not
        a ping/pong message -- hand it straight to the bridge to KISS-wrap
        and forward to BPQ32, no protocol.py parsing involved."""
        mod_name = self._mod_name(frame.modulation)
        n_frames = len(bpq_bridge.unpack_batch(frame.payload))
        frames_note = f" ({n_frames} frames)" if n_frames != 1 else ""

        if self.self_echo_filter.is_echo(frame.payload):
            # Local audio bleed-back of our own just-sent transmission --
            # not real incoming data. Forwarding this to BPQ32 would mean
            # it sees its own frame echoed back as if the other station
            # had sent it, which is exactly the kind of thing that
            # thoroughly confuses AX.25 session state. Log it distinctly
            # so it's not mistaken for a successful remote exchange.
            self._log(f"RX (air->BPQ32)  {len(frame.payload)} bytes{frames_note}  "
                       f"{self._signal_str(frame)}  [{mod_name}]  "
                       f"[own TX heard locally -- not forwarded to BPQ32]")
            return

        if chat.is_chat(frame.payload):
            self._on_chat_frame(frame)        # v0.9.38: never forwarded to BPQ32
            return

        if proto.is_pingpong(frame.payload):
            # v0.7.3: answer pings while connected to BPQ32, like VARA FM.
            # Never forwarded to BPQ32 -- see proto.is_pingpong for why this
            # can't catch real AX.25 traffic.
            self._handle_bridge_pingpong(frame)
            return
        ax_frames = bpq_bridge.unpack_batch(frame.payload)
        if link_adapt.session_frames(ax_frames):
            self._last_session_activity = time.time()      # v0.9.48: not for UI beacons
        try:
            self._note_peer_burst(ax_frames)               # v0.9.88
        except Exception:
            pass
        # v0.9.37: frames still on their way through a digipeater, and our own
        # frames repeated back by one, aren't this station's session traffic
        mine = self._my_calls()
        own_bases = {link_adapt.base_call(c) for c in mine}
        # v0.9.63: "our own frame repeated back" means a frame THIS station
        # transmitted, not any frame whose source shares our callsign. Field
        # (0.9.62, DX1FLR -> DV1UK-10 via DW1IRF): DX1FLR's connect went out
        # with source DV1UK; DV1UK took the relayed SABM for its own echo and
        # never passed it to its BPQ32 -- DV1UK-10 never answered.
        own_copy = [f for f in ax_frames if link_adapt.is_relayed(f) and self._we_sent(f)]
        if not own_copy:
            # v0.9.64: exact callsign incl. SSID -- DV1UK, DV1UK-4 (BBS) and
            # DV1UK-10 (gateway) are different stations
            foreign = [f for f in ax_frames if link_adapt.is_relayed(f)
                       and link_adapt.norm_call((link_adapt.ax25_parse(f) or ("", ""))[1]) in mine]
            if foreign and not getattr(self, "_warned_own_call", False):
                self._warned_own_call = True
                self._log(f"NOTE: a frame from {(link_adapt.ax25_parse(foreign[0]) or ('', '?'))[1]} "
                          f"-- exactly this station's callsign -- came through a digipeater, but this "
                          f"station didn't send it ({link_adapt.path_str(foreign[0])}). It is passed on "
                          f"to BPQ32. If that isn't meant, another station (or its BPQ32 / Winlink "
                          f"Express) is set to YOUR callsign.")
        session = [f for f in ax_frames if not link_adapt.in_transit(f) and f not in own_copy]
        try:
            # v0.9.61: acknowledgments (any copy) and the digipeater's turn
            self.stale.on_heard(ax_frames)
            self.relay_watch.heard(ax_frames)
            self.relay_watch.expect([f for f in ax_frames if link_adapt.in_transit(f)
                                     and link_adapt.base_call(link_adapt.ax25_hops(f)[0] or "")
                                     not in own_bases])
        except Exception:
            pass
        try:
            retry_note = self.retry_stats.on_traffic("rx", session) if session else None
        except Exception:
            retry_note = None
        if retry_note:
            self._log(retry_note)
        self._note_frame(frame)
        self._note_carriers(frame)
        self._note_link(frame)
        path = link_adapt.path_str(ax_frames[0]) if ax_frames else ""
        path_note = f"  {path}" if path else ""
        if own_copy and len(own_copy) == len(ax_frames):
            path_note += f"  [our frame{'s' if len(own_copy) > 1 else ''}, repeated by " \
                         f"{link_adapt.ax25_hops(own_copy[0])[1]}]"
        to_client = App.client_frames(ax_frames, own_bases, own_copy, self._digi_on())
        held = len(ax_frames) - len(to_client)
        if held and not own_copy:
            path_note += "  [heard direct -- not passed on, the relayed copy is]"
        self._log(f"RX (air->BPQ32)  {len(frame.payload)} bytes{frames_note}  "
                   f"{self._signal_str(frame)}  [{mod_name}]  drift={frame.drift_deg:.0f}°"
                   f"{self._fec_note(frame)}{path_note}  {{{link_adapt.batch_trace(ax_frames)}}}")
        self._maybe_digipeat(ax_frames)
        log_csv_row(my_call=self.call_var.get().strip().upper(), event="BPQ_RX",
                    quality=f"{frame.quality:.2f}",
                    note=f"{len(frame.payload)} bytes {mod_name} drift={frame.drift_deg:.0f}deg "
                         f"fec={frame.fec_corrected} snr={frame.snr_db:.0f}dB lvl={frame.level_dbfs:.0f}dBFS")
        self.rx_meter.add(sum(len(f) for f in bpq_bridge.unpack_batch(frame.payload)))
        # v0.9.72: our own frames heard repeated (the first hop is fine), and
        # direct copies held for a relay that may not decode (DirectFallback)
        now_t = time.time()
        for f in own_copy:
            self._own_relay_heard[link_adapt.relay_key(f)] = now_t
        if len(self._own_relay_heard) > 400:
            self._own_relay_heard = {k: t for k, t in self._own_relay_heard.items() if now_t - t < 120}
        if self.bridge:
            for f in ax_frames:
                if f not in to_client and f not in own_copy and link_adapt.in_transit(f) \
                        and self._for_my_client(f):
                    self.direct_fb.hold(f)
                    self.after(int(link_adapt.DirectFallback.FALLBACK_S * 1000) + 100, self._release_fallback)
            out = []
            for f in to_client:
                if link_adapt.is_relayed(f) and not link_adapt.in_transit(f):
                    first, keep = self.direct_fb.relayed(f)
                    if first:
                        self._log(f"  [relay brought later frames: passing on the direct copy of "
                                  f"{link_adapt.batch_trace(first)} it didn't carry]")
                    out += first
                    if not keep:
                        continue
                out.append(f)
            if out != to_client:
                held = True
            to_client = out
        if self.bridge and to_client:
            self._deliver_to_client(to_client, None if held else frame.payload)

    def _for_my_client(self, f: bytes) -> bool:
        """v0.9.72: is this frame addressed to our KISS client (BPQ32 /
        Winlink Express) -- its own callsigns, or this station's?"""
        p = link_adapt.ax25_parse(f)
        if p is None:
            return False
        dest = link_adapt.norm_call(p[0])
        mine = self._my_calls()
        return dest in self._client_calls or dest in mine \
            or link_adapt.base_call(dest) in {link_adapt.base_call(c) for c in mine}

    def _release_fallback(self):
        """GUI thread (v0.9.72): pass on held direct copies whose relay
        never came."""
        frames = self.direct_fb.due()
        if not frames or not self.bridge:
            return
        self._log(f"  [relayed copy not decoded: passing on the direct copy of "
                  f"{link_adapt.batch_trace(frames)} heard {link_adapt.DirectFallback.FALLBACK_S:.0f} s ago]")
        self._deliver_to_client(frames, None)

    def _deliver_to_client(self, to_client: list, raw_payload=None):
        """Frames to BPQ32 / Winlink Express: gateway path translation
        (v0.9.71), B2 checks, then the KISS client."""
        changed = False
        if self._gw_direct():
            out = []
            for f in to_client:
                g, note = self.via_map.to_client(f)
                if note:
                    self._log(f"Gateway: {note}.")
                changed |= g is not f
                out.append(g)
            to_client = out
        try:
            for f in to_client:
                # v0.9.74: only sessions with our client -- field (0.9.73): DX1FLR
                # overheard DW1IRF's direct session with DV1UK-10 in part and
                # logged a "broken" B2 message that wasn't its own
                if self._for_my_client(f):
                    self._b2_notes(self.b2watch.feed(f), given=True)
        except Exception:
            pass
        if self.bridge:
            self.bridge.deliver_from_air(raw_payload if raw_payload is not None and not changed
                                         else bpq_bridge.pack_batch(to_client))

    OWN_SENT_KEEP_S = 180.0

    def _note_we_sent(self, frames):
        """v0.9.63: remember our own transmitted frames (H bits cleared) to
        recognise them when a digipeater repeats them back."""
        now = time.time()
        d = getattr(self, "_own_sent", None)
        if d is None:
            d = self._own_sent = {}
        for k in [k for k, t in d.items() if now - t > self.OWN_SENT_KEEP_S]:
            del d[k]
        for f in frames:
            d[link_adapt.relay_key(f)] = now

    def _we_sent(self, frame: bytes) -> bool:
        d = getattr(self, "_own_sent", None) or {}
        t = d.get(link_adapt.relay_key(frame))
        return t is not None and time.time() - t <= self.OWN_SENT_KEEP_S

    def _digi_on(self) -> bool:
        v = getattr(self, "digi_var", None)
        try:
            return bool(v is not None and v.get())
        except Exception:
            return False

    @staticmethod
    def client_frames(ax_frames: list, own_bases: set, own_copy: list, digi_on: bool) -> list:
        """v0.9.57: the frames BPQ32 / Winlink Express should see. Field
        (DW1IRF -> DX1FLR-10 via DV1UK, 0.9.55-56): a 5 kB message failed the
        CMS B2 checksum twice, while every frame was relayed intact. Each end
        also heard the OTHER end directly (S/N 8-11 dB) and passed those copies
        on -- before the digipeater had repeated them -- so every frame reached
        the client twice, plus our own frames echoed back. A client that acts
        on a duplicated REJ resends a window twice, and with AX.25's modulo-8
        numbering a stale copy can be taken as new data. So:
        - a frame still waiting for a digipeater is held back, unless this
          station is that digipeater and BPQ32 does the repeating (SARA FM's
          own digipeater off: BPQ32 DIGIFLAG);
        - our own frames repeated back to us are held back."""
        out = []
        for f in ax_frames:
            if f in own_copy:
                continue
            if link_adapt.in_transit(f):
                nxt = link_adapt.ax25_hops(f)[0]
                if digi_on or link_adapt.base_call(nxt or "") not in own_bases:
                    continue
            out.append(f)
        return out

    # ── SARA Chat (v0.9.38) ──────────────────────────────────────────
    @staticmethod
    def _chat_dir() -> str:
        """Next to the program (SARA_CHAT_DIR overrides it -- the test suite)."""
        return os.environ.get("SARA_CHAT_DIR") or app_dir()

    def _chat_files_dir(self) -> str:
        d = os.path.join(self._chat_dir(), "chat_files")
        os.makedirs(d, exist_ok=True)
        return d

    def open_chat(self):
        if self.chat_win is None or not self.chat_win.winfo_exists():
            self.chat_win = chat_window.ChatWindow(self, self.chat)
        self.chat_win.deiconify()
        self.chat_win.lift()
        self.chat_win.show_conversation(self.chat_win.current)
        self._update_chat_button()

    def _chat_net(self, kind: str):
        self.open_chat()
        self.chat_win._net(kind)

    def _chat_can_send(self) -> bool:
        if self._chat_remote_on:
            return self.chat_client.ready()
        return self._radio_can_chat()

    def _radio_can_chat(self) -> bool:
        return self.engine is not None and not self._session_busy()

    def _chat_mode(self, peer, attempt: int) -> int:
        """v0.9.39: chat to a station uses Auto speed exactly like Winlink
        traffic: every receipt carries a link report (how they hear us), so
        the speed climbs as the S/N allows and steps down on losses and
        resends. NET (no receipts) always goes QPSK, or the fixed modulation."""
        if not self._auto():
            return self._modulation()
        if not peer:
            return ofdm.MOD_QPSK
        return self.speed.mode_for(peer)

    CHAT_FAST_FRAMES = 6      # v0.9.40: frames per burst at 16QAM/64QAM (each ~1 s)

    def _chat_burst_plan(self, peer: str):
        """(largest frame in bytes, frames per burst) at the speed chat will
        use to this station: two file pieces per frame and 6 frames per burst
        at 16QAM / 64QAM Wide, one piece and 4 frames otherwise."""
        if self._chat_remote_on:
            return chat.FRAME_CAP_NARROW, chat.WINDOW          # v0.9.75: the station picks the speed
        base = ofdm.base_mod(self._chat_mode(peer, 1))
        if base in (ofdm.MOD_QAM16_WIDE, ofdm.MOD_QAM64_WIDE):
            return chat.FRAME_CAP_FAST, self.CHAT_FAST_FRAMES
        return chat.FRAME_CAP_NARROW, chat.WINDOW

    def _chat_report(self, peer: str):
        if self._chat_remote_on:
            return None
        r = self.link_stats.take_report(peer)
        return r.pack() if r is not None else None

    def _chat_on_report(self, peer: str, extra: bytes):
        if self._chat_remote_on:
            self.chat_client.report(peer, extra)            # v0.9.75: about the station's radio
            return
        rep = link_adapt.parse_report(extra)
        if rep is None:
            return
        if self._verbose_rx:
            self._log(f"  [chat link report from {peer}: our {link_adapt.NAMES.get(rep.mode, rep.mode)} frames "
                      f"arrive at S/N {rep.snr_db} dB, {rep.n_ok} ok / {rep.n_bad} lost]")
        if self._auto():
            self.speed.on_report(peer, rep)

    @staticmethod
    def _chat_data_bytes(payload: bytes) -> int:
        """User data in a chat frame (message text or file piece), for the speed meters."""
        d = chat.unpack(payload)
        if d is None:
            return 0
        if d["type"] == chat.T_MSG:
            return len(d["text"].encode("utf-8"))
        if d["type"] == chat.T_CHUNK:
            return len(d["data"])
        if d["type"] == chat.T_CHUNKS:
            return sum(len(x[3]) for x in d["pieces"])
        return 0

    def _chat_transmit(self, payload: bytes, peer, attempt: int, on_sent) -> bool:
        if self._chat_remote_on:
            desc = self._chat_desc(payload)
            st = self.chat_client.station or "the station"

            def sent_remote():
                self.after(0, on_sent)
                self.after(0, lambda: self._log(f"CHAT TX  {desc}  [through {st}]"
                                                + (f"  (try {attempt})" if attempt > 1 else "")))
            return self.chat_client.transmit(payload, peer, attempt, sent_remote)
        if self.engine is None:
            return False
        # v0.9.75: the station's own operator and its remote operators see each
        # other's frames here; a frame for a remote operator isn't aired
        self._chat_local_deliver(payload, None)
        if self._chat_is_local(payload):
            self.after(0, on_sent)
            return True
        mod = self._chat_mode(peer, attempt)
        if len(payload) > chat.FRAME_CAP_NARROW and ofdm.base_mod(mod) not in (ofdm.MOD_QAM16_WIDE,
                                                                                ofdm.MOD_QAM64_WIDE):
            mod = ofdm.MOD_QAM16_WIDE_C      # a multi-piece frame needs a fast mode to decode
        desc = self._chat_desc(payload)
        n_data = self._chat_data_bytes(payload)
        if peer and n_data:
            self._last_tx_peer = peer          # the speed gauge shows the mode to this station

        def sent(_p, _m):
            self.after(0, on_sent)
            if n_data:
                self.after(0, lambda: self.tx_meter.add(n_data))
            self.after(0, lambda: self._log(f"CHAT TX  {desc}  [{self._mod_name(mod)}]"
                                            + (f"  (try {attempt})" if attempt > 1 else "")))
        self.engine.queue_transmit(payload, modulation=mod, on_sent=sent,
                                   on_error=lambda e: self._report_error("Chat TX error", e))
        return True

    @staticmethod
    def _chat_desc(payload: bytes) -> str:
        d = chat.unpack(payload)
        if d is None:
            return f"{len(payload)} bytes"
        to = "NET" if d["to"] == chat.NET else d["to"]
        t = d["type"]
        if t == chat.T_MSG:
            return f"message {d['frm']} -> {to}  ({len(d['text'])} chars)"
        if t == chat.T_ACK:
            return f"receipt {d['frm']} -> {to}  ({len(d['ids'])} received)"
        if t == chat.T_OFFER:
            return f"file {d['frm']} -> {to}  {d['name']} ({d['size']} bytes)"
        if t == chat.T_CHUNKS:
            idx = [x[2] + 1 for x in d["pieces"]]
            return f"file pieces {min(idx)}-{max(idx)} {d['frm']} -> {to}  ({len(idx)} in one frame)"
        return f"file piece {d['idx'] + 1} {d['frm']} -> {to}"

    def _on_chat_frame(self, frame):
        self._note_frame(frame)
        mod_name = self._mod_name(frame.modulation)
        me = chat.norm(self.call_var.get())
        pre = chat.unpack(frame.payload)
        srv = self.chat_server
        if srv is not None:
            srv.broadcast({"t": "rx", "p": chat_remote._b64(frame.payload),
                           "snr": link_adapt.link_snr_db(frame), "mode": mod_name})
        remote_calls = srv.calls() if srv is not None else set()
        if pre is not None and pre["frm"] != me and (pre["to"] in (me, chat.NET) or pre["to"] in remote_calls):
            # v0.9.39: how we hear them -- rides back on our receipts as a link report
            self.link_stats.on_frame(pre["frm"], frame.modulation, link_adapt.link_snr_db(frame),
                                     frame.fec_corrected, len(frame.payload),
                                     carrier_snr_db=link_adapt.carrier_snr_db(frame))
            self.rx_meter.add(self._chat_data_bytes(frame.payload))
        d = self.chat.on_frame(frame.payload, snr=link_adapt.link_snr_db(frame), mode=mod_name)
        if d is not None and d.get("frm") != me:
            self._note_carriers(frame)
        self._log(f"CHAT RX  {self._chat_desc(frame.payload)}  {self._signal_str(frame)}  [{mod_name}]")

    # ── SARA Chat over the network (v0.9.75) ─────────────────────────
    def _chat_heard(self, call, snr, mode, how):
        self.chat.on_heard(call, snr, mode, how)
        if self.chat_server is not None:
            self.chat_server.broadcast({"t": "heard", "call": call, "snr": snr, "mode": mode, "how": how})

    def _local_chat_calls(self) -> set:
        out = {chat.norm(self.call_var.get())}
        if self.chat_server is not None:
            out |= {chat.norm(c) for c in self.chat_server.calls()}
        return out

    def _chat_is_local(self, payload: bytes) -> bool:
        """A frame for someone on this station (its operator or a remote
        operator) needs no air time."""
        d = chat.unpack(payload)
        return d is not None and d["to"] != chat.NET and chat.norm(d["to"]) in self._local_chat_calls()

    def _chat_local_deliver(self, payload: bytes, from_conn):
        srv = self.chat_server
        if srv is not None:
            srv.broadcast({"t": "rx", "p": chat_remote._b64(payload), "snr": None, "mode": "this station"},
                          skip=from_conn)
        if from_conn is not None:
            self.chat.on_frame(payload, snr=None, mode="this station")

    def _start_chat_server(self):
        if not (getattr(self, "chat_serve_var", None) is not None and self.chat_serve_var.get()):
            return
        if self.chat_server is not None:
            return
        try:
            port = int(self.chat_port_var.get().strip() or chat_remote.DEFAULT_PORT)
        except ValueError:
            self._log("Remote chat: the port isn't a number -- not started.")
            return
        allow, err = bpq_bridge.parse_allowlist(self.kiss_allow_var.get())
        if err:
            self._log(f"Remote chat: {err} -- not started.")
            return
        srv = chat_remote.ChatServer(
            on_tx=lambda c, i, p, peer, att: self.after(0, lambda: self._remote_chat_tx(c, i, p, peer, att)),
            on_report=lambda peer, x: self.after(0, lambda: self._remote_chat_report(peer, x)),
            on_status=lambda m: self.after(0, lambda: self._log(m)),
            on_client=lambda c, joined: self.after(0, lambda: self._log(
                f"Remote chat: {c.call} {'connected from' if joined else 'left'} {c.addr[0]}")))
        srv.station = self.call_var.get().strip().upper()
        try:
            srv.start(port=port, allow=allow)
        except OSError as e:
            self._log(f"Remote chat: could not open port {port}: {e}")
            return
        self.chat_server = srv
        self._log(f"Remote chat ON (port {port}): operators connect with SARA FM > Setup > "
                  f"\"Chat through a remote SARA FM station\", host {', '.join(self._local_ipv4()) or 'this PC'}.")

    def _stop_chat_server(self):
        if self.chat_server is not None:
            self.chat_server.stop()
            self.chat_server = None

    def _remote_chat_tx(self, conn, mid, payload: bytes, peer, attempt: int):
        """GUI thread: a chat frame from a remote operator."""
        d = chat.unpack(payload)
        if d is None or self.chat_server is None:
            return
        self._chat_local_deliver(payload, conn)
        if self._chat_is_local(payload):
            conn.send({"t": "sent", "id": mid, "mode": "this station"})
            return
        if self.engine is None:
            return                                   # their engine resends later
        mod = self._chat_mode(peer, attempt)
        desc = self._chat_desc(payload)

        def sent(_p, _m):
            conn.send({"t": "sent", "id": mid, "mode": self._mod_name(mod)})
            self.after(0, lambda: self._log(f"CHAT TX  {desc}  [{self._mod_name(mod)}]  (for {conn.call}, "
                                            f"remote)" + (f"  (try {attempt})" if attempt > 1 else "")))
        self.engine.queue_transmit(payload, modulation=mod, on_sent=sent,
                                   on_error=lambda e: self._report_error("Chat TX error", e))

    def _remote_chat_report(self, peer: str, extra: bytes):
        rep = link_adapt.parse_report(extra)
        if rep is not None and self._auto() and peer:
            self.speed.on_report(peer, rep)

    def _apply_remote_chat(self):
        """Operator side: start or stop chatting through a remote station."""
        on = bool(self.chat_use_remote_var.get())
        self.chat_client.stop()
        self._chat_remote_on = False
        if on:
            host = self.chat_remote_host_var.get().strip()
            try:
                port = int(self.chat_remote_port_var.get().strip() or chat_remote.DEFAULT_PORT)
            except ValueError:
                port = 0
            if not host or not port:
                self._log("Remote chat: enter the station's address and port first.")
                self.chat_use_remote_var.set(False)
            else:
                self._chat_remote_on = True
                self.chat_client.start(host, port, call=self.call_var.get())
                self._log(f"Remote chat: connecting to {host}:{port} as {self.call_var.get().strip().upper()} ...")
        try:
            self._save_settings()
        except Exception:
            pass

    def _on_remote_chat_rx(self, payload: bytes, snr, mode: str):
        self.chat.on_frame(payload, snr=snr, mode=mode)
        self._log(f"CHAT RX  {self._chat_desc(payload)}  [{mode} via {self.chat_client.station or 'station'}]")

    def _on_remote_chat_status(self, msg: str):
        self._log(f"Remote chat: {msg}")
        try:
            if self.chat_win is not None:
                self.chat_win.status_var.set(f"Remote: {msg}")
        except Exception:
            pass

    def _on_chat_event(self, kind: str, data: dict):
        w = self.chat_win
        if w is not None:
            try:
                w.on_event(kind, data)
            except tk.TclError:
                pass
        e = data.get("entry") or {}
        if kind == "message" and e.get("dir") == "in":
            visible = w is not None and w.state() == "normal"
            if not visible:
                who = e.get("frm")
                where = " on the NET" if e.get("to") == chat.NET else ""
                self.status_var.set(f"New chat message from {who}{where} -- Chat menu or Chat button")
                try:
                    self.bell()
                except tk.TclError:
                    pass
        if kind in ("message", "status"):
            self._update_chat_button()

    def _update_chat_button(self):
        n = sum(self.chat.unread.values()) if hasattr(self, "chat") else 0
        try:
            self.chat_btn.configure(text=f"Chat ({n} new)" if n else "Chat")
        except (tk.TclError, AttributeError):
            pass

    # ── SARA FM digipeater (v0.9.37) ─────────────────────────────────
    DIGI_DUPE_S = 3.0     # the same frame decoded twice in one burst; NOT longer:
                          # an AX.25 retry (after FRACK) must be repeated again

    def _maybe_digipeat(self, ax_frames: list):
        """GUI thread. Digipeater on: repeat every frame whose next
        digipeater is this station's callsign or alias, as one burst."""
        if not (getattr(self, "digi_var", None) is not None and self.digi_var.get()) or not self.engine:
            return
        my_call = link_adapt.norm_call(self.call_var.get())
        aliases = [a for a in self.digi_alias_var.get().replace(",", " ").split()]
        now = time.time()
        recent = getattr(self, "_digi_recent", {})
        self._digi_recent = {k: t for k, t in recent.items() if now - t < self.DIGI_DUPE_S}
        out = []
        for f in ax_frames:
            nf = link_adapt.digipeat(f, my_call, aliases)
            if nf is None or nf in self._digi_recent:
                continue
            self._digi_recent[nf] = now
            out.append(nf)
        if not out:
            return
        relayed = getattr(self, "_relayed", {})
        self._relayed = {k: t for k, t in relayed.items() if now - t < 120.0}
        for nf in out:
            self._relayed[nf] = now
        mod = self._bridge_modulation(bpq_bridge.pack_batch(out))
        self.digi_count = getattr(self, "digi_count", 0) + len(out)
        self._log(f"DIGI: repeating {len(out)} frame{'s' if len(out) > 1 else ''}  "
                  f"{link_adapt.path_str(out[0])}  [{self._mod_name(mod)}]")
        # v0.9.58: re-pack for OUR transmit mode (see bpq_bridge.pack_for_mode)
        for batch in bpq_bridge.pack_for_mode(out, mod):
            self.engine.queue_transmit(batch, modulation=mod, on_sent=self._bridge_on_sent,
                                       on_error=lambda e: self._report_error("DIGI TX error", e))

    def _on_digi_change(self):
        on = bool(self.digi_var.get())
        names = ", ".join(sorted(self._my_calls())) or "(set My callsign first)"
        self._log(f"Digipeater {'ON: repeating frames sent via ' + names if on else 'off'}.")
        self._save_settings()
        try:
            self._update_status_gauges()
        except Exception:
            pass

    def _is_relayed_tx(self, f: bytes) -> bool:
        """A frame we sent as a digipeater (SARA FM's or BPQ32's)."""
        return f in getattr(self, "_relayed", {}) or link_adapt.is_relayed(f)

    # ── pings while bridged (v0.7.3) ─────────────────────────────────
    BRIDGE_PING_IDLE_S = 20.0

    def _session_busy(self) -> bool:
        """True while a Winlink/BPQ32 session is moving data: a ping or pong
        transmitted then would cut into it (and every lost frame costs a
        whole FRACK). Same courtesy VARA FM shows."""
        return (self.mode_var.get() == "bpq"
                and time.time() - self._last_session_activity < self.BRIDGE_PING_IDLE_S)

    def _handle_bridge_pingpong(self, frame):
        try:
            msg = proto.PingPongMessage.unpack(frame.payload)
        except Exception:
            return
        my_call = self.call_var.get().strip().upper()
        if (msg.msg_type == proto.MSG_PING and msg.to_call in (my_call, proto.BROADCAST)
                and self._session_busy()):
            self._log(f"RX PING  seq={msg.seq}  {msg.from_call} -> {msg.to_call}  "
                      f"{self._signal_str(frame)}  (Winlink session in progress -- not answering)")
            return
        self._handle_frame(frame)

    def _ping_allowed(self) -> bool:
        if self._session_busy():
            self._log("PING/Auto-Tune: a Winlink session is moving data -- try again when it's idle.")
            return False
        return True

    # ── polling loop (frame queue + level meter) ─────────────────────
    def _poll_engine(self):
        if self.engine:
            try:
                while True:
                    frame = self.engine.frame_queue.get_nowait()
                    self._frames_seen += 1
                    if self.mode_var.get() == "bpq":
                        self._handle_air_frame_for_bridge(frame)
                    else:
                        self._handle_frame(frame)
            except queue.Empty:
                pass
            level = None
            try:
                while True:
                    level = self.engine.level_queue.get_nowait()
            except queue.Empty:
                pass
            self._lite_n = getattr(self, "_lite_n", 0) + 1
            ui_now = not getattr(self, "_lite", False) or self._lite_n % self.LITE_POLL_EVERY == 0
            if level is not None and ui_now:
                self._update_meter(level)
            if ui_now:
                self._update_dcd()
            self._ptt_watchdog()
            self._audio_health()
            self._speed_tick += 1
            if self._speed_tick >= 10 and self.mode_var.get() == "bpq":
                self._speed_tick = 0
                self._update_speed_display()
            if self._speed_tick % 5 == 0:
                self._update_status_gauges()
        self._update_cpu()                                      # v0.9.44
        self._check_rx_recording()                              # v0.9.47
        if self.chat_server is not None:                        # v0.9.75
            self.chat_server.station = self.call_var.get().strip().upper()
            self.chat_server.set_can_send(self._radio_can_chat())
        try:
            self.chat.tick()                                    # v0.9.38
            self._chat_ticks = getattr(self, "_chat_ticks", 0) + 1
            if self._chat_ticks % 20 == 0 and self.chat_win is not None and self.chat_win.state() == "normal":
                self.chat_win.refresh_lists()
        except Exception as e:
            self._log(f"Chat error: {e}")
        self.after(100, self._poll_engine)

    # ── Save Logs (v0.8.8) ──────────────────────────────────────────
    def _station_info(self) -> str:
        def v(name):
            try:
                return getattr(self, name).get()
            except Exception:
                return "?"
        e = self.engine
        lines = [
            f"SARA FM station report -- {ofdm.VERSION}",
            f"Saved:        {time.strftime('%Y-%m-%d %H:%M:%S')}  (UTC{time.strftime('%z')})",
            f"Callsign:     {v('call_var')}",
            f"Target:       {v('target_var')}",
            f"Mode:         {v('mode_var')}",
            f"Modulation:   {v('modulation_var')}",
            f"DCD mode:     {v('dcd_mode_var')}",
            f"Audio buffer: {'large (Slow PC)' if getattr(self, 'slow_pc_var', None) is not None and self.slow_pc_var.get() else 'normal'}"
            + ("   No C modes: on" if getattr(self, 'no_coded_var', None) is not None and self.no_coded_var.get() else ""),
            f"CPU:          {App._cpu_summary(self) if hasattr(self, '_cpu_hist') else 'not measured yet'}",
            f"Receiver:     {getattr(self, '_frames_seen', 0)} frames decoded this run; candidates turned away: "
            + (", ".join(f"{k} {n}" for k, n in getattr(self, "_reject_counts", collections.Counter()).most_common())
               or "none")
            + (f"; strong but undecodable (overdriven sender?) {getattr(self, '_garbled_total', 0)}"
               if getattr(self, "_garbled_total", 0) else "")
            + (f"; sound-card clock correction {getattr(self, '_clock_ppm', 0.0):+.0f} ppm"
               if abs(getattr(self, "_clock_ppm", 0.0)) >= 300 else ""),
            f"Gateway:      {'on (one set of levels for callers)' if App._is_gateway(self) else 'off'}"
            + f"   saved levels for: {', '.join(t[0] for t in getattr(self, 'targets', []) if len(t) > 2 and t[2]) or 'no stations yet'}",
            f"Digipeater:   {'on' if v('digi_var') in (True, 'True', 1, '1') else 'off'}"
            + (f"   alias: {v('digi_alias_var')}" if v('digi_alias_var') not in ('', None, '?') else ""),
            f"Input device:  {v('in_dev_var')}",
            f"Output device: {v('out_dev_var')}",
            f"PTT:          {v('ptt_var')}   RA-Board: {v('ra_board_var')}   GPIO: {v('gpio_pin_var')}",
            f"TX delay ms:  {v('tx_delay_var')}",
            f"RX gain dB:   {v('rx_gain_db_var')}",
            f"TX level %:   {v('tx_level_var')}",
            f"KISS port:    {v('kiss_port_var')}   network access: {v('remote_kiss_var')}",
            f"Audio running: {e is not None}   Bridge running: {self.bridge is not None}",
        ]
        if e is not None:
            try:
                db = tuning.dbfs
                lines.append(f"Noise floor:  {db(e.noise_floor_rms):.0f} dBFS" if e.noise_floor_rms else "Noise floor:  (not yet measured)")
                lines.append(f"DCD now:      {e.dcd_state()}")
            except Exception:
                pass
        lines.append(f"Python:       {sys.version.split()[0]}   frozen build: {bool(getattr(sys, 'frozen', False))}")
        lines.append(f"Platform:     {sys.platform}")
        return "\n".join(lines) + "\n"

    def _save_logs(self):
        out_dir = log_bundle.desktop_dir() or app_dir()
        files = [LOG_CSV, self._settings_path(),
                 os.path.join(app_dir(), "sara_pp_crash.log"),
                 os.path.join(app_dir(), "selftest_result.txt")]
        try:
            path = log_bundle.make_bundle(self.call_var.get(), self._station_info(),
                                          self.log_box.get("1.0", "end"), files,
                                          self.session_log.recent_files(), out_dir)
        except Exception as ex:
            messagebox.showerror("Save Logs", f"Could not save the logs: {ex}")
            return
        self._log(f"Logs saved: {path}")
        try:
            if sys.platform.startswith("win"):
                import subprocess
                subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
        except Exception:
            pass
        messagebox.showinfo("Save Logs",
                            f"Saved:\n{path}\n\nAttach this one file to your email or message "
                            f"to the sysop. It contains this station's log and settings.")

    def _save_report(self):
        """v0.9.21: this run's log as one small text file on the Desktop."""
        out_dir = log_bundle.desktop_dir() or app_dir()
        logs = [p for p in getattr(self.session_log, "_opened", []) if os.path.isfile(p)]
        try:
            path = log_bundle.make_report(self.call_var.get(), self._station_info(), logs, out_dir)
        except Exception as ex:
            messagebox.showerror("Save Report", f"Could not save the report: {ex}")
            return
        kb = os.path.getsize(path) / 1024
        self._log(f"Report saved: {path} ({kb:.0f} kB)")
        try:
            if sys.platform.startswith("win"):
                import subprocess
                subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
        except Exception:
            pass
        messagebox.showinfo("Save Report",
                            f"Saved ({kb:.0f} kB):\n{path}\n\nThis run's log as one text file, "
                            f"without the quiet-channel lines. Send this file for test reports; "
                            f"use Save Logs for a full set when something goes wrong.")

    # ── v0.9.19: Auto's memory of each station, kept across restarts ──
    _SPEED_FILE = "sara_pp_speed.json"

    def _save_speed_memory(self):
        try:
            path = os.path.join(app_dir(), self._SPEED_FILE)
            tmp = path + ".tmp"
            with open(tmp, "w") as fh:
                json.dump(self.speed.export_state(), fh)
            os.replace(tmp, path)
        except Exception:
            pass

    def _load_speed_memory(self):
        try:
            with open(os.path.join(app_dir(), self._SPEED_FILE)) as fh:
                self.speed.import_state(json.load(fh))
        except Exception:
            pass

    def destroy(self):
        try:
            self.chat.save()
        except Exception:
            pass
        self._save_speed_memory()
        self._save_settings()
        try:
            self.session_log.close()
        except Exception:
            pass
        if self.bridge:
            self.bridge.stop()
        if self.engine:
            self.engine.stop()
        super().destroy()


def selftest() -> bool:
    """`SARA-FM.exe --selftest` (or `python sara_pingpong.py --selftest`):
    verify an install WITHOUT a radio -- every bundled library loads, sound
    devices are visible, and a full encode -> decode loopback works in both
    modulations, including an FEC repair of deliberately damaged audio.
    Prints a report, saves it to selftest_result.txt next to the program,
    and returns True on success."""
    import numpy as np
    lines, ok = [f"SARA FM self-test  ({ofdm.VERSION})"], True

    def step(name, fn):
        nonlocal ok
        try:
            detail = fn()
            lines.append(f"  [PASS] {name}" + (f" -- {detail}" if detail else ""))
        except Exception as e:
            ok = False
            lines.append(f"  [FAIL] {name} -- {type(e).__name__}: {e}")

    def libs():
        import scipy, pyaudio, serial  # noqa: F401
        return f"numpy {np.__version__}, scipy {scipy.__version__}"

    def cm108_lib():
        import hid
        return f"{len(hid.enumerate())} HID device(s) visible"

    def sound():
        import pyaudio
        pa = pyaudio.PyAudio()
        try:
            n = pa.get_device_count()
            ins = sum(1 for i in range(n) if pa.get_device_info_by_index(i)["maxInputChannels"] > 0)
            outs = sum(1 for i in range(n) if pa.get_device_info_by_index(i)["maxOutputChannels"] > 0)
        finally:
            pa.terminate()
        if ins == 0 or outs == 0:
            raise RuntimeError(f"{ins} input / {outs} output device(s) -- "
                               "is the USB audio interface plugged in?")
        return f"{ins} input / {outs} output device(s)"

    def loopback():
        payload = bytes(range(200))
        for mod in (ofdm.MOD_BPSK, ofdm.MOD_QPSK, ofdm.MOD_QPSK_WIDE, ofdm.MOD_QAM16_WIDE, ofdm.MOD_QAM64_WIDE,
                    ofdm.MOD_QAM16_WIDE_C, ofdm.MOD_QAM64_WIDE_C):
            rx = ofdm.OFDMReceiver()
            rx.push(ofdm.frame_encode(payload, modulation=mod))
            f = rx.poll()
            if f is None or f.payload != payload:
                raise RuntimeError(f"clean loopback failed ({App._mod_name(mod)})")
        burst = ofdm.frame_encode(payload, modulation=ofdm.MOD_QPSK).copy()
        k = (ofdm.PREAMBLE_REPS + 1 + 10) * ofdm.N_SYM
        burst[k:k + ofdm.N_SYM] = np.random.RandomState(1).normal(0, 1.0, ofdm.N_SYM)
        rx = ofdm.OFDMReceiver()
        rx.push(burst)
        f = rx.poll()
        if f is None or f.payload != payload or f.fec_corrected == 0:
            raise RuntimeError("FEC failed to repair a damaged symbol")
        return f"BPSK + QPSK + QPSK Wide + 16QAM Wide clean, FEC repaired {f.fec_corrected} bytes"

    step("core libraries load", libs)
    step("CM108 PTT library (hidapi)", cm108_lib)
    step("sound devices", sound)
    step("modem loopback + FEC", loopback)
    lines.append("RESULT: " + ("ALL PASSED" if ok else "FAILED -- see above"))
    report = "\n".join(lines)
    print(report)
    try:
        with open(os.path.join(app_dir(), "selftest_result.txt"), "w") as fh:
            fh.write(report + "\n")
    except OSError:
        pass
    return ok


def _arg_value(name: str, default=None):
    """'--web' -> default, '--web=8081' / '--web 8081' -> '8081', absent -> None."""
    for i, a in enumerate(sys.argv):
        if a == name:
            nxt = sys.argv[i + 1] if i + 1 < len(sys.argv) else ""
            return nxt if nxt and not nxt.startswith("--") else default
        if a.startswith(name + "="):
            return a.split("=", 1)[1]
    return None


def main():
    if "--selftest" in sys.argv:
        sys.exit(0 if selftest() else 1)
    # v0.9.81: Raspberry Pi / headless. --headless runs with no screen (on a
    # virtual display) and implies --web; --web[=PORT] serves the phone page
    # (port 8090 by default since 0.9.82); --autostart presses Start once the
    # settings are loaded; --mail-port[=3000] adds a Mail button that opens
    # PAREDN Webmail on the same computer.
    headless = "--headless" in sys.argv
    if headless and sys.platform.startswith("linux") and not os.environ.get("DISPLAY") \
            and not os.environ.get("SARA_FM_XVFB"):
        import shutil
        if shutil.which("xvfb-run"):
            os.environ["SARA_FM_XVFB"] = "1"
            os.execvp("xvfb-run", ["xvfb-run", "-a", sys.executable] + sys.argv)
        print("--headless needs a display or xvfb-run (sudo apt install xvfb)")
        sys.exit(1)
    web_port = _arg_value("--web", str(web_ui.DEFAULT_PORT))
    if headless and web_port is None:
        web_port = str(web_ui.DEFAULT_PORT)
    try:
        app = App()
        if web_port is not None:
            try:
                mail_port = _arg_value("--mail-port", "3000")      # v0.9.82: PAREDN Webmail
                app.web = web_ui.WebUI(app, port=int(web_port),
                                       allow=app.kiss_allow_var.get().strip() or "private",
                                       headless=headless,
                                       mail_port=int(mail_port) if mail_port and mail_port.isdigit() else None)
                app.web.start()
            except Exception as e:
                app._log(f"Web page could not start on port {web_port}: {e}")
        if headless:
            app.withdraw()
        if "--autostart" in sys.argv:
            app.after(2500, lambda: app.engine is None and app._toggle_connect())
        app.mainloop()
    except Exception:
        # If it fails to start on a remote gateway, leave the error where a
        # volunteer can find it and send it in.
        import traceback
        path = os.path.join(app_dir(), "sara_pp_crash.log")
        try:
            with open(path, "a") as fh:
                fh.write(f"--- {time.strftime('%Y-%m-%d %H:%M:%S')}  {ofdm.VERSION}\n")
                traceback.print_exc(file=fh)
        except OSError:
            pass
        traceback.print_exc()
        print(f"\nSARA-PP failed to start. Details saved to: {path}")
        raise


if __name__ == "__main__":
    main()
