# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 PAREDN (Philippine Amateur Radio Emergency Data Network) / DW1IRF
"""SARA Terminal (v0.9.90): a plain text terminal, like VARA Terminal, that
connects to a BPQ32 node over SARA FM -- e.g. DX1PRD-12, then CHAT.

The AX.25 connection itself is ax25_term.Session; this file is the window
and the small settings file (terminal callsign, stations connected to)."""
from __future__ import annotations

import json
import os
import tkinter as tk
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText

import ax25_term

SETTINGS_FILE = "sara_pp_terminal.json"
DEFAULT_SSID = 7
MAX_TARGETS = 15


def default_call(station_call: str) -> str:
    base = ax25_term.split_call(station_call or "")[0] if station_call else ""
    return f"{base}-{DEFAULT_SSID}" if base else ""


class TerminalWindow(tk.Toplevel):
    def __init__(self, app, folder: str):
        super().__init__(app)
        self.app = app
        self.path = os.path.join(folder, SETTINGS_FILE)
        self.title("SARA Terminal")
        self.geometry("820x540")
        self.minsize(600, 360)
        self.protocol("WM_DELETE_WINDOW", self.withdraw)
        st = self._load()
        self.targets = [t for t in st.get("targets", []) if ax25_term.valid_call(t)][:MAX_TARGETS]
        self.mycall_var = tk.StringVar(value=st.get("my_call") or default_call(app.call_var.get()))
        self.target_var = tk.StringVar(value=self.targets[0] if self.targets else "")
        self.session = None
        self._build()
        self._tick()

    # ── settings ──
    def _load(self) -> dict:
        try:
            with open(self.path) as fh:
                d = json.load(fh)
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self):
        try:
            with open(self.path, "w") as fh:
                json.dump({"my_call": ax25_term.norm(self.mycall_var.get()), "targets": self.targets}, fh)
        except OSError:
            pass

    # ── layout ──
    def _build(self):
        top = ttk.Frame(self)
        top.pack(fill="x", padx=8, pady=(8, 4))
        ttk.Label(top, text="My call:").pack(side="left")
        self.mycall_entry = ttk.Entry(top, textvariable=self.mycall_var, width=11)
        self.mycall_entry.pack(side="left", padx=(4, 12))
        ttk.Label(top, text="Connect to:").pack(side="left")
        self.target_combo = ttk.Combobox(top, textvariable=self.target_var, values=self.targets, width=20)
        self.target_combo.pack(side="left", padx=4)
        self.target_combo.bind("<Return>", lambda e: self.connect())
        self.connect_btn = ttk.Button(top, text="Connect", command=self.connect)
        self.connect_btn.pack(side="left", padx=4)
        self.disc_btn = ttk.Button(top, text="Disconnect", command=self.disconnect, state="disabled")
        self.disc_btn.pack(side="left")
        row2 = ttk.Frame(self)
        row2.pack(fill="x", padx=8)
        self.state_var = tk.StringVar(value="Not connected")
        ttk.Label(row2, textvariable=self.state_var, font=("TkDefaultFont", 10, "bold")).pack(side="right")
        ttk.Label(row2, text="e.g. DX1PRD-12, then type CHAT   (via a digipeater: DX1PRD-12 via DW1IRF).\n"
                             "Sent in SARA FM modes -- the node needs a SARA FM port.",
                  foreground="#666", justify="left").pack(side="left")

        bottom = ttk.Frame(self)
        bottom.pack(side="bottom", fill="x", padx=8, pady=(4, 8))
        self.line_var = tk.StringVar()
        self.line_entry = ttk.Entry(bottom, textvariable=self.line_var, font=("Consolas", 11))
        self.line_entry.pack(side="left", fill="x", expand=True)
        self.line_entry.bind("<Return>", lambda e: self.send_line())
        ttk.Button(bottom, text="Send", command=self.send_line).pack(side="left", padx=(6, 0))

        self.text = ScrolledText(self, wrap="word", font=("Consolas", 11), state="disabled",
                                 background="#101418", foreground="#d8e0e8", insertbackground="#d8e0e8")
        self.text.pack(fill="both", expand=True, padx=8, pady=4)
        self.text.tag_configure("me", foreground="#7fd4ff")
        self.text.tag_configure("sys", foreground="#f0c060")

    def _write(self, s: str, tag: str | None = None):
        self.text.configure(state="normal")
        self.text.insert("end", s, tag)
        if int(self.text.index("end-1c").split(".")[0]) > 3000:
            self.text.delete("1.0", "500.0")
        self.text.see("end")
        self.text.configure(state="disabled")

    def _sys(self, msg: str):
        tail = self.text.get("end-2c", "end-1c")
        self._write(("" if tail in ("", "\n") else "\n") + msg + "\n", "sys")
        try:
            self.app._log(f"Terminal: {msg.strip('* ').strip()}")
        except Exception:
            pass
        self._update_state()

    def _update_state(self):
        s = self.session.state if self.session else ax25_term.DISCONNECTED
        busy = s != ax25_term.DISCONNECTED
        self.connect_btn.configure(state="disabled" if busy else "normal")
        self.disc_btn.configure(state="normal" if busy else "disabled")
        self.mycall_entry.configure(state="disabled" if busy else "normal")
        peer = self.session.peer if self.session else ""
        self.state_var.set({ax25_term.CONNECTED: f"Connected to {peer}",
                            ax25_term.CONNECTING: f"Connecting to {peer} ...",
                            ax25_term.DISCONNECTING: "Disconnecting ..."}.get(s, "Not connected"))

    # ── actions ──
    @staticmethod
    def parse_target(text: str):
        """'DX1PRD-12' or 'DX1PRD-12 via DW1IRF[,DX1FLR]' -> (call, [digis]) or None."""
        parts = text.replace(",", " ").upper().split()
        if not parts:
            return None
        call, digis = parts[0], []
        if len(parts) > 1:
            if parts[1] not in ("VIA", "V"):
                return None
            digis = parts[2:]
            if not digis or len(digis) > 2:
                return None
        if not all(ax25_term.valid_call(c) for c in [call] + digis):
            return None
        return ax25_term.norm(call), [ax25_term.norm(d) for d in digis]

    def connect(self):
        if self.session and self.session.state != ax25_term.DISCONNECTED:
            return
        my = ax25_term.norm(self.mycall_var.get())
        if not ax25_term.valid_call(my):
            self._sys("*** Enter a valid callsign for the terminal, e.g. "
                      f"{default_call(self.app.call_var.get()) or 'DW1IRF-7'}")
            return
        station = ax25_term.norm(self.app.call_var.get())
        if my == station:
            self._sys(f"*** Use an SSID for the terminal (e.g. {default_call(station)}): {station} is "
                      f"this station's own callsign, which BPQ32 / Winlink Express may be using")
            return
        tgt = self.parse_target(self.target_var.get())
        if tgt is None:
            self._sys("*** Enter the station to connect to, e.g. DX1PRD-12  (or DX1PRD-12 via DW1IRF)")
            return
        if getattr(self.app, "engine", None) is None:
            self._sys("*** SARA FM isn't running yet -- check Settings (sound card, PTT)")
            return
        label = self.target_var.get().strip().upper()
        self.targets = [label] + [t for t in self.targets if t != label]
        self.targets = self.targets[:MAX_TARGETS]
        self.target_combo.configure(values=self.targets)
        self._save()
        self.session = ax25_term.Session(my, self.app._term_send, on_text=lambda s: self._write(s),
                                         on_status=self._sys)
        self.app.term = self.session
        self.session.connect(tgt[0], tgt[1])
        self.line_entry.focus_set()

    def disconnect(self):
        if self.session:
            self.session.disconnect()
        self._update_state()

    def send_line(self):
        line = self.line_var.get()
        if not self.session or self.session.state != ax25_term.CONNECTED:
            self._sys("*** Not connected -- press Connect first")
            return
        self.line_var.set("")
        if self.session.send_text(line):
            self._write(line + "\n", "me")

    def _tick(self):
        try:
            if self.session:
                self.session.tick()
        except Exception as e:
            try:
                self.app._log(f"Terminal error: {e}")
            except Exception:
                pass
        self.after(250, self._tick)
