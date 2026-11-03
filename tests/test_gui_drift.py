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

from src.core.engine import IntentTracker
from src.core.session_server import (
    TailRunner,
    TaskGoal,
    _tail_scan_stub,
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
