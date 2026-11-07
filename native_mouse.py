"""Route synchronous Playwright pointer actions through the Windows cursor.

Only contexts explicitly enabled with install_system_mouse are affected. DOM
inspection stays in Playwright; no browser mouse input is used as a fallback.
"""

from __future__ import annotations

import ctypes
import json
import os
import random
import sys
import threading
import time
import traceback
import uuid
from contextlib import contextmanager
from ctypes import wintypes
from functools import wraps
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import Locator, Mouse, Page, expect

from order_failures import (
    CursorPositionError,
    DesktopUnavailableError,
    ForegroundUnavailableError,
    InputInterferenceError,
    MouseTargetChangedError,
)

_installed = False
_pages = {}
_driver = None
_input_wait_stop = None
_input_wait_report = None


def pointer_diagnostic(stage, **details):
    """Bounded metadata only; never print DOM text, input values or URL queries."""
    try:
        payload = {
            "time": time.time(),
            "pid": os.getpid(),
            "shop": os.environ.get("PDDCHAT_SHOP", ""),
            "stage": stage,
            **details,
        }
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        if len(raw) > 10000:
            raw = json.dumps(
                {
                    "pid": os.getpid(),
                    "stage": stage,
                    "diagnostic_error": "metadata exceeded size limit",
                }
            )
        print("[鼠标诊断] " + raw, file=sys.stderr, flush=True)
    except Exception:  # noqa: BLE001, S110 - diagnostics cannot break order handling
        pass


def pointer_stack(frames):
    return [
        f"{Path(frame.filename).name}:{frame.lineno}:{frame.name}"
        for frame in frames[-12:]
    ]


def pointer_page_path(page):
    try:
        return urlsplit(page.url).path[:200] if isinstance(page.url, str) else "unknown"
    except Exception:  # noqa: BLE001 - optional diagnostic data only
        return "unknown"


def pointer_error_diagnostic(error, order_no):
    if "鼠标目标位于浏览器可视区域之外" in str(error):
        pointer_diagnostic(
            "analysis_error",
            order_no=order_no,
            error_type=type(error).__name__,
            error=str(error)[:800],
            stack=pointer_stack(traceback.extract_tb(error.__traceback__)),
        )


def prepare_system_mouse_page(page):
    """Settle native activation/maximization before callers measure DOM geometry."""
    if _enabled(page) and _driver is not None:
        with _driver.locked():
            _driver.window(page)


def configure_input_wait(stop_event=None, report=None):
    """Serve workers retain their current order while the desktop is unavailable."""
    global _input_wait_stop, _input_wait_report
    _input_wait_stop, _input_wait_report = stop_event, report


class _MouseInput(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class _InputUnion(ctypes.Union):
    _fields_ = [("mi", _MouseInput)]  # noqa: RUF012 - ctypes native layout declaration


class _Input(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("value", _InputUnion)]


class WindowsMouse:
    def __init__(self):
        if sys.platform != "win32":
            raise RuntimeError("系统鼠标模式需要 Windows 桌面。")
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        # Use physical coordinates on mixed-DPI desktops, including worker threads.
        self.user.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        self.user.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateMutexW.argtypes = [
            ctypes.c_void_p,
            wintypes.BOOL,
            wintypes.LPCWSTR,
        ]
        self.kernel.CreateMutexW.restype = wintypes.HANDLE
        self.kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self.kernel.ReleaseMutex.argtypes = [wintypes.HANDLE]
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.GetCurrentThreadId.restype = wintypes.DWORD
        self.user.GetForegroundWindow.restype = wintypes.HWND
        self.user.OpenInputDesktop.argtypes = [
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        ]
        self.user.OpenInputDesktop.restype = wintypes.HANDLE
        self.user.CloseDesktop.argtypes = [wintypes.HANDLE]
        self.user.GetThreadDesktop.argtypes = [wintypes.DWORD]
        self.user.GetThreadDesktop.restype = wintypes.HANDLE
        self.user.GetUserObjectInformationW.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        self.kernel.GetCurrentProcessId.restype = wintypes.DWORD
        self.kernel.ProcessIdToSessionId.argtypes = [
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        self.wts = ctypes.WinDLL("wtsapi32", use_last_error=True)
        self.wts.WTSQuerySessionInformationW.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(wintypes.DWORD),
        ]
        self.wts.WTSFreeMemory.argtypes = [ctypes.c_void_p]
        self.user.GetWindowRect.argtypes = [
            wintypes.HWND,
            ctypes.POINTER(wintypes.RECT),
        ]
        self.user.SetWindowPos.argtypes = [
            wintypes.HWND,
            wintypes.HWND,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.UINT,
        ]
        self.user.WindowFromPoint.argtypes = [wintypes.POINT]
        self.user.WindowFromPoint.restype = wintypes.HWND
        self.user.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        self.user.GetAncestor.restype = wintypes.HWND
        self.user.SendMessageTimeoutW.argtypes = [
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
            wintypes.UINT,
            wintypes.UINT,
            ctypes.POINTER(ctypes.c_size_t),
        ]
        self.user.SendMessageTimeoutW.restype = ctypes.c_ssize_t
        self.get_window_style = (
            self.user.GetWindowLongPtrW
            if ctypes.sizeof(ctypes.c_void_p) == 8
            else self.user.GetWindowLongW
        )
        self.get_window_style.argtypes = [wintypes.HWND, ctypes.c_int]
        self.get_window_style.restype = ctypes.c_ssize_t
        self.user.IsWindow.argtypes = [wintypes.HWND]
        self.user.GetClassNameW.argtypes = [
            wintypes.HWND,
            wintypes.LPWSTR,
            ctypes.c_int,
        ]
        self.user.IsWindowVisible.argtypes = [wintypes.HWND]
        self.user.GetWindowTextW.argtypes = [
            wintypes.HWND,
            wintypes.LPWSTR,
            ctypes.c_int,
        ]
        self.user.GetClientRect.argtypes = [
            wintypes.HWND,
            ctypes.POINTER(wintypes.RECT),
        ]
        self.user.ClientToScreen.argtypes = [
            wintypes.HWND,
            ctypes.POINTER(wintypes.POINT),
        ]
        self.user.SetForegroundWindow.argtypes = [wintypes.HWND]
        self.user.GetWindowThreadProcessId.argtypes = [
            wintypes.HWND,
            ctypes.POINTER(wintypes.DWORD),
        ]
        self.user.GetWindowThreadProcessId.restype = wintypes.DWORD
        self.user.AttachThreadInput.argtypes = [
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.BOOL,
        ]
        self.user.BringWindowToTop.argtypes = [wintypes.HWND]
        self.user.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        self.user.IsZoomed.argtypes = [wintypes.HWND]
        self.user.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
        self.user.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
        self.user.GetClipCursor.argtypes = [ctypes.POINTER(wintypes.RECT)]
        self.user.GetAsyncKeyState.argtypes = [ctypes.c_int]
        self.user.GetAsyncKeyState.restype = ctypes.c_short
        self.user.SendInput.argtypes = [
            wintypes.UINT,
            ctypes.POINTER(_Input),
            ctypes.c_int,
        ]
        self.user.SendInput.restype = wintypes.UINT
        self.user.mouse_event.argtypes = [
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_size_t,
        ]
        self.mutex = self.kernel.CreateMutexW(None, False, "Local\\PddchatSystemMouse")
        if not self.mutex:
            raise ctypes.WinError(ctypes.get_last_error())
        self.drag_page = None
        self.drag_owner = None
        self.buttons = set()
        self.windows = {}
        self.positions = {}

    def foreground(self, hwnd):
        self._wait_for_input_desktop()
        # SW_MAXIMIZE: restoring (SW_RESTORE) would shrink the window on every
        # pointer action, even when Chromium was launched with --start-maximized.
        if not self.user.IsZoomed(hwnd):
            self.user.ShowWindow(hwnd, 3)
        current_thread = self.kernel.GetCurrentThreadId()
        attempts = []
        for attempt in range(4):
            if self.user.GetForegroundWindow() == hwnd:
                return
            if not self.user.IsWindow(hwnd):
                raise RuntimeError("浏览器窗口已关闭，已停止系统鼠标操作。")
            active = self.user.GetForegroundWindow()
            target_thread = self.user.GetWindowThreadProcessId(hwnd, None)
            active_thread = (
                self.user.GetWindowThreadProcessId(active, None) if active else 0
            )
            attached = []
            attachment_results = []
            try:
                # Both the foreground queue and the target browser queue can
                # belong to other threads. Always release successful joins.
                for thread in dict.fromkeys((active_thread, target_thread)):
                    if thread and thread != current_thread:
                        ctypes.set_last_error(0)
                        joined = bool(
                            self.user.AttachThreadInput(current_thread, thread, True)
                        )
                        attachment_results.append(
                            (thread, joined, ctypes.get_last_error())
                        )
                        if joined:
                            attached.append(thread)
                self.user.BringWindowToTop(hwnd)
                ctypes.set_last_error(0)
                activated = bool(self.user.SetForegroundWindow(hwnd))
                activation_error = ctypes.get_last_error()
            finally:
                for thread in reversed(attached):
                    self.user.AttachThreadInput(current_thread, thread, False)
            attempts.append(
                f"{attempt + 1}:激活={int(activated)},错误={activation_error},"
                f"线程连接={attachment_results}"
            )
            # Allow asynchronous window activation to settle, then retry with
            # the *current* foreground thread rather than a stale handle.
            for _ in range(10):
                if self.user.GetForegroundWindow() == hwnd:
                    return
                time.sleep(0.03)
        if self._caption_activate(hwnd):
            if not self.user.IsZoomed(hwnd):
                self.user.ShowWindow(hwnd, 3)
            return
        state = self.desktop_state()
        if not state["interactive"]:
            raise DesktopUnavailableError(
                "[DESKTOP_UNAVAILABLE] "
                + state["description"]
                + "；未点击网页。本次属于输入环境异常，不计入连续业务失败。"
            )
        active = self.user.GetForegroundWindow()
        raise ForegroundUnavailableError(
            "[FOREGROUND_UNAVAILABLE] "
            "浏览器无法切换到前台，已停止系统鼠标操作。"
            f"已重试4次；目标[{self._window_diagnostic(hwnd)}]；"
            f"实际前台[{self._window_diagnostic(active)}]；"
            + "桌面="
            + state["description"]
            + "; 最后尝试="
            + attempts[-1]
            + "。安全标题栏激活未成功；本次属于输入环境异常，不计入连续业务失败。"
        )

    def desktop_state(self):
        session = wintypes.DWORD()
        session_state = None
        if self.kernel.ProcessIdToSessionId(
            self.kernel.GetCurrentProcessId(), ctypes.byref(session)
        ):
            data, length = ctypes.c_void_p(), wintypes.DWORD()
            if self.wts.WTSQuerySessionInformationW(
                None, session.value, 8, ctypes.byref(data), ctypes.byref(length)
            ):
                try:
                    if data.value and length.value >= ctypes.sizeof(ctypes.c_int):
                        session_state = ctypes.cast(
                            data, ctypes.POINTER(ctypes.c_int)
                        ).contents.value
                finally:
                    self.wts.WTSFreeMemory(data)
        # OpenInputDesktop alone also succeeds for a disconnected RDP session;
        # WTS state and UOI_IO distinguish that case without switching desktops.
        if session_state is not None and session_state != 0:
            return {
                "interactive": False,
                "description": f"Windows会话未处于活动状态（Session={session.value},WTS={session_state}），请恢复远程会话",
            }
        handle = self.user.OpenInputDesktop(0, False, 1)  # DESKTOP_READOBJECTS
        if not handle:
            return {
                "interactive": False,
                "description": f"无法访问当前输入桌面（Windows错误={ctypes.get_last_error()}），可能正在锁屏、切换安全桌面或权限不足",
            }
        try:

            def name(desktop):
                value, needed = ctypes.create_unicode_buffer(256), wintypes.DWORD()
                if self.user.GetUserObjectInformationW(
                    desktop, 2, value, ctypes.sizeof(value), ctypes.byref(needed)
                ):
                    return value.value
                return "未知"

            thread = self.user.GetThreadDesktop(self.kernel.GetCurrentThreadId())
            current_name, input_name = name(thread), name(handle)
            receiving, needed = wintypes.BOOL(), wintypes.DWORD()
            known = self.user.GetUserObjectInformationW(
                thread,
                6,
                ctypes.byref(receiving),
                ctypes.sizeof(receiving),
                ctypes.byref(needed),
            )
            interactive = (
                bool(known)
                and bool(receiving.value)
                and current_name != "未知"
                and current_name == input_name
                and input_name.casefold() != "winlogon"
            )
            description = f"Session={session.value},WTS={session_state},线程桌面={current_name},输入桌面={input_name},接收输入={bool(receiving.value) if known else '未知'}"
            if not interactive:
                description += "；请保持运行电脑解锁并恢复可交互远程桌面"
            return {"interactive": interactive, "description": description}
        finally:
            self.user.CloseDesktop(handle)

    def _wait_for_input_desktop(self):
        state = self.desktop_state()
        if not state["interactive"] and _input_wait_stop is not None:
            if getattr(self, "drag_page", None) is not None:
                self.release()
                raise DesktopUnavailableError(
                    "[DRAG_INTERRUPTED] 拖拽时桌面失去输入，已松开鼠标；不继续旧拖拽轨迹。"
                )
            try:
                while not state["interactive"]:
                    if _input_wait_report is not None:
                        _input_wait_report(
                            {
                                "type": "input_wait",
                                "waiting": True,
                                "message": "桌面暂不可用，已暂停并保留当前订单；恢复输入后继续。"
                                + state["description"],
                            }
                        )
                    if _input_wait_stop.wait(1):
                        raise DesktopUnavailableError(
                            "[TASK_STOPPED] 已停止等待桌面恢复，未发送鼠标点击。"
                        )
                    state = self.desktop_state()
                return state
            finally:
                if _input_wait_report is not None:
                    _input_wait_report(
                        {
                            "type": "input_wait",
                            "waiting": False,
                            "message": "已停止等待桌面。"
                            if _input_wait_stop.is_set()
                            else "桌面已恢复，继续当前订单；将重新确认窗口和鼠标目标。",
                        }
                    )
        for _ in range(25):
            if state["interactive"]:
                return state
            time.sleep(0.2)
            state = self.desktop_state()
        raise DesktopUnavailableError(
            "[DESKTOP_UNAVAILABLE] "
            + state["description"]
            + "；等待5秒仍未恢复，未执行鼠标点击。不计入连续业务失败。"
        )

    def _caption_point_valid(self, hwnd, x, y):
        if not (-32768 <= x <= 32767 and -32768 <= y <= 32767):
            return False
        at = self.user.WindowFromPoint(wintypes.POINT(x, y))
        if not at or self.user.GetAncestor(at, 2) != hwnd:
            return False
        result = ctypes.c_size_t()
        packed = (x & 0xFFFF) | ((y & 0xFFFF) << 16)
        ok = self.user.SendMessageTimeoutW(
            hwnd, 0x84, 0, packed, 3, 80, ctypes.byref(result)
        )
        return bool(ok) and result.value == 2  # HTCAPTION, never tab/button/client

    def _caption_activate(self, hwnd):
        if getattr(self, "drag_page", None) is not None or any(
            self.user.GetAsyncKeyState(key) & 0x8000 for key in (1, 2, 4)
        ):
            return False
        if not self.desktop_state()["interactive"]:
            return False
        rect = wintypes.RECT()
        if not self.user.GetWindowRect(hwnd, ctypes.byref(rect)):
            return False
        previous = self._cursor_position()
        was_topmost = bool(self.get_window_style(hwnd, -20) & 0x8)
        # Temporarily expose only the browser; no activation or global settings.
        if not self.user.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x13):
            return False
        clicked = None
        try:
            for dy in (16, 26, 36):
                for fraction in (0.5, 0.7, 0.35, 0.8, 0.2):
                    x, y = (
                        round(rect.left + (rect.right - rect.left) * fraction),
                        rect.top + dy,
                    )
                    if not self._caption_point_valid(hwnd, x, y):
                        continue
                    if not self.desktop_state()["interactive"]:
                        return False
                    self._set_cursor_position(x, y)
                    if not self._caption_point_valid(hwnd, x, y):
                        return False
                    self.assert_cursor_position(x, y)
                    if any(
                        self.user.GetAsyncKeyState(key) & 0x8000 for key in (1, 2, 4)
                    ):
                        return False
                    self.click_pulse("left")
                    clicked = (x, y)
                    for _ in range(15):
                        if self.user.GetForegroundWindow() == hwnd:
                            print(
                                "[系统鼠标] 已通过浏览器空白标题栏恢复前台，未点击网页内容。",
                                flush=True,
                            )
                            return True
                        time.sleep(0.02)
                    return False  # At most one actual click per activation.
            return False
        finally:
            self.user.SetWindowPos(hwnd, -1 if was_topmost else -2, 0, 0, 0, 0, 0x13)
            if clicked and self._cursor_position() == clicked:
                self.user.SetCursorPos(*previous)

    def _window_diagnostic(self, hwnd):
        # Class/PID identify the competing application without logging titles
        # that may contain buyer names, order numbers or private page content.
        if not hwnd:
            return "无窗口"
        pid = wintypes.DWORD()
        self.user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        window_class = ctypes.create_unicode_buffer(256)
        self.user.GetClassNameW(hwnd, window_class, len(window_class))
        return f"HWND={hwnd},PID={pid.value},类={window_class.value}"

    def _acquire_mouse(self):
        if _input_wait_stop is None:
            result = self.kernel.WaitForSingleObject(self.mutex, 30000)
        else:
            # Paused desktop time must not exhaust another shop's mouse lock wait.
            result = 0x102
            for _ in range(120):
                self._wait_for_input_desktop()
                if _input_wait_stop.is_set():
                    raise DesktopUnavailableError("[TASK_STOPPED] 系统鼠标任务已停止。")
                result = self.kernel.WaitForSingleObject(self.mutex, 250)
                if result != 0x102:
                    break
        if result not in (0, 0x80):
            raise RuntimeError("等待系统鼠标超时，请结束其它正在使用鼠标的任务。")

    @contextmanager
    def locked(self):
        self._acquire_mouse()
        try:
            yield
        finally:
            self.kernel.ReleaseMutex(self.mutex)

    def window(self, page):
        page.bring_to_front()
        cached = self.windows.get(page._impl_obj)
        if cached and self.user.IsWindowVisible(cached):
            self.foreground(cached)
            return cached
        token = "pdd-mouse-" + uuid.uuid4().hex
        title = page.title()
        found = []
        callback_type = ctypes.WINFUNCTYPE(
            wintypes.BOOL, wintypes.HWND, wintypes.LPARAM
        )

        @callback_type
        def visit(hwnd, _):
            value = ctypes.create_unicode_buffer(4096)
            self.user.GetWindowTextW(hwnd, value, len(value))
            if self.user.IsWindowVisible(hwnd) and token in value.value:
                found.append(hwnd)
            return True

        self.user.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
        try:
            page.evaluate("title => { document.title = title; }", token)
            for _ in range(20):
                self.user.EnumWindows(visit, 0)
                if found:
                    break
                page.wait_for_timeout(25)
        finally:
            page.evaluate("title => { document.title = title; }", title)
        if len(set(found)) != 1:
            raise RuntimeError("无法唯一识别浏览器窗口，已停止系统鼠标操作。")
        hwnd = found[0]
        self.foreground(hwnd)
        self.windows[page._impl_obj] = hwnd
        return hwnd

    def point(self, page, x, y):
        self.user.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
        if page.viewport_size is not None:
            raise RuntimeError(
                "系统鼠标模式需要 no_viewport=True，以便坐标跟随真实窗口。"
            )
        hwnd = self.window(page)
        metrics = page.evaluate(
            """() => ({w: innerWidth, h: innerHeight, dpr: devicePixelRatio,
                outerW:outerWidth, outerH:outerHeight, screenX,screenY,scrollX,scrollY,
                screenW:screen.width,screenH:screen.height,availW:screen.availWidth,availH:screen.availHeight,
                visual:visualViewport ? {w:visualViewport.width,h:visualViewport.height,scale:visualViewport.scale,x:visualViewport.offsetLeft,y:visualViewport.offsetTop}:null})"""
        )
        geometry = getattr(self, "_diagnostic_geometry", None)
        if geometry is None:
            geometry = self._diagnostic_geometry = {}
        key = id(page._impl_obj)
        signature = tuple(
            metrics.get(name)
            for name in (
                "w",
                "h",
                "dpr",
                "outerW",
                "outerH",
                "screenX",
                "screenY",
                "screenW",
                "screenH",
            )
        )
        previous = geometry.get(key)
        if previous != signature:
            if len(geometry) >= 64:
                geometry.pop(next(iter(geometry)))
            geometry[key] = signature
            pointer_diagnostic(
                "viewport_changed",
                path=pointer_page_path(page),
                before=previous,
                viewport=metrics,
                native=self._pointer_geometry(hwnd),
            )
        if not 0 <= x < metrics["w"] or not 0 <= y < metrics["h"]:
            pointer_diagnostic(
                "target_out_of_view",
                target={"x": x, "y": y},
                viewport=metrics,
                path=pointer_page_path(page),
                native=self._pointer_geometry(hwnd),
                stack=pointer_stack(traceback.extract_stack()[:-1]),
            )
            raise RuntimeError(
                f"鼠标目标位于浏览器可视区域之外。目标CSS=({x:.1f},{y:.1f})，可视CSS=({metrics['w']},{metrics['h']})，DPR={metrics['dpr']}；详情见[鼠标诊断]。"
            )
        rect, origin = wintypes.RECT(), wintypes.POINT(0, 0)
        if not self.user.GetClientRect(
            hwnd, ctypes.byref(rect)
        ) or not self.user.ClientToScreen(hwnd, ctypes.byref(origin)):
            raise ctypes.WinError(ctypes.get_last_error())
        # Chromium/Firefox client areas include their toolbar above the viewport.
        scale = metrics["dpr"]
        return round(origin.x + x * scale), round(
            origin.y + rect.bottom - metrics["h"] * scale + y * scale
        )

    def _pointer_geometry(self, hwnd):
        """Read-only native snapshot; failure to collect a field is nonfatal."""
        result = {}
        try:
            rect, client, origin = wintypes.RECT(), wintypes.RECT(), wintypes.POINT()
            if self.user.GetWindowRect(hwnd, ctypes.byref(rect)):
                result["window_rect"] = [rect.left, rect.top, rect.right, rect.bottom]
            if self.user.GetClientRect(hwnd, ctypes.byref(client)):
                result["client_rect"] = [
                    client.left,
                    client.top,
                    client.right,
                    client.bottom,
                ]
            if self.user.ClientToScreen(hwnd, ctypes.byref(origin)):
                result["client_origin"] = [origin.x, origin.y]
            result["maximized"] = bool(self.user.IsZoomed(hwnd))
            result["window"] = self._window_diagnostic(hwnd)
            result["foreground"] = self._window_diagnostic(
                self.user.GetForegroundWindow()
            )
            result["desktop"] = self.desktop_state()
            result["virtual_screen"] = [
                self.user.GetSystemMetrics(index) for index in (76, 77, 78, 79)
            ]
            clip = wintypes.RECT()
            if self.user.GetClipCursor(ctypes.byref(clip)):
                result["clip"] = [clip.left, clip.top, clip.right, clip.bottom]
            if hasattr(self.user, "GetDpiForWindow"):
                dpi = self.user.GetDpiForWindow
                dpi.argtypes = [wintypes.HWND]
                dpi.restype = wintypes.UINT
                result["window_dpi"] = int(dpi(hwnd))
            result["cursor"] = self._cursor_position()
        except Exception as error:  # noqa: BLE001 - metadata is best effort
            result["diagnostic_error"] = type(error).__name__
        return result

    def move(self, page, x, y, *, steps=1):
        with self.locked():
            self.ensure_idle_buttons()
            if self.drag_page is not None and self.drag_page is not page._impl_obj:
                raise RuntimeError("拖拽过程中不能切换页面。")
            end_x, end_y = self.point(page, x, y)
            start = wintypes.POINT()
            self.user.GetCursorPos(ctypes.byref(start))
            previous = None
            # Ordinary multi-step moves take 0.6–1.0 seconds in total. Explicit
            # drag trajectories and one-step recovery keep their own timing.
            step_pause = (
                random.uniform(0.6, 1.0) / steps
                if steps > 1 and self.drag_page is None
                else 0.008
            )
            for i in range(1, max(1, steps) + 1):
                if previous is not None:
                    self.assert_cursor_position(*previous)
                ratio = i / max(1, steps)
                target = (
                    round(start.x + (end_x - start.x) * ratio),
                    round(start.y + (end_y - start.y) * ratio),
                )
                previous = self._set_cursor_position(*target)
                if steps > 1:
                    time.sleep(step_pause)
            self.positions[page._impl_obj] = (x, y)
            return previous

    def _cursor_position(self):
        cursor = wintypes.POINT()
        if not self.user.GetCursorPos(ctypes.byref(cursor)):
            raise ctypes.WinError(ctypes.get_last_error())
        return cursor.x, cursor.y

    def _cursor_diagnostic(self, x, y, actual):
        bounds = wintypes.RECT()
        clip = "未知"
        if (
            self.user.GetClipCursor(ctypes.byref(bounds))
            and bounds.right > bounds.left
            and bounds.bottom > bounds.top
        ):
            clip = f"({bounds.left},{bounds.top})-({bounds.right},{bounds.bottom})"
        return f"目标=({x},{y})，实际={actual}，鼠标可移动范围={clip}"

    def _set_cursor_position(self, x, y):
        if not self.user.SetCursorPos(x, y):
            error = ctypes.get_last_error()
            raise CursorPositionError(
                f"[CURSOR_POSITIONING] 系统鼠标定位调用失败，目标=({x},{y})，系统错误={error}；未发送点击。"
            )
        actual = self._cursor_position()
        if abs(actual[0] - x) > 3 or abs(actual[1] - y) > 3:
            raise CursorPositionError(
                "[CURSOR_POSITIONING] 系统鼠标未到达指定坐标；"
                + self._cursor_diagnostic(x, y, actual)
                + "。可能受桌面边界、鼠标范围或会话状态影响，不能据此认定人工干预。"
            )
        # Later checks compare against the position actually reached, not a
        # requested coordinate that Windows may have adjusted slightly.
        return actual

    def assert_cursor_position(self, x, y):
        actual = self._cursor_position()
        if abs(actual[0] - x) > 3 or abs(actual[1] - y) > 3:
            raise InputInterferenceError(
                "[CURSOR_DISPLACED] 系统鼠标定位成功后发生位移；"
                + self._cursor_diagnostic(x, y, actual)
                + "。位移来源未确认，当前鼠标位置需要重新确认。"
            )

    def down(self, page, *, button="left", click_count=1):
        flags = {"left": 0x2, "right": 0x8, "middle": 0x20}
        flag = flags[button]
        if self.drag_page is None:
            self._acquire_mouse()
            self.drag_page = page._impl_obj
            self.drag_owner = threading.get_ident()
        try:
            self.window(page)
            position = self.positions.get(page._impl_obj)
            if position is not None:
                self.move(page, *position)
            self.user.mouse_event(flag, 0, 0, 0, 0)
            self.buttons.add(button)
        except BaseException:
            self.release()
            raise

    def release(self):
        if self.drag_owner != threading.get_ident():
            return
        for button in self.buttons:
            self.user.mouse_event(
                {"left": 0x4, "right": 0x10, "middle": 0x40}[button], 0, 0, 0, 0
            )
        self.buttons.clear()
        if self.drag_page is not None:
            self.drag_page = None
            self.drag_owner = None
            self.kernel.ReleaseMutex(self.mutex)

    def ensure_idle_buttons(self):
        """空闲移动前清理系统残留按键；显式 down/move/up 拖动不受影响。"""
        if self.drag_page is not None:
            return
        for key, flag in ((0x01, 0x04), (0x02, 0x10), (0x04, 0x40)):
            if self.user.GetAsyncKeyState(key) & 0x8000:
                self.user.mouse_event(flag, 0, 0, 0, 0)
                print("[系统鼠标] 检测到残留按键，已松开后重新定位。", flush=True)

    def click_pulse(self, button):
        down, up = {
            "left": (0x02, 0x04),
            "right": (0x08, 0x10),
            "middle": (0x20, 0x40),
        }[button]
        events = (_Input * 2)()
        events[0].value.mi.dwFlags = down
        events[1].value.mi.dwFlags = up
        # One system input batch: no movement or sleep between button down/up.
        complete = False
        try:
            complete = self.user.SendInput(2, events, ctypes.sizeof(_Input)) == 2
            if not complete:
                raise RuntimeError("系统鼠标点击未完整发送，已停止操作。")
        finally:
            # SendInput may insert only part of a batch (e.g. desktop changes).
            if (
                not complete
                or self.user.GetAsyncKeyState(
                    {"left": 1, "right": 2, "middle": 4}[button]
                )
                & 0x8000
            ):
                self.user.mouse_event(up, 0, 0, 0, 0)

    def up(self, page, *, button="left", click_count=1):
        self.release()

    def click(
        self, page, x, y, *, button="left", click_count=1, delay=0, _validate=None
    ):
        with self.locked():
            # A click is never a continuation of an unfinished slider drag.
            self.release()
            # Pause before aiming, with all buttons released. Keep double-click
            # pulses and recovery attempts together rather than slowing them.
            time.sleep(random.uniform(1.0, 3.0))
            self.ensure_idle_buttons()
            for i in range(click_count):
                # Retry only pre-click displacement. Never replay a delivered
                # click, resume a drag, or retry a changed/obscured target.
                for attempt in range(3):
                    try:
                        # Recovery uses a direct native move to shorten the
                        # interval during which a remote client can resync.
                        if attempt and delay:
                            time.sleep(delay / 1000)
                        aimed_position = self.move(
                            page, x, y, steps=1 if attempt else 8
                        )
                        if not attempt and delay:
                            time.sleep(delay / 1000)
                        self.assert_cursor_position(*aimed_position)
                        screen_x, screen_y = self.point(page, x, y)
                        if (
                            abs(screen_x - aimed_position[0]) > 3
                            or abs(screen_y - aimed_position[1]) > 3
                        ):
                            raise MouseTargetChangedError(
                                "[MOUSE_TARGET_CHANGED] 点击目标的屏幕坐标发生变化，"
                                f"原定位={aimed_position}，新目标=({screen_x},{screen_y})；已停止点击，不认定人工干预。"
                            )
                        if _validate is not None:
                            _validate()
                        self.ensure_idle_buttons()
                        self.assert_cursor_position(*aimed_position)
                        self._set_cursor_position(screen_x, screen_y)
                    except InputInterferenceError as exc:
                        if type(exc) is not InputInterferenceError or attempt == 2:
                            raise
                        print(
                            f"[系统鼠标] 点击前坐标被改变，准备第{attempt + 1}/2次重新定位；{exc}",
                            flush=True,
                        )
                        time.sleep(0.05)
                        continue
                    # No waits or Playwright calls between the final verified
                    # native positioning and the single down/up input batch.
                    self.click_pulse(button)
                    break
                if i + 1 < click_count:
                    time.sleep(0.06)

    def dblclick(self, page, x, y, **kwargs):
        self.click(page, x, y, click_count=2, **kwargs)

    def wheel(self, page, delta_x, delta_y):
        with self.locked():
            self.window(page)
            size = page.evaluate("() => ({w: innerWidth, h: innerHeight})")
            left, top = self.point(page, 0, 0)
            right, bottom = self.point(page, size["w"] - 1, size["h"] - 1)
            cursor = wintypes.POINT()
            self.user.GetCursorPos(ctypes.byref(cursor))
            if not (left <= cursor.x <= right and top <= cursor.y <= bottom):
                self.move(page, size["w"] / 2, size["h"] / 2)
            if not hasattr(self, "wheel_remainders"):
                self.wheel_remainders = {}
            pending = self.wheel_remainders.setdefault(page._impl_obj, [0.0, 0.0])
            for index, delta, flag, sign in (
                (0, delta_x, 0x1000, 1),
                (1, delta_y, 0x800, -1),
            ):
                total = pending[index] + delta
                units = int(total)
                pending[index] = total - units
                if units:
                    self.user.mouse_event(flag, 0, 0, (units * sign) & 0xFFFFFFFF, 0)


def _enabled(page):
    return getattr(page.context, "_system_mouse_enabled", False) is True


class _TargetLayoutChanged(RuntimeError):
    """Raised only before delivery; the locator can safely be measured again."""


_NATIVE_REVEAL_TARGET = r"""el => {
    const root=document.scrollingElement || document.documentElement;
    const rect=el.getBoundingClientRect();
    const viewport={left:0,top:0,right:Math.min(innerWidth,document.documentElement.clientWidth),bottom:Math.min(innerHeight,document.documentElement.clientHeight)};
    const cx=rect.x+rect.width/2,cy=rect.y+rect.height/2;
    const scrollable=(n,axis)=> n===root || /^(auto|scroll|overlay)$/.test(getComputedStyle(n)[axis==='x'?'overflowX':'overflowY']);
    const owner=(hit,axis)=>{for(let n=hit;n && n!==root && n!==document.body;n=n.parentElement) {
        if(scrollable(n,axis) && (axis==='x'?n.scrollWidth>n.clientWidth+1:n.scrollHeight>n.clientHeight+1)) return n;
    } return root;};
    const parents=[];
    for(let n=el.parentElement;n && n!==root && n!==document.body;n=n.parentElement) parents.push(n);
    parents.push(root);
    for(const n of parents.reverse()) {
        const r=n===root ? viewport : n.getBoundingClientRect();
        const v=n===root ? viewport : {left:Math.max(0,r.left+n.clientLeft),top:Math.max(0,r.top+n.clientTop),right:Math.min(viewport.right,r.left+n.clientLeft+n.clientWidth),bottom:Math.min(viewport.bottom,r.top+n.clientTop+n.clientHeight)};
        if(v.right<=v.left || v.bottom<=v.top) continue;
        for(const axis of ['y','x']) {
            const center=axis==='x'?cx:cy,lo=axis==='x'?v.left:v.top,hi=axis==='x'?v.right:v.bottom;
            if(center>=lo+2 && center<=hi-2) continue;
            if(!scrollable(n,axis)) continue;
            const top=axis==='x'?n.scrollLeft:n.scrollTop;
            const maximum=axis==='x'?n.scrollWidth-n.clientWidth:n.scrollHeight-n.clientHeight;
            const delta=Math.max(0,Math.min(maximum,top+center-(lo+hi)/2))-top;
            if(Math.abs(delta)<0.5) continue;
            for(const fy of [0.5,0.08,0.92,0.25,0.75]) for(const fx of [0.5,0.08,0.92,0.25,0.75]) {
                const x=v.left+(v.right-v.left)*fx,y=v.top+(v.bottom-v.top)*fy;
                const hit=document.elementFromPoint(x,y);
                if(hit && owner(hit,axis)===n) return {ready:false,x,y,axis,delta,box:{x:rect.x,y:rect.y,width:rect.width,height:rect.height},viewport};
            }
            return {ready:false,blocked:true,axis,viewport};
        }
    }
    return {ready:cx>=viewport.left && cx<viewport.right && cy>=viewport.top && cy<viewport.bottom,viewport};
}"""


def _scroll_locator_into_view(locator, timeout):
    """Reveal through native wheel input; DOM is inspected, never scrolled by JS."""
    deadline = time.monotonic() + (timeout if timeout is not None else 8000) / 1000
    for _ in range(40):
        _driver.window(locator.page)
        info = locator.evaluate(_NATIVE_REVEAL_TARGET)
        if info["ready"]:
            return
        if info.get("blocked") or "delta" not in info or time.monotonic() >= deadline:
            raise _TargetLayoutChanged(
                "目标尚未进入可见区域或滚动区域被遮挡，未发送点击。"
            )
        _driver.move(locator.page, info["x"], info["y"], steps=8)
        if locator.evaluate(_NATIVE_REVEAL_TARGET) != info:
            continue
        delta = min(320, max(12, abs(info["delta"]))) * (1 if info["delta"] > 0 else -1)
        _driver.wheel(
            locator.page,
            delta if info["axis"] == "x" else 0,
            delta if info["axis"] == "y" else 0,
        )
        time.sleep(0.15)
    raise _TargetLayoutChanged("目标滚动定位次数已达上限，未发送点击。")


def _locator_action(locator, action, **options):
    for attempt in range(3):
        try:
            return _locator_action_once(locator, action, **options)
        except RuntimeError as error:
            # A delivered click is never replayed. Double clicks may have
            # delivered the first pulse, so their errors must propagate.
            can_retry = action == "click" and options.get("click_count", 1) == 1
            changed = isinstance(
                error, (_TargetLayoutChanged, MouseTargetChangedError)
            ) or "鼠标目标位于浏览器可视区域之外" in str(error)
            if not can_retry or not changed or attempt == 2:
                raise
            pointer_diagnostic(
                "locator_remeasure",
                attempt=attempt + 1,
                path=pointer_page_path(locator.page),
            )
            time.sleep(0.15)


def _locator_action_once(locator, action, **options):
    page = locator.page
    with _driver.locked():
        _driver.release()
        _driver.ensure_idle_buttons()
        # Maximizing changes layout; locate the target only after the window has
        # reached its required state, so the first click uses the new coordinates.
        _driver.window(page)
        timeout = options.pop("timeout", None)
        locator.wait_for(state="visible", timeout=timeout)
        if action != "hover":
            expect(locator).to_be_enabled(timeout=timeout)
        _scroll_locator_into_view(locator, timeout)
        box = locator.bounding_box(timeout=timeout)
        if not box:
            raise RuntimeError("无法获取系统鼠标目标的位置。")
        time.sleep(0.1)
        settled = locator.bounding_box(timeout=timeout)
        if not settled or any(
            abs(settled[key] - box[key]) > 1 for key in ("x", "y", "width", "height")
        ):
            raise _TargetLayoutChanged("目标布局尚未稳定，重新定位后再点击。")
        box = settled
        position = options.pop("position", None) or {
            "x": box["width"] / 2,
            "y": box["height"] / 2,
        }
        x, y = box["x"] + position["x"], box["y"] + position["y"]
        pointer_diagnostic(
            "locator_target",
            action=action,
            box=box,
            position=position,
            target={"x": x, "y": y},
            path=pointer_page_path(page),
            stack=pointer_stack(traceback.extract_stack()[:-1]),
        )
        # Check the actual hit target, including overlays, before moving the OS cursor.
        force = options.pop("force", False)
        if not force and not locator.evaluate(
            "(el, p) => { const r=el.getBoundingClientRect(); const hit=el.ownerDocument.elementFromPoint(r.x+p.x,r.y+p.y); return hit===el || el.contains(hit); }",
            position,
        ):
            _log_obscured_target(locator, position)
            raise _TargetLayoutChanged("目标被其它元素遮挡，已停止系统鼠标点击。")
        if options.pop("trial", False):
            return
        modifiers = options.pop("modifiers", None) or []
        if modifiers:
            raise RuntimeError("系统鼠标暂不支持组合键点击。")
        options.pop("no_wait_after", None)
        if action == "hover":
            _driver.move(page, x, y, steps=8)
        else:

            def validate():
                current = locator.bounding_box(timeout=timeout)
                if (
                    not current
                    or abs(current["x"] + position["x"] - x) > 1
                    or abs(current["y"] + position["y"] - y) > 1
                ):
                    raise _TargetLayoutChanged(
                        "点击前目标位置发生变化，已停止系统鼠标点击。"
                    )
                if not force and not locator.evaluate(
                    "(el, p) => { const r=el.getBoundingClientRect(); const hit=el.ownerDocument.elementFromPoint(r.x+p.x,r.y+p.y); return hit===el || el.contains(hit); }",
                    position,
                ):
                    _log_obscured_target(locator, position)
                    raise _TargetLayoutChanged("点击前目标被遮挡，已停止系统鼠标点击。")

            options["_validate"] = validate
            getattr(_driver, action)(page, x, y, **options)


def _log_obscured_target(locator, position):
    # Geometry and class names only; no buyer data or element text.
    try:
        hit = locator.evaluate(
            """(el,p) => {
            const r=el.getBoundingClientRect(), hit=el.ownerDocument.elementFromPoint(r.x+p.x,r.y+p.y);
            if(!hit) return {hit:null};
            const b=hit.getBoundingClientRect();
            return {tag:hit.tagName,classes:String(hit.className).slice(0,200),box:{x:b.x,y:b.y,width:b.width,height:b.height}};
        }""",
            position,
        )
        pointer_diagnostic(
            "locator_obscured", hit=hit, path=pointer_page_path(locator.page)
        )
    except Exception:  # noqa: BLE001 - diagnostics must not change click safety
        return


def install_system_mouse(context):
    """Enable native pointer input for current and future pages of a context."""
    global _driver, _installed
    if _driver is None:
        _driver = WindowsMouse()
    if not _installed:
        for name in ("move", "down", "up", "click", "dblclick", "wheel"):
            original = getattr(Mouse, name)

            def make_mouse(action, fallback):
                @wraps(fallback)
                def wrapped(mouse, *args, **kwargs):
                    page = _pages.get(mouse._impl_obj)
                    if page is None:
                        return fallback(mouse, *args, **kwargs)
                    try:
                        return getattr(_driver, action)(page, *args, **kwargs)
                    except BaseException:
                        _driver.release()
                        raise

                return wrapped

            setattr(Mouse, name, make_mouse(name, original))
        for name in ("click", "dblclick", "hover"):
            original = getattr(Locator, name)

            def make_locator(action, fallback):
                @wraps(fallback)
                def wrapped(locator, **kwargs):
                    if not _enabled(locator.page):
                        return fallback(locator, **kwargs)
                    return _locator_action(locator, action, **kwargs)

                return wrapped

            setattr(Locator, name, make_locator(name, original))
            original_page = getattr(Page, name)

            def make_page(action, fallback):
                @wraps(fallback)
                def wrapped(page, selector, **kwargs):
                    if not _enabled(page):
                        return fallback(page, selector, **kwargs)
                    kwargs.pop("strict", None)
                    return _locator_action(page.locator(selector), action, **kwargs)

                return wrapped

            setattr(Page, name, make_page(name, original_page))
        _installed = True
    context._system_mouse_enabled = True

    def attach(page):
        _pages[page.mouse._impl_obj] = page

        def detach(*_):
            _pages.pop(page.mouse._impl_obj, None)
            _driver.windows.pop(page._impl_obj, None)
            _driver.positions.pop(page._impl_obj, None)
            getattr(_driver, "wheel_remainders", {}).pop(page._impl_obj, None)
            if _driver.drag_page is page._impl_obj:
                _driver.release()

        page.on("close", detach)
        page.on("crash", detach)

    for page in context.pages:
        attach(page)
        if isinstance(page, Page):
            with _driver.locked():
                _driver.window(page)
    context.on("page", attach)
