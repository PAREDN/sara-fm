# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 PAREDN (Philippine Amateur Radio Emergency Data Network) / DW1IRF
"""
chat_window.py -- the SARA Chat window (v0.9.38), opened from SARA FM's
Chat menu or the Chat button. All logic lives in chat.ChatEngine; this file
only shows it.

  left:   Chats (NET + each station, unread and held counts) and the Heard
          list (callsign, last heard, S/N, how, checked in)
  right:  the conversation -- each message with its time and status
          (sending, sent, delivered, held, failed) -- and the box to type in
  top:    To: (start a chat), CQ and Check in for the NET, Send file...
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import chat

STATUS_TEXT = {
    "queued": "queued", "sending": "sending...", "sent": "sent ✓", "delivered": "delivered ✓✓",
    "retrying": "resending...", "held": "held -- will deliver when heard", "failed": "failed ✗",
    "cancelled": "cancelled", "receiving": "receiving...", "received": "received ✓",
}
STATUS_COLOR = {"delivered": "#1f7a3a", "received": "#1f7a3a", "sent": "#555555", "held": "#b06a00",
                "failed": "#c0261d", "retrying": "#b06a00", "cancelled": "#888888"}


def _ago(t) -> str:
    if not t:
        return ""
    d = max(0, time.time() - t)
    if d < 60:
        return "just now"
    if d < 3600:
        return f"{int(d // 60)} min ago"
    if d < 86400:
        return f"{int(d // 3600)} h ago"
    return time.strftime("%b %d", time.localtime(t))


def _open_path(path: str):
    try:
        if sys.platform.startswith("win"):
            os.startfile(path)                      # noqa  (Windows only)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception as e:
        messagebox.showerror("Open file", str(e))


class ChatWindow(tk.Toplevel):
    def __init__(self, app, engine: chat.ChatEngine):
        super().__init__(app)
        self.app, self.engine = app, engine
        self.title("SARA Chat")
        self.geometry("900x580")
        self.minsize(700, 420)
        self.protocol("WM_DELETE_WINDOW", self.withdraw)
        self.current = chat.NET
        self._chat_keys: list = []
        self._build()
        self.refresh_lists()
        self.show_conversation(chat.NET)

    # ── layout ───────────────────────────────────────────────────────
    def _build(self):
        top = ttk.Frame(self)
        top.pack(fill="x", padx=8, pady=(8, 4))
        ttk.Label(top, text="To:").pack(side="left")
        self.to_var = tk.StringVar()
        self.to_combo = ttk.Combobox(top, textvariable=self.to_var, width=14)
        self.to_combo.pack(side="left", padx=4)
        self.to_combo.bind("<Return>", lambda e: self._open_to())
        ttk.Button(top, text="Open chat", command=self._open_to).pack(side="left")
        ttk.Separator(top, orient="vertical").pack(side="left", fill="y", padx=10)
        ttk.Button(top, text="CQ", width=6, command=lambda: self._net("cq")).pack(side="left")
        ttk.Button(top, text="Check in", command=lambda: self._net("checkin")).pack(side="left", padx=4)
        self.status_var = tk.StringVar(value="")
        ttk.Label(top, textvariable=self.status_var, foreground="#555").pack(side="right")

        body = ttk.PanedWindow(self, orient="horizontal")
        body.pack(fill="both", expand=True, padx=8, pady=4)

        left = ttk.Frame(body, width=250)
        body.add(left, weight=0)
        ttk.Label(left, text="Chats", font=("TkDefaultFont", 10, "bold")).pack(anchor="w")
        self.chat_list = tk.Listbox(left, height=8, activestyle="none", exportselection=False,
                                    font=("TkDefaultFont", 10))
        self.chat_list.pack(fill="x")
        self.chat_list.bind("<<ListboxSelect>>", self._on_chat_pick)
        ttk.Label(left, text="Heard", font=("TkDefaultFont", 10, "bold")).pack(anchor="w", pady=(8, 0))
        cols = ("call", "last", "snr", "how")
        self.heard_tv = ttk.Treeview(left, columns=cols, show="headings", height=10, selectmode="browse")
        for c, w, t in (("call", 80, "Call"), ("last", 70, "Last"), ("snr", 44, "S/N"), ("how", 56, "How")):
            self.heard_tv.heading(c, text=t)
            self.heard_tv.column(c, width=w, anchor="w", stretch=False)
        self.heard_tv.pack(fill="both", expand=True)
        self.heard_tv.bind("<Double-1>", self._on_heard_open)
        ttk.Label(left, text="Double-click a station to chat.  ✔ = checked in",
                  foreground="#666").pack(anchor="w")

        right = ttk.Frame(body)
        body.add(right, weight=1)
        self.head_var = tk.StringVar()
        ttk.Label(right, textvariable=self.head_var, font=("TkDefaultFont", 11, "bold")).pack(anchor="w")
        self.sub_var = tk.StringVar()
        ttk.Label(right, textvariable=self.sub_var, foreground="#555", wraplength=580,
                  justify="left").pack(anchor="w")
        # the typing area is packed from the bottom first, so it never gets squeezed
        ttk.Label(right, text="Enter sends, Shift+Enter starts a new line. A message that can't be "
                              "delivered now is held and sent when the station is next heard.",
                  foreground="#666", wraplength=580, justify="left").pack(side="bottom", anchor="w", pady=(2, 0))
        bottom = ttk.Frame(right)
        bottom.pack(side="bottom", fill="x")
        tf = ttk.Frame(right)
        tf.pack(fill="both", expand=True, pady=(4, 4))
        self.text = tk.Text(tf, wrap="word", state="disabled", font=("TkDefaultFont", 10),
                            padx=8, pady=6, spacing1=2, spacing3=4)
        sb = ttk.Scrollbar(tf, command=self.text.yview)
        self.text.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.text.pack(side="left", fill="both", expand=True)
        t = self.text
        t.tag_configure("me", foreground="#0b4f8a", font=("TkDefaultFont", 10, "bold"))
        t.tag_configure("them", foreground="#1f6b2f", font=("TkDefaultFont", 10, "bold"))
        t.tag_configure("time", foreground="#888888", font=("TkDefaultFont", 8))
        t.tag_configure("sys", foreground="#777777", font=("TkDefaultFont", 9, "italic"))
        t.tag_configure("cq", foreground="#a0005a", font=("TkDefaultFont", 10, "bold"))
        t.tag_configure("note", foreground="#b06a00", font=("TkDefaultFont", 8, "italic"))
        t.tag_configure("link", foreground="#0b4f8a", underline=True)
        t.tag_bind("link", "<Enter>", lambda e: t.configure(cursor="hand2"))
        t.tag_bind("link", "<Leave>", lambda e: t.configure(cursor=""))
        for st, col in STATUS_COLOR.items():
            t.tag_configure("st_" + st, foreground=col, font=("TkDefaultFont", 8))
        t.tag_configure("st_other", foreground="#888888", font=("TkDefaultFont", 8))

        self.entry = tk.Text(bottom, height=3, wrap="word", font=("TkDefaultFont", 10))
        self.entry.pack(side="left", fill="x", expand=True)
        self.entry.bind("<Return>", self._on_enter)
        btns = ttk.Frame(bottom)
        btns.pack(side="left", padx=(6, 0), fill="y")
        self.send_btn = ttk.Button(btns, text="Send", command=self.send)
        self.send_btn.pack(fill="x")
        self.file_btn = ttk.Button(btns, text="Send file...", command=self.send_file)
        self.file_btn.pack(fill="x", pady=(4, 0))

    # ── lists ────────────────────────────────────────────────────────
    def refresh_lists(self):
        e = self.engine
        keys = [chat.NET] + sorted([k for k in e.convs if k != chat.NET],
                                   key=lambda k: -max([x.get("ts", 0) for x in e.convs[k]] or [0]))
        if self.current not in keys:
            keys.append(self.current)
        self._chat_keys = keys
        sel = keys.index(self.current) if self.current in keys else 0
        self.chat_list.delete(0, "end")
        for k in keys:
            label = chat.NET_LABEL if k == chat.NET else k
            n = e.unread.get(k, 0)
            h = e.held_for(k) if k != chat.NET else 0
            if n:
                label += f"   ({n} new)"
            if h:
                label += f"   [{h} held]"
            self.chat_list.insert("end", label)
        self.chat_list.selection_clear(0, "end")
        self.chat_list.selection_set(sel)
        self.heard_tv.delete(*self.heard_tv.get_children())
        today = time.time() - 12 * 3600
        for call, h in sorted(e.heard.items(), key=lambda kv: -kv[1].get("last", 0)):
            mark = " ✔" if h.get("checkin", 0) > today else ""
            snr = h.get("snr")
            self.heard_tv.insert("", "end", iid=call, values=(call + mark, _ago(h.get("last")),
                                                              "" if snr is None else f"{snr:+d}", h.get("how", "")))
        self.to_combo["values"] = sorted(set(list(e.heard) + [k for k in e.convs if k != chat.NET]))
        self._update_status()

    def _update_status(self):
        app = self.app
        if getattr(app, "engine", None) is None:
            s = "Modem stopped -- press Start in SARA FM to chat"
        elif hasattr(app, "_session_busy") and app._session_busy():
            s = "Winlink session in progress -- chat waits until it's idle"
        else:
            s = f"Ready  ({self.engine.me() or 'set My callsign'})"
        self.status_var.set(s)

    def _on_chat_pick(self, _e=None):
        sel = self.chat_list.curselection()
        if sel and sel[0] < len(self._chat_keys):
            self.show_conversation(self._chat_keys[sel[0]])

    def _on_heard_open(self, _e=None):
        sel = self.heard_tv.selection()
        if sel:
            self.show_conversation(sel[0])

    def _open_to(self):
        call = chat.norm(self.to_var.get())
        if not call:
            return
        self.to_var.set("")
        self.show_conversation(call)

    # ── conversation ─────────────────────────────────────────────────
    def show_conversation(self, peer: str):
        self.current = peer
        self.engine.mark_read(peer)
        e = self.engine
        if peer == chat.NET:
            self.head_var.set("NET -- everyone on frequency")
            self.sub_var.set("NET messages go out once to every SARA FM station listening "
                             "(no delivery receipts). Use CQ to call, Check in to join a net.")
            self.file_btn.state(["disabled"])
        else:
            h = e.heard.get(peer, {})
            self.head_var.set(f"Chat with {peer}")
            bits = [f"last heard {_ago(h['last'])}" if h.get("last") else "not heard yet"]
            if h.get("snr") is not None:
                bits.append(f"we hear them S/N {h['snr']:+d} dB")
            if h.get("they_hear") is not None:
                bits.append(f"they hear us {h['they_hear']:+d} dB")
            held = e.held_for(peer)
            if held:
                bits.append(f"{held} message{'s' if held > 1 else ''} held for them")
            self.sub_var.set("   ·   ".join(bits))
            self.file_btn.state(["!disabled"])
        self._render()
        self.refresh_lists()
        upd = getattr(self.app, "_update_chat_button", None)
        if upd:
            upd()

    def _render(self):
        t = self.text
        t.configure(state="normal")
        t.delete("1.0", "end")
        me = self.engine.me()
        self._links = {}
        for i, x in enumerate(self.engine.convs.get(self.current, [])):
            stamp = time.strftime("%H:%M", time.localtime(x.get("ts", 0)))
            if x.get("dir") == "sys":
                t.insert("end", f"{stamp}  {x.get('text', '')}\n", "sys")
                continue
            who = x.get("frm") or me
            t.insert("end", stamp + "  ", "time")
            kind = x.get("kind")
            if kind == "cq":
                t.insert("end", f"{who}  CQ  ", "cq")
            elif kind == "checkin":
                t.insert("end", f"{who}  checked in  ", "cq")
            else:
                t.insert("end", f"{who}: ", "me" if x.get("dir") == "out" else "them")
            if kind == "file":
                size = x.get("size", 0)
                t.insert("end", "[file] ", "time")
                t.insert("end", f"{x.get('name')}  ({max(1, size // 1024)} kB)")
                if x.get("status") in ("sending", "receiving", "queued", "retrying"):
                    t.insert("end", f"  {x.get('done', 0)}/{x.get('n', 0)} pieces")
                if x.get("path"):
                    tag = f"link{i}"
                    self._links[tag] = x["path"]
                    t.insert("end", "   ")
                    t.insert("end", "Open", ("link", tag))
                    t.tag_bind(tag, "<Button-1>", lambda e, p=x["path"]: _open_path(p))
                    t.insert("end", "   ")
                    t.insert("end", "Folder", ("link", tag + "d"))
                    t.tag_bind(tag + "d", "<Button-1>", lambda e, p=x["path"]: _open_path(os.path.dirname(p)))
            elif kind != "checkin":
                t.insert("end", x.get("text", ""))
            st = x.get("status")
            if x.get("dir") == "out" and st:
                t.insert("end", "   " + STATUS_TEXT.get(st, st),
                         "st_" + st if st in STATUS_COLOR else "st_other")
            elif x.get("dir") == "in" and kind == "file" and st:
                t.insert("end", "   " + STATUS_TEXT.get(st, st),
                         "st_" + st if st in STATUS_COLOR else "st_other")
            if x.get("dir") == "in" and x.get("snr") is not None:
                t.insert("end", f"   S/N {x['snr']:+.0f}", "time")
            t.insert("end", "\n")
            note = x.get("note") if x.get("status") in ("held", "failed", "retrying") else None
            if x.get("held_note"):
                note = f"held by {who}'s station until you were heard -- written " + \
                       time.strftime("%b %d %H:%M", time.localtime(x.get("sent_ts", 0)))
            if note:
                t.insert("end", f"        {note}\n", "note")
        t.configure(state="disabled")
        t.see("end")

    # ── actions ──────────────────────────────────────────────────────
    def _on_enter(self, ev):
        if ev.state & 0x0001:          # Shift: new line
            return None
        self.send()
        return "break"

    def send(self):
        text = self.entry.get("1.0", "end").strip()
        if not text:
            return
        if not self.engine.me():
            messagebox.showwarning("SARA Chat", "Set My callsign in Settings > SARA Setup first.", parent=self)
            return
        if self.current == chat.NET:
            e = self.engine.send_net(text)
            if e is not None and e.get("status") == "failed":
                self._update_status()
                messagebox.showinfo("SARA Chat", e.get("note", "Not sent."), parent=self)
                return
        else:
            self.engine.send_text(self.current, text)
        self.entry.delete("1.0", "end")
        self._render()
        self.refresh_lists()

    def _net(self, kind: str):
        if not self.engine.me():
            messagebox.showwarning("SARA Chat", "Set My callsign in Settings > SARA Setup first.", parent=self)
            return
        e = self.engine.send_net("", kind)
        self.show_conversation(chat.NET)
        if e is not None and e.get("status") == "failed":
            messagebox.showinfo("SARA Chat", e.get("note", "Not sent."), parent=self)

    def send_file(self):
        if self.current == chat.NET:
            return
        path = filedialog.askopenfilename(parent=self, title=f"Send a file to {self.current}")
        if not path:
            return
        try:
            with open(path, "rb") as fh:
                data = fh.read()
            secs = len(data) * 8 / 700           # chat windows + receipts: ~700 bps in QPSK
            if len(data) > 8 * 1024 and not messagebox.askyesno(
                    "SARA Chat", f"{os.path.basename(path)} is {len(data) // 1024} kB -- about "
                                 f"{secs / 60:.0f} min on the air at typical speed. Send it?", parent=self):
                return
            self.engine.send_file(self.current, os.path.basename(path), data)
        except (OSError, ValueError) as ex:
            messagebox.showwarning("SARA Chat", str(ex), parent=self)
            return
        self._render()
        self.refresh_lists()

    # ── engine events (GUI thread) ───────────────────────────────────
    def on_event(self, kind: str, data: dict):
        if not self.winfo_exists():
            return
        if kind in ("message", "status", "file"):
            peer = data.get("peer") or (data.get("entry") or {}).get("to")
            entry = data.get("entry") or {}
            if entry.get("dir") == "in":
                peer = chat.NET if entry.get("to") == chat.NET else entry.get("frm")
            if peer == self.current and self.state() == "normal":
                self.engine.mark_read(peer)
                self._render()
                upd = getattr(self.app, "_update_chat_button", None)
                if upd:
                    self.after_idle(upd)
        self.refresh_lists()
