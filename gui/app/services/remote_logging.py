"""Non-blocking, opt-in forwarding of run-window logs. Standard library only."""

from __future__ import annotations

import base64
import itertools
import json
import logging
import queue
import re
import sqlite3
import threading
import time
import urllib.parse
import urllib.request
import uuid
from contextlib import contextmanager
from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parents[2] / "state/remote_logging.json"
MAX_EVENT_CHARS = 12000
MAX_SPOOL_BYTES = 20 * 1024 * 1024
RUN_ID = uuid.uuid4().hex
_sequence = itertools.count(1)
_sender = None
_progress = None


def publish_progress(snapshot):
    """Called on the UI thread; network work stays on the uploader thread."""
    global _progress
    _progress = {**snapshot, "session_id": RUN_ID, "ts": time.time()}


def validate_config(value):
    if not isinstance(value, dict):
        raise TypeError("设备配置应为JSON对象。")
    if not value.get("enabled"):
        return {**value, "enabled": False}
    parts = urllib.parse.urlsplit(value.get("endpoint", ""))
    if (
        parts.scheme != "https"
        or not parts.netloc
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
    ):
        raise ValueError("日志服务地址必须是HTTPS地址，不能包含账号、查询参数或片段。")
    if not re.fullmatch(r"[a-f0-9]{32}", value.get("device_id", "")):
        raise ValueError("设备编号无效，请导入网页生成的设备配置。")
    if not 32 <= len(value.get("token", "")) <= 200:
        raise ValueError("上传凭证无效。")
    return {**value, "endpoint": value["endpoint"].rstrip("/") + "/", "enabled": True}


def load_config(path=CONFIG_PATH):
    return (
        json.loads(Path(path).read_text(encoding="utf-8-sig"))
        if Path(path).is_file()
        else {"enabled": False}
    )


def save_config(value, path=CONFIG_PATH):
    value = validate_config(value)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(path)
    return value


def redact(text):
    text = str(text)[:MAX_EVENT_CHARS]
    text = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._-]+", "Bearer ***", text)
    text = re.sub(
        r"(?i)\b(api[_-]?key|authorization|password|secret|access[_-]?token)\b\s*[:=]\s*[^\s,;]+",
        r"\1=***",
        text,
    )
    return re.sub(r"\bsk-[A-Za-z0-9_-]{12,}", "***", text)


def event_from_record(record):
    message = redact(record.getMessage())
    shop = re.search(r"店铺\s*\[([^\]]{1,100})\]", message)
    prefixed = re.search(
        r"\[(发起复议|售后申诉|查询申诉结果|查询复议结果|查询售后结果)\]\[([^\]]{1,100})\]",
        message,
    )
    task = next(
        (
            name
            for name in ("负向申诉", "售后申诉", "发起复议", "复议", "结果回查")
            if name in message
        ),
        "",
    )
    if prefixed and prefixed.group(1).startswith("查询"):
        task = "结果回查"
    task = getattr(record, "remote_task", "") or task
    shop_name = getattr(record, "remote_shop", "") or (
        shop.group(1) if shop else prefixed.group(2) if prefixed else ""
    )
    level = record.levelname
    if (
        '"stage":"target_out_of_view"' in message
        or '"stage":"analysis_error"' in message
    ):
        level = "ERROR"
    if "[INPUT_INTERFERENCE]" in message or "跳过：" in message:
        level = "WARNING"
    elif record.levelno < logging.ERROR and re.search(
        r"失败\s*[:：]|错误\s*[:：]|初始化失败|分析失败|init 出错", message
    ):
        level = "ERROR"
    event = {
        "event_id": uuid.uuid4().hex,
        "session_id": RUN_ID,
        "seq": next(_sequence),
        "ts": record.created,
        "level": level,
        "shop": shop_name,
        "task": task,
        "message": message,
    }
    if (
        '"stage":"error_screenshot"' in message
        or '"stage": "error_screenshot"' in message
    ):
        try:
            diagnostic = json.loads(message[message.index("{") :])
            if re.fullmatch(r"\d{6}-\d{10,}", str(diagnostic.get("order_no", ""))):
                event.update(
                    message="订单 " + diagnostic["order_no"] + "：错误现场截图",
                    level="ERROR",
                    screenshot_path=diagnostic["screenshot_path"],
                )
        except (ValueError, KeyError, TypeError):
            pass
    return event


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None  # Never forward device credentials to a redirected URL.


class RemoteSender:
    def __init__(self, config, spool_path):
        self.config = validate_config(config)
        self.path = Path(spool_path)
        self.inbox = queue.Queue(maxsize=2000)
        self.stop_event = threading.Event()
        self.ready = threading.Event()
        self.pending = 0
        self.dropped = 0
        self.last_success = 0
        self.last_error = ""
        self.last_report = 0
        self.last_report_attempt = 0
        self.report_error = ""
        self.last_progress = 0
        self.writer = threading.Thread(
            target=self._writer, name="log-spool", daemon=True
        )
        self.uploader = threading.Thread(
            target=self._uploader, name="log-upload", daemon=True
        )

    def start(self):
        self.writer.start()
        self.uploader.start()

    def enqueue(self, event):
        try:
            self.inbox.put_nowait(event)
        except queue.Full:
            self.dropped += 1

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=5)
        try:
            with db:
                yield db
        finally:
            db.close()

    def _writer(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.connection() as db:
                db.execute("PRAGMA journal_mode=WAL")
                db.execute(
                    "CREATE TABLE IF NOT EXISTS outbox(id INTEGER PRIMARY KEY,device TEXT,event_id TEXT UNIQUE,payload TEXT,size INTEGER)"
                )
                db.execute(
                    "CREATE INDEX IF NOT EXISTS outbox_device ON outbox(device,id)"
                )
                db.execute(
                    "CREATE TABLE IF NOT EXISTS image_outbox(device TEXT,event_id TEXT,payload TEXT,size INTEGER,PRIMARY KEY(device,event_id))"
                )
                self.ready.set()
                while not self.stop_event.is_set() or not self.inbox.empty():
                    try:
                        first = self.inbox.get(timeout=0.2)
                    except queue.Empty:
                        continue
                    batch = [first]
                    while len(batch) < 100:
                        try:
                            batch.append(self.inbox.get_nowait())
                        except queue.Empty:
                            break
                    rows = []
                    for event in batch:
                        self._cache_image(db, event)
                        raw = json.dumps(
                            event, ensure_ascii=False, separators=(",", ":")
                        )
                        rows.append(
                            (
                                self.config["device_id"],
                                event["event_id"],
                                raw,
                                len(raw.encode("utf-8")),
                            )
                        )
                    with db:
                        db.executemany(
                            "INSERT OR IGNORE INTO outbox(device,event_id,payload,size) VALUES(?,?,?,?)",
                            rows,
                        )
                        size = db.execute(
                            "SELECT COALESCE(SUM(size),0) FROM outbox"
                        ).fetchone()[0]
                        while size > MAX_SPOOL_BYTES:
                            oldest = db.execute(
                                "SELECT id,size FROM outbox ORDER BY id LIMIT 100"
                            ).fetchall()
                            db.executemany(
                                "DELETE FROM outbox WHERE id=?",
                                [(row[0],) for row in oldest],
                            )
                            self.dropped += len(oldest)
                            size -= sum(row[1] for row in oldest)
        except Exception:  # noqa: BLE001 - telemetry must never interrupt orders
            self.last_error = "本地日志缓存不可用，请检查目录权限或磁盘空间。"

    def post(self, route, payload):
        raw = json.dumps(
            {"device_id": self.config["device_id"], **payload}, ensure_ascii=False
        ).encode("utf-8")
        request = urllib.request.Request(
            self.config["endpoint"] + route,
            data=raw,
            headers={
                "Authorization": "Bearer " + self.config["token"],
                "Content-Type": "application/json",
            },
        )
        with urllib.request.build_opener(NoRedirect()).open(
            request, timeout=8
        ) as response:
            data = response.read(1024 * 1024 + 1)
        if len(data) > 1024 * 1024:
            raise ValueError("日志服务响应过大。")
        return json.loads(data)

    def _uploader(self):
        while not self.ready.wait(0.2):
            if self.stop_event.is_set():
                return
        backoff = 2
        while not self.stop_event.wait(backoff):
            self._sync_report()
            self._sync_image()
            try:
                progress = _progress
                if progress is not None and time.time() - self.last_progress >= 2:
                    self.post("api/progress", {"progress": progress})
                    self.last_progress = time.time()
                with self.connection() as db:
                    rows = db.execute(
                        "SELECT event_id,payload,size FROM outbox WHERE device=? ORDER BY id LIMIT 100",
                        (self.config["device_id"],),
                    ).fetchall()
                    self.pending = db.execute(
                        "SELECT COUNT(*) FROM outbox WHERE device=?",
                        (self.config["device_id"],),
                    ).fetchone()[0]
                batch, size = [], 0
                for event_id, raw, length in rows:
                    if batch and size + length > 350 * 1024:
                        break
                    batch.append(json.loads(raw))
                    size += length
                if batch:
                    result = self.post("api/ingest", {"events": batch})
                    expected = {event["event_id"] for event in batch}
                    accepted = set(result.get("accepted_ids", [])) & expected
                    if accepted != expected:
                        raise ValueError("日志服务未确认完整批次。")
                    with self.connection() as db:
                        db.executemany(
                            "DELETE FROM outbox WHERE device=? AND event_id=?",
                            [(self.config["device_id"], value) for value in accepted],
                        )
                    self.pending = max(0, self.pending - len(accepted))
                elif time.time() - self.last_success >= 15:
                    self.post("api/heartbeat", {"session_id": RUN_ID})
                else:
                    continue
                self.last_success = time.time()
                self.last_error = ""
                backoff = 2
            except Exception as error:  # noqa: BLE001 - bounded retry, no logging recursion
                self.last_error = (
                    "日志上传暂不可用（" + type(error).__name__ + "），已保留本地缓存。"
                )
                backoff = min(30, backoff * 2)

    def _cache_image(self, db, event):
        filename = event.pop("screenshot_path", None)
        if not filename:
            return
        try:
            from .project_paths import PROJECT_PATHS

            path = Path(filename).resolve()
            if (
                not path.is_relative_to((PROJECT_PATHS.root / "debug").resolve())
                or path.suffix.lower() != ".png"
            ):
                return
            if not 0 < path.stat().st_size <= 2 * 1024 * 1024:
                return
            raw = path.read_bytes()
            payload = json.dumps(
                {
                    "event_id": event["event_id"],
                    "image": base64.b64encode(raw).decode("ascii"),
                }
            )
            db.execute(
                "INSERT OR REPLACE INTO image_outbox VALUES(?,?,?,?)",
                (self.config["device_id"], event["event_id"], payload, len(payload)),
            )
            while (
                db.execute("SELECT COALESCE(SUM(size),0) FROM image_outbox").fetchone()[
                    0
                ]
                > MAX_SPOOL_BYTES
            ):
                db.execute(
                    "DELETE FROM image_outbox WHERE rowid=(SELECT MIN(rowid) FROM image_outbox)"
                )
        except (OSError, ValueError, TypeError):
            return  # A missing screenshot must not discard the error log.

    def _sync_image(self):
        try:
            with self.connection() as db:
                row = db.execute(
                    "SELECT event_id,payload FROM image_outbox WHERE device=? ORDER BY rowid LIMIT 1",
                    (self.config["device_id"],),
                ).fetchone()
            if not row:
                return
            result = self.post("api/screenshots", json.loads(row[1]))
            if result.get("event_id") != row[0]:
                return
            with self.connection() as db:
                db.execute(
                    "DELETE FROM image_outbox WHERE device=? AND event_id=?",
                    (self.config["device_id"], row[0]),
                )
        except Exception:  # noqa: BLE001 - image upload retries independently of logs
            return

    def _build_report(self):
        from datetime import date

        from .auxiliary_queries import beijing_now, query_report

        return query_report(date(2000, 1, 1), beijing_now().date())

    def _sync_report(self):
        # Reporting has its own retry schedule. A failed progress upload, an
        # empty log queue or a previous report failure must not starve it.
        interval = 10 if self.report_error else 60
        if (
            self.last_report_attempt
            and time.time() - self.last_report_attempt < interval
        ):
            return
        self.last_report_attempt = time.time()
        previous = self.report_error
        try:
            report = self._build_report()
            self.post("api/report", {"report": report})
            self.last_report = time.time()
            self.report_error = ""
            if previous:
                self.enqueue(
                    event_from_record(
                        logging.LogRecord(
                            "remote_statistics",
                            logging.INFO,
                            "",
                            0,
                            "[统计同步] 已恢复，完整提交快照已上传。",
                            (),
                            None,
                        )
                    )
                )
        except Exception as error:  # noqa: BLE001 - independent telemetry retry
            self.report_error = "统计快照同步失败（" + type(error).__name__ + "）"
            if self.report_error != previous:
                self.enqueue(
                    event_from_record(
                        logging.LogRecord(
                            "remote_statistics",
                            logging.WARNING,
                            "",
                            0,
                            "[统计同步] "
                            + self.report_error
                            + "；日志上传继续，10秒后重试。",
                            (),
                            None,
                        )
                    )
                )
            try:
                self.post("api/report-status", {"error": self.report_error})
            except Exception:  # noqa: BLE001 - preserve the original report failure
                return

    def stop(self):
        self.stop_event.set()
        if self.writer.is_alive():
            self.writer.join(timeout=0.5)


class RemoteLogHandler(logging.Handler):
    def emit(self, record):
        if _sender is not None and getattr(record, "remote_run_event", False):
            try:
                _sender.enqueue(event_from_record(record))
            except Exception:  # noqa: BLE001, S110 - avoid logging recursion
                pass


def configure(path=CONFIG_PATH):
    global _sender
    stop()
    try:
        config = validate_config(load_config(path))
        if config.get("enabled"):
            _sender = RemoteSender(config, Path(path).parent / "remote-log-outbox.db")
            _sender.start()
    except (OSError, ValueError, TypeError):
        return False
    return True


def status():
    if _sender is None:
        return "未启用远程日志"
    if _sender.last_error:
        return _sender.last_error
    if _sender.report_error:
        return _sender.report_error
    if not _sender.last_success:
        return "正在连接日志服务器……"
    return f"最近上报：{time.strftime('%H:%M:%S', time.localtime(_sender.last_success))}；待传 {_sender.pending} 条；超限丢弃 {_sender.dropped} 条"


def stop():
    global _sender
    sender, _sender = _sender, None
    if sender:
        sender.stop()
