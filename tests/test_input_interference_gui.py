import unittest
from unittest.mock import patch

try:
    import PyQt5
except ImportError:
    raise unittest.SkipTest('requires GUI venv')

from tests import test_reconsideration_worker, test_aftersale_appeal_worker
from order_failures import InputInterferenceError, order_error_result


class InputInterferenceWorkerTests(unittest.TestCase):
    def test_interrupted_orders_continue_without_closing_shop_or_resetting_streak(self):
        for suite, name, ready, result_type, failure in (
            (test_reconsideration_worker, 'ReconsiderationWorker', 'reconsider_ready', 'reconsider_order_result', '失败：网络故障'),
            (test_aftersale_appeal_worker, 'AftersaleAppealWorker', 'aftersale_ready', 'aftersale_order_result', '失败: 网络故障'),
        ):
            with self.subTest(worker=name):
                module, fake = suite.worker_module, suite.FakeProcessLineIO
                fake.instances.clear()
                with patch.object(module, 'ProcessLineIO', fake), patch.object(module.random, 'choice', side_effect=lambda items: items[0]), patch.object(module.QTimer, 'singleShot', side_effect=lambda ms, fn: fn()):
                    worker = getattr(module, name)(pddchat_python='python.exe', main_py='main.py', pddchat_root='.', shops=['A'], max_orders=10, pool_size=1)
                    worker.start()
                    io = fake.instances[0]
                    io.line_received.emit({'type':ready, 'queue_size':10})
                    state = worker._states['A']
                    state.consecutive_failures = state.failed = 2
                    for i in range(3):
                        io.line_received.emit({'type':result_type, 'order_no':str(i), 'result':order_error_result(InputInterferenceError('mouse moved')), 'remaining':9-i})
                        self.assertEqual(state.consecutive_failures, 2)
                        self.assertEqual(state.failed, 2)
                        self.assertNotIn({'cmd':'stop'}, io.sent)
                    self.assertEqual(sum(command['cmd']=='next' for command in io.sent), 4)
                    io.line_received.emit({'type':result_type, 'order_no':'real failure', 'result':failure, 'remaining':6})
                    self.assertEqual(state.consecutive_failures, 3)
                    self.assertEqual(state.failed, 3)
                    self.assertIn({'cmd':'stop'}, io.sent)


if __name__ == '__main__':
    unittest.main()
