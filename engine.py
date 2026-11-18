"""Audio routing, PCM gain and AirPlay 2 lifecycle for the Windows app."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
import ipaddress
import json
import logging
import math
from pathlib import Path
import shutil
import subprocess
import threading
import time
from types import MappingProxyType

import numpy as np

from ap2 import audio
from ap2.group import AirPlay2Group
from windows_audio import Level, MEDIA_ROLES, WindowsAudio

START_LIMIT = 10
RECEIVER_DB = -12.0
MIN_RECEIVER_DB = -20.0  # Keep non-mute requests above the Xiaomi receivers' mute threshold.
CABLE_NAME = "CABLE Input (VB-Audio Virtual Cable)"
CAPTURE_NAME = "CABLE Output (VB-Audio Virtual Cable)"
FRAME_BYTES = audio.FRAMES_PER_PACKET * audio.CHANNELS * 2
MAX_QUEUE = 15 * FRAME_BYTES   # At most ~120 ms of PCM waiting for the sender.
log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Settings:
    hosts: list[str]
    ffmpeg: Path
    speaker_names: dict[str, str] = field(default_factory=dict)
    speaker_max_db: dict[str, float] = field(default_factory=dict)
    speaker_ports: dict[str, int] = field(default_factory=dict)

    def receiver_db(self, host):
        return self.speaker_max_db.get(host, RECEIVER_DB)

    def receiver_port(self, host):
        return self.speaker_ports.get(host, 7000)

    def for_speakers(self, speakers, profiles=None):
        """Only this fresh discovery can select receivers; IPs are not identities."""
        speakers = tuple(speakers)
        hosts = [speaker.host for speaker in speakers]
        if len(hosts) > 8 or len(set(hosts)) != len(hosts):
            raise ValueError("一次最多播放 8 台不同的本地音箱。")
        profiles = profiles or {}
        names, limits, ports = {}, {}, {}
        for speaker in speakers:
            profile = profiles.get(speaker.identifier, {})
            if not isinstance(profile, dict):
                profile = {}
            name = profile.get("name")
            names[speaker.host] = name if isinstance(name, str) and name.strip() else speaker.name
            db = profile.get("max_db", RECEIVER_DB)
            if isinstance(db, bool) or not isinstance(db, (float, int)) or not MIN_RECEIVER_DB <= db <= 0:
                db = RECEIVER_DB
            limits[speaker.host] = db
            if isinstance(speaker.port, bool) or not isinstance(speaker.port, int) or not 1 <= speaker.port <= 65535:
                raise ValueError("音箱广播的端口无效，请重新搜索。")
            ports[speaker.host] = speaker.port
        return Settings(hosts, self.ffmpeg, names, limits, ports)

    @classmethod
    def load(cls, path):
        data = json.loads(path.read_text(encoding="utf-8-sig")) if path.is_file() else {}
        hosts = data.get("hosts", [])
        if not isinstance(hosts, list) or not 0 <= len(hosts) <= 8:
            raise ValueError("hosts 如保留旧配置，须为不超过 8 台音箱的列表；界面只使用实时搜索结果。")
        parsed = [ipaddress.IPv4Address(host) for host in hosts]
        if any(ip.is_multicast or ip.is_unspecified or ip.is_loopback for ip in parsed):
            raise ValueError("hosts 必须是局域网音箱的单播 IPv4 地址。")
        hosts = [str(ip) for ip in parsed]
        if len(set(hosts)) != len(hosts):
            raise ValueError("hosts 中的音箱 IP 不能重复。")
        configured = data.get("ffmpeg")
        if configured:
            ffmpeg = path.parent / configured
        else:
            ffmpeg = path.parent / "bin" / "ffmpeg.exe"
            if not ffmpeg.is_file():
                ffmpeg = Path(shutil.which("ffmpeg.exe") or "ffmpeg.exe")
        if not ffmpeg.is_file():
            raise ValueError("未找到支持 DirectShow 的 FFmpeg。请设置 ffmpeg 路径。")
        names = data.get("speaker_names", {})
        if not isinstance(names, dict) or any(not isinstance(name, str) for name in names.values()):
            raise ValueError("speaker_names 应为音箱 IP 和显示名称的对应表。")
        names = {host: names.get(host, "").strip() for host in hosts}
        limits = data.get("speaker_max_db", {})
        if not isinstance(limits, dict) or any(
            host not in hosts or isinstance(db, bool) or not isinstance(db, (int, float))
            or not MIN_RECEIVER_DB <= db <= 0
            for host, db in limits.items()
        ):
            raise ValueError("speaker_max_db 须以已配置的音箱 IP 为键，填写 −20 至 0 的数值。")
        return cls(hosts, ffmpeg.resolve(), names, limits)


def find_cable(win):
    candidates = [dev for dev in win.render_devices() if dev.name.casefold() == CABLE_NAME.casefold()]
    if len(candidates) != 1:
        raise RuntimeError("找不到唯一可用的 VB-Audio CABLE Input。请检查虚拟声卡。")
    return candidates[0]


def startup_level(original, peak_ratio=1.0):
    ceiling = START_LIMIT / 100 / max(1.0, peak_ratio)
    return Level(min(original.scalar, ceiling), original.muted)


class SpeakerMix:
    """Preserve the balance while Windows scales all speakers together."""
    def __init__(self, hosts):
        self._lock = threading.Lock()
        self._master = Level(0.0, False)
        self._ratios = dict.fromkeys(hosts, 1.0)
        self._levels = MappingProxyType(dict.fromkeys(self._ratios, 0.0))

    def startup_peak_ratio(self, receiver_levels):
        # For startup only, budget any increase in receiver gain against the
        # old -12 dB baseline. Once playing, PCM still tracks the Windows master.
        with self._lock:
            return max((
                ratio * 10 ** (max(0.0, receiver_levels[host] - RECEIVER_DB) / 20)
                for host, ratio in self._ratios.items()
            ), default=1.0)

    def snapshot(self):
        with self._lock:
            return self._master, self._levels

    def gains(self):
        return self.snapshot()[1]

    def restore_ratios(self, ratios):
        """Carry the current session's balance across a fresh identity lookup."""
        with self._lock:
            for host in self._ratios:
                ratio = ratios.get(host, 1.0)
                if isinstance(ratio, (int, float)) and not isinstance(ratio, bool) and math.isfinite(ratio) and ratio >= 0:
                    self._ratios[host] = ratio

    def set_master(self, level):
        with self._lock:
            self._master = level
            scale = 0.0 if level.muted else level.scalar
            peak = max(self._ratios.values(), default=0.0)
            if scale * peak > 1.0:
                scale = 1.0 / peak
            self._levels = MappingProxyType({
                host: ratio * scale for host, ratio in self._ratios.items()
            })

    def set_speaker_percent(self, host, percent):
        with self._lock:
            master = self._master
            if master.muted or master.scalar <= 0:
                return
            current = dict(self._levels)
            current[host] = min(1.0, max(0.0, percent / 100))
            # Start from what each speaker is actually playing. When the group
            # was limited, this lets a single drag leave the other speakers alone.
            self._ratios = {key: gain / master.scalar for key, gain in current.items()}
            self._levels = MappingProxyType(current)


def recover(win, path):
    """Restore only playback roles still pointing to our temporary route."""
    if not path.is_file():
        return
    state = json.loads(path.read_text(encoding="utf-8"))
    cable_id = state["cable"]
    active = {d.id: d for d in win.render_devices()}
    speakers = next((d.id for d in active.values() if d.id != cable_id
                     and ("扬声器" in d.name or "speakers" in d.name.casefold())), None)
    for role in MEDIA_ROLES:
        if win.default(role).id != cable_id:
            continue
        target = state["before"][str(role)]
        if target not in active:
            target = speakers
        if target is None or target == cable_id:
            raise RuntimeError("原播放设备不在线。请连接原设备或在 Windows 选择电脑扬声器。")
        win.set_default_render(target, role)
        if win.default(role).id != target:
            raise RuntimeError("Windows 没有恢复原播放设备。请在系统声音设置中选择输出。")
    if cable_id in active:
        current = win.level(cable_id)
        if state.get("schema") == 2 or (current.scalar >= 0.995 and not current.muted):
            win.set_level(cable_id, Level(**state["cable_level"]))
    path.unlink()
    log.info("Playback endpoints restored")


class Route:
    def __init__(self, win, path):
        self.win, self.path = win, path
        self.cable = find_cable(win)
        self.before = {role: win.default(role) for role in MEDIA_ROLES}
        if any(d.id == self.cable.id for d in self.before.values()):
            raise RuntimeError("Windows 正在使用 CABLE Input。请先退出旧投放并选择正常播放设备。")

    def begin(self, peak_ratio=1.0):
        for role, endpoint in self.before.items():
            if self.win.default(role).id != endpoint.id:
                raise RuntimeError("正在连接时系统输出已变更，请重试。")
        old = self.win.level(self.cable.id)
        initial = startup_level(self.win.level(self.before[MEDIA_ROLES[0]].id), peak_ratio)
        data = {"schema": 2, "cable": self.cable.id, "before": {str(r): d.id for r, d in self.before.items()},
                "cable_level": {"scalar": old.scalar, "muted": old.muted}}
        target = self.path.with_suffix(".tmp")
        target.write_text(json.dumps(data, indent=2), encoding="utf-8")
        target.replace(self.path)
        self.win.set_level(self.cable.id, initial)
        for role in MEDIA_ROLES:
            self.win.set_default_render(self.cable.id, role)
        self.check()

    def check(self):
        if any(self.win.default(role).id != self.cable.id for role in MEDIA_ROLES):
            raise RuntimeError("系统已选择其他播放设备，投放已停止。")


class Capture:
    """DirectShow with a bounded PCM queue. Close FFmpeg through stdin."""
    def __init__(self, ffmpeg, stderr_path):
        self._pending = bytearray()
        self._lock = threading.Lock()
        self.bytes_in = 0
        self.bytes_dropped = 0
        cmd = [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-nostats", "-stdin",
               "-fflags", "nobuffer", "-probesize", "32", "-analyzeduration", "0",
               "-f", "dshow", "-audio_buffer_size", "40", "-i", f"audio={CAPTURE_NAME}",
               "-ac", "2", "-ar", str(audio.RATE), "-f", "s16le", "pipe:1"]
        with stderr_path.open("wb") as errors:
            self.process = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                            stderr=errors, bufsize=0,
                                            creationflags=subprocess.CREATE_NO_WINDOW)
        self._reader = threading.Thread(target=self._read, daemon=True, name="capture-reader")
        self._reader.start()

    def _read(self):
        while chunk := self.process.stdout.read(4096):
            with self._lock:
                self.bytes_in += len(chunk)
                self._pending.extend(chunk)
                over = len(self._pending) - MAX_QUEUE
                if over > 0:
                    trim = ((over + FRAME_BYTES - 1) // FRAME_BYTES) * FRAME_BYTES
                    del self._pending[:trim]
                    self.bytes_dropped += trim

    def read(self):
        if self.process.poll() is not None:
            raise RuntimeError("音频采集已退出。请检查 CABLE Output 和 FFmpeg。")
        with self._lock:
            if len(self._pending) < FRAME_BYTES:
                return None
            block = bytes(self._pending[:FRAME_BYTES])
            del self._pending[:FRAME_BYTES]
        return np.frombuffer(block, dtype="<i2")

    def close(self):
        p = self.process
        if p.poll() is None:
            try:
                p.stdin.write(b"q\n")
                p.stdin.flush()
            except (OSError, BrokenPipeError):
                pass
        p.stdin.close()
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            raise RuntimeError("FFmpeg 尚未退出。系统播放输出已进入恢复流程。") from None
        self._reader.join(timeout=1)
        p.stdout.close()


class SystemVolume:
    """Use the active Windows endpoint for the master volume and mute state."""
    def __init__(self, endpoint, publish):
        self.endpoint, self.publish = endpoint, publish
        self.changed = threading.Event()
        self.level = None
        endpoint.subscribe(self.changed.set)
        self.refresh()

    def refresh(self):
        self.changed.clear()
        current = self.endpoint.read()
        if current != self.level:
            self.level = current
            self.publish(current)

    def service(self):
        if self.changed.is_set():
            self.refresh()


class Player:
    def __init__(self, settings, state_dir, emit, mix, network=None):
        self.settings, self.state_dir, self.emit = settings, state_dir, emit
        self.mix = mix
        self.network = network
        self.stop = threading.Event()

    def check_network(self):
        if self.network is not None:
            from discovery import current_network
            if current_network().fingerprint != self.network.fingerprint:
                raise RuntimeError("网络已变化，旧连接已停止，请搜索当前网络中的音箱。")

    def sync_level(self, level):
        self.mix.set_master(level)
        self.emit("system-volume", None)

    def run(self):
        group = capture = None
        issue = None
        journal = self.state_dir / "restore.json"
        try:
            if self.stop.is_set():
                return
            if not self.settings.hosts:
                raise RuntimeError("请先搜索当前网络中的音箱。")
            self.check_network()
            with WindowsAudio() as win:
                recover(win, journal)
                route = Route(win, journal)
                try:
                    receiver_levels = {ip: self.settings.receiver_db(ip) for ip in self.settings.hosts}
                    group = AirPlay2Group(
                        [{"host": ip, "port": self.settings.receiver_port(ip),
                          "name": "RoomAudio", "volume_db": db}
                         for ip, db in receiver_levels.items()],
                        codec="alac", volume_db=RECEIVER_DB, latency_frames=audio.LATENCY_LOW,
                    )
                    group.setup_all(self.stop)
                    if self.stop.is_set():
                        return
                    self.check_network()
                    online = sum(m.cipher is not None for m in group.members)
                    if online != len(self.settings.hosts):
                        for m in group.members:
                            if m.cipher is None:
                                log.warning("%s: %s", m.device["host"], m.last_error)
                        raise RuntimeError(f"仅 {online}/{len(self.settings.hosts)} 台音箱就绪，请检查同一局域网中的音箱。")

                    capture = Capture(self.settings.ffmpeg, self.state_dir / "ffmpeg.log")
                    route.begin(self.mix.startup_peak_ratio(receiver_levels))
                    with win.volume(route.cable.id) as endpoint:
                        volume = SystemVolume(endpoint, self.sync_level)
                        checked_at = 0.0
                        self.emit("playing", online)
                        log.info("Playing: receivers=%d latency_mode=low lead_frames=%d initial=%d muted=%s "
                                 "receiver_max_db=%s", online, audio.LATENCY_LOW, volume.level.percent,
                                 volume.level.muted, list(receiver_levels.values()))

                        def provider():
                            nonlocal checked_at
                            now = time.monotonic()
                            if now - checked_at >= 0.75:
                                route.check()
                                if not all(m.session.is_alive(10) for m in group.members):
                                    raise RuntimeError("音箱连接已中断，请检查网络后重试。")
                                checked_at = now
                            volume.service()
                            return capture.read()

                        group.stream_live(provider, self.stop, record_log=False,
                                          speaker_gains=self.mix.gains)
                    log.info("Capture bytes=%d dropped=%d", capture.bytes_in, capture.bytes_dropped)
                finally:
                    self.stop.set()
                    recover(win, journal)
        except Exception as exc:
            issue = str(exc)
            log.exception("Playback stopped")
        finally:
            if group is not None:
                # Each member requests TEARDOWN independently, including partial setup.
                try:
                    with ThreadPoolExecutor(max_workers=len(group.members)) as pool:
                        list(pool.map(lambda member: member.session.teardown(), group.members))
                except Exception as exc:
                    log.exception("Receiver teardown failed")
                    issue = issue or str(exc)
            if capture is not None:
                try:
                    capture.close()
                except RuntimeError as exc:
                    log.error("%s", exc)
                    issue = issue or str(exc)
            self.emit("stopped", issue)
