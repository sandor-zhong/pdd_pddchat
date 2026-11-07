"""GUI 入口：`uv run python -m app.main`。"""

from __future__ import annotations

import os
import sys
import threading
import time

from PyQt5.QtWidgets import QApplication

# 必须先 import project_paths——它负责把主项目根目录加进 sys.path，
# 后续任何用到 db/scheduler_core 的模块（本文件下面 import 的
# main_window 及其间接依赖）才能正常工作。
from .services import msgbox
from .services.app_logging import logger, setup_logging
from .services.project_paths import PROJECT_PATHS

_APPLICATION_STYLE = """
QPushButton:disabled, QToolButton:disabled {
    color: #98a2b3;
    background-color: #eaecf0;
    border: 1px solid #d0d5dd;
}
"""


def main() -> None:
    # 尽早配置日志（QApplication 构造之前也行，但放前面能顺带兜住极早期
    # 的初始化异常），后续任何未捕获异常/页面打印的诊断信息都会落盘。
    setup_logging()
    startup_started = time.monotonic()

    def stage(name):
        logger.info(
            "[启动阶段] PID=%s 阶段=%s 累计耗时=%.2f秒",
            os.getpid(),
            name,
            time.monotonic() - startup_started,
        )

    stage("初始化开始")

    # 必须在 QApplication/MainWindow 构造前启动，后续诊断包才能区分：
    # 是 Python 对象持续被持有，还是 Qt/浏览器一侧的原生内存增长。
    import memory_monitor

    memory_monitor.start_python_heap_tracking()
    stage("内存跟踪已启用")

    app = QApplication(sys.argv)
    stage("Qt应用已创建")
    from update_agent import recover_pending

    try:
        recover_pending(PROJECT_PATHS.root)
    except Exception as error:  # noqa: BLE001 - surface recovery failures before loading business pages
        msgbox.warning(None, "软件更新", str(error))
        sys.exit(1)
    # Windows 不同主题对 disabled 状态的默认呈现差异很大，有的只略微
    # 改变文字颜色，运营很难判断按钮是否可点。应用级统一成明显的灰色；
    # 个别页面的蓝色主按钮/红色停止按钮再用更具体的 :disabled 规则覆盖。
    app.setStyleSheet(_APPLICATION_STYLE)
    stage("更新恢复检查完成")

    problems = PROJECT_PATHS.validate()
    if problems:
        msgbox.critical(
            None,
            "配置有误",
            "检测到路径配置问题，请检查 gui/config.json：\n\n" + "\n".join(problems),
        )
        sys.exit(1)

    # 延后 import：跟下面的 main_window 同理，需要上面的路径校验先跑完，
    # import scheduler_core 才不会因为 config.json 配错而在这里
    # ModuleNotFoundError 崩溃（见 single_instance.py 顶部说明）。
    from .services import single_instance

    if not single_instance.try_acquire():
        msgbox.warning(
            None,
            "已经在运行",
            "「拼多多申诉」已经在运行了，不需要重复打开。\n\n"
            "如果找不到已经打开的窗口，看看是不是被其它窗口挡住了，"
            "或者被最小化到了任务栏。",
        )
        sys.exit(1)

    try:
        from .services import remote_logging

        remote_logging.configure()
        app.aboutToQuit.connect(remote_logging.stop)
        stage("远程日志已配置")
        from .main_window import MainWindow  # 延后 import：确保上面的路径校验先跑完

        stage("窗口模块已导入")

        window = MainWindow()
        stage("主窗口已构建")
        memory_monitor.configure(role="gui", phase="running")
        memory_stop = memory_monitor.start_periodic()
        app.aboutToQuit.connect(memory_stop.set)
        app.aboutToQuit.connect(lambda: memory_monitor.record("gui_shutdown"))
        # Process enumeration/heap snapshots can be slow on the remote computer.
        # Keep them off the GUI startup path and retain a baseline after readiness.
        window.show()
        window.install_stop_hotkey()
        stage("窗口已显示，快捷键已初始化")
        from PyQt5.QtCore import QTimer

        import update_core

        def confirm_startup():
            stage("事件循环启动确认")
            update_core.acknowledge_startup(PROJECT_PATHS.root)
            threading.Thread(
                target=memory_monitor.record,
                args=("gui_started",),
                name="startup-memory-diagnostics",
                daemon=True,
            ).start()

        QTimer.singleShot(3000, confirm_startup)
        sys.exit(app.exec_())
    finally:
        single_instance.release()


if __name__ == "__main__":
    main()
