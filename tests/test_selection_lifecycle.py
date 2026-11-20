"""Selection and preference regressions with no real GUI, audio or network IO."""
from pathlib import Path
from queue import SimpleQueue
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from device_catalog import DeviceCatalog
from discovery import Network, Speaker
from engine import Settings, SpeakerMix
from room_audio import Window
from speaker_preferences import SpeakerPreferences
from windows_audio import Level


HOME = Network((("192.0.2.4", "255.255.255.0"),), "test-home")
AWAY = Network((("198.51.100.4", "255.255.255.0"),), "test-away")
BLACK = Speaker("mac:test-black", "Black", "192.0.2.20", 7000)
PRO = Speaker("mac:test-pro", "Pro", "192.0.2.21", 7099)
WHITE = Speaker("mac:test-white", "White", "192.0.2.22", 7000)


class Value:
    def __init__(self, value=None):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class Button:
    def __init__(self):
        self.disabled = False
        self.options = {}

    def state(self, flags):
        for flag in flags:
            if flag in ("disabled", "!disabled"):
                self.disabled = flag == "disabled"

    def configure(self, **options):
        self.options.update(options)


class Root:
    def __init__(self):
        self.callbacks = {}
        self.destroyed = False

    def after(self, delay, callback):
        token = f"callback-{len(self.callbacks)}"
        self.callbacks[token] = (delay, callback)
        return token

    def after_cancel(self, token):
        self.callbacks.pop(token, None)

    def destroy(self):
        self.destroyed = True

    def state(self):
        return "normal"


class Worker:
    def is_alive(self):
        return True


class SelectionLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        for name in ("showerror", "showwarning", "showinfo"):
            guard = patch("room_audio.messagebox." + name)
            guard.start()
            self.addCleanup(guard.stop)
        app = self.app = Window.__new__(Window)
        app.root = Root()
        app.state_dir = Path(self.temporary.name)
        app.base_settings = Settings([], Path("never-executed.exe"))
        app.settings = app.base_settings.for_speakers(())
        app.preferences = SpeakerPreferences(app.state_dir / "speaker_profiles.json")
        app.profiles = app.preferences.profiles
        app.preferences_failed = False
        app.balances = {}
        app.selection = {}
        app.choice_vars = {}
        app.mix_devices = ()
        app.requested_ids = app.save_after = None
        app.catalog = DeviceCatalog()
        app.network_info = HOME
        app.scan_stop, app.monitor_stop = threading.Event(), threading.Event()
        app.scanning = app.stopping = app.closing = app.playing = False
        app.refreshing_levels = False
        app.pending_scan = None
        app.events = SimpleQueue()
        app.player = app.thread = app.tray = None
        app.mix = SpeakerMix(())
        app.rows = {}
        app.network, app.count, app.status = Value(), Value(), Value()
        app.play, app.scan, app.exit = Button(), Button(), Button()
        app.watch_network = Mock()

        def render():
            app.rows = {device.host: (Value(), Value(), Button()) for device in app.catalog.devices}

        app.render_devices = render

    def accept(self, devices=(BLACK, PRO, WHITE), selected=None):
        app = self.app
        if selected is not None:
            for device in devices:
                app.preferences.set_selected(device.identifier, device.identifier in selected)
        token = app.catalog.begin(HOME)
        app.catalog.apply(token, HOME, devices)
        app.selection = {device.identifier: app.preferences.selected(device.identifier)
                         for device in devices}
        app.render_devices()
        app.apply_selection()
        return token

    def fresh_result(self, devices):
        token = self.app.catalog.begin(HOME)
        self.app.scanning = True
        self.app.scan_result((token, HOME, devices, None, True))

    def test_can_list_twelve_but_start_requires_one_to_eight_selected(self):
        devices = tuple(Speaker(f"mac:test-{i}", f"Room {i}", f"192.0.2.{30 + i}", 7000)
                        for i in range(12))
        for count in (0, 9):
            with self.subTest(selected=count):
                self.accept(devices, {d.identifier for d in devices[:count]})
                self.assertEqual(len(self.app.catalog.devices), 12)
                self.app.search = Mock()
                self.app.start()
                self.app.search.assert_not_called()
        self.accept(devices, {d.identifier for d in devices[:8]})
        self.app.search = Mock()
        self.app.start()
        self.app.search.assert_called_once_with(auto_start=True)
        self.assertEqual(self.app.requested_ids, frozenset(d.identifier for d in devices[:8]))

    def test_fresh_scan_preserves_selected_identity_when_ip_changes(self):
        self.accept(selected={BLACK.identifier})
        self.app.search = Mock()
        self.app.start()
        moved = Speaker(BLACK.identifier, BLACK.name, "192.0.2.50", 49152)
        newcomer = Speaker("mac:test-new", "New room", "192.0.2.51", 7000)
        self.assertTrue(self.app.preferences.selected(newcomer.identifier))
        self.app.begin_playback = Mock()
        self.fresh_result([moved, PRO, newcomer])
        self.assertEqual(self.app.settings.hosts, [moved.host])
        self.assertEqual(set(self.app.mix.gains()), {moved.host})
        self.assertFalse(self.app.selection[PRO.identifier])
        self.assertFalse(self.app.selection[newcomer.identifier])
        self.app.begin_playback.assert_called_once_with(HOME)

    def test_all_requested_speakers_missing_does_not_play_remaining_devices(self):
        self.accept(selected={BLACK.identifier})
        self.app.search = Mock()
        self.app.start()
        self.app.begin_playback = Mock()
        self.fresh_result([PRO, WHITE])
        self.assertEqual(self.app.settings.hosts, [])
        self.assertEqual(dict(self.app.mix.gains()), {})
        self.assertTrue(self.app.play.disabled)
        self.app.begin_playback.assert_not_called()

    def test_player_and_gain_map_include_only_checked_speakers(self):
        self.accept(selected={BLACK.identifier, WHITE.identifier})
        with patch("engine.Player") as player, patch("room_audio.threading.Thread"):
            self.app.begin_playback(HOME)
        player.assert_called_once()
        settings, _, _, mix, _ = player.call_args.args
        self.assertEqual(settings.hosts, [BLACK.host, WHITE.host])
        self.assertEqual(set(mix.gains()), {BLACK.host, WHITE.host})
        self.assertNotIn(PRO.host, settings.speaker_ports)
        self.assertNotIn(PRO.host, mix.ratios())

    def test_empty_or_oversized_selection_cannot_construct_player(self):
        devices = tuple(Speaker(f"mac:test-{i}", f"Room {i}", f"192.0.2.{30 + i}", 7000)
                        for i in range(9))
        for count in (0, 9):
            with self.subTest(selected=count):
                self.accept(devices, {d.identifier for d in devices[:count]})
                with patch("engine.Player") as player, patch("room_audio.threading.Thread") as worker:
                    self.app.begin_playback(HOME)
                player.assert_not_called()
                worker.assert_not_called()

    def test_unchecking_speaker_preserves_its_existing_ratio(self):
        self.app.preferences.set_ratio(BLACK.identifier, 1.5)
        self.accept()
        self.app.choice_changed(BLACK.identifier, False)
        self.assertNotIn(BLACK.host, self.app.settings.hosts)
        self.assertNotIn(BLACK.host, self.app.mix.gains())
        restored = SpeakerPreferences(self.app.preferences.path)
        self.assertFalse(restored.selected(BLACK.identifier))
        self.assertAlmostEqual(restored.ratio(BLACK.identifier), 1.5)

    def set_unequal_ratios_then_silence(self, muted):
        self.accept()
        self.app.playing = True
        self.app.mix.set_master(Level(.05, False))
        self.app.mix.set_speaker_percent(BLACK.host, 7.5)
        self.app.mix.set_speaker_percent(PRO.host, 2.5)
        self.app.mix.set_master(Level(.05 if muted else 0.0, muted))
        self.assertTrue(all(value == 0 for value in self.app.mix.gains().values()))

    def assert_ratios_saved(self):
        restored = SpeakerPreferences(self.app.state_dir / "speaker_profiles.json")
        self.assertAlmostEqual(restored.ratio(BLACK.identifier), 1.5)
        self.assertAlmostEqual(restored.ratio(PRO.identifier), .5)
        self.assertAlmostEqual(restored.ratio(WHITE.identifier), 1.0)

    def test_exit_saves_ratios_while_system_is_muted_or_zero(self):
        for muted in (True, False):
            with self.subTest(muted=muted):
                self.app.closing = False
                self.set_unequal_ratios_then_silence(muted)
                self.app.player = SimpleNamespace(stop=threading.Event())
                self.app.thread = Worker()
                self.app.exit_app()
                self.assertTrue(self.app.player.stop.is_set())
                self.assert_ratios_saved()

    def test_network_change_saves_muted_ratios_before_clearing_devices(self):
        self.set_unequal_ratios_then_silence(True)
        self.app.player = SimpleNamespace(stop=threading.Event())
        self.app.thread = Worker()
        self.app.launch_scan = Mock()
        self.app.network_changed(AWAY)
        self.assertEqual(self.app.settings.hosts, [])
        self.assertTrue(self.app.player.stop.is_set())
        self.assert_ratios_saved()

    def extreme_ratio(self):
        self.accept()
        self.app.playing = True
        self.app.mix.set_master(Level(.00005, False))
        # A tiny but nonzero system scalar can produce ratios beyond a
        # persistence validation limit. This is fake PCM state, no playback.
        self.app.mix.set_speaker_percent(BLACK.host, 9)
        self.app.player = SimpleNamespace(stop=threading.Event())
        self.app.thread = Worker()

    def test_preference_validation_failure_must_not_block_exit_stop(self):
        self.extreme_ratio()
        self.app.exit_app()
        self.assertTrue(self.app.closing)
        self.assertTrue(self.app.player.stop.is_set())

    def test_preference_validation_failure_must_not_block_network_stop(self):
        self.extreme_ratio()
        self.app.launch_scan = Mock()
        self.app.network_changed(AWAY)
        self.assertTrue(self.app.player.stop.is_set())
        self.assertFalse(self.app.catalog.ready)
        self.assertEqual(self.app.settings.hosts, [])

    def test_persisted_ratio_follows_identity_after_restart_not_reused_ip(self):
        app = self.app
        app.preferences.set_ratio(BLACK.identifier, 1.5)
        app.preferences.set_selected(PRO.identifier, False)
        app.preferences.save()
        app.preferences = SpeakerPreferences(app.state_dir / "speaker_profiles.json")
        app.profiles = app.preferences.profiles
        moved = Speaker(BLACK.identifier, BLACK.name, "192.0.2.50", 7000)
        unrelated = Speaker("mac:test-other", "Other", BLACK.host, 7000)
        self.accept([moved, PRO, unrelated])
        self.assertEqual(app.settings.hosts, [moved.host, unrelated.host])
        self.assertAlmostEqual(app.mix.ratios()[moved.host], 1.5)
        self.assertAlmostEqual(app.mix.ratios()[unrelated.host], 1.0)
        self.assertNotIn(PRO.host, app.mix.ratios())


if __name__ == "__main__":
    unittest.main()
