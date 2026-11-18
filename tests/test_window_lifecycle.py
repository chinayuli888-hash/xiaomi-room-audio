"""Exercise real Window transitions with in-memory widgets, never a Tk window."""
from pathlib import Path
from queue import SimpleQueue
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from device_catalog import DeviceCatalog
from discovery import Network, Speaker
from engine import Player, Settings, SpeakerMix
from room_audio import Window


HOME = Network((("192.0.2.4", "255.255.255.0"),), "same-ssid")
AWAY = Network((("198.51.100.4", "255.255.255.0"),), "same-ssid")
HOME_SPEAKER = Speaker("mac:home", "Home", "192.0.2.20", 7000)
AWAY_SPEAKER = Speaker("mac:away", "Away", "198.51.100.20", 49152)


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
        self.callbacks = []
        self.destroyed = False

    def after(self, delay, callback):
        self.callbacks.append((delay, callback))

    def destroy(self):
        self.destroyed = True

    def state(self):
        return "normal"


class Worker:
    def __init__(self, alive=True):
        self.alive = alive

    def is_alive(self):
        return self.alive


class WindowLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        app = self.app = Window.__new__(Window)
        app.root = Root()
        app.state_dir = Path(self.temporary.name)
        app.base_settings = Settings([HOME_SPEAKER.host], Path("never-executed.exe"),
                                     {HOME_SPEAKER.host: "Legacy name"},
                                     {HOME_SPEAKER.host: 0.0})
        app.settings = app.base_settings.for_speakers(())
        app.catalog = DeviceCatalog()
        app.network_info = HOME
        app.scan_stop = threading.Event()
        app.monitor_stop = threading.Event()
        app.scanning = app.stopping = app.closing = app.playing = False
        app.refreshing_levels = False
        app.pending_scan = None
        app.balances, app.profiles = {}, {}
        app.events = SimpleQueue()
        app.player = app.thread = app.tray = None
        app.mix = SpeakerMix(())
        app.rows = {}
        app.network, app.count, app.status = Value(), Value(), Value()
        app.play, app.scan, app.exit = Button(), Button(), Button()
        app.watch_network = Mock()

        def render():
            app.rows = {host: (Value(), Value(), Button()) for host in app.settings.hosts}

        app.render_devices = render

    def accept(self, network=HOME, devices=None):
        app = self.app
        devices = [HOME_SPEAKER] if devices is None else devices
        app.network_info = network
        token = app.catalog.begin(network)
        app.catalog.apply(token, network, devices)
        app.settings = app.base_settings.for_speakers(devices)
        app.mix = SpeakerMix(app.settings.hosts)
        app.render_devices()
        return token

    def test_start_always_reconfirms_even_a_ready_same_network(self):
        self.accept()
        self.app.search = Mock()
        with patch("engine.Player") as player:
            self.app.start()
        self.app.search.assert_called_once_with(auto_start=True)
        player.assert_not_called()

    def test_start_is_blocked_during_scanning_stopping_closing_or_live_sender(self):
        for state in ("scanning", "stopping", "closing", "live-thread"):
            with self.subTest(state=state):
                self.app.scanning = state == "scanning"
                self.app.stopping = state == "stopping"
                self.app.closing = state == "closing"
                self.app.thread = Worker() if state == "live-thread" else None
                self.app.search = Mock()
                self.app.start()
                self.app.search.assert_not_called()

    def test_begin_playback_requires_both_current_and_requested_network(self):
        self.accept()
        with patch("engine.Player") as player, patch("room_audio.threading.Thread") as worker:
            self.app.begin_playback(AWAY)
            self.app.network_info = AWAY
            self.app.begin_playback(HOME)
            self.app.catalog.invalidate(HOME)
            self.app.network_info = HOME
            self.app.begin_playback(HOME)
        player.assert_not_called()
        worker.assert_not_called()

    def test_fresh_accepted_results_create_only_one_sender_with_that_network(self):
        self.accept(AWAY, [AWAY_SPEAKER])
        with patch("engine.Player") as player, patch("room_audio.threading.Thread") as worker:
            self.app.begin_playback(AWAY)
            self.app.begin_playback(AWAY)
        self.assertEqual(player.call_count, 1)
        self.assertEqual(player.call_args.args[0].hosts, [AWAY_SPEAKER.host])
        self.assertEqual(player.call_args.args[0].receiver_port(AWAY_SPEAKER.host), 49152)
        self.assertEqual(player.call_args.args[-1], AWAY)
        worker.return_value.start.assert_called_once()

    def test_network_change_waits_for_sender_return_after_stopped_event(self):
        self.accept()
        app = self.app
        app.playing = True
        old_stop = app.scan_stop
        app.player = SimpleNamespace(stop=threading.Event())
        app.thread = old_worker = Worker()
        app.launch_scan = Mock()
        app.network_changed(AWAY)
        self.assertTrue(old_stop.is_set())
        self.assertTrue(app.player.stop.is_set())
        self.assertEqual(app.settings.hosts, [])
        self.assertEqual(app.pending_scan, (AWAY, False))
        app.launch_scan.assert_not_called()

        app.events.put(("stopped", None))
        app.poll()
        self.assertTrue(app.stopping)
        app.launch_scan.assert_not_called()
        wait_callbacks = [callback for delay, callback in app.root.callbacks if delay == 50]
        self.assertEqual(len(wait_callbacks), 1)
        old_worker.alive = False
        wait_callbacks[0]()
        self.assertFalse(app.stopping)
        self.assertIsNone(app.player)
        self.assertIsNone(app.thread)
        app.launch_scan.assert_called_once_with(AWAY, False)

    def test_old_scan_result_does_not_change_new_results_or_autostart(self):
        app = self.app
        old = app.catalog.begin(HOME)
        self.accept(AWAY, [AWAY_SPEAKER])
        app.begin_playback = Mock()
        app.scan_result((old, HOME, [HOME_SPEAKER], None, True))
        self.assertEqual(app.settings.hosts, [AWAY_SPEAKER.host])
        self.assertEqual(tuple(app.catalog.devices), (AWAY_SPEAKER,))
        app.begin_playback.assert_not_called()

    def test_scan_error_or_empty_result_leaves_no_legacy_fallback(self):
        app = self.app
        app.begin_playback = Mock()
        for devices, issue in (([], None), ([AWAY_SPEAKER], "discovery failed")):
            with self.subTest(issue=issue):
                self.accept()
                app.clear_devices(HOME)
                token = app.catalog.begin(HOME)
                app.scanning = True
                app.scan_result((token, HOME, devices, issue, True))
                self.assertFalse(app.catalog.ready)
                self.assertEqual(app.settings.hosts, [])
                self.assertEqual(app.rows, {})
                self.assertTrue(app.play.disabled)
        app.begin_playback.assert_not_called()

    def test_exit_cancels_scan_and_discards_late_success(self):
        app = self.app
        token = app.catalog.begin(HOME)
        app.scanning = True
        app.begin_playback = Mock()
        app.exit_app()
        self.assertTrue(app.scan_stop.is_set())
        self.assertTrue(app.monitor_stop.is_set())
        self.assertTrue(app.root.destroyed)
        app.scan_result((token, HOME, [HOME_SPEAKER], None, True))
        self.assertFalse(app.catalog.ready)
        self.assertEqual(app.settings.hosts, [])
        app.begin_playback.assert_not_called()

    def test_pending_scan_cannot_run_before_failed_route_restore(self):
        app = self.app
        app.stopping = True
        app.thread = Worker(alive=False)
        app.pending_scan = (AWAY, False)
        app.launch_scan = Mock()
        (app.state_dir / "restore.json").write_text("{}", encoding="utf-8")
        with patch("room_audio.messagebox.showerror") as alert:
            app.finish_stop("restore failed")
        app.launch_scan.assert_not_called()
        self.assertIsNone(app.pending_scan)
        self.assertTrue(app.play.disabled)
        alert.assert_called_once()

    def test_late_playing_event_after_network_change_cannot_mark_playing(self):
        app = self.app
        self.accept()
        app.player = SimpleNamespace(stop=threading.Event())
        app.thread = Worker()
        app.launch_scan = Mock()
        app.network_changed(AWAY)
        app.events.put(("playing", 1))
        app.poll()
        self.assertFalse(app.playing)
        self.assertTrue(app.player.stop.is_set())


class PlayerNoAudioGateTests(unittest.TestCase):
    def test_changed_network_prevents_any_audio_or_group_initialization(self):
        settings = Settings([HOME_SPEAKER.host], Path("never-executed.exe"))
        events = []
        with tempfile.TemporaryDirectory() as temp:
            player = Player(settings, Path(temp), lambda *event: events.append(event),
                            SpeakerMix(settings.hosts), HOME)
            with patch("discovery.current_network", return_value=AWAY), \
                    patch("engine.WindowsAudio") as windows, patch("engine.AirPlay2Group") as group, \
                    self.assertLogs("engine", level="ERROR"):
                player.run()
        windows.assert_not_called()
        group.assert_not_called()
        self.assertEqual(events[-1][0], "stopped")
        self.assertIn("网络", events[-1][1])


if __name__ == "__main__":
    unittest.main()
