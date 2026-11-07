"""Independent updater, copied to update-cache before the GUI exits."""

from __future__ import annotations

import argparse
import ctypes
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import update_core as core

STARTUP_CONFIRM_TIMEOUT = 180


def message(text):
    if os.name == "nt":
        ctypes.windll.user32.MessageBoxW(None, text, "拼多多申诉 — 更新", 0x10)


def claim(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name("." + path.name + "." + uuid.uuid4().hex)
    core.write_json(temp, payload)
    try:
        for _ in range(3):
            try:
                os.link(temp, path)
                return
            except FileExistsError:
                old = core.loads(path.read_bytes())
                if core.process_alive(old.get("pid")):
                    raise RuntimeError("仍有任务或更新进程正在运行，请结束后重试。")
                path.unlink(missing_ok=True)
        raise RuntimeError("未能取得更新锁。")
    finally:
        temp.unlink(missing_ok=True)


def launch_gui(root, *, health_file=None, token=None):
    root = Path(root)
    environment = dict(os.environ)
    environment.pop("PDDCHAT_UPDATE_HEALTH_FILE", None)
    environment.pop("PDDCHAT_UPDATE_HEALTH_TOKEN", None)
    if health_file:
        environment["PDDCHAT_UPDATE_HEALTH_FILE"] = str(health_file)
        environment["PDDCHAT_UPDATE_HEALTH_TOKEN"] = token
    # pythonw has no console: preserve early import/Qt errors too, before the
    # application's own logging has been configured. Close only our file handle.
    log_path = (
        Path(health_file).parent / "gui-startup.log"
        if health_file
        else root / "gui/state/logs/restart.log"
    )
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab") as output:
        return subprocess.Popen(
            [str(root.parent / "runtime/gui/pythonw.exe"), "-B", "-m", "app.main"],
            cwd=root / "gui",
            env=environment,
            stdout=output,
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )


def wait_for_startup(process, health_file, token):
    """A bounded grace period; elapsed time alone never counts as success."""
    health_file = Path(health_file)
    deadline = time.monotonic() + STARTUP_CONFIRM_TIMEOUT
    while True:
        exit_code = process.poll()
        if exit_code is not None:
            raise RuntimeError(
                f"新版未通过启动确认：进程已退出（退出码 {exit_code}）。"
                f"启动日志：{health_file.parent / 'gui-startup.log'}"
            )
        if (
            health_file.is_file()
            and core.loads(health_file.read_bytes()).get("token") == token
        ):
            return
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"新版启动确认超时（{STARTUP_CONFIRM_TIMEOUT}秒），进程仍在运行。"
                "可能启动缓慢或有提示窗口阻塞。"
                f"日志目录：{health_file.parent}；另请查看安装目录下 app/gui/state/logs/gui.log。"
            )
        time.sleep(0.2)


def recover_pending(root):
    root = Path(root).resolve()
    cache = root.parent / "update-cache"
    marker = cache / "active.json"
    if not marker.is_file():
        return
    state = core.loads(marker.read_bytes())
    if core.process_alive(state.get("pid")):
        if (
            state.get("health_token")
            and os.environ.get("PDDCHAT_UPDATE_HEALTH_TOKEN") == state["health_token"]
        ):
            return
        raise RuntimeError("程序正在更新，请等待更新完成后再打开。")
    if (
        state.get("health_token")
        and os.environ.get("PDDCHAT_UPDATE_HEALTH_TOKEN") == state["health_token"]
    ):
        raise RuntimeError("更新进程异常退出，请重新打开程序恢复上个版本。")
    if state.get("new_gui_pid") and core.process_alive(state["new_gui_pid"]):
        raise RuntimeError(
            "上次更新已中断，请先关闭仍在运行的程序窗口，再重新打开以恢复。"
        )
    job_dir = Path(state["job_dir"]).resolve()
    if not job_dir.is_relative_to(cache) or job_dir == cache:
        raise ValueError("更新恢复目录不合法。")
    transaction = job_dir / "transaction"
    if (transaction / "journal.json").is_file():
        journal = core.loads((transaction / "journal.json").read_bytes())
        if Path(journal["root"]).resolve() != root:
            raise ValueError("更新恢复目标不匹配。")
        if journal["phase"] not in ("committed", "rolled_back"):
            core.rollback(transaction)
    marker.unlink(missing_ok=True)


def run(job_file):
    job_file = Path(job_file).resolve()
    job = core.loads(job_file.read_bytes())
    root = Path(job["root"]).resolve()
    folder = job_file.parent
    cache = root.parent / "update-cache"
    if not folder.is_relative_to(cache) or not core.installed_layout(root):
        raise ValueError("不是受支持的安装目录，不能原位更新。")
    latest_raw, signature = (
        (folder / "latest.json").read_bytes(),
        (folder / "latest.json.sig").read_bytes(),
    )
    core.verify_signature(
        latest_raw, signature, (root / "update_public_key.json").read_bytes()
    )
    latest = core.validate_latest(core.loads(latest_raw))
    mode, selected = core.select_artifact(root, latest)
    if mode != job["mode"] or selected != job["artifact"]:
        raise ValueError("下载后当前版本发生变化，请重新检查更新。")
    package = folder / (
        "package.zip" if mode in ("incremental", "universal") else "setup.exe"
    )
    if (
        core.sha256_file(package) != selected["sha256"]
        or package.stat().st_size != selected["size"]
    ):
        raise ValueError("下载文件校验失败。")
    token = uuid.uuid4().hex
    state = {
        "pid": os.getpid(),
        "job_dir": str(folder),
        "phase": "waiting",
        "health_token": token,
    }
    marker, operation = cache / "active.json", root / "state/scheduler.lock"
    operation_id = uuid.uuid4().hex
    # Claim the same operation lock respected by all CLI and GUI browser tasks.
    claim(
        operation,
        {
            "pid": os.getpid(),
            "lock_id": operation_id,
            "shops": [],
            "operation": "应用更新",
        },
    )
    owns_marker, transaction_started, replacement_gui = False, False, None
    try:
        if marker.exists():
            recover_pending(root)
        claim(marker, state)
        owns_marker = True
        core.write_json(folder / "ready.json", {"pid": os.getpid()})
        deadline = time.monotonic() + 120
        while core.process_alive(job["parent_pid"]):
            if time.monotonic() > deadline:
                raise RuntimeError("主程序仍未退出，更新已取消，未替换程序文件。")
            time.sleep(0.2)
        state["phase"] = "applying"
        core.write_json(marker, state)
        if mode == "full":
            log = folder / "installer.log"
            result = subprocess.run(
                [
                    str(package),
                    "/VERYSILENT",
                    "/SUPPRESSMSGBOXES",
                    "/NORESTART",
                    "/SP-",
                    "/DIR=" + str(root.parent),
                    "/LOG=" + str(log),
                ],
                timeout=600,
                check=False,
            )
            if result.returncode not in (0, 3010):
                raise RuntimeError(
                    f"完整安装包执行失败，退出码 {result.returncode}。日志：{log}"
                )
            current = core.installed_release(root)
            if current["build"] != latest["build"] or not core.local_matches(
                root, current
            ):
                raise RuntimeError("完整安装后版本校验未通过。")
        else:
            changed, removed = core.prepare_patch(
                root,
                package,
                selected,
                latest,
                folder / "staging",
                **({"universal": True} if mode == "universal" else {}),
            )
            # Journal is created before replacements; recovery is repeatable.
            transaction_started = True
            core.begin_transaction(
                root,
                folder / "staging",
                changed,
                removed,
                latest["release"],
                folder / "transaction",
            )
            health_script = "from app.services.project_paths import PROJECT_PATHS; from app.main_window import MainWindow; import update_core"
            checked = subprocess.run(
                [
                    str(root.parent / "runtime/gui/python.exe"),
                    "-B",
                    "-c",
                    health_script,
                ],
                cwd=root / "gui",
                capture_output=True,
                timeout=30,
                check=False,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            if checked.returncode:
                (folder / "module-check.log").write_bytes(checked.stdout or b"")
                with (folder / "module-check.log").open("ab") as output:
                    output.write(checked.stderr or b"")
                raise RuntimeError(
                    f"新版模块导入检查失败，将回滚。日志：{folder / 'module-check.log'}"
                )
        state["phase"] = "health"
        core.write_json(marker, state)
        health_file = folder / "health.json"
        replacement_gui = launch_gui(root, health_file=health_file, token=token)
        state["new_gui_pid"] = replacement_gui.pid
        core.write_json(marker, state)
        wait_for_startup(replacement_gui, health_file, token)
        if mode in ("incremental", "universal"):
            core.commit(folder / "transaction")
        core.write_json(
            folder / "result.json",
            {"status": "success", "build": latest["build"]},
        )
    except BaseException as error:
        if replacement_gui and replacement_gui.poll() is None:
            replacement_gui.terminate()
            replacement_gui.wait(timeout=15)
        journal = folder / "transaction/journal.json"
        if transaction_started and journal.is_file():
            core.rollback(journal.parent)
        core.write_json(
            folder / "result.json", {"status": "failed", "message": str(error)}
        )
        if not core.process_alive(job["parent_pid"]):
            # Remove the update gate before restarting the restored application.
            if owns_marker:
                marker.unlink(missing_ok=True)
                owns_marker = False
            launch_gui(root)
        raise
    finally:
        if owns_marker:
            journal = folder / "transaction/journal.json"
            pending_recovery = False
            if journal.is_file():
                try:
                    pending_recovery = core.loads(journal.read_bytes())[
                        "phase"
                    ] not in ("committed", "rolled_back")
                except (ValueError, KeyError, OSError):
                    pending_recovery = True
            if pending_recovery:
                # Keep the recovery gate if rollback itself was interrupted or
                # blocked by a locked file. A later launch must not skip it.
                state["phase"] = "recovery_required"
                core.write_json(marker, state)
            else:
                marker.unlink(missing_ok=True)
        if (
            operation.is_file()
            and core.loads(operation.read_bytes()).get("lock_id") == operation_id
        ):
            operation.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--job", type=Path, required=True)
    args = parser.parse_args()
    try:
        run(args.job)
    except Exception as error:  # noqa: BLE001 - independent updater must surface all failures
        try:
            core.write_json(args.job.parent / "failed.json", {"message": str(error)})
        finally:
            message(str(error))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
