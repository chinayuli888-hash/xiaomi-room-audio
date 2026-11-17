import struct
import threading
import unittest

import numpy as np

from ap2 import audio
from ap2.group import AirPlay2Group


class UnencryptedTestSink:
    def __init__(self):
        self.packets = []

    def encrypt(self, payload, aad):
        return payload

    def sendto(self, packet, peer):
        self.packets.append(packet)


class SpeakerLevelsTests(unittest.TestCase):
    def test_per_speaker_attenuation_keeps_one_rtp_timeline(self):
        hosts = ["192.0.2.10", "192.0.2.11", "192.0.2.12"]
        group = AirPlay2Group([{"host": host} for host in hosts], latency_frames=audio.LATENCY_LOW)
        group._start_sync_threads = lambda *_: None
        sinks = []
        for member in group.members:
            sink = UnencryptedTestSink()
            member.cipher = sink
            member.session._sock_audio = sink
            sinks.append(sink)

        gains = {hosts[0]: 0.75, hosts[1]: 0.5, hosts[2]: 0.0}
        shape = audio.FRAMES_PER_PACKET * audio.CHANNELS
        frames = np.full(shape, -1024, dtype=np.int16)

        def encode(value):
            return audio.encode_alac_frame(np.full(shape, value, dtype=np.int16), audio.FRAMES_PER_PACKET)

        group.stream_live(lambda: frames, threading.Event(), max_packets=2, speaker_gains=lambda: gains)
        timestamps = []
        for sink, sample in zip(sinks, (-768, -512, 0)):
            self.assertEqual([packet[12:] for packet in sink.packets], [encode(sample)] * 2)
            timestamps.append([struct.unpack_from("!I", packet, 4)[0] for packet in sink.packets])
        self.assertEqual(timestamps[0], [audio.LATENCY_LOW, audio.LATENCY_LOW + audio.FRAMES_PER_PACKET])
        self.assertEqual(timestamps, [timestamps[0]] * len(hosts))
