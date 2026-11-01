"""Codex 适配器测试。

数据来源：**实机抓取**的 ``codex exec --json`` 输出
（codex-cli 0.162.1，Windows），不是凭空编造的样本。

重点验证三件事：

1. ``command_execution`` 在 ``item.started`` 时就产出 ``tool_call``
   —— 命令在**执行前**就完整，守护者能更早动手；
2. ``file_change`` 的路径被正确提取（意图漂移窗口摘要要用）；
3. 退出码缺失（``null``）时**不判失败** —— 宁可漏报不误报。
"""

from __future__ import annotations

import io
import json

from src.adapters.codex import CodexAdapter

#: 实机抓取的 codex 事件流（原样保留 Windows 路径与转义）。
REAL_CODEX_LINES = [
    {
        "type": "thread.started",
        "thread_id": "01a17af0-d92e-7402-b387-f73e498195df",
    },
    {"type": "turn.started"},
    {
        "type": "item.completed",
        "item": {
            "id": "item_0",
            "type": "agent_message",
            "text": "我会在当前目录创建 `hi.py`，随后直接执行并核对输出。",
        },
    },
    {
        "type": "item.started",
        "item": {
            "id": "item_1",
            "type": "file_change",
            "changes": [
                {
                    "path": "D:\\项目\\<项目目录>\\logs\\codex_scratch\\hi.py",
                    "kind": "add",
                }
            ],
            "status": "in_progress",
        },
    },
    {
        "type": "item.completed",
        "item": {
            "id": "item_1",
            "type": "file_change",
            "changes": [
                {
                    "path": "D:\\项目\\<项目目录>\\logs\\codex_scratch\\hi.py",
                    "kind": "add",
                }
            ],
            "status": "completed",
        },
    },
    {
        "type": "item.started",
        "item": {
            "id": "item_2",
            "type": "command_execution",
            "command": (
                "\"C:\\\\Users\\\\alpha\\\\AppData\\\\Local\\\\Microsoft\\\\WindowsApps"
                "\\\\Microsoft.PowerShell_8wekyb3d8bbwe\\\\pwsh.exe\" "
                "-Command 'python hi.py'"
            ),
            "aggregated_output": "",
            "exit_code": None,
            "status": "in_progress",
        },
    },
    {
        "type": "item.completed",
        "item": {
            "id": "item_2",
            "type": "command_execution",
            "command": (
                "\"C:\\\\Users\\\\alpha\\\\AppData\\\\Local\\\\Microsoft\\\\WindowsApps"
                "\\\\Microsoft.PowerShell_8wekyb3d8bbwe\\\\pwsh.exe\" "
                "-Command 'python hi.py'"
            ),
            "aggregated_output": "hi\r\n",
            "exit_code": 0,
            "status": "completed",
        },
    },
    {
        "type": "item.completed",
        "item": {
            "id": "item_3",
            "type": "agent_message",
            "text": "已创建 hi.py，运行输出为：\n\n```text\nhi\n```",
        },
    },
    {
        "type": "turn.completed",
        "usage": {
            "input_tokens": 73546,
            "cached_input_tokens": 48768,
            "cache_write_input_tokens": 0,
            "output_tokens": 269,
            "reasoning_output_tokens": 47,
        },
    },
]


def _events(lines=None):
    """把事件字典列表喂给适配器，返回统一事件列表。"""
    adapter = CodexAdapter()
    events = []
    for line in lines or REAL_CODEX_LINES:
        events.extend(adapter.parse_payload(line))
    return events


def test_real_stream_event_sequence():
    """实机事件流的产出顺序必须是预期的 7 个事件。"""
    events = _events()
    assert [event["type"] for event in events] == [
        "text",       # agent_message（完成时才有全文）
        "tool_call",  # file_change
        "tool_result",  # file_change 完成回执
        "tool_call",  # command_execution
        "tool_result",  # command_execution 完成回执
        "text",       # 最后的 agent_message
        "usage",      # turn.completed 的真实 Token 账单（v1.1.0）
    ]


def test_session_events_produce_nothing():
    """``thread.*`` / ``turn.*`` 不含可审查动作，不产出**审查**事件。

    ⚠️ 例外：``turn.completed`` 带着真实 ``usage`` —— 那是「账单」不是「动作」，
    所以产出一个 ``usage`` 事件（引擎据此记账），但它**不进规则引擎**。
    """
    events = _events(
        [
            {"type": "thread.started", "thread_id": "abc"},
            {"type": "turn.started"},
        ]
    )
    assert events == []


def test_turn_completed_yields_usage_event():
    """``turn.completed`` 的 usage 要能被采信（真实 Token 账单）。"""
    events = _events(
        [
            {"type": "turn.started"},
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 73546,
                    "output_tokens": 120,
                    "cached_input_tokens": 48768,
                },
            },
        ]
    )
    assert len(events) == 1
    usage = events[0]
    assert usage["type"] == "usage"
    assert usage["usage"]["input_tokens"] == 73546
    assert usage["usage"]["output_tokens"] == 120


def test_turn_completed_without_usage_produces_nothing():
    """没有 usage 的 ``turn.completed`` 仍然什么都不产出。"""
    events = _events([{"type": "turn.completed"}])
    assert events == []


def test_agent_message_only_on_completed():
    """``item.started`` 的 agent_message 是空的（还没写完），不产出事件。"""
    events = _events(
        [
            {
                "type": "item.started",
                "item": {"id": "i1", "type": "agent_message", "text": ""},
            }
        ]
    )
    assert events == []


def test_agent_message_text_is_kept():
    """Agent 消息正文被完整保留。"""
    events = _events([REAL_CODEX_LINES[2]])
    assert events[0]["type"] == "text"
    assert "创建 `hi.py`" in events[0]["text"]
    assert events[0]["source"] == "codex"


def test_command_execution_emits_tool_call_on_started():
    """★ 关键：命令在 ``item.started`` 时就完整产出 tool_call。

    守护者必须能在命令**执行前**就看到它并决定是否挂起。
    """
    events = _events([REAL_CODEX_LINES[5]])
    assert len(events) == 1
    assert events[0]["type"] == "tool_call"
    assert events[0]["tool"]["name"] == "shell"
    assert "python hi.py" in events[0]["tool"]["command"]
    # 参数里也留了一份，方便规则层直接看。
    assert "python hi.py" in events[0]["tool"]["arguments"]["command"]


def test_command_execution_result_on_completed():
    """命令完成时产出回执，带上输出与退出码。"""
    events = _events([REAL_CODEX_LINES[6]])
    assert len(events) == 2
    result = events[1]
    assert result["type"] == "tool_result"
    assert result["text"] == "hi\r\n"
    assert result["is_error"] is False
    assert result["tool_use_id"] == "item_2"


def test_null_exit_code_is_not_error():
    """退出码是 ``null`` 时**不能**判失败（不知道 ≠ 失败）。"""
    events = _events([REAL_CODEX_LINES[5]])
    # item.started 阶段只有 tool_call，没有回执。
    assert all(event["type"] != "tool_result" for event in events)


def test_nonzero_exit_code_is_error():
    """明确的非零退出码要判失败（死循环检测依赖它）。"""
    events = _events(
        [
            {
                "type": "item.completed",
                "item": {
                    "id": "i9",
                    "type": "command_execution",
                    "command": "pytest",
                    "aggregated_output": "boom",
                    "exit_code": 1,
                    "status": "completed",
                },
            }
        ]
    )
    result = events[1]
    assert result["type"] == "tool_result"
    assert result["is_error"] is True


def test_file_change_paths_extracted():
    """文件改动的路径被正确提取（意图漂移窗口摘要要用）。"""
    events = _events([REAL_CODEX_LINES[3]])
    tool_call = events[0]
    assert tool_call["type"] == "tool_call"
    assert tool_call["tool"]["name"] == "write"
    assert tool_call["tool"]["arguments"]["paths"] == [
        "D:\\项目\\<项目目录>\\logs\\codex_scratch\\hi.py"
    ]


def test_file_change_result_lists_paths():
    """文件改动完成回执把路径拼成一行文本。"""
    events = _events([REAL_CODEX_LINES[4]])
    result = events[1]
    assert result["type"] == "tool_result"
    assert result["is_error"] is False
    assert "hi.py" in result["text"]


def test_no_duplicate_tool_call_for_same_item():
    """同一个 item 的 started / completed 不应产出两条 tool_call。"""
    events = _events([REAL_CODEX_LINES[5], REAL_CODEX_LINES[6]])
    tool_calls = [e for e in events if e["type"] == "tool_call"]
    assert len(tool_calls) == 1


def test_reasoning_becomes_thinking():
    """``reasoning`` 映射成 ``thinking`` 事件。"""
    events = _events(
        [
            {
                "type": "item.completed",
                "item": {
                    "id": "r1",
                    "type": "reasoning",
                    "text": "先看看目录里有什么。",
                },
            }
        ]
    )
    assert events[0]["type"] == "thinking"
    assert events[0]["text"] == "先看看目录里有什么。"


def test_unknown_item_type_becomes_tool_call():
    """没见过的 item 类型按工具调用处理（MCP / 搜索等）。"""
    events = _events(
        [
            {
                "type": "item.started",
                "item": {"id": "m1", "type": "mcp_tool_call", "server": "x"},
            }
        ]
    )
    assert events[0]["type"] == "tool_call"
    assert events[0]["tool"]["name"] == "mcp_tool_call"


def test_error_event_is_text_with_error_flag():
    """codex 的 ``error`` 事件转成带错误标记的文本事件。"""
    events = _events([{"type": "error", "message": "模型不可用"}])
    assert events[0]["type"] == "text"
    assert events[0]["text"] == "模型不可用"
    assert events[0]["is_error"] is True


def test_parse_line_skips_bad_json():
    """坏行必须被跳过而不是抛异常（红线 R5）。"""
    adapter = CodexAdapter()
    assert adapter.parse_line("{不是 JSON") == []
    assert adapter.parse_line("") == []
    assert adapter.parse_line("[1, 2]") == []


def test_read_stream_from_pipe():
    """管道输入（``--json`` 的真实形态）能正常解析。"""
    body = "\n".join(
        json.dumps(line, ensure_ascii=False) for line in REAL_CODEX_LINES
    )
    events = list(CodexAdapter().read_stream(io.StringIO(body)))
    assert len(events) == 7
    assert events[0]["source"] == "codex"
