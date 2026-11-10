# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 PAREDN (Philippine Amateur Radio Emergency Data Network) / DW1IRF
"""
web_ui.py -- SARA FM on a phone browser (v0.9.81).

For a Raspberry Pi in a go-kit with no screen: SARA FM runs as usual (its
window on a virtual display), and this serves a small web page on the local
network -- the Pi's own Wi-Fi hotspot, a LAN, Tailscale -- with the meters,
lamps, Start / Ping / Tune, the target station, audio levels, the log and
SARA Chat. It works the same on a Windows gateway (``--web``).

Threads: the web server runs on its own threads and NEVER touches Tk. The Tk
main loop takes a snapshot every 0.5 s (``_tick``) and runs the actions the
page asked for from a queue, so every Tk call stays on the main thread (the
0.9.77 rule).

Access follows the KISS port's address rule: by default "private" -- this
computer, Tailscale, AREDN and LAN ranges. Like KISS, there is no password:
anyone allowed in can key the transmitter.
"""
import json
import os
import queue
import socket
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_PORT = 8090      # v0.9.82: 8080 is BPQ32 / LinBPQ's own web port
LOG_KEEP = 600
CHAT_KEEP = 60

# settings the page may change: key -> (app attribute, kind)
SETTINGS = {
    "callsign": ("call_var", "str"),
    "modulation": ("modulation_var", "choice"),
    "rx_gain_db": ("rx_gain_db_var", "int"),
    "tx_level_pct": ("tx_level_var", "int"),
    "tx_delay_ms": ("tx_delay_var", "int"),
    "input_device": ("in_dev_var", "choice"),
    "output_device": ("out_dev_var", "choice"),
    "ptt": ("ptt_var", "choice"),
    "ra_board": ("ra_board_var", "choice"),
    "serial_port": ("serial_port_var", "str"),
    "pi_gpio_pin": ("pi_gpio_var", "int"),
    "dcd_mode": ("dcd_mode_var", "choice"),
    "gateway": ("gateway_var", "bool"),
    "digipeater": ("digi_var", "bool"),
    "digi_alias": ("digi_alias_var", "str"),
    "slow_pc_buffer": ("slow_pc_var", "bool"),
    "no_coded_modes": ("no_coded_var", "bool"),
}


def _meter(g):
    """(value, caption) of a gauges.AnalogMeter, or (None, '')."""
    if g is None:
        return None, ""
    try:
        v = getattr(g, "_target_db", None)
        cap = g.itemcget(g._readout, "text")
        return (None if v is None else round(float(v), 1)), cap
    except Exception:
        return None, ""


def _lamp(l):
    try:
        on = getattr(l, "_on", None)
        return bool(on) and on != getattr(l, "OFF", None)
    except Exception:
        return False


class WebUI:
    def __init__(self, app, port: int = DEFAULT_PORT, allow: str = "private", headless: bool = False,
                 mail_port: int | None = None):
        self.app = app
        self.mail_port = mail_port      # v0.9.82: PAREDN Webmail on this computer (Mail button)
        self.port = int(port)
        self.headless = headless
        import bpq_bridge
        self._nets, err = bpq_bridge.parse_allowlist(allow or "private")
        if err:
            self._nets, _ = bpq_bridge.parse_allowlist("private")
        self._ip_allowed = bpq_bridge.ip_allowed
        self._lock = threading.Lock()
        self._state = {}
        self._logs = deque(maxlen=LOG_KEEP)
        self._log_seq = 0
        self._notices = deque(maxlen=20)
        self._actions = queue.Queue()
        self._server = None
        self.url = ""

    # ── wiring into the app (main thread) ────────────────────────────
    def start(self):
        orig = self.app._log

        def log_and_keep(msg, _orig=orig):
            _orig(msg)
            self._keep_log(str(msg))

        self.app._log = log_and_keep
        if self.headless:
            self._patch_dialogs()
        handler = self._handler_class()
        self._server = ThreadingHTTPServer(("0.0.0.0", self.port), handler)
        self._server.daemon_threads = True
        self.port = self._server.server_address[1]
        threading.Thread(target=self._server.serve_forever, name="web-ui", daemon=True).start()
        ips = []
        try:
            ips = list(self.app._local_ipv4())
        except Exception:
            pass
        shown = [ip for ip in ips if not ip.startswith("127.")] or ["this-computer"]
        self.url = f"http://{shown[0]}:{self.port}/"
        self.app._log(f"Web page on: " + ", ".join(f"http://{ip}:{self.port}/" for ip in shown)
                      + "  (phone or PC on the same network; no password -- anyone allowed "
                        "in can transmit)")
        self._tick()

    def stop(self):
        if self._server is not None:
            try:
                self._server.shutdown()
                self._server.server_close()
            except Exception:
                pass
            self._server = None

    def _keep_log(self, msg: str):
        now = time.time()
        stamp = time.strftime('%H:%M:%S', time.localtime(now))
        with self._lock:
            self._log_seq += 1
            self._logs.append((self._log_seq, stamp, msg))

    def _patch_dialogs(self):
        """With no one at the screen, a dialog box would wait forever and
        stop the program: show it on the page and in the log instead."""
        def note(kind):
            def f(title="", message="", **kw):
                text = f"{title}: {message}" if title else str(message)
                with self._lock:
                    self._notices.append({"t": time.time(), "kind": kind, "text": text})
                try:
                    self.app._log(f"[{kind}] {text}")
                except Exception:
                    pass
                return "ok"
            return f

        import tkinter.messagebox as mb
        for name, kind in (("showinfo", "info"), ("showwarning", "warning"), ("showerror", "error")):
            setattr(mb, name, note(kind))
        mb.askyesno = lambda *a, **k: False
        mb.askokcancel = lambda *a, **k: False
        mb.askyesnocancel = lambda *a, **k: None

    def _tick(self):
        try:
            while True:
                try:
                    fn = self._actions.get_nowait()
                except queue.Empty:
                    break
                try:
                    fn()
                except Exception as e:
                    self.app._log(f"Web page action failed: {e}")
            snap = self.snapshot()
            with self._lock:
                self._state = snap
        except Exception as e:                       # never let the page stop the program
            try:
                self.app._log(f"Web page update failed: {e}")
            except Exception:
                pass
        try:
            self.app.after(500, self._tick)
        except Exception:
            pass

    # ── what the page shows (main thread) ────────────────────────────
    def _var(self, name, default=""):
        v = getattr(self.app, name, None)
        try:
            return v.get() if v is not None else default
        except Exception:
            return default

    def _values(self, combo_name):
        c = getattr(self.app, combo_name, None)
        try:
            return list(c["values"]) if c is not None else []
        except Exception:
            return []

    def snapshot(self) -> dict:
        a = self.app
        rx_v, rx_cap = _meter(getattr(a, "gauge", None))
        sn_v, sn_cap = _meter(getattr(a, "snr_gauge", None))
        sp_v, sp_cap = _meter(getattr(a, "speed_gauge", None))
        dcd_color = ""
        try:
            dcd_color = a.dcd_lamp.itemcget(a._dcd_dot, "fill")
        except Exception:
            pass
        import ofdm_core
        import tuning
        settings = {}
        for key, (attr, kind) in SETTINGS.items():
            if getattr(a, attr, None) is not None:
                settings[key] = self._var(attr)
        dcd_labels = getattr(a, "DCD_LABELS", {})
        try:
            import audio_io
            dcd_opts = [dcd_labels[m] for m in audio_io.DCD_MODES if m in dcd_labels]
        except Exception:
            dcd_opts = list(dcd_labels.values())
        targets = []
        for t in getattr(a, "targets", []) or []:
            try:
                targets.append({"call": t[0], "loc": t[1], "levels": t[2]})
            except Exception:
                pass
        with self._lock:
            notices = list(self._notices)
        return {
            "version": ofdm_core.VERSION,
            "call": self._var("call_var"),
            "running": getattr(a, "engine", None) is not None,
            "status": self._var("status_var"),
            "mode_line": self._var("sb_mode_var"),
            "dcd_text": self._var("dcd_text_var"),
            "tune": self._var("tune_status_var"),
            "tune_short": self._var("sb_tune_var"),
            "snr_big": self._var("snr_big_var"),
            "lamps": {"tx": _lamp(getattr(a, "tx_lamp", None)), "rx": _lamp(getattr(a, "rx_lamp", None)),
                      "tcp": _lamp(getattr(a, "tcp_lamp", None)),
                      "dcd": dcd_color},
            "meters": {"rx": {"v": rx_v, "cap": rx_cap, "lo": -60, "hi": 0,
                              "target": tuning.TARGET_LEVEL_DBFS, "window": tuning.TARGET_WINDOW_DB},
                       "snr": {"v": sn_v, "cap": sn_cap, "lo": 0, "hi": 40},
                       "speed": {"v": sp_v, "cap": sp_cap, "lo": 0, "hi": 3000}},
            "target": self._var("target_var"),
            "target_levels": self._var("target_prof_var"),
            "targets": targets,
            "settings": settings,
            "options": {
                "modulation": ["Auto", "BPSK", "QPSK", "QPSK Wide", "16QAM Wide C", "16QAM Wide",
                               "64QAM Wide C", "64QAM Wide"],
                "input_device": self._values("in_dev_combo"),
                "output_device": self._values("out_dev_combo"),
                "ptt": ["VOX", "CM108", "RTS", "DTR", "Pi GPIO"],
                "ra_board": self._values("ra_board_combo"),
                "serial_port": self._values("serial_port_combo"),
                "dcd_mode": dcd_opts,
            },
            "chat": self._chat_snapshot(),
            "notices": notices,
            "headless": self.headless,
            "window_shown": self._window_shown(),
            "platform": __import__("sys").platform,
            "mail_port": self.mail_port,
            "t": time.time(),
        }

    def _window_shown(self) -> bool:
        try:
            return self.app.state() != "withdrawn"
        except Exception:
            return False

    def _chat_snapshot(self) -> dict:
        ch = getattr(self.app, "chat", None)
        if ch is None:
            return {"convs": []}
        convs = []
        try:
            import chat as _chat
            for peer, lst in ch.convs.items():
                msgs = []
                for e in lst[-CHAT_KEEP:]:
                    msgs.append({"dir": e.get("dir"), "frm": e.get("frm", ""), "text": e.get("text", "")
                                 or (f"[file] {e.get('name', '')}" if e.get("kind") == "file" else ""),
                                 "ts": e.get("ts", 0), "status": e.get("status", ""), "note": e.get("note", "")})
                last = lst[-1].get("ts", 0) if lst else 0
                convs.append({"peer": peer, "net": peer == _chat.NET, "unread": ch.unread.get(peer, 0),
                              "last": last, "msgs": msgs})
            convs.sort(key=lambda c: (not c["net"], -c["last"]))
            heard = [{"call": c, **{k: v for k, v in h.items() if k in ("last", "snr", "mode", "how")}}
                     for c, h in sorted(ch.heard.items(), key=lambda kv: -kv[1].get("last", 0))][:20]
        except Exception:
            heard = []
        return {"convs": convs, "heard": heard}

    # ── actions (queued from the web threads, run on the main thread) ─
    def _do(self, name: str, args: dict):
        a = self.app
        if name == "start":
            if a.engine is None:
                a._toggle_connect()
        elif name == "stop":
            if a.engine is not None:
                a._toggle_connect()
        elif name == "ping":
            a._send_ping()
        elif name == "tune":
            a._start_autotune()
        elif name == "tune_tx":
            a._start_txtune()
        elif name == "tune10":
            a._start_tune()
        elif name == "tx_anyway":
            a._dcd_override()
        elif name == "target":
            call = str(args.get("call", "")).strip().upper()
            if call:
                a.target_var.set(call)
                a._on_target_selected()
                a._save_settings()
        elif name == "save_target":
            a._save_target()
        elif name == "set":
            self._apply_settings(args.get("settings") or {})
        elif name == "refresh_devices":
            a._refresh_devices()
            a._refresh_ra_boards()
            if hasattr(a, "_refresh_serial_ports"):
                a._refresh_serial_ports()
        elif name == "test_ptt":
            a._test_ptt()
        elif name == "chat_send":
            to, text = str(args.get("to", "")).strip(), str(args.get("text", ""))
            import chat as _chat
            if to.upper() == _chat.NET:
                a.chat.send_net(text)
            elif to and text.strip():
                a.chat.send_text(to, text)
        elif name == "chat_net":
            a.chat.send_net(str(args.get("text", "")), kind=str(args.get("kind", "msg")))
        elif name == "chat_read":
            a.chat.mark_read(str(args.get("peer", "")))
            if hasattr(a, "_update_chat_button"):
                a._update_chat_button()
        elif name == "show_window":            # v0.9.83: Windows, started with --headless
            a.deiconify(); a.lift()
        elif name == "hide_window":
            a.withdraw()
        elif name == "quit":
            a._log("SARA FM closed from the web page.")
            a.after(300, a.destroy)
        elif name == "dismiss":
            with self._lock:
                self._notices.clear()
        else:
            raise ValueError(f"unknown action {name!r}")

    def _apply_settings(self, new: dict):
        a = self.app
        changed = []
        for key, val in new.items():
            if key not in SETTINGS:
                continue
            attr, kind = SETTINGS[key]
            var = getattr(a, attr, None)
            if var is None:
                continue
            if kind == "bool":
                val = bool(val)
            elif kind == "int":
                try:
                    val = str(int(float(val)))
                except (TypeError, ValueError):
                    continue
            else:
                val = str(val)
                if kind == "choice":
                    opts = self.snapshot_options().get(key)
                    if opts and val not in opts:
                        continue
            if key == "callsign":
                val = val.strip().upper()
            var.set(val)
            changed.append(key)
        if "ptt" in changed and hasattr(a, "_on_ptt_method_change"):
            a._on_ptt_method_change()
        if "no_coded_modes" in changed and hasattr(a, "_apply_caps"):
            a._apply_caps()
        if "slow_pc_buffer" in changed and hasattr(a, "_apply_lite"):
            a._apply_lite(a.slow_pc_var.get())
        if "digipeater" in changed and hasattr(a, "_on_digi_change"):
            a._on_digi_change()
        if "tx_delay_ms" in changed:
            a._apply_tx_delay()
        if changed:
            a._save_settings()
            a._log("Settings changed from the web page: " + ", ".join(changed))
            if hasattr(a, "_settings_closed"):
                a._settings_closed()          # v0.9.89: restarts by itself when needed

    def snapshot_options(self) -> dict:
        with self._lock:
            return dict(self._state.get("options", {}))

    # ── HTTP ─────────────────────────────────────────────────────────
    def _handler_class(self):
        ui = self

        class H(BaseHTTPRequestHandler):
            server_version = "SARA-FM"

            def log_message(self, *a):          # keep the console quiet
                pass

            def _allowed(self) -> bool:
                ip = self.client_address[0]
                if ui._ip_allowed(ip, ui._nets):
                    return True
                self.send_error(403, "Not allowed from this address (SARA FM KISS 'Allow from' rule)")
                return False

            def _send(self, code, body: bytes, ctype: str):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def _json(self, obj, code=200):
                self._send(code, json.dumps(obj).encode("utf-8"), "application/json")

            def do_GET(self):
                if not self._allowed():
                    return
                path = self.path.split("?", 1)[0]
                if path in ("/", "/index.html"):
                    self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
                elif path == "/api/state":
                    with ui._lock:
                        st = ui._state
                    self._json(st)
                elif path == "/api/log":
                    since = 0
                    if "since=" in self.path:
                        try:
                            since = int(self.path.split("since=", 1)[1].split("&")[0])
                        except ValueError:
                            since = 0
                    with ui._lock:
                        lines = [l for l in ui._logs if l[0] > since]
                    self._json({"lines": lines[-300:]})
                else:
                    self.send_error(404)

            def do_POST(self):
                if not self._allowed():
                    return
                if self.path.split("?", 1)[0] != "/api/action":
                    self.send_error(404)
                    return
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                    req = json.loads(self.rfile.read(min(n, 65536)) or b"{}")
                    name = str(req.get("action", ""))
                    args = req.get("args") or {}
                except Exception:
                    self._json({"ok": False, "error": "bad request"}, 400)
                    return
                if name not in ACTIONS:
                    self._json({"ok": False, "error": "unknown action"}, 400)
                    return
                ui._actions.put(lambda: ui._do(name, args))
                self._json({"ok": True})

        return H


ACTIONS = {"start", "stop", "ping", "tune", "tune_tx", "tune10", "tx_anyway", "target", "save_target",
           "set", "refresh_devices", "test_ptt", "chat_send", "chat_net", "chat_read", "dismiss",
           "show_window", "hide_window", "quit"}


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#1b2024">
<title>SARA FM</title>
<style>
:root{--bg:#f3f4f2;--panel:#fff;--ink:#1d2328;--muted:#5d6870;--rule:#d5dad8;--accent:#2563a6;
--ok:#2f8a4e;--warn:#c27a0a;--bad:#c0392b;--tx:#e03131;--rx:#2f9e44;--track:#e6e9e8;--btn:#e8ebea}
@media (prefers-color-scheme:dark){:root{--bg:#14181b;--panel:#1b2024;--ink:#e4e8e6;--muted:#9aa5ab;
--rule:#2c3338;--accent:#6aa6e8;--ok:#5cc07e;--warn:#f0a634;--bad:#e5806b;--track:#2a3136;--btn:#252b30}}
*{box-sizing:border-box}html,body{margin:0}
body{background:var(--bg);color:var(--ink);font:15px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif;
padding:12px max(12px,env(safe-area-inset-left)) 80px}
.wrap{max-width:720px;margin:0 auto;display:flex;flex-direction:column;gap:12px}
header{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap}
h1{font-size:20px;margin:0}.sub{color:var(--muted);font-size:13px}
.card{background:var(--panel);border:1px solid var(--rule);border-radius:10px;padding:12px}
.lamps{display:flex;gap:14px;flex-wrap:wrap}.lamp{display:flex;align-items:center;gap:6px;font-weight:600;font-size:13px}
.dot{width:14px;height:14px;border-radius:50%;background:var(--track);border:1px solid var(--rule)}
.meter{margin:8px 0}.mrow{display:flex;justify-content:space-between;font-size:13px}
.mrow b{font-variant-numeric:tabular-nums}
.bar{position:relative;height:14px;background:var(--track);border-radius:7px;overflow:hidden;margin-top:4px}
.fill{position:absolute;left:0;top:0;bottom:0;background:var(--accent);border-radius:7px;transition:width .3s}
.band{position:absolute;top:0;bottom:0;background:var(--ok);opacity:.25}
.tune{font-size:17px;font-weight:600}
.btns{display:grid;grid-template-columns:repeat(auto-fit,minmax(96px,1fr));gap:8px}
button{font:inherit;font-weight:600;padding:12px 8px;border-radius:8px;border:1px solid var(--rule);
background:var(--btn);color:var(--ink);cursor:pointer;min-height:46px}
button.primary{background:var(--accent);color:#fff;border-color:transparent}
button.stop{background:var(--bad);color:#fff;border-color:transparent}
button:active{transform:translateY(1px)}
select,input[type=text],input[type=number]{font:inherit;width:100%;padding:10px;border-radius:8px;
border:1px solid var(--rule);background:var(--panel);color:var(--ink);min-height:44px}
label{display:block;font-size:13px;color:var(--muted);margin:8px 0 3px}
.tabs{display:flex;gap:6px;position:sticky;top:0;background:var(--bg);padding:6px 0;z-index:2}
.tabs button{flex:1;min-height:40px;padding:8px}
.tabs button[aria-selected=true]{background:var(--ink);color:var(--bg)}
.log{font:12px/1.4 ui-monospace,Consolas,monospace;white-space:pre-wrap;word-break:break-word;
max-height:60vh;overflow:auto}
.notice{border-left:4px solid var(--warn);padding:8px 10px;background:var(--panel);border-radius:6px}
.notice.error{border-color:var(--bad)}
.chatlist button{width:100%;text-align:left;margin-bottom:6px;display:flex;justify-content:space-between}
.msgs{display:flex;flex-direction:column;gap:6px;max-height:50vh;overflow:auto;margin:8px 0}
.msg{max-width:85%;padding:8px 10px;border-radius:10px;background:var(--btn)}
.msg.out{align-self:flex-end;background:var(--accent);color:#fff}
.msg.sys{align-self:center;background:transparent;color:var(--muted);font-size:12px}
.msg small{display:block;opacity:.75;font-size:11px}
.row{display:flex;gap:8px}.row>*{flex:1}
.badge{background:var(--bad);color:#fff;border-radius:10px;padding:0 7px;font-size:12px}
.hint{color:var(--muted);font-size:12px}
.maillink{margin-left:auto;font-weight:600;text-decoration:none;color:#fff;background:var(--ok);padding:8px 14px;border-radius:8px}
[hidden]{display:none!important}
</style></head><body><div class="wrap">
<header><h1 id="call">SARA FM</h1><span class="sub" id="ver"></span><span class="sub" id="conn"></span>
<a id="mail" class="maillink" hidden>Mail</a></header>
<div id="notices"></div>
<nav class="tabs" role="tablist">
 <button role="tab" data-tab="radio" aria-selected="true">Radio</button>
 <button role="tab" data-tab="chat">Chat <span id="unread" class="badge" hidden></span></button>
 <button role="tab" data-tab="log">Log</button>
 <button role="tab" data-tab="setup">Setup</button>
</nav>

<section id="tab-radio">
 <div class="card"><div class="lamps">
  <span class="lamp"><span class="dot" id="l-tx"></span>TX</span>
  <span class="lamp"><span class="dot" id="l-rx"></span>RX</span>
  <span class="lamp"><span class="dot" id="l-dcd"></span>DCD</span>
  <span class="lamp"><span class="dot" id="l-tcp"></span>TCP</span>
 </div><div class="hint" id="dcdtext"></div></div>
 <div class="card">
  <div class="meter"><div class="mrow"><span>RX level</span><b id="m-rx-c"></b></div>
   <div class="bar"><div class="band" id="m-rx-band"></div><div class="fill" id="m-rx"></div></div></div>
  <div class="meter"><div class="mrow"><span>S/N</span><b id="m-snr-c"></b></div>
   <div class="bar"><div class="fill" id="m-snr"></div></div></div>
  <div class="meter"><div class="mrow"><span>Speed</span><b id="m-speed-c"></b></div>
   <div class="bar"><div class="fill" id="m-speed"></div></div></div>
  <div class="hint" id="modeline"></div>
 </div>
 <div class="card"><div class="tune" id="tune">Tune: not run yet</div></div>
 <div class="card">
  <label for="target">Target station</label>
  <div class="row"><select id="target"></select></div>
  <div class="hint" id="tlevels"></div>
 </div>
 <div class="btns">
  <button id="b-start" class="primary">Start</button>
  <button data-act="ping">Ping</button>
  <button data-act="tune">Tune</button>
  <button data-act="tune10">Tune 10 s</button>
  <button data-act="tx_anyway">TX anyway</button>
 </div>
 <div class="card"><div class="hint" id="status"></div></div>
</section>

<section id="tab-chat" hidden>
 <div class="card" id="chat-list"><div class="chatlist" id="convs"></div>
  <label for="newpeer">New conversation</label>
  <div class="row"><input type="text" id="newpeer" placeholder="Callsign" autocapitalize="characters">
  <button id="b-newpeer" style="flex:0 0 90px">Open</button></div>
  <div class="row" style="margin-top:8px"><button id="b-cq">Call CQ</button><button id="b-checkin">Check in</button></div>
 </div>
 <div class="card" id="chat-conv" hidden>
  <div class="row"><button id="b-back" style="flex:0 0 80px">Back</button><h2 id="conv-title" style="margin:0;align-self:center;font-size:17px"></h2></div>
  <div class="msgs" id="msgs"></div>
  <div class="row"><input type="text" id="msgtext" placeholder="Message"><button id="b-send" class="primary" style="flex:0 0 80px">Send</button></div>
 </div>
</section>

<section id="tab-log" hidden><div class="card"><div class="log" id="log"></div></div></section>

<section id="tab-setup" hidden><div class="card" id="setup"></div>
 <div class="btns" style="margin-top:8px"><button id="b-save" class="primary">Save settings</button>
 <button data-act="refresh_devices">Find devices</button><button data-act="test_ptt">Test PTT (2 s)</button></div>
 <p class="hint">Device, PTT and KISS port changes restart SARA FM by itself.</p>
 <div class="btns" id="winbtns" hidden style="margin-top:8px"><button id="b-win"></button>
 <button id="b-quit" class="stop">Close SARA FM</button></div>
 <p class="hint" id="winhint" hidden>On a slow PC, hiding the SARA FM window saves the most CPU: run it from this page instead.</p>
</section>
</div>
<script>
const $=id=>document.getElementById(id);let S=null,logSince=0,openPeer=null,setupBuilt=false;
async function act(action,args){try{await fetch('/api/action',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action,args:args||{}})});}catch(e){}setTimeout(poll,300);}
document.querySelectorAll('[data-act]').forEach(b=>b.onclick=()=>act(b.dataset.act));
$('b-start').onclick=()=>act(S&&S.running?'stop':'start');
document.querySelectorAll('[role=tab]').forEach(t=>t.onclick=()=>{document.querySelectorAll('[role=tab]').forEach(x=>x.setAttribute('aria-selected',x===t));
 ['radio','chat','log','setup'].forEach(n=>$('tab-'+n).hidden=n!==t.dataset.tab);if(t.dataset.tab==='setup')buildSetup(true);});
$('target').onchange=e=>act('target',{call:e.target.value});
function pct(m){if(m.v==null)return 0;return Math.max(0,Math.min(100,(m.v-m.lo)/(m.hi-m.lo)*100));}
function lamp(id,on,color){$(id).style.background=on?color:'';}
function esc(s){return String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
function render(){const s=S;if(!s)return;
 $('call').textContent=(s.call||'SARA FM');$('ver').textContent=s.version;
 $('conn').textContent=s.running?'running':'stopped';
 if(s.mail_port){const m=$('mail');m.hidden=false;m.href=location.protocol+'//'+location.hostname+':'+s.mail_port+'/';}
 lamp('l-tx',s.lamps.tx,'var(--tx)');lamp('l-rx',s.lamps.rx,'var(--rx)');lamp('l-tcp',s.lamps.tcp,'var(--ok)');
 $('l-dcd').style.background=s.lamps.dcd&&s.lamps.dcd!=='#888'&&s.running?s.lamps.dcd:'';
 $('dcdtext').textContent=s.dcd_text?('DCD: '+s.dcd_text):'';
 for(const k of ['rx','snr','speed']){const m=s.meters[k];$('m-'+k).style.width=pct(m)+'%';$('m-'+k+'-c').textContent=m.cap||'';}
 const r=s.meters.rx,lo=(r.target-r.window/2-r.lo)/(r.hi-r.lo)*100,w=r.window/(r.hi-r.lo)*100;
 $('m-rx-band').style.left=lo+'%';$('m-rx-band').style.width=w+'%';
 $('modeline').textContent=s.mode_line||'';$('tune').textContent=s.tune||'Tune: not run yet';
 $('status').textContent=s.status||'';const bs=$('b-start');bs.textContent=s.running?'Stop':'Start';bs.className=s.running?'stop':'primary';
 const sel=$('target');const calls=s.targets.map(t=>t.call);if(s.target&&!calls.includes(s.target))calls.unshift(s.target);
 const html=calls.map(c=>{const t=s.targets.find(x=>x.call===c);return `<option value="${esc(c)}">${esc(c)}${t&&t.loc?' — '+esc(t.loc):''}</option>`}).join('');
 if(sel.dataset.h!==html){sel.innerHTML=html;sel.dataset.h=html;}if(document.activeElement!==sel)sel.value=s.target;
 $('tlevels').textContent=s.target_levels||'';
 $('notices').innerHTML=s.notices.map(n=>`<div class="notice ${n.kind}">${esc(n.text)}</div>`).join('')+(s.notices.length?'<button onclick="act(\'dismiss\')">Dismiss</button>':'');
 renderChat();if(!setupBuilt)buildSetup(false);
 const wb=s.platform==='win32';$('winbtns').hidden=!wb;$('winhint').hidden=!wb;
 if(wb){$('b-win').textContent=s.window_shown?'Hide window on the PC':'Show window on the PC';}}
function renderChat(){const c=S.chat||{convs:[]};let unread=0;c.convs.forEach(x=>unread+=x.unread||0);
 $('unread').hidden=!unread;$('unread').textContent=unread;
 $('convs').innerHTML=c.convs.map(x=>`<button data-peer="${esc(x.peer)}"><span>${esc(x.net?'NET room':x.peer)}</span>${x.unread?`<span class="badge">${x.unread}</span>`:''}</button>`).join('')||'<p class="hint">No conversations yet.</p>';
 $('convs').querySelectorAll('button').forEach(b=>b.onclick=()=>openConv(b.dataset.peer));
 if(openPeer){const cv=c.convs.find(x=>x.peer===openPeer);const box=$('msgs');const atEnd=box.scrollTop+box.clientHeight>=box.scrollHeight-20;
  box.innerHTML=(cv?cv.msgs:[]).map(m=>`<div class="msg ${m.dir==='out'?'out':m.dir==='sys'?'sys':''}">${m.dir==='in'&&cv.peer==='*NET'?'<b>'+esc(m.frm)+'</b> ':''}${esc(m.text)}<small>${new Date(m.ts*1000).toLocaleTimeString()}${m.status?' · '+esc(m.status):''}${m.note?' · '+esc(m.note):''}</small></div>`).join('');
  if(atEnd)box.scrollTop=box.scrollHeight;if(cv&&cv.unread)act('chat_read',{peer:openPeer});}}
function openConv(p){openPeer=p;$('chat-list').hidden=true;$('chat-conv').hidden=false;$('conv-title').textContent=p==='*NET'?'NET room':p;renderChat();$('msgs').scrollTop=1e9;}
$('b-back').onclick=()=>{openPeer=null;$('chat-list').hidden=false;$('chat-conv').hidden=true;};
$('b-newpeer').onclick=()=>{const p=$('newpeer').value.trim().toUpperCase();if(p){$('newpeer').value='';openConv(p);}};
$('b-send').onclick=()=>{const t=$('msgtext').value;if(t.trim()&&openPeer){act('chat_send',{to:openPeer,text:t});$('msgtext').value='';}};
$('msgtext').onkeydown=e=>{if(e.key==='Enter')$('b-send').click();};
$('b-cq').onclick=()=>act('chat_net',{kind:'cq',text:''});$('b-checkin').onclick=()=>act('chat_net',{kind:'checkin',text:''});
const FIELDS=[['callsign','My callsign','text'],['modulation','Modulation','select'],['rx_gain_db','RX gain (dB)','number'],
 ['tx_level_pct','TX level (%)','number'],['tx_delay_ms','TX delay (ms)','number'],['input_device','Input device','select'],
 ['output_device','Output device','select'],['ptt','PTT','select'],['ra_board','RA-Board (CM108)','select'],
 ['serial_port','Serial port (RTS / DTR)','text'],['pi_gpio_pin','Pi GPIO pin (BCM)','number'],['dcd_mode','DCD mode','select'],
 ['gateway','This station is a gateway (one set of levels for every caller)','check'],['slow_pc_buffer','Slow PC mode: larger audio buffer, lighter window (buffer takes effect at Start)','check'],['no_coded_modes','No C modes: ask others not to send 16QAM / 64QAM Wide C here','check'],['digipeater','Digipeater on','check'],['digi_alias','Digipeater alias','text']];
function buildSetup(force){if(!S||(setupBuilt&&!force))return;const st=S.settings,op=S.options;
 $('setup').innerHTML=FIELDS.filter(f=>f[0] in st).map(([k,l,t])=>{const v=st[k];
  if(t==='select'){let o=(op[k]||[]).slice();if(v&&!o.includes(v))o.unshift(v);return `<label for="f-${k}">${l}</label><select id="f-${k}">${o.map(x=>`<option${x===v?' selected':''}>${esc(x)}</option>`).join('')}</select>`;}
  if(t==='check')return `<label><input type="checkbox" id="f-${k}"${v?' checked':''}> ${l}</label>`;
  return `<label for="f-${k}">${l}</label><input type="${t}" id="f-${k}" value="${esc(v)}">`;}).join('');
 const dl=op.serial_port||[];if(dl.length&&$('f-serial_port'))$('f-serial_port').setAttribute('placeholder',dl.join(', '));setupBuilt=true;}
$('b-save').onclick=()=>{const out={};FIELDS.forEach(([k,,t])=>{const el=$('f-'+k);if(!el)return;out[k]=t==='check'?el.checked:el.value;});act('set',{settings:out});setTimeout(()=>{setupBuilt=false;},800);};
$('b-win').onclick=()=>act(S&&S.window_shown?'hide_window':'show_window');
$('b-quit').onclick=()=>{if(confirm('Close SARA FM on the PC? The radio goes off the air.'))act('quit');};
async function poll(){try{const r=await fetch('/api/state');S=await r.json();render();}catch(e){$('conn').textContent='no connection to SARA FM';}}
async function pollLog(){try{const r=await fetch('/api/log?since='+logSince);const j=await r.json();if(j.lines.length){const box=$('log');const atEnd=box.scrollTop+box.clientHeight>=box.scrollHeight-20;
 box.textContent+=j.lines.map(l=>l[1]+'  '+l[2]).join('\n')+'\n';logSince=j.lines[j.lines.length-1][0];
 if(box.textContent.length>200000)box.textContent=box.textContent.slice(-150000);if(atEnd)box.scrollTop=box.scrollHeight;}}catch(e){}}
poll();pollLog();setInterval(poll,1000);setInterval(pollLog,2000);
</script></body></html>
"""
