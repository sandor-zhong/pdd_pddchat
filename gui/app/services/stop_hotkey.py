"""Windows global stop key, dispatched on the GUI thread without keyboard hooks."""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

from PyQt5.QtCore import QAbstractNativeEventFilter, QTimer

SHORTCUT = "Ctrl+Alt+F10"
HOTKEY_ID = 0x5044
WM_HOTKEY = 0x0312
MODIFIERS = 0x4000 | 0x0002 | 0x0001  # NOREPEAT | CONTROL | ALT
VK_F10 = 0x79


class StopHotkey(QAbstractNativeEventFilter):
    def __init__(self, callback):
        super().__init__()
        self.callback = callback
        self.window = None
        self.app = None
        self.user = None
        self.registered = False
        self.error = ""

    def register(self, window, app):
        if self.registered:
            return True
        if sys.platform != "win32":
            self.error = "全局停止快捷键需要Windows。"
            return False
        try:
            self.user = ctypes.WinDLL("user32", use_last_error=True)
            self.user.RegisterHotKey.argtypes = [
                wintypes.HWND,
                ctypes.c_int,
                wintypes.UINT,
                wintypes.UINT,
            ]
            self.user.RegisterHotKey.restype = wintypes.BOOL
            self.user.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
            self.user.UnregisterHotKey.restype = wintypes.BOOL
            if not self.user.RegisterHotKey(window, HOTKEY_ID, MODIFIERS, VK_F10):
                self.error = f"{SHORTCUT} 注册失败（Windows错误={ctypes.get_last_error()}），可能已被其他程序占用；仍可使用界面的停止按钮。"
                return False
            self.window, self.app = int(window), app
            self.registered = True
            app.installNativeEventFilter(self)
            app.aboutToQuit.connect(self.close)
            return True
        except OSError as error:
            self.error = "停止快捷键未启用：" + str(error)
            return False

    def nativeEventFilter(self, event_type, message):
        if self.registered and bytes(event_type) in (
            b"windows_generic_MSG",
            b"windows_dispatcher_MSG",
        ):
            msg = wintypes.MSG.from_address(int(message))
            if (
                msg.message == WM_HOTKEY
                and msg.wParam == HOTKEY_ID
                and msg.hWnd == self.window
            ):
                # Do not run cancellation/pipe writes inside the Windows message callback.
                QTimer.singleShot(0, self.callback)
                return True, 0
        return False, 0

    def close(self):
        if not self.registered:
            return
        self.registered = False
        self.app.removeNativeEventFilter(self)
        self.user.UnregisterHotKey(self.window, HOTKEY_ID)
