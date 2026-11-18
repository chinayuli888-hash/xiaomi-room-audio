# SPDX-License-Identifier: GPL-3.0-only AND MIT AND Apache-2.0
# Xiaomi Room Audio: Python adaptation and modifications, 2026-11-18.
# Timestamp helpers adapt pyatv (Copyright (c) 2020 Pierre Ståhl, MIT).
# The ALAC bit-packer adapts airplay2-sender-cpp by Andreas Wendorf (Apache-2.0).
# New project work is GPL-3.0-only; upstream portions retain their notices.
# See ../NOTICE and ../docs/PROVENANCE.md for the source and license mapping.
"""Timeline (NTP/RTP timestamp) helpers, WAV loading and RTP packet builders."""

import struct
import wave
from time import time_ns

import numpy as np

RATE = 44100
CHANNELS = 2
FRAMES_PER_PACKET = 352
# Sender-announced playout lead, in frames.
# default: pyatv/cpp conservative model 22050 + sample_rate (1.5 s), proven stable.
# low:    owntone/shairport min latency 11025 (250 ms); underrun risk on jitter.
LATENCY_DEFAULT = 22050 + RATE
LATENCY_LOW = 11025
LATENCY = LATENCY_DEFAULT


# ---------- NTP / timestamp math (ported from pyatv raop/timing.py) ----------

def ntp_now() -> int:
    now_us = time_ns() / 1000
    seconds = int(now_us / 1_000_000)
    frac = int(now_us - seconds * 1_000_000)
    return ((seconds + 0x83AA7E80) << 32) | (int((frac << 32) / 1_000_000))


def ntp2parts(ntp: int) -> tuple[int, int]:
    return ntp >> 32, ntp & 0xFFFFFFFF


def ntp2ts(ntp: int, rate: int) -> int:
    return int((ntp >> 16) * rate) >> 16


def ts2ntp(timestamp: int, rate: int) -> int:
    return int(int(timestamp << 16) / rate) << 16


# ---------- WAV loading ----------

def load_wav_stereo(path: str) -> np.ndarray:
    """Return interleaved stereo int16 PCM at 44100 Hz (mono upmixed, no resample)."""
    with wave.open(path, "rb") as w:
        nch = w.getnchannels()
        sr = w.getframerate()
        sw = w.getsampwidth()
        n = w.getnframes()
        raw = w.readframes(n)
    if sw != 2:
        raise ValueError(f"only 16-bit PCM supported, sampwidth={sw}")
    if sr != RATE:
        raise ValueError(f"sample rate {sr} != {RATE}; resample first with ffmpeg")
    x = np.frombuffer(raw, dtype=np.int16)
    if nch == 1:
        x = np.stack([x, x], axis=1).reshape(-1)
    elif nch == 2:
        pass
    else:
        x = x.reshape(-1, nch)[:, :2].reshape(-1)
    return x.astype(np.int16)


def frames_to_payload(frames: np.ndarray) -> bytes:
    """Interleaved int16 stereo -> big-endian L16 payload (RFC 3551 network order)."""
    be = frames.astype(">i2")
    return be.tobytes()


def encode_alac_frame(frames: np.ndarray, n_frames: int) -> bytes:
    """Wrap interleaved int16 stereo samples in an uncompressed ALAC escape frame.

    Bit layout (MSB-first, per shairport-sync alac.c / cpp encodeAlacFrame_):
      3b channel tag = 1 (stereo CPE) . 4b unused=0 . 12b unknown=0 .
      1b hasSize=0 . 2b wastedBytes=0 . 1b isNotCompressed=1 .
      nFrames x {L16,R16} MSB-first . 3b END tag=7 . zero-pad to byte.

    Fast path: the header (23 bits) and END (3 bits) are merged around the
    big-endian sample block with integer shifts, avoiding a per-sample Python
    bit loop.
    """
    n = n_frames * CHANNELS
    flat = frames[:n].astype(">i2")
    sample_be = flat.tobytes()
    sample_int = int.from_bytes(sample_be, "big")
    header = (1 << 20) | 1  # 23-bit: top tag 001 (stereo CPE), bottom bit 1 (notCompressed)
    sample_bits = n * 16
    total_bits = 23 + sample_bits + 3
    bits = (header << (sample_bits + 3)) | (sample_int << 3) | 0b111
    nbytes = (total_bits + 7) // 8
    # Left-align the bitstream: to_bytes right-aligns the integer, but the
    # header must sit in the first byte, so shift by the unused high bits.
    bits <<= 8 * nbytes - total_bits
    return bits.to_bytes(nbytes, "big")


def _encode_alac_frame_slow(frames: np.ndarray, n_frames: int) -> bytes:
    """Reference bit-by-bit packer (kept for byte-equality checks)."""
    out = bytearray()
    cur = 0
    filled = 0

    def put(value: int, bits: int) -> None:
        nonlocal cur, filled
        for i in range(bits - 1, -1, -1):
            cur = ((cur << 1) | ((value >> i) & 1)) & 0xFF
            filled += 1
            if filled == 8:
                out.append(cur)
                cur = 0
                filled = 0

    put(1, 3)
    put(0, 4)
    put(0, 12)
    put(0, 1)
    put(0, 2)
    put(1, 1)
    flat = frames[:n_frames * CHANNELS]
    for s in flat.astype(">i2"):
        put(int(s) & 0xFFFF, 16)
    put(7, 3)
    if filled > 0:
        out.append(cur << (8 - filled))
    return bytes(out)


# ---------- RTP packet builders ----------

def rtp_header(marker: bool, seq: int, rtptime: int, ssrc: int) -> bytes:
    b1 = 0xE0 if marker else 0x60
    return struct.pack(
        ">BBHII", 0x80, b1, seq & 0xFFFF,
        rtptime & 0xFFFFFFFF, ssrc & 0xFFFFFFFF,
    )


def sync_packet(first: bool, rtptime: int, latency: int, head_ts: int,
                rate: int) -> bytes:
    cur_ntp = ts2ntp(head_ts, rate)
    sec, frac = ntp2parts(cur_ntp)
    b0 = 0x90 if first else 0x80
    return struct.pack(
        ">BBHIIII", b0, 0xD4, 0x0007,
        (rtptime - latency) & 0xFFFFFFFF, sec, frac, rtptime & 0xFFFFFFFF,
    )


def timing_reply(req: bytes, now_ntp: int) -> bytes:
    """Reply to a TimingPacket (type 0xD3): echo request sendtime as reftime."""
    sec, frac = ntp2parts(now_ntp)
    reftime = req[24:32]
    return req[0:1] + struct.pack(">BH", 0xD3, 0x0007) + struct.pack(">I", 0) + \
        reftime + struct.pack(">IIII", sec, frac, sec, frac)
