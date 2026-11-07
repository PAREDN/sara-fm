# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 PAREDN (Philippine Amateur Radio Emergency Data Network) / DW1IRF
"""
gauges.py -- radio-style analog meters for the SARA FM window (v0.9.33/34).

AnalogMeter draws a moving-coil meter the way it looks on a transceiver's
front panel: a dark bezel, an amber back-lit face, a black printed scale
whose top end turns red, a thin black needle with a soft shadow, the dark
needle housing at the bottom, and a caption strip under the face (like the
small readouts under VARA FM's gauges).

  meter = AnalogMeter(parent, lo=-60, hi=0, majors=(-60, -48, ...),
                      zones=[(-16, -8, GREEN)], red_from=-3, title="RX LEVEL")
  meter.set_level(db)          # live needle (meter ballistics: fast up, slow down)
  meter.set_frame_level(db)    # RX level only: marker + caption for the last frame
  meter.set_value(v, caption, color)   # S/N, speed, ...: needle + caption

Lamp: a round indicator (TX / RX / DCD / TCP) with a label.
"""

from __future__ import annotations

import math
import tkinter as tk


def _mix(c1: str, c2: str, f: float) -> str:
    a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{int(round(x + (y - x) * f)):02x}" for x, y in zip(a, b))


class AnalogMeter(tk.Canvas):
    BEZEL = "#1c1c1c"
    RIM = "#4a4a4a"
    FACE = "#f2c96b"          # amber back-light, edges
    FACE_HOT = "#fff1c9"      # centre of the glow
    INK = "#1a1a1a"
    PRINT = "#3a2e17"         # brownish print, like a real meter face
    GREEN = "#1f7a3a"
    RED = "#c0261d"
    AMBER = "#c77800"

    def __init__(self, master, width=210, height=140, lo_db=-60.0, hi_db=0.0,
                 target_db=-12.0, target_window_db=4.0, clip_db=-3.0, title="RX LEVEL",
                 unit="dBFS", majors=None, zones=None, red_from=None, label_fmt=None,
                 caption="", **kw):
        super().__init__(master, width=width, height=height, bg=self.BEZEL,
                         highlightthickness=0, **kw)
        self.w, self.h = width, height
        self.lo, self.hi = float(lo_db), float(hi_db)
        self.target, self.window, self.clip = target_db, target_window_db, clip_db
        self.title, self.unit = title, unit
        self.majors = majors if majors is not None else (-60, -48, -36, -24, -12, -6, 0)
        # default zones = the RX level meter: green target band, red near clipping
        self.zones = zones if zones is not None else [(target_db - target_window_db,
                                                       target_db + target_window_db, self.GREEN)]
        self.red_from = clip_db if red_from is None and zones is None else red_from
        self.label_fmt = label_fmt or (lambda v: f"{v:g}")
        self.cap_h = max(16, int(height * 0.15))
        face_bottom = height - self.cap_h - 6
        self.cx = width / 2
        self.cy = face_bottom + height * 0.10          # pivot just below the visible face
        self.r = min(width * 0.43, (self.cy - 14) * 0.86)
        self.SPAN = 96.0
        self._db = self.lo
        self._target_db = self.lo
        self._peak = None
        self._face_bottom = face_bottom
        self._draw_face()
        sh = _mix(self.FACE, "#000000", 0.35)
        self._shadow = self.create_line(0, 0, 0, 0, fill=sh, width=2)
        self._needle = self.create_line(0, 0, 0, 0, fill=self.INK, width=2, capstyle="round")
        self._draw_housing()
        self._peak_mark = self.create_polygon(0, 0, 0, 0, 0, 0, fill=self.GREEN, outline="")
        # caption strip under the face
        cy0 = height - self.cap_h - 3
        self._cap_bg = self.create_rectangle(6, cy0, width - 6, height - 3, fill="#2a2a2a", outline="#555")
        self._readout = self.create_text(width / 2, cy0 + self.cap_h / 2, text=caption or "---",
                                         font=("TkDefaultFont", max(8, int(self.cap_h * 0.55)), "bold"),
                                         fill="#e8e8e8")
        self._place_needle(self._db)
        self._job = None
        self._tick()

    # ── geometry ─────────────────────────────────────────────────────
    def _angle(self, db: float) -> float:
        """Screen angle in degrees (0 = right, 90 = up) for a scale value."""
        f = (min(max(db, self.lo), self.hi) - self.lo) / (self.hi - self.lo)
        return 90.0 + self.SPAN / 2 - f * self.SPAN

    def _pt(self, ang: float, rad: float):
        a = math.radians(ang)
        return self.cx + rad * math.cos(a), self.cy - rad * math.sin(a)

    def _arc(self, v1, v2, rad, color, width):
        a1, a2 = self._angle(v2), self._angle(v1)
        self.create_arc(self.cx - rad, self.cy - rad, self.cx + rad, self.cy + rad,
                        start=a1, extent=a2 - a1, style="arc", outline=color, width=width)

    def _draw_face(self):
        w, fb = self.w, self._face_bottom
        # bezel rim and face
        self.create_rectangle(3, 3, w - 3, fb + 2, fill=self.RIM, outline="")
        self.create_rectangle(6, 6, w - 6, fb, fill=self.FACE, outline="")
        # back-light glow: concentric ellipses, brightest above the pivot
        gx, gy = self.cx, fb - (fb - 6) * 0.30
        for i in range(10, 0, -1):
            f = i / 10.0
            rx, ry = (w / 2 - 8) * f, (fb - 8) * 0.62 * f
            self.create_oval(gx - rx, gy - ry, gx + rx, gy + ry, outline="",
                             fill=_mix(self.FACE, self.FACE_HOT, 1 - f))
        # repaint the face edges the glow may have spilled over
        self.create_rectangle(3, 3, w - 3, 6, fill=self.RIM, outline="")
        self.create_rectangle(3, 3, 6, fb + 2, fill=self.RIM, outline="")
        self.create_rectangle(w - 6, 3, w - 3, fb + 2, fill=self.RIM, outline="")
        r = self.r
        fs = max(7, int(self.h * 0.06))
        # coloured bands just inside the scale line
        for a, b, col in self.zones:
            self._arc(a, b, r - 4, col, max(4, int(self.h * 0.035)))
        self._arc(self.lo, self.hi, r, self.PRINT, 2)
        if self.red_from is not None:
            self._arc(self.red_from, self.hi, r, self.RED, 3)
            self._arc(self.red_from, self.hi, r + 3, self.RED, 3)
        # ticks: majors labelled, minors between
        majors = list(self.majors)
        minors = []
        for a, b in zip(majors, majors[1:]):
            minors += [a + (b - a) * k / 4 for k in (1, 2, 3)]
        for v in minors:
            an = self._angle(v)
            col = self.RED if self.red_from is not None and v >= self.red_from else self.PRINT
            self.create_line(*self._pt(an, r), *self._pt(an, r + 4), fill=col, width=1)
        for v in majors:
            an = self._angle(v)
            col = self.RED if self.red_from is not None and v >= self.red_from else self.PRINT
            self.create_line(*self._pt(an, r), *self._pt(an, r + 8), fill=col, width=2)
            self.create_text(*self._pt(an, r + 8 + fs), text=self.label_fmt(v),
                             font=("TkDefaultFont", fs), fill=col)
        self.create_text(self.cx, self.cy - r * 0.60, text=self.title,
                         font=("TkDefaultFont", max(8, int(self.h * 0.075)), "bold"), fill=self.PRINT)
        self.create_text(w - 12, fb - 6, text=self.unit, anchor="se",
                         font=("TkDefaultFont", max(6, fs - 1)), fill=self.PRINT)

    def _draw_housing(self):
        """The dark cover over the needle's pivot along the bottom of the face."""
        w, fb = self.w, self._face_bottom
        hw = w * 0.17
        self.create_arc(self.cx - hw, fb - hw * 0.50, self.cx + hw, fb + hw * 0.50,
                        start=0, extent=180, style="chord", fill="#2b2b2b", outline="#111")
        self.create_rectangle(6, fb - 1, w - 6, fb + 2, fill="#2b2b2b", outline="")
        self.create_oval(self.cx - 4, fb - hw * 0.25 - 4, self.cx + 4, fb - hw * 0.25 + 4,
                         fill="#555", outline="")

    def _place_needle(self, v: float):
        a = self._angle(v)
        x1, y1 = self._pt(a, self.r * 0.30)
        x2, y2 = self._pt(a, self.r + 6)
        self.coords(self._needle, x1, y1, x2, y2)
        self.coords(self._shadow, x1 + 2, y1 + 3, x2 + 2, y2 + 3)

    # ── public ───────────────────────────────────────────────────────
    def set_level(self, v: float | None):
        """Live value for the needle (None = rest at the left stop)."""
        self._target_db = self.lo if v is None else max(self.lo, min(self.hi, v))

    def set_caption(self, text: str, color: str = "#e8e8e8"):
        self.itemconfigure(self._readout, text=text, fill=color)

    def set_value(self, v: float | None, caption: str = "", color: str = "#e8e8e8"):
        """S/N, speed and other held values: needle to v, caption below."""
        self.set_level(v)
        if caption:
            self.set_caption(caption, color)

    def set_frame_level(self, db: float | None):
        """RX level meter: marker and caption for the last decoded frame."""
        self._peak = db
        if db is None:
            self.coords(self._peak_mark, 0, 0, 0, 0, 0, 0)
            self.set_caption("Last frame: ---")
            return
        a = self._angle(db)
        tip = self._pt(a, self.r - 7)
        l = self._pt(a + 3, self.r - 15)
        rr = self._pt(a - 3, self.r - 15)
        self.coords(self._peak_mark, *tip, *l, *rr)
        ok = abs(db - self.target) <= self.window
        self.set_caption(f"Last frame {db:.0f} dBFS",
                         self.GREEN_LIT if ok else (self.RED_LIT if db > self.clip else self.AMBER_LIT))

    GREEN_LIT, RED_LIT, AMBER_LIT = "#5fd38d", "#ff6b5e", "#ffc24d"

    # v0.9.49: redraw only when the needle actually moves (a resting needle
    # used to repaint all three meters 25 times a second -- noticeable on a
    # slow gateway PC), and at 15 frames a second
    TICK_MS = 66

    def _tick(self):
        d = self._target_db - self._db
        if abs(d) > (self.hi - self.lo) * 0.002:
            self._db += d * (0.65 if d > 0 else 0.22)  # meter ballistics: up fast, down slow
            self._place_needle(self._db)
        try:
            self._job = self.after(self.TICK_MS, self._tick)
        except tk.TclError:
            pass

    def destroy(self):
        job = getattr(self, "_job", None)
        if job is not None:
            try:
                self.after_cancel(job)
            except Exception:
                pass
        super().destroy()


class Lamp(tk.Canvas):
    """Round status lamp with a label (beside it, or underneath)."""
    OFF = "#4a4a4a"

    def __init__(self, master, text: str, on_color: str, size=16, side=True, **kw):
        bg = kw.pop("bg", None)
        if bg is None:
            try:
                bg = master.winfo_toplevel().cget("bg")
            except Exception:
                bg = None
        tw = 8 * max(2, len(text)) + 6
        W, H = (size + 6 + tw, size + 6) if side else (max(size, tw) + 8, size + 18)
        super().__init__(master, width=W, height=H, highlightthickness=0, bg=bg, **kw)
        self.on_color = on_color
        if side:
            x0, y0 = 3, 3
            self.create_text(size + 8, H / 2, text=text, anchor="w", font=("TkDefaultFont", 8, "bold"))
        else:
            x0, y0 = (W - size) / 2, 2
            self.create_text(W / 2, size + 11, text=text, font=("TkDefaultFont", 8, "bold"))
        self._dot = self.create_oval(x0, y0, x0 + size, y0 + size, fill=self.OFF, outline="#222", width=1)
        self._on = None

    def set(self, on: bool, color: str | None = None):
        c = (color or self.on_color) if on else self.OFF
        if c != self._on:
            self.itemconfigure(self._dot, fill=c)
            self._on = c


class CpuBar(tk.Canvas):
    """v0.9.44: a small CPU meter for the status bar, like VARA FM's CPU
    gauge: SARA FM's share of this PC as a bar, plus the decoder's load."""
    GREEN, AMBER, RED = "#2e9e4f", "#d99a00", "#c0261d"

    def __init__(self, master, **kw):
        bg = kw.pop("bg", None)
        if bg is None:
            try:
                bg = master.cget("bg")
            except Exception:
                bg = None
        super().__init__(master, width=210, height=18, highlightthickness=0, bg=bg, **kw)
        self.create_text(2, 9, text="CPU", anchor="w", font=("TkDefaultFont", 8, "bold"))
        self.create_rectangle(32, 4, 92, 14, outline="#666", fill="#e6e6e6")
        self._bar = self.create_rectangle(33, 5, 33, 14, outline="", fill=self.GREEN)
        self._txt = self.create_text(98, 9, text="--", anchor="w", font=("TkDefaultFont", 8))
        self.pct = None
        self.decoder_pct = None

    @classmethod
    def color_for(cls, pct: float, decoder_pct: float | None) -> str:
        """Amber/red when either this PC is busy, or the decoder (which runs
        on one core) is close to falling behind."""
        load = max(pct, decoder_pct or 0.0)
        return cls.GREEN if load < 50 else (cls.AMBER if load < 80 else cls.RED)

    def set(self, pct: float | None, decoder_pct: float | None = None):
        self.pct, self.decoder_pct = pct, decoder_pct
        if pct is None:
            self.coords(self._bar, 33, 5, 33, 14)
            self.itemconfigure(self._txt, text="--")
            return
        w = max(0.0, min(100.0, pct)) / 100.0 * 58
        self.coords(self._bar, 33, 5, 33 + w, 14)
        self.itemconfigure(self._bar, fill=self.color_for(pct, decoder_pct))
        txt = f"{pct:.0f}%"
        if decoder_pct is not None:
            txt += f"  decoder {decoder_pct:.0f}%"
        self.itemconfigure(self._txt, text=txt)
