"""L1 规则层测试。

覆盖：高危命令（``rm -rf`` / ``sudo`` / ``chmod 777`` / ``git push -f`` 等）、
死循环检测（连续读取同一文件）、正常事件，以及配置驱动的自定义规则。
"""

from __future__ import annotations

from src.rules.basic import (
    VERDICT_DANGEROUS,
    VERDICT_LOOP,
    VERDICT_ON_TRACK,
    RuleEngine,
)


def tool_event(name: str, arguments: dict) -> dict:
    """构造一个 tool_call 事件。"""
    command = arguments.get("command", "")
    return {
        "source": "mcode",
        "type": "tool_call",
        "session_id": None,
        "timestamp": None,
        "tool": {
            "name": name,
            "arguments": arguments,
            "command": command,
        },
        "text": "",
        "raw": {},
    }


def thinking_event(text: str = "思考中") -> dict:
    """构造一个 thinking 事件。"""
    return {
        "source": "mcode",
        "type": "thinking",
        "session_id": None,
        "timestamp": None,
        "tool": None,
        "text": text,
        "raw": {},
    }


# ---------------------------------------------------------------------------
# 高危命令
# ---------------------------------------------------------------------------
def test_dangerous_rm_rf_is_blocked():
    engine = RuleEngine(config={})
    verdict = engine.check_event(tool_event("Bash", {"command": "rm -rf /tmp/data"}))

    assert verdict["verdict"] == VERDICT_DANGEROUS
    assert verdict["reason"].startswith("匹配到高危命令:")
    assert "rm -rf" in verdict["reason"]


def test_dangerous_sudo_is_blocked():
    engine = RuleEngine(config={})
    verdict = engine.check_event(tool_event("Bash", {"command": "sudo apt-get install nginx"}))

    assert verdict["verdict"] == VERDICT_DANGEROUS
    assert "sudo" in verdict["reason"]


def test_dangerous_chmod_777_is_blocked():
    engine = RuleEngine(config={})
    verdict = engine.check_event(tool_event("Bash", {"command": "chmod 777 /var/data"}))

    assert verdict["verdict"] == VERDICT_DANGEROUS
    assert "chmod 777" in verdict["reason"]


def test_dangerous_git_push_force_is_blocked():
    engine = RuleEngine(config={})
    verdict = engine.check_event(
        tool_event("Bash", {"command": "git push -f origin main"})
    )

    assert verdict["verdict"] == VERDICT_DANGEROUS
    assert "-f" in verdict["reason"]


def test_dangerous_command_found_in_arguments_only():
    """命令藏在参数里（command 字段为空）同样要能被拦下。"""
    engine = RuleEngine(config={})
    event = tool_event("SomeTool", {"cmd": "sudo rm -rf /"})
    event["tool"]["command"] = ""

    verdict = engine.check_event(event)

    assert verdict["verdict"] == VERDICT_DANGEROUS


def test_safe_command_is_on_track():
    engine = RuleEngine(config={})
    verdict = engine.check_event(tool_event("Bash", {"command": "ls -la"}))

    assert verdict["verdict"] == VERDICT_ON_TRACK
    assert verdict["reason"] == ""


def test_thinking_event_is_on_track():
    engine = RuleEngine(config={})
    verdict = engine.check_event(thinking_event())

    assert verdict["verdict"] == VERDICT_ON_TRACK


def test_config_can_extend_dangerous_commands():
    """配置里追加的自定义高危命令要生效。"""
    engine = RuleEngine(
        config={"rules": {"dangerous_commands": [r"\bkubectl\s+delete\b"]}}
    )

    blocked = engine.check_event(
        tool_event("Bash", {"command": "kubectl delete pod my-app"})
    )
    allowed = engine.check_event(
        tool_event("Bash", {"command": "kubectl get pods"})
    )

    assert blocked["verdict"] == VERDICT_DANGEROUS
    assert "kubectl delete" in blocked["reason"]
    assert allowed["verdict"] == VERDICT_ON_TRACK


def test_config_can_replace_builtin_dangerous_commands():
    """``dangerous_commands_replace`` 为真时只认配置里的规则。"""
    engine = RuleEngine(
        config={
            "rules": {
                "dangerous_commands": [r"\bkubectl\s+delete\b"],
                "dangerous_commands_replace": True,
            }
        }
    )

    assert (
        engine.check_event(tool_event("Bash", {"command": "rm -rf /tmp/x"}))[
            "verdict"
        ]
        == VERDICT_ON_TRACK
    )
    assert (
        engine.check_event(
            tool_event("Bash", {"command": "kubectl delete pod x"})
        )["verdict"]
        == VERDICT_DANGEROUS
    )


# ---------------------------------------------------------------------------
# 死循环检测
# ---------------------------------------------------------------------------
def test_three_consecutive_reads_of_same_file_is_loop():
    engine = RuleEngine(config={})
    read = tool_event("Read", {"file_path": "D:/proj/app.py"})

    assert engine.check_event(read)["verdict"] == VERDICT_ON_TRACK
    assert engine.check_event(read)["verdict"] == VERDICT_ON_TRACK
    verdict = engine.check_event(read)

    assert verdict["verdict"] == VERDICT_LOOP
    assert "连续 3 次读取同一个文件" in verdict["reason"]
    assert "D:/proj/app.py" in verdict["reason"]


def test_reading_different_files_is_not_a_loop():
    engine = RuleEngine(config={})

    for path in ("a.py", "b.py", "a.py", "b.py"):
        verdict = engine.check_event(tool_event("Read", {"file_path": path}))
        assert verdict["verdict"] == VERDICT_ON_TRACK


def test_write_action_breaks_the_streak():
    """写入动作会打断「连续读取」的累积。"""
    engine = RuleEngine(config={})
    read = tool_event("Read", {"file_path": "a.py"})

    engine.check_event(read)
    engine.check_event(read)
    verdict = engine.check_event(tool_event("Write", {"file_path": "a.py"}))

    assert verdict["verdict"] == VERDICT_ON_TRACK


def test_loop_threshold_is_configurable():
    engine = RuleEngine(config={"rules": {"loop": {"same_file_read_threshold": 5}}})
    read = tool_event("Read", {"file_path": "a.py"})

    for _ in range(4):
        assert engine.check_event(read)["verdict"] == VERDICT_ON_TRACK
    assert engine.check_event(read)["verdict"] == VERDICT_LOOP


def test_loop_detector_can_be_disabled():
    engine = RuleEngine(config={"rules": {"loop": {"enabled": False}}})
    read = tool_event("Read", {"file_path": "a.py"})

    for _ in range(5):
        assert engine.check_event(read)["verdict"] == VERDICT_ON_TRACK


def test_loop_detector_state_is_exposed():
    """状态机对外可读，便于日志与调试。"""
    engine = RuleEngine(config={})
    read = tool_event("Read", {"file_path": "a.py"})

    engine.check_event(read)
    engine.check_event(read)

    assert engine._loop.state == "READING(a.py, 2)"
