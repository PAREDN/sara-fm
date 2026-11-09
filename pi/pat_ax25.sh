#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 PAREDN / DW1IRF
#
# pat_ax25.sh -- connect SARA FM's KISS TCP port to the Linux kernel AX.25
# stack as the interface "sarafm", so Pat (ax25 engine "linux") and other
# kernel-AX.25 programs can use it. Run after SARA FM has been started:
#
#     sudo pi/pat_ax25.sh  CALLSIGN-SSID  [kiss-port]
#
# Needs: sudo apt install ax25-tools ax25-apps socat
# and a line for the port in /etc/ax25/axports (the script adds one).
set -euo pipefail
CALL="${1:?usage: sudo pat_ax25.sh CALLSIGN-SSID [kiss-port]}"
KPORT="${2:-8105}"
PTY=/tmp/sarafm-kiss

if [ "$(id -u)" -ne 0 ]; then echo "Run with sudo."; exit 1; fi
command -v kissattach >/dev/null || { echo "Install: sudo apt install ax25-tools ax25-apps"; exit 1; }

if ! grep -qE '^sarafm[[:space:]]' /etc/ax25/axports 2>/dev/null; then
  echo "sarafm  ${CALL}  1200  255  7  SARA FM (KISS TCP $KPORT)" >> /etc/ax25/axports
  echo "Added 'sarafm' to /etc/ax25/axports"
fi

pkill -f "socat.*$PTY" 2>/dev/null || true
socat PTY,link=$PTY,raw,echo=0 TCP:127.0.0.1:$KPORT &
sleep 1
kissattach $PTY sarafm
# SARA FM sends a whole window under one key-up and reports nothing back to
# the kernel (no ACKMODE), so give the kernel's retry timer room, like
# FRACK=20000 in BPQ32.cfg.example. Values are in milliseconds.
if [ -d /proc/sys/net/ax25 ]; then
  for d in /proc/sys/net/ax25/ax*; do
    [ -f "$d/t1_timeout" ] && echo 20000 > "$d/t1_timeout" || true
  done
fi
echo "AX.25 port 'sarafm' is up on SARA FM's KISS port $KPORT."
