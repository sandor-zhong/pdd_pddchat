"""Native pointer routing tests; optional desktop integration uses only a local page."""

import os
import unittest
from contextlib import nullcontext
from unittest.mock import MagicMock, patch

import native_mouse


class NativeRoutingTests(unittest.TestCase):
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
        ):
            native_mouse._locator_action(
                locator, "click", position={"x": 10, "y": 15}, delay=80
            )
        driver.click.assert_called_once()
        self.assertEqual(driver.click.call_args.args, (locator.page, 130, 245))
        self.assertEqual(driver.click.call_args.kwargs['delay'], 80)
        driver.click.call_args.kwargs['_validate']()

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
        self.assertLess(names.index('mouse_event'), names.index('SetCursorPos'))
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
        with patch.object(native_mouse.time, 'sleep') as sleep:
            driver.click(MagicMock(), 10, 20, delay=80)
        self.assertEqual(snapshots, [[2, 4]])
        self.assertIn(unittest.mock.call(0.08), sleep.call_args_list)
        self.assertIsNone(driver.drag_page)
        driver.user.mouse_event.assert_not_called()

    def test_partial_input_batch_always_releases_button_and_raises(self):
        driver = self.make_driver()
        driver.user.SendInput.return_value = 1
        with self.assertRaisesRegex(RuntimeError, '未完整发送'):
            driver.click_pulse('left')
        driver.user.mouse_event.assert_called_once_with(4, 0, 0, 0, 0)

    def test_external_movement_during_aim_delay_skips_without_clicking(self):
        from order_failures import InputInterferenceError
        driver = self.make_driver()
        def sleep(seconds):
            if seconds == 0.08:
                driver.user.SetCursorPos(900, 900)
        with patch.object(native_mouse.time, 'sleep', side_effect=sleep):
            with self.assertRaises(InputInterferenceError):
                driver.click(MagicMock(), 10, 20, delay=80)
        driver.user.SendInput.assert_not_called()

    def test_changed_target_before_pulse_aborts_instead_of_clicking_wrong_text(self):
        driver = self.make_driver()
        with self.assertRaisesRegex(RuntimeError, '目标变化'):
            driver.click(MagicMock(), 10, 20, _validate=MagicMock(side_effect=RuntimeError('目标变化')))
        driver.user.SendInput.assert_not_called()

    def test_drag_exception_after_down_still_releases_mouse(self):
        import captcha
        page = MagicMock()
        def wait(_):
            if page.mouse.down.called:
                raise RuntimeError('page closed during drag')
        page.wait_for_timeout.side_effect = wait
        with self.assertRaisesRegex(RuntimeError, 'page closed'):
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
