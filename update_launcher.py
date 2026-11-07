"""Stable installed shortcut target; recovers interrupted updates before GUI imports."""

import ctypes
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from update_agent import launch_gui, recover_pending


def main():
    root = Path(__file__).resolve().parent.parent / "app"
    try:
        recover_pending(root)
        launch_gui(root)
    except Exception as error:  # noqa: BLE001 - bootstrap cannot leave startup errors invisible
        ctypes.windll.user32.MessageBoxW(None, str(error), "拼多多申诉 — 启动", 0x10)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
