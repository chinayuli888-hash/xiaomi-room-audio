"""Minimal Core Audio bindings for Windows render endpoints and master volume."""
import ctypes as C
from ctypes import wintypes as W
from dataclasses import dataclass
from uuid import UUID

P = C.c_void_p
HRESULT = C.c_long
ole = C.WinDLL("ole32")
kernel = C.WinDLL("kernel32", use_last_error=True)


class Guid(C.Structure):
    _fields_ = [("data", C.c_ubyte * 16)]

    def __init__(self, value):
        super().__init__((C.c_ubyte * 16).from_buffer_copy(UUID(value).bytes_le))


class PropertyKey(C.Structure):
    _fields_ = [("fmtid", Guid), ("pid", W.DWORD)]


class PropVariant(C.Structure):
    _fields_ = [("vt", C.c_ushort), ("reserved", C.c_ushort * 3),
                ("pointer", P), ("padding", P)]


ENUM_CLSID = Guid("bcde0395-e52f-467c-8e3d-c4579291692e")
ENUM_IID = Guid("a95664d2-9614-4f35-a746-de8db63617e6")
VOLUME_IID = Guid("5cdf2c82-841e-4546-9722-0cf74078229a")
VOLUME_CALLBACK_IID = Guid("657804fa-d6ad-4496-8a60-352752af4f89")
IUNKNOWN_IID = Guid("00000000-0000-0000-c000-000000000046")
POLICY_CLSID = Guid("870af99c-171d-4f9e-af0d-e63df40c2bc9")
POLICY_IID = Guid("f8679f50-850a-41cf-9c72-430f290290c8")
FRIENDLY_NAME = PropertyKey(Guid("a45c254e-df1c-4efd-8020-67d146a850e0"), 14)
ACTIVE = 1
RENDER = 0
MEDIA_ROLES = (0, 1)  # Console and Multimedia. The Communications role is distinct.

ole.CoInitializeEx.argtypes = [P, W.DWORD]
ole.CoInitializeEx.restype = HRESULT
ole.CoCreateInstance.argtypes = [C.POINTER(Guid), P, W.DWORD, C.POINTER(Guid), C.POINTER(P)]
ole.CoCreateInstance.restype = HRESULT
ole.CoTaskMemFree.argtypes = [P]
ole.CoTaskMemFree.restype = None
ole.PropVariantClear.argtypes = [C.POINTER(PropVariant)]
ole.PropVariantClear.restype = HRESULT
kernel.CreateMutexW.argtypes = [P, W.BOOL, W.LPCWSTR]
kernel.CreateMutexW.restype = W.HANDLE
kernel.CloseHandle.argtypes = [W.HANDLE]
kernel.CloseHandle.restype = W.BOOL


def check(status, action):
    if status < 0:
        raise OSError(f"{action} (HRESULT 0x{status & 0xffffffff:08X})")


class Com:
    def __init__(self):
        self.ptr = P()

    def call(self, slot, result, types, *args):
        vtable = C.cast(self.ptr, C.POINTER(C.POINTER(P)))[0]
        return C.WINFUNCTYPE(result, P, *types)(vtable[slot])(self.ptr, *args)

    def invoke(self, slot, action, types, *args):
        check(self.call(slot, HRESULT, types, *args), action)

    def create(self, clsid, iid, action):
        check(ole.CoCreateInstance(C.byref(clsid), None, 1, C.byref(iid),
                                   C.byref(self.ptr)), action)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        if self.ptr:
            self.call(2, W.ULONG, ())
            self.ptr = P()


@dataclass(frozen=True)
class Endpoint:
    id: str
    name: str


@dataclass(frozen=True)
class Level:
    scalar: float
    muted: bool

    @property
    def percent(self):
        return round(self.scalar * 100)


class VolumeCallback:
    """Implement IAudioEndpointVolumeCallback and keep its native vtable alive."""
    def __init__(self, changed):
        self.changed = changed
        self.references = 1
        self.methods = (
            C.WINFUNCTYPE(HRESULT, P, C.POINTER(Guid), C.POINTER(P))(self.query_interface),
            C.WINFUNCTYPE(W.ULONG, P)(self.add_ref),
            C.WINFUNCTYPE(W.ULONG, P)(self.release),
            C.WINFUNCTYPE(HRESULT, P, P)(self.notify),
        )
        self.vtable = (P * len(self.methods))(*(C.cast(method, P) for method in self.methods))
        self.instance = C.pointer(C.cast(self.vtable, C.POINTER(P)))
        self.ptr = C.cast(self.instance, P)

    def query_interface(self, this, iid, out):
        if not out or not iid:
            return C.c_long(0x80004003).value  # E_POINTER
        out[0] = None
        value = bytes(iid.contents)
        if value not in (bytes(IUNKNOWN_IID), bytes(VOLUME_CALLBACK_IID)):
            return C.c_long(0x80004002).value  # E_NOINTERFACE
        self.add_ref(this)
        out[0] = this
        return 0

    def add_ref(self, _):
        self.references += 1
        return self.references

    def release(self, _):
        self.references -= 1
        return self.references

    def notify(self, _, data):
        # The callback must not block or call back into Core Audio.
        changed = self.changed
        if data and changed is not None:
            changed()
        return 0


# If unregister fails, Core Audio can still hold the native callback pointer.
_retained_callbacks = []


class EndpointVolume(Com):
    def __init__(self):
        super().__init__()
        self.callback = None

    def read(self):
        scalar, muted = C.c_float(), W.BOOL()
        self.invoke(9, "读取设备主音量", (C.POINTER(C.c_float),), C.byref(scalar))
        self.invoke(15, "读取静音状态", (C.POINTER(W.BOOL),), C.byref(muted))
        return Level(scalar.value, bool(muted.value))

    def set_scalar(self, scalar):
        self.invoke(7, "调整系统音量", (C.c_float, P), scalar, None)

    def set_muted(self, muted):
        self.invoke(14, "调整系统静音状态", (W.BOOL, P), muted, None)

    def subscribe(self, changed):
        if self.callback is not None:
            raise RuntimeError("Endpoint volume already has an observer")
        callback = VolumeCallback(changed)
        self.invoke(3, "订阅系统音量通知", (P,), callback.ptr)
        self.callback = callback

    def __exit__(self, *error):
        try:
            if self.callback is not None:
                self.callback.changed = None
                try:
                    self.invoke(4, "取消订阅系统音量通知", (P,), self.callback.ptr)
                except OSError:
                    _retained_callbacks.append(self.callback)
                    raise
                finally:
                    self.callback = None
        finally:
            super().__exit__(*error)


class WindowsAudio:
    def __enter__(self):
        # The sender owns this apartment. Notifications don't need a GUI message pump.
        check(ole.CoInitializeEx(None, 0), "初始化 Windows 音频")
        self.enumerator = Com()
        try:
            self.enumerator.create(ENUM_CLSID, ENUM_IID, "打开 Windows 音频设备")
        except Exception:
            ole.CoUninitialize()
            raise
        return self

    def __exit__(self, *error):
        try:
            self.enumerator.__exit__(*error)
        finally:
            ole.CoUninitialize()

    @staticmethod
    def _id(dev):
        out = P()
        dev.invoke(5, "读取音频设备 ID", (C.POINTER(P),), C.byref(out))
        try:
            return C.wstring_at(out)
        finally:
            ole.CoTaskMemFree(out)

    @staticmethod
    def _name(dev):
        with Com() as store:
            dev.invoke(4, "读取音频设备属性", (W.DWORD, C.POINTER(P)),
                       0, C.byref(store.ptr))
            v = PropVariant()
            store.invoke(5, "读取音频设备名称", (C.POINTER(PropertyKey), C.POINTER(PropVariant)),
                         C.byref(FRIENDLY_NAME), C.byref(v))
            try:
                return C.wstring_at(v.pointer) if v.vt == 31 and v.pointer else "音频输出"
            finally:
                ole.PropVariantClear(C.byref(v))

    def render_devices(self):
        with Com() as devices:
            self.enumerator.invoke(3, "枚举播放设备", (C.c_int, W.DWORD, C.POINTER(P)),
                                   RENDER, ACTIVE, C.byref(devices.ptr))
            count = W.UINT()
            devices.invoke(3, "读取播放设备数量", (C.POINTER(W.UINT),), C.byref(count))
            result = []
            for i in range(count.value):
                with Com() as dev:
                    devices.invoke(4, "选择播放设备", (W.UINT, C.POINTER(P)), i,
                                   C.byref(dev.ptr))
                    result.append(Endpoint(self._id(dev), self._name(dev)))
            return result

    def default(self, role=0, flow=RENDER):
        with Com() as dev:
            self.enumerator.invoke(4, "读取系统默认音频设备",
                                   (C.c_int, C.c_int, C.POINTER(P)), flow, role,
                                   C.byref(dev.ptr))
            return Endpoint(self._id(dev), self._name(dev))

    def set_default_render(self, endpoint_id, role):
        # Windows 10/11 PolicyConfig switches just the specified playback role.
        with Com() as policy:
            policy.create(POLICY_CLSID, POLICY_IID, "打开 Windows 播放设备策略")
            policy.invoke(13, "设置默认播放设备", (W.LPCWSTR, C.c_int), endpoint_id, role)

    def _activate_volume(self, endpoint_id, volume):
        with Com() as dev:
            self.enumerator.invoke(5, "打开音量设备", (W.LPCWSTR, C.POINTER(P)),
                                   endpoint_id, C.byref(dev.ptr))
            dev.invoke(3, "打开设备主音量", (C.POINTER(Guid), W.DWORD, P, C.POINTER(P)),
                       C.byref(VOLUME_IID), 23, None, C.byref(volume.ptr))

    def volume(self, endpoint_id):
        volume = EndpointVolume()
        self._activate_volume(endpoint_id, volume)
        return volume

    def level(self, endpoint_id):
        with self.volume(endpoint_id) as volume:
            return volume.read()

    def set_level(self, endpoint_id, level):
        with self.volume(endpoint_id) as volume:
            if level.muted:
                volume.set_muted(True)
            volume.set_scalar(level.scalar)
            if not level.muted:
                volume.set_muted(False)


class AlreadyRunning(RuntimeError):
    pass


class SingleInstance:
    def __enter__(self):
        C.set_last_error(0)
        self.handle = kernel.CreateMutexW(None, False, r"Local\RoomAudio.Windows.MediaRoute")
        code = C.get_last_error()
        if not self.handle:
            raise C.WinError(code)
        if code == 183:
            kernel.CloseHandle(self.handle)
            raise AlreadyRunning("小米全屋播放已经运行。请在任务栏或托盘中打开。")
        return self

    def __exit__(self, *_):
        kernel.CloseHandle(self.handle)
