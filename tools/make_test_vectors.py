#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 PAREDN / DW1IRF
"""
make_test_vectors.py -- build the SARA FM test vectors in test-vectors/.

For each vector this writes:
  <name>.wav   48 kHz mono 16-bit PCM: 0.3 s silence, the frame, 0.3 s silence
  <name>.json  every encoding stage, so an independent decoder can find
               exactly where it disagrees with the reference

Run from the repository root:   python tools/make_test_vectors.py
Then check them with:            python test-vectors/verify.py
"""
import hashlib, json, os, struct, sys, wave
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import ofdm_core as o, fec, fec_cc, protocol, chat, bpq_bridge, link_adapt as la  # noqa: E402

OUT = os.path.join(ROOT, "test-vectors")
SR = o.SAMPLE_RATE
PAD = int(0.3 * SR)
PEAK = 0.5                      # frame peak in the WAV (-6 dBFS)

MODES = [(o.MOD_BPSK, "bpsk"), (o.MOD_QPSK, "qpsk"), (o.MOD_QPSK_WIDE, "qpsk-wide"),
         (o.MOD_QAM16_WIDE_C, "16qam-wide-c"), (o.MOD_QAM16_WIDE, "16qam-wide"),
         (o.MOD_QAM64_WIDE_C, "64qam-wide-c"), (o.MOD_QAM64_WIDE, "64qam-wide")]


def bits_hex(bits):
    """Bit list -> hex, MSB first, zero-padded to a whole byte."""
    bits = [int(b) for b in bits] + [0] * ((-len(bits)) % 8)
    return o._bits_to_bytes(bits).hex()


def ax25_ui(dst, src, info):
    def addr(call, last):
        c, _, ssid = call.partition("-")
        return bytes(x << 1 for x in c.ljust(6).encode()) + bytes([0x60 | (int(ssid or 0) << 1) | (1 if last else 0)])
    return addr(dst, False) + addr(src, True) + bytes([0x03, 0xF0]) + info


def stages(payload, mode):
    """Recompute every stage the way ofdm_core.frame_encode does."""
    crc = o.crc16(payload)
    rs_in = payload + struct.pack(">H", crc)
    on_air = fec.encode(rs_in, o.fec_block(mode))
    code = o.HEADER_CODE.get(mode, mode)
    hdr = [(code >> (1 - i)) & 1 for i in range(2)] + [(len(on_air) >> (9 - i)) & 1 for i in range(10)]
    bps = o.bits_per_symbol(mode)
    d = {"crc16": f"{crc:04x}", "rs_input_hex": rs_in.hex(),
         "rs_blocks_data_bytes": fec._block_data_sizes(len(rs_in), o.fec_block(mode)),
         "rs_parity_bytes_per_block": fec.NSYM,
         "on_air_bytes_hex": on_air.hex(), "on_air_len": len(on_air),
         "header": {"code": code, "length": len(on_air), "bits": "".join(map(str, hdr))}}
    if mode in o.CODED:
        coded = fec_cc.encode(o._bytes_to_bits(on_air), o.cc_rate(mode))
        inter = fec_cc.interleave(coded, bps)
        d["convolutional"] = {"rate": o.cc_rate(mode), "coded_bits": len(coded),
                              "coded_hex": bits_hex(coded), "interleaved_hex": bits_hex(inter)}
        dbits = o._payload_bits(bps, [int(b) for b in inter])
    else:
        dbits = o._payload_bits(bps, o._bytes_to_bits(on_air))
    mapper = o._MODULATION_MAPPERS[mode]
    syms = []
    for i in range(0, len(dbits), bps):
        v = mapper(dbits[i:i + bps])
        syms.append([[round(float(np.real(x)), 6), round(float(np.imag(x)), 6)] for x in v])
    d["data_bits_per_symbol"] = bps
    d["data_symbols"] = len(syms)
    d["constellation_points"] = syms        # per symbol, data carriers in order, [re, im]
    if o.is_wide(mode):
        tc = o.WIDE_TRAINING_CARRIERS_BY_MODE[mode]
        d["training_bpsk"] = [int(np.real(tc[p]) < 0) for p in sorted(tc)]
    return d


def write_wav(path, x):
    pcm = np.clip(np.round(x * 32767), -32768, 32767).astype("<i2")
    with wave.open(path, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR); w.writeframes(pcm.tobytes())
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def decode(x):
    rx = o.OFDMReceiver()
    out = []
    for i in range(0, len(x), 960):
        rx.push(x[i:i + 960])
        f = rx.poll()
        while f is not None:
            out.append(f); f = rx.poll()
    return out


def snr_noise(burst, snr_db, seed):
    """White noise at snr_db, measured in the wide band (656-2625 Hz):
    in-band signal power / in-band noise power."""
    p_sig = float(np.mean(burst.astype(np.float64) ** 2))
    bw = (len(o.WIDE_BINS) * SR / o.N_FFT) / (SR / 2)          # share of the full band
    sigma = np.sqrt(p_sig / (10 ** (snr_db / 10)) / bw)
    return np.random.RandomState(seed).normal(0, sigma, len(burst) + 2 * PAD).astype(np.float32)


def make(name, desc, payload, mode, snr_db=None, seed=None):
    burst = o.frame_encode(payload, mode) * PEAK
    audio = np.concatenate([np.zeros(PAD, np.float32), burst, np.zeros(PAD, np.float32)])
    if snr_db is not None:
        audio = audio + snr_noise(burst, snr_db, seed)
    sha = write_wav(os.path.join(OUT, name + ".wav"), audio)
    # check from the WAV as written (16-bit), not the float signal
    with wave.open(os.path.join(OUT, name + ".wav")) as w:
        back = np.frombuffer(w.readframes(w.getnframes()), "<i2").astype(np.float32) / 32768
    got = decode(back)
    ok = len(got) == 1 and got[0].payload == payload and got[0].modulation == mode
    rec = {"name": name, "description": desc, "mode": la.NAMES[mode], "mode_id": mode,
           "payload_hex": payload.hex(), "payload_len": len(payload),
           "wav": name + ".wav", "wav_sha256": sha, "sample_rate": SR,
           "frame_start_sample": PAD, "frame_samples": len(burst), "frame_peak": PEAK,
           "added_noise": None if snr_db is None else {"in_band_snr_db": snr_db, "seed": seed},
           "reference_decode": {"ok": ok, "snr_db": round(got[0].snr_db, 1) if got and got[0].snr_db is not None else None,
                                "fec_corrected_bytes": got[0].fec_corrected if got else None}}
    rec.update(stages(payload, mode))
    with open(os.path.join(OUT, name + ".json"), "w") as f:
        json.dump(rec, f, indent=1)
    print(f"{'OK ' if ok else 'BAD'} {name:28s} {la.NAMES[mode]:13s} {len(payload):4d} B"
          + (f"  snr {snr_db} dB, fec {rec['reference_decode']['fec_corrected_bytes']}" if snr_db is not None else ""))
    return ok


def main():
    os.makedirs(OUT, exist_ok=True)
    for f in os.listdir(OUT):
        if f.endswith((".wav", ".json")) and f != "index.json":
            os.remove(os.path.join(OUT, f))
    text = b"SARA FM test vector. The quick brown fox jumps over the lazy dog 0123456789."
    ok, index = True, []

    def add(*a, **k):
        nonlocal ok
        ok &= make(*a, **k); index.append(a[0])

    # 1. the same AX.25 frame in every mode (clean)
    frame = ax25_ui("TEST", "N0CALL-1", text)
    batch = bpq_bridge.pack_batch([frame])
    for m, tag in MODES:
        add(f"clean-{tag}", f"One AX.25 UI frame in a batch, {la.NAMES[m]}, no noise", batch, m)
    # 2. multi-block RS (payload longer than one RS block) in a narrow and a wide mode
    # (narrow frames stay within the 250-byte batch limit SARA FM uses on air)
    mid = bpq_bridge.pack_batch([ax25_ui("TEST", "N0CALL-1", bytes(range(140)) + text)])
    big = bpq_bridge.pack_batch([ax25_ui("TEST", "N0CALL-1", bytes(range(256)) + text)])
    add("multiblock-qpsk", "Batch longer than one RS block (narrow: 231 B/block, 2 blocks)", mid, o.MOD_QPSK)
    add("multiblock-16qam-wide-c", "Batch longer than one RS block (wide: 160 B/block, 3 blocks)", big, o.MOD_QAM16_WIDE_C)
    # 3. ping, pong, chat, batch + link report
    add("ping-bpsk", "PING N0CALL-1 -> N0CALL-2, seq 7", protocol.make_ping("N0CALL-1", "N0CALL-2", 7), o.MOD_BPSK)
    add("pong-bpsk", "PONG with two-way report (S/N 24 dB, -18 dBFS, carriers 21 dB)",
        protocol.make_pong("N0CALL-2", "N0CALL-1", 7, snr_db=24, level_dbfs=-18, carrier_snr_db=21), o.MOD_BPSK)
    add("chat-qpsk", "SARA Chat text message, id 1, ts 1700000000",
        chat.pack(chat.T_MSG, "N0CALL-1", "N0CALL-2", chat.msg_body(1, 0, 1700000000, "Hello from the SARA FM test vectors")),
        o.MOD_QPSK)
    rep = la.Report(o.MOD_QPSK_WIDE, 7, 1, 28, 40, 9, caps=0, carrier_snr_db=22)
    two = bpq_bridge.pack_batch([ax25_ui("TEST", "N0CALL-1", b"frame one"), ax25_ui("TEST", "N0CALL-1", b"frame two")])
    add("batch-report-qpsk-wide", "Two AX.25 frames + link report trailer (QPSK Wide, 7 ok/1 bad, S/N 28, FEC 40/9 %, carriers 22)",
        la.attach_report(two, rep), o.MOD_QPSK_WIDE)
    # 4. noisy: white noise a few dB above each mode's limit; the decoder must
    #    still recover the frame (RS/Viterbi doing real work)
    #    (about 1-2 dB above where this exact signal + seed stops decoding)
    noisy = {o.MOD_BPSK: 6, o.MOD_QPSK: 9, o.MOD_QPSK_WIDE: 11, o.MOD_QAM16_WIDE_C: 16,
             o.MOD_QAM16_WIDE: 19, o.MOD_QAM64_WIDE_C: 21, o.MOD_QAM64_WIDE: 25}
    for m, tag in MODES:
        add(f"noisy-{tag}", f"Same batch as clean-{tag}, white noise at {noisy[m]} dB in-band S/N (seed 1)",
            batch, m, snr_db=noisy[m], seed=1)
    with open(os.path.join(OUT, "index.json"), "w") as f:
        json.dump({"generator": "tools/make_test_vectors.py", "reference_version": o.VERSION,
                   "vectors": index}, f, indent=1)
    print("ALL OK" if ok else "SOME VECTORS DID NOT DECODE")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
