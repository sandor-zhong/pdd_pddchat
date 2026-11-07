import unittest
import zipfile
from unittest.mock import MagicMock, patch

import update_agent
import update_core as core
from tests import test_updates as fixtures


class UniversalUpdatesTests(unittest.TestCase):
    make_package = fixtures.UpdatesTest.make_package
    agent_job = fixtures.UpdatesTest.agent_job

    def setUp(self):
        fixtures.UpdatesTest.setUp(self)
        for value in (self.old, self.new):
            value["update_features"] = ["universal_code"]
            value["data_schema"] = 1
        core.write_json(self.root / "app_release.json", self.old)
        with zipfile.ZipFile(
            self.package, "w", compression=zipfile.ZIP_DEFLATED
        ) as archive:
            archive.writestr(
                "code.json",
                core.dumps({"package_type": "universal_code", "release": self.new}),
            )
            for name, content in self.new_files.items():
                archive.writestr("files/" + name, content)
        self.entry = {
            "url": core.BASE_URL + "incremental/code.zip",
            "sha256": core.sha256_file(self.package),
            "size": self.package.stat().st_size,
        }
        self.latest.update(
            publish_mode="universal", full=None, incremental=[], universal=self.entry
        )

    def prepare(self, root, tag):
        return core.prepare_patch(
            root,
            self.package,
            self.entry,
            self.latest,
            self.install / tag,
            universal=True,
        )

    def test_same_package_upgrades_two_old_builds_and_removes_their_own_obsolete_files(
        self,
    ):
        second = self.install / "second"
        second.mkdir()
        files = {
            "main.py": b"another old version",
            "older_only.py": b"obsolete in this version",
        }
        old = fixtures.release(files, "150")
        old.update(update_features=["universal_code"], data_schema=1)
        for name, content in files.items():
            (second / name).write_bytes(content)
        core.write_json(second / "app_release.json", old)
        for index, root in enumerate((self.root, second)):
            with self.subTest(build=core.installed_release(root)["build"]):
                self.assertEqual(
                    core.select_artifact(root, self.latest), ("universal", self.entry)
                )
                changed, removed = self.prepare(root, f"staging-{index}")
                self.assertEqual(set(changed), set(self.new_files))
                core.begin_transaction(
                    root,
                    self.install / f"staging-{index}",
                    changed,
                    removed,
                    self.new,
                    self.install / f"tx-{index}",
                )
                core.commit(self.install / f"tx-{index}")
                self.assertTrue(core.local_matches(root, self.new))
                self.assertFalse((root / "old.py").exists())
                self.assertFalse((root / "older_only.py").exists())
        for name in self.protected:
            self.assertEqual((self.root / name).read_bytes(), b"keep-business-data")

    def test_rollback_restores_modified_old_code_and_preserves_business_data(self):
        (self.root / "main.py").write_bytes(b"local modified code")
        changed, removed = self.prepare(self.root, "staging")
        core.begin_transaction(
            self.root,
            self.install / "staging",
            changed,
            removed,
            self.new,
            self.install / "tx",
        )
        core.rollback(self.install / "tx")
        self.assertEqual((self.root / "main.py").read_bytes(), b"local modified code")
        self.assertEqual(core.installed_release(self.root)["build"], "100")
        for name in self.protected:
            self.assertEqual((self.root / name).read_bytes(), b"keep-business-data")

    def test_incompatible_environment_or_schema_needs_full_install(self):
        for field, value in (
            ("runtime_id", "b" * 64),
            ("data_schema", 2),
            ("update_features", []),
        ):
            with self.subTest(field=field):
                changed = {**self.old, field: value}
                core.write_json(self.root / "app_release.json", changed)
                self.assertEqual(
                    core.select_artifact(self.root, self.latest), ("manual", None)
                )
                with self.assertRaises(ValueError):
                    self.prepare(self.root, "staging")
        self.assertFalse((self.install / "staging").exists())

    def test_bootstrap_change_requires_full_install(self):
        new = {**self.new, "files": {**self.new["files"], "update_core.py": "b" * 64}}
        self.assertEqual(
            core.select_artifact(self.root, {**self.latest, "release": new}),
            ("manual", None),
        )

    def test_unmanaged_file_collision_is_preserved(self):
        (self.root / "new.py").write_bytes(b"unmanaged local file")
        with self.assertRaisesRegex(ValueError, "本地文件占用"):
            self.prepare(self.root, "staging")
        self.assertEqual((self.root / "new.py").read_bytes(), b"unmanaged local file")

    def test_extra_payload_file_is_rejected(self):
        with zipfile.ZipFile(self.package, "a") as archive:
            archive.writestr("files/.env", b"bad")
        self.entry.update(
            sha256=core.sha256_file(self.package), size=self.package.stat().st_size
        )
        with self.assertRaisesRegex(ValueError, "文件集合"):
            self.prepare(self.root, "staging")

    def test_agent_commits_universal_transaction_only_after_gui_health_ack(self):
        folder = self.agent_job()
        job = folder / "job.json"
        value = core.loads(job.read_bytes())
        value["mode"] = "universal"
        core.write_json(job, value)
        child = MagicMock(pid=87654)
        child.poll.return_value = None

        def launch(root, **kwargs):
            core.write_json(kwargs["health_file"], {"token": kwargs["token"]})
            return child

        with (
            patch.object(core, "verify_signature"),
            patch.object(core, "process_alive", return_value=False),
            patch.object(
                update_agent.subprocess, "run", return_value=MagicMock(returncode=0)
            ),
            patch.object(update_agent, "launch_gui", side_effect=launch),
        ):
            update_agent.run(job)
        self.assertEqual(
            core.loads((folder / "transaction/journal.json").read_bytes())["phase"],
            "committed",
        )
        self.assertEqual(core.installed_release(self.root)["build"], "200")


if __name__ == "__main__":
    unittest.main()
