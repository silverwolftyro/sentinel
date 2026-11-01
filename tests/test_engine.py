"""主流程调度测试。

覆盖：从适配器读事件 → 规则层判定 → 终端告警 + 告警日志落盘 → 汇总摘要。
"""

from __future__ import annotations

import json

from src.adapters.mcode import McodeAdapter
from src.core.engine import SentinelEngine
from src.rules.basic import RuleEngine


def _write_jsonl(tmp_path, lines):
    path = tmp_path / "stream.jsonl"
    body = "\n".join(json.dumps(line, ensure_ascii=False) for line in lines)
    path.write_text(body + "\n", encoding="utf-8")
    return path


LOG_LINES = [
    {"type": "thinking", "thinking": "先看看目录。"},
    {"type": "tool_call", "tool_call": {"name": "Bash", "arguments": {"command": "ls -la"}}},
    {"type": "tool_call", "tool_call": {"name": "Bash", "arguments": {"command": "rm -rf /tmp/x"}}},
    {"type": "tool_call", "tool_call": {"name": "Read", "arguments": {"file_path": "a.py"}}},
    {"type": "tool_call", "tool_call": {"name": "Read", "arguments": {"file_path": "a.py"}}},
    {"type": "tool_call", "tool_call": {"name": "Read", "arguments": {"file_path": "a.py"}}},
]


def test_run_counts_events_and_findings(tmp_path):
    path = _write_jsonl(tmp_path, LOG_LINES)

    engine = SentinelEngine(
        McodeAdapter(),
        RuleEngine(config={}),
        alert_log_path=str(tmp_path / "sentinel_alert.log"),
        console=False,
    )
    try:
        summary = engine.run(path)
    finally:
        engine.close()

    assert summary["total_events"] == 6
    # thinking + ls + 前两次读取（未达死循环阈值 3）= 4
    assert summary["counts"]["ON_TRACK"] == 4
    assert summary["counts"]["DANGEROUS"] == 1
    assert summary["counts"]["LOOP"] == 1
    assert len(summary["alerts"]) == 2


def test_run_writes_alert_log(tmp_path):
    path = _write_jsonl(tmp_path, LOG_LINES)
    alert_log = tmp_path / "sentinel_alert.log"

    engine = SentinelEngine(
        McodeAdapter(),
        RuleEngine(config={}),
        alert_log_path=str(alert_log),
        console=False,
    )
    try:
        engine.run(path)
    finally:
        engine.close()

    content = alert_log.read_text(encoding="utf-8")
    assert "匹配到高危命令" in content
    assert "rm -rf" in content
    assert "疑似死循环" in content
    assert "ALERT_JSON" in content


def test_run_prints_red_warning_to_console(tmp_path, capsys):
    path = _write_jsonl(tmp_path, LOG_LINES[:3])

    engine = SentinelEngine(
        McodeAdapter(),
        RuleEngine(config={}),
        alert_log_path=str(tmp_path / "sentinel_alert.log"),
        console=True,
    )
    try:
        engine.run(path)
    finally:
        engine.close()

    captured = capsys.readouterr()
    assert "DANGEROUS" in captured.out
    assert "rm -rf" in captured.out


def test_clean_log_produces_no_alerts(tmp_path):
    path = _write_jsonl(tmp_path, LOG_LINES[:2])

    engine = SentinelEngine(
        McodeAdapter(),
        RuleEngine(config={}),
        alert_log_path=str(tmp_path / "sentinel_alert.log"),
        console=False,
    )
    try:
        summary = engine.run(path)
    finally:
        engine.close()

    assert summary["alerts"] == []
    assert summary["counts"]["DANGEROUS"] == 0


def test_engine_accepts_an_open_stream(tmp_path):
    """``run`` 也接受已打开的流（管道场景）。"""
    import io

    body = "\n".join(json.dumps(line, ensure_ascii=False) for line in LOG_LINES)

    engine = SentinelEngine(
        McodeAdapter(),
        RuleEngine(config={}),
        alert_log_path=str(tmp_path / "sentinel_alert.log"),
        console=False,
    )
    try:
        summary = engine.run(io.StringIO(body + "\n"))
    finally:
        engine.close()

    assert summary["total_events"] == 6
    assert summary["counts"]["DANGEROUS"] == 1


def test_console_output_falls_back_to_ascii_when_encoding_cannot_render(tmp_path):
    """终端编码装不下中文时，终端输出降级为 ASCII 摘要但关键信息仍在。

    告警日志始终是 UTF-8，不受终端编码影响。
    """
    import io

    from src.core.engine import _console_safe

    class AsciiStream(io.StringIO):
        encoding = "ascii"

    message = "[CRITICAL] DANGEROUS: 匹配到高危命令: rm -rf | 工具=Bash"
    fallback = _console_safe(message, AsciiStream())

    assert fallback.isascii()
    assert "DANGEROUS" in fallback
    assert "rm -rf" in fallback


def test_console_output_keeps_chinese_when_encoding_supports_it(tmp_path):
    """终端编码支持中文时，保留完整中文文案。"""
    import io

    from src.core.engine import _console_safe

    class Utf8Stream(io.StringIO):
        encoding = "utf-8"

    message = "[CRITICAL] DANGEROUS: 匹配到高危命令: rm -rf"

    assert _console_safe(message, Utf8Stream()) == message
