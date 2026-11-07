"""Check and prepare updates off the GUI thread; never install in this process."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from urllib.error import HTTPError

from .project_paths import (
    PROJECT_PATHS,  # noqa: F401 - initialize installed module search path
)

# isort: split
import update_core as core


def check(root):
    root = Path(root).resolve()
    if not core.installed_layout(root):
        return {
            "available": False,
            "message": "当前为源码版或旧安装版，请先覆盖安装带更新功能的新版本。",
        }
    folder = root.parent / "update-cache" / uuid.uuid4().hex
    folder.mkdir(parents=True, exist_ok=False)
    for name in ("latest-v3.json", "latest-v2.json", "latest.json"):
        try:
            core.download(
                core.BASE_URL + name, folder / "latest.json", limit=2 * 1024 * 1024
            )
            break
        except HTTPError as error:
            if error.code != 404 or name == "latest.json":
                raise
    unverified = core.loads((folder / "latest.json").read_bytes())
    build = str(unverified.get("build", ""))
    if not re.fullmatch(r"\d{3,20}", build):
        raise ValueError("服务器构建编号无效。")
    core.download(
        core.BASE_URL + f"packages/release-{build}.json.sig",
        folder / "latest.json.sig",
        limit=8192,
    )
    core.verify_signature(
        (folder / "latest.json").read_bytes(),
        (folder / "latest.json.sig").read_bytes(),
        (root / "update_public_key.json").read_bytes(),
    )
    latest = core.validate_latest(core.loads((folder / "latest.json").read_bytes()))
    mode, artifact = core.select_artifact(root, latest)
    if mode is None:
        return {"available": False, "message": "当前已是最新构建。"}
    if mode == "manual":
        return {
            "available": False,
            "message": f"发现 {latest['version']}，当前构建、更新器或运行环境无法使用本次代码更新。请联系发布者获取完整安装包。",
        }
    return {
        "available": True,
        "latest": latest,
        "mode": mode,
        "artifact": artifact,
        "folder": str(folder),
    }


def prepare(root, plan, progress=None):
    root, folder = Path(root).resolve(), Path(plan["folder"]).resolve()
    entry = plan["artifact"]
    required = entry["size"] * 3 + 100 * 1024 * 1024
    if shutil.disk_usage(folder).free < required:
        raise RuntimeError("磁盘可用空间不足，请先清理空间。")
    path = folder / (
        "package.zip" if plan["mode"] in ("incremental", "universal") else "setup.exe"
    )
    core.download(
        entry["url"],
        path,
        limit=core.MAX_PACKAGE,
        expected_size=entry["size"],
        expected_sha=entry["sha256"],
        progress=progress,
    )
    return plan


def copy_helper_runtime(source, destination):
    source, destination = Path(source), Path(destination)
    destination.mkdir()
    for path in source.iterdir():
        if path.is_file():
            shutil.copy2(path, destination / path.name)
    for pth in destination.glob("python*._pth"):
        zip_name = pth.name.removesuffix("._pth") + ".zip"
        pth.write_text(zip_name + "\n.\nimport site\n", encoding="ascii")


def agent_source(root):
    root = Path(root)
    installed_files = core.installed_release(root).get("files", {})
    if all(
        (root / name).is_file()
        and core.sha256_file(root / name) == installed_files.get(name)
        for name in ("update_core.py", "update_agent.py")
    ):
        return root
    return root.parent / "updater"


def start_agent(root, plan):
    root, folder = Path(root).resolve(), Path(plan["folder"]).resolve()
    if not folder.is_relative_to(
        root.parent / "update-cache"
    ) or not core.installed_layout(root):
        raise ValueError("更新目录不合法。")
    download_folder = folder
    folder = download_folder / ("attempt-" + uuid.uuid4().hex)
    folder.mkdir(exist_ok=False)
    for name in (
        "latest.json",
        "latest.json.sig",
        "package.zip" if plan["mode"] in ("incremental", "universal") else "setup.exe",
    ):
        shutil.copy2(download_folder / name, folder / name)
    # Universal code updates can replace the managed agent without replacing the
    # stable launcher directory. Prefer that verified installed copy on later runs.
    bootstrap = agent_source(root)
    agent_files = ("update_core.py", "update_agent.py")
    for name in agent_files:
        shutil.copy2(bootstrap / name, folder / name)
    # A full installer may replace the installed Python interpreter and DLLs.
    # Run the independent helper from its own minimal stdlib runtime so it does
    # not lock any installed runtime files while Inno Setup is copying them.
    helper_runtime = folder / "runtime"
    copy_helper_runtime(root.parent / "runtime/gui", helper_runtime)
    core.write_json(
        folder / "job.json",
        {
            "root": str(root),
            "parent_pid": os.getpid(),
            "mode": plan["mode"],
            "artifact": plan["artifact"],
        },
    )
    child = subprocess.Popen(
        [
            str(helper_runtime / "pythonw.exe"),
            "-B",
            str(folder / "update_agent.py"),
            "--job",
            str(folder / "job.json"),
        ],
        cwd=folder,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if (folder / "ready.json").is_file():
            return {**plan, "folder": str(folder)}
        if (folder / "failed.json").is_file():
            raise RuntimeError(
                core.loads((folder / "failed.json").read_bytes())["message"]
            )
        if child.poll() is not None:
            raise RuntimeError("独立更新进程启动失败，请查看更新目录日志。")
        time.sleep(0.1)
    # Parent stays open: the updater has a bounded wait and will not replace files.
    raise RuntimeError("更新进程未及时就绪，主程序保持运行，未应用更新。")
