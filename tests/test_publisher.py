import errno
import io
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packaging"))
import publisher_backend as backend


class FakeSftp:
    def __init__(self):
        self.files = {}
        self.dirs = {"/updates"}
        self.events = []
        self.corrupt = False
        self.atomic = True

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def get_channel(self):
        return SimpleNamespace(settimeout=lambda _: None)

    def stat(self, path):
        if path in self.dirs:
            return SimpleNamespace(st_mode=stat.S_IFDIR)
        if path in self.files:
            return SimpleNamespace(st_mode=stat.S_IFREG)
        raise FileNotFoundError(errno.ENOENT, path)

    def mkdir(self, path):
        if path in self.dirs:
            raise FileExistsError(errno.EEXIST, path)
        self.dirs.add(path)

    def rmdir(self, path):
        self.dirs.remove(path)

    def remove(self, path):
        del self.files[path]

    def chmod(self, *args):
        pass

    def put(self, source, target, callback=None):
        self.files[target] = Path(source).read_bytes() + (
            b"corrupt" if self.corrupt else b""
        )
        self.events.append(("put", target))
        if callback:
            callback(len(self.files[target]), len(self.files[target]))

    def rename(self, source, target):
        if target in self.files:
            raise FileExistsError(errno.EEXIST, target)
        self.files[target] = self.files.pop(source)
        self.events.append(("rename", target))

    def posix_rename(self, source, target):
        if not self.atomic:
            raise OSError("unsupported atomic rename")
        self.files[target] = self.files.pop(source)
        self.events.append(("atomic", target))

    def open(self, path, mode):
        if "w" in mode:
            owner = self

            class Writer(io.BytesIO):
                def close(self):
                    owner.files[path] = self.getvalue()
                    super().close()

            return Writer()
        if path not in self.files:
            raise FileNotFoundError(errno.ENOENT, path)
        return io.BytesIO(self.files[path])


class PublisherTests(unittest.TestCase):
    def test_hash_runs_on_server_without_sftp_download(self):
        digest = "a" * 64
        client = unittest.mock.MagicMock()
        stdout = unittest.mock.MagicMock()
        stdout.read.return_value = (digest + "  /updates/file.exe\n").encode()
        stdout.channel.recv_exit_status.return_value = 0
        client.exec_command.return_value = (None, stdout, unittest.mock.MagicMock())
        sftp = unittest.mock.MagicMock()
        result = backend.remote_hash(sftp, "/updates/file.exe", client)
        self.assertEqual(result, digest)
        sftp.open.assert_not_called()
        client.exec_command.assert_called_once_with(
            "sha256sum -- /updates/file.exe", timeout=45
        )
        stdout.channel.close.assert_called_once()

    def test_failed_server_hash_is_reported_instead_of_silent_download(self):
        client = unittest.mock.MagicMock()
        stdout = unittest.mock.MagicMock()
        stdout.read.return_value = b""
        stdout.channel.recv_exit_status.return_value = 127
        client.exec_command.return_value = (None, stdout, unittest.mock.MagicMock())
        sftp = unittest.mock.MagicMock()
        with self.assertRaisesRegex(RuntimeError, "sha256sum"):
            backend.remote_hash(sftp, "/updates/file.exe", client)
        sftp.open.assert_not_called()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        (self.folder / "packages").mkdir()
        (self.folder / "incremental").mkdir()
        (self.folder / "incremental/patch.zip").write_bytes(b"patch")
        for name, content in {
            "latest.json": b"new manifest",
            "latest.json.sig": b"sig",
            "packages/setup.exe": b"installer",
            "packages/release-200.json.sig": b"sig",
        }.items():
            (self.folder / name).write_bytes(content)
        self.config = {"host": "server", "username": "root", "directory": "/updates"}
        self.sftp = FakeSftp()
        self.client = SimpleNamespace(open_sftp=lambda: self.sftp)
        self.connection = unittest.mock.MagicMock()
        self.connection.__enter__.return_value = self.client
        self.latest = {"version": "0.1.3", "build": "200"}

    def run_upload(self):
        def download(url, target, **kwargs):
            Path(target).write_bytes(
                b"sig" if url.endswith(".sig") else b"new manifest"
            )

        with (
            patch.object(backend, "connect", return_value=self.connection),
            patch.object(
                backend,
                "validate_bundle",
                return_value=(
                    self.latest,
                    (
                        ["packages/release-200.json.sig", "incremental/patch.zip"]
                        if self.latest.get("publish_mode")
                        in ("incremental", "universal")
                        else ["packages/setup.exe", "packages/release-200.json.sig"]
                    ),
                ),
            ),
            patch.object(backend.core, "download", side_effect=download),
            patch.object(backend.core, "verify_signature"),
        ):
            return backend.upload_bundle(
                self.folder, self.config, "secret", lambda _: None
            )

    def test_upload_publishes_manifest_last_after_hash_verified_payloads(self):
        self.run_upload()
        completed = [
            target
            for action, target in self.sftp.events
            if action in ("rename", "atomic") and "probe-" not in target
        ]
        self.assertEqual(
            completed,
            [
                "/updates/packages/setup.exe",
                "/updates/packages/release-200.json.sig",
                "/updates/latest.json.sig",
                "/updates/latest.json",
                "/updates/latest-v2.json.sig",
                "/updates/latest-v2.json",
                "/updates/latest-v3.json.sig",
                "/updates/latest-v3.json",
            ],
        )
        self.assertNotIn("/updates/.publish-lock", self.sftp.dirs)

    def test_corrupt_upload_never_publishes_manifest(self):
        self.sftp.corrupt = True
        with self.assertRaisesRegex(ValueError, "校验失败"):
            self.run_upload()
        self.assertNotIn("/updates/latest.json", self.sftp.files)

    def test_incremental_upload_preserves_legacy_feed_and_sends_no_installer(self):
        self.latest["publish_mode"] = "incremental"
        self.sftp.files["/updates/latest.json"] = b'{"build":"100"}'
        self.sftp.files["/updates/packages/release-100.json.sig"] = b"sig"
        with patch.object(
            backend.core, "validate_latest", return_value={"build": "100"}
        ):
            self.run_upload()
        self.assertEqual(self.sftp.files["/updates/latest.json"], b'{"build":"100"}')
        self.assertEqual(self.sftp.files["/updates/latest-v2.json"], b"new manifest")
        self.assertFalse(
            any(
                action == "put" and ".exe" in target
                for action, target in self.sftp.events
            )
        )

    def test_universal_upload_preserves_both_older_feeds(self):
        self.latest["publish_mode"] = "universal"
        for name in ("latest.json", "latest-v2.json"):
            self.sftp.files["/updates/" + name] = b'{"build":"100"}'
        self.sftp.files["/updates/packages/release-100.json.sig"] = b"sig"
        with patch.object(
            backend.core, "validate_latest", return_value={"build": "100"}
        ):
            self.run_upload()
        for name in ("latest.json", "latest-v2.json"):
            self.assertEqual(self.sftp.files["/updates/" + name], b'{"build":"100"}')
        self.assertEqual(self.sftp.files["/updates/latest-v3.json"], b"new manifest")
        self.assertFalse(
            any(
                action == "put" and ".exe" in target
                for action, target in self.sftp.events
            )
        )

    def test_atomic_rename_required_before_any_package_upload(self):
        self.sftp.atomic = False
        with self.assertRaisesRegex(OSError, "unsupported"):
            self.run_upload()
        self.assertFalse(any(action == "put" for action, _ in self.sftp.events))

    def test_conflicting_immutable_package_never_overwrites(self):
        self.sftp.files["/updates/packages/setup.exe"] = b"different"
        with self.assertRaisesRegex(ValueError, "拒绝覆盖"):
            self.run_upload()
        self.assertEqual(self.sftp.files["/updates/packages/setup.exe"], b"different")
        self.assertNotIn("/updates/latest.json", self.sftp.files)

    def test_existing_publication_lock_blocks_second_publisher(self):
        self.sftp.dirs.add("/updates/.publish-lock")
        with self.assertRaisesRegex(RuntimeError, "发布锁"):
            self.run_upload()
        self.assertIn("/updates/.publish-lock", self.sftp.dirs)

    def test_newer_server_build_blocks_upload_before_payloads(self):
        self.sftp.files["/updates/latest.json"] = b'{"build":"300"}'
        self.sftp.files["/updates/packages/release-300.json.sig"] = b"sig"
        with (
            patch.object(
                backend.core, "validate_latest", return_value={"build": "300"}
            ),
            self.assertRaisesRegex(ValueError, "拒绝回退"),
        ):
            self.run_upload()
        self.assertFalse(any(action == "put" for action, _ in self.sftp.events))

    def test_retry_skips_identical_installer(self):
        self.sftp.files["/updates/packages/setup.exe"] = b"installer"
        self.run_upload()
        self.assertFalse(
            any(
                action == "put" and "setup.exe" in target
                for action, target in self.sftp.events
            )
        )

    def test_directory_and_version_validation_rejects_shell_and_path_injection(self):
        for directory in ("/", "/updates/../etc", "relative", "C:\\updates"):
            with self.subTest(directory=directory), self.assertRaises(ValueError):
                backend.validate_server({**self.config, "directory": directory})
        for version in ("0.1.3; rm", "$(command)", "0.1"):
            with self.subTest(version=version), self.assertRaises(ValueError):
                backend.validate_version(version)

    def test_incremental_mode_rejects_legacy_base_before_building(self):
        from tests.test_updates import release

        base = self.folder / "legacy.json"
        backend.core.write_json(base, release({"main.py": b"old"}))
        with (
            patch.object(backend, "run_process") as run,
            self.assertRaisesRegex(ValueError, "完整发布"),
        ):
            backend.build_release("0.1.6", base, "notes", mode="incremental")
        run.assert_not_called()

    def test_gui_profile_excludes_password(self):
        import os

        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        import publisher_gui as gui
        from PyQt5.QtWidgets import QApplication

        app = QApplication.instance() or QApplication([])
        with patch.object(gui, "STATE", self.folder / "state"):
            window = gui.Publisher()
            self.assertFalse(hasattr(window, "base"))
            self.assertGreaterEqual(window.mode.findData("universal"), 0)
            window.password.setText("do-not-save-this-secret")
            window.save_profile()
            raw = (self.folder / "state/profile.json").read_text(encoding="utf-8")
            self.assertNotIn("do-not-save-this-secret", raw)
            window.close()
        self.assertIsNotNone(app)


if __name__ == "__main__":
    unittest.main()
