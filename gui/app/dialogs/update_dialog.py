from __future__ import annotations

from pathlib import Path

from PyQt5.QtCore import QThread, QTimer, pyqtSignal
from PyQt5.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
)

import update_core as core

from ..services import app_updates, msgbox
from ..services.app_logging import logger


class UpdateTask(QThread):
    result = pyqtSignal(object)
    failed = pyqtSignal(str)
    progress = pyqtSignal(int)

    def __init__(self, function, parent=None):
        super().__init__(parent)
        self.function = function

    def run(self):
        try:
            self.result.emit(self.function())
        except Exception as error:  # noqa: BLE001 - Qt thread boundary reports all failures
            logger.exception("应用更新失败")
            self.failed.emit(str(error))


class UpdateDialog(QDialog):
    def __init__(self, root, parent, busy, *, automatic=False):
        super().__init__(parent)
        self.root, self.busy, self.automatic = Path(root), busy, automatic
        self.plan, self.task = None, None
        self.setWindowTitle("检查软件更新")
        self.resize(540, 350)
        layout = QVBoxLayout(self)
        current = core.installed_release(root)
        self.status = QLabel("正在检查更新……")
        layout.addWidget(
            QLabel(
                f"当前版本：{current['version']} / {current['build']}"
                if current
                else "当前版本：开发版或旧安装版"
            )
        )
        layout.addWidget(self.status)
        self.notes = QTextBrowser()
        layout.addWidget(self.notes)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        layout.addWidget(self.progress)
        buttons = QHBoxLayout()
        self.install = QPushButton("下载并更新")
        self.install.setEnabled(False)
        self.install.clicked.connect(self._download)
        close = QPushButton("关闭")
        close.clicked.connect(self.reject)
        buttons.addWidget(self.install)
        buttons.addWidget(close)
        layout.addLayout(buttons)
        QTimer.singleShot(
            0, lambda: self._run(lambda: app_updates.check(self.root), self._checked)
        )

    def _run(self, function, callback):
        task = UpdateTask(function, self)
        self.task = task
        task.progress.connect(self.progress.setValue)
        task.result.connect(callback)
        task.failed.connect(self._failed)
        task.finished.connect(lambda: self._finished(task))
        task.start()

    def _finished(self, task):
        if self.task is task:
            self.task = None
        task.deleteLater()

    def _checked(self, plan):
        if not plan["available"]:
            self.status.setText(plan["message"])
            return
        self.plan = plan
        latest = plan["latest"]
        kind = {
            "incremental": "增量更新",
            "universal": "通用代码更新",
            "full": "完整安装包更新",
        }[plan["mode"]]
        self.status.setText(f"发现 {latest['version']} / {latest['build']}（{kind}）")
        notes = latest.get("release_notes", [])
        self.notes.setPlainText(
            "\n".join(notes)
            + f"\n\n下载大小：{plan['artifact']['size'] / 1024 / 1024:.1f} MB\n更新将关闭并重新启动程序，店铺数据保留。"
        )
        self.install.setEnabled(True)
        if self.automatic:
            if self.busy():
                self.parent().statusBar().showMessage(
                    "发现软件新版本，请在任务结束后通过“帮助 → 检查更新”安装。"
                )
            else:
                self.open()

    def _download(self):
        if self.task and self.task.isRunning():
            return
        reason = self.busy()
        if reason:
            msgbox.information(self, "暂不能安装更新", reason)
            return
        self.automatic = False
        self.install.setEnabled(False)
        self.status.setText("正在下载并校验……")

        def work():
            return app_updates.prepare(
                self.root,
                self.plan,
                lambda size, total: (
                    self.task.progress.emit(min(100, int(size * 100 / total)))
                    if total
                    else None
                ),
            )

        self._run(work, self._downloaded)

    def _downloaded(self, plan):
        reason = self.busy()
        if reason:
            self._failed("下载已完成，但现在有任务在运行。请停止任务后重新检查更新。")
            return
        self.status.setText("正在启动独立更新进程……")
        self._run(lambda: app_updates.start_agent(self.root, plan), self._agent_ready)

    def _agent_ready(self, _plan):
        self.status.setText("更新进程就绪，正在退出主程序……")
        # Let this QThread finish before closing its parent window.
        QTimer.singleShot(150, self._exit_for_update)

    def _exit_for_update(self):
        self.accept()
        self.parent().close()

    def _failed(self, message):
        self.status.setText("更新未完成，当前程序保持可用。")
        self.notes.setPlainText(message)
        self.install.setEnabled(self.plan is not None)
        if not self.automatic:
            msgbox.warning(self, "更新未完成", message)

    def reject(self):
        if self.task and self.task.isRunning():
            msgbox.information(self, "正在处理更新", "请等待当前检查或下载结束后关闭。")
            return
        super().reject()
