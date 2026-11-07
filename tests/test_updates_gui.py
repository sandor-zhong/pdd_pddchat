import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import shutil
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.error import HTTPError

from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication, QMainWindow

import update_core as core
from gui.app.dialogs.update_dialog import UpdateDialog
from gui.app.services import app_updates


class UpdateGuiTests(unittest.TestCase):
    def test_updated_agent_is_used_only_when_both_managed_files_match(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "app"
            root.mkdir()
            files = {}
            for name in ("update_core.py", "update_agent.py"):
                (root / name).write_bytes(b"managed fixture")
                files[name] = core.sha256_file(root / name)
            with patch.object(core, "installed_release", return_value={"files": files}):
                self.assertEqual(app_updates.agent_source(root), root)
                (root / "update_agent.py").write_bytes(b"modified fixture")
                self.assertEqual(
                    app_updates.agent_source(root), root.parent / "updater"
                )

    def server_manifest(self):
        from tests.test_updates import release

        return {
            "schema_version": 1,
            "app_id": core.APP_ID,
            "channel": "stable",
            "version": "0.1.1",
            "build": "200",
            "release": release({"main.py": b"new"}, "200"),
            "full": {
                "url": core.BASE_URL + "packages/setup.exe",
                "size": 1,
                "sha256": "b" * 64,
            },
            "incremental": [],
        }

    def test_client_falls_back_to_legacy_feed_only_on_missing_v2(self):
        calls = []
        latest = self.server_manifest()

        def download(url, target, **kwargs):
            calls.append(url)
            if url.endswith(("latest-v3.json", "latest-v2.json")):
                raise HTTPError(url, 404, "missing", {}, None)
            Path(target).write_bytes(
                b"sig" if url.endswith(".sig") else core.dumps(latest)
            )

        with (
            TemporaryDirectory() as temporary,
            patch.object(core, "installed_layout", return_value=True),
            patch.object(core, "download", side_effect=download),
            patch.object(core, "verify_signature"),
            patch.object(core, "select_artifact", return_value=(None, None)),
        ):
            root = Path(temporary) / "app"
            root.mkdir()
            (root / "update_public_key.json").write_bytes(b"public")
            result = app_updates.check(root)
        self.assertFalse(result["available"])
        self.assertTrue(calls[0].endswith("latest-v3.json"))
        self.assertTrue(calls[1].endswith("latest-v2.json"))
        self.assertTrue(calls[2].endswith("latest.json"))

    def test_unmatched_incremental_client_gets_manual_install_message(self):
        latest = self.server_manifest()
        latest.update(
            publish_mode="incremental",
            full=None,
            incremental=[
                {
                    "from_build": "100",
                    "base_manifest_sha256": "a" * 64,
                    "url": core.BASE_URL + "incremental/patch.zip",
                    "sha256": "b" * 64,
                    "size": 1,
                }
            ],
        )

        def download(url, target, **kwargs):
            Path(target).write_bytes(
                b"sig" if url.endswith(".sig") else core.dumps(latest)
            )

        with (
            TemporaryDirectory() as temporary,
            patch.object(core, "installed_layout", return_value=True),
            patch.object(core, "download", side_effect=download),
            patch.object(core, "verify_signature"),
            patch.object(core, "select_artifact", return_value=("manual", None)),
        ):
            root = Path(temporary) / "app"
            root.mkdir()
            (root / "update_public_key.json").write_bytes(b"public")
            result = app_updates.check(root)
        self.assertFalse(result["available"])
        self.assertIn("完整安装包", result["message"])

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def dialog(self, *, automatic=False, busy=""):
        parent = QMainWindow()
        self.addCleanup(parent.deleteLater)
        with patch("gui.app.dialogs.update_dialog.QTimer.singleShot"):
            dialog = UpdateDialog(
                Path("not-installed"), parent, lambda: busy, automatic=automatic
            )
        self.addCleanup(dialog.deleteLater)
        return dialog

    def plan(self):
        return {
            "available": True,
            "mode": "incremental",
            "latest": {"version": "0.1.2", "build": "200", "release_notes": ["new"]},
            "artifact": {"size": 123},
            "folder": "unused",
        }

    def test_background_update_never_opens_dialog_over_running_tasks(self):
        dialog = self.dialog(automatic=True, busy="申诉运行中")
        dialog._checked(self.plan())
        self.assertFalse(dialog.isVisible())
        self.assertIn("发现软件新版本", dialog.parent().statusBar().currentMessage())

    def test_busy_state_prevents_download(self):
        dialog = self.dialog(busy="申诉运行中")
        dialog.plan = self.plan()
        with (
            patch("gui.app.dialogs.update_dialog.msgbox.information") as info,
            patch.object(app_updates, "prepare") as prepare,
        ):
            dialog._download()
        prepare.assert_not_called()
        info.assert_called_once()

    def test_task_started_during_download_prevents_agent_launch(self):
        dialog = self.dialog(busy="任务开始运行")
        with (
            patch.object(dialog, "_run") as start,
            patch("gui.app.dialogs.update_dialog.msgbox.warning"),
        ):
            dialog._downloaded(self.plan())
        start.assert_not_called()

    def test_check_runs_off_gui_thread_and_reports_completion(self):
        dialog = self.dialog()
        with patch.object(
            app_updates,
            "check",
            return_value={"available": False, "message": "当前已是最新构建。"},
        ):
            dialog._run(lambda: app_updates.check(dialog.root), dialog._checked)
            task = dialog.task
            self.assertTrue(task.wait(3000))
            QTest.qWait(30)
        self.assertIn("最新构建", dialog.status.text())
        self.assertFalse(dialog.install.isEnabled())

    def test_source_tree_is_not_eligible_for_in_place_updates(self):
        with (
            TemporaryDirectory() as temporary,
            patch.object(core, "download") as download,
        ):
            result = app_updates.check(Path(temporary))
        self.assertFalse(result["available"])
        download.assert_not_called()

    def test_isolated_runtime_can_start_updater_without_installed_runtime_imports(self):
        project = Path(__file__).resolve().parents[1]
        source = project / "packaging/stage/runtime/gui"
        if not (source / "python.exe").is_file():
            self.skipTest("packaged Windows runtime not available")
        with TemporaryDirectory() as temporary:
            folder = Path(temporary)
            app_updates.copy_helper_runtime(source, folder / "runtime")
            for name in ("update_core.py", "update_agent.py"):
                shutil.copy2(project / name, folder / name)
            result = subprocess.run(
                [
                    str(folder / "runtime/python.exe"),
                    "-B",
                    str(folder / "update_agent.py"),
                    "--help",
                ],
                capture_output=True,
                timeout=20,
                check=True,
            )
            self.assertIn(b"--job", result.stdout)


if __name__ == "__main__":
    unittest.main()
