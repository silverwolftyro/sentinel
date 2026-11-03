"""v2.3.0 回归：雷达挂载（Attach 模式）与会话识别。

背景
====

v2.2.x 之前，哨兵的架构要求「用户必须**通过哨兵**来启动 Agent」。
这很不合理 —— 哨兵应该是**雷达**：能直接发现并监控用户**已经在终端里**
跑着的 CLI。

这一版走**旁路监听**方案：操作系统层面没法拦截已运行进程的 stdout，
所以哨兵扫描 Agent 的本地会话目录，找出**正在被活跃写入**的日志，
再把它们接进来实时审查。

本文件钉住三件事：

1. **发现** —— ``/api/discover`` 能列出活跃日志；
2. **挂载** —— ``/api/attach`` 能把外部日志接成一个会话；
3. **识别** —— 会话名自动带上「[Codex] D:\\work\\project_alpha」，
   提取不到工作区时明说「未知工作区」。
"""

from __future__ import annotations

import os

from src.radar import describe_session, detect_agent, format_label

#: 一段典型的 Codex 会话日志（含 cwd）。
CODEX_LINES = [
    '{"type":"thread.started","thread_id":"01a17af0-0000"}',
    '{"type":"turn.started"}',
    '{"type":"item.completed","item":{"id":"item_0",'
    '"type":"agent_message","text":"已经在当前目录创建了 hi.py"}}',
]

#: 一段典型的 MCode 会话日志。
MCODE_LINES = [
    '{"type":"thinking","thinking":"我先看看目录"}',
    '{"type":"tool_call","tool_call":{"name":"Bash",'
    '"arguments":{"command":"ls -la"}}}',
]


def _write_jsonl(path: str, lines) -> None:
    """写一份 JSONL 日志。"""
    with open(path, "w", encoding="utf-8") as handle:
        for line in lines:
            handle.write(line + "\n")


def _no_exclude_config(tmp_path) -> str:
    """造一份「**什么都不排除**」的配置文件。

    ⚠️ 为什么需要（v3.4.0）
    --------------------

    v3.4.0 起雷达的默认排除清单里有一条是 ``test``，
    而 pytest 的临时目录名恰好带 ``test``
    （``...\\pytest-of-xxx\\pytest-1\\test_xxx0``）——
    于是**所有**建在 ``tmp_path`` 下的日志都会被默认清单挡掉。

    这个用例测的是「发现接口能列出日志」，
    不是「排除逻辑」—— 所以显式给一份空清单，
    把两件事分开测。

    :param tmp_path: pytest 的临时目录。
    :return: 配置文件路径。
    """
    config = tmp_path / "no-exclude.yaml"
    config.write_text("radar:\n  exclude_keywords: []\n", encoding="utf-8")
    return str(config)




# ---------------------------------------------------------------------------
# 任务二：文件类型的自动识别
# ---------------------------------------------------------------------------
def test_按内容识别Codex():
    """codex 的事件流要能认出来。"""
    assert detect_agent("/tmp/unknown.jsonl", CODEX_LINES) == "codex"


def test_按内容识别MCode():
    """mcode 的事件流要能认出来。"""
    assert detect_agent("/tmp/unknown.jsonl", MCODE_LINES) == "mcode"


def test_按内容识别Claude():
    """Claude 的消息容器要能认出来。"""
    lines = [
        '{"type":"assistant","message":{"content":'
        '[{"type":"tool_use","name":"Bash","input":{"command":"ls"}}]}}'
    ]
    assert detect_agent("/tmp/unknown.jsonl", lines) == "claude"


def test_识别不出就是未知():
    """认不出来就说「未知」—— 不许瞎猜。"""
    assert detect_agent("/tmp/unknown.jsonl", ["不是 JSON"]) == "unknown"


def test_会话名带工作区():
    """任务二要求的展示格式。"""
    assert format_label("codex", "D:\\work\\project_alpha") == (
        "[Codex] D:\\work\\project_alpha"
    )


def test_没有工作区就说未知():
    """提取不到 cwd 时，**明确**标成「未知工作区」。"""
    assert format_label("codex", "") == "[Codex] 未知工作区"


def test_描述会话能取出工作区(tmp_path):
    """从日志里把 cwd 挖出来。"""
    path = tmp_path / "rollout.jsonl"
    _write_jsonl(
        str(path),
        ['{"type":"turn.started","cwd":"D:\\\\work\\\\project_alpha"}'],
    )
    described = describe_session(str(path))
    assert described is not None
    assert described.cwd == "D:\\work\\project_alpha"
    assert described.label == "[Codex] D:\\work\\project_alpha"




























def test_挂载不会写被监控的目录(tmp_path):
    """红线 R1：哨兵对被监控的目录**只读**。

    挂载的告警日志必须落在哨兵自己的目录下，
    绝不能写进用户正在跑 Agent 的那个项目里。
    """
    from src.core.session_server import _attach_alert_log_path

    target = tmp_path / "external.jsonl"
    _write_jsonl(str(target), CODEX_LINES)
    alert_log = _attach_alert_log_path(str(target))

    # 告警日志落在哨兵自己的 logs/attached/ 下面，
    # **不在**被监控文件所在的目录里。
    assert os.path.abspath(alert_log) != os.path.abspath(str(target))
    assert os.path.dirname(os.path.abspath(alert_log)) != os.path.abspath(
        str(tmp_path)
    )
    assert "attached" in os.path.dirname(alert_log)
