"""L2 同步调度器测试（v4.0.0）—— 同步语义、插队、背压、超时、并发。

被测对象是 :mod:`src.core.l2_scheduler` —— 它把
:class:`~src.core.priority_queue.PriorityTaskQueue` 的**异步**接口
包成「投递 + 等结论」的**同步**接口，让漂移判定、告警解释这类
**同步阻塞**的模型调用也能享受优先级排队。

核心验收
========

``Test插队与顺序::test_p0插队先于已排队的p2执行`` ——
P0 能插到已经排队的 P2 前面。

⚠️ 这条测试**不靠 sleep 猜时间**：用一个占位任务占住单并发引擎，
再用 ``pending()`` 这个**可观测条件**确认两个任务都已入队，
最后才放行引擎 —— 顺序是**结构保证**的，不是撞运气。
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Dict, List, Optional

import pytest

from src.core.l2_scheduler import (
    PRIORITY_P0,
    PRIORITY_P1,
    PRIORITY_P2,
    L2Preempted,
    L2Rejected,
    L2Scheduler,
)
from src.core.priority_queue import QueueOutcome

#: 档位标签（断言消息里用）。
LABEL_P0 = "P0_RED_LINE"
LABEL_P1 = "P1_DRIFT"
LABEL_P2 = "P2_ROUTINE"


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _wait_until(
    predicate: Callable[[], bool], timeout: float = 5.0
) -> bool:
    """等一个条件成立（轮询）。

    :param predicate: 判定函数。
    :param timeout: 最长等多少秒。
    :return: 条件成立过返回 ``True``。
    """
    deadline = time.monotonic() + max(float(timeout), 0.0)
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return False


class _Recorder:
    """记录每一次结论的收集器（线程安全）。"""

    def __init__(self) -> None:
        self.outcomes: List[QueueOutcome] = []
        self._lock = threading.Lock()

    def __call__(self, task_id: str, outcome: QueueOutcome) -> None:
        """结果回调。"""
        with self._lock:
            self.outcomes.append(outcome)

    def get(self, task_id: str) -> Optional[QueueOutcome]:
        """按 ID 找一次结论。"""
        with self._lock:
            for outcome in self.outcomes:
                if outcome.task_id == task_id:
                    return outcome
        return None

    def count(self) -> int:
        """一共收到几次结论。"""
        with self._lock:
            return len(self.outcomes)


def _assert_jumps_ahead(
    high: Any,
    low: Any,
    high_name: str,
    low_name: str,
) -> None:
    """断言「高档位插到低档位前面」。

    做法是**结构保证**的：先用占位任务占住单并发引擎，
    再按顺序把低档位、高档位投进队列（每次都等
    ``pending()`` 变多，确认**真的入队了**），最后放行引擎。

    :param high: 高档位。
    :param low: 低档位。
    :param high_name: 高档位任务的名字。
    :param low_name: 低档位任务的名字。
    """
    scheduler = L2Scheduler(max_pending=10, preempt_after_seconds=30.0)
    gate = threading.Event()
    started = threading.Event()
    order: List[str] = []
    errors: List[BaseException] = []

    def blocker() -> str:
        started.set()
        gate.wait(30.0)
        return "blocker"

    def make_task(name: str) -> Callable[[], str]:
        def task() -> str:
            order.append(name)
            return name

        return task

    def run(
        task_id: str, fn: Callable[[], Any], priority: Any
    ) -> None:
        try:
            scheduler.call(task_id, fn, priority=priority, timeout=20.0)
        except BaseException as exc:  # noqa: BLE001 —— 测试要看见它
            errors.append(exc)

    blocker_thread = threading.Thread(
        target=run, args=("blocker", blocker, PRIORITY_P2), daemon=True
    )
    blocker_thread.start()
    assert started.wait(3.0), "占位任务没有开跑"

    low_thread = threading.Thread(
        target=run, args=("low", make_task(low_name), low), daemon=True
    )
    low_thread.start()
    assert _wait_until(lambda: scheduler.pending() >= 1), (
        "低档位任务没有进队列"
    )

    high_thread = threading.Thread(
        target=run, args=("high", make_task(high_name), high), daemon=True
    )
    high_thread.start()
    assert _wait_until(lambda: scheduler.pending() >= 2), (
        "高档位任务没有进队列"
    )

    gate.set()
    for thread in (blocker_thread, low_thread, high_thread):
        thread.join(10.0)

    assert not errors, "不该有异常：{}".format(errors)
    assert order == [high_name, low_name], (
        "插队失败，执行顺序是 {}".format(order)
    )
    scheduler.stop()


# ---------------------------------------------------------------------------
# 同步语义
# ---------------------------------------------------------------------------
class Test同步语义:
    """「投递 + 等结论」的同步语义对不对。"""

    def test_正常执行返回函数结果(self) -> None:
        """正常跑完时，必须原样返回 ``fn()`` 的返回值。"""
        scheduler = L2Scheduler()
        try:
            result = scheduler.call("t-1", lambda: {"verdict": "ON_TRACK"})
            assert result == {"verdict": "ON_TRACK"}, (
                "返回值必须原样带回：{}".format(result)
            )
        finally:
            scheduler.stop()

    def test_函数抛异常原样透传(self) -> None:
        """``fn`` 抛的异常必须**原样**重抛（同一个对象，不许包一层）。"""
        scheduler = L2Scheduler()
        boom = ValueError("模型返回坏格式")

        def broken() -> str:
            raise boom

        try:
            with pytest.raises(ValueError) as excinfo:
                scheduler.call("t-2", broken)
            assert excinfo.value is boom, (
                "必须原样重抛同一个异常对象，实际是 {}".format(
                    type(excinfo.value).__name__
                )
            )
            assert scheduler.stats()["scheduler"]["failed"] == 1, (
                "失败必须记账"
            )
        finally:
            scheduler.stop()

    def test_超时抛超时错误(self) -> None:
        """超时必须抛 ``TimeoutError``，消息里带任务 ID 与档位。"""
        scheduler = L2Scheduler()

        def slow() -> str:
            time.sleep(1.0)
            return "done"

        try:
            with pytest.raises(TimeoutError) as excinfo:
                scheduler.call("t-3", slow, timeout=0.2)
            message = str(excinfo.value)
            assert "t-3" in message, "超时消息必须带任务 ID：{}".format(message)
            assert LABEL_P2 in message, (
                "超时消息必须带档位：{}".format(message)
            )
            assert scheduler.stats()["scheduler"]["timed_out"] == 1, (
                "超时必须记账"
            )
        finally:
            scheduler.stop()

    def test_超时后任务仍在后台跑完(self) -> None:
        """已知限制：超时只是「调用方不等了」，任务**仍在后台跑完**。"""
        scheduler = L2Scheduler()
        finished = threading.Event()

        def slow() -> str:
            time.sleep(0.5)
            finished.set()
            return "done"

        try:
            with pytest.raises(TimeoutError):
                scheduler.call("t-4", slow, timeout=0.1)
            assert finished.wait(5.0), (
                "超时之后任务应当仍在后台跑完（已知限制）"
            )
        finally:
            scheduler.stop()

    def test_任务id原样回传(self) -> None:
        """结论必须带着**原样的** ``task_id`` 回到调用方。"""
        recorder = _Recorder()
        scheduler = L2Scheduler(on_result=recorder)
        try:
            scheduler.call("drift-42", lambda: "ok")
            outcome = recorder.get("drift-42")
            assert outcome is not None, "回调里没有收到结论"
            assert outcome.task_id == "drift-42", (
                "任务 ID 必须原样回传：{}".format(outcome.task_id)
            )
            assert outcome.status == "completed", (
                "状态不对：{}".format(outcome.status)
            )
            assert recorder.count() == 1, "每条任务只该回调一次"
        finally:
            scheduler.stop()

    def test_重复任务id被拒收(self) -> None:
        """同一个 ``task_id`` 在途时再次投递 —— 必须明确拒收。"""
        scheduler = L2Scheduler()
        started = threading.Event()
        gate = threading.Event()
        errors: List[BaseException] = []

        def blocking() -> str:
            started.set()
            gate.wait(30.0)
            return "ok"

        def run() -> None:
            try:
                scheduler.call("dup", blocking, timeout=30.0)
            except BaseException as exc:  # noqa: BLE001 —— 测试要看见它
                errors.append(exc)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        assert started.wait(3.0), "第一个任务没有开跑"

        try:
            with pytest.raises(L2Rejected) as excinfo:
                scheduler.call("dup", lambda: "never")
            assert "dup" in str(excinfo.value), (
                "拒收消息必须带任务 ID：{}".format(excinfo.value)
            )
        finally:
            gate.set()
            thread.join(10.0)
            scheduler.stop()


# ---------------------------------------------------------------------------
# 插队与顺序
# ---------------------------------------------------------------------------
class Test插队与顺序:
    """红线插队、同档 FIFO。"""

    def test_p0插队先于已排队的p2执行(self) -> None:
        """核心验收：P0 能插到**已经排队的** P2 前面。"""
        _assert_jumps_ahead(PRIORITY_P0, PRIORITY_P2, "p0", "p2")

    def test_p1同样插队到p2前面(self) -> None:
        """P1（漂移判定）同样能插到 P2 前面。"""
        _assert_jumps_ahead(PRIORITY_P1, PRIORITY_P2, "p1", "p2")

    def test_同档位保持先进先出(self) -> None:
        """同档位内部仍然是先来先服务 —— 不能把日常任务也搞乱。"""
        scheduler = L2Scheduler()
        order: List[int] = []

        def make_task(index: int) -> Callable[[], int]:
            def task() -> int:
                order.append(index)
                return index

            return task

        try:
            for index in range(6):
                scheduler.call("p2-{}".format(index), make_task(index))
            assert order == [0, 1, 2, 3, 4, 5], (
                "同档位必须 FIFO，实际顺序 {}".format(order)
            )
        finally:
            scheduler.stop()


# ---------------------------------------------------------------------------
# 背压与拒收
# ---------------------------------------------------------------------------
class Test背压与拒收:
    """队列满了怎么办。"""

    def test_背压丢弃抛拒收异常(self) -> None:
        """队列满了，P2 被背压丢弃 —— 调用方拿到 ``L2Rejected``。"""
        scheduler = L2Scheduler(max_pending=1)
        started = threading.Event()
        gate = threading.Event()
        errors: List[BaseException] = []

        def blocker() -> str:
            started.set()
            gate.wait(30.0)
            return "blocker"

        def first_task() -> str:
            return "first"

        def run(task_id: str, fn: Callable[[], Any]) -> None:
            try:
                scheduler.call(task_id, fn, priority=PRIORITY_P2, timeout=30.0)
            except BaseException as exc:  # noqa: BLE001 —— 测试要看见它
                errors.append(exc)

        blocker_thread = threading.Thread(
            target=run, args=("blocker", blocker), daemon=True
        )
        blocker_thread.start()
        assert started.wait(3.0), "占位任务没有开跑"

        first_thread = threading.Thread(
            target=run, args=("p2-a", first_task), daemon=True
        )
        first_thread.start()
        assert _wait_until(lambda: scheduler.pending() >= 1), (
            "第一条 P2 没有进队列"
        )

        try:
            # 队列已满（pending == max_pending == 1）—— 这条必须被丢。
            with pytest.raises(L2Rejected) as excinfo:
                scheduler.call("p2-b", lambda: "never", priority=PRIORITY_P2)
            message = str(excinfo.value)
            assert "p2-b" in message, (
                "拒收消息必须带任务 ID：{}".format(message)
            )
            assert LABEL_P2 in message, (
                "拒收消息必须带档位：{}".format(message)
            )
            assert scheduler.stats()["scheduler"]["dropped"] == 1, (
                "丢弃必须记账"
            )
        finally:
            gate.set()
            blocker_thread.join(10.0)
            first_thread.join(10.0)
            scheduler.stop()

        assert not errors, "不该有异常：{}".format(errors)

    def test_红线永不被背压丢弃(self) -> None:
        """队列满了，P0 照样收下 —— 宁可排队也不能弄丢红线。"""
        scheduler = L2Scheduler(max_pending=1)
        started = threading.Event()
        gate = threading.Event()
        results: List[Any] = []
        errors: List[BaseException] = []

        def blocker() -> str:
            started.set()
            gate.wait(30.0)
            return "blocker"

        def first_task() -> str:
            return "first"

        def run(
            task_id: str, fn: Callable[[], Any], priority: Any
        ) -> None:
            try:
                scheduler.call(task_id, fn, priority=priority, timeout=30.0)
            except BaseException as exc:  # noqa: BLE001 —— 测试要看见它
                errors.append(exc)

        def run_p0() -> None:
            try:
                results.append(
                    scheduler.call(
                        "p0", lambda: "p0-ok", priority=PRIORITY_P0,
                        timeout=30.0,
                    )
                )
            except BaseException as exc:  # noqa: BLE001 —— 测试要看见它
                errors.append(exc)

        blocker_thread = threading.Thread(
            target=run, args=("blocker", blocker, PRIORITY_P2), daemon=True
        )
        blocker_thread.start()
        assert started.wait(3.0), "占位任务没有开跑"

        first_thread = threading.Thread(
            target=run, args=("p2-a", first_task, PRIORITY_P2), daemon=True
        )
        first_thread.start()
        assert _wait_until(lambda: scheduler.pending() >= 1), (
            "第一条 P2 没有进队列"
        )

        p0_thread = threading.Thread(target=run_p0, daemon=True)
        p0_thread.start()
        assert _wait_until(lambda: scheduler.pending() >= 2), (
            "P0 没有进队列（它不该被背压丢掉）"
        )

        gate.set()
        blocker_thread.join(10.0)
        first_thread.join(10.0)
        p0_thread.join(10.0)
        try:
            assert not errors, "红线不该被拒收：{}".format(errors)
            assert results == ["p0-ok"], (
                "红线必须跑完并带回结果：{}".format(results)
            )
            assert scheduler.stats()["scheduler"]["dropped"] == 0, (
                "红线不该计入丢弃"
            )
        finally:
            scheduler.stop()


# ---------------------------------------------------------------------------
# 并发
# ---------------------------------------------------------------------------
class Test并发:
    """多个调用线程同时用。"""

    def test_多线程并发各自拿到正确结果(self) -> None:
        """8 个线程同时 ``call`` —— 各自拿到**自己**的结果。"""
        scheduler = L2Scheduler(max_pending=20)
        results: Dict[str, Any] = {}
        lock = threading.Lock()

        def run(index: int) -> None:
            value = scheduler.call(
                "t-{}".format(index),
                lambda i=index: i * 10,
                timeout=30.0,
            )
            with lock:
                results["t-{}".format(index)] = value

        threads = [
            threading.Thread(target=run, args=(index,), daemon=True)
            for index in range(8)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(20.0)

        try:
            expected = {
                "t-{}".format(index): index * 10 for index in range(8)
            }
            assert results == expected, (
                "并发结果串台了：{}".format(results)
            )
            assert scheduler.stats()["scheduler"]["completed"] == 8, (
                "8 条任务都该完成"
            )
        finally:
            scheduler.stop()


# ---------------------------------------------------------------------------
# 统计与生命周期
# ---------------------------------------------------------------------------
class Test统计与生命周期:
    """统计字段、drain/stop、停机后的行为。"""

    def test_统计字段齐全(self) -> None:
        """``stats()`` 必须把队列统计和调度器自己的计数都交出来。"""
        scheduler = L2Scheduler()
        try:
            scheduler.call("s-1", lambda: 1)
            scheduler.call("s-2", lambda: 2, priority=PRIORITY_P0)
            stats = scheduler.stats()

            assert stats["name"] == "sentinel-l2", "名字不对"
            assert stats["stopped"] is False, "还没停机"
            assert stats["inflight"] == 0, "没有在途任务"

            mine = stats["scheduler"]
            assert mine["submitted"] == 2, "投递数不对"
            assert mine["completed"] == 2, "完成数不对"
            assert mine["failed"] == 0, "不该有失败"
            assert mine["timed_out"] == 0, "不该有超时"
            assert mine["dropped"] == 0, "不该有丢弃"
            assert mine["preempted"] == 0, "不该有抢占"
            assert mine["cancelled"] == 0, "不该有取消"

            labels = set(mine["wait_ms"])
            assert labels == {LABEL_P0, LABEL_P1, LABEL_P2}, (
                "各档位等待延迟必须齐全：{}".format(labels)
            )
            assert mine["wait_ms"][LABEL_P0]["count"] == 1, (
                "P0 的等待样本数不对"
            )
            assert mine["latency_ms"][LABEL_P2]["count"] == 1, (
                "P2 的端到端样本数不对"
            )

            # 队列自己的统计也在（顶层扁平字段）。
            assert stats["completed"] == 2, "队列统计也要交出来"
        finally:
            scheduler.stop()

    def test_drain与stop可用(self) -> None:
        """``drain()`` 等排空；``stop()`` 之后队列彻底空闲。"""
        scheduler = L2Scheduler()
        scheduler.call("d-1", lambda: "x")
        assert scheduler.drain(timeout=5.0) is True, "队列没排空"
        assert scheduler.pending() == 0, "不该还有待处理任务"
        scheduler.stop()
        assert scheduler.idle() is True, "停机后队列应当空闲"

    def test_停止后调用被拒收(self) -> None:
        """``stop()`` 之后再 ``call`` —— 明确抛 ``L2Rejected``。"""
        scheduler = L2Scheduler()
        scheduler.stop()
        with pytest.raises(L2Rejected) as excinfo:
            scheduler.call("after-stop", lambda: "never")
        message = str(excinfo.value)
        assert "after-stop" in message, (
            "拒收消息必须带任务 ID：{}".format(message)
        )
        assert "已停止" in message, (
            "拒收消息必须说明原因：{}".format(message)
        )

    def test_停止时在途任务以拒收收场(self) -> None:
        """停机时还没出结论的调用方，不该傻等到超时。"""
        scheduler = L2Scheduler()
        started = threading.Event()
        gate = threading.Event()
        errors: List[BaseException] = []

        def blocking() -> str:
            started.set()
            gate.wait(30.0)
            return "ok"

        def run() -> None:
            try:
                scheduler.call("in-flight", blocking, timeout=60.0)
            except BaseException as exc:  # noqa: BLE001 —— 测试要看见它
                errors.append(exc)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        assert started.wait(3.0), "任务没有开跑"

        scheduler.stop(timeout=5.0)
        thread.join(5.0)
        gate.set()

        assert len(errors) == 1, (
            "在途任务应当只收一次场：{}".format(errors)
        )
        assert isinstance(errors[0], L2Rejected), (
            "在途任务应当以 L2Rejected 收场：{}".format(errors[0])
        )


# ---------------------------------------------------------------------------
# 异常隔离
# ---------------------------------------------------------------------------
class Test异常隔离:
    """回调炸了不许拖垮调度器；抢占必须如实上报。"""

    def test_观测回调抛异常不影响调度器(self) -> None:
        """``on_result`` 回调抛异常 —— 调度器照样能用。"""
        calls: List[str] = []

        def broken(task_id: str, outcome: QueueOutcome) -> None:
            calls.append(task_id)
            raise RuntimeError("回调炸了")

        scheduler = L2Scheduler(on_result=broken)
        try:
            assert scheduler.call("o-1", lambda: "ok") == "ok", (
                "回调炸了不该影响返回值"
            )
            assert scheduler.call("o-2", lambda: "ok2") == "ok2", (
                "回调炸了不该影响下一次调用"
            )
            assert calls == ["o-1", "o-2"], "回调还是该被调用"
            assert scheduler.stats()["scheduler"]["completed"] == 2, (
                "两条任务都该记完成"
            )
        finally:
            scheduler.stop()

    def test_抢占抛抢占异常(self) -> None:
        """跑太久的任务被强制抢占 —— 调用方拿到 ``L2Preempted``。"""
        scheduler = L2Scheduler(max_pending=10, preempt_after_seconds=0.2)
        started = threading.Event()
        errors: List[BaseException] = []
        results: List[Any] = []

        def slow() -> str:
            started.set()
            # ⚠️ 故意不看取消令牌 —— 逼出「强制放弃」那条路。
            time.sleep(3.0)
            return "slow"

        def fast() -> str:
            results.append("fast")
            return "fast"

        def run_slow() -> None:
            try:
                scheduler.call(
                    "slow-p2", slow, priority=PRIORITY_P2, timeout=30.0
                )
            except BaseException as exc:  # noqa: BLE001 —— 测试要看见它
                errors.append(exc)

        def run_fast() -> None:
            try:
                results.append(
                    scheduler.call(
                        "fast-p0", fast, priority=PRIORITY_P0, timeout=30.0
                    )
                )
            except BaseException as exc:  # noqa: BLE001 —— 测试要看见它
                errors.append(exc)

        slow_thread = threading.Thread(target=run_slow, daemon=True)
        slow_thread.start()
        assert started.wait(3.0), "慢任务没有开跑"

        # 等它**越过**抢占阈值（等的是条件，不是猜一个 sleep 时长）。
        begin = time.monotonic()
        assert _wait_until(
            lambda: time.monotonic() - begin > 0.35
        ), "等待越过抢占阈值超时"

        fast_thread = threading.Thread(target=run_fast, daemon=True)
        fast_thread.start()

        slow_thread.join(20.0)
        fast_thread.join(20.0)
        try:
            assert len(errors) == 1, (
                "慢任务应当只报一次错：{}".format(errors)
            )
            assert isinstance(errors[0], L2Preempted), (
                "慢任务应当以 L2Preempted 收场：{}".format(errors[0])
            )
            assert "slow-p2" in str(errors[0]), (
                "抢占消息必须带任务 ID：{}".format(errors[0])
            )
            assert results == ["fast", "fast"], (
                "P0 必须抢到前面跑完：{}".format(results)
            )
            assert scheduler.stats()["scheduler"]["preempted"] == 1, (
                "抢占必须记账"
            )
        finally:
            scheduler.stop()
