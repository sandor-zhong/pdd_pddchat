"""Order-local input interruptions must not diagnose a shop as unhealthy."""

INPUT_INTERRUPTION = "[INPUT_INTERFERENCE]"


class InputInterferenceError(RuntimeError):
    def __init__(self, message):
        super().__init__(f"{INPUT_INTERRUPTION} {message}")


class CursorPositionError(InputInterferenceError):
    """Input interruption: positioning failed, without evidence of human input."""


class MouseTargetChangedError(InputInterferenceError):
    """Input interruption: the screen target changed after aiming."""


class DesktopUnavailableError(InputInterferenceError):
    """The input desktop cannot currently receive native mouse actions."""


class ForegroundUnavailableError(InputInterferenceError):
    """A usable desktop exists but the browser could not safely gain focus."""


def is_input_interference(value) -> bool:
    return INPUT_INTERRUPTION in str(value)


def order_error_result(error, prefix="失败: ") -> str:
    if is_input_interference(error):
        if str(error).startswith(("跳过：鼠标受外部干预", "跳过：系统鼠标操作中断")):
            return str(error)
        return f"跳过：系统鼠标操作中断，本单停止，继续下一单。{error}"
    return prefix + str(error)


def next_failure_streak(current: int, result, failed: bool) -> int:
    if is_input_interference(result):
        return current
    return current + 1 if failed else 0
