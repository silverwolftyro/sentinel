"""Claude Code 适配器测试。

⚠️ **未实机验证**：本机没有安装 ``claude`` CLI（``mcode`` / ``codex``
都有，``claude`` 没有），所以这里的样本是依据 Claude Code
``--output-format stream-json`` 的**公开协议结构**编写的，
不是实机抓取的。协议一旦变化，这个文件需要跟着更新。

覆盖重点：

1. ``user`` 消息的**字符串正文** → ``user_message`` 事件
   （引擎据此**自动锚定任务目标**）；
2. ``user`` 消息里夹着 ``tool_result`` 时**不算**用户请求；
3. ``assistant`` 消息里的 ``tool_use`` → ``tool_call``（含命令提取）；
4. ``system`` / ``result`` 不产出可审查事件。
"""

from __future__ import annotations

import io
import json

from src.adapters.claude import ClaudeAdapter

#: Claude Code ``--output-format stream-json`` 的典型事件序列。
SAMPLE_LINES = [
    {
        "type": "system",
        "subtype": "init",
        "session_id": "sess-123",
        "model": "claude-sonnet-4",
    },
    {
        "type": "user",
        "message": {"content": "帮我把配置文件里的端口改成 8080"},
        "session_id": "sess-123",
    },
    {
        "type": "assistant",
        "message": {
            "content": [{"type": "text", "text": "我先看一下配置。"}]
        },
        "session_id": "sess-123",
    },
    {
        "type": "assistant",
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu_01",
                    "name": "Read",
                    "input": {"file_path": "D:/proj/app.conf"},
                }
            ]
        },
        "session_id": "sess-123",
    },
    {
        "type": "assistant",
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu_02",
                    "name": "Bash",
                    "input": {"command": "grep -n port D:/proj/app.conf"},
                }
            ]
        },
        "session_id": "sess-123",
    },
    {
        "type": "user",
        "message": {
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_02",
                    "content": "12:port=8080",
                    "is_error": False,
                }
            ]
        },
        "session_id": "sess-123",
    },
    {
        "type": "result",
        "subtype": "success",
        "result": "已把端口改成 8080",
        "session_id": "sess-123",
    },
]


def _events(lines=None):
    """把事件字典列表喂给适配器，返回统一事件列表。"""
    adapter = ClaudeAdapter()
    events = []
    for line in lines or SAMPLE_LINES:
        events.extend(adapter.parse_payload(line))
    return events


def test_user_message_becomes_user_message_event():
    """★ 用户的第一条字符串消息要变成 ``user_message`` 事件。

    引擎靠它**自动锚定任务目标**（v0.8.0 引入的能力）。
    """
    events = _events([SAMPLE_LINES[1]])
    assert len(events) == 1
    assert events[0]["type"] == "user_message"
    assert events[0]["text"] == "帮我把配置文件里的端口改成 8080"


def test_system_and_result_produce_no_events():
    """``system``（会话初始化）与 ``result``（本轮结束）不产出可审查事件。"""
    assert _events([SAMPLE_LINES[0]]) == []
    assert _events([SAMPLE_LINES[6]]) == []


def test_assistant_text_block():
    """assistant 的文本块 → ``text`` 事件。"""
    events = _events([SAMPLE_LINES[2]])
    assert events[0]["type"] == "text"
    assert events[0]["text"] == "我先看一下配置。"


def test_tool_use_block_becomes_tool_call():
    """``tool_use`` 块 → ``tool_call`` 事件，带工具名与参数。"""
    events = _events([SAMPLE_LINES[3]])
    assert events[0]["type"] == "tool_call"
    assert events[0]["tool"]["name"] == "Read"
    assert events[0]["tool"]["arguments"]["file_path"] == "D:/proj/app.conf"


def test_bash_command_is_extracted():
    """★ Bash 工具的命令文本要被提取出来。

    规则层（L1 危险命令检测）**只看** ``tool.command``，
    提取不出来就等于没监控到。
    """
    events = _events([SAMPLE_LINES[4]])
    tool = events[0]["tool"]
    assert tool["name"] == "Bash"
    assert tool["command"] == "grep -n port D:/proj/app.conf"


def test_tool_result_block_becomes_tool_result():
    """``tool_result`` 块 → ``tool_result`` 事件，并带上失败标记。"""
    events = _events([SAMPLE_LINES[5]])
    assert events[0]["type"] == "tool_result"
    assert events[0]["is_error"] is False
    assert events[0]["text"] == "12:port=8080"
    assert events[0]["tool_use_id"] == "toolu_02"


def test_tool_result_with_error_flag():
    """``is_error: true`` 的回执要被判为失败。"""
    events = _events(
        [
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_03",
                            "content": "命令执行失败",
                            "is_error": True,
                        }
                    ]
                },
            }
        ]
    )
    assert events[0]["type"] == "tool_result"
    assert events[0]["is_error"] is True


def test_user_message_with_tool_result_is_not_a_request():
    """★ user 消息里只有 ``tool_result`` 时**不算**用户请求。

    否则引擎会把「工具回执」误当成任务目标。
    """
    events = _events([SAMPLE_LINES[5]])
    assert all(event["type"] != "user_message" for event in events)


def test_user_message_array_without_tool_result_is_a_request():
    """user 消息是数组但**不含**工具回执时，仍算用户请求。"""
    events = _events(
        [
            {
                "type": "user",
                "message": {
                    "content": [{"type": "text", "text": "再帮我补个测试"}]
                },
            }
        ]
    )
    assert events[0]["type"] == "user_message"
    assert events[0]["text"] == "再帮我补个测试"


def test_thinking_block():
    """``thinking`` 块 → ``thinking`` 事件。"""
    events = _events(
        [
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "thinking", "thinking": "先确认端口在哪一行。"}
                    ]
                },
            }
        ]
    )
    assert events[0]["type"] == "thinking"
    assert events[0]["text"] == "先确认端口在哪一行。"


def test_session_id_extracted():
    """会话 ID 被提取到事件的 ``session_id``。"""
    events = _events([SAMPLE_LINES[1]])
    assert events[0]["session_id"] == "sess-123"


def test_plain_text_payload_becomes_user_message():
    """裸 ``text`` 字段也当作用户请求（部分版本的形态）。"""
    events = _events([{"type": "user", "text": "干这个"}])
    assert events[0]["type"] == "user_message"
    assert events[0]["text"] == "干这个"


def test_full_sample_event_sequence():
    """整条样本流的事件顺序符合预期。"""
    events = _events()
    assert [event["type"] for event in events] == [
        "user_message",   # 用户需求（自动锚定目标）
        "text",           # assistant 文本
        "tool_call",      # Read
        "tool_call",      # Bash
        "tool_result",    # 工具回执
    ]


def test_parse_line_skips_bad_json():
    """坏行跳过而不是抛异常（红线 R5）。"""
    adapter = ClaudeAdapter()
    assert adapter.parse_line("{不是 JSON") == []
    assert adapter.parse_line("") == []


def test_read_stream_from_pipe():
    """管道输入能正常解析。"""
    body = "\n".join(
        json.dumps(line, ensure_ascii=False) for line in SAMPLE_LINES
    )
    events = list(ClaudeAdapter().read_stream(io.StringIO(body)))
    assert len(events) == 5
    assert events[0]["source"] == "claude"
