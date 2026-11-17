"""Blocking RTSP/HTTP connection over one TCP socket, with optional HAP crypto
processors on the wire (transparent encrypt/decrypt of the byte stream).
"""

import re
import socket
import uuid
from dataclasses import dataclass, field
from typing import Callable, Optional

_STATUS_RE = re.compile(r"^(RTSP|HTTP)/([0-9.]+) (\d+) (.*)")


@dataclass
class Response:
    protocol: str
    code: int
    message: str
    headers: dict
    body: bytes
    raw: bytes


class RtspConn:
    def __init__(self, host: str, port: int, timeout: float = 10.0,
                 log: Optional[list] = None) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.sock: Optional[socket.socket] = None
        self.send_processor: Callable[[bytes], bytes] = lambda d: d
        self.recv_processor: Callable[[bytes], bytes] = lambda d: d
        self.cseq = 0
        self.dacp_id = uuid.uuid4().hex[:16].upper()
        self.active_remote = int.from_bytes(uuid.uuid4().bytes[:4], "big")
        self._buf = b""
        self.log = log if log is not None else []

    def connect(self) -> None:
        self.sock = socket.create_connection((self.host, self.port), self.timeout)
        self.sock.settimeout(self.timeout)

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            finally:
                self.sock = None

    @property
    def local_ip(self) -> str:
        return self.sock.getsockname()[0] if self.sock else "0.0.0.0"

    def _log(self, direction: str, text: str) -> None:
        self.log.append(f"[{direction}] {text}")

    def _read_response(self) -> Response:
        assert self.sock is not None
        while True:
            # Try to parse a complete message from the (decrypted) buffer.
            if b"\r\n\r\n" in self._buf:
                head, _, rest = self._buf.partition(b"\r\n\r\n")
                head_str = head.decode("utf-8", "ignore")
                lines = head_str.split("\r\n")
                m = _STATUS_RE.match(lines[0])
                if not m:
                    raise ValueError(f"bad status line: {lines[0]!r}")
                protocol, _ver, code, message = m.group(1), m.group(2), int(m.group(3)), m.group(4)
                headers: dict[str, str] = {}
                for line in lines[1:]:
                    if ": " in line:
                        k, v = line.split(": ", 1)
                        headers[k.lower()] = v
                clen = int(headers.get("content-length", "0"))
                if len(rest) < clen:
                    # Need more body bytes.
                    chunk = self.sock.recv(65536)
                    if not chunk:
                        raise ConnectionError("socket closed before body")
                    self._buf += self.recv_processor(chunk)
                    continue
                body = rest[:clen]
                self._buf = rest[clen:]
                resp = Response(protocol, code, message, headers, body, head + b"\r\n\r\n" + body)
                self._log("RX", f"{protocol} {code} {message} len={len(body)}")
                return resp
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("socket closed")
            self._buf += self.recv_processor(chunk)

    def request(self, method: str, uri: str,
                protocol: str = "RTSP/1.0",
                headers: Optional[dict] = None,
                body: bytes = b"",
                content_type: Optional[str] = None,
                user_agent: str = "AirPlay/550.10",
                allow_error: bool = False) -> Response:
        assert self.sock is not None
        self.cseq += 1
        hdrs: dict[str, str] = {
            "CSeq": str(self.cseq),
            "DACP-ID": self.dacp_id,
            "Active-Remote": str(self.active_remote),
            "Client-Instance": self.dacp_id,
        }
        if headers:
            hdrs.update(headers)
        lines = [f"{method} {uri} {protocol}", f"User-Agent: {user_agent}"]
        for k, v in hdrs.items():
            lines.append(f"{k}: {v}")
        if content_type:
            lines.append(f"Content-Type: {content_type}")
        if body:
            lines.append(f"Content-Length: {len(body)}")
        msg = ("\r\n".join(lines) + "\r\n\r\n").encode("utf-8") + body
        self._log("TX", f"{method} {uri} {protocol} body={len(body)}B")
        self.sock.sendall(self.send_processor(msg))
        resp = self._read_response()
        if not allow_error and not (200 <= resp.code < 300):
            raise RuntimeError(
                f"{method} {uri} -> {resp.code} {resp.message}: "
                f"{resp.body[:200]!r}"
            )
        return resp
