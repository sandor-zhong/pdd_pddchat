import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

import scheduler_core
from gui.app.pages.run_page import RunPage


class DailySkipProgressTests(unittest.TestCase):
    def test_shopless_skip_event_finishes_only_named_shops(self):
        page = SimpleNamespace(
            _run_shops=["A", "B"],
            _run_terminal_shops=set(),
            _run_initial_totals={},
            _run_remaining={},
            _set_reconsider_status=MagicMock(),
            _refresh_run_progress=MagicMock(),
        )
        RunPage._track_run_progress(
            page,
            scheduler_core.SchedulerEvent(
                "round_skip_done", None, {"shops": ["B", "unselected"]}
            ),
        )
        self.assertEqual(page._run_terminal_shops, {"B"})
        self.assertEqual(page._run_skipped_today, {"B"})
        self.assertEqual(page._run_initial_totals, {"B": 0})
        page._set_reconsider_status.assert_called_once_with(
            "B", "今日已完成，本轮跳过", "0"
        )
        page._refresh_run_progress.assert_called_once()


if __name__ == "__main__":
    unittest.main()
