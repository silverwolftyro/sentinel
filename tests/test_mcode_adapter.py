"""mcode 适配器测试。

覆盖：``thinking`` 提取、``tool_call`` 的工具名与参数提取、Claude 风格
assistant 消息、坏行跳过、空行跳过、管道流输入。
"""

from __future__ import annotations

import io
import json

from src.adapters.mcode import McodeAdapter

#: 模拟的 mcode stream-json 输出。
SAMPLE_LINES = [
    {
        "type": "thinking",
        "thinking": "我需要先确认配置文件是否存在，再决定要不要改。",
    },
    {
        "type": "tool_call",
        "tool_call": {
            "name": "Bash",
            "arguments": {"command": "ls -la"},
        },
    },
    {"type": "text", "text": "目录已列出。"},
    {
        "type": "tool_call",
        "tool_call": {
            "name": "Read",
            "arguments": {"file_path": "D:/proj/app.py"},
        },
    },
]


def _write_jsonl(tmp_path, lines):
    """把若干 dict 写成 JSONL 文件并返回路径。"""
    path = tmp_path / "stream.jsonl"
    body = "\n".join(json.dumps(line, ensure_ascii=False) for line in lines)
    path.write_text(body + "\n", encoding="utf-8")
    return path


def test_read_stream_extracts_thinking_and_tool_call(tmp_path):
    """核心用例：thinking 正文与 tool_call 的工具名/参数都要被正确提取。"""
    path = _write_jsonl(tmp_path, SAMPLE_LINES)

    events = list(McodeAdapter().read_stream(path))

    assert [event["type"] for event in events] == [
        "thinking",
        "tool_call",
        "text",
        "tool_call",
    ]

    # --- thinking ---
    assert events[0]["text"] == "我需要先确认配置文件是否存在，再决定要不要改。"
    assert events[0]["tool"] is None

    # --- tool_call：工具名与参数 ---
    assert events[1]["tool"]["name"] == "Bash"
    assert events[1]["tool"]["arguments"] == {"command": "ls -la"}
    assert events[1]["tool"]["command"] == "ls -la"

    # --- tool_call：文件读取类工具 ---
    assert events[3]["tool"]["name"] == "Read"
    assert events[3]["tool"]["arguments"]["file_path"] == "D:/proj/app.py"


def test_events_carry_source_and_raw_payload(tmp_path):
    """每个事件都要带上 source 与原始载荷，便于回溯。"""
    path = _write_jsonl(tmp_path, SAMPLE_LINES[:1])

    events = list(McodeAdapter().read_stream(path))

    assert events[0]["source"] == "mcode"
    assert events[0]["raw"]["type"] == "thinking"


def test_assistant_message_with_content_blocks(tmp_path):
    """Claude 风格 assistant 消息：一行里同时带 thinking 与 tool_use。"""
    path = _write_jsonl(
        tmp_path,
        [
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "thinking", "thinking": "先读一下配置。"},
                        {
                            "type": "tool_use",
                            "name": "Read",
                            "input": {"file_path": "config.yaml"},
                        },
                    ]
                },
            }
        ],
    )

    events = list(McodeAdapter().read_stream(path))

    assert [event["type"] for event in events] == ["thinking", "tool_call"]
    assert events[0]["text"] == "先读一下配置。"
    assert events[1]["tool"]["name"] == "Read"
    assert events[1]["tool"]["arguments"] == {"file_path": "config.yaml"}


def test_flat_tool_call_variants_are_normalized(tmp_path):
    """``name``+``arguments`` 与 ``name``+``args`` 两种平铺写法都要能归一化。"""
    path = _write_jsonl(
        tmp_path,
        [
            {"type": "tool_call", "name": "Bash", "arguments": {"command": "pwd"}},
            {"type": "tool_call", "name": "Bash", "args": {"command": "whoami"}},
        ],
    )

    events = list(McodeAdapter().read_stream(path))

    assert events[0]["tool"]["name"] == "Bash"
    assert events[0]["tool"]["command"] == "pwd"
    assert events[1]["tool"]["name"] == "Bash"
    assert events[1]["tool"]["command"] == "whoami"


def test_invalid_lines_are_skipped(tmp_path):
    """坏行、空行、非对象 JSON 都要被跳过，且不影响后续事件。"""
    path = tmp_path / "broken.jsonl"
    path.write_text(
        "\n".join(
            [
                "",
                "not json at all",
                json.dumps({"type": "thinking", "thinking": "有效的"}),
                "[1, 2, 3]",
                json.dumps({"type": "text", "text": "也在"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    events = list(McodeAdapter().read_stream(path))

    assert [event["type"] for event in events] == ["thinking", "text"]
    assert events[0]["text"] == "有效的"


def test_read_stream_accepts_a_pipe():
    """``read_stream`` 也要能直接吃一个已打开的流（管道 / stdin）。"""
    body = "\n".join(json.dumps(line, ensure_ascii=False) for line in SAMPLE_LINES)

    events = list(McodeAdapter().read_stream(io.StringIO(body + "\n")))

    assert len(events) == 4
    assert events[1]["tool"]["name"] == "Bash"


# ---------------------------------------------------------------------------
# tool_result（上下文追踪器的死循环检测依赖它）
# ---------------------------------------------------------------------------
def test_tool_result_is_parsed_with_error_flag(tmp_path):
    path = _write_jsonl(
        tmp_path,
        [
            {"type": "tool_call", "tool_call": {"name": "Bash",
                                                "arguments": {"command": "pytest"}}},
            {"type": "tool_result", "tool_use_id": "call_1",
             "content": "AssertionError", "is_error": True},
        ],
    )

    events = list(McodeAdapter().read_stream(path))

    assert events[1]["type"] == "tool_result"
    assert events[1]["is_error"] is True
    assert events[1]["text"] == "AssertionError"


def test_successful_tool_result_has_no_error_flag(tmp_path):
    path = _write_jsonl(
        tmp_path,
        [{"type": "tool_result", "content": "3 passed", "is_error": False}],
    )

    events = list(McodeAdapter().read_stream(path))

    assert events[0]["is_error"] is False


def test_tool_result_inside_assistant_message(tmp_path):
    """Claude 风格消息里的 tool_result 内容块。"""
    path = _write_jsonl(
        tmp_path,
        [
            {
                "type": "user",
                "message": {
                    "content": [
                        {"type": "tool_result", "is_error": True, "content": "boom"}
                    ]
                },
            }
        ],
    )

    events = list(McodeAdapter().read_stream(path))

    assert events[0]["type"] == "tool_result"
    assert events[0]["is_error"] is True


def test_error_is_detected_from_status_field(tmp_path):
    path = _write_jsonl(
        tmp_path,
        [{"type": "tool_result", "status": "error", "content": "boom"}],
    )

    events = list(McodeAdapter().read_stream(path))

    assert events[0]["is_error"] is True


def test_error_is_detected_from_success_false(tmp_path):
    path = _write_jsonl(
        tmp_path,
        [{"type": "tool_result", "success": False, "content": "boom"}],
    )

    events = list(McodeAdapter().read_stream(path))

    assert events[0]["is_error"] is True


# ---------------------------------------------------------------------------
# mcode v0.6 事件溯源协议（实测抓取的真实格式）
# ---------------------------------------------------------------------------
#: 真实 mcode v0.6.12 的 stream-json 事件序列。
V6_LINES = [
    {
        "schemaVersion": 1, "sequence": 1, "timestampMs": 1793033900000,
        "runId": "run-1", "sessionId": "s-1", "turnId": "t-1",
        "type": "exec.started",
    },
    {
        "schemaVersion": 1, "sequence": 2, "timestampMs": 1793033900100,
        "runId": "run-1", "sessionId": "s-1", "turnId": "t-1",
        "type": "session.started",
    },
    {
        "schemaVersion": 1, "sequence": 3, "timestampMs": 1793033900200,
        "runId": "run-1", "sessionId": "s-1", "turnId": "t-1",
        "type": "item.started",
        "item": {
            "id": "call_01", "type": "tool_call",
            "toolCall": {"id": "call_01", "name": "write", "status": 4},
        },
    },
    {
        "schemaVersion": 1, "sequence": 4, "timestampMs": 1793033900300,
        "runId": "run-1", "sessionId": "s-1", "turnId": "t-1",
        "type": "item.updated",
        "item": {
            "id": "call_01", "type": "tool_call",
            "toolCall": {
                "id": "call_01", "name": "write", "status": 1,
                "input": {"path": "D:/proj/hello.py", "content": "print(1)"},
            },
        },
    },
    {
        "schemaVersion": 1, "sequence": 5, "timestampMs": 1793033900900,
        "runId": "run-1", "sessionId": "s-1", "turnId": "t-1",
        "type": "item.completed",
        "item": {
            "id": "call_01", "type": "tool_call",
            "toolCall": {
                "id": "call_01", "name": "write", "status": 2,
                "input": {"path": "D:/proj/hello.py", "content": "print(1)"},
                "output": {
                    "content": [{"type": "text", "text": "wrote 8 bytes"}],
                    "details": {"path": "D:/proj/hello.py"},
                },
            },
        },
    },
    {
        "schemaVersion": 1, "sequence": 6, "timestampMs": 1793033901000,
        "runId": "run-1", "sessionId": "s-1", "turnId": "t-1",
        "type": "item.completed",
        "item": {
            "id": "msg-1", "type": "agent_message",
            "content": "已经创建好了。",
        },
    },
    {
        "schemaVersion": 1, "sequence": 7, "timestampMs": 1793033901100,
        "runId": "run-1", "sessionId": "s-1", "turnId": "t-1",
        "type": "turn.completed",
    },
]


def test_v6_session_events_produce_no_noise(tmp_path):
    """v0.6 的会话级事件不该产生 ``unknown`` 噪声。"""
    path = _write_jsonl(tmp_path, V6_LINES)

    events = list(McodeAdapter().read_stream(path))

    assert [event["type"] for event in events] == [
        "tool_call", "tool_result", "text",
    ]


def test_v6_tool_call_is_emitted_once_with_arguments(tmp_path):
    """同一次调用被反复推送时，``tool_call`` 只产一次。"""
    path = _write_jsonl(tmp_path, V6_LINES)

    events = list(McodeAdapter().read_stream(path))

    calls = [event for event in events if event["type"] == "tool_call"]
    assert len(calls) == 1
    assert calls[0]["tool"]["name"] == "write"
    assert calls[0]["tool"]["arguments"]["path"] == "D:/proj/hello.py"


def test_v6_tool_result_carries_output_text(tmp_path):
    """完成事件要带回工具输出。"""
    path = _write_jsonl(tmp_path, V6_LINES)

    events = list(McodeAdapter().read_stream(path))

    results = [event for event in events if event["type"] == "tool_result"]
    assert len(results) == 1
    assert results[0]["text"] == "wrote 8 bytes"
    assert results[0]["is_error"] is False


def test_v6_agent_message_is_taken_only_when_completed(tmp_path):
    """Agent 消息只在完成时取全文（中途是碎片）。"""
    path = _write_jsonl(
        tmp_path,
        [
            {
                "schemaVersion": 1, "runId": "r", "type": "item.started",
                "item": {
                    "id": "m1", "type": "agent_message", "contentDelta": "已",
                },
            },
            {
                "schemaVersion": 1, "runId": "r", "type": "item.completed",
                "item": {
                    "id": "m1", "type": "agent_message", "content": "已经完成。",
                },
            },
        ],
    )

    events = list(McodeAdapter().read_stream(path))

    assert [event["type"] for event in events] == ["text"]
    assert events[0]["text"] == "已经完成。"


def test_v6_user_message_is_recognized(tmp_path):
    """用户请求要能被认出来（自动锚定任务目标靠它）。"""
    path = _write_jsonl(
        tmp_path,
        [
            {
                "schemaVersion": 1, "runId": "r", "type": "item.completed",
                "item": {
                    "id": "u1", "type": "user_message",
                    "content": "重构 auth 模块并修复登录 bug",
                },
            },
        ],
    )

    events = list(McodeAdapter().read_stream(path))

    assert events[0]["type"] == "user_message"
    assert events[0]["text"] == "重构 auth 模块并修复登录 bug"


def test_v6_bash_command_is_extracted(tmp_path):
    """bash 工具的命令要从 ``input.command`` 里抽出来。"""
    path = _write_jsonl(
        tmp_path,
        [
            {
                "schemaVersion": 1, "runId": "r", "type": "item.updated",
                "item": {
                    "id": "call_9", "type": "tool_call",
                    "toolCall": {
                        "id": "call_9", "name": "bash", "status": 1,
                        "input": {
                            "command": "python hello.py",
                            "description": "跑一下",
                        },
                    },
                },
            },
        ],
    )

    events = list(McodeAdapter().read_stream(path))

    assert events[0]["tool"]["command"] == "python hello.py"


def test_v6_output_failure_is_detected_from_error_marker(tmp_path):
    """输出里明确的失败标记要能识别出来。"""
    path = _write_jsonl(
        tmp_path,
        [
            {
                "schemaVersion": 1, "runId": "r", "type": "item.completed",
                "item": {
                    "id": "call_x", "type": "tool_call",
                    "toolCall": {
                        "id": "call_x", "name": "bash", "status": 2,
                        "input": {"command": "boom"},
                        "output": {
                            "content": [], "details": {"error": "炸了"},
                        },
                    },
                },
            },
        ],
    )

    events = list(McodeAdapter().read_stream(path))

    results = [event for event in events if event["type"] == "tool_result"]
    assert results[0]["is_error"] is True


def test_v6_reasoning_is_mapped_to_thinking(tmp_path):
    """mcode 的 ``reasoning`` item 就是 Agent 的自述 —— 要映射成思考事件。"""
    path = _write_jsonl(
        tmp_path,
        [
            {
                "schemaVersion": 1, "runId": "r", "type": "item.completed",
                "item": {
                    "id": "u:reasoning", "type": "reasoning",
                    "content": "Verified. Let me also confirm the file.",
                },
            },
        ],
    )

    events = list(McodeAdapter().read_stream(path))

    assert events[0]["type"] == "thinking"
    assert "Verified" in events[0]["text"]


def test_v6_status_alone_must_not_be_treated_as_failure(tmp_path):
    """只凭 ``status`` 数字**不能**判定失败 —— 宁可漏报不误报。"""
    path = _write_jsonl(
        tmp_path,
        [
            {
                "schemaVersion": 1, "runId": "r", "type": "item.completed",
                "item": {
                    "id": "call_y", "type": "tool_call",
                    "toolCall": {
                        "id": "call_y", "name": "bash", "status": 2,
                        "input": {"command": "ok"},
                        "output": {"content": [{"type": "text", "text": "ok"}]},
                    },
                },
            },
        ],
    )

    events = list(McodeAdapter().read_stream(path))

    results = [event for event in events if event["type"] == "tool_result"]
    assert results[0]["is_error"] is False


def test_flat_tool_result_keeps_failure_flag(tmp_path):
    """扁平形态的回执把内容**嵌在** ``tool_result`` 里 —— 必须拆开看。

    不拆的话「失败了没有」会丢，
    死循环检测（``[STUCK]``）与外泄检测（``[LEAK]``）
    对这种格式就永远不触发。
    """
    path = _write_jsonl(
        tmp_path,
        [
            {
                "type": "tool_result",
                "tool_result": {
                    "status": "error",
                    "output": "FAILED tests/test_auth.py::test_login",
                },
            },
        ],
    )

    events = list(McodeAdapter().read_stream(path))

    assert events[0]["type"] == "tool_result"
    assert events[0]["is_error"] is True
    assert "FAILED" in events[0]["text"]


def test_flat_tool_result_success_is_not_an_error(tmp_path):
    """嵌套形态也不能把成功误判成失败。"""
    path = _write_jsonl(
        tmp_path,
        [
            {
                "type": "tool_result",
                "tool_result": {"status": "ok", "output": "4 passed"},
            },
        ],
    )

    events = list(McodeAdapter().read_stream(path))

    assert events[0]["is_error"] is False
    assert events[0]["text"] == "4 passed"

