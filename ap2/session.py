# SPDX-License-Identifier: GPL-3.0-only AND MIT AND Apache-2.0
# Xiaomi Room Audio: Python adaptation and modifications, 2026-11-18.
# Protocol sequencing follows pyatv (Pierre Ståhl, MIT) and
# airplay2-sender-cpp (Andreas Wendorf, Apache-2.0), with OwnTone references.
# New project work is GPL-3.0-only; upstream portions retain their notices.
# See ../NOTICE and ../docs/PROVENANCE.md for the source and license mapping.
"""One AirPlay 2 (HAP transient) sender session to a single receiver."""

import plistlib
import socket
import struct
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from . import audio, tlv8
from .chacha import HAPSession
from .hap_srp import TransientSrp
from .rtsp import RtspConn, Response


def hkdf(salt: str, info: str, ikm: bytes, length: int = 32) -> bytes:
    hk = HKDF(algorithm=hashes.SHA512(), length=length,
              salt=salt.encode(), info=info.encode())
    return hk.derive(ikm)


@dataclass
class SessionStats:
    data_port: int = 0
    control_port: int = 0
    event_port: int = 0
    packets_sent: int = 0
    first_send_monotonic: Optional[float] = None
    last_send_monotonic: Optional[float] = None
    packet_log: list = field(default_factory=list)
    events: list = field(default_factory=list)
    # Liveness / reconnect bookkeeping (driven by the group watchdog).
    last_healthy_ts: Optional[float] = None
    last_error: str = ""
    reconnect_count: int = 0
    last_reconnect_ts: Optional[float] = None


class AirPlay2Session:
    def __init__(self, host: str, port: int = 7000,
                 name: str = "python-ap2", model: str = "iPhone14,3",
                 log: Optional[list] = None, open_event_channel: bool = False,
                 codec: str = "alac", volume_db: float = 0.0,
                 latency_frames: Optional[int] = None):
        self.host = host
        self.port = port
        self.name = name
        self.model = model
        self.log = log if log is not None else []
        self.open_event_channel = open_event_channel
        self._codec = codec
        self._volume_db = volume_db
        self.latency_frames = latency_frames or audio.LATENCY

        self.conn = RtspConn(host, port, log=self.log)
        self.session_id = uuid.uuid4().int & 0xFFFFFFFF
        self.device_id = ":".join(f"{b:02X}" for b in uuid.uuid4().bytes[:6])
        self.session_uuid = str(uuid.uuid4()).upper()
        self.srp: Optional[TransientSrp] = None
        self.shared: bytes = b""
        self.hap = HAPSession()
        self.stats = SessionStats()

        self._sock_audio: Optional[socket.socket] = None
        self._sock_control: Optional[socket.socket] = None
        self._sock_timing: Optional[socket.socket] = None
        self._event_sock: Optional[socket.socket] = None
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()
        self._event_hap: Optional[HAPSession] = None
        # Retransmit backlog (RAOP 0x55 request -> 0xD6 reply), matched to
        # pyatv/cpp/saundo: keep the last N audio packets per session.
        self._backlog: dict[int, bytes] = {}
        self._backlog_order: list[int] = []
        self._BACKLOG_MAX = 1024

    # ------------------------------------------------------------------ health

    def mark_healthy(self) -> None:
        self.stats.last_healthy_ts = time.monotonic()

    def is_alive(self, stale_after: float = 10.0) -> bool:
        """Control socket open and a successful control exchange within the
        last `stale_after` seconds. UDP sendto cannot detect a dead peer, so
        liveness is judged on the TCP control channel / feedback replies."""
        if self.conn.sock is None:
            return False
        if self.stats.last_healthy_ts is None:
            return False
        return (time.monotonic() - self.stats.last_healthy_ts) < stale_after

    # ------------------------------------------------------------------ setup

    def _pair_headers(self, hkp: int) -> dict:
        return {
            "Connection": "keep-alive",
            "X-Apple-HKP": str(hkp),
            "X-Apple-Client-Name": self.name,
            "Content-Type": "application/octet-stream",
        }

    def pair_transient(self) -> None:
        """HAP transient pair-setup (SRP, PIN 3939). Switches conn to encrypted."""
        self.conn.connect()

        # /pair-pin-start (best effort: some receivers 403 this on transient).
        try:
            self.conn.request("POST", "/pair-pin-start", protocol="HTTP/1.1",
                              headers=self._pair_headers(4), body=b"",
                              content_type="application/octet-stream",
                              allow_error=True)
        except Exception as e:
            self.stats.events.append(f"pair-pin-start warn: {e}")

        self.srp = TransientSrp()

        # M1 -> M2
        m1 = {
            tlv8.TlvValue.Method: b"\x00",
            tlv8.TlvValue.SeqNo: b"\x01",
            tlv8.TlvValue.Flags: bytes([tlv8.Flags.TransientPairing.value]),
        }
        r = self.conn.request("POST", "/pair-setup", protocol="HTTP/1.1",
                              headers=self._pair_headers(4),
                              body=tlv8.write_tlv(m1),
                              content_type="application/octet-stream")
        m2 = tlv8.read_tlv(r.body)
        if tlv8.TlvValue.Error in m2:
            raise RuntimeError(f"pair-setup M2 error TLV: {m2[tlv8.TlvValue.Error]!r}")
        salt = m2[tlv8.TlvValue.Salt]
        server_b = m2[tlv8.TlvValue.PublicKey]

        pub_a, proof = self.srp.process(server_b, salt)

        # M3 -> M4
        m3 = {
            tlv8.TlvValue.SeqNo: b"\x03",
            tlv8.TlvValue.PublicKey: pub_a,
            tlv8.TlvValue.Proof: proof,
        }
        r = self.conn.request("POST", "/pair-setup", protocol="HTTP/1.1",
                              headers=self._pair_headers(4),
                              body=tlv8.write_tlv(m3),
                              content_type="application/octet-stream")
        m4 = tlv8.read_tlv(r.body)
        if tlv8.TlvValue.Error in m4:
            raise RuntimeError(f"pair-setup M4 error TLV: {m4[tlv8.TlvValue.Error]!r}")
        proof = m4.get(tlv8.TlvValue.Proof)
        if proof is None or not self.srp.verify_server_proof(proof):
            raise RuntimeError("Receiver SRP proof verification failed")
        self.shared = self.srp.shared
        if len(self.shared) != 64:
            raise RuntimeError(f"unexpected SRP K length {len(self.shared)}")

        # Derive control channel keys and enable encryption on this socket.
        out_key = hkdf("Control-Salt", "Control-Write-Encryption-Key", self.shared)
        in_key = hkdf("Control-Salt", "Control-Read-Encryption-Key", self.shared)
        self.hap.enable(out_key, in_key)
        self.conn.send_processor = self.hap.encrypt
        self.conn.recv_processor = self.hap.decrypt
        self.stats.events.append("transient pair-setup ok; control channel encrypted")

    def _bind_udp(self) -> tuple[int, int, int]:
        local = self.conn.local_ip
        self._sock_audio = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock_control = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock_timing = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        for s in (self._sock_audio, self._sock_control, self._sock_timing):
            s.bind((local, 0))
            s.settimeout(0.5)
        return (self._sock_audio.getsockname()[1],
                self._sock_control.getsockname()[1],
                self._sock_timing.getsockname()[1])

    def _decode_plist(self, resp: Response) -> dict:
        if not resp.body:
            return {}
        try:
            return plistlib.loads(resp.body)
        except Exception as e:
            self.stats.events.append(f"bplist decode warn: {e}; body={resp.body[:80]!r}")
            return {}

    def setup(self) -> None:
        """Full AP2 setup after pair_transient(). Binds UDP, negotiates ports."""
        audio_port, control_port, timing_port = self._bind_udp()
        # Timing UDP must be answered DURING the handshake: the receiver sends
        # 0xD2 timing requests to our timingPort and only answers the stream
        # SETUP once the clock exchange completes. Starting this after setup()
        # leaves those requests unanswered and the stream SETUP hangs (proven).
        self._sock_timing.settimeout(0.5)
        self._sock_control.settimeout(0.5)
        t = threading.Thread(target=self._timing_thread, daemon=True)
        t.start()
        self._threads.append(t)
        c = threading.Thread(target=self._control_thread, daemon=True)
        c.start()
        self._threads.append(c)

        # GET /info (best effort)
        try:
            info = self.conn.request("GET", "/info", protocol="HTTP/1.1",
                                     allow_error=True)
            self.stats.events.append(
                f"GET /info -> {info.code} body={info.body.hex()[:200]}")
        except Exception as e:
            self.stats.events.append(f"GET /info warn: {e}")

        # Session-level SETUP (bplist), matched to cpp/owntone field set.
        session_body = {
            "deviceID": self.device_id,
            "sessionUUID": self.session_uuid,
            "timingPort": timing_port,
            "timingProtocol": "NTP",
            "isMultiSelectAirPlay": True,
            "groupContainsGroupLeader": False,
            "macAddress": self.device_id,
            "model": self.model,
            "name": self.name,
            "osBuildVersion": "20F66",
            "osName": "iPhone OS",
            "osVersion": "16.5",
            "senderSupportsRelay": False,
            "sourceVersion": "690.7.1",
            "statsCollectionEnabled": False,
        }
        r = self.conn.request(
            "SETUP", f"rtsp://{self.conn.local_ip}/{self.session_id}",
            headers={"X-Apple-StreamID": "1"},
            body=plistlib.dumps(session_body, fmt=plistlib.FMT_BINARY),
            content_type="application/x-apple-binary-plist")
        setup_resp = self._decode_plist(r)
        self.stats.event_port = int(setup_resp.get("eventPort", 0) or 0)
        self.stats.events.append(
            f"session SETUP ok, eventPort={self.stats.event_port} "
            f"body={r.body.hex()[:200]}")
        self.stats.events.append(f"session SETUP plist={setup_resp}")

        if self.stats.event_port:
            self._open_event_channel()

        # RECORD (empty body, no Range/RTP-Info; cpp/owntone parity). The
        # receiver enters RECORD state before the stream SETUP.
        rec = self.conn.request(
            "RECORD", f"rtsp://{self.conn.local_ip}/{self.session_id}",
            headers={"Range": "npt=0-",
                     "RTP-Info": f"seq=0;rtptime={self.latency_frames}"},
            allow_error=True)
        self.stats.events.append(
            f"RECORD -> {rec.code} body={rec.body.hex()[:160]}")

        # Stream-level SETUP (bplist). AP2 receivers hardcode ALAC; negotiate
        # ALAC 0x40000/ct=2 by default, L16 0x800/ct=1 when codec="pcm".
        audio_key = self.shared[:32]
        if self._codec == "alac":
            audio_format, ct = 0x40000, 2
        else:
            audio_format, ct = 0x800, 1
        stream = {
            "audioFormat": audio_format,
            "audioMode": "default",
            "controlPort": control_port,
            "ct": ct,
            "isMedia": True,
            "latencyMax": 88200,
            "latencyMin": 11025,
            "shk": audio_key,
            "spf": audio.FRAMES_PER_PACKET,
            "sr": audio.RATE,
            "type": 0x60,
            "supportsDynamicStreamID": False,
            "streamConnectionID": self.session_id,
        }
        stream_body = {"streams": [stream]}
        r = self.conn.request(
            "SETUP", f"rtsp://{self.conn.local_ip}/{self.session_id}",
            headers={"X-Apple-StreamID": "1"},
            body=plistlib.dumps(stream_body, fmt=plistlib.FMT_BINARY),
            content_type="application/x-apple-binary-plist")
        stream_resp = self._decode_plist(r)
        streams = stream_resp.get("streams", [])
        if not streams:
            raise RuntimeError(f"stream SETUP returned no streams: {r.body[:120]!r}")
        s0 = streams[0]
        self.stats.data_port = int(s0.get("dataPort", 0) or 0)
        self.stats.control_port = int(s0.get("controlPort", 0) or 0)
        if not self.stats.data_port:
            raise RuntimeError("stream SETUP returned no dataPort")
        if not self.stats.control_port:
            self.stats.control_port = self.stats.data_port
        self._audio_key = audio_key
        self.stats.events.append(
            f"stream SETUP ok, dataPort={self.stats.data_port} "
            f"controlPort={self.stats.control_port} body={r.body.hex()[:200]}")

        # Some receivers need a 0 dB command to clear their initial mute.
        # Complete both commands before the group can send any PCM.
        if self.set_volume_db(0.0) != 200:
            raise RuntimeError("Receiver did not confirm unmute")
        if self._volume_db < 0.0 and self.set_volume_db(self._volume_db) != 200:
            raise RuntimeError("Receiver did not confirm safe playback gain")
        fb = threading.Thread(target=self._feedback_thread, daemon=True)
        fb.start()
        self._threads.append(fb)
        self.mark_healthy()

    def _open_event_channel(self) -> None:
        try:
            # Read/Write info reversed: this connection originates from the sender
            # (pyatv ap2_session._setup_event_channel).
            out = hkdf("Events-Salt", "Events-Read-Encryption-Key", self.shared)
            in_ = hkdf("Events-Salt", "Events-Write-Encryption-Key", self.shared)
            self._event_hap = HAPSession()
            self._event_hap.enable(out, in_)
            self._event_sock = socket.create_connection(
                (self.host, self.stats.event_port), self.conn.timeout)
            self.stats.events.append(
                f"event channel -> {self.host}:{self.stats.event_port}")
            t = threading.Thread(target=self._event_channel_loop, daemon=True)
            t.start()
            self._threads.append(t)
        except Exception as e:
            self.stats.events.append(f"event channel warn: {e}")
            self._event_sock = None

    def _event_channel_loop(self) -> None:
        """Decrypt event-channel requests and answer 200 (EventChannel behaviour)."""
        assert self._event_sock is not None and self._event_hap is not None
        buf = b""
        while not self._stop.is_set():
            try:
                data = self._event_sock.recv(65536)
            except socket.timeout:
                continue
            except OSError:
                break
            if not data:
                break
            buf += self._event_hap.decrypt(data)
            while b"\r\n\r\n" in buf:
                head, _, rest = buf.partition(b"\r\n\r\n")
                lines = head.decode("utf-8", "ignore").split("\r\n")
                if not lines:
                    break
                headers: dict[str, str] = {}
                for line in lines[1:]:
                    if ": " in line:
                        k, v = line.split(": ", 1)
                        headers[k.lower()] = v
                clen = int(headers.get("content-length", "0"))
                if len(rest) < clen:
                    break
                body = rest[:clen]
                buf = rest[clen:]
                # Minimal bare 200 (cpp/README #7): only Server/CSeq. Adding
                # Content-Length/Audio-Latency corrupts the receiver timeline.
                resp_lines = ["RTSP/1.0 200 OK"]
                if "cseq" in headers:
                    resp_lines.append(f"CSeq: {headers['cseq']}")
                if "server" in headers:
                    resp_lines.append(f"Server: {headers['server']}")
                resp = ("\r\n".join(resp_lines) + "\r\n\r\n").encode()
                self._event_sock.sendall(self._event_hap.encrypt(resp))
                self.stats.events.append("event channel: replied 200")

    def _event_reply_thread_stub(self) -> None:
        pass

    # ------------------------------------------------------------- UDP threads

    def cache_audio(self, seq: int, wire: bytes) -> None:
        """Retain one sent audio packet for possible retransmit (0x55 -> 0xD6)."""
        self._backlog[seq] = wire
        self._backlog_order.append(seq)
        while len(self._backlog_order) > self._BACKLOG_MAX:
            old = self._backlog_order.pop(0)
            self._backlog.pop(old, None)

    def _control_thread(self) -> None:
        """Answer receiver retransmit requests (type 0x55) from the backlog."""
        while not self._stop.is_set():
            try:
                data, addr = self._sock_control.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                break
            if len(data) < 8 or (data[1] & 0x7F) != 0x55:
                continue
            lost_seq = (data[4] << 8) | data[5]
            lost_count = (data[6] << 8) | data[7]
            for k in range(lost_count):
                s = (lost_seq + k) & 0xFFFF
                wire = self._backlog.get(s)
                if wire is None:
                    continue
                resp = b"\x80\xD6" + struct.pack(">H", s) + wire
                try:
                    self._sock_control.sendto(resp, addr)
                except OSError:
                    pass

    def _timing_thread(self) -> None:
        while not self._stop.is_set():
            try:
                data, addr = self._sock_timing.recvfrom(2048)
                reply = audio.timing_reply(data, audio.ntp_now())
                self._sock_timing.sendto(reply, addr)
            except socket.timeout:
                continue
            except OSError:
                break

    def _sync_thread(self, get_rtptime, get_head_ts, rate: int) -> None:
        first = True
        next_tick = time.monotonic()
        while not self._stop.is_set():
            try:
                pkt = audio.sync_packet(first, get_rtptime(), self.latency_frames,
                                        get_head_ts(), rate)
                self._sock_control.sendto(
                    pkt, (self.host, self.stats.control_port))
                first = False
            except OSError:
                break
            next_tick += 1.0
            self._stop.wait(max(0.0, next_tick - time.monotonic()))

    def start_udp_threads(self) -> None:
        # Timing reply thread is already started in setup(); nothing else to
        # pre-start here (the control sync thread is launched by stream_pcm).
        pass

    # ------------------------------------------------------------ streaming

    def stream_pcm(self, pcm: "np.ndarray", record_log: bool = True) -> None:
        """Paced send of interleaved stereo int16 PCM. Blocks until exhausted."""
        from .chacha import AudioCipher
        cipher = AudioCipher(self._audio_key)

        n_frames = len(pcm) // audio.CHANNELS
        packets = (n_frames + audio.FRAMES_PER_PACKET - 1) // audio.FRAMES_PER_PACKET
        seq = 0
        start_ts = audio.ntp2ts(audio.ntp_now(), audio.RATE)
        head_ts = start_ts
        rtptime = self.latency_frames
        ssrc = self.session_id

        def get_rtptime():
            return rtptime

        def get_head_ts():
            return head_ts

        sync = threading.Thread(target=self._sync_thread,
                               args=(get_rtptime, get_head_ts, audio.RATE),
                               daemon=True)
        sync.start()
        self._threads.append(sync)

        clock_start = time.monotonic()
        for i in range(packets):
            if self._stop.is_set():
                break
            off = i * audio.FRAMES_PER_PACKET * audio.CHANNELS
            chunk = pcm[off:off + audio.FRAMES_PER_PACKET * audio.CHANNELS]
            pad = audio.FRAMES_PER_PACKET * audio.CHANNELS - len(chunk)
            if pad > 0:
                chunk = np.concatenate([chunk, np.zeros(pad, dtype=np.int16)])
            if self._codec == "alac":
                payload = audio.encode_alac_frame(chunk, audio.FRAMES_PER_PACKET)
            else:
                payload = audio.frames_to_payload(chunk)
            header = audio.rtp_header(marker=(i == 0), seq=seq,
                                      rtptime=rtptime, ssrc=ssrc)
            aad = header[4:12]
            wire = header + cipher.encrypt(payload, aad)

            t0 = time.perf_counter()
            self._sock_audio.sendto(wire, (self.host, self.stats.data_port))
            t1 = time.perf_counter()

            now = time.monotonic()
            if self.stats.first_send_monotonic is None:
                self.stats.first_send_monotonic = now
            self.stats.last_send_monotonic = now
            self.stats.packets_sent += 1
            if record_log:
                self.stats.packet_log.append({
                    "idx": i, "seq": seq, "rtptime": rtptime,
                    "bytes": len(wire), "send_ts": t0,
                    "send_dt_ms": (t1 - t0) * 1000,
                })

            seq = (seq + 1) & 0xFFFF
            head_ts += audio.FRAMES_PER_PACKET
            rtptime = head_ts - start_ts + self.latency_frames

            # Pace to 44100 frames/s off the monotonic clock.
            target = clock_start + (head_ts - start_ts) / audio.RATE
            sleep_for = target - time.monotonic()
            if sleep_for > 0:
                time.sleep(sleep_for)

    # ------------------------------------------------------------- teardown

    def set_volume_db(self, db: float) -> int | None:
        """SET_PARAMETER volume (text/parameters). AP2 receivers can sit muted
        at their own default until told otherwise (cpp #90: "connected but
        silent"); push 0 dB (no attenuation) so audio is audible by default."""
        body = f"volume: {db:.6f}".encode()
        try:
            r = self.conn.request(
                "SET_PARAMETER", f"rtsp://{self.conn.local_ip}/{self.session_id}",
                content_type="text/parameters", body=body, allow_error=True)
            self.stats.events.append(f"volume {db:.1f} dB -> {r.code}")
            return r.code
        except Exception as e:
            self.stats.events.append(f"volume warn: {e}")
            return None

    def _feedback_thread(self) -> None:
        # AP2 keep-alive: POST /feedback RTSP/1.0 every 2 s. An HTTP/1.1 line
        # is silently ignored by the RTSP parser; without it the receiver
        # tears down at ~30 s. A 2xx reply stamps the session healthy; anything
        # else (or a socket error) leaves the last healthy timestamp stale so
        # the group watchdog can flag and reconnect this member.
        while not self._stop.is_set():
            try:
                resp = self.conn.request("POST", "/feedback", protocol="RTSP/1.0",
                                         body=b"", allow_error=True)
                if 200 <= resp.code < 300:
                    self.mark_healthy()
                else:
                    self.stats.last_error = f"feedback {resp.code}"
                    self.stats.events.append(f"feedback suspect: {resp.code}")
            except Exception as e:
                self.stats.last_error = f"feedback warn: {e}"
                self.stats.events.append(f"feedback warn: {e}")
            self._stop.wait(2.0)

    def feedback(self) -> None:
        try:
            self.conn.request("POST", "/feedback", protocol="RTSP/1.0",
                              body=b"", allow_error=True)
        except Exception as e:
            self.stats.events.append(f"feedback warn: {e}")

    def teardown(self) -> None:
        self._stop.set()
        try:
            if self.conn.sock is not None:
                self.conn.sock.settimeout(1.5)  # do not block exit on teardown reply
                self.conn.request(
                    "TEARDOWN", f"rtsp://{self.conn.local_ip}/{self.session_id}",
                    allow_error=True)
        except Exception as e:
            self.stats.events.append(f"teardown warn: {e}")
        finally:
            for s in (self._sock_audio, self._sock_control,
                      self._sock_timing, self._event_sock):
                if s is not None:
                    try:
                        s.close()
                    except OSError:
                        pass
            self.conn.close()
