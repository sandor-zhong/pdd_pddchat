import json
import unittest

from tests.test_remote_logs import RelayTests


class DashboardTests(RelayTests):
    def test_daily_skipped_shops_are_not_reported_as_waiting_for_slots(self):
        import time

        self.provision()
        session = "a" * 32
        snapshot = {
            "task": "负向申诉",
            "session_id": session,
            "started": time.time() - 1,
            "progress": 0.4,
            "shops": [
                {
                    "shop": "A",
                    "total": 3,
                    "remaining": 0,
                    "terminal": True,
                    "status": "本轮已完成",
                },
                {
                    "shop": "B",
                    "total": None,
                    "terminal": False,
                    "status": "等待浏览器名额",
                },
                {
                    "shop": "C",
                    "total": None,
                    "terminal": False,
                    "status": "等待浏览器名额",
                },
            ],
        }
        self.http.post(
            "/api/progress",
            json={"device_id": self.config["device_id"], "progress": snapshot},
            headers=self.auth,
        )
        self.send(
            [
                self.event(
                    session_id="b" * 32,
                    message="[调度器] 今天已完成，跳过（不开浏览器）: C",
                ),
                self.event(
                    session_id=session,
                    ts=time.time() - 60,
                    message="[调度器] 今天已完成，跳过（不开浏览器）: C",
                ),
                self.event(
                    session_id=session,
                    message="[调度器] 今天已完成，跳过（不开浏览器）: B",
                ),
            ]
        )
        task = self.http.get("/api/progress").json()["tasks"][0]
        self.assertTrue(task["shops"][1]["terminal"])
        self.assertTrue(task["shops"][1]["skipped_today"])
        self.assertEqual(task["shops"][1]["status"], "今日已完成，本轮跳过")
        self.assertFalse(task["shops"][2]["terminal"])
        self.assertAlmostEqual(task["progress"], 0.4 + 1 / 3)

    def test_missing_report_uses_only_explicit_final_submission_receipts(self):
        self.provision()
        self.send(
            [
                self.event(
                    message="[调度器] 店铺 [A] 订单 261106-123456789: 已提交（耗时 1分）"
                ),
                self.event(
                    message="[调度器] 店铺 [A] 订单 261106-123456789: 已提交（耗时 1分）"
                ),
                self.event(
                    message="[调度器] 店铺 [A] 订单 261106-987654321: 分析失败: timeout",
                    level="ERROR",
                ),
                self.event(message="[调度器] 店铺 [A] 订单 261106-333333333: 模拟成功"),
                self.event(
                    message="[售后申诉][A] 订单 261106-222222222：已提交（耗时 8秒）",
                    task="售后申诉",
                ),
                self.event(
                    message="[发起复议][A] 订单 261106-444444444：已提交复议（耗时 8秒）",
                    task="发起复议",
                ),
                self.event(message="[流水线] 负向申诉：处理50，提交20，失败30"),
            ]
        )
        data = self.http.get("/api/stats").json()
        self.assertFalse(data["complete"])
        self.assertEqual(data["sources"][0]["mode"], "confirmed_logs")
        self.assertEqual(sum(g["total"] for g in data["groups"]), 3)
        self.assertEqual(self.http.get("/api/stats?shop=B").json()["groups"], [])
        self.assertEqual(len(self.http.get("/api/stats?kind=复议").json()["groups"]), 1)
        self.assertNotIn("123456789", json.dumps(data))

    def test_missing_data_is_unknown_and_report_error_is_visible(self):
        self.provision()
        self.assertEqual(
            self.http.post(
                "/api/report-status",
                json={"device_id": self.config["device_id"], "error": "bad"},
            ).status_code,
            401,
        )
        self.http.post(
            "/api/report-status",
            json={
                "device_id": self.config["device_id"],
                "error": "统计快照同步失败（OperationalError）",
            },
            headers=self.auth,
        )
        data = self.http.get("/api/stats").json()
        self.assertFalse(data["complete"])
        self.assertEqual(data["sources"][0]["mode"], "unavailable")
        self.assertIn("OperationalError", data["sources"][0]["error"])

    def test_snapshot_replaces_log_fallback_without_double_counting(self):
        from datetime import datetime, timedelta, timezone

        self.provision()
        self.send(
            [
                self.event(
                    message="[调度器] 店铺 [A] 订单 261106-123456789: 已提交（耗时 1分）"
                )
            ]
        )
        today = datetime.now(timezone(timedelta(hours=8))).date().isoformat()
        self.http.post(
            "/api/report",
            json={
                "device_id": self.config["device_id"],
                "report": {
                    "details": [
                        {
                            "shop": "A",
                            "business_type": "负向申诉",
                            "submission_time": today + " 10:00:00",
                            "outcome": "通过",
                        }
                    ]
                },
            },
            headers=self.auth,
        )
        data = self.http.get("/api/stats").json()
        self.assertTrue(data["complete"])
        self.assertEqual(data["groups"][0]["total"], 1)
        self.assertEqual(data["groups"][0]["approved"], 1)
        self.assertEqual(data["sources"][0]["mode"], "snapshot")

    def test_task_snapshot_and_invalid_upload(self):
        self.provision()
        snapshot = {
            "task_id": "round-a",
            "task": "负向申诉",
            "progress": 0.25,
            "shops": [{"shop": "A", "total": 20, "remaining": 15}],
        }
        body = {"device_id": self.config["device_id"], "progress": snapshot}
        self.assertEqual(self.http.post("/api/progress", json=body).status_code, 401)
        self.assertEqual(
            self.http.post("/api/progress", json=body, headers=self.auth).status_code,
            200,
        )
        snapshot["task_id"] = "round-b"
        self.http.post("/api/progress", json=body, headers=self.auth)
        tasks = self.http.get("/api/progress").json()["tasks"]
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["task_id"], "round-b")
        self.assertEqual(tasks[0]["shops"][0]["remaining"], 15)
        snapshot["progress"] = 2
        self.assertEqual(
            self.http.post("/api/progress", json=body, headers=self.auth).status_code,
            400,
        )

    def test_today_stats_excludes_yesterday_and_does_not_return_details(self):
        from datetime import datetime, timedelta, timezone

        self.provision()
        today = datetime.now(timezone(timedelta(hours=8))).date()
        rows = [
            {
                "shop": "A",
                "business_type": "负向申诉",
                "submission_time": str(today) + " 00:00:01",
                "outcome": "待审核",
            },
            {
                "shop": "A",
                "business_type": "负向申诉",
                "submission_time": str(today) + " 01:00:00",
                "outcome": "通过",
            },
            {
                "shop": "A",
                "business_type": "负向申诉",
                "submission_time": str(today - timedelta(days=1)) + " 23:59:59",
                "outcome": "未通过",
            },
        ]
        self.http.post(
            "/api/report",
            json={"device_id": self.config["device_id"], "report": {"details": rows}},
            headers=self.auth,
        )
        stats = self.http.get("/api/stats").json()
        self.assertNotIn("details", stats)
        self.assertEqual(stats["groups"][0]["total"], 2)
        self.assertEqual(stats["groups"][0]["approved"], 1)
        self.assertEqual(self.http.get("/api/stats?shop=B").json()["groups"], [])
        self.assertEqual(self.http.get("/api/stats?start=invalid").status_code, 400)
        self.assertEqual(
            self.http.get("/api/stats?start=2026-11-10&end=2026-11-01").status_code, 400
        )

    def test_order_error_persists_after_later_step(self):
        self.provision()
        session = "a" * 32
        self.send(
            [
                self.event(
                    message="订单 1234567890：上传失败：网络超时",
                    level="ERROR",
                    session_id=session,
                ),
                self.event(message="订单 1234567890：正在重试", session_id=session),
            ]
        )
        order = self.http.get("/api/orders").json()["orders"][0]
        self.assertTrue(order["has_error"])
        self.assertIn("网络超时", order["last_error"])
        self.assertLessEqual(order["first_ts"], order["ts"])

    def test_report_and_orders(self):
        self.provision()
        report = {"details": [{"order_no": "1234567890", "outcome": "通过"}]}
        self.assertEqual(
            self.http.post(
                "/api/report",
                json={"device_id": self.config["device_id"], "report": report},
            ).status_code,
            401,
        )
        self.assertEqual(
            self.http.post(
                "/api/report",
                json={"device_id": self.config["device_id"], "report": report},
                headers=self.auth,
            ).status_code,
            200,
        )
        self.assertEqual(
            self.http.get("/api/reports").json()["reports"][0]["report"], report
        )
        self.send([self.event(message="订单 1234567890：正在采集聊天")])
        self.assertEqual(
            self.http.get("/api/orders").json()["orders"][0]["order"], "1234567890"
        )

    def test_public_read_admin_write(self):
        from fastapi.testclient import TestClient

        from server.log_relay.app import create_app

        app = create_app({"db": self.tmp.name + "/public.db"})
        with TestClient(app) as http:
            for path in ("/api/logs", "/api/orders", "/api/reports", "/api/devices"):
                self.assertEqual(http.get(path).status_code, 200)
            self.assertEqual(
                http.post(
                    "/api/devices", json={"name": "x"}, headers=self.origin
                ).status_code,
                401,
            )


if __name__ == "__main__":
    unittest.main()
