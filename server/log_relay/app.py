"""Authenticated, bounded log storage and resumable SSE. Run behind HTTPS nginx."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse

STATIC = Path(__file__).parent / "static"
ID = re.compile(r"^[a-f0-9]{32}$")


def confirmed_log_submissions(events):
    """Conservative fallback: final per-order receipts, never summary counters."""
    rows = {}
    for event in events:
        text = event.get("message", "")
        negative = re.fullmatch(
            r"\[调度器\] 店铺 \[([^\]]+)\] 订单 (\d{6}-\d+): 已提交(?:（耗时[^）]*）)?",
            text.strip(),
        )
        other = re.fullmatch(
            r"\[(发起复议|售后申诉)\]\[([^\]]+)\] 订单 (\d{6}-\d+)[：:]\s*(已提交复议|已提交)(?:（耗时[^）]*）)?",
            text.strip(),
        )
        if negative:
            shop, order = negative.groups()
            kind = "负向申诉"
        elif other:
            task, shop, order, result = other.groups()
            if (task == "发起复议") != (result == "已提交复议"):
                continue
            kind = "复议" if task == "发起复议" else "售后申诉"
        else:
            continue
        key = (event["device"], shop, kind, order)
        timestamp = float(event["ts"])
        if key in rows and rows[key]["ts"] <= timestamp:
            continue
        rows[key] = {
            "device": key[0],
            "shop": shop,
            "business_type": kind,
            "order_no": order,
            "submission_time": datetime.fromtimestamp(
                timestamp, timezone(timedelta(hours=8))
            ).isoformat(),
            "outcome": "结果待确认",
            "ts": timestamp,
        }
    return list(rows.values())


def reconcile_daily_skips(task, events):
    """Compatibility for clients that leave daily-skipped shops labelled waiting."""
    skipped = set()
    for event in events:
        if (
            event.get("session_id") != task.get("session_id")
            or event.get("ts", 0) < task.get("started", 0) - 1
        ):
            continue
        match = re.fullmatch(
            r"\[调度器\] 今天已完成，跳过（不开浏览器）:\s*(.+)",
            event.get("message", "").strip(),
        )
        if match:
            skipped.update(part.strip() for part in match.group(1).split(","))
    added = 0
    for shop in task.get("shops", []):
        if (
            shop["shop"] not in skipped
            or shop.get("terminal")
            or shop.get("total") is not None
            or shop.get("status") not in ("等待浏览器名额", "等待开始")
        ):
            continue
        shop.update(
            terminal=True,
            skipped_today=True,
            total=0,
            remaining=0,
            processed=0,
            status="今日已完成，本轮跳过",
        )
        added += 1
    if added:
        task["progress"] = min(1, task.get("progress", 0) + added / len(task["shops"]))
    return task


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def password_hash(password, salt):
    return hashlib.scrypt(
        password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1
    ).hex()


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS devices(id TEXT PRIMARY KEY,name TEXT,token TEXT UNIQUE,seen REAL);
                CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY,expires REAL);
                CREATE TABLE IF NOT EXISTS logs(id INTEGER PRIMARY KEY AUTOINCREMENT,device TEXT,event_id TEXT,received REAL,payload TEXT,UNIQUE(device,event_id));
                CREATE INDEX IF NOT EXISTS log_device ON logs(device,id);
                CREATE INDEX IF NOT EXISTS log_received ON logs(received);
                CREATE TABLE IF NOT EXISTS reports(device TEXT PRIMARY KEY, updated REAL, payload TEXT);
                CREATE TABLE IF NOT EXISTS progress(device TEXT PRIMARY KEY, updated REAL, payload TEXT);
                CREATE TABLE IF NOT EXISTS report_status(device TEXT PRIMARY KEY, updated REAL, error TEXT);
                CREATE TABLE IF NOT EXISTS screenshots(device TEXT,event_id TEXT,received REAL,image BLOB,PRIMARY KEY(device,event_id));
            """)
        self.last_cleanup = 0

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def cleanup(self):
        if time.time() - self.last_cleanup < 60:
            return
        with self.connection() as db:
            db.execute("DELETE FROM sessions WHERE expires<?", (time.time(),))
            db.execute("DELETE FROM logs WHERE received<?", (time.time() - 7 * 86400,))
            db.execute(
                "DELETE FROM screenshots WHERE received<?", (time.time() - 7 * 86400,)
            )
            # 20k * <=~50KB UTF-8 payload bounds logical storage to <=1GB.
            db.execute(
                "DELETE FROM logs WHERE id <= COALESCE((SELECT id FROM logs ORDER BY id DESC LIMIT 1 OFFSET 19999),0) - 1"
            )
        self.last_cleanup = time.time()

    def snapshot(self, filters, after=None, before=None):
        clauses, args = ["1=1"], []
        for field in ("device", "shop", "task", "level"):
            value = filters.get(field, "")
            if value:
                clauses.append(
                    (
                        "device"
                        if field == "device"
                        else f"json_extract(payload,'$.{field}')"
                    )
                    + "=?"
                )
                args.append(value)
        if filters.get("q"):
            clauses.append("instr(json_extract(payload,'$.message'),?)>0")
            args.append(filters["q"])
        if after is not None:
            clauses.append("id>?")
            args.append(after)
        if before is not None:
            clauses.append("id<?")
            args.append(before)
        with self.connection() as db:
            high = db.execute("SELECT COALESCE(MAX(id),0) FROM logs").fetchone()[0]
            order = "ASC" if after is not None else "DESC"
            rows = db.execute(
                "SELECT * FROM logs WHERE "
                + " AND ".join(clauses)
                + f" ORDER BY id {order} LIMIT 200",
                args,
            ).fetchall()
        if after is None:
            rows = list(reversed(rows))
        events = [
            {
                **json.loads(row["payload"]),
                "id": row["id"],
                "device": row["device"],
                "received": row["received"],
            }
            for row in rows
        ]
        # Never advance past unread matching events when a batch is full.
        cursor = events[-1]["id"] if after is not None and len(events) == 200 else high
        return {"events": events, "cursor": cursor}


def create_app(settings=None):
    settings = settings or {
        "db": os.environ.get("LOG_DB", "/var/lib/pddchat-log-relay/logs.db"),
        "salt": os.environ["LOG_PASSWORD_SALT"],
        "password_hash": os.environ["LOG_PASSWORD_HASH"],
        "origin": os.environ.get("LOG_ORIGIN", "https://flowtry.top"),
        "endpoint": os.environ.get("LOG_ENDPOINT", "https://flowtry.top/pdd-logs/"),
    }
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    store = app.state.store = Store(settings["db"])
    attempts = {}

    @app.middleware("http")
    async def security(request, call_next):
        response = await call_next(request)
        response.headers.update(
            {
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'; base-uri 'none'",
            }
        )
        return response

    async def body(request, limit=512 * 1024):
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > limit:
                raise HTTPException(413, "请求过大")
        try:
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise TypeError()
            return value
        except (ValueError, UnicodeError, TypeError):
            raise HTTPException(400, "JSON格式错误") from None

    def viewer(request):
        if settings.get("public_view", True):
            return
        administrator(request)

    def administrator(request):
        token = request.cookies.get("pdd_logs", "")
        with store.connection() as db:
            row = db.execute(
                "SELECT expires FROM sessions WHERE token=?", (digest(token),)
            ).fetchone()
        if not row or row[0] < time.time():
            raise HTTPException(401, "请登录")

    def same_origin(request):
        if request.headers.get("origin") != settings["origin"]:
            raise HTTPException(403, "来源不允许")

    def device(request, value):
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer "):
            raise HTTPException(401)
        with store.connection() as db:
            row = db.execute(
                "SELECT id FROM devices WHERE token=?", (digest(auth[7:]),)
            ).fetchone()
        if not row or row[0] != value.get("device_id"):
            raise HTTPException(401)
        return row[0]

    def filters(request):
        return {
            key: request.query_params.get(key, "")[:200]
            for key in ("device", "shop", "task", "level", "q")
        }

    def number(value):
        try:
            return max(0, int(value))
        except (ValueError, TypeError):
            raise HTTPException(400, "游标无效") from None

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/static/{name}")
    async def asset(name: str):
        if name not in ("app.js", "style.css"):
            raise HTTPException(404)
        return FileResponse(STATIC / name)

    @app.get("/health")
    async def health():
        return {"ok": True}

    @app.post("/api/login")
    async def login(request: Request):
        same_origin(request)
        value = await body(request)
        address = request.client.host
        now = time.time()
        # Bound rate-limiter memory and reject after ten attempts/minute per IP.
        for key in list(attempts):
            if attempts[key][0] < now - 60:
                del attempts[key]
        since, count = attempts.get(address, (now, 0))
        if count >= 10 or len(attempts) > 10000:
            raise HTTPException(429, "稍后重试")
        attempts[address] = (since, count + 1)
        password = value.get("password", "")
        if (
            not isinstance(password, str)
            or len(password) > 200
            or not hmac.compare_digest(
                password_hash(password, settings["salt"]), settings["password_hash"]
            )
        ):
            raise HTTPException(401, "密码错误")
        token = secrets.token_urlsafe(32)
        store.cleanup()
        with store.connection() as db:
            db.execute("INSERT INTO sessions VALUES(?,?)", (digest(token), now + 43200))
        response = JSONResponse({"ok": True})
        response.set_cookie(
            "pdd_logs",
            token,
            max_age=43200,
            secure=True,
            httponly=True,
            samesite="strict",
            path="/pdd-logs/",
        )
        return response

    @app.post("/api/logout")
    async def logout(request: Request):
        viewer(request)
        same_origin(request)
        with store.connection() as db:
            db.execute(
                "DELETE FROM sessions WHERE token=?",
                (digest(request.cookies.get("pdd_logs", "")),),
            )
        response = JSONResponse({"ok": True})
        response.delete_cookie("pdd_logs", path="/pdd-logs/")
        return response

    @app.get("/api/devices")
    async def devices(request: Request):
        viewer(request)
        with store.connection() as db:
            return {
                "devices": [
                    dict(row)
                    for row in db.execute(
                        "SELECT id,name,seen FROM devices ORDER BY name"
                    )
                ]
            }

    @app.post("/api/devices")
    async def provision(request: Request):
        administrator(request)
        same_origin(request)
        value = await body(request)
        name = str(value.get("name", ""))[:100].strip()
        if not name:
            raise HTTPException(400, "请填写电脑名称")
        ident, token = secrets.token_hex(16), secrets.token_urlsafe(32)
        with store.connection() as db:
            db.execute(
                "INSERT INTO devices VALUES(?,?,?,0)", (ident, name, digest(token))
            )
        return {
            "enabled": True,
            "device_id": ident,
            "token": token,
            "endpoint": settings["endpoint"],
            "name": name,
        }

    @app.post("/api/heartbeat")
    async def heartbeat(request: Request):
        value = await body(request)
        ident = device(request, value)
        with store.connection() as db:
            db.execute("UPDATE devices SET seen=? WHERE id=?", (time.time(), ident))
        return {"ok": True}

    @app.post("/api/ingest")
    async def ingest(request: Request):
        value = await body(request)
        ident = device(request, value)
        events = value.get("events")
        if not isinstance(events, list) or not 1 <= len(events) <= 100:
            raise HTTPException(400, "批次无效")
        normalized = []
        for event in events:
            if not isinstance(event, dict) or not ID.fullmatch(
                str(event.get("event_id", ""))
            ):
                raise HTTPException(400, "事件编号无效")
            clean = {
                key: str(event.get(key, ""))[:limit]
                for key, limit in (
                    ("event_id", 32),
                    ("session_id", 32),
                    ("level", 16),
                    ("shop", 100),
                    ("task", 100),
                    ("message", 12000),
                )
            }
            try:
                clean["ts"] = float(event["ts"])
                clean["seq"] = max(0, int(event["seq"]))
                if not math.isfinite(clean["ts"]):
                    raise ValueError()
            except (KeyError, ValueError, TypeError, OverflowError):
                raise HTTPException(400, "时间或序号无效") from None
            normalized.append(clean)
        now = time.time()
        with store.connection() as db:
            db.executemany(
                "INSERT OR IGNORE INTO logs(device,event_id,received,payload) VALUES(?,?,?,?)",
                [
                    (ident, e["event_id"], now, json.dumps(e, ensure_ascii=False))
                    for e in normalized
                ],
            )
            db.execute("UPDATE devices SET seen=? WHERE id=?", (now, ident))
        store.cleanup()
        return {"accepted_ids": [e["event_id"] for e in normalized]}

    @app.post("/api/report")
    async def upload_report(request: Request):
        value = await body(request, 16 * 1024 * 1024)
        ident = device(request, value)
        report = value.get("report")
        if not isinstance(report, dict) or not isinstance(report.get("details"), list):
            raise HTTPException(400, "查询快照无效")
        with store.connection() as db:
            db.execute(
                "INSERT OR REPLACE INTO reports VALUES(?,?,?)",
                (ident, time.time(), json.dumps(report, ensure_ascii=False)),
            )
            db.execute("DELETE FROM report_status WHERE device=?", (ident,))
        return {"ok": True}

    @app.post("/api/screenshots")
    async def upload_screenshot(request: Request):
        value = await body(request, 3 * 1024 * 1024)
        ident = device(request, value)
        event_id = value.get("event_id", "")
        if not isinstance(event_id, str) or not ID.fullmatch(event_id):
            raise HTTPException(400, "截图编号无效")
        try:
            raw = base64.b64decode(value.get("image", ""), validate=True)
            if (
                not 33 <= len(raw) <= 2 * 1024 * 1024
                or raw[:8] != b"\x89PNG\r\n\x1a\n"
                or raw[12:16] != b"IHDR"
            ):
                raise ValueError()
            width, height = (
                int.from_bytes(raw[16:20], "big"),
                int.from_bytes(raw[20:24], "big"),
            )
            if (
                not 0 < width <= 16000
                or not 0 < height <= 16000
                or width * height > 40000000
            ):
                raise ValueError()
        except (ValueError, TypeError):
            raise HTTPException(400, "仅支持2MB以内的有效PNG截图") from None
        with store.connection() as db:
            db.execute(
                "INSERT OR REPLACE INTO screenshots VALUES(?,?,?,?)",
                (ident, event_id, time.time(), raw),
            )
            while (
                db.execute(
                    "SELECT COALESCE(SUM(length(image)),0) FROM screenshots"
                ).fetchone()[0]
                > 100 * 1024 * 1024
                or db.execute("SELECT COUNT(*) FROM screenshots").fetchone()[0] > 100
            ):
                db.execute(
                    "DELETE FROM screenshots WHERE rowid=(SELECT rowid FROM screenshots ORDER BY received LIMIT 1)"
                )
        return {"event_id": event_id}

    @app.get("/api/screenshots/{ident}/{event_id}")
    async def screenshot(request: Request, ident: str, event_id: str):
        viewer(request)
        if not ID.fullmatch(ident) or not ID.fullmatch(event_id):
            raise HTTPException(404)
        store.cleanup()
        with store.connection() as db:
            row = db.execute(
                "SELECT image FROM screenshots WHERE device=? AND event_id=?",
                (ident, event_id),
            ).fetchone()
        if not row:
            raise HTTPException(404, "截图不存在或已过期")
        return Response(
            bytes(row["image"]),
            media_type="image/png",
            headers={
                "Content-Disposition": 'inline; filename="error-screenshot.png"',
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.post("/api/report-status")
    async def report_status(request: Request):
        value = await body(request)
        ident = device(request, value)
        error = value.get("error")
        if not isinstance(error, str) or len(error) > 300:
            raise HTTPException(400, "统计同步状态无效")
        with store.connection() as db:
            db.execute(
                "INSERT OR REPLACE INTO report_status VALUES(?,?,?)",
                (ident, time.time(), error),
            )
        return {"ok": True}

    @app.get("/api/reports")
    async def reports(request: Request):
        viewer(request)
        with store.connection() as db:
            return {
                "reports": [
                    {
                        "device": row["device"],
                        "updated": row["updated"],
                        "report": json.loads(row["payload"]),
                    }
                    for row in db.execute("SELECT * FROM reports")
                ]
            }

    @app.get("/api/orders")
    async def orders(request: Request):
        viewer(request)
        store.cleanup()
        selected = filters(request)
        with store.connection() as db:
            rows = db.execute(
                "SELECT id,device,payload FROM logs ORDER BY id"
            ).fetchall()
            images = {
                (r["device"], r["event_id"])
                for r in db.execute("SELECT device,event_id FROM screenshots")
            }
        grouped = {}
        for row in rows:
            e = json.loads(row["payload"])
            e.update(id=row["id"], device=row["device"])
            if any(
                selected.get(k) and e.get(k) != selected[k]
                for k in ("device", "shop", "task")
            ):
                continue
            match = re.search(r"(?:订单(?:号)?)[\s:：\[【]*(\d[\d-]{8,})", e["message"])
            if not match:
                continue
            order = match.group(1)
            key = (
                e["device"],
                e.get("session_id"),
                e.get("shop"),
                e.get("task"),
                order,
            )
            item = grouped.setdefault(
                key,
                {
                    "order": order,
                    "events": [],
                    "first_ts": e["ts"],
                    "has_error": False,
                    "last_error": "",
                    "screenshots": [],
                },
            )
            item.update(
                {
                    k: e.get(k, "")
                    for k in ("device", "shop", "task", "ts", "message", "session_id")
                }
            )
            item["events"].append(e)
            if (e["device"], e["event_id"]) in images:
                item["screenshots"].append(
                    {
                        "url": "api/screenshots/" + e["device"] + "/" + e["event_id"],
                        "ts": e["ts"],
                    }
                )
            if e.get("level") == "ERROR" or re.search(
                r"申诉失败|提交失败|处理失败|初始化失败|失败\s*[:：]|[:：]\s*失败(?:$|[，,。])",
                e["message"],
            ):
                item["has_error"] = True
                item["last_error"] = e["message"]
        return {
            "orders": sorted(grouped.values(), key=lambda e: e["ts"], reverse=True),
            "scope": "最近7天且最多20000条日志；仅展示日志明确关联订单的步骤",
        }

    @app.post("/api/progress")
    async def upload_progress(request: Request):
        value = await body(request)
        ident = device(request, value)
        snapshot = value.get("progress")
        if not isinstance(snapshot, dict) or not isinstance(
            snapshot.get("shops"), list
        ):
            raise HTTPException(400, "任务进度无效")
        if len(snapshot["shops"]) > 1000 or any(
            not isinstance(s, dict) or not isinstance(s.get("shop"), str)
            for s in snapshot["shops"]
        ):
            raise HTTPException(400, "店铺进度无效")
        try:
            fraction = float(snapshot["progress"])
            if not math.isfinite(fraction) or not 0 <= fraction <= 1:
                raise ValueError()
        except (KeyError, ValueError, TypeError):
            raise HTTPException(400, "进度百分比无效") from None
        with store.connection() as db:
            db.execute(
                "INSERT OR REPLACE INTO progress VALUES(?,?,?)",
                (ident, time.time(), json.dumps(snapshot, ensure_ascii=False)),
            )
        return {"ok": True}

    @app.get("/api/progress")
    async def progress(request: Request):
        viewer(request)
        with store.connection() as db:
            tasks = []
            for row in db.execute("SELECT * FROM progress").fetchall():
                task = {
                    "device": row["device"],
                    "updated": row["updated"],
                    **json.loads(row["payload"]),
                }
                if (
                    task.get("task") == "负向申诉"
                    and task.get("session_id")
                    and isinstance(task.get("started"), (float, int))
                ):
                    events = [
                        json.loads(r["payload"])
                        for r in db.execute(
                            "SELECT payload FROM logs WHERE device=? AND json_extract(payload,'$.session_id')=? AND json_extract(payload,'$.ts')>=? AND instr(json_extract(payload,'$.message'),'今天已完成，跳过（不开浏览器）')>0",
                            (row["device"], task["session_id"], task["started"] - 1),
                        )
                    ]
                    reconcile_daily_skips(task, events)
                tasks.append(task)
            return {"tasks": tasks}

    @app.get("/api/stats")
    async def stats(request: Request):
        viewer(request)
        store.cleanup()
        today = datetime.now(timezone(timedelta(hours=8))).date().isoformat()
        start = request.query_params.get("start") or today
        end = request.query_params.get("end") or today
        try:
            if date.fromisoformat(start) > date.fromisoformat(end):
                raise ValueError()
        except ValueError:
            raise HTTPException(400, "日期范围无效") from None
        selected = filters(request)
        kind = request.query_params.get("kind", "")
        groups, sources = {}, []
        with store.connection() as db:
            snapshots = db.execute("SELECT * FROM reports").fetchall()
            devices = db.execute("SELECT id,seen FROM devices").fetchall()
            errors = {
                r["device"]: r["error"]
                for r in db.execute("SELECT * FROM report_status")
            }
            log_rows = db.execute(
                "SELECT device,payload FROM logs ORDER BY id"
            ).fetchall()
        fallback = confirmed_log_submissions(
            [{**json.loads(r["payload"]), "device": r["device"]} for r in log_rows]
        )
        available = {r["device"] for r in snapshots}
        for entry in devices:
            ident = entry["id"]
            if ident in available or (
                selected.get("device") and ident != selected["device"]
            ):
                continue
            details = [r for r in fallback if r["device"] == ident]
            sources.append(
                {
                    "device": ident,
                    "updated": None,
                    "mode": "confirmed_logs" if details else "unavailable",
                    "error": errors.get(ident, ""),
                    "complete": False,
                }
            )
            snapshots.append(
                {
                    "device": ident,
                    "updated": None,
                    "payload": json.dumps({"details": details}),
                }
            )
        for snapshot in snapshots:
            ident = snapshot["device"]
            if selected.get("device") and ident != selected["device"]:
                continue
            if snapshot["updated"] is not None:
                sources.append(
                    {
                        "device": ident,
                        "updated": snapshot["updated"],
                        "mode": "snapshot",
                        "error": errors.get(ident, ""),
                        "complete": time.time() - snapshot["updated"] <= 120
                        and not errors.get(ident),
                    }
                )
            for row in json.loads(snapshot["payload"])["details"]:
                day = str(row.get("submission_time", ""))[:10]
                if (
                    not start <= day <= end
                    or (selected.get("shop") and row.get("shop") != selected["shop"])
                    or (kind and row.get("business_type") != kind)
                ):
                    continue
                key = (ident, row.get("shop", ""), row.get("business_type", ""))
                group = groups.setdefault(
                    key,
                    {
                        "device": ident,
                        "shop": key[1],
                        "kind": key[2],
                        "total": 0,
                        "approved": 0,
                        "partial": 0,
                        "rejected": 0,
                        "waiting": 0,
                        "unknown": 0,
                        "withdrawn": 0,
                    },
                )
                group["total"] += 1
                field = {
                    "通过": "approved",
                    "部分通过": "partial",
                    "未通过": "rejected",
                    "结果待确认": "unknown",
                    "已撤回": "withdrawn",
                }.get(row.get("outcome"), "waiting")
                group[field] += 1
        return {
            "today": today,
            "start": start,
            "end": end,
            "groups": list(groups.values()),
            "sources": sources,
            "complete": bool(sources) and all(s["complete"] for s in sources),
        }

    @app.get("/api/logs")
    async def logs(request: Request):
        viewer(request)
        store.cleanup()
        before = request.query_params.get("before")
        return store.snapshot(
            filters(request), before=number(before) if before else None
        )

    @app.get("/api/stream")
    async def stream(request: Request):
        viewer(request)
        cursor = number(
            request.headers.get("last-event-id")
            or request.query_params.get("after", "0")
        )
        selected = filters(request)

        async def generate():
            nonlocal cursor
            ping = 0
            yield "retry: 2000\n\n"
            while not await request.is_disconnected():
                try:
                    viewer(request)
                except HTTPException:
                    yield "event: expired\ndata: {}\n\n"
                    return
                store.cleanup()
                result = store.snapshot(selected, after=cursor)
                cursor = result["cursor"]
                if result["events"] or time.monotonic() - ping >= 15:
                    yield f"id: {cursor}\nevent: logs\ndata: {json.dumps(result, ensure_ascii=False)}\n\n"
                    ping = time.monotonic()
                await asyncio.sleep(1)

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
        )

    return app
