#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 PAREDN / DW1IRF
#
# install_pi.sh -- set up SARA FM on a Raspberry Pi (Raspberry Pi OS Bookworm,
# Lite or Desktop; Pi 3B/3B+, 4 or 5). Run from the unzipped sara-fm folder:
#
#     bash pi/install_pi.sh
#
# It installs the packages SARA FM needs, lets your user reach the USB audio
# interface's PTT (CM108 / RA-Board / AIOC), serial ports and the GPIO pins,
# and sets SARA FM to start at boot with the phone web page on port 8090.
# Safe to run again after updating SARA FM.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
USER_NAME="${SUDO_USER:-$USER}"
PORT="${SARA_FM_WEB_PORT:-8090}"
MAIL_PORT="${PAREDN_WEBMAIL_PORT:-3000}"

if [ "$(id -u)" -eq 0 ] && [ -z "${SUDO_USER:-}" ]; then
  echo "Run this as your normal user (it uses sudo where needed), not as root."
  exit 1
fi

echo "== SARA FM for Raspberry Pi: installing into $DIR for user $USER_NAME"

echo "== 1/5 System packages"
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-numpy python3-scipy python3-pyaudio \
     python3-tk python3-serial python3-gpiozero xvfb libusb-1.0-0 \
     libudev-dev libusb-1.0-0-dev socat

echo "== 2/5 Python environment (uses the system numpy/scipy, adds hidapi)"
python3 -m venv --system-site-packages "$DIR/venv"
"$DIR/venv/bin/pip" install --upgrade pip >/dev/null
"$DIR/venv/bin/pip" install hidapi

echo "== 3/5 Permissions: USB PTT, serial ports, GPIO, audio"
sudo tee /etc/udev/rules.d/99-sara-fm.rules >/dev/null <<'RULES'
# SARA FM: let members of plugdev key PTT on CM108-family USB sound cards
# (C-Media 0d8c: RA-Board, DX1PRD interface, many others) and the AIOC.
SUBSYSTEM=="hidraw", ATTRS{idVendor}=="0d8c", MODE="0660", GROUP="plugdev"
SUBSYSTEM=="hidraw", ATTRS{idVendor}=="1209", ATTRS{idProduct}=="7388", MODE="0660", GROUP="plugdev"
RULES
sudo udevadm control --reload-rules
sudo udevadm trigger
for g in audio dialout plugdev gpio; do
  if getent group "$g" >/dev/null; then sudo usermod -aG "$g" "$USER_NAME"; fi
done

echo "== 4/5 Self-test (no radio needed)"
"$DIR/venv/bin/python" "$DIR/sara_pingpong.py" --selftest || \
  echo "   (A sound-device FAIL only means no USB audio interface is plugged in yet.)"

echo "== 5/5 Start at boot (systemd service 'sara-fm')"
sudo tee /etc/systemd/system/sara-fm.service >/dev/null <<UNIT
[Unit]
Description=SARA FM modem (headless, web page on port $PORT)
After=network-online.target sound.target
Wants=network-online.target

[Service]
User=$USER_NAME
WorkingDirectory=$DIR
ExecStart=/usr/bin/xvfb-run -a $DIR/venv/bin/python $DIR/sara_pingpong.py --headless --web=$PORT --mail-port=$MAIL_PORT --autostart
Environment=SARA_FM_XVFB=1
Restart=on-failure
RestartSec=5
Nice=-5

[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable sara-fm.service

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
cat <<DONE

Done. Reboot once so the new permissions apply:

    sudo reboot

After the reboot SARA FM starts by itself. On a phone or PC on the same
network open:

    http://${IP:-<this-pi's-address>}:$PORT/

Go to Setup: enter your callsign, pick the USB audio interface for input and
output, pick the PTT method, then Save settings and press Start.

Useful commands:
    sudo systemctl status sara-fm       is it running?
    journalctl -u sara-fm -f            its output
    sudo systemctl restart sara-fm      restart after a change
    sudo systemctl disable --now sara-fm    stop starting at boot

Mail from the phone: bash pi/install_linbpq.sh YOURCALL, then PAREDN Webmail
(see docs/RASPBERRY_PI.md).
DONE
