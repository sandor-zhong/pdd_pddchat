import ctypes
import unittest
from unittest.mock import MagicMock, patch

import native_mouse
from order_failures import DesktopUnavailableError


class DesktopActivationTests(unittest.TestCase):
    def driver(self):
        driver = object.__new__(native_mouse.WindowsMouse)
        driver.user, driver.kernel, driver.wts = MagicMock(), MagicMock(), MagicMock()
        driver.drag_page = None
        driver.user.GetAsyncKeyState.return_value = 0
        driver.desktop_state = MagicMock(
            return_value={"interactive": True, "description": "Default,接收输入=True"}
        )
        driver.user.GetWindowRect.side_effect = self.rect
        driver.get_window_style = MagicMock(return_value=0)
        driver._cursor_position = MagicMock(return_value=(10, 20))
        driver._set_cursor_position = MagicMock()
        driver.assert_cursor_position = MagicMock()
        driver.click_pulse = MagicMock()
        driver.user.GetForegroundWindow.return_value = 100
        return driver

    @staticmethod
    def rect(hwnd, pointer):
        value = pointer._obj
        value.left, value.top, value.right, value.bottom = 0, 0, 1920, 1080
        return 1

    def test_caption_activation_clicks_once_and_restores_non_topmost(self):
        driver = self.driver()
        driver._caption_point_valid = MagicMock(return_value=True)
        self.assertTrue(driver._caption_activate(100))
        driver.click_pulse.assert_called_once_with("left")
        self.assertEqual(driver._caption_point_valid.call_count, 2)
        self.assertEqual(driver.user.SetWindowPos.call_args.args[1], -2)

    def test_caption_activation_preserves_existing_topmost(self):
        driver = self.driver()
        driver.get_window_style.return_value = 8
        driver._caption_point_valid = MagicMock(return_value=True)
        self.assertTrue(driver._caption_activate(100))
        self.assertEqual(driver.user.SetWindowPos.call_args.args[1], -1)

    def test_foreground_uses_caption_after_activation_requests_are_denied(self):
        driver = self.driver()
        foreground = [200]
        driver.kernel.GetCurrentThreadId.return_value = 10
        driver.user.GetWindowThreadProcessId.side_effect = lambda hwnd, _: (
            20 if hwnd == 100 else 30
        )
        driver.user.GetForegroundWindow.side_effect = lambda: foreground[0]
        driver.user.SetForegroundWindow.return_value = False
        driver._caption_point_valid = MagicMock(return_value=True)
        driver.click_pulse.side_effect = lambda _: foreground.__setitem__(0, 100)
        with patch.object(native_mouse.time, "sleep"):
            driver.foreground(100)
        self.assertEqual(driver.user.SetForegroundWindow.call_count, 4)
        driver.click_pulse.assert_called_once_with("left")

    def test_no_safe_titlebar_never_moves_or_clicks(self):
        driver = self.driver()
        driver._caption_point_valid = MagicMock(return_value=False)
        self.assertFalse(driver._caption_activate(100))
        driver._set_cursor_position.assert_not_called()
        driver.click_pulse.assert_not_called()

    def test_overlay_appearing_after_movement_prevents_click(self):
        driver = self.driver()
        driver._caption_point_valid = MagicMock(side_effect=[True, False])
        self.assertFalse(driver._caption_activate(100))
        driver.click_pulse.assert_not_called()
        self.assertEqual(driver.user.SetWindowPos.call_args.args[1], -2)

    def test_locked_desktop_and_held_button_never_try_caption(self):
        for locked, held in ((True, False), (False, True)):
            with self.subTest(locked=locked, held=held):
                driver = self.driver()
                driver.desktop_state.return_value["interactive"] = not locked
                driver.user.GetAsyncKeyState.return_value = 0x8000 if held else 0
                self.assertFalse(driver._caption_activate(100))
                driver.user.SetWindowPos.assert_not_called()
                driver.click_pulse.assert_not_called()

    def test_desktop_unavailable_wait_is_bounded_and_count_exempt(self):
        driver = self.driver()
        driver.desktop_state.return_value = {
            "interactive": False,
            "description": "桌面不可交互",
        }
        with (
            patch.object(native_mouse.time, "sleep"),
            self.assertRaisesRegex(DesktopUnavailableError, "DESKTOP_UNAVAILABLE"),
        ):
            driver.foreground(100)
        self.assertEqual(driver.desktop_state.call_count, 26)
        driver.user.SetForegroundWindow.assert_not_called()
        driver.click_pulse.assert_not_called()

    def test_brief_desktop_transition_recovers_without_pointer_actions(self):
        driver = self.driver()
        driver.desktop_state.side_effect = [
            {"interactive": False, "description": "切换中"},
            {"interactive": True, "description": "Default"},
        ]
        with patch.object(native_mouse.time, "sleep"):
            driver.foreground(100)
        driver.click_pulse.assert_not_called()

    def test_hit_test_rejects_page_buttons_and_other_windows(self):
        driver = self.driver()
        driver.user.WindowFromPoint.return_value = 100
        driver.user.GetAncestor.return_value = 100

        def hit(hwnd, message, wparam, lparam, flags, timeout, result):
            result._obj.value = 1  # HTCLIENT: never click the document to focus.
            return 1

        driver.user.SendMessageTimeoutW.side_effect = hit
        self.assertFalse(driver._caption_point_valid(100, 500, 20))
        driver.user.GetAncestor.return_value = 200
        self.assertFalse(driver._caption_point_valid(100, 500, 20))

    def probe(self, input_name="Default", thread_name="Default", receiving=True):
        driver = self.driver()
        del driver.desktop_state  # Exercise the real Win32 probe on fake APIs.
        driver.kernel.ProcessIdToSessionId.return_value = False
        driver.user.OpenInputDesktop.return_value = 2
        driver.user.GetThreadDesktop.return_value = 1

        def information(handle, index, value, length, needed):
            if index == 2:
                value.value = thread_name if handle == 1 else input_name
            elif index == 6:
                value._obj.value = receiving
            return 1

        driver.user.GetUserObjectInformationW.side_effect = information
        return driver

    def test_real_probe_distinguishes_active_and_secure_desktop(self):
        for input_name, receiving, expected in (
            ("Default", True, True),
            ("Default", False, False),
            ("Winlogon", True, False),
        ):
            with self.subTest(input_name=input_name, receiving=receiving):
                driver = self.probe(input_name=input_name, receiving=receiving)
                self.assertEqual(driver.desktop_state()["interactive"], expected)
                driver.user.CloseDesktop.assert_called_once_with(2)

    def test_disconnected_session_does_not_treat_future_desktop_as_active(self):
        driver = self.probe()
        state = ctypes.c_int(4)  # WTSDisconnected
        driver.kernel.ProcessIdToSessionId.return_value = True

        def query(server, session, field, data, length):
            data._obj.value = ctypes.addressof(state)
            length._obj.value = ctypes.sizeof(state)
            return 1

        driver.wts.WTSQuerySessionInformationW.side_effect = query
        self.assertFalse(driver.desktop_state()["interactive"])
        driver.user.OpenInputDesktop.assert_not_called()
        driver.wts.WTSFreeMemory.assert_called_once()


if __name__ == "__main__":
    unittest.main()
