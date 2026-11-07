"""Native pointer routing tests; optional desktop integration uses only a local page."""

import os
import unittest
from contextlib import nullcontext
from unittest.mock import MagicMock, patch

import native_mouse


class NativeRoutingTests(unittest.TestCase):
    def foreground_driver(self):
        driver = object.__new__(native_mouse.WindowsMouse)
        driver.user, driver.kernel = MagicMock(), MagicMock()
        driver.desktop_state = MagicMock(
            return_value={"interactive": True, "description": "输入桌面可用"}
        )
        driver.kernel.GetCurrentThreadId.return_value = 10
        driver.user.IsWindow.return_value = True
        driver.user.IsZoomed.return_value = True
        driver.user.GetWindowThreadProcessId.side_effect = lambda hwnd, pid: {
            100: 20,
            200: 30,
            300: 40,
        }[hwnd]
        driver.user.AttachThreadInput.return_value = True
        return driver

    def test_foreground_retries_with_new_active_thread_and_detaches(self):
        driver = self.foreground_driver()
        foreground = [200]
        driver.user.GetForegroundWindow.side_effect = lambda: foreground[0]

        def activate(hwnd):
            foreground[0] = (
                300 if driver.user.SetForegroundWindow.call_count == 1 else hwnd
            )
            return foreground[0] == hwnd

        driver.user.SetForegroundWindow.side_effect = activate
        with patch.object(native_mouse.time, "sleep"):
            driver.foreground(100)
        self.assertEqual(driver.user.SetForegroundWindow.call_count, 2)
        self.assertEqual(
            driver.user.AttachThreadInput.call_args_list,
            [
                unittest.mock.call(10, 30, True),
                unittest.mock.call(10, 20, True),
                unittest.mock.call(10, 20, False),
                unittest.mock.call(10, 30, False),
                unittest.mock.call(10, 40, True),
                unittest.mock.call(10, 20, True),
                unittest.mock.call(10, 20, False),
                unittest.mock.call(10, 40, False),
            ],
        )
        driver.user.SendInput.assert_not_called()

    def test_foreground_failure_has_bounded_retries_and_diagnostics(self):
        driver = self.foreground_driver()
        driver.user.GetForegroundWindow.return_value = 200
        driver.user.SetForegroundWindow.return_value = False
        driver._window_diagnostic = lambda hwnd: f"window-{hwnd}"
        with (
            patch.object(native_mouse.time, "sleep"),
            self.assertRaisesRegex(
                RuntimeError, r"已重试4次；目标\[window-100\]；实际前台\[window-200\]"
            ),
        ):
            driver.foreground(100)
        self.assertEqual(driver.user.SetForegroundWindow.call_count, 4)
        driver.user.SendInput.assert_not_called()
        driver.user.SetCursorPos.assert_not_called()

    def test_foreground_exception_still_detaches_threads(self):
        driver = self.foreground_driver()
        driver.user.GetForegroundWindow.return_value = 200
        driver.user.BringWindowToTop.side_effect = RuntimeError("activation failed")
        with self.assertRaisesRegex(RuntimeError, "activation failed"):
            driver.foreground(100)
        self.assertEqual(
            driver.user.AttachThreadInput.call_args_list[-2:],
            [
                unittest.mock.call(10, 20, False),
                unittest.mock.call(10, 30, False),
            ],
        )

    def test_failed_thread_attachment_is_not_detached(self):
        driver = self.foreground_driver()
        driver.user.GetForegroundWindow.side_effect = [200, 200, 100]
        driver.user.AttachThreadInput.return_value = False
        driver.foreground(100)
        self.assertEqual(driver.user.AttachThreadInput.call_count, 2)

    def test_already_foreground_does_not_reattach_or_activate(self):
        driver = self.foreground_driver()
        driver.user.IsZoomed.return_value = False
        driver.user.GetForegroundWindow.return_value = 100
        driver.foreground(100)
        driver.user.ShowWindow.assert_called_once_with(100, 3)
        driver.user.AttachThreadInput.assert_not_called()
        driver.user.SetForegroundWindow.assert_not_called()

    def test_obscured_target_never_clicks(self):
        locator = MagicMock()
        locator.bounding_box.return_value = {
            "x": 20,
            "y": 30,
            "width": 100,
            "height": 40,
        }
        locator.evaluate.return_value = False
        driver = MagicMock()
        driver.locked.side_effect = lambda: nullcontext()
        with (
            patch.object(native_mouse, "_driver", driver),
            patch.object(native_mouse, "expect"),
            patch.object(native_mouse, "_scroll_locator_into_view"),
            self.assertRaisesRegex(RuntimeError, "遮挡"),
        ):
            native_mouse._locator_action(locator, "click")
        driver.click.assert_not_called()

    def test_local_position_converts_to_main_frame_coordinates(self):
        locator = MagicMock()
        locator.bounding_box.return_value = {
            "x": 120,
            "y": 230,
            "width": 100,
            "height": 40,
        }
        driver = MagicMock()
        driver.locked.side_effect = lambda: nullcontext()
        with (
            patch.object(native_mouse, "_driver", driver),
            patch.object(native_mouse, "expect"),
            patch.object(native_mouse, "_scroll_locator_into_view"),
        ):
            native_mouse._locator_action(
                locator, "click", position={"x": 10, "y": 15}, delay=80
            )
        driver.click.assert_called_once()
        self.assertEqual(driver.click.call_args.args, (locator.page, 130, 245))
        self.assertEqual(driver.click.call_args.kwargs["delay"], 80)
        driver.click.call_args.kwargs["_validate"]()

    def test_reflow_before_click_remeasures_and_delivers_only_once(self):
        locator = MagicMock()
        old = {"x": 1700, "y": 300, "width": 100, "height": 40}
        new = {"x": 700, "y": 300, "width": 100, "height": 40}
        locator.bounding_box.side_effect = [old, old, new, new, new, new]
        locator.evaluate.return_value = True
        driver = MagicMock()
        driver.locked.side_effect = lambda: nullcontext()
        delivered = []

        def click(page, x, y, **options):
            options["_validate"]()
            delivered.append((x, y))

        driver.click.side_effect = click
        with (
            patch.object(native_mouse, "_driver", driver),
            patch.object(native_mouse, "expect"),
            patch.object(native_mouse, "_scroll_locator_into_view"),
            patch.object(native_mouse.time, "sleep"),
            patch.object(native_mouse, "pointer_diagnostic"),
        ):
            native_mouse._locator_action(locator, "click")
        self.assertEqual(delivered, [(750, 320)])
        self.assertEqual(driver.click.call_count, 2)

    def test_error_after_click_is_not_replayed(self):
        locator = MagicMock()
        locator.bounding_box.return_value = {
            "x": 20,
            "y": 30,
            "width": 100,
            "height": 40,
        }
        locator.evaluate.return_value = True
        driver = MagicMock()
        driver.locked.side_effect = lambda: nullcontext()
        driver.click.side_effect = RuntimeError(
            "result confirmation failed after delivery"
        )
        with (
            patch.object(native_mouse, "_driver", driver),
            patch.object(native_mouse, "expect"),
            patch.object(native_mouse, "_scroll_locator_into_view"),
            patch.object(native_mouse.time, "sleep"),
            patch.object(native_mouse, "pointer_diagnostic"),
            self.assertRaisesRegex(RuntimeError, "after delivery"),
        ):
            native_mouse._locator_action(locator, "click")
        driver.click.assert_called_once()

    def test_fractional_wheel_units_are_accumulated_instead_of_dropped(self):
        driver = object.__new__(native_mouse.WindowsMouse)
        driver.locked = lambda: nullcontext()
        driver.window = MagicMock()
        driver.user = MagicMock()
        driver.point = lambda page, x, y: (x, y)
        page = MagicMock()
        page.evaluate.return_value = {"w": 1024, "h": 641}
        for _ in range(8):
            driver.wheel(page, 0, 0.25)
        sent = [call.args[3] for call in driver.user.mouse_event.call_args_list]
        self.assertEqual(sent, [0xFFFFFFFF, 0xFFFFFFFF])
        driver.user.SendInput.assert_not_called()

    def make_driver(self):
        driver = object.__new__(native_mouse.WindowsMouse)
        driver.user, driver.kernel = MagicMock(), MagicMock()
        driver.user.GetAsyncKeyState.return_value = 0
        driver.user.SetCursorPos.return_value = 1
        cursor = [0, 0]

        def set_position(x, y):
            cursor[:] = [x, y]
            return 1

        def get_position(pointer):
            pointer._obj.x, pointer._obj.y = cursor
            return 1

        driver.user.SetCursorPos.side_effect = set_position
        driver.user.GetCursorPos.side_effect = get_position
        driver.user.SendInput.return_value = 2
        driver.drag_page = driver.drag_owner = None
        driver.buttons, driver.positions = set(), {}
        driver.point = MagicMock(return_value=(100, 200))
        driver.locked = lambda: nullcontext()
        return driver

    def test_idle_move_releases_system_button_before_cursor_movement(self):
        driver = self.make_driver()
        driver.user.GetAsyncKeyState.side_effect = lambda key: 0x8000 if key == 1 else 0
        driver.move(MagicMock(), 10, 20)
        names = [c[0] for c in driver.user.mock_calls]
        self.assertLess(names.index("mouse_event"), names.index("SetCursorPos"))
        driver.user.mouse_event.assert_called_once_with(4, 0, 0, 0, 0)

    def test_explicit_drag_move_does_not_clear_held_button(self):
        driver = self.make_driver()
        page = MagicMock()
        driver.drag_page = page._impl_obj
        driver.move(page, 10, 20)
        driver.user.GetAsyncKeyState.assert_not_called()
        driver.user.mouse_event.assert_not_called()

    def test_click_sends_down_up_in_one_batch_after_aim_delay(self):
        driver = self.make_driver()
        snapshots = []

        def send(count, events, size):
            snapshots.append([events[i].value.mi.dwFlags for i in range(count)])
            self.assertEqual(size, native_mouse.ctypes.sizeof(native_mouse._Input))
            return count

        driver.user.SendInput.side_effect = send
        with patch.object(native_mouse.time, "sleep") as sleep:
            driver.click(MagicMock(), 10, 20, delay=80)
        self.assertEqual(snapshots, [[2, 4]])
        self.assertIn(unittest.mock.call(0.08), sleep.call_args_list)
        self.assertIsNone(driver.drag_page)
        driver.user.mouse_event.assert_not_called()

    def test_partial_input_batch_always_releases_button_and_raises(self):
        driver = self.make_driver()
        driver.user.SendInput.return_value = 1
        with self.assertRaisesRegex(RuntimeError, "未完整发送"):
            driver.click_pulse("left")
        driver.user.mouse_event.assert_called_once_with(4, 0, 0, 0, 0)

    def test_remote_resync_during_aim_delay_recovers_with_one_click(self):
        driver = self.make_driver()

        def sleep(seconds):
            if seconds == 0.08:
                driver.user.SetCursorPos(900, 900)

        with patch.object(native_mouse.time, "sleep", side_effect=sleep):
            driver.click(MagicMock(), 10, 20, delay=80)
        driver.user.SendInput.assert_called_once()

    def test_transient_resync_revalidates_target_before_recovered_click(self):
        driver = self.make_driver()
        checks = []

        def validate():
            checks.append(True)
            if len(checks) == 1:
                driver.user.SetCursorPos(900, 900)

        with patch.object(native_mouse.time, "sleep"):
            driver.click(MagicMock(), 10, 20, _validate=validate)
        self.assertEqual(len(checks), 2)
        driver.user.SendInput.assert_called_once()

    def test_recovery_never_clicks_target_that_becomes_obscured(self):
        driver = self.make_driver()
        checks = []

        def validate():
            checks.append(True)
            if len(checks) == 1:
                driver.user.SetCursorPos(900, 900)
            else:
                raise RuntimeError("目标被遮挡")

        with (
            patch.object(native_mouse.time, "sleep"),
            self.assertRaisesRegex(RuntimeError, "目标被遮挡"),
        ):
            driver.click(MagicMock(), 10, 20, _validate=validate)
        driver.user.SendInput.assert_not_called()

    def test_failed_second_double_click_never_replays_first_click(self):
        driver = self.make_driver()
        checks = []

        def validate():
            checks.append(True)
            if len(checks) > 1:
                driver.user.SetCursorPos(900, 900)

        with (
            patch.object(native_mouse.time, "sleep"),
            self.assertRaises(native_mouse.InputInterferenceError),
        ):
            driver.click(MagicMock(), 10, 20, click_count=2, _validate=validate)
        self.assertEqual(len(checks), 4)
        driver.user.SendInput.assert_called_once()

    def test_system_coordinate_clipping_is_position_error_not_displacement(self):
        from order_failures import CursorPositionError

        driver = self.make_driver()
        driver.point.return_value = (800, 600)
        original = driver.user.SetCursorPos.side_effect
        driver.user.SetCursorPos.side_effect = lambda x, y: original(
            min(x, 300), min(y, 250)
        )
        with (
            patch.object(native_mouse.time, "sleep"),
            self.assertRaisesRegex(
                CursorPositionError,
                r"CURSOR_POSITIONING.*目标=.*实际=.*不能据此认定人工干预",
            ),
        ):
            driver.click(MagicMock(), 10, 20)
        driver.user.SendInput.assert_not_called()

    def test_changed_screen_mapping_is_target_change_not_cursor_displacement(self):
        from order_failures import MouseTargetChangedError

        driver = self.make_driver()
        driver.point.side_effect = [(100, 200), (120, 200)]
        with (
            patch.object(native_mouse.time, "sleep"),
            self.assertRaisesRegex(
                MouseTargetChangedError,
                r"MOUSE_TARGET_CHANGED.*原定位=\(100, 200\).*新目标=\(120,200\)",
            ),
        ):
            driver.click(MagicMock(), 10, 20)
        driver.user.SendInput.assert_not_called()

    def test_small_system_adjustment_uses_actual_position_as_baseline(self):
        driver = self.make_driver()
        original = driver.user.SetCursorPos.side_effect
        driver.user.SetCursorPos.side_effect = lambda x, y: original(x - 2, y)
        with patch.object(native_mouse.time, "sleep"):
            driver.click(MagicMock(), 10, 20)
        driver.user.SendInput.assert_called_once()

    def test_movement_during_target_validation_is_detected_before_click(self):
        driver = self.make_driver()
        validate = MagicMock(side_effect=lambda: driver.user.SetCursorPos(900, 900))
        with (
            patch.object(native_mouse.time, "sleep"),
            self.assertRaisesRegex(
                native_mouse.InputInterferenceError, "CURSOR_DISPLACED"
            ),
        ):
            driver.click(MagicMock(), 10, 20, _validate=validate)
        self.assertEqual(validate.call_count, 3)
        driver.user.SendInput.assert_not_called()

    def test_final_reposition_failure_does_not_send_click(self):
        from order_failures import CursorPositionError

        driver = self.make_driver()
        original = driver.user.SetCursorPos.side_effect

        def set_position(x, y):
            return (
                original(0, 0)
                if driver.user.SetCursorPos.call_count == 9
                else original(x, y)
            )

        driver.user.SetCursorPos.side_effect = set_position
        with (
            patch.object(native_mouse.time, "sleep"),
            self.assertRaises(CursorPositionError),
        ):
            driver.click(MagicMock(), 10, 20)
        driver.user.SendInput.assert_not_called()

    def test_changed_target_before_pulse_aborts_instead_of_clicking_wrong_text(self):
        driver = self.make_driver()
        with self.assertRaisesRegex(RuntimeError, "目标变化"):
            driver.click(
                MagicMock(),
                10,
                20,
                _validate=MagicMock(side_effect=RuntimeError("目标变化")),
            )
        driver.user.SendInput.assert_not_called()

    def test_drag_exception_after_down_still_releases_mouse(self):
        import captcha

        page = MagicMock()

        def wait(_):
            if page.mouse.down.called:
                raise RuntimeError("page closed during drag")

        page.wait_for_timeout.side_effect = wait
        with self.assertRaisesRegex(RuntimeError, "page closed"):
            captcha._human_drag_slider(page, 10, 20, 100)
        page.mouse.up.assert_called_once()

    def test_drag_release_always_sends_button_up(self):
        driver = object.__new__(native_mouse.WindowsMouse)
        import threading

        driver.drag_owner = threading.get_ident()
        driver.drag_page = object()
        driver.buttons = {"left"}
        driver.user, driver.kernel, driver.mutex = MagicMock(), MagicMock(), object()
        driver.release()
        driver.user.mouse_event.assert_called_once_with(4, 0, 0, 0, 0)
        driver.kernel.ReleaseMutex.assert_called_once_with(driver.mutex)
        self.assertIsNone(driver.drag_page)


@unittest.skipUnless(
    os.environ.get("PDD_NATIVE_MOUSE_SMOKE") == "1",
    "requires an interactive Windows desktop",
)
class NativeDesktopTests(unittest.TestCase):
    def test_real_cursor_click_drag_wheel_and_popup(self):
        from ctypes import byref, wintypes

        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            engine = os.environ.get("PDD_NATIVE_MOUSE_BROWSER", "chromium")
            browser = getattr(pw, engine).launch(headless=False)
            try:
                context = browser.new_context(no_viewport=True)
                native_mouse.install_system_mouse(context)
                page = context.new_page()
                page.set_content("""<button id="button" style="margin:80px;width:140px;height:50px">Click</button>
                    <input id="slider" type="range" style="width:300px" value="0">
                    <div style="height:3000px"></div>
                    <script>window.clicks=0;button.onclick=()=>window.clicks++;</script>""")
                page.locator("#button").click()
                page.wait_for_function("window.clicks === 1")
                driver = native_mouse._driver
                hwnd = driver.windows[page._impl_obj]
                self.assertTrue(driver.user.IsZoomed(hwnd))
                # A restored window must maximize again before locating a target.
                driver.user.ShowWindow(hwnd, 9)
                page.locator("#button").click()
                page.wait_for_function("window.clicks === 2")
                self.assertTrue(driver.user.IsZoomed(hwnd))
                box = page.locator("#button").bounding_box()
                x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
                expected = native_mouse._driver.point(page, x, y)
                cursor = wintypes.POINT()
                native_mouse._driver.user.GetCursorPos(byref(cursor))
                self.assertLessEqual(abs(cursor.x - expected[0]), 2)
                self.assertLessEqual(abs(cursor.y - expected[1]), 2)
                slider = page.locator("#slider").bounding_box()
                page.mouse.move(slider["x"] + 8, slider["y"] + slider["height"] / 2)
                try:
                    page.mouse.down()
                    page.mouse.move(
                        slider["x"] + 250, slider["y"] + slider["height"] / 2, steps=20
                    )
                finally:
                    page.mouse.up()
                self.assertGreater(int(page.locator("#slider").input_value()), 60)
                page.mouse.wheel(0, 500)
                page.wait_for_function("scrollY > 0")
                popup = context.new_page()
                popup.set_content(
                    "<button onclick=\"this.textContent='done'\">popup</button>"
                )
                popup.locator("button").click()
                self.assertEqual(popup.locator("button").inner_text(), "done")
            finally:
                browser.close()
