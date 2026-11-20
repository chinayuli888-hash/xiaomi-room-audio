"""Network-selection regressions; no Tk window, audio endpoint or network IO."""
import json
from pathlib import Path
import tempfile
import unittest

from device_catalog import DeviceCatalog
from discovery import Network, Speaker
from engine import RECEIVER_DB, Settings


HOME = Network((("192.0.2.4", "255.255.255.0"),), "home")
AWAY = Network((("198.51.100.4", "255.255.255.0"),), "away")


def speaker(index=20, *, network="192.0.2", identifier=None, port=7000):
    return Speaker(identifier or f"mac:0000000000{index:02x}", f"Speaker {index}",
                   f"{network}.{index}", port, "XiaomiSound")


class DeviceCatalogTests(unittest.TestCase):
    def setUp(self):
        self.catalog = DeviceCatalog()

    def accept(self, network=HOME, devices=None):
        devices = [speaker()] if devices is None else devices
        token = self.catalog.begin(network)
        self.assertTrue(self.catalog.apply(token, network, devices))
        return token

    def assert_empty(self):
        self.assertFalse(self.catalog.ready)
        self.assertEqual(tuple(self.catalog.devices), ())
        self.assertFalse(self.catalog.can_play(HOME))
        self.assertFalse(self.catalog.can_play(AWAY))

    def test_startup_never_has_previously_configured_receivers(self):
        self.assert_empty()
        self.assertIsNone(self.catalog.network)
        self.assertFalse(self.catalog.can_play(None))

    def test_search_clears_old_results_then_replaces_instead_of_appending(self):
        first = self.accept()
        second = self.catalog.begin(AWAY)
        self.assertGreater(second, first)
        self.assert_empty()
        fresh = [speaker(30, network="198.51.100", port=49152)]
        self.assertTrue(self.catalog.apply(second, AWAY, fresh))
        self.assertEqual(list(self.catalog.devices), fresh)
        self.assertTrue(self.catalog.can_play(AWAY))
        self.assertFalse(self.catalog.can_play(HOME))
        # A repeated search of the same network is also a replacement.
        third = self.catalog.begin(AWAY)
        self.assertTrue(self.catalog.apply(third, AWAY, fresh))
        self.assertEqual(len(self.catalog.devices), 1)

    def test_empty_search_and_current_error_never_restore_old_devices(self):
        self.accept()
        token = self.catalog.begin(HOME)
        self.assertTrue(self.catalog.apply(token, HOME, []))
        self.assert_empty()
        token = self.accept()
        self.assertTrue(self.catalog.fail(token))
        self.assert_empty()

    def test_late_result_and_error_cannot_override_newer_search(self):
        old = self.catalog.begin(HOME)
        new = self.catalog.begin(AWAY)
        fresh = [speaker(network="198.51.100")]
        self.assertTrue(self.catalog.apply(new, AWAY, fresh))
        self.assertFalse(self.catalog.apply(old, HOME, [speaker()]))
        self.assertFalse(self.catalog.fail(old))
        self.assertEqual(list(self.catalog.devices), fresh)
        self.assertTrue(self.catalog.can_play(AWAY))

    def test_same_ssid_with_changed_interface_still_requires_fresh_scan(self):
        self.accept()
        moved = Network(AWAY.addresses, HOME.ssid)
        self.assertFalse(self.catalog.can_play(moved))
        token = self.catalog.begin(moved)
        self.assertFalse(self.catalog.apply(token, HOME, [speaker()]))
        self.assert_empty()
        self.assertTrue(self.catalog.apply(token, moved, [speaker(network="198.51.100")]))
        self.assertTrue(self.catalog.can_play(moved))

    def test_cancel_or_shutdown_invalidates_in_flight_scan(self):
        for replacement in (None, AWAY):
            with self.subTest(replacement=replacement):
                token = self.catalog.begin(HOME)
                self.catalog.invalidate(replacement)
                self.assertFalse(self.catalog.apply(token, HOME, [speaker()]))
                self.assertFalse(self.catalog.fail(token))
                self.assert_empty()

    def test_discovery_can_list_more_than_eight_for_user_selection(self):
        self.accept(devices=[speaker(i) for i in range(20, 28)])
        self.assertEqual(len(self.catalog.devices), 8)
        self.assertTrue(self.catalog.can_play(HOME))
        token = self.catalog.begin(HOME)
        self.assertTrue(self.catalog.apply(token, HOME, [speaker(i) for i in range(20, 32)]))
        self.assertEqual(len(self.catalog.devices), 12)
        self.assertTrue(self.catalog.can_play(HOME))

    def test_duplicate_host_batch_is_rejected_even_with_different_identities(self):
        token = self.catalog.begin(HOME)
        duplicate = Speaker("mac:other", "Other speaker", speaker().host, 7099)
        with self.assertRaises(ValueError):
            self.catalog.apply(token, HOME, [speaker(), duplicate])
        self.assert_empty()


class FreshSettingsTests(unittest.TestCase):
    def setUp(self):
        self.old_host = "192.0.2.20"
        self.base = Settings([self.old_host], Path("unused-ffmpeg.exe"),
                             {self.old_host: "Old calibrated speaker"},
                             {self.old_host: 0.0}, {self.old_host: 7000})

    def test_fresh_list_replaces_legacy_hosts_and_preserves_advertised_port(self):
        device = speaker(30, network="198.51.100", port=49152)
        settings = self.base.for_speakers([device])
        self.assertEqual(settings.hosts, [device.host])
        self.assertEqual(settings.speaker_names, {device.host: device.name})
        self.assertEqual(settings.receiver_port(device.host), 49152)
        self.assertEqual(settings.ffmpeg, self.base.ffmpeg)
        self.assertNotIn(self.old_host, settings.speaker_max_db)
        self.assertEqual(self.base.hosts, [self.old_host])

    def test_new_speaker_reusing_old_ip_does_not_inherit_legacy_gain(self):
        device = speaker(identifier="mac:brand-new")
        settings = self.base.for_speakers([device])
        self.assertEqual(settings.receiver_db(device.host), RECEIVER_DB)
        self.assertEqual(settings.speaker_names[device.host], device.name)

    def test_profile_follows_identifier_across_ip_change_not_other_speaker(self):
        calibrated = speaker(30, network="198.51.100", identifier="mac:known", port=7099)
        other = speaker(20, identifier="mac:new")
        settings = self.base.for_speakers([calibrated, other], {
            "mac:known": {"name": "Calibrated black", "max_db": 0.0},
            self.old_host: {"name": "Stale IP profile", "max_db": 0.0},
        })
        self.assertEqual(settings.speaker_names[calibrated.host], "Calibrated black")
        self.assertEqual(settings.receiver_db(calibrated.host), 0.0)
        self.assertEqual(settings.receiver_port(calibrated.host), 7099)
        self.assertEqual(settings.speaker_names[other.host], other.name)
        self.assertEqual(settings.receiver_db(other.host), RECEIVER_DB)

    def test_empty_result_constructs_empty_settings_without_fallback(self):
        settings = self.base.for_speakers([])
        self.assertEqual(settings.hosts, [])
        self.assertEqual(settings.speaker_names, {})
        self.assertEqual(settings.speaker_max_db, {})
        self.assertEqual(settings.speaker_ports, {})

    def test_invalid_profile_cannot_raise_unrecognized_speaker_gain(self):
        device = speaker()
        for profile in ({"max_db": True}, {"max_db": 1}, {"max_db": -22},
                        {"max_db": float("nan")}, "invalid profile"):
            with self.subTest(profile=profile):
                settings = self.base.for_speakers([device], {device.identifier: profile})
                self.assertEqual(settings.receiver_db(device.host), RECEIVER_DB)

    def test_fresh_settings_enforces_eight_and_no_duplicate_hosts(self):
        self.assertEqual(len(self.base.for_speakers([speaker(i) for i in range(20, 28)]).hosts), 8)
        for devices in ([speaker(i) for i in range(20, 29)], [speaker(), speaker()]):
            with self.subTest(count=len(devices)), self.assertRaises(ValueError):
                self.base.for_speakers(devices)

    def test_invalid_advertised_port_is_not_replaced_with_default(self):
        for port in (0, -1, 65536, "7000", True, False):
            with self.subTest(port=port), self.assertRaises(ValueError):
                self.base.for_speakers([speaker(port=port)])

    def test_file_with_zero_hosts_loads_without_old_address_requirement(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            # Only an existence sentinel; tests never execute this file.
            (directory / "ffmpeg.exe").write_bytes(b"")
            config = directory / "settings.json"
            config.write_text(json.dumps({"hosts": [], "ffmpeg": "ffmpeg.exe"}), encoding="utf-8")
            settings = Settings.load(config)
        self.assertEqual(settings.hosts, [])
        self.assertEqual(settings.speaker_names, {})


if __name__ == "__main__":
    unittest.main()
