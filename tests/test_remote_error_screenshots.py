import base64
import json
import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gui.app.services import remote_logging as client
from tests.test_remote_logs import RelayTests

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a0r8AAAAASUVORK5CYII="
)


class ScreenshotRelayTests(RelayTests):
    def test_screenshot_upload_is_authenticated_and_attached_to_exact_error_event(self):
        self.provision()
        event = self.event(
            message="订单 261106-123456789012345：错误现场截图", level="ERROR"
        )
        payload = {
            "device_id": self.config["device_id"],
            "event_id": event["event_id"],
            "image": base64.b64encode(PNG).decode(),
        }
        self.assertEqual(
            self.http.post("/api/screenshots", json=payload).status_code, 401
        )
        self.assertEqual(
            self.http.post(
                "/api/screenshots",
                json={**payload, "device_id": "f" * 32},
                headers=self.auth,
            ).status_code,
            401,
        )
        self.assertEqual(
            self.http.post("/api/screenshots", json=payload, headers=self.auth).json()[
                "event_id"
            ],
            event["event_id"],
        )
        self.send([event])
        order = self.http.get("/api/orders").json()["orders"][0]
        self.assertEqual(len(order["screenshots"]), 1)
        url = "/" + order["screenshots"][0]["url"]
        response = self.http.get(url)
        self.assertEqual(response.content, PNG)
        self.assertEqual(response.headers["content-type"], "image/png")
        self.http.cookies.clear()
        self.assertEqual(self.http.get(url).status_code, 401)

    def test_invalid_images_rejected_and_expired_images_removed(self):
        self.provision()
        event = self.event()
        payload = {
            "device_id": self.config["device_id"],
            "event_id": event["event_id"],
            "image": "not an image",
        }
        self.assertEqual(
            self.http.post(
                "/api/screenshots", json=payload, headers=self.auth
            ).status_code,
            400,
        )
        payload["image"] = base64.b64encode(PNG).decode()
        self.http.post("/api/screenshots", json=payload, headers=self.auth)
        with self.app.state.store.connection() as db:
            db.execute("UPDATE screenshots SET received=0")
        self.app.state.store.last_cleanup = 0
        self.assertEqual(
            self.http.get(
                "/api/screenshots/" + self.config["device_id"] + "/" + event["event_id"]
            ).status_code,
            404,
        )


class ScreenshotSenderTests(unittest.TestCase):
    def test_error_image_is_durably_queued_and_retries_without_consuming_log(self):
        from gui.app.services.project_paths import PROJECT_PATHS

        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.object(PROJECT_PATHS, "root", Path(temporary)),
        ):
            image = Path(temporary) / "debug/shop/error.png"
            image.parent.mkdir(parents=True)
            image.write_bytes(PNG)
            diagnostic = {
                "stage": "error_screenshot",
                "order_no": "261106-123456789012345",
                "screenshot_path": str(image),
            }
            record = logging.LogRecord(
                "test",
                logging.INFO,
                "",
                0,
                "[鼠标诊断] " + json.dumps(diagnostic),
                (),
                None,
            )
            record.remote_shop = "shop"
            record.remote_task = "负向申诉"
            event = client.event_from_record(record)
            self.assertEqual(event["level"], "ERROR")
            self.assertNotIn(temporary, event["message"])
            sender = client.RemoteSender(
                {
                    "enabled": True,
                    "endpoint": "https://example.com/",
                    "device_id": "a" * 32,
                    "token": "b" * 40,
                },
                Path(temporary) / "spool.db",
            )
            sender.enqueue(event)
            sender.stop_event.set()
            sender._writer()
            with sender.connection() as db:
                self.assertEqual(
                    db.execute("SELECT COUNT(*) FROM image_outbox").fetchone()[0], 1
                )
                self.assertEqual(
                    db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], 1
                )
                self.assertNotIn(
                    "screenshot_path",
                    db.execute("SELECT payload FROM outbox").fetchone()[0],
                )
            with patch.object(sender, "post", side_effect=OSError("offline")):
                sender._sync_image()
            with sender.connection() as db:
                self.assertEqual(
                    db.execute("SELECT COUNT(*) FROM image_outbox").fetchone()[0], 1
                )
            with patch.object(
                sender, "post", return_value={"event_id": event["event_id"]}
            ):
                sender._sync_image()
            with sender.connection() as db:
                self.assertEqual(
                    db.execute("SELECT COUNT(*) FROM image_outbox").fetchone()[0], 0
                )
                self.assertEqual(
                    db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], 1
                )


if __name__ == "__main__":
    unittest.main()
