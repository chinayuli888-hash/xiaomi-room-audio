"""Native Windows notification-area icon for the Tk window."""
from collections import deque
import ctypes as C
from ctypes import wintypes as W
import logging

APP_NAME = "小米全屋播放"
WINDOW_CLASS = "RoomAudio.NotificationArea"
SHOW_MESSAGE = "RoomAudio.NotificationArea.Show"
WM_TRAY = 0x8001
WM_CONTEXTMENU = 0x007B
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
NIN_SELECT = 0x0400
NIN_KEYSELECT = 0x0401
NIM_ADD, NIM_MODIFY, NIM_DELETE, NIM_SETFOCUS, NIM_SETVERSION = range(5)
MENU_SHOW, MENU_EXIT = 1, 2
log = logging.getLogger(__name__)

user32 = C.WinDLL("user32", use_last_error=True)
shell32 = C.WinDLL("shell32", use_last_error=True)
kernel32 = C.WinDLL("kernel32", use_last_error=True)

HANDLE, UINT = W.HANDLE, W.UINT
WNDPROC = C.WINFUNCTYPE(C.c_ssize_t, W.HWND, UINT, W.WPARAM, W.LPARAM)


class WindowClass(C.Structure):
    _fields_ = [
        ("style", UINT), ("wndProc", WNDPROC), ("clsExtra", C.c_int), ("wndExtra", C.c_int),
        ("instance", HANDLE), ("icon", HANDLE), ("cursor", HANDLE), ("background", HANDLE),
        ("menu", W.LPCWSTR), ("name", W.LPCWSTR),
    ]


class NotifyIconData(C.Structure):
    _fields_ = [
        ("cbSize", W.DWORD), ("hWnd", W.HWND), ("uID", UINT), ("uFlags", UINT),
        ("callback", UINT), ("icon", HANDLE), ("tip", C.c_wchar * 128),
        ("state", W.DWORD), ("stateMask", W.DWORD), ("info", C.c_wchar * 256),
        ("version", UINT), ("infoTitle", C.c_wchar * 64), ("infoFlags", W.DWORD),
        ("guid", W.DWORD * 4), ("balloonIcon", HANDLE),
    ]


def _bind(dll, name, result, *args):
    fn = getattr(dll, name)
    fn.restype, fn.argtypes = result, args
    return fn


module_handle = _bind(kernel32, "GetModuleHandleW", HANDLE, W.LPCWSTR)
register_message = _bind(user32, "RegisterWindowMessageW", UINT, W.LPCWSTR)
register_class = _bind(user32, "RegisterClassW", W.WORD, C.POINTER(WindowClass))
unregister_class = _bind(user32, "UnregisterClassW", W.BOOL, W.LPCWSTR, HANDLE)
create_window = _bind(user32, "CreateWindowExW", W.HWND, W.DWORD, W.LPCWSTR, W.LPCWSTR, W.DWORD,
                      C.c_int, C.c_int, C.c_int, C.c_int, W.HWND, HANDLE, HANDLE, C.c_void_p)
find_window = _bind(user32, "FindWindowW", W.HWND, W.LPCWSTR, W.LPCWSTR)
destroy_window = _bind(user32, "DestroyWindow", W.BOOL, W.HWND)
default_proc = _bind(user32, "DefWindowProcW", C.c_ssize_t, W.HWND, UINT, W.WPARAM, W.LPARAM)
post_message = _bind(user32, "PostMessageW", W.BOOL, W.HWND, UINT, W.WPARAM, W.LPARAM)
load_icon = _bind(user32, "LoadImageW", HANDLE, HANDLE, W.LPCWSTR, UINT, C.c_int, C.c_int, UINT)
destroy_icon = _bind(user32, "DestroyIcon", W.BOOL, HANDLE)
system_metric = _bind(user32, "GetSystemMetrics", C.c_int, C.c_int)
set_foreground = _bind(user32, "SetForegroundWindow", W.BOOL, W.HWND)
cursor_position = _bind(user32, "GetCursorPos", W.BOOL, C.POINTER(W.POINT))
create_menu = _bind(user32, "CreatePopupMenu", HANDLE)
append_menu = _bind(user32, "AppendMenuW", W.BOOL, HANDLE, UINT, C.c_size_t, W.LPCWSTR)
track_menu = _bind(user32, "TrackPopupMenu", W.BOOL, HANDLE, UINT,
                   C.c_int, C.c_int, C.c_int, W.HWND, C.c_void_p)
destroy_menu = _bind(user32, "DestroyMenu", W.BOOL, HANDLE)
notify_icon = _bind(shell32, "Shell_NotifyIconW", W.BOOL, W.DWORD, C.POINTER(NotifyIconData))


def show_existing():
    hwnd = find_window(WINDOW_CLASS, None)
    message = register_message(SHOW_MESSAGE)
    return bool(hwnd and message and post_message(hwnd, message, 0, 0))


def _checked(value):
    if not value:
        raise C.WinError(C.get_last_error())
    return value


class TrayIcon:
    def __init__(self, show, exit_app, icon_path):
        self.show, self.exit_app = show, exit_app
        self.pending = deque()
        self.instance = _checked(module_handle(None))
        self.taskbar_created = _checked(register_message("TaskbarCreated"))
        self.show_message = _checked(register_message(SHOW_MESSAGE))
        self.proc = WNDPROC(self.wnd_proc)
        self.hwnd = self.image = None
        self.registered = self.visible = self.version4 = False
        klass = WindowClass(wndProc=self.proc, instance=self.instance, name=WINDOW_CLASS)
        try:
            _checked(register_class(C.byref(klass)))
            self.registered = True
            # An invisible top-level window receives Explorer's restart broadcast.
            self.hwnd = _checked(create_window(0, WINDOW_CLASS, APP_NAME, 0,
                                               0, 0, 0, 0, None, None, self.instance, None))
            self.image = _checked(load_icon(None, str(icon_path), 1,
                                           system_metric(49), system_metric(50), 0x10))
            self.data = NotifyIconData(cbSize=C.sizeof(NotifyIconData), hWnd=self.hwnd,
                                       uID=1, uFlags=0x01 | 0x02 | 0x04 | 0x80,
                                       callback=WM_TRAY, icon=self.image, tip=APP_NAME, version=4)
            self.ensure_icon()
        except OSError:
            self.close()
            raise

    def ensure_icon(self):
        if self.hwnd and not self.visible:
            data = C.byref(self.data)
            self.visible = bool(notify_icon(NIM_ADD, data) or notify_icon(NIM_MODIFY, data))
            if self.visible:
                self.version4 = bool(notify_icon(NIM_SETVERSION, data))
        return self.visible

    def wnd_proc(self, hwnd, message, wparam, lparam):
        try:
            # Tk may be dispatching this native message with its Tcl lock held.
            # Queue Python callables here and run them later from Tk's timer.
            if message == self.taskbar_created:
                self.visible = False
                self.pending.append(self.ensure_icon)
                return 0
            if message == self.show_message:
                self.pending.append(self.show)
                return 0
            if message == WM_TRAY:
                event = lparam & 0xffff
                if event in (NIN_SELECT, NIN_KEYSELECT, WM_LBUTTONUP, WM_LBUTTONDBLCLK):
                    self.pending.append(self.show)
                elif event == WM_CONTEXTMENU or (event == WM_RBUTTONUP and not self.version4):
                    self.pending.append(self.open_menu)
                return 0
        except Exception:
            log.exception("Tray notification failed")
        return default_proc(hwnd, message, wparam, lparam)

    def dispatch(self):
        if self.pending:
            self.pending.popleft()()

    def open_menu(self):
        if not self.hwnd:
            return
        point = W.POINT()
        if not cursor_position(C.byref(point)):
            return
        menu = _checked(create_menu())
        try:
            _checked(append_menu(menu, 0, MENU_SHOW, "打开" + APP_NAME))
            _checked(append_menu(menu, 0x0800, 0, None))
            _checked(append_menu(menu, 0, MENU_EXIT, "退出"))
            set_foreground(self.hwnd)
            choice = track_menu(menu, 0x0100 | 0x0080 | 0x0002, point.x, point.y, 0, self.hwnd, None)
            post_message(self.hwnd, 0, 0, 0)
        finally:
            destroy_menu(menu)
        if choice == MENU_SHOW:
            self.show()
        elif choice == MENU_EXIT:
            self.exit_app()
        elif self.visible:
            notify_icon(NIM_SETFOCUS, C.byref(self.data))

    def close(self):
        if self.visible:
            notify_icon(NIM_DELETE, C.byref(self.data))
            self.visible = False
        if self.hwnd:
            destroy_window(self.hwnd)
            self.hwnd = None
        if self.image:
            destroy_icon(self.image)
            self.image = None
        if self.registered:
            unregister_class(WINDOW_CLASS, self.instance)
            self.registered = False
        self.pending.clear()
