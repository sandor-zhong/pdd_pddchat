import io
import json
import threading
import unittest
from collections import deque
from unittest.mock import MagicMock, patch

import native_mouse
import pdd_chat
import scheduler_core


class PointerDiagnosticTests(unittest.TestCase):
    def driver(self):
        driver = object.__new__(native_mouse.WindowsMouse)
        driver.user = MagicMock()
        driver.window = MagicMock(return_value=123)
        driver._pointer_geometry = MagicMock(
            return_value={"window_rect": [0, 0, 1000, 850]}
        )
        page = MagicMock()
        page.viewport_size = None
        page.url = "https://example.com/mms-chat/search?secret=not-for-logs#private"
        page.evaluate.return_value = {"w": 800, "h": 600, "dpr": 1.25}

        def client(hwnd, rect):
            rect._obj.right, rect._obj.bottom = 1000, 850
            return True

        def origin(hwnd, point):
            point._obj.x, point._obj.y = 0, 20
            return True

        driver.user.GetClientRect.side_effect = client
        driver.user.ClientToScreen.side_effect = origin
        return driver, page

    def test_outside_point_records_coordinates_viewport_stack_without_clicking(self):
        driver, page = self.driver()
        with (
            patch.object(native_mouse, "pointer_diagnostic") as log,
            self.assertRaisesRegex(RuntimeError, r"可视CSS=\(800,600\)"),
        ):
            driver.point(page, 900, 700)
        event = next(
            call.kwargs
            for call in log.call_args_list
            if call.args[0] == "target_out_of_view"
        )
        self.assertEqual(event["target"], {"x": 900, "y": 700})
        self.assertEqual(event["viewport"]["dpr"], 1.25)
        self.assertTrue(
            any("test_pointer_diagnostics.py:" in line for line in event["stack"])
        )
        self.assertNotIn("not-for-logs", json.dumps(event))
        driver.user.SendInput.assert_not_called()
        driver.user.SetCursorPos.assert_not_called()

    def test_geometry_changes_are_logged_without_logging_every_move(self):
        driver, page = self.driver()
        with patch.object(native_mouse, "pointer_diagnostic") as log:
            self.assertEqual(driver.point(page, 100, 100), (125, 245))
            driver.point(page, 100, 100)
            self.assertEqual(log.call_count, 1)
            page.evaluate.return_value = {"w": 640, "h": 600, "dpr": 1.25}
            driver.point(page, 100, 100)
            self.assertEqual(log.call_count, 2)
            self.assertEqual(log.call_args.kwargs["before"][0], 800)
            self.assertEqual(log.call_args.kwargs["viewport"]["w"], 640)

    def test_chat_container_target_and_visible_intersection_are_recorded(self):
        page = MagicMock()
        info = {
            "box": {"x": 100, "y": 200, "width": 500, "height": 1200},
            "viewport": {"width": 1000, "height": 600},
            "view": {"left": 100, "top": 200, "right": 600, "bottom": 600},
            "target": {"x": 350, "y": 400},
            "state": {"top": 0, "max": 800},
            "kind": "element",
        }
        with (
            patch.object(pdd_chat, "prepare_system_mouse_page"),
            patch.object(pdd_chat, "_chat_capture_geometry", return_value=info),
            patch.object(pdd_chat, "pointer_diagnostic") as log,
        ):
            self.assertEqual(pdd_chat._aim_chat_scroll(page, "test-order", 1), info)
        self.assertEqual(log.call_args.args[0], "chat_scroll_target")
        data = log.call_args.kwargs
        self.assertEqual(data["order_no"], "test-order")
        self.assertEqual(data["visible_intersection"], info["view"])
        page.mouse.move.assert_called_once_with(350, 400)

    def test_analysis_error_includes_order_and_traceback(self):
        with patch.object(native_mouse, "pointer_diagnostic") as log:
            try:
                raise RuntimeError("鼠标目标位于浏览器可视区域之外。")
            except RuntimeError as error:
                native_mouse.pointer_error_diagnostic(error, "test-order")
        self.assertEqual(log.call_args.kwargs["order_no"], "test-order")
        self.assertTrue(log.call_args.kwargs["stack"])

    def test_rejected_move_remeasures_instead_of_reusing_old_target(self):
        page = MagicMock()
        first = {
            "target": {"x": 900, "y": 700},
            "box": {},
            "viewport": {},
            "state": {},
            "kind": "page",
            "view": {},
        }
        resized = dict(first, target={"x": 400, "y": 300})
        page.mouse.move.side_effect = [
            RuntimeError("鼠标目标位于浏览器可视区域之外。"),
            None,
        ]
        with (
            patch.object(pdd_chat, "prepare_system_mouse_page"),
            patch.object(
                pdd_chat,
                "_chat_capture_geometry",
                side_effect=[first, resized, resized],
            ),
            patch.object(pdd_chat, "pointer_diagnostic"),
        ):
            pdd_chat._aim_chat_scroll(page, "test-order", 1)
        self.assertEqual(
            [call.args for call in page.mouse.move.call_args_list],
            [(900, 700), (400, 300)],
        )
        page.mouse.wheel.assert_not_called()

    def test_diagnostic_writes_stderr_and_never_protocol_stdout(self):
        out, err = io.StringIO(), io.StringIO()
        with (
            patch.object(native_mouse.sys, "stdout", out),
            patch.object(native_mouse.sys, "stderr", err),
        ):
            native_mouse.pointer_diagnostic("test", target={"x": 10, "y": 20})
        self.assertEqual(out.getvalue(), "")
        self.assertTrue(err.getvalue().startswith("[鼠标诊断] "))
        self.assertEqual(
            json.loads(err.getvalue().split("] ", 1)[1])["target"]["x"], 10
        )

    def test_scheduler_forwards_pointer_lines_without_forwarding_arbitrary_stderr(self):
        worker = object.__new__(scheduler_core.ShopWorker)
        worker.proc = MagicMock(
            stderr=io.StringIO('[鼠标诊断] {"stage":"test"}\nprivate ordinary stderr\n')
        )
        worker._stderr_lines = deque(maxlen=500)
        worker._stderr_lock = threading.Lock()
        worker.diagnostic_callback = MagicMock()
        worker._read_stderr()
        worker.diagnostic_callback.assert_called_once_with(
            '[鼠标诊断] {"stage":"test"}'
        )
        self.assertEqual(len(worker._stderr_lines), 2)


if __name__ == "__main__":
    unittest.main()
