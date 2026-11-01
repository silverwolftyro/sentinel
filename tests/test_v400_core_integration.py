"""v4.0.0 核心组件集成测试。

覆盖三件事（对应 v4.0.0 的三大集成点）：

1. **日志压缩器** —— L2 判定前把窗口压成演进流（不再是「第 N 步」）；
2. **微观统计** —— 独立前置检测器，命中 ``STATS_LOOP`` 就打断并出告警；
3. **优先级队列** —— 红线解释（P0）插队到漂移判定（P1）前面。

⚠️ 断言的是**调用链路**（谁被调用、拿到的输入长什么样），
不是组件内部实现 —— 组件自己的单元测试在
``tests/test_log_compressor.py`` / ``tests/test_micro_stats.py`` /
``tests/test_priority_queue.py`` / ``tests/test_l2_scheduler.py``。
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

from src.adapters.mcode import McodeAdapter
from src.core.engine import (
    IntentTracker,
    SentinelEngine,
    _l2_priority_for_verdict,
    build_compressed_summary,
    build_window_summary,
)
from src.core.l2_scheduler import L2Scheduler
from src.core.micro_stats import VERDICT_STATS_LOOP, MicroStatsDetector
from src.core.priority_queue import PRIORITY_P0, PRIORITY_P1, PRIORITY_P2
from src.rules.basic import RuleEngine

# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


def _thinking(text: str) -> dict:
    """造一条思考事件。"""
    return {"type": "thinking", "text": text}


def _tool(name: str, **arguments: object) -> dict:
    """造一条工具调用事件。"""
    return {
        "type": "tool_call",
        "tool": {"name": name, "arguments": dict(arguments)},
    }


WINDOW_EVENTS = [
    _thinking("我先把旧构建产物清掉"),
    _tool("Read", file_path="D:/proj/build/old.js"),
    _tool("Bash", command="rm -rf build"),
    _tool("Bash", command="rm -rf build"),
    _tool("Bash", command="rm -rf build"),
]


class _SpyContextTracker:
    """上下文追踪器**探针** —— 记录自己被调了几次。"""

    def __init__(self) -> None:
        self.calls = 0

    def observe(self, event):  # noqa: ANN001, ANN201 —— 探针不挑类型
        """记录一次调用。

        :param event: 当前事件。
        :return: 永远不命中（隐式返回 ``None``）。
        """
        self.calls += 1


# ---------------------------------------------------------------------------
# 任务一 A：日志压缩器接入
# ---------------------------------------------------------------------------


def test_compressed_summary_drops_goal_line_and_uses_step_markers():
    """压缩摘要要剥掉目标行，并换成「[N] [...]」步标记。"""
    summary = build_compressed_summary(WINDOW_EVENTS, "清理旧构建产物")

    assert "[Task Goal]:" not in summary
    assert "[Current Action]:" in summary
    assert "[1]" in summary
    assert "第1步" not in summary


def test_compressed_summary_keeps_command_text_old_format_lost_it():
    """压缩器能保住命令文本 —— 旧格式在这一步是「（无命令文本）」。"""
    summary = build_compressed_summary(WINDOW_EVENTS, "清理旧构建产物")
    old = build_window_summary(WINDOW_EVENTS)

    assert "rm -rf build" in summary
    assert "（无命令文本）" in old


def test_compressed_summary_falls_back_when_empty():
    """空窗口返回空串（不回落成噪声）。"""
    assert build_compressed_summary([], "目标") == ""


def test_intent_tracker_feeds_compressed_summary_to_evaluator():
    """IntentTracker 交给 L2 的是**压缩后**的演进流。"""
    seen = {}

    def evaluator(goal, summary):
        seen["goal"] = goal
        seen["summary"] = summary
        return SimpleNamespace(drift=False, reason="", degraded=False)

    tracker = IntentTracker(
        goal="清理旧构建产物",
        window=len(WINDOW_EVENTS),
        threshold=2,
        evaluator=evaluator,
    )
    for event in WINDOW_EVENTS:
        tracker.observe(event)

    assert seen, "判定回调没有被调用"
    assert seen["goal"] == "清理旧构建产物"
    assert "[Current Action]:" in seen["summary"]
    assert "第1步" not in seen["summary"]


# ---------------------------------------------------------------------------
# 任务一 B：微观统计接入
# ---------------------------------------------------------------------------


def _make_engine(tmp_path, **kwargs) -> SentinelEngine:
    """造一个不打印、不落真实日志的引擎。"""
    return SentinelEngine(
        McodeAdapter(),
        RuleEngine(config={}),
        alert_log_path=str(tmp_path / "sentinel_alert.log"),
        console=False,
        **kwargs,
    )


def test_micro_stats_emits_stats_loop_alert(tmp_path):
    """同一文件读太多次 → 产出 STATS_LOOP 告警。"""
    detector = MicroStatsDetector(
        window_minutes=10.0,
        same_file_threshold=3,
        same_command_threshold=999,
        stall_minutes=999.0,
    )
    engine = _make_engine(tmp_path, micro_stats=detector)
    try:
        for _ in range(4):
            engine.process_event(_tool("Read", file_path="D:/proj/a.py"))
    finally:
        engine.close()

    stats_alerts = [
        alert
        for alert in engine.alerts
        if alert.get("verdict") == VERDICT_STATS_LOOP
    ]
    assert stats_alerts, "没有产出 STATS_LOOP 告警"
    assert "micro_stats" in stats_alerts[0].get("triggers", [])
    assert stats_alerts[0].get("micro_stats", {}).get("kind") == "file_repeat"


def test_micro_stats_hit_interrupts_later_detectors(tmp_path):
    """命中微统计就**打断**：后面的上下文判定不再跑。

    阈值是 3，所以前 3 次 Read 还没触发（上下文判定照跑），
    第 4 次命中 ``STATS_LOOP`` → 上下文判定被跳过。
    对照组（不挂检测器）是 4 次 —— 差的那一次就是打断的证据。
    """
    detector = MicroStatsDetector(
        window_minutes=10.0,
        same_file_threshold=3,
        same_command_threshold=999,
        stall_minutes=999.0,
    )
    spy = _SpyContextTracker()
    engine = _make_engine(
        tmp_path,
        micro_stats=detector,
        context_tracker=spy,
    )
    try:
        for _ in range(4):
            engine.process_event(_tool("Read", file_path="D:/proj/a.py"))
    finally:
        engine.close()

    assert spy.calls == 3, "微统计命中后仍跑了上下文判定"


def test_micro_stats_absent_leaves_pipeline_untouched(tmp_path):
    """没挂检测器时，链路行为与 v3.9.0 完全一致。"""
    spy = _SpyContextTracker()
    engine = _make_engine(tmp_path, context_tracker=spy)
    try:
        for _ in range(4):
            engine.process_event(_tool("Read", file_path="D:/proj/a.py"))
    finally:
        engine.close()

    assert spy.calls == 4


def test_red_line_alert_still_precedes_micro_stats(tmp_path):
    """红线照常先出 —— 微统计不吞红线（R1–R9 一字未改）。"""
    detector = MicroStatsDetector(
        window_minutes=10.0,
        same_file_threshold=3,
        same_command_threshold=999,
        stall_minutes=999.0,
    )
    engine = _make_engine(tmp_path, micro_stats=detector)
    try:
        for _ in range(4):
            engine.process_event(_tool("Read", file_path="D:/proj/a.py"))
        engine.process_event(_tool("Bash", command="rm -rf /"))
    finally:
        engine.close()

    verdicts = [alert.get("verdict") for alert in engine.alerts]
    assert "DANGEROUS" in verdicts, "红线没有触发"
    assert VERDICT_STATS_LOOP in verdicts, "微统计没有触发"


# ---------------------------------------------------------------------------
# 任务一 C：优先级队列接入
# ---------------------------------------------------------------------------


def test_engine_shares_one_scheduler_between_drift_and_explain():
    """漂移判定与红线解释必须共用**同一个**调度器。"""
    tracker = IntentTracker(goal="目标", evaluator=lambda *_: None)
    engine = SentinelEngine(
        McodeAdapter(),
        RuleEngine(config={}),
        console=False,
        intent_tracker=tracker,
    )
    try:
        assert engine.l2_scheduler is not None
        assert tracker.scheduler is engine.l2_scheduler
        assert engine.explanation_worker.scheduler is engine.l2_scheduler
    finally:
        engine.close()


def test_red_line_verdict_maps_to_p0():
    """红线 → P0；危险动作 → P1；其余 → P2。"""
    assert (
        _l2_priority_for_verdict({"severity": "red_line"})
        is PRIORITY_P0
    )
    assert (
        _l2_priority_for_verdict(
            {"verdict": "DANGEROUS", "severity": "standard"}
        )
        is PRIORITY_P1
    )
    assert _l2_priority_for_verdict({"verdict": "ON_TRACK"}) is PRIORITY_P2
    assert _l2_priority_for_verdict(None) is PRIORITY_P2


def test_p0_jumps_ahead_of_already_queued_p2():
    """P0 插到**已排队**的 P2 前面（跨档严格插队）。"""
    order = []
    running = threading.Event()
    release = threading.Event()

    def slow():
        running.set()
        release.wait(5.0)
        order.append("p2-running")
        return "slow"

    def queued_p2():
        order.append("p2-queued")
        return "queued"

    def red_line():
        order.append("p0")
        return "red"

    scheduler = L2Scheduler()
    try:
        blocker = threading.Thread(
            target=lambda: scheduler.call(
                "t-slow", slow, priority=PRIORITY_P2, timeout=10.0
            )
        )
        blocker.start()
        assert running.wait(5.0), "慢任务没有开跑"

        queued = threading.Thread(
            target=lambda: scheduler.call(
                "t-p2", queued_p2, priority=PRIORITY_P2, timeout=10.0
            )
        )
        queued.start()

        deadline = time.time() + 5.0
        while scheduler.pending() < 1 and time.time() < deadline:
            time.sleep(0.01)
        assert scheduler.pending() >= 1, "P2 没有排进队列"

        scheduler.call("t-p0", red_line, priority=PRIORITY_P0, timeout=10.0)
        release.set()
        blocker.join(5.0)
        queued.join(5.0)
    finally:
        release.set()
        scheduler.stop()

    assert "p0" in order
    assert "p2-queued" in order
    assert order.index("p0") < order.index("p2-queued")


def test_intent_tracker_without_scheduler_calls_evaluator_directly():
    """没挂调度器时**直连**判定回调（行为与 v3.9.0 一致）。"""
    calls = []

    def evaluator(goal, summary):
        calls.append((goal, summary))
        return SimpleNamespace(drift=False, reason="", degraded=False)

    tracker = IntentTracker(
        goal="目标",
        window=1,
        threshold=2,
        evaluator=evaluator,
    )
    assert tracker.scheduler is None
    tracker.observe(_tool("Bash", command="ls"))

    assert len(calls) == 1
