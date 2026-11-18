"""Fresh network-scoped discovery state, without playback or GUI side effects."""


class DeviceCatalog:
    def __init__(self):
        self.generation = 0
        self.network = None
        self.devices = ()
        self.ready = False

    def begin(self, network):
        self.generation += 1
        self.network = network
        self.devices = ()
        self.ready = False
        return self.generation

    def invalidate(self, network=None):
        return self.begin(network)

    def apply(self, generation, network, devices):
        if generation != self.generation or self.network is None or network is None:
            return False
        if network.fingerprint != self.network.fingerprint:
            return False
        devices = tuple(devices)
        hosts = [speaker.host for speaker in devices]
        if len(devices) > 8 or len(set(hosts)) != len(hosts):
            self.devices, self.ready = (), False
            raise ValueError("发现的音箱超过 8 台或地址重复，请检查网络设备。")
        self.devices, self.ready = devices, bool(devices)
        return True

    def fail(self, generation):
        if generation != self.generation:
            return False
        self.devices, self.ready = (), False
        return True

    def can_play(self, network):
        return bool(self.ready and self.devices and network is not None and self.network is not None
                    and self.network.fingerprint == network.fingerprint)
