"""哨兵看板 v0.7.0 新增能力的测试。

覆盖三块：

1. **任务目标（意图锚点）端点** —— ``GET/POST /api/task_goal``；
2. **DRIFT 告警在看板里的归类** —— 橘黄卡片，区别于红色红线级；
3. **流式监听运行器** —— 目标在运行中热更新。

全部离线：不绑定端口、不访问网络、不碰真实日志。
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi.testclient import TestClient

from src.core.engine import IntentTracker
from src.gui.server import (
    TailRunner,
    TaskGoal,
    _tail_scan_stub,
    create_app,
    normalize_alert,
    parse_alert_line,
)

#: 仓库根目录（tests/ 的上一级）。
ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = ROOT / "src" / "gui" / "static"

#: 引擎写进告警日志的一行「意图漂移」告警。
DRIFT_ALERT_PAYLOAD: Dict[str, Any] = {
    "timestamp": 1793032000.5,
    "verdict": "DRIFT",
    "level": "warn",
    "rule_id": "intent_drift",
    "reason": "[WARNING] 意图漂移：Agent 可能偏离了原始任务",
    "evidence": "第3步开始在做与目标无关的事",
    "severity": "standard",
    "category": "intent_drift",
    "source": "sentinel",
    "event_type": "intent_drift",
    "tool": "IntentTracker",
    "command": "第1步 [思考] …",
    "arguments": {},
    "explanation": "模型判定 Agent 正在做与原始目标无关的操作。",
    "explanation_degraded": False,
}


class _FakeLLM:
    """假装有 ``evaluate_drift`` 的模型客户端。"""

    def __init__(self) -> None:
        self.calls: List[tuple] = []

    def evaluate_drift(self, goal: str, summary: str) -> Any:
        """记录一次调用，返回「没漂移」。"""
        self.calls.append((goal, summary))
        return None


class _FakeEngine:
    """最小引擎替身，只保留 TailRunner 用得到的接口。"""

    def __init__(self, tracker: Optional[IntentTracker] = None) -> None:
        self.intent_tracker = tracker
        self.stop_event = threading.Event()
        self.ran: List[tuple] = []
        self.closed = False

    def run(self, path: Any, tail: bool = False) -> Dict[str, Any]:
        """记录一次运行。"""
        self.ran.append((path, tail))
        return {"total_events": 0}

    def close(self) -> None:
        """标记已关闭。"""
        self.closed = True


def _make_app(tmp_path, **kwargs):
    """造一个指向临时目录的应用（默认不启动后台线程）。"""
    watch = tmp_path / "agent.jsonl"
    watch.write_text("", encoding="utf-8")
    alert_log = tmp_path / "alerts.log"
    alert_log.write_text("", encoding="utf-8")
    return create_app(
        str(watch),
        str(alert_log),
        start_watcher=False,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# 任务目标端点
# ---------------------------------------------------------------------------
def test_task_goal_endpoint_defaults_to_empty(tmp_path):
    """没设过目标时，GET 返回空串。"""
    app = _make_app(tmp_path)
    with TestClient(app) as client:
        response = client.get("/api/task_goal")

    assert response.status_code == 200
    assert response.json() == {"ok": True, "goal": ""}


def test_task_goal_post_sets_and_returns_goal(tmp_path):
    """POST 设定目标，返回设定后的值。"""
    app = _make_app(tmp_path)
    with TestClient(app) as client:
        response = client.post(
            "/api/task_goal", json={"goal": "重构 auth 模块并修复 bug"}
        )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["goal"] == "重构 auth 模块并修复 bug"


def test_task_goal_post_trims_whitespace(tmp_path):
    """首尾空白被裁掉 —— 目标文本会直接进 Prompt。"""
    app = _make_app(tmp_path)
    with TestClient(app) as client:
        client.post("/api/task_goal", json={"goal": "  修复登录 bug  "})
        current = client.get("/api/task_goal").json()

    assert current["goal"] == "修复登录 bug"


def test_task_goal_post_empty_string_clears_goal(tmp_path):
    """空串表示停用意图漂移检测。"""
    app = _make_app(tmp_path)
    with TestClient(app) as client:
        client.post("/api/task_goal", json={"goal": "清理构建产物"})
        client.post("/api/task_goal", json={"goal": "   "})
        current = client.get("/api/task_goal").json()

    assert current["goal"] == ""


def test_task_goal_holder_shares_state_with_app(tmp_path):
    """应用与外部持有的是**同一个** TaskGoal 实例。"""
    holder = TaskGoal("初始目标")
    app = _make_app(tmp_path, task_goal=holder)
    with TestClient(app) as client:
        client.post("/api/task_goal", json={"goal": "新目标"})

    assert holder.get() == "新目标"


def test_task_goal_holder_notifies_listener_on_change(tmp_path):
    """目标变了，监听器会被叫醒（引擎靠它换锚点）。"""
    holder = TaskGoal()
    seen: List[str] = []
    holder.on_change(seen.append)

    holder.set("新的目标")

    assert seen == ["新的目标"]


def test_task_goal_holder_skips_notify_when_unchanged(tmp_path):
    """目标没变就不打扰监听器。"""
    holder = TaskGoal("一样的目标")
    seen: List[str] = []
    holder.on_change(seen.append)

    holder.set("一样的目标")

    assert seen == []


# ---------------------------------------------------------------------------
# DRIFT 告警在看板里的归类
# ---------------------------------------------------------------------------
def test_drift_alert_is_parsed_from_alert_log():
    """引擎写的漂移行能被看板的日志解析器认出来。"""
    line = (
        "2026-10-26 22:48:46,393 WARNING ALERT_JSON "
        + json.dumps(DRIFT_ALERT_PAYLOAD, ensure_ascii=False)
    )

    parsed = parse_alert_line(line)

    assert parsed is not None
    assert parsed["verdict"] == "DRIFT"


def test_drift_alert_normalized_is_standard_severity():
    """漂移是「标准级」，不是红线级 —— 所以守护者不会碰它。"""
    normalized = normalize_alert(DRIFT_ALERT_PAYLOAD)

    assert normalized["verdict"] == "DRIFT"
    assert normalized["severity"] == "standard"
    assert normalized["category"] == "intent_drift"
    assert normalized["reason"].startswith("[WARNING] 意图漂移")


def test_dashboard_js_maps_drift_to_its_own_class():
    """前端把 DRIFT 归到自己的类，不落进红色的 danger。"""
    js = (STATIC_DIR / "dashboard.js").read_text(encoding="utf-8")

    assert '"DRIFT"' in js
    assert '"drift"' in js
    # DRIFT 必须在 DANGEROUS 之前被单独判掉。
    drift_at = js.index('"DRIFT"')
    danger_at = js.index('"DANGEROUS"')
    assert drift_at > danger_at
    assert 'return "drift";' in js


def test_dashboard_css_defines_drift_color():
    """橘黄色漂移卡片有独立的样式变量。"""
    css = (STATIC_DIR / "dashboard.css").read_text(encoding="utf-8")

    assert "--drift:" in css
    assert ".alert-card--drift" in css
    assert ".tag--drift" in css


def test_dashboard_html_has_goal_input():
    """顶栏有任务目标输入框。"""
    from src.gui.server import TEMPLATES_DIR

    html = (Path(TEMPLATES_DIR) / "dashboard.html").read_text(encoding="utf-8")

    assert "goal-form" in html
    assert "task_goal" in html or "任务目标" in html


# ---------------------------------------------------------------------------
# 流式监听运行器
# ---------------------------------------------------------------------------
def test_tail_runner_apply_goal_updates_existing_tracker():
    """引擎已有追踪器时，改目标会换锚点并清零连续计数。"""
    tracker = IntentTracker(goal="旧目标", evaluator=_FakeLLM().evaluate_drift)
    tracker.consecutive_drift = 1
    engine = _FakeEngine(tracker)
    runner = TailRunner(engine, "x.jsonl")

    runner.apply_goal("新目标")

    assert tracker.goal == "新目标"
    assert tracker.consecutive_drift == 0


def test_tail_runner_apply_goal_creates_tracker_when_missing():
    """启动时没设目标、后来才设的 —— 补一个追踪器。"""
    engine = _FakeEngine(None)
    llm = _FakeLLM()
    runner = TailRunner(
        engine,
        "x.jsonl",
        drift_config={"window": 5, "threshold": 2, "enabled": True},
        llm_client=llm,
    )

    runner.apply_goal("重构 auth 模块")

    assert engine.intent_tracker is not None
    assert engine.intent_tracker.goal == "重构 auth 模块"
    assert engine.intent_tracker.window == 5
    assert engine.intent_tracker.threshold == 2


def test_tail_runner_apply_goal_without_model_does_nothing():
    """没有模型就不做漂移判定（规则层绝不调用模型，也绝不自己编）。"""
    engine = _FakeEngine(None)
    runner = TailRunner(engine, "x.jsonl", llm_client=None)

    runner.apply_goal("重构 auth 模块")

    assert engine.intent_tracker is None


def test_tail_runner_apply_goal_empty_goal_without_tracker():
    """清空目标且引擎本来就没有追踪器 —— 什么都不做。"""
    engine = _FakeEngine(None)
    runner = TailRunner(engine, "x.jsonl", llm_client=_FakeLLM())

    runner.apply_goal("")

    assert engine.intent_tracker is None


def test_tail_runner_stop_sets_stop_event():
    """停止监听只是置位停止信号 —— 绝不 kill 任何进程。"""
    engine = _FakeEngine()
    runner = TailRunner(engine, "x.jsonl")

    runner.stop()

    assert engine.stop_event.is_set()


def test_tail_scan_stub_reports_tail_mode():
    """流式模式下的「立即扫描」是个占位，不重复扫历史。"""
    result = _tail_scan_stub("x.jsonl")

    assert result["ok"] is True
    assert result["mode"] == "tail"
    # 不能带 counts —— 否则看板会拿空统计覆盖真实计数。
    assert "counts" not in result
