"""Import a per-device upload credential; never blocks the order worker."""

import json

from PyQt5.QtCore import QTimer, QUrl
from PyQt5.QtGui import QDesktopServices
from PyQt5.QtWidgets import (
    QCheckBox,
    QDialog,
    QFileDialog,
    QLabel,
    QPushButton,
    QVBoxLayout,
)

from ..services import msgbox, remote_logging


class RemoteLogDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("远程运行日志")
        self.resize(560, 260)
        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel("先在日志网页生成设备配置，再导入运行电脑。仅上报运行窗口日志。")
        )
        self.enabled = QCheckBox("启用远程日志上传")
        try:
            self.config = remote_logging.load_config()
        except (OSError, ValueError):
            self.config = {"enabled": False}
        self.enabled.setChecked(bool(self.config.get("enabled")))
        layout.addWidget(self.enabled)
        self.info = QLabel()
        self.info.setWordWrap(True)
        layout.addWidget(self.info)
        import_button = QPushButton("导入设备配置 JSON")
        import_button.clicked.connect(self.import_config)
        layout.addWidget(import_button)
        open_button = QPushButton("打开日志查看网页")
        open_button.clicked.connect(
            lambda: QDesktopServices.openUrl(
                QUrl(self.config.get("endpoint", "https://flowtry.top/pdd-logs/"))
            )
        )
        layout.addWidget(open_button)
        save = QPushButton("保存设置")
        save.clicked.connect(self.save)
        layout.addWidget(save)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(1000)
        self.refresh()

    def refresh(self):
        self.info.setText(
            "设备："
            + str(self.config.get("name", "未配置"))
            + "\n"
            + remote_logging.status()
        )

    def import_config(self):
        path, _ = QFileDialog.getOpenFileName(self, "导入设备配置", "", "JSON (*.json)")
        if not path:
            return
        try:
            with open(path, encoding="utf-8-sig") as stream:
                value = json.loads(stream.read(32769))
            self.config = remote_logging.validate_config({**value, "enabled": True})
            self.enabled.setChecked(True)
            self.refresh()
        except (OSError, ValueError, TypeError) as error:
            msgbox.warning(self, "配置无效", str(error))

    def save(self):
        try:
            self.config = remote_logging.save_config(
                {**self.config, "enabled": self.enabled.isChecked()}
            )
            remote_logging.configure()
            self.refresh()
        except (OSError, ValueError, TypeError) as error:
            msgbox.warning(self, "保存失败", str(error))
