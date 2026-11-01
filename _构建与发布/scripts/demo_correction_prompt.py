"""演示「一键复制纠偏提示」的两种产出形态（v1.2.0）。

用途：
1. 让开发者**肉眼**检查提示文本写得像不像人话；
2. 给文档 / 验收提供**可复现**的样例。

用法::

    python _构建与发布/scripts/demo_correction_prompt.py
"""

from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from src.core.correction import build_prompt, copy_label  # noqa: E402

#: 一条**红线级**告警（被守护者拦下）。
RED_LINE_ALERT = {
    "alert_id": "demo-redline",
    "verdict": "DANGEROUS",
    "level": "critical",
    "rule_id": "catastrophic_mass_delete",
    "reason": "红线级命中：用 find 批量删除文件",
    "severity": "red_line",
    "command": "find . -name '*.tmp' -delete",
    "explanation": (
        "批量删除当前目录下所有临时文件。影响：当前目录及所有子目录里"
        "名字以 .tmp 结尾的临时文件（会被直接删除，不进回收站）。"
        "后果：这些临时文件会被永久删掉，若其中混着别的程序正在用的数据就找不回来了。"
    ),
    "context_history": [
        {"kind": "tool_call", "command": "ls -la", "is_error": False},
        {"kind": "tool_result", "command": "", "is_error": True},
    ],
}

#: 一条 ``[STUCK]`` 告警（Agent 卡在死循环里）。
STUCK_ALERT = {
    "alert_id": "demo-stuck",
    "verdict": "STUCK",
    "level": "warn",
    "rule_id": "context_stuck",
    "reason": "[STUCK] 测试连续失败3次，Agent 可能卡住了",
    "severity": "standard",
    "command": "python -m pytest -q",
    "explanation": (
        "Agent 连续三次运行 pytest 都失败，但从未修改被测文件 —— "
        "它在重复同一步骤，而不是在排查失败原因。"
    ),
    "context_history": [
        {"kind": "tool_call", "command": "python -m pytest -q", "is_error": False},
        {"kind": "tool_result", "command": "", "is_error": True},
        {"kind": "tool_call", "command": "python -m pytest -q", "is_error": False},
        {"kind": "tool_result", "command": "", "is_error": True},
        {"kind": "tool_call", "command": "python -m pytest -q", "is_error": False},
        {"kind": "tool_result", "command": "", "is_error": True},
    ],
}

TASK_GOAL = "重构 auth 模块并修复登录 bug"


def main() -> int:
    """打印两种形态的纠偏提示。"""
    for title, alert in (
        ("红线级拦截", RED_LINE_ALERT),
        ("[STUCK] 卡死", STUCK_ALERT),
    ):
        print("=" * 70)
        print(f"  {title} → 看板按钮文案：【{copy_label(alert)}】")
        print("=" * 70)
        print(build_prompt(alert, task_goal=TASK_GOAL))
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
