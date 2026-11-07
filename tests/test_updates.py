import hashlib
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import update_agent
import update_core as core
from update_agent import claim, recover_pending


def release(files, build="100"):
    return {
        "schema_version": 1,
        "app_id": core.APP_ID,
        "version": "0.1.1",
        "build": build,
        "runtime_id": "a" * 64,
        "files": {n: hashlib.sha256(v).hexdigest() for n, v in files.items()},
    }


class UpdatesTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.install = Path(self.temporary.name)
        self.root = self.install / "app"
        self.root.mkdir()
        self.old_files = {
            "main.py": b"old = 1\n",
            "old.py": b"obsolete\n",
            "gui/app/main.py": b"gui = 1\n",
        }
        self.new_files = {
            "main.py": b"new = 2\n",
            "new.py": b"added\n",
            "gui/app/main.py": b"gui = 1\n",
        }
        for name, value in self.old_files.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(value)
        self.old, self.new = release(self.old_files), release(self.new_files, "200")
        core.write_json(self.root / "app_release.json", self.old)
        self.protected = [
            ".env",
            "state/pddchat.db",
            "state/profiles/shop/cookie",
            "gui/state/shop_accounts.db",
            "gui/config.json",
            "evidence/proof.png",
        ]
        for name in self.protected:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"keep-business-data")
        self.package = self.install / "patch.zip"
        self.make_package()

    def make_package(self, extra=None):
        changed = {
            n: v for n, v in self.new_files.items() if self.old_files.get(n) != v
        }
        removed = sorted(set(self.old_files) - set(self.new_files))
        with zipfile.ZipFile(
            self.package, "w", compression=zipfile.ZIP_DEFLATED
        ) as archive:
            archive.writestr(
                "patch.json",
                core.dumps(
                    {"from_build": "100", "release": self.new, "delete": removed}
                ),
            )
            for name, value in changed.items():
                archive.writestr("files/" + name, value)
            if extra:
                archive.writestr(extra, b"bad")
        self.entry = {
            "from_build": "100",
            "base_manifest_sha256": core.sha256_file(self.root / "app_release.json"),
            "url": core.BASE_URL + "incremental/test.zip",
            "sha256": core.sha256_file(self.package),
            "size": self.package.stat().st_size,
        }
        self.latest = {
            "schema_version": 1,
            "app_id": core.APP_ID,
            "channel": "stable",
            "version": "0.1.1",
            "build": "200",
            "release": self.new,
            "incremental": [self.entry],
            "full": {
                "url": core.BASE_URL + "packages/setup.exe",
                "sha256": "b" * 64,
                "size": 100,
            },
        }

    def test_incremental_only_matches_base_without_full_installer(self):
        latest = {**self.latest, "publish_mode": "incremental", "full": None}
        core.validate_latest(latest)
        self.assertEqual(
            core.select_artifact(self.root, latest), ("incremental", self.entry)
        )

    def test_incremental_only_unmatched_or_modified_client_needs_manual_install(self):
        latest = {**self.latest, "publish_mode": "incremental", "full": None}
        (self.root / "main.py").write_bytes(b"locally changed")
        self.assertEqual(core.select_artifact(self.root, latest), ("manual", None))

    def test_incremental_only_cannot_have_empty_patches_or_silent_full_fallback(self):
        for changes in (
            {"incremental": [], "full": None},
            {"full": self.latest["full"]},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                core.validate_latest(
                    {**self.latest, "publish_mode": "incremental", **changes}
                )

    def test_patch_then_rollback_preserves_business_data(self):
        staging = self.install / "staging"
        changed, removed = core.prepare_patch(
            self.root, self.package, self.entry, self.latest, staging
        )
        transaction = self.install / "transaction"
        core.begin_transaction(
            self.root, staging, changed, removed, self.new, transaction
        )
        self.assertFalse((self.root / "old.py").exists())
        self.assertEqual((self.root / "new.py").read_bytes(), b"added\n")
        self.assertEqual(core.installed_release(self.root)["build"], "200")
        for name in self.protected:
            self.assertEqual((self.root / name).read_bytes(), b"keep-business-data")
        core.rollback(transaction)
        core.rollback(
            transaction
        )  # Recovery is repeatable after a second interruption.
        self.assertFalse((self.root / "new.py").exists())
        for name, value in self.old_files.items():
            self.assertEqual((self.root / name).read_bytes(), value)
        self.assertEqual(core.installed_release(self.root)["build"], "100")

    def test_successful_transaction_is_committed(self):
        staging = self.install / "staging"
        changed, removed = core.prepare_patch(
            self.root, self.package, self.entry, self.latest, staging
        )
        core.begin_transaction(
            self.root, staging, changed, removed, self.new, self.install / "tx"
        )
        core.commit(self.install / "tx")
        self.assertEqual(
            core.loads((self.install / "tx/journal.json").read_bytes())["phase"],
            "committed",
        )

    def test_mid_apply_failure_rolls_back(self):
        staging = self.install / "staging"
        changed, removed = core.prepare_patch(
            self.root, self.package, self.entry, self.latest, staging
        )
        original = Path.replace

        def replace(path, target):
            if path.name == "new.py.update-tmp":
                raise PermissionError("locked")
            return original(path, target)

        with patch.object(Path, "replace", replace), self.assertRaises(PermissionError):
            core.begin_transaction(
                self.root, staging, changed, removed, self.new, self.install / "tx"
            )
        self.assertEqual(
            (self.root / "main.py").read_bytes(), self.old_files["main.py"]
        )
        self.assertTrue((self.root / "old.py").is_file())

    def test_wrong_base_and_modified_local_source_fail_closed(self):
        self.entry["from_build"] = "101"
        with self.assertRaisesRegex(ValueError, "版本不匹配"):
            core.prepare_patch(
                self.root,
                self.package,
                self.entry,
                self.latest,
                self.install / "stage1",
            )
        self.entry["from_build"] = "100"
        (self.root / "main.py").write_text("local modification")
        self.assertEqual(core.select_artifact(self.root, self.latest)[0], "full")
        with self.assertRaisesRegex(ValueError, "文件或运行时不匹配"):
            core.prepare_patch(
                self.root,
                self.package,
                self.entry,
                self.latest,
                self.install / "stage2",
            )

    def test_matching_release_selects_incremental_but_runtime_change_selects_full(self):
        self.assertEqual(core.select_artifact(self.root, self.latest)[0], "incremental")
        self.latest["release"]["runtime_id"] = "c" * 64
        self.assertEqual(core.select_artifact(self.root, self.latest)[0], "full")

    def test_zip_unexpected_paths_are_rejected_without_changing_install(self):
        self.make_package(extra="files/../../state/pddchat.db")
        with self.assertRaisesRegex(ValueError, "文件集合不匹配"):
            core.prepare_patch(
                self.root, self.package, self.entry, self.latest, self.install / "stage"
            )
        self.assertEqual(
            (self.root / "state/pddchat.db").read_bytes(), b"keep-business-data"
        )

    def test_protected_and_windows_unsafe_paths_are_rejected(self):
        for name in [
            "../main.py",
            "/main.py",
            "gui/app/../../state/x.py",
            "gui/app/STATE/x.json",
            "gui/config.json",
            "gui/state/x.json",
            ".env",
            "state/x.py",
            "gui/app/CON.py",
            "gui/app/a.py:stream",
            "gui\\app\\x.py",
            "gui/app/x.py.",
            "runtime/gui/x.py",
        ]:
            with self.subTest(name=name), self.assertRaises(ValueError):
                core.program_path(name)

    def test_duplicate_and_case_colliding_manifest_paths_are_rejected(self):
        with self.assertRaises(ValueError):
            core.loads(b'{"build":1,"build":2}')
        self.new["files"]["MAIN.py"] = "c" * 64
        with self.assertRaises(ValueError):
            core.validate_release(self.new)

    def test_unexpected_local_new_file_is_not_overwritten(self):
        (self.root / "new.py").write_text("local custom file")
        with self.assertRaisesRegex(ValueError, "本地文件占用"):
            core.prepare_patch(
                self.root, self.package, self.entry, self.latest, self.install / "stage"
            )

    def test_update_url_limits(self):
        for value in [
            "http://flowtry.top/updates/pdd_appeal/x",
            "https://other.test/x",
            core.BASE_URL + "../x",
            core.BASE_URL + "%2e%2e/x",
            core.BASE_URL + "x#fragment",
            "https://flowtry.top.evil.test/updates/pdd_appeal/x",
        ]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                core.allowed_url(value)

    def test_download_bad_hash_never_publishes_final_file(self):
        response = MagicMock()
        response.__enter__.return_value = response
        response.url = core.BASE_URL + "incremental/a.zip"
        response.read.side_effect = [b"bad", b""]
        with patch.object(core.urllib.request, "build_opener") as opener:
            opener.return_value.open.return_value = response
            with self.assertRaisesRegex(ValueError, "SHA256"):
                core.download(
                    response.url,
                    self.install / "download.zip",
                    limit=100,
                    expected_sha="0" * 64,
                )
        self.assertFalse((self.install / "download.zip").exists())
        self.assertFalse((self.install / "download.zip.part").exists())

    def test_recovery_rolls_back_only_uncommitted_transactions(self):
        folder = self.install / "update-cache/job"
        folder.mkdir(parents=True)
        changed, removed = core.prepare_patch(
            self.root, self.package, self.entry, self.latest, folder / "staging"
        )
        core.begin_transaction(
            self.root,
            folder / "staging",
            changed,
            removed,
            self.new,
            folder / "transaction",
        )
        core.write_json(
            self.install / "update-cache/active.json",
            {"pid": 999999, "job_dir": str(folder)},
        )
        with patch.object(core, "process_alive", return_value=False):
            recover_pending(self.root)
        self.assertEqual(core.installed_release(self.root)["build"], "100")
        self.assertFalse((self.install / "update-cache/active.json").exists())

    def test_live_update_blocks_second_launch(self):
        core.write_json(
            self.install / "update-cache/active.json",
            {"pid": 42, "job_dir": str(self.install / "update-cache/job")},
        )
        with (
            patch.object(core, "process_alive", return_value=True),
            self.assertRaisesRegex(RuntimeError, "正在更新"),
        ):
            recover_pending(self.root)

    def test_live_operation_lock_is_not_removed_by_updater(self):
        lock = self.root / "state/scheduler.lock"
        core.write_json(lock, {"pid": 42, "lock_id": "business-task"})
        with (
            patch.object(core, "process_alive", return_value=True),
            self.assertRaises(RuntimeError),
        ):
            claim(lock, {"pid": os.getpid()})
        self.assertEqual(core.loads(lock.read_bytes())["lock_id"], "business-task")

    def agent_job(self):
        runtime = self.install / "runtime/gui"
        runtime.mkdir(parents=True)
        (runtime / "pythonw.exe").write_bytes(b"fixture")
        (self.root / "update_public_key.json").write_text("{}")
        folder = self.install / "update-cache/job"
        folder.mkdir(parents=True)
        import shutil

        shutil.copy2(self.package, folder / "package.zip")
        core.write_json(folder / "latest.json", self.latest)
        (folder / "latest.json.sig").write_bytes(b"fixture signature")
        core.write_json(
            folder / "job.json",
            {
                "root": str(self.root),
                "parent_pid": 1234,
                "mode": "incremental",
                "artifact": self.entry,
            },
        )
        return folder

    def test_agent_commits_only_after_new_gui_acknowledges_startup(self):
        folder = self.agent_job()
        process = MagicMock()
        process.pid = 4321
        process.poll.return_value = None

        def launch(root, **options):
            core.write_json(options["health_file"], {"token": options["token"]})
            return process

        with (
            patch.object(core, "verify_signature") as verify,
            patch.object(core, "process_alive", return_value=False),
            patch.object(
                update_agent.subprocess, "run", return_value=MagicMock(returncode=0)
            ),
            patch.object(update_agent, "launch_gui", side_effect=launch),
        ):
            update_agent.run(folder / "job.json")
        verify.assert_called_once()
        self.assertEqual(core.installed_release(self.root)["build"], "200")
        self.assertEqual(
            core.loads((folder / "transaction/journal.json").read_bytes())["phase"],
            "committed",
        )
        self.assertFalse((self.install / "update-cache/active.json").exists())
        self.assertFalse((self.root / "state/scheduler.lock").exists())

    def test_agent_startup_failure_restores_old_build_and_restarts_old_gui(self):
        folder = self.agent_job()
        failed_process = MagicMock()
        failed_process.pid = 4321
        failed_process.poll.return_value = 1
        with (
            patch.object(core, "verify_signature"),
            patch.object(core, "process_alive", return_value=False),
            patch.object(
                update_agent.subprocess, "run", return_value=MagicMock(returncode=0)
            ),
            patch.object(
                update_agent, "launch_gui", side_effect=[failed_process, MagicMock()]
            ) as launch,
            self.assertRaisesRegex(RuntimeError, "启动确认"),
        ):
            update_agent.run(folder / "job.json")
        self.assertEqual(launch.call_count, 2)
        self.assertEqual(core.installed_release(self.root)["build"], "100")
        self.assertEqual(
            (self.root / "main.py").read_bytes(), self.old_files["main.py"]
        )
        self.assertFalse((self.root / "new.py").exists())

    def test_agent_invalid_signature_never_touches_installation(self):
        folder = self.agent_job()
        with (
            patch.object(
                core, "verify_signature", side_effect=ValueError("invalid signature")
            ),
            self.assertRaisesRegex(ValueError, "invalid signature"),
        ):
            update_agent.run(folder / "job.json")
        self.assertEqual(core.installed_release(self.root)["build"], "100")
        self.assertFalse((folder / "transaction").exists())

    def test_startup_timeout_terminates_new_gui_and_rolls_back(self):
        folder = self.agent_job()
        process = MagicMock(pid=4321)
        process.poll.return_value = None
        with (
            patch.object(core, "verify_signature"),
            patch.object(core, "process_alive", return_value=False),
            patch.object(
                update_agent.subprocess, "run", return_value=MagicMock(returncode=0)
            ),
            patch.object(
                update_agent, "launch_gui", side_effect=[process, MagicMock()]
            ) as launch,
            patch.object(
                update_agent,
                "wait_for_startup",
                side_effect=RuntimeError("新版启动确认超时"),
            ),
            self.assertRaisesRegex(RuntimeError, "启动确认超时"),
        ):
            update_agent.run(folder / "job.json")
        process.terminate.assert_called_once()
        self.assertEqual(launch.call_count, 2)
        self.assertEqual(core.installed_release(self.root)["build"], "100")
        self.assertEqual(
            core.loads((folder / "transaction/journal.json").read_bytes())["phase"],
            "rolled_back",
        )

    def test_failed_rollback_retains_gate_for_next_launch_recovery(self):
        folder = self.agent_job()
        failed_process = MagicMock()
        failed_process.pid, failed_process.poll.return_value = 4321, 1
        with (
            patch.object(core, "verify_signature"),
            patch.object(core, "process_alive", return_value=False),
            patch.object(
                update_agent.subprocess, "run", return_value=MagicMock(returncode=0)
            ),
            patch.object(update_agent, "launch_gui", return_value=failed_process),
            patch.object(
                core, "rollback", side_effect=PermissionError("locked during rollback")
            ),
            self.assertRaises(PermissionError),
        ):
            update_agent.run(folder / "job.json")
        marker = self.install / "update-cache/active.json"
        self.assertTrue(marker.is_file())
        self.assertEqual(core.loads(marker.read_bytes())["phase"], "recovery_required")
        with patch.object(core, "process_alive", return_value=False):
            recover_pending(self.root)
        self.assertEqual(core.installed_release(self.root)["build"], "100")
        self.assertFalse(marker.exists())


@unittest.skipUnless(os.name == "nt", "Windows CNG")
class SignatureTests(unittest.TestCase):
    def test_windows_signature_and_tamper_rejection(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "release_tools_for_test",
            Path(__file__).resolve().parents[1] / "packaging/release_tools.py",
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            private, public, latest = (
                root / "private.xml",
                root / "public.json",
                root / "latest.json",
            )
            module.init_key(private, public)
            latest.write_bytes(b'{"test":true}\n')
            signature = module.sign(latest, private, public)
            core.verify_signature(
                latest.read_bytes(), signature.read_bytes(), public.read_bytes()
            )
            with self.assertRaises(ValueError):
                core.verify_signature(
                    b"tampered", signature.read_bytes(), public.read_bytes()
                )

    def test_release_builder_generates_verifiable_full_and_incremental_artifacts(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "release_tools_builder_test",
            Path(__file__).resolve().parents[1] / "packaging/release_tools.py",
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            private, public = root / "private.xml", root / "public.json"
            module.init_key(private, public)
            stage, snapshots = root / "stage/app", root / "releases"
            stage.mkdir(parents=True)
            (stage / "main.py").write_text("old = 1")
            (root / "release_config.json").write_text('{"version":"0.1.1"}')
            installer = root / "setup.exe"
            installer.write_bytes(b"installer fixture")
            original_sign = module.sign
            with (
                patch.multiple(
                    module, ROOT=root, PUBLIC=public, STAGE=stage, RELEASES=snapshots
                ),
                patch.object(
                    module,
                    "sign",
                    side_effect=lambda filename: original_sign(
                        filename, private, public
                    ),
                ),
            ):
                module.stamp(build="100")
                (stage / "main.py").write_text("new = 2")
                module.stamp(build="200")
                module.receipt(installer)
                output = module.publish(
                    installer,
                    snapshots / "100/app_release.json",
                    "test notes",
                    root / "published",
                )
                installer.unlink()
                delta_output = module.publish(
                    base=snapshots / "100/app_release.json",
                    output=root / "delta-only",
                    mode="incremental",
                )
                code_output = module.publish(
                    output=root / "universal", mode="universal"
                )
            latest_raw = (output / "latest.json").read_bytes()
            core.verify_signature(
                latest_raw,
                (output / "latest.json.sig").read_bytes(),
                public.read_bytes(),
            )
            latest = core.validate_latest(core.loads(latest_raw))
            self.assertEqual(len(latest["incremental"]), 1)
            package = (
                output / "incremental" / Path(latest["incremental"][0]["url"]).name
            )
            with zipfile.ZipFile(package) as archive:
                self.assertEqual(
                    set(archive.namelist()), {"patch.json", "files/main.py"}
                )
            self.assertFalse(any("private" in p.name for p in output.rglob("*")))
            self.assertFalse((delta_output / "latest.json").exists())
            delta_raw = (delta_output / "latest-v2.json").read_bytes()
            core.verify_signature(
                delta_raw,
                (delta_output / "latest-v2.json.sig").read_bytes(),
                public.read_bytes(),
            )
            delta = core.validate_latest(core.loads(delta_raw))
            self.assertIsNone(delta["full"])
            self.assertEqual(delta["publish_mode"], "incremental")
            self.assertFalse(list(delta_output.rglob("*.exe")))
            self.assertEqual(len(delta["incremental"]), 1)
            code_raw = (code_output / "latest-v3.json").read_bytes()
            core.verify_signature(
                code_raw,
                (code_output / "latest-v3.json.sig").read_bytes(),
                public.read_bytes(),
            )
            code = core.validate_latest(core.loads(code_raw))
            self.assertEqual(code["publish_mode"], "universal")
            self.assertIsNone(code["full"])
            self.assertFalse((code_output / "latest.json").exists())
            self.assertFalse((code_output / "latest-v2.json").exists())
            self.assertFalse(list(code_output.rglob("*.exe")))
            with zipfile.ZipFile(
                next((code_output / "incremental").glob("*.zip"))
            ) as archive:
                self.assertEqual(
                    set(archive.namelist()),
                    {"code.json"}
                    | {"files/" + name for name in code["release"]["files"]},
                )


if __name__ == "__main__":
    unittest.main()
