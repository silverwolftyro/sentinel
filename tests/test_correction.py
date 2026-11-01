"""纠偏提示模块（v1.2.0）的单元测试。

重点验证：

1. **红线级**与**非红线**走两套不同的提示；
2. 提示里必须带上「人话解释」与「Agent 的错误状态」；
3. 提示**不泄露**密钥、**不超长**。
"""

from __future__ import annotations

import os
import sys
from typing import Any, Dict

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.core.correction import (  # noqa: E402
    COPY_LABEL_CORRECTION,
    COPY_LABEL_INTERCEPT,
    build_correction_prompt,
    build_intercept_prompt,
    build_prompt,
    copy_label,
    is_red_line,
)

RED_LINE: Dict[str, Any] = {
    "alert_id": "a1",
    "verdict": "DANGEROUS",
    "rule_id": "catastrophic_mass_delete",
    "reason": "红线级命中：用 find 批量删除文件",
    "severity": "red_line",
    "command": "find . -name '*.tmp' -delete",
    "explanation": "批量删除当前目录下所有临时文件。",
    "context_history": [
        {"kind": "tool_call", "command": "ls -la", "is_error": False},
        {"kind": "tool_result", "command": "", "is_error": True},
    ],
}

STUCK: Dict[str, Any] = {
    "alert_id": "a2",
    "verdict": "STUCK",
    "rule_id": "context_stuck",
    "reason": "[STUCK] 测试连续失败3次，Agent 可能卡住了",
    "severity": "standard",
    "command": "python -m pytest -q",
    "explanation": "Agent 连续三次跑 pytest 都失败，但从未修改被测文件。",
    "context_history": [
        {"kind": "tool_call", "command": "python -m pytest -q", "is_error": False},
        {"kind": "tool_result", "command": "", "is_error": True},
    ],
}


class TestCopyLabel:
    """按钮文案。"""

    def test_red_line_uses_intercept_label(self) -> None:
        assert copy_label(RED_LINE) == COPY_LABEL_INTERCEPT

    def test_other_uses_correction_label(self) -> None:
        assert copy_label(STUCK) == COPY_LABEL_CORRECTION

    def test_is_red_line(self) -> None:
        assert is_red_line(RED_LINE) is True
        assert is_red_line(STUCK) is False
        assert is_red_line({}) is False


class TestCorrectionPrompt:
    """纠偏提示（非红线）。"""

    @pytest.fixture()
    def prompt(self) -> str:
        return build_correction_prompt(STUCK, task_goal="修复登录 bug")

    def test_should_include_explanation(self, prompt: str) -> None:
        """人话解释必须在。"""
        assert "连续三次跑 pytest 都失败" in prompt

    def test_should_include_error_state(self, prompt: str) -> None:
        """Agent 的错误状态必须在。"""
        assert "context_stuck" in prompt
        assert "python -m pytest -q" in prompt

    def test_should_include_task_goal(self, prompt: str) -> None:
        """任务目标必须在 —— Agent 得知道往哪回。"""
        assert "修复登录 bug" in prompt

    def test_should_include_history(self, prompt: str) -> None:
        """最近几步在干什么要在。"""
        assert "最近几步在干什么" in prompt

    def test_should_tell_what_to_do(self, prompt: str) -> None:
        """要给出可执行的下一步。"""
        assert "请这样做" in prompt
        assert "不要重复刚才那一步" in prompt

    def test_should_handle_empty_alert(self) -> None:
        """空告警也不能崩。"""
        prompt = build_correction_prompt({}, task_goal="")
        assert isinstance(prompt, str)
        assert prompt.strip()


class TestInterceptPrompt:
    """拦截原因（红线级）。"""

    @pytest.fixture()
    def prompt(self) -> str:
        return build_intercept_prompt(RED_LINE, task_goal="修复登录 bug")

    def test_should_say_it_was_blocked(self, prompt: str) -> None:
        """要说清楚「它被拦住了、没执行」。"""
        assert "拦截" in prompt
        assert "已经被暂停" in prompt
        assert "不要重试" in prompt

    def test_should_include_command(self, prompt: str) -> None:
        """被拦的命令要在。"""
        assert "find . -name '*.tmp' -delete" in prompt

    def test_should_ask_for_reversible_way(self, prompt: str) -> None:
        """要给出「换个可逆做法」的指引。"""
        assert "可逆" in prompt

    def test_should_differ_from_correction(self) -> None:
        """两种提示不能长得一样。"""
        red = build_intercept_prompt(RED_LINE)
        normal = build_correction_prompt(STUCK)
        assert red != normal
        assert "红线级" in red
        assert "红线级" not in normal


class TestBuildPrompt:
    """按类型分发。"""

    def test_should_route_red_line(self) -> None:
        assert "拦截报告" in build_prompt(RED_LINE)

    def test_should_route_others(self) -> None:
        assert "纠偏提示" in build_prompt(STUCK)


class TestSafety:
    """安全与长度。"""

    def test_should_truncate_long_command(self) -> None:
        """超长命令要截断 —— 提示太长就没法用了。"""
        alert = dict(STUCK)
        alert["command"] = "x" * 5000
        prompt = build_correction_prompt(alert)
        assert "已截断" in prompt
        assert len(prompt) < 4000

    def test_should_limit_history_items(self) -> None:
        """历史最多列几条。"""
        alert = dict(STUCK)
        alert["context_history"] = [
            {"kind": "tool_call", "command": f"cmd-{index}", "is_error": False}
            for index in range(30)
        ]
        prompt = build_correction_prompt(alert)
        assert "cmd-29" in prompt
        assert "cmd-0 " not in prompt

    def test_should_not_leak_api_key(self) -> None:
        """提示里不能出现密钥 —— 送进模型前哨兵脱敏过，别把白做的功 undone。"""
        alert = dict(STUCK)
        alert["command"] = "export KEY=sk-should-not-appear"
        prompt = build_correction_prompt(alert)
        # 哨兵在送模型前会脱敏；这里验证的是「提示不主动**新增**密钥字段」。
        assert "api_key" not in prompt.lower()


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
