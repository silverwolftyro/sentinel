"""哨兵 GUI 监控面板测试。

全部离线：不绑定真实端口、不访问网络。用 ``fastapi.testclient.TestClient``
在进程内驱动 ASGI 应用，文件读写都落在 pytest 的 ``tmp_path`` 里。
"""

from __future__ import annotations

import asyncio
import json
import os
import threading

import pytest

from src.core.session_server import (
    EventHub,
    FileTailer,
    normalize_alert,
    parse_alert_line,
)

#: 静态资源目录（用来断言前端源码里的关键约定）。
STATIC_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "src",
    "gui",
    "static",
)

# ---------------------------------------------------------------------------
# 样例数据
# ---------------------------------------------------------------------------
ALERT_PAYLOAD = {
    "timestamp": 1793026126.39,
    "verdict": "DANGEROUS",
    "level": "critical",
    "rule_id": "dangerous_command",
    "reason": "匹配到高危命令: rm -rf",
    "evidence": "rm -rf",
    "source": "mcode",
    "event_type": "tool_call",
    "tool": "Bash",
    "command": "rm -rf /tmp/x",
    "arguments": {"command": "rm -rf /tmp/x"},
    "explanation": "清理临时构建产物。",
    "explanation_degraded": False,
}

ALERT_JSON_LINE = (
    "2026-10-26 22:48:46,393 WARNING ALERT_JSON "
    + json.dumps(ALERT_PAYLOAD, ensure_ascii=False)
)

HUMAN_LINE = (
    "2026-10-26 22:48:46,393 WARNING [CRITICAL] DANGEROUS: "
    "匹配到高危命令: rm -rf | 工具=Bash | 命令=rm -rf /tmp/x"
)


class _FakeRequest:
    """最小 Request 替身，让 SSE 生成器可以离线测试。

    :param disconnect_after: 第几次 ``is_disconnected()`` 开始返回真。
    """

    def __init__(self, disconnect_after: int = 1) -> None:
        self._remaining = int(disconnect_after)

    async def is_disconnected(self) -> bool:
        if self._remaining <= 0:
            return True
        self._remaining -= 1
        return False












def test_normalize_alert_synthesizes_missing_alert_id():
    """老版本写的告警行没有 ``alert_id`` —— 要给它合成一个**稳定**的。

    没有 ID 的卡片，【复制纠偏提示】会静默失败（点了没反应）。
    """
    raw = {
        "timestamp": 1793026126.39196,
        "verdict": "LOOP",
        "rule_id": "repeat_read_loop",
        "reason": "连续 3 次读取同一个文件",
    }

    first = normalize_alert(dict(raw))
    second = normalize_alert(dict(raw))

    assert first["alert_id"]
    # 同一行日志必须每次得到同一个 ID（否则按 ID 找不回）。
    assert first["alert_id"] == second["alert_id"]
    # 不一样的告警不能撞号。
    other = normalize_alert({**raw, "reason": "别的告警"})
    assert other["alert_id"] != first["alert_id"]


def test_normalize_alert_keeps_existing_alert_id():
    """本来就有的 ``alert_id`` 不能被改写。"""
    raw = {
        "timestamp": 1793026126.39196,
        "alert_id": "1793091301482-000001",
        "verdict": "STUCK",
    }

    assert normalize_alert(raw)["alert_id"] == "1793091301482-000001"


















def test_hub_delivers_events_from_another_thread():
    """后台线程投递的事件能安全进入事件循环。"""
    async def scenario():
        hub = EventHub()
        hub.attach_loop(asyncio.get_running_loop())
        queue = hub.subscribe()
        thread = threading.Thread(
            target=hub.publish,
            args=({"type": "line", "text": "hello"},),
        )
        thread.start()
        thread.join()
        return await asyncio.wait_for(queue.get(), timeout=2.0)

    event = asyncio.run(scenario())

    assert event["type"] == "line"
    assert event["text"] == "hello"




# ---------------------------------------------------------------------------
# 告警解析
# ---------------------------------------------------------------------------
def test_parse_alert_line_extracts_structured_alert():
    """ALERT_JSON 行能被解析成结构化告警。"""
    alert = parse_alert_line(ALERT_JSON_LINE)

    assert alert is not None
    assert alert["verdict"] == "DANGEROUS"
    assert alert["rule_id"] == "dangerous_command"
    assert alert["command"] == "rm -rf /tmp/x"


def test_parse_alert_line_ignores_human_lines():
    """给人看的那一行不会被当成结构化告警。"""
    assert parse_alert_line(HUMAN_LINE) is None
    assert parse_alert_line("2026-10-26 22:48:46,393 WARNING 提示") is None


def test_parse_alert_line_rejects_broken_json():
    """JSON 非法时返回 None，不抛异常。"""
    assert parse_alert_line("WARNING ALERT_JSON {不是 JSON}") is None
    assert parse_alert_line("WARNING ALERT_JSON 不是 JSON") is None


def test_normalize_alert_fills_defaults():
    """缺省字段被补齐，方便前端直接渲染。"""
    normalized = normalize_alert({"verdict": "LOOP"})

    assert normalized["verdict"] == "LOOP"
    assert normalized["explanation_degraded"] is False
    assert normalized["arguments"] == {}
    assert normalized["rule_id"] == "-"


# ---------------------------------------------------------------------------
# 增量跟随
# ---------------------------------------------------------------------------
def test_tailer_reads_only_new_bytes(tmp_path):
    """tailer 只读新增字节，不会重复吐旧内容。"""
    path = tmp_path / "agent.jsonl"
    path.write_text("line-1\n", encoding="utf-8")
    tailer = FileTailer(str(path))

    assert tailer.poll() == ["line-1"]
    assert tailer.poll() == []

    with path.open("a", encoding="utf-8") as handle:
        handle.write("line-2\n")

    assert tailer.poll() == ["line-2"]
    assert tailer.poll() == []


def test_tailer_resets_offset_on_truncation(tmp_path):
    """文件被原地截断后，偏移归零重新读。"""
    path = tmp_path / "alert.log"
    path.write_text("aaaa\nbbbb\n", encoding="utf-8")
    tailer = FileTailer(str(path))

    assert tailer.poll() == ["aaaa", "bbbb"]

    path.write_text("c\n", encoding="utf-8")

    assert tailer.poll() == ["c"]


def test_tailer_detects_rotation(tmp_path):
    """同名新文件（轮转）会把偏移归零。"""
    path = tmp_path / "agent.jsonl"
    path.write_text("old-1\n", encoding="utf-8")
    tailer = FileTailer(str(path))

    assert tailer.poll() == ["old-1"]

    if not os.stat(str(path)).st_ino:
        pytest.skip("当前文件系统不提供 st_ino，无法验证轮转检测")

    rotated = tmp_path / "rotated.jsonl"
    rotated.write_text("new-1\nnew-2\n", encoding="utf-8")
    os.replace(str(rotated), str(path))

    assert tailer.poll() == ["new-1", "new-2"]


def test_tailer_handles_crlf_and_broken_bytes(tmp_path):
    """CRLF 与非法字节都不会让 tailer 崩掉。"""
    path = tmp_path / "mixed.log"
    path.write_bytes(b"a\r\nb\r\n")
    tailer = FileTailer(str(path))

    assert tailer.poll() == ["a", "b"]

    with path.open("ab") as handle:
        handle.write(b"\xff\xfe\n")

    assert tailer.poll() == ["\ufffd\ufffd"]


def test_tailer_waits_for_complete_line(tmp_path):
    """半行不会被当成完整行吐出来。"""
    path = tmp_path / "partial.log"
    path.write_bytes(b"half")
    tailer = FileTailer(str(path))

    assert tailer.poll() == []

    with path.open("ab") as handle:
        handle.write(b"-done\n")

    assert tailer.poll() == ["half-done"]
