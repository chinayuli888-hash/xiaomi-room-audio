"""M3 group sender: one process, one audio clock, one shared RTP/NTP timeline,
one AP2 session per receiver.

Each session keeps its own UDP ports, TCP control channel, SSRC, audio cipher and
nonce counter, but every packet boundary, `rtptime` and NTP head timestamp come
from the shared clock so all receivers are aligned to the same playout timeline.

This module is the multi-receiver layer over `AirPlay2Session`; live three-way
acoustic alignment is measured once the other receivers are released for testing.
"""

import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from . import audio
from .chacha import AudioCipher
from .session import AirPlay2Session


# Per-member reconnect policy. A member flagged unhealthy for DEAD_AFTER seconds
# is rebuilt on its own worker thread; one dead receiver never restarts the
# whole process. Backoff ramps 1s/2s/4s then caps at 30s.
DEAD_AFTER = 8.0
WATCH_INTERVAL = 1.0
BACKOFF_SEQ = (1.0, 2.0, 4.0)
BACKOFF_CAP = 30.0


def _scale_chunk(chunk, previous, target):
    if previous == target:
        if target == 1.0:
            return chunk
        return np.rint(chunk * target).astype(np.int16)
    frames = chunk.reshape(-1, audio.CHANNELS)
    ramp = np.linspace(previous, target, len(frames), dtype=np.float32)
    return np.rint(frames * ramp[:, None]).astype(np.int16).ravel()


@dataclass
class GroupMember:
    device: dict
    session: AirPlay2Session
    cipher: Optional[AudioCipher] = None
    seq: int = 0
    packet_log: list = field(default_factory=list)
    send_times: list = field(default_factory=list)
    # Reconnect bookkeeping surfaced to the heartbeat.
    reconnect_count: int = 0
    last_reconnect_ts: Optional[float] = None
    last_error: str = ""
    reconnecting: bool = False
    down_since: Optional[float] = None
    lock: threading.Lock = field(default_factory=threading.Lock)


class AirPlay2Group:
    def __init__(self, devices: list[dict], codec: str = "alac",
                 volume_db: float = 0.0, latency_frames: Optional[int] = None):
        self.codec = codec
        self.volume_db = volume_db
        self.latency_frames = latency_frames or audio.LATENCY
        self.members: list[GroupMember] = []
        # Shared-timeline closures, bound when streaming starts; a rebuilt
        # session re-joins the same monotonic RTP/NTP timeline.
        self._get_rtptime = None
        self._get_head_ts = None
        self._stop_event: Optional[threading.Event] = None
        self._watchdog: Optional[threading.Thread] = None
        for d in devices:
            self.members.append(GroupMember(device=d, session=self._new_session(d)))

    def _new_session(self, device: dict) -> AirPlay2Session:
        return AirPlay2Session(
            device["host"], device.get("port", 7000),
            name=device.get("name", "python-ap2"),
            codec=self.codec, volume_db=device.get("volume_db", self.volume_db),
            latency_frames=self.latency_frames)

    # ------------------------------------------------------------------ setup

    def setup_all(self, stop_event: Optional[threading.Event] = None) -> None:
        """Pair and SETUP every receiver. A member whose setup raises keeps
        its slot (cipher=None) so the watchdog can rebuild it later; other
        receivers are never torn down because of it."""
        for m in self.members:
            if stop_event is not None and stop_event.is_set():
                break
            s = m.session
            try:
                s.pair_transient()
                s.setup()
                m.cipher = AudioCipher(s._audio_key)
            except Exception as e:  # keep other receivers alive
                msg = f"{type(e).__name__}: {e}"
                m.last_error = msg
                s.stats.events.append(f"group setup failed: {msg}")
                try:
                    s.teardown()
                except Exception:
                    pass

    # ------------------------------------------------------------ watchdog

    def start_watchdog(self, stop_event: threading.Event) -> None:
        self._stop_event = stop_event
        self._watchdog = threading.Thread(
            target=self._watchdog_loop, daemon=True, name="ap2-watchdog")
        self._watchdog.start()

    def _watchdog_loop(self) -> None:
        while not self._stop_event.is_set():
            self._stop_event.wait(WATCH_INTERVAL)
            if self._stop_event.is_set():
                break
            for m in list(self.members):
                self._inspect(m)

    def _inspect(self, m: GroupMember) -> None:
        now = time.monotonic()
        if m.session.is_alive(DEAD_AFTER):
            m.down_since = None
            return
        if m.reconnecting:
            return
        if m.down_since is None:
            m.down_since = now
            m.last_error = "session unhealthy"
            return
        if now - m.down_since >= DEAD_AFTER:
            threading.Thread(
                target=self._reconnect_worker, args=(m,),
                daemon=True, name=f"ap2-reconnect-{m.device['host']}").start()

    def _reconnect_worker(self, m: GroupMember) -> None:
        m.reconnecting = True
        backoff_idx = 0
        try:
            while not self._stop_event.is_set():
                try:
                    self._rebuild(m)
                    m.reconnect_count += 1
                    m.last_reconnect_ts = time.monotonic()
                    m.last_error = ""
                    m.down_since = None
                    return
                except Exception as e:  # noqa: BLE001 - recorded per member
                    m.last_error = f"{type(e).__name__}: {e}"
                    m.session.stats.events.append(
                        f"reconnect failed: {m.last_error}")
                    delay = (BACKOFF_SEQ[backoff_idx] if backoff_idx < len(BACKOFF_SEQ)
                             else BACKOFF_CAP)
                    backoff_idx += 1
                    self._stop_event.wait(delay)
        finally:
            m.reconnecting = False

    def _rebuild(self, m: GroupMember) -> None:
        """Tear down the dead session and run pair -> SETUP -> RECORD -> stream
        SETUP on a fresh session, then hot-swap it into the member. The shared
        RTP/NTP timeline is untouched; only this member's seq/cipher/ssrc reset."""
        old = m.session
        try:
            old.teardown()
        except Exception:
            pass
        sess = self._new_session(m.device)
        sess.pair_transient()
        sess.setup()
        cipher = AudioCipher(sess._audio_key)
        with m.lock:
            m.session = sess
            m.cipher = cipher
            m.seq = 0
            m.packet_log = []
            m.send_times = []
        # Re-join the shared monotonic timeline with a fresh sync thread.
        if self._get_rtptime is not None:
            t = threading.Thread(
                target=sess._sync_thread,
                args=(self._get_rtptime, self._get_head_ts, audio.RATE),
                daemon=True)
            t.start()
            sess._threads.append(t)

    # ------------------------------------------------------------ streaming

    def _start_sync_threads(self, get_rtptime, get_head_ts) -> None:
        for m in self.members:
            t = threading.Thread(
                target=m.session._sync_thread,
                args=(get_rtptime, get_head_ts, audio.RATE), daemon=True)
            t.start()
            m.session._threads.append(t)

    def stream(self, pcm: np.ndarray, record_log: bool = True) -> dict:
        """Paced shared-clock send to all members. Blocks until exhausted."""
        if not self.members:
            return {"members": 0, "packets": 0}

        n_frames = len(pcm) // audio.CHANNELS
        packets = (n_frames + audio.FRAMES_PER_PACKET - 1) // audio.FRAMES_PER_PACKET

        # Shared timeline: one NTP-derived start, one latency, one head_ts.
        start_ts = audio.ntp2ts(audio.ntp_now(), audio.RATE)
        head_ts = start_ts
        rtptime = self.latency_frames

        def get_rtptime():
            return rtptime

        def get_head_ts():
            return head_ts

        self._get_rtptime = get_rtptime
        self._get_head_ts = get_head_ts
        self._start_sync_threads(get_rtptime, get_head_ts)

        clock_start = time.monotonic()
        total = 0
        for i in range(packets):
            off = i * audio.FRAMES_PER_PACKET * audio.CHANNELS
            chunk = pcm[off:off + audio.FRAMES_PER_PACKET * audio.CHANNELS]
            pad = audio.FRAMES_PER_PACKET * audio.CHANNELS - len(chunk)
            if pad > 0:
                chunk = np.concatenate([chunk, np.zeros(pad, dtype=np.int16)])

            if self.codec == "alac":
                payload = audio.encode_alac_frame(chunk, audio.FRAMES_PER_PACKET)
            else:
                payload = audio.frames_to_payload(chunk)

            marker = (i == 0)
            for m in list(self.members):
                with m.lock:
                    s = m.session
                    cipher = m.cipher
                    seq = m.seq
                if cipher is None:
                    continue
                header = audio.rtp_header(marker=marker, seq=seq,
                                          rtptime=rtptime, ssrc=s.session_id)
                aad = header[4:12]
                wire = header + cipher.encrypt(payload, aad)
                t0 = time.perf_counter()
                try:
                    s._sock_audio.sendto(wire, (s.host, s.stats.data_port))
                except OSError:
                    pass
                t1 = time.perf_counter()
                s.cache_audio(seq, wire)
                with m.lock:
                    if m.session is s:
                        m.seq = (seq + 1) & 0xFFFF
                total += 1
                if record_log:
                    m.packet_log.append({
                        "idx": i, "seq": seq, "rtptime": rtptime,
                        "bytes": len(wire), "send_ts": t0,
                        "send_dt_ms": (t1 - t0) * 1000,
                    })
                    m.send_times.append(t0)

            head_ts += audio.FRAMES_PER_PACKET
            rtptime = head_ts - start_ts + self.latency_frames

            target = clock_start + (head_ts - start_ts) / audio.RATE
            sleep_for = target - time.monotonic()
            if sleep_for > 0:
                time.sleep(sleep_for)

        return {
            "members": len(self.members),
            "packets": total,
            "per_member": [
                {"host": m.session.host, "packets": len(m.packet_log),
                 "data_port": m.session.stats.data_port,
                 "control_port": m.session.stats.control_port,
                 "events": m.session.stats.events}
                for m in self.members
            ],
        }

    # ------------------------------------------------------------- teardown

    def stream_live(self, provider, stop_event: threading.Event,
                    record_log: bool = True, max_packets: int = 0, *,
                    speaker_gains: Optional[Callable[[], Mapping[str, float]]] = None) -> dict:
        """Paced shared-clock send from a live chunk provider.

        `provider()` returns one FRAMES_PER_PACKET stereo int16 chunk, or None
        (silence) when no fresh audio is available. Blocks until stop_event or
        max_packets (0 = unlimited). Provider must never block long.
        Absolute speaker gains (0 through 1) are sampled once per packet.
        All members use the same PCM frames and RTP/NTP timeline.
        """
        if not self.members:
            return {"members": 0, "packets": 0}

        start_ts = audio.ntp2ts(audio.ntp_now(), audio.RATE)
        head_ts = start_ts
        rtptime = self.latency_frames

        def get_rtptime():
            return rtptime

        def get_head_ts():
            return head_ts

        self._get_rtptime = get_rtptime
        self._get_head_ts = get_head_ts
        self._start_sync_threads(get_rtptime, get_head_ts)

        clock_start = time.monotonic()
        total = 0
        i = 0
        silence = np.zeros(audio.FRAMES_PER_PACKET * audio.CHANNELS,
                           dtype=np.int16)
        last_gains = {}
        while not stop_event.is_set():
            if max_packets and i >= max_packets:
                break
            chunk = provider()
            if chunk is None or len(chunk) < len(silence):
                chunk = silence

            gains = speaker_gains() if speaker_gains is not None else {}
            encoded = {}
            marker = (i == 0)
            for m in list(self.members):
                with m.lock:
                    s = m.session
                    cipher = m.cipher
                    seq = m.seq
                if cipher is None:
                    continue  # mid-rebuild; this member rejoins on next packets
                host = m.device["host"]
                target_gain = gains.get(host, 1.0)
                previous_gain = last_gains.get(host, target_gain)
                last_gains[host] = target_gain
                silent = chunk is silence or target_gain <= 0.0
                key = (0.0, 0.0) if silent else (previous_gain, target_gain)
                if key not in encoded:
                    pcm = silence if silent else _scale_chunk(chunk, previous_gain, target_gain)
                    if self.codec == "alac":
                        encoded[key] = audio.encode_alac_frame(pcm, audio.FRAMES_PER_PACKET)
                    else:
                        encoded[key] = audio.frames_to_payload(pcm)
                header = audio.rtp_header(marker=marker, seq=seq,
                                          rtptime=rtptime, ssrc=s.session_id)
                aad = header[4:12]
                wire = header + cipher.encrypt(encoded[key], aad)
                t0 = time.perf_counter()
                try:
                    s._sock_audio.sendto(wire, (s.host, s.stats.data_port))
                except OSError:
                    pass
                t1 = time.perf_counter()
                s.cache_audio(seq, wire)
                with m.lock:
                    if m.session is s:
                        m.seq = (seq + 1) & 0xFFFF
                total += 1
                if record_log:
                    m.packet_log.append({
                        "idx": i, "seq": seq, "rtptime": rtptime,
                        "bytes": len(wire), "send_ts": t0,
                        "send_dt_ms": (t1 - t0) * 1000,
                    })
                    m.send_times.append(t0)

            head_ts += audio.FRAMES_PER_PACKET
            rtptime = head_ts - start_ts + self.latency_frames
            i += 1

            target = clock_start + (head_ts - start_ts) / audio.RATE
            sleep_for = target - time.monotonic()
            if sleep_for > 0:
                stop_event.wait(sleep_for)

        return {
            "members": len(self.members),
            "packets": total,
            "per_member": [
                {"host": m.session.host, "packets": len(m.packet_log),
                 "data_port": m.session.stats.data_port,
                 "control_port": m.session.stats.control_port,
                 "events": m.session.stats.events}
                for m in self.members
            ],
        }

    def broadcast_volume_db(self, db: float) -> dict:
        """Push one dBFS volume to every member; return per-member response."""
        out: dict[str, int] = {}
        for m in self.members:
            m.session.set_volume_db(db)
            code = None
            for ev in reversed(m.session.stats.events):
                if ev.startswith("volume "):
                    code = ev.rsplit(" -> ", 1)[-1]
                    break
            out[m.session.host] = code
        return out

    def teardown_all(self) -> None:
        for m in self.members:
            try:
                m.session.teardown()
            except Exception:
                pass
