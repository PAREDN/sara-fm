#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 PAREDN / DW1IRF
#
# install_linbpq.sh -- add LinBPQ (G8BPQ's node + BBS, the Linux BPQ32) to a
# SARA FM Raspberry Pi, for a go-kit or small gateway:
#
#     bash pi/install_linbpq.sh  YOURCALL
#
# - downloads LinBPQ for the Pi into ~/linbpq (G8BPQ's 32-bit build; on 64-bit
#   Raspberry Pi OS it installs the 32-bit libraries it needs)
# - writes a starter ~/linbpq/bpq32.cfg ONLY if there is none: node, BBS,
#   web pages on port 8080, and the SARA FM radio port (KISS TCP 8105,
#   ACKMODE) -- passwords left as CHANGEME for you to fill in
# - sets LinBPQ to start at boot after SARA FM (service 'linbpq')
#
# LinBPQ is G8BPQ's software with its own terms; this script only fetches it
# from his site. Then install PAREDN Webmail (its linux/install.sh) for the
# phone mail page.
set -euo pipefail
CALL="$(echo "${1:-}" | tr '[:lower:]' '[:upper:]')"
[ -n "$CALL" ] || { echo "usage: bash pi/install_linbpq.sh YOURCALL"; exit 1; }
USER_NAME="${SUDO_USER:-$USER}"
HOME_DIR="$(getent passwd "$USER_NAME" | cut -d: -f6)"
BPQ="$HOME_DIR/linbpq"
SITE="www.cantab.net/users/john.wiseman/Downloads"

echo "== LinBPQ for $CALL in $BPQ"
mkdir -p "$BPQ/HTML"
cd "$BPQ"

echo "== 1/4 Libraries"
sudo apt-get update
sudo apt-get install -y unzip curl
if [ "$(uname -m)" = "aarch64" ]; then
  # G8BPQ's Pi build is 32-bit (armhf): add the 32-bit libraries it uses
  sudo dpkg --add-architecture armhf
  sudo apt-get update
  for p in libc6:armhf zlib1g:armhf libpcap0.8:armhf libasound2:armhf libminiupnpc17:armhf libconfig9:armhf; do
    sudo apt-get install -y "$p" || echo "   (could not install $p -- continuing)"
  done
else
  for p in zlib1g libpcap0.8 libasound2 libminiupnpc17 libconfig9; do
    sudo apt-get install -y "$p" || echo "   (could not install $p -- continuing)"
  done
fi

echo "== 2/4 LinBPQ program and web pages"
fetch() { curl -fsSL -o "$2" "https://$SITE/$1" || curl -fsSL -o "$2" "http://$SITE/$1"; }
if fetch Beta/pilinbpq linbpq.new; then
  mv linbpq.new linbpq && chmod +x linbpq
  echo "   [OK] linbpq downloaded"
else
  rm -f linbpq.new
  [ -x linbpq ] && echo "   [WARN] download failed -- keeping the linbpq already here" || {
    echo "   [ERROR] could not download LinBPQ from $SITE/Beta/pilinbpq."
    echo "           Download 'pilinbpq' from G8BPQ's site yourself, save it as $BPQ/linbpq,"
    echo "           run 'chmod +x $BPQ/linbpq', then run this script again."
    exit 1; }
fi
if fetch HTMLPages.zip HTMLPages.zip; then
  unzip -oq HTMLPages.zip -d HTML && rm -f HTMLPages.zip && echo "   [OK] web pages in $BPQ/HTML"
else
  echo "   [WARN] HTMLPages.zip not downloaded -- the node's own management pages may be missing"
fi

echo "== 3/4 bpq32.cfg"
if [ -f bpq32.cfg ]; then
  echo "   [OK] keeping your existing bpq32.cfg"
else
  cat > bpq32.cfg <<CFG
; ============================================================
;  LinBPQ starter config for a SARA FM go-kit / small station
;  Written by SARA FM's pi/install_linbpq.sh. Same format as
;  BPQ32.cfg on Windows. Fill in every CHANGEME, then:
;      sudo systemctl restart linbpq
; ============================================================
NODECALL=$CALL-2
NODEALIAS=CHANGE          ; up to 6 characters
LOCATOR=CHANGEME          ; your grid square, e.g. PK04lo
BBS=1
NODE=1
PASSWORD=CHANGEME         ; sysop password for the node
; start the BBS (mail) and chat with the node
LINMAIL
LINCHAT

IDINTERVAL=10
BTINTERVAL=0
PACLEN=236
T3=120
IDLETIME=720

; --- SARA FM radio port: see BPQ32.cfg.example in the SARA FM folder ---
PORT
 PORTNUM=1
 ID=SARA FM
 TYPE=ASYNC
 PROTOCOL=KISS
 IPADDR=127.0.0.1
 TCPPORT=8105             ; = SARA FM's "KISS TCP port"
 CHANNEL=A
 QUALITY=0
 MAXFRAME=7
 FULLDUP=0
 FRACK=12000
 RESPTIME=500
 RETRIES=5
 PACLEN=236
 SLOTTIME=10
 PERSIST=255
 NOKEEPALIVES=1
 KISSOPTIONS=ACKMODE
ENDPORT

; --- Telnet / web: node web pages and BPQ WebMail on HTTPPort ---
PORT
 PORTNUM=10
 ID=Telnet Server
 DRIVER=Telnet
 CONFIG
 TCPPORT=8010
 FBBPORT=8011
 HTTPPort=8080            ; PAREDN Webmail talks to this port
 LOCALNET=127.0.0.1/32,100.64.0.0/10
 LOGINPROMPT=user:
 PASSWORDPROMPT=password:
 MAXSESSIONS=10
 CMS=1                    ; send mail to Winlink over the internet when there is internet
 CMSCALL=$CALL
 CMSPASS=CHANGEME         ; your Winlink password
 RELAYAPPL=BBS
 USER=$CALL,CHANGEME,$CALL,,SYSOP
ENDPORT

APPLICATION 1,BBS,,$CALL-4
APPLICATION 2,CHAT,,$CALL-12
CFG
  echo "   [OK] wrote a starter bpq32.cfg -- edit the CHANGEME lines"
fi

echo "== 4/4 Start at boot (service 'linbpq', after SARA FM)"
sudo tee /etc/systemd/system/linbpq.service >/dev/null <<UNIT
[Unit]
Description=LinBPQ node and BBS
After=network-online.target sara-fm.service
Wants=network-online.target

[Service]
User=$USER_NAME
WorkingDirectory=$BPQ
# SARA FM must be listening on its KISS port first
ExecStartPre=/bin/sleep 15
ExecStart=$BPQ/linbpq
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable linbpq.service

cat <<DONE

Done. Next:
  1. Edit $BPQ/bpq32.cfg -- every CHANGEME (passwords, alias, locator).
  2. sudo systemctl start linbpq      (then: journalctl -u linbpq -f)
  3. Open http://<pi-address>:8080/ -> Mail Mgmt, and set up forwarding to
     Winlink the way your BPQ32 BBS stations do (radio via a gateway such as
     DV1UK-10, or CMS over the internet).
  4. Install PAREDN Webmail: in its folder, bash linux/install.sh
     (LinBPQ address 127.0.0.1, port 8080, webmail port 3000).
DONE
