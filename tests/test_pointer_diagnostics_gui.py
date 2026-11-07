import io
import unittest
from unittest.mock import MagicMock

from gui.app.workers.aftersale_appeal_worker import AftersaleAppealWorker
from gui.app.workers.base_process_worker import ProcessLineIO
from gui.app.workers.reconsideration_worker import ReconsiderationWorker


class PointerGuiDiagnosticTests(unittest.TestCase):
    def test_stderr_diagnostics_are_routed_as_telemetry_messages(self):
        channel = ProcessLineIO(["python"], ".")
        channel._proc = MagicMock(
            stderr=io.StringIO('[鼠标诊断] {"stage":"test"}\nordinary\n')
        )
        received = []
        channel.line_received.connect(received.append)
        channel._read_stderr()
        self.assertEqual(
            received,
            [{"type": "pointer_diagnostic", "message": '[鼠标诊断] {"stage":"test"}'}],
        )

    def test_each_appeal_worker_forwards_diagnostics_without_finishing_order(self):
        for cls in (AftersaleAppealWorker, ReconsiderationWorker):
            worker = MagicMock()
            channel = object()
            worker.sender.return_value = channel
            worker._shop_by_io = {channel: "test-shop"}
            state = MagicMock(processed=0, remaining=20, phase="busy")
            worker._states = {"test-shop": state}
            cls._on_line(
                worker, {"type": "pointer_diagnostic", "message": "[鼠标诊断] sample"}
            )
            worker.diagnostic_line.emit.assert_called_once()
            self.assertEqual(state.processed, 0)
            self.assertEqual(state.remaining, 20)
            worker.shop_order_result.emit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
