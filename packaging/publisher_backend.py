"""Developer-only release construction and ordered SFTP publication."""

from __future__ import annotations

import base64
import errno
import hashlib
import os
import re
import shlex
import stat
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import update_core as core


def validate_version(version):
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError("版本号请填写三段数字，例如 0.1.3。")
    return version


def validate_server(config):
    if not config.get("host", "").strip() or not config.get("username", "").strip():
        raise ValueError("请填写 SFTP 主机和用户名。")
    if not 1 <= int(config.get("port", 22)) <= 65535:
        raise ValueError("端口应为 1～65535。")
    directory = config.get("directory", "").strip()
    path = PurePosixPath(directory)
    if (
        not path.is_absolute()
        or str(path) == "/"
        or ".." in path.parts
        or "\\" in directory
        or "\0" in directory
    ):
        raise ValueError("服务器目录必须是更新目录的绝对路径，不能填根目录或包含 ..。")
    return str(path)


def fingerprint(key):
    return "SHA256:" + base64.b64encode(
        hashlib.sha256(key.asbytes()).digest()
    ).decode().rstrip("=")


def connect(config, password=""):
    import paramiko

    validate_server(config)
    expected = config.get("fingerprint", "").strip()

    class CheckedKey(paramiko.MissingHostKeyPolicy):
        def missing_host_key(self, client, hostname, key):
            actual = fingerprint(key)
            if not expected:
                raise ValueError(
                    f"首次连接，请从服务器管理处核对以下主机指纹，填入指纹框后重试：{actual}"
                )
            if actual != expected:
                raise ValueError(f"服务器主机指纹不匹配。实际指纹：{actual}")

    client = paramiko.SSHClient()
    if not expected:
        client.load_system_host_keys()
    client.set_missing_host_key_policy(CheckedKey())
    try:
        client.connect(
            config["host"].strip(),
            port=int(config.get("port", 22)),
            username=config["username"].strip(),
            password=password or None,
            key_filename=config.get("key_file") or None,
            passphrase=password or None,
            timeout=15,
            banner_timeout=15,
            auth_timeout=20,
            channel_timeout=30,
        )
        client.get_transport().set_keepalive(15)
        return client
    except BaseException:
        client.close()
        raise


def remote_bytes(sftp, path, limit=2 * 1024 * 1024):
    with sftp.open(path, "rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("服务器清单或签名超过大小上限。")
    return raw


def remote_hash(sftp, path, client=None):
    if client is not None and hasattr(client, "exec_command"):
        # Hash on the SSH server instead of downloading the entire installer
        # through synchronous SFTP reads after it was just uploaded.
        _, stdout, stderr = client.exec_command(
            shlex.join(["sha256sum", "--", path]), timeout=45
        )
        try:
            result = stdout.read(512).decode("ascii", errors="replace")
            code = stdout.channel.recv_exit_status()
            digest = result.split(maxsplit=1)[0] if result else ""
            if code != 0 or not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise RuntimeError(
                    "服务器端 SHA256 校验失败，请确认服务器可运行 sha256sum。"
                )
            return digest
        finally:
            stdout.close()
            stderr.close()
            stdout.channel.close()
    digest = hashlib.sha256()
    with sftp.open(path, "rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def exists(sftp, path):
    try:
        return sftp.stat(path)
    except OSError as error:
        if error.errno == errno.ENOENT:
            return None
        raise


def manifest_files(folder):
    folder = Path(folder)
    manifest = next(
        (
            folder / name
            for name in ("latest-v3.json", "latest-v2.json", "latest.json")
            if (folder / name).is_file()
        ),
        folder / "latest.json",
    )
    return manifest, manifest.with_suffix(manifest.suffix + ".sig")


def validate_bundle(folder):
    folder = Path(folder).resolve()
    manifest, signed = manifest_files(folder)
    raw = manifest.read_bytes()
    core.verify_signature(
        raw,
        signed.read_bytes(),
        (ROOT / "update_public_key.json").read_bytes(),
    )
    latest = core.validate_latest(core.loads(raw))
    signature = f"packages/release-{latest['build']}.json.sig"
    if (folder / signature).read_bytes() != signed.read_bytes():
        raise ValueError("独立签名文件与清单签名不一致。")
    names = [signature]
    entries = (
        ([latest["full"]] if latest.get("full") else [])
        + ([latest["universal"]] if latest.get("universal") else [])
        + latest.get("incremental", [])
    )
    for entry in entries:
        core.allowed_url(entry["url"])
        name = entry["url"][len(core.BASE_URL) :]
        parts = PurePosixPath(name).parts
        if (
            len(parts) != 2
            or parts[0] not in ("packages", "incremental")
            or not re.fullmatch(r"[A-Za-z0-9_.-]+", parts[1])
        ):
            raise ValueError(
                "发布包 URL 必须指向 packages 或 incremental 下的普通文件。"
            )
        if name not in names:
            path = folder / name
            if (
                not path.is_file()
                or path.stat().st_size != entry["size"]
                or core.sha256_file(path) != entry["sha256"]
            ):
                raise ValueError(f"发布包校验失败：{name}")
            names.append(name)
    return latest, names


@contextmanager
def publication_lock(sftp, directory):
    if not stat.S_ISDIR(sftp.stat(directory).st_mode):
        raise ValueError("服务器更新目录不存在或不是目录。")
    path = directory + "/.publish-lock"
    try:
        sftp.mkdir(path)
    except OSError as error:
        raise RuntimeError(
            "无法取得服务器发布锁，请检查目录权限，或确认没有其他发布任务后清理 .publish-lock 空目录。"
        ) from error
    try:
        # Verify atomic replacement support before uploading a large installer.
        first, second = path + "/probe-a", path + "/probe-b"
        with sftp.open(first, "w") as stream:
            stream.write(b"probe")
        try:
            sftp.posix_rename(first, second)
        finally:
            for probe in (first, second):
                if exists(sftp, probe):
                    sftp.remove(probe)
        yield
    finally:
        sftp.rmdir(path)


def upload_bundle(folder, config, password, log=print, progress=lambda value: None):
    folder = Path(folder).resolve()
    latest, names = validate_bundle(folder)
    manifest, signed = manifest_files(folder)
    directory = validate_server(config)
    with (
        connect(config, password) as client,
        client.open_sftp() as sftp,
        publication_lock(sftp, directory),
    ):
        sftp.get_channel().settimeout(45)
        if not stat.S_ISDIR(sftp.stat(directory).st_mode):
            raise ValueError("服务器更新目录不存在或不是目录。")
        for manifest_name in ("latest.json", "latest-v2.json", "latest-v3.json"):
            remote_latest = directory + "/" + manifest_name
            if not exists(sftp, remote_latest):
                continue
            old_raw = remote_bytes(sftp, remote_latest)
            old = core.validate_latest(core.loads(old_raw))
            signature = remote_bytes(
                sftp, directory + f"/packages/release-{old['build']}.json.sig", 8192
            )
            core.verify_signature(
                old_raw, signature, (ROOT / "update_public_key.json").read_bytes()
            )
            if int(old["build"]) > int(latest["build"]):
                raise ValueError("服务器版本比本次发布更新，拒绝回退。")
            if old["build"] == latest["build"] and old_raw != manifest.read_bytes():
                raise ValueError("相同构建号的服务器清单内容不同，拒绝覆盖。")
        for sub in ("packages", "incremental"):
            path = directory + "/" + sub
            info = exists(sftp, path)
            if info is None:
                sftp.mkdir(path)
            elif not stat.S_ISDIR(info.st_mode):
                raise ValueError(f"服务器 {sub} 不是目录。")

        # Weight progress by bytes, not file count: a tiny signature is not a
        # third of the actual transfer. Reserve the final 15% for publication.
        total_bytes = sum((folder / name).stat().st_size for name in names)
        completed_bytes = 0
        # All large artifacts and the immutable signature arrive BEFORE latest.
        for name in names:
            source, target = folder / name, directory + "/" + name
            digest = core.sha256_file(source)
            if exists(sftp, target):
                log("校验服务器已有文件：" + name)
                if remote_hash(sftp, target, client) != digest:
                    raise ValueError(f"服务器同名文件内容不同，拒绝覆盖：{name}")
                log("已上传且校验一致，跳过：" + name)
            else:
                temporary = target + ".upload-" + uuid.uuid4().hex
                log("上传：" + name)
                started = time.monotonic()
                last_report = [started, -1]

                def upload_progress(
                    sent,
                    total,
                    completed_bytes=completed_bytes,
                    last_report=last_report,
                    started=started,
                    source=source,
                ):
                    value = int((completed_bytes + sent) * 85 / max(total_bytes, 1))
                    if value != last_report[1]:
                        progress(value)
                        last_report[1] = value
                    now = time.monotonic()
                    if now - last_report[0] >= 1 or sent == total:
                        speed = sent / max(now - started, 0.01) / 1024 / 1024
                        log(
                            f"上传进度 {source.name}：{sent / 1024 / 1024:.1f}/{total / 1024 / 1024:.1f} MB，平均 {speed:.2f} MB/s"
                        )
                        last_report[0] = now

                sftp.put(
                    str(source),
                    temporary,
                    callback=upload_progress,
                )
                sftp.chmod(temporary, 0o644)
                log("校验服务器 SHA256：" + name)
                if remote_hash(sftp, temporary, client) != digest:
                    raise ValueError("服务器文件校验失败：" + name)
                sftp.rename(temporary, target)
            completed_bytes += source.stat().st_size
            progress(int(completed_bytes * 85 / max(total_bytes, 1)))
        log("安装包和增量包校验通过，正在发布清单……")
        progress(90)
        # Legacy clients keep a valid full-package feed. Incremental-only
        # publications advance only the feed read by newer clients.
        manifest_names = ("latest-v2.json.sig", "latest-v2.json")
        if latest.get("publish_mode") == "universal":
            manifest_names = ("latest-v3.json.sig", "latest-v3.json")
        if latest.get("publish_mode", "full") == "full":
            manifest_names = (
                "latest.json.sig",
                "latest.json",
                *manifest_names,
                "latest-v3.json.sig",
                "latest-v3.json",
            )
        for name in manifest_names:
            target = directory + "/" + name
            temporary = target + ".upload-" + uuid.uuid4().hex
            source = signed if name.endswith(".sig") else manifest
            sftp.put(str(source), temporary)
            sftp.chmod(temporary, 0o644)
            if remote_hash(sftp, temporary, client) != core.sha256_file(source):
                raise ValueError("服务器清单上传校验失败。")
            # Do not delete the old manifest to emulate atomic replacement.
            # Unsupported servers fail here and leave the old latest intact.
            sftp.posix_rename(temporary, target)
        log("服务器清单已原子切换，正在验证 HTTPS 公开地址……")
        progress(95)
    try:
        with tempfile.TemporaryDirectory() as temporary:
            check = Path(temporary)
            core.download(
                core.BASE_URL
                + (
                    "latest-v2.json"
                    if latest.get("publish_mode") == "incremental"
                    else "latest-v3.json"
                ),
                check / "latest.json",
                limit=2 * 1024 * 1024,
            )
            if (check / "latest.json").read_bytes() != manifest.read_bytes():
                raise ValueError("HTTPS 返回了不同的清单，可能有缓存或目录映射错误。")
            core.download(
                core.BASE_URL + f"packages/release-{latest['build']}.json.sig",
                check / "sig",
                limit=8192,
            )
            core.verify_signature(
                (check / "latest.json").read_bytes(),
                (check / "sig").read_bytes(),
                (ROOT / "update_public_key.json").read_bytes(),
            )
    except Exception as error:
        raise RuntimeError(
            f"服务器已发布，但 HTTPS 验证未通过；请勿重新构建，可用原发布包重试验证。原因：{error}"
        ) from error
    progress(100)
    log(f"发布成功并通过 HTTPS 验证：{latest['version']} / {latest['build']}")
    return latest


def check_connection(config, password):
    directory = validate_server(config)
    with connect(config, password) as client, client.open_sftp() as sftp:
        if not stat.S_ISDIR(sftp.stat(directory).st_mode):
            raise ValueError("服务器更新目录不是目录。")
    return "SFTP 登录和目录检查通过。"


def run_process(arguments, log):
    environment = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    with subprocess.Popen(
        arguments,
        cwd=ROOT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    ) as child:
        for line in child.stdout:
            log(line.rstrip())
        if child.wait():
            raise RuntimeError("构建或发布文件生成失败，请查看日志末尾。")


def build_release(version, base, notes, log=print, mode="full"):
    validate_version(version)
    if mode not in ("full", "incremental", "universal"):
        raise ValueError("发布模式无效。")
    if mode == "universal":
        bootstrap = {
            name: core.sha256_file(ROOT / name)
            for name in (
                "update_core.py",
                "update_agent.py",
                "update_launcher.py",
                "update_public_key.json",
            )
        }
        candidates = [
            core.loads(path.read_bytes())
            for path in (ROOT / "packaging/releases").glob("*/app_release.json")
        ]
        if not any(
            "universal_code" in item.get("update_features", [])
            and all(
                item["files"].get(name) == digest for name, digest in bootstrap.items()
            )
            for item in candidates
        ):
            raise ValueError(
                "请先完整发布一次支持通用代码包的更新器，再使用日常通用代码发布。"
            )
    if mode == "incremental":
        if not base:
            raise ValueError("日常增量发布必须选择旧版本基线。")
        old = core.validate_release(core.loads(Path(base).read_bytes()))
        if "incremental_only" not in old.get("update_features", []):
            raise ValueError("该旧基线尚不支持仅增量发布，请先完整发布一次新版更新器。")
        for name in (
            "update_core.py",
            "update_agent.py",
            "update_launcher.py",
            "update_public_key.json",
        ):
            if old["files"].get(name) != core.sha256_file(ROOT / name):
                raise ValueError("更新器或公钥已变化，请选择完整发布。")
    stage = ROOT / "packaging/stage/app"
    if stage.resolve() != ROOT / "packaging/stage/app":
        raise ValueError("暂存目录指向了其他位置。")
    for path in [stage, *stage.rglob("*")]:
        if path.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise ValueError("暂存目录含链接或联接点，停止构建。")
    state = ROOT / "packaging/publisher-state"
    state.mkdir(exist_ok=True)
    lock = state / "build.lock"
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise RuntimeError(
            "已有发布工具正在构建。若上次异常退出，请确认没有构建进程后删除 packaging/publisher-state/build.lock。"
        ) from None
    try:
        os.close(descriptor)
        core.write_json(ROOT / "release_config.json", {"version": version})
        notes_file = state / "release-notes.txt"
        notes_file.write_text(notes, encoding="utf-8")
        run_process(
            [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(ROOT / "packaging/publisher_build.ps1"),
                "-Version",
                version,
                *(["-CodeOnly"] if mode in ("incremental", "universal") else []),
            ],
            log,
        )
        release = core.installed_release(stage)
        python = ROOT / ".venv/Scripts/python.exe"
        args = [
            str(python),
            str(ROOT / "packaging/release_tools.py"),
            "publish",
            "--notes-file",
            str(notes_file),
            "--mode",
            mode,
        ]
        if base:
            args.extend(["--base", str(base)])
        run_process(args, log)
        folder = ROOT / "dist/updates" / release["build"]
        latest, names = validate_bundle(folder)
        bundle = ROOT / "dist" / f"pddchat-update-server-{latest['build']}.zip"
        with zipfile.ZipFile(bundle, "x", compression=zipfile.ZIP_STORED) as archive:
            metadata = [path.name for path in folder.glob("latest*.json*")]
            for name in [*names, *metadata]:
                archive.write(folder / name, name)
        log("发布文件已生成：" + str(folder))
        return str(folder)
    finally:
        lock.unlink(missing_ok=True)
