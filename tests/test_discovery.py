import threading
import unittest
from types import SimpleNamespace

from discovery import (Network, NetworkChanged, SERVICE_TYPES, _Listener,
                       discover_speakers)


NET = Network((("192.0.2.4", "255.255.255.0"),), "test")
AP, RAOP = SERVICE_TYPES


def info(host="192.0.2.20", port=7000, properties=None, server="speaker.local."):
    props = {b"features": b"0x200,0x40", b"deviceid": b"AA:BB:CC:DD:EE:FF",
             b"model": b"XiaomiSound"}
    if properties is not None:
        props = properties
    return SimpleNamespace(properties=props, server=server, port=port,
                           parsed_addresses=lambda: [host])


class FakeZC:
    def __init__(self, entries):
        self.entries, self.closed = entries, False

    def get_service_info(self, service_type, name, timeout):
        entry = self.entries.get((service_type, name))
        return entry() if callable(entry) else entry

    def close(self):
        self.closed = True


class FakeBrowser:
    def __init__(self, zc, types, listener, actions):
        self.cancelled = False
        self.listener = listener
        for operation, service_type, name in actions:
            getattr(listener, operation + "_service")(zc, service_type, name)

    def cancel(self):
        self.cancelled = True


class DiscoveryTests(unittest.TestCase):
    def scan(self, entries, actions=None, tcp=None, reader=None, stop=None):
        zc = FakeZC(entries)
        holder = {}

        def zc_factory(**kwargs):
            self.assertEqual(kwargs, {"interfaces": ["192.0.2.4"]})
            return zc

        def browser_factory(z, types, listener):
            self.assertEqual(types, list(SERVICE_TYPES))
            holder["browser"] = FakeBrowser(z, types, listener, actions or [
                ("add", service_type, name) for service_type, name in entries])
            return holder["browser"]

        try:
            return discover_speakers(NET, stop or threading.Event(), timeout=.015,
                                     _network_reader=reader or (lambda: NET),
                                     _zeroconf_factory=zc_factory,
                                     _browser_factory=browser_factory,
                                     _tcp_check=tcp or (lambda *_: True))
        finally:
            if "browser" in holder:
                self.assertTrue(holder["browser"].cancelled)
                self.assertTrue(zc.closed)

    def test_duplicate_services_merge_and_preserve_actual_raop_port(self):
        result = self.scan({(AP, "Kitchen." + AP): info(port=7000),
                            (RAOP, "AABBCCDDEEFF@Kitchen." + RAOP): info(port=7099)})
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].port, 7099)
        self.assertEqual(result[0].identifier, "mac:aabbccddeeff")
        self.assertEqual(result[0].name, "Kitchen")
        self.assertEqual(result[0].model, "XiaomiSound")

    def test_fresh_scan_does_not_keep_previous_results(self):
        self.assertEqual(len(self.scan({(AP, "Speaker." + AP): info()})), 1)
        self.assertEqual(self.scan({}), [])

    def test_remove_during_resolution_cannot_resurrect_service(self):
        listener = _Listener()
        name = "Speaker." + AP
        listener.add_service(None, AP, name)
        _, _, generation = listener.next()
        listener.remove_service(None, AP, name)
        listener.store((AP, name.casefold()), generation, ["stale"])
        self.assertEqual(listener.snapshot(), [])
        self.assertEqual(self.scan({(AP, name): info()},
                                  [("add", AP, name), ("remove", AP, name)]), [])

    def test_removed_during_tcp_probe_is_not_returned(self):
        zc = FakeZC({(AP, "Speaker." + AP): info()})
        browser = []
        def make_browser(z, types, listener):
            item = FakeBrowser(z, types, listener, [("add", AP, "Speaker." + AP)])
            browser.append(item)
            return item
        def probe(*_):
            browser[0].listener.remove_service(zc, AP, "Speaker." + AP)
            return True
        result = discover_speakers(NET, threading.Event(), timeout=.01,
                                  _network_reader=lambda: NET, _zeroconf_factory=lambda **_: zc,
                                  _browser_factory=make_browser, _tcp_check=probe)
        self.assertEqual(result, [])

    def test_removed_ap2_advertisement_cannot_validate_a_remaining_legacy_record(self):
        ap_name, raop_name = "Speaker." + AP, "AABBCCDDEEFF@Speaker." + RAOP
        zc = FakeZC({(AP, ap_name): info(), (RAOP, raop_name): info(
            port=7099, properties={b"ft": b"0x200"})})
        browser = []
        def make_browser(z, types, listener):
            item = FakeBrowser(z, types, listener, [
                ("add", AP, ap_name), ("add", RAOP, raop_name)])
            browser.append(item)
            return item
        def probe(*_):
            browser[0].listener.remove_service(zc, AP, ap_name)
            return True
        self.assertEqual(discover_speakers(
            NET, threading.Event(), timeout=.01, _network_reader=lambda: NET,
            _zeroconf_factory=lambda **_: zc, _browser_factory=make_browser, _tcp_check=probe), [])

    def test_cancellation_during_resolution_returns_empty_and_closes_resources(self):
        stop = threading.Event()
        def cancelled():
            stop.set()
            return info()
        self.assertEqual(self.scan({(AP, "Speaker." + AP): cancelled}, stop=stop), [])

    def test_subnet_loopback_broadcast_multicast_and_own_host_are_rejected(self):
        for host in ("198.51.100.20", "127.0.0.1", "192.0.2.255", "192.0.2.0",
                     "224.0.0.251", "192.0.2.4", "::1"):
            with self.subTest(host=host):
                def forbidden(*_):
                    raise AssertionError("Must not probe rejected address")
                self.assertEqual(self.scan({(AP, "Speaker." + AP): info(host=host)}, tcp=forbidden), [])

    def test_no_ap2_capability_or_unreachable_is_not_returned(self):
        self.assertEqual(self.scan({(RAOP, "AABBCCDDEEFF@Legacy." + RAOP): info(
            properties={b"ft": b"0x200", b"am": b"OldSpeaker"})}), [])
        self.assertEqual(self.scan({(AP, "Speaker." + AP): info()}, tcp=lambda *_: False), [])

    def test_network_change_rejects_results_and_closes_fresh_instance(self):
        changed = Network(NET.addresses, "different-wifi")
        count = 0
        def reader():
            nonlocal count
            count += 1
            return NET if count <= 2 else changed
        with self.assertRaises(NetworkChanged):
            self.scan({(AP, "Speaker." + AP): info()}, reader=reader)

    def test_cancelled_scan_does_not_create_zeroconf(self):
        stop = threading.Event()
        stop.set()
        self.assertEqual(discover_speakers(NET, stop, _zeroconf_factory=lambda **_: self.fail()), [])

    def test_identity_canonicalizes_order_and_mask_and_changes_with_ssid(self):
        same = Network((("192.0.2.4", "24"),), "test")
        self.assertEqual(same.identity, NET.identity)
        self.assertEqual(same.fingerprint, NET.fingerprint)
        self.assertNotEqual(NET.fingerprint, Network(NET.addresses, "other").fingerprint)

    def test_tcp_is_bound_to_current_interface_and_real_port(self):
        seen = []
        self.scan({(AP, "Speaker." + AP): info(port=7123)},
                  tcp=lambda *a: seen.append(a) or True)
        self.assertEqual(seen, [("192.0.2.20", 7123, "192.0.2.4", .2)])


if __name__ == "__main__":
    unittest.main()
