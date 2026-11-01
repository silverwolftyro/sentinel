"""案发时间线测试（v2.2.0）。

覆盖：节点分类（开始目标 / 漂移 / 红线拦截 / 复活重试…）、
节点排序与**间隔**、安全点关联（能回看「当时的改动」）、
日志格式契约（``EVENT_JSON`` 与 ``ALERT_JSON`` 各走各的），
以及看板端点能不能真的吐出这条线。
"""

from __future__ import annotations

import importlib.util
import json
import os
import threading
from pathlib import Path

from fastapi.testclient import TestClient

from src.core.timeline import (
    NODE_ACTION,
    NODE_ALERT,
    NODE_DRIFT,
    NODE_LOOP,
    NODE_REDLINE,
    NODE_SAFE_POINT,
    NODE_STUCK,
    build_timeline,
    format_gap,
    render_timeline_text,
    summarize,
)
from src.gui.server import (
    SessionSpec,
    WatchTarget,
    create_app,
    parse_event_line,
)


# ---------------------------------------------------------------------------
# 样例
# ---------------------------------------------------------------------------
def _alert(
    *,
    ts=1000.0,
    verdict="DANGEROUS",
    severity="red_line",
    rule_id="dangerous_command",
    reason="匹配到高危命令: rm -rf",
    safe_point=None,
    alert_id="a-1",
):
    """造一条告警字典。"""
    data = {
        "alert_id": alert_id,
        "timestamp": ts,
        "verdict": verdict,
        "level": "critical" if severity == "red_line" else "warn",
        "severity": severity,
        "rule_id": rule_id,
        "reason": reason,
        "command": "rm -rf /tmp/x",
        "tool": "Bash",
        "agent": "codex",
    }
    if safe_point is not None:
        data["safe_point"] = safe_point
    return data


def _event(
    *,
    ts=900.0,
    kind="session_start",
    title="开始目标",
    detail="重构 auth 模块",
    severity="info",
    point_id="",
    safe_point_id="",
):
    """造一条操作事件字典。"""
    return {
        "kind": kind,
        "title": title,
        "detail": detail,
        "severity": severity,
        "timestamp": ts,
        "point_id": point_id,
        "safe_point_id": safe_point_id,
        "extra": {},
    }


# ---------------------------------------------------------------------------
# 节点分类
# ---------------------------------------------------------------------------
def test_红线告警变成红线节点():
    """红线级 = 真的被拦下来了 —— 时间线上必须是critical。"""
    nodes = build_timeline([_alert(severity="red_line")])
    assert len(nodes) == 1
    node = nodes[0]
    assert node["kind"] == NODE_REDLINE
    assert node["severity"] == "critical"
    assert "dangerous_command" in node["title"]


def test_普通高危命令不算红线():
    """普通级高危命令只是提示 —— 不能摆出红线的架子。"""
    nodes = build_timeline([_alert(severity="standard")])
    assert nodes[0]["kind"] == NODE_ALERT
    assert nodes[0]["severity"] == "warn"


def test_漂移卡死死循环各有各的节点():
    """三种上下文判定要能被一眼区分。"""
    nodes = build_timeline(
        [
            _alert(verdict="DRIFT", severity="standard", ts=1.0),
            _alert(verdict="STUCK", severity="standard", ts=2.0),
            _alert(verdict="LOOP", severity="standard", ts=3.0),
        ]
    )
    kinds = [node["kind"] for node in nodes]
    assert kinds == [NODE_DRIFT, NODE_STUCK, NODE_LOOP]
    assert all(node["severity"] == "warn" for node in nodes)


def test_操作事件变成操作节点():
    """复活重试这类「我们做了什么」是操作节点。"""
    nodes = build_timeline([], [_event(kind="resurrect", title="复活重试")])
    assert nodes[0]["kind"] == NODE_ACTION
    assert nodes[0]["title"] == "复活重试"


def test_安全点变成快照节点():
    """安全点是「改动之前的那个版本」—— 它是回看的入口。"""
    points = [{"id": "sp-1", "label": "基线安全点", "created_at": 100.0}]
    nodes = build_timeline([], [], safe_points=points)
    assert nodes[0]["kind"] == NODE_SAFE_POINT
    assert nodes[0]["point_id"] == "sp-1"
    assert nodes[0]["has_diff"] is True


# ---------------------------------------------------------------------------
# 排序与间隔
# ---------------------------------------------------------------------------
def test_节点按时间排序():
    """时间线必须是从早到晚 —— 顺序错了整个复盘就没意义了。"""
    nodes = build_timeline(
        [_alert(ts=300.0), _alert(ts=100.0, alert_id="a-2")],
        [_event(ts=200.0)],
    )
    times = [node["ts"] for node in nodes]
    assert times == sorted(times)


def test_间隔被人话化():
    """「隔了多久」是用户真正想看的那个数。"""
    nodes = build_timeline(
        [],
        [
            _event(ts=100.0, title="开始目标"),
            _event(ts=700.0, title="复活重试"),
        ],
    )
    assert nodes[0]["extra"]["gap_text"] == ""
    assert nodes[1]["extra"]["gap_text"] == "10 分 0 秒"


def test_间隔格式():
    """间隔的单位换算不能错。"""
    assert format_gap(0) == "0 秒"
    assert format_gap(45) == "45 秒"
    assert "分" in format_gap(200)
    assert "小时" in format_gap(3700)


# ---------------------------------------------------------------------------
# 安全点关联
# ---------------------------------------------------------------------------
def test_告警能关联到安全点与_diff_基线():
    """红线告警要能回看「当时的改动」—— 靠 safe_point 里的回滚点。"""
    alert = _alert(
        safe_point={
            "id": "sp-captured",
            "rollback_point": {"id": "sp-baseline"},
        }
    )
    nodes = build_timeline([alert])
    node = nodes[0]
    assert node["safe_point_id"] == "sp-captured"
    assert node["point_id"] == "sp-baseline"
    assert node["has_diff"] is True


def test_没有安全点就明说没有():
    """没打快照时不能编一个出来。"""
    nodes = build_timeline([_alert()])
    assert nodes[0]["safe_point_id"] == ""
    assert nodes[0]["point_id"] == ""
    assert nodes[0]["has_diff"] is False


def test_没有自带快照时挂到基线():
    """守护者缺席 → 红线告警没快照，但基线总在。

    用户照样要能回看「Agent 到底改了什么」。
    """
    nodes = build_timeline([_alert()], fallback_point_id="sp-base")
    assert nodes[0]["point_id"] == "sp-base"
    assert nodes[0]["has_diff"] is True
    # ⚠️ safe_point_id 仍然是空的 —— 不能假装那次打过快照。
    assert nodes[0]["safe_point_id"] == ""


# ---------------------------------------------------------------------------
# 汇总与渲染
# ---------------------------------------------------------------------------
def test_汇总给出计数与跨度():
    """汇总要能一句话说清「这一路发生了什么」。"""
    nodes = build_timeline(
        [_alert(ts=100.0), _alert(ts=200.0, alert_id="a-2")],
        [_event(ts=50.0)],
    )
    info = summarize(nodes)
    assert info["total"] == 3
    assert info["counts"][NODE_REDLINE] == 2
    assert info["first"] == 50.0
    assert info["last"] == 200.0


def test_终端渲染不炸():
    """终端渲染也要能给人看。"""
    nodes = build_timeline([_alert()], [_event()])
    text = render_timeline_text(nodes)
    assert "哨兵案发时间线" in text
    assert "红线拦截" in text


def test_空时间线有话说():
    """没有节点时不能只给一句空字符串。"""
    assert render_timeline_text([]).strip() != ""


# ---------------------------------------------------------------------------
# 日志格式契约
# ---------------------------------------------------------------------------
def test_解析事件行():
    """``EVENT_JSON`` 行要能单独解析出来。"""
    line = (
        "2026-10-27 10:00:00,000 WARNING EVENT_JSON "
        + json.dumps(_event(), ensure_ascii=False)
    )
    data = parse_event_line(line)
    assert data is not None
    assert data["kind"] == "session_start"


def test_告警行不会被当成事件():
    """两种契约各走各的 —— 混了看板就会凭空多出告警卡片。"""
    line = (
        "2026-10-27 10:00:00,000 WARNING ALERT_JSON "
        + json.dumps(_alert(), ensure_ascii=False)
    )
    assert parse_event_line(line) is None


def test_没有事件标记的行返回空():
    """普通日志行不是事件。"""
    assert parse_event_line("2026-10-27 10:00:00,000 WARNING 随便什么") is None


def test_事件行_json_坏了返回空():
    """JSON 坏了就当没有 —— 不能把看板搞崩。"""
    line = "2026-10-27 10:00:00,000 WARNING EVENT_JSON {不是 JSON"
    assert parse_event_line(line) is None


def test_缺kind的事件不算事件():
    """没有类型的事件行没有意义 —— 丢掉。"""
    line = (
        "2026-10-27 10:00:00,000 WARNING EVENT_JSON "
        + json.dumps({"title": "没有类型"}, ensure_ascii=False)
    )
    assert parse_event_line(line) is None


# ---------------------------------------------------------------------------
# 看板端到端
# ---------------------------------------------------------------------------
def _write_lines(path: Path, lines) -> None:
    """往告警日志里写若干行。"""
    with path.open("a", encoding="utf-8") as handle:
        for line in lines:
            handle.write(line + "\n")


def test_看板时间线端点(tmp_path):
    """从日志文件到看板端点 —— 整条链路要通。"""
    watch = tmp_path / "agent.jsonl"
    watch.write_text("", encoding="utf-8")
    alert_log = tmp_path / "alerts.log"
    alert_log.write_text("", encoding="utf-8")
    _write_lines(
        alert_log,
        [
            "2026-10-27 10:00:00,000 WARNING EVENT_JSON "
            + json.dumps(_event(), ensure_ascii=False),
            "2026-10-27 10:05:00,000 WARNING ALERT_JSON "
            + json.dumps(
                _alert(
                    ts=1793105100.0,
                    safe_point={
                        "id": "sp-1",
                        "rollback_point": {"id": "sp-base"},
                    },
                ),
                ensure_ascii=False,
            ),
        ],
    )

    spec = SessionSpec(
        id="tl",
        label="时间线会话",
        agent="codex",
        watch_path=str(watch),
        alert_log_path=str(alert_log),
        watch_target=WatchTarget(agent="codex"),
    )
    app = create_app(
        sessions=[spec],
        host="127.0.0.1",
        port=8000,
        start_watcher=True,
        poll_interval=0.05,
    )
    with TestClient(app) as client:
        body = {}
        deadline = 5.0
        waited = 0.0
        while waited < deadline:
            body = client.get("/api/sessions/tl/timeline").json()
            if body.get("nodes"):
                break
            waited += 0.05
            threading.Event().wait(0.05)

    assert body["ok"] is True
    kinds = [node["kind"] for node in body["nodes"]]
    assert NODE_ACTION in kinds
    assert NODE_REDLINE in kinds

    # 红线节点要能关联到 Diff 基线。
    redline = [
        node for node in body["nodes"] if node["kind"] == NODE_REDLINE
    ][0]
    assert redline["point_id"] == "sp-base"
    assert redline["has_diff"] is True


def test_时间线端点对不存在的会话返回404(tmp_path):
    """会话不存在要说清楚，别给一份空时间线糊弄人。"""
    watch = tmp_path / "a.jsonl"
    watch.write_text("", encoding="utf-8")
    alert_log = tmp_path / "a.log"
    alert_log.write_text("", encoding="utf-8")
    spec = SessionSpec(
        id="only",
        watch_path=str(watch),
        alert_log_path=str(alert_log),
    )
    app = create_app(sessions=[spec], host="127.0.0.1", port=8000)
    with TestClient(app) as client:
        response = client.get("/api/sessions/nope/timeline")
    assert response.status_code == 404
    assert response.json()["ok"] is False


# ---------------------------------------------------------------------------
# 启动器：事件落盘
# ---------------------------------------------------------------------------
def _load_launcher():
    """加载启动器脚本（它不在 ``src`` 包里，是独立脚本）。"""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(
        root, "_构建与发布", "scripts", "run_with_sentinel.py"
    )
    spec = importlib.util.spec_from_file_location("sentinel_launcher_v22", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_启动器写出的事件能被看板解析(tmp_path):
    """启动器写的事件行 —— 看板必须认得。"""
    launcher = _load_launcher()
    log = tmp_path / "alerts.log"
    launcher._write_event(
        str(log),
        "session_start",
        "开始目标",
        "重构 auth 模块",
        agent="codex",
    )
    text = log.read_text(encoding="utf-8")
    assert "EVENT_JSON" in text
    data = parse_event_line(text.strip())
    assert data is not None
    assert data["kind"] == "session_start"
    assert data["title"] == "开始目标"


def test_启动器写事件失败不抛异常(tmp_path):
    """写不了事件也不该把启动搞崩。"""
    launcher = _load_launcher()
    # 目录不存在 → open 会失败。
    payload = launcher._write_event(
        str(tmp_path / "no" / "such" / "dir" / "x.log" / "y.log"),
        "session_start",
        "开始目标",
    )
    assert payload["kind"] == "session_start"


# ---------------------------------------------------------------------------
# 前端
# ---------------------------------------------------------------------------
def test_顶栏有时间线按钮():
    """【时间线】按钮得在顶栏上 —— 用户要找得到。"""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(
        os.path.join(root, "src", "gui", "templates", "dashboard.html"),
        encoding="utf-8",
    ) as handle:
        html = handle.read()
    assert 'id="timeline-button"' in html
    assert "时间线" in html


def test_前端接了时间线与体检():
    """两个按钮都要真的绑上处理函数。"""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(
        os.path.join(root, "src", "gui", "static", "dashboard.js"),
        encoding="utf-8",
    ) as handle:
        js = handle.read()
    assert "timeline-button" in js
    assert "health-button" in js
    assert "openTimeline" in js
    assert "openHealth" in js
    assert "/timeline" in js
    assert "/api/health" in js


def test_前端认得时间线事件():
    """``timeline_event`` 走 SSE 进来 —— 前端要认。"""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(
        os.path.join(root, "src", "gui", "static", "dashboard.js"),
        encoding="utf-8",
    ) as handle:
        js = handle.read()
    assert "timeline_event" in js
