# SARA FM

**An open OFDM sound-card modem for Winlink over VHF/UHF FM radios.**

SARA FM turns an ordinary FM transceiver and a USB sound-card interface into a
fast data link for Winlink and packet radio. It plugs into BPQ32, RMS Packet
or Winlink Express as a KISS TNC over TCP, and adapts its speed to each link,
from BPSK up to 64-QAM, between 0.9 and 8.1 kbit/s gross.

It was built by and for **PAREDN** (Philippine Amateur Radio Emergency Data
Network), and is published so other emergency-communications groups can use,
check and improve it.

## Features

- **OFDM waveform**: 16 or 22 carriers between 469 Hz and 2.6 kHz, so it fits
  through a normal FM voice radio.
- **Automatic speed** in each direction of a link, driven by link reports from
  the other station.
- **Forward error correction**: Reed-Solomon, plus a convolutional code in the
  fastest modes, so fewer frames need an AX.25 retry.
- **KISS over TCP** with ACKMODE, for BPQ32, RMS Packet, Winlink Express and
  other KISS clients.
- **AX.25 digipeating**, with gateway-side handling of digipeated Winlink
  sessions.
- **SARA Chat**: keyboard chat and file transfer between stations, also usable
  from another computer over the network.
- **SARA Terminal**: a plain terminal, like VARA Terminal, that connects over
  SARA FM to a BPQ32 node and its Chat Server.
- **Phone web page** and a **Raspberry Pi** headless mode for low-power
  go-kits.
- **One-button Tune** that sets audio levels between two stations.

## Getting started

- **Windows gateways and clients:** download the latest `sara-fm-*-win64.zip`
  from *Releases*, unzip it, and follow `QUICKSTART_GATEWAY.txt`. No Python
  install is needed.
- **From source** (Windows or Linux, Python 3.11+):

  ```
  pip install -r requirements.txt
  python sara_pingpong.py
  ```

  Run the offline tests first with `python test_offline.py`.

- **Raspberry Pi (go-kits, small gateways):** runs with no screen, operated
  from a web page on your phone. Run `bash pi/install_pi.sh` and see
  [`docs/RASPBERRY_PI.md`](docs/RASPBERRY_PI.md). Mail comes from LinBPQ and
  PAREDN Webmail on the phone (`pi/install_linbpq.sh`). Pat also works.
- **Any computer:** add `--web` to also get the phone page, e.g.
  `python sara_pingpong.py --web`.

## Documentation

| File | What it is |
| --- | --- |
| [`docs/SPEC.md`](docs/SPEC.md) | SARA FM Protocol Specification: everything needed to build a compatible modem |
| [`QUICKSTART_GATEWAY.txt`](QUICKSTART_GATEWAY.txt) | Setting up a gateway or client station |
| [`docs/RASPBERRY_PI.md`](docs/RASPBERRY_PI.md) | Raspberry Pi install, phone web page, LinBPQ and Pat |
| [`BPQ32.cfg.example`](BPQ32.cfg.example) | BPQ32 port configuration |
| [`test-vectors/`](test-vectors/) | Reference recordings with their decoded bytes, for checking other implementations |
| [`HISTORY.md`](HISTORY.md) | Version-by-version development history and field-test notes |

## Code layout

| File | Role |
| --- | --- |
| `ofdm_core.py` | Waveform: encode, detect, decode |
| `fec.py`, `fec_cc.py` | Reed-Solomon and convolutional coding |
| `protocol.py` | Ping / pong payloads |
| `bpq_bridge.py`, `kiss.py` | KISS TCP server and AX.25 batching |
| `link_adapt.py` | Link reports, speed control, digipeater handling |
| `chat.py`, `chat_remote.py`, `chat_window.py` | SARA Chat |
| `audio_io.py`, `ptt.py` | Sound card and PTT (CM108, serial RTS/DTR, Pi GPIO, VOX) |
| `web_ui.py` | Phone web page (`--web`, `--headless`) |
| `pi/` | Raspberry Pi installers (SARA FM, LinBPQ) and Pat helper |
| `tuning.py` | Level and S/N guidance for Tune |
| `sara_pingpong.py` | The application window |
| `test_offline.py` | Offline test suite (no radio needed) |
| `tools/make_test_vectors.py` | Regenerates `test-vectors/` |

## Contributing

Bug reports from the field are the most valuable contribution. Please include
the log bundle (*Log → Save Logs*) and say which stations and
radios were involved.

For code changes:

- Keep the on-air format compatible. A change to anything in `docs/SPEC.md`
  needs a version check so older stations ignore it or reject it cleanly.
- Run `python test_offline.py` and `python test-vectors/verify.py` before
  sending a change. Both must pass.
- If you write your own implementation, decoding every file in `test-vectors/`
  is the first milestone.

## License

- **Code:** GNU General Public License v3.0 or later. See [`LICENSE`](LICENSE).
- **Protocol specification** (`docs/SPEC.md`): Creative Commons Attribution 4.0
  International (CC BY 4.0).
- **Protocol:** anyone may implement the protocol, in any language, with no
  permission or fee needed.

Copyright © 2026 PAREDN / DW1IRF.

This program is distributed in the hope that it will be useful, but WITHOUT ANY
WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A
PARTICULAR PURPOSE. See the GNU General Public License for more details.

Operators are responsible for following their own country's amateur radio
rules.
