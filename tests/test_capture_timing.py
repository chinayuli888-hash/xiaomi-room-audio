"""No audio hardware: bounded capture, wire pacing and COM thread ownership."""
import math
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np

from ap2 import audio
from ap2.group import AirPlay2Group
from engine import AudioControl, CaptureBuffer, FRAME_BYTES, MAX_QUEUE, SpeakerMix
from windows_audio import Level


class Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        # Deliberately unlike QPC: Windows Python 3.12 GetTickCount64.
        return math.floor(self.now / .015625) * .015625

    def perf_counter(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class CoarseEvent:
    """Model Windows' coarse condition wait without changing system timers."""
    def __init__(self, clock):
        self.clock = clock

    def is_set(self):
        return False

    def wait(self, seconds):
        self.clock.now += math.ceil(seconds / .015625) * .015625
        return False


def wire_probe(provider, packets=12, prerecorded=False):
    clock = Clock()
    group = AirPlay2Group([{"host": "192.0.2.10"}, {"host": "192.0.2.11"}],
                          latency_frames=audio.LATENCY_LOW)
    group._start_sync_threads = lambda *_: None
    sinks = []
    for member in group.members:
        sink = SimpleNamespace(packets=[], times=[])

        def sendto(packet, peer, sink=sink):
            sink.packets.append(packet)
            sink.times.append(clock.now)
            # Work between deadlines exposes coarse-clock drift; a zero-cost
            # fake can pass even with the wrong clock when waits are capped.
            clock.now += .0001

        member.session._sock_audio = SimpleNamespace(sendto=sendto)
        member.cipher = SimpleNamespace(encrypt=lambda payload, aad: payload)
        sinks.append(sink)
    with patch("ap2.group.time", clock):
        result = (group.stream(np.tile(provider(), packets), record_log=False)
                  if prerecorded else
                  group.stream_live(provider, CoarseEvent(clock), max_packets=packets))
    return result, sinks


class CaptureTimingTests(unittest.TestCase):
    def test_partial_pipe_reads_preserve_pcm_frames_without_padding_or_loss(self):
        buffer = CaptureBuffer()
        expected = np.arange(6 * FRAME_BYTES // 2, dtype=np.int16)
        wire = expected.tobytes()
        for offset in range(0, len(wire), 4096):
            buffer.feed(wire[offset:offset + 4096])
        actual = np.concatenate([buffer.read() for _ in range(6)])
        np.testing.assert_array_equal(actual, expected)
        self.assertIsNone(buffer.read())
        stats = buffer.stats()
        self.assertEqual(stats["bytes_out"], len(wire))
        self.assertEqual(stats["bytes_dropped"], 0)
        self.assertEqual(stats["underrun_packets"], 1)

    def test_overflow_keeps_latency_bound_and_reports_startup_separately(self):
        buffer = CaptureBuffer()
        source = np.arange(16 * FRAME_BYTES // 2, dtype=np.int16)
        buffer.feed(source.tobytes())
        self.assertEqual(buffer.stats()["bytes_dropped"], FRAME_BYTES)
        self.assertEqual(buffer.stats()["pending_bytes"], MAX_QUEUE)
        buffer.mark_stream_start()
        buffer.feed(bytes(FRAME_BYTES))
        stats = buffer.stats()
        self.assertEqual(stats["startup_bytes_dropped"], FRAME_BYTES)
        self.assertEqual(stats["stream_bytes_dropped"], FRAME_BYTES)
        self.assertEqual(stats["bytes_dropped"], 2 * FRAME_BYTES)
        self.assertEqual(stats["queue_limit_bytes"], 15 * FRAME_BYTES)
        self.assertEqual(stats["pending_bytes"], MAX_QUEUE)
        np.testing.assert_array_equal(buffer.read(), source[FRAME_BYTES:FRAME_BYTES * 3 // 2])

    def test_wire_packets_keep_sub_tick_pacing_and_shared_timestamps(self):
        pcm = np.zeros(audio.FRAMES_PER_PACKET * audio.CHANNELS, dtype=np.int16)
        result, sinks = wire_probe(lambda: pcm)
        for index, sink in enumerate(sinks):
            np.testing.assert_allclose(sink.times,
                                       np.arange(12) * audio.FRAMES_PER_PACKET / audio.RATE + index * .0001)
        self.assertEqual([p[4:8] for p in sinks[0].packets],
                         [p[4:8] for p in sinks[1].packets])
        self.assertEqual(result["timing"]["source_underruns"], 0)
        self.assertEqual(result["timing"]["late_over_packet"], 0)
        result, _ = wire_probe(lambda: None, packets=3)
        self.assertEqual(result["timing"]["source_underruns"], 3)
        self.assertEqual(result["timing"]["initial_consecutive_underruns"], 3)
        self.assertEqual(result["timing"]["after_first_pcm_underruns"], 0)
        self.assertIsNone(result["timing"]["first_pcm_after_stream_ms"])

    def test_underrun_phases_preserve_total_with_initial_and_later_gaps(self):
        pcm = np.zeros(audio.FRAMES_PER_PACKET * audio.CHANNELS, dtype=np.int16)
        chunks = iter((None, None, pcm, None, pcm, None))
        result, _ = wire_probe(lambda: next(chunks), packets=6)
        stats = result["timing"]
        self.assertEqual(stats["source_underruns"], 4)
        self.assertEqual(stats["initial_consecutive_underruns"], 2)
        self.assertEqual(stats["after_first_pcm_underruns"], 2)
        self.assertEqual(stats["source_underruns"], stats["initial_consecutive_underruns"] +
                         stats["after_first_pcm_underruns"])
        self.assertAlmostEqual(stats["first_pcm_after_stream_ms"],
                               2 * audio.FRAMES_PER_PACKET / audio.RATE * 1000)

    def test_prerecorded_sender_also_uses_precise_clock_for_deadlines(self):
        pcm = np.zeros(audio.FRAMES_PER_PACKET * audio.CHANNELS, dtype=np.int16)
        _, sinks = wire_probe(lambda: pcm, prerecorded=True)
        np.testing.assert_allclose(sinks[0].times, np.arange(12) * audio.FRAMES_PER_PACKET / audio.RATE)

    def test_capture_gap_and_duration_are_not_quantized_to_gettickcount(self):
        clock = Clock()
        buffer = CaptureBuffer()
        with patch("engine.time", clock):
            buffer.feed(bytes(100))
            clock.now = .007
            buffer.feed(bytes(100))
            buffer.mark_stream_start()
            clock.now = .011
            stats = buffer.stats()
        self.assertEqual(stats["max_reader_gap_ms"], 7)
        self.assertEqual(stats["stream_seconds"], .004)
        self.assertNotEqual(clock.monotonic(), clock.perf_counter())

    def test_saved_ratios_survive_group_limiting_zero_and_mute(self):
        mix = SpeakerMix(("black", "pro", "white"))
        original = {"black": 2.0, "pro": .5, "white": .8}
        mix.restore_ratios(original)
        for level in (Level(.8, False), Level(0, False), Level(.1, True)):
            mix.set_master(level)
            saved = mix.ratios()
            self.assertEqual(saved, original)
            saved["black"] = 0
            self.assertEqual(mix.ratios(), original)


class AudioControlTests(unittest.TestCase):
    def windows(self, blocked, release, fail=False):
        main_thread = threading.get_ident()
        owners = []

        class Endpoint:
            def __enter__(self):
                owners.append(threading.get_ident())
                return self

            def __exit__(self, *_):
                owners.append(threading.get_ident())

            def subscribe(self, changed):
                self.changed = changed

            def read(self):
                return Level(.05, False)

        class Windows:
            calls = 0

            def __enter__(self):
                owners.append(threading.get_ident())
                return self

            def __exit__(self, *_):
                owners.append(threading.get_ident())

            def volume(self, cable_id):
                return Endpoint()

            def default(self, role):
                self.calls += 1
                if self.calls > 2:
                    blocked.set()
                    release.wait(2)
                return SimpleNamespace(id="other" if fail else "cable")

        return Windows, main_thread, owners

    def test_slow_route_query_cannot_block_pcm_and_com_stays_in_owner_thread(self):
        blocked, release, stop = threading.Event(), threading.Event(), threading.Event()
        factory, main_thread, owners = self.windows(blocked, release)
        published = []
        member = SimpleNamespace(session=SimpleNamespace(is_alive=lambda timeout: True))
        control = AudioControl("cable", [member], published.append, stop)
        with patch("engine.WindowsAudio", factory), patch.object(AudioControl, "CHECK_INTERVAL", 0):
            try:
                control.start()
                self.assertTrue(blocked.wait(1), "fake route query never blocked")
                buffer = CaptureBuffer()
                buffer.feed(bytes(FRAME_BYTES * 12))

                def provider():
                    control.check()
                    return buffer.read()

                result, sinks = wire_probe(provider)
                self.assertEqual(result["packets"], 24)
                self.assertEqual(result["timing"]["source_underruns"], 0)
                self.assertFalse(release.is_set())
            finally:
                release.set()
                control.close()
        self.assertFalse(control.thread.is_alive())
        self.assertEqual(published[0], Level(.05, False))
        self.assertEqual(len(set(owners)), 1)
        self.assertNotIn(main_thread, owners)

    def test_control_failure_stops_sender_and_preserves_error_for_nonblocking_check(self):
        blocked, release, stop = threading.Event(), threading.Event(), threading.Event()
        factory, _, _ = self.windows(blocked, release, fail=True)
        control = AudioControl("cable", [], lambda level: None, stop)
        with patch("engine.WindowsAudio", factory):
            try:
                with self.assertRaisesRegex(RuntimeError, "系统已选择"):
                    control.start()
                self.assertTrue(stop.is_set())
                with self.assertRaisesRegex(RuntimeError, "系统已选择"):
                    control.check()
            finally:
                release.set()
                control.close()


if __name__ == "__main__":
    unittest.main()
