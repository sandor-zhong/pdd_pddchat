import asyncio
import json
import logging
import sqlite3
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from gui.app.services import remote_logging as client
from server.log_relay.app import create_app, password_hash


class RelayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        salt = "00" * 16
        self.app = create_app(
            {
                "db": Path(self.tmp.name) / "logs.db",
                "public_view": False,
                "salt": salt,
                "password_hash": password_hash("test-password", salt),
                "origin": "https://testserver",
                "endpoint": "https://testserver/pdd-logs/",
            }
        )
        self.http = TestClient(self.app, base_url="https://testserver/pdd-logs/")
        # Production nginx strips the mount prefix and uvicorn sets root_path.
        self.http = TestClient(
            self.app, base_url="https://testserver", root_path="/pdd-logs"
        )
        self.addCleanup(self.http.close)
        self.origin = {"Origin": "https://testserver"}

    def login(self):
        response = self.http.post(
            "/api/login", json={"password": "test-password"}, headers=self.origin
        )
        self.assertEqual(response.status_code, 200)
        # TestClient routes at '/', while the production cookie is path-scoped.
        self.http.cookies.set(
            "pdd_logs",
            response.cookies.get("pdd_logs"),
            domain="testserver.local",
            path="/",
        )

    def provision(self):
        self.login()
        response = self.http.post(
            "/api/devices", json={"name": "测试电脑"}, headers=self.origin
        )
        self.assertEqual(response.status_code, 200)
        self.config = response.json()
        self.auth = {"Authorization": "Bearer " + self.config["token"]}

    def send(self, events):
        return self.http.post(
            "/api/ingest",
            json={"device_id": self.config["device_id"], "events": events},
            headers=self.auth,
        )

    def event(self, **updates):
        return {
            "event_id": uuid.uuid4().hex,
            "session_id": uuid.uuid4().hex,
            "seq": 1,
            "ts": time.time(),
            "message": "订单测试",
            "shop": "A",
            "task": "负向申诉",
            "level": "INFO",
            **updates,
        }

    def test_auth_and_origin(self):
        self.assertEqual(self.http.get("/api/logs").status_code, 401)
        self.assertEqual(
            self.http.post(
                "/api/login", json={"password": "test-password"}
            ).status_code,
            403,
        )
        self.assertEqual(
            self.http.post(
                "/api/login", json={"password": "wrong"}, headers=self.origin
            ).status_code,
            401,
        )
        self.login()
        self.assertEqual(self.http.get("/api/devices").status_code, 200)

    def test_ack_dedup_filters_and_ownership(self):
        self.provision()
        event = self.event(message="<script>not executed</script>")
        for _ in range(2):
            self.assertEqual(
                self.send([event]).json()["accepted_ids"], [event["event_id"]]
            )
        result = self.http.get("/api/logs?shop=A").json()
        self.assertEqual(len(result["events"]), 1)
        self.assertEqual(self.http.get("/api/logs?shop=B").json()["events"], [])
        self.assertEqual(
            self.http.post(
                "/api/heartbeat", json={"device_id": "wrong"}, headers=self.auth
            ).status_code,
            401,
        )

    def test_cursor_full_batch_does_not_skip(self):
        self.provision()
        for _ in range(3):
            self.assertEqual(
                self.send([self.event() for _ in range(100)]).status_code, 200
            )
        first = self.app.state.store.snapshot({}, after=0)
        second = self.app.state.store.snapshot({}, after=first["cursor"])
        self.assertEqual(len(first["events"]), 200)
        self.assertEqual(len(second["events"]), 100)
        self.assertLess(first["events"][-1]["id"], second["events"][0]["id"])
        self.assertEqual(
            self.app.state.store.snapshot({"shop": "B"}, after=0)["cursor"],
            second["cursor"],
        )

    def test_sse_frames_and_resume_header(self):
        self.provision()
        self.send([self.event(), self.event()])
        from starlette.requests import Request

        cookie = self.http.cookies.get("pdd_logs", domain="testserver.local", path="/")
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/api/stream",
                "query_string": b"after=0",
                "headers": [
                    (b"cookie", f"pdd_logs={cookie}".encode()),
                    (b"last-event-id", b"1"),
                ],
            }
        )
        request.is_disconnected = lambda: asyncio.sleep(0, result=False)
        endpoint = next(
            route.endpoint for route in self.app.routes if route.path == "/api/stream"
        )

        async def read():
            response = await endpoint(request)
            self.assertEqual(response.headers["x-accel-buffering"], "no")
            self.assertIn("text/event-stream", response.headers["content-type"])
            iterator = response.body_iterator
            self.assertIn("retry: 2000", await anext(iterator))
            frame = await anext(iterator)
            await iterator.aclose()
            return frame

        frame = asyncio.run(read())
        self.assertIn("id: 2\nevent: logs\ndata: ", frame)
        self.assertEqual(len(json.loads(frame.split("data: ")[1])["events"]), 1)

    def test_bounded_input_and_retention(self):
        self.provision()
        self.assertEqual(self.send([{"event_id": "bad"}]).status_code, 400)
        self.assertEqual(
            self.http.post(
                "/api/ingest", content=b"x" * 524289, headers=self.auth
            ).status_code,
            413,
        )
        self.send([self.event()])
        with self.app.state.store.connection() as db:
            db.execute("UPDATE logs SET received=0")
        self.app.state.store.last_cleanup = 0
        self.app.state.store.cleanup()
        self.assertEqual(self.http.get("/api/logs").json()["events"], [])


class SenderTests(unittest.TestCase):
    def test_report_failure_is_reported_and_retried_independently(self):
        sender = client.RemoteSender(
            {
                "enabled": True,
                "endpoint": "https://example.com/",
                "device_id": "a" * 32,
                "token": "b" * 40,
            },
            Path("unused-report-spool.db"),
        )
        with (
            patch.object(
                sender,
                "_build_report",
                side_effect=sqlite3.OperationalError("private database path"),
            ),
            patch.object(sender, "post") as post,
            patch.object(sender, "enqueue") as enqueue,
        ):
            sender._sync_report()
            self.assertEqual(sender.last_report, 0)
            self.assertIn("OperationalError", sender.report_error)
            self.assertNotIn("private", sender.report_error)
            post.assert_called_once_with(
                "api/report-status", {"error": sender.report_error}
            )
            self.assertEqual(enqueue.call_count, 1)
        with patch.object(sender, "_build_report") as build:
            sender._sync_report()
            build.assert_not_called()
        sender.last_report_attempt -= 11
        with (
            patch.object(sender, "_build_report", return_value={"details": []}),
            patch.object(sender, "post") as post,
            patch.object(sender, "enqueue"),
        ):
            sender._sync_report()
            post.assert_called_once_with("api/report", {"report": {"details": []}})
        self.assertEqual(sender.report_error, "")
        self.assertGreater(sender.last_report, 0)

    def test_out_of_view_pointer_diagnostic_is_an_error_event(self):
        record = logging.LogRecord(
            "test",
            20,
            "",
            0,
            '[鼠标诊断] {"stage":"target_out_of_view","target":{"x":900,"y":700}}',
            (),
            None,
        )
        self.assertEqual(client.event_from_record(record)["level"], "ERROR")

    def test_task_and_shop_metadata_from_gui_formats(self):
        for line, task in (
            ("[售后申诉][店A] 订单 1", "售后申诉"),
            ("[查询复议结果][店A] 开始", "结果回查"),
        ):
            event = client.event_from_record(
                logging.LogRecord("test", 20, "", 0, line, (), None)
            )
            self.assertEqual(event["shop"], "店A")
            self.assertEqual(event["task"], task)
        record = logging.LogRecord("test", 20, "", 0, "[调度器] 开始", (), None)
        record.remote_shop, record.remote_task = "店B", "负向申诉"
        self.assertEqual(client.event_from_record(record)["task"], "负向申诉")
        self.assertEqual(client.event_from_record(record)["shop"], "店B")

    def test_handler_only_forwards_run_window_events(self):
        from unittest.mock import MagicMock

        sender = MagicMock()
        record = logging.LogRecord("test", 20, "", 0, "ordinary diagnostics", (), None)
        handler = client.RemoteLogHandler()
        with patch.object(client, "_sender", sender):
            handler.emit(record)
            sender.enqueue.assert_not_called()
            record.remote_run_event = True
            handler.emit(record)
            sender.enqueue.assert_called_once()

    def test_redaction_and_config(self):
        text = client.redact(
            "Authorization: Bearer abcdefgh123 password=secret123 api_key=sk-1234567890123456"
        )
        for secret in ("abcdefgh123", "secret123", "sk-1234567890123456"):
            self.assertNotIn(secret, text)
        for value in ([], {"enabled": True, "endpoint": "http://unsafe"}):
            with self.assertRaises((ValueError, TypeError)):
                client.validate_config(value)

    def test_spool_survives_network_failure_and_ack_removes(self):
        with tempfile.TemporaryDirectory() as tmp:
            sender = client.RemoteSender(
                {
                    "enabled": True,
                    "endpoint": "https://example.com/",
                    "device_id": "a" * 32,
                    "token": "b" * 40,
                },
                Path(tmp) / "outbox.db",
            )
            record = logging.LogRecord(
                "test", 20, "", 1, "店铺 [A] 负向申诉 测试", (), None
            )
            event = client.event_from_record(record)
            sender.enqueue(event)
            # Exercise writer and uploader loops without waiting for retry timers.
            sender.stop_event.set()
            sender._writer()
            sender.stop_event.clear()
            with sender.connection() as db:
                self.assertEqual(
                    db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], 1
                )
            calls = iter([False, True])
            with (
                patch.object(
                    sender.stop_event, "wait", side_effect=lambda _: next(calls)
                ),
                patch.object(sender, "post", side_effect=OSError("offline")),
                patch.object(sender, "_sync_report"),
            ):
                sender._uploader()
            with sender.connection() as db:
                self.assertEqual(
                    db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], 1
                )
            calls = iter([False, True])
            with (
                patch.object(
                    sender.stop_event, "wait", side_effect=lambda _: next(calls)
                ),
                patch.object(
                    sender, "post", return_value={"accepted_ids": [event["event_id"]]}
                ),
                patch.object(sender, "_sync_report"),
            ):
                sender._uploader()
            with sender.connection() as db:
                self.assertEqual(
                    db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], 0
                )


if __name__ == "__main__":
    unittest.main()
