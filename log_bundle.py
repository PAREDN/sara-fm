# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 PAREDN (Philippine Amateur Radio Emergency Data Network) / DW1IRF
"""
log_bundle.py -- one-click log collection for field testing (v0.8.8).

Volunteers used to copy log text out of the window by hand and hunt for files
in folders, and files sent back were sometimes named for the wrong station.
Now:

  * SessionLog keeps a running copy of every log line in a file next to
    the program, so nothing is lost when the window is closed or the
    program crashes. v0.9.2: named with the callsign, date and start time,
    logs/sara_pp_<CALL>_YYYY-MM-DD_HHMMSS.log -- one per program run (and a
    new one at midnight), so two stations' logs never share a name.
  * make_bundle() puts everything a diagnosis needs into ONE zip, named with
    the station's callsign and the time, e.g.
        SARA-FM_logs_DU3FS_2026-10-07_1245.zip
    on the Desktop (or next to the program if there's no Desktop).

What goes in: station_info.txt (version, settings, devices, levels, time zone),
the daily session logs for the last few days, the text currently in the log
window, the ping/pong CSV, the settings file, and the crash log / self-test
result if they exist. Nothing is sent anywhere -- the volunteer attaches the
zip to an email or message themselves.
"""

from __future__ import annotations

import datetime as _dt
import os
import re
import time
import zipfile

LOG_DIR_NAME = "logs"
KEEP_DAYS_IN_BUNDLE = 3      # daily session logs included (today and the 2 before)


def safe_call(call: str) -> str:
    """Callsign made safe for a filename ('' -> 'NOCALL')."""
    c = re.sub(r"[^A-Za-z0-9-]", "", (call or "").strip().upper())
    return c or "NOCALL"


def desktop_dir() -> str | None:
    """The user's Desktop folder, if one exists (Windows OneDrive-redirected
    Desktops included)."""
    home = os.path.expanduser("~")
    cands = []
    up = os.environ.get("USERPROFILE")
    for base in filter(None, (up, home)):
        cands += [os.path.join(base, "Desktop"), os.path.join(base, "OneDrive", "Desktop")]
    od = os.environ.get("OneDrive")
    if od:
        cands.append(os.path.join(od, "Desktop"))
    for c in cands:
        if os.path.isdir(c):
            return c
    return None


class SessionLog:
    """Appends log lines to logs/sara_pp_<CALL>_YYYY-MM-DD_HHMMSS.log: one
    file per program run, a new one at midnight or when the callsign is
    changed. call_fn returns the station's callsign (may be '' at first).
    Never raises: logging must not break the program."""

    def __init__(self, app_dir: str, version: str = "", call_fn=None):
        self.dir = os.path.join(app_dir, LOG_DIR_NAME)
        self.version = version
        self.call_fn = call_fn
        self._fh = None
        self._day = None
        self._call = None
        self.path = None
        self._opened = []          # files this run wrote

    def _current_call(self) -> str:
        try:
            return safe_call(self.call_fn() if self.call_fn else "")
        except Exception:
            return "NOCALL"

    def path_for(self, call: str, when: float | None = None) -> str:
        when = time.time() if when is None else when
        stamp = time.strftime("%Y-%m-%d_%H%M%S", time.localtime(when))
        return os.path.join(self.dir, f"sara_pp_{safe_call(call)}_{stamp}.log")

    def write(self, line: str):
        try:
            today = _dt.date.today()
            call = self._current_call()
            if (self._fh is not None and self._call == "NOCALL" and call != "NOCALL"
                    and today == self._day and self.path):
                # the callsign wasn't known yet when the file was started (first
                # lines at startup): rename that file rather than leave a stub
                self.close()
                new = self.path.replace("sara_pp_NOCALL_", f"sara_pp_{call}_", 1)
                try:
                    os.replace(self.path, new)
                    self._opened = [new if p == self.path else p for p in self._opened]
                    self.path = new
                except OSError:
                    pass
                self._fh = open(self.path, "a", encoding="utf-8", buffering=1)
                self._call = call
            if self._fh is None or today != self._day or (call != self._call and call != "NOCALL"):
                self.close()
                os.makedirs(self.dir, exist_ok=True)
                self.path = self.path_for(call)
                self._fh = open(self.path, "a", encoding="utf-8", buffering=1)
                self._day = today
                self._call = call
                if self.path not in self._opened:
                    self._opened.append(self.path)
                self._fh.write(f"===== {time.strftime('%Y-%m-%d %H:%M:%S')} "
                               f"(UTC{time.strftime('%z')})  {self.version} =====\n")
            self._fh.write(line.rstrip("\n") + "\n")
        except Exception:
            pass

    def close(self):
        try:
            if self._fh is not None:
                self._fh.close()
        except Exception:
            pass
        self._fh = None

    def recent_files(self, days: int = KEEP_DAYS_IN_BUNDLE) -> list[str]:
        """Session logs from the last `days` days (oldest first), whatever
        their name -- older versions' sara_pp_YYYY-MM-DD.log too."""
        cutoff = time.time() - days * 86400
        out = []
        try:
            names = os.listdir(self.dir)
        except OSError:
            names = []
        for n in names:
            p = os.path.join(self.dir, n)
            if n.startswith("sara_pp_") and n.endswith(".log") and os.path.isfile(p):
                try:
                    if os.path.getmtime(p) >= cutoff:
                        out.append(p)
                except OSError:
                    pass
        for p in self._opened:
            if p not in out and os.path.isfile(p):
                out.append(p)
        return sorted(out, key=lambda p: os.path.getmtime(p))


def make_bundle(call: str, station_info: str, window_text: str, files: list[str],
                session_logs: list[str], out_dir: str, now: float | None = None) -> str:
    """Write the zip and return its full path. files / session_logs that don't
    exist are skipped. Raises OSError only if the zip itself can't be written."""
    now = time.time() if now is None else now
    stamp = time.strftime("%Y-%m-%d_%H%M", time.localtime(now))
    name = f"SARA-FM_logs_{safe_call(call)}_{stamp}"
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, name + ".zip")
    n = 2
    while os.path.exists(path):                      # two bundles in the same minute
        path = os.path.join(out_dir, f"{name}_{n}.zip")
        n += 1
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(f"{name}/station_info.txt", station_info)
        z.writestr(f"{name}/log_window.txt", window_text)
        for p in session_logs:
            if os.path.isfile(p):
                z.write(p, f"{name}/session_logs/{os.path.basename(p)}")
        for p in files:
            if os.path.isfile(p):
                z.write(p, f"{name}/{os.path.basename(p)}")
    return path


# ── v0.9.21: a small plain-text test report ─────────────────────────────
# Field: a verbose test run makes session logs of 2-12 MB, nearly all of it
# "[rejected: low_energy ...]" and "[rejected: low_preamble_snr ...]" lines
# (the receiver noting quiet audio or noise, 10-20 times a second). Those are
# counted in the report, not copied; everything else is kept in full.
NOISY_REJECTS = ("low_energy", "low_preamble_snr")
_REJECT_RE = re.compile(r"\[rejected: ([a-z_]+)")


def make_report(call: str, station_info: str, session_logs: list[str], out_dir: str,
                now: float | None = None) -> str:
    """Write SARA-FM_report_<CALL>_<time>.txt (station info, a summary, then
    this run's session log minus the noisy reject lines) and return its path."""
    now = time.time() if now is None else now
    stamp = time.strftime("%Y-%m-%d_%H%M", time.localtime(now))
    name = f"SARA-FM_report_{safe_call(call)}_{stamp}"
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, name + ".txt")
    n = 2
    while os.path.exists(path):
        path = os.path.join(out_dir, f"{name}_{n}.txt")
        n += 1
    kept, counts, summaries, n_lines = [], {}, [], 0
    for p in session_logs:
        try:
            with open(p, encoding="utf-8", errors="replace") as fh:
                kept.append(f"----- {os.path.basename(p)} -----\n")
                for line in fh:
                    n_lines += 1
                    m = _REJECT_RE.search(line)
                    if m:
                        counts[m.group(1)] = counts.get(m.group(1), 0) + 1
                        if m.group(1) in NOISY_REJECTS:
                            continue
                    if "Session summary" in line:
                        summaries.append(line)
                    kept.append(line if line.endswith("\n") else line + "\n")
        except OSError:
            continue
    with open(path, "w", encoding="utf-8") as out:
        out.write(station_info)
        out.write("\n== Winlink/BPQ32 sessions ==\n")
        out.writelines(summaries or ["(none in this run)\n"])
        out.write("\n== Rejected candidates (counted; quiet/noise ones not listed below) ==\n")
        for reason, k in sorted(counts.items(), key=lambda kv: -kv[1]):
            out.write(f"  {k:7d}  {reason}\n")
        out.write(f"\n== Log ({len(kept)} of {n_lines} lines) ==\n")
        out.writelines(kept)
    return path
