"""v0.8.0 —— 异步漂移审计与自动锚定任务目标的测试。

两条主线：

1. **异步队列** —— 主循环**绝不被模型调用堵住**；
   模型太慢时按背压策略丢弃窗口并留下 ``[WARN] 漂移检测超载``。
2. **自动锚定** —— 流里第一条用户请求就是任务目标；
   显式指定的目标（命令行 / 看板）谁都不许覆盖。
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, List

from src.core.engine import (
    EVENT_USER_MESSAGE,
    IntentTracker,
    SentinelEngine,
    build_window_summary,
)
from src.rules.basic import RuleEngine


class _SlowEvaluator:
    """一个**很慢**的判定器：每次调用都阻塞到放行。

    用来证明「主循环不会被模型调用拖住」。

    :param delay: 每次判定的阻塞秒数。
    :param drift: 判定结论。
    """

    def __init__(self, delay: float = 0.5, drift: bool = False) -> None:
        self.delay = float(delay)
        self.drift = bool(drift)
        self.calls: List[tuple] = []
        self._lock = threading.Lock()
        self._release = threading.Event()
        self._release.set()

    def block(self) -> None:
        """让后续判定开始阻塞（模拟模型卡住）。"""
        self._release.clear()

    def release(self) -> None:
        """放行被阻塞的判定。"""
        self._release.set()

    def __call__(self, goal: str, summary: str) -> Any:
        """判定入口。"""
        with self._lock:
            self.calls.append((goal, summary))
        self._release.wait(timeout=10.0)
        time.sleep(self.delay)
        return _Result(drift=self.drift, reason="测试结论")


class _Result:
    """判定结果的最小替身。

    :param drift: 是否漂移。
    :param reason: 结论理由。
    :param degraded: 是否降级。
    """

    def __init__(
        self,
        drift: bool = False,
        reason: str = "",
        degraded: bool = False,
    ) -> None:
        self.drift = drift
        self.reason = reason
        self.degraded = degraded


def _tool_event(index: int) -> Dict[str, Any]:
    """造一条工具调用事件。"""
    return {
        "source": "test",
        "type": "tool_call",
        "tool": {
            "name": "Bash",
            "arguments": {},
            "command": f"echo step-{index}",
        },
        "text": "",
    }


def _user_event(text: str) -> Dict[str, Any]:
    """造一条用户请求事件。"""
    return {
        "source": "test",
        "type": EVENT_USER_MESSAGE,
        "tool": None,
        "text": text,
    }


# ---------------------------------------------------------------------------
# 一、异步队列：不阻塞 + 背压
# ---------------------------------------------------------------------------
class TestAsyncQueue:
    """异步漂移审计的行为约定。"""

    def test_observe_should_not_block_on_slow_model(self) -> None:
        """模型卡住时，observe() 仍要**立刻**返回。"""
        evaluator = _SlowEvaluator(delay=0.0)
        evaluator.block()
        tracker = IntentTracker(
            goal="重构 auth 模块",
            window=5,
            threshold=2,
            evaluator=evaluator,
            wait_for_result=False,
        )
        tracker.attach_async(lambda verdict: None)

        started = time.monotonic()
        for index in range(5):
            tracker.observe(_tool_event(index))
        elapsed = time.monotonic() - started

        assert elapsed < 0.5, "observe() 被模型调用堵住了"
        tracker.stop()

    def test_engine_process_event_should_not_block(self) -> None:
        """引擎的单步入口也不能被模型调用堵住。"""
        evaluator = _SlowEvaluator(delay=0.0)
        evaluator.block()
        tracker = IntentTracker(
            goal="重构 auth 模块",
            window=5,
            threshold=2,
            evaluator=evaluator,
        )
        engine = SentinelEngine(
            _FakeAdapter(),
            RuleEngine({}),
            alert_log_path=None,
            console=False,
            llm_client=None,
            intent_tracker=tracker,
        )
        try:
            started = time.monotonic()
            for index in range(5):
                engine.process_event(_tool_event(index))
            elapsed = time.monotonic() - started
            assert elapsed < 0.5, "引擎主循环被漂移判定堵住了"
        finally:
            engine.close()

    def test_should_drop_windows_when_queue_is_full(self) -> None:
        """模型太慢 → 队列塞满 → 按背压策略**丢弃**窗口。"""
        evaluator = _SlowEvaluator(delay=0.0)
        evaluator.block()
        tracker = IntentTracker(
            goal="重构 auth 模块",
            window=5,
            threshold=2,
            evaluator=evaluator,
            max_pending=3,
            wait_for_result=False,
        )
        tracker.attach_async(lambda verdict: None)

        # 10 个窗口 = 50 步事件。
        for index in range(50):
            tracker.observe(_tool_event(index))

        assert tracker.dropped > 0, "队列满了却没有丢弃任何窗口"
        assert tracker.pending <= tracker.max_pending + 1
        evaluator.release()
        tracker.stop()

    def test_should_log_overload_warning(self, caplog) -> None:
        """超载要留下 ``[WARN] 漂移检测超载`` 的痕迹。"""
        evaluator = _SlowEvaluator(delay=0.0)
        evaluator.block()
        tracker = IntentTracker(
            goal="重构 auth 模块",
            window=5,
            threshold=2,
            evaluator=evaluator,
            max_pending=1,
            wait_for_result=False,
        )
        tracker.attach_async(lambda verdict: None)

        with caplog.at_level("WARNING"):
            for index in range(30):
                tracker.observe(_tool_event(index))

        assert any("漂移检测超载" in record.message for record in caplog.records)
        evaluator.release()
        tracker.stop()

    def test_should_deliver_result_via_callback(self) -> None:
        """异步模式下结论走回调。"""
        evaluator = _SlowEvaluator(delay=0.0, drift=True)
        tracker = IntentTracker(
            goal="重构 auth 模块",
            window=5,
            threshold=2,
            evaluator=evaluator,
            wait_for_result=False,
        )
        received: List[Dict[str, Any]] = []
        tracker.attach_async(received.append)

        for index in range(5):
            tracker.observe(_tool_event(index))
        assert tracker.drain(timeout=5.0)

        assert len(received) == 1
        assert received[0]["drift"] is True
        assert received[0]["alert"] is False  # 第一次漂移还不够
        tracker.stop()

    def test_should_alert_after_two_consecutive_drift_windows(self) -> None:
        """连续 2 个窗口漂移 → 结论里 ``alert`` 为真。"""
        evaluator = _SlowEvaluator(delay=0.0, drift=True)
        tracker = IntentTracker(
            goal="重构 auth 模块",
            window=5,
            threshold=2,
            evaluator=evaluator,
            wait_for_result=False,
        )
        received: List[Dict[str, Any]] = []
        tracker.attach_async(received.append)

        for index in range(10):
            tracker.observe(_tool_event(index))
        assert tracker.drain(timeout=5.0)

        assert len(received) == 2
        assert received[0]["alert"] is False
        assert received[1]["alert"] is True
        tracker.stop()

    def test_drain_should_wait_for_pending_windows(self) -> None:
        """收尾时 drain() 要等所有窗口出结论 —— 否则告警会丢。"""
        evaluator = _SlowEvaluator(delay=0.05, drift=True)
        tracker = IntentTracker(
            goal="重构 auth 模块",
            window=5,
            threshold=2,
            evaluator=evaluator,
            wait_for_result=False,
        )
        received: List[Dict[str, Any]] = []
        tracker.attach_async(received.append)

        for index in range(5):
            tracker.observe(_tool_event(index))

        assert tracker.drain(timeout=5.0)
        assert len(received) == 1
        tracker.stop()

    def test_should_discard_inflight_result_after_goal_change(self) -> None:
        """换目标 → 在途的旧判定作废（换了目标，之前的判定不算数）。"""
        evaluator = _SlowEvaluator(delay=0.0)
        tracker = IntentTracker(
            goal="旧目标",
            window=5,
            threshold=2,
            evaluator=evaluator,
            wait_for_result=False,
        )
        received: List[Dict[str, Any]] = []
        tracker.attach_async(received.append)

        for index in range(5):
            tracker.observe(_tool_event(index))
        tracker.set_goal("新目标")
        evaluator.release()
        tracker.drain(timeout=5.0)

        assert received == [], "换目标后旧判定仍然生效了"
        tracker.stop()

    def test_sync_mode_should_still_return_verdict(self) -> None:
        """同步模式（默认）仍返回结论 —— 测试与脚本化调用不受影响。"""
        evaluator = _SlowEvaluator(delay=0.0, drift=True)
        tracker = IntentTracker(
            goal="重构 auth 模块",
            window=5,
            threshold=2,
            evaluator=evaluator,
        )

        verdict = None
        for index in range(5):
            verdict = tracker.observe(_tool_event(index))

        assert verdict is not None
        assert verdict["drift"] is True
        assert verdict["alert"] is False
        tracker.stop()


# ---------------------------------------------------------------------------
# 二、自动锚定任务目标
# ---------------------------------------------------------------------------
class TestAutoAnchor:
    """从流里自动提取任务目标。"""

    def test_should_anchor_from_first_user_message(self) -> None:
        """第一条用户请求就是任务目标。"""
        tracker = IntentTracker(window=5, threshold=2, evaluator=_SlowEvaluator())
        engine = SentinelEngine(
            _FakeAdapter(),
            RuleEngine({}),
            alert_log_path=None,
            console=False,
            llm_client=None,
            intent_tracker=tracker,
        )
        try:
            engine.process_event(_user_event("重构 auth 模块并修复登录 bug"))
            assert tracker.goal == "重构 auth 模块并修复登录 bug"
        finally:
            engine.close()

    def test_should_print_anchor_message(self, capsys) -> None:
        """锚定时要打印 ``[SENTINEL] 已自动锚定任务目标``。"""
        tracker = IntentTracker(window=5, threshold=2, evaluator=_SlowEvaluator())
        engine = SentinelEngine(
            _FakeAdapter(),
            RuleEngine({}),
            alert_log_path=None,
            console=True,
            llm_client=None,
            intent_tracker=tracker,
        )
        try:
            engine.process_event(_user_event("修复登录 bug"))
            captured = capsys.readouterr()
            assert "[SENTINEL] 已自动锚定任务目标" in captured.out
            assert "修复登录 bug" in captured.out
        finally:
            engine.close()

    def test_should_override_config_goal(self) -> None:
        """配置里的目标只是**默认值** —— 自动提取到的真实请求优先。"""
        tracker = IntentTracker(
            goal="配置里的旧目标",
            window=5,
            threshold=2,
            evaluator=_SlowEvaluator(),
        )
        engine = SentinelEngine(
            _FakeAdapter(),
            RuleEngine({}),
            alert_log_path=None,
            console=False,
            llm_client=None,
            intent_tracker=tracker,
        )
        try:
            engine.process_event(_user_event("流里的真实目标"))
            assert tracker.goal == "流里的真实目标"
        finally:
            engine.close()

    def test_should_not_override_explicit_goal(self) -> None:
        """命令行 / 看板设定的**显式**目标谁都不许覆盖。"""
        tracker = IntentTracker(
            goal="人明确给的目标",
            window=5,
            threshold=2,
            evaluator=_SlowEvaluator(),
        )
        tracker.set_goal("人明确给的目标", explicit=True)
        engine = SentinelEngine(
            _FakeAdapter(),
            RuleEngine({}),
            alert_log_path=None,
            console=False,
            llm_client=None,
            intent_tracker=tracker,
        )
        try:
            engine.process_event(_user_event("流里的真实目标"))
            assert tracker.goal == "人明确给的目标"
        finally:
            engine.close()

    def test_should_keep_config_goal_when_stream_has_no_user_message(self) -> None:
        """流里没有用户请求 → 退回配置里的目标。"""
        tracker = IntentTracker(
            goal="配置里的目标",
            window=5,
            threshold=2,
            evaluator=_SlowEvaluator(),
        )
        engine = SentinelEngine(
            _FakeAdapter(),
            RuleEngine({}),
            alert_log_path=None,
            console=False,
            llm_client=None,
            intent_tracker=tracker,
        )
        try:
            engine.process_event(_tool_event(1))
            assert tracker.goal == "配置里的目标"
        finally:
            engine.close()

    def test_should_ignore_empty_user_message(self) -> None:
        """空的用户请求不该锚定出一个空目标。"""
        tracker = IntentTracker(window=5, threshold=2, evaluator=_SlowEvaluator())
        engine = SentinelEngine(
            _FakeAdapter(),
            RuleEngine({}),
            alert_log_path=None,
            console=False,
            llm_client=None,
            intent_tracker=tracker,
        )
        try:
            engine.process_event(_user_event("   "))
            assert tracker.goal == ""
        finally:
            engine.close()

    def test_set_goal_should_clear_consecutive_counter(self) -> None:
        """换目标要清零连续漂移计数。"""
        evaluator = _SlowEvaluator(delay=0.0, drift=True)
        tracker = IntentTracker(
            goal="旧目标",
            window=5,
            threshold=2,
            evaluator=evaluator,
        )
        for index in range(5):
            tracker.observe(_tool_event(index))
        assert tracker.consecutive_drift == 1

        tracker.set_goal("新目标")

        assert tracker.consecutive_drift == 0
        tracker.stop()


# ---------------------------------------------------------------------------
# 三、引擎单步入口
# ---------------------------------------------------------------------------
class TestEngineProcessEvent:
    """``process_event`` —— 真实管道接管的入口。"""

    def test_should_count_verdicts(self) -> None:
        """单步入口也要维护计数。"""
        engine = SentinelEngine(
            _FakeAdapter(),
            RuleEngine({}),
            alert_log_path=None,
            console=False,
            llm_client=None,
        )
        try:
            engine.process_event(_tool_event(1))
            assert engine.counts["ON_TRACK"] == 1
            assert engine.processed_events == 1
        finally:
            engine.close()

    def test_should_emit_dangerous_alert(self) -> None:
        """危险命令要出告警。"""
        engine = SentinelEngine(
            _FakeAdapter(),
            RuleEngine({}),
            alert_log_path=None,
            console=False,
            llm_client=None,
        )
        try:
            event = {
                "source": "test",
                "type": "tool_call",
                "tool": {
                    "name": "Bash",
                    "arguments": {},
                    "command": "rm -rf /",
                },
                "text": "",
            }
            engine.process_event(event)
            assert engine.alerts
            assert engine.alerts[0]["verdict"] == "DANGEROUS"
        finally:
            engine.close()

    def test_window_summary_should_include_file_path(self) -> None:
        """摘要要带上文件路径 —— 判断漂移最关键的信号。"""
        summary = build_window_summary([
            {
                "type": "tool_call",
                "tool": {
                    "name": "Read",
                    "arguments": {"file_path": "src/auth/login.py"},
                    "command": "",
                },
            },
        ])
        assert "file_path=src/auth/login.py" in summary


class _FakeAdapter:
    """最小适配器替身。"""

    name = "fake"

    def read_stream(self, path: Any) -> Any:
        """不产出任何事件。"""
        return iter(())

    def tail_stream(self, path: Any, **kwargs: Any) -> Any:
        """不产出任何事件。"""
        return iter(())
