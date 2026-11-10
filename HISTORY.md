# SARA FM — development history

This is the full development record, newest version first after the
original design notes. For an introduction, see README.md.

## Original design notes: SARA-PP, a minimal OFDM ping/pong link tester

A stripped-down, from-scratch rebuild aimed at one question only:
**does station A's ping reliably produce station B's pong, symmetrically,
over the real link?** No BPQ32, no KISS, no AX.25, no mode negotiation,
no VARA/AGWPE passthrough. Once this is rock solid, features get added
back one at a time — each one re-tested with the same offline suite
before it ever touches a radio.

```
Your Radio ←→ Soundcard ←→ sara_pingpong.py ←(GUI)
```

## Why this exists

The original SARA FM (v1.4) had a real, diagnosed bug: a 5 ms amplitude
ramp in `frame_encode()` landed 37.5% of the way into the *first*
preamble repetition, cutting its average transmit energy roughly in
half — right where the receiver most needs a clean, strong reference.
That bug lived specifically because TX shaping (ramps, squelch tone,
PTT settle silence) and the OFDM payload were built inside the same
function, across a fairly large codebase (AFSK mode, OFDM mode, GUI,
VARA/AGWPE passthrough, IL2P — a lot of surface area for a subtle bug
to hide in).

This rebuild fixes that by construction, not just by shrinking a
number:

- **`ofdm_core.py`** does DSP *only* — encode, detect, decode. It never
  adds silence, keying delay, or a squelch tone. Its only shaping is a
  small (1.5 ms) edge taper that never meaningfully touches a preamble
  repetition — see the module docstring and the regression test that
  guards this (`test_preamble_energy_is_flat` in `test_offline.py`).
- **`audio_io.py`** is the *only* place PTT timing and lead-in/tail
  silence are added, wrapped around the DSP burst.
- Preamble detection was also rewritten to use delayed autocorrelation
  (the actual Schmidl-Cox technique) instead of fixed-template
  cross-correlation, because template correlation turned out to be very
  fragile to sub-symbol timing offset — the kind of offset any real
  capture (unknown leading silence, unknown RX start point) will have.
  This was caught by the offline test suite before ever reaching a
  radio.

## Files

| File | What it is |
|---|---|
| `ofdm_core.py` | Pure DSP — waveform gen, preamble detection, encode/decode. No hardware deps. |
| `protocol.py` | The tiny PING/PONG payload format. |
| `audio_io.py` | Soundcard I/O + the TX chain (silence/PTT timing lives here only). |
| `ptt.py` | PTT control (VOX / CM108 / RTS / DTR) — carried over from the original project; hardware-specific and unrelated to the DSP bug. |
| `kiss.py` | KISS protocol framing (encode/decode). Pure, no network or hardware. |
| `fec.py` | Reed-Solomon forward error correction (pure, no hardware, no extra dependency): repairs damaged frames instead of dropping them. |
| `throughput.py` | Link speed meter (pure): live and session-average bps for bridge mode. |
| `build_exe.bat` | Builds the standalone Windows SARA-PP (no Python needed on gateways). Run once per release on a PC with Python. |
| `QUICKSTART_GATEWAY.txt` | Install/config steps for volunteers receiving the standalone build. |
| `tuning.py` | Level/S-N targets and advice for Auto-Tune, the two-way ping report, and the RX meter (pure). |
| `ping_window.py` | VARA-style graphical ping / Auto-Tune result window (two towers, colour-coded S/N and level). |
| `fm_channel.py` | FM radio channel simulator (test helper): modulation, RF noise, discriminator -- incl. open-squelch hiss and quieting. |
| `bpq_bridge.py` | TCP server BPQ32 connects to; bridges KISS ↔ the OFDM physical layer, coalescing rapid small frames into fewer transmissions. |
| `BPQ32.cfg.example` | BPQ32-side port config — the exact settings BPQ32 needs to talk to SARA-PP. |
| `sara_pingpong.py` | The GUI — run this (BPQ32/KISS bridge; PING and Auto-Tune built in). |
| `test_offline.py` | Offline regression suite — **run this after every change, before touching a radio.** |
| `sara_pingpong_log.csv` | Auto-created test log — every ping/pong/timeout gets a row. |

## Quick start

```
pip install -r requirements.txt
python test_offline.py      # should print "N passed, 0 failed" in under a second
python sara_pingpong.py
```

In the GUI: pick your input/output audio device, your PTT method, enter
your callsign, hit **Start**, then **PING**. Leave Target as `*` to
ping any listening station, or put a specific callsign in it.

For a real pass-rate number instead of eyeballing a handful of manual
pings, use **Auto-ping**: set a count and an interval (seconds between
sends), hit **Start Auto**, and walk away — it sends that many pings
back-to-back, tallies pass/timeout as results come in, and logs a
`BATCH_SUMMARY` row (with pass rate %) to the CSV when done. Stop Auto
cancels any pings not yet sent but still waits for ones already in
flight before finalizing the summary, so a stopped batch's numbers stay
accurate rather than silently dropping in-flight results.

## The waveform (for reference)

| Parameter | Value |
|---|---|
| Sample rate | 48000 Hz |
| Subcarriers | 16 (bins 5–20, ~469–1875 Hz), 4 pilot + 12 data |
| Modulation | BPSK (default, ≈900 bps) or QPSK (≈1800 bps) — picked per-frame in the GUI |
| Symbol | 512 FFT + 128 cyclic prefix = 640 samples (13.33 ms) |
| Preamble | 4× repeated known symbol (always BPSK), detected via delayed autocorrelation |
| Frame CRC | CRC-16/CCITT-FALSE |

**Modulation is self-describing, not a setting the two stations have to
agree on.** Every frame's header carries 1 bit saying whether its data
symbols are BPSK or QPSK — the receiver reads this off each frame, so
you can set one station to QPSK and ping/pong both directions without
touching the other station's dropdown at all. A station replying to a
ping always echoes back in the modulation the ping arrived in.

QPSK roughly doubles throughput (2 bits/carrier instead of 1) at the
cost of less noise margin — the header itself is always BPSK regardless
of the data mode, since the receiver has to read it before it can know
which mode to expect.

## Test log format

`sara_pingpong_log.csv` gets one row per event: `TX_PING`, `TX_PONG`,
`RX_PING`, `RX_PONG_MATCHED` (with round-trip time), `RX_PONG_UNMATCHED`,
`TIMEOUT`. Columns include the firmware `version` string, so after a
session you can filter by version and station pair and actually see
whether a change helped — the same idea we talked about for making
updates easy to track, just automatic instead of a manually kept log.

## Versioning workflow

- `ofdm_core.VERSION` is a single string, shown in the GUI title bar and
  written into every CSV row. Bump it when you change the DSP.
- Use git. This project is initialized as a repo (see below) — commit
  every change with a message that says what changed and why (e.g.
  *"shorten edge taper, move detection to autocorrelation"*), and tag
  what you actually burn onto each station:
  ```
  git tag v0.1.0-test1
  ```
  so that when a test fails, you can say with certainty which commit
  was running on which radio — something the old fade-window bug made
  hard to pin down, since it was fixed once, reportedly unsuccessfully,
  with no way to confirm afterward whether the fix actually shipped.

## BPQ32/KISS bridge

Once the link itself is proven (this is the point the project reached
after sustained 30/30 ping/pong results in both BPSK and QPSK), SARA-PP
can act as the actual modem a BPQ32 node talks to, instead of a
standalone test tool.

In the GUI, switch **Mode** from "Ping/Pong Test" to "BPQ32/KISS
Bridge". This shares every other setting (audio devices, PTT, GPIO pin,
TX delay, modulation) with ping/pong mode — nothing needs
reconfiguring. Set **KISS TCP port** (default 8105) and click **Start
Bridge**.

SARA-PP is the TCP *server* here; BPQ32 connects *to* it. See
`BPQ32.cfg.example` for the exact port config — it carries forward
specific lessons from the original project's BPQ32 setup (`TYPE=ASYNC`
not `TYPE=KISS`, `TCPPORT=` not `IPPORT=`, `PROTOCOL=KISS` on its own
line) that weren't obvious from BPQ32's own documentation the first
time around. Start SARA-PP's bridge before starting/restarting BPQ32.

What crosses the bridge is raw AX.25 bytes, KISS-framed exactly as
BPQ32 expects — SARA-PP doesn't parse or care about AX.25 structure
itself, it just carries whatever BPQ32 hands it over the air and hands
back whatever it decodes. `kiss.py` (pure framing, no network) and
`bpq_bridge.py` (the TCP server) are the two pieces; both are covered
by `test_offline.py`, including a full software-only round trip
(KISS frame → OFDM air link → KISS frame again) with no BPQ32, radio,
or network required to verify the whole chain.

## v0.9.96 — cleaner main window; new-version notice

- The main window has no button row any more: Ping and Chat are on the
  menu bar. Chat on the menu bar shows "(1 new)" for unread messages.
- The big "Tune: not run yet" banner is gone; Auto-Tune's result shows in
  the Ping window and the log, and in the status bar while it runs.
- New version notice, like VARA FM: once per start (20 s in) SARA FM asks
  GitHub for the latest release of PAREDN/sara-fm. If it is newer, the
  status bar says "New SARA FM x.y.z available -- click to download".
  Help > Check for a new version checks by hand; Settings > SARA Setup can
  turn the automatic check off. Only the version number goes out (in the
  User-Agent). It works once the repository is public; with no internet it
  stays quiet. New `update_check.py`.

## v0.9.95 — the station list moves into the Ping window

Field (DW1IRF): from View > Gauges only, Ping opened the Ping window but
there was no way to save the adjusted levels -- Save was on the main
window's Target row, which that view hides.

- The Ping window has Location, Save and Remove, plus the station's saved
  levels. Save keeps the station, its location and the levels on the
  sliders; Remove takes it off the list.
- The main window no longer has the Target, Location, Save and Remove row;
  Modulation stays there.

## v0.9.94 — Ping window sliders follow the station picked

Field (DW1IRF, 0.9.93): picking a station in the Ping window didn't move the
sliders to that station's saved levels. With "This station is a gateway"
ticked, picking a station keeps the gateway's levels (0.9.78), and that rule
applied in the Ping window too.

- Picking a station in the Ping window always brings back its saved levels,
  and the sliders show them, also on a gateway: pinging and tuning from the
  Ping window is this station calling out (as in 0.9.87). The gateway's own
  levels come back for the next caller.
- Settings menu: SARA Setup and Devices only (Tune removed; View > Full shows
  the Target row).
- View menu: "Gauges only" (no "to sit next to VARA FM").

## v0.9.93 — menus laid out like VARA FM

Asked for by the sysop, so volunteers who know VARA FM find their way:

- Menu bar: Settings, View, Ping, Log, Chat, Terminal, Help. Chat and
  Terminal open their windows directly.
- Settings: SARA Setup, Devices, Tune. Tune shows the full window (Target,
  Location, Save, Modulation, PING). "Tune TX level only" is gone (Auto-Tune
  does both). "Start SARA FM with Windows" moved into SARA Setup;
  "Restart SARA FM" and the 10-second test tone (was TUNE 10s) moved into
  Devices, beside Test PTT.
- Main window: only PING (and Chat) on the button row; Tune and TUNE 10s
  are gone from it.
- Ping window: "Ping to:" station picker, so several stations can be pinged
  without closing it (as in VARA FM); picking one brings back its saved
  levels. Two sliders, Transmit audio (TX level %) and Receive audio (RX
  gain dB), to set levels by hand; Save in the main window keeps them.
- SARA Chat: dark (white text on black) by default, with a Light / Dark
  button that is remembered; a Files button opens the received-files folder.
- Messages that said "press TUNE 10s" or "press Tune" now name the new
  places.

## v0.9.92 — the Windows build's tests no longer depend on the PC

Field: `build_exe.bat` for 0.9.91 stopped with 2 failed tests on DW1IRF's
PC (all passed on the test machines). The tests were reading the real
sara_pp_settings.json in the build folder. That file had "This station is
a gateway" ticked, and a gateway keeps one set of levels on purpose, so
"picking a station puts back its saved levels" failed. Since 0.9.89,
closing a settings window can also start the modem, so on a PC with a real
callsign, sound card and RA-Board a test could start the real modem.

- The offline tests use their own temporary settings folder and never read
  or write the real settings file.
- Closing a settings window starts or restarts SARA FM only when it was
  opened to run (main()), not when a test opens the window.

## v0.9.91 — SARA Terminal: acknowledgments and typed lines go out

Field (0.9.90, DW1IRF-7 -> DV1UK-12 BPQ Chat Server): the terminal
connected and the server asked for a name, but nothing typed ever arrived.
DV1UK's log shows it polling DW1IRF-7 every 16 s and hearing nothing back
except the connect and the disconnect. The transmitter was dropping every
terminal I frame and acknowledgment: with no KISS client connected (Winlink
Express had disconnected), SARA FM treats queued data frames as left over
from a session that's over (the 0.9.70 rule), and the terminal's frames were
caught by it. Only U frames (SABM, DISC) passed.

- The terminal's own frames are exempt from the "KISS client gone" and
  "pointless resend" rules, which are about the KISS client's frames. The
  KISS client's leftovers are still dropped as before.

## v0.9.90 — SARA Terminal (connect to a BPQ32 node / Chat Server)

Asked for by the sysop: a plain terminal like VARA Terminal, to reach the
BPQ32 Chat Server on DX1PRD-12 (Mt Mataba) over SARA FM, the way EasyTerm
reaches it over 1200-baud packet with the UZ7HO soundmodem.

- Chat > SARA Terminal: My call (default this station with -7, e.g.
  DW1IRF-7), Connect to (e.g. DX1PRD-12, or "DX1PRD-12 via DW1IRF"),
  Connect / Disconnect, a text window and a typing line. Type CHAT once
  connected.
- New `ax25_term.py`: a small AX.25 v2.0 link layer (modulo 8): connect /
  disconnect, numbered frames with acknowledgments, resends after a lost
  frame (REJ) or a quiet timer (poll), a check on a quiet link every
  5 minutes, and giving up after 6 tries. Timers suit SARA FM air times
  (12 s, like FRACK=12000 on the gateways).
- This is the only place SARA FM makes AX.25 frames itself, and only for
  the terminal's own connection under the terminal's own callsign. Frames
  of BPQ32 / Winlink Express sessions are still never created, changed or
  answered by SARA FM. Received frames for the terminal's callsign go to
  the terminal and never to the KISS client; incoming connections to the
  terminal are refused (DM).
- On the air: SARA FM modes, Auto speed, link reports and the "don't
  answer in a window's gaps" rule, exactly as for BPQ32's frames. The log
  shows them as TX (TERM->air).
- The node needs a SARA FM port in its BPQ32 to hear the terminal.

## v0.9.89 — starts by itself, like VARA FM

Asked for by the sysop: no more pressing Start, and Ping within reach in the
Gauges-only view.

- No Start button. SARA FM starts the sound card, PTT and KISS port as soon
  as it opens. On a PC that isn't set up yet, it opens SARA Setup or Devices
  instead of showing an error, and starts when that window is closed.
- Closing a settings window after changing the sound card, PTT, KISS port,
  network access or Slow PC mode restarts SARA FM by itself (also from the
  phone page). During a Winlink transfer it waits until the transfer is idle.
  Settings > Restart SARA FM does it by hand.
- Ping and Tune are first on the menu bar, so they work in View > Gauges
  only.
- Settings > Start SARA FM with Windows (Windows only) adds or removes a
  shortcut in the Windows Startup folder.
- One copy per folder: Winlink Express can launch SARA FM itself ("Packet
  sound modem" path + "Automatically launch packet sound modem"). A second
  copy from the same folder brings the running one to the front and closes,
  instead of fighting it for the sound card and KISS port. Copies in
  different folders (a gateway and a client on one PC) still run side by
  side. It starts half a second after opening, so it is ready when Winlink
  Express connects.
- `--no-autostart` opens SARA FM without starting (for testing);
  `--autostart` is no longer needed.

## v0.9.88 — don't answer in the gaps of the other station's window

Field: Winlink Express on DW1IRF's PC used DX1FLR's radio over Tailscale
(KISS TCP) to send to the DW1IRF-10 gateway. DX1FLR sent 7-frame windows
with 0.2-0.4 s gaps between frames. BPQ32 at the gateway acknowledged each
frame as it arrived. The gateway's SARA FM read the channel clear in a gap
and sent the acknowledgment on top of DX1FLR's next frame, which was lost.
Every window needed a REJ and a resend, Auto dropped DX1FLR to BPSK (longer
windows, more gaps), and both sessions ended before the message went
through. All 8 lost frames came right after one of the gateway's own
transmissions.

- When the last frame heard from another station is an I frame with no
  poll bit, that station's window isn't over. Our next AX.25 transmission
  now waits for 1.2 s of continuous clear channel, as digipeater relays
  already do (0.9.66), instead of the first clear moment. A window that
  ends with a poll (RR p, or an I frame with P) is answered at once as
  before. The hold lapses 6 s after the last frame heard.
- A strong frame that won't decode within 4 s of this station's own
  transmission no longer counts toward the "OVERDRIVEN transmitter" note.
  It was a collision, and the note told DX1FLR to lower a TX level that
  was fine.

## v0.9.87 — saved levels for the station you call

Field: DW1IRF called DX1FLR-10 on 0.9.86 and the session ran at DV1UK's
levels (TX 14%, RX gain 26). DX1FLR heard DW1IRF at S/N 15-17 dB instead
of the usual 22-25 dB, and DW1IRF received DX1FLR at -1 dBFS (too hot).
So Auto stayed in narrow QPSK for most of the session: 5.2 kB took 1:19.
The cause was that the new 0.9.86 folder's settings file had no levels
saved for DX1FLR. DX1FLR's own 0.9.86 report was clean: no lost audio, and
the decoder peaked at 40% of a core (it was 108% before).

- A station with "This station is a gateway" ticked now uses the saved
  levels for the station it CALLS. When the call finishes, or the next
  caller connects, it goes back to its gateway levels. Callers still always
  get the gateway levels.
- When there are no saved levels for the station on a new connection, the
  log says so and suggests running Auto-Tune once.
- When this folder has no saved levels for that station, SARA FM looks in
  the other SARA FM folders next to it (an older version's folder in
  Downloads, say). If one with the same sound card has levels saved for
  that station, it copies them in and uses them, and logs where they came
  from.
- The station report shows a "Gateway:" line and which stations have
  saved levels.

## v0.9.86 — numpy's maths library on one thread

Field: DX1FLR on 0.9.85, with Slow PC mode on, now decoded ten 16QAM Wide C
frames in a row (about 5 kB) where 0.9.84 lost the first C burst. A 5.2 kB
message arrived intact. There was still one sound-card input loss per
session; when that loss hits a C frame, BPQ32 resends the whole 7-frame
window. The report showed the decoder peaking at 108% of a core and
"waiting for a free core". No change on the air.

- **numpy's maths library (OpenBLAS) now runs on one thread.** By default it
  starts a worker thread for every core, and idle workers spin for a while,
  competing with the sound card on a slow 4-core PC. SARA FM's matrices are
  small, so one thread is just as fast and leaves the other cores free.

## v0.9.85 — coded modes decode at half the CPU; optional "No C modes"

Field: DX1FLR on 0.9.84, with Slow PC mode on, still lost sound-card input,
but only while 16QAM Wide C frames were arriving. The QPSK Wide frames that
followed decoded cleanly. No change on the air.

- **Faster Viterbi decoder.** The branch metrics are worked out in two matrix
  products up front, the adds are done in place, and the path metrics are
  renormalised every 64 steps instead of every step. It gives the same
  output as before: a new test checks this at both code rates, from clean to
  very noisy, and all 20 test vectors still pass. Measured decode cost for
  one 632-byte frame:

  | Mode | Before | After |
  | --- | --- | --- |
  | 16QAM Wide C | 157 ms | 77 ms |
  | QPSK Wide | 70 ms | 69 ms |

  So a C-mode frame now costs about what a QPSK Wide frame costs, where
  before it was twice as much.
- **"No C modes"** (Settings > SARA Setup > Link, also on the phone page) is
  the last resort for a PC that still can't keep up. This station stops
  announcing that it decodes the coded modes, so other stations send it QPSK
  Wide / 16QAM Wide instead; a sender already in a C mode steps down on the
  next report. It's off by default. The "not keeping up" warning suggests it
  when Slow PC mode is already on.

## v0.9.84 — Slow PC mode lightens the window; hide the window and run from the phone

Field: with 0.9.83 and most other programs closed, DV1OTT and the DX1FLR
gateway still warned "not keeping up". DV1OTT's own numbers showed the SARA
FM window using 13–23% of a core, more than decoding, and freezing for up to
0.6 s. No change on the air.

- **"Slow PC mode"** (the 0.9.83 checkbox, renamed) now also:
  - redraws the meters 4 times a second with no needle swing, instead of 15
  - updates the meters, lamps and DCD text 3 times a second
  - keeps 500 lines in the on-screen log instead of 3,000 (the log files
    still keep everything)
  - lets the sound card's callback get the processor sooner when the
    decoder or the window is busy (Python thread switch interval 1 ms)

  Measured on a test PC with the window busy: the window's cost dropped
  from 3.3% to 2.3% of a core.
- **Hidden window on Windows:** start `SARA-FM.exe --headless --autostart`. It runs with
  no window, and the phone page on port 8090 controls it. Measured: 1.5% of
  a core, less than half of the normal window. The page's Setup tab has
  "Show / Hide window on the PC" and "Close SARA FM".
- `QUICKSTART_GATEWAY.txt` §14 lists the steps for a slow gateway PC: Slow PC
  mode, then hidden window, then a Pi.

## v0.9.83 — slow PCs: larger audio buffer; KISS port takeover warning

Field test DV1OTT ↔ DU3FS (0.9.79). The radio link was clean: DU3FS → DV1OTT
data carriers at 15–19 dB, and Tune approved both ways. Transfers still ran
at only 600–1,086 bps, and one message failed twice. No change on the air.

- **Slow PC: larger audio buffer** (Settings > SARA Setup > Link, also on the
  phone page). DV1OTT's PC lost sound-card input 1–6 times every 10 s while
  frames were arriving: its decoder was 30–48% busy where DU3FS's was 2–3%,
  with window freezes up to 0.6 s. Frames arrived with S/N 25–30 dB and still
  failed, with phase jumps of 50–175° where audio was missing. Each loss made
  BPQ32 resend the rest of its window (14–19 frames per session), and Auto
  bounced between 16QAM Wide C and QPSK Wide.
  - The new setting gives the sound card 4× the slack (341 ms instead of
    85 ms).
  - The "not keeping up" warning now suggests it when input was lost.
  - The station report shows which buffer is in use.
  - The cost: the level meter and carrier sense see audio up to a third of
    a second later.
- **KISS port takeover is reported.** At DU3FS, two programs kept connecting
  to the same KISS port. Each new connection silently replaced the last, so
  the program running a Winlink session was cut off mid-session, and the far
  end's polls went unanswered. Now:
  - the log says when a new connection takes over the port
  - the old one is logged as "cut off (replaced)"
  - two or more takeovers within 2 minutes say that two programs are taking
    turns, and how to fix it

## v0.9.82 — Raspberry Pi go-kit mail: LinBPQ + PAREDN Webmail

PAREDN Webmail on a phone was preferred over Pat and RadioMail, so the Pi
go-kit now uses the same setup as PAREDN's BPQ32 stations: LinBPQ keeps the
mailbox and PAREDN Webmail is the phone's mail page. No change on the air.

- **`pi/install_linbpq.sh CALL`** does three things:
  - downloads G8BPQ's LinBPQ, adding the 32-bit libraries it needs on 64-bit
    Raspberry Pi OS
  - writes a starter `bpq32.cfg` (only if there is none) with the node, the
    BBS, web pages on 8080, and the SARA FM port as in `BPQ32.cfg.example`.
    Passwords are left as CHANGEME.
  - makes LinBPQ start at boot after SARA FM
- **The phone page moved to port 8090** (`--web`, the Pi service) because
  8080 is BPQ32's / LinBPQ's own web port; field (K6IRF): it clashed with the
  BPQ32 node.
- **Mail button** on SARA FM's page (`--mail-port=3000`, on by default on the
  Pi) opens PAREDN Webmail on the same computer.
- **`docs/RASPBERRY_PI.md`** has a new section on LinBPQ + PAREDN Webmail,
  with the port plan: SARA FM 8090, Webmail 3000, LinBPQ 8080, KISS 8105.
- PAREDN Webmail itself goes to 1.4.0 so it runs on Linux too. It is a
  separate package.

## v0.9.81 — Raspberry Pi: runs with no screen, run from a phone

For low-power go-kits and small gateways. No change on the air.

- **Phone web page** (`web_ui.py`). `--web` serves a page on port 8080 with
  everything needed in the field:
  - the RX level, S/N and speed meters and the TX / RX / DCD / TCP lamps
  - the Tune answer, and the Start, Ping, Tune, Tune 10 s and TX anyway buttons
  - the target station
  - SARA Chat: direct messages, the NET room, CQ and check-in
  - the live log
  - Setup: callsign, levels, devices, PTT, DCD mode, gateway, digipeater

  Access follows the KISS "Allow from" rule (private addresses by default),
  with no password, like KISS. The page never touches the window from its own
  threads: the window takes a snapshot every 0.5 s and runs the page's
  actions itself, as 0.9.77 requires.
- **`--headless`** runs SARA FM on a virtual display (xvfb) with the web page,
  and turns dialog boxes into notices on the page. Unattended, a dialog box
  would otherwise wait forever and stop the program. **`--autostart`** presses
  Start once the settings are loaded.
- **PTT:**
  - **Pi GPIO**: a Raspberry Pi pin keys the radio through a transistor or
    opto-isolator.
  - **Serial port** setting for RTS / DTR, e.g. a Digirig on `/dev/ttyUSB0`
    or COM5. Fix: until now RTS / DTR never got a port, so they could not key
    anything.
- **`pi/install_pi.sh`** installs everything on Raspberry Pi OS Bookworm:
  - packages, plus USB-PTT, serial and GPIO permissions
  - the self-test
  - a `sara-fm` service that starts at boot
- **`pi/pat_ax25.sh`** attaches SARA FM to the Linux AX.25 stack for Pat.
- **`docs/RASPBERRY_PI.md`**: the guide, with LinBPQ (same configuration as
  BPQ32), Pat, and the Pi's own Wi-Fi hotspot for the field.
- Not yet tested on a real Pi. The modem code is unchanged, and the web page,
  headless mode and installer scripts were tested on Linux.

## v0.9.80 — open source: GPL v3, protocol specification, test vectors

No change on the air: 0.9.80 talks to 0.9.79 and earlier exactly as before.

- Licensed under the GNU GPL v3 or later (`LICENSE`, a header in every source
  file, and a line in Help > About). The protocol specification is CC BY 4.0.
- `docs/SPEC.md`: the SARA FM Protocol Specification, everything another
  implementation needs to work with SARA FM on the air.
- `test-vectors/`: 20 reference recordings (every mode, clean and noisy,
  multi-block Reed-Solomon, ping, pong, chat, and a batch with a link report),
  each with its bytes at every coding stage. `test-vectors/verify.py` checks
  them, and `tools/make_test_vectors.py` rebuilds them.
- The old README (this file) is now `HISTORY.md`. `README.md` is a short
  introduction for the public repository. The Help menu has entries for both.
- Field tested (0.9.79, DW1IRF): RMS Packet 2.2.3.0 connects to SARA FM's
  KISS TCP port with ACKMODE, the same way BPQ32 does.

## v0.9.79 — the Ping window keeps Tune's one answer

Field (0.9.78, DW1IRF): after Tune, the Ping window still only ever showed
the old wordy note ("Tune TX: TX level 10% -- data carriers 16 dB ... S/N
reads lower here than at 14% ... Link looks good."). The one-ping check sent
right after Tune replaced the new answer with it. That ping now shows the
same answer as the banner (e.g. "APPROVED: Saved for DV1UK: TX 10% ..."); the
numbers go to the log. Ordinary pings no longer say "arrives very quietly:
raise your TX level" when the other station reports clean data carriers.

## v0.9.78 — one Tune button with one answer; gateways keep one set of levels

Volunteers found Ping / Auto-Tune / Tune TX confusing: three panels of
numbers and advice that pulled both ways ("data carriers 16 dB -- clean"
next to "arrives very quietly: raise your TX level, or they raise their
volume").
- One Tune button (Auto-Tune and Tune TX in one, as Auto-Tune already ran
  both) ends with ONE answer, like VARA FM's drive calibration: APPROVED
  (saved), or the one thing to do -- TURN UP / TURN DOWN YOUR TX AUDIO,
  RAISE YOUR TX LEVEL to N%, TURN DOWN YOUR RADIO'S VOLUME, YOUR LEVELS ARE
  FINE (the other station should Tune), WEAK LINK (not a setting), NO
  REPLY. Clean data at the far end is approved even when it arrives
  quietly. The answer shows in the big banner, the Ping window and as the
  last line in the log; the numbers stay in the log. "Tune TX level only" is
  in the Settings menu.
- Gateways: Setup > "This station is a gateway: one set of audio levels for
  every caller". Until now a gateway switched to its saved levels for each
  caller it had tuned with, and kept the LAST caller's levels for everyone
  else. A gateway now keeps one set (set once with Tune); callers tune to it
  and their own levels for that gateway come back each time they connect.

## v0.9.77 — fix: remote-chat client read the window from its network thread

The real cause of the 0.9.75/0.9.76 Windows build failure: the remote-chat
client read the callsign from the window on its own network thread. Windows
Tk only allows that while the window's main loop is running; in the build's
test it isn't, so the thread died ("main thread is not in main loop") and
the test operator never connected. In the running app it would usually have
worked, but it was fragile. The callsign is now read on the window's thread
when chat is switched on, and the test enforces the rule.

## v0.9.76 — build fix for the remote-chat test on Windows

The 0.9.75 Windows build stopped at the remote-chat test ("the operator's
message goes out on the STATION's radio"; it passes on Linux). The likely
cause: its second half reused the port the first half had just closed, and on
Windows a port can be re-opened while the old one is still there. The test
now takes a fresh port, waits longer, and on a failure prints what each side
saw instead of stopping the run. The remote-chat port itself no longer uses
SO_REUSEADDR on Windows, so two SARA FM copies set to the same chat port get
"port in use" instead of silently sharing it.

## v0.9.75 — SARA Chat from another computer

Like VARA Chat with a VARA FM modem: an operator without a radio runs SARA FM
on his own PC, ticks Setup > "Chat through a remote SARA FM station" (the
station's Tailscale address, port 8180), and chats under his own callsign
through that station's radio. The station ticks "Let operators on other
computers chat through this station's radio". Remote chat has its own port,
so the station's BPQ32 keeps the KISS port, and several operators can be
connected at once. Each operator's chat (receipts, resends, held messages,
NET, files) runs on his own PC; the station only carries the frames.
Messages between the station's operator and a remote operator are passed
locally, not aired. Access follows the KISS "Allow from" list.

## v0.9.74 — B2 check only for this station's own sessions

Field (0.9.73): DX1FLR overheard part of DW1IRF's direct session with
DV1UK-10 and logged "B2 message ... data stream broken" for a message that
wasn't its own (DV1UK, the real receiver, logged it intact). Frames between
other stations are still passed to the KISS client as before, but only
sessions addressed to this station's client are checked.

## v0.9.73 — fix: 0.9.72 passed held frames on out of order

Field (0.9.72, DX1FLR): long pauses and a disconnect. When a relay arrived,
0.9.72 passed on "earlier" held direct copies first -- but it judged
"earlier" by decode time, and all frames of one burst share it. So the relay
of {UA I0} delivered I0 ahead of the UA, and {I0..I4} delivered I3 first;
Winlink Express dropped the out-of-order frames and waited on its retry
timer. Held frames now keep the order they were heard in, and only frames
heard strictly before the relayed one are passed on ahead of it.

## v0.9.72 — faster through a digipeater

Field (0.9.71, DX1PRD-2 -> DV1UK-10 via DW1IRF): a 14.7 kB message went
through intact but took 8:43, 2:13 of it waiting on retry timers.
- An answer heard direct is passed on when its relay doesn't decode. DX1FLR
  often heard DV1UK-10's acknowledgment direct, cleanly, but not DW1IRF's
  relay of it -- and SARA FM held the direct copy (v0.9.57) while Winlink
  Express waited 10-25 s to ask again. Now the held copy goes to the client
  (as the relay would deliver it) at once when the relay brings later frames
  without it, else 6 s after it was heard; a late relayed copy is then dropped,
  so the client never gets a frame twice. This also shortens the end of the
  session (FF / FQ / DISC), where the same waits added up.
- Link reports addressed to the KISS client's own callsign count. DW1IRF's
  relays to DX1PRD-2 (Winlink Express at DX1FLR) carry DW1IRF's report on how
  it hears DX1FLR, but DX1FLR only took reports addressed to "DX1FLR" -- so
  its speed to DW1IRF never rose above QPSK at 21 dB.
- A resend of a frame the digipeater was heard repeating no longer slows the
  first hop: that frame was lost beyond the digipeater. (It had dropped
  DX1FLR -> DW1IRF to BPSK for the last 2:20 of the message.)

## v0.9.71 — digipeated Winlink: gateway shows the caller to BPQ32 as direct; B2 messages checked

Field (0.9.55-0.9.70): a Winlink message through SARA FM's digipeater failed
the CMS B2 checksum ("failed to assemble correct binary image") at BOTH
gateways -- DW1IRF -> DX1FLR-10 via DV1UK and DX1PRD-2 -> DV1UK-10 via DW1IRF --
with every frame relayed intact and acknowledged. Comparing the logs: the
same message (45RUB2ULLMYU) went through DX1FLR-10 direct half an hour after
failing via DV1UK, and BPQ32 had been given exactly the same AX.25 frames,
accepted in the same order (content checksums match frame for frame). The
only difference BPQ32 saw was the digipeater in the address field.

- Gateway: a session that comes in through a digipeater is handed to BPQ32 as
  a plain direct connection; SARA FM remembers the path and puts it back on
  BPQ32's replies (reversed, not yet repeated). Same frames, same order, same
  timers -- only the address field. On by default (Setup > Digipeater >
  "Gateway: show callers that come through a digipeater to BPQ32 as
  direct"); the log says "Gateway: DX1PRD-2 connected to DV1UK-10 via DW1IRF:
  BPQ32 is shown a direct connection ...". BPQ32's log then shows the caller
  without the digipeater.
- B2 check: each Winlink message passing the bridge is followed the way an
  AX.25 receiver does (repeats and out-of-order copies dropped) and checked
  when it completes -- block checksum, length against the proposal, size
  field. The client station logs "... checksum OK -- intact (as BPQ32 /
  Winlink Express on this PC sent it)", the gateway "... (as handed to BPQ32
  / Winlink Express on this PC)". If the gateway says intact and the CMS still
  reports a checksum failure, the data was changed after SARA FM.

## v0.9.70 — no 4.5 s wait before transmitting; nothing sent after the session ends

Field (0.9.69, DX1PRD-2 at DX1FLR -> DV1UK-10 via DW1IRF):
- Pressing Start in Winlink Express took ~5 s before SARA FM keyed up, and
  almost every transmission waited "4.5 s leaving the channel to the
  digipeater's relay first". DW1IRF relays DX1FLR's frames in 16QAM, the speed
  for its next hop (DV1UK-10), which DX1FLR can't decode -- so DX1FLR never
  "heard the relay" and kept waiting for it. Now any SARA FM signal heard after
  a frame went out counts as its relay.
- After a session ended, queued data frames still went out (two BPSK frames,
  ~7 s). Once the KISS client has disconnected, its queued data and
  acknowledgments are dropped; DISC / DM / UA still go.

The relay itself worked: DW1IRF heard all seven frames of DX1FLR's window and
DV1UK received every one intact (checksums match). DV1UK-10's BPQ32 then
answered with DISC (first try) or eight FRMRs with an empty reason and a
garbled digipeater address (second try) -- that is BPQ32's side.

## v0.9.69 — a slow RA-Board is reported instead of just freezing the window

Field (0.9.68, DV1UK, two RA-Boards): "Not Responding" after Start and on a
PING. The log shows Start taking ~20 s, "Testing PTT" ~10 s per board instead
of 2 s, and a PING whose reply came 13 s later (DV1UK keyed ~10 s late). The
RA-Board (USB HID) calls were taking seconds, and while Windows answers one,
the whole program waits -- window, audio and all.

- Every RA-Board call slower than 0.5 s is recorded and logged with what it
  was and how long it took, plus what usually causes it.
- If Start takes 3 s or more, the log says how long the PTT/RA-Board part and
  the sound card part took.
- The USB scan for RA-Boards no longer runs twice each time.

## v0.9.68 — fix: the window could lock up for good on a new connection

Field (0.9.66, DX1FLR log): after a dropped session the KISS client
reconnected and SARA FM froze ("Not Responding"); the log stops on "KISS client
connected". On Windows a Tk call such as after() made from another thread is
handed to the window's thread, and the caller waits for it. The bridge thread
did that while holding the Auto-speed lock (logging the speed for the new
connection), while the window's thread was waiting for that same lock to
update the speed display -- each waited for the other forever. A slow,
busy PC makes the moment wide enough to hit.

- after() from any thread other than the window's now only queues the call;
  the window's own loop runs it within 40 ms. Nothing waits on the window any
  more.

## v0.9.67 — a frozen window leaves a record of what it was stuck on

Field (0.9.66, DX1FLR -> DV1UK-10 direct): four messages up to 10 kB went in
0:22-0:41 (64QAM Wide C on the good link); on the fifth the session dropped,
and on the retry SARA FM showed "Not Responding" and had to be closed, so no
report could be saved.

- A watchdog thread notices when the window has not updated for 8 s and writes
  what every part of the program is doing to
  logs/sara_pp_freeze_<CALL>_<date_time>.txt. It runs outside the window, so
  it works while the window is frozen. Send that file (and the sara_pp_*.log
  from the same folder, which is written as the program runs) after a freeze.

## v0.9.66 — a digipeater waits for the sender's whole burst to end

Field (0.9.65, DX1FLR -> DV1UK-10 via DW1IRF): a 218-byte message went
through; a 2.8 kB one failed twice. DX1FLR sent its 7-frame windows in BPSK
(3.4 s a frame, 0.2 s gaps). DW1IRF's channel sense read clear for a moment
between two of them and it keyed up to relay the frames so far -- in the
middle of DX1FLR's window. While relaying it couldn't hear DX1FLR, so it
missed every other frame (I6, I1, I3), and DV1UK-10 answered REJ6 over and
over.

- A digipeater now needs the channel clear for 1.2 s without a break before it
  relays (DIGI_MIN_CLEAR_S). Gaps inside a burst are 0.2 s, so it always waits
  for the sender's window to finish. The sender's own transmissions are not
  affected.

## v0.9.65 — the frame trace shows why a frame was rejected

Field (0.9.64, DX1FLR -> DV1UK-10 via DW1IRF): the digipeater path now works
(connects, CMS prompt, proposal), but every time the message itself went
out, DV1UK-10's BPQ32 answered with FRMR (frame reject) and the session fell
apart. The trace couldn't say what was rejected or why.

- FRMR is decoded: which frame it rejects, the sender's V(S)/V(R), and the
  reason (W control field, X info not allowed, Y info too long, Z bad N(R)).
- The P/F bit shows "p" on a command (poll) and "f" on a response (final),
  "p?" when the frame's C bits don't say.

## v0.9.64 — SSIDs are different stations

DV1UK, DV1UK-4 (BBS) and DV1UK-10 (Winlink gateway) are different stations,
as in VARA FM. Up to 0.9.62 SARA FM compared base callsigns when deciding
whether a relayed frame was its own echo; 0.9.63 stopped using callsigns for
that. The note added in 0.9.63 now also needs the exact callsign, SSID
included, and is worded as a note, not a warning.

## v0.9.63 — only frames this station sent count as "our frame, repeated"

Field (0.9.62, DX1FLR -> DV1UK-10 via DW1IRF): DW1IRF relayed every connect
request, but DV1UK-10 never answered. The connect went out from DX1FLR with
source callsign DV1UK (DV1UK>DV1UK-10 via DW1IRF). DV1UK's SARA FM matched the
source against its own callsign, took the relayed copy for its own frame
echoed back, and never passed it to its BPQ32.

- A relayed frame now counts as our own only if this station actually sent
  it (remembered for 3 minutes). Anything else is passed to BPQ32.
- A relayed frame carrying this station's callsign that it didn't send logs a
  warning: another station, or its BPQ32 / Winlink Express, is set to your
  callsign.

## v0.9.62 — "answer quieter" can't push a reply below -12 dB

Field (0.9.61, DX1FLR <-> DW1IRF): DX1FLR asked DW1IRF to answer quieter on
every ping for a minute; DW1IRF went 14% -> 7% -> 5% and DX1FLR still said
"clipped" -- so it wasn't DW1IRF's level. At 5% DW1IRF's SignaLink VOX barely
keyed and DX1FLR then read DW1IRF as far too quiet (-47 dBFS).

- The pinging station asks once per clipped reply it actually sees, not on
  every ping for 60 s.
- The answering station steps down at most 12 dB below its own TX level. If it
  is still called clipped there, it isn't the TX level: it says so, answers at
  its normal level again and ignores that station's asks for 10 minutes.

Results (DW1IRF -> DV1UK-10 direct, 0.9.61): 2.8-14.9 kB messages in 0:16-1:30,
against 0:23-1:38 with 0.9.55 and 0:47-2:05 with VARA FM 4.4.

## v0.9.61 — resends that can't help are skipped; the digipeater goes first

Field (0.9.60, with the new frame trace): neither the digipeater session nor
the direct one to DV1UK-10 got the message through, and the direct session
was much slower than older versions.

- Direct, DW1IRF Winlink Express -> DV1UK-10, BPSK: Winlink Express times its
  retry (FRACK) from when it hands a frame to the KISS port, not from when it
  goes on the air. A 4-frame BPSK window takes ~13 s to air, so the timer ran
  out while the window was still going out and the same frames were handed
  over again -- and aired straight behind the originals in the same key-up
  (I5 I6 I7 I0 I5 I6 I7 ...). DV1UK-10 never got a gap to answer in (it waited
  30 s for a clear channel three times); after 2 minutes it disconnected.
  Earlier a resend queued just as DV1UK-10's acknowledgment arrived was aired
  anyway (20 s of BPSK), and DV1UK-10 answered every stale copy with REJ5.
  Now a resent I frame is skipped when it can't help: the other end has
  already acknowledged it, or it was already sent since the other end last
  had a chance to answer. The log says "[skipped N resent frames ...]". SARA FM
  still sends nothing of its own; every real retry is the client's.
- Those resends also no longer count as "they missed a frame" for Auto speed
  (that wrongly put the link on BPSK for 90 s, making every window longer).
- Via the DV1UK digipeater: DX1FLR's BPQ32 answered (REJ6) the moment
  DW1IRF's window ended -- exactly when DV1UK started relaying it -- so DX1FLR
  never heard the relayed first frame (I6, six times over). A station that has
  just heard or sent frames still to be relayed now leaves the channel to the
  digipeater: up to 4.5 s for the relay to start, and while it runs, until the
  relayed copies are heard.
- Tune TX: when a quieter level gets no reply, one check ping at a level that
  answered before shows whether the other station is still listening. If it
  isn't (field: DV1UK's PC fell 3 s behind its audio during DW1IRF's sweep),
  the misses are forgotten and those levels tested again after a pause, instead
  of "57%: overdriven? 28%: too quiet" and keeping a distorted 40%.

## v0.9.60 — AX.25 frame trace in the log

Field (DW1IRF -> DX1FLR-10 via DV1UK, 0.9.59): the 5 kB message reached the
CMS and failed again, this time "Received data stream not a correct format".
The digipeater bugs fixed in 0.9.57-0.9.59 didn't show; frames did get lost
at DV1UK (DW1IRF overdriven at 40%) and were resent by AX.25 -- but the logs
carried only sizes, so a duplicated or changed frame couldn't be traced.

- Every RX/TX line now ends with the AX.25 frames it carried, e.g.
  {I3/5 #a1f2 RR5}: I = info frame N(S)/N(R) and a checksum of its data,
  RR/REJ with N(R), SABM/UA/DISC/DM. The same frame can be followed through
  all three stations' logs, and a duplicate, a gap or a changed frame found.

## v0.9.59 — clock corrections confirmed only by a whole frame; watchdog

Field (DW1IRF -> DX1FLR-10 via DV1UK, 0.9.58): DV1UK, the digipeater, set a
-8841 ppm "sound-card clock" correction from a burst of weak junk candidates
(estimates scattered -6000..+12000 ppm). A frame HEADER then passed while the
frame itself failed FEC -- and a header was enough to confirm the trial
(v0.9.54). DV1UK went deaf to both ends; DX1FLR's polls were never relayed
and the session died.

- Only a whole decoded frame (CRC good) confirms a clock correction; a
  header passing just keeps the trial open until its frame is in.
- Watchdog: a confirmed correction is dropped again when frames with a real
  preamble (S/N 8 dB+) keep failing and nothing has decoded for 15 s -- a
  digipeater hears several stations, and a correction right for one is wrong
  for the others. Logged as "correction dropped".

## v0.9.58 — the digipeater re-packs relayed frames for its own speed

Field (DW1IRF -> DX1FLR-10 via DV1UK, 0.9.57): no checksum error this time,
but the session broke off. DW1IRF sent its message in 16QAM, two 282-byte
frames per transmission (562 bytes). DV1UK relayed each pair as ONE frame in
QPSK Wide, its speed toward DX1FLR -- over QPSK Wide's size limit, so DW1IRF
and DX1FLR both turned every one away ("implausible_length ... over max
frame size"), every window was resent and BPQ32 gave up.

- The digipeater now packs what it relays to fit the mode it transmits in
  (two QPSK Wide frames instead of one oversized one).

## v0.9.57 — digipeated sessions: only the relayed copy goes to BPQ32

Field (DW1IRF -> DX1FLR-10 via DV1UK, 0.9.55): the same 5 kB message failed
the CMS B2 checksum twice, though DV1UK relayed every data frame once, in
order. Both ends also heard each other DIRECTLY (S/N 8-11 dB) and SARA FM
passed those copies on before the digipeater had repeated them -- so every
frame reached Winlink Express / BPQ32 twice -- plus each station's own frames
echoed back by the relay. A client that acts on a duplicated REJ resends a
window twice; with AX.25's modulo-8 frame numbers a stale copy can then be
taken as new data, and the reassembled message fails its checksum.

- A frame still waiting for its digipeater is no longer passed to the KISS
  client (log: "heard direct -- not passed on, the relayed copy is"), except
  when this station is that digipeater and BPQ32 does the repeating
  (SARA FM's own digipeater off, BPQ32 DIGIFLAG=1).
- Our own frames repeated back by the digipeater are no longer passed on.

## v0.9.56 — connect requests always in a sturdy mode

Field (DW1IRF -> DX1FLR-10 via DV1UK, 0.9.55): a new connection warm-started
at 16QAM Wide (the last session had ended clean in 64QAM), the connect
request failed at the digipeater and Winlink Express waited 15 s for nothing.
A SABM is ~26 bytes: it now goes no faster than QPSK Wide; the session's
data still starts at the warm-start speed.

## v0.9.55 — Tune TX tests in a mode BOTH directions can carry

Field (DU3FS -> DW3TDJ, 0.9.54): DW3TDJ heard DU3FS at S/N 28 dB, so DU3FS's
Tune TX ran in QPSK Wide -- but the replies come back in the same mode, and
DU3FS hears DW3TDJ at only 16 dB with data carriers ~7 dB. The replies failed
to decode, Tune TX reported "57%: overdriven?, 28%: too quiet" and kept 40%,
where DW3TDJ's Ping window showed DU3FS's data carriers at 7 dB (red).

- **Test mode from both directions**: QPSK Wide only when they hear us at
  16 dB+ AND we hear them at 16 dB+ with data carriers 10 dB+; else BPSK.
- **A reply that arrives but won't decode isn't blamed on the TX level**:
  twice in a row and Tune TX starts again in BPSK.

## v0.9.54 — no more false sound-card clock corrections (DW3TDJ went deaf)

Field (DW3TDJ <-> DU3FS, 0.9.53): Auto-Tune from DW3TDJ failed twice -- yet
DU3FS heard every one of DW3TDJ's pings and answered. DW3TDJ's report showed
"sound-card clock correction +15917 ppm" (1.6%), applied at 15:56:46; from
then on it decoded nothing from DU3FS. Its 60 s recording reproduces it: a
steady tone (-24 dBFS, right after DW3TDJ's own reply) can be stretched to
repeat perfectly at the preamble lag, so the clock rescue took it for a
far-off preamble. Every later frame was then warped.

- **A corrected preamble must still look like a preamble**: energy on all
  16 carriers (the tone check), and S/N of at least 8 dB in its own right.
- **A correction is a trial** until a header or frame decodes with it; if
  none does within 3 s it is undone and the buffered audio un-warped. Only a
  confirmed correction is logged.
- Regression test from the real recording (testdata/
  field_dw3tdj_tone_false_clockfix.wav): 0.9.53 decodes 1 of 3 pings and
  sets 17760 ppm; now 3 of 3 and no correction.
- Tune TX no longer prints "nan" when the starting level got no reply.

## v0.9.53 — CPU breakdown for shared gateway PCs

Field (DX1BBC: Wyse 3040 running 4 VARA FM + BPQ32, Winlink session with
DW1IRF, 0.9.52): two messages sent (7 kB at 377 bps), but a "not keeping up"
warning every 10 s while 275-byte frames arrived -- decoder "busy" 33-68%,
0.5-0.9 s behind, no audio lost. Offline the same frames cost ~2% of one
core (23 ms per second of audio), so most of that "busy" time was the
decoder waiting for a core the other programs held.

- **The decoder's own CPU work is measured** (thread CPU time) next to its
  wall-clock busy time. When it mostly waited, the warning says so: other
  programs are using the cores; SARA FM settings won't fix it.
- **Station report CPU line adds a breakdown** for the last minute: decoder
  work, window, audio/bridge/other, and time the decoder waited for a core.
- **"Fell behind" warns from 1.0 s** (was 0.5 s). Audio is only lost past
  3 s, and that is always reported.

## v0.9.52 — Ping window shows the data carriers; Tune TX explains its pick

Field (DX1BBC / DW1IRF, 0.9.51): during Auto-Tune the Ping window showed
S/N +25 dB, all green, at the louder test levels; the final ping at the level
Tune TX chose showed +16..20 dB, partly light green -- it looked as if
Auto-Tune had settled on something worse. It hadn't: at 28% DX1BBC's data
carriers arrived at 6 dB (clipped), at 10% at 15 dB. Overall S/N comes from
the repeated preamble, and a clipped signal still repeats perfectly, so that
number keeps rising with TX level; only the carriers show the distortion.

- **Third box on each side: "Data"** -- the data-carrier S/N (what Tune TX
  measures and the speed follows): green 16+ dB (fastest modes), light
  green 11+ (16QAM), yellow 7+ (QPSK), red below. Every ping now asks the
  other station for it (v0.9.14+ answer).
- **After Tune TX the window keeps its verdict** instead of just "Link looks
  good": the level chosen, its carriers, and -- when S/N reads lower than at
  a louder level -- why that's expected.

## v0.9.51 — an overdriven REPLY is recognised; "answer quieter" ping

Field (DX1BBC fresh install at TX 40%, DW1IRF Auto-Tune, 0.9.50): DX1BBC
heard and answered every ping, but each pong reached DW1IRF strong (preamble
25 dB) and clipped. The new overdrive NOTE fired, yet Auto-Tune took it as
"no reply" and changed DW1IRF's OWN TX level (40 -> 20 -> 55 -> 70 -> 85%),
which couldn't help. With DX1BBC set to 20% by hand, both Auto-Tunes worked.

- **A strong clipped frame while a ping waits for its pong = "they answered"**:
  logged as such; Auto-Tune keeps your TX level instead of changing it.
- **The next ping asks for a quieter reply** (new flag bit in the ping's
  flag byte): a 0.9.51 responder halves its reply level for that station
  (40% -> 20% -> 10%, never below 5%), says so in its log, and keeps it until
  it runs Auto-Tune / Tune TX to that station. Old stations don't recognise
  the flagged ping (they were unreadable anyway); only sent after a clipped
  reply.
- Auto-Tune's result says plainly when the OTHER station must lower its level.

DX1BBC's 60 s recording also showed the one pong it missed after tuning:
80 ms of signal, then the radio's audio muted for 0.45 s (below the hiss) --
the same mute appears with nobody transmitting. A radio setting (dual watch /
scan / battery save / squelch at threshold), not the decoder.

## v0.9.50 — Auto-Tune also tries LESS audio; overdrive warning; VOX margin

Field (DX1BBC <-> DW1IRF, 0.9.49): DX1BBC's pings at TX 40/55/70/85% all
reached DW1IRF strong (preamble S/N 20-25 dB, -5.5 dBFS) but garbled -- FEC
and header failures even in BPSK. At 20% they arrived 1.5 dB quieter and
decoded at once: the radio was clipping. Auto-Tune only ever went UP. And
DW1IRF's Tune TX kept 14% right above a level (10%) where its VOX never
keyed; DX1BBC then missed most of DW1IRF's pongs.

- **Auto-Tune first retry goes DOWN**: no reply at the start level -> about
  half of it (40% -> 20%, minimum 15%), then 55 / 70 / 85% as before.
- **Overdrive warning at the receiving station**: frames with a strong, clean
  preamble (18 dB+) that still fail to decode are counted; two in a minute
  log a NOTE asking the sender to lower its TX level. The station report's
  Receiver line shows the count ("strong but undecodable").
- **Tune TX keeps a VOX margin**: the quietest level that answered, right
  above one that got no reply at all, is swapped for the next level up when
  that one is about as clean (within 1 dB).

## v0.9.49 — about half the receive CPU; lighter meters; volunteer guide

Field (DW3TDJ, 0.9.48): the decoder used 22-24% of a core with nothing on
the air, and 81% at peak in sessions, on a slow PC -- and a Wyse 3040 gateway
box will be slower still.

- **Idle gate in the receiver**: every audio block used to re-filter and
  re-search the whole search buffer (~8x the audio received). Now a cheap
  streaming check -- a causal band filter over the new audio only, and the
  detector's own repetition test held for 192 samples -- decides whether a
  preamble could be arriving; only then (and for 1.5 s after, or while a
  frame is under way) does the full search run.
  Measured on the real DW3TDJ / DU3FS recordings: **16.9 -> 7.4 ms and
  17.5 -> 9.7 ms of CPU per second of audio**; idle flat hiss skips >80% of
  full searches. Decoding is unchanged: FM channel simulator (every path,
  CNR 6-20 dB), sound-card clock offsets, and the field recording excerpt
  all decode exactly as in 0.9.48.
- **Meters redraw only when the needle moves**, at 15 frames a second (was
  all three, 25 times a second, even at rest).
- **Quick Start section 17**: what test volunteers should do and send back
  (report from both ends, a 60 s recording when two stations can't decode
  each other, the VARA FM ping for comparison).

## v0.9.48 — fix: frames lost at the header after a clean preamble (DW3TDJ <-> DU3FS)

Field (0.9.47 recordings, Log > Record received audio): DU3FS's BPSK pings
reached DW3TDJ at preamble S/N 26 dB, -12 dBFS, clock offset only ~-500 ppm
-- yet 4 of 5 were lost with low_header_confidence. Decoding the recordings
offline showed why: the preamble detector fired **260-370 samples early**,
more than the 128-sample guard interval, so the header was read straddling
two symbols. The audio just before the preamble (the lead-in fill tone, with
a 2nd harmonic at 2060 Hz from the radio path) already correlated at the
repetition lag, so the detector's plateau began early. VARA FM's sync isn't
fooled by that; ours was.

- **Fine timing against the known preamble**: after the detector's guess,
  the receiver cross-correlates with the (receive-filtered) preamble
  waveform within +-480 samples and starts there. The real recordings: 5 of
  5 pings decode at DW3TDJ (was 1), and DW3TDJ's ping in DU3FS's recording
  decodes (was 0). A 5.8 s excerpt of the real recording is now part of the
  test suite (testdata/).
- The detector also ignores negative correlation at the repetition lag
  (a real preamble repeats in sign; a tone half a cycle out does not) --
  fewer noise candidates too.
- Header energy check limit 1.8 -> 2.05: with exact timing one clean header
  measured 1.81; a real overlap measures ~2.4.
- **BPQ32 beacons no longer count as a Winlink session**: DW3TDJ's APRS
  beacon every 5 minutes made SARA FM refuse to answer pings for 20 s
  ("Winlink session in progress -- not answering"). UI frames are now
  ignored for that.
- FM channel simulator: decoding at CNR 6 dB slightly better; no change
  elsewhere; CPU unchanged.

## v0.9.47 — tools to find out why two stations can't decode each other

Field (0.9.46, DW3TDJ <-> DU3FS): still no pings either way, VARA FM fine.
No clock correction fired, so the sound-card clock is NOT the problem. In
both directions the frames are heard and their preambles recognised, then
lost at the header/FEC stage: DU3FS turned away 10 (low_header_confidence 7,
fec_uncorrectable 3) for DW3TDJ's 12 pings; DW3TDJ turned away 15 (9 + 4 +
implausible_length 2) for DU3FS's 12. DU3FS works fine with DW1IRF; DW3TDJ
has never decoded a SARA FM frame from anyone. Something in DW3TDJ's audio
chain changes the signal between the preamble and the header -- the kind of
thing audio "enhancements" (noise suppression, AGC, loudness) or a radio's
DSP noise reduction / mic compressor do.

- **Log > Record received audio (60 s)** saves exactly what the sound card
  delivers (before RX gain) as a WAV in the log folder
  (SARA-FM_rx_<CALL>_<time>.wav), to send to the sysop for analysis.
- **A frame lost after its preamble was recognised is always logged** (no
  Verbose RX needed) with the preamble's S/N, level, measured clock offset
  and carrier spread -- rare, and exactly the clue for links like this one.

## v0.9.46 — copes with sound cards whose clock is off (DW3TDJ <-> DU3FS)

Field (0.9.45): VARA FM pinged DW3TDJ <-> DU3FS-10 every time; SARA FM never.
DU3FS's receiver turned away 19 candidates at the header/FEC stage
(low_header_confidence 15, fec_uncorrectable 4) -- one for each of DW3TDJ's
19 pings, every one with a clean preamble -- while DU3FS and DW1IRF ran a
Winlink session fine. That is the signature of a sound card running
off-frequency: cheap "USB PnP Sound Device" dongles can be 0.2-1% fast or
slow, and the receiver only tolerated about +-0.15% (1500 ppm).

- **The preamble now measures the offset** (its four identical repetitions
  slide in phase in proportion to each carrier's frequency): within +-300 ppm
  at S/N 13 dB.
- **The receiver resamples the incoming audio to undo it** -- the frame
  being received and everything after -- and re-measures on every station's
  preamble, so stations with different clocks can take turns (tested:
  +0.5%, 0, +0.5%, +0.3%, 0, -0.2%, +0.5% in a row, every frame decoded).
  A correction is only applied when it makes the preamble repeat more
  cleanly, so a noisy estimate never spoils a good frame and open-squelch
  hiss never sets one (tested).
- Every mode now decodes with the other station's sound card up to 0.5% fast
  or 0.6% slow (often 0.8%); before: 0.15%. Frames with normal clocks decode
  exactly as before (FM channel simulator, all paths and S/N levels).
- The log says when it corrects ("Sound-card clock: the station on the air
  runs 0.49% slow relative to this PC (+4935 ppm) -- corrected"), and the
  report's Receiver line shows the correction in use.
- Both ends benefit: each station corrects what it receives, so DW3TDJ's
  own off-clock card is undone at DU3FS, and DU3FS's normal signal is undone
  at DW3TDJ. Both need 0.9.46.

## v0.9.45 — fix: decoder at 100% on an idle channel

Field (DW1IRF, 0.9.44, only Auto-Tune to DV1UK): "decoder working 99-100%
of the time" with nothing else on the air; the report showed 8413
low_preamble_snr and 2073 not_preamble candidates in ~4 minutes (~44 a
second) at RX gain +20 dB. Pongs then arrived after their timeout, and 3 s
of audio at a time was skipped.

- Cause: each turned-away candidate skipped only half a symbol and waited for
  the next audio block, which adds two -- so with a candidate in every block
  the receive buffer grew to its 9 s ceiling and every block re-filtered all
  of it. Before v0.9.41 the level gate trimmed those quiet candidates away;
  loud open-squelch hiss (high noise floor) could always cause it, which may
  also be what the "not keeping up" warnings on gateways like DX1PAR and
  DX1FLR were.
- Fix: every candidate in the buffer is now handled in the same pass. Same
  stress (56 candidates a second): buffer max 0.09 s instead of 5.6 s,
  decoder 4-5% of a core instead of 25% on the test machine (100% on
  DW1IRF's). Frames decode exactly as before (FM channel simulator, all
  paths and S/N levels).

## v0.9.44 — CPU meter

- A **CPU** meter in the status bar (both views), like VARA FM's CPU gauge:
  the bar and first number are SARA FM's share of this whole PC (all cores,
  comparable with VARA's figure); "decoder N%" is how busy the receiver is
  on its one core -- near 100% it is about to fall behind. Green below 50%,
  amber 50-80%, red above (either number). Updated every second, smoothed.
- Station reports get a **CPU:** line (now, peak in the last minute, decoder
  load, number of cores) -- for comparing gateways such as the Wyse 3040
  boxes with VARA FM running alongside.

## v0.9.43 — the "not keeping up" warning says where the time went

Field (DW1IRF, 0.9.42, during a Winlink session): "decoding fell 1.0s
behind", then "3.0s skipped". Checked first: the receiver changes in
0.9.41/0.9.42 cost nothing extra -- on white noise, open-squelch FM hiss with
hum and whine, at -54 to -9 dBFS, the receiver uses the same CPU as 0.9.40
(~2% of one core here), and decoding a frame takes 0.02-0.10x its air time
in every mode. So the warning now carries its own diagnosis:

- **decoder working N% of the time** -- how long the decoder itself worked
  in those 10 s;
- **SARA FM used N% of one CPU core** -- the whole program;
- **longest window freeze** -- if SARA FM's own window stalled;
- and what that points to: the decoder too slow for this PC (use QPSK Wide
  or untick 64QAM), *other programs or Windows* holding the PC up (SARA FM
  itself wasn't busy -- antivirus, updates, browser, VARA/BPQ32, power
  plan), or the window freezing (send the report).

## v0.9.42 — field DW3TDJ/DU3FS: diagnostics, and a better tone check

- **Reports always show what the receiver did**: a "Receiver:" line with
  frames decoded this run and every candidate it turned away, by reason --
  even without Verbose RX (the DW3TDJ/DU3FS reports had nothing to go on).
- **Two RA-Boards connected**: at Start the log names the RA-Board used for
  PTT and the sound card used for audio, and says to check they are the same
  interface (DU3FS has two; audio to one radio while keying the other means
  nothing goes out).
- **Tone check (v0.9.41) made sturdier**: a candidate is a whistle if its
  carriers peak either before or after removing the radio's frequency tilt.
  Checked: every exactly-repeating tone 450-1875 Hz is caught, and no real
  preamble through any radio audio path (incl. 12 dB de-emphasis tilt) at
  CNR 6-12 dB is mistaken for one. In the FM channel simulator, frames
  decode exactly as with no tone check at all.

## v0.9.41 — hears weak and quiet stations like VARA FM; a sturdier Auto-Tune

Field (DW3TDJ -> DU3FS-10): VARA FM pinged fine (DU3FS heard DW3TDJ at
S/N +9.9 dB, VU -35 dB) while SARA FM's Auto-Tune got "no reply"; starting
from RX gain 0 and TX 40% often took a lot of knob-turning.

- **The receiver no longer drops quiet frames.** Before, a frame arriving
  below about -30 dBFS was thrown away by a fixed level gate unless the
  noise floor was quiet -- so at RX gain 0 a weak or quiet station was never
  heard and Auto-Tune had nothing to tune from (it only worked once the gain
  was already roughly right). Now only near-silence (-60 dBFS) is gated;
  noise and whistles are told apart from a real preamble by its shape: it
  must repeat (preamble S/N, as before) and spread over all 16 carriers (a
  whistle or hum sits in one or two). Measured: a BPSK ping at S/N 9 dB in
  hiss now decodes at -30, -40 and -46 dBFS input (before: lost below
  ~-30). RX gain is back to what it should be -- setting the meter, not
  deciding whether anything is heard.
- **PING and Auto-Tune ping in BPSK** when Modulation is Auto (the most
  sensitive mode, ~4 dB better than QPSK; the pong comes back in BPSK).
- **Auto-Tune raises the TX level when nobody answers**: 40% -> 55% -> 70%
  -> 85% between tries (on FM more audio = more deviation = better S/N at
  the far end), keeps the level that got a reply (Tune TX then fine-tunes
  it), and puts it back if nobody ever answers.
- **Tune TX on a weak link pings in BPSK** (QPSK Wide only when the far end
  last reported S/N 16 dB or better), so it gets replies at all.
- **Advice**: when the far end hears you quietly *and* noisily, it now says
  to raise your TX level (deviation), not "it's the radio path".

## v0.9.40 — more file data per burst on fast links

Field (DW1IRF -> DV1UK, 0.9.39, 64QAM Wide C, no errors): each burst was 4
frames of one 300-byte piece, ~0.55 s each, then ~2.7 s of turnaround for
the receipt -- about 1.6 kbps of file data, most of it preambles and waiting.

- At **16QAM / 64QAM Wide** chat now packs **two file pieces per frame** and
  sends **6 frames per burst** (3.4 kB per burst instead of 1.2 kB): about
  1.6x the file speed on a good link (~2.6 kbps of file data at 64QAM Wide C
  by the same timing). Slower modes keep one piece per frame, 4 per burst.
- Frame sizes stay inside what the receiver decodes with 200 ppm sound-card
  clock drift, measured per mode: up to 320 bytes in BPSK/QPSK/QPSK Wide,
  680 bytes in the 16QAM/64QAM modes (both checked in the test suite).
  File pieces are 280 bytes (were 300) so one piece fits every mode with
  margin.
- Multi-piece frames only go to a station whose receipts say it takes them
  (0.9.40+); a 0.9.38/0.9.39 station keeps getting one piece per frame, so
  mixed versions still work.

## v0.9.39 — chat speed: the gauge moves, and Auto climbs with the S/N

- **Speed gauge and speed line count chat traffic.** Message text and file
  pieces, sent and received, now feed the same meters as Winlink data (the
  gauge showed "idle" through a whole file transfer).
- **Auto speed for chat works like a Winlink session.** Every chat receipt
  now carries a link report (how the receiving station hears the sender:
  S/N, carrier S/N, FEC, frames lost), so a file or a long chat steps up
  QPSK -> QPSK Wide -> 16QAM ... as far as the S/N allows (64QAM only if
  "Auto may use 64QAM" is ticked), and steps down on losses or a window that
  had to be resent. 0.9.38 kept chat at QPSK Wide or below and never
  learned from chat itself. The speed gauge shows the mode to the station
  you are chatting with.
- File pieces are 300 bytes (were 200): fewer frames per file. Every chat
  frame size is checked to decode in every mode.
- A 0.9.38 station still chats with 0.9.39 (it ignores the report in the
  receipt), but speed only climbs when both ends run 0.9.39.

## v0.9.38 — SARA Chat

Keyboard-to-keyboard chat between SARA FM stations, built into SARA FM:
**Chat** button or **Chat > Open SARA Chat**. It shares the radio with a
running BPQ32 gateway -- chat frames are SARA FM's own and never reach
BPQ32 -- so a sysop can chat without stopping Winlink. Every station needs
v0.9.38 or newer to chat.

- **One-to-one chats with delivery receipts.** Each message shows
  *sending*, *sent* (on the air), *delivered* (the other station confirmed
  it) or *held/failed*. Up to 4 messages go per burst; the other station
  confirms them in one short reply; anything unconfirmed is resent
  automatically (the 3rd and 4th tries one speed step slower). Messages
  always show in the order they were written.
- **Leave a message.** A message that isn't confirmed after 4 tries is
  *held* and delivered automatically the next time that station is heard --
  on chat, a ping or Winlink traffic -- for up to 7 days, even across
  restarts. The other side sees it was held and when it was written.
- **NET room** for everyone on frequency, with **CQ** and **Check in**
  buttons; check-ins are marked in the Heard list. NET messages go once,
  without receipts.
- **Small files and pictures** (up to 32 kB) inside a chat, in 200-byte
  pieces with receipts and a CRC check; received files go to the
  `chat_files` folder with Open / Folder links in the chat.
- **Heard list**: every station heard (chat, ping or Winlink), when, S/N
  and how; the chat header shows how you hear them and how they hear you.
- **Courtesy to Winlink**: while a Winlink session is moving data, chat
  waits (receipts still go, they are short). Speed: QPSK to a station Auto
  knows nothing about, otherwise its Auto speed up to QPSK Wide.
- Unread messages show on the Chat button and in the status bar, with a
  beep. History and held messages are kept in `sara_chat.json` next to the
  program.
- Not yet: chat through a digipeater (SARA Chat goes direct).

## v0.9.37 — digipeater

A station in the middle can now relay for two stations that can't hear
each other (DW1IRF -> "DX1FLR-10 via DX1CW"). Every station on the path
needs v0.9.37 or newer. See QUICKSTART section 15.

- **SARA FM's own digipeater**: Settings > SARA Setup > Digipeater, "Repeat
  frames sent via this station". Repeats each frame whose next digipeater
  is My callsign (SSID included) or the alias, sets the AX.25 H bit
  (an alias is replaced by the real callsign), and sends the whole window
  as one burst. A relay site needs only a radio, an interface and SARA FM
  -- no BPQ32. Frames decoded twice within 3 s are repeated once; a real
  AX.25 retry (after FRACK) is repeated again, as it must be.
- **BPQ32 digipeating** (DIGIFLAG=1 on the SARA FM port) works too; SARA FM
  warns if both are on.
- **Speed, link reports and saved levels follow the radio hop**: the next
  digipeater not yet repeated (else the destination) when sending, the
  last one that repeated (else the source) when receiving. A burst for
  several hops goes at the slowest of their speeds. A link report counts
  only at the station it was meant for, so a station overhearing a relay
  no longer takes another hop's report as its own (direct links behave
  exactly as before).
- **Logs show the path**: "DW1IRF>DX1FLR-10 via DX1CW*" (* = repeated),
  "DIGI: repeating 2 frames ...", "TX (DIGI->air)", and "[our frame,
  repeated by DX1CW]" when a station hears its own frame relayed. Relayed
  frames stay out of the session retry accounting.
- The station report has a Digipeater line; the status bar says "DIGI on".

## v0.9.36 — SARA FM name, gauge title, simpler SARA Setup

- **Gauge title**: a new box next to *My callsign* in Settings > SARA Setup.
  Its text appears as a title line above the gauges in **View > Gauges
  only** (e.g. "DX1FLR-10  PAREDN Winlink Gateway - Navotas"); leave it
  empty for no title. Saved with the settings.
- **Ping/Pong Test mode removed.** SARA FM always runs as the BPQ32/KISS
  bridge; PING, Auto-Tune, Tune TX and TUNE 10s work while bridged and idle,
  as before. An old settings file with mode "pingpong" now starts bridged.
- **No Start/Stop Bridge button**: the bridge starts when you press Start and
  stops with Stop. KISS port and network-access changes take effect at the
  next Start.
- **Auto-ping** (count / interval) removed.
- **Name**: the version is now `sara-fm-0.9.36`; the Windows build is
  `SARA-FM.exe` in a `SARA-FM` folder, zipped as `sara-fm-<version>-win64.zip`;
  reports and log bundles are `SARA-FM_report_...txt` / `SARA-FM_logs_...zip`;
  the self-test is `SARA-FM Selftest.bat`. The settings file keeps its name
  (`sara_pp_settings.json`), so copying it from the old folder into the new
  one keeps your setup.
- On-air behaviour is unchanged from 0.9.35 / 0.9.34.

## v0.9.35 — saved audio levels per station

Auto-Tune to one gateway, then another, used to overwrite the first
station's levels.

- Each station in the Target list now keeps its own **RX gain and TX
  level**. They are saved automatically when Auto-Tune or Tune TX finishes
  for that Target, and by the **Save** button (which also saves the
  location).
- **Picking a station** from the list puts its saved levels back; the line
  under the Target row shows them ("Saved levels: RX +24 dB, TX 25%
  (2026-10-28)"), and the list shows them next to each station.
- A **new Winlink connection** (AX.25 connect either way) with a station
  that has saved levels uses them too -- DX1CW-10 matches an entry saved as
  DX1CW. The log says when levels change and from what.
- Stored in sara_pp_settings.json with the target list; older files load
  as before (no saved levels yet).

## v0.9.34 — "Gauges only" view next to VARA FM; radio-style meters

- **View menu**: *Full* (target row, gauges, buttons, Auto-Tune bar, log)
  or *Gauges only* -- a strip about the size of VARA FM's window (three
  meters, TX / RX / DCD / TCP lamps, status bar) to sit above or below it.
  The choice is saved.
- **Meters drawn like a transceiver's**: dark bezel, amber back-lit face,
  brown printed scale turning red at the top end, thin black needle with a
  shadow, the dark needle housing, and a caption strip underneath:
  - **RX LEVEL** (dBFS): green target band at -12 +/-4, red over -3; needle
    = live input, green marker + caption = the last decoded frame.
  - **S/N** (0-40 dB): red under 8, amber 8-12, green above; needle and
    caption = the last frame's S/N.
  - **SPEED** (0-3 kbps): current link rate and the mode Auto is using.
- **Lamps**: TX, RX, DCD and **TCP** (lit while BPQ32 / Winlink Express is
  connected over KISS), with *TX anyway*.
- **Status bar** (both views): status, DCD reason, Auto-Tune progress,
  mode.
- Fixed (v0.9.33): the bridge status line was missing from the new layout,
  so starting or stopping the bridge raised an error.

## v0.9.33 — new main window, VARA FM style

Volunteers will run SARA FM next to VARA FM and pick whichever suits the
day, so the window now works like VARA's:

- **Menu bar**: Settings > SARA Setup (callsign, mode, KISS port and bridge,
  network KISS, DCD mode, Auto 64QAM, auto-ping) and Settings > Devices
  (sound card, PTT / RA-Board / GPIO, TX delay, RX gain, TX level, live RX
  bar). Log > Save Report, Save Logs, Open log folder, Verbose RX
  diagnostics, Clear window. Help > Quick Start, README / version history,
  About. Settings are saved when a dialog is closed.
- **Main window**: Target (with location) and Modulation on top; a
  radio-style **analog RX level meter** (gauges.py: cream face, dBFS scale,
  green target zone at -12 +/-4 dBFS, red clip zone, needle with meter
  ballistics, green marker and readout for the last decoded frame); **TX /
  RX / DCD lamps**; the last frame's **S/N in large type** (green / amber /
  red); speed line; Start / PING / Auto-Tune / Tune TX / TUNE 10s; the
  Auto-Tune status bar; the log.
- Fixed: on a fresh install (v0.9.32) Modulation showed Auto but the
  program kept using BPSK until the box was changed.
- No change on the air; every control keeps its name and setting.

## v0.9.32 — volunteer-ready defaults and a clearer window

Ahead of handing SARA FM to more volunteers:

- **Defaults** (a new install with no settings file): Mode **BPQ32/KISS
  Bridge**, Modulation **Auto**, DCD **SARA-PP signals only**, TX level
  **40%** (was 80), TX delay **600 ms** (was 300 -- most PAREDN stations use
  the DX1PRD USB interface), RX gain 0 dB. Existing settings files are
  unchanged.
- **Target list**: the Target box is now a drop-down of stations
  (callsign + location), kept in the settings file. Type a callsign, enter
  its location, press *Save to list*; *Remove* deletes it. Starts with
  DV1UK, DX1CW, DX1FLR, DX1PAR.
- **Auto-Tune status banner** in large type under the buttons: yellow while
  running ("AUTO-TUNE RUNNING (1 of 2: RX gain) -- ping 2 to DV1UK..." then
  "(2 of 2: TX level) -- TX 25% (level 2 of 6)..."), green when done with
  the result (RX gain, TX level, data-carrier S/N), red if it stopped (no
  reply). Tune TX on its own uses the same banner.
- **Audio device lists** show each sound card once. Windows lists every
  card under 3-4 audio systems with different numbers; after plugging in a
  second USB interface DW1IRF ended up on the wrong copy and decoded
  nothing until a reboot. A device saved from another list is still found.

## v0.9.31 — no 3-second hold after every transmission at DX1FLR

Field (DW1IRF -> DX1FLR on 145.220): with v0.9.30, DX1FLR held DCD
~3.3 s after each DW1IRF transmission ("waited 3.2-3.8s ... channel read
-6 dBFS"); the same path on v0.9.26 ran visibly quicker. DW1IRF's frames
reach DX1FLR at -12 dBFS; when DW1IRF's carrier drops, DX1FLR's radio puts
out noise at -6 dBFS -- inside v0.9.28's 6 dB level window, so the carrier
hold (v0.9.27) read it as DW1IRF still on the air.

- The carrier hold now needs a level within 3 dB of the station's frames
  (was 6). Simulated: noise 6 dB over the frames now holds DCD 0.0 s
  (3.1 s before, matching the field); a burst with an unrecognised header
  still reads busy throughout.

## v0.9.30 — retry-time accounting per session

Field (v0.9.28 on 145.220, DX1CW / DX1FLR): the biggest cost in slow
sessions was silence -- 12-15 s with no traffic, each ended by BPQ32 or
Winlink Express giving up on a lost frame (FRACK) and polling or resending.
It took lining up log timestamps by hand to see it.

- New module session_stats.py. A **recovery** is a stretch of 4 s or more
  with no SARA-PP traffic either way, ended by a resent I-frame (same N(S),
  same contents, by either station), a poll (RR/RNR/REJ with the P/F bit),
  or a repeated connect request. A pause ended by ordinary new traffic is
  not counted.
- Each recovery logs one line, e.g.
  `Retry: 13.8s with no traffic, then DX1CW-10 polled (AX.25 retry timer)`.
- The session summary line (also in Save Report) ends with
  `retries: 8 (frames resent: 3 by us, 5 by them), 1:52 waiting on retry
  timers (24% of the session)`, or `retries: none`.
- No change on the air.

## v0.9.29 — receiver uses less than half the CPU on an idle channel

Field (v0.9.28, DW1IRF -> DX1FLR gateway): a bare test connection took over
a minute or failed. DX1FLR's PC fell 3-9 s behind on an IDLE channel and
skipped audio to catch up, over and over ("decoding fell over 3s behind,
so 3.0s of received audio was skipped"), and the first connect request of
every attempt never reached its decoder -- each connect waited out BPQ32's
retry timer at least once.

- Profiling the receiver on open-squelch FM hiss: over half of its time
  went to the band-pass filter re-deriving its fixed starting conditions
  and padding through a generic helper, on every audio block. They are now
  computed once. Same filter output (to about 1e-8), 2.2-2.7% of one core
  instead of 5.8% on an idle channel -- less than half the load.

## v0.9.28 — no 3-second pause after every reply on noisy radios

Field (v0.9.27, DX1CW <- DW1IRF): DW1IRF's frames reached DX1CW at
-30 dBFS, but when DW1IRF's carrier dropped DX1CW's radio put out noise at
-20/-21 dBFS (squelch tail / hiss). v0.9.27's carrier hold read that noise
as DW1IRF still on the air, so on nearly every turn DX1CW waited ~3.2 s
before answering ("waited 3.1s for clear channel ... channel read -21 dBFS").

- The carrier hold now counts only a level within 6 dB of the frames that
  station's signal actually arrived at. Noise 9 dB louder (DX1CW) no longer
  holds DCD; a missed header in the middle of a burst (DW1IRF) still does.

## v0.9.27 — DCD holds through a whole burst, even past a missed header

Field (v0.9.26, DX1CW -> DW1IRF, Auto in BPSK): DX1CW sent 6-frame bursts
(2.8 s frames, ~19 s keyed). Every second or third frame, DW1IRF's DCD
("SARA-PP signals only") read clear right after decoding a frame -- the next
frame's header hadn't been recognised -- and DW1IRF keyed its
acknowledgment into the middle of DX1CW's burst, destroying the next frame
each time. Simulated: one unrecognised header left that DCD deaf for a whole
BPSK frame (2.7 s).

- After direct proof of a SARA-PP station (its fill tone, a frame arriving
  or decoded), its carrier now also holds DCD for 3.5 s. The carrier is read
  by the level detector, whose threshold sits between the noise floor and
  the weakest station heard, so open-squelch hiss still doesn't trip it.
- Only proof heard since this station's own last transmission counts, so
  its own squelch tail never stretches the wait.
- DCD still clears within about half a second of the burst's end.

## v0.9.26 — steadier on slow or busy gateway PCs

Field (v0.9.24, gateway DX1PAR): "sound card input lost" 5-13 times every
10 s through whole sessions, and right after Start the decoder fell 19 s
behind. Lost input damages frames; a late decoder delivers frames after
the sender has timed out and leaves DCD deaf to the other station (it then
keys up over this one). The modem itself needs about 4% of one core of a
modern PC on any input, so this is about timing on a busy or low-power PC.

- The sound card hands over audio in 85 ms pieces instead of 21 ms (four
  times the slack before input is lost). Inside, they are cut back into the
  same small blocks, so level, DCD and decoding behave exactly as before.
- A decoder more than 3 s behind now skips ahead to live audio instead of
  falling further behind; the warning says how much was skipped.
- SARA-PP asks Windows for "above normal" priority, so the audio gets its
  turn first on a busy PC.
- The on-screen log keeps the last 2,000-3,000 lines (hours of verbose
  diagnostics slowed the whole program). The daily log file keeps
  everything.

## v0.9.25 — DCD ignores a steady tone near 1 kHz

Field (v0.9.24, DV1UK, DCD mode "SARA-PP signals only"): the lamp sat on
"busy -- SARA-PP signal" with nobody transmitting. The fill-tone detector
read +23..+28 dB at a quiet input (-48 dBFS): something at that station's
input whistles near 1 kHz (hum, sound-card whine or a carrier), close
enough to SARA-PP's 1031 Hz fill tone to pass for it. Every transmission
waited the full 30 s and an 8-minute session moved 135 bytes. (Not caused
by v0.9.24 -- the DCD code was unchanged.)

- A real fill tone never runs unbroken for more than about a second (or
  10 s for a far-end TUNE). A tone heard unbroken for 12 s is now marked
  foreign: the lamp shows "clear (ignoring a steady ~1 kHz tone)" and the
  log says so once. For the next 30 minutes the tone counts as busy only
  for its first 2.5 s each time it reappears, or when it comes back 10 dB
  louder (a station's fill tone on top). Real SARA-PP bursts and a far-end
  TUNE still read busy throughout.
- A burst left waiting for DCD when Stop was pressed is now dropped
  instead of failing with "OSError: Stream closed".

## v0.9.24 — stronger error correction: coded 16QAM / 64QAM Wide

Field (v0.9.23 vs VARA FM over the same DV1UK <-> DW1IRF path): SARA-PP
finished every Winlink session sooner (VARA spent 40-60 s connecting and
stepping through speeds), but on the weak direction (DV1UK -> DW1IRF, data
carriers 13-18 dB) VARA moved the data itself ~1.8x faster -- SARA-PP had to
stay in QPSK Wide, because plain 16QAM needs carriers of ~16 dB+.

- **Coded modes**: "16QAM Wide C" and "64QAM Wide C" add an inner
  convolutional code (K=7, the 802.11a/DVB code, rate 2/3 and 3/4) decoded
  with SOFT decisions (the receiver's confidence in every bit, weighted by
  each carrier's measured noise), interleaved across carriers and symbols,
  inside the existing Reed-Solomon code (fec_cc.py). Simulated FM channel,
  250-byte frames:
    QPSK Wide      ~2.1 kbps on air, from data carriers ~9-10 dB
    16QAM Wide C   ~2.6 kbps, from ~12-13 dB   (plain 16QAM: from ~16 dB)
    16QAM Wide     ~3.8 kbps, from ~16 dB
    64QAM Wide C   ~4.2 kbps, from ~18 dB      (plain 64QAM: from ~21 dB)
    64QAM Wide     ~5.2 kbps, from ~21-22 dB
- Auto's ladder: BPSK, QPSK, QPSK Wide, 16QAM Wide C, 16QAM Wide, 64QAM Wide
  C, 64QAM Wide. Coded modes are used only with stations that announce them
  (v0.9.24+); with older ones Auto skips those rungs. Step-ups need the
  receiver's reported data-carrier S/N: 15 dB for 16QAM C, 19 for 16QAM, 20
  for 64QAM C, 24 for 64QAM.
- On the air the coded modes use header code 2 with training-symbol patterns
  of their own. The pattern is now matched against the preamble's channel
  estimate. Offline this also fixed two old faults: the FIRST frame of a
  16QAM burst was often lost, and plain 64QAM bursts failed altogether (its
  pattern was misread when the FFT window sat near the cyclic-prefix edge).
  Wide FFT windows that sit too near that edge are now moved.
- Auto-Tune now runs Tune TX right after setting the RX gain: one button for
  both. The Tune TX button stays for TX-only.
- Speed memory files from earlier versions are ignored (the ladder changed).

## v0.9.23 — 64QAM Wide try on 21 dB carriers (experiment)

Field (v0.9.22, DW1IRF -> DV1UK): sends ran in 16QAM Wide with DV1UK
reporting data carriers of 18-23 dB (mostly 20-22, FEC 0-17%) -- never the
24 dB Auto needed for 64QAM -- while DV1UK had decoded manual 64QAM pings at
~22 dB with only 5 bytes of FEC.

- Auto now tries 64QAM Wide from carriers of 21 dB (was 24), still only on a
  spotless 16QAM run (FEC 4% or less, no losses, S/N 29+) or at S/N 33.
- In 64QAM it steps back down when carriers fall under 18 dB (was 21), and
  on any loss or heavy FEC as before; a failed try puts 64QAM on hold
  (90 s, doubling).
- Untick "Auto: 64QAM (test)" to go back to 16QAM as the top.

## v0.9.22 — 16QAM Wide only on clean carriers

Field (v0.9.21): receives much better (15.3 kB in 1:38). The one long pause
(send 3: 33 s with both DCD lamps green) was a lost acknowledgment that
DV1UK had sent in 16QAM Wide: its QPSK Wide reports were spotless at
21-23 dB, so Auto kept trying 16QAM -- but DW1IRF measures DV1UK's data
carriers at only 13-18 dB, where 16QAM needed 42-92% of its FEC.

- Stepping up from QPSK Wide to 16QAM Wide now also needs the receiver to
  report data carriers of 19 dB or better (v0.9.18+ reports carry them;
  older stations step up on S/N alone, as before). DW1IRF -> DV1UK (carriers
  21-22 dB) still runs 16QAM.

## v0.9.21 — Save Report: one small file per test run

Verbose test logs were 2-12 MB, nearly all "[rejected: low_energy ...]" and
"[rejected: low_preamble_snr ...]" lines (the receiver noting quiet audio or
noise 10-20 times a second).

- New **Save Report** button (next to Save Logs): writes
  SARA-PP_report_<CALL>_<date>_<time>.txt on the Desktop -- station info,
  the Winlink/BPQ32 session summaries, a count of every kind of rejected
  candidate, then this run's whole log WITHOUT those two noisy kinds.
  Field logs: 9.3 MB -> 61 kB, 2.6 MB -> 85 kB. Every other line (frames,
  speed changes, link reports, lost frames, carrier checks, warnings) is kept.
- Save Logs (the full zip) is unchanged, for when something goes wrong.

## v0.9.20 — interference doesn't drag Auto down to BPSK

Field (v0.9.19, DV1UK-10 -> DW1IRF): receives 3 and 4 took 3:19-3:24
instead of ~1:10. Every lost frame was a long one (306-317 bytes on the
air) at the link's normal ~20 dB, and the frames that arrived showed every
data carrier clean -- in BPSK as well as QPSK Wide. That's interference
hitting frames, not a weak signal. Auto still stepped down to BPSK, whose
3 s frames are hit even more often.

- Losses while the receiver reports clean carriers (12 dB or better) and
  light FEC (25% or less) now stop at QPSK Wide. A weak signal (low S/N),
  heavy FEC or poor carriers still step down as before.

## v0.9.19 — Auto remembers each station for hours, and across restarts

Field (v0.9.18, DV1UK-10 -> DW1IRF): after a ~40 minute break between test
sets, Auto had forgotten that QPSK Wide works better than narrow QPSK with
DW1IRF (its memory lasted 30 minutes), so every receive started in narrow
QPSK again -- 43 of 47 frames -- and ran slower than in v0.9.17.

- What Auto learns about a station (which modes worked, which failed, where
  the last session ended) now lasts 6 hours instead of 30 minutes. The
  first report of a new session still corrects a wrong start.
- It is also saved to sara_pp_speed.json next to the program (when a
  Winlink/BPQ32 session ends and when SARA-PP closes) and loaded at start,
  so a gateway restart doesn't forget it. Delete the file to start fresh;
  build_exe.bat leaves it out of new builds.

## v0.9.18 — 64QAM Wide (experimental)

Goal: sessions past 2,000 bps on clean links. After Tune TX, DW1IRF's data
carriers reach DV1UK at ~22 dB -- close to what 64-QAM needs.

- **64QAM Wide**: 6 bits per carrier on the 18 Wide data carriers, 50% more
  than 16QAM Wide. Two full AX.25 frames take ~0.72 s on the air instead of
  ~1.04 s. Simulated FM channels: decodes from ~29 dB preamble / ~23 dB
  data-carrier S/N, clean from ~32 / ~25.
- On the air it uses header code 2 (as 16QAM Wide) with its own training
  symbol; the receiver tells them apart by which pattern gives a smooth
  channel estimate. Stations announce "I decode 64QAM" (CAP_QAM64), and
  Auto never sends it to one that hasn't -- so 64QAM is only used between
  two v0.9.18+ stations.
- Link reports now also carry the median data-carrier S/N (an 11th byte
  that older versions ignore). Auto steps up from 16QAM to 64QAM only when
  the other station reports carriers at 24 dB or better (S/N 33, or a
  spotless run at 29+), and back down when carriers fall under 21 dB, S/N
  under 28, or on losses / heavy FEC.
- "64QAM Wide" can be picked by hand for tests, and the "Auto: 64QAM (test)"
  box next to Modulation turns it off for Auto (saved in settings).
- The verbose link-report line shows the reported carrier S/N.

## v0.9.17 — reach QPSK Wide sooner on a weaker clean link

Field (v0.9.16, DV1UK-10 -> DW1IRF, a weaker day: 19-21 dB): every report
was spotless (FEC 0%, 7 ok / 0 lost), but under the 21 dB step-up from
narrow QPSK, so receives sat in QPSK for 45-80 s per session before a probe
moved them to QPSK Wide, and every new connection started in QPSK again.

- Spotless QPSK reports at 17 dB+ now try QPSK Wide (which decodes from
  ~12 dB), like spotless Wide reports at 22 dB+ try 16QAM Wide.
- Spotless tries wait 15 s in the mode (was the 45 s probe wait); the
  wait still grows after a failed try.
- A session that ENDED spotless in a mode starts the next connection in
  that same mode ("last session ended spotless in QPSK Wide"), not one
  below.

## v0.9.16 — receives can reach 16QAM Wide on a clean, quieter link

Field (v0.9.14 after Tune TX, DV1UK-10 -> DW1IRF): DV1UK's QPSK Wide frames
arrived spotless -- every carrier clean at 17-19 dB, FEC 0%, 0 lost -- but
at 22-25 dB preamble S/N, under the 27 dB Auto needs to step up to 16QAM
Wide (which decodes from ~22 dB). Every receive stayed in QPSK Wide.

- Three spotless reports in a row (FEC 4% or less, no losses) at 22 dB or
  more now try the next mode up, on the probe timer ("trying faster: QPSK
  Wide spotless ..."). A try that fails doubles the wait, and below 24 dB
  puts the faster mode on hold as before.
- 16QAM Wide steps down below 20 dB S/N (was 23); losses or heavy FEC
  still step it down at any S/N.
- After Tune TX, one ordinary ping at the level kept refreshes the Ping
  window (it showed the last, deliberately quiet test ping).

## v0.9.15 — Tune TX works with VOX

Field (v0.9.14, DW1IRF with a SignaLink on VOX): at the quietest test
levels (9%, 6%) the VOX never keyed, three unanswered pings ended Tune TX
as "no reply", and the good measurements at 35-13% were thrown away.

- A quieter level that gets no reply at all now ends the sweep, and the
  levels already measured are used and shown. The table lists it as
  "no reply (too quiet to key VOX, or to be heard)".
- "No reply" is reported only when the other station never answered.

## v0.9.14 — Tune TX

Field (v0.9.12-13, DW1IRF -> DV1UK-10): frames arrived at 30-38 dB on the
preamble but only 9-13 dB on the data carriers (the DISTORTED warning), so
16QAM Wide sat at its limit. SARA-PP sets the transmit audio level itself,
so it can now find the right one.

- New **Tune TX** button (next to Auto-Tune): pings the Target in QPSK Wide
  at the current TX level, one step louder when there's room, then 3, 6, 9
  and 12 dB quieter -- two pings each. The other station's PONG reports the
  data-carrier S/N for each ping; the log lists every level, and the
  loudest one within 1 dB of the cleanest is saved (the current level stays
  unless another is at least 2 dB cleaner). If the carriers read the same
  at every level, the log says TX level isn't the cause.
- Both stations need v0.9.14 (older versions don't answer a Tune TX ping).
  Ordinary PING, Auto-Tune and all Winlink traffic are unchanged and work
  with older versions.
- The red "CLIP" on the input meter no longer shows while transmitting:
  that was the station's own TX audio leaking into its input, not receive
  clipping (field: DX1BBC on every TX).

Also recommended (no new version needed): RESPTIME=500 on the SARA-PP port
in BPQ32 -- see BPQ32.cfg.example.

## v0.9.13 — Auto skips narrow QPSK when QPSK Wide does better

Field (v0.9.12, DW1IRF <-> DV1UK-10): sends were good (10 kB in 1:08, QPSK
Wide), but receives from DV1UK ran mostly in BPSK, and sessions often
stalled ~20 s right after connecting. DW1IRF heard DV1UK's two lowest
carriers (562/656 Hz) at only 5-7 dB, so narrow QPSK frames used 40-100%
of their FEC or were lost -- often DV1UK's first reply, which Winlink
then retried ~20 s later. QPSK Wide doesn't use carriers that low and
reported FEC 0-8% on the same link, but Auto had to pass through the
failing QPSK to reach it, and v0.9.12's holds kept it in BPSK.

- Auto keeps 30 minutes of each mode's link reports per station. When
  narrow QPSK has been clearly more troubled than QPSK Wide (a loss or
  FEC 40%+ in a quarter more of its reports), the ladder skips QPSK:
  BPSK <-> QPSK Wide, a troubled QPSK goes to Wide instead of BPSK, and a
  new connection starts in QPSK Wide ("QPSK Wide has worked better than
  QPSK with this station").
- Links where narrow QPSK is fine are unchanged.
- Auto-Tune's noise warning is clearer when background noise is as loud
  as the signal, and suggests closing the squelch or DCD "SARA-PP
  signals only".

## v0.9.12 — no stalls from a failing mode or a noisy channel

Field (v0.9.11, DW1IRF with Winlink Express <-> DV1UK-10, VHF): the first
message went through, the second never did. Two causes in the logs:

- DW1IRF's QPSK frames reached DV1UK at preamble S/N 40 dB but only ~6 dB
  on the carriers (distorted) -- BPSK arrived clean, QPSK never did.
  Every clean BPSK report stepped Auto straight back to QPSK, the next
  7-frame window was lost, and the session sat 25-35 s in a FRACK wait
  (5 times in one message); every new connection started in QPSK.
  Now a mode that fails before giving one clean report (at S/N under
  24 dB) is put on hold -- 90 s, doubling on each repeat up to 10 min:
  no step up, probe or new-connection start in it until then. The log
  says "QPSK on hold 90 s". A clean report in that mode clears it.
- DV1UK waited 10-30 s for a "clear channel" while nobody was
  transmitting (noise floor 1 dB under the signal, "carrier detected by
  spectrum") -- each wait another stall. During a session (a SARA-PP
  frame decoded in the last 60 s), DCD Auto now transmits after 8 s of
  "busy" that has no SARA-PP signal in it; the log adds "busy but no
  SARA-PP signal: local noise or interference?". A real burst from the
  other station (its frames decoding, its fill tone) is still waited out,
  and outside a session the full 30 s wait is unchanged.

## v0.9.11 — no deaf spells from noise claiming a fast mode

Field (DV1UK gateway <- DW1IRF with a SignaLink, VHF, 4-6 km): DV1UK
rejected 17 "16QAM Wide" headers at preamble S/N 3-11 dB claiming 450-810
bytes. Each was noise, but the receiver waited ~1 s for it -- deaf to real
frames meanwhile. 16QAM needs ~19 dB.

- A header claiming a mode its preamble is far too weak for (QPSK < 5 dB,
  QPSK Wide < 8, 16QAM Wide < 14) is rejected at once
  (snr_too_low_for_mode) -- and counted as a lost frame for the link
  report, so a real fade still steps Auto down.

## v0.9.10 — uploads reach 16QAM Wide sooner

Field (v0.9.9): no collisions, 9.7 kB sent in 0:52 (1:33 in v0.9.8), but
uploads spent much of each session climbing: K6IRF sent 45 of its 135 data
frames in 16QAM Wide, the gateway all 103. Each session restarted at QPSK
and needed 2 clean reports per step, and a report arrives only once per
window. Downloads hid this: the handshake gives the gateway its reports
before the bulk data starts.

- **One clean report steps up** when it is 2+ dB above the step-up S/N
  with FEC <= 10% (otherwise 2 reports, as before).
- **A new connection within 30 min of a session that ended clean** starts
  one step below where that session ended (e.g. QPSK Wide after 16QAM);
  one good report takes it back up. After losses, or a long gap, it still
  starts fresh at QPSK.

## v0.9.9 — the gateway's own squelch tail no longer cuts its yield short

Field (v0.9.8): no losses, no step-downs, only 3 frames missed (23 before),
but both 9.7 kB sends ran ~1:35 vs ~0:52 receives. Each stalled ~30 s at
the end: after its acknowledgment, BPQ32 sent one more 19-byte control frame
~1.5 s later, just as K6IRF started its next (last) window -- both lost,
then Winlink's ~30 s retry timer. The 6.5 s control yield should have held
that frame, but it ended after ~0.5 s: the channel read "busy" from
DW1IRF's own squelch tail right after its transmission.

- In the yield, a SARA-PP signal (fill tone / frame) still ends it at
  once; any other "busy" only after YIELD_SELF_TAIL_S (1.2 s).

## v0.9.8 — frames that never reached the decoder; Auto ignores those resends

Field (v0.9.7, K6IRF -> DW1IRF sends 2:10-2:38 vs ~0:52 in v0.9.4):
DW1IRF decoded every frame it locked onto, but many of K6IRF's frames --
often the first data frame of a window -- never reached its decoder
(low_preamble_snr rejections right where their preamble arrived). Winlink
resent whole windows (15.7-20 kB on air for a 9.7 kB message), and each
resend made Auto step down (once 30 ms after stepping UP), so the resends
went out slower still.

- **Receiver**: after a candidate whose "preamble" doesn't repeat
  (low_preamble_snr) it moves on half a symbol, not four -- four could
  jump over a real preamble starting just after it. Test: a fake repeated
  symbol right before a real frame, 15/20 -> 18/20 decoded.
- **Auto**: a resend while the other station's latest report is clean and
  strong no longer steps down (a frame that never reached their decoder,
  or a lost acknowledgment, isn't fixed by a slower mode).

## v0.9.7 — sends back to v0.9.4 speed; transmit gaps made visible

Field (v0.9.6, K6IRF <-> DW1IRF): sends slowed (9.7 kB 0:52 -> 1:15-2:07).
That path's narrow carriers run ~14 dB below the preamble yet decode
cleanly; v0.9.6's +8 dB offset dropped its reports to 21-23 dB, under the
24 dB "strong" mark, so resent frames walked Auto down to BPSK. Also 23 of
K6IRF's frames never reached DW1IRF's decoder (0 in v0.9.4; the modem is
unchanged), with K6IRF's PC losing input audio at the same time.

- CARRIER_SNR_OFFSET_DB 8 -> 14; distortion warning at an 18 dB gap.
  DV1UK's distorted signal (gap ~20) is still reported ~22 dB and warned.
- Transmit-audio underruns are counted and reported ("transmit audio ran
  dry N times") in the PC-not-keeping-up warning.

## v0.9.6 — distorted signals reported honestly

Field (DV1UK -> DU3FS, v0.9.5): DU3FS read DV1UK's frames at a 27-31 dB
preamble S/N while its carrier check measured only 1-11 dB on the data
carriers; ~half the QPSK frames were lost, yet Auto kept "trying faster
again: S/N 28 dB". The preamble S/N compares four identical repetitions,
so a signal distorted the same way each time still reads clean.

- **Link reports** use min(preamble S/N, median carrier S/N + 8 dB) --
  unchanged on healthy links (carriers run ~8 dB below the preamble
  figure), honest on distorted ones, so Auto stops probing faster there.
- **Carrier check warns** when the preamble reads 14+ dB above the
  carriers: the signal is distorted (TX audio too loud / clipping, or a
  steady tone in the band), not weak.

## v0.9.5 — network KISS never locks out this PC

Field (DV1UK): with "Allow network KISS clients" on and only a Tailscale
address in the list, the gateway's own BPQ32 (127.0.0.1) was refused every
30 s -- the gateway was off the air while the setting was on. This PC
(127.x, ::1) is now always allowed; the list only controls other computers.

## v0.9.4 — 16QAM Wide: a faster top speed for strong links

The v0.9.3 field carrier checks show 18-26 dB per-carrier S/N (error-vector
based, so real radio distortion included) on every Wide carrier, and the
links run 28-32 dB preamble S/N -- room for a denser constellation.

- **16QAM Wide** (header code 2, once the retired narrow 8PSK): QPSK Wide's
  22 carriers, training symbol, pilots and tracking, 4 bits per carrier
  (72 bits/symbol, 2x QPSK Wide), Gray-coded per axis. Simulated (field-
  shaped and FM channels): every frame decodes from ~22 dB preamble S/N,
  fails below ~19; survives 300 ppm clock drift and mid-frame dropouts;
  sensitive to TX overdrive (2x clipping breaks it -- keep TX level so the
  far end reads in target).
- **Two AX.25 frames per OFDM frame** in 16QAM Wide (MAX_BATCH_BYTES_QAM16
  560): one preamble, no gap. A 7-frame window: ~8.8 s in QPSK Wide ->
  ~4.4 s; with the field's ~2.2 s per-window overhead, ~1.6x on big
  transfers.
- **Auto**: ladder BPSK -> QPSK -> QPSK Wide -> 16QAM Wide. Up from Wide
  after 2 clean reports at >= 27 dB; down below 23 dB or on any loss/heavy
  FEC. Only to stations that say they decode it: link reports gain a 10th
  byte (capabilities) that v0.9.0-v0.9.3 ignore; a 9-byte report means
  "no 16QAM" -- mixed versions keep working, topping out at QPSK Wide.
- Older receivers (v0.7.1-v0.9.3) reject a 16QAM frame instantly
  (invalid_modulation), never misread it. "16QAM Wide" is also selectable
  manually (testing only -- the other station must run v0.9.4+).

## v0.9.3 — reply yield by frame type

Field (v0.9.2, 8 runs): download wrap-up (last data -> disconnect) fell
from 33-39 s to 14-16 s, as intended. One upload still collided at the
end: BPQ32 answered ~4.3 s after K6IRF's last frames and Winlink's next
control frame went out at 4.5 s. The longer yield also held the gateway's
consecutive BPQ32 text lines ~2 s each.

- **Two yields** (audio_io.reply_yield_s): a burst of AX.25 control frames
  only (RR/RNR/REJ, DISC, UA... -- every collision seen so far was one)
  yields CONTROL_REPLY_YIELD_S 6.5 s; a burst carrying I-frames keeps
  REPLY_YIELD_S 2.5 s. Both end at once when the other station is heard.

## v0.9.2 — quicker disconnect after downloads; log files named by station

Field (v0.9.1, 10 runs): no lost frames in either direction, no BPSK, no
audio warnings. But every one of the 5 downloads ended with a collision:
K6IRF acknowledged BPQ32's last frame, Winlink Express's next frame went
out when the 2.5 s reply yield ran out, and BPQ32's reply started ~3.1 s
after K6IRF's transmission -- both keyed within ~0.1 s, both frames lost,
~20 s wait for the retry.

- **REPLY_YIELD_S 2.5 -> 4.5 s.** Still ends at once when the other
  station's carrier is heard or a frame from it is decoded, so normal
  turn-taking isn't slowed; only an unanswered station waits longer.
- **Session log files** are now logs/sara_pp_<CALL>_YYYY-MM-DD_HHMMSS.log,
  one per program run (new one at midnight or on a callsign change); a
  file started before the callsign loaded is renamed for it. Save Logs
  still collects the last 3 days, old-style names included.

## v0.9.1 — long BPSK frames lost on a busy PC; Auto stops getting stuck slow

Field (v0.9.0, run 12): the gateway's BPSK frames reached K6IRF at S/N
33-36 dB, yet 17 of 27 failed FEC or vanished (every other frame of a
window missing, the rest with random +-180 deg "drift"), while K6IRF
decoded 236 of 237 Wide frames and the gateway decoded K6IRF's BPSK fine.
Auto read the losses as a bad link, went to BPSK and stayed ~5 minutes.

- **Decoder off the sound-card callback** (audio_io._rx_worker): the
  callback now only measures levels (DCD stays live) and queues the audio;
  a decoder thread does the rest, so a slow decode can't make the sound
  card drop input. The callback used to re-filter and re-search the whole
  growing buffer on every 21 ms block while a frame arrived -- work that
  grew with frame length (BPSK 2.8 s worst).
- **Receiver waits cheaply** (OFDMReceiver.poll): a frame that passed the
  header checks and is still arriving isn't re-processed until all of it
  is here. Measured: BPSK 13% -> 2% of real time on the test machine.
- **Audio health in the log**: sound card input overruns and decoder lag
  over 0.5 s produce a warning (at most every 10 s).
- **Auto**: losses reported at S/N >= 24 dB never step below QPSK (not a
  noise problem; also applies to BPQ32 resends after a strong report).
  In a slower mode for 45 s with a strong enough signal for the next one,
  Auto tries one step up ("trying faster again"); a probe that fails within
  30 s doubles the wait (up to 180 s). An AX.25 connect (SABM) either way
  starts that station fresh at QPSK, like reconnecting in VARA FM.

## v0.9.0 — Phase 2: automatic speed selection; KISS ACKMODE

VARA FM changes speed during a session; it runs its own ARQ, so it knows
which blocks failed. SARA-PP sits under AX.25 (BPQ32 / Winlink Express
own the acknowledgments), and the field showed one station can't judge how
it is heard from how it hears (K6IRF heard DW1IRF cleanly while DW1IRF
struggled with K6IRF's Wide frames). New module link_adapt.py:

- **Link reports.** Each bridge burst carries, after its AX.25 frames, a
  9-byte report for the station it's addressed to: mode, frames ok/lost,
  median preamble S/N, worst and average FEC use (% of capacity). Batches
  carry explicit frame lengths, so receivers since v0.5 never see it (it
  never reaches BPQ32). Lost frames (fec_uncorrectable/crc_fail at S/N
  >= 5 dB, not during our own TX) are charged to the station heard in the
  last 20 s; rejects now always feed this, logging still only if verbose.
- **Per-station speed controller**, ladder BPSK -> QPSK -> QPSK Wide,
  start QPSK. Down one step at once on a reported loss, FEC use >= 60%,
  or S/N below 11 dB (QPSK) / 16 dB (Wide); up one step after 2 clean
  reports with S/N >= 15 (BPSK) / 21 (QPSK) and FEC <= 25% -- hysteresis
  4-6 dB. After a step down: further downs held off 8 s (rest of the old
  window), ups 30 s. Reports about another mode are ignored.
- **Retransmission detection** (older peers): BPQ32 resending the same
  I-frame (same N(S) and contents within 90 s) steps down -- unless a clean
  report arrived AFTER our last transmission (then their ack was lost).
  Without reports Auto never goes above QPSK; it creeps back to QPSK
  after 90 s clean.
- **"Auto" Modulation**: bridge traffic per destination callsign; pings,
  Auto-Tune and TUNE use QPSK. Mode changes are logged with the reason;
  the speed line shows "Auto: <mode> to <station>".
- **KISS ACKMODE** (BPQ32 KISSOPTIONS=ACKMODE): command 0x0C frames carry
  a 2-byte id; SARA-PP echoes it once the frame has actually been
  transmitted (merged batches matched by content), and acknowledges
  everything outstanding if a transmission fails so BPQ32's own timers
  retry. FRACK then counts from transmission (BPQ32.cfg.example: try
  12000). An older SARA-PP ignores ACKMODE frames, so upgrade first.
- **Receiver**: Wide gets its own size limit (MAX_RX_DATA_BYTES_WIDE 420):
  the largest AX.25 frame (8 digipeaters) + report is 415 bytes on air in
  Wide (391 in BPSK/QPSK) -- the 8-digipeater case already exceeded 400 in
  Wide since v0.8.3.

**Simulated with the real modem, receiver, reports and controller** (two
stations, 7-frame windows of 236-byte frames, AX.25 go-back-N, link S/N
30 -> 17 -> 9 -> 30 dB, 34 windows):

| | delivered | bps | link survives |
|---|---|---|---|
| Auto | 46.4 kB | 684 | yes -- Wide at 30/17 dB, BPSK at 9 dB, back to Wide |
| fixed QPSK Wide | 40.0 kB | 740* | no -- 0 of 56 frames at 9 dB |
| fixed QPSK | 40.3 kB | 606* | no -- 1 of 56 at 9 dB |
| fixed BPSK | 49.1 kB | 468 | yes |

(*average over the windows it ran; a real AX.25 session would have
disconnected after 5 windows with nothing delivered.) Auto: Wide from the
3rd window; at 9 dB two lost windows (Wide, QPSK) then BPSK; back to Wide
four windows after the link recovered.

Compatibility: no change to the waveform; reports are invisible to older
versions; fixed modulation settings behave exactly as before.

- link_adapt.py: new. kiss.py: CMD_ACKMODE. bpq_bridge.py: ACKMODE,
  on_client_frame hook. audio_io.py: finalize_payload hook (after merge,
  before encode). ofdm_core.py: Wide size limit. sara_pingpong.py: Auto,
  reports, controller, speed line. BPQ32.cfg.example, QUICKSTART 14.
- test_offline.py: +34 checks incl. ACKMODE over a socket and a short
  two-station simulation with the real modem (306 without a display).

## v0.8.9 — real error messages; PTT can't stay stuck

*Package update (same program):* the Windows build stopped at 273 passed /
1 failed -- "a 7-frame window goes out under ONE key-up -- sent=7,
key-ups=1". The check also required PTT to have been released, but checked
the moment the 7th frame was reported sent; PTT is released after the
150 ms closing tone, which hadn't finished on that PC. A test timing flaw,
not a program fault: the test now waits for the release and prints it.
Reproduced with a slowed closing tone (releases=0 at the old check point,
1 about 0.3 s later); the fixed test passes under the same slowdown.

**Field (v0.8.8, K6IRF):** SARA-PP stopped working and the radio's PTT
stayed keyed until the program was restarted. The console showed only

    NameError: cannot access free variable 'e' where it is not associated
    with a value in enclosing scope   (sara_pingpong.py line 1021, "TX error")

Two bugs together:

1. **Lost error messages.** Six places logged errors with
   `self.after(0, lambda: self._log(f"...{e}"))`. Python deletes `e` when
   the except block ends; the lambda runs later on the GUI thread, so the
   real error was replaced by a NameError. Now `_report_error()` formats the
   message and traceback immediately (traceback also into the daily session
   log). A test scans every module for this pattern.
2. **PTT left keyed.** A transmission failed (most likely a USB hiccup on
   the RA-Board -- RF in the cabling was seen earlier), and the PTT-OFF HID
   write failed too. PTTController.ptt_off() had already cleared its
   "keyed" flag BEFORE the write, so every later release was skipped; the
   CM108 layer then skipped "on" writes too. Only a restart (which reopens
   the board and sends OFF) cleared it.

**Fixes:**
- CM108 writes check the result, retry 3 times, and on a stale handle
  REOPEN THE SAME BOARD by USB path -- never "the first board found" (a
  station with two RA-Boards must not key the other radio). ptt_off/ptt_on
  always send their command.
- PTTController keeps PTT marked keyed until a release succeeds
  (`release_failed`), so the next release really retries.
- PTT watchdog in the GUI: while a release has failed and nothing is
  transmitting, retry every second; warn every 15 s ("PTT release is
  FAILING -- the radio may be stuck transmitting ..."); log when released.

Tests with a fake RA-Board: one failed OFF write -> retried, released; a
release that keeps failing -> PTT stays marked keyed, watchdog retries,
released when USB recovers; with two boards, the other is never opened.

No on-air change; interoperates with v0.8.x.

## v0.8.8 — Save Logs button

DU3FS gateway now runs v0.8.7 on UHF, with volunteers testing. Field
logs used to be copied out of the window by hand, files had to be found in
folders, and some arrived named for the wrong station.

- **Every log line is also written to logs/sara_pp_YYYY-MM-DD.log** next
  to the program (one file per day, header with version and UTC offset),
  so nothing is lost when the window closes or the program crashes.
- **"Save Logs" button** (next to TUNE 10s, always available): one zip on
  the Desktop (OneDrive Desktop too; else next to the program), named
  SARA-PP_logs_<CALLSIGN>_<YYYY-MM-DD_HHMM>.zip, holding station_info.txt
  (version, callsign, mode, modulation, DCD mode, devices, PTT, TX delay,
  RX gain, TX level, KISS settings, noise floor, DCD state, time zone),
  log_window.txt, the last 3 days' session logs, the ping/pong CSV, the
  settings file, and the crash log / self-test result if present. Opens
  Explorer with the file selected and says to attach it. Nothing is sent
  anywhere automatically.
- build_exe.bat also removes the build PC's logs folder from the zip.

No on-air change; interoperates with v0.8.x.

- log_bundle.py: new (SessionLog, make_bundle). sara_pingpong.py: button,
  _station_info(), _save_logs(). QUICKSTART section 13.
- test_offline.py: +8 checks (264, no display).

## v0.8.7 — yield until the reply is heard; millisecond log times

**Field (v0.8.6, hand-timed):** sends 1133/1159 bps and receives
1084-1212 bps; all carrier checks clean, no audio-glitch losses. Two
problems remained:

1. **8.3 kB send took 2:01 (551 bps): two collisions.** 22:37:28 --
   DW1IRF sent a 19-byte frame 2.0 s after its previous transmission,
   just past the 1.5 s yield, as K6IRF keyed up its next window: the
   window's first frame was lost (decode error at normal drift and S/N)
   and AX.25 resent the rest. 22:37:54 -- K6IRF's final frame overlapped
   DW1IRF's transmission ("hissing"); nothing moved until K6IRF's poll 45 s
   later. One-second log stamps couldn't time the second one.
2. **Fifth message stopped; cured by lowering TX power.** K6IRF logged
   signals 5-7x stronger than anywhere else in the session plus garbled
   headers and a lost frame at excellent S/N: consistent with RF getting
   into the audio/USB cabling at full power. QUICKSTART: ferrite chokes.

**Changes:**
- REPLY_YIELD_S 1.5 -> 2.5 s, but the yield ends as soon as a complete
  frame from the other station has been received after our TX ended
  (REPLY_HEARD_MARGIN_S excludes our own echo). Timing test: nothing heard
  -> 2.55 s; reply already received -> 0.28 s (turns aren't slowed).
- Log timestamps now carry milliseconds.

No on-air change; interoperates with v0.8.x. (Source restored from the
v0.8.6 release zip; git history restarts at v0.8.6.)

## v0.8.6 — glitch-proof tracking on strong frames; single TX report

**Field (v0.8.5, both station logs):** hand-timed against v0.8.4, all in
QPSK Wide. Receives improved as predicted by acknowledgment batching:
~1065-1089 -> ~1104-1296 bps (+18-20%); the logs show K6IRF's 7
acknowledgments going out as one frame ("TX 121 bytes (7 frames)").
The 2.9 kB send also improved (933 -> 1123 bps), but the 5.7 and 8.4 kB
sends were slower (1020/1066 -> 872/836 bps).

Two separate findings explained the sends:

1. **Reporting bug (v0.8.5):** a merged frame was logged -- and counted in
   the speed meter / session summary -- once per merged item ("TX 265
   bytes (7 frames)" x3 in the same second; DW1IRF received it once). The
   GUI passes a bound method as the TX callback; each reference is a new
   object, and de-duplication compared identity. Inflated the TX kB
   figures, not the traffic. Fixed (equality).
2. **A real lost frame:** de-duplicated, the 8.4 kB send still needed 47
   large data frames vs 33 in v0.8.4 -- 14 resent. DW1IRF lost one: S/N
   28.7 dB, fec_uncorrectable, drift +150 deg where its neighbours read
   ~-34: a ~180 deg jump mid-frame. AX.25 go-back-N then resent the rest
   of the window, more than once.

**Cause, simulated:** a few audio samples dropped or repeated mid-frame (a
USB sound card / Windows audio hiccup) shift every later symbol's timing;
each sample becomes ~12 deg of phase SLOPE across the band (the field jump
~= 15 samples). Up to 8 samples decoded; 16 killed every frame in every
mode. Narrow tracking (one common correction) couldn't represent a slope;
Wide's line fit used phase angles that wrap at +-180 deg.

**Fix -- phase-line tracking (receiver only):** pilots weighted by channel
magnitude, phases UNWRAPPED across frequency, a straight line (common
phase + slope) fitted and applied; a symbol that straddles a glitch or is
hit by a burst doesn't fit a line and is skipped (FEC repairs its bytes).
Glitch survival: narrow up to ~48 samples, Wide ~32 (was 8); two glitches
per frame fine; clock drift on strong frames: all modes 8/8 at 500 ppm
(narrow QPSK used to fail at 200).

**Weak links unchanged, deliberately:** side by side with the old tracker
at the decoding edge, any slope term cost frames (BPSK 8 -> 4-7 of 16,
QPSK 10 -> 5-7) -- 4 noisy pilots can't measure a slope, and it often
looks like a jump. So narrow modes use phase-line tracking only on frames
with preamble S/N >= 15 dB (TRACK_SLOPE_MIN_SNR_DB); below that they are
bit-for-bit the pre-v0.8.6 tracker (equal at every edge level: 14/8/1 and
16/15/10 of 16). Wide keeps slope tracking always (as validated; it's used
on strong links).

Disconnect: on the air the closing takes ~11 s after the last data frame
(final acknowledgment window + FF/FQ/DISC/UA); further time before a
gateway session is fully free isn't visible in SARA-PP's log.

No on-air change; interoperates with v0.8.x.

- ofdm_core.py: `_robust_track()`, SNR-gated `_track_pilots()`, Wide uses
  the shared tracker. audio_io.py: merged-frame callback de-dup by equality.
- test_offline.py: +5 checks, 8PSK drift test restated (254, no display).

## v0.8.5 — acknowledgment batching (Phase 1 of the speed plan)

**Field (v0.8.4):** six transfers (three each way) all completed in
QPSK Wide at ~1000 bps; cleaner disconnects; one 16.8 s hold-off was a
non-SARA-PP signal on the frequency (loud, no SARA-PP preamble) -- DCD
correctly waited it out. Every carrier check clean (18-22 dB per carrier
on the K6IRF -> DW1IRF path that used to struggle).

**Where a transfer cycle's time goes** (one 7-frame window, Wide):
~8 s data (1.6 kB), ~3.4 s for the 7 acknowledgments, ~1.5 s key-ups
and turnarounds -- ~13 s, ~1000 bps. Winlink Express acknowledges each
data frame separately; those 19-byte RR frames queue up while DCD holds
the station during the other side's window, then went out as SEVEN OFDM
frames, each with its own preamble and gap.

**Change:** at burst time the engine merges consecutive queued batches
(`bpq_bridge.merge_batches`) -- same modulation, AX.25 order preserved,
within the same 250-byte MAX_BATCH_BYTES cap the coalescer uses (so no
frame is longer on the air than the drift tests validated). 7
acknowledgments: 7 frames / 3.5 s -> 1 frame / 1.4 s, cutting ~2 s from
each ~13 s cycle: expect roughly 15-20% more throughput in every
modulation. No on-air format change: receivers have always unpacked
multi-frame batches, so this works with v0.7.x stations too.

**Speed plan** (from the VARA FM comparison): 1) acknowledgment batching
(this); 2) automatic Wide/QPSK/BPSK selection + KISS ACKMODE; 3) a 16QAM
Wide level for strong links (carrier checks show 18-22 dB per carrier,
plausibly enough); beyond -- a 9600-port mode and/or SARA-PP's own
large-block ARQ, where VARA-like level-3 rates live.

- bpq_bridge.py: `merge_batches()`. audio_io.py: burst-time merging,
  `_requeue_front()`. sara_pingpong.py: enables merging in bridge mode.
- test_offline.py: +5 checks (249, no display).

## v0.8.4 — yield to the reply; BPSK bridge warning

**Field (v0.8.3), K6IRF <-> DW1IRF BBS, both in QPSK Wide for uploads:**
5546 B in 46 s, 10683 B in 1:25, 16388 B in 2:20 (~940-1000 bps); two
downloads 5722 B in 43 s and 16940 B in 2:23. Two earlier downloads
stalled -- and the BBS was transmitting **BPSK** then (its Modulation
setting); the smooth ones came right after it switched to Wide.

**BPSK stall:** a 7 x 256-byte BPSK window is ~21.3 s on the air (QPSK
11.8, Wide 8.9) -- longer than BPQ32's FRACK (20 s), which starts when
BPQ32 hands the frames over. Every window timed out and was resent; the
resends kept the BBS transmitting almost continuously, K6IRF's DCD held
its acknowledgments (up to 30 s), and more timeouts followed: one
download ran 17 minutes / 40 kB. Not a Wide problem; the real fix is
KISS ACKMODE (BPQ32 starts FRACK when a frame actually goes out) --
planned with automatic speed selection. For now SARA-PP logs a warning
when a bridge starts in BPSK; QUICKSTART says use MAXFRAME 3 for BPSK.

**Talk-over collisions:** at the end of one download the BBS sent FQ,
K6IRF acknowledged, then K6IRF got a second short frame to send ~1 s
later -- exactly when the BBS sent its DISC. Both keyed up together,
both frames were lost, and the BBS's 23 s timeout made the hang-up take
~45 s. AX.25 takes turns: when one station stops, the other usually
answers within ~0.5-1 s; a NEW transmission in that gap sees a clear
channel (the reply hasn't keyed up yet), and the 0-0.3 s CSMA jitter
can't separate two stations reacting to the same moment. The logs show
this pattern ~46 times in one test (1-s resolution, so approximate);
each one on a control frame costs a 15-23 s FRACK stall.

**Fix -- yield to the reply:** a new transmission waits until
REPLY_YIELD_S (1.5 s) after our own last transmission ended, unless the
other station's reply shows on DCD first, in which case the normal DCD
wait holds us until it ends. Timing test: second frame 0.3 s after our
first, reply on air 0.6-1.6 s -> our key-up at ~1.75 s, no overlap
(before: ~0.3 s, on top of the reply). Bulk transfers are unaffected
(the next window follows the other side's acknowledgment). Pings/pongs
skip it. Not counted as a carrier-sense wait in the log.

The normal 4-6 s after a transfer before the link is free is the closing
exchange itself (FF, FQ, DISC, UA: several over-the-air turns) -- also
true of older versions and other half-duplex modems.

No on-air format change; interoperates with v0.8.3.

- audio_io.py: `REPLY_YIELD_S`, `_yield_to_reply()`, `_last_tx_end`.
- sara_pingpong.py: BPSK bridge warning. QUICKSTART section 12.
- test_offline.py: +3 checks (244, no display).

## v0.8.3 — Wide moved up the band; smaller Wide FEC blocks

**Field (v0.8.1/0.8.2), K6IRF -> DW1IRF BBS, QPSK Wide:** a 2966-byte
message went through in 22 s; a 6000-byte one choked three times at the
same point. The per-carrier check answered the band question -- it was
the LOW end, not the top:

| carrier | bit errors | S/N |
|---|---|---|
| 469 Hz | 1.4-2.5% | ~4 dB |
| 562 Hz | 4.6-6.3% | ~5 dB |
| 656-750 Hz | ~0.1% | 7-8 dB |
| 1.0-1.9 kHz | 0% | 10-14 dB |
| 2531 Hz | 0.3-0.8% | 7-8 dB |

(DW1IRF -> K6IRF: only 469 Hz slightly weak.) Radios commonly high-pass
the audio below a few hundred Hz to keep CTCSS tones out.

The choke: each 6000-byte attempt sent 21 x 275-byte frames, then a final
211-byte frame -- rejected fec_uncorrectable at +29 dB every time. The
275-byte frames (277 data bytes) got TWO Reed-Solomon blocks, 24 repairs,
and needed 4-12. At <= 231 data bytes a frame got ONE block, 12 repairs,
and the 211-byte frame needed more. A retransmission is identical, so it
failed the same way each time. (The 2966-byte message happened to end on
a 181-byte frame that squeaked through with 9.)

A channel shaped to the measured per-carrier S/N (field_channel_dw1irf.py,
calibrated so Wide's repair counts match the field's) reproduces it:
v0.8.2 Wide 20-22/24 frames decoded, median 4.5-8 repairs.

**Changes (Wide only -- narrow QPSK/BPSK unchanged, bit for bit):**
- Band 656-2625 Hz (bins 7-28): 22 carriers, pilots at both edges and
  every 7th, 18 data = 36 bits/symbol (QPSK 24, v0.8.0 Wide 40).
- Wide FEC blocks hold at most 160 data bytes (`WIDE_MAX_BLOCK_DATA`),
  so 161-231-byte frames get two blocks; shorter and longer frames are
  unchanged. fec.py takes the block size as a parameter.

**Measured (simulation):**
- Field-shaped channel: 24/24 at 275/211/181 bytes, median 1 repair.
- 3 dB worse than the field: Wide 15-19/24, QPSK 23-24/24 -- Wide has
  less margin; it is for strong links.
- FM channel: like QPSK to CNR 8 on speaker audio; on the 9600 port its
  dependable floor is ~CNR 10 (QPSK ~8).
- Drift: phase-slope tracking 8/8 to 400 ppm (QPSK fails at 200).
- 7 x 256-byte window 8.9 s (v0.8.0 Wide 8.2 s, QPSK 11.8 s).

**Compatibility:** v0.8.3 Wide is not compatible with v0.8.0-0.8.2 Wide
(both test stations must upgrade). QPSK/BPSK interoperate with all
v0.7.x/v0.8.x stations as before.

- ofdm_core.py: Wide band/pilots, `WIDE_MAX_BLOCK_DATA`, `fec_block()`.
- fec.py: `max_block_data` parameter. field_channel_dw1irf.py: new (tests).
- QUICKSTART section 12 rewritten. test_offline.py: 241 (no display).

## v0.8.2 — duplicate-callsign warning; clean distribution zips

**Field (v0.8.1):** pings between the two test stations went unanswered
in every modulation, on v0.8.1 at either or both ends. The software path
was verified (a decoded PING produces a PONG in BPSK, QPSK and Wide, and
the PONG decodes), and K6IRF's log showed why: it received PINGs "from
K6IRF" with sequence numbers it never sent (restarting at seq 1, at
-38/-28 dBFS -- not its own loud echo), labelled "own TX heard locally".
Both stations had My callsign = K6IRF -- nobody was DW1IRF, so nobody
answered, and each took the other's pings for its own echo. Settings
(including the callsign) live in sara_pp_settings.json next to the
program, so copying a SARA-PP folder between PCs copies the identity.

- A PING from our own callsign whose sequence number we didn't send in
  the last 20 s is flagged "[ANOTHER station is using YOUR callsign]",
  with a log warning and a one-time pop-up. Our real echo stays quiet.
- build_exe.bat deletes per-station files (settings, logs, self-test
  result, crash log) from dist\SARA-PP before zipping, so a build PC's
  identity never ships to other stations.
- QUICKSTART: don't copy SARA-PP folders between stations.

No on-air change. test_offline.py: +4 checks.

## v0.8.1 — per-carrier check; Wide pilot weighting

**Field (v0.8.0), K6IRF <-> DW1IRF BBS:** QPSK worked throughout (four
messages, 557-778 bps sessions). QPSK Wide: the first two messages went
through, the third stalled on three attempts (sessions of 5-6 minutes
sending 18-30 kB for one message -- retransmissions), and a final Wide
run reached 1004 bps, the fastest yet. The logs showed:
- K6IRF -> DW1IRF, large Wide frames: 100% needed FEC repair, median 15
  bytes, up to 24 -- the FEC limit (12 per block x 2). Frames just past
  it can't be fixed, the sender retransmits, and Winlink Express sits
  with its progress bar at the end. Overall S/N was +29 dB, drift
  moderate -- not a weak link. QPSK on the same path: 48% repaired,
  median 3 bytes.
- DW1IRF -> K6IRF, Wide: 15% repaired, median 1 byte -- clean.
So Wide works over the air, but this one direction (K6IRF's transmit
path or DW1IRF's receive path) damages some carriers. A simulated 15 dB
roll-off above 2.5 kHz reproduces the signature (median 15 bytes, ~40%
of frames failing), so the top edge is the leading suspect -- but not
proven, so the band is left alone until field data says which carriers.

**Per-carrier check.** After a frame decodes, re-encoding the corrected
data gives the exact transmitted bits, so each data carrier's raw bit
errors and S/N (error-vector power vs the ideal point) are measured.
Frames from OTHER stations are accumulated and every 20 frames of a
modulation the log shows every carrier (Hz: bit errors / S/N) and names
the weakest -- e.g. a simulated 2.5 kHz roll-off is flagged as
"2531 Hz (13.2%, S/N -10 dB)".

**Wide pilot weighting.** The phase-vs-frequency fit now weights each
pilot by its channel magnitude. With the radio's low end rolled off (the
375 Hz pilot buried), the equal-weight fit skewed every carrier: 0/12
frames decoded; weighted, 11/12. Top-edge losses are dominated by the
lost data carriers themselves, which the carrier check will locate.

No on-air change; Wide interoperates with v0.8.0.

- ofdm_core.py: `_equalized()`, `_carrier_stats()`,
  `DecodedFrame.carrier_stats`, weighted `_track_pilots_wide()`.
- sara_pingpong.py: `_note_carriers()` / `_log_carriers()`.
- test_offline.py: +5 checks (234, no display).

## v0.8.0 — QPSK Wide (Phase 1): more carriers, ~25% faster transfers

VARA FM uses more carriers as conditions allow. Phase 1 adds a
selectable **QPSK Wide** mode; Phase 2 (automatic selection) can follow
once field results are in. (8PSK, tried in v0.5.11, packed more bits per
carrier and needed a cleaner channel than FM audio gives; Wide keeps
QPSK's robustness per carrier and adds carriers.)

**Is the band there?** The FM channel simulator measured per-carrier S/N
within a few dB of today's band out to ~2.7 kHz on both the speaker
(pre/de-emphasis) and 9600-port audio paths. Real radios' TX audio
filters/limiters aren't modelled, so the top edge stays at 2.625 kHz
pending field tests.

**Layout:** 25 carriers on bins 4-28 (375-2625 Hz) vs 16 on 5-20
(469-1875 Hz); 5 pilots (both edges) + 20 data carriers = 40 bits/symbol
vs 24. Frame: [preamble x4][header][Wide training symbol][data...]. The
preamble and header are unchanged (narrow), so detection and every
header check are untouched; header modulation value 3 = QPSK Wide (a
receiver without Wide support rejects it instantly as invalid_modulation
-- never misreads it). The training symbol gives the channel estimate
for the new outer carriers. Wide data is demodulated from a separately,
wider-filtered copy of the buffer; detection keeps the narrow filter.

**Drift tracking:** a sample-clock mismatch rotates higher carriers
more, so Wide fits phase vs frequency across its pilots instead of
narrow mode's single common correction. Measured (256-byte frames):
Wide survives 300 ppm (8/8) where QPSK, and Wide with a common
correction, fail at 200 ppm. (Narrow mode is unchanged here; giving it
the same tracking is a candidate for a later version.)

**Measured:**
- 256-byte frame 1.41 s -> 0.89 s; 7-frame window 11.8 s -> 8.2 s on the
  air (~30%); with turnarounds, large transfers ~25% faster. Shorter
  windows also give Winlink Express's 15 s FRACK more margin.
- FM channel, frames in open-squelch hiss: Wide decodes like QPSK down
  to CNR 8 dB on speaker and 9600 audio, needing ~1 dB more at the edge.
- DCD: Wide carriers reach the 2.3-2.9 kHz band Auto DCD's spectrum
  check uses; a Wide transmission could look idle, the open-squelch
  noise floor learned it, and idle hiss then read busy 100% (speaker
  audio). Fixed: a second containing a SARA-PP signal (fill tone or a
  frame arriving) is never treated as idle -- now 1% on all paths.

**Compatibility:** both stations need v0.8.0 to use Wide; everything
else is unchanged, and v0.8.0 interoperates with v0.7.x in BPSK/QPSK.

- ofdm_core.py: `MOD_QPSK_WIDE`, `WIDE_BINS/PILOT_POS/DATA_POS`,
  training symbol, `extra_symbols()`, wide demod + phase-slope tracking.
- audio_io.py: noise floor never learns from seconds with a SARA-PP
  signal. sara_pingpong.py: "QPSK Wide" option; self-test covers Wide.
- QUICKSTART section 12. test_offline.py: +10 checks (229, no display).

## v0.7.7 — VARA-style DCD; minimum preamble S/N

**Field (v0.7.6):** with squelch open, a test station would not transmit
-- its DCD read busy. VARA FM, on the same radios (open squelch, or the
9600 port), keeps its DCD green; its DCD can be overridden; and pings
ignore it.

**Investigation.** The FM simulator gained realistic radio audio paths
(flat; speaker/accessory with pre/de-emphasis -- the usual RA-Board
hookup; narrow speaker; 9600 data port). De-emphasis reshapes open-
squelch hiss a lot (in-band/above-band ratio ~+11 dB instead of ~+2,
hiss 14 dB louder than a SARA-PP transmission) -- yet v0.7.6 still showed
0% false busy on all four paths with STEADY hiss. Real hiss isn't steady:
hiss swelling +-6 dB made Auto read busy ~35% of the time. The field
failure is plausibly that (or something else the model lacks), so rather
than tune detectors blind, DCD now works like VARA's:

- **DCD lamp** (green clear / yellow busy, with the reason: level,
  spectrum, SARA-PP signal).
- **DCD Mode:** Auto (as before); **SARA-PP signals only** -- busy only
  when a SARA-PP station is audible: its fill tone (lead-in, gaps, tail;
  detected from the same FFT, tone bin vs neighbours) or a frame that
  passed every header check still arriving. Hiss of any shape can't trip
  it (swelling hiss: 0%), it detects real bursts on every audio path
  (speaker and 9600 port, CNR 10: 99-100%); it won't hold off for voice
  or other modes. **Off** never waits.
- **Transmit anyway** button: releases a busy DCD for 3 s.
- **PING, PONG, Auto-Tune and TUNE ignore DCD**, as in VARA.
- **Stuck-busy diagnostic:** busy 15 s with no SARA-PP frames heard logs
  what each detector read (level vs threshold, spectrum vs baseline, fill
  tone) and suggests "SARA-PP signals only".

**Receiver fix found on the way:** simulated FM voice on the channel made
the receiver commit to four bogus "frames" (48-370 bytes, each leaving it
deaf for that long) with preamble S/N of -12.9 to -60 dB -- the weak-
signal rule only covered claims over 4 s. Every candidate now needs
preamble S/N >= 2 dB (`RX_MIN_PREAMBLE_SNR_DB`), checked before the
header is read. Measured: weakest real frames that still decode ~4.8 dB
(BPSK; QPSK ~9.5); noise candidates never above -1.2; voice -12.9 or
lower. Voice now causes 0 bogus waits.

No on-air change; compatible with v0.7.4-v0.7.6 stations.

- audio_io.py: `DCD_MODES`, fill-tone detect, `sara_busy()`,
  `dcd_state()`, `override_dcd()`, `transmit(ignore_dcd=)`, TUNE ignores
  DCD. ofdm_core.py: `receiving_frame()`, `RX_MIN_PREAMBLE_SNR_DB`.
- sara_pingpong.py: DCD row, pings/pongs ignore DCD, saved DCD mode,
  stuck-busy diagnostic. fm_channel.py: audio paths.
- QUICKSTART section 11. test_offline.py: +10 checks (220, no display).

## v0.7.6 — open-squelch radios: carrier detect by spectrum

Many PAREDN gateways run their radio with squelch at zero, so VARA FM's
input gauge shows activity. Then the radio outputs loud hiss whenever
nobody transmits, and a carrier QUIETS it. To test this properly a
realistic FM channel simulator was added (`fm_channel.py`: transmitter
FM at 5 kHz deviation, RF noise at a chosen CNR, 16 kHz IF, phase
discriminator, 3 kHz radio audio). It reproduces a strong carrier's audio
(correlation 0.90 against the radio-filtered reference), and with no
carrier its hiss arrives ~9 dB LOUDER than a real SARA-PP transmission.

Measured with v0.7.5 on an open-squelch radio:
- Decoding is fine: frames decode at CNR 8-30 dB; 5 minutes of loud hiss
  produced no false frames and no noise candidates.
- Carrier sense was BLIND: the other station's bursts were seen as busy
  0% of the time, strong or weak -- every open-squelch gateway would
  step on incoming bursts. And once a burst had been heard, the noise
  floor (quietest second of the last minute) became the QUIETED
  transmission, so idle hiss then read busy ~75% of the time.

Fixes:
1. **Spectral carrier detect**, the radio noise-squelch principle: FM
   hiss is strongest at high audio frequencies, so with no carrier the
   band above SARA-PP's signal (2.3-2.9 kHz) is full of hiss; any
   carrier quiets it while the signal fills 0.4-1.95 kHz. In-band /
   above-band ratio (median): open-squelch hiss ~+2 dB; SARA-PP bursts
   +17..+27 dB (CNR 12-30), +10..+13 (CNR 6-8); squelch-closed sound-
   card hiss ~0; mains hum ~+16 -- so it is judged against the station's
   own idle baseline (lowest 1-s average over a minute), +8 dB, and OR'd
   with the loudness rule.
2. **Noise floor learns only from idle-sounding seconds** (spectrum near
   its baseline), so quieted transmissions can't become the "floor".
3. **Clipping counts only while a carrier is present**: open-squelch hiss
   that clips is harmless, and "lower the radio volume" advice would
   weaken real signals.

Result (simulated, two 7-frame bursts each): open squelch CNR 30/12/8 dB
-- bursts seen busy 100%, idle hiss 0%, key-up detected within 0-64 ms,
14/14 frames decoded; closed squelch (with and without mains hum)
unchanged. Long waits in the log say "carrier detected by spectrum".

No on-air change: compatible with v0.7.4/v0.7.5; any station whose radio
runs open squelch needs v0.7.6.

- audio_io.py: `spectral_ratio_db()`, `spectral_busy()`, `level_busy()`,
  idle-only noise floor, carrier-gated clip tracking, `last_wait_by`.
- fm_channel.py: new (tests). sara_pingpong.py: wait note.
- QUICKSTART_GATEWAY.txt: open-squelch notes.
- test_offline.py: +8 checks (210 without a display).

## v0.7.5 — network KISS access for remote testing

Until now the KISS server listened on 127.0.0.1 only. For remote testing
(running Winlink Express on one PC against another station's radio over
Tailscale, the AREDN mesh, or a LAN) the bridge can now listen on the
network. No change on the air: fully compatible with v0.7.4 stations;
only a station that should accept remote clients needs it.

KISS has no authentication -- whoever connects can transmit on the radio
under any callsign -- so the design is conservative:
- **Off by default** ("Allow network KISS clients", bridge mode). Off =
  127.0.0.1 only, exactly as before.
- **Allow-list**, default `private`: this PC, Tailscale (100.64.0.0/10),
  AREDN/LAN (10/8, 172.16/12, 192.168/16). Public internet addresses are
  refused unless listed (IPs or CIDR ranges, e.g. `private 44.0.0.0/8`).
  Typos are reported, not ignored.
- **No hijacking.** Previously ANY new connection replaced the current
  client -- harmless on localhost (lets BPQ32 reconnect after a restart),
  but on a network a stray connection or port scan would silently
  disconnect a gateway's BPQ32. Now a connection from a DIFFERENT address
  is refused while a client is connected ("already serving ...", logged);
  a reconnect from the SAME address still takes over, so BPQ32 recovery
  and a remote tester's reconnect both work.
- Starting the bridge with network access logs this PC's IP addresses,
  the allowed ranges, and a no-password warning. Refusals are logged but
  don't overwrite the status line (which keeps showing who's connected).
  Settings are saved; the controls lock while the bridge runs.

Steps for operators: QUICKSTART_GATEWAY.txt section 10.

- bpq_bridge.py: `start(host, allow)`, `parse_allowlist()`,
  `ip_allowed()`, refusal rules.
- sara_pingpong.py: network controls, start logic, saved settings.
- test_offline.py: +7 checks (202 without a display), incl. live
  sockets from stand-in remote addresses.

## v0.7.4 — field-test candidate: shorter in-burst gaps (+13% per window)

**Both stations must run v0.7.4.** An older receiver stays deaf for the
whole post-decode cooldown and would miss frames 2-7 of every burst.

**v0.7.3 field results:** the ping window and Auto-Tune graphics worked
as intended. The DW1IRF-side failures traced to that station's radio
receive audio, not SARA-PP:
- 13:54-13:55: DW1IRF's carrier sense held its pings 30 s on a steady
  -29 dBFS signal while K6IRF's log shows K6IRF transmitted NOTHING in
  that window -- sustained audio from DW1IRF's own radio (squelch opening
  on noise, or another signal on frequency).
- 14:09: K6IRF decoded both of DW1IRF's pings and sent PONGs; DW1IRF
  decoded neither. After the re-tune, with RX gain unchanged, K6IRF's
  frames arrived 15 dB hotter (-2 dBFS) -- radio volume/squelch changed.
- Over 5 minutes after Auto-Tune, K6IRF's level at DW1IRF drifted from
  -12 to -24 dBFS with no gain change, while K6IRF heard DW1IRF at a
  steady -43 dBFS throughout. S/N stayed +22..+38 dB (link fine).

**Throughput, measured from the logs:**
- Downloads (BBS -> client): windows of 7 x 256-byte frames, one every
  2.0 s. Seven is the AX.25 maximum (MAXFRAME <= 7), so this direction
  already sends the most frames per window the protocol allows.
- Uploads (Winlink Express -> BBS): windows of 4 x 148-byte frames --
  Winlink Express's own packet length (~128) and max-frames (4) settings.
  ~512 bytes per round trip vs ~1,650 for downloads: over 3x as many
  turnarounds. A settings change in Winlink Express, not code (see
  QUICKSTART_GATEWAY.txt section 8).
- In each window, ~0.5 s per frame was a filled gap required by the
  receiver's post-decode cooldown.

**Change:** the receiver no longer goes deaf during the 0.35 s cooldown.
It keeps searching but only ACCEPTS a preamble with S/N >= 4 dB, and
stays silent about anything else -- as before. The cooldown exists for
squelch-tail noise bursts, which have no repeating structure: over 643
candidates of five noise kinds (white/radio hiss, clicks, hum, fill-tone
tails) the highest preamble S/N was -1.2 dB. Real frames repeat their
preamble exactly (field: +20..+40 dB; the weakest still-decodable QPSK
frames ~8 dB). A first threshold of 10 dB lost 3 of 28 weak-link frames,
so it was lowered to 4.

With that, the in-burst gap drops from 0.5 s to 0.2 s. Gap chosen by
measurement at the edge of usability (S/N ~9 dB, 336 frames each):
0.5 s 232, 0.3 s 250, 0.2 s 241, 0.12 s 223 decoded (spread ~+/-10),
and a check that the S/N rule itself caused none of the losses. A
7 x 256-byte QPSK window: 13.5 s -> 11.7 s on the air (~13%).

- ofdm_core.py: `COOLDOWN_ACCEPT_SNR_DB`; cooldown searches silently.
- audio_io.py: `BURST_GAP_S` 0.5 -> 0.2.
- test_offline.py: +3 checks (194 with a display).

## v0.7.3 — receiver edge-ringing fix; pings answered while bridged; graphical ping window

**Field results with v0.7.2** (two Winlink downloads via K6IRF-10): both
completed. The signal-anchored carrier sense worked: every long wait now
logged e.g. "channel read -13 dBFS vs busy above -23" -- real traffic
(DW1IRF's bursts) 10 dB over the threshold, no false busy. Session 2 lost
26 s to ONE lost frame: K6IRF missed the BBS's 95-byte proposal, polled,
and BPQ32 (which resends on its own FRACK timer, not on a poll) waited it
out.

**Why that frame was lost -- an artifact of the receiver itself.** At
that moment K6IRF's receiver was waiting on a noise candidate claiming a
plausible 382-byte BPSK frame (valid FEC length, under the 400-byte cap,
3.4 s -- just under the weak-signal rule's 4 s), and the real frame
arrived inside that wait. Investigation (not the first guess -- a
noise-relative gate raise was tried, measured, and dropped because it
hurt weak frames in band-limited noise while barely helping): the
receiver band-pass filters its whole buffer on every poll, and the filter
rings at the buffer's freshly trimmed oldest edge -- measured +10 dB in
the first 64 samples, gone by one symbol. Idle polling trims and
re-filters constantly, so that edge kept looking like a frame start: in
simulated white hiss (K6IRF's case: +31 dB RX gain on sound-card hiss)
EVERY noise candidate past the energy gate sat at buffer position 0, 12-15
dB above the true noise.

**Fix:** every buffer trim keeps one symbol of pre-roll before the point
it used to cut at, and after a trim the preamble search starts past it --
the same audio position as before, so no real preamble is missed or
misaligned (the very start of the audio stream, never trimmed, is still
searched from sample 0). Measured through the real engine receive path:
white hiss at -34 dBFS, noise candidates reaching header decode 44 -> 0
in 4 minutes; weak real frames decode exactly as before in both white and
band-limited noise.

**Pings answered while bridged to BPQ32**, like VARA FM. Recognising a
ping among AX.25 traffic must never misfire (a misread data frame would
silently drop out of a Winlink session); the batch format's first byte
equals MSG_PING, so `protocol.is_pingpong()` requires two genuine,
space-padded callsigns where a batch always has a 0x00/0x01 length byte.
Checked: 0 of 23,940 random AX.25 batches matched. Pings and pongs are
handled by SARA-PP and never forwarded to BPQ32; a ping is answered only
when no session data has moved for 20 s (otherwise logged as received,
not answered), and the PING / Auto-Tune buttons now work in bridge mode
under the same rule.

**Graphical ping window**, modelled on VARA FM's: two towers (you on the
left, the other station on the right), "You hear <them>" boxes (S/N,
level after RX gain) and "<They> hear you" boxes (S/N, their raw input,
from the two-way report), colour-coded with the same thresholds as the
text verdicts. Opens on PING / Auto-Tune, beside the main window; shows
round trip, "No reply", and Auto-Tune's advice; has its own Ping /
Auto-Tune / Close buttons.

Also: the PONG reply thread and the bridge's per-frame TX logging no
longer read Tk variables from worker threads (fragile in Tkinter).

- ofdm_core.py: `EDGE_GUARD`, pre-roll on every trim, guarded search;
  S/N in `fec_uncorrectable` diagnostics.
- protocol.py: `is_pingpong()`. ping_window.py: new. tuning.py: box
  colours.
- sara_pingpong.py: bridge ping handling, session courtesy, ping window,
  thread-safety fixes.
- test_offline.py: +15 checks (193 with a display, 191 without -- the
  window render test is skipped where no display exists).

## v0.7.2 — busy threshold anchored to the received signal

**Field results with v0.7.1** (three Winlink downloads via K6IRF-10):
runs 1 and 3 completed (5108 bytes in 1:56; 5064 bytes in 1:46, the
message itself at 5054 bytes/min, ~674 bps -- close to the v0.6.1 best),
each needing just one REJ. The v0.7.1 protections were visibly working:
no transmission larger than one 256-byte frame (cap enforced), 12
invalid-modulation and 10 implausible-length headers rejected instantly,
8 FEC repairs. Every long carrier-sense wait at K6IRF (9-11 s) lined up
with DW1IRF sending a full 6-8 frame burst -- burst mode holding the ack
off exactly as designed, no step-ons.

**Run 2 failed** with K6IRF sending 13 connect requests in ~56 s. BPQ32
answered every one with a UA, but DW1IRF's SARA-PP transmitted none of
them: its carrier sense held them for **26.4 s**, though only short
connect requests with ~3 s silent gaps were on the air. Both stations'
screenshots showed idle input 1-3 dB under the busy threshold (DW1IRF:
idle -50, busy above -49; K6IRF: idle -34, busy above -31).

Cause: v0.7.0's "busy = noise floor + 6 dB" measures the floor as the
QUIETEST second of the last minute. Real background noise is usually
steady a few dB above its quietest moments, so the threshold landed on
top of ordinary idle noise. Reproduced as a cliff: steady idle noise at
-50 dBFS over a -57 floor read busy 46% of the time; at -49, 96%.

**Fix:** once real frames have been received, the threshold sits midway
(in dB) between the noise floor and the WEAKEST recently received
station -- never closer than 6 dB to the noise, never closer than 10 dB
to that station (its fill tone, ~2.5 dB under its preamble, must still
be heard). With DW1IRF's numbers (-55 floor, -20 dBFS frames) that's
~-37 dBFS: ~18 dB of margin each side instead of ~1 dB over idle noise.
Same reproduction: idle noise from -50 up to -40 dBFS now reads busy 0%,
while a real keyed burst from the -20 dBFS station is heard 100%. Using
the weakest station means a loud own-transmission echo or a strong
neighbor can't push the threshold above a weaker one. Levels expire
after 10 minutes and shift with RX gain changes. Before any frame is
received it falls back to floor + 6 dB.

Long waits (3 s+) now say what the channel read, e.g.
`[waited 9.8s ...]  (channel read -14 dBFS vs busy above -37, noise
floor -55)`, so a log distinguishes real traffic from noise near the
threshold.

- audio_io.py: received-level history, `weakest_signal_dbfs()`,
  anchored `busy_threshold_rms()`, `_wait_clear()` diagnostics,
  `SIGNAL_MARGIN_DB`.
- sara_pingpong.py: long-wait log lines include the channel reading.
- test_offline.py: +8 checks (178 total), including the field
  reproduction.

## v0.7.1 — coalescing cap actually enforced; garbled headers rejected instantly

First BBS/gateway tests with v0.7.0 (Auto-Tune had both stations at
-12 dBFS, S/N +23..27 dB both ways) still showed long stalls. Logs from
both stations plus the BPQ32 monitor pinned down three causes:

**1. The coalescing cap was never enforced for mixed frame sizes (bug).**
`MAX_BATCH_BYTES` (250) was checked only AFTER appending a frame, so a
waiting small frame plus a full-size one went out together. Reproduced:
[130, 252] -> one 388-byte transmission, [236, 236] -> 478, [60, 180,
252] -> 500 -- matching field logs ("386 bytes (2 frames)", "510 bytes
(2 frames)"). The cap exists because ~510-byte transmissions failed
from clock drift in the field (v0.5.4/v0.5.5); long messages mix small
and large frames constantly, so they hit this most. Now flushes BEFORE
adding a frame that would exceed the cap; small frames still combine
(four acks -> one 70-byte transmission).

**2. A garbled header swallowed a real frame (session 1's long pause).**
The BBS sent its `;PM` proposal; at that moment K6IRF's receiver locked
onto a garbled header claiming 836 bytes of 8PSK, waited ~2.5 s for it,
and lost the real frame inside that span. The BBS then waited out its
FRACK before resending. New instant header checks, before committing to
wait:
- Only BPSK/QPSK are accepted. 8PSK hasn't been transmittable since
  v0.5.12, so an 8PSK (or unused value 3) header can only be garbage.
- Claims over `MAX_RX_DATA_BYTES` (400 on air) are implausible. The
  largest real frame is one maximal AX.25 frame alone (PACLEN 256 +
  8-digipeater header) at 382 bytes on air; a capped batch is ~322.
  Field bogus claims were 629-999 bytes.
- Lengths no FEC-encoded frame can have are rejected (field: "invalid
  FEC frame length 18" had only been caught after waiting).
A random garbled header now passes these checks ~17% of the time
instead of ~75%, before the existing confidence/energy checks.

**3. Duplicate connect requests reset the link (session 2's stall) --
a Winlink Express setting, not SARA-PP.** Winlink Express re-sent its
connect request (SABM) after ~3 s, before the BBS's UA could make the
round trip over the air. Each duplicate SABM made BPQ32 reset the link;
its first data frame ("Trying...") was lost in a reset; K6IRF sent
`REJ R0` asking for it; BPQ32 no longer had it. Deadlock. Fix: raise
Winlink Express's frame-acknowledge timeout / retry timer for the packet
session (see QUICKSTART_GATEWAY.txt).

- bpq_bridge.py: cap checked before appending.
- ofdm_core.py: `RX_MODULATIONS`, `MAX_RX_DATA_BYTES`, FEC-length check;
  all instance-overridable so the 8PSK DSP and the weak-signal rule are
  still tested directly.
- test_offline.py: +6 checks (170 total) using the field values; tests
  that used now-impossible claims updated (one 8PSK comparison had
  started passing vacuously and was fixed).

## v0.7.0 — level-independent operation, S/N, Auto-Tune and TUNE

**Field finding:** sharing a sound card with VARA FM, SARA-PP needed its
input >10 dB hotter than VARA could tolerate (VARA's VU into the red);
SARA-PP's level bar sat at maximum while "quality" barely reached 0.5;
too much RX gain made things worse; and the step-on came back with long
messages. All of it traced to the same design flaw: SARA-PP judged
signals against FIXED levels, while VARA judges them against the noise
(S/N).

Measured in simulation before changing anything:
- Carrier sense used a fixed -25.6 dBFS busy threshold. The fill tone
  arrives ~6 dB below the data, so with frames at -28 dBFS the far end
  heard **0%** of a keyed burst -- the step-on. With +30 dB RX gain to
  compensate, amplified noise read busy **100%** of quiet time, so every
  transmission waited up to 30 s.
- The receiver's own low-energy gate was a fixed -33 dBFS: quiet but
  clean frames were dropped before decoding.
- "quality" was a level reading: a frame at 1/10th volume read 0.09 with
  a 36 dB S/N. It pushed users to turn levels up for no benefit.
- The RX bar pinned at full scale by -12 dBFS.

**Fixes:**
1. **S/N on every frame**, from the repeated preamble (signal identical
   between repetitions, noise not; normalized correlation rho gives
   SNR = rho/(1-rho), skipping the edge-tapered first repetition).
   Within ~1-2 dB of true in-band S/N from 10 to ~37 dB, same at full
   and 1/20th level. Plus received level in dBFS. Shown on every RX line
   and as a bold "Last frame" readout with plain verdicts.
2. **Noise-relative carrier sense.** The engine tracks the noise floor
   (quietest 1-second average over the last minute; a burst is 14-23 s,
   so a minute always holds quiet) and "busy" means 6 dB above it.
   Same scenarios: frames at -28 dBFS -> burst heard 100%; +30 dB gain
   -> 0% false busy. Changing RX gain rescales the noise history at once.
3. **Receiver gate may be lowered, never raised**, to 12 dB over that
   floor (hard floor -60 dBFS). The project once tried an adaptive gate
   that sampled ambient audio next to transmissions and crept upward
   until real frames were rejected (reverted, documented in ofdm_core);
   this one uses the minimum-statistics floor and can only be more
   permissive than the old fixed gate. Clean frames now decode down to
   ~-58 dBFS; 80 s of pure noise at two levels produced no false frames.
4. **Long-claim guard by S/N too**: with high gain, amplified noise read
   above the old quality threshold and could claim a ~9 s frame; preamble
   S/N doesn't change with gain. Real frames top out at ~2.7 s.
5. **Fill tone +3.5 dB** (amplitude 0.3 -> 0.45): now at the data's own
   RMS, peaks still well below a frame's.
6. **RX meter in dBFS** (-60..0) with the target zone marked, red zone
   above -3 dBFS, noise floor and busy threshold shown, and a CLIP
   warning from raw (pre-gain) peaks -- gain can't undo clipping.
7. **Two-way ping report.** PONG carries the responder's S/N and RAW
   (pre-RX-gain) level of the received ping, like VARA's ping window.
   One ping shows how they hear you and how you hear them. 22-byte PONG;
   a 20-byte PONG from an older station still parses.
8. **Auto-Tune** (Ping/Pong mode): pings the Target a few times, sets
   RX gain so frames land at -12 dBFS (simulated: one step lands every
   case from -50 to -8 dBFS raw), then reports both directions with
   advice only where a gain setting can't help: their input clipping or
   very quiet (your TX level), poor S/N (the radio path, not a level),
   your own input clipping, gain range exhausted (radio volume), or noise
   too close to signals for reliable carrier sense.
9. **TUNE 10s**: a steady tone at the same RMS as real data, for setting
   levels while watching the other station's meter. It is the fill-tone
   frequency, so it is never mistaken for a frame (safe in bridge mode).

**Compatibility:** bridge traffic is unchanged from v0.6.x. Ping/Pong
and Auto-Tune need v0.7.0 on both stations (a v0.6.x station can't parse
the longer PONG).

- ofdm_core.py: `preamble_snr_db()`, `preamble_level_dbfs()`,
  `DecodedFrame.snr_db/level_dbfs`, `effective_min_energy()`,
  S/N condition on the long-claim guard.
- audio_io.py: noise-floor tracking, `busy_threshold_rms()`, rx_gain
  property, `raw_peak`, `transmit_tone()`, `FILL_AMPLITUDE` 0.45.
- protocol.py: optional PONG report. tuning.py: new.
- sara_pingpong.py: dBFS meter, S/N display, Auto-Tune, TUNE.
- test_offline.py: +20 checks (160 total).

## v0.6.5 — RX gain, TX level, and saved settings (sharing a sound card with VARA FM)

Field setup: SARA-PP and VARA FM on the same gateway, sharing one sound
card and radio interface. Both read the SAME Windows recording level:
VARA wanted it low (its VU showed 0 dB / red at the level SARA-PP had
been run at, and -41 dB when lowered), so there was no single setting
that suited both.

The fix is per-program gain, not a single shared level: set Windows for
VARA, then match SARA-PP with its own controls.

- **RX gain (dB)**, -20 to +40: a digital boost/cut applied the moment
  audio arrives, before the modem, the level meter, and carrier sense.
  Measured on the real receive path with a quiet, VARA-friendly input:
  at 0 dB the frame did not decode AND carrier sense did not notice the
  channel was busy (so SARA-PP would also have transmitted over other
  stations); at +24 dB it decoded at quality 0.87 with carrier sense
  working. Gain can't recover detail lost to a too-quiet or clipping
  sound card, only rescale what arrived.
- **TX level (%)**: replaces the fixed 80% transmit amplitude, so
  SARA-PP's drive can be matched without moving the shared Windows
  playback level VARA depends on. Applies to frames and the fill tone.

**Saved settings.** Previously nothing persisted: every start (and
every gateway reboot) meant re-selecting devices, PTT, GPIO, TX delay,
mode, modulation, and now levels. Saved to `sara_pp_settings.json` next
to the program (written on Start and on close, atomically), restored at
startup. Audio devices are matched by NAME, not list position, because
Windows often renumbers them after a reboot or USB replug; a missing
device is reported in the log rather than silently swapped. A corrupt
settings file falls back to defaults.

Also: the "No RA-Board detected" hint no longer suggests
`pip install hidapi` inside the standalone build, where it doesn't apply.

Steps for operators are in QUICKSTART_GATEWAY.txt, section 7.

- audio_io.py: `AudioEngine.rx_gain`, `AudioEngine.tx_level`,
  `DEFAULT_TX_LEVEL`; `transmit()`/`queue_transmit()` default to
  `tx_level`.
- sara_pingpong.py: RX gain / TX level controls (live), settings
  save/load with name-based device matching.
- test_offline.py: +8 checks (140 total).

## v0.6.4 — test-suite fix for Windows builds (no change to the program)

The first real Windows run of `build_exe.bat` (Python 3.11.9) passed
131 of 132 checks and stopped on one: the frozen-mode log-folder test.
SARA-PP itself was right -- it reported `C:\gateway\SARA-PP` -- but the
test, written on Linux, compared against a hard-coded path without a
drive letter. The expected value is now computed with the same path
functions the program uses, so it agrees on Windows and Linux. No
change to the modem, the GUI, or anything on the air.

## v0.6.3 — standalone Windows build for gateway rollout

Installing on a new PAREDN gateway stopped at "Python was not found."
Rather than install Python on every gateway, SARA-PP can now be built
into a standalone program: Python and every library bundled in one
folder, double-click `SARA-PP.exe`, nothing to install.

**Building (once per release, on any Windows PC with Python 3.12):**

    build_exe.bat

It installs the requirements and PyInstaller, runs the full test
suite (skip with `build_exe.bat notest`), builds `dist\SARA-PP\`,
self-tests the built program, and produces
`SARA-PP-<version>-win64.zip` -- the file to send to gateways. Volunteers
follow `QUICKSTART_GATEWAY.txt` inside it. ~200 MB unzipped (mostly
scipy), normal for a bundled scientific Python app.

**Fixed before shipping**, found by reviewing what changes when frozen
(both confirmed by building a real bundle and running its self-test):
- Logs were written "next to the script" -- inside the bundle's hidden
  internal folder when frozen. They now go next to `SARA-PP.exe`.
- ptt.py's hidapi self-repair runs `sys.executable -m pip ...`. Frozen,
  `sys.executable` is SARA-PP.exe itself, so a CM108 hiccup would have
  launched extra copies of the app. Skipped when frozen (hidapi is
  bundled).

**New for remote support:**
- `SARA-PP.exe --selftest` (or double-click `SARA-PP Selftest.bat`):
  checks bundled libraries, sound devices (fails if none -- e.g. the
  USB interface unplugged), and a full encode -> decode loopback in BPSK
  and QPSK including an FEC repair. No radio needed. Result saved to
  `selftest_result.txt`. Also works as `python sara_pingpong.py
  --selftest`.
- If the program fails to start, the error is saved to
  `sara_pp_crash.log` next to the program for a volunteer to send in.

**Also corrected:** `BPQ32.cfg.example` still carried the original
FRACK=8000 / SLOTTIME=100 / PERSIST=64 / MAXFRAME=4 values, contrary to
everything proven in the field. Now FRACK=20000, SLOTTIME=10,
PERSIST=255, MAXFRAME=7, with the reasons inline and an explicit
warning against KISSOPTIONS=ACKMODE.

- sara_pingpong.py: `app_dir()`, `selftest()`, `main()` with crash log.
- ptt.py: no pip self-repair when frozen.
- New: build_exe.bat, "SARA-PP Selftest.bat", QUICKSTART_GATEWAY.txt,
  .gitignore.
- test_offline.py: +3 checks (132 total) for the frozen-mode fixes.

## v0.6.2 — link speed meter, and the first speed benchmark vs VARA FM

**Field result with v0.6.1:** a Winlink download of 2796 bytes in 29
seconds -- 5727 bytes/minute, **~771 bps effective**. That is 5.1x the
first complete transfer earlier in this project (1120 bytes/min, ~149
bps), and ~1.36x the ~566 bps that PAREDN volunteers get from
unregistered VARA FM. The same log showed zero `fec_uncorrectable`,
zero CRC failures, the 635-byte post-collision phantom gone, and FEC
repairing 6 frames (1-3 bytes each) that would previously have cost a
retry each.

**Speed meter.** In bridge mode a bold line under the bridge controls
now shows, updated every second:

    Speed  RX 771 bps (last 30s)  ·  session 768 bps, 2.7 kB in 0:29     TX 110 bps  ·  0.4 kB

and when the KISS client disconnects, a summary goes into the log:

    Session summary (0:29):  RX 2.7 kB = 771 bps   TX 400 B = 110 bps

What's counted (see throughput.py): AX.25 frame bytes actually
delivered to BPQ32 or actually put on the air -- after FEC + CRC, after
batch unpacking. Not raw audio, not FEC parity, not failed attempts. It
includes AX.25 headers, so it reads a few percent above the
message-only figure Winlink Express prints at the end. Meters reset
when a KISS client connects (a new session). The session figure is the
one to compare between test runs and versions.

**Remaining BPQ32 setting (BBS station):** the v0.6.1 monitor log still
shows polls 8-11s after each 6-7 frame hand-off, while a full window
takes ~14s to air -- that port's FRACK is still ~8000. Setting it to
~20000 removes those mid-burst polls, a free speed gain.

- throughput.py: new `ThroughputMeter` (rolling window + session
  average, young sessions not diluted by the window length).
- sara_pingpong.py: RX/TX counting, live speed line, reset on KISS
  connect, session summary on disconnect.
- test_offline.py: +6 checks (129 total), including a replay of the
  real field session that must read ~771 bps.

## v0.6.1 — keep the channel audibly busy: fill tone + one key-up per burst

Field session (v0.6.0): the other station audibly stepped on the end of
DW1IRF's 256-byte frames, every burst. K6IRF's log showed the exact
pattern each time: two frames received, then
`[waited 1.6s for clear channel]`, then K6IRF transmitting an ack
MID-BURST, then `fec_uncorrectable` -- DW1IRF's next frame destroyed.
(Those were FEC correctly giving up on collision-garbled audio, not
repairs: the session had zero FEC repairs, i.e. the link was otherwise
clean.)

Cause: every frame was its own key-up with a 600ms lead-in of pure
SILENCE. On FM, a keyed transmitter sending silence is inaudible, so
between frames (tail + un-key + re-key + lead-in, ~0.75s) the channel
looked free to energy-based carrier sense. Winlink Express sends
mid-burst acks on its own response timer; K6IRF's carrier sense saw
"clear" in the gap and keyed up; DW1IRF, already committed to its next
frame, talked over it, and K6IRF (deaf while transmitting) lost it.
A different blind spot from v0.5.13's: not two stations deciding at the
same instant, but a keyed station being invisible for most of a second.

Two fixes, both standard packet-TNC practice:

1. **Occupancy fill tone.** Lead-in, tail, and inter-frame gaps carry a
   steady 1031.25 Hz tone instead of silence (real TNCs send flags
   during TXDELAY for the same reason), so a keyed station is audible
   to the far end's carrier sense from the moment it keys. The
   frequency is chosen, not arbitrary: the receiver detects frames by a
   signal repeating every 640 samples, and a tone with a whole number of
   cycles in 640 samples looks like an endless preamble -- verified,
   1200 Hz DID trigger false detections. 1031.25 Hz sits where that
   correlation is exactly zero: no false triggers in 6s of tone. Level
   0.3 x TX gain: ~4x the far end's busy threshold, yet below the data's
   own level (no extra deviation). Also makes VOX key immediately.

2. **Burst mode.** Frames BPQ32 hands over together (a MAXFRAME window)
   go out under ONE key-up, with tone-filled 0.5s gaps (longer than the
   receiver's post-decode cooldown, so no preamble is thrown away), so
   the channel never goes quiet mid-burst. Saves ~0.4s of dead air per
   frame as a side effect.

Carrier-sense give-up raised from 4s to 30s: a 7-frame window now
legitimately holds the channel ~14s (QPSK) / ~23s (BPSK), and a 4s cap
would make the waiting station give up mid-burst and transmit over it.

Verified end to end by driving the real burst transmitter and capturing
exactly the audio it sends: a 7-frame window keys once, the far end's
carrier sense sees zero clear moments anywhere in the keyed period, and
7/7 frames decode with no false candidates.

Compatibility: frames themselves are unchanged from v0.6.0, and a
v0.6.0 receiver decodes burst-mode audio fine. But each station only
protects its OWN transmissions this way, so both should run v0.6.1.

Recommended BPQ32 setting on the BBS side: BPQ32 starts its FRACK timer
when it hands frames to the KISS port, not when they reach the air, and
a full window now takes ~14s to transmit at QPSK. The v0.6.0 monitor log
shows polls 8s after each hand-off, i.e. mid-burst. A port FRACK of
about 20000 avoids those wasted early polls.

- audio_io.py: `FILL_TONE_HZ`, `FILL_AMPLITUDE`, `fill_tone()`;
  `transmit()` lead-in/tail use the fill; new `queue_transmit()` burst
  transmitter (single worker, one key-up per queued run, `BURST_GAP_S`,
  `BURST_MAX_FRAMES`); `CARRIER_SENSE_MAX_WAIT_S` 4 -> 30.
- sara_pingpong.py: bridge mode queues frames to the burst transmitter
  instead of a thread per frame; TX log lines unchanged.
- test_offline.py: +7 checks (123 total): fill tone produces no false
  frames, the 1200 Hz hazard is detected (proving test sensitivity),
  fill audibility margin, and the end-to-end burst checks above.

## v0.6.0 — Forward Error Correction: repair damaged frames instead of dropping them

**Protocol change: both stations must run v0.6.0.** A v0.5.x station
cannot decode v0.6.0 frames and vice versa.

Until now SARA-PP, like standard AX.25, was detect-and-retry only: a
frame with even one bad bit failed its CRC, was dropped, and BPQ32's
ARQ retransmitted it. Each retry costs a full round trip (often ~10s on
this link), and field measurement showed those round trips, not
modulation speed, are the real throughput bottleneck. FEC lets the
receiver REPAIR a frame with a limited number of damaged bytes.

Reed-Solomon over bytes, the same family FX.25 and IL2P add underneath
AX.25. Like those, it lives entirely in the modem below the KISS
boundary: BPQ32 and Winlink Express never see it, and BPQ32's ARQ
remains the recovery mechanism for anything FEC can't repair.

- 24 parity bytes per block (repairs up to 12 bad bytes per block),
  blocks of up to 231 data bytes, sized evenly.
- Byte interleaving across blocks: a burst of consecutive damaged bytes
  on the air is spread across all blocks rather than exhausting one, so
  a 2-block frame survives a contiguous burst twice what one block
  could repair.
- The CRC-16 stays, checked AFTER correction: a rare RS mis-correction
  is still caught rather than forwarded. Verified: randomized heavy
  corruption never produces an accepted wrong payload.
- Hand-written codec (`fec.py`), so no new package for PAREDN stations
  to install. Verified during development as byte-identical to the
  independent `reedsolo` library's encoder, with exact correction of
  every within-capacity error pattern tested.
- Decode costs ~1ms per frame, far inside the audio callback's ~21ms
  real-time budget.

Parity level chosen by measurement (256-byte QPSK frames, 30 trials per
condition; "old" = frames that would have survived without FEC):

| Damage | old | 16 parity | 24 parity | 32 parity |
|---|---|---|---|---|
| noise sigma=0.32 | 5/30 | 30/30 | 30/30 | 30/30 |
| noise sigma=0.40 | 0/30 | 23/30 | 28/30 | 30/30 |
| 4-symbol interference burst | 2/30 | 30/30 | 30/30 | 30/30 |
| 8-symbol interference burst | 0/30 | 30/30 | 30/30 | 30/30 |
| 22-symbol interference burst | 0/30 | 15/30 | 30/30 | 30/30 |

24 over 16 costs ~3% more airtime (~67ms on a 256-byte frame); one
rescued frame saves a ~10s retry, so it pays for itself rescuing even
~1 extra frame in 100. Overall FEC overhead: a 256-byte batch goes from
258 to 306 bytes on air, a short ack from 21 to 45 bytes.

What FEC does NOT fix, stated plainly:
- Half-duplex collisions at the receiving station. When a station keys
  up, its own receiver is muted for its entire transmission including
  the TX lead-in, wiping out far more of the other station's frame than
  any code can repair. v0.5.13's carrier-sense jitter addresses that.
- Clock drift past the ~90 degree cliff: the damage spans the whole
  remainder of the frame. The 250-byte coalescing cap still governs
  that, and was re-validated with FEC's longer frames (still 5/5
  through 150ppm in both modulations), so it stays unchanged.

On the air: any RX line ending in `[FEC repaired N bytes]` is a frame
that v0.5.x would have dropped, costing a retry. That count is the
direct field measure of what FEC is buying.

- fec.py: new module.
- ofdm_core.py: `frame_encode` FEC-encodes payload+CRC (the header's
  length field now carries the on-air encoded size); the receiver
  FEC-decodes before the CRC check; new `fec_uncorrectable` rejection;
  `DecodedFrame.fec_corrected`.
- sara_pingpong.py: `[FEC repaired N bytes]` on RX log lines, `fec=N`
  in the CSV note.
- test_offline.py: +6 checks (116 total): codec correctness, length
  inverse, interleaving, end-to-end gain through the real OFDM link.
  The old "corrupted frame is rejected" test now asserts the stronger,
  correct invariant (repaired or rejected, never a wrong payload), and
  two header-overlap tests were recalibrated for the new frame encoding.

## v0.5.13 — closed the TX-lead blind window that caused a directly-observed collision

A real collision was directly OBSERVED on the radio -- not inferred
from logs, watched happening on the monitor: K6IRF keyed up and
transmitted a 19-byte frame while DW1IRF was still mid-transmission on
a 256-byte frame, stepping on it. Cross-checking both stations' full
session logs confirmed a real, substantial effect: of 84 real 256-byte
frames DW1IRF sent in one session, only 50 showed up as successfully
received on K6IRF's side -- a ~40% miss rate, independent of any
logging/clock-offset question.

Root cause: carrier sense (v0.5.0) checks the channel once, before the
TX_LEAD silence (300-600ms, needed for PTT/relay settling) -- not
after it. During that silent lead-in, a station is committed to
transmitting but produces no audible signal yet. If the OTHER
station's own check happens to land in that exact window, the channel
still looks clear to it too, and it commits as well -- both stations
end up committed before either one's actual signal is audible to the
other. A real, structural gap in "check once, then go" carrier sense,
not a sensitivity problem -- the classic reason real AX.25 systems use
randomized backoff (this project's own BPQ32 configs already carry
PERSIST/SLOTTIME for exactly this) rather than a single check.

Added a short random jitter (`CSMA_JITTER_MAX_S`, up to 0.3s) placed
as close to PTT as the code gets, with a re-check of the channel
immediately after: two stations that both saw "clear" at the same
instant (e.g. both reacting to the same prior transmission ending) now
commit at slightly different, randomized moments instead of the
identical one, giving whichever jitter is shorter a real head start --
audible to the other before its own commit point, which the re-check
then correctly catches as busy.

Verified directly, not just reasoned about: a channel that's clear at
the initial check but becomes busy specifically during the jitter
window (simulating the other station starting its own transmission in
that gap) is caught by the re-check and waited out, confirmed with a
deterministic test forcing exactly that sequence -- not left to chance
timing in the test itself.

- audio_io.py: `CSMA_JITTER_MAX_S`; `AudioEngine.transmit()` now adds
  the jitter and re-checks the channel immediately before PTT, not
  just once at the top of the function.
- test_offline.py: +1 test (108 total) -- the blind-window collision
  scenario reproduced deterministically and caught, the common
  clear-channel case still proceeds to PTT without being blocked, and
  the jitter value's bounds are confirmed.

## v0.5.12 — 8-PSK confirmed non-functional on real hardware; retired from the UI

v0.5.11 shipped 8-PSK as a selectable, fully-tested option, with an
honest synthetic finding that it already had LESS drift margin than
QPSK, not more -- a loss on paper even before real-world testing. Real
hardware testing confirmed it outright: in Ping/Pong mode, zero Pong
replies ever came back; in BPQ32 bridge mode, real exchanges simply
stopped and the session disconnected. Not occasional unreliability --
apparently unable to survive this radio path's real, non-drift
impairments (limiting, compression, frequency-response rolloff, general
noise) at all, on top of the angular margin it was already short on.

Considered and declined building adaptive modulation (start robust,
step up once the link proves itself, the way VARA FM varies its speed)
specifically for 8-PSK: that architecture rescues a mode that works but
is merely riskier under some conditions. 8-PSK isn't that -- it appears
to fail every single time regardless of how good the link is, so
switching into it mid-session wouldn't trade some risk for more speed,
it would just break the link at that exact moment. Adaptive switching
can't rescue a mode that doesn't decode at all; it only delays finding
out.

Retired from the GUI's selectable choices so it can't be accidentally
picked again. The DSP core (`MOD_8PSK`, the Gray-coded constellation,
decode support) stays in place and tested -- a received 8-PSK frame,
however it got sent, is still decoded and labeled correctly -- it's
only no longer offered as something to transmit with.

- sara_pingpong.py: `8PSK` removed from the modulation dropdown's
  choices; `_MOD_NAME_TO_CODE` keeps the mapping (for correctly
  labeling a received frame), with the reasoning for the asymmetry
  documented at both.

## v0.5.11 — 8-PSK, implemented and tested, but not the throughput win hoped for

With the physical layer reliable, the next lever considered was higher-
order modulation: 8-PSK packs 3 bits/carrier (36 bits/symbol) against
QPSK's 2 (24 bits/symbol), and a denser constellation sends a given
payload in less time -- hypothesized to leave LESS time for clock drift
to accumulate, potentially allowing a larger safe coalescing cap than
QPSK's established 250 bytes, not just faster transmission at the same
size.

Implemented properly: `MOD_8PSK`, a Gray-coded 8-point constellation
(adjacent points differ by exactly one bit, so a small angular error
costs one bit, not more), and the header's modulation field widened
from 1 bit to 2 (three live modes now need 2 bits to select), which
required shrinking `MAX_PAYLOAD_LEN` from 2047 to 1023 bytes -- still
comfortably above this project's real ~250-byte coalescing cap. Clean
round-trip tests passed immediately.

Rigorous multi-trial drift testing -- the same methodology used to
validate every other drift-sensitive constant in this project -- did
not bear out the hypothesis. 8-PSK's constellation points are 45
degrees apart (vs QPSK's 90), roughly halving the angular margin before
a decision crosses into the wrong point, and that cost more than the
shorter transmission time saved. Measured directly: at the identical
254-byte payload, QPSK's drift cliff sits around 170ppm, 8-PSK's around
150ppm. At QPSK's own established 250-byte cap specifically, 8-PSK only
held reliably (5/5 multi-trial) through 100ppm, dropping to 1/5 by
130ppm -- a real, measured reliability loss, not a hypothetical one.
8-PSK's OWN genuinely safe cap (5/5 through 150ppm, the same rigor
QPSK's cap was held to) measured around 150 bytes -- smaller than
QPSK's, not larger. Net real-world throughput including realistic fixed
per-transmission overhead (TX lead + tail): QPSK @ 250B works out to
~129 bytes/sec per transmission against 8-PSK @ its own 150B cap at
~118 bytes/sec -- 8-PSK comes out slightly BEHIND, not ahead. And since
this project's actual observed real-world throughput (~18.7 bytes/sec,
from the first completed real transfer) sits far below either number,
the real bottleneck is protocol-level round trips, not modulation
speed -- meaning 8-PSK would not have meaningfully helped real transfers
even had it won the physical-layer comparison.

Kept available rather than discarded: fully implemented, tested, and
selectable in the GUI's modulation dropdown, since the math could favor
it on a cleaner link with less real drift than this project's hardware
has shown, and the hypothesis itself was reasonable to test. Not wired
as bpq_bridge.py's default, and `MAX_BATCH_BYTES` was not raised on the
strength of it -- an honest negative result, not a shipped "win."

Also fixed in passing: a real, previously-latent bug in the TEST SUITE
itself (`bytes(int64_array)` reads raw memory -- 8 bytes per element --
not element values, so several tests were silently testing payloads 8x
larger than their own comments claimed; masked until now because the
old, larger `MAX_PAYLOAD_LEN` happened to still fit the inflated sizes).
Fixed with the correct `.astype(np.uint8).tobytes()` pattern throughout,
and the header-corruption test's corruption-strength thresholds were
recalibrated against the now-correctly-sized payloads.

- ofdm_core.py: `MOD_8PSK`, Gray-coded constellation (`_bits_to_8psk`,
  `_8psk_values_to_bits`), `HEADER_MOD_BITS` (header's modulation field
  1 bit -> 2), `MAX_PAYLOAD_LEN` 2047 -> 1023 bytes.
- sara_pingpong.py: modulation dropdown gains "8PSK"; four scattered
  binary BPSK/QPSK name mappings consolidated into one
  `_mod_name()`/`_MOD_NAME_TO_CODE` pair that handles all three.
- test_offline.py: +1 test (105 total) -- clean 8-PSK round trip, the
  core finding (8-PSK's drift cliff measurably below QPSK's at the same
  payload size), and 8-PSK's own 150-byte cap validated at the same
  multi-trial rigor as every other constant. Fixed the latent
  8x-oversized-payload bug in three existing tests and recalibrated
  the header-corruption gain thresholds against the correction.

## v0.5.10 — a recurring weak false candidate cost 3+ minutes per session

With v0.5.9 on both stations, a real field session showed substantial,
genuine progress for the first time -- the AX.25 ack sequence wrapped
through R1-R7 repeatedly, meaning dozens of real frames got through,
not just the first one of each burst. Verbose RX diagnostics confirmed
v0.5.9's checks were firing in the field exactly as designed (7
low_header_confidence, 3 header_energy_mismatch catches). But the
transfer still didn't complete, and the remaining `crc_fail` entries
split cleanly into two very different categories:

- Four at quality 0.95-0.96, matching real traffic closely (one even
  exactly 256+2 bytes) -- genuinely real signal, occasional bit-level
  loss, exactly what BPQ32's own ARQ exists to retry. Not a problem.
- Fourteen at quality 0.035-0.051 -- real signals on this link have
  never measured below 0.18 in any field log this project has seen --
  all claiming the identical n_data_bytes=1659 at BPSK, a 14.84s wait.
  14 occurrences cost over 3 cumulative minutes of the receiver blind
  to every genuine frame arriving during each wait -- a bigger total
  drag on this session than anything v0.5.9 addressed.

v0.5.9's checks compare a frame's header against its OWN preamble (a
mismatch between the two); this is different -- both are uniformly
weak, consistent with plain noise self-correlating at the preamble lag
rather than a real signal's header being corrupted. A separate,
targeted check: a candidate this weak claiming a long wait is rejected
immediately rather than committed to. Calibrated with real margin on
both sides -- the lowest real-field quality this project has observed
(0.18) sits comfortably above the false-positive range (0.035-0.051)
this exists for, and the wait-duration threshold (4s) comfortably
exceeds a real full-size coalesced frame's own duration (~1.65-3.2s),
so only the combination of weak AND long gets the extra scrutiny.

- ofdm_core.py: `WEAK_SIGNAL_QUALITY_MAX`, `MAX_WAIT_FOR_WEAK_SIGNAL_S`;
  checked in `poll()` right after a candidate's implied wait duration is
  known, before the broader implausible_length check.
- test_offline.py: +1 test (100 total) -- the exact field scenario
  reconstructed directly from its numbers is caught immediately; a
  genuine weak-but-real signal at the project's lowest-ever observed
  field quality still decodes; a weak signal making only a short claim
  is correctly left alone.

## v0.5.9 — header-only corruption could blind the receiver for ~7 seconds

With v0.5.8 confirmed working (a full AX.25 monitor trace showed zero
self-echo instances), a new, cleaner pattern emerged: the first frame
of each rapid-fire burst decoded fine, every one after it vanished.
Verbose RX diagnostics caught the actual mechanism directly:

```
[rejected: crc_fail  n_data_bytes=768, modulation=0 (BPSK), quality=0.953]
```

DW1IRF's own log confirmed it only ever sent 256-byte QPSK frames --
768 (close to 3x256) at BPSK was never real. A brief overlap (most
likely the other station's own short transmission) landing specifically
on the header symbol -- not the rest of the frame, which stayed clean
enough to give that high 0.95 quality -- corrupted just enough to
produce a bogus but structurally-valid modulation+length. The existing
checks only reject claims that are out of range or exceed the buffer's
capacity; a plausible-looking wrong length sailed through them. The
receiver then committed to waiting out that claim's entire duration
(~7s for a BPSK claim that size) before even attempting a decode and
finally rejecting it -- and every genuine frame arriving during that
multi-second blind spot was missed entirely, which is exactly why only
the first frame of each burst got through.

Two complementary checks added, each verified against a direct,
reproducible simulation of the real mechanism (not just inferred from
the log):
- `_header_confidence`: a soft-decision check -- corruption strong
  enough to push a header bit decision ambiguously close to the BPSK
  boundary is caught here.
- `header_energy_ratio`: corruption strong enough to flip a bit
  CONFIDENTLY to the wrong value is invisible to the confidence check
  (a strong wrong decision sits just as far from the boundary as a
  strong right one) -- but an overlapping signal still shows up as
  extra energy at that one symbol relative to the clean preamble, which
  this catches instead.
Calibrated empirically: clean headers measure confidence ~0.9-0.98 and
energy ratio 0.48-1.32 across 60+ varied trials; the two failure modes
these exist for measured confidence 0.06 and energy ratio 2.38-3.54
respectively -- comfortable separation in both directions, with zero
false positives against clean, varied traffic in testing.

- ofdm_core.py: `OFDMReceiver._header_confidence()`,
  `HEADER_CONFIDENCE_MIN`, `HEADER_ENERGY_RATIO_MAX`; both checked in
  `poll()` right after the header is located, before committing to
  anything based on its claimed length.
- test_offline.py: +1 test (95 total) -- clean traffic never trips
  either check (60 varied trials), a direct reproduction of both real
  failure mechanisms (ambiguous and confidently-wrong corruption) is
  caught immediately rather than after a long wait, and a mild overlap
  too weak to actually corrupt the header is correctly left alone.

## v0.5.8 — self-echo was timestamped at the wrong moment entirely

A low-level AX.25 monitor trace from BPQ32 itself (not just our own
bridge log) settled a question v0.5.6 and v0.5.7 hadn't fully answered:
DW1IRF's own BPQ32 was still receiving exact copies of its own earlier
frames back -- 12+ seconds after they were originally sent, labeled as
if from the remote station. Both the self-echo window (v0.5.6) and its
thread-safety (v0.5.7) were real, legitimate fixes, but neither was the
full story.

Root cause: `record_sent()` was called the instant `_bridge_transmit`
started -- essentially when `on_air_frame` spawned each transmission's
thread, all at once as BPQ32 hands over a burst. The ACTUAL broadcast
(and thus the actual echo) only happens once that thread wins its turn
at `AudioEngine`'s shared `_tx_lock`. With the smaller coalescing cap
(v0.5.5), a full BPQ32 MAXFRAME=7 burst now sends each real ~256-byte
frame as its own transmission, each in its own thread, all queuing
behind one another. A frame near the end of that queue could have its
`record_sent()` call happen immediately, while its real transmission
(and echo) didn't happen until 10+ seconds later, once six other
transmissions had each taken their turn -- blowing past even the
widened window, because the window was measuring from the wrong
starting point the whole time.

Fixed by adding `AudioEngine.on_tx_start`, fired from inside
`transmit()` only once a thread actually holds `_tx_lock` and is about
to key PTT -- the real broadcast moment. `record_sent()` now happens
from that hook instead of before `transmit()` is even called.

- audio_io.py: `AudioEngine.on_tx_start` hook, fired at the real
  broadcast moment inside the `_tx_lock`-protected section.
- sara_pingpong.py: wires `engine.on_tx_start =
  self.self_echo_filter.record_sent`; `_bridge_transmit` no longer
  calls `record_sent` itself.
- test_offline.py: +1 test (91 total) -- confirms the hook fires once
  per transmission, correctly reflecting actual lock-acquisition order
  rather than thread-spawn order, when several transmissions are
  queued and serialized behind one another.

## v0.5.7 — the self-echo filter wasn't thread-safe

A field log with v0.5.6's wider window showed real progress again but
a new, more specific pattern: within each burst of 256-byte real data
frames, the first several were correctly tagged `[own TX heard
locally]`, but consistently the LAST one or two were not -- decoded and
forwarded to BPQ32 as if real. This pointed somewhere different from
the window itself.

`sara_pingpong.py`'s `on_air_frame` spawns a brand-new thread for every
single transmission from BPQ32. Once BPQ32 sends frames close together
(which real bursts do throughout, not rarely), multiple
`_bridge_transmit` calls run concurrently -- all sharing one
`SelfEchoFilter` instance that was never made thread-safe. Its
`_recent` list had a classic read-modify-write race: one thread's
`_prune()` computes a filtered snapshot, and if another thread's
`record_sent()` appends an entry before the first thread's delayed
reassignment lands, that fresh entry is silently discarded the moment
the stale snapshot overwrites it. Confirmed the exact mechanism with a
deliberately adversarial timing reproduction before trusting it as the
cause -- not just assumed from the pattern. The race is too narrow to
reliably trigger under the GIL's normal fast scheduling, which explains
why only the busiest bursts (more frames in flight at once) showed it,
and consistently near the end where overlap had accumulated.

Fixed by protecting `SelfEchoFilter`'s shared state with a lock, so
`record_sent()` and `is_echo()` from different threads correctly
serialize instead of interleaving.

- sara_pingpong.py: `SelfEchoFilter` now holds a `threading.Lock()`
  around all access to `_recent`.
- test_offline.py: +1 test (90 total) -- reaches into the real, now-
  locked method and injects the exact adversarial delay that caused
  loss in the unlocked version, confirming the lock forces correct
  serialization rather than relying on luck to avoid the race.

## v0.5.6 — the self-echo filter's window was shorter than real transmissions

A field log with v0.5.5 showed real, incremental progress (Winlink
Express reached "Receiving...", and audible collisions dropped to just
once per run instead of several) -- but also something new: every
19-63 byte self-heard echo was correctly tagged `[own TX heard
locally]`, while every single 256-byte one was not, decoded and
forwarded to the station's own BPQ32 as if it were real incoming data.

The cause: `SelfEchoFilter`'s window was 2.0s, validated against short
ping/pong-style traffic. A 256-byte frame's own transmission (lead-in +
burst + tail) takes ~1.96s on its own -- almost the entire window
consumed before the echo is even fully received and decoded, with zero
margin left for decode latency, let alone for carrier sense (v0.5.0)
potentially adding up to another 4s of wait before the burst even
starts. Small control frames transmit fast enough to fit comfortably
within the old window; real data frames, once coalescing and BPQ32
activity started producing them regularly, did not.

Resized to 10.0s -- comfortably above the worst realistic total delay
(a maximal coalesced batch at BPSK, the slower modulation, plus the
full carrier-sense wait, plus lead/tail), computed at under 8s.

- sara_pingpong.py: `SelfEchoFilter`'s default `window_s` 2.0 → 10.0,
  with the full timing math documented at the class itself.
- test_offline.py: +1 test (89 total) -- reproduces the bug directly
  with the old window (confirming it was real, not assumed) and
  confirms the new default survives the worst realistic delay.

## v0.5.5 — tighter margin, and real collisions confirmed separately

With both stations on v0.5.4, the attachment transfer still failed --
but this time the evidence split cleanly into two distinct, confirmed
problems, not one.

**Real over-the-air collisions, despite carrier sense.** K6IRF's log
showed `crc_fail` candidates claiming sizes (1008, 1633, 1231 bytes)
that DW1IRF's own log confirms it never actually transmitted (DW1IRF
only ever sent 510-byte batches this session) -- the signature of two
stations' audio mixing together, the garbled result occasionally
resembling a valid preamble+header with an essentially random claimed
length. This matches a field report earlier in the same session of
audibly hearing one station's TX step on the other's. Carrier sense
(v0.5.0) reduces this but can't eliminate it: it reacts to energy that
has already arrived, so a narrow window around the exact moment both
stations decide to transmit remains a real, inherent gap in any simple
energy-based carrier sense -- the same reason real AX.25 systems lean
on retry/ARQ rather than expecting zero collisions.

**Separately, genuine margin was still too thin.** A frame that *did*
match DW1IRF's real 510-byte transmission exactly (`n_data_bytes=512`
= 510 + the 2-byte frame CRC) still failed, with `drift_deg=-89.975` --
right on the same ~90° cliff v0.5.4 was built to stay clear of. v0.5.4's
500-byte cap was validated with a single deterministic trial per
ppm level; real crystal oscillators drift with temperature and warm-up
time, so a single clean trial isn't the same as a robust margin.

Re-validated this time with multiple trials per size/ppm combination,
each at a different sub-symbol timing offset (since one deterministic
resample() call can land luckily or unluckily right at a boundary).
`MAX_BATCH_BYTES` reduced from 500 to 250 -- passing every trial (5/5)
through 150ppm in both modulations, roughly double the ~80-100ppm the
second field test suggests this hardware can actually produce.

Honest trade-off: at 250 bytes, a full 236-byte BPQ32 frame barely
coalesces with anything else -- most individual real data frames will
now go out close to on their own. Coalescing still meaningfully helps
the many small control/handshake frames (15-65 bytes each) that
motivated it in the first place; the throughput gain for bulk transfers
specifically is much smaller than before. That's the right trade given
tonight's evidence -- reliability over squeezing out transmission
count, especially since the collision finding means some retries are
simply inherent to this link now, same as any other packet radio
system, and BPQ32's own ARQ (`RETRIES`, `FRACK`) is what's meant to
absorb them.

- bpq_bridge.py: `MAX_BATCH_BYTES` 500 → 250, full reasoning and both
  rounds of field evidence documented at the constant itself.
- test_offline.py: the drift-safety regression test now checks 100 and
  130ppm (above the second round's observed ~80-100ppm) rather than the
  original round's ~30-60ppm, and is multi-trial by construction
  (varies sub-symbol offset) rather than a single deterministic check.

## v0.5.4 — real clock drift exceeded correction once transmissions got long enough

v0.5.3 fixed the buffer being too small to even HOLD a full-MAXFRAME
coalesced transmission -- but with both stations confirmed running
v0.5.3, the attachment transfer still failed, and this time with
something genuinely new: Verbose RX diagnostics showed the 1780-byte
batch arriving and being fully captured, but failing CRC with large,
erratic `drift_deg` values across retries (-110°, 170°, 89°, 134°,
24°, 8°, -138°, -92°, 149°) despite excellent quality (~0.95) on every
attempt -- not noise, not a false lock, a real signal failing to
decode correctly.

Directly instrumented the per-symbol pilot tracking (v0.4.7) against
this exact scenario: it turned out to be measuring drift smoothly and
accurately the entire way through the transmission -- a clean, linear
phase trace from -0.7° to -94° across 580 symbols, tracking real
clock drift correctly symbol by symbol. The tracking was never broken.
The problem is more fundamental: once accumulated phase drift crosses
roughly 90° (where QPSK's constellation quadrants are decided), the
underlying bit decisions start flipping regardless of how accurately
that drift is being measured -- correction can follow the drift, but
can't prevent the decision boundary itself from being crossed once
total accumulated rotation gets that large. A transmission's maximum
safe duration is fundamentally bounded by how fast real drift
accumulates, not by how well it's tracked.

Confirmed empirically: at this transmission's actual length (~8s at
QPSK), reliable decoding broke down starting around just 30ppm of
clock drift -- a level this project's own hardware has now
demonstrably shown, and one that was comfortably fine at the ~1.2s
duration (85 symbols) this fix was originally validated against. The
same drift rate over a longer transmission accumulates proportionally
more total rotation -- exactly what real constant-rate clock mismatch
physics predicts.

Fixed in `bpq_bridge.py`, not `ofdm_core.py`: `MAX_BATCH_BYTES` reduced
from 1900 to 500 -- bounding how large a single coalesced transmission
is allowed to get, independent of `ofdm_core`'s receive buffer (which
stays large, sized to the full protocol maximum, since that's a
different concern: being ABLE to hold whatever legitimately arrives,
not choosing how much to send at once). 500 bytes keeps even BPSK (the
slower modulation, so the longer duration for a given byte count)
reliable through 80ppm in direct testing -- a real margin above the
~20-30ppm this hardware has actually shown, not just an untested
assumption like the previous cap's larger headroom.

- bpq_bridge.py: `MAX_BATCH_BYTES` 1900 → 500, with the full reasoning
  and field evidence documented at the constant itself.
- test_offline.py: +2 tests (87 total) -- a realistic maximum coalesced
  batch (bounded by the new cap, not the old one) survives 30ppm and
  60ppm drift in both modulations, and the bridge's own flush logic
  actually produces multiple smaller batches across a longer burst
  rather than one large one.

## v0.5.3 — coalescing + a full MAXFRAME window silently exceeded the buffer

Three-way log comparison (DW1IRF's bridge, K6IRF's bridge, and BPQ32's
own BBS session log, all from the same window) found something neither
log alone could show: DW1IRF repeatedly transmitted `1780 bytes (7
frames)` -- and K6IRF's log showed NOTHING corresponding to it. Not a
rejected candidate, not corruption -- no receive log entry at all. The
BPQ32.cfg SLOTTIME/PERSIST fix (recommended a few versions back) had
worked exactly as intended: BPQ32 was finally using its full MAXFRAME=7
window instead of capping around 4. But bpq_bridge.py's own frame
coalescing (v0.4.9) then bundled all 7 of those 236-byte AX.25 frames
into ONE OFDM transmission -- 1780 bytes, needing ~8s at QPSK, ~16s at
BPSK. The receive buffer at the time (MAX_BUF_SYMS, sized for "any
realistic BPQ32 PACLEN," ~700 bytes / 6.6s, on the theory that the full
2047-byte header maximum was a theoretical ceiling no real client would
approach) was too small to ever hold enough of this transmission to
attempt a decode -- the front was silently discarded before a complete
decode was even possible, every single time, hence zero receive log
entries across multiple retries spanning minutes.

That "no real client sends that much" assumption was the bug: frame
coalescing is specifically designed to combine several small frames
into one transmission, so once BPQ32 was sending a full window, hitting
close to the protocol's own header ceiling was inevitable, not
theoretical.

Fixed by sizing MAX_BUF_SYMS to the full MAX_PAYLOAD_LEN (2047 bytes,
the actual ceiling the 11-bit header length field can express) at BPSK
(the slower, longer-duration mode) with margin -- eliminating this
class of mismatch regardless of what bpq_bridge.py's own batching
limits are set to, rather than chasing today's specific 1780-byte
number. Benchmarked at this size: ~12-16ms per `sosfiltfilt` call,
comfortably inside the ~20ms real-time budget, and this size is only
reached transiently while genuinely receiving a very long frame, not as
a steady-state cost.

- ofdm_core.py: `MAX_BUF_SYMS` resized from a "realistic PACLEN" guess
  to the protocol's full theoretical maximum.
- test_offline.py: the old "claiming the max length gets rejected as
  implausible" test now (correctly) asserts the opposite -- that
  claiming the max is legitimately satisfiable, not implausible,
  because the buffer can now actually hold it. Added a direct
  end-to-end regression guard reproducing the real failure: a
  bpq_bridge-style coalesced batch at a full `MAX_BATCH_FRAMES` window
  (1906 bytes) decodes correctly in both modulations (82 tests total).

## v0.5.2 — bridge mode was forwarding its own echo to BPQ32

A field log from K6IRF's own BBS session revealed something distinct
from the carrier-sense question v0.5.1 was checking for: every single
`TX (BPQ32->air)` was immediately followed by an `RX (air->BPQ32)` of
the identical byte count, at a visibly lower quality (0.26) than
genuine remote traffic (0.96-0.97) -- local audio bleed-back of the
station's own transmission. Ping/Pong mode has always filtered exactly
this (comparing callsigns in the decoded ping/pong message), but
BPQ32/KISS Bridge mode never got the same protection -- it has no
callsign concept at all, since it only ever sees raw AX.25 bytes. Every
one of these self-heard echoes was being decoded successfully and
forwarded straight to the station's own local BPQ32, which would see
its own just-sent frame echoed back as if the other station had sent
it -- exactly the kind of thing that thoroughly confuses AX.25 session
state, independent of anything actually wrong with the over-air link.

Added `SelfEchoFilter`: protocol-agnostic, since Bridge mode has no
callsign to compare -- it works by comparing a decoded frame's raw
bytes against a short (2s) history of what this station itself just
transmitted. An echo is, by definition, an exact byte-for-byte copy of
something we just sent, so this catches it regardless of what protocol
is actually inside those bytes. A caught echo is logged distinctly
(`[own TX heard locally -- not forwarded to BPQ32]`) rather than being
silently dropped, so it's visible but clearly not mistaken for a
successful remote exchange.

- sara_pingpong.py: `SelfEchoFilter` class; `_bridge_transmit` records
  every sent payload, `_handle_air_frame_for_bridge` checks incoming
  frames against it before forwarding to the bridge.
- test_offline.py: +1 test (80 total) -- exact-match detection within
  the window, no false positives on different payloads or genuine
  remote traffic, and correct expiry once the window passes.

## v0.5.1 — carrier-sense wait is now visible in the log

Field report right after v0.5.0: downloading consistently failed while
sending worked, and the receiving station's bridge log showed a real,
apparently normal handshake followed by NO bulk data ever arriving --
just small frames trading back and forth until BPQ32 gave up and
disconnected. v0.5.0 added carrier sense with zero visibility into it:
if one station's local noise floor sits persistently above
`CARRIER_BUSY_RMS` (self-heard bleed, radio hiss, anything), every one
of ITS transmissions could be eating a meaningful chunk of the 4-second
wait before anything looked obviously wrong anywhere in the log -- the
receiving station would just never see the sender's data arrive, with
no way to tell why from its own side.

`AudioEngine.on_carrier_wait` now reports any wait over 0.3s (quiet on
the common, already-clear case so this doesn't spam normal operation),
logged directly in the GUI without needing Verbose RX diagnostics
turned on -- this is directly actionable regardless of verbose mode.

This doesn't yet confirm carrier sense IS the cause of the reported
download failures -- it makes it checkable. The next test will show
directly whether the sending station is burning real time waiting to
transmit, or whether this is something else entirely.

- audio_io.py: `AudioEngine.on_carrier_wait` hook; `transmit()` reports
  any wait over 0.3s.
- sara_pingpong.py: logs `[waited X.Xs for clear channel before
  transmitting]` whenever it fires.
- test_offline.py: +1 test (74 total) -- confirms the hook fires for a
  real wait and stays silent when the channel was already clear.

## v0.5.0 — carrier sense: stop stepping on the other station

Field report: with a real B2F handshake underway, one station's TX
audibly stepped on the other's from time to time. That pointed at a
real architectural gap, not a tuning issue -- the bridge had no carrier
sense at all. Whenever BPQ32 handed it a frame (after coalescing), it
keyed up and transmitted immediately, with no check for whether the
channel was already occupied by the other station. Two independently-
operating half-duplex stations with no coordination will eventually
transmit at the same moment; when that happens both signals garble each
other, forcing BPQ32 into an ARQ retry/timeout cycle -- costing far more
time than a brief wait ever would have. (Ping/pong mode never showed
this because one side's own transmit lock already prevents it from
colliding with itself; it's specifically a two-independent-stations
problem, exactly BPQ32's situation.)

Real AX.25 systems solve this with carrier sense -- and this project's
own BPQ32 config already carries `PERSIST`/`SLOTTIME` values meant for
exactly that, which the bridge had been ignoring entirely. Added a
simple RMS-level carrier sense in `audio_io.py` (closer to real-world
DCD than full AX.25 p-persistent CSMA): before every transmission,
`AudioEngine.transmit()` now waits for the recent RX level to drop
below `CARRIER_BUSY_RMS` (calibrated to quality ~0.12 -- comfortably
above the ambient-noise territory `ofdm_core`'s own energy gate guards
against, comfortably below any real signal this project has observed in
the field), polling briefly rather than keying up blind. Capped at
`CARRIER_SENSE_MAX_WAIT_S` (4s) so a stuck "busy" reading can't stall a
transmission forever.

The wait-loop logic (`wait_for_clear_channel`) is a free function
specifically so it's testable without any audio hardware -- a fake
busy-check and a fake sleep, no pyaudio or real device involved.

- audio_io.py: `CARRIER_BUSY_RMS`, `wait_for_clear_channel()`,
  `AudioEngine.is_channel_busy()`; `transmit()` now waits for a clear
  channel before keying up.
- test_offline.py: +2 tests (72 total) -- the wait-loop logic (waits the
  right number of cycles while busy, gives up at the cap rather than
  hanging forever, doesn't wait at all when already clear) and the
  threshold's calibration relative to both the noise floor and real
  field signal levels.

## v0.4.9 — frame coalescing for the BPQ32/Winlink handshake

With the physical layer now solid (messages decompressing correctly end
to end), real sessions still had a slow start: Winlink's own B2F
handshake sends roughly 20-30 small AX.25 frames back and forth before
real content transfer even begins, visible directly in bridge logs as
many separate `TX (BPQ32->air)` / `RX (air->BPQ32)` lines a second or
two apart, most only 15-60 bytes. Every one of those paid the same
fixed overhead (TX delay + burst + TX tail) regardless of how little
data it carried -- the real cost of this handshake was never the OFDM
data rate, it was how many separate times that fixed cost got paid.

Added frame coalescing in `bpq_bridge.py`: when BPQ32 hands the bridge
an AX.25 frame, it waits up to `COALESCE_WINDOW_S` (0.12s) to see if
another arrives right behind it (capped at `MAX_BATCH_FRAMES`=8 /
`MAX_BATCH_BYTES`=1900 so a sustained stream still flushes promptly)
before sending everything collected as ONE OFDM transmission, instead
of one per frame. The receiving station's bridge transparently splits
a batch back into individual KISS frames before handing them to its own
BPQ32 -- BPQ32 on both ends only ever sees normal frames; the batching
is purely an internal SARA-PP-to-SARA-PP optimization on the air link,
invisible to anything above it. An isolated frame (nothing else arrives
within the window) still goes out promptly on its own, so this only
helps bursts and never adds meaningful delay to a single frame.

Both ends need to be running this version for it to work correctly --
an older peer would misinterpret a batch's marker/length-prefix bytes
as raw AX.25 content.

- bpq_bridge.py: `pack_batch`/`unpack_batch` (the wire format, with a
  safe fallback to treating unrecognized input as a single frame), and
  `_client_loop` reworked to coalesce with a short socket timeout
  rather than process each KISS frame immediately as it arrives.
- sara_pingpong.py: TX/RX bridge log lines and CSV notes now show frame
  count alongside byte count when a transmission carries more than one.
- test_offline.py: +3 tests (batch pack/unpack round trip across
  several sizes, safe fallback on malformed/non-batch input, and an
  end-to-end real-socket test confirming frames sent close together
  arrive as one batch while an isolated frame still goes out on its
  own promptly).

## v0.4.8 — expose measured drift, instead of guessing whether it's enough

v0.4.7's pilot tracking measurably helped (QPSK ran noticeably longer
before failing in real testing), but real BPQ32-to-Winlink-Express
transfers were still occasionally hitting `crc_fail` on long frames,
and a real transfer log showed WHY this matters more than it might
seem: a message needing only ~12 real frames (PACLEN=236) still failed
to decompress. Even a meaningfully improved per-frame success rate
isn't good enough for a protocol with zero tolerance for a single
corrupted byte anywhere in a multi-frame message -- at, say, 90-95%
per-frame reliability, a 12-frame message has a real chance of hitting
at least one bad frame.

Rather than guess at another tuning pass, `DecodedFrame.drift_deg` and
the `crc_fail` rejection's `drift_deg` field expose the actual
pilot-measured channel drift (in degrees, accumulated from the preamble
to the last data symbol) on every long frame, success or failure.
Synthetic testing shows this is a meaningful, monotonic number: QPSK
decoding starts failing once drift approaches ~90° (a quarter-turn --
enough to cross into the wrong constellation quadrant), climbing
smoothly from there as simulated clock mismatch increases (50ppm →
~28°, 100ppm → ~50°, 150ppm → ~71°, 200ppm+ → crosses 90° and fails).

This turns "is it still failing?" into "how many degrees of drift does
this specific link actually have, and how close to the ~90° cliff is
it?" -- the next real field log will show directly whether the
remaining failures are a magnitude problem (real drift exceeds what one
common pilot correction can track, which would call for per-subcarrier
tracking or a different correction strategy) or something else the
number would reveal by NOT being large at the moment of a failure.

## v0.4.7 — real BPQ32/Winlink traffic exposed a long-frame-only bug

With v0.4.6 holding up well for ping/pong, real BPQ32-to-Winlink-Express
traffic (actual file transfers, not test frames) started hitting a
different problem: `crc_fail` on frames that were, in every other
respect, perfect -- `n_data_bytes=254` (exactly BPQ32's real PACLEN),
quality 0.87-0.95 (the link's completely normal, strong quality). Not
noise, not a false lock -- a genuine real frame, cleanly received,
decoding to the wrong bits.

The difference from every ping/pong test this project ever ran: those
frames are 22 bytes, needing only ~8 QPSK symbols (~107ms of data).
BPQ32's real 254-byte frames need ~85 QPSK symbols (over a full second)
or ~176 BPSK symbols (over 2+ seconds). The receiver's channel estimate
is a single snapshot taken from the preamble at the very start of a
frame, then reused unchanged for every data symbol after it. Over
~100ms that staleness is negligible. Over a full second, two
independent, unsynchronized sound card clocks drift against each other
enough to flip bits in the later symbols -- a mechanism that literally
cannot show up on a 107ms frame, no matter how long you test with one.

Reproduced directly (not guessed): resampling a 252-byte test burst by
80+ ppm -- a realistic mismatch between two different sound cards --
broke decoding with the exact same signature (crc_fail, n_data_bytes
matching the real payload, quality in the normal range). Fixed with
per-symbol pilot-based channel tracking: the pilot carriers already
present in every symbol (previously transmitted but never read back)
now measure how far the channel has drifted since the last estimate,
and that correction carries forward symbol-by-symbol through the whole
frame -- the standard real-world OFDM technique for exactly this
problem. Verified: reliable decoding pushed from ~80ppm to 150ppm+ for
both modulations, confirmed on 252-byte (real BPQ32-scale) frames.

## v0.4.6 — the real culprit wasn't our own signal at all

v0.4.5's bigger residual-tail margin did not fix the pattern -- a
second real field log (same K6IRF<->DW1IRF link) still showed an
`implausible_length` or `crc_fail` rejection right after nearly every
successful decode, pass rate essentially unchanged (75% vs 70%). But
this log revealed something v0.4.4's first log didn't make obvious:
the "bad" decoded lengths were different and essentially random every
single time (1403, 1657, 1914, 1617, 1473...) -- not what you'd expect
from a deterministic residual of our *own* just-decoded signal, which
would look similar each time. That pattern points somewhere else
entirely: something external arriving in a narrow, consistent window
right after the remote station's transmission ends. The likely source
is a squelch-tail/PTT-release noise burst from the remote radio -- a
common FM artifact, and nothing about our own trim margin could ever
fix noise that isn't our own signal.

Added a post-decode cooldown: for `POST_DECODE_COOLDOWN_S` (0.35s)
after any successful decode, the receiver doesn't search for a new
preamble at all, and actively discards whatever arrives during that
window rather than merely ignoring it (an artifact that just sits in
the buffer would otherwise be found the instant the cooldown lifts, as
if it had arrived later). This costs nothing against genuine traffic:
this system's own TX chain (TX_LEAD_MS + burst + TX_TAIL_MS, plus any
reply delay) always leaves far more than 350ms between two distinct
real transmissions.

As a side effect, the v0.4.3 realistic-noise-floor test improved from
1.0% to 0.0% missed -- the cooldown also rides through the brief
window right after a decode where a stray noise correlation is most
likely in general, not just the specific squelch-tail case that
motivated it.

## v0.4.5 — found via real field data: residual-tail margin was too small

The v0.4.4 diagnostics worked exactly as intended: a real K6IRF-to-
DW1IRF log showed a near-perfectly consistent pattern -- almost every
single successful `RX PONG ... PASS` was immediately followed by an
`implausible_length` or `crc_fail` rejection, with substantial real
signal energy (0.7-2.3, far above the noise floor -- this is not noise,
it's real leftover signal). This is the residual-tail-corrupts-next-
candidate mechanism from v0.4.2, still happening: the margin added then
(one cyclic prefix, N_CP = 128 samples) was evidently not enough to
reliably clear whatever real-world residual (likely accumulated timing
drift between two independent, unsynchronized sound card clocks over a
1-2 second QPSK burst, possibly combined with analog PTT-release
transients) survives past the computed end of a real over-air frame.

Increased the margin to a full extra symbol (N_SYM = 640 samples,
~13ms) -- about 5x more headroom. Verified against a simulated
post-burst analog transient: insufficient under the old margin, fully
cleared under the new one. Could not be reproduced with a perfectly
clean synthetic burst alone (no clock drift, no analog imperfections,
by construction) -- which is exactly why this took real field data to
surface rather than more synthetic testing.

## v0.4.4 — verbose RX diagnostics, for data instead of more guessing

v0.4.3's fix (stricter sustain check + fixed energy floor) was tuned
against synthetic Gaussian noise models. Field testing afterward showed
a far higher miss rate (30-45%) than those synthetic tests predicted,
with a telling detail: every *successful* frame showed strong, healthy
quality (0.75-0.87) — ruling out a weak/marginal-signal explanation.
Pings would go out (confirmed by their own locally-heard echo) and
simply get no reply at all, sometimes on two independent pings in a
row, seconds apart — consistent with the receiver getting stuck on
something for an extended stretch, not a one-off fluke. Likely real
noise has more structure (hum, switching supply noise, USB audio
artifacts, faint cross-talk between the two stations' own radios) than
simple white Gaussian noise ever modeled.

Rather than continue tuning detection parameters against more guesses
about what real noise looks like, `OFDMReceiver.on_reject` is a new
diagnostic hook: set it to a callback and every rejected candidate
(low energy, invalid length, CRC failure, etc) is reported with its
reason and details (energy, decoded length, quality) — never altering
decoding behavior itself, and never able to crash the receiver even if
the callback raises. The GUI exposes this as a **"Verbose RX
diagnostics"** checkbox: enable it, run a test, and every rejection
shows up inline in the log, right next to the TIMEOUT it likely
caused. That turns the next test session into real data about this
specific link's actual noise characteristics, instead of another round
of parameter guesses.

## v0.4.3 — real ambient noise was never actually tested until now

Every offline test through v0.4.2, including the "realistic pacing"
ones, used literal zeros for silence between frames. No real microphone
ever produces true digital silence -- room noise, hum, and self-noise
are always present, even when quiet. Pure ratio-based preamble
detection (the v0.3.0 approach, restored in v0.4.2) can occasionally
lock onto that noise by chance, strongly enough to clear the
correlation threshold despite carrying no real signal.

Two changes, together:
- The "sustain" check (requiring the correlation to stay high, not just
  spike once) now requires a full symbol's worth of points at a
  stricter level, not half a symbol at a looser one. A genuine
  preamble's plateau is wide, so this costs it nothing; a brief noise
  correlation is measurably less likely to sustain that long.
- A fixed absolute energy floor is back (MIN_ENERGY, equivalent to
  quality ~0.05 -- comfortably below the lowest real quality this
  project has observed working, 0.18, with margin). An *adaptive*
  version of this (calibrating to each session's own measured noise
  floor) was tried first and reverted: it sampled "ambient" readings
  from the live buffer, which can accidentally capture real signal
  energy right at the edge of a transmission, contaminating the
  estimate upward over a session until genuine signals could no longer
  clear it. A fixed, conservative floor is less clever but safer.

Tested against sessions with actual injected noise (not pure silence)
across several noise levels and seeds: ~1% of frames missed overall,
down from severe, systemic failure. Not zero -- occasional single-frame
misses on a real link are normal and are what retry logic above this
layer (a KISS/AX.25 stack's own ARQ, or simply re-pinging) is meant to
absorb. If real-world testing still shows a much higher miss rate than
this, that points to something specific to that link's actual noise
characteristics worth sharing in detail (quality numbers from the log,
whether misses cluster around particular conditions) rather than
further guessing at parameters in the abstract.

## v0.4.2 — three more receiver bugs found and fixed the same day

After v0.4.0/v0.4.1, real ping/pong testing still failed completely.
Investigating that surfaced three distinct, real bugs — two pre-existing
(exposed, not caused, by the v0.4.0 buffer-size change) and one in a
v0.4.0 safety check that turned out to be a no-op:

1. **Residual tail corrupts the next frame's header.** The preamble
   detector's reported position can be off by up to ~1 cyclic prefix's
   worth of samples (harmless for demodulation, which the CP is there to
   absorb) — but that same imprecise position was used to decide exactly
   where to trim the buffer after a successful decode, leaving a small
   real leftover of the just-decoded frame sitting in the buffer. That
   leftover could be mistaken for the start of the *next* frame,
   corrupting its header (wrong modulation bit, nonsense length). Fixed
   by trimming a bit further past the computed end.

2. **The v0.4.0 "stale candidate" safety check was a no-op.** It
   compared accumulated data against the same cap push() itself
   enforces, so it could never actually fire once the buffer filled —
   meaning a candidate whose decoded header claimed an implausibly large
   length (which does happen: random signal content can occasionally
   self-correlate at lag N_SYM by chance, especially with QPSK's denser
   constellation) would wait forever for data that could never fully
   arrive, silently blocking every real frame afterward for the rest of
   the session, with no error or crash. Fixed with a direct check: once
   a header's claimed length is known, if it exceeds what the buffer
   could ever hold, abandon that candidate immediately.

3. **Rejecting a candidate only skipped one symbol, not past the whole
   preamble block.** The repeated preamble reps are each self-similar by
   design — skipping just one symbol after rejecting a candidate lands
   exactly on the *next* repetition, re-triggering the same false
   detection and crawling through an entire bogus block one symbol at a
   time instead of clearing it in one step. Fixed by skipping a full
   `PREAMBLE_REPS` worth of symbols on rejection.

**Testing before shipping this time:** a realistic-pacing stress test
(30 mixed BPSK/QPSK frames per run, 1.5–3s gaps matching actual
round-trip timing) across 30 different random seeds — all 30 clean,
0 missing frames. A separate, deliberately unrealistic dense-packing
stress test (frames 30–100ms apart) still occasionally drops frames to
transient false locks; this is a known, lower-priority edge case, not
something this system can actually produce between two real stations —
our own TX chain enforces several hundred ms to seconds between any two
of *our own* transmissions (TX delay + burst + tail), so frames this
close together can't occur in practice here.

## Receiver buffer sizing (fixed in v0.4.0 — read this if upgrading)

Versions through v0.3.0 sized the receiver's internal buffer only for
ping/pong-length payloads (22 bytes). The first real BPQ32 traffic
exposed this: the AX.25 link connected fine (short control frames like
SABM/UA are small enough to fit), but no actual data frame ever got
through — a real 254-byte AX.25 I-frame needs over a second of audio,
far more than the old ~320ms buffer retained, so the front of every
such frame was silently discarded before it could ever be decoded.
Fixed by sizing the buffer from `MAX_PAYLOAD_LEN` at BPSK (the
longer-duration mode) instead of an arbitrary small constant, and by
switching the bandpass filter from re-processing the whole buffer every
~20ms audio callback to an incremental streaming filter with saved
state — necessary once the buffer is multi-second, or filtering alone
would fall behind real-time. A second, related bug (a spurious
low-energy preamble-detector hit — filter-ringing right after a burst
ends, not independent noise — could pass the normalized correlation
check and then block every subsequent frame forever) is fixed with an
absolute energy gate alongside the ratio check. Both are covered by
dedicated regression tests in `test_offline.py`
(`test_long_bpq_style_frame`, `test_spurious_hit_does_not_block_next_frame`).

## Known limitations / next steps

- No FEC, no ARQ, no retransmission — a corrupted frame is just
  dropped. That's intentional for now; it keeps a failed test
  unambiguous (bad link, not a bug in retry logic).
- No CFO (frequency offset) correction beyond what the CP guard
  interval tolerates naturally. Offline testing shows this is robust
  to at least 500 ppm of sample-clock drift between two independent
  sound cards — well beyond what typical consumer audio hardware
  exhibits — so this likely isn't worth adding until real testing
  shows otherwise.
- `audio_io.py`'s lead-in silence (300 ms) has no squelch-opening tone,
  unlike the old code's dedicated 500 ms pre-tone. If squelch-tail
  clipping turns out to be a real problem on your radios, the right
  fix is a short tone added to the silence gap in `audio_io.py` —
  never inside `ofdm_core.py`.
- The BPQ32 bridge carries one KISS port (PORTNUM=1) and doesn't
  multiplex multiple logical ports over one audio link — matching the
  original project's config, which also used a single port.
