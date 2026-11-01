"""演示模式 —— 生成一段**模拟**的 Agent 流式日志。

为什么需要它
============
用户第一次双击 ``启动哨兵.bat`` 时，多半**还不知道哨兵长什么样**。
弹一篇帮助文档让人自己猜「所以这东西有什么用」，等于把用户挡在门外。

所以不如直接生成一段「假的 Agent 输出」，让看板上**立刻**出现
真实的红线告警卡片 —— 一眼就知道这东西是干什么的。

这份日志是**模拟的**，不是真的 Agent 跑的；
但格式与 mcode 的 ``stream-json`` **完全一致**，
所以走的是同一套适配器、同一套规则引擎、同一条告警落盘链路。
演示里看到的红线卡片，和真实接管时看到的**是同一种东西**。

演示里为什么放了一条红线命令
=============================
只放正常事件的话，看板上空空如也 —— 用户还是不知道这东西有什么用。
放一条 ``catastrophic_mass_delete``（批量删除），看板就会亮起一张
**红色告警卡片**，连人话解释都是真的（走的是本地模型）。

⚠️ 注意：演示日志里的命令**只是被写进日志文件**，
哨兵在这里是**审查**它，不是**执行**它。
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List

__all__ = [
    "DEMO_ALERT_LOG_NAME",
    "DEMO_LINES",
    "DEMO_TASK_GOAL",
    "DEMO_WATCH_LOG_NAME",
    "generate_demo_log",
]

#: 演示用的被监控日志文件名（相对 ``logs/``）。
DEMO_WATCH_LOG_NAME = "demo_session.jsonl"

#: 演示用的告警日志文件名（相对 ``logs/``）。
DEMO_ALERT_LOG_NAME = "demo_alert.log"

#: 演示会话的任务目标 —— 看板的「当前监控目标」会显示它。
DEMO_TASK_GOAL = "在当前目录创建 hello.py 并运行它"

#: 演示日志的事件序列（mcode ``stream-json`` 扁平格式）。
#:
#: 刻意包含三类内容：
#:
#: 1. 正常事件（思考 / 读目录 / 写文件）—— 让日志区有东西滚；
#: 2. 一条**红线级**命令（``find … -delete``）—— 触发红色告警卡片；
#: 3. 一条工具回执 —— 让上下文追踪器有东西可判断。
DEMO_LINES: List[Dict[str, Any]] = [
    {
        "type": "user_message",
        "content": DEMO_TASK_GOAL,
    },
    {
        "type": "thinking",
        "thinking": "先看看当前目录有哪些文件，再决定怎么写。",
    },
    {
        "type": "tool_call",
        "tool_call": {"name": "Bash", "arguments": {"command": "ls -la"}},
    },
    {
        "type": "tool_result",
        "tool_result": {"status": "ok", "output": "README.md\nsrc\ntests"},
    },
    {
        "type": "text",
        "text": "目录里有 README.md、src 和 tests，看起来是个普通项目。",
    },
    {
        "type": "thinking",
        "thinking": "顺手把项目里的临时文件清理掉吧，用 find 批量删。",
    },
    {
        "type": "tool_call",
        "tool_call": {
            "name": "Bash",
            "arguments": {"command": "find . -name '*.tmp' -delete"},
        },
    },
    {
        "type": "tool_result",
        "tool_result": {"status": "ok", "output": ""},
    },
    {
        "type": "tool_call",
        "tool_call": {
            "name": "Bash",
            "arguments": {"command": "echo 'print(\"hello\")' > hello.py"},
        },
    },
    {
        "type": "text",
        "text": "hello.py 已创建。",
    },
    {
        "type": "result",
        "result": {"status": "ok"},
    },
]


def generate_demo_log(log_dir: str = "logs") -> str:
    """生成演示用的被监控日志文件。

    每次调用都会**覆盖**旧文件 —— 演示就该从干净状态开始，
    否则看板上会堆着上一次演示的告警。

    :param log_dir: 日志目录（默认 ``logs``）。
    :return: 生成的日志文件**绝对路径**。
    """
    target_dir = os.path.abspath(log_dir)
    os.makedirs(target_dir, exist_ok=True)
    path = os.path.join(target_dir, DEMO_WATCH_LOG_NAME)
    with open(path, "w", encoding="utf-8") as handle:
        for payload in DEMO_LINES:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
    return path
