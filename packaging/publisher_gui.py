"""Developer release console. Run with gui/.venv; never shipped to customers."""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from PyQt5.QtCore import QThread, pyqtSignal
from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import (
    QApplication,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

sys.path.insert(0, str(Path(__file__).resolve().parent))
import publisher_backend as backend

ROOT = backend.ROOT
STATE = ROOT / "packaging/publisher-state"


class Task(QThread):
    line = pyqtSignal(str)
    progress = pyqtSignal(int)
    done = pyqtSignal(object)
    failed = pyqtSignal(str)
    built = pyqtSignal(str)

    def __init__(self, work, parent):
        super().__init__(parent)
        self.work = work

    def run(self):
        try:
            self.done.emit(self.work(self.line.emit, self.progress.emit))
        except Exception as error:  # noqa: BLE001 - worker boundary reports errors to the GUI
            self.failed.emit(str(error))


class Publisher(QWidget):
    def __init__(self):
        super().__init__()
        self.task = None
        self.setWindowTitle("拼多多申诉 · 发布工具")
        self.resize(900, 820)
        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel("构建 → 通用代码包与签名 → SFTP上传 → 最后发布清单 → HTTPS验证")
        )
        release_box = QGroupBox("发布内容")
        form = QFormLayout(release_box)
        self.version = QLineEdit()
        current = json.loads(
            (ROOT / "release_config.json").read_text(encoding="utf-8-sig")
        )["version"]
        parts = current.split(".")
        self.version.setText(".".join(parts[:-1] + [str(int(parts[-1]) + 1)]))
        form.addRow("新版本号", self.version)
        self.mode = QComboBox()
        self.mode.addItem("完整发布（安装包＋通用代码包）", "full")
        self.mode.addItem("日常通用代码发布（无需基线，只上传代码包）", "universal")
        form.addRow("发布模式", self.mode)
        self.notes = QTextEdit()
        self.notes.setMaximumHeight(90)
        self.notes.setPlaceholderText("每行一条更新说明，例如：修复某项问题")
        form.addRow("更新说明", self.notes)
        layout.addWidget(release_box)
        server_box = QGroupBox("自动上传（SFTP / SSH）")
        server = QFormLayout(server_box)
        self.fields = {}
        for key, title, default in [
            ("host", "服务器主机 / IP", "flowtry.top"),
            ("port", "SSH端口", "22"),
            ("username", "用户名", ""),
            ("directory", "更新目录（服务器绝对路径）", ""),
            ("fingerprint", "主机指纹 SHA256（已在known_hosts中的主机可留空）", ""),
            ("key_file", "SSH私钥文件（密码登录可留空）", ""),
        ]:
            edit = QLineEdit(default)
            self.fields[key] = edit
            server.addRow(title, edit)
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.Password)
        self.password.setPlaceholderText("仅本次使用，不保存；密钥登录时可填密钥口令")
        server.addRow("密码 / 私钥口令", self.password)
        buttons = QHBoxLayout()
        self.save = QPushButton("保存配置")
        self.test = QPushButton("测试连接")
        self.save.clicked.connect(self.save_profile)
        self.test.clicked.connect(self.test_connection)
        buttons.addWidget(self.save)
        buttons.addWidget(self.test)
        server.addRow(buttons)
        layout.addWidget(server_box)
        row = QHBoxLayout()
        self.build = QPushButton("仅生成发布包")
        self.publish = QPushButton("一键构建并上传")
        self.upload = QPushButton("上传已有发布包")
        self.open = QPushButton("打开发布目录")
        for button in (self.build, self.publish, self.upload, self.open):
            row.addWidget(button)
        self.build.clicked.connect(lambda: self.start_build(False))
        self.publish.clicked.connect(lambda: self.start_build(True))
        self.upload.clicked.connect(self.start_upload)
        self.open.clicked.connect(
            lambda: os.startfile(str(self.last_folder or ROOT / "dist"))
        )
        layout.addLayout(row)
        self.status = QLabel(
            "就绪。首次测试连接如提示新主机指纹，请核对后填写；服务器需支持原子重命名。"
        )
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.progress = QProgressBar()
        layout.addWidget(self.progress)
        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        self.output.setMaximumBlockCount(2500)
        layout.addWidget(self.output, 1)
        self.last_folder = None
        profile = STATE / "profile.json"
        if profile.exists():
            config = json.loads(profile.read_text(encoding="utf-8"))
            self.mode.setCurrentIndex(
                max(0, self.mode.findData(config.get("mode", "full")))
            )
            for key, edit in self.fields.items():
                edit.setText(str(config.get(key, edit.text())))
            saved = config.get("last_folder")
            if saved and Path(saved).is_dir():
                self.last_folder = Path(saved)

    def config(self):
        return {key: edit.text().strip() for key, edit in self.fields.items()}

    def save_profile(self):
        STATE.mkdir(exist_ok=True)
        backend.core.write_json(
            STATE / "profile.json",
            {
                **self.config(),
                "last_folder": str(self.last_folder or ""),
                "mode": self.mode.currentData(),
            },
        )
        self.status.setText("连接配置已保存，密码没有保存。")

    def append(self, message):
        # No credentials are ever passed through logs or subprocess arguments.
        if self.password.text():
            message = message.replace(self.password.text(), "***")
        self.output.appendPlainText(message)
        if message.startswith(("上传", "校验", "安装包和增量包", "服务器清单")):
            self.status.setText(message)
        STATE.mkdir(exist_ok=True)
        with (STATE / "publisher.log").open("a", encoding="utf-8") as stream:
            stream.write(
                datetime.now(UTC).astimezone().strftime("[%Y-%m-%d %H:%M:%S] ")
                + message
                + "\n"
            )

    def run_task(self, work):
        if self.task:
            return
        self.save_profile()
        for widget in (
            self.build,
            self.publish,
            self.upload,
            self.test,
            self.save,
            self.version,
            self.mode,
            self.notes,
            self.password,
            *self.fields.values(),
        ):
            widget.setEnabled(False)
        self.status.setText("处理中，请等待……")
        self.progress.setRange(0, 0)
        task = Task(work, self)
        self.task = task
        task.line.connect(self.append)
        task.progress.connect(self.set_progress)
        task.done.connect(self.completed)
        task.built.connect(self.remember_folder)
        task.failed.connect(self.failed)
        task.finished.connect(lambda: self.finished(task))
        task.start()

    def set_progress(self, value):
        self.progress.setRange(0, 100)
        self.progress.setValue(value)

    def remember_folder(self, folder):
        self.last_folder = Path(folder)
        self.save_profile()

    def completed(self, result):
        if isinstance(result, dict) and result.get("folder"):
            self.last_folder = Path(result["folder"])
            self.save_profile()
            if result.get("published"):
                backend.core.write_json(STATE / "last-published.json", result)
        self.set_progress(100)
        text = result if isinstance(result, str) else result["message"]
        self.status.setText(text)
        self.append(text)

    def failed(self, error):
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.status.setText(
            "失败，详细信息见日志。已生成的发布包会保留，可直接重试上传。"
        )
        self.append("失败：" + error)
        QMessageBox.warning(
            self,
            "发布未完成",
            error.replace(self.password.text(), "***")
            if self.password.text()
            else error,
        )

    def finished(self, task):
        self.task = None
        for widget in (
            self.build,
            self.publish,
            self.upload,
            self.test,
            self.save,
            self.version,
            self.mode,
            self.notes,
            self.password,
            *self.fields.values(),
        ):
            widget.setEnabled(True)
        task.deleteLater()

    def test_connection(self):
        config, password = self.config(), self.password.text()
        self.run_task(lambda log, progress: backend.check_connection(config, password))

    def start_build(self, upload):
        version, notes = (
            self.version.text().strip(),
            self.notes.toPlainText().strip(),
        )
        config, password = self.config(), self.password.text()
        mode = self.mode.currentData()
        try:
            backend.validate_version(version)
            if not notes:
                raise ValueError("请填写本次更新说明。")
            if upload:
                backend.validate_server(config)
        except ValueError as error:
            QMessageBox.warning(self, "请检查输入", str(error))
            return

        def work(log, progress):
            if upload:
                log(backend.check_connection(config, password))
            folder = backend.build_release(version, None, notes, log, mode=mode)
            self.task.built.emit(folder)
            log("发布文件已生成，可直接使用“上传已有发布包”重试：" + folder)
            if upload:
                backend.upload_bundle(folder, config, password, log, progress)
            return {
                "folder": folder,
                "published": upload,
                "message": "发布成功，HTTPS验证通过。"
                if upload
                else "发布文件已生成，尚未上传。",
            }

        self.run_task(work)

    def start_upload(self):
        folder = QFileDialog.getExistingDirectory(
            self,
            "选择含更新清单的发布目录",
            str(self.last_folder or ROOT / "dist/updates"),
        )
        if not folder:
            return
        self.last_folder = Path(folder)
        config, password = self.config(), self.password.text()

        def work(log, progress):
            backend.upload_bundle(folder, config, password, log, progress)
            return {
                "folder": folder,
                "published": True,
                "message": "发布成功，HTTPS验证通过。",
            }

        self.run_task(work)

    def closeEvent(self, event):
        if self.task:
            QMessageBox.information(
                self, "正在发布", "请等待当前操作结束后再关闭，避免中断打包或上传。"
            )
            event.ignore()
        else:
            event.accept()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setFont(QFont("Microsoft YaHei", 10))
    window = Publisher()
    window.show()
    sys.exit(app.exec_())
