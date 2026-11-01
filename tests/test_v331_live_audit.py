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
import re
from pathlib import Path

from fastapi.testclient import TestClient

from src.adapters.mcode import McodeAdapter
from src.core.engine import ONTRACK_MARKER, SentinelEngine
from src.core.live_audit import format_clock, summarize_event
from src.gui.server import (
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
# 四、前端：流水区 + 强引导 + 宽容度
# ---------------------------------------------------------------------------
def test_模板里有实时审核流水区():
    """告警面板里要有一块「实时审核流水」。"""
    html = _read("src", "gui", "templates", "dashboard.html")
    assert 'id="live-audit"' in html
    assert 'id="live-audit-lines"' in html
    assert "实时审核流水" in html


def test_流水是独立的一行且卡片不再压它():
    """流水层是**正常的一行**，告警卡片**不再**靠 z-index 压它。

    ⚠️ v3.5.2 起**彻底废除**了 v3.3.1 的旧设计 ——
    那时流水被做成 ``position: absolute`` 的背景层，
    告警卡片靠 ``z-index: 2`` 压在上面，
    结果两段文字**直接叠在一起**，完全没法看。
    这一版改成 Flex 纵向布局，上下分离，各占各的行。
    """
    css = _read("src", "gui", "static", "dashboard.css")
    assert ".live-audit {" in css
    assert ".live-audit__line {" in css

    # ⚠️ 规则体里**含注释** —— 而注释里恰好提到了 z-index，
    #    所以先把注释剥掉再断言（不然会把注释当成声明）。
    def _strip_comments(text):
        return re.sub(r"/\*.*?\*/", "", text, flags=re.S)

    # 流水层不再是绝对定位的背景层。
    audit_block = _strip_comments(
        css.split(".live-audit {", 1)[1].split("}", 1)[0]
    )
    assert "position: absolute" not in audit_block
    assert "position: static;" in audit_block

    # 告警卡片不再有 z-index —— 中间面板里不该有层级打架。
    card_block = _strip_comments(
        css.split(".alert-card {", 1)[1].split("}", 1)[0]
    )
    assert "z-index" not in card_block


def test_前端会处理on_track事件():
    """SSE 的 ``on_track`` 事件要有人接。"""
    js = _read("src", "gui", "static", "dashboard.js")
    assert 'event.type === "on_track"' in js
    assert "appendLiveAudit" in js
    assert "renderLiveAudit" in js


def test_未设置会变成橘黄高亮():
    """没目标时，「未设置」不能只是灰字。"""
    js = _read("src", "gui", "static", "dashboard.js")
    css = _read("src", "gui", "static", "dashboard.css")
    html = _read("src", "gui", "templates", "dashboard.html")

    assert "watch-target__source--none" in js
    assert ".watch-target__source--none {" in css
    assert "var(--drift)" in css
    # 气泡常驻，不是「鼠标移上去才有」。
    assert 'id="watch-goal-nudge"' in html
    assert ".watch-target__nudge {" in css


def test_气泡文案是任务书原话():
    """引导语要一字不差 —— 用户照着做才行。"""
    text = "雷达找不到初始目标，请在此手动输入任务目标，以启用漂移监控。"
    js = _read("src", "gui", "static", "dashboard.js")
    html = _read("src", "gui", "templates", "dashboard.html")
    assert text in js
    assert text in html


def test_未绑定进程有悬停提示():
    """``[未绑定进程]`` 的悬停提示要说清楚「为什么」和「怎么办」。"""
    js = _read("src", "gui", "static", "dashboard.js")
    assert "未找到对应进程。如需拦截，请使用 --agent 模式启动" in js
    assert "或确保 Agent 命令行包含日志路径。" in js
    assert "UNBOUND_PID_HINT" in js
    # 也要说清楚「告警照常」—— 免得用户以为整个哨兵瘫了。
    assert "L1/L2 告警与推送不受影响" in js


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


def test_没绑定的会话不提供武装能力(tmp_path):
    """没有 PID → 武装开关不可用（但**不是**因为告警坏了）。"""
    from src.gui.server import create_app

    watch = tmp_path / "watch.jsonl"
    watch.write_text("", encoding="utf-8")
    alert = tmp_path / "alert.log"
    alert.write_text("", encoding="utf-8")

    def factory(info):
        """假工厂：挂载一个**没有进程绑定**的会话。"""
        return {
            "ok": True,
            "alert_log_path": str(tmp_path / "attached.alerts.jsonl"),
            "scan_callback": lambda target: {"ok": True, "counts": {}},
            "workdir": "",
            "tail_runner": None,
        }

    app = create_app(
        str(watch),
        str(alert),
        start_watcher=False,
        attach_factory=factory,
    )
    client = TestClient(app)

    log = tmp_path / "mcode.jsonl"
    _write_jsonl(log, [
        {"type": "tool_call", "tool_call": {
            "name": "Bash", "arguments": {"command": "ls"}}},
    ])
    attached = client.post("/api/attach", json={"path": str(log)})
    assert attached.status_code == 200
    session = attached.json()["session"]

    assert session["process_bound"] is False
    assert session["pid"] == 0
    # 武装不可用 —— 但会话本身照常存在、照常被监控。
    assert session["arm_available"] is False
    assert session["state"] in ("starting", "running", "idle")


# ---------------------------------------------------------------------------
# 六、手动锚定后，顶部自动变成「手动锚定」
# ---------------------------------------------------------------------------
def test_设定目标后来源是手动锚定(tmp_path):
    """用户点「设定」→ 顶部的目标来源变成「手动锚定」。"""
    from src.gui.server import create_app

    watch = tmp_path / "watch.jsonl"
    watch.write_text("", encoding="utf-8")
    alert = tmp_path / "alert.log"
    alert.write_text("", encoding="utf-8")

    def factory(info):
        """假工厂。"""
        return {
            "ok": True,
            "alert_log_path": str(tmp_path / "attached.alerts.jsonl"),
            "scan_callback": lambda target: {"ok": True, "counts": {}},
            "workdir": "",
            "tail_runner": None,
        }

    app = create_app(
        str(watch),
        str(alert),
        start_watcher=False,
        attach_factory=factory,
    )
    client = TestClient(app)

    log = tmp_path / "mcode.jsonl"
    _write_jsonl(log, [
        {"type": "tool_call", "tool_call": {
            "name": "Bash", "arguments": {"command": "ls"}}},
    ])
    attached = client.post("/api/attach", json={"path": str(log)})
    session_id = attached.json()["session"]["id"]

    before = client.get(
        "/api/sessions/{}/watch_target".format(session_id)
    ).json()
    assert before["goal_source"] == "none"
    assert before["goal_source_label"] == "未设置"

    client.post(
        "/api/sessions/{}/task_goal".format(session_id),
        json={"goal": "重构 auth 模块并修复登录 bug"},
    )

    after = client.get(
        "/api/sessions/{}/watch_target".format(session_id)
    ).json()
    assert after["goal_source"] == "explicit"
    assert after["goal_source_label"] == "手动锚定"
    assert after["goal"] == "重构 auth 模块并修复登录 bug"


def test_前端设定后立刻刷新顶部状态():
    """点了「设定」→ 顶部状态**立刻**跟上（不等 SSE 绕一圈）。"""
    js = _read("src", "gui", "static", "dashboard.js")
    block = js.split("function submitTaskGoal", 1)[1].split("\n  }", 1)[0]
    assert "loadSessionTarget" in block or "loadWatchTarget" in block


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
