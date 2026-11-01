"""v0.9.0 —— L2 解释彻底异步化的测试。

核心契约：

1. **L1 命中后立刻返回** —— 主循环绝不等模型；
2. **守护者先动手** —— 挂起发生在解释之前；
3. **原始告警先发出** —— 日志里先出现 ``ALERT_JSON``（没有解释），
   解释回来后另起一行 ``ALERT_UPDATE_JSON``；
4. **追加更新能对上号** —— 更新事件带着原告警的 ``alert_id``；
5. **背压** —— 解释队列满了要丢弃，不能把主循环堵死。
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any, Dict, List, Optional

from src.core.engine import (
    ALERT_UPDATE_MARKER,
    ExplanationWorker,
    SentinelEngine,
)
from src.rules.basic import RuleEngine


class _SlowExplainer:
    """一个**很慢**的解释器。

    :param delay: 每次解释的阻塞秒数。
    :param text: 解释文本。
    """

    def __init__(self, delay: float = 0.5, text: str = "帮你把临时目录删掉") -> None:
        self.delay = float(delay)
        self.text = text
        self.calls: List[Dict[str, Any]] = []
        self.threads: List[str] = []
        self._lock = threading.Lock()
        self._release = threading.Event()
        self._release.set()

    def block(self) -> None:
        """让后续解释开始阻塞（模拟模型卡住）。"""
        self._release.clear()

    def release(self) -> None:
        """放行被阻塞的解释。"""
        self._release.set()

    def explain(self, payload: Dict[str, Any]) -> Any:
        """解释入口。"""
        with self._lock:
            self.calls.append(payload)
            self.threads.append(threading.current_thread().name)
        self._release.wait(timeout=10.0)
        time.sleep(self.delay)
        return _Explanation(self.text)


class _Explanation:
    """解释结果的最小替身。

    :param text: 解释文本。
    :param degraded: 是否降级。
    """

    def __init__(self, text: str = "", degraded: bool = False) -> None:
        self.text = text
        self.degraded = degraded
        self.reason = ""


class _RecordingGuardian:
    """记录干预时刻的假守护者。

    :param intervene_hook: 干预时调用的钩子（用来观察当时的解释状态）。
    """

    def __init__(self, intervene_hook: Optional[Any] = None) -> None:
        self.interventions: List[Dict[str, Any]] = []
        self.hook = intervene_hook

    def intervene(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """记录一次干预。"""
        self.interventions.append(payload)
        if self.hook is not None:
            self.hook()
        return {"ok": True, "message": "（测试）已挂起", "pid": 12345}

    def status(self) -> Dict[str, Any]:
        """状态。"""
        return {"ok": True, "enabled": True}

    def override(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """放行。"""
        return {"ok": True}


def _danger_event(command: str = "rm -rf /") -> Dict[str, Any]:
    """造一条会命中红线的事件。"""
    return {
        "source": "test",
        "type": "tool_call",
        "tool": {
            "name": "Bash",
            "arguments": {"command": command},
            "command": command,
        },
        "text": "",
    }


def _engine(tmp_path, explainer=None, guardian=None) -> SentinelEngine:
    """造一个引擎（告警写到临时目录）。"""
    return SentinelEngine(
        _FakeAdapter(),
        RuleEngine({}),
        alert_log_path=str(tmp_path / "alerts.log"),
        console=False,
        llm_client=explainer,
        guardian=guardian,
    )


class _FakeAdapter:
    """最小适配器替身。"""

    name = "fake"

    def read_stream(self, path: Any) -> Any:
        """不产出事件。"""
        return iter(())

    def tail_stream(self, path: Any, **kwargs: Any) -> Any:
        """不产出事件。"""
        return iter(())


# ---------------------------------------------------------------------------
# 一、主循环不被模型堵住
# ---------------------------------------------------------------------------
class TestAsyncExplanation:
    """L2 解释异步化的行为约定。"""

    def test_process_event_should_return_immediately(self, tmp_path) -> None:
        """模型卡住时，process_event 仍要**立刻**返回。"""
        explainer = _SlowExplainer(delay=0.0)
        explainer.block()
        engine = _engine(tmp_path, explainer)
        try:
            started = time.monotonic()
            engine.process_event(_danger_event())
            elapsed = time.monotonic() - started
            assert elapsed < 0.3, "主循环被 L2 解释堵住了"
        finally:
            explainer.release()
            engine.close()

    def test_explanation_should_run_in_background_thread(self, tmp_path) -> None:
        """解释跑在**后台线程**，不是主线程。"""
        explainer = _SlowExplainer(delay=0.0)
        engine = _engine(tmp_path, explainer)
        try:
            engine.process_event(_danger_event())
            assert engine.explanation_worker.drain(timeout=5.0)
            assert explainer.calls, "解释根本没被调用"
            assert threading.current_thread().name not in explainer.threads
        finally:
            engine.close()

    def test_guardian_should_act_before_explanation(self, tmp_path) -> None:
        """守护者必须在解释**之前**动手 —— 不能等模型把话说完。"""
        explainer = _SlowExplainer(delay=0.0)
        explainer.block()
        observed: Dict[str, Any] = {}

        def hook() -> None:
            # 干预发生时，解释还没返回（还在阻塞）。
            observed["explanation_returned"] = bool(explainer.calls) and not (
                explainer._release.is_set()
            )

        guardian = _RecordingGuardian(hook)
        engine = _engine(tmp_path, explainer, guardian)
        try:
            engine.process_event(_danger_event("rm -rf /"))
            assert guardian.interventions, "守护者没有被触发"
            assert observed["explanation_returned"] is False, (
                "解释先于干预返回了 —— 顺序反了"
            )
        finally:
            explainer.release()
            engine.close()

    def test_alert_should_be_emitted_without_explanation_first(
        self, tmp_path
    ) -> None:
        """原始告警先发出（不带解释），解释随后追加。"""
        explainer = _SlowExplainer(delay=0.0)
        explainer.block()
        engine = _engine(tmp_path, explainer)
        try:
            engine.process_event(_danger_event())
            content = (tmp_path / "alerts.log").read_text(encoding="utf-8")
            assert "ALERT_JSON" in content
            assert ALERT_UPDATE_MARKER not in content
        finally:
            explainer.release()
            engine.close()

    def test_update_event_should_carry_alert_id(self, tmp_path) -> None:
        """追加更新事件要带着原告警的 ID —— 靠它对上号。"""
        explainer = _SlowExplainer(delay=0.0)
        engine = _engine(tmp_path, explainer)
        try:
            engine.process_event(_danger_event())
            assert engine.explanation_worker.drain(timeout=5.0)
            content = (tmp_path / "alerts.log").read_text(encoding="utf-8")
            assert ALERT_UPDATE_MARKER in content

            alert_id = engine.alerts[0]["alert_id"]
            update_line = next(
                line
                for line in content.splitlines()
                if ALERT_UPDATE_MARKER in line
            )
            payload = json.loads(
                update_line.split(ALERT_UPDATE_MARKER, 1)[1].strip()
            )
            assert payload["alert_id"] == alert_id
            assert payload["update"] is True
            assert payload["explanation"] == "帮你把临时目录删掉"
            assert payload["explanation_pending"] is False
        finally:
            engine.close()

    def test_explanation_should_be_written_back_to_alert(self, tmp_path) -> None:
        """解释回来后要回填到原告警上。"""
        explainer = _SlowExplainer(delay=0.0)
        engine = _engine(tmp_path, explainer)
        try:
            engine.process_event(_danger_event())
            assert engine.explanation_worker.drain(timeout=5.0)
            assert engine.alerts[0]["explanation"] == "帮你把临时目录删掉"
            assert engine.alerts[0]["explanation_pending"] is True
        finally:
            engine.close()

    def test_alert_should_mark_explanation_pending(self, tmp_path) -> None:
        """原始告警要标上「解释在路」，前端才知道显示什么。"""
        explainer = _SlowExplainer(delay=0.0)
        explainer.block()
        engine = _engine(tmp_path, explainer)
        try:
            engine.process_event(_danger_event())
            assert engine.alerts[0]["explanation_pending"] is True
            assert engine.alerts[0].get("explanation", "") == ""
        finally:
            explainer.release()
            engine.close()

    def test_no_llm_should_mark_not_pending(self, tmp_path) -> None:
        """压根没有模型层时，不标「在路」—— 否则永远转圈。"""
        engine = _engine(tmp_path, None)
        try:
            engine.process_event(_danger_event())
            assert engine.alerts[0]["explanation_pending"] is False
            assert engine.alerts[0]["explanation"] == ""
        finally:
            engine.close()

    def test_drain_should_wait_for_explanations(self, tmp_path) -> None:
        """收尾时 drain() 要等解释回来 —— 否则解释会丢。"""
        explainer = _SlowExplainer(delay=0.05)
        engine = _engine(tmp_path, explainer)
        try:
            engine.process_event(_danger_event())
            assert engine.explanation_worker.drain(timeout=5.0)
            assert engine.alerts[0]["explanation"] == "帮你把临时目录删掉"
        finally:
            engine.close()

    def test_explanation_failure_should_not_break_engine(self, tmp_path) -> None:
        """解释抛异常也不能影响主流程。"""
        explainer = _SlowExplainer(delay=0.0)

        def boom(payload):
            raise RuntimeError("模型炸了")

        explainer.explain = boom  # type: ignore[assignment]
        engine = _engine(tmp_path, explainer)
        try:
            engine.process_event(_danger_event())
            assert engine.explanation_worker.drain(timeout=5.0)
            assert engine.alerts[0]["explanation_degraded"] is True
        finally:
            engine.close()


# ---------------------------------------------------------------------------
# 二、解释队列的背压
# ---------------------------------------------------------------------------
class TestExplanationBackpressure:
    """解释队列满了要丢弃，不能把主循环堵死。"""

    def test_should_drop_when_queue_is_full(self) -> None:
        """模型太慢 → 队列塞满 → 丢弃解释请求。"""
        explainer = _SlowExplainer(delay=0.0)
        explainer.block()
        received: List[str] = []
        worker = ExplanationWorker(
            explainer=explainer.explain,
            on_explanation=lambda alert_id, fields: received.append(alert_id),
            max_pending=1,
        )
        try:
            for index in range(10):
                worker.submit(f"a{index}", {"n": index})
            assert worker.dropped > 0, "队列满了却没有丢弃任何请求"
        finally:
            explainer.release()
            worker.stop()

    def test_should_log_overload_warning(self, caplog) -> None:
        """超载要留下痕迹。"""
        explainer = _SlowExplainer(delay=0.0)
        explainer.block()
        worker = ExplanationWorker(
            explainer=explainer.explain,
            on_explanation=lambda alert_id, fields: None,
            max_pending=1,
        )
        try:
            with caplog.at_level("WARNING"):
                for index in range(10):
                    worker.submit(f"a{index}", {"n": index})
            assert any(
                "解释队列" in record.message for record in caplog.records
            )
        finally:
            explainer.release()
            worker.stop()

    def test_submit_should_not_block_caller(self) -> None:
        """投递**绝不能**阻塞调用方。"""
        explainer = _SlowExplainer(delay=0.0)
        explainer.block()
        worker = ExplanationWorker(
            explainer=explainer.explain,
            on_explanation=lambda alert_id, fields: None,
            max_pending=1,
        )
        try:
            started = time.monotonic()
            for index in range(5):
                worker.submit(f"a{index}", {"n": index})
            elapsed = time.monotonic() - started
            assert elapsed < 0.3, "投递被阻塞了"
        finally:
            explainer.release()
            worker.stop()
