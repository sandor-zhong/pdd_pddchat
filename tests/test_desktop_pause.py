"""Desktop pause and cancellation tests; no session switching is performed."""

import queue
import threading
import unittest
from unittest.mock import MagicMock, patch

import native_mouse
import scheduler_core
from order_failures import DesktopUnavailableError


class InputPauseTests(unittest.TestCase):
    def tearDown(self):
        native_mouse.configure_input_wait()

    def driver(self, states):
        driver = object.__new__(native_mouse.WindowsMouse)
        driver.desktop_state = MagicMock(side_effect=states)
        driver.user = MagicMock()
        driver.drag_page = None
        return driver

    def test_pause_keeps_current_action_until_desktop_recovers(self):
        paused = {"interactive": False, "description": "disconnected"}
        ready = {"interactive": True, "description": "console"}
        driver = self.driver([paused] * 8 + [ready])
        stop = MagicMock()
        stop.wait.return_value = False
        stop.is_set.return_value = False
        events = []
        native_mouse.configure_input_wait(stop, events.append)
        self.assertEqual(driver._wait_for_input_desktop(), ready)
        self.assertEqual(stop.wait.call_count, 8)
        self.assertTrue(all(e["waiting"] for e in events[:-1]))
        self.assertFalse(events[-1]["waiting"])
        driver.user.SendInput.assert_not_called()

    def test_stop_cancels_pause_without_clicking(self):
        driver = self.driver([{"interactive": False, "description": "locked"}])
        stop = threading.Event()
        stop.set()
        events = []
        native_mouse.configure_input_wait(stop, events.append)
        with self.assertRaisesRegex(DesktopUnavailableError, "TASK_STOPPED"):
            driver._wait_for_input_desktop()
        driver.user.SendInput.assert_not_called()
        self.assertFalse(events[-1]["waiting"])

    def test_drag_is_released_instead_of_resumed_after_disconnect(self):
        driver = self.driver([{"interactive": False, "description": "disconnected"}])
        driver.drag_page = object()
        driver.release = MagicMock()
        native_mouse.configure_input_wait(threading.Event(), lambda _: None)
        with self.assertRaisesRegex(DesktopUnavailableError, "DRAG_INTERRUPTED"):
            driver._wait_for_input_desktop()
        driver.release.assert_called_once()
        driver.user.SendInput.assert_not_called()

    def worker(self):
        worker = object.__new__(scheduler_core.ShopWorker)
        worker.alive = True
        worker.out_queue = queue.Queue()
        worker.diagnostic_callback = MagicMock()
        return worker

    def test_live_pause_heartbeats_extend_timeout_until_result(self):
        worker = self.worker()
        clock = [0.0]
        replies = iter(
            [{"type": "input_wait", "waiting": True}] * 8
            + [
                {"type": "input_wait", "waiting": False},
                {"type": "result", "result": "done"},
            ]
        )

        def get(timeout):
            clock[0] += 1
            return next(replies)

        worker.out_queue.get = get
        with patch.object(
            scheduler_core.time, "monotonic", side_effect=lambda: clock[0]
        ):
            result = worker.wait_response(3)
        self.assertEqual(result["result"], "done")
        self.assertEqual(worker.diagnostic_callback.call_count, 2)

    def test_stale_pause_heartbeat_does_not_hide_hung_worker(self):
        worker = self.worker()
        clock = [0.0]
        first = [True]

        def get(timeout):
            clock[0] += timeout
            if first.pop() if first else False:
                return {"type": "input_wait", "waiting": True}
            raise queue.Empty()

        worker.out_queue.get = get
        with patch.object(
            scheduler_core.time, "monotonic", side_effect=lambda: clock[0]
        ):
            self.assertIsNone(worker.wait_response(3))
        self.assertLessEqual(clock[0], 7)

    def test_stop_remains_responsive_during_wait(self):
        worker = self.worker()
        stop = threading.Event()
        stop.set()
        self.assertIsNone(worker.wait_response(300, stop))

    def test_other_shops_check_desktop_while_waiting_for_mouse(self):
        driver = self.driver([{"interactive": True}])
        driver.kernel = MagicMock()
        driver.mutex = 123
        driver.kernel.WaitForSingleObject.side_effect = [0x102, 0]
        driver._wait_for_input_desktop = MagicMock()
        native_mouse.configure_input_wait(threading.Event(), lambda _: None)
        with driver.locked():
            pass
        self.assertEqual(driver._wait_for_input_desktop.call_count, 2)
        self.assertTrue(
            all(
                call.args[1] == 250
                for call in driver.kernel.WaitForSingleObject.call_args_list
            )
        )
        driver.kernel.ReleaseMutex.assert_called_once_with(123)


if __name__ == "__main__":
    unittest.main()
