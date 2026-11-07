"""Startup health checks without launching applications or waiting in real time."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import update_agent
import update_core as core


class UpdateStartupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.health = Path(self.temporary.name) / "health.json"
        self.process = MagicMock()
        self.process.poll.return_value = None
        self.elapsed = 0.0

    def sleep(self, seconds):
        self.elapsed += seconds

    def test_slow_start_over_old_deadline_succeeds_with_valid_ack(self):
        def sleep(seconds):
            self.sleep(seconds)
            if self.elapsed >= 65:
                core.write_json(self.health, {"token": "expected"})

        with (
            patch.object(
                update_agent.time, "monotonic", side_effect=lambda: self.elapsed
            ),
            patch.object(update_agent.time, "sleep", side_effect=sleep),
        ):
            update_agent.wait_for_startup(self.process, self.health, "expected")
        self.assertGreater(self.elapsed, 40)
        self.assertLess(self.elapsed, update_agent.STARTUP_CONFIRM_TIMEOUT)

    def test_alive_process_without_ack_times_out(self):
        # A stale or unrelated acknowledgement must never commit the update.
        core.write_json(self.health, {"token": "wrong"})
        with (
            patch.object(
                update_agent.time, "monotonic", side_effect=lambda: self.elapsed
            ),
            patch.object(update_agent.time, "sleep", side_effect=self.sleep),
            self.assertRaisesRegex(RuntimeError, "启动确认超时.*180秒"),
        ):
            update_agent.wait_for_startup(self.process, self.health, "expected")
        self.assertGreaterEqual(self.elapsed, 180)

    def test_exited_process_rejects_even_valid_ack_and_reports_exit_code(self):
        core.write_json(self.health, {"token": "expected"})
        self.process.poll.return_value = 7
        with self.assertRaisesRegex(RuntimeError, "进程已退出（退出码 7）"):
            update_agent.wait_for_startup(self.process, self.health, "expected")

    def test_launch_captures_early_pythonw_errors(self):
        root = Path(self.temporary.name) / "app"
        with patch.object(update_agent.subprocess, "Popen") as launch:
            update_agent.launch_gui(root, health_file=self.health, token="expected")
        options = launch.call_args.kwargs
        self.assertEqual(options["stderr"], update_agent.subprocess.STDOUT)
        self.assertEqual(options["env"]["PDDCHAT_UPDATE_HEALTH_TOKEN"], "expected")
        self.assertEqual(
            Path(options["stdout"].name), self.health.parent / "gui-startup.log"
        )
        self.assertTrue(options["stdout"].closed)


if __name__ == "__main__":
    unittest.main()
