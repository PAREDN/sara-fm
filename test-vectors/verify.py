#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 PAREDN / DW1IRF
"""
verify.py -- check the SARA FM test vectors against this copy of the code.

For every vector in index.json:
  1. decode the WAV with ofdm_core's receiver -> must give exactly the payload
  2. recompute every encoding stage -> must match the .json byte for byte
  3. (clean vectors) re-encode the audio -> must match the WAV to +/-1 LSB

Run from anywhere:  python test-vectors/verify.py
Exit code 0 = all passed. Another implementation can use the same .json
files directly; see test-vectors/README.md.
"""
import json, os, sys, wave
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
import ofdm_core as o                      # noqa: E402
import make_test_vectors as mk             # noqa: E402

STAGE_KEYS = ("crc16", "rs_input_hex", "rs_blocks_data_bytes", "on_air_bytes_hex", "on_air_len",
              "header", "convolutional", "data_bits_per_symbol", "data_symbols",
              "constellation_points", "training_bpsk")


def read_wav(path):
    with wave.open(path) as w:
        assert w.getframerate() == o.SAMPLE_RATE and w.getnchannels() == 1 and w.getsampwidth() == 2
        return np.frombuffer(w.readframes(w.getnframes()), "<i2")


def main():
    index = json.load(open(os.path.join(HERE, "index.json")))
    fails = 0
    for name in index["vectors"]:
        v = json.load(open(os.path.join(HERE, name + ".json")))
        payload, mode = bytes.fromhex(v["payload_hex"]), v["mode_id"]
        pcm = read_wav(os.path.join(HERE, v["wav"]))
        problems = []
        got = mk.decode(pcm.astype(np.float32) / 32768)
        if not (len(got) == 1 and got[0].payload == payload and got[0].modulation == mode):
            problems.append(f"decode: got {len(got)} frame(s)"
                            + (f", mode {got[0].modulation}, payload match {got[0].payload == payload}" if got else ""))
        st = mk.stages(payload, mode)
        for k in STAGE_KEYS:
            if v.get(k) != st.get(k):
                problems.append(f"stage '{k}' differs")
        if v["added_noise"] is None:
            ref = np.clip(np.round(o.frame_encode(payload, mode) * v["frame_peak"] * 32767), -32768, 32767)
            seg = pcm[v["frame_start_sample"]:v["frame_start_sample"] + v["frame_samples"]].astype(np.int32)
            if len(ref) != len(seg) or np.max(np.abs(ref.astype(np.int32) - seg)) > 1:
                problems.append("re-encoded audio differs from the WAV")
        print(("PASS " if not problems else "FAIL ") + f"{name:28s} {v['mode']:13s}"
              + ("" if not problems else "  " + "; ".join(problems)))
        fails += bool(problems)
    print(f"\n{len(index['vectors']) - fails}/{len(index['vectors'])} vectors passed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
