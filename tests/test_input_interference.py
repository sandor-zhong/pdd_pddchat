import unittest
from unittest.mock import patch, MagicMock

from order_failures import InputInterferenceError, is_input_interference, next_failure_streak, order_error_result


class InterferencePolicyTests(unittest.TestCase):
    def test_negative_scheduler_continues_result_and_error_interruptions_until_real_failure(self):
        import scheduler_core
        import time
        worker = MagicMock()
        worker.remaining, worker.consecutive_failures = 10, 2
        worker.processed_count, worker.order_duration_total = 0, 0.0
        worker.started_monotonic = time.monotonic()
        error = str(InputInterferenceError('mouse moved'))
        responses = [
            {'type':'result', 'order_no':'1', 'result':'失败: ' + error},
            {'type':'error', 'order_no':'2', 'message':error},
            {'type':'result', 'order_no':'3', 'result':order_error_result(error)},
            {'type':'result', 'order_no':'4', 'result':'失败: 登录失效'},
        ]
        counts = []
        def response(*args, **kwargs):
            counts.append(worker.consecutive_failures)
            return responses.pop(0)
        worker.wait_response.side_effect = response
        events = []
        scheduler = scheduler_core.Scheduler(['A'], rest_enabled=False, on_event=events.append)
        def fill(pending):
            if pending:
                pending.pop()
                scheduler.active['A'] = worker
        scheduler._stop_event = MagicMock()
        scheduler._stop_event.is_set.return_value = False
        scheduler._stop_event.wait.return_value = False
        with patch.object(scheduler, '_fill_pool', side_effect=fill), patch.object(scheduler, '_order_pending_by_priority'), patch.object(scheduler_core.db, 'get_shop_daily_status', return_value=None), patch.object(scheduler_core.db, 'mark_shop_daily_failed') as failed:
            scheduler._run_loop(False)
        self.assertEqual(counts, [2, 2, 2, 2])
        self.assertEqual(worker.consecutive_failures, 3)
        self.assertEqual(worker.processed_count, 4)
        failed.assert_called_once_with('A', '连续 3 单失败')
        self.assertEqual(sum(e.type == 'worker_too_many_failures' for e in events), 1)

    def test_many_interrupted_orders_do_not_increment_or_clear_failure_streak(self):
        streak = 2
        for _ in range(5):
            result = order_error_result(InputInterferenceError('mouse moved'))
            self.assertTrue(result.startswith('跳过：'))
            streak = next_failure_streak(streak, result, True)
            self.assertEqual(streak, 2)
        self.assertEqual(next_failure_streak(streak, '失败: 登录失效', True), 3)
        self.assertEqual(next_failure_streak(streak, '已提交', False), 0)

    def test_ordinary_timeouts_are_not_exempted_without_interference_evidence(self):
        for text in ('Timeout 30000ms exceeded while waiting for event "page"', '聊天查询未确认新结果', '点击前目标位置发生变化'):
            self.assertFalse(is_input_interference(text))
            self.assertEqual(next_failure_streak(2, text, True), 3)

    def test_analysis_and_submit_database_statuses_are_skipped(self):
        import db
        error = InputInterferenceError('mouse moved')
        with patch.object(db, '_upsert') as upsert:
            db.record_analysis_failed('shop', 'order', error)
            self.assertEqual(upsert.call_args.kwargs['status'], 'skipped')
            db.record_submit_result('shop', 'order', order_error_result(error))
            self.assertEqual(upsert.call_args.kwargs['status'], 'skipped')

    def test_human_click_does_not_retry_interrupted_click(self):
        import stealth
        page, locator = MagicMock(), MagicMock()
        locator.wait_for.side_effect = InputInterferenceError('mouse moved')
        with self.assertRaises(InputInterferenceError):
            stealth.human_click(page, locator)
        locator.click.assert_not_called()

    def test_submit_stops_current_order_without_counting_more_records(self):
        import main
        error = InputInterferenceError('mouse moved')
        with patch.object(main, '_order_has_analysis', return_value=False), patch.object(main, 'process_single_order', side_effect=error), patch.object(main, 'save_error_screenshot', return_value=None), patch.object(main.db, 'record_analysis_failed'), patch.object(main, 'submit_single_order') as submit:
            _, result = main.process_and_submit_one_order(MagicMock(), MagicMock(), 'order', {})
        self.assertTrue(result.startswith('跳过：'))
        submit.assert_not_called()


if __name__ == '__main__':
    unittest.main()
