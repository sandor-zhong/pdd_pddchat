"""No real key registration, key presses, browser actions or submissions."""

import ctypes
import unittest
from ctypes import wintypes
from unittest.mock import MagicMock, patch

from gui.app.pages.run_page import RunPage
from gui.app.services import stop_hotkey


class StopHotkeyTests(unittest.TestCase):
    def register(self, success=True):
        callback, user, app = MagicMock(), MagicMock(), MagicMock()
        user.RegisterHotKey.return_value = success
        key = stop_hotkey.StopHotkey(callback)
        with (
            patch.object(stop_hotkey.sys, "platform", "win32"),
            patch.object(ctypes, "WinDLL", return_value=user),
        ):
            result = key.register(123, app)
        return key, callback, user, app, result

    def test_registers_global_nonrepeating_combination_and_releases_once(self):
        key, _, user, app, result = self.register()
        self.assertTrue(result)
        user.RegisterHotKey.assert_called_once_with(
            123, stop_hotkey.HOTKEY_ID, 0x4003, 0x79
        )
        app.installNativeEventFilter.assert_called_once_with(key)
        self.assertTrue(key.register(123, app))
        self.assertEqual(user.RegisterHotKey.call_count, 1)
        key.close()
        key.close()
        app.removeNativeEventFilter.assert_called_once_with(key)
        user.UnregisterHotKey.assert_called_once_with(123, stop_hotkey.HOTKEY_ID)

    def test_conflicting_hotkey_does_not_claim_registration(self):
        key, _, user, app, result = self.register(False)
        self.assertFalse(result)
        self.assertFalse(key.registered)
        self.assertIn("可能已被其他程序占用", key.error)
        app.installNativeEventFilter.assert_not_called()
        key.close()
        user.UnregisterHotKey.assert_not_called()

    def test_only_our_windows_hotkey_message_dispatches(self):
        key, callback, _, _, _ = self.register()
        msg = wintypes.MSG()
        msg.hWnd = 123
        msg.message = stop_hotkey.WM_HOTKEY
        msg.wParam = stop_hotkey.HOTKEY_ID
        with patch.object(stop_hotkey.QTimer, "singleShot") as dispatch:
            self.assertEqual(
                key.nativeEventFilter(b"windows_generic_MSG", ctypes.addressof(msg)),
                (True, 0),
            )
            dispatch.assert_called_once_with(0, callback)
            callback.assert_not_called()
            msg.hWnd = 456
            self.assertEqual(
                key.nativeEventFilter(b"windows_generic_MSG", ctypes.addressof(msg)),
                (False, 0),
            )
            msg.hWnd = 123
            msg.wParam = 999
            self.assertEqual(
                key.nativeEventFilter(b"windows_dispatcher_MSG", ctypes.addressof(msg)),
                (False, 0),
            )
            self.assertEqual(key.nativeEventFilter(b"other", 0), (False, 0))
            self.assertEqual(dispatch.call_count, 1)
        key.close()

    def test_stop_routes_through_existing_safe_cancellation(self):
        page = MagicMock()
        page.is_running.return_value = True
        self.assertTrue(RunPage.request_current_stop(page, "hotkey"))
        self.assertTrue(page._pipeline_cancelled)
        page._request_stop.assert_called_once_with(reason="hotkey")
        page._request_reconsider_stop.assert_called_once_with(reason="hotkey")
        page._request_aftersale_stop.assert_called_once_with(reason="hotkey")
        page._check_results_worker.cancel.assert_called_once()
        page._check_reconsider_results_worker.cancel.assert_called_once()
        page._check_aftersale_results_worker.cancel.assert_called_once()

    def test_idle_stop_is_a_noop(self):
        page = MagicMock()
        page.is_running.return_value = False
        self.assertFalse(RunPage.request_current_stop(page, "hotkey"))
        page._request_stop.assert_not_called()
        page._request_reconsider_stop.assert_not_called()
        page._request_aftersale_stop.assert_not_called()


if __name__ == "__main__":
    unittest.main()
