"""Fresh, interface-scoped IPv4 AirPlay 2 discovery; never pairs or sends audio.

APIs: https://python-zeroconf.readthedocs.io/en/latest/api.html
AirPlay capability heuristic: pyatv/protocols/airplay/utils.py (bits 38/48).
"""
from collections import deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import ctypes as C
from ctypes import wintypes as W
from dataclasses import dataclass
import hashlib
import ipaddress
import json
import os
import re
import socket
import threading
import time

SERVICE_TYPES = ("_airplay._tcp.local.", "_raop._tcp.local.")
AP2_FEATURES = (1 << 38) | (1 << 48)


class NetworkChanged(RuntimeError):
    pass


@dataclass(frozen=True)
class Network:
    addresses: tuple[tuple[str, str], ...]
    ssid: str = ""

    def __post_init__(self):
        canonical = set()
        for address, mask in self.addresses:
            interface = ipaddress.IPv4Interface(f"{address}/{mask}")
            if (interface.ip.is_loopback or interface.ip.is_unspecified
                    or interface.ip.is_multicast or interface.network.prefixlen == 0):
                continue
            canonical.add((str(interface.ip), str(interface.netmask)))
        object.__setattr__(self, "addresses", tuple(sorted(canonical)))

    @property
    def identity(self):
        return self.addresses, self.ssid

    @property
    def fingerprint(self):
        return hashlib.sha256(json.dumps(self.identity, ensure_ascii=False).encode()).hexdigest()

    def source_for(self, host):
        try:
            ip = ipaddress.IPv4Address(host)
        except ipaddress.AddressValueError:
            return None
        if ip.is_loopback or ip.is_unspecified or ip.is_multicast or ip.is_reserved:
            return None
        own = {address for address, _ in self.addresses}
        if str(ip) in own:
            return None
        for address, mask in self.addresses:
            net = ipaddress.IPv4Network(f"{address}/{mask}", strict=False)
            if ip in net and ip not in (net.network_address, net.broadcast_address):
                return address
        return None


@dataclass(frozen=True)
class Speaker:
    identifier: str
    name: str
    host: str
    port: int
    model: str = ""


class _SocketAddress(C.Structure):
    _fields_ = [("pointer", C.c_void_p), ("length", C.c_int)]


class _Unicast(C.Structure):
    pass


_Unicast._fields_ = [
    ("length", W.ULONG), ("flags", W.DWORD), ("next", C.POINTER(_Unicast)),
    ("address", _SocketAddress), ("prefix_origin", C.c_int), ("suffix_origin", C.c_int),
    ("dad_state", C.c_int), ("valid", W.ULONG), ("preferred", W.ULONG),
    ("lease", W.ULONG), ("prefix", C.c_ubyte),
]


class _Adapter(C.Structure):
    pass


_Adapter._fields_ = [
    ("length", W.ULONG), ("index", W.DWORD), ("next", C.POINTER(_Adapter)),
    ("name", C.c_char_p), ("unicast", C.POINTER(_Unicast)),
    ("anycast", C.c_void_p), ("multicast", C.c_void_p), ("dns", C.c_void_p),
    ("dns_suffix", W.LPWSTR), ("description", W.LPWSTR), ("friendly", W.LPWSTR),
    ("physical", C.c_ubyte * 8), ("physical_length", W.DWORD), ("flags", W.DWORD),
    ("mtu", W.DWORD), ("kind", W.DWORD), ("status", C.c_int),
]


def _ipv4_addresses():
    if os.name != "nt":
        raise OSError("Automatic network detection currently requires Windows.")
    api = C.WinDLL("iphlpapi").GetAdaptersAddresses
    api.argtypes = [W.ULONG, W.ULONG, C.c_void_p, C.c_void_p, C.POINTER(W.ULONG)]
    api.restype = W.ULONG
    size = W.ULONG(15000)
    for _ in range(3):
        buffer = C.create_string_buffer(size.value)
        code = api(socket.AF_INET, 0x2E, None, buffer, C.byref(size))
        if code != 111:
            break
    if code == 232:  # ERROR_NO_DATA
        return ()
    if code:
        raise C.WinError(code)
    output = []
    adapter = C.cast(buffer, C.POINTER(_Adapter))
    while adapter:
        row = adapter.contents
        # Operational Ethernet/Wi-Fi only, not loopback, VPN, or tunnels.
        if row.status == 1 and row.kind in (6, 71):
            address = row.unicast
            while address:
                item = address.contents
                if (item.address.length >= 16 and item.address.pointer and item.dad_state in (3, 4)
                        and 0 < item.prefix <= 32
                        and C.cast(item.address.pointer, C.POINTER(W.USHORT)).contents.value == socket.AF_INET):
                    ip = socket.inet_ntoa(C.string_at(item.address.pointer + 4, 4))
                    output.append((ip, str(ipaddress.IPv4Network(f"0.0.0.0/{item.prefix}").netmask)))
                address = item.next
        adapter = row.next
    return tuple(output)


def _wifi_ssid():
    """Read Native Wi-Fi metadata without spawning netsh or reading passwords."""
    class Interface(C.Structure):
        _fields_ = [("guid", C.c_ubyte * 16), ("description", C.c_wchar * 256),
                    ("state", W.DWORD)]

    handle, memory = W.HANDLE(), C.c_void_p()
    api = C.WinDLL("wlanapi")
    api.WlanOpenHandle.argtypes = [W.DWORD, C.c_void_p, C.POINTER(W.DWORD), C.POINTER(W.HANDLE)]
    api.WlanEnumInterfaces.argtypes = [W.HANDLE, C.c_void_p, C.POINTER(C.c_void_p)]
    api.WlanQueryInterface.argtypes = [W.HANDLE, C.c_void_p, W.DWORD, C.c_void_p,
                                      C.POINTER(W.DWORD), C.POINTER(C.c_void_p), C.c_void_p]
    api.WlanFreeMemory.argtypes = [C.c_void_p]
    api.WlanCloseHandle.argtypes = [W.HANDLE, C.c_void_p]
    negotiated = W.DWORD()
    if api.WlanOpenHandle(2, None, C.byref(negotiated), C.byref(handle)):
        return ""
    try:
        if api.WlanEnumInterfaces(handle, None, C.byref(memory)):
            return ""
        count = C.cast(memory, C.POINTER(W.DWORD)).contents.value
        rows = C.cast(memory.value + 8, C.POINTER(Interface))
        names = []
        for index in range(min(count, 64)):
            if rows[index].state != 1:
                continue
            data, size = C.c_void_p(), W.DWORD()
            try:
                code = api.WlanQueryInterface(handle, C.byref(rows[index].guid), 7, None,
                                             C.byref(size), C.byref(data), None)
                if not code and data and size.value >= 556:
                    # WLAN_CONNECTION_ATTRIBUTES: 2 DWORD + WCHAR[256], then DOT11_SSID.
                    length = C.cast(data.value + 520, C.POINTER(W.DWORD)).contents.value
                    if length <= 32:
                        names.append(C.string_at(data.value + 524, length).decode("utf-8", "replace"))
            finally:
                if data:
                    api.WlanFreeMemory(data)
        return " / ".join(sorted(set(names)))
    finally:
        if memory:
            api.WlanFreeMemory(memory)
        api.WlanCloseHandle(handle, None)


def current_network() -> Network:
    addresses = _ipv4_addresses()
    try:
        ssid = _wifi_ssid()
    except OSError:
        ssid = ""
    return Network(addresses, ssid)


def _features(value):
    try:
        words = value.split(",")
        if len(words) > 2:
            return 0
        return int(words[0], 16) | (int(words[1], 16) << 32 if len(words) == 2 else 0)
    except (ValueError, AttributeError):
        return 0


@dataclass(frozen=True)
class _Record:
    speaker: Speaker
    capable: bool
    raop: bool
    key: tuple[str, str]
    generation: int


def _records(info, service_type, name, network, key, generation):
    def text(value):
        return value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value or "")
    properties = {text(k).casefold(): text(v) for k, v in info.properties.items()}
    model = properties.get("model") or properties.get("am") or ""
    raop = service_type == SERVICE_TYPES[1]
    features = _features(properties.get("features") or properties.get("ft") or "")
    capable = bool(features & AP2_FEATURES and (raop or features & (1 << 9)))
    instance = name.removesuffix("." + service_type)
    identity = (properties.get("deviceid") or properties.get("device-id")
                or properties.get("macaddress") or properties.get("mac")
                or (instance.split("@", 1)[0] if raop and "@" in instance else ""))
    compact = re.sub(r"[^0-9a-f]", "", identity.casefold())
    identifier = ("mac:" + compact if len(compact) == 12
                  else "device:" + identity.casefold() if identity
                  else "mdns:" + (info.server or instance).casefold().rstrip("."))
    display = properties.get("name") or instance.split("@", 1)[-1]
    if not isinstance(info.port, int) or not 1 <= info.port <= 65535:
        return []
    output = []
    for host in info.parsed_addresses():
        if network.source_for(host):
            output.append(_Record(Speaker(identifier, display, host, info.port, model),
                                  capable, raop, key, generation))
    return output


class _Listener:
    def __init__(self):
        self.lock = threading.Lock()
        self.pending = deque()
        self.active = {}
        self.records = {}
        self.generation = 0

    def add_service(self, zc, service_type, name):
        key = service_type, name.casefold()
        with self.lock:
            self.generation += 1
            self.active[key] = self.generation
            self.records.pop(key, None)
            self.pending.append((service_type, name, self.generation))

    update_service = add_service

    def remove_service(self, zc, service_type, name):
        with self.lock:
            key = service_type, name.casefold()
            self.active.pop(key, None)
            self.records.pop(key, None)

    def next(self):
        with self.lock:
            return self.pending.popleft() if self.pending else None

    def store(self, key, generation, records):
        with self.lock:
            if self.active.get(key) == generation:
                self.records[key] = records

    def snapshot(self):
        with self.lock:
            return [record for group in self.records.values() for record in group]

    def present(self, record):
        with self.lock:
            return self.active.get(record.key) == record.generation


def _tcp_reachable(host, port, source, timeout):
    try:
        with socket.create_connection((host, port), timeout=timeout, source_address=(source, 0)):
            return True
    except OSError:
        return False


def discover_speakers(network: Network, stop_event: threading.Event, timeout=6.0, *,
                      _network_reader=None, _zeroconf_factory=None, _browser_factory=None,
                      _tcp_check=None) -> list[Speaker]:
    """Resolve fresh DNS-SD records only on this network; cancelled scans return [].

    Private keyword hooks allow deterministic tests without sockets or live mDNS.
    """
    if stop_event.is_set():
        return []
    if not 0 < timeout <= 8:
        raise ValueError("Discovery timeout must be greater than 0 and at most 8 seconds.")
    reader, tcp_check = _network_reader or current_network, _tcp_check or _tcp_reachable

    def check_network():
        if reader().identity != network.identity:
            raise NetworkChanged("网络已变化，已丢弃旧网络的音箱结果。")

    check_network()
    if not network.addresses:
        return []
    if _zeroconf_factory is None or _browser_factory is None:
        from zeroconf import IPVersion, ServiceBrowser, Zeroconf
        _zeroconf_factory = _zeroconf_factory or (
            lambda **kw: Zeroconf(ip_version=IPVersion.V4Only, **kw))
        _browser_factory = _browser_factory or ServiceBrowser
    listener, browser = _Listener(), None
    zc = _zeroconf_factory(interfaces=[address for address, _ in network.addresses])
    deadline = time.monotonic() + timeout
    try:
        browser = _browser_factory(zc, list(SERVICE_TYPES), listener=listener)
        next_network_check = 0.0
        while time.monotonic() < deadline and not stop_event.is_set():
            if time.monotonic() >= next_network_check:
                check_network()
                next_network_check = time.monotonic() + .3
            pending = listener.next()
            if pending is None:
                stop_event.wait(min(.04, max(0, deadline - time.monotonic())))
                continue
            service_type, name, generation = pending
            key = service_type, name.casefold()
            info = zc.get_service_info(service_type, name,
                                       timeout=max(1, min(180, (deadline - time.monotonic()) * 1000)))
            if info:
                listener.store(key, generation,
                               _records(info, service_type, name, network, key, generation))
        if stop_event.is_set():
            return []
        check_network()
        records = listener.snapshot()
        # Merge by stable advertised identity or current host; RAOP/AirPlay may
        # advertise different ports but represent one physical receiver.
        groups = []
        for record in records:
            matches = [group for group in groups if any(
                item.speaker.identifier == record.speaker.identifier
                or item.speaker.host == record.speaker.host for item in group)]
            if matches:
                merged = [record]
                for group in matches:
                    groups.remove(group)
                    merged.extend(group)
                groups.append(merged)
            else:
                groups.append([record])
        eligible = [group for group in groups if any(r.capable for r in group)]
        candidates = {record for group in eligible for record in group}
        results = []
        # Hard probe budget, plus bounded 200 ms socket shutdown, keeps a normal
        # six-second scan cancellable and well below eight seconds.
        probe_deadline = time.monotonic() + .4
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = {pool.submit(tcp_check, r.speaker.host, r.speaker.port,
                                   network.source_for(r.speaker.host), .2): r
                       for r in sorted(candidates, key=lambda r: (r.speaker.host, r.speaker.port))[:64]}
            reachable = set()
            while futures and time.monotonic() < probe_deadline and not stop_event.is_set():
                done, _ = wait(futures, timeout=.04, return_when=FIRST_COMPLETED)
                for future in done:
                    record = futures.pop(future)
                    try:
                        if future.result() and listener.present(record):
                            reachable.add(record)
                    except OSError:
                        pass
            for future in futures:
                future.cancel()
            for group in eligible:
                active = [r for r in group if listener.present(r)]
                if not any(r.capable for r in active):
                    continue
                choices = [r for r in active if r in reachable]
                if choices:
                    selected = min(choices, key=lambda r: (not r.raop, not r.capable, r.speaker.host))
                    # Prefer the richest metadata without replacing the selected endpoint's real port.
                    details = max(active, key=lambda r: (bool(r.speaker.model), r.capable)).speaker
                    identifier = max(active, key=lambda r: r.speaker.identifier.startswith("mac:")).speaker.identifier
                    item = selected.speaker
                    results.append(Speaker(identifier, details.name, item.host, item.port, details.model))
        if stop_event.is_set():
            return []
        check_network()
        return sorted(results, key=lambda s: (s.name.casefold(), s.identifier))
    finally:
        if browser is not None:
            browser.cancel()
        zc.close()
