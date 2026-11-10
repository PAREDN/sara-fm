#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 PAREDN (Philippine Amateur Radio Emergency Data Network) / DW1IRF
"""
ping_window.py -- graphical ping / Auto-Tune result window (v0.7.3).

Modelled on VARA FM's Ping window, which PAREDN's Winlink volunteers
already know: two towers, the local station on the left and the far
station on the right, with colour-coded S/N and level boxes beside each.

  Left boxes  = how YOU hear THEM   (measured here: S/N, level after RX gain)
  Right boxes = how THEY hear YOU   (from the PONG's two-way report: S/N,
                                     their RAW input level before their gain)

Colours come from tuning.py (same thresholds as the text log, so the two
always agree). The window only displays; Ping / Auto-Tune buttons call
back into the main app.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

import tuning


class PingWindow(tk.Toplevel):
    W, H = 760, 300                   # v0.9.52: third row = data carriers
    BOX_W, BOX_H = 196, 36

    def __init__(self, master, on_ping=None, on_autotune=None, target_var=None, on_target=None,
                 rx_var=None, tx_var=None, loc_var=None, prof_var=None, on_save=None, on_remove=None):
        super().__init__(master)
        self.title("SARA FM Ping")
        self.resizable(False, False)
        self._on_ping = on_ping
        self._on_autotune = on_autotune
        self._on_target = on_target

        top = ttk.Frame(self, padding=(10, 8, 10, 0))
        top.pack(fill="x")
        self.peer_var = tk.StringVar(value="Ping to: -")
        self.target_combo = None
        if target_var is not None:
            # v0.9.93: pick the station right here, like VARA FM -- no need to
            # close the window to ping the next one
            ttk.Label(top, text="Ping to:", font=("TkDefaultFont", 11, "bold")).pack(side="left")
            self.target_combo = ttk.Combobox(top, textvariable=target_var, width=24)
            self.target_combo.pack(side="left", padx=6)
            self.target_combo.bind("<<ComboboxSelected>>", lambda e: self._target())
            self.target_combo.bind("<Return>", lambda e: self._target())
        else:
            ttk.Label(top, textvariable=self.peer_var, font=("TkDefaultFont", 11, "bold")).pack(side="left")
        ttk.Button(top, text="Close", command=self.destroy).pack(side="right")
        self.autotune_btn = ttk.Button(top, text="Auto-Tune", command=self._autotune)
        self.autotune_btn.pack(side="right", padx=6)
        self.ping_btn = ttk.Button(top, text="Ping", command=self._ping)
        self.ping_btn.pack(side="right")

        # v0.9.95: the station list is kept here -- Location, Save (callsign,
        # location and the levels now on the sliders), Remove
        self.save_btn = self.remove_btn = None
        if loc_var is not None and on_save is not None:
            row = ttk.Frame(self, padding=(10, 6, 10, 0))
            row.pack(fill="x")
            ttk.Label(row, text="Location:").pack(side="left")
            ttk.Entry(row, textvariable=loc_var, width=28).pack(side="left", padx=6)
            self.save_btn = ttk.Button(row, text="Save", width=7, command=on_save)
            self.save_btn.pack(side="left")
            if on_remove is not None:
                self.remove_btn = ttk.Button(row, text="Remove", width=8, command=on_remove)
                self.remove_btn.pack(side="left", padx=(4, 0))
            if prof_var is not None:
                ttk.Label(row, textvariable=prof_var, foreground="#555").pack(side="left", padx=(12, 0))

        self.canvas = tk.Canvas(self, width=self.W, height=self.H, bg="white", highlightthickness=0)
        self.canvas.pack(padx=10, pady=8)

        # v0.9.93: set the levels by hand, while watching the boxes above;
        # Auto-Tune moves these sliders too. Save keeps them for the station.
        self.tx_scale = self.rx_scale = None
        if rx_var is not None and tx_var is not None:
            lv = ttk.Frame(self, padding=(10, 0, 10, 0))
            lv.pack(fill="x")
            ttk.Label(lv, text="Transmit audio (TX level %)").grid(row=0, column=0, sticky="w")
            self.tx_scale = tk.Scale(lv, from_=5, to=100, resolution=1, orient="horizontal",
                                     length=300, variable=tx_var, showvalue=True)
            self.tx_scale.grid(row=1, column=0, sticky="w", padx=(0, 30))
            ttk.Label(lv, text="Receive audio (RX gain dB)").grid(row=0, column=1, sticky="w")
            self.rx_scale = tk.Scale(lv, from_=-20, to=40, resolution=1, orient="horizontal",
                                     length=300, variable=rx_var, showvalue=True)
            self.rx_scale.grid(row=1, column=1, sticky="w")
            ttk.Label(lv, text="Ping again after a change to see the effect. Press Save (above) to keep "
                               "these levels for this station.",
                      foreground="#666", wraplength=740, justify="left").grid(row=2, column=0, columnspan=2,
                                                                                sticky="w", pady=(2, 0))

        self.status_var = tk.StringVar(value="")
        ttk.Label(self, textvariable=self.status_var, font=("TkDefaultFont", 10, "bold")).pack(anchor="w", padx=12)
        self.advice = tk.Text(self, height=4, width=100, wrap="word", relief="flat",
                              bg=self.cget("bg"), font=("TkDefaultFont", 9))
        self.advice.pack(padx=10, pady=(2, 10), fill="x")
        self.advice.configure(state="disabled")

        self._local = "-"
        self._remote = "-"
        self._draw()

    # ── buttons ──────────────────────────────────────────────────────
    def _ping(self):
        if self._on_ping:
            self._on_ping()

    def _autotune(self):
        if self._on_autotune:
            self._on_autotune()

    def _target(self):
        if self._on_target:
            self._on_target()

    # ── public API ───────────────────────────────────────────────────
    def set_targets(self, values):
        if self.target_combo is not None:
            self.target_combo.configure(values=list(values))

    def reset_result(self):
        """A different station picked: clear the last one's numbers."""
        for a in ("_you", "_they"):
            if hasattr(self, a):
                setattr(self, a, None)
        self._car = (None, None)
        self.status_var.set("")
        self.set_advice([])
        self._draw()

    def set_peers(self, local: str, remote: str):
        self._local, self._remote = local or "-", remote or "-"
        self.peer_var.set(f"Ping to: {self._remote}")
        self._draw()

    def set_pinging(self, seq: int | None = None):
        self.status_var.set(f"Pinging {self._remote}..." + (f"  (seq {seq})" if seq is not None else ""))
        self._draw(link="active")

    def set_timeout(self):
        self.status_var.set(f"No reply from {self._remote}.")
        self._draw(link="failed")
        self.set_advice([f"Check: same frequency, {self._remote} running SARA-PP (Ping/Pong mode, "
                         f"or bridged and idle), and levels -- ask them to watch their RX meter "
                         f"while you send a Test tone (Settings > Devices)."])

    def set_result(self, you_hear: tuple, they_hear: tuple | None, rtt_s: float | None = None,
                   carriers: tuple | None = None):
        """you_hear = (snr_db, level_dbfs after RX gain);
        they_hear = (snr_db, raw_input_dbfs) or None if the far end runs a
        version without the two-way report.
        carriers = (yours, theirs): data-carrier S/N in dB, either may be None
        (v0.9.52)."""
        self._you = you_hear
        self._they = they_hear
        self._car = carriers or (None, None)
        self.status_var.set(f"Reply from {self._remote}" +
                            (f"  --  round trip {rtt_s:.1f} s" if rtt_s is not None else ""))
        self._draw(link="ok")

    def set_advice(self, lines: list[str]):
        self.advice.configure(state="normal")
        self.advice.delete("1.0", "end")
        self.advice.insert("end", "\n".join(lines))
        self.advice.configure(state="disabled")

    # ── drawing ──────────────────────────────────────────────────────
    def _draw(self, link: str = "idle"):
        c = self.canvas
        c.delete("all")
        lx, rx_, base, top = 300, 460, self.H - 50, 70
        self._tower(lx, base, top, facing=+1)
        self._tower(rx_, base, top, facing=-1)
        c.create_text(lx, base + 22, text=self._local, font=("TkDefaultFont", 13, "bold"))
        c.create_text(rx_, base + 22, text=self._remote, font=("TkDefaultFont", 13, "bold"))
        self._link(lx + 30, rx_ - 30, top + 20, link)

        you = getattr(self, "_you", None)
        they = getattr(self, "_they", None)
        # left boxes: how you hear them
        c.create_text(15 + self.BOX_W / 2, 92, text=f"You hear {self._remote}", font=("TkDefaultFont", 10))
        self._box(15, 105, *self._fmt_snr(you[0] if you else None), tuning.snr_colors(you[0] if you else None))
        self._box(15, 150, *self._fmt_lvl("Level", you[1] if you else None, "dBFS"),
                  tuning.level_colors(you[1] if you else None))
        car = getattr(self, "_car", (None, None))
        self._box(15, 195, *self._fmt_car(car[0] if you else None), tuning.carrier_colors(car[0] if you else None))
        # right boxes: how they hear you
        rx0 = self.W - 15 - self.BOX_W
        c.create_text(rx0 + self.BOX_W / 2, 92, text=f"{self._remote} hears you", font=("TkDefaultFont", 10))
        self._box(rx0, 105, *self._fmt_snr(they[0] if they else None), tuning.snr_colors(they[0] if they else None))
        self._box(rx0, 150, *self._fmt_lvl("Input", they[1] if they else None, "dBFS"),
                  tuning.raw_input_colors(they[1] if they else None))
        self._box(rx0, 195, *self._fmt_car(car[1] if they else None), tuning.carrier_colors(car[1] if they else None))
        if you is not None and they is None:
            c.create_text(rx0 + self.BOX_W / 2, 245, text="(no report: older SARA-PP)",
                          font=("TkDefaultFont", 8), fill="#666")

    @staticmethod
    def _fmt_snr(snr):
        return ("S/N", "--" if snr is None else f"{snr:+.0f} dB")

    @staticmethod
    def _fmt_car(car):
        return ("Data", "--" if car is None else f"{car:+.0f} dB")

    @staticmethod
    def _fmt_lvl(label, lvl, unit):
        return (label, "--" if lvl is None else f"{lvl:.0f} {unit}")

    def _box(self, x, y, label, value, colors):
        fill, fg = colors
        self.canvas.create_rectangle(x, y, x + self.BOX_W, y + self.BOX_H, fill=fill, outline="")
        self.canvas.create_text(x + self.BOX_W / 2, y + self.BOX_H / 2, text=f"{label}: {value}",
                                fill=fg, font=("TkDefaultFont", 13, "bold"))

    def _tower(self, x, base, top, facing):
        c, col = self.canvas, "#2b5d8a"
        hw_base, hw_top = 26, 5
        # legs
        c.create_line(x - hw_base, base, x - hw_top, top, fill=col, width=2)
        c.create_line(x + hw_base, base, x + hw_top, top, fill=col, width=2)
        # lattice bracing
        n = 9
        for i in range(n + 1):
            f = i / n
            y = base - (base - top) * f
            hw = hw_base - (hw_base - hw_top) * f
            c.create_line(x - hw, y, x + hw, y, fill=col)
            if i < n:
                f2 = (i + 1) / n
                y2 = base - (base - top) * f2
                hw2 = hw_base - (hw_base - hw_top) * f2
                c.create_line(x - hw, y, x + hw2, y2, fill=col)
                c.create_line(x + hw, y, x - hw2, y2, fill=col)
        # mast and dish facing the other tower
        c.create_line(x, top, x, top - 18, fill=col, width=2)
        # Tk arc angles: 90..270 is the LEFT half (a dish opening right).
        start = 90 if facing > 0 else 270
        cx = x + 6 * facing
        c.create_arc(cx - 9, top - 2, cx + 9, top + 34,
                     start=start, extent=180, style="arc", outline=col, width=3)

    def _link(self, x0, x1, y, state):
        c = self.canvas
        if state == "idle":
            c.create_line(x0, y, x1, y, fill="#cccccc", dash=(4, 4))
            return
        if state == "failed":
            c.create_line(x0, y, x1, y, fill="#d64545", dash=(6, 4), width=2)
            mx = (x0 + x1) / 2
            c.create_line(mx - 10, y - 10, mx + 10, y + 10, fill="#d64545", width=3)
            c.create_line(mx - 10, y + 10, mx + 10, y - 10, fill="#d64545", width=3)
            return
        color = "#f0a020" if state == "active" else "#e8b400"
        # zig-zag "signal" between the dishes, arrowheads both ways
        pts = []
        steps = 10
        for i in range(steps + 1):
            px = x0 + (x1 - x0) * i / steps
            py = y + (8 if i % 2 else -8) if 0 < i < steps else y
            pts += [px, py]
        c.create_line(*pts, fill=color, width=2)
        c.create_line(x0 + 6, y, x0, y, arrow="last", fill=color, width=2)
        c.create_line(x1 - 6, y, x1, y, arrow="last", fill=color, width=2)
