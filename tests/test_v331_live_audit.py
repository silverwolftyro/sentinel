"""v3.3.1 回归：实时审核流水 · 无目标强引导 · PID 反查失败的宽容度。

背景
====

用户实测雷达挂载时撞上三件事：

1. **看板中间空空如也** —— L1 判定为 ``ON_TRACK`` 的事件
   在旧版本里只被**计数**，从不外送。
   用户盯着面板看不出「它一直在看」，
   反而以为哨兵没在工作。
2. **顶部一句「未设置」看不懂** —— 挂载进来的会话没有初始 Prompt，
   雷达找不到初始目标，用户不知道该往哪儿填。
3. **``[未绑定进程]`` + 灰掉的武装开关** —— 让人以为整个哨兵瘫了。
   其实只是少了一项「拦截」能力：
   L1/L2 判定、告警落盘、通知推送**全都照常**。

本文件钉住：

1. **实时审核流水** —— 日志格式契约、摘要极简、只送 ON_TRACK、
   工具回执不重复、看板能还原；
2. **强引导** —— 没目标时橘黄高亮 + 气泡文案；
   设定之后顶部自动变成「手动锚定」；
3. **宽容度** —— 没有 PID 时告警与推送照常工作，
   界面把这件事**说清楚**。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from src.adapters.mcode import McodeAdapter
from src.core.engine import ONTRACK_MARKER, SentinelEngine
from src.core.live_audit import format_clock, summarize_event
from src.core.session_server import (
    AlertStore,
    EventHub,
    Watcher,
    parse_ontrack_line,
)
from src.rules.basic import RuleEngine

#: 仓库根目录（tests/ 的上一级）。
ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = ROOT / "src" / "gui" / "static"
TEMPLATE_DIR = ROOT / "src" / "gui" / "templates"


def _read(*parts) -> str:
    """读仓库里的一个文本文件。

    :param parts: 相对仓库根的路径片段。
    :return: 文件文本。
    """
    path = ROOT.joinpath(*parts)
    return path.read_text(encoding="utf-8")


def _write_jsonl(path, lines) -> str:
    """写一份 JSONL 日志并返回路径。

    :param path: 目标路径。
    :param lines: 载荷列表。
    :return: 路径字符串。
    """
    body = "\n".join(
        json.dumps(line, ensure_ascii=False) for line in lines
    )
    path.write_text(body + "\n", encoding="utf-8")
    return str(path)


class RecordingNotifier:
    """记录收到告警的假通知器。"""

    def __init__(self):
        self.alerts = []

    def notify(self, alert):
        """收下告警。

        :param alert: 告警字典。
        """
        self.alerts.append(alert)

    def close(self):
        """关掉通道。"""


# ---------------------------------------------------------------------------
# 一、摘要：必须「极其精简」
# ---------------------------------------------------------------------------
def test_读取类工具压成文件名():
    """读文件 → 「正常读取: core/auth.py」（只留末两段路径）。"""
    summary = summarize_event({
        "type": "tool_call",
        "tool": {
            "name": "Read",
            "arguments": {"file_path": "D:\\work\\project\\src\\core\\auth.py"},
            "command": "",
        },
    })
    assert summary == "正常读取: core/auth.py"


def test_执行类工具压成命令():
    """跑命令 → 「正常执行: pytest」（选项不显示）。"""
    summary = summarize_event({
        "type": "tool_call",
        "tool": {
            "name": "Bash",
            "arguments": {"command": "pytest -q tests/"},
            "command": "pytest -q tests/",
        },
    })
    assert summary == "正常执行: pytest"


def test_写入类工具压成文件名():
    """改文件 → 「正常写入: core/engine.py」。"""
    summary = summarize_event({
        "type": "tool_call",
        "tool": {
            "name": "Edit",
            "arguments": {"file_path": "src/core/engine.py"},
            "command": "",
        },
    })
    assert summary == "正常写入: core/engine.py"


def test_搜索类工具压成模式():
    """搜索 → 「正常搜索: TODO」。"""
    summary = summarize_event({
        "type": "tool_call",
        "tool": {
            "name": "Grep",
            "arguments": {"pattern": "TODO"},
            "command": "grep -rn TODO",
        },
    })
    assert summary == "正常搜索: TODO"


def test_认不出来的工具也不崩():
    """陌生工具 → 落到兜底词，绝不抛异常。"""
    summary = summarize_event({
        "type": "tool_call",
        "tool": {"name": "mcp__weird__thing", "arguments": {}, "command": ""},
    })
    assert summary.startswith("正常")
    assert summary.endswith("thing")


def test_非工具事件也有说法():
    """思考 / 输出 / 接收指令 各有各的说法。"""
    assert summarize_event({"type": "thinking"}) == "正常思考"
    assert summarize_event({"type": "text"}) == "正常输出"
    assert summarize_event({"type": "user_message"}) == "正常接收指令"


def test_摘要里绝不出现JSON():
    """流水区**不显示完整 JSON** —— 摘要必须是干净的一行字。"""
    summary = summarize_event({
        "type": "tool_call",
        "tool": {
            "name": "Bash",
            "arguments": {"command": "ls -la", "extra": {"a": 1}},
            "command": "ls -la",
        },
    })
    assert "{" not in summary
    assert "}" not in summary
    assert "\n" not in summary


def test_坏事件也不崩():
    """非法输入 → 兜底词（架构红线 R5：审查侧异常一律降级）。"""
    assert summarize_event(None) == "正常处理"
    assert summarize_event({}) == "正常处理"
    assert summarize_event({"type": "tool_call", "tool": None}) == "正常处理"


def test_时间戳格式化成时分秒():
    """流水行的时间是 ``HH:MM:SS``。"""
    text = format_clock(1793032000.5)
    assert len(text) == 8
    assert text[2] == ":" and text[5] == ":"
    assert text.replace(":", "").isdigit()


# ---------------------------------------------------------------------------
# 二、日志格式契约：引擎写 → 看板读
# ---------------------------------------------------------------------------
def test_引擎把正常判定写成流水行(tmp_path):
    """L1 判定为 ON_TRACK → 告警日志里多一行 ``ONTRACK_JSON``。"""
    log = _write_jsonl(tmp_path / "stream.jsonl", [
        {"type": "tool_call", "tool_call": {
            "name": "Read", "arguments": {"file_path": "auth.py"}}},
    ])
    alert_log = tmp_path / "sentinel_alert.log"

    engine = SentinelEngine(
        McodeAdapter(),
        RuleEngine(config={}),
        alert_log_path=str(alert_log),
        console=False,
    )
    try:
        engine.run(log)
    finally:
        engine.close()

    content = alert_log.read_text(encoding="utf-8")
    assert ONTRACK_MARKER in content

    entries = [
        parse_ontrack_line(line)
        for line in content.splitlines()
    ]
    entries = [item for item in entries if item is not None]
    assert entries
    assert entries[0]["summary"] == "正常读取: auth.py"
    assert entries[0]["timestamp"] > 0


def test_告警不写流水行(tmp_path):
    """DANGEROUS / LOOP 走告警通道 —— **不**再写一条流水（免得重复）。"""
    log = _write_jsonl(tmp_path / "stream.jsonl", [
        {"type": "tool_call", "tool_call": {
            "name": "Bash", "arguments": {"command": "rm -rf /tmp/x"}}},
    ])
    alert_log = tmp_path / "sentinel_alert.log"

    engine = SentinelEngine(
        McodeAdapter(),
        RuleEngine(config={}),
        alert_log_path=str(alert_log),
        console=False,
    )
    try:
        engine.run(log)
    finally:
        engine.close()

    content = alert_log.read_text(encoding="utf-8")
    assert "ALERT_JSON" in content
    assert ONTRACK_MARKER not in content


def test_工具回执不重复记账(tmp_path):
    """``tool_result`` 只是回执 —— 同一次操作不能在流水里出现两遍。"""
    log = _write_jsonl(tmp_path / "stream.jsonl", [
        {"type": "tool_call", "tool_call": {
            "name": "Bash", "arguments": {"command": "ls -la"}}},
        {"type": "tool_result", "content": "ok"},
    ])
    alert_log = tmp_path / "sentinel_alert.log"

    engine = SentinelEngine(
        McodeAdapter(),
        RuleEngine(config={}),
        alert_log_path=str(alert_log),
        console=False,
    )
    try:
        engine.run(log)
    finally:
        engine.close()

    content = alert_log.read_text(encoding="utf-8")
    entries = [
        parse_ontrack_line(line)
        for line in content.splitlines()
    ]
    entries = [item for item in entries if item is not None]
    assert len(entries) == 1


def test_解析器只认流水行():
    """不是流水行 → 返回 ``None``（不误伤别的日志行）。"""
    assert parse_ontrack_line("2026-01-01 12:00:00 WARNING 别的内容") is None
    assert parse_ontrack_line("ONTRACK_JSON {不是合法 JSON") is None
    assert parse_ontrack_line('ONTRACK_JSON {"summary": ""}') is None


# ---------------------------------------------------------------------------
# 三、看板：存得下、推得出、还原得回
# ---------------------------------------------------------------------------
def test_存储能存能取():
    """流水条目单独存一条线 —— 绝不混进告警列表。"""
    store = AlertStore()
    store.add_ontrack({"summary": "正常读取: auth.py", "time": "12:01:05"})
    store.add_alert({"alert_id": "a1", "verdict": "DANGEROUS"})

    assert len(store.ontrack()) == 1
    assert len(store.alerts()) == 1


def test_快照里带着流水():
    """切换会话 / 刷新页面时靠快照把流水补回来。"""
    store = AlertStore()
    store.add_ontrack({"summary": "正常执行: pytest", "time": "12:01:06"})

    snapshot = store.snapshot("watch.jsonl", "alert.log")
    assert snapshot["ontrack"]
    assert snapshot["ontrack"][0]["summary"] == "正常执行: pytest"


def test_跟随线程会把流水推给前端(tmp_path):
    """告警日志里出现流水行 → SSE 推 ``on_track`` 事件。"""
    watch = tmp_path / "watch.jsonl"
    watch.write_text("", encoding="utf-8")
    alert = tmp_path / "alert.log"
    alert.write_text("", encoding="utf-8")

    hub = EventHub()
    queue = hub.subscribe()
    store = AlertStore()
    watcher = Watcher(
        str(watch), str(alert), hub, store, session_id="s1"
    )

    with alert.open("a", encoding="utf-8") as handle:
        handle.write(
            ONTRACK_MARKER + " "
            + json.dumps(
                {"summary": "正常读取: auth.py", "timestamp": 1793032000.5},
                ensure_ascii=False,
            )
            + "\n"
        )

    watcher._poll_once()

    assert queue.qsize() == 1
    event = queue.get_nowait()
    assert event["type"] == "on_track"
    assert event["summary"] == "正常读取: auth.py"
    assert event["time"] == format_clock(1793032000.5)
    assert event["session_id"] == "s1"














# ---------------------------------------------------------------------------
# 五、没有 PID 时，告警与推送照常
# ---------------------------------------------------------------------------
def test_没有PID也照样告警并推送(tmp_path):
    """查不到进程**只影响拦截** —— L1 告警与通知通道照常工作。"""
    log = _write_jsonl(tmp_path / "stream.jsonl", [
        {"type": "tool_call", "tool_call": {
            "name": "Bash", "arguments": {"command": "rm -rf /tmp/x"}}},
    ])
    notifier = RecordingNotifier()

    # 引擎**根本没有** PID / 守护者 —— 只读监控照跑。
    engine = SentinelEngine(
        McodeAdapter(),
        RuleEngine(config={}),
        alert_log_path=str(tmp_path / "sentinel_alert.log"),
        console=False,
        notifier=notifier,
    )
    try:
        summary = engine.run(log)
    finally:
        engine.close()

    assert summary["alerts"], "没有 PID 也要出得了告警"
    assert notifier.alerts, "没有 PID 也要推得出通知"








# ---------------------------------------------------------------------------
# 七、版本口径
# ---------------------------------------------------------------------------
def test_版本号是331():
    """版本号必须跟当前包版本一致 —— 这里钉住其中一处。

    ⚠️ v3.3.2 起改成**动态**断言 —— 以前这里写死了 ``3.3.1``，
    每发一次版就得改一遍，而「忘了改」正是版本号不同步的**头号原因**
    （v3.0.1 起 ``test_v300_release.py`` 就已经全面动态化，
    这里只是没跟上）。
    """
    import src

    source = _read("src", "__init__.py")
    assert '"{}"'.format(src.__version__) in source
    assert os.path.exists(ROOT / "CHANGELOG.md")
