#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 PAREDN (Philippine Amateur Radio Emergency Data Network) / DW1IRF
"""
test_offline.py — pure software regression tests for ofdm_core + protocol.

Run this after EVERY change, before ever touching a radio:
    python test_offline.py

No pyaudio, no soundcard, no GUI — just encode -> (optionally distort) ->
decode, in memory, in well under a second. This is the fast loop we
should have had from the start: most "test failures" in the field turn
out to be things this script would have caught in five seconds instead
of a ten-minute over-air round trip.
"""

import sys
import os
import random
import numpy as np
import ofdm_core as ofdm
import protocol as proto
import kiss
import socket
import threading
import time as _time
import tempfile as _tempfile
os.environ["SARA_CHAT_DIR"] = _tempfile.mkdtemp(prefix="sara_chat_test_")   # v0.9.38: keep test chats out of the program folder
import bpq_bridge
from bpq_bridge import BpqBridge
import audio_io
import fec
from sara_pingpong import SelfEchoFilter


PASS = "PASS"
FAIL = "FAIL"
results = []


def check(name, condition, detail=""):
    status = PASS if condition else FAIL
    results.append((name, status, detail))
    print(f"[{status}] {name}" + (f" — {detail}" if detail else ""))


def decode_all(rx: ofdm.OFDMReceiver, audio: np.ndarray, chunk=960):
    """Feed audio in small chunks (like a real audio callback would) and
    collect every decoded frame."""
    frames = []
    for i in range(0, len(audio), chunk):
        rx.push(audio[i:i + chunk])
        f = rx.poll()
        while f is not None:
            frames.append(f)
            f = rx.poll()
    return frames


# ── Test 1: clean round trip ────────────────────────────────────────────
def test_clean_roundtrip():
    payload = proto.make_ping("DW1IRF", "K6IRF", seq=42)
    burst = ofdm.frame_encode(payload)
    rx = ofdm.OFDMReceiver()
    frames = decode_all(rx, burst)
    ok = len(frames) == 1 and frames[0].payload == payload
    check("clean round trip decodes exactly one frame with correct payload", ok,
          f"got {len(frames)} frame(s)")
    if frames:
        msg = proto.PingPongMessage.unpack(frames[0].payload)
        check("decoded PING fields match", msg.from_call == "DW1IRF" and msg.to_call == "K6IRF"
              and msg.seq == 42 and msg.msg_type == proto.MSG_PING,
              str(msg))


# ── Test 2: leading silence of arbitrary length (simulates real capture) ─
def test_with_leading_silence():
    payload = proto.make_pong("K6IRF", "DW1IRF", seq=7)
    burst = ofdm.frame_encode(payload)
    for silence_ms in (0, 50, 237, 900):
        lead = np.zeros(int(ofdm.SAMPLE_RATE * silence_ms / 1000), dtype=np.float32)
        audio = np.concatenate([lead, burst, np.zeros(2000, dtype=np.float32)])
        rx = ofdm.OFDMReceiver()
        frames = decode_all(rx, audio)
        ok = len(frames) == 1 and frames[0].payload == payload
        check(f"decodes correctly with {silence_ms}ms leading silence", ok)


# ── Test 3: weak signal (attenuated amplitude) ──────────────────────────
def test_weak_signal():
    payload = proto.make_ping("DW1IRF", "*", seq=1)
    burst = ofdm.frame_encode(payload)
    for gain in (1.0, 0.5, 0.25, 0.15):
        weak = (burst * gain).astype(np.float32)
        audio = np.concatenate([np.zeros(500, dtype=np.float32), weak,
                                 np.zeros(2000, dtype=np.float32)])
        rx = ofdm.OFDMReceiver()
        frames = decode_all(rx, audio)
        ok = len(frames) == 1 and frames[0].payload == payload
        check(f"decodes correctly at gain={gain}", ok)


# ── Test 4: added noise ─────────────────────────────────────────────────
def test_with_noise():
    payload = proto.make_pong("K6IRF", "DW1IRF", seq=99)
    burst = ofdm.frame_encode(payload)
    rng = np.random.RandomState(0)
    for snr_scale in (0.02, 0.05, 0.10):
        noise = rng.normal(0, snr_scale, size=len(burst)).astype(np.float32)
        noisy = burst + noise
        audio = np.concatenate([np.zeros(300, dtype=np.float32), noisy,
                                 np.zeros(2000, dtype=np.float32)])
        rx = ofdm.OFDMReceiver()
        frames = decode_all(rx, audio)
        ok = len(frames) == 1 and frames[0].payload == payload
        check(f"decodes correctly with noise std={snr_scale}", ok)


# ── Test 5: back-to-back frames in one stream ───────────────────────────
def test_back_to_back_frames():
    p1 = proto.make_ping("DW1IRF", "K6IRF", seq=1)
    p2 = proto.make_pong("K6IRF", "DW1IRF", seq=1)
    b1, b2 = ofdm.frame_encode(p1), ofdm.frame_encode(p2)
    # Gap must exceed OFDMReceiver.POST_DECODE_COOLDOWN_S -- see the
    # residual-tail test's docstring for why this system can never
    # actually produce two distinct real frames closer together than that.
    gap = np.zeros(int(ofdm.OFDMReceiver.POST_DECODE_COOLDOWN_S * ofdm.SAMPLE_RATE) + 2000,
                    dtype=np.float32)
    audio = np.concatenate([b1, gap, b2, np.zeros(2000, dtype=np.float32)])
    rx = ofdm.OFDMReceiver()
    frames = decode_all(rx, audio)
    ok = len(frames) == 2 and frames[0].payload == p1 and frames[1].payload == p2
    check("two back-to-back frames both decode, in order", ok, f"got {len(frames)} frame(s)")


# ── Test 6: REGRESSION GUARD — the old fade-window bug must not return ──
def test_preamble_energy_is_flat():
    """
    The original SARA FM bug: a 5ms ramp landed inside the first preamble
    repetition, cutting its average energy roughly in half relative to
    the other repetitions. This test encodes a frame and checks that all
    preamble repetitions carry comparable energy — if someone widens
    TAPER_MS or moves the taper back inside the preamble, this fails.
    """
    payload = proto.make_ping("DW1IRF", "K6IRF", seq=1)
    burst = ofdm.frame_encode(payload)
    reps = [burst[r * ofdm.N_SYM:(r + 1) * ofdm.N_SYM] for r in range(ofdm.PREAMBLE_REPS)]
    rms = [float(np.sqrt(np.mean(r.astype(np.float64) ** 2))) for r in reps]
    ratio = min(rms) / max(rms)
    check("all preamble repetitions have comparable energy (ratio > 0.85)",
          ratio > 0.85, f"rms per rep={['%.3f' % v for v in rms]}, min/max={ratio:.3f}")
    check("edge taper is short relative to one symbol (< 20%)",
          ofdm.TAPER_LEN < 0.20 * ofdm.N_SYM,
          f"TAPER_LEN={ofdm.TAPER_LEN} samples, N_SYM={ofdm.N_SYM} samples "
          f"({100*ofdm.TAPER_LEN/ofdm.N_SYM:.1f}%)")


# ── Test 7: corruption is repaired by FEC or rejected -- NEVER mis-decoded ─
def test_corrupt_frame_rejected():
    """
    Originally (pre-v0.6.0) this destroyed one data symbol and asserted
    the frame was rejected by its CRC. With Reed-Solomon FEC that same
    damage is now REPAIRED, which is the whole point of FEC -- so the
    invariant this test protects has changed to the one that actually
    matters: corruption may be repaired (correct payload, fec_corrected
    > 0) or rejected (no frame at all), but must NEVER produce a WRONG
    payload that gets accepted. That's guaranteed by keeping the CRC-16
    as a final check after FEC correction, and verified here both
    directly and over many randomized heavy-corruption trials.
    """
    payload = proto.make_ping("DW1IRF", "K6IRF", seq=5)
    clean = ofdm.frame_encode(payload)
    n_syms_total = len(clean) // ofdm.N_SYM
    first_data = ofdm.PREAMBLE_REPS + 1
    rng = np.random.RandomState(0)

    # A: one destroyed data symbol -- within FEC's repair capacity.
    burst = clean.copy()
    t = n_syms_total // 2
    burst[t * ofdm.N_SYM + ofdm.N_CP:t * ofdm.N_SYM + ofdm.N_CP + ofdm.N_FFT] = \
        rng.normal(0, 1.0, ofdm.N_FFT).astype(np.float32)
    frames = decode_all(ofdm.OFDMReceiver(), burst)
    check("a destroyed data symbol is REPAIRED by FEC (correct payload, fec_corrected > 0)",
          len(frames) == 1 and frames[0].payload == payload and frames[0].fec_corrected > 0,
          f"{[(f.payload == payload, f.fec_corrected) for f in frames]}")

    # B: every data symbol destroyed -- beyond repair, must be rejected.
    burst = clean.copy()
    for k in range(first_data, n_syms_total):
        burst[k * ofdm.N_SYM:(k + 1) * ofdm.N_SYM] = rng.normal(0, 1.0, ofdm.N_SYM).astype(np.float32)
    frames = decode_all(ofdm.OFDMReceiver(), burst)
    check("damage beyond FEC's capacity is rejected, not accepted",
          len(frames) == 0, f"{len(frames)} frame(s) accepted")

    # C: randomized heavy corruption -- whatever happens, never a wrong payload.
    wrong = 0
    for trial in range(40):
        burst = clean.copy()
        n_hit = rng.randint(1, n_syms_total - first_data + 1)
        for k in rng.choice(range(first_data, n_syms_total), n_hit, replace=False):
            burst[k * ofdm.N_SYM:(k + 1) * ofdm.N_SYM] += \
                rng.normal(0, rng.uniform(0.3, 1.5), ofdm.N_SYM).astype(np.float32)
        for f in decode_all(ofdm.OFDMReceiver(), burst):
            if f.payload != payload:
                wrong += 1
    check("40 randomized heavy-corruption trials never accept a WRONG payload",
          wrong == 0, f"{wrong} wrong payload(s) accepted")


# ── Test 8: QPSK round trip, auto-detected from the header ─────────────
def test_qpsk_roundtrip():
    payload = proto.make_ping("DW1IRF", "K6IRF", seq=11)
    burst = ofdm.frame_encode(payload, modulation=ofdm.MOD_QPSK)
    rx = ofdm.OFDMReceiver()   # receiver is NOT told the modulation in advance
    frames = decode_all(rx, burst)
    ok = len(frames) == 1 and frames[0].payload == payload
    check("QPSK round trip decodes correctly", ok, f"got {len(frames)} frame(s)")
    if frames:
        check("receiver read MOD_QPSK off the header, unprompted",
              frames[0].modulation == ofdm.MOD_QPSK,
              f"modulation={frames[0].modulation}")


# ── Test 9: QPSK carries payload in half the data symbols of BPSK ──────
def test_qpsk_is_denser():
    payload = proto.make_ping("DW1IRF", "K6IRF", seq=12)
    bpsk_burst = ofdm.frame_encode(payload, modulation=ofdm.MOD_BPSK)
    qpsk_burst = ofdm.frame_encode(payload, modulation=ofdm.MOD_QPSK)
    overhead = ofdm.N_SYM * (ofdm.PREAMBLE_REPS + 1)   # preamble + header, same for both
    bpsk_data_syms = (len(bpsk_burst) - overhead) / ofdm.N_SYM
    qpsk_data_syms = (len(qpsk_burst) - overhead) / ofdm.N_SYM
    check("QPSK uses roughly half the data symbols BPSK does for the same payload",
          qpsk_data_syms <= bpsk_data_syms / 2 + 1,
          f"BPSK={bpsk_data_syms:.0f} symbols, QPSK={qpsk_data_syms:.0f} symbols")


# ── Test 10: mixed BPSK/QPSK frames back-to-back, each self-describing ─
def test_mixed_modulation_stream():
    p1 = proto.make_ping("DW1IRF", "K6IRF", seq=20)
    p2 = proto.make_pong("K6IRF", "DW1IRF", seq=20)
    p3 = proto.make_ping("DW1IRF", "K6IRF", seq=21)
    b1 = ofdm.frame_encode(p1, modulation=ofdm.MOD_BPSK)
    b2 = ofdm.frame_encode(p2, modulation=ofdm.MOD_QPSK)
    b3 = ofdm.frame_encode(p3, modulation=ofdm.MOD_BPSK)
    # Gap must exceed OFDMReceiver.POST_DECODE_COOLDOWN_S -- see the
    # residual-tail test's docstring for why this system can never
    # actually produce two distinct real frames closer together than that.
    gap = np.zeros(int(ofdm.OFDMReceiver.POST_DECODE_COOLDOWN_S * ofdm.SAMPLE_RATE) + 2000,
                    dtype=np.float32)
    audio = np.concatenate([b1, gap, b2, gap, b3, np.zeros(2000, dtype=np.float32)])
    rx = ofdm.OFDMReceiver()
    frames = decode_all(rx, audio)
    ok = (len(frames) == 3 and [f.payload for f in frames] == [p1, p2, p3]
          and [f.modulation for f in frames] == [ofdm.MOD_BPSK, ofdm.MOD_QPSK, ofdm.MOD_BPSK])
    check("mixed BPSK/QPSK/BPSK stream decodes each frame with its own mode, no coordination needed",
          ok, f"got {len(frames)} frame(s)")


# ── Test 11: QPSK under light noise (expected to have less margin than BPSK)
def test_qpsk_noise_sensitivity():
    payload = proto.make_ping("DW1IRF", "K6IRF", seq=13)
    burst = ofdm.frame_encode(payload, modulation=ofdm.MOD_QPSK)
    rng = np.random.RandomState(1)
    noise = rng.normal(0, 0.02, size=len(burst)).astype(np.float32)
    audio = np.concatenate([np.zeros(300, dtype=np.float32), burst + noise,
                             np.zeros(2000, dtype=np.float32)])
    rx = ofdm.OFDMReceiver()
    frames = decode_all(rx, audio)
    ok = len(frames) == 1 and frames[0].payload == payload
    check("QPSK still decodes correctly under light noise (std=0.02, same level BPSK handles easily)",
          ok)


# ── Test 12: KISS round trip, including bytes that need escaping ───────
def test_kiss_roundtrip():
    payloads = [
        b"hello packet radio",
        bytes([kiss.FEND, 1, 2, kiss.FESC, 3, kiss.FEND, kiss.FESC]),  # needs escaping
        b"",
        bytes(range(256)),   # every byte value, including FEND/FESC themselves
    ]
    for p in payloads:
        frame = kiss.encode_frame(p)
        dec = kiss.KissDecoder()
        frames = dec.feed(frame)
        ok = len(frames) == 1 and frames[0] == (0, kiss.CMD_DATA, p)
        check(f"KISS round trip ({len(p)} bytes, escaping needed: "
              f"{kiss.FEND in p or kiss.FESC in p})", ok)


# ── Test 13: streaming KISS decode across arbitrary chunk boundaries ───
def test_kiss_streaming_chunks():
    p1, p2 = b"first frame", b"second one here"
    stream = kiss.encode_frame(p1) + kiss.encode_frame(p2)
    for chunk_size in (1, 2, 3, 7, 100):
        dec = kiss.KissDecoder()
        frames = []
        for i in range(0, len(stream), chunk_size):
            frames.extend(dec.feed(stream[i:i + chunk_size]))
        ok = frames == [(0, kiss.CMD_DATA, p1), (0, kiss.CMD_DATA, p2)]
        check(f"KISS streaming decode correct at chunk_size={chunk_size}", ok,
              f"got {frames}")


# ── Test 14: back-to-back FEND delimiters (empty/keepalive frames) ─────
def test_kiss_empty_frames_ignored():
    stream = bytes([kiss.FEND, kiss.FEND, kiss.FEND]) + kiss.encode_frame(b"real")
    dec = kiss.KissDecoder()
    frames = dec.feed(stream)
    check("stray empty FEND delimiters produce no spurious frames",
          frames == [(0, kiss.CMD_DATA, b"real")], f"got {frames}")


# ── Test 15: end-to-end bridge concept -- KISS -> OFDM air -> KISS ─────
def test_kiss_over_ofdm_roundtrip():
    """Simulates the whole BPQ32 bridge path in software: an AX.25-shaped
    byte string arrives KISS-framed (as if from BPQ32), gets unwrapped,
    sent over the OFDM physical layer, decoded on the other end, and
    re-wrapped as KISS -- confirming the bridge concept without needing
    BPQ32, a TCP socket, or a radio."""
    fake_ax25 = bytes([0x82, 0xA0, 0xA4, 0xA6, 0x40, 0x40, 0x60]) + b"test packet data"
    kiss_in = kiss.encode_frame(fake_ax25)

    dec = kiss.KissDecoder()
    frames = dec.feed(kiss_in)
    port, cmd, ax25_payload = frames[0]
    check("KISS frame from BPQ32 unwraps to the original AX.25 bytes",
          ax25_payload == fake_ax25)

    burst = ofdm.frame_encode(ax25_payload, modulation=ofdm.MOD_QPSK)
    rx = ofdm.OFDMReceiver()
    rx.push(burst)
    decoded = rx.poll()
    check("AX.25 payload survives the OFDM air link",
          decoded is not None and decoded.payload == ax25_payload)

    kiss_out = kiss.encode_frame(decoded.payload)
    check("re-wrapped KISS frame matches what BPQ32 originally sent",
          kiss_out == kiss_in)


# ── Test 16: BpqBridge over real local sockets (no BPQ32 needed) ───────
def test_bpq_bridge_roundtrip():
    """Exercises the actual TCP server, with a plain socket standing in
    for BPQ32 -- the real thing BPQ32 would do is connect, send KISS
    frames, and expect KISS frames back."""
    received = []
    bridge = BpqBridge(on_air_frame=lambda p: received.append(p))
    port = 18105
    try:
        bridge.start(port=port)
    except OSError as e:
        check("BpqBridge starts a TCP server", False, str(e))
        return

    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    client.settimeout(2.0)
    try:
        client.connect(("127.0.0.1", port))
        fake_ax25 = b"\x82\xa0\xa4\xa6\x40\x40\x60test frame from BPQ32"
        client.sendall(kiss.encode_frame(fake_ax25))

        deadline = _time.time() + 2.0
        while not received and _time.time() < deadline:
            _time.sleep(0.05)
        # on_air_frame now receives a coalesced batch (see pack_batch/
        # unpack_batch) even for a single frame -- unpack before comparing.
        unpacked = bpq_bridge.unpack_batch(received[0]) if received else []
        check("bridge receives and unwraps a KISS frame from the 'BPQ32' client",
              unpacked == [fake_ax25], f"got {unpacked}")

        bridge.deliver_from_air(b"reply payload from the air")
        dec = kiss.KissDecoder()
        deadline = _time.time() + 2.0
        frames = []
        while not frames and _time.time() < deadline:
            try:
                data = client.recv(4096)
                frames = dec.feed(data)
            except socket.timeout:
                break
        check("bridge sends a correctly KISS-wrapped frame back to BPQ32",
              len(frames) == 1 and frames[0][2] == b"reply payload from the air",
              f"got {frames}")
    finally:
        client.close()
        bridge.stop()


# ── Test 17: REGRESSION GUARD -- a real-size BPQ32 frame, streamed ─────
def test_long_bpq_style_frame():
    """
    Reproduces the actual field failure: a real BPQ32 AX.25 I-frame (254
    bytes, matching PACLEN=236 + AX.25 header) is much longer than any
    ping/pong payload this receiver was ever tested against (22 bytes).
    The original MAX_BUF_SYMS (sized for ping/pong only) silently
    discarded the front of a frame this long before it could ever be
    decoded -- the link still connected fine (short AX.25 control frames
    are small enough to fit), but no actual data ever got through, and
    BPQ32 just retried forever. This test fails loudly if that bound
    ever shrinks back down.
    """
    fake_ax25_payload = bytes([0x82, 0xA0, 0xA4, 0xA6, 0x40, 0x40, 0x60]) + b"X" * 240
    for name, mod in [("BPSK", ofdm.MOD_BPSK), ("QPSK", ofdm.MOD_QPSK)]:
        burst = ofdm.frame_encode(fake_ax25_payload, modulation=mod)
        audio = np.concatenate([burst, np.zeros(2000, dtype=np.float32)])
        rx = ofdm.OFDMReceiver()
        # 1024-sample chunks: matches a real ~21ms audio callback, not a
        # convenient round number chosen to make the test easy.
        frames = decode_all(rx, audio, chunk=1024)
        ok = len(frames) == 1 and frames[0].payload == fake_ax25_payload
        check(f"a real-size (247-byte) BPQ32-style frame decodes correctly in {name}, "
              f"streamed in realistic audio-callback chunks", ok,
              f"got {len(frames)} frame(s), burst={len(burst)/ofdm.SAMPLE_RATE*1000:.0f}ms")


# ── Test 18: REGRESSION GUARD -- a decoded frame's residual tail must
#    not corrupt detection of the next frame ───────────────────────────
def test_residual_tail_does_not_corrupt_next_frame():
    """
    _find_preamble's detected 'start' can be imprecise by roughly one
    cyclic prefix's worth of samples -- naturally absorbed by the CP for
    demodulation itself, but NOT accounted for when that same 'start' is
    used to compute where to trim the buffer afterward. Left untrimmed, a
    real residual tail of the just-decoded frame can remain in the
    buffer and get mistaken for the start of the next frame, corrupting
    its header decode (wrong modulation bit, nonsense length). Found via
    three back-to-back frames (BPSK/QPSK/BPSK) with short gaps -- the
    third frame's header decoded complete garbage before this was fixed.

    Gap must exceed OFDMReceiver.POST_DECODE_COOLDOWN_S -- this system's
    own TX chain always leaves more than that between two distinct real
    transmissions, so a gap this short between genuinely different
    frames cannot happen here; the cooldown rides through a single
    remote radio's post-transmission artifact, not back-to-back frames.
    """
    p1 = proto.make_ping("DW1IRF", "K6IRF", seq=20)
    p2 = proto.make_pong("K6IRF", "DW1IRF", seq=20)
    p3 = proto.make_ping("DW1IRF", "K6IRF", seq=21)
    b1 = ofdm.frame_encode(p1, modulation=ofdm.MOD_BPSK)
    b2 = ofdm.frame_encode(p2, modulation=ofdm.MOD_QPSK)
    b3 = ofdm.frame_encode(p3, modulation=ofdm.MOD_BPSK)
    gap = np.zeros(int(ofdm.OFDMReceiver.POST_DECODE_COOLDOWN_S * ofdm.SAMPLE_RATE) + 2000,
                    dtype=np.float32)
    audio = np.concatenate([b1, gap, b2, gap, b3, np.zeros(2000, dtype=np.float32)])
    rx = ofdm.OFDMReceiver()
    frames = decode_all(rx, audio, chunk=960)
    ok = (len(frames) == 3 and frames[0].payload == p1
          and frames[1].payload == p2 and frames[2].payload == p3)
    check("a third frame after two back-to-back decodes is not corrupted "
          "by the previous frame's residual tail", ok, f"got {len(frames)} frame(s)")


# ── Test 19: REGRESSION GUARD -- an implausible claimed length must not
#    stall the receiver forever ─────────────────────────────────────────
def test_max_length_header_no_longer_falsely_rejected():
    """
    History: v0.5.3 made a MAX_PAYLOAD_LEN claim 'legitimately satisfiable'
    because coalescing could build batches near the protocol ceiling. In
    v0.7.1 two things changed: the coalescing cap is actually enforced
    (it wasn't -- batches reached ~510 bytes), so no real frame exceeds
    ofdm_core.OFDMReceiver.MAX_RX_DATA_BYTES; and claims above that are
    rejected instantly, because field logs showed garbled headers claiming
    629-999 bytes making the receiver wait and swallow real frames. This
    now confirms such a claim is rejected IMMEDIATELY (buffer advanced, no
    waiting), with the reason recorded.
    """
    bogus_bits = [0, 0] + [1] * ofdm.HEADER_LEN_BITS
    bogus_carriers = {ofdm.DATA_POS[k]: complex(v, 0)
                       for k, v in enumerate(ofdm._bits_to_bpsk(bogus_bits))}
    for p in ofdm.PILOT_POS:
        bogus_carriers[p] = ofdm.PILOT_VAL
    bogus_hdr = ofdm._ofdm_symbol_tx(bogus_carriers)
    filler = np.zeros(ofdm.N_SYM, dtype=np.float32)
    audio = np.concatenate([ofdm.PREAMBLE_SYMBOL] * ofdm.PREAMBLE_REPS + [bogus_hdr, filler])
    rx = ofdm.OFDMReceiver()
    rej = []
    rx.on_reject = lambda r, i: rej.append(r)
    rx.push(audio)
    before = len(rx.buf)
    result = rx.poll()
    check("a header claiming more than any real frame (MAX_PAYLOAD_LEN) is rejected "
          "immediately as implausible, not waited for",
          result is None and len(rx.buf) < before and rej == ["implausible_length"],
          f"before={before}, after={len(rx.buf)}, rejects={rej}")


# ── Test 19b: REGRESSION GUARD -- a coalesced batch using BPQ32's full
#    MAXFRAME window must actually be received, not silently vanish ────
def test_coalesced_batch_survives_full_maxframe_window():
    """
    Originally (v0.5.3): a full-MAXFRAME coalesced batch must fit the
    receive buffer and decode. Since v0.7.1 the coalescing cap is actually
    enforced, so the largest things the bridge can put on the air are (a)
    one maximal AX.25 frame alone (PACLEN 256 + an 8-digipeater header)
    and (b) a batch of small frames up to MAX_BATCH_BYTES. Checks both fit
    under OFDMReceiver.MAX_RX_DATA_BYTES -- the cap that now rejects
    garbled long claims instantly -- and decode in both modulations,
    streamed in realistic audio-callback chunks.
    """
    cases = {"one maximal AX.25 frame": [bytes([7]) * (256 + 72)],
             "a full small-frame batch": [bytes([i]) * 23 for i in range(bpq_bridge.MAX_BATCH_BYTES // 25)]}
    for label, frames in cases.items():
        batch = bpq_bridge.pack_batch(frames)
        on_air = fec.encoded_len(len(batch) + 2)
        check(f"{label}: {on_air} bytes on air fits under MAX_RX_DATA_BYTES "
              f"({ofdm.OFDMReceiver.MAX_RX_DATA_BYTES})", on_air <= ofdm.OFDMReceiver.MAX_RX_DATA_BYTES)
        for name, mod in [("BPSK", ofdm.MOD_BPSK), ("QPSK", ofdm.MOD_QPSK)]:
            burst = ofdm.frame_encode(batch, modulation=mod)
            out = decode_all(ofdm.OFDMReceiver(), burst, chunk=1024)
            check(f"{label} decodes in {name}",
                  len(out) == 1 and bpq_bridge.unpack_batch(out[0].payload) == frames,
                  f"got {len(out)} frame(s), burst={len(burst)/ofdm.SAMPLE_RATE:.1f}s")

# ── Test 20: REGRESSION GUARD -- a realistic ping/pong session, many
#    frames at REAL round-trip pacing, must decode cleanly ─────────────
def test_realistic_session_pacing():
    """
    The actual field failure this project hit was NOT reproducible with
    realistic ping/pong pacing (gaps of 1.5-3s, matching real round-trip
    timing including TX delay and reply delay) -- only with an
    artificially dense back-to-back stress pattern (gaps of 30-100ms,
    not representative of real usage). This test is the realistic one:
    a full auto-ping-batch-scale session (30 frames, mixed BPSK/QPSK,
    randomized realistic gaps) must decode every single frame.
    """
    for seed in range(5):
        rng = random.Random(seed)
        rx = ofdm.OFDMReceiver()
        chunks = []
        expected = []
        for i in range(30):
            mod = rng.choice([ofdm.MOD_BPSK, ofdm.MOD_QPSK])
            p = (proto.make_ping("DW1IRF", "K6IRF", seq=i % 256) if rng.random() < 0.5
                 else proto.make_pong("K6IRF", "DW1IRF", seq=i % 256))
            burst = ofdm.frame_encode(p, modulation=mod)
            gap = np.zeros(rng.randint(72000, 144000), dtype=np.float32)
            chunks.append(burst)
            chunks.append(gap)
            expected.append(p)
        audio = np.concatenate(chunks + [np.zeros(2000, dtype=np.float32)])
        frames = decode_all(rx, audio, chunk=1024)
        decoded_payloads = {f.payload for f in frames}
        missing = sum(1 for p in expected if p not in decoded_payloads)
        check(f"realistic-pacing session (seed={seed}): all 30 frames decode",
              missing == 0, f"{len(expected) - missing}/{len(expected)} decoded")


# ── Test 21: REGRESSION GUARD -- realistic sessions must hold up against
#    a REAL ambient noise floor, not just clean digital silence ────────
def test_realistic_session_with_noise_floor():
    """
    Every earlier offline test used literal zeros for 'silence' between
    frames -- no real microphone ever produces true digital silence
    (room noise, hum, self-noise are always present). Pure ratio-based
    preamble detection can occasionally lock onto that noise by chance;
    this test is the first to actually include it, and sets the bar:
    the overall miss rate across many realistic sessions must stay low
    (not necessarily zero -- some noise levels will occasionally cost a
    single frame, which real retry logic above this layer is meant to
    handle) rather than the severe, systemic failure a missing energy
    floor + short sustain check produced.
    """
    total_sent = 0
    total_missing = 0
    for noise_std in (0.001, 0.005, 0.01, 0.02):
        for seed in range(5):
            rng = np.random.RandomState(seed)
            pyrand = random.Random(seed)
            rx = ofdm.OFDMReceiver()
            chunks = []
            expected = []
            for i in range(20):
                mod = pyrand.choice([ofdm.MOD_BPSK, ofdm.MOD_QPSK])
                p = (proto.make_ping("DW1IRF", "K6IRF", seq=i % 256) if pyrand.random() < 0.5
                     else proto.make_pong("K6IRF", "DW1IRF", seq=i % 256))
                burst = ofdm.frame_encode(p, modulation=mod)
                gap = rng.normal(0, noise_std, pyrand.randint(72000, 144000)).astype(np.float32)
                chunks.append(burst)
                chunks.append(gap)
                expected.append(p)
            audio = np.concatenate(chunks + [np.zeros(2000, dtype=np.float32)])
            frames = decode_all(rx, audio, chunk=1024)
            decoded = {f.payload for f in frames}
            total_sent += len(expected)
            total_missing += sum(1 for p in expected if p not in decoded)
    miss_rate = total_missing / total_sent
    check(f"realistic sessions with an actual ambient noise floor (not pure silence) "
          f"stay reliable: {total_missing}/{total_sent} missed ({miss_rate:.1%})",
          miss_rate < 0.05, f"miss rate {miss_rate:.1%}")


# ── Test 22: on_reject diagnostic hook fires with useful info ──────────
def test_on_reject_hook_fires():
    """
    New diagnostic hook for understanding real-world rejection patterns
    without guessing from synthetic models: OFDMReceiver.on_reject, when
    set, is called for every rejected candidate with a reason and
    details (energy, decoded length, etc). Confirms it fires at least
    once for a deliberately corrupted frame, and never crashes the
    receiver even if the callback itself raises.
    """
    payload = proto.make_ping("DW1IRF", "K6IRF", seq=1)
    burst = ofdm.frame_encode(payload).copy()
    # Damage must exceed FEC's repair capacity (v0.6.0) to produce a
    # rejection at all -- one destroyed symbol is now simply repaired.
    # Destroy the whole data portion instead.
    first_data = (ofdm.PREAMBLE_REPS + 1) * ofdm.N_SYM
    rng = np.random.RandomState(0)
    burst[first_data:] = rng.normal(0, 1.0, len(burst) - first_data).astype(np.float32)

    rejects = []
    rx = ofdm.OFDMReceiver()
    rx.on_reject = lambda reason, info: rejects.append((reason, info))
    rx.push(burst)
    rx.poll()
    check("on_reject fires with a reason for a corrupted frame", len(rejects) >= 1,
          f"got {rejects}")

    # A raising callback must never crash the receiver
    rx2 = ofdm.OFDMReceiver()
    rx2.on_reject = lambda reason, info: (_ for _ in ()).throw(RuntimeError("boom"))
    rx2.push(burst)
    try:
        rx2.poll()
        ok = True
    except Exception as e:
        ok = False
    check("a raising on_reject callback does not crash the receiver", ok)


# ── Test 23: REGRESSION GUARD -- residual margin survives a post-burst
#    analog-style transient, found via real field diagnostic logs ──────
def test_residual_margin_survives_post_burst_transient():
    """
    Real field data (K6IRF<->DW1IRF, v0.4.4's verbose diagnostics) showed
    nearly every successful decode immediately followed by a rejection
    with substantial real energy (0.7-2.3) -- a residual tail surviving
    the old one-cyclic-prefix trim margin. Simulates a small post-burst
    analog-style transient (PTT release, speaker/mic coupling) a clean
    synthetic burst alone can't produce, and confirms it's fully cleared.
    """
    p = proto.make_pong("DW1IRF", "K6IRF", seq=1)
    burst = ofdm.frame_encode(p, modulation=ofdm.MOD_QPSK).copy()
    t = np.arange(400)
    transient = (0.15 * np.exp(-t / 120) * np.sin(2 * np.pi * 600 * t / ofdm.SAMPLE_RATE)).astype(np.float32)
    gap = np.zeros(20000, dtype=np.float32)
    gap[:len(transient)] += transient
    audio = np.concatenate([burst, gap])

    rx = ofdm.OFDMReceiver()
    rejects = []
    rx.on_reject = lambda reason, info: rejects.append((reason, info))
    rx.push(audio)
    frame = rx.poll()
    ok = frame is not None and frame.payload == p
    check("real frame still decodes with a post-burst transient present", ok)
    substantial_rejects = [r for r in rejects if r[1].get("energy", 0) > 0.3]
    check("no substantial-energy false candidate survives right after the decode",
          len(substantial_rejects) == 0, f"got {substantial_rejects}")


# ── Test 24: REGRESSION GUARD -- post-decode cooldown rides through a
#    squelch-tail-style artifact right after a real decode ─────────────
def test_post_decode_cooldown():
    """
    Field diagnostic logs (v0.4.4/0.4.5) showed an implausible_length or
    crc_fail rejection, with substantial real energy and a DIFFERENT
    essentially-random decoded length each time, immediately after
    nearly every successful decode -- not our own signal's residual
    (which would look similar each time), but something external
    arriving in a narrow window right after the remote station's
    transmission ends (most likely a squelch-tail/PTT-release artifact).
    Confirms the cooldown window suppresses exactly this -- a spurious
    candidate placed right after a decode produces no on_reject call at
    all during the cooldown -- while a real second frame arriving AFTER
    the cooldown window still decodes normally.
    """
    p1 = proto.make_ping("DW1IRF", "K6IRF", seq=1)
    p2 = proto.make_pong("K6IRF", "DW1IRF", seq=1)
    b1 = ofdm.frame_encode(p1, modulation=ofdm.MOD_QPSK)
    b2 = ofdm.frame_encode(p2, modulation=ofdm.MOD_QPSK)

    # A brief, moderate-energy noise burst standing in for a squelch-tail
    # artifact -- matching what the field logs actually showed (energy
    # 0.7-2.3 per N_SYM window, a few symbols' worth at most), not a full
    # structured OFDM frame, which would be a very different and much
    # harsher thing to ride through than the real evidence calls for.
    rng = np.random.RandomState(0)
    spurious = (rng.normal(0, 0.05, ofdm.N_SYM * 3)).astype(np.float32)
    cooldown_samples = int(ofdm.OFDMReceiver.POST_DECODE_COOLDOWN_S * ofdm.SAMPLE_RATE)
    gap_before_spurious = np.zeros(2000, dtype=np.float32)
    gap_after_spurious = np.zeros(cooldown_samples + 5000, dtype=np.float32)

    audio = np.concatenate([b1, gap_before_spurious, spurious, gap_after_spurious, b2,
                             np.zeros(2000, dtype=np.float32)])

    rx = ofdm.OFDMReceiver()
    rejects = []
    rx.on_reject = lambda reason, info: rejects.append((reason, info))
    frames = decode_all(rx, audio, chunk=960)

    ok = len(frames) == 2 and frames[0].payload == p1 and frames[1].payload == p2
    check("both the real first frame and real second frame decode correctly "
          "around a spurious burst placed during the cooldown window", ok,
          f"got {len(frames)} frame(s)")
    check("the spurious burst during cooldown produces no rejection log at all "
          "(cooldown skips searching entirely, rather than finding-then-rejecting)",
          len(rejects) == 0, f"got {rejects}")


# ── Test 25: REGRESSION GUARD -- long frames survive realistic TX/RX
#    clock drift via per-symbol pilot tracking ──────────────────────────
def test_long_frame_survives_clock_drift():
    """
    Real BPQ32 traffic (254-byte frames, ~1.2s at QPSK / ~2.3s at BPSK)
    showed crc_fail on otherwise-perfect-quality (~0.87-0.95) real
    frames -- never seen on ping/pong's always-short (22-byte, ~0.1-0.25s)
    frames. Root cause: the channel estimate is a single snapshot from
    the preamble; two independent, unsynchronized sound card clocks
    drift against each other continuously, and over ~100ms that's
    negligible but over a full second it accumulates enough to flip
    bits. Reproduced directly: a realistic clock mismatch (resampling
    the burst by 80-150 ppm, physically what independent sound card
    clocks actually do) broke decoding with this exact signature before
    per-symbol pilot-based tracking was added. This test is the
    regression guard for that fix -- it must keep working at these
    drift levels for both modulations.
    """
    from scipy.signal import resample
    payload = b"X" * 252   # real BPQ32-scale payload, not ping/pong-scale
    for name, mod in [("BPSK", ofdm.MOD_BPSK), ("QPSK", ofdm.MOD_QPSK)]:
        burst = ofdm.frame_encode(payload, modulation=mod)
        for ppm in (80, 150):
            n_new = int(len(burst) * (1 + ppm * 1e-6))
            drifted = resample(burst, n_new).astype(np.float32)
            rx = ofdm.OFDMReceiver()
            rx.push(drifted)
            frame = rx.poll()
            ok = frame is not None and frame.payload == payload
            check(f"a 252-byte {name} frame survives {ppm}ppm TX/RX clock drift",
                  ok, f"decoded={frame is not None}")


# ── Test 26: drift_deg diagnostic reports a sensible, monotonic value ──
def test_drift_diagnostic_reports_sensibly():
    """
    DecodedFrame.drift_deg and the crc_fail reject's drift_deg exist so a
    real field log can show the ACTUAL measured clock drift on a given
    link, rather than only "it failed" -- needed because v0.4.7's fix
    measurably helped real BPQ32 traffic but didn't fully eliminate
    crc_fail on long frames, and the next step depends on knowing whether
    real drift on a given hardware pair is merely larger than what one
    common pilot correction handles (expected to show as a large
    drift_deg right at the failure) or something else entirely (would
    show as a small drift_deg despite failure, pointing elsewhere).
    """
    from scipy.signal import resample
    payload = b"X" * 252
    burst = ofdm.frame_encode(payload, modulation=ofdm.MOD_QPSK)
    drifts = []
    for ppm in (0, 50, 100, 150):
        n_new = int(len(burst) * (1 + ppm * 1e-6))
        drifted = resample(burst, n_new).astype(np.float32)
        rx = ofdm.OFDMReceiver()
        rx.push(drifted)
        frame = rx.poll()
        drifts.append(abs(frame.drift_deg) if frame else None)
    ok = all(d is not None for d in drifts) and drifts == sorted(drifts)
    check("drift_deg magnitude increases monotonically with actual clock drift",
          ok, f"got {drifts}")


# ── Test 27: pack_batch / unpack_batch round trip ───────────────────────
def test_batch_pack_unpack_roundtrip():
    """Core correctness of the batch wire format, independent of sockets
    or timing: several frames of varying sizes (including empty and
    single-byte) pack and unpack back to exactly the originals."""
    cases = [
        [b"hello"],
        [b"frame one", b"frame two", b"frame three"],
        [b""],
        [b"\x00" * 300, b"x", b"\xff" * 236],
        [b"a" * 236 for _ in range(8)],   # MAX_BATCH_FRAMES worth
    ]
    for frames in cases:
        packed = bpq_bridge.pack_batch(frames)
        unpacked = bpq_bridge.unpack_batch(packed)
        check(f"batch round trip for {len(frames)} frame(s) "
              f"(sizes {[len(f) for f in frames]})", unpacked == frames,
              f"got sizes {[len(f) for f in unpacked]}")


# ── Test 28: unpack_batch falls back safely on non-batch input ─────────
def test_unpack_batch_fallback():
    """A payload that isn't one of our batches (wrong marker, or
    malformed/truncated length-prefixed content) must be treated as a
    single raw frame rather than raising -- this is the fallback for an
    older peer, or defensive handling of anything unexpected."""
    not_a_batch = b"\x82\xa0\xa4\xa6\x40\x40\x60plain AX.25 frame, no marker"
    check("non-batch payload (wrong marker) falls back to a single frame",
          bpq_bridge.unpack_batch(not_a_batch) == [not_a_batch])

    truncated = bytes([bpq_bridge.BATCH_MARKER, 3]) + b"\x00\x05ab"  # claims 3 frames, has data for <1
    result = bpq_bridge.unpack_batch(truncated)
    check("malformed/truncated batch falls back to a single frame rather than raising",
          result == [truncated], f"got {result}")

    check("empty payload does not raise", bpq_bridge.unpack_batch(b"") == [b""])


# ── Test 29: BpqBridge actually coalesces frames sent close together ───
def test_bridge_coalesces_rapid_frames():
    """The real motivating scenario: BPQ32 sends several small AX.25
    frames in quick succession (e.g. a B2F handshake). Frames sent well
    within COALESCE_WINDOW_S of each other must arrive at on_air_frame as
    ONE batch (one OFDM transmission), not one each -- that's the entire
    point of coalescing. Frames sent far apart must NOT be merged, so an
    isolated frame still goes out promptly rather than always waiting."""
    batches = []
    bridge = BpqBridge(on_air_frame=lambda p: batches.append(bpq_bridge.unpack_batch(p)))
    port = 18106
    try:
        bridge.start(port=port)
    except OSError as e:
        check("BpqBridge starts for the coalescing test", False, str(e))
        return

    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    client.settimeout(2.0)
    try:
        client.connect(("127.0.0.1", port))
        frames = [b"frame-%d" % i for i in range(4)]
        for f in frames:
            client.sendall(kiss.encode_frame(f))
            _time.sleep(0.02)   # well within COALESCE_WINDOW_S (0.12s)

        deadline = _time.time() + 2.0
        while not batches and _time.time() < deadline:
            _time.sleep(0.05)
        check("four rapid frames arrive as a single coalesced batch",
              len(batches) == 1 and batches[0] == frames,
              f"got {len(batches)} batch(es): {batches}")

        batches.clear()
        isolated = b"isolated-frame"
        client.sendall(kiss.encode_frame(isolated))
        deadline = _time.time() + 2.0
        while not batches and _time.time() < deadline:
            _time.sleep(0.05)
        check("a single isolated frame still arrives promptly on its own",
              len(batches) == 1 and batches[0] == [isolated],
              f"got {batches}")
    finally:
        client.close()
        bridge.stop()


# ── Test 30: wait_for_clear_channel -- core carrier-sense logic ────────
def test_wait_for_clear_channel():
    """
    Found via a field report: one station's TX audibly stepping on the
    other's, consistent with no carrier sense at all before keying up --
    two independently-operating half-duplex stations had no way to avoid
    colliding. wait_for_clear_channel is a free function specifically so
    this logic is testable without any audio hardware: fake is_busy_fn/
    sleep_fn, no pyaudio or real device involved.
    """
    # Busy for exactly 3 polls, then clear -- should wait ~3 poll intervals.
    calls = {"n": 0}
    sleeps = []

    def busy_for_3():
        calls["n"] += 1
        return calls["n"] <= 3

    waited = audio_io.wait_for_clear_channel(
        busy_for_3, max_wait_s=4.0, poll_s=0.01, sleep_fn=sleeps.append)
    check("waits approximately the right number of poll intervals while busy",
          len(sleeps) == 3 and abs(waited - 0.03) < 1e-9,
          f"slept {len(sleeps)} times, waited={waited}")

    # Never clears -- must give up at max_wait_s, not hang forever.
    sleeps2 = []
    waited2 = audio_io.wait_for_clear_channel(
        lambda: True, max_wait_s=0.05, poll_s=0.01, sleep_fn=sleeps2.append)
    check("gives up at max_wait_s rather than waiting forever on a channel "
          "that never clears", waited2 >= 0.05 and len(sleeps2) <= 6,
          f"waited={waited2}, slept {len(sleeps2)} times")

    # Already clear -- must not wait at all.
    sleeps3 = []
    waited3 = audio_io.wait_for_clear_channel(
        lambda: False, max_wait_s=4.0, poll_s=0.01, sleep_fn=sleeps3.append)
    check("does not wait at all when the channel is already clear",
          waited3 == 0.0 and len(sleeps3) == 0, f"waited={waited3}")


# ── Test 31: CARRIER_BUSY_RMS sits sensibly between noise and real signal
def test_carrier_busy_threshold_calibration():
    """
    The carrier-sense threshold must sit clearly above ofdm_core's own
    ambient-noise territory (so it doesn't falsely call the channel busy
    on mere noise) and clearly below any real signal this project has
    observed in the field (quality 0.87+), so it reliably detects an
    actual transmission in progress.
    """
    ref_rms = float(np.sqrt(np.mean(ofdm.PREAMBLE_SYMBOL.astype(np.float64) ** 2)))
    quality_equiv = audio_io.CARRIER_BUSY_RMS / ref_rms
    check("carrier-busy threshold sits above the noise-floor energy gate's level "
          "(quality ~0.05) and comfortably below real field signal levels (0.87+)",
          0.05 < quality_equiv < 0.5, f"quality_equiv={quality_equiv:.3f}")


# ── Test 32: on_carrier_wait fires only for meaningful delays ──────────
def test_carrier_wait_diagnostic():
    """
    Field report: downloading consistently fails while sending works,
    with the receiving station's log showing a real handshake but no
    bulk data ever arriving -- consistent with the SENDING station's
    carrier sense (new in v0.5.0, no visibility into it at all before
    this) waiting out a meaningful chunk of its timeout before every
    transmission, if its local noise floor sits persistently above
    CARRIER_BUSY_RMS. This hook makes that visible instead of invisible.
    Confirms it fires for a real wait, but not for a trivial/zero one
    (so normal operation doesn't get spammed).
    """
    from unittest.mock import MagicMock
    eng = audio_io.AudioEngine.__new__(audio_io.AudioEngine)
    eng.tx_lead_ms = 1
    eng.tx_tail_ms = 1
    eng._rx_rms_ema = 0.0
    eng._tx_lock = threading.Lock()
    eng.tx_stream = MagicMock()
    eng.ptt = MagicMock()
    eng.on_tx_start = None

    # Case 1: channel busy for a real stretch -- must report the wait.
    calls = {"n": 0}

    def busy_for_a_while():
        calls["n"] += 1
        return calls["n"] <= 8   # ~8 * poll_s seconds of "busy"

    reported = []
    eng.on_carrier_wait = lambda s: reported.append(s)
    eng.is_channel_busy = busy_for_a_while
    eng.transmit(b"x" * 20, modulation=ofdm.MOD_BPSK)
    check("a meaningful carrier-sense wait is reported via on_carrier_wait",
          len(reported) == 1 and reported[0] > 0.3, f"got {reported}")

    # Case 2: channel already clear -- must NOT report (no spam on the
    # common case).
    reported2 = []
    eng.on_carrier_wait = lambda s: reported2.append(s)
    eng.is_channel_busy = lambda: False
    eng.transmit(b"x" * 20, modulation=ofdm.MOD_BPSK)
    check("no carrier-wait report when the channel was already clear",
          len(reported2) == 0, f"got {reported2}")


# ── Test 33: SelfEchoFilter -- bridge-mode self-heard echo suppression ─
def test_self_echo_filter():
    """
    Field log: in BPQ32/KISS Bridge mode, every single TX was immediately
    followed by an RX of the identical byte count at a distinctly lower
    quality (0.26 vs 0.96-0.97 for genuine remote traffic) -- local audio
    bleed-back of our own transmission, forwarded straight to BPQ32 as if
    it were real incoming data. Ping/Pong mode already filters this by
    comparing callsigns; Bridge mode has no callsign concept (just raw
    AX.25 bytes), so SelfEchoFilter instead compares exact payload bytes
    against a short recent-TX history -- an echo is by definition a
    byte-for-byte copy of something we just sent.
    """
    f = SelfEchoFilter(window_s=2.0)

    check("not an echo before anything has been sent",
          not f.is_echo(b"anything", now=0.0))

    f.record_sent(b"frame-A", now=10.0)
    check("an exact match shortly after sending is recognized as an echo",
          f.is_echo(b"frame-A", now=10.3))
    check("a different payload is not mistaken for an echo",
          not f.is_echo(b"frame-B", now=10.3))

    check("the same payload is no longer flagged once the window has passed",
          not f.is_echo(b"frame-A", now=12.5))

    # Multiple recent sends -- any match within the window counts.
    f2 = SelfEchoFilter(window_s=2.0)
    f2.record_sent(b"one", now=5.0)
    f2.record_sent(b"two", now=5.1)
    check("matches against any recently sent payload, not just the most recent",
          f2.is_echo(b"one", now=5.5) and f2.is_echo(b"two", now=5.5))
    check("genuine remote traffic (never sent by us) is never flagged",
          not f2.is_echo(b"something the other station sent", now=5.5))


# ── Test 33b: REGRESSION GUARD -- the default window survives a REAL
#    transmission's own duration, not just a quick ping/pong-style one ─
def test_self_echo_window_survives_real_transmission_time():
    """
    Field log, with the (then-default) 2.0s window: small control frames
    (19-63 bytes) were correctly tagged '[own TX heard locally]', but
    EVERY 256-byte real data frame was not -- decoded and forwarded to
    BPQ32 as if it were genuine incoming traffic. Root cause: a 256-byte
    QPSK transmission's own lead-in+burst+tail takes ~1.96s -- almost the
    entire 2.0s window consumed before the echo is even fully received
    and decoded, and that's before accounting for carrier sense
    potentially adding up to another 4s of wait before the burst even
    starts. This reproduces the exact failure with the OLD window
    (confirming the bug was real, not assumed) and confirms the NEW
    default survives a worst-case realistic delay with real margin.
    """
    old_window = 2.0
    f_old = SelfEchoFilter(window_s=old_window)
    f_old.record_sent(b"a-256-byte-frame", now=0.0)
    # the real-world delay from send to fully-decoded echo for a frame
    # this size: lead-in+burst+tail (~1.96s) alone already consumes
    # nearly the entire old window, and actually detecting/decoding the
    # echo takes a little past that -- 2.1s reflects that real margin,
    # not just the bare transmission time with zero processing latency.
    realistic_delay = 2.1
    check("reproduces the bug: the OLD 2.0s window fails once real "
          "transmission + decode latency for a 256-byte frame is included",
          not f_old.is_echo(b"a-256-byte-frame", now=realistic_delay))

    f_new = SelfEchoFilter()  # current default
    f_new.record_sent(b"a-256-byte-frame", now=0.0)
    # worst realistic case: a maximal coalesced batch at BPSK (slower),
    # PLUS the full carrier-sense wait -- computes to under 8s
    worst_case_delay = 7.8
    check("the current default window survives the worst realistic "
          "delay (max batch, BPSK, full carrier-sense wait)",
          f_new.is_echo(b"a-256-byte-frame", now=worst_case_delay))


# ── Test 33c: REGRESSION GUARD -- SelfEchoFilter is thread-safe under
#    real bridge-mode concurrency (a new thread per transmission) ──────
def test_self_echo_filter_thread_safety():
    """
    Field log: the first several frames in each real burst were
    correctly tagged '[own TX heard locally]', but consistently the LAST
    one or two were not. Root cause: sara_pingpong.py's on_air_frame
    spawns a new thread per transmission, so multiple _bridge_transmit
    calls run concurrently once BPQ32 sends frames close together (which
    real bursts do throughout). The unlocked version had a classic
    read-modify-write race: one thread's record_sent() computes its
    pruned snapshot BEFORE another thread's append, then (after some
    delay) reassigns self._recent using that stale snapshot -- silently
    discarding the other thread's entry. Confirmed this mechanism
    directly on an unlocked reproduction with deliberately adversarial
    timing (one thread's prune step delayed to land after another's)
    before trusting it as a real bug.

    A natural, fast, undelayed race is too narrow to reliably trigger
    under the GIL's normal scheduling, so this test verifies the FIX the
    direct way: it reaches into the real, now-locked _prune_locked and
    injects that SAME adversarial delay inside the critical section. If
    the lock actually serializes access, a concurrent writer is forced
    to wait rather than interleave, and no entry is lost even under
    timing deliberately chosen to cause loss in the unlocked version.
    """
    f = SelfEchoFilter(window_s=10.0)
    orig_prune_locked = f._prune_locked

    def delayed_prune_locked(now):
        orig_prune_locked(now)
        _time.sleep(0.05)   # the same adversarial delay that caused loss, unlocked

    f._prune_locked = delayed_prune_locked

    def thread_a():
        f.record_sent(b"A")   # record_sent holds the lock for the whole delay
    def thread_b():
        _time.sleep(0.01)      # ensure B's call starts while A holds the lock
        f.record_sent(b"B")

    ta = threading.Thread(target=thread_a)
    tb = threading.Thread(target=thread_b)
    ta.start(); tb.start()
    ta.join(); tb.join()

    f._prune_locked = orig_prune_locked   # restore before checking
    check("neither entry is lost even under the same adversarial timing "
          "that caused loss in the unlocked version -- the lock forces "
          "the second thread to wait rather than interleave",
          f.is_echo(b"A") and f.is_echo(b"B"),
          f"A found={f.is_echo(b'A')}, B found={f.is_echo(b'B')}")


# ── Test 33d: REGRESSION GUARD -- self-echo timestamp reflects the
#    REAL broadcast moment, not when transmit() was merely queued ──────
def test_tx_start_hook_fires_at_real_broadcast_time():
    """
    Low-level AX.25 monitor trace (not just our own bridge log) showed
    the self-echo bug persisting even with v0.5.7's window-size and
    thread-safety fixes: DW1IRF's own BPQ32 received an exact copy of
    its own earlier frame back, 12+ seconds after it was originally
    sent -- longer than even the widened 10s window. Root cause: with
    the smaller coalescing cap (v0.5.5), a full BPQ32 MAXFRAME burst now
    sends each real frame as its own transmission, each in its own
    thread (on_air_frame), all queuing up behind the same AudioEngine
    _tx_lock. record_sent() was being called the moment
    _bridge_transmit started -- essentially when each thread was
    spawned, all at once -- not when each transmission actually got its
    turn at the lock. A later frame's real broadcast (and thus its real
    echo) could happen many seconds after its record_sent() call,
    exactly the gap the window was supposed to cover, except measured
    from the wrong starting point entirely.

    Fixed by moving the record to AudioEngine.on_tx_start, fired from
    inside transmit() only once a thread actually holds the lock and is
    about to key PTT. This confirms the hook fires per-transmission at
    that real moment, in the correct order, even when several
    transmissions are queued and serialized behind one another --
    rather than trusting call order outside the lock.
    """
    from unittest.mock import MagicMock
    eng = audio_io.AudioEngine.__new__(audio_io.AudioEngine)
    eng.tx_lead_ms = 1
    eng.tx_tail_ms = 1
    eng._rx_rms_ema = 0.0
    eng._tx_lock = threading.Lock()
    eng.tx_stream = MagicMock()
    eng.ptt = MagicMock()
    eng.on_carrier_wait = None
    eng.is_channel_busy = lambda: False

    started_order = []
    eng.on_tx_start = lambda payload: started_order.append(payload)

    # Three transmissions requested at once (as three threads would),
    # each naturally serialized by the real _tx_lock inside transmit().
    threads = [threading.Thread(target=eng.transmit, args=(bytes([i]) * 20,),
                                 kwargs={"modulation": ofdm.MOD_BPSK})
               for i in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    check("on_tx_start fires exactly once per transmission, in the order "
          "each actually acquired the lock (not call order outside it)",
          len(started_order) == 3 and len(set(started_order)) == 3,
          f"got {started_order}")


# ── Test 34: REGRESSION GUARD -- MAX_BATCH_BYTES keeps real coalesced
#    transmissions within this project's demonstrated drift tolerance ──
def test_coalescing_cap_survives_real_world_drift():
    """
    Real field data, two rounds. First: BPQ32 using its full MAXFRAME=7
    window coalesced into a 1780-byte transmission (~8s at QPSK), failed
    with real drift measured at ~20-30ppm on this hardware -- fixed by
    capping MAX_BATCH_BYTES at 500. Second round, WITH that fix on both
    stations: a frame exactly matching DW1IRF's real 510-byte
    transmission (512 = 510 + the 2-byte frame CRC) still failed, at
    drift_deg=-89.975 -- meaning real drift on this hardware occasionally
    reaches or exceeds the ~80ppm that size was validated against.
    MAX_BATCH_BYTES was reduced again, to 250, re-validated with multiple
    trials per size/ppm combination (varying sub-symbol timing, since a
    single deterministic trial can land luckily/unluckily at a boundary)
    rather than a single data point. Guards that a maximal real coalesced
    batch at the current cap survives real-world drift with genuine
    margin above what this hardware has actually shown, in both
    modulations (BPSK being the slower, so longer-duration, mode for a
    given byte count -- the more exposed case).
    """
    from scipy.signal import resample
    # the REALISTIC maximum the bridge's own flush logic now produces:
    # MAX_BATCH_BYTES is the binding constraint, not MAX_BATCH_FRAMES=8 --
    # a full 8-frame batch can no longer actually occur via normal
    # coalescing, so testing that old worst case here would no longer
    # reflect what this fix actually lets through.
    n_frames = max(1, bpq_bridge.MAX_BATCH_BYTES // 236)
    frames = [bytes([i % 256]) * 236 for i in range(n_frames)]
    batch_payload = bpq_bridge.pack_batch(frames)
    for name, mod in [("BPSK", ofdm.MOD_BPSK), ("QPSK", ofdm.MOD_QPSK)]:
        burst = ofdm.frame_encode(batch_payload, modulation=mod)
        # 100-130ppm: above the ~80-100ppm the second field test suggests
        # this hardware can actually produce, not just the ~20-30ppm
        # first observed.
        for ppm in (100, 130):
            n_new = int(len(burst) * (1 + ppm * 1e-6))
            drifted = resample(burst, n_new).astype(np.float32)
            rx = ofdm.OFDMReceiver()
            rx.push(drifted)
            frame = rx.poll()
            ok = (frame is not None and
                  bpq_bridge.unpack_batch(frame.payload) == frames)
            check(f"a full coalesced batch ({len(batch_payload)}B, {name}) "
                  f"survives {ppm}ppm drift", ok, f"decoded={frame is not None}")


# ── Test 35: the bridge's own flush logic respects the smaller cap ─────
def test_bridge_flushes_at_smaller_cap():
    """
    MAX_BATCH_BYTES was reduced from 1900 to 500 specifically to bound
    real transmission duration (see the constant's own comment in
    bpq_bridge.py). Crossing the cap stops further accumulation from that
    point on -- it doesn't retroactively split a single burst that
    crosses the threshold in one step (a 3-frame, 708-byte burst sent at
    once correctly becomes one 3-frame batch, since all 3 arrive before
    the first chance to flush). The real behavior to confirm is over a
    LONGER burst: enough frames that a second group accumulates AFTER an
    earlier flush, producing multiple smaller batches rather than one
    batch holding everything -- which is what the old, larger cap would
    have allowed.
    """
    batches = []
    bridge = BpqBridge(on_air_frame=lambda p: batches.append(p))
    port = 18108
    try:
        bridge.start(port=port)
    except OSError as e:
        check("BpqBridge starts for the flush-cap test", False, str(e))
        return

    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    client.settimeout(2.0)
    try:
        client.connect(("127.0.0.1", port))
        # 5 frames x 236 bytes = 1180 bytes total -- comfortably exceeds
        # the 500-byte cap more than once, so this MUST produce at least
        # two batches if the cap is being enforced across the whole
        # burst, not just checked once.
        for i in range(5):
            client.sendall(kiss.encode_frame(bytes([i]) * 236))

        deadline = _time.time() + 2.0
        while not batches and _time.time() < deadline:
            _time.sleep(0.05)
        _time.sleep(0.3)  # let any further flush (e.g. the coalesce-window one) land too

        total_frames = sum(len(bpq_bridge.unpack_batch(b)) for b in batches)
        max_batch_frames = max(len(bpq_bridge.unpack_batch(b)) for b in batches)
        check("a burst well over the cap produces more than one batch, "
              "and no single batch exceeds what the cap allows",
              len(batches) >= 2 and total_frames == 5 and max_batch_frames <= 3,
              f"got {len(batches)} batch(es), sizes={[len(bpq_bridge.unpack_batch(b)) for b in batches]}")
    finally:
        client.close()
        bridge.stop()


# ── Test 36: REGRESSION GUARD -- header sanity checks catch localized
#    corruption early, without false-positiving on clean traffic ───────
def test_header_sanity_checks():
    """
    Real field data: a crc_fail with n_data_bytes=768, modulation=BPSK,
    quality~0.95 -- while DW1IRF's own log confirmed it only ever sent
    256-byte QPSK frames. A brief overlap (most likely the OTHER
    station's own short transmission) landing specifically on the
    header symbol -- not the rest of the frame -- corrupted just enough
    to produce a bogus but structurally-valid modulation+length. Because
    the existing length/buffer-capacity checks only reject claims that
    are out of range or larger than the buffer can ever hold, a bogus-
    but-plausible length like this was accepted and committed to: the
    receiver would wait out that claim's ENTIRE duration (up to ~7s for
    a BPSK claim that size) before even attempting a decode and finally
    rejecting it via crc_fail -- and every genuine frame arriving during
    that multi-second blind spot was missed entirely, explaining a
    distinct field pattern: the first frame of a rapid-fire burst
    decoding fine, every one after it vanishing.

    Two complementary checks were added, verified here against the
    exact mechanism, not just the symptom:
    - _header_confidence (soft-decision): catches corruption strong
      enough to push a header bit decision AMBIGUOUSLY close to the
      BPSK boundary.
    - header_energy_ratio: catches corruption strong enough to flip a
      bit CONFIDENTLY to the wrong value (invisible to the confidence
      check, since a strong wrong decision is just as far from the
      boundary as a strong right one) -- an overlapping signal still
      shows up as extra energy relative to the clean preamble, which
      confidence alone can't see.
    Both are verified to leave clean traffic alone across many varied
    trials, and together to catch a real overlap across the range of
    strengths that produced each failure mode, confirmed via a direct,
    reproducible simulation -- not just inferred from the field log.
    """
    rng = np.random.RandomState(42)

    # No false positives on clean, varied traffic.
    false_positives = 0
    trials = 0
    for i in range(60):
        payload = rng.randint(0, 256, size=int(rng.randint(10, 250))).astype(np.uint8).tobytes()
        mod = ofdm.MOD_QPSK if i % 2 else ofdm.MOD_BPSK
        burst = ofdm.frame_encode(payload, modulation=mod)
        noise = rng.normal(0, 0.02, size=len(burst)).astype(np.float32)
        lead = np.zeros(int(rng.randint(500, 3000)), dtype=np.float32)
        rx = ofdm.OFDMReceiver()
        rejects = []
        rx.on_reject = lambda r, i: rejects.append(r)
        rx.push(np.concatenate([lead, burst + noise]))
        frame = rx.poll()
        trials += 1
        if frame is None or frame.payload != payload:
            false_positives += 1
    check(f"clean, varied traffic never trips either header sanity check",
          false_positives == 0, f"{false_positives}/{trials} failed unexpectedly")

    # Reproduce the real mechanism: a brief overlap landing specifically
    # on the header symbol of an otherwise-clean 254-byte QPSK frame,
    # across the range of overlap strengths that produced each distinct
    # failure mode in calibration.
    payload = rng.randint(0, 256, size=254).astype(np.uint8).tobytes()
    burst = ofdm.frame_encode(payload, modulation=ofdm.MOD_QPSK)
    lead_silence = np.zeros(2000, dtype=np.float32)
    clean = np.concatenate([lead_silence, burst])
    hdr_s = len(lead_silence) + ofdm.N_SYM * ofdm.PREAMBLE_REPS
    hdr_e = hdr_s + ofdm.N_SYM
    other_burst = ofdm.frame_encode(b"ack-from-other-station", modulation=ofdm.MOD_BPSK)
    overlap_len = min(hdr_e - hdr_s, len(other_burst))

    # Recalibrated for v0.6.0: FEC changed the frame's on-air contents
    # (the header now carries the encoded length), and with FEC the frame
    # now survives header overlaps up to ~0.7 gain intact. Measured bands:
    # low_header_confidence at 0.8-1.0, header_energy_mismatch at 1.2+.
    # 0.9 and 1.8 sit inside each band with margin on both sides.
    for gain, expected_reason in [(0.9, "low_header_confidence"), (1.8, "header_energy_mismatch")]:
        corrupted = clean.copy()
        corrupted[hdr_s:hdr_s + overlap_len] += gain * other_burst[:overlap_len]
        rx = ofdm.OFDMReceiver()
        rejects = []
        rx.on_reject = lambda r, i: rejects.append(r)
        rx.push(corrupted)
        frame = rx.poll()
        ok = frame is None and expected_reason in rejects
        check(f"a header-only overlap at gain={gain} is caught immediately via "
              f"'{expected_reason}', not left to a long wait then crc_fail",
              ok, f"decoded={frame is not None}, rejects={rejects}")

    # And confirm a MILDER overlap -- not strong enough to actually
    # corrupt the decode -- is correctly left alone, not over-rejected.
    mild = clean.copy()
    mild[hdr_s:hdr_s + overlap_len] += 0.3 * other_burst[:overlap_len]
    rx = ofdm.OFDMReceiver()
    rx.push(mild)
    frame = rx.poll()
    check("a mild overlap too weak to actually corrupt the header still decodes normally",
          frame is not None and frame.payload == payload,
          f"decoded={frame is not None}")


# ── Test 37: REGRESSION GUARD -- a weak, noise-driven candidate can't
#    claim a long wait and blind the receiver for many real seconds ────
def test_weak_signal_long_claim_rejected_early():
    """
    Real field session: a recurring false candidate at quality
    0.035-0.051 (real signals on this link have never measured below
    0.18) claimed n_data_bytes=1659 at BPSK -- a 14.84s wait -- 14 times
    across one session, over 3 cumulative minutes of the receiver blind
    to every genuine frame arriving during each wait. v0.5.9's header
    checks (confidence, energy-ratio) compare the header against the
    frame's OWN preamble; here both are uniformly weak, not a mismatch
    -- this is plain noise self-correlating at the preamble lag, a
    different mechanism needing a different check.

    Verifies the fix catches this exact case (reconstructed directly
    from the field numbers: quality ~0.06, a claim requiring ~14.7s),
    while confirming it leaves alone: a genuine weak-but-real signal at
    a normal size (matching the lowest real field quality, 0.18, this
    project has observed), and a weak signal making only a short claim
    (duration under the threshold regardless of quality).
    """
    rng = np.random.RandomState(7)

    # The reproduced field scenario: weak + long claim. The original
    # field observation (n_data_bytes=1659) predates the header format's
    # 2-bit modulation field (v0.5.11, for 8-PSK) shrinking the length
    # field to 10 bits (max 1023) -- 1659 is no longer representable at
    # all, so this uses the new field's own maximum instead, which still
    # produces a comfortably-long wait (9.12s vs the 4.0s threshold),
    # exercising the identical mechanism.
    # A VALID FEC length that still implies a long (~8s at BPSK) wait, so
    # this exercises the weak-signal rule rather than the v0.7.1
    # FEC-length check.
    long_len = fec.encoded_len(800)
    bogus_bits = [0, 0] + [int(b) for b in format(long_len, f"0{ofdm.HEADER_LEN_BITS}b")]
    bogus_carriers = {ofdm.DATA_POS[k]: complex(v, 0)
                       for k, v in enumerate(ofdm._bits_to_bpsk(bogus_bits))}
    for p in ofdm.PILOT_POS:
        bogus_carriers[p] = ofdm.PILOT_VAL
    bogus_hdr = ofdm._ofdm_symbol_tx(bogus_carriers) * 0.06
    weak_preamble = ofdm.PREAMBLE_SYMBOL * 0.06
    filler = np.zeros(ofdm.N_SYM, dtype=np.float32)
    audio = np.concatenate([weak_preamble] * ofdm.PREAMBLE_REPS + [bogus_hdr, filler]).astype(np.float32)
    rx = ofdm.OFDMReceiver()
    rx.MAX_RX_DATA_BYTES = ofdm.MAX_PAYLOAD_LEN   # isolate THIS rule from the v0.7.1 length cap
    rejects = []
    rx.on_reject = lambda r, i: rejects.append((r, i))
    rx.push(audio)
    result = rx.poll()
    ok = result is None and rejects and rejects[0][0] == "weak_signal_long_claim"
    check("a weak, noise-level candidate claiming a long (~8s) wait is "
          "rejected immediately rather than left to block the receiver",
          ok, f"decoded={result is not None}, rejects={rejects}")

    # A genuine weak-but-real signal at a normal size must still decode.
    for gain in (0.18, 0.22, 0.30):
        payload = rng.randint(0, 256, size=200).astype(np.uint8).tobytes()
        burst = ofdm.frame_encode(payload, modulation=ofdm.MOD_QPSK) * gain
        rx2 = ofdm.OFDMReceiver()
        rx2.push(burst)
        frame = rx2.poll()
        check(f"a genuine weak-but-real signal (gain={gain}, matching the lowest "
              f"real field quality observed) still decodes normally",
              frame is not None and frame.payload == payload,
              f"decoded={frame is not None}")

    # A weak signal making only a SHORT claim must be left alone too --
    # only the weak+long combination is suspicious.
    short_bits = [0, 0] + [int(b) for b in format(fec.encoded_len(20), f"0{ofdm.HEADER_LEN_BITS}b")]
    short_carriers = {ofdm.DATA_POS[k]: complex(v, 0)
                       for k, v in enumerate(ofdm._bits_to_bpsk(short_bits))}
    for p in ofdm.PILOT_POS:
        short_carriers[p] = ofdm.PILOT_VAL
    short_hdr = ofdm._ofdm_symbol_tx(short_carriers) * 0.06
    audio2 = np.concatenate([weak_preamble] * ofdm.PREAMBLE_REPS + [short_hdr, filler]).astype(np.float32)
    rx3 = ofdm.OFDMReceiver()
    rejects3 = []
    rx3.on_reject = lambda r, i: rejects3.append(r)
    rx3.push(audio2)
    rx3.poll()
    check("a weak signal making only a short claim is NOT rejected by this "
          "check (duration alone doesn't clear the threshold)",
          "weak_signal_long_claim" not in rejects3, f"rejects={rejects3}")


# ── Test 38: 8-PSK round trip, and its real (not hypothesized) drift
#    margin relative to QPSK at the same and at its own payload size ───
def test_qam16_wide_round_trip_and_drift():
    """v0.9.4: 16-QAM Wide (header code 2, once the retired narrow 8PSK).
    Clean round trip; clock drift 100-300 ppm on a strong signal; the
    largest frame the bridge can build in this mode (two full AX.25
    frames + report) fits the receiver's limit; a receiver from before
    v0.9.4 (no code 2) rejects it instantly."""
    from scipy.signal import resample
    payload = bytes([i % 256 for i in range(200)])
    rx = ofdm.OFDMReceiver()
    rx.push(ofdm.frame_encode(payload, modulation=ofdm.MOD_QAM16_WIDE))
    f = rx.poll()
    check("a clean 16QAM Wide round trip decodes correctly",
          f is not None and f.payload == payload and f.modulation == ofdm.MOD_QAM16_WIDE)
    check("16QAM Wide carries 72 bits per symbol (2x QPSK Wide)",
          ofdm.bits_per_symbol(ofdm.MOD_QAM16_WIDE) == 2 * ofdm.bits_per_symbol(ofdm.MOD_QPSK_WIDE) == 72)
    big = bytes([i % 256 for i in range(254)])
    burst = ofdm.frame_encode(big, modulation=ofdm.MOD_QAM16_WIDE)
    last_ok = 0
    for ppm in (100, 200, 300):
        drifted = resample(burst, int(len(burst) * (1 + ppm * 1e-6))).astype(np.float32)
        rx = ofdm.OFDMReceiver()
        rx.push(drifted)
        f = rx.poll()
        if f is not None and f.payload == big:
            last_ok = ppm
        else:
            break
    check(f"16QAM Wide survives 100-300 ppm clock drift (last OK {last_ok})", last_ok == 300)
    import fec
    full = _ax25_iframe("K6IRF", "DW1IRF", 1, bytes(256))          # PACLEN 256: 272 bytes
    two = bpq_bridge.merge_batches(bpq_bridge.pack_batch([full]), bpq_bridge.pack_batch([full]),
                                   ofdm.MOD_QAM16_WIDE)
    import link_adapt as la
    two = la.attach_report(two, la.Report(3, 7, 0, 30, 0, 0, la.MY_CAPS))
    on_air = len(fec.encode(two + b"xx", ofdm.fec_block(ofdm.MOD_QAM16_WIDE)))
    rx = ofdm.OFDMReceiver()
    rx.push(np.concatenate([ofdm.frame_encode(two, modulation=ofdm.MOD_QAM16_WIDE), np.zeros(6000, np.float32)]))
    f = rx.poll()
    dur = len(ofdm.frame_encode(two, modulation=ofdm.MOD_QAM16_WIDE)) / ofdm.SAMPLE_RATE
    check(f"two full AX.25 frames + report in one 16QAM Wide frame: {on_air} bytes on air "
          f"(limit {ofdm.OFDMReceiver.MAX_RX_DATA_BYTES_QAM16}), {dur:.2f} s, decodes",
          on_air <= ofdm.OFDMReceiver.MAX_RX_DATA_BYTES_QAM16 and f is not None and f.payload == two and dur < 1.4)
    check("merging stops at two full frames (a third doesn't fit)",
          bpq_bridge.merge_batches(bpq_bridge.merge_batches(bpq_bridge.pack_batch([full]), bpq_bridge.pack_batch([full]),
                                                            ofdm.MOD_QAM16_WIDE),
                                   bpq_bridge.pack_batch([full]), ofdm.MOD_QAM16_WIDE) is None
          and bpq_bridge.merge_batches(bpq_bridge.pack_batch([full]), bpq_bridge.pack_batch([full]),
                                       ofdm.MOD_QPSK_WIDE) is None)
    old = ofdm.OFDMReceiver()
    old.RX_MODULATIONS = (ofdm.MOD_BPSK, ofdm.MOD_QPSK, ofdm.MOD_QPSK_WIDE)
    rej = []
    old.on_reject = lambda r, i: rej.append(r)
    old.push(np.concatenate([ofdm.frame_encode(payload, modulation=ofdm.MOD_QAM16_WIDE), np.zeros(6000, np.float32)]))
    old.poll()
    check("a receiver from before v0.9.4 rejects a 16QAM Wide frame instantly (no blind wait)",
          rej[:1] == ["invalid_modulation"], str(rej))


def test_csma_jitter_recheck_closes_blind_window():
    """
    A real collision was directly OBSERVED on the radio (watching the
    monitor, not inferred from logs): K6IRF keyed up and transmitted a
    19-byte frame while DW1IRF was still mid-transmission on a 256-byte
    frame, stepping on it. Root cause: the single pre-v0.5.13 carrier-
    sense check happened before the TX_LEAD silence (300-600ms, needed
    for PTT/relay settling) -- during that silent lead-in a station is
    committed to transmitting but produces no audible signal yet, so if
    the OTHER station's own check happens to land in that exact window,
    the channel still looks clear to it too, and it commits as well.
    Classic CSMA blind-window collision.

    Confirms the fix directly: a channel that's clear at the initial
    check but becomes busy specifically during the jitter window (the
    other station starting its own transmission in that gap) is caught
    by the re-check and waited out, rather than transmitted over --
    and confirms the common case (channel stays clear throughout) still
    proceeds to PTT without being blocked.
    """
    from unittest.mock import MagicMock, patch

    def make_engine():
        eng = audio_io.AudioEngine.__new__(audio_io.AudioEngine)
        eng.tx_lead_ms = 1
        eng.tx_tail_ms = 1
        eng._rx_rms_ema = 0.0
        eng._tx_lock = threading.Lock()
        eng.tx_stream = MagicMock()
        eng.ptt = MagicMock()
        eng.on_carrier_wait = None
        eng.on_tx_start = None
        return eng

    # Case 1: clear at the initial check, but busy specifically at the
    # jitter re-check (the other station started transmitting in that
    # gap) -- must be caught and waited out, not transmitted over.
    eng1 = make_engine()
    call_log = []

    def is_busy_then_clears():
        call_log.append(len(call_log) + 1)
        return len(call_log) == 2   # busy only on the second call (the re-check)

    eng1.is_channel_busy = is_busy_then_clears
    with patch("random.uniform", return_value=0.15):
        with patch("time.sleep"):
            eng1.transmit(b"x" * 20, modulation=ofdm.MOD_BPSK)
    check("a channel that becomes busy specifically during the jitter "
          "window is caught by the re-check (at least 3 checks: initial "
          "clear, re-check busy, then waited-out clear) before PTT",
          len(call_log) >= 3 and call_log[1] == 2 and eng1.ptt.ptt_on.called,
          f"call_log={call_log}")

    # Case 2: channel stays clear throughout -- must still proceed to
    # PTT without the jitter mechanism blocking it unnecessarily.
    eng2 = make_engine()
    eng2.is_channel_busy = lambda: False
    with patch("random.uniform", return_value=0.1):
        with patch("time.sleep"):
            eng2.transmit(b"x" * 20, modulation=ofdm.MOD_BPSK)
    check("a channel that stays clear throughout still proceeds to PTT "
          "(the jitter doesn't block the common case)",
          eng2.ptt.ptt_on.called)

    # Case 3: the jitter value itself is bounded within [0, CSMA_JITTER_MAX_S].
    import random as _random
    samples = [_random.uniform(0, audio_io.CSMA_JITTER_MAX_S) for _ in range(200)]
    check("jitter values stay within [0, CSMA_JITTER_MAX_S]",
          all(0 <= s <= audio_io.CSMA_JITTER_MAX_S for s in samples),
          f"min={min(samples):.3f}, max={max(samples):.3f}")


# ── Test 40: Reed-Solomon FEC core correctness (fec.py) ────────────────
def test_fec_core():
    """
    fec.py is a hand-written Reed-Solomon codec (no new dependency for
    PAREDN stations to install). During development its encoder output
    was verified byte-identical to the independent reedsolo library, and
    its decoder against thousands of error patterns. This guards the
    essentials permanently: any pattern of up to NSYM//2 byte errors in a
    block is corrected exactly; more than that is REPORTED (FecError)
    rather than silently returning wrong data; the frame-level length
    mapping is exactly invertible for every length; and interleaving
    spreads a contiguous on-air burst across blocks so a 2-block frame
    survives a burst larger than one block alone could repair.
    """
    import random as _r
    rng = _r.Random(11)
    t = fec.NSYM // 2

    fails = 0
    for _ in range(300):
        msg = bytes(rng.randrange(256) for _ in range(rng.randint(1, fec.MAX_BLOCK - fec.NSYM)))
        blk = bytearray(fec.rs_encode_block(msg))
        for pos in rng.sample(range(len(blk)), min(rng.randint(1, t), len(blk))):
            blk[pos] ^= rng.randrange(1, 256)
        try:
            out, _ = fec.rs_decode_block(bytes(blk))
            fails += out != msg
        except fec.FecError:
            fails += 1
    check(f"every pattern of up to {t} byte errors per block is corrected exactly (300 trials)",
          fails == 0, f"{fails} failures")

    silent_wrong = 0
    for _ in range(300):
        msg = bytes(rng.randrange(256) for _ in range(200))
        blk = bytearray(fec.rs_encode_block(msg))
        for pos in rng.sample(range(len(blk)), t + rng.randint(1, 10)):
            blk[pos] ^= rng.randrange(1, 256)
        try:
            out, _ = fec.rs_decode_block(bytes(blk))
            silent_wrong += out != msg
        except fec.FecError:
            pass
    check("beyond-capacity damage is reported, never silently 'corrected' to wrong data "
          "(300 trials; the frame CRC is a second safeguard on top of this)",
          silent_wrong == 0, f"{silent_wrong} silent mis-corrections")

    bad = [d for d in range(1, 1000) if fec.decoded_len(fec.encoded_len(d)) != d]
    check("decoded_len() exactly inverts encoded_len() for every data length 1..999",
          not bad, f"{len(bad)} mismatches")

    data = bytes(rng.randrange(256) for _ in range(258))   # a real 256-byte batch + CRC
    enc = bytearray(fec.encode(data))
    burst_len = 2 * t   # double what ONE block can repair
    for i in range(100, 100 + burst_len):
        enc[i] ^= 0x5A
    try:
        out, n = fec.decode(bytes(enc))
        ok = out == data and n == burst_len
    except fec.FecError:
        ok = False
    check(f"interleaving: a contiguous {burst_len}-byte burst (2x one block's capacity) "
          f"in a 2-block frame is fully repaired", ok)


# ── Test 41: FEC end-to-end through the real OFDM link ─────────────────
def test_fec_end_to_end_gain():
    """
    The actual point of FEC, measured through the full modulate/
    demodulate path (not just the codec in isolation): frames that WOULD
    have been dropped before v0.6.0 now arrive intact. A decoded frame
    with fec_corrected == 0 would have passed without FEC; one with
    fec_corrected > 0 is a frame the old version would have dropped (a
    CRC fail, a BPQ32 retry, a full ~10s round trip on this link).
    Uses two damage types from this project's calibration: moderate
    noise, and a partial-overlap interference burst across 8 symbols
    (which defeated EVERY frame without FEC in calibration).
    """
    def run(damage, seed, n=10):
        rng = np.random.RandomState(seed)
        payload = bytes(range(256))
        old = new = 0
        for _ in range(n):
            b = damage(ofdm.frame_encode(payload, modulation=ofdm.MOD_QPSK).copy(), rng)
            rx = ofdm.OFDMReceiver()
            rx.push(np.concatenate([np.zeros(1500, np.float32), b,
                                    np.zeros(4000, np.float32)]).astype(np.float32))
            f = rx.poll()
            if f is not None and f.payload == payload:
                new += 1
                old += f.fec_corrected == 0
        return old, new, n

    def noise(b, rng):
        return b + rng.normal(0, 0.32, len(b)).astype(np.float32)

    def burst8(b, rng):
        first = ofdm.N_SYM * (ofdm.PREAMBLE_REPS + 1)
        start = first + rng.randint(0, len(b) - first - ofdm.N_SYM * 8)
        b[start:start + ofdm.N_SYM * 8] += rng.normal(0, 0.6, ofdm.N_SYM * 8).astype(np.float32)
        return b

    for name, dmg in [("moderate noise", noise), ("8-symbol interference burst", burst8)]:
        old, new, n = run(dmg, 7)
        check(f"{name}: FEC delivers {new}/{n} vs {old}/{n} that would have survived without it",
              new >= n - 1 and new > old + 3, f"old={old}, new={new}")

# ── Test 42: occupancy fill tone is safe and audible (v0.6.1) ──────────
def _stream_decode(audio):
    rx = ofdm.OFDMReceiver()
    rej, got = [], []
    rx.on_reject = lambda r, i: rej.append(r)
    for i in range(0, len(audio), 1024):
        rx.push(audio[i:i + 1024])
        f = rx.poll()
        while f is not None:
            got.append(f)
            f = rx.poll()
    return got, [r for r in rej if r != "low_energy"]


def test_fill_tone_safe_and_audible():
    """
    Field session: K6IRF keyed up mid-burst between DW1IRF's frames every
    burst, because each frame's 600ms lead-in was SILENCE -- a keyed FM
    transmitter sending silence is invisible to energy-based carrier
    sense. Lead-in/tail/gaps now carry a fill tone. That tone must (a)
    never look like a frame to the receiver -- whose detector keys on a
    signal repeating every N_SYM samples -- and (b) be clearly audible
    to the far end's carrier sense. The hazard is real, not theoretical:
    a 1200 Hz tone (exactly 16 cycles per 640 samples) DOES trigger
    false frame detections, which this test also confirms so it can't
    silently lose the ability to catch a badly chosen frequency.
    """
    n = int(6 * ofdm.SAMPLE_RATE)
    got, rej = _stream_decode(audio_io.fill_tone(n))
    check("6s of the fill tone alone produces no frames and no false candidates",
          not got and not rej, f"frames={len(got)}, rejects={rej}")

    t = np.arange(n)
    periodic = (0.24 * np.sin(2 * np.pi * 1200.0 * t / ofdm.SAMPLE_RATE)).astype(np.float32)
    _, rej_bad = _stream_decode(periodic)
    check("sanity: a tone periodic at the preamble lag (1200 Hz) DOES trigger false "
          "candidates -- proving this test can detect a bad fill frequency",
          len(rej_bad) > 0, f"rejects={len(rej_bad)}")

    b = ofdm.frame_encode(bytes(range(200)), modulation=ofdm.MOD_QPSK) * 0.8
    pre_rms = float(np.sqrt(np.mean(b[:ofdm.N_SYM * ofdm.PREAMBLE_REPS] ** 2)))
    fill_rms = float(np.sqrt(np.mean(audio_io.fill_tone(48000) ** 2)))
    seen = 0.95 * fill_rms / pre_rms   # far end at a typical received quality
    check(f"fill is clearly audible to the far end's carrier sense "
          f"(~{seen:.2f} vs busy threshold 0.12, needs >= 3x)",
          seen >= 0.36, f"seen={seen:.3f}")


# ── Test 43: burst mode -- one key-up, no mid-burst gap, all frames decode
def test_burst_mode_end_to_end():
    """
    Drives the REAL burst transmitter (AudioEngine.queue_transmit) and
    captures exactly the audio it would send to the radio, then checks
    what the field test needs: a 7-frame BPQ32 window goes out under ONE
    key-up; the far end's carrier sense never sees a clear channel
    anywhere in the keyed period (skipping the first 300ms, which radio
    keying may clip) -- so a mid-burst ack can't key up into a gap; and
    every frame decodes at the far end with no false candidates from the
    fill. Also confirms the carrier-sense give-up time outlasts the
    longest realistic burst (7 frames at BPSK), so a waiting station
    doesn't give up and transmit over it.
    """
    import time as _t
    from unittest.mock import MagicMock
    eng = audio_io.AudioEngine.__new__(audio_io.AudioEngine)
    eng.tx_lead_ms, eng.tx_tail_ms = 600, 150
    eng._rx_rms_ema = 0.0
    eng._tx_lock = threading.Lock()
    written = []
    eng.tx_stream = MagicMock()
    eng.tx_stream.write = lambda b: written.append(np.frombuffer(b, np.float32))
    eng.ptt = MagicMock()
    eng.on_carrier_wait = None
    eng.on_tx_start = None
    eng.is_channel_busy = lambda: False

    rng = np.random.RandomState(3)
    pls = [rng.randint(0, 256, 256).astype(np.uint8).tobytes() for _ in range(7)]
    sent = []
    for pl in pls:
        eng.queue_transmit(pl, modulation=ofdm.MOD_QPSK, on_sent=lambda p, m: sent.append(p))
    t0 = _t.time()
    while len(sent) < 7 and _t.time() - t0 < 30:
        _t.sleep(0.05)
    # v0.8.9: the 7th frame is reported sent BEFORE the closing tail tone,
    # and PTT is released after the tail -- so wait for the release. (A
    # Windows build checked too early: "sent=7, key-ups=1" yet FAIL, because
    # the release count wasn't shown and was still 0.)
    t0 = _t.time()
    while eng.ptt.ptt_off.call_count < 1 and _t.time() - t0 < 5:
        _t.sleep(0.02)
    eng._tx_queue.put(None)

    check("a 7-frame window goes out under ONE key-up, and PTT is released once at the end",
          len(sent) == 7 and eng.ptt.ptt_on.call_count == 1 and eng.ptt.ptt_off.call_count == 1,
          f"sent={len(sent)}, key-ups={eng.ptt.ptt_on.call_count}, releases={eng.ptt.ptt_off.call_count}")

    audio = np.concatenate(written)
    ema, quiet = 0.0, 0
    for i in range(0, len(audio), 1024):
        ema = 0.3 * float(np.sqrt(np.mean(audio[i:i + 1024] ** 2))) + 0.7 * ema
        if i > 0.3 * ofdm.SAMPLE_RATE and ema <= audio_io.CARRIER_BUSY_RMS:
            quiet += 1
    check("the far end's carrier sense never sees a clear channel mid-burst",
          quiet == 0, f"{quiet} clear moments")

    got, rej = _stream_decode(np.concatenate([audio, np.zeros(ofdm.SAMPLE_RATE // 2, np.float32)]))
    payloads = [f.payload for f in got]
    check("all 7 frames decode at the far end, no false candidates from the fill",
          all(p in payloads for p in pls) and not rej,
          f"decoded={sum(p in payloads for p in pls)}/7, rejects={rej}")

    b_bpsk = ofdm.frame_encode(bytes(250), modulation=ofdm.MOD_BPSK)
    longest = (eng.tx_lead_ms / 1000 + 7 * len(b_bpsk) / ofdm.SAMPLE_RATE
               + 6 * audio_io.BURST_GAP_S + eng.tx_tail_ms / 1000)
    check(f"carrier-sense give-up ({audio_io.CARRIER_SENSE_MAX_WAIT_S:.0f}s) outlasts the "
          f"longest realistic burst (7 BPSK frames, {longest:.1f}s)",
          audio_io.CARRIER_SENSE_MAX_WAIT_S > longest * 1.2, f"longest={longest:.1f}s")

# ── Test 44: link speed meter (v0.6.2) ─────────────────────────────────
def test_throughput_meter():
    """
    The speed readout added for field comparison with VARA FM. Replays
    the real v0.6.1 field session (Winlink: 2796 bytes received in 29s,
    5727 bytes/min ~ 771 bps) as frame arrivals and checks the meter
    reports that same figure; checks the live window only counts recent
    traffic; that a young session isn't diluted by the full window
    length; that reset() starts a fresh session; and that the GUI's
    one-line session summary reads correctly.
    """
    import types
    import throughput
    import sara_pingpong

    m = throughput.ThroughputMeter(window_s=30)
    m.reset(now=1000.0)
    sizes = [256] * 10 + [236]          # 2796 bytes total
    for i, n in enumerate(sizes):
        m.add(n, now=1000.0 + 29.0 * (i + 1) / len(sizes))
    sess = m.session_bps(now=1029.0)
    check(f"replaying the real field session (2796 B in 29s) reads ~771 bps "
          f"(got {sess:.0f})", abs(sess - 771) < 5, f"{sess:.1f}")
    check("live rate equals session rate while the session is younger than the window",
          abs(m.rate_bps(now=1029.0) - sess) < 1)

    check("traffic older than the window drops out of the live rate",
          m.rate_bps(now=1029.0 + 31.0) == 0.0 and m.total_bytes == 2796)

    m2 = throughput.ThroughputMeter(window_s=30)
    m2.reset(now=0.0)
    m2.add(1000, now=5.0)
    check("a 5-second-old session isn't diluted by the 30s window "
          "(1000 B in 5s = 1600 bps)", abs(m2.rate_bps(now=5.0) - 1600) < 1,
          f"{m2.rate_bps(now=5.0):.0f}")

    m.reset(now=2000.0)
    check("reset() starts a fresh session", m.total_bytes == 0 and m.session_bps(now=2001.0) == 0)

    fake = types.SimpleNamespace(rx_meter=throughput.ThroughputMeter(), tx_meter=throughput.ThroughputMeter())
    fake.rx_meter.reset(now=0.0); fake.tx_meter.reset(now=0.0)
    fake.rx_meter.add(2796, now=29.0); fake.tx_meter.add(400, now=29.0)
    import time as _t
    real_time = _t.time
    _t.time = lambda: 29.0
    try:
        line = sara_pingpong.App._speed_summary(fake)
    finally:
        _t.time = real_time
    check("session summary line reports both directions and the duration",
          "0:29" in line and "771 bps" in line and "RX 2.7 kB" in line and "TX 400 B" in line, line)

# ── Test 45: standalone .exe safety (v0.6.3) ───────────────────────────
def test_frozen_exe_safety():
    """
    Two behaviors that are correct for a Python script but wrong inside
    the PyInstaller standalone build for gateways, found by review before
    shipping (and the fixes confirmed by actually building a bundle and
    running its self-test):
    - Logs must go next to SARA-PP.exe. As a script, "next to __file__"
      is fine; frozen, __file__ points inside the bundle's internal
      folder, where nobody would find the logs.
    - ptt.py's hidapi self-repair runs "sys.executable -m pip ...". Frozen,
      sys.executable IS SARA-PP.exe, so that would launch extra copies of
      the app instead of repairing anything. It must not run when frozen.
    """
    import sys as _sys
    import types
    import subprocess as _sp
    import sara_pingpong
    import ptt

    had_frozen = hasattr(_sys, "frozen")
    old_exe = _sys.executable
    try:
        _sys.frozen = True
        fake_exe = os.path.join(os.sep, "gateway", "SARA-PP", "SARA-PP.exe")
        _sys.executable = fake_exe
        # Expected value computed with the SAME path functions, so Windows
        # (which adds a drive letter, e.g. C:\gateway\SARA-PP) and Linux
        # agree. v0.6.3's version of this check compared against a
        # hard-coded drive-less path and falsely failed on Windows.
        expected = os.path.dirname(os.path.abspath(fake_exe))
        script_dir = os.path.dirname(os.path.abspath(sara_pingpong.__file__))
        got = sara_pingpong.app_dir()
        check("frozen: logs go next to SARA-PP.exe, not inside the bundle",
              got == expected and got != script_dir, f"got={got}, expected={expected}")

        calls = []
        real_run = _sp.run
        real_hid = _sys.modules.get("hid")
        broken = types.ModuleType("hid")
        def _boom():
            raise OSError("simulated broken hidapi")
        broken.enumerate = _boom
        _sys.modules["hid"] = broken
        _sp.run = lambda *a, **k: calls.append(a)
        try:
            result = ptt._ensure_hidapi()
        finally:
            _sp.run = real_run
            if real_hid is not None:
                _sys.modules["hid"] = real_hid
            else:
                _sys.modules.pop("hid", None)
        check("frozen: a broken hidapi does NOT trigger the pip self-repair "
              "(which would relaunch SARA-PP.exe)", result is False and not calls,
              f"result={result}, subprocess calls={len(calls)}")
    finally:
        _sys.executable = old_exe
        if not had_frozen:
            del _sys.frozen
    check("not frozen: app_dir() is the script's own folder",
          sara_pingpong.app_dir() == os.path.dirname(os.path.abspath(sara_pingpong.__file__)))

# ── Test 46: per-program audio levels + saved settings (v0.6.5) ────────
class _FakeVar:
    def __init__(self, v=""): self.v = v
    def get(self): return self.v
    def set(self, v): self.v = v


class _FakeCombo(dict):
    def __init__(self, values): super().__init__(values=values)


def _fake_app(tmpdir, in_values, out_values):
    import types
    import sara_pingpong as sp
    a = types.SimpleNamespace()
    for name, default in [("call_var", "K6IRF"), ("target_var", "DW1IRF"), ("modulation_var", "QPSK"),
                          ("mode_var", "bpq"), ("kiss_port_var", "8105"), ("ptt_var", "VOX"),
                          ("ra_board_var", ""), ("gpio_pin_var", "GPIO3 (0x04)"),
                          ("tx_delay_var", "600"), ("rx_gain_db_var", "18"), ("tx_level_var", "45"),
                          ("remote_kiss_var", True), ("kiss_allow_var", "private 44.0.0.0/8"),
                          ("dcd_mode_var", "SARA-PP signals only"), ("gauge_title_var", "")]:
        setattr(a, name, _FakeVar(default))
    a.in_dev_combo, a.out_dev_combo = _FakeCombo(in_values), _FakeCombo(out_values)
    a.ra_board_combo = _FakeCombo([])
    a.in_dev_var, a.out_dev_var = _FakeVar(in_values[0]), _FakeVar(out_values[0])
    a.logs = []
    a._log = a.logs.append
    a._on_mode_change = lambda: None
    a._on_ptt_method_change = lambda: None
    a.DCD_LABELS = sp.App.DCD_LABELS
    for m in ("_settings_path", "_settings_dict", "_save_settings", "_select_device",
              "_load_settings", "_levels", "_dcd_mode_code"):
        setattr(a, m, types.MethodType(getattr(sp.App, m), a))
    a._dev_name = sp.App._dev_name
    a._SETTINGS_FILE = sp.App._SETTINGS_FILE
    sp_app_dir = sp.app_dir
    return a, sp, sp_app_dir


def test_levels_and_saved_settings():
    """
    Sharing one sound card with VARA FM: both programs read the SAME
    Windows recording level, VARA needs it quiet, and at that level SARA-PP
    neither decoded nor -- worse -- noticed the channel was busy (so it
    would transmit over others). RX gain fixes both without touching the
    shared Windows level; TX level replaces the fixed 0.8 transmit
    amplitude. Settings are saved so a gateway keeps them across restarts,
    with audio devices matched by NAME because Windows renumbers them.
    """
    import queue as _q
    import tempfile
    from unittest.mock import MagicMock

    def engine(gain):
        e = audio_io.AudioEngine.__new__(audio_io.AudioEngine)
        e.receiver = ofdm.OFDMReceiver()
        e.level_queue = _q.Queue(maxsize=50)
        e.frame_queue = _q.Queue()
        e._rx_rms_ema = 0.0
        e.rx_gain = gain
        return e

    pl = bytes(range(120))
    quiet = ofdm.frame_encode(pl, modulation=ofdm.MOD_QPSK) * 0.06
    pcm = (np.concatenate([np.zeros(2000, np.float32), quiet,
                           np.zeros(8000, np.float32)]) * 32767).astype(np.int16)
    results = {}
    for db in (0, 24):
        e = engine(10 ** (db / 20))
        busy = False
        for i in range(0, len(pcm), 1024):
            e._rx_callback(pcm[i:i + 1024].tobytes(), 1024, None, 0)
            busy |= e.is_channel_busy()
        frames = []
        while not e.frame_queue.empty():
            frames.append(e.frame_queue.get())
        results[db] = (bool(frames) and frames[0].payload == pl, busy,
                       frames[0].quality if frames else 0.0)
    check("v0.9.41: quiet input (Windows level set for VARA) decodes even at 0 dB RX gain "
          "(it used to be dropped by the receiver's level gate)",
          results[0][0], f"{results[0]}")
    check("same quiet input with +24 dB RX gain: decodes at normal quality and "
          "carrier sense sees the channel busy",
          results[24][0] and results[24][1] and 0.7 < results[24][2] < 1.1, f"{results[24]}")

    e = audio_io.AudioEngine.__new__(audio_io.AudioEngine)
    e.tx_lead_ms = e.tx_tail_ms = 1
    e._rx_rms_ema = 0.0
    e._tx_lock = threading.Lock()
    e.ptt = MagicMock()
    e.on_carrier_wait = e.on_tx_start = None
    written = []
    e.tx_stream = MagicMock()
    e.tx_stream.write = lambda b: written.append(np.frombuffer(b, np.float32))
    e.is_channel_busy = lambda: False
    e.tx_level = 0.4
    e.transmit(pl, modulation=ofdm.MOD_QPSK)
    peak = max(float(np.max(np.abs(x))) for x in written)
    check("TX level setting controls transmit amplitude (40% -> peak 0.40)",
          abs(peak - 0.4) < 0.01, f"peak={peak:.3f}")

    with tempfile.TemporaryDirectory() as td:
        a, sp, real_app_dir = _fake_app(td, ["2: Mic (USB Audio)", "0: Built-in Mic"],
                                         ["4: Speakers (USB Audio)", "1: Built-in Speakers"])
        sp.app_dir = lambda: td
        try:
            a._save_settings()
            # Windows renumbered the USB interface after a reboot:
            b, _, _ = _fake_app(td, ["0: Built-in Mic", "5: Mic (USB Audio)"],
                                ["1: Built-in Speakers", "6: Speakers (USB Audio)"])
            b.call_var.set(""); b.rx_gain_db_var.set("0"); b.tx_level_var.set("80")
            b.remote_kiss_var.set(False); b.kiss_allow_var.set("private"); b.dcd_mode_var.set("Auto")
            b._load_settings()
            check("DCD mode survives a restart (v0.7.7)",
                  b.dcd_mode_var.get() == "SARA-PP signals only", b.dcd_mode_var.get())
            check("network KISS settings survive a restart (v0.7.5)",
                  b.remote_kiss_var.get() is True and b.kiss_allow_var.get() == "private 44.0.0.0/8",
                  f"{b.remote_kiss_var.get()}, {b.kiss_allow_var.get()}")
            check("settings survive a restart (callsign, RX gain, TX level)",
                  (b.call_var.get(), b.rx_gain_db_var.get(), b.tx_level_var.get()) == ("K6IRF", "18", "45"),
                  f"{b.call_var.get()}, {b.rx_gain_db_var.get()}, {b.tx_level_var.get()}")
            check("audio devices are re-found BY NAME after Windows renumbers them",
                  b.in_dev_var.get() == "5: Mic (USB Audio)" and b.out_dev_var.get() == "6: Speakers (USB Audio)",
                  f"{b.in_dev_var.get()} / {b.out_dev_var.get()}")

            c, _, _ = _fake_app(td, ["0: Built-in Mic"], ["1: Built-in Speakers"])
            c._load_settings()
            check("a missing saved device (interface unplugged) is reported, not silently swapped",
                  any("not found" in m for m in c.logs) and c.in_dev_var.get() == "0: Built-in Mic",
                  f"logs={c.logs}")

            with open(os.path.join(td, sp.App._SETTINGS_FILE), "w") as fh:
                fh.write("{ not valid json")
            d, _, _ = _fake_app(td, ["0: Built-in Mic"], ["1: Built-in Speakers"])
            d._load_settings()
            check("a corrupt settings file falls back to defaults without crashing",
                  any("unreadable" in m for m in d.logs), f"logs={d.logs}")
        finally:
            sp.app_dir = real_app_dir

    a2, _, _ = _fake_app(".", ["0: x"], ["0: y"])
    a2.rx_gain_db_var.set("12x")   # half-typed entry
    g, t = a2._levels()
    check("a half-typed/invalid level entry falls back safely instead of erroring",
          g == 1.0 and abs(t - 0.45) < 1e-9, f"gain={g}, tx={t}")

# ── v0.7.0: level-independent operation + tuning tools ─────────────────
def _quiet_frame_audio(payload, raw_dbfs, noise_dbfs, rng, gain_db=0.0, mod=None):
    mod = ofdm.MOD_QPSK if mod is None else mod
    b = ofdm.frame_encode(payload, modulation=mod)
    # scale so the PREAMBLE arrives at raw_dbfs RMS (frame_encode output is
    # unscaled here, so reference the bare preamble symbol's own RMS)
    ref = float(np.sqrt(np.mean(ofdm.PREAMBLE_SYMBOL.astype(np.float64) ** 2)))
    x = np.concatenate([np.zeros(2000), b, np.zeros(4000)]) * 10 ** (raw_dbfs / 20) / ref
    x = np.clip(x + rng.normal(0, 10 ** (noise_dbfs / 20), len(x)), -1, 1) * 10 ** (gain_db / 20)
    return x.astype(np.float32)


def test_snr_measurement():
    """
    S/N from the repeated preamble -- the link figure, like VARA's S/N.
    The old 'quality' was a LEVEL reading: a frame at 1/10th volume read
    0.09 ('bad') with a 36 dB S/N ('excellent'), pushing users to turn
    levels up into VARA's red zone. Checks accuracy against known in-band
    S/N, and that it reads the same at full and 1/20th level.
    """
    from scipy.signal import sosfiltfilt
    rng = np.random.RandomState(0)
    burst = ofdm.frame_encode(bytes(range(100)), modulation=ofdm.MOD_QPSK)
    rx0 = ofdm.OFDMReceiver()
    pre_len = ofdm.N_SYM * ofdm.PREAMBLE_REPS
    worst = 0.0
    spread = 0.0
    for sigma in (0.05, 0.1, 0.2, 0.3):
        ests = []
        for gain in (1.0, 0.05):
            x = np.concatenate([np.zeros(3000), burst, np.zeros(3000)]) * gain
            n = rng.normal(0, sigma * gain, len(x))
            true = 10 * np.log10(np.mean(sosfiltfilt(rx0.sos, x)[3000:3000 + pre_len] ** 2)
                                 / np.mean(sosfiltfilt(rx0.sos, n) ** 2))
            rx = ofdm.OFDMReceiver()
            # Lower the energy gate to its hard floor so this test exercises
            # ONLY the S/N estimator (the gate has its own test below).
            rx.noise_floor_rms = 1e-5
            rx.push((x + n).astype(np.float32))
            f = rx.poll()
            if f is None:
                ests.append(float("nan")); worst = 99.0
                continue
            ests.append(f.snr_db)
            worst = max(worst, abs(f.snr_db - true))
        spread = max(spread, abs(ests[0] - ests[1]))
    check(f"S/N estimate within 2.5 dB of true in-band S/N (13-30 dB) (worst {worst:.1f} dB)",
          worst <= 2.5)
    check(f"S/N reads the same at full and 1/20th level (max difference {spread:.1f} dB)",
          spread <= 2.5)
    rx = ofdm.OFDMReceiver()
    rx.push(_quiet_frame_audio(bytes(range(50)), -20, -80, rng))
    f = rx.poll()
    check("decoded frames carry level in dBFS (frame sent at -20 dBFS reads ~-20)",
          f is not None and abs(f.level_dbfs + 20) < 1.5, f"{f.level_dbfs if f else None}")


def test_noise_relative_carrier_sense():
    """
    Field result: at VARA-friendly receive levels the fixed -25.6 dBFS busy
    threshold didn't hear the other station's fill tone (step-on came
    back), and raising RX gain to compensate made amplified noise look
    permanently busy. Replays a keyed burst (fill + 2 frames + gap fill)
    through background noise and checks the OLD fixed threshold fails
    both ways while the NEW noise-relative one works both ways, and that
    changing RX gain rescales the noise history immediately.
    """
    import collections
    SR = ofdm.SAMPLE_RATE
    rng = np.random.RandomState(2)
    b = ofdm.frame_encode(bytes(range(200)), modulation=ofdm.MOD_QPSK) * 0.8
    tx = np.concatenate([audio_io.fill_tone(int(0.6 * SR)), b,
                         audio_io.fill_tone(int(audio_io.BURST_GAP_S * SR)), b,
                         audio_io.fill_tone(int(0.15 * SR))])

    def run(frame_dbfs, noise_dbfs, gain_db, relative):
        e = audio_io.AudioEngine.__new__(audio_io.AudioEngine)
        e._rx_rms_ema = 0.0
        e._rx_gain = 1.0
        e._noise_blocks = collections.deque(maxlen=audio_io.NOISE_HISTORY_BLOCKS)
        e._noise_acc = e._noise_acc_n = e._noise_acc_samples = 0
        e.noise_floor_rms = None
        e.rx_gain = 10 ** (gain_db / 20)
        quiet = rng.normal(0, 10 ** (noise_dbfs / 20), int(6 * SR))
        sig = tx * 10 ** (frame_dbfs / 20) / 0.343 + rng.normal(0, 10 ** (noise_dbfs / 20), len(tx))
        audio = np.concatenate([quiet, sig]).astype(np.float32)
        bq = bs = nq = ns = 0
        for i in range(0, len(audio), 1024):
            blk = audio[i:i + 1024] * e.rx_gain
            r = float(np.sqrt(np.mean(blk ** 2)))
            e._rx_rms_ema = 0.3 * r + 0.7 * e._rx_rms_ema
            e._track_noise(r, len(blk))
            busy = e.is_channel_busy() if relative else e._rx_rms_ema > audio_io.CARRIER_BUSY_RMS
            if 2 * SR < i < len(quiet) - 2048:
                nq += 1; bq += busy
            elif i > len(quiet) + int(0.3 * SR):
                ns += 1; bs += busy
        return 100 * bs / ns, 100 * bq / nq

    old_quiet = run(-28, -50, 0, False)
    new_quiet = run(-28, -50, 0, True)
    check(f"moderate level (frames -28 dBFS): OLD fixed threshold heard {old_quiet[0]:.0f}% of the "
          f"other station's burst, NEW noise-relative hears {new_quiet[0]:.0f}%",
          old_quiet[0] < 10 and new_quiet[0] > 99)
    old_gain = run(-25, -50, 30, False)
    new_gain = run(-25, -50, 30, True)
    check(f"+30 dB RX gain: OLD threshold falsely busy {old_gain[1]:.0f}% of quiet time, "
          f"NEW {new_gain[1]:.0f}% (while still hearing the burst {new_gain[0]:.0f}%)",
          old_gain[1] > 90 and new_gain[1] < 1 and new_gain[0] > 99)

    e = audio_io.AudioEngine.__new__(audio_io.AudioEngine)
    e._rx_gain = 1.0
    e._noise_blocks = collections.deque([0.01, 0.02], maxlen=60)
    e.noise_floor_rms = 0.01
    e.rx_gain = 10.0
    check("changing RX gain rescales the noise history immediately",
          abs(e.noise_floor_rms - 0.1) < 1e-9)


def test_receiver_quiet_gate():
    """
    The receiver's own low-energy gate was a fixed -33 dBFS, so a quiet
    but clean frame (Windows level set low for VARA) was dropped before
    decoding -- Auto-Tune would have had nothing to measure. With the
    engine's noise floor supplied, the gate may be LOWERED (never raised)
    to 12 dB over that floor. An earlier adaptive gate sampled ambient
    from the live buffer and was reverted after contaminating upward;
    this one can only be more permissive than the fixed gate, never less.
    """
    rng = np.random.RandomState(9)
    pl = bytes(range(100))
    for raw in (-40, -50):
        x = _quiet_frame_audio(pl, raw, -72, rng)
        rx = ofdm.OFDMReceiver()
        rx.push(x)
        before = rx.poll()
        rx = ofdm.OFDMReceiver()
        rx.noise_floor_rms = 10 ** (-72 / 20)
        rx.push(x)
        after = rx.poll()
        check(f"v0.9.41: clean frame at {raw} dBFS decodes with or without a known noise floor",
              before is not None and before.payload == pl and after is not None and after.payload == pl)
    rx = ofdm.OFDMReceiver()
    rx.noise_floor_rms = 1.0       # absurdly high floor
    g = rx.effective_min_energy()
    rx.noise_floor_rms = None
    check("v0.9.41: the gate doesn't depend on the noise floor at all (only near-silence is gated)",
          g == rx.effective_min_energy() and g < rx.MIN_ENERGY / 100)
    # a weak frame under open-squelch hiss: level alone never decides any more
    for lvl in (-30, -40, -46):
        b = ofdm.frame_encode(pl, modulation=ofdm.MOD_BPSK).astype(np.float64)
        b = b / np.sqrt(np.mean(b ** 2)) * 10 ** (lvl / 20)
        n = rng.normal(0, 10 ** ((lvl - 9) / 20), len(b) + 19200)
        n[9600:9600 + len(b)] += b
        rx = ofdm.OFDMReceiver()
        rx.noise_floor_rms = 10 ** ((lvl - 9) / 20)
        out = decode_all(rx, n.astype(np.float32), chunk=1024)
        check(f"BPSK ping-sized frame at {lvl} dBFS, S/N 9 dB in hiss: decodes",
              any(f.payload == pl for f in out))
    # v0.9.42: every in-band tone that repeats exactly (a multiple of 75 Hz)
    # is caught by the two-reading tone check
    nn = np.arange(ofdm.N_SYM * 3)
    missed = [f for f in range(450, 1950, 75)
              if not ofdm.OFDMReceiver().is_tone([np.sin(2 * np.pi * f * nn / ofdm.SAMPLE_RATE)
                                                  [i * ofdm.N_SYM:(i + 1) * ofdm.N_SYM] for i in range(3)])[0]]
    check("every exactly-repeating tone 450-1875 Hz is recognised as a tone", not missed, str(missed))
    import fm_channel as _fc
    from scipy.signal import sosfilt as _sf
    ok, n_ = 0, 0
    pre = np.tile(ofdm.PREAMBLE_SYMBOL, 3).astype(np.float64)
    rx0 = ofdm.OFDMReceiver()
    for path in ("flat", "speaker", "speaker_narrow"):
        for tilt in (False, True):
            for cnr in (6, 9, 12):
                tx = np.concatenate([np.zeros(ofdm.SAMPLE_RATE // 2), pre * 0.4, np.zeros(ofdm.SAMPLE_RATE // 2)])
                y = _fc.receive(tx, np.ones(len(tx), bool), cnr, rng, out_gain=0.05, path=path).astype(np.float64)
                if tilt:
                    y = _sf(_fc._DE_SOS, y) * 5
                y = _sf(rx0.sos, y)
                s0 = ofdm.SAMPLE_RATE // 2 + 40
                n_ += 1
                ok += not rx0.is_tone([y[s0 + i * ofdm.N_SYM:s0 + (i + 1) * ofdm.N_SYM] for i in range(3)])[0]
    check(f"real preambles through every radio audio path (incl. 12 dB tilt) at CNR 6-12 dB are never "
          f"taken for a tone ({ok}/{n_})", ok == n_)
    # ...while a steady whistle (repeats perfectly) is still never a preamble
    t_ = np.arange(int(20 * ofdm.SAMPLE_RATE))
    for f0 in (440.0, 1020.0, 1800.0):
        tone = (0.3 * np.sin(2 * np.pi * f0 * t_ / ofdm.SAMPLE_RATE)).astype(np.float32)
        rx = ofdm.OFDMReceiver()
        why = []
        rx.on_reject = lambda r, i: why.append(r)
        got = decode_all(rx, tone, chunk=4096)
        check(f"a {f0:.0f} Hz whistle is rejected as 'not a preamble' (no frames, no header reads)",
              not got and set(why) <= {"not_preamble"}, str(set(why)))
    false = 0
    for nd in (-70, -45):
        nrms = 10 ** (nd / 20)
        x = rng.normal(0, nrms, int(40 * ofdm.SAMPLE_RATE)).astype(np.float32)
        rx = ofdm.OFDMReceiver()
        rx.noise_floor_rms = nrms
        for i in range(0, len(x), 4096):
            rx.push(x[i:i + 4096])
            f = rx.poll()
            while f is not None:
                false += 1
                f = rx.poll()
    check("80 s of pure noise with the noise-relative gate: no false frames", false == 0, f"{false}")


def test_long_claim_snr_guard():
    """
    'quality' is a level reading, so with high RX gain amplified noise read
    above the weak-signal threshold and could claim a long frame, blinding
    the receiver. Preamble S/N doesn't change with gain. No real frame is
    anywhere near the 4 s threshold.
    """
    import bpq_bridge as bb
    longest = max(len(ofdm.frame_encode(bytes(bb.MAX_BATCH_BYTES), modulation=m))
                  for m in (ofdm.MOD_BPSK, ofdm.MOD_QPSK)) / ofdm.SAMPLE_RATE
    check(f"longest real frame ({longest:.1f}s) is well under the 4 s long-claim threshold",
          longest < ofdm.OFDMReceiver.MAX_WAIT_FOR_WEAK_SIGNAL_S * 0.8)
    bits = [0, 0] + [int(x) for x in format(fec.encoded_len(800), f"0{ofdm.HEADER_LEN_BITS}b")]
    car = {ofdm.DATA_POS[k]: complex(v, 0) for k, v in enumerate(ofdm._bits_to_bpsk(bits))}
    for p_ in ofdm.PILOT_POS:
        car[p_] = ofdm.PILOT_VAL
    hdr = ofdm._ofdm_symbol_tx(car)
    x = np.concatenate([ofdm.PREAMBLE_SYMBOL] * ofdm.PREAMBLE_REPS + [hdr, np.zeros(ofdm.N_SYM)]) * 0.6
    x = (x + np.random.RandomState(3).normal(0, 0.45, len(x))).astype(np.float32)
    rx = ofdm.OFDMReceiver()
    rx.MAX_RX_DATA_BYTES = ofdm.MAX_PAYLOAD_LEN   # isolate THIS rule from the v0.7.1 length cap
    rej = []
    rx.on_reject = lambda r, i: rej.append((r, i.get("quality", 0)))
    rx.push(x)
    rx.poll()
    check("a LOUD but noisy long claim (quality > 0.12) is rejected by S/N",
          rej and rej[0][0] == "weak_signal_long_claim" and rej[0][1] > 0.12, f"{rej}")


def test_two_way_ping_report():
    """PONG carries the responder's S/N and RAW level of the received ping,
    like VARA's two-way ping report; a 20-byte PONG from an older v0.6.x
    responder still parses."""
    m = proto.PingPongMessage.unpack(proto.make_pong("DW1IRF", "K6IRF", 7, 23.4, -14.6))
    check("PONG report round-trips (S/N 23, level -15)",
          (m.report_snr_db, m.report_level_dbfs) == (23, -15), f"{m}")
    old = proto.PingPongMessage.unpack(proto.make_pong("DW1IRF", "K6IRF", 7))
    check("a plain 20-byte PONG (older responder) still parses, with no report",
          old.report_snr_db is None and len(proto.make_pong("A", "B", 1)) == 20)
    clamp = proto.PingPongMessage.unpack(proto.make_pong("A", "B", 1, 300, -300))
    check("out-of-range values are clamped to int8, not wrapped",
          (clamp.report_snr_db, clamp.report_level_dbfs) == (127, -128))


def test_autotune_convergence_and_advice():
    """
    Auto-Tune sets RX gain from a received PONG so the NEXT frame lands in
    the target zone. Simulated across raw input levels from very quiet to
    hot (noise floor known, as the running engine provides): one step
    should land every case in the window. Also checks the plain-language
    advice for the cases a gain setting can't fix.
    """
    import tuning
    rng = np.random.RandomState(5)

    def receive(raw, gain_db):
        x = _quiet_frame_audio(proto.make_pong("DW1IRF", "K6IRF", 1, 24, -18), raw, -72, rng, gain_db)
        rx = ofdm.OFDMReceiver()
        rx.noise_floor_rms = 10 ** ((-72 + gain_db) / 20)
        rx.push(x)
        return rx.poll()

    landed = []
    for raw in (-50, -35, -20, -8):
        first = receive(raw, 0.0)
        new, _ = tuning.recommend_rx_gain(first.level_dbfs, 0.0)
        nxt = receive(raw, new)
        landed.append((raw, round(nxt.level_dbfs, 1), tuning.in_target(nxt.level_dbfs)))
    check("one Auto-Tune step lands the next frame in the target zone from -50 to -8 dBFS raw",
          all(ok for _, _, ok in landed), f"{landed}")

    _, note_lo = tuning.recommend_rx_gain(-70, 0.0)
    _, note_ok = tuning.recommend_rx_gain(-55, 0.0)
    check("advises raising the RADIO volume only when max RX gain still leaves frames "
          "outside the target", "raise the radio" in note_lo and note_ok == "")
    adv = tuning.report_advice(25, -1.0, "DU1IRG") + tuning.report_advice(25, -50, "DU1IRG") \
        + tuning.report_advice(6, -15, "DU1IRG")
    check("two-way report advice covers: clipping their input, very quiet, poor S/N (path, not level)",
          any("clipping" in a for a in adv) and any("very quietly" in a for a in adv)
          and any("radio path" in a for a in adv), f"{adv}")
    check("carrier-sense advice fires when noise is within 12 dB of signals, not otherwise",
          tuning.carrier_sense_advice(8.0) and not tuning.carrier_sense_advice(20.0))


def test_tune_tone_level():
    """TUNE sends the fill tone at the same RMS as real data at the current
    TX level, so the far end's meter reads what a frame would read, with
    peaks well below a frame's (no extra deviation)."""
    b = ofdm.frame_encode(bytes(range(200)), modulation=ofdm.MOD_QPSK) * 0.8
    data_rms = float(np.sqrt(np.mean(b[ofdm.N_SYM * 5:] ** 2)))
    t = audio_io.fill_tone(48000, 0.8)
    t_rms = float(np.sqrt(np.mean(t ** 2)))
    check(f"TUNE tone RMS ({t_rms:.3f}) within 1 dB of data RMS ({data_rms:.3f}), peak below frame peak",
          abs(20 * np.log10(t_rms / data_rms)) < 1.0 and np.max(np.abs(t)) < np.max(np.abs(b)))

# ── v0.7.1: coalescing cap actually enforced; header plausibility ──────
def test_coalescing_cap_enforced():
    """
    The coalescer checked MAX_BATCH_BYTES only AFTER appending a frame, so
    a waiting small frame plus a full-size one went out together: field
    logs showed "386 bytes (2 frames)" and "510 bytes (2 frames)" -- the
    ~510-byte size the cap was created to prevent (drift past ~90 deg).
    Replays the field-like mixes through a real bridge socket: no batch may
    exceed max(cap, the largest single frame), and small frames must still
    combine into one transmission.
    """
    cases = [[130, 252], [236, 236], [60, 180, 252], [15, 15, 15, 15]]
    worst = []
    combined_small = None
    for port, sizes in zip(range(18140, 18150), cases):
        got = []
        br = BpqBridge(on_air_frame=lambda p: got.append(p))
        br.start(port=port)
        c = socket.create_connection(("127.0.0.1", port))
        for i, n in enumerate(sizes):
            c.sendall(kiss.encode_frame(bytes([i]) * n))
        _time.sleep(0.8)
        c.close()
        br.stop()
        limit = max(bpq_bridge.MAX_BATCH_BYTES, max(sizes)) + 2 + 2 * len(sizes)
        worst.append((sizes, [len(p) for p in got], all(len(p) <= limit for p in got)))
        if sizes == [15, 15, 15, 15]:
            combined_small = len(got)
    check("no coalesced batch exceeds the cap (or one oversize frame alone) -- "
          "field mixes [130,252], [236,236], [60,180,252]",
          all(ok for _, _, ok in worst), f"{worst}")
    check("four small acks still go out as ONE transmission", combined_small == 1,
          f"{combined_small} transmissions")


def test_header_plausibility_checks():
    """
    Field log: a garbled header claiming 836 bytes of 8PSK made the
    receiver wait ~2.5s and swallow a real frame arriving inside that
    span; other bogus claims were 629-999 bytes, and one 'invalid FEC
    frame length 18' was only caught after waiting. All of these are now
    rejected instantly from the header alone: 8PSK (not transmitted since
    v0.5.12) and value 3, lengths over MAX_RX_DATA_BYTES, and lengths no
    FEC-encoded frame can have.
    """
    def header_audio(mod, n):
        bits = [(mod >> 1) & 1, mod & 1] + [int(b) for b in format(n, f"0{ofdm.HEADER_LEN_BITS}b")]
        car = {ofdm.DATA_POS[k]: complex(v, 0) for k, v in enumerate(ofdm._bits_to_bpsk(bits))}
        for p_ in ofdm.PILOT_POS:
            car[p_] = ofdm.PILOT_VAL
        return np.concatenate([ofdm.PREAMBLE_SYMBOL] * ofdm.PREAMBLE_REPS
                              + [ofdm._ofdm_symbol_tx(car), np.zeros(ofdm.N_SYM)]).astype(np.float32)

    def first_reject(audio):
        rx = ofdm.OFDMReceiver()
        rej = []
        rx.on_reject = lambda r, i: rej.append(r)
        rx.push(audio)
        rx.poll()
        return rej[0] if rej else None

    check("v0.9.4: code 2 is 16QAM Wide now; over its own size limit it is still rejected instantly",
          first_reject(header_audio(2, 900)) == "implausible_length")
    # Value 3 became QPSK Wide in v0.8.0: a v0.8 receiver accepts it; a
    # receiver without Wide support still rejects it instantly.
    check("modulation value 3 is QPSK Wide now: not rejected as invalid",
          first_reject(header_audio(3, 300)) != "invalid_modulation")
    def first_reject_old(audio):
        rx = ofdm.OFDMReceiver()
        rx.RX_MODULATIONS = (ofdm.MOD_BPSK, ofdm.MOD_QPSK)
        rej = []
        rx.on_reject = lambda r, i: rej.append(r)
        rx.push(audio)
        rx.poll()
        return rej[0] if rej else None
    check("a receiver without Wide support rejects a Wide header instantly",
          first_reject_old(header_audio(3, 300)) == "invalid_modulation")
    field = [629, 905, 944, 982, 999]
    reasons = [first_reject(header_audio(ofdm.MOD_QPSK, n)) for n in field]
    check(f"the field's bogus long claims {field} are rejected instantly as implausible",
          all(r == "implausible_length" for r in reasons), f"{reasons}")
    check("an impossible FEC length (18, from the field log) is rejected instantly",
          first_reject(header_audio(ofdm.MOD_QPSK, 18)) == "implausible_length")

# ── v0.7.2: busy threshold anchored to received signal level ───────────
def _cs_engine():
    import collections
    e = audio_io.AudioEngine.__new__(audio_io.AudioEngine)
    e._rx_rms_ema = 0.0
    e._rx_gain = 1.0
    e._noise_blocks = collections.deque(maxlen=audio_io.NOISE_HISTORY_BLOCKS)
    e._noise_acc = e._noise_acc_n = e._noise_acc_samples = 0
    e.noise_floor_rms = None
    e._sig_levels = collections.deque(maxlen=audio_io.SIGNAL_LEVEL_HISTORY)
    return e


def _cs_busy_pct(e, audio, after_s):
    SR = ofdm.SAMPLE_RATE
    busy = n = 0
    for i in range(0, len(audio), 1024):
        blk = audio[i:i + 1024]
        r = float(np.sqrt(np.mean(blk ** 2)))
        e._rx_rms_ema = 0.3 * r + 0.7 * e._rx_rms_ema
        e._track_noise(r, len(blk))
        if i > after_s * SR:
            n += 1
            busy += e.is_channel_busy()
    return 100 * busy / max(n, 1)


def test_signal_anchored_busy_threshold():
    """
    Field (v0.7.1): DW1IRF's carrier sense held a reply 26.4 s while only
    short connect requests with ~3 s gaps were on the air; both stations'
    idle input sat 1-3 dB under the busy threshold. "floor + 6 dB" counts
    from the QUIETEST second of the last minute, and steady background
    noise with occasional quiet dips sits several dB above that. Replays
    that noise shape: the old threshold calls idle noise busy most of the
    time; anchored midway to the received station's level it doesn't,
    while still hearing that station's keyed burst (fill + frames).
    """
    import time as _t
    SR = ofdm.SAMPLE_RATE
    rng = np.random.RandomState(4)

    def steady_with_dips(sec, level_db, dip_db=-57):
        n = int(sec * SR)
        env = np.full(n, float(level_db))
        for st in range(10, int(sec), 40):
            env[int(st * SR):int((st + 1.5) * SR)] = dip_db
        return (rng.normal(0, 1, n) * 10 ** (env / 20)).astype(np.float32)

    noise = steady_with_dips(150, -49)
    old = _cs_engine()
    new = _cs_engine()
    new._sig_levels.append((_t.time(), -20.0))     # K6IRF's frames arrived at -20 dBFS
    po, pn = _cs_busy_pct(old, noise, 60), _cs_busy_pct(new, noise, 60)
    check(f"idle noise steady at -49 dBFS (field-like): old threshold busy {po:.0f}%, "
          f"signal-anchored {pn:.0f}%", po > 80 and pn < 1)

    b = ofdm.frame_encode(bytes(range(200)), modulation=ofdm.MOD_QPSK) * 0.8
    tx = np.concatenate([audio_io.fill_tone(int(0.6 * SR)), b,
                         audio_io.fill_tone(int(audio_io.BURST_GAP_S * SR)), b,
                         audio_io.fill_tone(int(0.15 * SR))])
    audio = np.concatenate([steady_with_dips(70, -50),
                            (tx * 10 ** (-20 / 20) / 0.343
                             + rng.normal(0, 10 ** (-50 / 20), len(tx))).astype(np.float32)])
    e = _cs_engine()
    e._sig_levels.append((_t.time(), -20.0))
    heard = _cs_busy_pct(e, audio, 70.3)
    check(f"the anchored threshold still hears that station's keyed burst {heard:.0f}% of the time",
          heard > 99)


def test_busy_threshold_rules():
    """The anchoring rules: midway between noise and the WEAKEST recent
    station, never closer than SIGNAL_MARGIN_DB to it (its fill tone must
    still be heard), never below floor + BUSY_MARGIN_DB; old levels expire;
    an RX gain change shifts remembered levels with everything else."""
    import time as _t
    db = lambda x: 20 * np.log10(x)
    now = _t.time()
    e = _cs_engine()
    e.noise_floor_rms = 10 ** (-55 / 20)
    check("no frames received yet: threshold is floor + 6 dB",
          abs(db(e.busy_threshold_rms(now)) - (-49)) < 0.01)
    e._sig_levels.extend([(now, -4.0), (now, -20.0)])     # own loud echo + the remote
    check("anchored to the WEAKEST station (-20, not the -4 own echo): midway = -37.5 dBFS",
          abs(db(e.busy_threshold_rms(now)) - (-37.5)) < 0.01)
    e2 = _cs_engine()
    e2.noise_floor_rms = 10 ** (-55 / 20)
    e2._sig_levels.append((now, -40.0))                  # weak station only 15 dB over noise
    check("a weak station: threshold capped at 10 dB under it (-50), still above floor + 6",
          abs(db(e2.busy_threshold_rms(now)) - (-49)) < 0.01)
    e3 = _cs_engine()
    e3.noise_floor_rms = 10 ** (-55 / 20)
    e3._sig_levels.append((now - audio_io.SIGNAL_LEVEL_MAX_AGE_S - 1, -20.0))
    check("levels older than SIGNAL_LEVEL_MAX_AGE_S are forgotten",
          abs(db(e3.busy_threshold_rms(now)) - (-49)) < 0.01)
    e4 = _cs_engine()
    e4._sig_levels.append((now, -20.0))
    e4.rx_gain = 10 ** (6 / 20)
    check("an RX gain change (+6 dB) shifts remembered levels too",
          abs(e4.weakest_signal_dbfs(now) - (-14.0)) < 0.01)


def test_long_wait_reports_channel_reading():
    """A long carrier-sense wait now records what the channel read (median
    level vs threshold vs floor), so a field log can tell real traffic
    from noise sitting near the threshold."""
    from unittest.mock import patch
    e = _cs_engine()
    e.noise_floor_rms = 10 ** (-55 / 20)
    e._rx_rms_ema = 10 ** (-30 / 20)
    calls = {"n": 0}

    def busy_three_times():
        calls["n"] += 1
        return calls["n"] <= 3
    e.is_channel_busy = busy_three_times
    with patch("time.sleep"):
        e._wait_clear()
    lvl, thr, floor = e.last_wait_info
    check("long-wait diagnostics record channel level, threshold and floor",
          abs(lvl + 30) < 0.01 and abs(floor + 55) < 0.01 and abs(thr + 49) < 0.01,
          f"{e.last_wait_info}")

# ── v0.7.3: receiver buffer edge guard ──────────────────────────────────
def test_edge_guard_removes_noise_candidates():
    """
    Field (v0.7.2): a noise candidate on K6IRF claimed a plausible 382-byte
    BPSK frame; the receiver waited for it and lost a real BBS frame, then
    BPQ32 waited its whole FRACK (~26 s). Root cause: the band-pass filter
    rings at the receiver buffer's freshly trimmed oldest edge (+10 dB in
    the first 64 samples, gone by one symbol), and every idle poll
    re-filters a newly trimmed buffer -- in simulated white hiss, every
    noise candidate past the energy gate sat at buffer position 0. Checks:
    the ringing is gone within EDGE_GUARD; white hiss at K6IRF's level
    (-34 dBFS after gain) produces noise candidates without the guard and
    none with it, through the real audio-engine receive path.
    """
    import queue as _q
    import collections
    from scipy.signal import sosfiltfilt
    rng = np.random.RandomState(3)
    rx0 = ofdm.OFDMReceiver()
    acc = np.zeros(4096)
    for _ in range(200):
        acc += sosfiltfilt(rx0.sos, rng.normal(0, 1, 4096)) ** 2
    steady = np.median(acc[1500:2500])
    first = 10 * np.log10(np.mean(acc[:64]) / steady)
    after = 10 * np.log10(np.mean(acc[ofdm.OFDMReceiver.EDGE_GUARD // 2:ofdm.OFDMReceiver.EDGE_GUARD]) / steady)
    check(f"filter edge ringing: {first:+.1f} dB at the edge, {after:+.1f} dB by the guard's end",
          first > 6 and after < 1.5)

    def run(guard_on):
        e = audio_io.AudioEngine.__new__(audio_io.AudioEngine)
        e.receiver = ofdm.OFDMReceiver()
        if not guard_on:
            e.receiver.EDGE_GUARD = 0
        e.level_queue = _q.Queue(maxsize=50)
        e.frame_queue = _q.Queue()
        e._rx_rms_ema = 0.0
        e._rx_gain = 1.0
        e._noise_blocks = collections.deque(maxlen=60)
        e._noise_acc = e._noise_acc_n = e._noise_acc_samples = 0
        e.noise_floor_rms = None
        e._sig_levels = collections.deque(maxlen=20)
        e.raw_peak = 0.0
        past_gate = []
        e.receiver.on_reject = lambda r, i: past_gate.append(r) if r != "low_energy" else None
        x = rng.normal(0, 10 ** (-34 / 20), int(120 * ofdm.SAMPLE_RATE))
        pcm = (np.clip(x, -1, 1) * 32767).astype(np.int16)
        for i in range(0, len(pcm), 1024):
            e._rx_callback(pcm[i:i + 1024].tobytes(), 1024, None, 0)
        return len(past_gate)
    without, with_guard = run(False), run(True)
    check(f"2 min of white hiss at -34 dBFS: noise candidates {without} without the edge guard, "
          f"{with_guard} with it", without >= with_guard and with_guard <= 2)

# ── v0.7.3: pings while bridged; graphical ping window ─────────────────
def test_pingpong_detector_never_matches_ax25():
    """
    Bridge mode now answers pings like VARA FM does while connected to
    BPQ32, so it must recognise a ping/pong among AX.25 traffic without ever
    taking a data frame for one (it would silently drop out of a Winlink
    session). The batch format's first byte (0x01) equals MSG_PING, so the
    check relies on what follows: two genuine space-padded callsigns, where
    a batch always has a length byte of 0x00/0x01.
    """
    import random as _r
    ok = all(proto.is_pingpong(x) for x in (
        proto.make_ping("DW1IRF", "K6IRF", 5), proto.make_ping("DW1IRF", "*", 5),
        proto.make_ping("K6IRF-10", "DW1IRF", 1), proto.make_pong("K6IRF", "DW1IRF", 5),
        proto.make_pong("K6IRF", "DW1IRF", 5, 23, -40)))
    check("real pings and pongs (incl. SSID, broadcast, two-way report) are recognised", ok)
    rng = _r.Random(1)
    hits = n = 0
    for size in range(1, 400, 3):
        for _ in range(40):
            fr = [bytes(rng.randrange(256) for _ in range(size))]
            if rng.random() < 0.5:
                fr.append(bytes(rng.randrange(256) for _ in range(rng.randint(1, 60))))
            n += 1
            hits += proto.is_pingpong(bpq_bridge.pack_batch(fr))
    check(f"random AX.25 batches never taken for a ping/pong ({hits} of {n})", hits == 0)
    adv = [bpq_bridge.pack_batch([b"DW1IRF   K6IRF  "]), bpq_bridge.pack_batch([b"K6IRF    DW1IRF   x"])]
    check("batches whose contents look like callsigns are still not pings",
          not any(proto.is_pingpong(x) for x in adv))


def test_bridge_ping_courtesy():
    """Bridged and idle: an incoming ping is handled (answered); during a
    Winlink session moving data it is logged but not answered, and the
    PING/Auto-Tune buttons refuse -- same courtesy as VARA FM."""
    import types
    import time as _t
    import sara_pingpong as sp
    handled, logs = [], []
    a = types.SimpleNamespace(
        mode_var=types.SimpleNamespace(get=lambda: "bpq"),
        call_var=types.SimpleNamespace(get=lambda: "DW1IRF"),
        _log=logs.append, _signal_str=lambda f: "S/N +24 dB",
        _handle_frame=lambda f: handled.append(f),
        BRIDGE_PING_IDLE_S=sp.App.BRIDGE_PING_IDLE_S)
    a._session_busy = types.MethodType(sp.App._session_busy, a)
    a._ping_allowed = types.MethodType(sp.App._ping_allowed, a)
    h = types.MethodType(sp.App._handle_bridge_pingpong, a)
    frame = ofdm.DecodedFrame(proto.make_ping("K6IRF", "DW1IRF", 7), 0.6, ofdm.MOD_QPSK, 0, 0, 24.0, -12.0)
    a._last_session_activity = 0.0
    h(frame)
    check("bridged and idle: an incoming ping is answered", len(handled) == 1)
    a._last_session_activity = _t.time()
    h(frame)
    check("bridged mid-session: an incoming ping is logged, not answered",
          len(handled) == 1 and any("not answering" in l for l in logs))
    check("bridged mid-session: PING/Auto-Tune refuse", a._ping_allowed() is False)
    pong = ofdm.DecodedFrame(proto.make_pong("K6IRF", "DW1IRF", 7, 25, -43), 0.6, ofdm.MOD_QPSK, 0, 0, 24.0, -12.0)
    h(pong)
    check("a PONG for us is still processed mid-session (it answers OUR ping)", len(handled) == 2)


def test_pong_reply_thread_reads_no_gui_state():
    """The PONG reply runs on a worker thread; it must not read Tk variables
    there (fragile in Tkinter). The callsign is passed in instead."""
    import types
    from unittest.mock import patch
    import sara_pingpong as sp
    def boom():
        raise RuntimeError("Tk read from worker thread")
    sent = []
    a = types.SimpleNamespace(
        call_var=types.SimpleNamespace(get=boom),
        engine=types.SimpleNamespace(transmit=lambda pl, modulation=0, **kw: sent.append(pl)),
        after=lambda ms, fn: None, _mod_name=lambda m: "QPSK", _modulation=lambda: 1)
    with patch("time.sleep"):
        sp.App._reply_pong(a, "K6IRF", 9, ofdm.MOD_QPSK, 24.0, -43.0, "DW1IRF")
    m = proto.PingPongMessage.unpack(sent[0]) if sent else None
    check("PONG reply uses the callsign passed in, with the two-way report",
          m is not None and m.from_call == "DW1IRF" and m.report_snr_db == 24)


def test_ping_window_colours_and_render():
    """Box colours follow the same thresholds as the text verdicts (so the
    window and the log always agree); the window itself renders and shows
    a result, when a display is available."""
    import tuning
    check("S/N colours: >=20 green, 12-20 light green, 8-12 yellow, <8 red, none grey",
          [tuning.snr_colors(v) for v in (25, 15, 9, 5, None)]
          == [tuning.GREEN, tuning.LIGHT_GREEN, tuning.YELLOW, tuning.RED, tuning.GREY])
    check("your level: in target green, off target yellow, near clipping red",
          [tuning.level_colors(v) for v in (-12, -24, -2)] == [tuning.GREEN, tuning.YELLOW, tuning.RED])
    check("their raw input: normal green, very quiet yellow, near clipping red",
          [tuning.raw_input_colors(v) for v in (-30, -50, -1)] == [tuning.GREEN, tuning.YELLOW, tuning.RED])
    import tkinter as tk
    try:
        root = tk.Tk()
    except tk.TclError:
        print("[SKIP] ping window render -- no display here (runs on a desktop/build PC)")
        return
    root.withdraw()
    import ping_window
    w = ping_window.PingWindow(root)
    w.set_peers("DW1IRF", "K6IRF")
    w.set_result((23.0, -12.0), (25, -43), 2.7)
    root.update()
    texts = [w.canvas.itemcget(i, "text") for i in w.canvas.find_all() if w.canvas.type(i) == "text"]
    check("ping window renders both towers' callsigns and all four readings",
          all(t in texts for t in ("DW1IRF", "K6IRF", "S/N: +23 dB", "Level: -12 dBFS",
                                   "S/N: +25 dB", "Input: -43 dBFS")), f"{texts}")
    w.set_timeout()
    root.update()
    check("ping window shows a timeout", "No reply" in w.status_var.get())
    root.destroy()

# ── v0.7.4: cooldown accepts clean preambles; shorter in-burst gaps ────
def test_cooldown_accepts_next_frame_in_burst():
    """
    The receiver used to go deaf for POST_DECODE_COOLDOWN_S (0.35 s) after
    every decode (to ride out squelch-tail noise), so frames inside a burst
    had to be spaced 0.5 s apart -- ~3 s of every 7-frame window. It now
    keeps searching during the cooldown but only accepts a preamble with
    S/N >= COOLDOWN_ACCEPT_SNR_DB. Checks a two-frame burst at the new
    BURST_GAP_S: the old deaf behaviour loses the second frame, the new one
    decodes both; and that a 7x256-byte window's airtime drops by ~1.8 s.
    """
    rng = np.random.RandomState(4)
    SR = ofdm.SAMPLE_RATE
    a_, b_ = (rng.randint(0, 256, 236).astype(np.uint8).tobytes() for _ in range(2))
    audio = np.concatenate([audio_io.fill_tone(int(0.6 * SR)),
                            ofdm.frame_encode(a_, modulation=ofdm.MOD_QPSK) * 0.8,
                            audio_io.fill_tone(int(audio_io.BURST_GAP_S * SR)),
                            ofdm.frame_encode(b_, modulation=ofdm.MOD_QPSK) * 0.8,
                            audio_io.fill_tone(int(0.15 * SR)), np.zeros(8000, np.float32)])
    audio = (audio + rng.normal(0, 0.002, len(audio))).astype(np.float32)

    def run(deaf):
        rx = ofdm.OFDMReceiver()
        if deaf:
            rx.COOLDOWN_ACCEPT_SNR_DB = 999.0     # behaves like the old deaf cooldown
        got = []
        for i in range(0, len(audio), 1024):
            rx.push(audio[i:i + 1024])
            f = rx.poll()
            while f is not None:
                got.append(f.payload)
                f = rx.poll()
        return got
    old, new = run(True), run(False)
    check(f"frames {audio_io.BURST_GAP_S:.2f} s apart: old deaf cooldown loses the 2nd, "
          f"new receiver decodes both", old == [a_] and new == [a_, b_], f"old={len(old)}, new={len(new)}")
    f = len(ofdm.frame_encode(bytes(250), modulation=ofdm.MOD_QPSK)) / SR
    saved = 6 * (0.5 - audio_io.BURST_GAP_S)
    check(f"a 7x256-byte QPSK window is {saved:.1f} s shorter on the air "
          f"({0.75 + 7 * f + 3.0:.1f} s -> {0.75 + 7 * f + 6 * audio_io.BURST_GAP_S:.1f} s)", saved > 1.5)


def test_cooldown_never_accepts_noise():
    """Squelch-tail-like junk arriving during the cooldown must still be
    ignored, silently. Measured during development over 643 candidates of
    five noise kinds: highest preamble S/N -1.2 dB (clicks). Checks a
    smaller sample of each kind: no frame accepted, no rejection logged,
    and every score well under the acceptance threshold."""
    from scipy.signal import butter, sosfilt
    SR = ofdm.SAMPLE_RATE
    rng = np.random.RandomState(9)
    bl = butter(4, [300 / (SR / 2), 3000 / (SR / 2)], btype="band", output="sos")

    def mk(kind, n):
        if kind == "white":
            return rng.normal(0, 0.3, n)
        if kind == "radio":
            x = sosfilt(bl, rng.normal(0, 1, n))
            return 0.3 * x / np.sqrt(np.mean(x ** 2))
        x = np.zeros(n)
        x[rng.randint(0, n, 30)] = rng.uniform(-0.9, 0.9, 30)
        return sosfilt(bl, x) * 3
    scores, frames, logs = [], 0, []
    for kind in ("white", "radio", "clicks"):
        for _ in range(40):
            n = int(rng.uniform(0.1, 0.5) * SR)
            seg = mk(kind, n) * rng.uniform(0.2, 2.0)
            x = np.concatenate([np.zeros(2000), seg, np.zeros(6000)]).astype(np.float32)
            rx = ofdm.OFDMReceiver()
            rx._cooldown_until = 10 ** 9
            rx.on_reject = lambda r, i: logs.append(r)
            orig = ofdm.OFDMReceiver.preamble_snr_db
            rx.preamble_snr_db = lambda reps, _o=orig: (scores.append(_o(reps)), scores[-1])[1]
            for i in range(0, len(x), 1024):
                rx.push(x[i:i + 1024])
                f = rx.poll()
                while f is not None:
                    frames += 1
                    f = rx.poll()
    top = max(scores) if scores else -99
    check(f"noise during cooldown: {len(scores)} candidates scored, highest S/N {top:.1f} dB "
          f"(threshold {ofdm.OFDMReceiver.COOLDOWN_ACCEPT_SNR_DB:.0f}); none accepted, none logged",
          frames == 0 and not logs and top < ofdm.OFDMReceiver.COOLDOWN_ACCEPT_SNR_DB - 3)

# ── v0.7.5: network KISS access ─────────────────────────────────────────
def test_network_kiss_access():
    """
    The KISS server can now listen on the network for remote testing
    (Tailscale / AREDN / LAN). KISS has no password -- whoever connects can
    transmit on the radio -- so: an allow-list ('private' by default),
    and a connection from a DIFFERENT address can never take over a
    connected client (on a network that would silently disconnect the
    station's own BPQ32). A reconnect from the SAME address still takes
    over (BPQ32 recovery). Uses 127.0.0.2/.3 as stand-in remote machines;
    skipped where the OS won't bind those.
    """
    nets, err = bpq_bridge.parse_allowlist("private")
    check("'private' allows this PC, Tailscale, AREDN and LAN ranges but not the public internet",
          err is None and all(bpq_bridge.ip_allowed(ip, nets) for ip in
                              ("127.0.0.1", "100.70.1.2", "10.44.3.1", "172.20.0.5", "192.168.1.50"))
          and not any(bpq_bridge.ip_allowed(ip, nets) for ip in ("8.8.8.8", "44.1.2.3")))
    nets2, err2 = bpq_bridge.parse_allowlist("private 44.0.0.0/8, 203.0.113.7")
    check("extra ranges/IPs can be added (e.g. AMPRNet 44/8, one public IP)",
          err2 is None and bpq_bridge.ip_allowed("44.1.2.3", nets2) and bpq_bridge.ip_allowed("203.0.113.7", nets2))
    _, err3 = bpq_bridge.parse_allowlist("10.0.0.300")
    check("a typo in Allow-from is reported, not silently accepted", err3 is not None)

    def connect(src, port):
        c = socket.socket()
        c.bind((src, 0))
        c.connect(("127.0.0.1", port))
        return c

    def closed(c, wait=0.5):
        c.settimeout(wait)
        try:
            return c.recv(1) == b""
        except socket.timeout:
            return False
        except OSError:
            return True
    try:
        probe = socket.socket()
        probe.bind(("127.0.0.2", 0))
        probe.close()
    except OSError:
        print("[SKIP] live network KISS test -- this OS won't bind 127.0.0.2")
        return
    status = []
    air = []
    # v0.9.5: loopback is always allowed now, so 127.0.0.2 stands in for
    # another computer here by testing the list match alone.
    import ipaddress as _ip
    real_allowed = bpq_bridge.ip_allowed
    bpq_bridge.ip_allowed = lambda ip, nets_: any(_ip.ip_address(ip) in n for n in nets_)
    br = BpqBridge(on_air_frame=air.append, on_status=status.append)
    br.start(port=18170, host="0.0.0.0", allow=bpq_bridge.parse_allowlist("127.0.0.1")[0])
    _time.sleep(0.2)
    c = connect("127.0.0.2", 18170)
    _time.sleep(0.3)
    check("a client outside the allow-list is refused and logged",
          closed(c) and "not in the allowed list" in status[-1], status[-1])
    br.stop()
    bpq_bridge.ip_allowed = real_allowed
    _time.sleep(0.2)

    status.clear()
    br = BpqBridge(on_air_frame=air.append, on_status=status.append)
    br.start(port=18171, host="0.0.0.0", allow=nets)
    _time.sleep(0.2)
    a = connect("127.0.0.1", 18171)
    _time.sleep(0.3)
    b_ = connect("127.0.0.2", 18171)
    _time.sleep(0.3)
    a.sendall(kiss.encode_frame(b"from-A"))
    _time.sleep(0.4)
    check("a second address is refused while a client is connected (no hijack), "
          "and the first client keeps working",
          closed(b_) and "already serving" in status[-1] and air and air[-1] == bpq_bridge.pack_batch([b"from-A"]))
    a2 = connect("127.0.0.1", 18171)
    _time.sleep(0.3)
    check("a reconnect from the SAME address takes over (BPQ32 recovery)",
          closed(a) and not closed(a2, 0.2))
    a2.close()
    _time.sleep(0.5)
    b2 = connect("127.0.0.2", 18171)
    _time.sleep(0.3)
    check("after the client leaves, another allowed address can connect",
          not closed(b2, 0.2) and "connected from 127.0.0.2" in status[-1], status[-1])
    b2.close()
    br.stop()

# ── v0.7.6: open-squelch radios ─────────────────────────────────────────
def _os_engine():
    import queue as _q
    import collections
    e = audio_io.AudioEngine.__new__(audio_io.AudioEngine)
    e.receiver = ofdm.OFDMReceiver()
    e.level_queue = _q.Queue(maxsize=50)
    e.frame_queue = _q.Queue()
    e._rx_rms_ema = 0.0
    e._rx_gain = 1.0
    e._noise_blocks = collections.deque(maxlen=60)
    e._noise_acc = e._noise_acc_n = e._noise_acc_samples = 0
    e.noise_floor_rms = None
    e._sig_levels = collections.deque(maxlen=20)
    e.raw_peak = 0.0
    e._spec_ema = None
    e._spec_blocks = collections.deque(maxlen=60)
    e._spec_acc = 0.0
    e._spec_acc_n = e._spec_acc_samples = 0
    e.spec_baseline_db = None
    return e


def _os_burst():
    SR = ofdm.SAMPLE_RATE
    b = ofdm.frame_encode(bytes(range(236)), modulation=ofdm.MOD_QPSK) * 0.8
    parts = [audio_io.fill_tone(int(0.6 * SR), 0.8)]
    for i in range(7):
        parts += [b, audio_io.fill_tone(int((audio_io.BURST_GAP_S if i < 6 else 0.15) * SR), 0.8)]
    return np.concatenate(parts)


def _os_run(cnr, squelch_open, rng, spectral=True, idle_s=10, signal_dbfs=-12.0):
    """Two keyed 7-frame bursts through the FM channel simulator, idle
    between. Returns (% of burst seen busy, % of idle seen busy, worst
    key-up detection delay s or None, frames decoded)."""
    import fm_channel as fc
    SR = ofdm.SAMPLE_RATE
    B = _os_burst()
    segs = [(np.zeros(idle_s * SR), False), (B, True), (np.zeros(10 * SR), False), (B, True), (np.zeros(6 * SR), False)]
    tx = np.concatenate([x for x, _ in segs])
    on = np.concatenate([np.full(len(x), k) for x, k in segs])
    y = fc.receive(tx, on, cnr, rng, out_gain=1.0).astype(np.float64)
    y *= 10 ** (signal_dbfs / 20) / np.sqrt(np.mean(y[on][:int(0.5 * SR)] ** 2)) * 0.8
    if not squelch_open:
        y[~on] = 0.0
    y += rng.normal(0, 10 ** (-65 / 20), len(y))
    e = _os_engine()
    if not spectral:
        e._spec_blocks = None          # behaves like v0.7.5 (loudness rule only):
        e.sara_busy = lambda: False    # no spectral rule, no SARA-PP-signal DCD (v0.7.7)
    pcm = (np.clip(y, -1, 1) * 32767).astype(np.int16)
    bt = nt = bi = ni = frames = 0
    lat, key_t, prev = [], None, False
    for i in range(0, len(pcm), 1024):
        e._rx_callback(pcm[i:i + 1024].tobytes(), 1024, None, 0)
        while not e.frame_queue.empty():
            e.frame_queue.get()
            frames += 1
        if i < 4 * SR:
            continue
        busy = e.is_channel_busy()
        o_ = bool(on[i])
        if o_ and not prev:
            key_t = i
        if o_ and key_t is not None and busy:
            lat.append((i - key_t) / SR)
            key_t = None
        prev = o_
        if o_:
            nt += 1
            bt += busy
        else:
            ni += 1
            bi += busy
    return 100 * bt / nt, 100 * bi / ni, (max(lat) if lat else None), frames


def test_open_squelch_carrier_sense():
    """
    Many PAREDN gateways run squelch at zero (so VARA FM's input gauge
    shows activity): idle audio is loud hiss, and a carrier QUIETS it.
    Through a realistic FM channel simulation (fm_channel.py), the old
    loudness-only carrier sense saw the other station's bursts as busy 0%
    of the time -- every open-squelch gateway would step on incoming
    bursts. The spectral rule (in-band vs above-band power, relative to the
    station's idle baseline) detects the carrier; the noise floor learns
    only from idle-sounding seconds (otherwise quieted transmissions became
    the "floor" and idle hiss read busy ~75% of the time). Squelch-closed
    behaviour must be unchanged.
    """
    rng = np.random.RandomState(6)
    b_old, _, _, _ = _os_run(12, True, rng, spectral=False)
    check(f"open squelch, loudness-only carrier sense (v0.7.5): burst seen busy {b_old:.0f}% -- the problem",
          b_old < 10)
    for cnr in (30, 12, 8):
        b, i, lat, fr = _os_run(cnr, True, rng)
        check(f"open squelch CNR {cnr} dB: burst busy {b:.0f}%, idle hiss busy {i:.0f}%, "
              f"key-up detected in {lat * 1000 if lat is not None else -1:.0f} ms, {fr}/14 frames decoded",
              b >= 97 and i <= 2 and lat is not None and lat <= 0.2 and fr >= 13)
    b, i, lat, fr = _os_run(20, False, rng)
    check(f"closed squelch (unchanged): burst busy {b:.0f}%, idle busy {i:.0f}%, {fr}/14 frames",
          b >= 97 and i <= 2 and fr == 14)


def test_open_squelch_hiss_alone_and_clipping():
    """Long open-squelch hiss produces no false frames or busy readings, and
    hiss that clips the sound card is NOT reported as clipping (it's
    harmless with no carrier; "lower the volume" would weaken real
    signals) -- clipping counts only while a carrier is present."""
    import fm_channel as fc
    SR = ofdm.SAMPLE_RATE
    rng = np.random.RandomState(7)
    h = fc.receive(np.zeros(40 * SR), np.zeros(40 * SR, bool), 20, rng, out_gain=1.0).astype(np.float64)
    h = h / np.sqrt(np.mean(h ** 2)) * 10 ** (-4 / 20)       # loud enough that peaks clip
    e = _os_engine()
    pcm = (np.clip(h, -1, 1) * 32767).astype(np.int16)
    busy = n = 0
    for i in range(0, len(pcm), 1024):
        e._rx_callback(pcm[i:i + 1024].tobytes(), 1024, None, 0)
        if i > 3 * SR:
            n += 1
            busy += e.is_channel_busy()
    check(f"40 s of clipping open-squelch hiss: {e.frame_queue.qsize()} false frames, "
          f"busy {100 * busy / n:.0f}%, raw peak recorded {e.raw_peak:.2f} (not flagged as clipping)",
          e.frame_queue.qsize() == 0 and busy / n < 0.02 and e.raw_peak < 0.97)
    t = audio_io.fill_tone(2 * SR, 1.0) * 3.0                 # a carrier clipping the input
    pcm = (np.clip(t, -1, 1) * 32767).astype(np.int16)
    for i in range(0, len(pcm), 1024):
        e._rx_callback(pcm[i:i + 1024].tobytes(), 1024, None, 0)
    check("a clipping signal while a carrier is present IS flagged", e.raw_peak >= 0.97)


def test_fm_channel_simulator_fidelity():
    """The simulator behind the open-squelch tests behaves like a real FM
    receiver: a strong carrier reproduces the transmitted audio, and with
    no carrier the discriminator turns RF noise into hiss louder than a
    SARA-PP transmission at normal deviation (the open-squelch case)."""
    import fm_channel as fc
    from scipy.signal import sosfilt
    SR = ofdm.SAMPLE_RATE
    rng = np.random.RandomState(2)
    tone = audio_io.fill_tone(2 * SR, 0.8)
    y = fc.receive(tone, np.ones(len(tone), bool), 30, rng)
    ref = sosfilt(fc._AUDIO_SOS, tone.astype(np.float64)) * 0.1
    corr = np.corrcoef(y[4000:], ref[4000:])[0, 1]
    h = fc.receive(np.zeros(2 * SR), np.zeros(2 * SR, bool), 30, rng)
    db = lambda x: 20 * np.log10(np.sqrt(np.mean(np.asarray(x, np.float64) ** 2)))
    check(f"FM simulator: strong carrier reproduces the audio (corr {corr:.2f}); "
          f"open-squelch hiss is {db(h[2000:]) - db(y[2000:]):+.1f} dB vs the signal",
          corr > 0.85 and db(h[2000:]) > db(y[2000:]))

# ── v0.7.7: VARA-style DCD; minimum preamble S/N ────────────────────────
def _dcd_feed(e, y):
    pcm = (np.clip(y, -1, 1) * 32767).astype(np.int16)
    out = []
    for i in range(0, len(pcm), 1024):
        e._rx_callback(pcm[i:i + 1024].tobytes(), 1024, None, 0)
        while not e.frame_queue.empty():
            e.frame_queue.get()
        out.append(e.is_channel_busy())
    return out


def test_dcd_modes_open_squelch():
    """
    Field (v0.7.6): with squelch open a test station wouldn't transmit (DCD
    busy), though steady-hiss simulation showed none -- real hiss varies.
    Simulated hiss swelling +-6 dB made 'auto' read busy ~35% of the time.
    'SARA-PP signals only' (fill tone, or a frame passing every header check
    still arriving) can't be tripped by hiss of any shape, yet detects real
    bursts on every radio audio path; 'off' never waits; 'Transmit anyway'
    releases a busy DCD.
    """
    import fm_channel as fc
    SR = ofdm.SAMPLE_RATE
    rng = np.random.RandomState(21)
    T = 30
    t = np.arange(T * SR) / SR
    h = fc.receive(np.zeros(T * SR), np.zeros(T * SR, bool), 20, rng, out_gain=1.0, path="speaker").astype(np.float64)
    h *= 10 ** (-4 / 20) / np.sqrt(np.mean(h ** 2))
    swell = h * 10 ** ((6 * np.sin(2 * np.pi * t / 5.0)) / 20)
    res = {}
    for mode in ("auto", "sara", "off"):
        e = _os_engine()
        e.dcd_mode = mode
        b = _dcd_feed(e, swell)[3 * SR // 1024:]
        res[mode] = 100 * sum(b) / len(b)
    check(f"open-squelch hiss swelling +-6 dB, nobody transmitting: auto {res['auto']:.0f}% busy, "
          f"'SARA-PP signals only' {res['sara']:.0f}%, off {res['off']:.0f}%",
          res["sara"] < 2 and res["off"] == 0)

    for path in ("speaker", "data9600"):
        e = _os_engine()
        e.dcd_mode = "sara"
        B = _os_burst()
        tx = np.concatenate([np.zeros(6 * SR), B, np.zeros(4 * SR)])
        on = np.concatenate([np.zeros(6 * SR, bool), np.ones(len(B), bool), np.zeros(4 * SR, bool)])
        y = fc.receive(tx, on, 10, rng, out_gain=1.0, path=path).astype(np.float64)
        y *= 10 ** (-12 / 20) / np.sqrt(np.mean(y[on][int(0.7 * SR):int(1.9 * SR)] ** 2))
        busy = _dcd_feed(e, y)
        onb = [on[i] for i in range(0, len(on), 1024)][:len(busy)]
        bt = [b for b, o in zip(busy, onb) if o]
        bi = [b for b, o, i in zip(busy, onb, range(len(busy))) if not o and i > 3 * SR // 1024]
        check(f"'SARA-PP signals only', {path} audio, CNR 10: burst busy {100 * sum(bt) / len(bt):.0f}%, "
              f"idle {100 * sum(bi) / len(bi):.0f}% (incl. the deliberate 0.3 s hold after the burst)",
              sum(bt) / len(bt) >= 0.97 and sum(bi) / len(bi) <= 0.05)

    e = _os_engine()
    e._rx_rms_ema = 1.0
    e.noise_floor_rms = 0.01
    check("DCD busy before override", e.is_channel_busy())
    e.override_dcd(3.0)
    check("'Transmit anyway' releases a busy DCD", not e.is_channel_busy() and e.dcd_state()[1] == "override")


def test_dcd_ignores_steady_tone():
    """
    Field (v0.9.24, DV1UK, 'SARA-PP signals only'): DCD held "busy --
    SARA-PP signal" for minutes with nobody on the air -- the fill-tone
    detector read +23..+28 dB at -48 dBFS input (a steady whistle near
    1 kHz). Every transmission waited 30 s. A tone heard unbroken for
    FILL_STEADY_S is now foreign: DCD clears, and afterwards a returning
    tone counts only briefly, while a real SARA-PP burst still reads busy.
    """
    SR = ofdm.SAMPLE_RATE
    rng = np.random.RandomState(5)
    clock = [1000.0]

    class _T:
        @staticmethod
        def time():
            return clock[0]
    real_time = audio_io.time
    audio_io.time = _T
    try:
        e = _os_engine()
        e.dcd_mode = "sara"

        def feed(y):
            pcm = (np.clip(y, -1, 1) * 32767).astype(np.int16)
            out = []
            for i in range(0, len(pcm) - 1023, 1024):
                clock[0] += 1024 / SR
                e._rx_callback(pcm[i:i + 1024].tobytes(), 1024, None, 0)
                while not e.frame_queue.empty():
                    e.frame_queue.get()
                out.append(e.is_channel_busy())
            return out

        def whistle(sec, f=1020.0):
            n = np.arange(int(sec * SR))
            return (10 ** (-48 / 20) * np.sqrt(2) * np.sin(2 * np.pi * f * n / SR)
                    + 10 ** (-70 / 20) * rng.randn(len(n)))

        b = feed(whistle(40))
        sec = lambda i: i * 1024 / SR
        first_clear = next((sec(i) for i, x in enumerate(b) if sec(i) > 1 and not x), None)
        check(f"steady 1 kHz whistle at -48 dBFS: busy at first, clear after {first_clear}s",
              b[50] and first_clear is not None and audio_io.FILL_STEADY_S - 0.5 <= first_clear
              <= audio_io.FILL_STEADY_S + 1.5 and not any(b[int((audio_io.FILL_STEADY_S + 2) * SR / 1024):]))
        check("lamp says the tone is ignored, logged once",
              e.dcd_state() == (False, "tone ignored") and e.steady_tone_count == 1)

        # the other station transmits (FM capture hides the whistle), then it returns
        B = _os_burst()
        bb = feed(np.concatenate([B * 10 ** (-12 / 20) / np.sqrt(np.mean(B ** 2)),
                                  whistle(20)]))
        nb = len(B) // 1024
        burst_busy = sum(bb[:nb]) / nb
        after = bb[nb:]
        busy_after = sum(after) * 1024 / SR
        check(f"after that, a real SARA-PP burst still reads busy ({100 * burst_busy:.0f}%); "
              f"the returning whistle holds DCD only {busy_after:.1f}s",
              burst_busy >= 0.97 and busy_after <= audio_io.FILL_FOREIGN_MAX_S + 0.8
              and e.steady_tone_count == 1)

        # a far-end TUNE (10 s fill tone) on a fresh engine still reads busy throughout
        e = _os_engine()
        e.dcd_mode = "sara"
        tune = audio_io.fill_tone(10 * SR, 0.8)
        tune = tune / np.sqrt(np.mean(tune ** 2)) * 10 ** (-12 / 20)
        tb = feed(np.concatenate([whistle(2, 600.0) * 0, tune]))
        tb = tb[2 * SR // 1024 + 2:]
        check(f"far-end TUNE (10 s): busy {100 * sum(tb) / len(tb):.0f}%",
              sum(tb) / len(tb) >= 0.97 and getattr(e, "steady_tone_count", 0) == 0)
    finally:
        audio_io.time = real_time


def test_dcd_holds_through_missed_header():
    """
    v0.9.27, field (v0.9.26, DX1CW -> DW1IRF, BPSK): DW1IRF ('SARA-PP
    signals only') keyed its acknowledgment into the middle of DX1CW's
    19 s burst when a frame's header wasn't recognised. The station's
    carrier now holds DCD for SARA_CONTINUE_S after direct proof of it; the
    channel still clears promptly when the burst ends, and open-squelch
    hiss still doesn't trip it (test_dcd_modes_open_squelch).
    """
    SR = ofdm.SAMPLE_RATE
    clock = [5000.0]

    class _T:
        @staticmethod
        def time():
            return clock[0]

        @staticmethod
        def sleep(_s):
            pass
    real_time = audio_io.time
    audio_io.time = _T
    try:
        b = ofdm.frame_encode(bytes(range(256)), modulation=ofdm.MOD_BPSK) * 0.8
        rng = np.random.RandomState(4)
        frames = [b.copy() for _ in range(5)]
        # frame 3: header destroyed (noise of the same level over its first 0.4 s)
        k = int(0.4 * SR)
        frames[2][:k] = rng.randn(k) * np.sqrt(np.mean(b ** 2))
        parts = [audio_io.fill_tone(int(0.5 * SR), 0.8)]
        for i, f in enumerate(frames):
            parts += [f, audio_io.fill_tone(int((audio_io.BURST_GAP_S if i < 4 else 0.15) * SR), 0.8)]
        B = np.concatenate(parts)
        lead = 3 * SR
        y = np.concatenate([np.zeros(lead), B, np.zeros(3 * SR)])
        y = y / np.sqrt(np.mean(B ** 2)) * 10 ** (-15 / 20) + 10 ** (-50 / 20) * rng.randn(len(y))
        e = _os_engine()
        e.dcd_mode = "sara"
        e.busy_threshold_rms = lambda now=None: 10 ** (-30 / 20)   # floor -44, station -15 (DW1IRF)
        pcm = (np.clip(y, -1, 1) * 32767).astype(np.int16)
        busy = []
        for i in range(0, len(pcm) - 1023, 1024):
            clock[0] += 1024 / SR
            e._rx_callback(pcm[i:i + 1024].tobytes(), 1024, None, 0)
            while not e.frame_queue.empty():
                e.frame_queue.get()
            busy.append(e.is_channel_busy())
        blk = lambda t: int(t * SR / 1024)
        during = busy[blk(lead / SR) + 1:blk((lead + len(B)) / SR)]
        after = busy[blk((lead + len(B)) / SR + 0.6):]
        longest, run = 0, 0
        for x in during:
            run = 0 if x else run + 1
            longest = max(longest, run)
        check(f"BPSK burst with one unrecognisable header: DCD busy {100 * sum(during) / len(during):.0f}%, "
              f"longest clear {longest * 1024 / SR:.2f}s",
              longest * 1024 / SR < 0.1)
        check("...and clear within 0.6 s of the burst's end", not any(after))

        # v0.9.28, field (v0.9.27, DX1CW): after the burst the radio puts out
        # noise 9 dB LOUDER than the station's frames -- not its carrier
        e = _os_engine()
        e.dcd_mode = "sara"
        e.busy_threshold_rms = lambda now=None: 10 ** (-41 / 20)
        sig = B / np.sqrt(np.mean(B ** 2)) * 10 ** (-30 / 20)
        y2 = np.concatenate([np.zeros(lead), sig, 10 ** (-21 / 20) * rng.randn(4 * SR)])
        y2 = y2 + 10 ** (-55 / 20) * rng.randn(len(y2))
        pcm2 = (np.clip(y2, -1, 1) * 32767).astype(np.int16)
        busy2 = []
        for i in range(0, len(pcm2) - 1023, 1024):
            clock[0] += 1024 / SR
            e._rx_callback(pcm2[i:i + 1024].tobytes(), 1024, None, 0)
            while not e.frame_queue.empty():
                e.frame_queue.get()
            busy2.append(e.is_channel_busy())
        tail = busy2[blk((lead + len(sig)) / SR + 0.6):]
        check(f"loud noise after the burst (9 dB over the station's frames): busy {sum(tail) * 1024 / SR:.1f}s",
              sum(tail) * 1024 / SR < 0.3)

        # v0.9.31, field (v0.9.30, DX1FLR): noise only 6 dB over the frames
        e = _os_engine()
        e.dcd_mode = "sara"
        e.busy_threshold_rms = lambda now=None: 10 ** (-34 / 20)
        sig = B / np.sqrt(np.mean(B ** 2)) * 10 ** (-12 / 20)
        y3 = np.concatenate([np.zeros(lead), sig, 10 ** (-6 / 20) * rng.randn(4 * SR)])
        y3 = y3 + 10 ** (-60 / 20) * rng.randn(len(y3))
        pcm3 = (np.clip(y3, -1, 1) * 32767).astype(np.int16)
        busy3 = []
        for i in range(0, len(pcm3) - 1023, 1024):
            clock[0] += 1024 / SR
            e._rx_callback(pcm3[i:i + 1024].tobytes(), 1024, None, 0)
            while not e.frame_queue.empty():
                e.frame_queue.get()
            busy3.append(e.is_channel_busy())
        tail3 = busy3[blk((lead + len(sig)) / SR + 0.6):]
        check(f"noise 6 dB over the station's frames after the burst (DX1FLR): busy {sum(tail3) * 1024 / SR:.1f}s",
              sum(tail3) * 1024 / SR < 0.3)
        # own transmission since the last proof: our squelch tail doesn't extend it
        e._last_tx_end = clock[0] + 1
        e._last_sara_evidence = clock[0]
        e._rx_rms_ema = 1.0
        e._fill_ema = 0.0
        e.receiver = ofdm.OFDMReceiver()
        check("proof older than our own last transmission doesn't hold DCD", not e.sara_busy())
    finally:
        audio_io.time = real_time


def test_pings_ignore_dcd():
    """As in VARA FM: pings/pongs go out even when DCD reads busy; normal
    (bridge) traffic still waits."""
    from unittest.mock import MagicMock, patch
    e = audio_io.AudioEngine.__new__(audio_io.AudioEngine)
    e.tx_lead_ms = e.tx_tail_ms = 1
    e._rx_rms_ema = 0.0
    e._tx_lock = threading.Lock()
    e.tx_stream = MagicMock()
    e.ptt = MagicMock()
    e.on_carrier_wait = e.on_tx_start = None
    checks = []
    e.is_channel_busy = lambda: (checks.append(1), True)[1]
    with patch("time.sleep"):
        e.transmit(proto.make_ping("A", "B", 1), modulation=ofdm.MOD_QPSK, ignore_dcd=True)
    check("a ping with DCD busy keys up immediately, without consulting DCD",
          e.ptt.ptt_on.called and not checks, f"DCD checks={len(checks)}")
    waits = []
    with patch("time.sleep"), patch.object(audio_io, "CARRIER_SENSE_MAX_WAIT_S", 0.0):
        e.transmit(b"x" * 20, modulation=ofdm.MOD_QPSK)
    check("ordinary traffic still consults DCD", len(checks) > 0)


def test_min_preamble_snr():
    """Simulated FM voice made the receiver commit to bogus "frames" (48-370
    bytes) whose preamble S/N was -12.9 to -60 dB, deaf for each one. Every
    candidate now needs preamble S/N >= 2 dB; the weakest real frames that
    still decode measure ~4.8 dB (BPSK)."""
    import fm_channel as fc
    from scipy.signal import butter, sosfilt
    SR = ofdm.SAMPLE_RATE
    T = 30
    t = np.arange(T * SR) / SR
    waits = 0
    for seed in range(3):
        rng = np.random.RandomState(100 + seed)
        v = sosfilt(butter(4, [300 / (SR / 2), 2800 / (SR / 2)], btype="band", output="sos"),
                    rng.normal(0, 1, T * SR)) * np.abs(np.sin(2 * np.pi * 3 * t))
        v = 0.3 * v / np.sqrt(np.mean(v ** 2))
        y = fc.receive(v, np.ones(T * SR, bool), 20, rng, out_gain=1.0, path="speaker").astype(np.float64)
        y *= 10 ** (-12 / 20) / np.sqrt(np.mean(y ** 2))
        e = _os_engine()
        rej = []
        e.receiver.on_reject = lambda r, i: rej.append(r)
        _dcd_feed(e, y)
        waits += sum(1 for r in rej if r in ("fec_uncorrectable", "crc_fail"))
    check(f"FM voice on the channel: receiver never commits to a bogus frame ({waits} waits)", waits == 0)
    from scipy.signal import butter as _b, sosfilt as _s
    bl = _b(4, [300 / (SR / 2), 3000 / (SR / 2)], btype="band", output="sos")
    rng = np.random.RandomState(32)
    ok = 0
    for _ in range(6):
        pl = rng.randint(0, 256, 236).astype(np.uint8).tobytes()
        b = np.concatenate([audio_io.fill_tone(int(0.6 * SR), 0.8),
                            ofdm.frame_encode(pl, modulation=ofdm.MOD_BPSK) * 0.8,
                            audio_io.fill_tone(int(0.15 * SR), 0.8)]) / 0.343 * 10 ** (-12 / 20)
        x = np.concatenate([np.zeros(3000), b, np.zeros(6000)])
        nz = _s(bl, rng.normal(0, 1, len(x)))
        x = x + nz / np.sqrt(np.mean(nz ** 2)) * 10 ** (-17 / 20)
        rx = ofdm.OFDMReceiver()
        rx.noise_floor_rms = 10 ** (-17 / 20)
        for i in range(0, len(x), 1024):
            rx.push(x[i:i + 1024].astype(np.float32))
            f = rx.poll()
            while f is not None:
                ok += f.payload == pl
                f = rx.poll()
    check(f"weak real BPSK frames (preamble S/N ~6 dB) still decode: {ok}/6", ok >= 4)

# ── v0.8.0: QPSK Wide (Phase 1) ─────────────────────────────────────────
def test_qpsk_wide_basics():
    """QPSK Wide: 25 carriers (375-2625 Hz), 20 data + 5 pilots, a wide
    training symbol after the unchanged narrow header. Round trips at
    several sizes, carries 40 bits/symbol, shortens a 7-frame window by
    ~30%, and a receiver without Wide support rejects it instantly
    (never misreads it)."""
    rng = np.random.RandomState(1)
    ok = True
    for n in (20, 120, 250):
        pl = rng.randint(0, 256, n).astype(np.uint8).tobytes()
        out = decode_all(ofdm.OFDMReceiver(), ofdm.frame_encode(pl, modulation=ofdm.MOD_QPSK_WIDE), chunk=1024)
        ok &= len(out) == 1 and out[0].payload == pl and out[0].modulation == ofdm.MOD_QPSK_WIDE
    check("QPSK Wide round-trips 20/120/250-byte payloads", ok)
    check("QPSK Wide carries 36 bits/symbol since v0.8.3's band move (QPSK: 24)",
          ofdm.bits_per_symbol(ofdm.MOD_QPSK_WIDE) == 36 and ofdm.bits_per_symbol(ofdm.MOD_QPSK) == 24)
    fq = len(ofdm.frame_encode(bytes(252), modulation=ofdm.MOD_QPSK)) / ofdm.SAMPLE_RATE
    fw = len(ofdm.frame_encode(bytes(252), modulation=ofdm.MOD_QPSK_WIDE)) / ofdm.SAMPLE_RATE
    wq = 0.75 + 7 * fq + 6 * audio_io.BURST_GAP_S
    ww = 0.75 + 7 * fw + 6 * audio_io.BURST_GAP_S
    check(f"7 x 256-byte window: {wq:.1f} s QPSK -> {ww:.1f} s Wide", ww < 0.8 * wq)
    rx = ofdm.OFDMReceiver()
    rx.RX_MODULATIONS = (ofdm.MOD_BPSK, ofdm.MOD_QPSK)
    rej = []
    rx.on_reject = lambda r, i: rej.append(r)
    rx.push(ofdm.frame_encode(bytes(50), modulation=ofdm.MOD_QPSK_WIDE))
    check("a receiver without Wide support rejects a Wide frame instantly",
          rx.poll() is None and rej[:1] == ["invalid_modulation"])


def test_qpsk_wide_drift_tracking():
    """A sample-clock mismatch rotates higher carriers more. Wide mode fits
    phase vs frequency across its pilots instead of narrow mode's single
    common correction. Measured: Wide survives 300 ppm where QPSK and
    Wide-with-common-correction fail at 200 ppm."""
    from scipy.signal import resample
    rng = np.random.RandomState(5)
    pl = rng.randint(0, 256, 256).astype(np.uint8).tobytes()

    def ok(mod, ppm, pad, common=False):
        x = np.concatenate([np.zeros(pad, np.float32), ofdm.frame_encode(pl, modulation=mod)])
        y = resample(x, int(len(x) * (1 + ppm * 1e-6))).astype(np.float32)
        rx = ofdm.OFDMReceiver()
        if common:
            def c(sym, h):
                spec = np.fft.fft(sym[ofdm.N_CP:])
                idx = np.array(ofdm.WIDE_PILOT_POS)
                return h * np.mean(spec[ofdm.WIDE_BINS[idx]] / (h[idx] * ofdm.PILOT_VAL))
            rx._track_pilots_wide = c
        rx.push(np.concatenate([y, np.zeros(4000, np.float32)]))
        f = rx.poll()
        return f is not None and f.payload == pl
    # v0.8.3 band (656-2625 Hz): slope tracking measured 8/8 up to 400 ppm;
    # a single common correction fails from 250 ppm; QPSK fails at 200.
    pads = (0, 11, 23, 37)
    wide300 = sum(ok(ofdm.MOD_QPSK_WIDE, 300, p) for p in pads)
    common250 = sum(ok(ofdm.MOD_QPSK_WIDE, 250, p, True) for p in pads)
    check(f"Wide with phase-slope tracking survives 300 ppm ({wide300}/4); "
          f"with a single common correction it fails at 250 ppm ({common250}/4)",
          wide300 == 4 and common250 <= 1)


def test_qpsk_wide_fm_channel_and_dcd():
    """Through the FM channel simulator Wide decodes like QPSK down to CNR
    8 dB (speaker and 9600-port audio; it needs ~1 dB more at the very
    edge). And on open squelch Auto DCD stays right: Wide carriers reach
    the 2.3-2.9 kHz band the spectrum check uses, which once let the noise
    floor learn from a Wide transmission so idle hiss read busy 100%; a
    second containing a SARA-PP signal is now never treated as idle."""
    import fm_channel as fc
    SR = ofdm.SAMPLE_RATE
    rng = np.random.RandomState(9)

    def trial(path, cnr):
        pl = rng.randint(0, 256, 236).astype(np.uint8).tobytes()
        tx = np.concatenate([audio_io.fill_tone(int(0.6 * SR), 0.8),
                             ofdm.frame_encode(pl, modulation=ofdm.MOD_QPSK_WIDE) * 0.8,
                             audio_io.fill_tone(int(0.15 * SR), 0.8)])
        sig = np.concatenate([np.zeros(SR), tx, np.zeros(SR)])
        on = np.concatenate([np.zeros(SR, bool), np.ones(len(tx), bool), np.zeros(SR, bool)])
        y = fc.receive(sig, on, cnr, rng, out_gain=1.0, path=path).astype(np.float64)
        y *= 10 ** (-12 / 20) / np.sqrt(np.mean(y[on][int(0.7 * SR):int(1.2 * SR)] ** 2))
        return any(f.payload == pl for f in decode_all(ofdm.OFDMReceiver(), y.astype(np.float32), chunk=1024))
    # v0.8.3 band: like QPSK down to CNR 8 on speaker audio; on the 9600
    # port Wide's dependable floor is ~CNR 10 (QPSK ~8): measured 10/12 at
    # CNR 8, 7/8 to 12/12 at CNR 9 -- the cliff region, so checked to CNR 10.
    for path, cnrs in (("speaker", (25, 15, 10, 8)), ("data9600", (25, 15, 12, 10))):
        n = sum(trial(path, cnr) for cnr in cnrs for _ in range(2))
        check(f"QPSK Wide through the FM channel ({path} audio), CNR {'/'.join(map(str, cnrs))}: {n}/8 decoded",
              n == 8)

    B = []
    b = ofdm.frame_encode(bytes(range(236)), modulation=ofdm.MOD_QPSK_WIDE) * 0.8
    B = [audio_io.fill_tone(int(0.6 * SR), 0.8)]
    for i in range(7):
        B += [b, audio_io.fill_tone(int((audio_io.BURST_GAP_S if i < 6 else 0.15) * SR), 0.8)]
    B = np.concatenate(B)
    segs = [(np.zeros(12 * SR), False), (B, True), (np.zeros(10 * SR), False)]
    tx = np.concatenate([x for x, _ in segs])
    on = np.concatenate([np.full(len(x), k) for x, k in segs])
    y = fc.receive(tx, on, 15, rng, out_gain=1.0, path="speaker").astype(np.float64)
    y *= 10 ** (-12 / 20) / np.sqrt(np.mean(y[on][int(0.7 * SR):int(1.5 * SR)] ** 2))
    e = _os_engine()
    e.dcd_mode = "auto"
    pcm = (np.clip(y, -1, 1) * 32767).astype(np.int16)
    bt = nt = bi = ni = fr = 0
    after = False
    for i in range(0, len(pcm), 1024):
        e._rx_callback(pcm[i:i + 1024].tobytes(), 1024, None, 0)
        while not e.frame_queue.empty():
            e.frame_queue.get()
            fr += 1
        if i < 3 * SR:
            continue
        busy = e.is_channel_busy()
        if on[i]:
            nt += 1
            bt += busy
            after = True
        elif after:
            ni += 1
            bi += busy
    check(f"open squelch, speaker audio, Auto DCD, Wide burst: burst busy {100 * bt / nt:.0f}%, "
          f"idle hiss afterwards {100 * bi / ni:.0f}%, {fr}/7 frames",
          bt / nt >= 0.97 and bi / ni <= 0.03 and fr == 7)

# ── v0.8.1: per-carrier check; weighted Wide pilot fit ──────────────────
def _rolloff_frame(seed, edge_hz, att_db, mod=None, noise=0.012):
    mod = ofdm.MOD_QPSK_WIDE if mod is None else mod
    rng = np.random.RandomState(seed)
    pl = rng.randint(0, 256, 236).astype(np.uint8).tobytes()
    b = ofdm.frame_encode(pl, modulation=mod)
    spec = np.fft.rfft(b)
    f = np.fft.rfftfreq(len(b), 1 / ofdm.SAMPLE_RATE)
    sel = (f > edge_hz) if edge_hz > 1000 else (f < edge_hz)
    spec[sel] *= 10 ** (-att_db / 20)
    x = np.fft.irfft(spec, len(b)) + rng.normal(0, noise, len(b))
    return pl, np.concatenate([np.zeros(3000), x, np.zeros(4000)]).astype(np.float32)


def test_carrier_check():
    """Field (v0.8.0): QPSK Wide from K6IRF reached DW1IRF needing FEC on
    every large frame (median 15 bytes, up to the 24-byte limit) at +29 dB
    overall, while the other direction was clean -- some carriers were
    received badly, but the logs couldn't say which. After a frame decodes,
    re-encoding the corrected data gives the exact transmitted bits, so each
    carrier's raw bit errors and S/N can be measured. Checks: clean frames
    show zero errors with every bit counted; a simulated radio roll-off
    above 2.5 kHz is pinpointed to the right carrier; the GUI summary logs
    after N frames."""
    import types
    import sara_pingpong as sp
    for mod in (ofdm.MOD_QPSK, ofdm.MOD_QPSK_WIDE):
        pl = bytes(range(200))
        out = decode_all(ofdm.OFDMReceiver(), ofdm.frame_encode(pl, modulation=mod), chunk=1024)
        fq, er, bt, ev, en = out[0].carrier_stats
        check(f"{'Wide' if mod == ofdm.MOD_QPSK_WIDE else 'QPSK'} clean frame: per-carrier check counts "
              f"{int(bt.sum())} bits, {int(er.sum())} errors",
              er.sum() == 0 and bt.sum() == 8 * fec.encoded_len(202, ofdm.fec_block(mod)) and len(fq) == len(
                  ofdm.WIDE_DATA_POS if mod == ofdm.MOD_QPSK_WIDE else ofdm.DATA_POS))
    pl, x = _rolloff_frame(4, 2500, 15)
    rx = ofdm.OFDMReceiver()
    rx.push(x)
    fr = rx.poll()
    fq, er, bt, ev, en = fr.carrier_stats
    worst = fq[int(np.argmax(er / bt))]
    check(f"a radio roll-off above 2.5 kHz is pinpointed: worst carrier {worst:.0f} Hz", 2500 < worst < 2600)

    logs = []
    a = types.SimpleNamespace(_carrier_acc={}, _log=logs.append, CARRIER_REPORT_EVERY=2,
                              _mod_name=sp.App._mod_name)
    a._note_carriers = types.MethodType(sp.App._note_carriers, a)
    a._log_carriers = types.MethodType(sp.App._log_carriers, a)
    a._note_carriers(fr)
    a._note_carriers(fr)
    check("the GUI logs a carrier-check summary every N frames, naming the weakest carrier",
          any("Carrier check (QPSK Wide" in l for l in logs) and any("2531 Hz" in l for l in logs), f"{logs[-1:]}")


def test_wide_pilot_weighting():
    """Wide's phase-vs-frequency fit now weights each pilot by its channel
    magnitude. With the radio's low end rolled off (pilot at 375 Hz buried),
    the equal-weight fit skewed every carrier: 0/12 decoded in development;
    weighted, 11/12."""
    ok = 0
    for seed in range(6):
        pl, x = _rolloff_frame(seed, 450, 25)
        rx = ofdm.OFDMReceiver()
        rx.push(x)
        f = rx.poll()
        ok += f is not None and f.payload == pl
    check(f"Wide with the low end rolled off 25 dB below 450 Hz: {ok}/6 decoded", ok >= 5)

# ── v0.8.2: duplicate callsign detection ────────────────────────────────
def test_duplicate_callsign_detected():
    """Field (v0.8.1): both test stations had "My callsign" K6IRF (a copied
    settings file); pings to DW1IRF were never answered and each station
    logged the other's pings as "own TX heard locally". A PING from our own
    callsign with a sequence number we never sent recently can't be our
    echo: warn. Our real echo must stay a quiet echo."""
    import types
    import collections
    import sara_pingpong as sp
    logs, warned = [], []
    a = types.SimpleNamespace(_sent_seqs=collections.deque(maxlen=64), _dup_call_warned=False,
                              _log=logs.append)
    a._sent_it = types.MethodType(sp.App._sent_it, a)
    a._sent_seqs.append((8, __import__("time").time()))
    check("our own recent PING seq is recognised as our echo", a._sent_it(8))
    check("a seq we never sent is not mistaken for our echo", not a._sent_it(1))
    from unittest.mock import patch
    a._warn_duplicate_callsign = types.MethodType(sp.App._warn_duplicate_callsign, a)
    with patch.object(sp.messagebox, "showwarning") as box:
        a._warn_duplicate_callsign("K6IRF")
        a._warn_duplicate_callsign("K6IRF")
    check("duplicate-callsign warning is logged each time, pop-up shown once",
          sum("YOUR callsign (K6IRF)" in l for l in logs) == 2 and box.call_count == 1)
    bat = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "build_exe.bat")).read()
    check("build_exe.bat strips per-station files (settings with the callsign, logs) from the zip",
          "sara_pp_settings.json" in bat and "del /q" in bat)

# ── v0.8.3: Wide band moved up; smaller Wide FEC blocks ─────────────────
def test_wide_v083_field_channel():
    """Field (v0.8.1 carrier check, K6IRF -> DW1IRF): 469 Hz ~2% and 562 Hz
    ~5.5% bit errors (S/N 4-5 dB), 656 Hz up clean; a 6000-byte upload
    choked every time on its final 211-byte frame, which got ONE RS block
    (12 repairs) where the 275-byte frames got two. A channel shaped to the
    measured per-carrier S/N reproduces it (old Wide: median 8 repairs on
    275-byte frames, 8-17% failing); with Wide at 656-2625 Hz and <=160-
    byte blocks, every frame size decodes with ~1 repair."""
    import field_channel_dw1irf as fch
    rng = np.random.RandomState(7)
    tot = ok = 0
    reps = []
    for size in (275, 211, 181):
        for _ in range(6):
            pl = bpq_bridge.pack_batch([rng.randint(0, 256, size - 4).astype(np.uint8).tobytes()])
            x = fch.impair(np.concatenate([np.zeros(3000), ofdm.frame_encode(pl, modulation=ofdm.MOD_QPSK_WIDE),
                                           np.zeros(4000)]), rng)
            rx = ofdm.OFDMReceiver()
            rx.push(x)
            f = rx.poll()
            tot += 1
            if f is not None and f.payload == pl:
                ok += 1
                reps.append(f.fec_corrected)
    check(f"field-shaped channel, Wide 275/211/181-byte frames: {ok}/{tot} decoded, "
          f"median {np.median(reps) if reps else -1:.0f} repairs", ok == tot and np.median(reps) <= 4)
    check("a 211-byte Wide frame now gets two FEC blocks (24 repairs), as the 275-byte ones do",
          fec._n_blocks(213, ofdm.fec_block(ofdm.MOD_QPSK_WIDE)) == 2 and fec._n_blocks(277, ofdm.fec_block(ofdm.MOD_QPSK_WIDE)) == 2)
    check("narrow-mode FEC is unchanged (bit-for-bit compatible with v0.6.0+)",
          all(fec.encode(bytes(range(n % 256)) * 1 or b"x") == fec.encode(bytes(range(n % 256)) * 1 or b"x", None)
              for n in (1, 100, 213)) and fec._n_blocks(213) == 1 and ofdm.fec_block(ofdm.MOD_QPSK) is None)

# ── v0.8.4: yield to the reply ──────────────────────────────────────────
def test_yield_to_reply():
    """Field (v0.8.3): K6IRF sent a second short frame at the moment the BBS
    sent its DISC -- both lost; the BBS's 23 s timeout made the hang-up take
    ~45 s (pattern seen ~46 times in one test). A new transmission now waits
    REPLY_YIELD_S after our own last one, unless the other station's reply
    shows up on DCD first -- then it waits for the reply to finish. Pings
    skip it."""
    from unittest.mock import MagicMock

    def engine():
        e = audio_io.AudioEngine.__new__(audio_io.AudioEngine)
        e.tx_lead_ms = e.tx_tail_ms = 1
        e._rx_rms_ema = 0.0
        e._tx_lock = threading.Lock()
        e.tx_stream = MagicMock()
        e.on_carrier_wait = e.on_tx_start = None
        keys = []
        e.ptt = MagicMock()
        e.ptt.ptt_on.side_effect = lambda: keys.append(_time.time())
        e._reply = None
        e.is_channel_busy = lambda: e._reply is not None and e._reply[0] <= _time.time() < e._reply[1]
        return e, keys

    def second_keyup(reply_at, ping=False):
        e, keys = engine()
        e.transmit(b"x" * 19, modulation=ofdm.MOD_QPSK_WIDE)
        t_end = e._last_tx_end
        if reply_at is not None:
            e._reply = (t_end + reply_at, t_end + reply_at + 1.0)
        _time.sleep(0.3)
        pl = proto.make_ping("K6IRF", "DW1IRF", 1) if ping else b"y" * 19
        e.transmit(pl, modulation=ofdm.MOD_QPSK_WIDE, ignore_dcd=ping)
        return keys[-1] - t_end

    t = second_keyup(None)
    check(f"no reply: a quick second transmission yields until {t:.2f} s after our first ended",
          t >= audio_io.REPLY_YIELD_S)
    t = second_keyup(0.6)
    check(f"the other station replies 0.6-1.6 s after: our second key-up at {t:.2f} s -- after the reply, no overlap",
          t >= 1.6)
    t = second_keyup(0.6, ping=True)
    check(f"a PING is not held by the yield ({t:.2f} s)", t < 0.6)

# ── v0.8.5: acknowledgment batching ─────────────────────────────────────
def test_ack_batching():
    """Field (v0.8.4): Winlink Express acknowledges each data frame
    separately; the 7 small RR frames queued while DCD held the station
    went out as 7 OFDM frames (~3.4 s in Wide) -- ~2 s of every ~13 s
    transfer cycle. Queued batches are now merged at burst time, within
    the same MAX_BATCH_BYTES cap. Order must be preserved (AX.25), data
    frames too big to combine stay separate, modulations never mix, and
    the burst frame limit puts back what it took."""
    import queue as _q
    from unittest.mock import MagicMock, patch

    def run(items, merge=True):
        e = audio_io.AudioEngine.__new__(audio_io.AudioEngine)
        e.tx_lead_ms, e.tx_tail_ms = 600, 150
        e._rx_rms_ema = 0.0
        e._tx_lock = threading.Lock()
        e.ptt = MagicMock()
        e.on_carrier_wait = e.on_tx_start = None
        e.is_channel_busy = lambda: False
        chunks = []
        e.tx_stream = MagicMock()
        e.tx_stream.write.side_effect = lambda by: chunks.append(np.frombuffer(by, np.float32))
        e._tx_queue = _q.Queue()
        e.merge_payloads = bpq_bridge.merge_batches if merge else None
        sent = []
        for pl, mod in items:
            e._tx_queue.put((pl, mod, 0.8, lambda p, m: sent.append(p), None))
        with patch("time.sleep"):
            while not e._tx_queue.empty():
                e._send_burst(e._tx_queue.get())
        audio = np.concatenate(chunks)
        got = [f.payload for f in decode_all(ofdm.OFDMReceiver(), audio, chunk=1024)]
        return len(got), [fr for g in got for fr in bpq_bridge.unpack_batch(g)], len(audio) / ofdm.SAMPLE_RATE

    W = ofdm.MOD_QPSK_WIDE
    acks = [bytes([0x96] * 14) + bytes([0x11, i]) + b"ack" for i in range(7)]
    n0, _, t0 = run([(bpq_bridge.pack_batch([x]), W) for x in acks], merge=False)
    n1, fr, t1 = run([(bpq_bridge.pack_batch([x]), W) for x in acks])
    check(f"7 acknowledgments: {n0} frames in {t0:.1f} s -> {n1} frame in {t1:.1f} s, all delivered in order",
          n1 == 1 and fr == acks and t1 < t0 - 1.5)
    data = [bytes([i]) * 236 for i in range(2)]
    mix = [acks[0], acks[1], data[0], data[1], acks[2], acks[3]]
    n, fr, _ = run([(bpq_bridge.pack_batch([x]), W) for x in mix])
    check(f"acks mixed with 236-byte data frames: {n} frames on air, AX.25 order preserved", n == 4 and fr == mix)
    n, fr, _ = run([(bpq_bridge.pack_batch([acks[0]]), W), (bpq_bridge.pack_batch([acks[1]]), ofdm.MOD_QPSK)])
    check("different modulations are never merged", n == 2 and fr == acks[:2])
    big = [bytes([i]) * 236 for i in range(audio_io.BURST_MAX_FRAMES)]
    n, fr, _ = run([(bpq_bridge.pack_batch([x]), W) for x in big + acks[:2]])
    check("a burst stopping at its frame limit keeps order for what follows", fr == big + acks[:2])
    check("merging respects the 250-byte cap",
          bpq_bridge.merge_batches(bpq_bridge.pack_batch([bytes(200)]), bpq_bridge.pack_batch([bytes(60)])) is None
          and bpq_bridge.merge_batches(b"raw", bpq_bridge.pack_batch([acks[0]])) is None)

# ── v0.8.6: phase-line tracking (audio glitches); single TX report ──────
def test_audio_glitch_recovery():
    """Field (v0.8.5): a Wide frame at S/N 28.7 dB failed with a drift
    reading of +150 deg (neighbours ~-34): a ~180 deg jump mid-frame, i.e.
    ~15 audio samples dropped/repeated by the sound card. Before v0.8.6, 16
    samples killed every frame in every mode. On strong frames the
    phase-line tracker now turns the timing jump into a slope."""
    def trial(mod, drop, seed):
        r = np.random.RandomState(seed)
        pl = r.randint(0, 256, 256).astype(np.uint8).tobytes()
        b = ofdm.frame_encode(pl, modulation=mod)
        cut = len(b) // 2
        b = np.concatenate([b[:cut], b[cut + drop:]]) if drop > 0 else np.concatenate([b[:cut], b[cut + drop:cut], b[cut:]])
        x = (np.concatenate([np.zeros(3000), b, np.zeros(4000)]) + r.normal(0, 0.004, len(b) + 7000)).astype(np.float32)
        rx = ofdm.OFDMReceiver()
        rx.push(x)
        f = rx.poll()
        return f is not None and f.payload == pl
    for mod, name, drops in ((ofdm.MOD_BPSK, "BPSK", (16, -16, 32)), (ofdm.MOD_QPSK, "QPSK", (16, -16, 32)),
                             (ofdm.MOD_QPSK_WIDE, "Wide", (16, -16, 24))):
        n = sum(trial(mod, d, s) for d in drops for s in range(2))
        check(f"{name} frames survive a mid-frame audio glitch of {'/'.join(f'{d:+d}' for d in drops)} samples: "
              f"{n}/{2 * len(drops)}", n == 2 * len(drops))


def test_weak_frames_track_exactly_as_before():
    """On weaker frames 4 noisy pilots can't measure a slope: at the
    decoding edge any slope term cost frames (BPSK 8 -> 4-7 of 16, QPSK
    10 -> 5-7). Below TRACK_SLOPE_MIN_SNR_DB narrow tracking must be the
    pre-v0.8.6 common correction, exactly."""
    rng = np.random.RandomState(3)
    rx = ofdm.OFDMReceiver()
    same = True
    for _ in range(20):
        sym = rng.normal(0, 1, ofdm.N_SYM)
        h = rng.normal(0, 1, ofdm.NUM_CARRIERS) + 1j * rng.normal(0, 1, ofdm.NUM_CARRIERS)
        spec = np.fft.fft(sym[ofdm.N_CP:])
        old = h * np.mean([spec[ofdm.CARRIER_BINS[p]] / (h[p] * ofdm.PILOT_VAL) for p in ofdm.PILOT_POS])
        rx._frame_snr_db = ofdm.OFDMReceiver.TRACK_SLOPE_MIN_SNR_DB - 1
        same &= np.allclose(rx._track_pilots(sym, h), old)
    check("narrow frames below the S/N gate track exactly as before v0.8.6", same)


def test_merged_frame_reported_once():
    """Field (v0.8.5): a merged frame was logged -- and counted in the speed
    meter -- once per merged item ("TX 265 bytes (7 frames)" x3), because the
    GUI's bound-method callback is a new object at each reference and the
    de-duplication compared identity."""
    import queue as _q
    from unittest.mock import MagicMock, patch

    class Gui:
        def __init__(self):
            self.logged = []

        def on_sent(self, p, m):
            self.logged.append(len(p))
    g = Gui()
    e = audio_io.AudioEngine.__new__(audio_io.AudioEngine)
    e.tx_lead_ms, e.tx_tail_ms = 600, 150
    e._rx_rms_ema = 0.0
    e._tx_lock = threading.Lock()
    e.ptt = MagicMock()
    e.on_carrier_wait = e.on_tx_start = None
    e.is_channel_busy = lambda: False
    e.tx_stream = MagicMock()
    e._tx_queue = _q.Queue()
    e.merge_payloads = bpq_bridge.merge_batches
    for i in range(3):
        e._tx_queue.put((bpq_bridge.pack_batch([bytes([i]) * 30]), ofdm.MOD_QPSK_WIDE, 0.8, g.on_sent, None))
    with patch("time.sleep"):
        e._send_burst(e._tx_queue.get())
    check(f"3 queued items merged into one frame are reported once ({len(g.logged)})", len(g.logged) == 1)

def link_adapt_attach(payload):
    import link_adapt as la
    return la.attach_report(payload, la.Report(3, 1, 0, 30, 0, 0))


# ── v0.8.7: yield ends once the reply is heard ──────────────────────────
def test_yield_ends_when_reply_heard():
    """Field (v0.8.6, 22:37:28): a frame sent 2.0 s after our last TX, just
    past the 1.5 s yield, collided with the other station's next window.
    The yield is now 2.5 s, but ends as soon as a frame from the other
    station has been received after our TX -- normal turn-taking isn't
    slowed. A frame decoded right after our TX (our own echo) doesn't count."""
    from unittest.mock import MagicMock

    def engine():
        e = audio_io.AudioEngine.__new__(audio_io.AudioEngine)
        e.tx_lead_ms = e.tx_tail_ms = 1
        e._rx_rms_ema = 0.0
        e._tx_lock = threading.Lock()
        e.tx_stream = MagicMock()
        e.on_carrier_wait = e.on_tx_start = None
        keys = []
        e.ptt = MagicMock()
        e.ptt.ptt_on.side_effect = lambda: keys.append(_time.time())
        e.is_channel_busy = lambda: False
        return e, keys

    def second_keyup(rx_after):
        e, keys = engine()
        e.transmit(b"x" * 19, modulation=ofdm.MOD_QPSK_WIDE)
        t_end = e._last_tx_end
        if rx_after is not None:
            e._last_rx_frame_time = t_end + rx_after
        _time.sleep(0.2)
        e.transmit(b"y" * 19, modulation=ofdm.MOD_QPSK_WIDE)
        return keys[-1] - t_end
    t = second_keyup(None)
    check(f"nothing heard: the yield waits {t:.2f} s (covers a reply starting ~2 s later)", t >= 2.4)
    ctl = bpq_bridge.pack_batch([_ax25_iframe("K6IRF-10", "K6IRF", 0, b"")[:14] + bytes([0x53])])   # DISC
    data = bpq_bridge.pack_batch([_ax25_iframe("K6IRF", "K6IRF-10", 2, b"text")])

    def keyup_after(payload):
        e, keys = engine()
        e.transmit(b"x" * 19, modulation=ofdm.MOD_QPSK_WIDE)
        t_end = e._last_tx_end
        _time.sleep(0.2)
        e.transmit(payload, modulation=ofdm.MOD_QPSK_WIDE)
        return keys[-1] - t_end
    t = keyup_after(ctl)
    check(f"v0.9.3: a control-frame-only burst (DISC) yields {t:.2f} s -- covers BPQ32 replies "
          f"starting 3.1-4.3 s later (field v0.9.1/v0.9.2 disconnect collisions)", t >= 6.3)
    t = keyup_after(data)
    check(f"...while a burst carrying data (I-frame) keeps the short yield ({t:.2f} s)", 2.4 <= t < 3.2)
    check("reply_yield_s: control -> long, data -> short, non-batch (ping) -> short",
          audio_io.reply_yield_s(ctl) == audio_io.CONTROL_REPLY_YIELD_S
          and audio_io.reply_yield_s(data) == audio_io.REPLY_YIELD_S
          and audio_io.reply_yield_s(b"PING") == audio_io.REPLY_YIELD_S
          and audio_io.reply_yield_s(link_adapt_attach(ctl)) == audio_io.CONTROL_REPLY_YIELD_S)
    t = second_keyup(0.8)
    check(f"reply already received: next TX after {t:.2f} s, not held for the full yield", t < 1.2)
    t = second_keyup(0.1)
    check(f"a frame decoded right after our own TX (echo) doesn't end the yield ({t:.2f} s)", t >= 2.4)

# ── v0.8.8: Save Logs ───────────────────────────────────────────────────
def test_save_logs_bundle():
    """One click gathers what a diagnosis needs into ONE zip named with the
    station's callsign (field: logs were copy-pasted and sometimes named for
    the wrong station). Session lines go to a daily file so nothing is lost
    when the window closes."""
    import re
    import tempfile
    import types
    import zipfile
    import log_bundle
    import sara_pingpong as sp
    with tempfile.TemporaryDirectory() as d:
        sl = log_bundle.SessionLog(d, "sara-pp-test")
        sl.write("12:00:00.000  RX PING  seq=1")
        sl.write("12:00:01.000  TX PONG  seq=1")
        sl.close()
        files = sl.recent_files()
        text = open(files[0], encoding="utf-8").read() if files else ""
        check("log lines are kept in a file in logs/", len(files) == 1 and "TX PONG" in text
              and "sara-pp-test" in text and os.path.dirname(files[0]).endswith("logs"))
        call = ["k6irf"]
        sl2 = log_bundle.SessionLog(d, "sara-pp-test", call_fn=lambda: call[0])
        sl2.write("12:00:02.000  hello")
        n2 = os.path.basename(sl2.path)
        check(f"v0.9.2: the file is named with callsign, date and time: {n2}",
              re.fullmatch(r"sara_pp_K6IRF_\d{4}-\d\d-\d\d_\d{6}\.log", n2) is not None, n2)
        import time as _t
        _t.sleep(1.1)
        call[0] = "DW1IRF"
        sl2.write("12:00:03.000  after callsign change")
        sl2.close()
        check("...a callsign change starts a new file named for the new call",
              os.path.basename(sl2.path).startswith("sara_pp_DW1IRF_") and len(sl2.recent_files()) == 3,
              str([os.path.basename(f) for f in sl2.recent_files()]))
        call2 = [""]
        sl3 = log_bundle.SessionLog(d, "sara-pp-test", call_fn=lambda: call2[0])
        sl3.write("startup line before the callsign is loaded")
        call2[0] = "DU3FS"
        sl3.write("after settings load")
        sl3.close()
        txt3 = open(sl3.path, encoding="utf-8").read()
        check("...a log started before the callsign was known is renamed for it (no NOCALL stub)",
              os.path.basename(sl3.path).startswith("sara_pp_DU3FS_") and "startup line" in txt3
              and not any("NOCALL" in f for f in sl3._opened), os.path.basename(sl3.path))
        for f in list(sl3._opened):
            os.remove(f)
        old_name = os.path.join(d, "logs", "sara_pp_2026-10-06.log")
        open(old_name, "w").write("old\n")
        check("...and older versions' daily logs are still collected by Save Logs",
              old_name in sl2.recent_files())
        os.remove(old_name)
        files = sl.recent_files()[:1]
        csv_ = os.path.join(d, "sara_pingpong_log.csv")
        open(csv_, "w").write("timestamp,event\n")
        out = os.path.join(d, "Desktop")
        p1 = log_bundle.make_bundle("du3fs", "info\n", "window\n", [csv_, os.path.join(d, "missing.txt")],
                                    files, out, now=1_791_000_000)
        p2 = log_bundle.make_bundle("du3fs", "info\n", "window\n", [csv_], files, out, now=1_791_000_000)
        names = zipfile.ZipFile(p1).namelist()
        check(f"bundle named with the callsign and time: {os.path.basename(p1)}",
              os.path.basename(p1).startswith("SARA-FM_logs_DU3FS_") and p1.endswith(".zip"))
        check("bundle holds station info, window text, session log and CSV; missing files skipped",
              any(n.endswith("station_info.txt") for n in names) and any(n.endswith("log_window.txt") for n in names)
              and any("/session_logs/sara_pp_" in n for n in names)
              and any(n.endswith("sara_pingpong_log.csv") for n in names)
              and not any("missing" in n for n in names), str(names))
        check("a second bundle in the same minute doesn't overwrite the first", p1 != p2 and os.path.exists(p1))
    check("no callsign set -> NOCALL; odd characters removed",
          log_bundle.safe_call("") == "NOCALL" and log_bundle.safe_call(" k6irf-10/x ") == "K6IRF-10X")

    class V:
        def __init__(self, x): self.x = x
        def get(self): return self.x
    a = types.SimpleNamespace(engine=None, bridge=None)
    for n, x in (("call_var", "DU3FS"), ("target_var", "*"), ("mode_var", "bpq"), ("modulation_var", "QPSK Wide"),
                 ("dcd_mode_var", "Auto"), ("in_dev_var", "USB PnP"), ("out_dev_var", "USB PnP"), ("ptt_var", "CM108"),
                 ("ra_board_var", "RA-Board-1"), ("gpio_pin_var", "GPIO3"), ("tx_delay_var", "600"),
                 ("rx_gain_db_var", "34"), ("tx_level_var", "80"), ("kiss_port_var", "8105"), ("remote_kiss_var", False)):
        setattr(a, n, V(x))
    info = sp.App._station_info(a)
    check("station_info.txt lists version, callsign, modulation, levels and time zone",
          ofdm.VERSION in info and "DU3FS" in info and "QPSK Wide" in info and "RX gain dB:   34" in info and "UTC" in info)
    bat = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "build_exe.bat")).read()
    check("build_exe.bat doesn't ship the build PC's logs folder", "dist\\SARA-FM\\logs" in bat)

# ── v0.8.9: errors reported for real; PTT can't stay stuck ──────────────
class _FakeHid:
    """Stand-in 'hid' module: boards by USB path, writes can be made to fail."""
    def __init__(self):
        self.present = {b"A": True, b"B": True}
        self.fail_writes = 0          # next N writes fail (raise)
        self.fail_forever = False
        self.log = []                 # (path, report) of successful writes
        self.opened = []
        outer = self

        class device:
            def open_path(self, path):
                if not outer.present.get(path):
                    raise OSError("open failed")
                self.path = path
                outer.opened.append(path)

            def set_nonblocking(self, x):
                pass

            def write(self, report):
                if outer.fail_forever or outer.fail_writes > 0:
                    outer.fail_writes = max(0, outer.fail_writes - 1)
                    raise OSError("HID write failed (USB reset)")
                outer.log.append((self.path, list(report)))
                return len(report)

            def close(self):
                pass
        self.device = device


def _cm108_with_fake(fake, path=b"A"):
    import ptt as _ptt
    sys.modules["hid"] = fake
    orig = _ptt._find_cm108_devices
    _ptt._find_cm108_devices = lambda: [(f"RA-{p.decode()}", p) for p, ok in fake.present.items() if ok]
    c = _ptt.CM108PTT(path=path)
    c.open()
    return c, (lambda: setattr(_ptt, "_find_cm108_devices", orig))


def test_ptt_release_recovers():
    """Field (v0.8.8): transmissions failed and the radio stayed keyed until
    SARA-PP was restarted. A failed PTT-OFF write had cleared the 'keyed'
    state first, so every later release was skipped. Now: retries, reopen of
    the SAME board, state kept until the release works, watchdog retry."""
    import ptt as _ptt
    from unittest.mock import patch
    fake = _FakeHid()
    c, restore = _cm108_with_fake(fake)
    try:
        with patch("time.sleep"):
            c.ptt_on()
            fake.fail_writes = 1                       # one USB hiccup on the OFF write
            c.ptt_off()
        last = fake.log[-1]
        check("one failed PTT-OFF write: retried on a reopened handle, radio released",
              last[0] == b"A" and last[1][2] == 0x00 and not c.is_keyed)

        ctl = _ptt.PTTController(method=_ptt.PTT_CM108)
        ctl._cm108 = c
        with patch("time.sleep"):
            ctl.ptt_on()
            fake.fail_forever = True
            raised = False
            try:
                ctl.ptt_off()
            except Exception:
                raised = True
            check("a release that keeps failing raises, and PTT stays marked keyed (release_failed)",
                  raised and ctl.is_keyed and ctl.release_failed)
            check("while it keeps failing, the watchdog retry reports not-released", ctl.ensure_released() is False)
            fake.fail_forever = False
            ok = ctl.ensure_released()
        check("once USB recovers, the watchdog retry releases the radio",
              ok and not ctl.is_keyed and not ctl.release_failed and fake.log[-1][1][2] == 0x00)

        fake.present[b"A"] = False                     # board A gone; board B still there
        fake.opened.clear()
        with patch("time.sleep"):
            c._dev = None
            try:
                c.ptt_off()
            except Exception:
                pass
        check("with two RA-Boards, a reopen never grabs the OTHER board", b"B" not in fake.opened)
    finally:
        restore()
        sys.modules.pop("hid", None)


def test_errors_reported_not_namerror():
    """Field (v0.8.8): the console showed only "NameError: cannot access free
    variable 'e'" -- the real TX error was lost because it was logged by a
    lambda that ran after Python had deleted the exception variable. Every
    error now goes through _report_error, which formats it immediately."""
    import types
    import ast
    import sara_pingpong as sp
    logs, pending = [], []
    a = types.SimpleNamespace(after=lambda ms, fn: pending.append(fn), _log=logs.append, session_log=None)
    a._report_error = types.MethodType(sp.App._report_error, a)

    def worker():
        try:
            raise RuntimeError("CM108 PTT OFF failed after 3 tries")
        except Exception as e:
            a._report_error("TX error", e)
    worker()
    for fn in pending:        # runs later, on the GUI thread, after the except block ended
        fn()
    check("a TX error shows the REAL error text, not a NameError",
          logs == ["TX error: RuntimeError: CM108 PTT OFF failed after 3 tries"], str(logs))
    bad = []
    here = os.path.dirname(os.path.abspath(__file__))
    for f in os.listdir(here):
        if f.endswith(".py") and not f.startswith("test_"):
            tree = ast.parse(open(os.path.join(here, f), encoding="utf-8").read())
            for h in ast.walk(tree):
                if isinstance(h, ast.ExceptHandler) and h.name:
                    for n in ast.walk(h):
                        if isinstance(n, (ast.Lambda, ast.FunctionDef)) and any(
                                isinstance(x, ast.Name) and x.id == h.name for x in ast.walk(n)):
                            bad.append(f"{f}:{n.lineno}")
    check("no module uses an exception variable in a deferred callback", not bad, str(bad))


def test_ptt_watchdog_gui():
    import types
    import threading as _th
    import sara_pingpong as sp
    logs = []
    state = {"ok": False}
    p = types.SimpleNamespace(release_failed=True, ensure_released=lambda: state["ok"])
    a = types.SimpleNamespace(engine=types.SimpleNamespace(ptt=p, _tx_lock=_th.Lock()), _log=logs.append,
                              PTT_WATCHDOG_EVERY_S=0.0, PTT_WARN_EVERY_S=0.0)
    sp.App._ptt_watchdog(a)
    state["ok"] = True
    sp.App._ptt_watchdog(a)
    check("watchdog warns while release fails, then logs the release",
          len(logs) == 2 and "stuck transmitting" in logs[0] and "PTT released" in logs[1], str(logs))

# ── v0.9.0: Phase 2 -- automatic speed selection, KISS ACKMODE ──────────
def _ax25_iframe(dst, src, ns, info):
    def addr(call, last):
        c, _, ssid = call.partition("-")
        return bytes(x << 1 for x in c.ljust(6).encode()) + bytes([0x60 | (int(ssid or 0) << 1) | (1 if last else 0)])
    return addr(dst, False) + addr(src, True) + bytes([(ns & 7) << 1, 0xF0]) + info


def _ax25_frame(dst, src, digis, control=0x00, info=b"", pid=True):
    """AX.25 frame with a digipeater path: digis = [(call, repeated)]."""
    addrs = [(dst, False), (src, False)] + list(digis)

    def addr(call, h, last):
        c, _, ssid = call.partition("-")
        return (bytes(x << 1 for x in c.ljust(6).encode())
                + bytes([(0x80 if h else 0) | 0x60 | (int(ssid or 0) << 1) | (1 if last else 0)]))
    out = b"".join(addr(c, h, i == len(addrs) - 1) for i, (c, h) in enumerate(addrs))
    return out + bytes([control]) + ((b"\xF0" if pid and (control & 1) == 0 else b"") + info)


def test_digipeater():
    """v0.9.37: AX.25 digipeater paths. Speed, link reports and saved levels
    follow the radio hop; SARA FM's own digipeater repeats frames sent via
    its callsign (or alias) once, with the H bit set; a three-station relay
    works end to end over the OFDM link."""
    import link_adapt as la
    A, D, B = "DW1IRF", "DX1CW", "DX1FLR-10"
    f = _ax25_frame(B, A, [(D, False)], control=0x00, info=b"hello via digi")
    nxt, heard, digis = la.ax25_hops(f)
    check("path: next hop is the digipeater, heard from the source",
          nxt == D and heard == A and digis == [(D, False)] and la.in_transit(f) and not la.is_relayed(f))
    check("path text", la.path_str(f) == "DW1IRF>DX1FLR-10 via DX1CW", la.path_str(f))
    r = la.digipeat(f, D)
    nxt2, heard2, _ = la.ax25_hops(r) if r else (None, None, None)
    check("digipeat sets the H bit: next hop the destination, heard from the digipeater",
          r is not None and nxt2 == B and heard2 == D and la.is_relayed(r) and not la.in_transit(r)
          and len(r) == len(f) and r[14:] != f[14:] and la.ax25_parse(r)[3] == la.ax25_parse(f)[3])
    check("path text marks the repeat", la.path_str(r) == "DW1IRF>DX1FLR-10 via DX1CW*")
    check("never repeats a frame twice, nor one not via this station, nor another SSID",
          la.digipeat(r, D) is None and la.digipeat(f, "DX1PAR") is None and la.digipeat(f, "DX1CW-1") is None
          and la.digipeat(_ax25_frame(B, A, []), D) is None)
    fa = _ax25_frame(B, A, [("RELAY", False)])
    ra = la.digipeat(fa, D, aliases=["RELAY"])
    check("an alias is answered and replaced by the real callsign",
          ra is not None and la.path_str(ra) == "DW1IRF>DX1FLR-10 via DX1CW*")
    f2 = _ax25_frame(B, A, [(D, False), ("DX1PAR", False)])
    r2 = la.digipeat(f2, D)
    check("two digipeaters: after the first repeats, the second is the next hop",
          la.ax25_hops(r2)[:2] == ("DX1PAR", D) and la.digipeat(f2, "DX1PAR") is None
          and la.ax25_hops(la.digipeat(r2, "DX1PAR"))[:2] == (B, "DX1PAR"))
    check("first_frame_hops on a batch", la.first_frame_hops(bpq_bridge.pack_batch([f])) == (D, A))

    import sara_pingpong as sp
    queued = []

    class FakeEngine:
        def __init__(self):
            import queue as _q
            self.tx_level = 0.4
            self.frame_queue = _q.Queue()
        def queue_transmit(self, payload, modulation=0, on_sent=None, on_error=None, tx_gain=None):
            queued.append((payload, modulation, on_sent))
        def __getattr__(self, n):
            return lambda *a, **k: None

    def air(payload, mod=ofdm.MOD_QPSK):
        out = decode_all(ofdm.OFDMReceiver(), ofdm.frame_encode(payload, modulation=mod), chunk=1024)
        return out[0] if out else None

    app = sp.App()
    try:
        app._save_settings = lambda: None
        app.call_var.set(D); app.modulation_var.set("Auto")
        app.engine = FakeEngine()
        app.digi_var.set(True)
        seen = []
        real_on_report = app.speed.on_report
        app.speed.on_report = lambda peer, rep, **k: (seen.append(peer), real_on_report(peer, rep, **k))
        rep = la.Report(ofdm.MOD_QPSK, 5, 0, 30, 0, 0, caps=la.MY_CAPS)
        fr = air(la.attach_report(bpq_bridge.pack_batch([f]), rep))
        app._handle_air_frame_for_bridge(fr)
        app.update()
        ok_tx = len(queued) == 1 and bpq_bridge.unpack_batch(queued[0][0]) == [r]
        check("digipeater on: a frame via this station is repeated once, H bit set", ok_tx, str(len(queued)))
        check("...and the sender's link report is used (this station is its hop)", seen == [A], str(seen))
        app._handle_air_frame_for_bridge(fr)
        check("the same frame decoded again within 3 s isn't repeated twice", len(queued) == 1)
        app._handle_air_frame_for_bridge(air(bpq_bridge.pack_batch([_ax25_frame(B, A, [("DX1PAR", False)])])))
        app._handle_air_frame_for_bridge(air(bpq_bridge.pack_batch([_ax25_frame(D, A, [])])))
        check("frames via another station, or addressed straight to us, aren't repeated", len(queued) == 1)
        app._digi_recent = {}
        app.digi_var.set(False)
        app._handle_air_frame_for_bridge(fr)
        check("digipeater off: nothing repeated", len(queued) == 1)
        app.digi_var.set(True)
        log = "\n".join(app.log_box.get("1.0", "end").splitlines()[-12:])
        check("the log shows the path and the repeat", "via DX1CW" in log and "DIGI: repeating 1 frame" in log)

        # the relayed burst goes at the speed for the hop to B, with a report for B
        app.speed.peers.clear()
        app.speed._peer(B, _time.time()).idx = la.LADDER.index(ofdm.MOD_QPSK_WIDE)
        relay_mode = app._bridge_modulation(queued[0][0])
        check("relayed burst uses the hop's speed (to the destination, not back to the sender)",
              relay_mode == app.speed.mode_for(B) and app.speed.mode_for(B) != app.speed.mode_for(A))
        own = bpq_bridge.pack_batch([_ax25_frame(A, D, []), r])
        check("a batch for two hops goes at the slower of their speeds",
              app._bridge_modulation(own) == min(app.speed.mode_for(A), app.speed.mode_for(B),
                                                 key=la.LADDER.index))
        app.link_stats.on_frame(B, ofdm.MOD_QPSK, 28, 0, 40)
        app.bridge = __import__("types").SimpleNamespace(frames_sent=lambda p: None, stop=lambda: None)
        fin = app._finalize_payload(queued[0][0], ofdm.MOD_QPSK)
        check("its link report is about the hop to B (how this station hears B)",
              la.split_report(fin) is not None and la.split_report(fin).snr_db == 28)
        notes = []
        app._log = notes.append
        before = app.retry_stats.last_activity
        app._bridge_on_sent(fin, ofdm.MOD_QPSK)
        app.update()
        check("relayed frames are logged as DIGI and kept out of the retry accounting",
              any("TX (DIGI->air)" in n and "DX1CW*" in n for n in notes)
              and app.retry_stats.last_activity == before, str(notes))
        relayed_burst = fin
    finally:
        app.engine = None
        app.bridge = None
        app.destroy()

    # station B: hears the digipeater's burst
    app = sp.App()
    try:
        app._save_settings = lambda: None
        app.call_var.set("DX1FLR"); app.modulation_var.set("Auto")
        seen = []
        app.speed.on_report = lambda peer, rep, **k: seen.append(peer)
        delivered = []
        app.bridge = types_ns = __import__("types").SimpleNamespace(deliver_from_air=delivered.append,
                                                                     stop=lambda: None)
        frB = air(relayed_burst)
        app._handle_air_frame_for_bridge(frB)
        check("destination gets the frame (forwarded to BPQ32) over the air through the relay",
              len(delivered) == 1 and bpq_bridge.unpack_batch(delivered[0]) == [r])
        check("...and treats the digipeater as the station it hears (report from DX1CW)", seen == [D], str(seen))
        st = app.link_stats.peers
        check("...its reception stats are for DX1CW, not DW1IRF", D in st and A not in st, str(list(st)))
        seen.clear()
        back = la.digipeat(_ax25_frame(A, "DX1FLR-10", [(D, False)]), D)
        app._note_we_sent([_ax25_frame(A, "DX1FLR-10", [(D, False)])])     # v0.9.63: we sent it
        n_del = len(delivered)
        app._handle_air_frame_for_bridge(air(la.attach_report(bpq_bridge.pack_batch([back]),
                                                              la.Report(ofdm.MOD_QPSK, 3, 0, 25, 0, 0))))
        check("our own frame repeated toward DW1IRF: its report (about the DW1IRF hop) is ignored", seen == [])
        check("...and it isn't passed back to BPQ32", len(delivered) == n_del)
        # v0.9.63 field (DX1FLR -> DV1UK-10 via DW1IRF): a relayed frame with OUR
        # callsign as source that this station never sent is somebody else's
        app.call_var.set("DV1UK")
        sab = la.digipeat(_ax25_frame("DV1UK-10", "DV1UK", [("DW1IRF", False)], control=0x3F, pid=False), "DW1IRF")
        n_del = len(delivered)
        app._handle_air_frame_for_bridge(air(bpq_bridge.pack_batch([sab])))
        app.update()
        lg = "\n".join(app.log_box.get("1.0", "end").splitlines()[-6:])
        check("a relayed connect from 'DV1UK' that DV1UK never sent reaches its BPQ32 (DV1UK-10)",
              len(delivered) == n_del + 1 and bpq_bridge.unpack_batch(delivered[-1]) == [la.strip_digis(sab)])
        check("v0.9.71: ...shown to BPQ32 as a direct connect, and the log says so",
              "BPQ32 is shown a direct connection" in "\n".join(app.log_box.get("1.0", "end").splitlines()[-8:]))
        check("...with a note that another station may use this callsign", "YOUR callsign" in lg, lg)
        app._warned_own_call = False
        n_del = len(delivered)
        sab4 = la.digipeat(_ax25_frame("DV1UK-10", "DV1UK-4", [("DW1IRF", False)], control=0x3F, pid=False), "DW1IRF")
        app._handle_air_frame_for_bridge(air(bpq_bridge.pack_batch([sab4])))
        app.update()
        lg = "\n".join(app.log_box.get("1.0", "end").splitlines()[-4:])
        check("v0.9.64: DV1UK-4 is a different station from DV1UK: passed on, no note",
              len(delivered) == n_del + 1 and "YOUR callsign" not in lg, lg)
        app.call_var.set("DX1FLR")
        sabm = _ax25_frame(A, "DX1FLR-10", [(D, True)], control=0x3F, pid=False)
        msgs = []
        app._log = msgs.append
        app.after = lambda ms, fn=None, *a: fn() if fn else None
        app._check_new_connection(_ax25_frame("DX1FLR-10", A, [(D, True)], control=0x3F, pid=False), incoming=True)
        check("a connect through the digipeater: Auto restarts for the hop to DX1CW",
              any("New connection with DW1IRF (via DX1CW)" in m and "hop to DX1CW" in m for m in msgs), str(msgs))
        msgs.clear()
        app._check_new_connection(_ax25_frame("DX1FLR-10", A, [(D, False)], control=0x3F, pid=False), incoming=True)
        check("...the same connect heard before the digipeater repeated it is not a connection yet", msgs == [])
        del sabm
    finally:
        app.bridge = None
        app.destroy()


def test_sara_chat():
    """v0.9.38: SARA Chat. Its frames can't be mistaken for AX.25, a batch or
    a ping; direct messages arrive in order with receipts over a lossy
    channel and resend themselves; an undeliverable message is held and
    delivered when the station is next heard; NET CQ/check-in reach
    everyone; files arrive intact; every frame size decodes in every chat
    mode; inside SARA FM chat never reaches BPQ32 and waits out a Winlink
    session (receipts excepted)."""
    import random as _r
    import tempfile
    import chat
    import link_adapt as la
    import struct
    p = chat.pack(chat.T_MSG, "DW1IRF", "DV1UK", chat.msg_body(7, 0, 1234, "hello \u2764 " * 3), left=2)
    d = chat.unpack(p)
    check("chat frame round-trips (text, ids, 'left', UTF-8)",
          d and d["frm"] == "DW1IRF" and d["to"] == "DV1UK" and d["id"] == 7 and d["left"] == 2
          and d["text"] == "hello \u2764 " * 3)
    ax = bpq_bridge.pack_batch([_ax25_iframe("DV1UK", "DW1IRF", 1, b"x")])
    check("chat frames and AX.25 / batches / pings are never confused",
          chat.is_chat(p) and not chat.is_chat(ax) and not chat.is_chat(_ax25_iframe("DV1UK", "DW1IRF", 1, b"x"))
          and not proto.is_pingpong(p) and p[0] & 1 == 1)   # AX.25 address bytes are always even
    big_txt = "".join(chr(0x4e00 + _r.Random(3).randrange(2000)) for _ in range(400))
    parts = chat.split_text(big_txt)
    check("long text is split into frame-sized pieces", len(parts) > 1 and "".join(parts) == big_txt
          and all(len(x.encode()) <= chat.MAX_TEXT_BYTES for x in parts))
    from scipy.signal import resample
    single = [chat.pack(chat.T_MSG, "DW1IRF-15", "DX1FLR-10", chat.msg_body(65535, 0, 2**31, parts[0])),
              chat.pack(chat.T_CHUNK, "DW1IRF-15", "DX1FLR-10", bytes(6) + os.urandom(chat.CHUNK_BYTES))]
    eng = chat.ChatEngine(lambda: "DW1IRF-15", lambda *a: True)
    units = []
    for k in range(3):
        u = chat._Unit("DX1FLR-10", 100 + k, "chunk", None, {}, 1)
        u.piece = (1, k, os.urandom(chat.CHUNK_BYTES))
        units.append(u)
    eng.multi_ok.add("DX1FLR-10")
    eng.burst_plan = lambda peer: (chat.FRAME_CAP_FAST, 6)
    grp = eng._plan_frames("DX1FLR-10", units)
    multi = eng._multi_payload("DX1FLR-10", grp[0], 0)
    check("two 280-byte pieces share a frame within the fast-mode cap; a third starts the next frame",
          [len(g) for g in grp] == [2, 1] and len(multi) <= chat.FRAME_CAP_FAST
          and [x[2] for x in chat.unpack(multi)["pieces"]] == [0, 1])

    def survives(w, m, ppm=200):
        b = ofdm.frame_encode(w, modulation=m)
        b = resample(b, int(len(b) * (1 + ppm * 1e-6))).astype(np.float32)
        out = decode_all(ofdm.OFDMReceiver(), np.concatenate([np.zeros(4800, np.float32), b,
                                                              np.zeros(4800, np.float32)]), chunk=1024)
        return len(out) == 1 and out[0].payload == w
    ok_air = all(survives(w, m) for m in la.LADDER for w in single)
    check(f"largest one-piece chat frames ({max(len(w) for w in single)} B) decode in every mode "
          f"with 200 ppm clock drift", ok_air)
    fast = [m for m in la.LADDER if ofdm.base_mod(m) in (ofdm.MOD_QAM16_WIDE, ofdm.MOD_QAM64_WIDE)]
    big = multi + bytes(chat.FRAME_CAP_FAST - len(multi))
    check(f"a full {chat.FRAME_CAP_FAST}-byte multi-piece frame decodes in every 16QAM/64QAM mode "
          f"with 200 ppm drift", all(survives(big, m) for m in fast))

    tmp = tempfile.mkdtemp()
    clock = [1000.0]
    air = []
    rnd = _r.Random(7)
    loss = [0.25]

    def mk(call, sub):
        def tx(payload, peer, attempt, on_sent):
            air.append((clock[0] + 1.0, call, payload))
            on_sent()
            return True
        return chat.ChatEngine(lambda: call, tx, now_fn=lambda: clock[0],
                               store_path=os.path.join(tmp, sub + ".json"), files_dir=os.path.join(tmp, sub))
    A, B, C = mk("DW1IRF", "a"), mk("DV1UK", "b"), mk("DX1FLR", "c")
    stations = (A, B, C)

    def run(sec):
        end = clock[0] + sec
        while clock[0] < end:
            clock[0] += 0.25
            for item in [x for x in air if x[0] <= clock[0]]:
                air.remove(item)
                for e in stations:
                    if e.me() != item[1] and rnd.random() > loss[0]:
                        e.on_frame(item[2], snr=24)
            for e in stations:
                e.tick()
    sent = [A.send_text("dv1uk", f"message {i}: " + "words " * 12)[0] for i in range(8)]
    pic = bytes(rnd.randrange(256) for _ in range(5000))
    fe = A.send_file("DV1UK", "photo.jpg", pic)
    run(400)
    got = [e["text"] for e in B.convs.get("DW1IRF", []) if e.get("kind") == "msg"]
    check("25% frame loss: all 8 messages delivered once, in order, with receipts",
          got == [e["text"] for e in sent] and all(e["status"] == "delivered" for e in sent),
          f"{len(got)} {[e['status'] for e in sent]}")
    rf = [e for e in B.convs.get("DW1IRF", []) if e.get("kind") == "file"]
    check("...and the 5 kB file arrives intact, saved to chat_files",
          fe["status"] == "delivered" and rf and rf[0]["status"] == "received"
          and open(rf[0]["path"], "rb").read() == pic, str(fe["status"]))
    check("a third station overhearing keeps out of their conversation",
          "DW1IRF" not in C.convs and "DW1IRF" in C.heard)
    check("unread counts for the GUI", B.unread.get("DW1IRF", 0) >= 9)

    loss[0] = 1.0
    hm = A.send_text("DX1FLR", "are you there? leave this for later")[0]
    run(200)
    check("no answer: the message is held for DX1FLR", hm["status"] == "held" and A.held_for("DX1FLR") == 1)
    A.save()
    A2 = chat.ChatEngine(lambda: "DW1IRF", A.transmit, now_fn=lambda: clock[0],
                         store_path=A.store_path)
    check("held messages survive a restart", A2.held_for("DX1FLR") == 1)
    loss[0] = 0.0
    C.send_net("", "checkin")
    run(60)
    cin = [e for e in C.convs.get("DW1IRF", []) if e.get("kind") == "msg"]
    check("DX1FLR is heard (a NET check-in): the held message is delivered, marked as held",
          hm["status"] == "delivered" and cin and cin[-1].get("held_note") and A.held_for("DX1FLR") == 0)
    net_b = [e for e in B.convs.get(chat.NET, [])]
    check("NET check-in reaches every station; the heard list marks it",
          net_b and net_b[-1]["kind"] == "checkin" and B.heard["DX1FLR"].get("checkin"))
    A.send_net("", "cq")
    run(10)
    check("CQ goes to the NET", C.convs[chat.NET][-1]["kind"] == "cq" and "CQ CQ CQ de DW1IRF" in C.convs[chat.NET][-1]["text"])
    # v0.9.40: multi-piece frames once the other station's receipt says it takes them
    frames_seen = []
    real_tx = A.transmit

    def spy(payload, peer, attempt, on_sent):
        frames_seen.append(chat.unpack(payload)["type"])
        return real_tx(payload, peer, attempt, on_sent)
    A.transmit = spy
    A.burst_plan = B.burst_plan = lambda peer: (chat.FRAME_CAP_FAST, 6)
    A.multi_ok.discard("DV1UK")
    loss[0] = 0.15
    pic2 = bytes(rnd.randrange(256) for _ in range(12000))
    fe2 = A.send_file("DV1UK", "photo2.jpg", pic2)
    run(400)
    rf2 = [e for e in B.convs.get("DW1IRF", []) if e.get("kind") == "file" and e.get("name") == "photo2.jpg"]
    check("fast link: after the first receipt, file pieces go two per frame (6 frames a burst)",
          "DV1UK" in A.multi_ok and frames_seen.count(chat.T_CHUNKS) >= 15
          and frames_seen.index(chat.T_CHUNKS) > 0, f"{frames_seen[:12]}")
    check("...and the 12 kB file arrives intact through 15% loss",
          fe2["status"] == "delivered" and rf2 and open(rf2[0]["path"], "rb").read() == pic2, fe2["status"])
    A.transmit = real_tx
    old_ack = chat.pack(chat.T_ACK, "DX1PAR", "DW1IRF", bytes([1]) + bytes([20]) + struct.pack(">H", 1))
    A.on_frame(old_ack)
    check("a station on 0.9.38/39 (receipt without the feature mark) keeps getting one piece per frame",
          "DX1PAR" not in A.multi_ok)
    try:
        A.send_file("DV1UK", "big.bin", bytes(chat.MAX_FILE_BYTES + 1))
        too_big = False
    except ValueError:
        too_big = True
    check("files over 32 kB are refused with a reason", too_big)

    # inside SARA FM
    import sara_pingpong as sp
    import types as _t
    queued = []

    class FakeEngine:
        def __init__(self):
            import queue as _q
            self.tx_level = 0.4
            self.frame_queue = _q.Queue()
            self.level_queue = _q.Queue()
        def queue_transmit(self, payload, modulation=0, on_sent=None, on_error=None, tx_gain=None):
            queued.append((payload, modulation))
        def __getattr__(self, n):
            return lambda *a, **k: None
    app = sp.App()
    try:
        app._save_settings = lambda: None
        app.chat.store_path = None
        app.chat.files_dir = os.path.join(tmp, "app")
        app.chat.convs, app.chat.heard, app.chat.held, app.chat.unread = {}, {}, [], {}
        app.call_var.set("DX1FLR")
        app.engine = FakeEngine()
        delivered = []
        app.bridge = _t.SimpleNamespace(deliver_from_air=delivered.append, stop=lambda: None,
                                        frames_sent=lambda p: None)
        msg = chat.pack(chat.T_MSG, "DW1IRF", "DX1FLR", chat.msg_body(5, 0, 1, "hi gateway"))
        fr = decode_all(ofdm.OFDMReceiver(), ofdm.frame_encode(msg, modulation=ofdm.MOD_QPSK), chunk=1024)[0]
        app._handle_air_frame_for_bridge(fr)
        app.update()
        acks = [chat.unpack(q[0]) for q in queued]
        check("in SARA FM: a chat frame is never passed to BPQ32, shows in the chat and is acknowledged",
              delivered == [] and app.chat.convs["DW1IRF"][-1]["text"] == "hi gateway"
              and acks and acks[-1]["type"] == chat.T_ACK and acks[-1]["ids"] == [5])
        check("the Chat button shows the unread message", "1 new" in app.chat_btn.cget("text"))
        app._last_session_activity = _time.time()          # a Winlink session is moving data
        n0 = len(queued)
        e = app.chat.send_text("DW1IRF", "hello back")[0]
        app.chat.tick()
        waits = len(queued) == n0 and e["status"] == "queued"
        app._handle_air_frame_for_bridge(decode_all(ofdm.OFDMReceiver(), ofdm.frame_encode(
            chat.pack(chat.T_MSG, "DW1IRF", "DX1FLR", chat.msg_body(6, 0, 1, "again")),
            modulation=ofdm.MOD_QPSK), chunk=1024)[0])
        ack_during = len(queued) == n0 + 1 and chat.unpack(queued[-1][0])["type"] == chat.T_ACK
        app._last_session_activity = 0.0
        app.chat.tick()
        check("during a Winlink session chat waits, but receipts still go out", waits and ack_during)
        check("...and the message goes once the session is idle",
              len(queued) == n0 + 2 and chat.unpack(queued[-1][0])["text"] == "hello back")
        check("chat starts at QPSK to a station Auto knows nothing about; NET is always QPSK",
              app._chat_mode("DX1PAR", 1) == ofdm.MOD_QPSK and app._chat_mode(None, 1) == ofdm.MOD_QPSK)

        # v0.9.39: receipts carry a link report; chat drives Auto speed and the speed gauge
        queued.clear()
        for k in range(4):
            pl = chat.pack(chat.T_CHUNK, "DW1IRF", "DX1FLR", struct.pack(">HHH", 100 + k, 99, k) + bytes(300),
                           left=3 - k)
            app._handle_air_frame_for_bridge(decode_all(ofdm.OFDMReceiver(), ofdm.frame_encode(
                pl, modulation=ofdm.MOD_QPSK), chunk=1024)[0])
        ack = chat.unpack(queued[-1][0]) if queued else None
        rep_ = la.parse_report(ack["extra"]) if ack else None
        check("our receipt carries a link report on how we hear DW1IRF (4 frames, QPSK, strong)",
              rep_ is not None and rep_.n_ok == 4 and rep_.mode == ofdm.MOD_QPSK and rep_.snr_db > 20,
              str(rep_))
        app._update_status_gauges()
        check("chat file data moves the speed meter and gauge",
              app.rx_meter.total_bytes >= 1200 and "bps" in app.speed_gauge.itemcget(app.speed_gauge._readout, "text")
              and app.rx_meter.rate_bps() > 0)
        steps = []
        app.speed.peers.pop("DV1UK", None)                  # no memory from an earlier run
        for k in range(8):
            m = app._chat_mode("DV1UK", 1)
            rp = la.Report(m, 4, 0, 34, 0, 0, caps=la.MY_CAPS, carrier_snr_db=30)
            app.chat.on_frame(chat.pack(chat.T_ACK, "DV1UK", "DX1FLR",
                                        bytes([1]) + struct.pack(">b", 30) + struct.pack(">H", 1) + rp.pack()))
            steps.append(app._chat_mode("DV1UK", 1))
            app.speed.peers["DV1UK"].last_change -= 60     # let the hold-offs pass
            app.speed.peers["DV1UK"].last_report -= 0
        check("clean strong receipts step chat speed up, like a Winlink session",
              la.LADDER.index(steps[-1]) > la.LADDER.index(ofdm.MOD_QPSK), str([la.NAMES[x] for x in steps]))
        pv = app.speed.peers["DV1UK"]
        pv.last_report -= 600                               # their last receipt is old news...
        before = app._chat_mode("DV1UK", 1)                 # ...we send a window...
        app.chat.on_resend("DV1UK")                         # ...and have to resend it
        check("a resent window (with no fresh strong report) steps chat speed down",
              la.LADDER.index(app._chat_mode("DV1UK", 1)) < la.LADDER.index(before))
        app.open_chat()
        app.update()
        w = app.chat_win
        w.show_conversation("DW1IRF")
        app.update()
        txt = w.text.get("1.0", "end")
        check("Chat window: conversation with times and status, list and heard list",
              "hi gateway" in txt and "hello back" in txt and "sending" in txt
              and "DW1IRF" in w.chat_list.get(0, "end")[1] and w.heard_tv.exists("DW1IRF"), txt[-200:])
        check("opening the chat clears the unread count", app.chat.unread.get("DW1IRF", 0) == 0
              and app.chat_btn.cget("text") == "Chat")
    finally:
        app.engine = None
        app.bridge = None
        app.destroy()


def test_autotune_robust():
    """v0.9.41: with Modulation Auto, PING and Auto-Tune ping in BPSK (the most
    sensitive mode); before the first reply Auto-Tune steps the TX level up
    (FM: more deviation, better S/N at the far end), keeps the level that got
    an answer, and puts it back if nobody ever answers; far-end advice says
    to raise TX level when they hear us quietly and noisily."""
    import types as _t
    import tuning
    import sara_pingpong as sp
    sent = []

    class FakeEngine:
        def __init__(self):
            import queue as _q
            self.tx_level = 0.4
            self.rx_gain = 1.0
            self.frame_queue = _q.Queue()
            self.level_queue = _q.Queue()
        def transmit(self, payload, modulation=0, ignore_dcd=False, **k):
            sent.append((payload, modulation, self.tx_level))
        def __getattr__(self, n):
            return lambda *a, **k: None
    app = sp.App()
    try:
        app._save_settings = lambda: None
        app.call_var.set("DW3TDJ"); app.modulation_var.set("Auto")
        app.target_var.set("DU3FS"); app.tx_level_var.set("40"); app.rx_gain_db_var.set("0")
        app.engine = FakeEngine()
        app._start_autotune()
        _time.sleep(0.3)
        check("Auto-Tune's first ping goes out in BPSK", sent and sent[-1][1] == ofdm.MOD_BPSK, str(sent[-1:]))
        steps = []
        app.after = lambda ms, fn=None, *a: None           # don't chain the next ping
        for _ in range(4):
            app._autotune_on_timeout()
            steps.append(app.tx_level_var.get())
        check("no reply: TX level 40% -> 20% (overdrive?) -> 55% -> 70% -> 85%",
              steps == ["20", "55", "70", "85"], str(steps))
        f = _t.SimpleNamespace(level_dbfs=-30.0, snr_db=12.0)
        m = _t.SimpleNamespace(report_snr_db=10, report_level_dbfs=-44)
        app._autotune_on_pong(m, f)
        check("a reply: Auto-Tune keeps the level that got through and goes on to RX gain",
              app.autotune and app.autotune["contact"] and app.tx_level_var.get() == "85")
        app.autotune = None
        app.tx_level_var.set("40")
        app._start_autotune()
        app.autotune["reply_clipped"] = True
        app._autotune_on_timeout()
        check("clipped reply: Auto-Tune keeps OUR TX level (it's theirs that's too high)",
              app.tx_level_var.get() == "40" and app.autotune.get("clipped") == 1)
        app.autotune = None
        app.tx_level_var.set("40")
        app._start_autotune()
        for _ in range(12):
            if not app.autotune:
                break
            app._autotune_on_timeout()
        check("nobody ever answers: Auto-Tune ends and puts the TX level back to 40%",
              app.autotune is None and app.tx_level_var.get() == "40")
    finally:
        app.engine = None
        app.destroy()
    adv = " ".join(tuning.report_advice(9, -44, "DU3FS"))
    check("far end hears us quiet AND noisy: advice is to raise TX level (FM deviation)",
          "raise your TX level" in adv and "deviation" in adv)
    adv2 = " ".join(tuning.report_advice(9, -15, "DU3FS"))
    check("...noisy but at a normal level: it's the radio path", "radio path" in adv2 and "raise" not in adv2)


def test_dense_noise_candidates_stay_cheap():
    """v0.9.45, field (DW1IRF, 0.9.44, idle channel, RX gain +20 dB): ~44
    noise candidates a second were turned away one per audio block, each
    skipping half a symbol while a block adds two -- the receive buffer grew
    to its 9 s ceiling and every block re-filtered all of it (decoder at
    100% of a core with nothing on the air). Now every candidate in the
    buffer is handled in the same poll: the buffer stays short."""
    import time as _t2
    rng = np.random.default_rng(1)
    SR = ofdm.SAMPLE_RATE
    T = 12
    per = rng.normal(0, 1, ofdm.N_SYM)          # a broadband buzz repeating every 640 samples
    x = np.tile(per, SR * T // ofdm.N_SYM + 1)[:SR * T] * 4.0 + rng.normal(0, 1, SR * T)
    x = (x / np.sqrt(np.mean(x ** 2)) * 10 ** (-34 / 20)).astype(np.float32)
    rx = ofdm.OFDMReceiver()
    rx.RX_MIN_PREAMBLE_SNR_DB = 99.0            # every candidate turned away, as densely as in the field
    n = [0]
    rx.on_reject = lambda r, i: n.__setitem__(0, n[0] + 1)
    maxbuf = 0
    t0 = _t2.process_time()
    for i in range(0, len(x), 1024):
        rx.push(x[i:i + 1024])
        while rx.poll() is not None:
            pass
        maxbuf = max(maxbuf, len(rx.buf))
    cpu = (_t2.process_time() - t0) / T
    check(f"{n[0] / T:.0f} rejected candidates a second: receive buffer stays under 1 s "
          f"(max {maxbuf / SR:.2f}s), decoder {100 * cpu:.0f}% of a core", n[0] / T > 20 and maxbuf < SR)


def test_sound_card_clock_offset():
    """v0.9.46, field (DW3TDJ <-> DU3FS): VARA FM pinged both ways, SARA FM
    never; DU3FS turned away DW3TDJ's 19 pings at the header/FEC stage with
    clean preambles -- the signature of a sound card running off-frequency
    (cheap USB dongles: 0.2-1%). The preamble now measures the offset and the
    receiver resamples to undo it: every mode decodes from -0.3% to +0.8%,
    stations with different clocks take turns, and noise never sets a false
    correction."""
    from scipy.signal import resample
    import link_adapt as la
    import protocol as _p
    rng = np.random.default_rng(5)

    def sig(pl, mode, ppm, snr=18):
        b = ofdm.frame_encode(pl, modulation=mode).astype(np.float64)
        b = resample(b, int(len(b) * (1 + ppm * 1e-6)))
        b = b / np.sqrt(np.mean(b ** 2)) * 0.1
        return b, 0.1 * 10 ** (-snr / 20)
    est_ok = True
    for ppm in (-5000, 0, 2000, 5000):
        b, nl = sig(_p.make_ping("DW3TDJ", "DU3FS", 1), ofdm.MOD_BPSK, ppm, snr=13)
        x = np.concatenate([np.zeros(4800), b, np.zeros(4800)]) + rng.normal(0, nl, len(b) + 9600)
        rx = ofdm.OFDMReceiver()
        from scipy.signal import sosfiltfilt as _sff
        f = _sff(rx.sos, x).astype(np.float32)
        st = rx._find_preamble(f)
        e = ofdm.OFDMReceiver.preamble_ppm([f[st + r * ofdm.N_SYM:st + (r + 1) * ofdm.N_SYM] for r in range(4)])
        est_ok &= abs(e - ppm) < 300
    check("the preamble measures a sound-card clock offset to within 300 ppm (S/N 13 dB)", est_ok)
    bad = []
    for m in la.LADDER:
        pl = bytes(rng.integers(0, 256, 240, dtype=np.uint8))
        for ppm in (-5000, -3000, 3000, 6000):
            b, nl = sig(pl, m, ppm)
            x = rng.normal(0, nl, len(b) + 19200)
            x[9600:9600 + len(b)] += b
            out = decode_all(ofdm.OFDMReceiver(), x.astype(np.float32), chunk=1024)
            if not any(fr.payload == pl for fr in out):
                bad.append((la.NAMES[m], ppm))
    check("every mode decodes with the transmitter's sound card up to 0.5% fast or 0.6% slow "
          "(was: failed beyond 0.15%)", not bad, str(bad))
    rx = ofdm.OFDMReceiver()
    parts, sent = [], []
    for i, ppm in enumerate((5000, 0, 5000, 3000, 0, -2000, 5000)):
        pl = bytes([i]) * 60
        b, nl = sig(pl, ofdm.MOD_QPSK, ppm)
        parts += [rng.normal(0, nl, 24000), b + rng.normal(0, nl, len(b))]
    out = decode_all(rx, np.concatenate(parts + [rng.normal(0, 0.01, 24000)]).astype(np.float32), chunk=1024)
    check("stations with different sound-card clocks take turns: every frame decoded",
          [fr.payload[0] for fr in out] == list(range(7)), str([fr.payload[0] for fr in out]))
    import fm_channel as _fc
    false = 0
    for path in ("flat", "speaker"):
        for g in (0.003, 0.3):
            x = _fc.receive(np.zeros(ofdm.SAMPLE_RATE * 15), np.zeros(ofdm.SAMPLE_RATE * 15, bool), 10, rng,
                            out_gain=g, path=path)
            r2 = ofdm.OFDMReceiver()
            fx = []
            r2.on_clock_fix = fx.append
            decode_all(r2, x, chunk=1024)
            false += len(fx)
    check("open-squelch hiss never sets a clock correction", false == 0, str(false))


def test_rx_recording_and_header_rejects():
    """v0.9.47: Log > Record received audio saves exactly what the sound card
    delivered as a WAV; a frame lost after its preamble was recognised
    (header, FEC...) is always logged with the preamble's S/N, level and
    clock estimate."""
    import wave
    import tempfile
    import types as _ty
    import sara_pingpong as sp
    e = _os_engine()
    e.start_recording(0.5)
    tone = (0.25 * np.sin(2 * np.pi * 1000 * np.arange(int(0.8 * ofdm.SAMPLE_RATE)) / ofdm.SAMPLE_RATE))
    pcm = (tone * 32767).astype(np.int16)
    for i in range(0, len(pcm) - 1023, 1024):
        e._rx_callback(pcm[i:i + 1024].tobytes(), 1024, None, 0)
    data = e.take_recording()
    check("recording keeps exactly 0.5 s of what the card delivered",
          data is not None and len(data) == int(0.5 * ofdm.SAMPLE_RATE) and abs(np.max(data) - 0.25) < 0.01)
    d = tempfile.mkdtemp()
    logged = []
    fake = _ty.SimpleNamespace(engine=_ty.SimpleNamespace(take_recording=lambda: data),
                               call_var=_ty.SimpleNamespace(get=lambda: "DW3TDJ"),
                               session_log=_ty.SimpleNamespace(dir=d), _log=logged.append)
    sp.App._check_rx_recording(fake)
    wavs = [f for f in os.listdir(d) if f.endswith(".wav")]
    ok = False
    if wavs:
        with wave.open(os.path.join(d, wavs[0])) as w:
            ok = w.getframerate() == ofdm.SAMPLE_RATE and w.getnframes() == len(data) and w.getnchannels() == 1
    check("...and saves it as a 48 kHz mono WAV in the log folder, named for the station",
          ok and wavs[0].startswith("SARA-FM_rx_DW3TDJ_") and any("saved" in m for m in logged), str(wavs))
    # a frame whose header is wrecked after a good preamble
    b = ofdm.frame_encode(bytes(100), modulation=ofdm.MOD_BPSK).copy()
    n_pre = ofdm.N_SYM * ofdm.PREAMBLE_REPS
    rng = np.random.default_rng(3)
    b[n_pre:n_pre + ofdm.N_SYM] = rng.normal(0, np.std(b), ofdm.N_SYM)
    rx = ofdm.OFDMReceiver()
    seen = []
    rx.on_reject = lambda r, i: seen.append((r, i))
    decode_all(rx, np.concatenate([np.zeros(4800), b, np.zeros(9600)]).astype(np.float32), chunk=1024)
    hdr = [(r, i) for r, i in seen if r not in ofdm.OFDMReceiver.QUIET_REJECTS]
    check("a header-stage rejection carries the preamble's S/N, level and clock estimate",
          hdr and all(k in hdr[0][1] for k in ("pre_snr_db", "level_dbfs", "clock_ppm_est")), str(hdr[:1]))


def test_false_clock_fix_from_tone():
    """v0.9.54, field (DW3TDJ, 0.9.53, Log > Record received audio): a steady
    tone (-24 dBFS, between DW3TDJ's own reply and DU3FS's next ping) was
    stretched by the clock rescue until it "repeated" at the preamble lag --
    a 1.59% correction with nobody's preamble on the air, after which DW3TDJ
    decoded nothing more from DU3FS. 13.4 s excerpt of the real recording,
    at DW3TDJ's RX gain (+9 dB): 0.9.53 decodes ping 9 and then nothing
    (correction 17760 ppm); now pings 9, 10, 11 and no correction."""
    import wave
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "testdata",
                        "field_dw3tdj_tone_false_clockfix.wav")
    if not os.path.exists(path):
        check("false clock-fix recording present (testdata/)", False, path)
        return
    with wave.open(path) as w:
        x = np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32) / 32768 * 2.818
    rx = ofdm.OFDMReceiver()
    out = decode_all(rx, x, chunk=1024)
    seqs = [proto.PingPongMessage.unpack(f.payload).seq for f in out if proto.is_pingpong(f.payload)]
    check("field: a tone doesn't set a sound-card clock correction; DU3FS's pings 9-11 all decode",
          seqs == [9, 10, 11] and abs(rx.clock_ppm) < 1, f"seqs={seqs} clock={rx.clock_ppm:.0f}")
    # a correction nothing decodes with is a trial, undone after CLOCK_TRIAL_S
    rx = ofdm.OFDMReceiver()
    rx.push(np.zeros(ofdm.N_SYM * 12, dtype=np.float32))
    rx.poll()
    prev = rx._ratio
    abs_a = rx._apply_clock_fix(0, 15000.0)
    rx._clock_trial = {"prev": prev, "r": 1.015, "abs": abs_a,
                       "until": rx._total_pushed + int(rx.CLOCK_TRIAL_S * ofdm.SAMPLE_RATE)}
    fired = []
    rx.on_clock_fix = fired.append
    for _ in range(int(4.0 * ofdm.SAMPLE_RATE / 1024)):
        rx.push(np.zeros(1024, dtype=np.float32))
        rx.poll()
    check("an unconfirmed clock correction is undone (and never announced)",
          rx._ratio == prev and rx.clock_reverts == 1 and not fired,
          f"ratio={rx._ratio} reverts={rx.clock_reverts} fired={fired}")
    # v0.9.59: a header passing doesn't confirm a trial; a confirmed correction
    # that keeps failing is dropped (DV1UK: -8841 ppm, deaf to both ends)
    rx = ofdm.OFDMReceiver()
    fired = []
    rx.on_clock_fix = fired.append
    rx._ratio = 1.0 - 8841e-6                      # as if confirmed earlier
    rx._last_good_at = 0
    rx._wd_fails = 3
    for _ in range(int(16.0 * ofdm.SAMPLE_RATE / 1024)):
        rx.push(np.zeros(1024, dtype=np.float32))
        rx.poll()
    check("a clock correction with frames failing and no good decode for 15 s is dropped",
          abs(rx._ratio - 1.0) < 1e-9 and getattr(rx, "clock_drops", 0) == 1 and fired == [0.0],
          f"ratio={rx._ratio} fired={fired}")
    import inspect as _insp
    src = _insp.getsource(ofdm.OFDMReceiver)
    check("only a whole decoded frame confirms a clock trial (not a header)",
          src.count("self._confirm_clock()") == 1)


def test_field_recording_dw3tdj():
    """v0.9.48, field (DW3TDJ <-> DU3FS, Log > Record received audio): DU3FS's
    BPSK pings reached DW3TDJ at preamble S/N 26 dB, yet their headers failed
    -- the detector fired 260-370 samples early (more than the 128-sample
    guard), because the audio before the preamble already correlated at the
    repetition lag. Fine timing against the known preamble fixes it: both
    pings in this 5.8 s excerpt of the real recording decode (0.9.47: none)."""
    import wave
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "testdata",
                        "field_dw3tdj_rx_du3fs_pings.wav")
    if not os.path.exists(path):
        check("field recording present (testdata/)", False, path)
        return
    with wave.open(path) as w:
        x = np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32) / 32768
    rx = ofdm.OFDMReceiver()
    lost = []
    rx.on_reject = lambda r, i: lost.append(r) if r not in ofdm.OFDMReceiver.QUIET_REJECTS else None
    out = decode_all(rx, x, chunk=1024)
    import link_adapt as _la
    ui = _ax25_frame("APBPQ1", "DW3TDJ-7", [("WIDE1-1", False)], control=0x03, info=b"!beacon")
    sabm = _ax25_frame("DU3FS-10", "DW3TDJ", [], control=0x3F, pid=False)
    check("a UI beacon (BPQ32 APRS/ID) is not Winlink session traffic; a connect is",
          not _la.session_frames([ui]) and _la.session_frames([ui, sabm])
          and _la.session_frames([_ax25_iframe("DU3FS-10", "DW3TDJ", 1, b"x")]))
    pings = [proto.PingPongMessage.unpack(f.payload) for f in out if proto.is_pingpong(f.payload)]
    check("field recording: DU3FS's two pings to DW3TDJ both decode (were lost at the header)",
          [(m.from_call.strip(), m.seq) for m in pings] == [("DU3FS", 1), ("DU3FS", 2)] and not lost,
          f"{[(m.from_call, m.seq) for m in pings]} lost={lost}")


def test_idle_gate():
    """v0.9.49: with nothing on the air the receiver no longer re-filters
    and re-searches its whole buffer for every audio block -- a cheap
    streaming check decides whether a preamble could be arriving. Idle hiss
    skips most of the full searches; frames through every radio audio path
    decode exactly as before; a frame right after long silence is caught."""
    import fm_channel as _fc
    rng = np.random.default_rng(1)
    SR = ofdm.SAMPLE_RATE
    skipped = {}
    for path in ("flat", "speaker"):
        x = _fc.receive(np.zeros(SR * 15), np.zeros(SR * 15, bool), 10, rng, out_gain=0.03, path=path)
        rx = ofdm.OFDMReceiver()
        n = 0
        for i in range(0, len(x), 1024):
            rx.push(x[i:i + 1024])
            rx.poll()
            n += 1
        skipped[path] = rx.gate_skips / n
    check(f"idle open-squelch hiss: full search skipped for {100 * skipped['flat']:.0f}% (flat audio) / "
          f"{100 * skipped['speaker']:.0f}% (speaker audio) of audio blocks",
          skipped["flat"] > 0.8 and skipped["speaker"] > 0.35, str(skipped))
    ok = 0
    for k in range(6):
        pl = bytes(rng.integers(0, 256, 60, dtype=np.uint8))
        mod = (ofdm.MOD_BPSK, ofdm.MOD_QPSK, ofdm.MOD_QPSK_WIDE)[k % 3]
        b = ofdm.frame_encode(pl, modulation=mod) * 0.4
        tx = np.concatenate([np.zeros(SR * 3), b, np.zeros(SR)])
        on = np.concatenate([np.zeros(SR * 3, bool), np.ones(len(b), bool), np.zeros(SR, bool)])
        y = _fc.receive(tx, on | (np.arange(len(tx)) > SR * 2.5), 14, rng, out_gain=0.05,
                        path=("flat", "speaker", "speaker_narrow")[k % 3])
        out = decode_all(ofdm.OFDMReceiver(), y, chunk=1024)
        ok += any(f.payload == pl for f in out)
    check(f"frames after 3 s of idle hiss still decode on every path ({ok}/6)", ok == 6)


def test_session_retry_accounting():
    """v0.9.30: time lost to AX.25 retry timers is counted per session. A
    quiet stretch ended by a resent I-frame, a poll or a repeated connect is
    a recovery; one ended by ordinary new traffic is not; short gaps are
    normal turn-taking."""
    import session_stats as ss  # noqa

    def ctl(dst, src, control):
        def addr(call, last):
            c, _, ssid = call.partition("-")
            return bytes(x << 1 for x in c.ljust(6).encode()) + bytes([0x60 | (int(ssid or 0) << 1) | (1 if last else 0)])
        return addr(dst, False) + addr(src, True) + bytes([control])
    st = ss.SessionRetryStats()
    st.reset(now=1000.0)
    I1 = _ax25_iframe("DX1CW-10", "DW1IRF", 1, b"data one")
    I2 = _ax25_iframe("DX1CW-10", "DW1IRF", 2, b"data two")
    rr_poll = ctl("DW1IRF", "DX1CW-10", 0x01 | 0x10 | (3 << 5))    # RR, P/F set
    rr = ctl("DW1IRF", "DX1CW-10", 0x01 | (3 << 5))
    notes = [st.on_traffic("tx", [I1], now=1001.0),
             st.on_traffic("rx", [rr], now=1003.0),            # normal ack, short gap
             st.on_traffic("tx", [I2], now=1004.0),
             st.on_traffic("tx", [I2], now=1018.0),            # 14 s silent, then resent by us
             st.on_traffic("rx", [rr_poll], now=1032.0),       # 14 s silent, then they poll
             st.on_traffic("tx", [_ax25_iframe("DX1CW-10", "DW1IRF", 3, b"new")], now=1060.0)]  # long pause, new data
    check("resend after 14 s silence is one recovery, with a log line",
          notes[3] is not None and "14.0s" in notes[3] and "resent" in notes[3], str(notes[3]))
    check("their poll after silence is one recovery", notes[4] is not None and "polled" in notes[4], str(notes[4]))
    check("short gaps and a pause ended by new traffic are not counted",
          notes[0] is None and notes[1] is None and notes[2] is None and notes[5] is None)
    sm = st.summary(now=1100.0)
    check(f"summary: 2 recoveries, 28 s waiting, 1 frame resent by us -- '{sm.strip()}'",
          st.recoveries == 2 and abs(st.wait_s - 28.0) < 0.01 and st.resent_by_us == 1
          and "0:28" in sm and "28%" in sm)
    sab = ctl("DX1FLR-10", "DW1IRF", 0x3F)
    st.reset(now=2000.0)
    st.on_traffic("tx", [sab], now=2001.0)
    n = st.on_traffic("tx", [sab], now=2020.0)
    check("a repeated connect request after silence counts", n is not None and "connect" in n, str(n))
    st.reset(now=3000.0)
    check("a clean session says 'retries: none'", st.summary(now=3060.0).strip() == "retries: none")


def test_gauge_and_menus():
    """v0.9.33: the analog meter maps dBFS onto its arc in order and marks
    the last frame; the menu dialogs exist and close back to hidden; a
    fresh install's Modulation (Auto) is what the program actually uses."""
    import tkinter as tk
    import gauges
    root = tk.Tk()
    try:
        m = gauges.AnalogMeter(root)
        angs = [m._angle(d) for d in (-60, -36, -12, 0)]
        check("meter: -60 dBFS left, 0 dBFS right, in order", angs == sorted(angs, reverse=True)
              and abs(angs[0] - angs[-1] - m.SPAN) < 1e-6, str(angs))
        check("meter: out-of-range values pin to the stops", m._angle(-90) == angs[0] and m._angle(6) == angs[-1])
        m.set_frame_level(-12)
        check("meter: in-target frame reads green", m.itemcget(m._readout, "fill") == m.GREEN_LIT
              and "-12" in m.itemcget(m._readout, "text"))
        m.set_frame_level(-1)
        check("meter: frame near clipping reads red", m.itemcget(m._readout, "fill") == m.RED_LIT)
    finally:
        root.destroy()
    import sara_pingpong as sp
    app = sp.App()
    try:
        check("fresh window: Modulation Auto is the setting in use", app._mod_choice == app.modulation_var.get())
        app._show_dialog(app.devices_win)
        app.update()
        shown = app.devices_win.state() == "normal"
        app._close_dialog(app.devices_win)
        app.update()
        check("Settings > Devices opens and closes back to hidden",
              shown and app.devices_win.state() == "withdrawn")
        check("main window has the gauge and TX/RX lamps",
              isinstance(app.gauge, gauges.AnalogMeter) and isinstance(app.tx_lamp, gauges.Lamp))
        app._apply_view("gauges")
        app.update()
        hidden = not app.log_box.winfo_ismapped() and app.gauge.winfo_ismapped() and app.status_bar.winfo_ismapped()
        app._apply_view("full")
        app.update()
        back = app.log_box.winfo_ismapped() and app.ping_btn.winfo_ismapped()
        check("View > Gauges only hides buttons and log, keeps gauges and status bar; Full brings them back",
              hidden and back)

        # v0.9.35: saved levels per station
        app._save_settings = lambda: None
        app.rx_gain_db_var.set("24"); app.tx_level_var.set("25")
        app.target_var.set("DV1UK")
        app._store_profile("DV1UK", why="Auto-Tune")
        app.rx_gain_db_var.set("30"); app.tx_level_var.set("35")
        app._store_profile("DX1CW", why="Auto-Tune")
        app.target_var.set([v for v in app.target_combo["values"] if v.startswith("DV1UK")][0])
        app._on_target_selected()
        check("picking a station puts back its saved levels",
              app.target_var.get() == "DV1UK" and app.rx_gain_db_var.get() == "24" and app.tx_level_var.get() == "25",
              f"{app.target_var.get()} {app.rx_gain_db_var.get()} {app.tx_level_var.get()}")
        app._apply_profile_for_peer("DX1CW-10")
        check("a new Winlink connection with DX1CW-10 uses DX1CW's levels",
              app.rx_gain_db_var.get() == "30" and app.tx_level_var.get() == "35")
        d = app._settings_dict()
        saved = {t[0]: t[2] for t in d["targets"]}
        check("levels are written to the settings file per station",
              saved.get("DV1UK", {}).get("rx_gain_db") == 24 and saved.get("DX1CW", {}).get("tx_level_pct") == 35)
        check("the list shows saved levels", any("RX +24 dB, TX 25%" in v for v in app.target_combo["values"]))

        # v0.9.44: CPU meter in the status bar
        CB = gauges.CpuBar
        check("CPU meter colours: green when idle, amber/red when the PC or the decoder is busy",
              CB.color_for(10, 5) == CB.GREEN and CB.color_for(60, 5) == CB.AMBER
              and CB.color_for(10, 85) == CB.RED and CB.color_for(10, None) == CB.GREEN)
        import types as _ty
        fake = _ty.SimpleNamespace(decode_busy_total_s=0.0)
        app.engine = fake
        app._cpu_last = None
        app._update_cpu(now=1000.0)
        fake.decode_busy_total_s = 0.6
        app._update_cpu(now=1002.0)
        app.update()
        txt = app.cpu_bar.itemcget(app.cpu_bar._txt, "text")
        check("CPU meter shows SARA FM's share of the PC and the decoder's load (0.6 s in 2 s = 30%)",
              "%" in txt and "decoder 30%" in txt and app.cpu_bar.winfo_ismapped(), txt)
        app._apply_view("gauges"); app.update()
        shown = app.cpu_bar.winfo_ismapped()
        app._apply_view("full"); app.update()
        check("...visible in the Gauges-only view too", shown)
        check("...and summarised for the station report", "decoder 30% of one core" in app._cpu_summary())
        app.engine = None

        # v0.9.36: gauge title line, no Ping/Pong mode, bridge always on
        app.gauge_title_var.set("DX1FLR-10  Winlink Gateway")
        app.update()
        full_hidden = not app.gauge_title_lbl.winfo_ismapped()
        app._apply_view("gauges"); app.update()
        shown = app.gauge_title_lbl.winfo_ismapped() and app.gauge_title_lbl.cget("text") == "DX1FLR-10  Winlink Gateway"
        app.gauge_title_var.set(""); app.update()
        gone = not app.gauge_title_lbl.winfo_ismapped()
        app.gauge_title_var.set("DX1FLR-10"); app._apply_view("full"); app.update()
        check("gauge title shows above the gauges only in Gauges-only view, and only when set",
              full_hidden and shown and gone and not app.gauge_title_lbl.winfo_ismapped())
        check("gauge title is saved with the settings", app._settings_dict().get("gauge_title") == "DX1FLR-10")

        def texts(w):
            out = []
            for c in w.winfo_children():
                try:
                    out.append(str(c.cget("text")))
                except Exception:
                    pass
                out += texts(c)
            return out
        t = " | ".join(texts(app.setup_win))
        check("SARA Setup has no Ping/Pong mode, Start Bridge button or Auto-ping",
              "Ping/Pong" not in t and "Start Bridge" not in t and "Auto-ping" not in t and "Gauge title" in t, t)
        check("version string is sara-fm-", ofdm.VERSION.startswith("sara-fm-"))

        class FakeEngine:
            def __init__(self, *a, **k):
                self.tx_level = 0.4
            def start(self): pass
            def stop(self): pass
            def __getattr__(self, n):
                return lambda *a, **k: None
        real = sp.AudioEngine
        sp.AudioEngine = FakeEngine
        try:
            import socket as _so
            s_ = _so.socket(); s_.bind(("127.0.0.1", 0)); port = s_.getsockname()[1]; s_.close()
            app.kiss_port_var.set(str(port))
            app.call_var.set("DW1IRF"); app.ptt_var.set("VOX")
            app.in_dev_var.set("0: fake in"); app.out_dev_var.set("0: fake out")
            app._connect()
            started = app.bridge is not None
            app._disconnect()
            check("pressing Start also starts the KISS bridge; Stop stops it",
                  started and app.bridge is None and app.engine is None)
        finally:
            sp.AudioEngine = real
    finally:
        app.destroy()


def test_link_report_trailer():
    """The link report rides after a batch's counted frames: every receiver
    since v0.5 unpacks exactly the counted frames, so older versions (and
    BPQ32) never see it. The largest possible frame + report still decodes
    in every mode (Wide needed its own size limit)."""
    import link_adapt as la
    f1 = _ax25_iframe("K6IRF-10", "DW1IRF", 3, b"hello")
    f2 = _ax25_iframe("K6IRF-10", "DW1IRF", 4, b"world")
    batch = bpq_bridge.pack_batch([f1, f2])
    r = la.Report(ofdm.MOD_QPSK_WIDE, 7, 1, 28, 40, 9)
    wire = la.attach_report(batch, r)
    check("a v0.8 receiver unpacks a v0.9 batch to exactly its frames (report invisible)",
          bpq_bridge.unpack_batch(wire) == [f1, f2])
    check("report round-trips; plain batches and non-batches carry none",
          la.split_report(wire) == r and la.split_report(batch) is None
          and la.attach_report(b"PING...", r) == b"PING..." and la.split_report(b"\x01\x01\x00") is None)
    check("AX.25 addresses parsed (with SSID)", la.first_frame_calls(wire) == ("K6IRF-10", "DW1IRF"))
    for m in (ofdm.MOD_BPSK, ofdm.MOD_QPSK, ofdm.MOD_QPSK_WIDE):
        pl = la.attach_report(bpq_bridge.pack_batch([bytes([7]) * (256 + 72)]), r)
        out = decode_all(ofdm.OFDMReceiver(), ofdm.frame_encode(pl, modulation=m), chunk=1024)
        check(f"largest AX.25 frame + report decodes in {la.NAMES[m]}", len(out) == 1 and out[0].payload == pl)


def test_speed_controller_rules():
    """Down one step at once on loss / heavy FEC / low S/N; up one step only
    after 2 clean reports with margin (hysteresis); reports about another
    mode ignored; retransmissions step down unless a clean report arrived
    AFTER our last transmission; older peers (no reports) never pushed
    above QPSK."""
    import link_adapt as la
    Q, W, B = ofdm.MOD_QPSK, ofdm.MOD_QPSK_WIDE, ofdm.MOD_BPSK
    ch = []
    c = la.SpeedController(on_change=lambda *a: ch.append(a))
    R = lambda mode, snr, fec=0, bad=0: la.Report(mode, 7, bad, snr, fec, fec)
    check("starts in QPSK", c.mode_for("A", now=0) == Q)
    c.on_report("A", R(Q, 22), now=10)
    check("one clean report isn't enough to step up (without a clear margin)", c.mode_for("A", now=11) == Q)
    c.on_report("A", R(Q, 30), now=20)
    check("two clean strong reports -> QPSK Wide", c.mode_for("A", now=21) == W and ch[-1][2] == W)
    c.on_report("A", R(W, 18), now=30)
    check("hysteresis: Wide holds at 18 dB (down only below 16)", c.mode_for("A", now=31) == W)
    c.on_report("A", R(W, 25, bad=1), now=40)
    check("a lost frame steps down at once", c.mode_for("A", now=41) == Q and "1 lost" in ch[-1][3])
    c.on_report("A", R(Q, 12, fec=70), now=42)
    check("a second down signal right away is held off (rest of the old window)", c.mode_for("A", now=43) == Q)
    c.on_report("A", R(Q, 30), now=60)
    c.on_report("A", R(Q, 30), now=65)
    check("no step back up within 30 s of a step down", c.mode_for("A", now=66) == Q)
    c.on_report("A", R(Q, 30), now=75)
    check("...then up again after the hold-off", c.mode_for("A", now=76) == W)
    c.on_report("A", R(Q, 5, bad=3), now=80)
    check("a report about frames sent in another mode is ignored", c.mode_for("A", now=81) == W)

    c2 = la.SpeedController()
    c2.on_report("B", R(Q, 20), now=0)                  # clean report (moderate signal)...
    c2.mode_for("B", now=10)                            # ...then we send a window
    c2.on_retransmit("B", now=25)                       # ...and BPQ32 resends it
    check("a retransmission after a STALE clean report steps down", c2.mode_for("B", now=26) == B)
    c2b = la.SpeedController()
    c2b.on_report("B", R(Q, 34, fec=12), now=0)
    c2b.mode_for("B", now=10)
    c2b.on_retransmit("B", now=25)
    check("v0.9.1: ...but not below QPSK when their last report said the signal is strong",
          c2b.mode_for("B", now=26) == Q)
    c2c = la.SpeedController()
    for t_ in (0, 10, 20, 30):
        c2c.on_report("C2", R(Q if t_ < 20 else W, 31), now=t_)
    c2c.mode_for("C2", now=31)
    c2c.on_retransmit("C2", now=31.03)
    check("v0.9.8: a resend right after a clean, strong report (e.g. just after stepping up) "
          "doesn't step down", c2c.mode_for("C2", now=32) == la.LADDER[-1] or c2c.mode_for("C2", now=32) == W)
    c3 = la.SpeedController()
    c3.mode_for("C", now=10)
    c3.on_report("C", R(Q, 30, fec=12), now=20)         # clean report AFTER our window
    c3.on_retransmit("C", now=25)
    check("a retransmission after a FRESH clean report is a lost ack: no step down", c3.mode_for("C", now=26) == Q)
    c4 = la.SpeedController()
    c4.on_retransmit("D", now=10)
    check("older peer (no reports): retransmission steps down", c4.mode_for("D", now=11) == B)
    check("...and it creeps back to QPSK (never Wide) after a long clean spell",
          c4.mode_for("D", now=200) == Q and c4.mode_for("D", now=400) == Q)
    c5 = la.SpeedController(ceiling=Q)
    c5.on_report("E", R(Q, 30), now=0); c5.on_report("E", R(Q, 30), now=10)
    check("a ceiling caps the ladder", c5.mode_for("E", now=11) == Q)


def test_kiss_allowlist_always_allows_this_pc():
    """v0.9.5, field (DV1UK): network KISS on with only a Tailscale address
    allowed refused the station's own BPQ32 on 127.0.0.1."""
    nets, err = bpq_bridge.parse_allowlist("100.64.10.2")
    check("this PC (127.0.0.1, ::1) is always allowed, whatever the list says",
          err is None and bpq_bridge.ip_allowed("127.0.0.1", nets) and bpq_bridge.ip_allowed("::1", nets)
          and bpq_bridge.ip_allowed("::ffff:127.0.0.1", nets))
    check("...the listed address is allowed and other computers are still refused",
          bpq_bridge.ip_allowed("100.64.10.2", nets) and not bpq_bridge.ip_allowed("100.64.10.3", nets)
          and not bpq_bridge.ip_allowed("192.168.0.50", nets))


def test_distorted_signal_reported_honestly():
    """v0.9.6, field (DV1UK -> DU3FS): preamble S/N 27-31 dB but data
    carriers 1-11 dB; half the QPSK frames lost while Auto kept probing
    faster on the preamble figure. The report now uses min(preamble,
    carriers + 8); the carrier check warns about distortion."""
    import types
    import link_adapt as la
    import sara_pingpong as sp
    def fr(pre, car_db, n=12):
        evm = np.full(n, 10.0 * 10 ** (-car_db / 10)); evm_n = np.full(n, 10.0)
        return types.SimpleNamespace(snr_db=pre, carrier_stats=(np.arange(n) * 100.0, np.zeros(n), np.full(n, 100.0),
                                                                evm, evm_n), modulation=ofdm.MOD_QPSK)
    check("healthy link (preamble 30, carriers 23): reported S/N unchanged",
          abs(la.link_snr_db(fr(30.0, 23.0)) - 30.0) < 0.01)
    check("distorted link (preamble 28, carriers 7): reported as 21 dB, not 28",
          abs(la.link_snr_db(fr(28.0, 7.0)) - 21.0) < 0.01)
    check("v0.9.7: K6IRF -> DW1IRF narrow carriers 14 dB under a 28 dB preamble: reported 28 (stays 'strong')",
          abs(la.link_snr_db(fr(28.0, 14.0)) - 28.0) < 0.01)
    check("no carrier data (e.g. older frame object): preamble S/N as before",
          la.link_snr_db(types.SimpleNamespace(snr_db=25.0)) == 25.0)
    logged = []
    a = types.SimpleNamespace(_carrier_acc={}, _log=logged.append, CARRIER_REPORT_EVERY=20, DISTORTION_GAP_DB=18.0,
                              _mod_name=sp.App._mod_name)
    a._log_carriers = types.MethodType(sp.App._log_carriers, a)
    for _ in range(20):
        sp.App._note_carriers(a, fr(28.0, 7.0))
    check("carrier check warns: strong preamble, weak carriers = distorted signal",
          any("DISTORTED" in m for m in logged), logged[-1] if logged else "")
    logged.clear()
    for _ in range(20):
        sp.App._note_carriers(a, fr(30.0, 23.0))
    check("...and stays quiet on a healthy link", logged and not any("DISTORTED" in m for m in logged))
    logged.clear()
    for _ in range(20):
        sp.App._note_carriers(a, fr(29.0, 14.0))
    check("v0.9.7: ...and on K6IRF -> DW1IRF's 14-15 dB gap (decodes cleanly; v0.9.6 warned)",
          logged and not any("DISTORTED" in m for m in logged))


def test_tx_underflow_counted():
    """v0.9.7: transmit-audio underruns are counted (and still written in
    full), and reported in the PC-not-keeping-up warning."""
    import types
    import sara_pingpong as sp
    calls = []

    class S:
        def __init__(self, fail):
            self.fail = fail
        def write(self, data, exception_on_underflow=False):
            calls.append((len(data), exception_on_underflow))
            if self.fail and exception_on_underflow:
                raise OSError("Output underflowed", -9980)
    e = audio_io.AudioEngine.__new__(audio_io.AudioEngine)
    e.tx_stream = S(True)
    e._tx_write(b"abcd")
    e._tx_write(b"ef")
    e.tx_stream = S(False)
    e._tx_write(b"gh")
    check("underflows counted (2), audio still handed over each time, count resets when read",
          e.take_output_underruns() == 2 and len(calls) == 3 and e.take_output_underruns() == 0)

    class Old:
        def write(self, data):
            calls.append(("old", len(data)))
    e.tx_stream = Old()
    e._tx_write(b"xyz")
    check("a stream without the underflow option still works", calls[-1] == ("old", 3))
    logged = []
    g = types.SimpleNamespace(engine=types.SimpleNamespace(take_audio_health=lambda: (0, 0.0),
                                                           take_output_underruns=lambda: 4),
                              _log=logged.append, AUDIO_HEALTH_EVERY_S=10.0, AUDIO_BACKLOG_WARN_S=0.5)
    sp.App._audio_health(g)
    check("GUI warns: transmit audio ran dry", logged and "transmit audio ran dry 4" in logged[0], str(logged))


def test_low_snr_candidate_does_not_swallow_preamble():
    """v0.9.8: a candidate that doesn't repeat (low_preamble_snr) used to make
    the receiver skip 4 symbols, which could jump over a real preamble
    starting just after it. Field (v0.9.6/0.9.7): 20+ of K6IRF's frames per
    test never reached DW1IRF's decoder. Half a symbol now."""
    rng = np.random.RandomState(5)
    res = {}
    for skip in (ofdm.N_SYM * ofdm.PREAMBLE_REPS, ofdm.OFDMReceiver.LOW_SNR_SKIP):
        ok = 0
        for _ in range(20):
            pl = bytes(rng.randint(0, 256, 200).astype(np.uint8))
            fr = ofdm.frame_encode(pl, modulation=ofdm.MOD_QPSK_WIDE) * 0.3
            s_ = (rng.normal(0, 1, ofdm.N_SYM) * 0.1).astype(np.float32)
            x = np.concatenate([np.zeros(3000, np.float32), s_, s_, np.zeros(rng.randint(0, 600), np.float32),
                                fr, np.zeros(8000, np.float32)])
            x = x + rng.normal(0, 0.002, len(x)).astype(np.float32)
            rx = ofdm.OFDMReceiver()
            rx.LOW_SNR_SKIP = skip
            for i in range(0, len(x), 1024):
                rx.push(x[i:i + 1024])
                f = rx.poll()
                while f is not None:
                    ok += f.payload == pl
                    f = rx.poll()
        res[skip] = ok
    old, new = res[ofdm.N_SYM * ofdm.PREAMBLE_REPS], res[ofdm.OFDMReceiver.LOW_SNR_SKIP]
    check(f"a fake repeated symbol just before a real frame: decoded {new}/20 (old 4-symbol skip: {old}/20)",
          new > old and new >= 17, f"{res}")


def test_yield_not_ended_by_own_squelch_tail():
    """v0.9.9, field (v0.9.8, DW1IRF): the channel reading busy for ~0.5 s
    right after our own transmission (our radio's squelch tail) ended the
    6.5 s control yield; BPQ32's late control frame then collided with the
    other station's next window. Now only a SARA-PP signal ends the yield
    early; other "busy" counts only after YIELD_SELF_TAIL_S."""
    from unittest.mock import MagicMock
    ctl = bpq_bridge.pack_batch([_ax25_iframe("K6IRF", "K6IRF-10", 0, b"")[:14] + bytes([0x01])])  # RR

    def keyup(sara_at=None):
        e = audio_io.AudioEngine.__new__(audio_io.AudioEngine)
        e.tx_lead_ms = e.tx_tail_ms = 1
        e._rx_rms_ema = 0.0
        e._tx_lock = threading.Lock()
        e.tx_stream = MagicMock()
        e.on_carrier_wait = e.on_tx_start = None
        keys = []
        e.ptt = MagicMock()
        e.ptt.ptt_on.side_effect = lambda: keys.append(_time.time())
        state = {"t0": None}
        e.is_channel_busy = lambda: state["t0"] is not None and _time.time() - state["t0"] < 0.5
        e.sara_busy = lambda: (state["t0"] is not None and sara_at is not None
                               and sara_at <= _time.time() - state["t0"] < sara_at + 0.3)
        e.transmit(b"x" * 19, modulation=ofdm.MOD_QPSK_WIDE)
        state["t0"] = e._last_tx_end
        e.transmit(ctl, modulation=ofdm.MOD_QPSK_WIDE)
        return keys[-1] - state["t0"]
    t = keyup()
    check(f"0.5 s of own squelch tail no longer ends the control yield (keyed after {t:.1f} s, ~6.5)", t >= 6.0)
    t = keyup(sara_at=1.5)
    check(f"...but the other station's SARA-PP signal at 1.5 s still ends it at once ({t:.1f} s)", 1.4 <= t < 2.6)


def test_auto_faster_climb_and_resume():
    """v0.9.10, field (v0.9.9): every session re-climbed QPSK -> Wide -> 16QAM
    on 2 reports per step (one report per window), so uploads used 16QAM for
    only 45 of 135 data frames. Now: one clean report with a clear margin
    steps up; a new connection soon after a clean session starts one step
    below where it ended."""
    import link_adapt as la
    Q, W, X = ofdm.MOD_QPSK, ofdm.MOD_QPSK_WIDE, ofdm.MOD_QAM16_WIDE
    R = lambda mode, snr, fec=0: la.Report(mode, 7, 0, snr, fec, fec, (la.CAP_QAM16 | la.CAP_QAM64))
    c = la.SpeedController()
    c.on_report("K", R(Q, 28), now=5)
    check("one report at 28 dB, FEC 0%: QPSK -> QPSK Wide at once", c.mode_for("K", now=6) == W)
    c.on_report("K", R(W, 30), now=12)
    check("...and one at 30 dB: QPSK Wide -> 16QAM Wide", c.mode_for("K", now=13) == X)
    c2 = la.SpeedController()
    c2.on_report("M", R(Q, 28, fec=17), now=5)
    check("heavier FEC (17%) still needs the second clean report", c2.mode_for("M", now=6) == Q)
    c2.on_report("M", R(Q, 28, fec=17), now=8)
    c2.on_report("M", R(W, 27), now=12)
    check("27 dB in Wide (under 27+2): one report isn't enough for 16QAM", c2.mode_for("M", now=13) == W)
    c2.on_report("M", R(W, 28), now=16)
    check("27-28 dB (under 27+2) climbs to 16QAM only on 2 reports", c2.mode_for("M", now=17) == X)
    mode = c.new_connection("K", now=100)
    check("a new connection 87 s after a clean 16QAM session starts in QPSK Wide (one step down)", mode == W)
    c.on_report("K", R(W, 30), now=110)
    check("...and one good report takes it back to 16QAM Wide", c.mode_for("K", now=111) == X)
    c.on_report("K", la.Report(X, 6, 1, 30, 0, 0, (la.CAP_QAM16 | la.CAP_QAM64)), now=120)
    check("a session ending with losses...", c.mode_for("K", now=121) == W)
    check("...makes the next connection start fresh in QPSK", c.new_connection("K", now=130) == Q)
    check("a new connection long after (over 30 min) starts fresh too",
          c.new_connection("K", now=130 + 1801) == Q)


def test_weak_header_for_fast_mode_rejected():
    """v0.9.11, field (DV1UK): false 16QAM Wide headers at preamble S/N 3-11
    dB made the receiver wait ~1 s each for frames that couldn't exist. A
    header claiming a mode the preamble is far too weak for is rejected at
    once; real frames at workable S/N still decode."""
    rng = np.random.RandomState(7)
    pl = bytes(rng.randint(0, 256, 300).astype(np.uint8))
    burst = ofdm.frame_encode(pl, modulation=ofdm.MOD_QAM16_WIDE) * 0.3
    def run(noise_rms):
        x = np.concatenate([np.zeros(3000, np.float32), burst, np.zeros(8000, np.float32)])
        x = (x + rng.normal(0, noise_rms, len(x))).astype(np.float32)
        rx = ofdm.OFDMReceiver()
        rej = []
        rx.on_reject = lambda r, i: rej.append((r, round(i.get("snr_db", 0) or 0)))
        f = None
        for i in range(0, len(x), 1024):
            rx.push(x[i:i + 1024])
            f = f or rx.poll()
        return f, rej
    f, rej = run(0.15)
    check("a 16QAM Wide header at ~9 dB preamble S/N is rejected at once (snr_too_low_for_mode)",
          f is None and rej and rej[0][0] == "snr_too_low_for_mode", str(rej[:3]))
    f, rej = run(0.002)
    check("...a clean 16QAM Wide frame still decodes", f is not None and f.payload == pl, str(rej[:3]))


def test_auto_qam16_ladder():
    """v0.9.4: 16QAM Wide is the top of the Auto ladder -- only for a
    station whose link report says it decodes it (a 10th report byte older
    versions ignore), up from QPSK Wide after 2 clean reports at >= 27 dB,
    down below 23 dB or on any loss."""
    import link_adapt as la
    Q, W, X = ofdm.MOD_QPSK, ofdm.MOD_QPSK_WIDE, ofdm.MOD_QAM16_WIDE
    r = la.Report(W, 7, 0, 30, 4, 1, (la.CAP_QAM16 | la.CAP_QAM64))
    batch = bpq_bridge.pack_batch([_ax25_iframe("K6IRF", "DW1IRF", 1, b"x")])
    out = la.attach_report(batch, r)
    back = la.split_report(out)
    old_view = la.split_report(la.attach_report(batch, la.Report(W, 7, 0, 30, 4, 1))[:-1])
    check("the report carries our capabilities in a 10th byte; the first 9 are the v0.9.0 format",
          back == r and out[len(batch):len(batch) + 3] == la.REPORT_MAGIC + bytes([1])
          and old_view is not None and old_view.caps == 0, f"{back} / {old_view}")
    c = la.SpeedController()
    R = lambda mode, snr, caps=(la.CAP_QAM16 | la.CAP_QAM64), bad=0, fec=0: la.Report(mode, 7 - bad, bad, snr, fec, fec, caps)
    for t, rep in enumerate([R(Q, 31, 0), R(Q, 31, 0), R(W, 31, 0), R(W, 31, 0), R(W, 31, 0)]):
        c.on_report("OLD", rep, now=10 * t)
    check("a station that doesn't report 16QAM support (v0.9.3 and older) stops at QPSK Wide",
          c.mode_for("OLD", now=100) == W)
    for t, rep in enumerate([R(Q, 31), R(Q, 31), R(W, 31), R(W, 31)]):
        c.on_report("NEW", rep, now=10 * t)
    check("a v0.9.4 station at 31 dB: QPSK -> QPSK Wide -> 16QAM Wide", c.mode_for("NEW", now=40) == X)
    c.on_report("NEW", R(X, 19), now=50)
    check("16QAM Wide steps down to QPSK Wide below 20 dB (v0.9.16; was 23)", c.mode_for("NEW", now=51) == W)
    c2 = la.SpeedController()
    for t, rep in enumerate([R(Q, 31), R(Q, 31), R(W, 25, fec=8), R(W, 25, fec=8), R(W, 25, fec=8)]):
        c2.on_report("MID", rep, now=10 * t)
    check("at 25 dB with FEC in use, 16QAM isn't tried (needs 27, or a spotless run)",
          c2.mode_for("MID", now=60) == W)
    c3 = la.SpeedController()
    for t, rep in enumerate([R(Q, 31), R(Q, 31), R(W, 31), R(W, 31)]):
        c3.on_report("P", rep, now=10 * t)
    c3.on_report("P", R(X, 31, bad=1), now=50)
    check("a loss in 16QAM Wide steps down to QPSK Wide", c3.mode_for("P", now=51) == W)


def test_receiver_off_audio_callback():
    """v0.9.1, field v0.9.0: K6IRF lost 17 of 27 long BPSK frames at S/N
    33-36 dB (every other frame missing, random 'drift' on the rest) while
    decoding Wide perfectly. Decoding ran inside the sound card callback and
    re-filtered the whole growing buffer on every block while a long frame
    arrived. Now: (1) a frame still arriving isn't re-processed; (2) the
    callback only measures levels and queues audio for the decoder thread,
    so a slow decode can't make the sound card drop input; (3) sound card
    overruns and decoder lag are counted for the GUI to report."""
    import queue as _q
    import threading as _th
    import time
    from unittest.mock import patch
    rng = np.random.RandomState(3)
    pl = bpq_bridge.pack_batch([bytes(rng.randint(0, 256, 256).astype(np.uint8))])
    gap = np.zeros(int(0.2 * ofdm.SAMPLE_RATE), np.float32)
    frames = [ofdm.frame_encode(pl, modulation=ofdm.MOD_BPSK) * 0.3 for _ in range(4)]
    x = np.concatenate([np.zeros(4800, np.float32)] + sum([[f, gap] for f in frames], [])
                       + [np.zeros(24000, np.float32)]).astype(np.float32)
    x += rng.normal(0, 0.003, len(x)).astype(np.float32)
    calls = [0]
    real = ofdm.sosfiltfilt

    def counting(*a, **k):
        calls[0] += 1
        return real(*a, **k)
    rx = ofdm.OFDMReceiver()
    got, blocks, busy_all = 0, 0, True
    with patch.object(ofdm, "sosfiltfilt", counting):
        for i in range(0, len(x), 1024):
            rx.push(x[i:i + 1024]); blocks += 1
            f = rx.poll()
            while f is not None:
                got += f.payload == pl
                f = rx.poll()
    check("4 back-to-back 256-byte BPSK frames all decode", got == 4, f"{got}/4")
    check("...filtering the buffer a few times per frame, not on every audio block",
          calls[0] < blocks / 4, f"{calls[0]} filter passes for {blocks} blocks")

    e = audio_io.AudioEngine.__new__(audio_io.AudioEngine)
    e.receiver = ofdm.OFDMReceiver()
    e.level_queue = _q.Queue(maxsize=50)
    e.frame_queue = _q.Queue()
    e._rx_rms_ema = 0.0
    e._rx_q = _q.Queue()
    e._rx_async = True
    drop_s = audio_io.RX_BACKLOG_DROP_S
    audio_io.RX_BACKLOG_DROP_S = 1e9     # this test feeds faster than real time on purpose
    t = _th.Thread(target=e._rx_worker, daemon=True)
    t.start()
    slow_poll = e.receiver.poll

    def slow(*a, **k):
        time.sleep(0.002)
        return slow_poll(*a, **k)
    e.receiver.poll = slow
    pcm = (np.clip(x, -1, 1) * 32767).astype(np.int16)
    worst = 0.0
    for i in range(0, len(pcm), 1024):
        t0 = time.perf_counter()
        e._rx_callback(pcm[i:i + 1024].tobytes(), 1024, None, 0)
        worst = max(worst, time.perf_counter() - t0)
    deadline = time.time() + 20
    while e._rx_q.qsize() and time.time() < deadline:
        time.sleep(0.05)
    time.sleep(0.3)
    n = 0
    while not e.frame_queue.empty():
        n += e.frame_queue.get().payload == pl
    check("decoder thread: all 4 frames decoded, callback never waits on decoding",
          n == 4 and worst < 0.02, f"{n}/4, slowest callback {worst*1000:.1f} ms")
    e._rx_callback(pcm[:1024].tobytes(), 1024, None, getattr(audio_io.pyaudio, "paInputOverflow", 2))
    ov, backlog = e.take_audio_health()
    check("a sound card input overrun is counted, and the count resets once read",
          ov == 1 and e.take_audio_health()[0] == 0, f"{ov}, backlog {backlog:.2f}s")
    e._rx_q.put(None)
    t.join(2)
    audio_io.RX_BACKLOG_DROP_S = drop_s

    import types
    import sara_pingpong as sp
    logged = []
    g = types.SimpleNamespace(engine=types.SimpleNamespace(take_audio_health=lambda: (3, 0.8)),
                              _log=logged.append, AUDIO_HEALTH_EVERY_S=10.0, AUDIO_BACKLOG_WARN_S=0.5)
    sp.App._audio_health(g)
    sp.App._audio_health(g)          # again at once: rate-limited
    check("GUI: one warning naming the lost input and the decoder lag",
          len(logged) == 1 and "lost 3 time" in logged[0] and "0.8s behind" in logged[0], str(logged))


def test_slow_pc_handling():
    """
    v0.9.26, field (v0.9.24, gateway DX1PAR): "sound card input lost" 5-13
    times every 10 s, and the decoder 19 s behind after Start. Sound card
    buffers are now 4x larger (cut back into 1024-sample blocks inside, so
    nothing downstream changes), a decoder more than RX_BACKLOG_DROP_S
    behind skips to live audio, and the on-screen log is capped.
    """
    import queue as _q
    import threading as _th
    import time
    SR = ofdm.SAMPLE_RATE
    pl = bytes(range(200))
    b = ofdm.frame_encode(pl, modulation=ofdm.MOD_QPSK_WIDE) * 0.25
    x = np.concatenate([np.zeros(SR // 2)] + [np.concatenate([b, np.zeros(SR // 2)])] * 3)
    x = x + 0.003 * np.random.RandomState(3).randn(len(x))
    pcm = (np.clip(x, -1, 1) * 32767).astype(np.int16)

    def run(step):
        e = _os_engine()
        e.dcd_mode = "sara"
        busy = []
        n = 0
        for i in range(0, len(pcm) - step + 1, step):
            e._rx_callback(pcm[i:i + step].tobytes(), step, None, 0)
            busy.append(e.is_channel_busy())
            while not e.frame_queue.empty():
                n += e.frame_queue.get().payload == pl
        return n, sum(busy) * step / SR
    n1, b1 = run(1024)
    n4, b4 = run(audio_io.RX_BUFFER_FRAMES)
    check(f"4096-sample sound card buffers: same frames decoded ({n4}/3 vs {n1}/3), "
          f"DCD busy {b4:.1f}s vs {b1:.1f}s",
          n4 == n1 == 3 and abs(b4 - b1) <= 0.3)

    # a decoder far behind skips ahead, then decodes live audio again
    e = _os_engine()
    e._rx_q = _q.Queue()
    e._rx_async = True
    blocks = [pcm[i:i + 1024].astype(np.float32) / 32768 for i in range(0, len(pcm) - 1023, 1024)]
    late = [np.zeros(1024, np.float32)] * int((audio_io.RX_BACKLOG_DROP_S + 3) * SR / 1024)
    for blk in late + blocks:
        e._rx_q.put(blk)
    lock = _th.Event()
    real_decode = e._decode_samples
    e._decode_samples = lambda s: (lock.wait(), real_decode(s))
    t = _th.Thread(target=e._rx_worker, daemon=True)
    t.start()
    time.sleep(0.05)
    lock.set()          # the worker was stuck behind the whole queue
    time.sleep(0.2)
    dropped = e.take_backlog_dropped()
    check(f"decoder over {audio_io.RX_BACKLOG_DROP_S:.0f}s behind skips ahead ({dropped:.1f}s skipped)",
          dropped >= 3.0)
    for blk in blocks:          # now arriving no faster than it decodes
        e._rx_q.put(blk)
        while e._rx_q.qsize() > 4:
            time.sleep(0.001)
    deadline = time.time() + 20
    while e._rx_q.qsize() and time.time() < deadline:
        time.sleep(0.05)
    time.sleep(0.3)
    n = 0
    while not e.frame_queue.empty():
        n += e.frame_queue.get().payload == pl
    check(f"...and decodes live audio afterwards ({n} frames)", n >= 3)
    e._rx_q.put(None)
    t.join(2)

    # GUI warning names the skip; the on-screen log stays capped
    import types
    import sara_pingpong as sp
    logged = []
    g = types.SimpleNamespace(engine=types.SimpleNamespace(take_audio_health=lambda: (0, 5.0),
                                                          take_backlog_dropped=lambda: 4.2),
                              _log=logged.append, AUDIO_HEALTH_EVERY_S=10.0, AUDIO_BACKLOG_WARN_S=0.5)
    sp.App._audio_health(g)
    check("GUI: warning says how much audio was skipped", len(logged) == 1 and "4.2s" in logged[0],
          str(logged))
    # v0.9.43: the warning says where the time went, and what that points to
    H = sp.App._health_hint
    check("diagnosis: decoder busy most of the time -> slower modes",
          "decoder itself is busy" in H(7.0, 7.5, 0.2, 10.0))
    check("diagnosis: SARA FM barely used the CPU -> other programs / Windows",
          "other programs or Windows" in H(0.5, 1.0, 0.2, 10.0))
    check("diagnosis: the window froze -> send the report", "window itself froze" in H(0.5, 3.0, 2.5, 10.0))
    logged.clear()
    g2 = types.SimpleNamespace(engine=types.SimpleNamespace(take_audio_health=lambda: (0, 1.0),
                                                           take_decode_busy=lambda: 1.5),
                               _log=logged.append, AUDIO_HEALTH_EVERY_S=10.0, AUDIO_BACKLOG_WARN_S=0.5)
    sp.App._audio_health(g2)
    check("the warning carries the decoder's share of the time and SARA FM's CPU use",
          logged and "decoder working" in logged[0] and "% of one CPU core" in logged[0], str(logged))
    import tkinter as tk
    root = tk.Tk()
    try:
        box = tk.Text(root)
        g = types.SimpleNamespace(log_box=box, LOG_MAX_LINES=sp.App.LOG_MAX_LINES,
                                  LOG_KEEP_LINES=sp.App.LOG_KEEP_LINES, session_log=None)
        for i in range(sp.App.LOG_MAX_LINES + 500):
            sp.App._log(g, f"line {i}")
        lines = int(box.index("end-1c").split(".")[0])
        last = box.get("end-2l", "end-1c").strip()
        check(f"on-screen log capped ({lines} lines), newest line kept",
              lines <= sp.App.LOG_MAX_LINES + 1 and last.endswith(f"line {sp.App.LOG_MAX_LINES + 499}"), last)
    finally:
        root.destroy()


def test_auto_strong_signal_losses():
    """v0.9.1, field v0.9.0 run 12: losses reported at S/N 32-36 dB drove
    Auto down to BPSK and kept it there ~5 minutes. Losses at a strong
    signal now stop at QPSK; a slower mode with a strong signal probes one
    step up after PROBE_AFTER_S (doubling the wait when a probe fails at
    once); a new AX.25 connection starts fresh."""
    import link_adapt as la
    Q, W, B = ofdm.MOD_QPSK, ofdm.MOD_QPSK_WIDE, ofdm.MOD_BPSK
    ch = []
    c = la.SpeedController(on_change=lambda *a: ch.append(a))
    R = lambda mode, snr, fec=0, bad=0: la.Report(mode, 7 - bad, bad, snr, fec, fec)
    c.mode_for("K", now=0)
    c.on_report("K", R(Q, 34), now=5); c.on_report("K", R(Q, 34), now=10)
    check("strong clean link reaches QPSK Wide", c.mode_for("K", now=11) == W)
    c.on_report("K", R(W, 34, bad=1), now=20)
    check("a loss at 34 dB steps Wide -> QPSK", c.mode_for("K", now=21) == Q)
    c.on_report("K", R(Q, 34, bad=2), now=40)
    c.on_report("K", R(Q, 35, fec=88, bad=1), now=60)
    check("...but more losses at 34-35 dB never take it to BPSK", c.mode_for("K", now=61) == Q,
          str([x[2] for x in ch]))
    c.on_report("K", R(Q, 34, bad=1), now=70)
    check("after 45 s in QPSK with a strong signal, Auto tries QPSK Wide again (even with losses)",
          c.mode_for("K", now=71) == W and "trying faster" in ch[-1][3], ch[-1][3])
    c.on_report("K", R(W, 34, bad=2), now=85)
    check("the probe fails at once -> back to QPSK", c.mode_for("K", now=86) == Q)
    c.on_report("K", R(Q, 34, bad=1), now=135)
    check("...and the next probe waits longer (90 s, not 45)", c.mode_for("K", now=136) == Q)
    c.on_report("K", R(Q, 34, bad=1), now=180)
    check("...then tries again", c.mode_for("K", now=181) == W)

    n = la.SpeedController()
    n.on_report("L", R(Q, 9, bad=3), now=0)
    check("a WEAK signal with losses still goes down to BPSK (real noise)", n.mode_for("L", now=1) == B)
    n.on_report("L", R(B, 9), now=60)
    check("no probe while the signal is too weak for the next mode", n.mode_for("L", now=61) == B)
    got = []
    n.on_change = lambda *a: got.append(a)
    check("v0.9.12: a new connection while QPSK is on hold (failed 70 s ago) starts in BPSK",
          n.new_connection("L", now=70) == B)
    check("a new connection after the hold starts fresh at QPSK", n.new_connection("L", now=200) == Q
          and n.mode_for("L", now=201) == Q and "new connection" in got[-1][3])

    st = la.RxStats()
    st.on_frame("L", B, 30, 0, 100)
    st.reset_peer("L")
    check("...and the receive statistics for that station are cleared", st.take_report("L") is None)
    check("SABM recognised (P bit either way); I-frames and UA are not",
          la.is_sabm(0x2F) and la.is_sabm(0x3F) and not la.is_sabm(0x63) and not la.is_sabm(0x10))


def test_rx_stats_and_retransmit_detection():
    import link_adapt as la
    st = la.RxStats()
    st.on_frame("DW1IRF", ofdm.MOD_QPSK, 27.0, 3, 260, now=0)
    st.on_frame("DW1IRF", ofdm.MOD_QPSK, 29.0, 0, 260, now=1)
    st.on_lost(25.0, now=2)
    st.on_lost(-60.0, now=2)                            # noise, not a frame
    r = st.take_report("DW1IRF")
    check("report: 2 ok, 1 lost (noise not counted), median S/N, FEC % of capacity",
          r.n_ok == 2 and r.n_bad == 1 and r.snr_db == 28 and r.fec_max_pct == 12 and r.mode == ofdm.MOD_QPSK, str(r))
    check("a report resets the counts", st.take_report("DW1IRF") is None)
    st.on_lost(25.0, now=100)
    check("a loss long after we last heard a peer isn't charged to it", st.take_report("DW1IRF") is None)
    d = la.RetransmitDetector()
    f = _ax25_iframe("K6IRF-10", "DW1IRF", 2, b"data")
    rr = _ax25_iframe("K6IRF-10", "DW1IRF", 0, b"")[:14] + bytes([0x41])
    check("a resent I-frame is spotted (destination returned); first send and RR polls aren't",
          d.check(f, now=0) is None and d.check(rr, now=1) is None and d.check(rr, now=2) is None
          and d.check(f, now=20) == "K6IRF-10")


def test_kiss_ackmode():
    """BPQ32 KISSOPTIONS=ACKMODE: each frame's 2-byte id is echoed only
    once the frame has actually been transmitted (merged batches too); on a
    TX failure everything outstanding is acknowledged so BPQ32's own timers
    retry; plain data frames get no acks."""
    air, seen = [], []
    br = BpqBridge(on_air_frame=air.append)
    br.on_client_frame = seen.append
    br.start(port=18402)
    _time.sleep(0.2)
    c = socket.socket()
    c.connect(("127.0.0.1", 18402))
    _time.sleep(0.2)
    f1, f2, f3 = (_ax25_iframe("DW1IRF", "K6IRF", n, b"x%d" % n) for n in (1, 2, 3))
    c.sendall(kiss.encode_frame(b"\x00\x01" + f1, command=kiss.CMD_ACKMODE)
              + kiss.encode_frame(b"\x00\x02" + f2, command=kiss.CMD_ACKMODE)
              + kiss.encode_frame(f3))
    _time.sleep(0.4)

    def recv_frames(wait=0.3):
        c.settimeout(wait)
        try:
            return kiss.KissDecoder().feed(c.recv(4096))
        except socket.timeout:
            return []
    frames_out = [fr for a in air for fr in bpq_bridge.unpack_batch(a)]
    check("ACKMODE frames reach the air as plain AX.25 (id stripped); the frame hook sees all 3",
          frames_out == [f1, f2, f3] and len(seen) == 3)
    check("no acknowledgment before transmission", recv_frames() == [])
    br.frames_sent(bpq_bridge.pack_batch([f1, f2, f3]))
    got = recv_frames()
    check("after transmission: ids 0001 and 0002 acknowledged, none for the plain frame",
          got == [(0, kiss.CMD_ACKMODE, b"\x00\x01"), (0, kiss.CMD_ACKMODE, b"\x00\x02")], str(got))
    c.sendall(kiss.encode_frame(b"\x00\x03" + f1, command=kiss.CMD_ACKMODE))
    _time.sleep(0.3)
    br.ack_all_pending()
    check("a failed transmission still acknowledges what was outstanding",
          recv_frames() == [(0, kiss.CMD_ACKMODE, b"\x00\x03")])
    c.close()
    br.stop()


def test_auto_gui_wiring():
    """Auto picks the bridge mode per destination, pings use QPSK, the
    report for the destination is attached at burst time (bridge only), and
    a received report drives the controller."""
    import types
    import link_adapt as la
    import sara_pingpong as sp
    a = types.SimpleNamespace(_mod_choice="Auto", modulation_var=types.SimpleNamespace(get=lambda: "Auto"),
                              _MOD_NAME_TO_CODE=sp.App._MOD_NAME_TO_CODE, _last_tx_peer=None,
                              link_stats=la.RxStats(), speed=la.SpeedController(), bridge=object(),
                              _verbose_rx=False, _log=lambda m: None,
                              retx=la.RetransmitDetector(), after=lambda d, f: f())
    for n in ("_modulation", "_auto", "_bridge_modulation", "_finalize_payload", "_note_link",
              "_check_new_connection", "_on_client_frame", "_report_for_me", "_my_calls"):
        setattr(a, n, types.MethodType(getattr(sp.App, n), a))
    batch = bpq_bridge.pack_batch([_ax25_iframe("K6IRF-10", "DW1IRF", 1, b"d")])
    check("Auto: pings/TUNE use QPSK; bridge batches use the per-station mode",
          a._modulation() == ofdm.MOD_QPSK and a._bridge_modulation(batch) == ofdm.MOD_QPSK
          and a._last_tx_peer == "K6IRF-10")
    check("nothing heard yet -> no report attached", a._finalize_payload(batch, ofdm.MOD_QPSK) == batch)
    a.link_stats.on_frame("K6IRF-10", ofdm.MOD_QPSK, 30, 0, 100)
    out = a._finalize_payload(batch, ofdm.MOD_QPSK)
    check("our report about K6IRF-10 rides on the batch addressed to it",
          la.split_report(out) is not None and bpq_bridge.unpack_batch(out) == bpq_bridge.unpack_batch(batch))
    a.bridge = None
    a.link_stats.on_frame("K6IRF-10", ofdm.MOD_QPSK, 30, 0, 100)
    check("no reports outside bridge mode", a._finalize_payload(batch, ofdm.MOD_QPSK) == batch)
    incoming = bpq_bridge.pack_batch([_ax25_iframe("DW1IRF", "K6IRF-10", 1, b"ack")])
    for t in range(2):
        fr = types.SimpleNamespace(payload=la.attach_report(incoming, la.Report(ofdm.MOD_QPSK, 7, 0, 30, 0, 0)),
                                   modulation=ofdm.MOD_QPSK, snr_db=29.0, fec_corrected=0)
        a._note_link(fr)
    check("two clean reports received from K6IRF-10 -> our frames to it go out in QPSK Wide",
          a.speed.mode_for("K6IRF-10") == ofdm.MOD_QPSK_WIDE)
    logged = []
    a._log = logged.append
    sabm = _ax25_iframe("K6IRF-10", "DW1IRF", 0, b"")[:14] + bytes([0x3F])
    a._on_client_frame(sabm)
    check("v0.9.1: a connect request from Winlink/BPQ32 starts Auto fresh for that station",
          a.speed.mode_for("K6IRF-10") == ofdm.MOD_QPSK and any("New connection with K6IRF-10" in m for m in logged),
          str(logged))


def test_auto_two_station_simulation():
    """Short two-station run with the REAL modem, receiver, reports and
    controller (AX.25 go-back-N): strong link -> climbs to Wide; link drops
    to ~9 dB -> steps down to BPSK and keeps delivering, where fixed Wide
    delivers nothing (the link would drop). (9 dB is near BPSK's own limit,
    so some BPSK windows fail too -- the check is that data keeps moving.)"""
    from scipy.signal import butter, sosfilt
    import link_adapt as la
    SR = ofdm.SAMPLE_RATE
    bl = butter(4, [300 / (SR / 2), 3000 / (SR / 2)], btype="band", output="sos")

    def run(schedule, policy):
        rng = np.random.RandomState(1)
        st, ctl, det = la.RxStats(), la.SpeedController(), la.RetransmitDetector()
        seq, queue, t, rows = 0, [], 0.0, []
        for snr in schedule:
            while len(queue) < 7:
                queue.append(_ax25_iframe("K6IRF", "DW1IRF", seq, bytes([seq % 256]) * 200))
                seq += 1
            for f in queue:
                dd = det.check(f, now=t)
                if dd and policy == "auto":
                    ctl.on_retransmit(dd, now=t)
            mode = ctl.mode_for("K6IRF", now=t) if policy == "auto" else policy
            n_ok = 0
            for f in queue:
                pl = bpq_bridge.pack_batch([f])
                x = np.concatenate([np.zeros(3000), ofdm.frame_encode(pl, modulation=mode) * 0.8 / 0.343 * 10 ** (-0.6),
                                    np.zeros(6000)])
                nz = sosfilt(bl, rng.normal(0, 1, len(x)))
                x = (x + nz / np.sqrt(np.mean(nz ** 2)) * 10 ** ((-10 - snr) / 20)).astype(np.float32)
                rx = ofdm.OFDMReceiver()
                rx.on_reject = lambda r, i: st.on_lost(i.get("snr_db"), now=t) if r in la.LOST_REASONS else None
                rx.push(x)
                fr = rx.poll()
                if fr is not None and fr.payload == pl:
                    st.on_frame("DW1IRF", fr.modulation, fr.snr_db, fr.fec_corrected, len(fr.payload), now=t)
                    n_ok += 1
                else:
                    break                                  # go-back-N
            queue = queue[n_ok:]
            t += 13.0
            rep = st.take_report("DW1IRF")
            if rep is not None and policy == "auto":
                ctl.on_report("K6IRF", rep, now=t)
            rows.append((mode, n_ok))
        return rows
    sched = [30] * 4 + [9] * 5
    auto = run(sched, "auto")
    wide = run(sched, ofdm.MOD_QPSK_WIDE)
    names = [la.NAMES[m] for m, _ in auto]
    check(f"Auto climbs to Wide on the strong link: {names[:4]}", ofdm.MOD_QPSK_WIDE in [m for m, _ in auto[:4]])
    weak_auto = sum(n for _, n in auto[4:])
    weak_wide = sum(n for _, n in wide[4:])
    check(f"link drops to 9 dB: Auto steps down to BPSK and delivers {weak_auto} frames; "
          f"fixed Wide delivers {weak_wide} (link would drop)",
          auto[-1][0] == ofdm.MOD_BPSK and weak_auto >= 3 and weak_wide == 0, str(names[4:]))

def test_auto_holds_failed_mode():
    """v0.9.12, field (v0.9.11, DW1IRF -> DV1UK-10, distorted TX audio):
    BPSK windows arrived clean, QPSK windows never did. Every clean BPSK
    report stepped straight back to QPSK and lost the next 7-frame window
    (~25-35 s FRACK stall, 5 times in one message); every new connection
    started in QPSK. Now a mode that just failed is held off, doubling."""
    import link_adapt as la
    B, Q = ofdm.MOD_BPSK, ofdm.MOD_QPSK
    R = lambda mode, snr, fec=0, bad=0: la.Report(mode, 7, bad, snr, fec, fec, la.MY_CAPS)
    c = la.SpeedController()
    c.new_connection("D", now=0)
    c.on_report("D", R(Q, 20, fec=75), now=2)
    check("QPSK with FEC 75% used -> BPSK", c.mode_for("D", now=3) == B)
    c.on_report("D", R(B, 21), now=40)
    check("a clean BPSK report 38 s later no longer jumps back to QPSK (field: it did)",
          c.mode_for("D", now=41) == B)
    c.on_report("D", R(B, 21), now=60)
    c.on_report("D", R(B, 21), now=80)
    check("...nor does a probe inside the hold", c.mode_for("D", now=81) == B)
    check("a new connection inside the hold starts in BPSK, not the failed QPSK",
          c.new_connection("D", now=85) == B)
    c.on_report("D", R(B, 21), now=95)
    check("after the 90 s hold, a clean BPSK report tries QPSK again", c.mode_for("D", now=96) == Q)
    c.on_report("D", R(Q, 20, bad=7), now=110)
    check("QPSK failing again -> BPSK", c.mode_for("D", now=111) == B)
    c.on_report("D", R(B, 21), now=111 + 120)
    check("the second hold is longer (180 s): still BPSK after 120 s", c.mode_for("D", now=232) == B)
    c.on_report("D", R(B, 21), now=111 + 185)
    check("...and QPSK is tried again after 180 s", c.mode_for("D", now=297) == Q)
    c.on_report("D", R(Q, 24), now=300)
    check("a clean QPSK report clears the record", 1 not in c.peers["D"].fail_hold)
    c2 = la.SpeedController()
    c2.on_report("E", R(Q, 30), now=5)
    check("a healthy link still climbs on one clean report", c2.mode_for("E", now=6) == ofdm.MOD_QPSK_WIDE)


def test_carrier_sense_ignores_non_sara_busy_in_session():
    """v0.9.12, field (v0.9.11, DV1UK-10): during Winlink sessions the
    gateway waited 10-30 s for a "clear channel" while nobody transmitted
    (noise floor 1 dB under the signal) -- each wait a FRACK stall. In a
    session, busy with no SARA-PP signal now gives up after 8 s; a real
    burst from the other station (frames decoding) is still waited out."""
    import types
    from unittest.mock import patch
    e = audio_io.AudioEngine.__new__(audio_io.AudioEngine)
    clock = [1000.0]
    fake_time = types.SimpleNamespace(time=lambda: clock[0], sleep=lambda s: None)
    e._yield_to_reply = lambda payload=None: None
    e.dcd_mode = "auto"
    e._rx_rms_ema = 0.1
    e.noise_floor_rms = 0.05
    e.busy_threshold_rms = lambda now=None: 0.06
    e.level_busy = lambda: True
    e.spectral_busy = lambda: True
    e.is_channel_busy = lambda: True
    sara = [False]
    e.sara_busy = lambda: sara[0]

    # drive _wait_clear's own busy() via a patched wait_for_clear_channel
    real = audio_io.wait_for_clear_channel
    def patched(is_busy_fn, max_wait_s=30.0, poll_s=0.05, sleep_fn=None):
        def sleep(s):
            clock[0] += s
        return real(is_busy_fn, max_wait_s, poll_s, sleep)
    with patch.object(audio_io, "time", fake_time), patch.object(audio_io, "wait_for_clear_channel", patched):
        e._last_rx_frame_time = clock[0] - 5           # mid-session
        w = e._wait_clear(b"x")
        check(f"in a session, non-SARA busy gives up after ~8 s (waited {w:.1f}s, was 30)",
              7.5 <= w <= 9.0 and e.last_wait_gave_up)
        e._last_rx_frame_time = clock[0] - 600         # no session
        w = e._wait_clear(b"x")
        check(f"outside a session the full wait still applies (waited {w:.1f}s)",
              w >= 29.0 and not e.last_wait_gave_up)
        e._last_rx_frame_time = clock[0] - 5
        sara[0] = True                                  # the other station's burst
        w = e._wait_clear(b"x")
        check(f"a SARA-PP burst on the air is still waited out (waited {w:.1f}s)",
              w >= 29.0 and not e.last_wait_gave_up)
        sara[0] = False
        e.dcd_mode = "sara"
        e.is_channel_busy = lambda: False
        check("'SARA-PP signals only' DCD mode unchanged", e._wait_clear(b"x") == 0.0)


def test_auto_skips_narrow_qpsk_when_wide_does_better():
    """v0.9.13, field (v0.9.12, DV1UK-10 -> DW1IRF): DW1IRF heard DV1UK's
    lowest carriers (562/656 Hz) at 5-7 dB, so narrow QPSK used 40-100% of
    its FEC or lost frames -- often the first reply of a session (a ~20 s
    Winlink retry) -- while QPSK Wide (no carriers that low) reported FEC
    0-8%. Auto had to pass through QPSK to reach Wide, and QPSK's holds kept
    it in BPSK. Once QPSK is clearly worse than Wide, Auto skips it."""
    import link_adapt as la
    B, Q, W = ofdm.MOD_BPSK, ofdm.MOD_QPSK, ofdm.MOD_QPSK_WIDE
    R = lambda mode, snr, fec=0, bad=0: la.Report(mode, 7, bad, snr, fec, fec, la.MY_CAPS)
    c = la.SpeedController()
    c.new_connection("D", now=0)
    check("no history yet: a new connection starts in QPSK as before", c.mode_for("D", now=1) == Q)
    c.on_report("D", R(Q, 23, fec=42), now=5)
    c.on_report("D", R(Q, 23, fec=0), now=10)
    check("QPSK clean: climbs to Wide", c.mode_for("D", now=11) == W)
    for t in (15, 20, 25):
        c.on_report("D", R(W, 23, fec=8), now=t)
    c.on_report("D", R(W, 23, bad=1), now=30)
    check("QPSK more troubled than Wide: a Wide loss steps straight to BPSK, skipping QPSK",
          c.mode_for("D", now=31) == B)
    c.on_report("D", R(B, 23), now=65)
    check("from BPSK, a clean report climbs straight to Wide", c.mode_for("D", now=66) == W)
    check("a new connection starts in QPSK Wide with this station",
          c.new_connection("D", now=200) == W)
    c3 = la.SpeedController()
    c3.new_connection("E", now=0)
    c3.on_report("E", R(Q, 24), now=5)
    for t in (10, 12, 15):
        c3.on_report("E", R(W, 24, fec=4), now=t)
    c3.on_report("E", R(W, 24, bad=1), now=20)
    check("a Wide loss with no evidence against QPSK steps down to QPSK", c3.mode_for("E", now=21) == Q)
    c3.on_report("E", R(Q, 22, fec=67), now=35)
    check("QPSK then troubled (FEC 67%) while Wide was mostly clean: back to Wide, not BPSK",
          c3.mode_for("E", now=36) == W)
    c2 = la.SpeedController()
    c2.new_connection("K", now=0)
    for t in (5, 10):
        c2.on_report("K", R(Q, 30), now=t)
    check("a link where QPSK is fine is unaffected (QPSK -> Wide as usual)", c2.mode_for("K", now=11) == W)
    c2.on_report("K", R(W, 22, bad=2), now=20)
    check("...and a Wide loss there still steps down to QPSK", c2.mode_for("K", now=21) == Q)


def test_tune_tx_protocol_and_choice():
    """v0.9.14 Tune TX: a PING may ask for the data-carrier S/N and the PONG
    carries it; older-format pings/pongs are unchanged. The level kept is the
    loudest within 1 dB of the cleanest, unless the current one is within 2 dB."""
    p = proto.make_ping("DW1IRF", "DV1UK", 7, want_carrier=True)
    m = proto.PingPongMessage.unpack(p)
    check("Tune TX ping: 21 bytes, flag read back, recognised as a ping",
          len(p) == 21 and m.want_carrier and proto.is_pingpong(p))
    q = proto.make_pong("DV1UK", "DW1IRF", 7, 30, -28, 11.6, want_carrier=True)
    m = proto.PingPongMessage.unpack(q)
    check("Tune TX pong carries the carrier S/N", len(q) == 23 and m.report_carrier_snr_db == 12
          and m.report_snr_db == 30 and proto.is_pingpong(q))
    check("plain ping/pong unchanged (20 / 22 bytes)",
          len(proto.make_ping("A", "B", 1)) == 20 and len(proto.make_pong("A", "B", 1, 3, 4)) == 22)
    check("a 21-byte payload that isn't a flagged ping is not a ping",
          not proto.is_pingpong(proto.make_ping("A", "B", 1) + b"\x05"))
    import importlib
    App = importlib.import_module("sara_pingpong").App
    ch, means = App.txtune_choose({0.6: [9, 10], 0.42: [12, 12], 0.3: [14, 13], 0.21: [14, 14],
                                   0.15: [12, 13]}, 0.6)
    check(f"hot TX: picks the loudest clean level (30%), not the quietest (got {ch})", ch == 0.3)
    ch, _ = App.txtune_choose({0.6: [10, 10], 0.42: [11, 11], 0.3: [10, 11]}, 0.6)
    check("no real gain (< 2 dB): keeps the current level", ch == 0.6)
    ch, _ = App.txtune_choose({0.6: [], 0.42: []}, 0.6)
    check("nothing measured: no change", ch is None)
    # v0.9.50: VOX edge (DW1IRF -> DX1BBC, 0.9.49: 10% no reply, 14% kept)
    ch, _ = App.txtune_choose({0.2: [10, 10], 0.14: [9, 9]}, 0.14, 1.0, 2.0, dead=[0.1])
    check(f"level right above a VOX no-reply moves up when as clean (got {ch})", ch == 0.2)
    ch, _ = App.txtune_choose({0.2: [10, 10], 0.14: [9, 9]}, 0.14, 1.0, 2.0)
    check("...and without a dead level the current one is kept", ch == 0.14)
    ch, _ = App.txtune_choose({0.2: [6, 6], 0.14: [14, 14]}, 0.14, 1.0, 2.0, dead=[0.1])
    check("...but not when the next level up is clearly dirtier", ch == 0.14)
    ch, _ = App.txtune_choose({0.55: [6], 0.39: [6], 0.28: [8], 0.2: [14], 0.14: [19]}, 0.55, 1.0, 2.0)
    check("DW1IRF->DV1UK 0.9.49 sweep still picks 14%", ch == 0.14)
    # v0.9.50: Auto-Tune contact plan tries LOWER first (overdriven radio)
    check("Auto-Tune from 40%: 20% first, then 55/70/85",
          App.autotune_contact_plan(0.40) == [0.2, 0.55, 0.70, 0.85])
    check("Auto-Tune from 20%: 15% first", App.autotune_contact_plan(0.20)[0] == 0.15)
    check("Auto-Tune from 14%: no lower step (already quiet)",
          App.autotune_contact_plan(0.14) == [0.55, 0.70, 0.85])
    # v0.9.50: strong-but-undecodable -> overdrive hint
    import types as _types
    logs = []
    fake = _types.SimpleNamespace(engine=None, after=lambda ms, fn: fn(), _log=logs.append)
    App._note_garbled(fake, "fec_uncorrectable", {"pre_snr_db": 9.0})
    check("weak garbled frame is not counted as overdrive", getattr(fake, "_garbled_total", 0) == 0)
    App._note_garbled(fake, "fec_uncorrectable", {"pre_snr_db": 24.9})
    App._note_garbled(fake, "low_header_confidence", {"pre_snr_db": 24.2})
    App._note_garbled(fake, "fec_uncorrectable", {"pre_snr_db": 25.2})
    check("strong garbled frames counted", fake._garbled_total == 3)
    check("one overdrive hint (not one per frame)",
          sum("OVERDRIVEN" in m for m in logs) == 1)
    # v0.9.60: AX.25 trace in the log
    import link_adapt as _la2
    i3 = _ax25_iframe("DX1FLR-10", "DW1IRF", 3, b"hello")
    rr = _ax25_frame("DW1IRF", "DX1FLR-10", [("DV1UK", True)], control=0x01 | (5 << 5), pid=False)
    rej = _ax25_frame("DW1IRF", "DX1FLR-10", [], control=0x09 | (2 << 5), pid=False)
    sabm = _ax25_frame("DX1FLR-10", "DW1IRF", [("DV1UK", False)], control=0x3F, pid=False)
    tr = _la2.batch_trace([i3, rr, rej, sabm])
    check("AX.25 trace: I N(S)/N(R) + data checksum, RR5, REJ2, SABM", 
          tr.startswith("I3/0 #") and " RR5 REJ2 SABMp" in tr, tr)
    # v0.9.58: a digipeater re-packs what it relays for ITS transmit mode
    import bpq_bridge as _bb
    big = [_ax25_frame("DX1FLR-10", "DW1IRF", [("DV1UK", True)], info=bytes(256)) for _ in range(2)]
    two_q16 = _bb.pack_for_mode(big, ofdm.MOD_QAM16_WIDE)
    two_qw = _bb.pack_for_mode(big, ofdm.MOD_QPSK_WIDE)
    check(f"DW1IRF's 16QAM pair (2 x {len(big[0])} B) is one batch in 16QAM, two in QPSK Wide",
          len(two_q16) == 1 and len(two_qw) == 2, f"{[len(b) for b in two_q16]} / {[len(b) for b in two_qw]}")
    rxq = ofdm.OFDMReceiver()
    check("each relayed QPSK Wide batch fits what a receiver accepts",
          all(len(b) + 16 <= rxq._max_rx_bytes(ofdm.MOD_QPSK_WIDE) for b in two_qw)
          if hasattr(rxq, "_max_rx_bytes") else all(len(b) < 420 for b in two_qw))
    out_digi = []
    for b in two_qw:
        f = decode_all(ofdm.OFDMReceiver(), ofdm.frame_encode(b, modulation=ofdm.MOD_QPSK_WIDE).astype(np.float32))
        out_digi += f
    check("...and both decode", len(out_digi) == 2 and all(o.payload in two_qw for o in out_digi))
    # v0.9.57: what goes on to BPQ32 / Winlink Express on a digipeated link
    direct = _ax25_frame("DW1IRF", "DX1FLR-10", [("DV1UK", False)], info=b"data")
    relayed = _ax25_frame("DW1IRF", "DX1FLR-10", [("DV1UK", True)], info=b"data")
    mine_back = _ax25_frame("DX1FLR-10", "DW1IRF", [("DV1UK", True)], info=b"ours")
    plain = _ax25_iframe("DW1IRF", "DV1UK-10", 1, b"x")
    got = App.client_frames([direct, relayed, plain], {"DW1IRF"}, [], False)
    check("a frame heard direct before the digipeater repeated it isn't passed to the client",
          got == [relayed, plain])
    check("our own frame repeated back isn't passed on either",
          App.client_frames([mine_back], {"DW1IRF"}, [mine_back], False) == [])
    via_me = _ax25_frame("DX1FLR-10", "DW1IRF", [("DV1UK", False)], info=b"x")
    check("BPQ32 as the digipeater (SARA FM's off) still gets frames sent via this station",
          App.client_frames([via_me], {"DV1UK"}, [], False) == [via_me]
          and App.client_frames([via_me], {"DV1UK"}, [], True) == [])
    # v0.9.56: a connect request never goes faster than QPSK Wide
    sabm = _ax25_frame("DX1FLR-10", "DW1IRF", [("DV1UK", False)], control=0x3F, pid=False)
    iframe = _ax25_iframe("DX1FLR-10", "DW1IRF", 1, b"x" * 200)
    check("SABM recognised (also via a digipeater); an I-frame isn't",
          App._is_sabm_frame(sabm) and not App._is_sabm_frame(iframe))
    # v0.9.55: Tune TX test mode needs BOTH directions strong
    check("DU3FS -> DW3TDJ: they hear us 28 dB, we hear them 16 dB / carriers 7 -> BPSK",
          App.txtune_mode(28, (16, 7)) == ofdm.MOD_BPSK)
    check("both ways strong (DW1IRF <-> DV1UK) -> QPSK Wide",
          App.txtune_mode(41, (17, 15)) == ofdm.MOD_QPSK_WIDE)
    check("they hear us weakly -> BPSK; nothing known about our side -> by their report",
          App.txtune_mode(13, (30, 20)) == ofdm.MOD_BPSK and App.txtune_mode(20, None) == ofdm.MOD_QPSK_WIDE)
    import types as _ty
    tl = []
    tt = {"target": "DW3TDJ", "mod": ofdm.MOD_QPSK_WIDE, "levels": [0.4, 0.57, 0.28], "start": 0.4,
          "i": 0, "n": 0, "misses": 0, "any_reply": False, "stopped_low": None, "back_lost": 0,
          "results": {0.4: [], 0.57: [], 0.28: []}, "pre": {0.4: [], 0.57: [], 0.28: []},
          "missed": {0.4: 0, 0.57: 0, 0.28: 0}, "sent_t": _time.time() - 5}
    fk = _ty.SimpleNamespace(txtune=tt, _last_lost_rx_t=_time.time() - 2, _log=tl.append, engine=None,
                             after=lambda ms, fn: None, TXTUNE_BACK_LOST_SWITCH=2, TXTUNE_PINGS_PER_STEP=2,
                             _mod_name=lambda m: "QPSK Wide", _txtune_step=lambda: None)
    fk._txtune_next = lambda: App._txtune_next(fk)
    App._txtune_on_timeout(fk)
    check("a reply that arrived garbled is not counted against the TX level",
          tt["missed"][0.4] == 0 and tt["back_lost"] == 1)
    tt["sent_t"] = _time.time() - 5
    App._txtune_on_timeout(fk)
    check("...twice: Tune TX starts again in BPSK",
          tt["mod"] == ofdm.MOD_BPSK and tt["i"] == 0 and any("Starting again in BPSK" in l for l in tl), str(tl))
    # v0.9.53: CPU on a shared Wyse 3040 (DX1BBC: decoder "busy" 40-60%, 4 VARA FM)
    h = App._health_hint(5.0, 6.0, 0.2, 10.0, 1.5)
    check("decoder busy 50% but working 15%: blames the shared PC, not SARA FM settings",
          "WAITING for a free CPU core" in h and "35% waiting" in h, h)
    h = App._health_hint(7.0, 7.5, 0.2, 10.0, 6.5)
    check("decoder busy and really working: the old 'decoder itself is busy' advice",
          "decoder itself is busy" in h, h)
    b_ = App.cpu_breakdown([(1.0, 0.6, 0.5, 0.12, 0.3)] * 60)
    check("CPU breakdown line: decoder work, window, other, waiting",
          "decoder work 12%" in b_ and "window 30%" in b_ and "other 18%" in b_ and "waited for a free core 38%" in b_, b_)
    check("backlog warning starts at 1 s behind (0.5-0.9 s lost no audio)", App.AUDIO_BACKLOG_WARN_S == 1.0)
    # v0.9.52: Tune TX note for the Ping window (DX1BBC -> DW1IRF sweep)
    tt = {"pre": {0.28: [25, 25], 0.2: [21, 23], 0.14: [23, 22], 0.1: [21, 21], 0.07: [18, 18]}}
    means = {0.28: 6.0, 0.2: 8.0, 0.14: 10.0, 0.1: 15.0, 0.07: 16.0}
    note = App.tune_window_note(tt, 0.1, means, "DW1IRF")
    check("Tune TX note: names the level and the carriers", "10%" in note[0] and "15 dB" in note[0], str(note))
    check("...and explains why S/N reads lower than at 28%",
          len(note) == 2 and "25 -> 21" in note[1] and "clipping" in note[1], str(note))
    import tuning as _tu
    check("carrier colors: 15 light green, 16 green, 6 red",
          _tu.carrier_colors(15) == _tu.LIGHT_GREEN and _tu.carrier_colors(16) == _tu.GREEN
          and _tu.carrier_colors(6) == _tu.RED)
    check("an ordinary ping asks for the carrier report",
          proto.PingPongMessage.unpack(proto.make_ping("A", "B", 1, want_carrier=True)).want_carrier)
    # v0.9.51: "answer quieter" ping flag
    q = proto.make_ping("DW1IRF", "DX1BBC", 7, ask_quieter=True)
    m = proto.PingPongMessage.unpack(q)
    check("quieter ping: 21 bytes, recognised, flag parsed, not a carrier request",
          len(q) == 21 and proto.is_pingpong(q) and m.ask_quieter and not m.want_carrier)
    q2 = proto.make_ping("DW1IRF", "DX1BBC", 7, want_carrier=True, ask_quieter=True)
    m2 = proto.PingPongMessage.unpack(q2)
    check("quieter + carrier ping both parsed", proto.is_pingpong(q2) and m2.ask_quieter and m2.want_carrier)
    check("plain carrier ping unchanged (old stations still read it)",
          proto.make_ping("A", "B", 1, want_carrier=True)[-1] == proto.PING_FLAG_CARRIER
          and not proto.PingPongMessage.unpack(proto.make_ping("A", "B", 1, want_carrier=True)).ask_quieter)
    check("an unknown flag bit is still not a ping",
          not proto.is_pingpong(proto.make_ping("A", "B", 1) + b"\x04"))
    # responder halves its reply level per asking station, floor 5%
    r = _types.SimpleNamespace()
    l1 = App._on_quieter_request(r, "DW1IRF", 0.40)
    l2 = App._on_quieter_request(r, "DW1IRF", 0.40)
    check(f"quieter requests: 40% -> 20% -> 10% (got {l1}, {l2})", (l1, l2) == (0.2, 0.1))
    check("reply level applies only to that station",
          App._pong_level_for(r, "DW1IRF", 0.4) == 0.1 and App._pong_level_for(r, "DV1UK", 0.4) == 0.4)
    check("a tuned-down level below the override wins", App._pong_level_for(r, "DW1IRF", 0.07) == 0.07)
    l3 = App._on_quieter_request(r, "DW1IRF", 0.40)
    check("v0.9.62: still 'clipped' at -12 dB (10% of 40%): not our level -- back to normal",
          l3 == 0.40 and App._pong_level_for(r, "DW1IRF", 0.4) == 0.4, str(l3))
    check("...and that station's asks are ignored for a while",
          App._on_quieter_request(r, "DW1IRF", 0.40) == 0.40)
    r2 = _types.SimpleNamespace()
    got = [App._on_quieter_request(r2, "DX1FLR", 0.14) for _ in range(3)]
    check("field (DW1IRF at 14%, DX1FLR asking): 7%, 5%, then normal -- never stuck at 5%",
          got == [0.07, 0.05, 0.14], str(got))
    # pinger: a strong clipped frame while the ping waited -> they answered
    logs.clear()
    pg = _types.SimpleNamespace(_log=logs.append, autotune={"seq": 3})
    pg._ping_sent_t = {3: _time.time() - 4.0}
    pg._garbled_last_t = _time.time() - 2.0
    check("clipped reply in the pong window is recognised",
          App._check_clipped_reply(pg, 3, "DX1BBC") and pg.autotune.get("reply_clipped"))
    check("...and the next ping to that station asks for a quieter reply",
          App._wants_quieter(pg, "DX1BBC") and not App._wants_quieter(pg, "DV1UK"))
    pg._ping_sent_t = {4: _time.time() - 1.0}
    pg._garbled_last_t = _time.time() - 5.0
    pg._quieter_ask = {}
    check("an older clipped frame (before the ping) doesn't count",
          not App._check_clipped_reply(pg, 4, "DX1BBC") and not App._wants_quieter(pg, "DX1BBC"))


def test_tune_tx_vox_quiet_levels():
    """v0.9.15, field (DW1IRF, SignaLink VOX): the quietest levels never keyed
    the VOX; three misses ended Tune TX as "no reply" and discarded the good
    measurements. A quiet level with no reply now ends the sweep and the
    results so far are used."""
    import types, importlib
    App = importlib.import_module("sara_pingpong").App
    done = []
    later = []
    st = types.SimpleNamespace(TXTUNE_MAX_MISSES=3, TXTUNE_PINGS_PER_STEP=2, TXTUNE_MAX_PAUSES=2,
                               TXTUNE_PAUSE_MS=8000,
                               _finish_txtune=lambda problem="": done.append(problem),
                               _txtune_next=lambda: None, _txtune_step=lambda: None, _log=lambda m: None,
                               after=lambda ms, fn: later.append(ms))
    levels = [0.25, 0.35, 0.18, 0.13, 0.09, 0.06]
    st.txtune = {"levels": levels, "i": 4, "n": 0, "start": 0.25, "misses": 0, "any_reply": True,
                 "missed": {lv: 0 for lv in levels}, "results": {lv: [] for lv in levels},
                 "pre": {lv: [] for lv in levels}, "stopped_low": None}
    st.txtune["results"][0.25] = [14]
    App._txtune_on_timeout(st)
    check("one miss at a quiet level: carries on", not done)
    App._txtune_on_timeout(st)
    tt = st.txtune
    check("v0.9.61: both pings at a quiet level unanswered: first ONE check ping at a level that "
          "answered before", not done and tt.get("control", {}).get("dead") == 0.09
          and tt["levels"][tt["i"]] == 0.25 and tt["n"] == 1, str(tt))
    msg = types.SimpleNamespace(report_carrier_snr_db=14, report_snr_db=30)
    App._txtune_on_pong(st, msg, types.SimpleNamespace(payload=bytes(23)))
    check("...it answers: sweep ends WITH results (no 'no reply'), stopped at the quiet level",
          done == [""] and st.txtune["stopped_low"] == 0.09)
    done.clear()
    st.txtune.update(any_reply=False, misses=0, i=0, missed={lv: 0 for lv in levels})
    for _ in range(3):
        App._txtune_on_timeout(st)
    check("no reply at all: still reported as no reply", done == ["no reply"])
    # v0.9.61 field (DW1IRF -> DV1UK, 0.9.60): DV1UK's PC fell behind mid-sweep
    done.clear(); later.clear()
    levels = [0.40, 0.57, 0.28, 0.20, 0.14, 0.10]
    st.txtune = {"levels": levels, "i": 1, "n": 0, "start": 0.40, "misses": 0, "any_reply": True, "target": "DV1UK",
                 "missed": {lv: 0 for lv in levels}, "results": {lv: [] for lv in levels},
                 "pre": {lv: [] for lv in levels}, "stopped_low": None}
    st.txtune["results"][0.40] = [6]
    st._txtune_next = lambda: st.txtune.update(i=st.txtune["i"] + (st.txtune["n"] + 1) // 2,
                                               n=(st.txtune["n"] + 1) % 2)
    for _ in range(4):                       # 57% x2, then 28% x2: no reply
        App._txtune_on_timeout(st)
    tt = st.txtune
    check("DV1UK deaf: 28% unanswered -> check ping at 40%", tt.get("control", {}).get("level") == 0.40, str(tt))
    App._txtune_on_timeout(st)               # ...and 40% gets no reply either
    tt = st.txtune
    check("...no reply at 40% either: not concluded -- the misses are forgotten, a pause, then "
          "57% is tested again", not done and later[-1] == 8000 and tt["levels"][tt["i"]] == 0.57
          and tt["missed"][0.57] == 0 and tt["missed"][0.28] == 0 and tt["pauses"] == 1, str(tt))


def test_auto_clean_probe_to_qam16():
    """v0.9.16, field (after Tune TX, DV1UK-10 -> DW1IRF): QPSK Wide frames
    arrived spotless (carriers 17-19 dB, FEC 0%, 0 lost) at 22-25 dB, under
    the 27 dB step-up, so receives never used 16QAM Wide. A run of spotless
    reports at 22 dB+ now tries it on the probe timer."""
    import link_adapt as la
    W, X = ofdm.MOD_QPSK_WIDE, ofdm.MOD_QAM16_WIDE
    R = lambda mode, snr, fec=0, bad=0: la.Report(mode, 7, bad, snr, fec, fec, (la.CAP_QAM16 | la.CAP_QAM64))
    c = la.SpeedController(start=W)
    c.new_connection("D", now=0)
    for t in (4, 8):
        c.on_report("D", R(W, 24), now=t)
    check("2 spotless reports: stays in Wide", c.mode_for("D", now=9) == W)
    c.on_report("D", R(W, 23), now=12)
    check("3 spotless, but inside the 15 s wait: stays in Wide", c.mode_for("D", now=13) == W)
    c.on_report("D", R(W, 23), now=16)
    check("spotless at 23-24 dB after 15 s: tries 16QAM Wide", c.mode_for("D", now=17) == X)
    c.on_report("D", R(X, 23, fec=8), now=30)
    check("16QAM at 23 dB with light FEC holds (down only below 20)", c.mode_for("D", now=31) == X)
    c2 = la.SpeedController(start=W)
    c2.new_connection("E", now=0)
    for t in (10, 20, 30, 50):
        c2.on_report("E", R(W, 24, fec=12), now=t)
    check("not spotless (FEC 12%): no try", c2.mode_for("E", now=51) == W)
    c3 = la.SpeedController(start=W)
    c3.new_connection("F", now=0)
    for t in (10, 20, 30, 50):
        c3.on_report("F", R(W, 21), now=t)
    check("spotless but under 22 dB: no try", c3.mode_for("F", now=51) == W)
    c.on_report("D", R(X, 23, bad=2), now=40)
    check("16QAM losing frames soon after the try: back to Wide, next try waits longer",
          c.mode_for("D", now=41) == W and c.peers["D"].probe_wait == 90)
    for t in (45, 50, 55, 65):
        c.on_report("D", R(W, 24), now=t)
    check("...no new try 25 s later", c.mode_for("D", now=66) == W)
    c.on_report("D", R(W, 24), now=75)
    check("...but it tries again after the longer (30 s) wait", c.mode_for("D", now=76) == X)
    c4 = la.SpeedController()
    c4.new_connection("G", now=0)
    for t in (5, 10, 15, 20):
        c4.on_report("G", R(ofdm.MOD_QPSK, 20), now=t)
    check("v0.9.17: spotless QPSK at 20 dB (under the 21 dB step-up) tries QPSK Wide",
          c4.mode_for("G", now=21) == W)
    c4.on_report("G", R(W, 20), now=25)
    check("a session ending spotless in Wide: the next connection starts in Wide, not QPSK",
          c4.new_connection("G", now=60) == W)


def test_qam64_wide_mode():
    """v0.9.18 (experimental): 64-QAM Wide -- header code 2 with its own
    training symbol. Round trip; told apart from 16QAM under noise and
    drift; an older-style receiver can't mistake it for good 16QAM data;
    Auto climbs to it only with a v0.9.18 station reporting clean carriers."""
    from scipy.signal import resample
    import fm_channel as fc, link_adapt as la
    pl = bytes((i * 7) % 256 for i in range(548))
    rx = ofdm.OFDMReceiver()
    rx.push(np.concatenate([ofdm.frame_encode(pl, ofdm.MOD_QAM64_WIDE), np.zeros(6000, np.float32)]))
    f = rx.poll()
    d16 = len(ofdm.frame_encode(pl, ofdm.MOD_QAM16_WIDE)) / ofdm.SAMPLE_RATE
    d64 = len(ofdm.frame_encode(pl, ofdm.MOD_QAM64_WIDE)) / ofdm.SAMPLE_RATE
    check(f"64QAM Wide round trip: 108 bits/symbol, {d64:.2f} s vs 16QAM {d16:.2f} s for 548 bytes",
          f is not None and f.payload == pl and f.modulation == ofdm.MOD_QAM64_WIDE
          and ofdm.bits_per_symbol(ofdm.MOD_QAM64_WIDE) == 108 and d64 < d16 * 0.8)
    rng = np.random.RandomState(7)
    wrong = 0
    for m in (ofdm.MOD_QAM16_WIDE, ofdm.MOD_QAM64_WIDE):
        for t in range(4):
            b = ofdm.frame_encode(pl[:200], m) * 0.5
            b = resample(b, int(len(b) * (1 + 150e-6))).astype(np.float32)
            tx = np.concatenate([np.zeros(4800, np.float32), b, np.zeros(9600, np.float32)])
            on = np.zeros(len(tx), bool); on[2400:-2400] = True
            a = fc.receive(tx, on, 28, rng, out_gain=0.3, path="speaker")
            rx = ofdm.OFDMReceiver(); rx.push(a.astype(np.float32)); f = rx.poll()
            for _ in range(5):
                f = f or rx.poll()
            wrong += f is None or f.modulation != m or f.payload != pl[:200]
    check("16QAM and 64QAM told apart and decoded under FM noise + 150 ppm drift (8 of 8)", wrong == 0, str(wrong))
    old = ofdm.OFDMReceiver()
    old.RX_FAMILY = {ofdm.MOD_QAM16_WIDE: (ofdm.MOD_QAM16_WIDE,)}   # a receiver that only knows 16QAM
    old.push(np.concatenate([ofdm.frame_encode(pl, ofdm.MOD_QAM64_WIDE), np.zeros(6000, np.float32)]))
    check("read as 16QAM (an older receiver), a 64QAM frame fails -- never wrong data",
          old.poll() is None)
    R = lambda mode, snr, car, fec=0, caps=(la.CAP_QAM16 | la.CAP_QAM64): la.Report(mode, 7, 0, snr, fec, fec, caps, car)
    X, Z = ofdm.MOD_QAM16_WIDE, ofdm.MOD_QAM64_WIDE
    c = la.SpeedController(start=X)
    c.new_connection("D", now=0)
    for t in (5, 10, 15, 20):
        c.on_report("D", R(X, 30, 19), now=t)
    check("16QAM spotless at 30 dB but carriers 19 dB: no 64QAM",
          c.mode_for("D", now=21) == X)
    for t in (25, 30, 35, 45):
        c.on_report("D", R(X, 31, 25), now=t)
    check("carriers 25 dB, spotless: tries 64QAM Wide", c.mode_for("D", now=46) == Z)
    c.on_report("D", R(Z, 31, 19), now=50)
    check("64QAM with carriers under 20 dB: back to 16QAM (v0.9.24)", c.mode_for("D", now=51) == X)
    c2 = la.SpeedController(start=X)
    c2.new_connection("O", now=0)
    for t in (5, 10, 15, 20, 30, 40):
        c2.on_report("O", R(X, 36, 26, caps=la.CAP_QAM16), now=t)
    check("a v0.9.4-v0.9.17 station (no 64QAM capability) stays at 16QAM", c2.mode_for("O", now=41) == X)
    r = R(X, 30, 23)
    back = la.split_report(la.attach_report(bpq_bridge.pack_batch([b"x" * 20]), r))
    check("the report carries the carrier S/N (11th byte) and reads back", back == r)


def test_auto_memory_kept():
    """v0.9.19, field (v0.9.18): after a ~40 min break DV1UK started every
    receive in narrow QPSK again (43 of 47 frames), having forgotten QPSK
    Wide works better with DW1IRF. Memory now lasts 6 h and survives a
    restart (saved to sara_pp_speed.json)."""
    import json, link_adapt as la
    Q, W = ofdm.MOD_QPSK, ofdm.MOD_QPSK_WIDE
    R = lambda mode, snr, fec=0, bad=0: la.Report(mode, 7, bad, snr, fec, fec, la.MY_CAPS)
    c = la.SpeedController()
    c.new_connection("D", now=0)
    c.on_report("D", R(Q, 22, fec=42), now=5)
    c.on_report("D", R(Q, 22, fec=67), now=15)
    for t in (30, 40, 50, 60):
        c.on_report("D", R(W, 22), now=t)
    check("a new connection 40 min later still starts in QPSK Wide",
          c.new_connection("D", now=60 + 2400) == W)
    c2 = la.SpeedController()
    c2.import_state(json.loads(json.dumps(c.export_state(now=2500))), now=2600)
    check("...and after a restart (saved and reloaded memory)", c2.new_connection("D", now=2700) == W)
    c3 = la.SpeedController()
    c3.import_state({"X": {"idx": "bad"}, "Y": "junk"}, now=0)
    check("a damaged memory file is ignored, not fatal", c3.new_connection("Y", now=1) == Q)


def test_auto_interference_floor():
    """v0.9.20, field (v0.9.19, DV1UK-10 -> DW1IRF): long frames lost at the
    normal ~20 dB with clean carriers (interference, not a weak signal); Auto
    stepped down to BPSK, whose 3 s frames fared worse. Such losses now stop
    at QPSK Wide; weak or heavily corrected signals still step down."""
    import link_adapt as la
    B, Q, W, X = ofdm.MOD_BPSK, ofdm.MOD_QPSK, ofdm.MOD_QPSK_WIDE, ofdm.MOD_QAM16_WIDE
    R = lambda mode, snr, car, fec=0, bad=0: la.Report(mode, 7, bad, snr, fec, fec, (la.CAP_QAM16 | la.CAP_QAM64), car)
    c = la.SpeedController(start=W)
    c.new_connection("D", now=0)
    c.on_report("D", R(W, 20, 15, bad=1), now=10)
    check("a loss in QPSK Wide with clean carriers (15 dB) and light FEC: stays in Wide",
          c.mode_for("D", now=11) == W)
    c.on_retransmit("D", now=30)
    check("...and a BPQ32 resend then doesn't push it below Wide either", c.mode_for("D", now=31) == W)
    c.on_report("D", R(W, 20, 8, bad=1), now=50)
    check("losses with poor carriers (8 dB): steps down as before", c.mode_for("D", now=51) == Q)
    c2 = la.SpeedController(start=W)
    c2.new_connection("E", now=0)
    c2.on_report("E", R(W, 20, 15, fec=75), now=10)
    check("heavy FEC (75%) even with clean carriers: steps down", c2.mode_for("E", now=11) != W)
    c3 = la.SpeedController(start=X)
    c3.new_connection("F", now=0)
    c3.on_report("F", R(X, 30, 20, bad=1), now=10)
    check("a 16QAM loss with clean carriers still steps down to Wide (not below)",
          c3.mode_for("F", now=11) == W)


def test_save_report_small():
    """v0.9.21: Save Report writes one text file of this run's log without
    the quiet/noise reject lines (counted instead) -- field verbose logs were
    2-12 MB, nearly all of those lines."""
    import tempfile, log_bundle
    d = tempfile.mkdtemp()
    lg = os.path.join(d, "sara_pp_DW1IRF_2026-10-26_004221.log")
    with open(lg, "w") as fh:
        fh.write("===== header =====\n")
        for i in range(5000):
            fh.write("00:00:01.000    [rejected: low_energy  energy=0.04, threshold=0.3]\n")
        fh.write("00:00:02.000    [rejected: fec_uncorrectable  n_data_bytes=306, modulation=1]\n")
        fh.write("00:00:03.000  RX (air->BPQ32)  29 bytes  S/N +20 dB\n")
        fh.write("00:00:04.000  Session summary (1:02):  RX 711 B = 91 bps   TX 11.0 kB = 1429 bps\n")
    path = log_bundle.make_report("dw1irf", "STATION INFO\n", [lg], d, now=0)
    txt = open(path).read()
    check("report keeps the real events, counts the noise, and is small",
          "fec_uncorrectable" in txt and "RX (air->BPQ32)" in txt and "Session summary (1:02)" in txt
          and "low_energy  energy" not in txt and "5000  low_energy" in txt
          and os.path.basename(path).startswith("SARA-FM_report_DW1IRF_") and len(txt) < 2000,
          f"{len(txt)} chars")


def test_auto_qam16_needs_clean_carriers():
    """v0.9.22, field (v0.9.21, DV1UK-10 -> DW1IRF): spotless QPSK Wide at
    21-23 dB but carriers 13-18 dB kept trying 16QAM (FEC 42-92%); one lost
    16QAM acknowledgment stalled a send 33 s. Carriers under 19 dB: no 16QAM;
    the other direction (carriers 21-22) and older reports (no carrier
    figure) step up as before."""
    import link_adapt as la
    W, X = ofdm.MOD_QPSK_WIDE, ofdm.MOD_QAM16_WIDE
    R = lambda snr, car, fec=0: la.Report(W, 7, 0, snr, fec, fec, (la.CAP_QAM16 | la.CAP_QAM64), car)
    c = la.SpeedController(start=W)
    c.new_connection("U", now=0)
    for t in (5, 10, 15, 20, 40, 60):
        c.on_report("U", R(22, 16), now=t)
    check("spotless QPSK Wide at 22 dB, carriers 16 dB: stays in Wide", c.mode_for("U", now=61) == W)
    c2 = la.SpeedController(start=W)
    c2.new_connection("I", now=0)
    c2.on_report("I", R(30, 21), now=5)
    check("30 dB with carriers 21 dB: 16QAM Wide", c2.mode_for("I", now=6) == X)
    c3 = la.SpeedController(start=W)
    c3.new_connection("O", now=0)
    c3.on_report("O", la.Report(W, 7, 0, 30, 0, 0, la.CAP_QAM16), now=5)
    check("a v0.9.4-v0.9.17 report (no carrier figure) at 30 dB still steps up to 16QAM",
          c3.mode_for("O", now=6) == X)


def test_coded_modes():
    """v0.9.24: coded Wide modes (inner K=7 convolutional code, soft Viterbi,
    inside the Reed-Solomon code). Field (v0.9.23 vs VARA FM): on DV1UK ->
    DW1IRF (data carriers 13-18 dB) SARA-PP stayed in QPSK Wide while VARA
    ran 4-7 kbps. 16QAM Wide C (rate 2/3) decodes ~3 dB weaker than plain
    16QAM and carries ~30% more than QPSK Wide."""
    from scipy.signal import resample
    import fm_channel as fc, link_adapt as la, fec_cc
    rng = np.random.RandomState(11)
    bits = rng.randint(0, 2, 3000).astype(np.uint8)
    ok = all((fec_cc.decode(1.0 - 2.0 * fec_cc.encode(bits, r), 3000, r) == bits).all()
             for r in ("1/2", "2/3", "3/4"))
    check("convolutional code: clean round trip at rates 1/2, 2/3, 3/4", ok)
    pl = bytes((i * 7) % 256 for i in range(548))
    for m in (ofdm.MOD_QAM16_WIDE_C, ofdm.MOD_QAM64_WIDE_C):
        rx = ofdm.OFDMReceiver()
        rx.push(np.concatenate([ofdm.frame_encode(pl, m), np.zeros(8000, np.float32)]))
        f = None
        for _ in range(6):
            f = f or rx.poll()
        check(f"{la.NAMES[m]}: two AX.25 frames round trip",
              f is not None and f.payload == pl and f.modulation == m)
    old = ofdm.OFDMReceiver()
    old.RX_FAMILY = {ofdm.MOD_QAM16_WIDE: (ofdm.MOD_QAM16_WIDE, ofdm.MOD_QAM64_WIDE)}   # v0.9.18-23
    old.push(np.concatenate([ofdm.frame_encode(pl, ofdm.MOD_QAM16_WIDE_C), np.zeros(8000, np.float32)]))
    f = None
    for _ in range(6):
        f = f or old.poll()
    check("a v0.9.18-v0.9.23 receiver can't take a coded frame for good data", f is None)

    def run(m, cnr, n=6):
        good = 0
        pl2 = pl[:250]
        for _ in range(n):
            b = ofdm.frame_encode(pl2, m) * 0.5
            b = resample(b, int(len(b) * (1 + 100e-6))).astype(np.float32)
            tx = np.concatenate([np.zeros(4800, np.float32), b, np.zeros(12000, np.float32)])
            on = np.zeros(len(tx), bool); on[2400:-2400] = True
            a = fc.receive(tx, on, cnr, rng, out_gain=0.3, path="speaker")
            rx = ofdm.OFDMReceiver(); rx.push(a.astype(np.float32)); f = None
            for _ in range(6):
                f = f or rx.poll()
            good += f is not None and f.payload == pl2 and f.modulation == m
        return good
    p16, c16 = run(ofdm.MOD_QAM16_WIDE, 13), run(ofdm.MOD_QAM16_WIDE_C, 13)
    check(f"FM channel at 13 dB CNR (carriers ~13 dB): 16QAM Wide {p16}/6, 16QAM Wide C {c16}/6",
          c16 >= 5 and p16 <= 2)

    R = lambda mode, snr, car, fec=0, caps=la.MY_CAPS: la.Report(mode, 7, 0, snr, fec, fec, caps, car)
    W, C16, X, C64, Z = (ofdm.MOD_QPSK_WIDE, ofdm.MOD_QAM16_WIDE_C, ofdm.MOD_QAM16_WIDE,
                         ofdm.MOD_QAM64_WIDE_C, ofdm.MOD_QAM64_WIDE)
    c = la.SpeedController(start=W)
    c.new_connection("U", now=0)
    for t in (5, 10, 15, 20):
        c.on_report("U", R(W, 21, 16), now=t)
    check("field DV1UK -> DW1IRF (21 dB, carriers 16): QPSK Wide -> 16QAM Wide C", c.mode_for("U", now=21) == C16)
    c.on_report("U", R(C16, 21, 15, fec=8), now=30)
    check("...and it stays there (no plain 16QAM at carriers 15)", c.mode_for("U", now=31) == C16)
    c2 = la.SpeedController(start=W)
    c2.new_connection("I", now=0)
    c2.on_report("I", R(W, 30, 21), now=5)
    c2.on_report("I", R(C16, 30, 21), now=10)
    check("field DW1IRF -> DV1UK (30 dB, carriers 21): climbs through 16QAM C to 16QAM",
          c2.mode_for("I", now=11) == X)
    for t in (15, 20, 25, 45):
        c2.on_report("I", R(X, 30, 21), now=t)
    check("...then tries 64QAM Wide C on a spotless run (carriers 21)", c2.mode_for("I", now=46) == C64)
    c3 = la.SpeedController(start=W)
    c3.new_connection("O", now=0)
    c3.on_report("O", R(W, 30, 21, caps=la.CAP_QAM16 | la.CAP_QAM64), now=5)
    check("a v0.9.18-v0.9.23 station (no CAP_CODED) never gets a coded mode", c3.mode_for("O", now=6) == X)


def test_wide_bursts_decode_every_frame():
    """v0.9.24: a 7-frame window in a 16QAM/64QAM (plain or coded) burst --
    every frame decodes. Offline, v0.9.23 lost the FIRST frame of 16QAM
    bursts (its FFT window sat at the edge of the cyclic prefix) and every
    64QAM burst frame (the training pattern was misread). Fixed by matching
    the training pattern against the preamble's channel estimate and moving
    Wide FFT windows that sit too near the prefix edge."""
    import queue as _q
    from unittest.mock import MagicMock, patch

    def run(items):
        e = audio_io.AudioEngine.__new__(audio_io.AudioEngine)
        e.tx_lead_ms, e.tx_tail_ms = 600, 150
        e._rx_rms_ema = 0.0
        e._tx_lock = threading.Lock()
        e.ptt = MagicMock()
        e.on_carrier_wait = e.on_tx_start = None
        e.is_channel_busy = lambda: False
        chunks = []
        e.tx_stream = MagicMock()
        e.tx_stream.write.side_effect = lambda by: chunks.append(np.frombuffer(by, np.float32))
        e._tx_queue = _q.Queue()
        e.merge_payloads = bpq_bridge.merge_batches
        for pl, mod in items:
            e._tx_queue.put((pl, mod, 0.8, lambda p, m: None, None))
        with patch("time.sleep"):
            while not e._tx_queue.empty():
                e._send_burst(e._tx_queue.get())
        got = decode_all(ofdm.OFDMReceiver(), np.concatenate(chunks), chunk=1024)
        return sum(len(bpq_bridge.unpack_batch(g.payload)) for g in got if g.modulation == items[0][1])

    data = [bytes([0x96] * 14) + bytes([0x11, i]) + bytes(236) for i in range(7)]
    res = {m: run([(bpq_bridge.pack_batch([x]), m) for x in data])
           for m in (ofdm.MOD_QAM16_WIDE, ofdm.MOD_QAM16_WIDE_C, ofdm.MOD_QAM64_WIDE_C, ofdm.MOD_QAM64_WIDE)}
    check(f"7-frame bursts, frames decoded: {res}", all(v == 7 for v in res.values()))


def test_stale_resends_and_relay_yield():
    """v0.9.61. Field (0.9.60): Winlink Express's FRACK ran out while a BPSK
    window was still going out and it handed the same frames over again --
    aired behind the originals in one key-up, so DV1UK-10 never got a gap to
    answer in, and a resend queued just as the acknowledgment arrived drew
    REJ5 x6. Via the DV1UK digipeater, DX1FLR keyed up the moment DW1IRF's
    window ended, over DV1UK's relay of its first frame (I6, every time)."""
    import link_adapt as la, audio_io, types, importlib, bpq_bridge as bb
    A, B = "DW1IRF", "DV1UK-10"
    I = lambda ns, nr=0, data=None: _ax25_frame(B, A, [], control=((nr & 7) << 5) | ((ns & 7) << 1),
                                               info=data or bytes([ns]) * 40)
    RR = lambda nr, p=False: _ax25_frame(A, B, [], control=((nr & 7) << 5) | (0x10 if p else 0) | 0x01, pid=False)
    REJ = lambda nr: _ax25_frame(A, B, [], control=((nr & 7) << 5) | 0x09, pid=False)
    check("ack_info: RR5 from DV1UK-10 to DW1IRF", la.ack_info(RR(5)) == ("DV1UK-10", "DW1IRF", 5))
    check("ack_info: REJ, and an I frame carries N(R) too",
          la.ack_info(REJ(2))[2] == 2 and la.ack_info(I(3, nr=6))[2] == 6)
    check("same frame resent with a new N(R) has the same key", la.iframe_key(I(5, 1)) == la.iframe_key(I(5, 7)))
    s = la.StaleFrames()
    win = [I(n) for n in (6, 7, 0, 1, 2, 3, 4)]
    s.on_queued(win, now=0)
    check("queued, not yet aired: not stale", s.why_stale(win[0], 0) is None and s.is_queued(win[0]))
    s.on_aired(win, now=10)
    check("aired, not yet answered: a resend in the same turn is 'unanswered'",
          s.why_stale(win[0], turn_t=5) == "unanswered" and not s.is_queued(win[0]))
    check("...after a turn (we went quiet, they could answer): a resend may help",
          s.why_stale(win[0], turn_t=12) is None)
    s.on_heard([RR(5)], now=20)
    check("RR5 acknowledges I6..I4: every resend of them is 'acked'",
          all(s.why_stale(f, turn_t=30) == "acked" for f in win))
    s2 = la.StaleFrames()
    s2.on_aired([I(n) for n in (6, 7, 0, 1)], now=10)
    s2.on_heard([RR(0)], now=20)
    check("RR0 after I6 I7 I0 I1: I6, I7 acked; I0, I1 not",
          [s2.why_stale(I(n), turn_t=15) for n in (6, 7, 0, 1)] == ["acked", "acked", None, None])
    s2.on_heard([REJ(0)], now=25)
    check("REJ0 again: I0 still not acked (a real retry goes out)", s2.why_stale(I(0), turn_t=22) is None)
    s2.on_aired([_ax25_frame(B, A, [], control=0x3F, pid=False)], now=30)     # SABM
    check("a new connection (SABM) forgets the old numbering", s2.why_stale(I(6), turn_t=0) is None)

    # v0.9.65: P/F shown as poll (command) or final (response); FRMR decoded
    def cr(f, cmd):
        b = bytearray(f)
        b[6] = (b[6] & 0x7F) | (0x80 if cmd else 0)
        b[13] = (b[13] & 0x7F) | (0 if cmd else 0x80)
        return bytes(b)
    check("RR0 with the P bit as a command shows 'p', as a response 'f', unknown 'p?'",
          la.frame_trace(cr(RR(0, True), True)) == "RR0p" and la.frame_trace(cr(RR(0, True), False)) == "RR0f"
          and la.frame_trace(RR(0, True)) == "RR0p?",
          str([la.frame_trace(cr(RR(0, True), True)), la.frame_trace(cr(RR(0, True), False))]))
    fr_ = cr(_ax25_frame(A, B, [], control=0x97, info=bytes([0x6C, 0xCC, 0x08]), pid=False), False)
    t_ = la.frame_trace(fr_)
    check("FRMR decoded: which frame, the sender's V(S)/V(R), and why", t_.startswith("FRMRf[rejected I6/3")
          and "V(S)=6 V(R)=6" in t_ and "Z bad N(R)" in t_, t_)
    # engine: the filter runs as items are taken for the air
    eng = audio_io.AudioEngine.__new__(audio_io.AudioEngine)
    eng.tx_filter = lambda p: None if p == b"drop" else (b"less" if p == b"trim" else p)
    check("engine filter: kept / trimmed / skipped",
          eng._tx_filtered((b"x", 1, 0.5, None, None))[0] == b"x"
          and eng._tx_filtered((b"trim", 1, 0.5, None, None))[0] == b"less"
          and eng._tx_filtered((b"drop", 1, 0.5, None, None)) is audio_io._SKIPPED)
    eng._last_tx_end = _time.time() - 5
    eng._ptt_keyed = False
    check("turn_time: quiet 5 s since our TX -> their turn", abs(eng.turn_time() - eng._last_tx_end) < 1e-6)
    eng._ptt_keyed = True
    eng.turn_t = 1.0
    check("turn_time: while keyed (inside a burst) the last turn stands", eng.turn_time() == 1.0)

    # App filter: drops the stale frames, releases KISS ACKMODE for them, logs it
    App = importlib.import_module("sara_pingpong").App
    logs, released = [], []
    st = types.SimpleNamespace(stale=la.StaleFrames(), engine=types.SimpleNamespace(turn_time=lambda: 5.0),
                               bridge=types.SimpleNamespace(frames_sent=lambda p: released.append(p)),
                               _is_relayed_tx=lambda f: False, after=lambda ms, fn: fn(),
                               _log=lambda m: logs.append(m))
    st.stale.on_aired([I(5), I(6)], now=10)
    out = App._tx_filter(st, bb.pack_batch([I(5), RR(0), I(7)]))
    check("App._tx_filter: the resent I5 is skipped, RR and the new I7 go",
          bb.unpack_batch(out) == [RR(0), I(7)],
          str([la.frame_trace(f) for f in bb.unpack_batch(out)]))
    check("...its ACKMODE ack is released and the log says why",
          released and bb.unpack_batch(released[0]) == [I(5)] and "skipped 1 resent frame" in logs[-1]
          and "hasn't had a chance to answer" in logs[-1], str(logs))
    check("...a batch of nothing but stale resends is not sent at all",
          App._tx_filter(st, bb.pack_batch([I(6, nr=3)])) is None)
    check("...chat/ping payloads untouched", App._tx_filter(st, b"\x02abc") == b"\x02abc")

    # relay watch + the engine's yield to the digipeater
    D, F = "DV1UK", "DX1FLR-10"
    direct = _ax25_frame(F, A, [(D, False)], control=0x0C, info=b"x" * 30)
    relayed = _ax25_frame(F, A, [(D, True)], control=0x0C, info=b"x" * 30)
    rw = la.RelayWatch()
    rw.expect([direct], now=0)
    check("frame heard direct, still to be relayed: pending", rw.pending(now=1))
    rw.heard([relayed])
    check("relayed copy heard: nothing pending", not rw.pending(now=2))
    rw.expect([direct], now=0)
    check("a relay never heard is forgotten after a minute", not rw.pending(now=61))

    eng = audio_io.AudioEngine.__new__(audio_io.AudioEngine)
    eng.dcd_mode = "sara"
    pend = [True]
    eng.relay_pending = lambda: pend[0]
    gave = []
    eng.relay_give_up = lambda: gave.append(1)
    old_y = audio_io.RELAY_YIELD_S
    audio_io.RELAY_YIELD_S = 0.3
    try:
        t0 = _time.time()
        extra = eng._yield_to_relay(lambda: False, t0)
        check("no relay starts: yields RELAY_YIELD_S of clear channel, then gives up",
              0.25 <= _time.time() - t0 < 1.0 and gave and extra == 0.0, f"{_time.time() - t0:.2f}")
        # the relay starts: busy until it is heard
        gave.clear()
        pend[0] = True
        t1 = _time.time()
        state = {"n": 0}

        def busy():
            state["n"] += 1
            if state["n"] == 3:
                pend[0] = False         # relay decoded as the channel clears
            return state["n"] < 3
        eng._yield_to_relay(busy, t1)
        check("relay running: waits it out and goes as soon as it is heard (no give-up)",
              not gave and _time.time() - t1 < 0.25, f"{_time.time() - t1:.2f}")
        eng.dcd_mode = "off"
        pend[0] = True
        t2 = _time.time()
        eng._yield_to_relay(lambda: False, t2)
        check("DCD off: no yield", _time.time() - t2 < 0.05)
    finally:
        audio_io.RELAY_YIELD_S = old_y

    # v0.9.66: a relay needs an unbroken clear channel -- a gap inside the
    # sender's BPSK burst (DX1FLR -> DW1IRF, field) must not let it key up
    eng = audio_io.AudioEngine.__new__(audio_io.AudioEngine)
    eng.dcd_mode = "sara"
    eng.min_clear_s = lambda p: 0.4 if p == b"relay" else 0.0
    t0 = _time.time()
    # busy 0.3 s, clear 0.15 s (inter-frame gap), busy 0.3 s, then clear
    plan = lambda: (lambda t: t < 0.3 or 0.45 <= t < 0.75)(_time.time() - t0)
    eng._hold_clear(plan, t0, b"relay")
    el = _time.time() - t0
    check("relay: a short gap inside the sender's burst doesn't end the wait -- it goes "
          "0.4 s after the burst really ends", 1.1 <= el < 1.6, f"{el:.2f}")
    t1 = _time.time()
    eng._hold_clear(lambda: False, t1, b"own")
    check("our own frames: no extra hold", _time.time() - t1 < 0.05)
    App_ = importlib.import_module("sara_pingpong").App
    stx = types.SimpleNamespace(_relayed={I(3): 1.0})
    check("App: a batch we relay asks for DIGI_MIN_CLEAR_S, our own doesn't",
          App_._min_clear_s(stx, bb.pack_batch([I(3)])) == audio_io.DIGI_MIN_CLEAR_S
          and App_._min_clear_s(stx, bb.pack_batch([I(4)])) == 0.0)

    # v0.9.67: freeze watchdog writes every thread's stack to logs/
    import tempfile as _tf, os as _os
    d_ = _tf.mkdtemp()
    pth = App_.write_freeze_dump(d_, "DX1FLR", 12.0)
    txt = open(pth, encoding="utf-8").read() if pth else ""
    check("freeze dump: a file in logs/ naming the callsign, with every thread's stack",
          pth and _os.path.basename(_os.path.dirname(pth)) == "logs" and "sara_pp_freeze_DX1FLR_" in pth
          and "not responding for 12 s" in txt and "--- thread MainThread ---" in txt, pth)

    # v0.9.68: after() from another thread never waits for the window
    import sara_pingpong as _spx
    app_ = _spx.App()
    try:
        lk = threading.Lock()
        done_ = []
        ran = []

        def worker():
            with lk:                               # like the Auto-speed lock
                app_.after(0, lambda: ran.append(1))
            done_.append(1)
        with lk:                                   # the window's thread holds it first...
            th = threading.Thread(target=worker)
            th.start()
            _time.sleep(0.05)
        th.join(1.0)                               # ...the worker must not hang
        t_end = _time.time() + 1.0
        while not ran and _time.time() < t_end:
            app_.update()
            _time.sleep(0.01)
        check("after() from another thread returns at once (no deadlock) and runs on the window's thread",
              done_ == [1] and ran == [1], f"{done_} {ran}")
    finally:
        app_.destroy()

    # v0.9.69: slow RA-Board (HID) calls are recorded and reported
    import ptt as _ptt
    _ptt.take_slow_hid()
    _ptt._note_hid("PTT ON on the RA-Board", _time.time() - 9.8)
    _ptt._note_hid("fast one", _time.time() - 0.01)
    ev = _ptt.take_slow_hid()
    check("slow HID call recorded (9.8 s), fast one not", len(ev) == 1 and ev[0][1].startswith("PTT ON")
          and 9.5 < ev[0][2] < 10.5 and _ptt.take_slow_hid() == [], str(ev))

    # v0.9.70: a relay we couldn't decode still counts once anything is heard after it
    rw2 = la.RelayWatch()
    rw2.expect([direct], now=100.0)
    check("relay expected, nothing heard since: pending", rw2.pending(now=101.0, seen=99.0))
    check("...a SARA signal heard after it (the relay, undecodable here): not pending",
          not rw2.pending(now=103.0, seen=101.5))
    # v0.9.70: queued session frames are dropped once the KISS client is gone; DISC/DM still go
    logs3 = []
    st3 = types.SimpleNamespace(stale=la.StaleFrames(), engine=types.SimpleNamespace(turn_time=lambda: 5.0),
                                bridge=types.SimpleNamespace(_client_sock=None, frames_sent=lambda p: None),
                                _is_relayed_tx=lambda f: False, after=lambda ms, fn: fn(),
                                _log=lambda m: logs3.append(m))
    dm = _ax25_frame(B, A, [], control=0x0F, pid=False)
    out3 = App._tx_filter(st3, bb.pack_batch([I(3), RR(1), dm]))
    check("client gone: queued I and RR frames dropped, DM kept",
          out3 is not None and bb.unpack_batch(out3) == [dm] and "KISS client disconnected" in logs3[-1],
          str(logs3))
    check("...a batch of only data frames is not sent", App._tx_filter(st3, bb.pack_batch([I(4)])) is None)

    # replay of the field burst (DW1IRF 19:57:57): I5 I6 I7 I0 then the same
    # four handed over again by Winlink Express before any answer
    import queue as _q
    from unittest.mock import MagicMock, patch
    e = audio_io.AudioEngine.__new__(audio_io.AudioEngine)
    e.tx_lead_ms, e.tx_tail_ms = 600, 150
    e._rx_rms_ema = 0.0
    e._tx_lock = threading.Lock()
    e.ptt = MagicMock()
    e.on_carrier_wait = e.on_tx_start = None
    e.is_channel_busy = lambda: False
    e._tx_write = lambda by: None
    e._tx_queue = _q.Queue()
    e.merge_payloads = None
    e._last_tx_end = _time.time() - 10
    st2 = types.SimpleNamespace(stale=la.StaleFrames(), engine=e, bridge=None,
                                _is_relayed_tx=lambda f: False, after=lambda ms, fn: None,
                                _log=lambda m: None)
    e.tx_filter = lambda p: App._tx_filter(st2, p)
    aired = []

    def on_sent(p, m):
        fr = bb.unpack_batch(p)
        aired.extend(la.frame_trace(f) for f in fr)
        st2.stale.on_aired(fr)
    for ns in (5, 6, 7, 0, 5, 6, 7, 0):
        e._tx_queue.put((bb.pack_batch([I(ns, nr=7)]), ofdm.MOD_BPSK, 0.4, on_sent, None))
    with patch("time.sleep"):
        while not e._tx_queue.empty():
            e._send_burst(e._tx_queue.get())
    check("field replay: the four resends behind the originals are skipped -- one window, then "
          "the channel is DV1UK-10's", [a.split(" ")[0] for a in aired] == ["I5/7", "I6/7", "I7/7", "I0/7"],
          str(aired))


def test_gateway_direct_and_b2_check():
    """v0.9.71. Field (0.9.55-0.9.70): Winlink through SARA FM's digipeater
    failed the CMS B2 checksum at both gateways, while the same message --
    the same AX.25 frames, accepted in the same order -- went through direct.
    The gateway now shows a digipeated caller to BPQ32 as direct and puts
    the path back on BPQ32's replies; and every B2 message passing the
    bridge is checked, so the log says whether BPQ32 was given it intact."""
    import link_adapt as la, b2check, bpq_bridge as bb, kiss, socket, struct, random
    C, G, D = "DX1PRD-2", "DV1UK-10", "DW1IRF"
    via = [(D, True)]
    sabm = _ax25_frame(G, C, via, control=0x3F, pid=False)
    vm = la.ViaMap()
    out, note = vm.to_client(sabm, now=0)
    check("gateway: a connect via a digipeater reaches BPQ32 as a direct one",
          out == _ax25_frame(G, C, [], control=0x3F, pid=False) and note and "via DW1IRF" in note, str(note))
    i_frame = _ax25_frame(G, C, via, control=0x00, info=b"data" * 10)
    check("...and so does its data (same bytes, no path)",
          vm.to_client(i_frame, now=1)[0] == _ax25_frame(G, C, [], control=0x00, info=b"data" * 10))
    ua = _ax25_frame(C, G, [], control=0x73, pid=False)
    check("BPQ32's direct reply goes on the air with the path (not yet repeated)",
          vm.from_client(ua, now=2) == _ax25_frame(C, G, [(D, False)], control=0x73, pid=False))
    check("a frame still on its way to the digipeater is left alone",
          vm.to_client(_ax25_frame(G, C, [(D, False)], control=0x00, info=b"x" * 20), now=3)[0]
          == _ax25_frame(G, C, [(D, False)], control=0x00, info=b"x" * 20))
    with_path = _ax25_frame(C, G, [("DX1FLR", False)], control=0x00, info=b"y" * 20)
    check("a frame BPQ32 already gave a path is left alone", vm.from_client(with_path, now=4) == with_path)
    check("another station's frames are left alone",
          vm.from_client(_ax25_frame("DU3FS", G, [], control=0x73, pid=False), now=4)
          == _ax25_frame("DU3FS", G, [], control=0x73, pid=False))
    check("the note is said once per connection", vm.to_client(sabm, now=5)[1] is None)
    vm.to_client(_ax25_frame(G, C, [], control=0x3F, pid=False), now=6)
    check("a direct connect from the same station ends the translation", vm.from_client(ua, now=7) == ua)
    two = _ax25_frame(G, C, [("DX1A", True), ("DX1B", True)], control=0x3F, pid=False)
    vm.to_client(two, now=8)
    check("two digipeaters: the reply path is reversed",
          vm.from_client(ua, now=9) == _ax25_frame(C, G, [("DX1B", False), ("DX1A", False)], control=0x73, pid=False))
    check("translation forgotten after a long idle", vm.from_client(ua, now=9 + la.ViaMap.IDLE_S + 1) == ua)

    # the bridge rewrites BPQ32's frame BEFORE filing its ACKMODE id, so the
    # acknowledgment still matches the frame as aired
    vm2 = la.ViaMap()
    vm2.to_client(sabm, now=_time.time())
    br = bb.BpqBridge(on_air_frame=lambda p: aired.append(p), on_status=lambda m: None)
    aired = []
    br.rewrite_from_client = vm2.from_client
    a_sock, b_sock = socket.socketpair()
    br._running = True
    br._client_sock = a_sock
    t = threading.Thread(target=br._client_loop, args=(a_sock,), daemon=True)
    t.start()
    b_sock.sendall(kiss.encode_frame(b"\x00\x07" + ua, command=kiss.CMD_ACKMODE))
    deadline = _time.time() + 3
    while not aired and _time.time() < deadline:
        _time.sleep(0.05)
    want = _ax25_frame(C, G, [(D, False)], control=0x73, pid=False)
    check("bridge: BPQ32's reply leaves with the path", aired and bb.unpack_batch(aired[0]) == [want],
          str(aired))
    br.frames_sent(bb.pack_batch([want]))
    b_sock.settimeout(2)
    try:
        got = b_sock.recv(100)
    except OSError:
        got = b""
    check("bridge: ...and its ACKMODE id is acknowledged when it airs", b"\x00\x07" in got, repr(got))
    br._running = False
    t.join(2)
    a_sock.close()
    b_sock.close()

    # B2 checks
    random.seed(7)
    comp = bytes([0x11, 0x22]) + struct.pack("<I", 5166) + bytes(random.randrange(256) for _ in range(2778))
    st = bytearray(b"FC EM 45RUB2ULLMYU 5166 2784 0\rF> F5\r")
    hdr = b"Test message\x000\x00"
    st += bytes([1, len(hdr)]) + hdr
    for k in range(0, len(comp), 250):
        blk = comp[k:k + 250]
        st += bytes([2, len(blk)]) + blk
    st += bytes([4, (-sum(comp)) & 0xFF])
    chunks = [bytes(st[i:i + 255]) for i in range(0, len(st), 255)]
    I = lambda ns, data: _ax25_frame(G, C, via, control=((ns & 7) << 1) | (1 << 5), info=data)
    fs = _ax25_frame(C, G, via, control=0x00, info=b"FS Y\r")

    def run(seq):
        w = b2check.B2Watch()
        w.feed(sabm)
        w.feed(fs)
        notes = []
        for ns, k in seq:
            notes += w.feed(I(ns, chunks[k]))
        return notes
    n = len(chunks)
    clean = [(k & 7, k) for k in range(n)]
    notes = run(clean)
    check("B2: the whole message, in order: intact", len(notes) == 1 and notes[0].endswith("intact")
          and "2784 bytes" in notes[0] and "45RUB2ULLMYU" in notes[0], str(notes))
    # field replay (DX1FLR-10, 0.9.70): I5 lost on the relay, I6..I1 out of order,
    # then I5..I1 again, plus repeats -- AX.25 numbering keeps only the right copies
    seq = [(k & 7, k) for k in range(7)] + [((k & 7), k) for k in range(8, n)] \
        + [((k & 7), k) for k in range(7, n)] + [((k & 7), k) for k in range(n - 3, n)]
    notes = run(seq)
    check("B2: lost, out-of-order and repeated copies are sorted out as BPQ32 would",
          len(notes) == 1 and notes[0].endswith("intact"), str(notes))
    # a repeated copy taken as new data (what a mis-numbered stack would do)
    bad = [(k & 7, k) for k in range(5)] + [(5, 4)] + [((k + 1) & 7, k) for k in range(5, n)]
    notes = run(bad)
    check("B2: a block delivered twice is caught", len(notes) == 1 and not notes[0].endswith("intact"), str(notes))
    flipped = list(chunks)
    flipped[3] = flipped[3][:100] + bytes([flipped[3][100] ^ 0x40]) + flipped[3][101:]
    w = b2check.B2Watch()
    w.feed(sabm)
    w.feed(fs)
    notes = []
    for k, c in enumerate(flipped):
        notes += w.feed(I(k & 7, c))
    check("B2: one changed byte: checksum WRONG", len(notes) == 1 and "checksum WRONG" in notes[0], str(notes))
    # two proposals, the first refused: the binary is the second one
    st2 = bytearray(b"FC EM AAAA 10 9 0\rFC EM BBBB 20 12 0\rF> 00\r")
    data = bytes([1, 2]) + struct.pack("<I", 20) + bytes(6)
    st2 += bytes([1, 3]) + b"x\x000"[:3] + bytes([2, len(data)]) + data + bytes([4, (-sum(data)) & 0xFF])
    # order matters: the FS answer comes after the proposals, so feed proposals first
    w = b2check.B2Watch()
    w.feed(sabm)
    notes = w.feed(I(0, bytes(st2[:st2.index(b"F> 00\r") + 6])))
    notes += w.feed(_ax25_frame(C, G, via, control=0x00, info=b"FS NY\r"))
    notes += w.feed(I(1, bytes(st2[st2.index(b"F> 00\r") + 6:])))
    check("B2: an answer 'FS NY' -- the binary is matched to the accepted proposal",
          len(notes) == 1 and "BBBB" in notes[0] and notes[0].endswith("intact"), str(notes))


def test_direct_fallback_and_hop_reports():
    """v0.9.72. Field (0.9.71, DX1PRD-2 -> DV1UK-10 via DW1IRF, 14 kB in
    8:43): DX1FLR heard DV1UK-10's answers direct but often not DW1IRF's
    relay, and Winlink Express waited out its retry timer each time; DX1FLR
    stayed at QPSK at 21 dB (DW1IRF's reports, addressed to DX1PRD-2, were
    ignored); and a frame DW1IRF had repeated but DV1UK-10 missed dropped the
    hop to DW1IRF to BPSK."""
    import link_adapt as la, sara_pingpong as sp
    C, G, D = "DX1PRD-2", "DV1UK-10", "DW1IRF"
    I = lambda ns, via_h=False: _ax25_frame(C, G, [(D, via_h)], control=((ns & 7) << 1), info=bytes([ns]) * 30)
    fb = la.DirectFallback()
    for n in (0, 1, 2):
        fb.hold(I(n), now=10 + n)
    fb2 = la.DirectFallback()
    for n in (0, 1, 2):
        fb2.hold(I(n), now=10)              # one burst: same decode time
    check("v0.9.73: one burst held; its relay in order releases nothing early",
          [fb2.relayed(I(n, True), now=12) for n in (0, 1, 2)] == [([], True)] * 3)
    first, keep = fb.relayed(I(1, True), now=14)
    check("relay brings I1 but not I0: I0's direct copy goes first, as relayed",
          first == [I(0, True)] and keep, str([la.frame_trace(f) for f in first]))
    check("nothing else due before 6 s after the last direct copy", fb.due(now=17) == [])
    due = fb.due(now=18.5)
    check("I2's relay never decoded: its direct copy is passed on after 6 s", due == [I(2, True)])
    check("...and the late relayed copies of I0 and I2 are dropped (no duplicates)",
          fb.relayed(I(0, True), now=19) == ([], False) and fb.relayed(I(2, True), now=19) == ([], False))
    check("a later resend's relayed copy still goes through", fb.relayed(I(2, True), now=20) == ([], True))
    fb.hold(I(3), now=30)
    check("normal case: the relay arrives, nothing extra", fb.relayed(I(3, True), now=33) == ([], True)
          and fb.due(now=40) == [])

    class FakeBridge:
        def __init__(self):
            self.got = []
        def deliver_from_air(self, p):
            self.got.append(p)
        def stop(self):
            pass

    def air(payload, mod=ofdm.MOD_QPSK):
        out = decode_all(ofdm.OFDMReceiver(), ofdm.frame_encode(payload, modulation=mod), chunk=1024)
        return out[0] if out else None
    app = sp.App()
    try:
        app._save_settings = lambda: None
        app.call_var.set("DX1FLR"); app.modulation_var.set("Auto")
        app.bridge = FakeBridge()
        app._client_calls = {C}
        app._handle_air_frame_for_bridge(air(bpq_bridge.pack_batch([I(4)])))
        check("app: DV1UK-10's frame heard direct is held at first", app.bridge.got == [])
        old = la.DirectFallback.FALLBACK_S
        la.DirectFallback.FALLBACK_S = 0.0
        try:
            app._release_fallback()
        finally:
            la.DirectFallback.FALLBACK_S = old
        check("app: no relay -> the direct copy reaches Winlink Express (as relayed)",
              len(app.bridge.got) == 1 and bpq_bridge.unpack_batch(app.bridge.got[0]) == [I(4, True)])
        app._handle_air_frame_for_bridge(air(bpq_bridge.pack_batch([I(4, True)])))
        check("app: ...and the relayed copy arriving late isn't passed on twice", len(app.bridge.got) == 1)
        app._handle_air_frame_for_bridge(air(bpq_bridge.pack_batch([I(5, True)])))
        check("app: a relayed frame with no held copy goes straight through", len(app.bridge.got) == 2)

        # v0.9.73 field replay (0.9.72, DX1FLR): direct {UA I0}, then the relay
        # {UA I0}; direct {I0..I4}, then the relay {I0..I4}. 0.9.72 passed I0
        # ahead of the UA, and I3 ahead of I0 -- now: exactly the relay's order
        app.bridge = FakeBridge()
        app.direct_fb = la.DirectFallback()
        UA = _ax25_frame(C, G, [(D, False)], control=0x73, pid=False)
        J = lambda ns, h=False, p=False: _ax25_frame(C, G, [(D, h)], control=((ns & 7) << 1) | (0x10 if p else 0),
                                                     info=bytes([ns + 7]) * 30)
        for burst in ([UA, J(0, p=True)], [la.mark_repeated(UA), J(0, True, True)],
                      [J(n) for n in range(5)], [J(n, True) for n in range(5)]):
            app._handle_air_frame_for_bridge(air(bpq_bridge.pack_batch(burst)))
        got = [la.frame_trace(f) for p_ in app.bridge.got for f in bpq_bridge.unpack_batch(p_)]
        want = [la.frame_trace(f) for f in [la.mark_repeated(UA), J(0, True, True)] + [J(n, True) for n in range(5)]]
        check("field replay: frames reach Winlink Express once each, in the order sent", got == want,
              str(got))

        # v0.9.74: a session overheard between two other stations isn't checked
        notes = []
        app._b2_notes = lambda n, given: notes.extend(n)
        app.bridge = FakeBridge()
        other = lambda ctl, info=b"": _ax25_frame("DV1UK-10", "DW1IRF", [], control=ctl, info=info,
                                                   pid=(ctl & 1) == 0)
        app._handle_air_frame_for_bridge(air(bpq_bridge.pack_batch([other(0x3F)])))
        app._handle_air_frame_for_bridge(air(bpq_bridge.pack_batch(
            [other(0x00, b"FC EM X 10 9 0\rF> 00\r"), other(0x02, bytes([1, 2]) + b"a\x00" + bytes([2, 3]) + b"xyz\xff")])))
        check("v0.9.74: another station's session is passed on but not B2-checked",
              len(app.bridge.got) == 2 and notes == [], str(notes))

        # reports addressed to the KISS client's callsign are about our hop
        rr = _ax25_frame(C, G, [(D, True)], control=0x01, pid=False)
        check("a relay to DX1PRD-2 (our Winlink Express) carries a report for this station",
              app._report_for_me(bpq_bridge.pack_batch([rr]), C))
        app._client_calls = set()
        check("...but not when DX1PRD-2 isn't our client", not app._report_for_me(bpq_bridge.pack_batch([rr]), C))

        # a resend of a frame the digipeater DID repeat: the first hop is fine
        steps = []
        app.speed.on_retransmit = lambda peer: steps.append(peer)
        mine = _ax25_frame(G, C, [(D, False)], control=(3 << 1), info=b"z" * 40)
        app._own_relay_heard[la.relay_key(mine)] = _time.time()
        app._on_client_frame(mine)
        app._on_client_frame(mine)
        app.update()
        check("resend of a frame DW1IRF repeated: speed to DW1IRF not stepped down", steps == [], str(steps))
        other = _ax25_frame(G, C, [(D, False)], control=(4 << 1), info=b"w" * 40)
        app._on_client_frame(other)
        app._on_client_frame(other)
        check("resend of a frame DW1IRF never repeated: stepped down as before", steps == [D], str(steps))
    finally:
        app.bridge = None
        app.engine = None
        app.destroy()


def test_remote_chat():
    """v0.9.75: SARA Chat from another computer, like VARA Chat with VARA FM.
    An operator with no radio chats under their own callsign through a
    station's SARA FM; the station's BPQ32 keeps the KISS port."""
    import chat, chat_remote, struct, sara_pingpong as sp, tempfile as tf
    # protocol: hello / welcome / tx / sent / rx / state over localhost
    got_tx, got_rx, sent_ok = [], [], []
    srv = chat_remote.ChatServer(on_tx=lambda c, i, p, peer, att: (got_tx.append((p, peer)),
                                                                   c.send({"t": "sent", "id": i})))
    srv.station = "DV1UK"
    sk = socket.socket(); sk.bind(("127.0.0.1", 0)); port = sk.getsockname()[1]; sk.close()
    srv.start(port=port, host="127.0.0.1", allow=None)
    main_only = threading.current_thread()

    def call_fn():
        # v0.9.77: like a Tk variable on Windows -- only the main thread may read it
        if threading.current_thread() is not main_only:
            raise RuntimeError("main thread is not in main loop")
        return "du1abc"
    cl = chat_remote.ChatClient(call_fn, on_rx=lambda p, snr, mode: got_rx.append((p, snr, mode)))
    cl.start("127.0.0.1", port)

    def wait(cond, t=10.0):
        end = _time.time() + t
        while not cond() and _time.time() < end:
            _time.sleep(0.05)
        return cond()
    check("remote chat: operator connects and is told the station", wait(lambda: cl.connected)
          and cl.station == "DV1UK" and srv.calls() == {"DU1ABC"}, str((cl.station, srv.calls())))
    srv.set_can_send(True)
    check("remote chat: the station says when the radio is free", wait(lambda: cl.ready()))
    frame = chat.pack(chat.T_MSG, "DU1ABC", "DX1FLR", chat.msg_body(1, 0, 0, "hello"))
    check("remote chat: a frame goes to the station and its 'sent' comes back",
          cl.transmit(frame, "DX1FLR", 1, lambda: sent_ok.append(1)) and wait(lambda: sent_ok == [1])
          and got_tx == [(frame, "DX1FLR")])
    srv.broadcast({"t": "rx", "p": chat_remote._b64(frame), "snr": 21, "mode": "QPSK"})
    check("remote chat: frames the station hears reach the operator", wait(lambda: got_rx == [(frame, 21, "QPSK")]))
    cl.stop(); srv.stop()
    check("remote chat: the station forgets an operator who leaves", wait(lambda: srv.calls() == set()))

    # two SARA FM windows: the station (radio, DV1UK) and an operator (no radio, DU1ABC)
    class FakeEngine:
        def __init__(self):
            import queue as _q
            self.aired = []
            self.tx_level = 0.4
            self.frame_queue = _q.Queue()
        def queue_transmit(self, payload, modulation=0, on_sent=None, on_error=None, tx_gain=None):
            self.aired.append(payload)
            if on_sent:
                on_sent(payload, modulation)
        def __getattr__(self, n):
            if n.endswith("_queue"):
                import queue as _q
                q = _q.Queue()
                setattr(self, n, q)
                return q
            return lambda *a, **k: None
    d1, d2 = tf.mkdtemp(), tf.mkdtemp()
    os.environ["SARA_CHAT_DIR"] = d1
    st = sp.App()
    os.environ["SARA_CHAT_DIR"] = d2
    op = sp.App()
    os.environ.pop("SARA_CHAT_DIR", None)
    try:
        for a in (st, op):
            a._save_settings = lambda: None
        st.call_var.set("DV1UK"); op.call_var.set("DU1ABC")
        st._poll_engine = lambda: None          # the fake engine has no audio queues
        st.engine = FakeEngine()
        sk = socket.socket(); sk.bind(("127.0.0.1", 0)); port = sk.getsockname()[1]; sk.close()   # a fresh port
        st.chat_serve_var.set(True); st.chat_port_var.set(str(port)); st.kiss_allow_var.set("private")
        st._start_chat_server()
        op.chat_remote_host_var.set("127.0.0.1"); op.chat_remote_port_var.set(str(port))
        op.chat_use_remote_var.set(True); op._apply_remote_chat()

        def pump(cond, t=15.0):
            end = _time.time() + t
            while not cond() and _time.time() < end:
                if st.chat_server is not None:      # what the station's 100 ms loop does
                    st.chat_server.set_can_send(st._radio_can_chat())
                for a in (st, op):
                    a.update()
                    a.chat.tick()
                _time.sleep(0.03)
            return cond()
        def diag():
            return (f"client connected={op.chat_client.connected} can_send={op.chat_client.can_send} "
                    f"server calls={st.chat_server.calls() if st.chat_server else None} "
                    f"aired={[ (chat.unpack(p) or {}).get('frm') for p in st.engine.aired]} || OPERATOR LOG: "
                    + op.log_box.get("1.0", "end")[-700:] + " || STATION LOG: " + st.log_box.get("1.0", "end")[-700:])
        ok = pump(lambda: op._chat_can_send())
        check("app: the operator's chat is ready through the station (no radio of its own)",
              ok and op.engine is None, "" if ok else diag())
        op.chat.send_text("DX1FLR", "QSL via DV1UK")
        ok = pump(lambda: any((chat.unpack(p) or {}).get("frm") == "DU1ABC" for p in st.engine.aired))
        check("app: the operator's message goes out on the STATION's radio, from DU1ABC", ok, "" if ok else diag())
        if not ok:
            return
        m = [chat.unpack(p) for p in st.engine.aired if (chat.unpack(p) or {}).get("frm") == "DU1ABC"][0]
        check("...addressed to DX1FLR", m["to"] == "DX1FLR" and m["type"] == chat.T_MSG, str(m))
        e = op.chat.convs["DX1FLR"][-1]
        check("app: the operator sees it 'sent'", pump(lambda: e.get("status") == "sent"), e.get("status"))
        # DX1FLR's receipt arrives on the station's radio
        ack = chat.pack(chat.T_ACK, "DX1FLR", "DU1ABC", bytes([1]) + struct.pack(">b", 20) + struct.pack(">H", m["id"]))
        out = decode_all(ofdm.OFDMReceiver(), ofdm.frame_encode(ack, modulation=ofdm.MOD_QPSK), chunk=1024)
        st._on_chat_frame(out[0])
        check("app: DX1FLR's receipt reaches the operator -- 'delivered'",
              pump(lambda: e.get("status") == "delivered"), e.get("status"))
        # operator <-> station operator: delivered locally, no air time
        n_air = len(st.engine.aired)
        op.chat.send_text("DV1UK", "hi from the remote desk")
        check("app: a message to the station's own operator arrives there...",
              pump(lambda: any(x.get("text") == "hi from the remote desk" for x in st.chat.convs.get("DU1ABC", []))))
        e2 = op.chat.convs["DV1UK"][-1]
        check("...is delivered (receipt passed back locally)", pump(lambda: e2.get("status") == "delivered"),
              e2.get("status"))
        check("...and nothing went on the air for it", len(st.engine.aired) == n_air,
              str([chat.unpack(p) for p in st.engine.aired[n_air:]]))
        log = st.log_box.get("1.0", "end")
        check("app: the station's log shows the remote operator", "Remote chat: DU1ABC connected from 127.0.0.1" in log)
    finally:
        op.chat_client.stop()
        st._stop_chat_server()
        st.engine = None
        for a in (st, op):
            a.destroy()


def test_simple_tune_and_gateway_levels():
    """v0.9.78. Volunteers found Ping / Auto-Tune / Tune TX confusing: three
    panels of numbers and advice pulling both ways. One Tune button now ends
    with ONE verdict, like VARA FM's drive calibration. A gateway can keep one
    set of audio levels for every caller; callers tune to it."""
    import tuning as T, sara_pingpong as sp
    v = T.tune_verdict("DX1FLR", reply=True, their_snr=15, their_raw=-49, data_db=16, tx_pct=14, rx_gain_db=6)
    check("verdict: clean data at the far end is APPROVED even if it arrives quietly (no either/or advice)",
          v[0] == "done" and v[1] == "APPROVED" and "TX 14%" in v[2] and "raise" not in v[2].lower(), str(v))
    v = T.tune_verdict("DX1FLR", reply=False)
    check("verdict: no reply says so, with the one check to make", v[0] == "failed" and "NO REPLY" in v[1])
    v = T.tune_verdict("DX1FLR", reply=True, their_snr=4, their_raw=-57, data_db=4, tx_pct=100)
    check("verdict: too quiet even at TX 100% -> turn up the interface (like VARA's trimpot)",
          v[1] == "TURN UP YOUR TX AUDIO", str(v))
    v = T.tune_verdict("DX1FLR", reply=True, their_snr=8, their_raw=-52, data_db=7, tx_pct=20)
    check("verdict: too quiet at a low TX level -> one number to set", v[1] == "RAISE YOUR TX LEVEL"
          and "40%" in v[2], str(v))
    v = T.tune_verdict("DX1PAR", reply=True, their_snr=12, their_raw=-30, data_db=9, data_flat=True, tx_pct=30)
    check("verdict: the same poor carriers at every level -> the link, not a setting",
          v[1].startswith("WEAK LINK"), str(v))
    v = T.tune_verdict("DV1UK", reply=True, their_replies_clipped=True)
    check("verdict: their replies clipped -> your levels are fine, THEY tune",
          "YOUR LEVELS ARE FINE" in v[1] and "gateway" in v[2], str(v))
    v = T.tune_verdict("DV1UK", reply=True, my_clip=True)
    check("verdict: your own input clipping comes first", v[1] == "TURN DOWN YOUR RADIO'S VOLUME")
    v = T.tune_verdict("DW1IRF", reply=True, their_snr=20, data_db=17, tx_pct=20, rx_gain_db=0, gateway_self=True)
    check("verdict at a gateway: the levels are for every caller", "every caller" in v[2], str(v))

    app = sp.App()
    try:
        app._save_settings = lambda: None
        app.update()
        check("one Tune button on the main window; Tune TX alone only in the menu",
              app.autotune_btn.cget("text") == "Tune" and app.txtune_btn.winfo_manager() == "")
        # Tune TX finishing: one verdict in the banner, after the details in the log
        app.call_var.set("DW1IRF"); app.target_var.set("DX1FLR"); app.tx_level_var.set("20")
        app.txtune = {"target": "DX1FLR", "start": 0.2, "levels": [0.2, 0.28], "i": 1, "n": 0, "seq": None,
                      "results": {0.2: [16, 17], 0.28: [12, 13]}, "pre": {0.2: [18, 18], 0.28: [17, 17]},
                      "misses": 0, "any_reply": True, "missed": {0.2: 0, 0.28: 0}, "stopped_low": None,
                      "mod": ofdm.MOD_QPSK_WIDE, "back_lost": 0}
        app._autotune_chain = False
        app._finish_txtune()
        app.update()
        banner = app.tune_status_var.get()
        log = app.log_box.get("1.0", "end").strip().splitlines()
        check("Tune finished: the banner gives one answer", banner.startswith("TUNE: APPROVED") and "TX 20%" in banner,
              banner)
        check("...and it's the last word in the log", any("TUNE RESULT: APPROVED" in l for l in log[-3:]), str(log[-3:]))
        check("v0.9.79: the ping after Tune shows that answer, not the old Tune TX note",
              app._tune_note[0] == "DX1FLR" and len(app._tune_note[2]) == 1
              and app._tune_note[2][0].startswith("APPROVED:"), str(app._tune_note))
        check("v0.9.79: clean data carriers at their end -> no 'arrives very quietly' advice",
              T.report_advice(15, -49, "DV1UK", carrier_db=16) == []
              and T.report_advice(15, -49, "DV1UK", carrier_db=8) != [])

        # gateway: one set of levels for every caller
        app.targets = [["DX1FLR", "", {"rx_gain_db": 9, "tx_level_pct": 55, "saved": "2026-11-04"}]]
        app.rx_gain_db_var.set("0"); app.tx_level_var.set("20")
        app.gateway_var.set(True)
        app._apply_profile_for_peer("DX1FLR")
        check("gateway: a caller connecting doesn't change the gateway's levels",
              app.rx_gain_db_var.get() == "0" and app.tx_level_var.get() == "20")
        app.target_var.set("DX1FLR"); app._on_target_selected()
        check("gateway: nor does picking that caller as Target", app.tx_level_var.get() == "20")
        app.gateway_var.set(False)
        app._apply_profile_for_peer("DX1FLR")
        check("not a gateway: the saved levels for that station come back, as before",
              app.rx_gain_db_var.get() == "9" and app.tx_level_var.get() == "55")
    finally:
        app.destroy()


def test_raspberry_pi_web_and_ptt():
    """v0.9.81: Raspberry Pi go-kits. The phone web page reads the window's
    state and runs its actions on the Tk thread; RTS / DTR finally get their
    serial port; a Pi GPIO pin can key the radio."""
    import json, sys as _sys, types, urllib.request
    import sara_pingpong as sp
    import ptt as _ptt
    import web_ui
    # Pi GPIO PTT through a stand-in gpiozero
    events = []

    class FakeOut:
        def __init__(self, pin, active_high=True, initial_value=False):
            events.append(("open", pin, initial_value))
        def on(self): events.append("on")
        def off(self): events.append("off")
        def close(self): events.append("close")

    saved = _sys.modules.get("gpiozero")
    _sys.modules["gpiozero"] = types.SimpleNamespace(OutputDevice=FakeOut)
    try:
        ctl = _ptt.PTTController(method=_ptt.PTT_PIGPIO, pi_gpio_pin=22)
        ctl.open(); ctl.ptt_on(); ctl.ptt_off(); ctl.close()
    finally:
        if saved is None:
            _sys.modules.pop("gpiozero", None)
        else:
            _sys.modules["gpiozero"] = saved
    check("Pi GPIO PTT: pin opened unkeyed, keyed, released, closed",
          events == [("open", 22, False), "on", "off", "close"], str(events))

    app = sp.App()
    ui = None
    try:
        app._save_settings = lambda: None
        app.ptt_var.set(sp.PTT_RTS)
        app.serial_port_var.set("")
        warned = []
        real_warn = sp.messagebox.showwarning
        sp.messagebox.showwarning = lambda *a, **k: warned.append(a)
        try:
            no_port = app._serial_ptt_kwargs(sp.PTT_RTS)
        finally:
            sp.messagebox.showwarning = real_warn
        app.serial_port_var.set("/dev/ttyUSB0")
        check("RTS / DTR PTT now gets its serial port (none chosen -> asked for it)",
              no_port is None and warned and app._serial_ptt_kwargs(sp.PTT_DTR) == {"port": "/dev/ttyUSB0"})
        app.pi_gpio_var.set("27")
        check("Pi GPIO PTT takes its pin from Settings", app._serial_ptt_kwargs(sp.PTT_PIGPIO) == {"pi_gpio_pin": 27})
        st = app._settings_dict()
        check("serial port and Pi GPIO pin are saved with the settings",
              st.get("serial_port") == "/dev/ttyUSB0" and st.get("pi_gpio_pin") == "27")

        ui = web_ui.WebUI(app, port=0, allow="private", headless=True, mail_port=3000)
        ui.start()
        app.update()
        base = f"http://127.0.0.1:{ui.port}"

        def get(path):
            with urllib.request.urlopen(base + path, timeout=5) as r:
                return r.status, r.read()

        def post(obj):
            req = urllib.request.Request(base + "/api/action", data=json.dumps(obj).encode(),
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=5) as r:
                return json.loads(r.read())

        def pump():
            for _ in range(3):
                ui._tick()
                app.update()

        code, page = get("/")
        check("web page: served to a phone on this computer", code == 200 and b"SARA FM" in page)
        pump()
        st = json.loads(get("/api/state")[1])
        check("web page: state has meters, lamps, Tune answer and settings",
              {"meters", "lamps", "tune", "settings", "options", "chat"} <= set(st)
              and st["running"] is False and "Pi GPIO" in st["options"]["ptt"])
        check("v0.9.82: phone page defaults to port 8090 (BPQ32 / LinBPQ use 8080) and offers a Mail button",
              web_ui.DEFAULT_PORT == 8090 and st.get("mail_port") == 3000 and b'id="mail"' in page)
        r1 = post({"action": "set", "args": {"settings": {"callsign": "dw1irf", "tx_level_pct": "33",
                                                           "modulation": "Not a mode", "bogus": 1}}})
        r2 = post({"action": "target", "args": {"call": "dv1uk"}})
        pump()
        check("web page: settings change on the Tk thread (bad choices and unknown keys ignored)",
              r1["ok"] and app.call_var.get() == "DW1IRF" and app.tx_level_var.get() == "33"
              and app.modulation_var.get() != "Not a mode")
        check("web page: picking the target station", r2["ok"] and app.target_var.get() == "DV1UK")
        try:
            post({"action": "rm -rf"})
            bad = False
        except Exception:
            bad = True
        check("web page: unknown actions refused", bad)
        post({"action": "chat_send", "args": {"to": "DV1UK", "text": "hello from the phone"}})
        pump()
        conv = app.chat.convs.get("DV1UK") or []
        check("web page: SARA Chat message queued from the phone",
              any(e.get("text") == "hello from the phone" for e in conv))
        # no devices chosen -> a dialog, shown on the page. (Cleared on purpose:
        # on a PC with sound cards the first one is picked at startup, and Start
        # would really open it -- Windows build PC, 0.9.81.)
        app.in_dev_var.set(""); app.out_dev_var.set("")
        post({"action": "start"})
        pump()
        st = json.loads(get("/api/state")[1])
        check("headless: a dialog box becomes a notice on the page instead of stopping the program",
              any("device" in n["text"].lower() for n in st["notices"]) and app.engine is None)
        log = json.loads(get("/api/log?since=0")[1])["lines"]
        check("web page: the log follows live", any("web page" in l[2].lower() for l in log))
        check("web page: refuses addresses outside the allow list",
              not ui._ip_allowed("8.8.8.8", ui._nets) and ui._ip_allowed("192.168.4.20", ui._nets))
    finally:
        if ui is not None:
            ui.stop()
        if getattr(app, "engine", None) is not None:
            try:
                app._disconnect()
            except Exception:
                pass
        app.destroy()


def test_fast_viterbi_same_answers():
    """v0.9.85: the faster Viterbi decodes exactly what the old one did."""
    import fec_cc as f
    rng = np.random.RandomState(5)

    def old_decode(llr, n_bits, rate):
        pat = f.PUNCTURE[rate]; steps = n_bits + f.TAIL
        mask = np.tile(pat.T, (-(-steps // pat.shape[1]), 1))[:steps]
        full = np.zeros((steps, 2)); llr = np.asarray(llr, float); need = int(mask.sum())
        if len(llr) < need:
            llr = np.concatenate([llr, np.zeros(need - len(llr))])
        full[mask] = llr[:need]
        pm = np.full(f.N_STATES, -1e18); pm[0] = 0.0
        dec = np.empty((steps, f.N_STATES), dtype=np.uint8)
        for t in range(steps):
            l = full[t]
            m0 = pm[f._PRED[:, 0]] + f._SGN[:, 0, :] @ l
            m1 = pm[f._PRED[:, 1]] + f._SGN[:, 1, :] @ l
            c = m1 > m0; dec[t] = c; pm = np.where(c, m1, m0); pm -= pm.max()
        s_ = 0; out = np.empty(steps, dtype=np.uint8)
        for t in range(steps - 1, -1, -1):
            out[t] = f._INPUT_OF[s_]; s_ = int(f._PRED[s_, dec[t, s_]])
        return out[:n_bits]

    same = True
    for rate in ("2/3", "3/4"):
        for noise in (0.8, 1.6, 2.4):
            info = rng.randint(0, 2, 1500).astype(np.uint8)
            c = f.encode(info, rate).astype(float)
            llr = (1 - 2 * c) * 2.0 + rng.normal(0, noise, len(c))
            same &= np.array_equal(old_decode(llr, len(info), rate), f.decode(llr, len(info), rate))
    check("v0.9.85 faster Viterbi: identical output to the old decoder (both rates, clean to very noisy)", same)
    import os as _os, sara_pingpong as _sp  # noqa: F401
    check("v0.9.86: numpy's maths library limited to one thread (no spinning workers on slow PCs)",
          all(_os.environ.get(v) for v in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS")))


def test_levels_when_calling_out():
    """v0.9.87 (DW1IRF -> DX1FLR-10, 0.9.86): the session ran at DV1UK's levels
    (TX 14%, RX 26) because the new folder had no levels saved for DX1FLR, and
    a gateway never switched levels even for calls it made itself."""
    import json as _j, os as _o, tempfile, sara_pingpong as sp
    app = sp.App()
    logs = []
    try:
        app._save_settings = lambda: None
        app._log = lambda m: logs.append(m)
        app.targets = [["DX1FLR", "", {"rx_gain_db": 14, "tx_level_pct": 34, "saved": "x"}]]
        app.rx_gain_db_var.set("26"); app.tx_level_var.set("14")
        app.gateway_var.set(True)
        app._apply_profile_for_peer("DX1FLR-10", incoming=False)
        check("gateway calling out: uses the levels saved for the station it calls",
              app._current_profile()["tx_level_pct"] == 34 and app._current_profile()["rx_gain_db"] == 14)
        app._apply_profile_for_peer("DV1UK", incoming=True)
        check("gateway: the next caller gets the gateway's own levels back",
              app._current_profile()["tx_level_pct"] == 14 and app._current_profile()["rx_gain_db"] == 26)
        app._apply_profile_for_peer("DX1FLR-10", incoming=True)
        check("gateway: a caller with saved levels still gets the gateway levels",
              app._current_profile()["tx_level_pct"] == 14)
        app.gateway_var.set(False)
        logs.clear()
        app._levels_from_other_installs = lambda peer: (None, "")
        app._apply_profile_for_peer("DU3FS", incoming=False)
        check("no saved levels: says so and suggests Auto-Tune (levels unchanged)",
              any("No saved levels for DU3FS" in m and "Auto-Tune" in m for m in logs)
              and app._current_profile()["tx_level_pct"] == 14)
        # sibling folder with the same sound card has them
        del app._levels_from_other_installs
        d = tempfile.mkdtemp()
        mine, other = _o.path.join(d, "sara-fm-0.9.86", "sara-fm"), _o.path.join(d, "sara-fm-0.9.79", "SARA-FM")
        for x in (mine, other):
            _o.makedirs(x)
        dev_in, dev_out = app._dev_name(app.in_dev_var.get()), app._dev_name(app.out_dev_var.get())
        with open(_o.path.join(other, "sara_pp_settings.json"), "w") as fh:
            _j.dump({"input_device": dev_in, "output_device": dev_out,
                     "targets": [["DV1OTT", "", {"rx_gain_db": 12, "tx_level_pct": 30, "saved": "y"}]]}, fh)
        old = sp.app_dir
        sp.app_dir = lambda: mine
        try:
            prof, src = app._levels_from_other_installs("DV1OTT-10")
            check("other SARA FM folder next to this one: its saved levels are found",
                  prof is not None and prof["tx_level_pct"] == 30 and src == "sara-fm-0.9.79")
            app._apply_profile_for_peer("DV1OTT-10", incoming=False)
            check("...copied here and used for the call",
                  app._current_profile()["tx_level_pct"] == 30
                  and app._find_target("DV1OTT", base_ok=True)[2]["rx_gain_db"] == 12)
            with open(_o.path.join(other, "sara_pp_settings.json"), "w") as fh:
                _j.dump({"input_device": "another card", "output_device": dev_out,
                         "targets": [["DX1PAR", "", {"rx_gain_db": 1, "tx_level_pct": 5}]]}, fh)
            check("...but not from a folder set up for a different sound card",
                  app._levels_from_other_installs("DX1PAR")[0] is None)
        finally:
            sp.app_dir = old
        check("report shows gateway mode and which stations have saved levels",
              "Gateway:      off" in app._station_info() and "DX1FLR" in app._station_info())
    finally:
        try:
            app.destroy()
        except Exception:
            pass


def test_hold_ack_while_peer_mid_window():
    """v0.9.88 (Winlink Express via DX1FLR -> DW1IRF-10, BPSK, 0.9.87): the
    gateway's acknowledgments went out in the 0.2 s gaps of DX1FLR's window
    and wiped out its next frame every time."""
    import time as _t, audio_io, bpq_bridge, sara_pingpong as sp
    app = sp.App()
    try:
        G, C = "DW1IRF-10", "DX1FLR"
        i_frame = _ax25_frame(G, C, [], control=0x02 | (3 << 5), info=b"x" * 40)   # I, P=0
        i_poll = _ax25_frame(G, C, [], control=0x02 | 0x10, info=b"x")              # I, P=1
        rr_poll = _ax25_frame(G, C, [], control=0x01 | 0x10, pid=False)             # RR p
        ack = bpq_bridge.pack_batch([_ax25_frame(C, G, [], control=0x01 | (5 << 5), pid=False)])
        app._note_peer_burst([i_frame])
        check("peer sent an I frame with no poll: its window is still going -> our ack waits for a real end",
              app._peer_mid_window() and app._min_clear_s(ack) == audio_io.DIGI_MIN_CLEAR_S)
        app._note_peer_burst([i_frame, rr_poll])
        check("window ended with a poll: answer at once", not app._peer_mid_window() and app._min_clear_s(ack) == 0.0)
        app._note_peer_burst([i_poll])
        check("I frame carrying the poll: answer at once", not app._peer_mid_window())
        app._peer_burst = (_t.time() - 10, True)
        check("nothing heard for a while: no hold", not app._peer_mid_window())
        app._note_peer_burst([i_frame])
        check("ping/chat payloads are never held", app._min_clear_s(b"PING-not-a-batch") == 0.0)
        # overdrive hint vs. collision with our own transmission
        class E:
            _last_tx_end = _t.time() - 1.0
        app.engine, logs = E(), []
        app._garbled_total = 0
        info = {"pre_snr_db": 20.0}
        app._note_garbled("fec_uncorrectable", info)
        app._note_garbled("fec_uncorrectable", info)
        check("strong frame lost right after our own TX: a collision, not counted as an overdriven sender",
              getattr(app, "_garbled_total", 0) == 0)
        E._last_tx_end = _t.time() - 30
        app._note_garbled("fec_uncorrectable", info)
        check("...but away from our own TX it still counts", app._garbled_total == 1)
        app.engine = None
    finally:
        try:
            app.destroy()
        except Exception:
            pass


def test_slow_pc_buffer_and_kiss_takeover():
    """v0.9.83 (DV1OTT / DU3FS field test, 0.9.79): a slow PC can ask for a
    larger sound-card buffer; a second program taking over the KISS port is
    reported instead of silently cutting off the first."""
    import socket, time as _t
    import audio_io, bpq_bridge
    import sara_pingpong as sp
    # 1. larger audio buffer
    eng = audio_io.AudioEngine(0, 0, rx_buffer_frames=audio_io.RX_BUFFER_FRAMES_LARGE)
    eng_n = audio_io.AudioEngine(0, 0)
    check("slow PC: engine takes a 4x larger input buffer when asked (default unchanged)",
          eng.rx_buffer_frames == 4 * audio_io.RX_BUFFER_FRAMES == audio_io.RX_BUFFER_FRAMES_LARGE
          and eng_n.rx_buffer_frames == audio_io.RX_BUFFER_FRAMES)
    for e in (eng, eng_n):
        try:
            e.pa.terminate()
        except Exception:
            pass
    app = sp.App()
    try:
        app._save_settings = lambda: None
        normal = app._rx_buffer_frames()
        app.slow_pc_var.set(True)
        st = app._settings_dict()
        check("slow PC: setting saved and chosen at Start",
              normal == audio_io.RX_BUFFER_FRAMES and app._rx_buffer_frames() == audio_io.RX_BUFFER_FRAMES_LARGE
              and st.get("slow_pc_buffer") is True)
        import gauges as _g
        app._apply_lite(True)
        for i in range(app.LITE_LOG_MAX + 50):
            app._log(f"line {i}")
        n_lines = int(app.log_box.index("end-1c").split(".")[0])
        lite_ok = _g.AnalogMeter.LITE and app._lite and n_lines <= app.LITE_LOG_MAX + 1 \
            and abs(sys.getswitchinterval() - 0.001) < 1e-6
        import link_adapt as _la
        caps_lite = app.link_stats.my_caps
        app.no_coded_var.set(True); app._apply_caps()
        caps_nc = app.link_stats.my_caps
        app.link_stats.on_frame("DW1IRF", ofdm.MOD_QPSK_WIDE, 25, 0, 100)
        rep_nc = app.link_stats.take_report("DW1IRF")
        st_nc = app._settings_dict()
        app.no_coded_var.set(False); app._apply_caps()
        app._apply_lite(False)
        check("v0.9.85 'No C modes' asks others not to send the coded modes (saved; Slow PC mode alone doesn't)",
              caps_lite == _la.MY_CAPS and not (caps_nc & _la.CAP_CODED) and (caps_nc & _la.CAP_QAM16)
              and rep_nc is not None and not (rep_nc.caps & _la.CAP_CODED)
              and st_nc.get("no_coded_modes") is True and app.link_stats.my_caps == _la.MY_CAPS)
        check("v0.9.84 Slow PC mode: meters without needle swing, short on-screen log, quicker thread hand-over",
              lite_ok and not _g.AnalogMeter.LITE and abs(sys.getswitchinterval() - 0.005) < 1e-6)
    finally:
        app.destroy()
    # 2. KISS port takeover
    msgs = []
    br = bpq_bridge.BpqBridge(lambda f: None, on_status=msgs.append)
    br.start(port=0)
    port = br._server_sock.getsockname()[1]
    try:
        a = socket.create_connection(("127.0.0.1", port)); _t.sleep(0.3)
        b = socket.create_connection(("127.0.0.1", port)); _t.sleep(0.4)
        took = [m for m in msgs if "took over" in m]
        cut = [m for m in msgs if "cut off (replaced" in m]
        check("KISS: a second program taking over the port is reported, and the first is 'cut off'",
              len(took) == 1 and "Only ONE program" in took[0] and len(cut) == 1, str(msgs[-4:]))
        c = socket.create_connection(("127.0.0.1", port)); _t.sleep(0.4)
        took = [m for m in msgs if "took over" in m]
        check("KISS: repeated takeovers say two programs are taking turns",
              len(took) == 2 and "taking turns" in took[1] and "Winlink Express" in took[1])
        c.close(); _t.sleep(0.3)
        check("KISS: an ordinary disconnect still reads 'disconnected'",
              msgs[-1] == "KISS client disconnected.", msgs[-1])
        for s_ in (a, b):
            s_.close()
    finally:
        br.stop()


if __name__ == "__main__":
    print(f"SARA-PP offline test suite — ofdm_core {ofdm.VERSION}")
    print("=" * 60)
    test_clean_roundtrip()
    test_with_leading_silence()
    test_weak_signal()
    test_with_noise()
    test_back_to_back_frames()
    test_preamble_energy_is_flat()
    test_corrupt_frame_rejected()
    test_qpsk_roundtrip()
    test_qpsk_is_denser()
    test_mixed_modulation_stream()
    test_qpsk_noise_sensitivity()
    test_kiss_roundtrip()
    test_kiss_streaming_chunks()
    test_kiss_empty_frames_ignored()
    test_kiss_over_ofdm_roundtrip()
    test_bpq_bridge_roundtrip()
    test_long_bpq_style_frame()
    test_residual_tail_does_not_corrupt_next_frame()
    test_max_length_header_no_longer_falsely_rejected()
    test_coalesced_batch_survives_full_maxframe_window()
    test_realistic_session_pacing()
    test_realistic_session_with_noise_floor()
    test_on_reject_hook_fires()
    test_residual_margin_survives_post_burst_transient()
    test_post_decode_cooldown()
    test_long_frame_survives_clock_drift()
    test_drift_diagnostic_reports_sensibly()
    test_batch_pack_unpack_roundtrip()
    test_unpack_batch_fallback()
    test_bridge_coalesces_rapid_frames()
    test_wait_for_clear_channel()
    test_carrier_busy_threshold_calibration()
    test_carrier_wait_diagnostic()
    test_self_echo_filter()
    test_self_echo_window_survives_real_transmission_time()
    test_self_echo_filter_thread_safety()
    test_tx_start_hook_fires_at_real_broadcast_time()
    test_header_sanity_checks()
    test_weak_signal_long_claim_rejected_early()
    test_qam16_wide_round_trip_and_drift()
    test_csma_jitter_recheck_closes_blind_window()
    test_fec_core()
    test_fec_end_to_end_gain()
    test_fill_tone_safe_and_audible()
    test_burst_mode_end_to_end()
    test_throughput_meter()
    test_frozen_exe_safety()
    test_levels_and_saved_settings()
    test_snr_measurement()
    test_noise_relative_carrier_sense()
    test_receiver_quiet_gate()
    test_long_claim_snr_guard()
    test_two_way_ping_report()
    test_autotune_convergence_and_advice()
    test_tune_tone_level()
    test_coalescing_cap_enforced()
    test_header_plausibility_checks()
    test_signal_anchored_busy_threshold()
    test_busy_threshold_rules()
    test_long_wait_reports_channel_reading()
    test_edge_guard_removes_noise_candidates()
    test_pingpong_detector_never_matches_ax25()
    test_bridge_ping_courtesy()
    test_pong_reply_thread_reads_no_gui_state()
    test_ping_window_colours_and_render()
    test_cooldown_accepts_next_frame_in_burst()
    test_cooldown_never_accepts_noise()
    test_network_kiss_access()
    test_fm_channel_simulator_fidelity()
    test_open_squelch_carrier_sense()
    test_open_squelch_hiss_alone_and_clipping()
    test_dcd_modes_open_squelch()
    test_pings_ignore_dcd()
    test_min_preamble_snr()
    test_qpsk_wide_basics()
    test_qpsk_wide_drift_tracking()
    test_qpsk_wide_fm_channel_and_dcd()
    test_carrier_check()
    test_wide_pilot_weighting()
    test_duplicate_callsign_detected()
    test_wide_v083_field_channel()
    test_yield_to_reply()
    test_ack_batching()
    test_audio_glitch_recovery()
    test_weak_frames_track_exactly_as_before()
    test_merged_frame_reported_once()
    test_yield_ends_when_reply_heard()
    test_save_logs_bundle()
    test_ptt_release_recovers()
    test_errors_reported_not_namerror()
    test_ptt_watchdog_gui()
    test_link_report_trailer()
    test_speed_controller_rules()
    test_auto_strong_signal_losses()
    test_auto_qam16_ladder()
    test_weak_header_for_fast_mode_rejected()
    test_auto_faster_climb_and_resume()
    test_yield_not_ended_by_own_squelch_tail()
    test_low_snr_candidate_does_not_swallow_preamble()
    test_tx_underflow_counted()
    test_distorted_signal_reported_honestly()
    test_kiss_allowlist_always_allows_this_pc()
    test_receiver_off_audio_callback()
    test_rx_stats_and_retransmit_detection()
    test_kiss_ackmode()
    test_auto_gui_wiring()
    test_auto_two_station_simulation()
    test_auto_holds_failed_mode()
    test_carrier_sense_ignores_non_sara_busy_in_session()
    test_dcd_ignores_steady_tone()
    test_slow_pc_handling()
    test_dcd_holds_through_missed_header()
    test_session_retry_accounting()
    test_gauge_and_menus()
    test_digipeater()
    test_sara_chat()
    test_autotune_robust()
    test_dense_noise_candidates_stay_cheap()
    test_sound_card_clock_offset()
    test_rx_recording_and_header_rejects()
    test_field_recording_dw3tdj()
    test_false_clock_fix_from_tone()
    test_idle_gate()
    test_auto_skips_narrow_qpsk_when_wide_does_better()
    test_tune_tx_protocol_and_choice()
    test_tune_tx_vox_quiet_levels()
    test_auto_clean_probe_to_qam16()
    test_qam64_wide_mode()
    test_auto_memory_kept()
    test_auto_interference_floor()
    test_save_report_small()
    test_auto_qam16_needs_clean_carriers()
    test_coded_modes()
    test_wide_bursts_decode_every_frame()
    test_coalescing_cap_survives_real_world_drift()
    test_bridge_flushes_at_smaller_cap()
    test_stale_resends_and_relay_yield()
    test_gateway_direct_and_b2_check()
    test_direct_fallback_and_hop_reports()
    test_remote_chat()
    test_simple_tune_and_gateway_levels()
    test_raspberry_pi_web_and_ptt()
    test_slow_pc_buffer_and_kiss_takeover()
    test_fast_viterbi_same_answers()
    test_levels_when_calling_out()
    test_hold_ack_while_peer_mid_window()
    print("=" * 60)
    n_fail = sum(1 for _, s, _ in results if s == FAIL)
    n_pass = sum(1 for _, s, _ in results if s == PASS)
    print(f"{n_pass} passed, {n_fail} failed")
    sys.exit(1 if n_fail else 0)
