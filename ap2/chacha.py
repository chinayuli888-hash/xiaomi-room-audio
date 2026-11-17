"""ChaCha20-Poly1305 layers: HAP control/event channel framing + audio payload.

Nonce convention (HAP): 12-byte IETF nonce = 4 zero bytes || 8-byte LE counter.
Ported in behavior from pyatv support/chacha20.py + auth/hap_session.py.
"""

from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

NONCE_LENGTH = 12
FRAME_LENGTH = 1024
AUTH_TAG_LENGTH = 16


def _nonce(counter: int) -> bytes:
    return b"\x00\x00\x00\x00" + counter.to_bytes(8, "little")


class HAPSession:
    """Transparent encrypted framing for one TCP control/event channel."""

    def __init__(self) -> None:
        self._out: ChaCha20Poly1305 | None = None
        self._in: ChaCha20Poly1305 | None = None
        self._out_counter = 0
        self._in_counter = 0
        self._in_buf = b""

    def enable(self, out_key: bytes, in_key: bytes) -> None:
        self._out = ChaCha20Poly1305(out_key)
        self._in = ChaCha20Poly1305(in_key)

    @property
    def encrypted(self) -> bool:
        return self._out is not None

    def encrypt(self, data: bytes) -> bytes:
        if self._out is None:
            return data
        out = b""
        while data:
            frame = data[:FRAME_LENGTH]
            data = data[FRAME_LENGTH:]
            length = len(frame).to_bytes(2, "little")
            ct = self._out.encrypt(_nonce(self._out_counter), frame, length)
            self._out_counter += 1
            out += length + ct
        return out

    def decrypt(self, data: bytes) -> bytes:
        if self._in is None:
            return data
        self._in_buf += data
        out = b""
        while self._in_buf:
            length = self._in_buf[:2]
            if len(length) < 2:
                break
            block_length = int.from_bytes(length, "little") + AUTH_TAG_LENGTH
            if len(self._in_buf) < 2 + block_length:
                break
            block = self._in_buf[2:2 + block_length]
            pt = self._in.decrypt(_nonce(self._in_counter), block, length)
            self._in_counter += 1
            out += pt
            self._in_buf = self._in_buf[2 + block_length:]
        return out


class AudioCipher:
    """Per-packet ChaCha20-Poly1305 for the realtime audio payload."""

    def __init__(self, key: bytes) -> None:
        if len(key) != 32:
            raise ValueError(f"audio key must be 32 bytes, got {len(key)}")
        self._aead = ChaCha20Poly1305(key)
        self._counter = 0

    def encrypt(self, payload: bytes, aad: bytes) -> bytes:
        nonce8 = self._counter.to_bytes(8, "little")
        ct = self._aead.encrypt(b"\x00\x00\x00\x00" + nonce8, payload, aad)
        self._counter += 1
        return ct + nonce8
