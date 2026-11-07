"""HTTPS/SSE smoke test; never prints the viewer password or upload token."""

import argparse
import http.cookiejar
import json
import logging
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from gui.app.services import remote_logging


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--configure-client", action="store_true")
    args = parser.parse_args()
    access = json.loads(
        (ROOT / "state/remote-log-access.json").read_text(encoding="utf-8")
    )
    endpoint = access["url"]
    config = {
        "enabled": True,
        "endpoint": endpoint,
        "device_id": access["device_id"],
        "token": access["token"],
        "name": "主运行电脑",
    }
    cookies = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookies))

    def request(route, data=None, headers=None):
        return opener.open(
            urllib.request.Request(
                endpoint + route,
                data=json.dumps(data).encode() if data is not None else None,
                headers={"Content-Type": "application/json", **(headers or {})},
            ),
            timeout=20,
        )

    with request("health") as response:
        assert json.load(response)["ok"]
    with request(
        "api/login", {"password": access["password"]}, {"Origin": "https://flowtry.top"}
    ) as response:
        assert response.status == 200
    with request("api/logs") as response:
        cursor = json.load(response)["cursor"]
    sender = remote_logging.RemoteSender(config, ROOT / "state/live-verify-unused.db")
    event = remote_logging.event_from_record(
        logging.LogRecord(
            "test", 20, "", 0, "[连接测试] HTTPS上传与SSE实时推送验证", (), None
        )
    )
    start = time.monotonic()
    # Open the stream before upload, testing flush rather than replay alone.
    with request("api/stream?after=" + str(cursor)) as response:
        assert response.headers.get_content_type() == "text/event-stream"
        assert response.readline().strip() == b"retry: 2000"
        assert sender.post("api/ingest", {"events": [event]})["accepted_ids"] == [
            event["event_id"]
        ]
        found = None
        while time.monotonic() - start < 20:
            line = response.readline().decode().strip()
            if line.startswith("data: "):
                batch = json.loads(line[6:])
                if any(
                    item["event_id"] == event["event_id"] for item in batch["events"]
                ):
                    found = batch["cursor"]
                    break
        assert found is not None, "SSE实时帧未收到"
        first_frame_seconds = time.monotonic() - start
    replay = remote_logging.event_from_record(
        logging.LogRecord("test", 20, "", 0, "[连接测试] SSE断线游标续传验证", (), None)
    )
    sender.post("api/ingest", {"events": [replay]})
    with request(
        "api/stream?after=0", headers={"Last-Event-ID": str(found)}
    ) as response:
        for _ in range(10):
            line = response.readline().decode().strip()
            if line.startswith("data: "):
                batch = json.loads(line[6:])
                assert [e["event_id"] for e in batch["events"]] == [replay["event_id"]]
                break
        else:
            raise RuntimeError("未收到续传帧")
    # Also exercise the real background spool/upload thread path.
    with tempfile.TemporaryDirectory() as temporary:
        background = remote_logging.RemoteSender(config, Path(temporary) / "outbox.db")
        event = remote_logging.event_from_record(
            logging.LogRecord(
                "test", 20, "", 0, "[连接测试] 后台缓存与批量上报验证", (), None
            )
        )
        background.start()
        try:
            background.enqueue(event)
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline:
                with request("api/logs") as response:
                    if any(
                        e["event_id"] == event["event_id"]
                        for e in json.load(response)["events"]
                    ):
                        break
                time.sleep(0.3)
            else:
                raise RuntimeError("后台上传未在12秒内完成")
        finally:
            background.stop()
            background.uploader.join(timeout=10)
    if args.configure_client:
        remote_logging.save_config(config)
    print(
        f"HTTPS登录、后台批量上传、SSE实时帧与断线续传验证通过；首帧耗时 {first_frame_seconds:.2f} 秒。"
    )


if __name__ == "__main__":
    main()
