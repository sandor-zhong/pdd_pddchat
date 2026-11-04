"""Order-local input interruptions must not diagnose a shop as unhealthy."""

INPUT_INTERRUPTION = "[INPUT_INTERFERENCE]"


class InputInterferenceError(RuntimeError):
    def __init__(self, message):
        super().__init__(f"{INPUT_INTERRUPTION} {message}")


def is_input_interference(value) -> bool:
    return INPUT_INTERRUPTION in str(value)


def order_error_result(error, prefix="失败: ") -> str:
    if is_input_interference(error):
        if str(error).startswith("跳过：鼠标受外部干预"):
            return str(error)
        return f"跳过：鼠标受外部干预，本单停止，继续下一单。{error}"
    return prefix + str(error)


def next_failure_streak(current: int, result, failed: bool) -> int:
    if is_input_interference(result):
        return current
    return current + 1 if failed else 0
