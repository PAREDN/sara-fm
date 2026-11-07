# SARA FM test vectors

Reference recordings for checking a SARA FM implementation, made by the
reference code (SARA FM 0.9.79). Each vector is two files:

- **`<name>.wav`**: 48 kHz, mono, 16-bit PCM. It holds 0.3 s of silence, one
  frame with a peak of −6 dBFS, then 0.3 s of silence. The `noisy-*` files add
  white noise.
- **`<name>.json`**: the payload and every encoding stage of that frame.

## Vectors

| Group | Files | What they check |
| --- | --- | --- |
| Every mode, clean | `clean-bpsk` … `clean-64qam-wide` (7) | The same AX.25 batch in each mode |
| Every mode, noisy | `noisy-*` (7) | Same frames with white noise about 1–2 dB above where decoding fails |
| Multi-block | `multiblock-qpsk`, `multiblock-16qam-wide-c` | Payloads longer than one Reed-Solomon block (byte interleave) |
| Payload types | `ping-bpsk`, `pong-bpsk`, `chat-qpsk`, `batch-report-qpsk-wide` | Ping, pong with two-way report, SARA Chat message, batch with link report |

`index.json` lists them all.

## JSON fields

Names in brackets are the spec section each field follows (`docs/SPEC.md`).

| Field | Meaning |
| --- | --- |
| `payload_hex` | The payload handed to the modem (section 5) |
| `mode`, `mode_id` | Mode name and internal number (section 3.3) |
| `crc16` | CRC-16/CCITT-FALSE of the payload (section 4.1, step 1) |
| `rs_input_hex` | Payload + CRC, the input to Reed-Solomon |
| `rs_blocks_data_bytes` | Data bytes in each Reed-Solomon block |
| `on_air_bytes_hex` | After Reed-Solomon and the byte interleave (section 4.1, steps 2–3) |
| `header` | Header code, length, and the 12 header bits, MSB first (section 3.2) |
| `convolutional` | Coded modes only: the coded bits, and the bits after the interleaver, packed as hex MSB first with zero-padding to a whole byte (section 4.2) |
| `training_bpsk` | Wide modes only: training-symbol bits on all 22 carriers, position 0 first |
| `constellation_points` | For each data symbol, the `[re, im]` value of each data carrier in position order, before the IFFT (section 3.4) |
| `frame_start_sample`, `frame_samples` | Where the frame sits in the WAV |
| `reference_decode` | What the reference decoder measured on this WAV |

## How to use them

- **Checking the reference code:** run `python test-vectors/verify.py`. For each
  vector, it decodes the WAV and checks the payload. It recomputes every stage
  and compares it to the JSON. For clean vectors, it also re-encodes the audio
  and requires a match within 1 LSB.
- **Checking an encoder:** encode `payload_hex` in `mode` and compare each stage
  with the JSON. The first stage that differs is the one to fix. Audio from
  another FFT library can differ by a rounding step, so compare samples with a
  small tolerance rather than byte for byte.
- **Checking a decoder:** feed it the WAV. It should return exactly
  `payload_hex`. The noisy files check that its error correction really works.

To regenerate the files after an intentional format change, run
`python tools/make_test_vectors.py`. The output is deterministic, so unchanged
code gives identical files.

These files are licensed under CC BY 4.0, like the specification.
