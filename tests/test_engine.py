from pathlib import Path
import tempfile
import unittest

from engine import CABLE_NAME, RECEIVER_DB, Level, Route, SpeakerMix, SystemVolume, recover, startup_level
from windows_audio import Endpoint


class FakeAudio:
    speakers = Endpoint("speakers", "扬声器/耳机")
    cable = Endpoint("cable", CABLE_NAME)
    bluetooth = Endpoint("bluetooth", "Bluetooth headphones")

    def __init__(self):
        self.defaults = {0: self.speakers, 1: self.bluetooth, 2: self.bluetooth}
        self.cable_level = Level(0.14, True)
        self.fail_media_change = False

    def render_devices(self):
        return [self.speakers, self.cable, self.bluetooth]

    def default(self, role):
        return self.defaults[role]

    def set_default_render(self, endpoint_id, role):
        if self.fail_media_change and role == 1 and endpoint_id == self.cable.id:
            raise OSError("simulated Windows error")
        self.defaults[role] = next(d for d in self.render_devices() if d.id == endpoint_id)

    def level(self, endpoint_id):
        return self.cable_level if endpoint_id == self.cable.id else Level(0.65, False)

    def set_level(self, endpoint_id, level):
        self._require_cable(endpoint_id)
        self.cable_level = level

    def _require_cable(self, endpoint_id):
        if endpoint_id != self.cable.id:
            raise AssertionError("The route may adjust only the temporary cable endpoint.")


class SoundSafetyTests(unittest.TestCase):
    def test_startup_cap_leaves_low_volume_and_mute_intact(self):
        self.assertEqual(startup_level(Level(0.65, False)), Level(0.10, False))
        self.assertEqual(startup_level(Level(0.04, True)), Level(0.04, True))
        self.assertEqual(startup_level(Level(0.65, False), peak_ratio=2), Level(0.05, False))

    def test_rollback_after_partial_switch(self):
        with tempfile.TemporaryDirectory() as temp:
            journal = Path(temp) / "restore.json"
            win = FakeAudio()
            win.fail_media_change = True
            route = Route(win, journal)
            with self.assertRaises(OSError):
                route.begin()
            self.assertEqual(win.defaults[0], win.cable)
            self.assertEqual(win.cable_level, Level(0.10, False))
            recover(win, journal)
            self.assertEqual(win.defaults, {0: win.speakers, 1: win.bluetooth, 2: win.bluetooth})
            self.assertEqual(win.cable_level, Level(0.14, True))
            self.assertFalse(journal.exists())

    def test_manual_device_selection_is_preserved(self):
        with tempfile.TemporaryDirectory() as temp:
            journal = Path(temp) / "restore.json"
            win = FakeAudio()
            Route(win, journal).begin()
            win.defaults[0] = win.bluetooth
            win.cable_level = Level(0.20, True)
            recover(win, journal)
            self.assertEqual(win.defaults, {0: win.bluetooth, 1: win.bluetooth, 2: win.bluetooth})
            self.assertEqual(win.cable_level, Level(0.14, True))


class QuantizedEndpoint:
    level = Level(0.10, False)

    def subscribe(self, notify):
        self.notify = notify

    def read(self):
        return self.level

    def set_scalar(self, scalar):
        self.level = Level(round(scalar * 20) / 20, self.level.muted)
        self.notify()

    def set_muted(self, muted):
        self.level = Level(self.level.scalar, muted)
        self.notify()


class VolumeSyncTests(unittest.TestCase):
    def test_os_notifications_drive_master_readback_and_mute(self):
        endpoint, seen = QuantizedEndpoint(), []
        volume = SystemVolume(endpoint, seen.append)
        endpoint.set_scalar(0.35)
        endpoint.set_muted(True)
        volume.service()
        self.assertEqual(seen[-1], Level(0.35, True))
        self.assertEqual(seen[-1].percent, 35)
        endpoint.set_scalar(0.43)
        endpoint.set_muted(False)
        volume.service()
        self.assertEqual(seen[-1], Level(0.45, False))
        self.assertEqual(seen[-1], endpoint.read())


class SpeakerMixTests(unittest.TestCase):
    def assert_gains(self, mix, *expected):
        self.assertEqual(len(mix.gains()), len(expected))
        for actual, target in zip(mix.gains().values(), expected):
            self.assertAlmostEqual(actual, target)

    def test_independent_raise_then_master_volume_scales_all_three(self):
        mix = SpeakerMix(("left", "middle", "right"))
        mix.set_master(Level(0.10, False))
        mix.set_speaker_percent("left", 15)
        mix.set_speaker_percent("right", 6)
        self.assert_gains(mix, 0.15, 0.10, 0.06)
        mix.set_master(Level(0.20, False))
        self.assert_gains(mix, 0.30, 0.20, 0.12)
        mix.set_master(Level(0.05, False))
        self.assert_gains(mix, 0.075, 0.05, 0.03)

    def test_raised_receiver_ceiling_preserves_startup_signal_budget(self):
        mix = SpeakerMix(("left", "middle", "right"))
        receiver_levels = {"left": 0.0, "middle": RECEIVER_DB, "right": 0.0}
        peak = mix.startup_peak_ratio(receiver_levels)
        mix.set_master(startup_level(Level(0.45, False), peak))
        previous_startup_signal = 0.10 * 10 ** (RECEIVER_DB / 20)
        for host, gain in mix.gains().items():
            signal = gain * 10 ** (receiver_levels[host] / 20)
            self.assertLessEqual(signal, previous_startup_signal + 1e-9)
        self.assertAlmostEqual(mix.gains()["left"], previous_startup_signal)
        mix.set_master(Level(0.01, False))
        self.assert_gains(mix, 0.01, 0.01, 0.01)
        mix.set_speaker_percent("left", 100)
        mix.set_speaker_percent("right", 100)
        self.assert_gains(mix, 1.0, 0.01, 1.0)

    def test_group_limit_preserves_ratio_and_drag_at_limit_is_independent(self):
        mix = SpeakerMix(("left", "middle", "right"))
        mix.set_master(Level(0.4, False))
        mix.set_speaker_percent("left", 80)
        mix.set_speaker_percent("middle", 20)
        mix.set_master(Level(0.8, False))
        self.assert_gains(mix, 1.0, 0.25, 0.5)
        mix.set_speaker_percent("middle", 40)
        self.assert_gains(mix, 1.0, 0.40, 0.5)
        mix.set_master(Level(0.4, False))
        self.assert_gains(mix, 0.5, 0.2, 0.25)

    def test_mute_and_zero_restore_balance_including_a_silent_speaker(self):
        mix = SpeakerMix(("left", "middle", "right"))
        mix.set_master(Level(0.1, False))
        mix.set_speaker_percent("left", 12)
        mix.set_speaker_percent("right", 0)
        mix.set_master(Level(0.1, True))
        self.assert_gains(mix, 0, 0, 0)
        mix.set_master(Level(0.0, False))
        self.assert_gains(mix, 0, 0, 0)
        mix.set_master(Level(0.1, False))
        self.assert_gains(mix, 0.12, 0.10, 0)
