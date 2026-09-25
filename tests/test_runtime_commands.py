from qq_bot.health import ComponentHealth
from qq_bot.runtime_commands import _line


def test_runtime_status_normal_details_are_consistent() -> None:
    healthy = ComponentHealth(state="ok", detail="视奸正常")

    assert _line("QQ / NapCat", healthy) == "QQ / NapCat：正常（已连接）"
    assert _line("视频解析", healthy) == "视频解析：正常（正常）"
    assert _line("Switch", healthy) == "Switch：正常（正常）"
    assert _line("PS5", healthy) == "PS5：正常（正常）"
    assert _line("Steam", healthy) == "Steam：正常（正常）"
    assert _line("Xbox", healthy) == "Xbox：正常（正常）"


def test_runtime_status_keeps_failure_detail() -> None:
    failed = ComponentHealth(state="error", detail="服务超时")

    assert _line("Steam", failed) == "Steam：异常（服务超时）"
