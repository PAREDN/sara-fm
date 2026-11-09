# SARA FM on a Raspberry Pi

SARA FM runs on a Raspberry Pi with no screen. You set it up and run it from a
web page on your phone. A Pi, a USB audio interface and an FM radio make a
low-power Winlink station for a go-kit, or a small always-on gateway.

> **Status (0.9.82): not yet field-tested on a Pi.** The
> modem code is the same as on Windows, and the web page and installer have
> been tested on Linux. Please report how it goes on your Pi model.

## What you need

| Item | Notes |
| --- | --- |
| Raspberry Pi 4 or 5 | Recommended. Pi 3B / 3B+ should also work. |
| microSD card, 16 GB+ | Raspberry Pi OS **Bookworm**, Lite (no desktop) is enough |
| USB audio interface | RA-Board / DX1PRD CM108 interface, Digirig, AIOC, SignaLink, or any CM108 dongle |
| FM radio + cable | As for the Windows version |
| Power | Official supply or a 5 V USB-C pack. Pi 4: about 3–6 W |

**PTT options:**

| PTT setting | Use it for |
| --- | --- |
| **CM108** | RA-Board, DX1PRD interface, AIOC, most CM108 dongles: PTT through the USB sound card |
| **RTS** / **DTR** | Digirig and other USB-serial keying. Set *Serial port* to e.g. `/dev/ttyUSB0` |
| **Pi GPIO** | A Pi pin driving a transistor or opto-isolator on the radio's PTT line. Default BCM 17 (header pin 11). Never connect a GPIO pin straight to the radio |
| **VOX** | SignaLink and other VOX interfaces |

## Install

1. Flash **Raspberry Pi OS (64-bit) Bookworm** with Raspberry Pi Imager. In
   the Imager settings, set a user name and password, Wi-Fi, and turn on SSH.
2. Copy the `sara-fm` folder to the Pi, for example with `scp`, or download
   it from GitHub with `git clone`.
3. Log in over SSH and run:

   ```
   cd sara-fm
   bash pi/install_pi.sh
   sudo reboot
   ```

The installer does the following:

- installs the packages SARA FM needs
- gives your user access to USB PTT, serial ports and GPIO
- runs the self-test
- sets SARA FM to start at boot as the service `sara-fm`, with the web page
  on port 8090

## First run from your phone

1. Connect the phone to the same network as the Pi, or to the Pi's own
   hotspot (below).
2. Open `http://<pi-address>:8090/`. The installer prints the address, and
   `hostname -I` on the Pi shows it too.
3. **Setup tab:**
   - enter your callsign
   - pick the USB audio interface as both *Input* and *Output device*
     (it is listed like `USB Audio Device: - (hw:1,0)`)
   - pick the PTT method
4. Tap **Save settings**, then **Test PTT (2 s)** and watch the radio key.
5. **Radio tab:** pick the target station, tap **Start**, then **Tune**.

| Tab | What it has |
| --- | --- |
| **Radio** | RX level, S/N and speed meters; TX / RX / DCD / TCP lamps; the Tune answer; Start, Ping, Tune, Tune 10 s and TX anyway |
| **Chat** | SARA Chat: direct messages, the NET room, CQ and check-in |
| **Log** | SARA FM's log, live |
| **Setup** | Callsign, levels, devices, PTT, DCD mode, gateway and digipeater |

**Who can open the page:** access follows the KISS "Allow from" rule. By
default that is private addresses only: the Pi itself, LAN, AREDN and
Tailscale. As with KISS, there is no password, so anyone who can open the
page can transmit. Keep the Pi off public networks.

## Field use: the Pi's own Wi-Fi hotspot

With no other network around, the Pi can make its own Wi-Fi network for your
phone:

```
sudo nmcli device wifi hotspot ifname wlan0 ssid SARA-FM password "choose-a-password"
sudo nmcli connection modify Hotspot connection.autoconnect yes
```

On that network the Pi is `10.42.0.1`, so the page is at
`http://10.42.0.1:8090/` (and Mail at `http://10.42.0.1:3000/`).

## Mail from the phone: LinBPQ + PAREDN Webmail (recommended)

This is the same setup PAREDN's BPQ32 stations run: LinBPQ (the Linux BPQ32)
keeps the mailbox, and PAREDN Webmail is the mail page on the phone, with
Welfare messages, Operator mail and the Winlink forms. SARA FM's page gets a
green **Mail** button that opens it.

| What | Port | Page |
| --- | --- | --- |
| SARA FM (radio) | 8090 | `http://<pi>:8090/` |
| PAREDN Webmail (mail) | 3000 | `http://<pi>:3000/` |
| LinBPQ (node management, BPQ WebMail) | 8080 | `http://<pi>:8080/` |
| SARA FM KISS (LinBPQ connects here) | 8105 | none |

1. Install LinBPQ, with your callsign:

   ```
   bash pi/install_linbpq.sh YOURCALL
   ```

   This downloads G8BPQ's LinBPQ and writes a starter `~/linbpq/bpq32.cfg`
   (only if there is none) with:
   - the node and BBS
   - web pages on 8080
   - the SARA FM radio port, with the same settings as `BPQ32.cfg.example`

   It also makes LinBPQ start at boot after SARA FM.
2. Edit `~/linbpq/bpq32.cfg` and fill in every `CHANGEME`: passwords,
   alias, locator. Then run `sudo systemctl start linbpq`.
3. Open `http://<pi>:8080/` → **Mail Mgmt**. Set up forwarding to Winlink
   the same way as on your BPQ32 BBS stations: by radio through a gateway
   (e.g. DV1UK-10), or CMS over the internet when there is internet. To copy
   an existing station, its mail settings are `BPQMail.cfg` on Windows, which
   is `linmail.cfg` in `~/linbpq`.
4. Install PAREDN Webmail: copy the `paredn-webmail` folder to the Pi, then
   run:

   ```
   cd paredn-webmail
   bash linux/install.sh
   ```

   Answer `127.0.0.1`, `8080` and `3000`.
5. On the phone, open `http://<pi>:8090/` and tap **Mail**, or open
   `http://<pi>:3000/` directly. Add both to the home screen.

> The LinBPQ and webmail steps haven't been run on a Pi yet. LinBPQ's Pi
> download is a 32-bit program; on 64-bit Raspberry Pi OS the installer adds
> the 32-bit libraries it needs. If LinBPQ won't start, check
> `journalctl -u linbpq` and send the output.

## With Pat (Winlink client for go-kits)

Pat reaches SARA FM through the Linux kernel's AX.25 support. `socat` bridges
the KISS TCP port to a virtual serial port, and `kissattach` turns that into
an AX.25 interface called `sarafm`.

1. Install Pat (from its GitHub releases page) and the AX.25 tools:

   ```
   sudo apt install ax25-tools ax25-apps
   ```

2. With SARA FM running, attach the port. Repeat this after every reboot, or
   add it to a startup script.

   ```
   sudo pi/pat_ax25.sh YOURCALL-7
   ```

3. In Pat's configuration (`pat configure`), set the AX.25 engine to `linux`
   and the port to `sarafm`.
4. Connect: `pat connect ax25:///DV1UK-10`, or use Pat's web interface.

The kernel's AX.25 stack doesn't use KISS ACKMODE. The helper therefore sets
the kernel's retry timer to 20 s, like `FRACK=20000` in the BPQ32 example. If
you see needless repeats, raise it.

> The Pat route has not been field-tested yet. LinBPQ is the tried path,
> since it is the same as BPQ32.

## Day-to-day commands

| Command | What it does |
| --- | --- |
| `sudo systemctl status sara-fm` | Is it running? |
| `journalctl -u sara-fm -f` | Its output, live |
| `sudo systemctl restart sara-fm` | Restart, e.g. after updating the files |
| `sudo systemctl disable --now sara-fm` | Stop it starting at boot |
| `venv/bin/python sara_pingpong.py --web` | Run it with its window on a Pi desktop, plus the web page |

Settings, logs and chat history are kept in the `sara-fm` folder, as on
Windows. To update SARA FM, replace the program files, keep those, and run
`sudo systemctl restart sara-fm`.

## How much of the Pi it uses

On a desktop PC, decoding uses about 2–3 % of one core while frames are
arriving, and 0.3 % on an idle channel. A Pi 4 should need several times that,
an estimated 10–20 % of one core. That leaves plenty of room for LinBPQ or
Pat. A Pi 3 has less headroom; watch for "not keeping up" warnings in the log.

## Troubleshooting

| Problem | What to check |
| --- | --- |
| Page doesn't open | Is it running (`systemctl status sara-fm`)? Is the phone on the same network? Try the address from `hostname -I`. Since 0.9.82 the page is on port **8090** |
| Mail button doesn't open | Is the webmail running (`systemctl status paredn-webmail`)? Is LinBPQ running (`systemctl status linbpq`)? |
| "No RA-Board (CM108) detected" | Reboot after installing, so the USB permission rule and group take effect. `ls -l /dev/hidraw*` should show group `plugdev` |
| No sound devices listed | Tap **Find devices**. Run `arecord -l` and `aplay -l`: the interface should be listed |
| Serial PTT error | Check `ls /dev/ttyUSB*` and that your user is in `dialout` (`groups`) |
| Pi GPIO PTT error | The user must be in `gpio`; check the pin number (BCM, not header pin) |
