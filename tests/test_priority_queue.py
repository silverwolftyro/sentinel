"""优先级队列测试（v3.8.1）—— 插队、抢占、背压、资源释放、无死锁。

验收主测试
==========

``test_three_agents_spam_never_blocks_p0``：
3 个 Agent 同时狂发 100 个 P2，中间穿插 5 个 P0，断言 ——

* **无死锁**：所有任务都在超时前出结论；
* **P0 等得极短**：平均等待延迟远小于「排完全部 P2 队列」的量级，
  且**远小于**同一份负载在普通 FIFO 队列下的等待延迟（对照实验）；
* **不浪费请求**：被背压丢弃的 P2 每一笔都有降级结果，**绝不静默消失**。

被测对象是 :mod:`src.core.priority_queue` —— 一个**零依赖叶子模块**，
它**不碰业务路由**（本轮刻意不接线到
:class:`~src.core.engine.SentinelEngine`）。
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from src.core.priority_queue import (
    PRIORITY_P0,
    PRIORITY_P1,
    PRIORITY_P2,
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_DROPPED,
    STATUS_FAILED,
    STATUS_PREEMPTED,
    CancelToken,
    Priority,
    PriorityTaskQueue,
    QueueOutcome,
    TaskCancelled,
    classify_priority,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _wait_until(
    predicate: Callable[[], bool], timeout: float = 3.0
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
    """记录每一次任务结局的收集器（线程安全）。"""

    def __init__(self) -> None:
        self.outcomes: List[QueueOutcome] = []
        self.order: List[str] = []
        self._lock = threading.Lock()

    def __call__(self, task_id: str, outcome: QueueOutcome) -> None:
        """结果回调。"""
        with self._lock:
            self.outcomes.append(outcome)
            self.order.append(task_id)

    def get(self, task_id: str) -> Optional[QueueOutcome]:
        """按 ID 找一次结局。"""
        with self._lock:
            for outcome in self.outcomes:
                if outcome.task_id == task_id:
                    return outcome
        return None

    def of(self, priority: Priority) -> List[QueueOutcome]:
        """取某个档位的全部结局。"""
        with self._lock:
            return [item for item in self.outcomes if item.priority == priority]


# ---------------------------------------------------------------------------
# 档位判定
# ---------------------------------------------------------------------------
def test_red_line_verdict_is_p0():
    """红线级判定必须落在 P0。"""
    assert (
        classify_priority({"verdict": "DANGEROUS", "severity": "red_line"})
        is PRIORITY_P0
    )


def test_drift_streak_is_p1():
    """连续漂移达到阈值落在 P1。"""
    assert (
        classify_priority(
            {"drift": True}, drift_streak=2, drift_threshold=2
        )
        is PRIORITY_P1
    )


def test_on_track_is_p2():
    """日常的 ON_TRACK 落在 P2。"""
    assert (
        classify_priority({"verdict": "ON_TRACK", "severity": "none"})
        is PRIORITY_P2
    )


def test_non_red_line_danger_is_p1():
    """非红线的危险判定是 P1 —— 比红线低一档，但仍是「有问题」。"""
    assert (
        classify_priority({"verdict": "DANGEROUS", "severity": "standard"})
        is PRIORITY_P1
    )


def test_priority_literals_match_rules_layer():
    """本模块里的字面量必须与规则层一致 —— 别悄悄漂移。"""
    from src.rules.basic import SEVERITY_RED_LINE, VERDICT_DANGEROUS

    assert (
        classify_priority(
            {"verdict": VERDICT_DANGEROUS, "severity": SEVERITY_RED_LINE}
        )
        is PRIORITY_P0
    )


def test_priority_coercion_accepts_common_spellings():
    """档位能认数字、字符串与枚举成员。"""
    from src.core.priority_queue import _coerce_priority

    assert _coerce_priority(0) is PRIORITY_P0
    assert _coerce_priority("P0") is PRIORITY_P0
    assert _coerce_priority("red_line") is PRIORITY_P0
    assert _coerce_priority("drift") is PRIORITY_P1
    assert _coerce_priority(Priority.P2_ROUTINE) is PRIORITY_P2
    # 认不出来就落到最保守的那一档。
    assert _coerce_priority("不存在的档位") is PRIORITY_P2


# ---------------------------------------------------------------------------
# 插队
# ---------------------------------------------------------------------------
def test_p0_jumps_ahead_of_waiting_p2():
    """P2 在排队时来的 P0，必须被调度到最前。"""
    gate = threading.Event()
    order: List[str] = []
    recorder = _Recorder()

    def handler(payload: Dict[str, Any]) -> str:
        order.append(payload["name"])
        if payload["name"] == "blocker":
            gate.wait(3.0)
        return payload["name"]

    queue_ = PriorityTaskQueue(handler=handler, on_result=recorder)
    queue_.submit("blocker", {"name": "blocker"}, PRIORITY_P2)
    assert _wait_until(lambda: queue_.running), "blocker 没有开跑"

    # 三条 P2 排在后面，然后来一条 P0。
    queue_.submit("p2-a", {"name": "p2-a"}, PRIORITY_P2)
    queue_.submit("p2-b", {"name": "p2-b"}, PRIORITY_P2)
    queue_.submit("p0", {"name": "p0"}, PRIORITY_P0)
    assert queue_.pending == 3

    gate.set()
    assert queue_.drain(timeout=5.0), "队列没排空"

    # ⚠️ P0 虽然**最后**到，却**第一个**跑（blocker 之后立刻就是它）。
    assert order == ["blocker", "p0", "p2-a", "p2-b"]


def test_p0_wait_is_shorter_than_p2_wait():
    """插队的直接效果：P0 的等待延迟明显小于 P2。"""
    gate = threading.Event()
    recorder = _Recorder()

    def handler(payload: Dict[str, Any]) -> str:
        if payload["name"] == "blocker":
            gate.wait(1.0)
        else:
            # ⚠️ 给每次调用一个**可测量**的耗时 ——
            #    否则毫秒取整会把差异抹平，测不出插队的效果。
            time.sleep(0.01)
        return payload["name"]

    queue_ = PriorityTaskQueue(handler=handler, on_result=recorder)
    queue_.submit("blocker", {"name": "blocker"}, PRIORITY_P2)
    assert _wait_until(lambda: queue_.running)

    for index in range(5):
        queue_.submit("p2-{}".format(index), {"name": "p2"}, PRIORITY_P2)
    # 让 P2 先在队列里**待一会儿**，P0 随后才到。
    time.sleep(0.05)
    queue_.submit("p0", {"name": "p0"}, PRIORITY_P0)
    gate.set()
    assert queue_.drain(timeout=5.0)

    p0 = recorder.get("p0")
    p2 = recorder.get("p2-0")
    assert p0 is not None and p2 is not None
    assert p0.status == STATUS_COMPLETED
    assert p0.wait_ms < p2.wait_ms, (
        "P0 等待 {}ms，不该比 P2 的 {}ms 还长".format(p0.wait_ms, p2.wait_ms)
    )


def test_same_priority_keeps_fifo_order():
    """同档位内部仍然是先来先服务 —— 不能把 P2 也搞乱。"""
    recorder = _Recorder()
    order: List[str] = []

    def handler(payload: Dict[str, Any]) -> str:
        order.append(payload["name"])
        return payload["name"]

    queue_ = PriorityTaskQueue(handler=handler, on_result=recorder)
    for index in range(6):
        queue_.submit("p2-{}".format(index), {"name": str(index)}, PRIORITY_P2)
    assert queue_.drain(timeout=5.0)

    assert order == ["0", "1", "2", "3", "4", "5"]


# ---------------------------------------------------------------------------
# 抢占
# ---------------------------------------------------------------------------
def test_long_running_task_is_preempted_for_p0():
    """跑了太久的任务会被强制抢占，把位置腾给 P0。"""
    started = threading.Event()
    finished = threading.Event()
    recorder = _Recorder()

    def handler(payload: Dict[str, Any]) -> str:
        if payload["name"] == "slow":
            started.set()
            # ⚠️ 故意**不看**取消令牌 —— 逼出「强制放弃」那条路。
            time.sleep(3.0)
            finished.set()
        return payload["name"]

    queue_ = PriorityTaskQueue(
        handler=handler,
        on_result=recorder,
        preempt_after_seconds=0.2,
        cancel_grace_seconds=0.05,
    )
    queue_.submit("slow", {"name": "slow"}, PRIORITY_P2)
    assert started.wait(2.0), "slow 没有开跑"

    # 让它越过阈值，然后来一条 P0。
    time.sleep(0.3)
    queue_.submit("p0", {"name": "p0"}, PRIORITY_P0)

    assert queue_.drain(timeout=5.0), "队列没排空"

    p0 = recorder.get("p0")
    slow = recorder.get("slow")
    assert p0 is not None and p0.status == STATUS_COMPLETED
    assert slow is not None
    assert slow.status == STATUS_PREEMPTED
    assert slow.degraded, "被抢占必须如实标记为降级"
    # 那个不配合的线程还在后台跑着 —— 它的结果**绝不算数**。
    assert not finished.is_set()


def test_short_task_is_not_preempted():
    """跑得不久的任务**不抢** —— 抢了反而更亏。"""
    recorder = _Recorder()
    started = threading.Event()

    def handler(payload: Dict[str, Any]) -> str:
        started.set()
        time.sleep(0.3)
        return payload["name"]

    queue_ = PriorityTaskQueue(
        handler=handler,
        on_result=recorder,
        preempt_after_seconds=5.0,
        cancel_grace_seconds=0.05,
    )
    queue_.submit("quick", {"name": "quick"}, PRIORITY_P2)
    assert started.wait(2.0)
    queue_.submit("p0", {"name": "p0"}, PRIORITY_P0)
    assert queue_.drain(timeout=5.0)

    quick = recorder.get("quick")
    assert quick is not None
    assert quick.status == STATUS_COMPLETED, "没超时就不该被抢"


def test_task_can_stop_itself_when_cancelled():
    """任务配合取消时，走**协作式**那条路（不是被强抢）。"""
    recorder = _Recorder()
    started = threading.Event()

    def handler(payload: Dict[str, Any], token: CancelToken) -> str:
        started.set()
        for _ in range(200):
            token.raise_if_cancelled()
            time.sleep(0.01)
        return "done"

    queue_ = PriorityTaskQueue(
        handler=handler,
        on_result=recorder,
        preempt_after_seconds=0.1,
        cancel_grace_seconds=1.0,
    )
    queue_.submit("polite", {"name": "polite"}, PRIORITY_P2)
    assert started.wait(2.0)
    time.sleep(0.2)
    queue_.submit("p0", {"name": "p0"}, PRIORITY_P0)
    assert queue_.drain(timeout=5.0)

    polite = recorder.get("polite")
    assert polite is not None
    assert polite.status == STATUS_CANCELLED
    assert polite.degraded


def test_cancelled_task_can_raise_task_cancelled():
    """任务自己抛 :class:`TaskCancelled` 也算「取消」，不算「失败」。"""
    recorder = _Recorder()

    def handler(payload: Dict[str, Any], token: CancelToken) -> str:
        raise TaskCancelled("我自己收手了")

    queue_ = PriorityTaskQueue(handler=handler, on_result=recorder)
    queue_.submit("x", {"name": "x"}, PRIORITY_P2)
    assert queue_.drain(timeout=5.0)

    outcome = recorder.get("x")
    assert outcome is not None
    assert outcome.status == STATUS_CANCELLED
    assert outcome.error == ""


# ---------------------------------------------------------------------------
# 背压与熔断
# ---------------------------------------------------------------------------
def _fill_queue(queue_: PriorityTaskQueue, gate: threading.Event) -> None:
    """把队列占满（worker 卡在第一个任务上）。"""

    def handler(payload: Dict[str, Any]) -> str:
        gate.wait(3.0)
        return "done"

    queue_.handler = handler
    queue_.submit("blocker", {"name": "blocker"}, PRIORITY_P2)
    assert _wait_until(lambda: queue_.running), "blocker 没有开跑"


def test_p2_is_dropped_when_queue_is_full():
    """队列满了就丢 P2，并给出**降级结果**。"""
    gate = threading.Event()
    recorder = _Recorder()
    queue_ = PriorityTaskQueue(
        handler=lambda payload: "done",
        on_result=recorder,
        max_pending=10,
    )
    _fill_queue(queue_, gate)

    for index in range(10):
        receipt = queue_.submit("p2-{}".format(index), {}, PRIORITY_P2)
        assert receipt.accepted, "前 10 条应当收下"
    assert queue_.pending == 10

    overflow = queue_.submit("p2-overflow", {}, PRIORITY_P2)
    assert not overflow.accepted
    assert overflow.status == STATUS_DROPPED
    assert overflow.degraded
    assert "队列已满" in overflow.reason

    # ⚠️ 丢弃**不是沉默** —— 降级结果必须交出去。
    dropped = recorder.get("p2-overflow")
    assert dropped is not None
    assert dropped.status == STATUS_DROPPED
    assert dropped.degraded

    gate.set()
    queue_.drain(timeout=5.0)


def test_p0_is_never_dropped_by_backpressure():
    """背压**只丢 P2** —— 红线永远不丢。"""
    gate = threading.Event()
    recorder = _Recorder()
    queue_ = PriorityTaskQueue(
        handler=lambda payload: "done",
        on_result=recorder,
        max_pending=10,
    )
    _fill_queue(queue_, gate)
    for index in range(10):
        queue_.submit("p2-{}".format(index), {}, PRIORITY_P2)

    receipt = queue_.submit("p0", {}, PRIORITY_P0)
    assert receipt.accepted, "红线任务不能被背压丢掉"
    assert queue_.pending == 11

    gate.set()
    queue_.drain(timeout=5.0)


def test_dropped_tasks_are_counted():
    """丢弃必须**记账** —— 沉默的失效是最坏的失效。"""
    gate = threading.Event()
    recorder = _Recorder()
    queue_ = PriorityTaskQueue(
        handler=lambda payload: "done",
        on_result=recorder,
        max_pending=2,
    )
    _fill_queue(queue_, gate)

    for index in range(6):
        queue_.submit("p2-{}".format(index), {}, PRIORITY_P2)

    stats = queue_.stats()
    assert stats["dropped"] == 4
    assert stats["submitted"] == 7
    assert stats["accepted"] == 3

    gate.set()
    queue_.drain(timeout=5.0)


# ---------------------------------------------------------------------------
# 资源释放与异常隔离
# ---------------------------------------------------------------------------
def test_worker_releases_resources_after_queue_drains():
    """队列清空后，后台线程**自动退出**（资源全部释放）。"""
    queue_ = PriorityTaskQueue(handler=lambda payload: "done")
    queue_.submit("a", {}, PRIORITY_P2)
    queue_.submit("b", {}, PRIORITY_P2)
    assert queue_.drain(timeout=5.0)

    assert _wait_until(lambda: queue_.idle, timeout=3.0), "线程没有退出"
    stats = queue_.stats()
    assert stats["worker_alive"] is False
    assert stats["pending"] == 0
    assert stats["outstanding"] == 0


def test_queue_is_idle_before_first_use():
    """还没投递过任务时，不该占着线程。"""
    queue_ = PriorityTaskQueue(handler=lambda payload: "done")
    assert queue_.idle
    assert queue_.stats()["worker_alive"] is False


def test_failing_task_does_not_kill_the_worker():
    """任务炸了，后台线程**不能死** —— 下一个任务照跑。"""
    recorder = _Recorder()

    def handler(payload: Dict[str, Any]) -> str:
        if payload["name"] == "boom":
            raise RuntimeError("模型炸了")
        return payload["name"]

    queue_ = PriorityTaskQueue(handler=handler, on_result=recorder)
    queue_.submit("boom", {"name": "boom"}, PRIORITY_P2)
    queue_.submit("after", {"name": "after"}, PRIORITY_P2)
    assert queue_.drain(timeout=5.0)

    boom = recorder.get("boom")
    after = recorder.get("after")
    assert boom is not None and boom.status == STATUS_FAILED
    assert boom.degraded
    assert "RuntimeError" in boom.error
    assert after is not None and after.status == STATUS_COMPLETED


def test_failing_callback_does_not_kill_the_worker():
    """回调炸了也不能影响队列。"""

    def bad_callback(task_id: str, outcome: QueueOutcome) -> None:
        raise ValueError("回调炸了")

    seen: List[str] = []
    queue_ = PriorityTaskQueue(
        handler=lambda payload: payload["name"],
        on_result=bad_callback,
    )
    queue_.submit("a", {"name": "a"}, PRIORITY_P2)
    assert queue_.drain(timeout=5.0)
    assert queue_.stats()["completed"] == 1
    assert seen == []


def test_missing_handler_fails_gracefully():
    """没绑定处理函数 —— 任务降级失败，线程不死。"""
    recorder = _Recorder()
    queue_ = PriorityTaskQueue(on_result=recorder)
    queue_.submit("a", {}, PRIORITY_P2)
    assert queue_.drain(timeout=5.0)
    outcome = recorder.get("a")
    assert outcome is not None and outcome.status == STATUS_FAILED


# ---------------------------------------------------------------------------
# 验收：3 个 Agent 同时狂发
# ---------------------------------------------------------------------------
class _FifoBaseline:
    """普通 FIFO 队列（今天的做法）—— 用来量一量「插队」到底值多少。

    刻意**不设上限**：这样对比出来的差异**纯粹**来自等待顺序，
    不掺杂「谁被丢了」的影响。
    """

    def __init__(self, handler: Callable[[Dict[str, Any]], Any]) -> None:
        self._handler = handler
        self._queue: "queue.Queue[Any]" = queue.Queue()
        self._waits: Dict[str, List[Optional[float]]] = {}
        self._lock = threading.Lock()
        self._cv = threading.Condition()
        self._submitted = 0
        self._done = 0
        self._worker = threading.Thread(
            target=self._loop, name="fifo-baseline", daemon=True
        )
        self._worker.start()

    def submit(
        self,
        task_id: str,
        payload: Dict[str, Any],
        priority: Priority = PRIORITY_P2,
    ) -> QueueOutcome:
        """投递（**忽略档位** —— FIFO 就是 FIFO）。"""
        with self._lock:
            self._waits[task_id] = [time.monotonic(), None]
            self._submitted += 1
        self._queue.put((task_id, payload))
        return QueueOutcome(
            task_id=task_id, priority=priority, status="queued"
        )

    def _loop(self) -> None:
        while True:
            task_id, payload = self._queue.get()
            if task_id is None:
                return
            started = time.monotonic()
            try:
                self._handler(payload)
            except Exception as exc:  # noqa: BLE001 —— 对照组不许崩
                logger.debug(
                    "FIFO 对照组任务失败（已忽略）：%s: %s",
                    type(exc).__name__,
                    exc,
                )
            with self._lock:
                self._waits[task_id][1] = started
            with self._cv:
                self._done += 1
                self._cv.notify_all()

    def drain(self, timeout: float = 30.0) -> bool:
        """等所有任务跑完。"""
        deadline = time.monotonic() + max(float(timeout), 0.0)
        with self._cv:
            while self._done < self._submitted:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._cv.wait(min(remaining, 0.05))
            return True

    def wait_ms(self, task_id: str) -> int:
        """某个任务的等待延迟（毫秒）。"""
        with self._lock:
            submitted, started = self._waits[task_id]
        if started is None:
            return 0
        return int((started - submitted) * 1000)


def _fake_model_call(payload: Dict[str, Any]) -> str:
    """模拟一次模型调用（单并发引擎上要花好几秒，这里按比例缩小）。"""
    time.sleep(payload.get("cost", 0.006))
    return payload.get("name", "")


def _spawn_workload(
    submit: Callable[..., QueueOutcome],
    *,
    p2_total: int = 100,
    p0_total: int = 5,
    agents: int = 3,
) -> threading.Thread:
    """起 3 个 Agent 线程 + 1 个红线线程，往 ``submit`` 里灌任务。

    红线线程在 P2 提交到 ``10 / 30 / 50 / 70 / 90`` 时各插一条 P0 ——
    这就是「**穿插**」。

    :param submit: 投递回调。
    :param p2_total: P2 总数。
    :param p0_total: P0 总数。
    :param agents: Agent 线程数。
    :return: 红线线程（调用方负责 join）。
    """
    counter = {"count": 0}
    lock = threading.Lock()
    per_agent = p2_total // agents
    remainder = p2_total - per_agent * agents

    def agent_loop(index: int, count: int) -> None:
        for step in range(count):
            submit(
                "agent{}-p2-{}".format(index, step),
                {"name": "agent{}-p2-{}".format(index, step)},
                PRIORITY_P2,
            )
            with lock:
                counter["count"] += 1
            # 让 3 个 Agent 的提交**交错**开跑。
            time.sleep(0.0008)

    threads = []
    for index in range(agents):
        count = per_agent + (1 if index < remainder else 0)
        thread = threading.Thread(
            target=agent_loop, args=(index, count), daemon=True
        )
        thread.start()
        threads.append(thread)

    thresholds = [
        int((step + 1) * p2_total / (p0_total + 1))
        for step in range(p0_total)
    ]

    def redline_loop() -> None:
        for order, threshold in enumerate(thresholds):
            while True:
                with lock:
                    reached = counter["count"] >= threshold
                if reached:
                    break
                time.sleep(0.002)
            submit(
                "p0-{}".format(order),
                {"name": "p0-{}".format(order)},
                PRIORITY_P0,
            )
            time.sleep(0.01)

    redline = threading.Thread(target=redline_loop, name="redline", daemon=True)
    redline.start()
    for thread in threads:
        thread.join(timeout=30.0)
    return redline


def test_three_agents_spam_never_blocks_p0():
    """验收主测试 —— 3 个 Agent 狂发 100 个 P2 + 5 个 P0，P0 绝不被堵。"""
    recorder = _Recorder()
    queue_ = PriorityTaskQueue(
        handler=_fake_model_call,
        on_result=recorder,
        max_pending=10,
        preempt_after_seconds=10.0,
    )

    started = time.monotonic()
    redline = _spawn_workload(queue_.submit)
    redline.join(timeout=30.0)

    # ⚠️ 无死锁：所有已收下的任务都要在超时前出结论。
    assert queue_.drain(timeout=30.0), "队列没排空 —— 有死锁"
    elapsed_ms = int((time.monotonic() - started) * 1000)

    stats = queue_.stats()
    p0_outcomes = recorder.of(PRIORITY_P0)
    p2_outcomes = recorder.of(PRIORITY_P2)

    # --- P0 必须一个不落、全部正常跑完 ---
    assert len(p0_outcomes) == 5, "5 条 P0 都要有结论"
    assert all(item.status == STATUS_COMPLETED for item in p0_outcomes)
    assert all(item.wait_ms >= 0 for item in p0_outcomes)

    p0_waits = [item.wait_ms for item in p0_outcomes]
    p2_waits = [item.wait_ms for item in p2_outcomes]
    p0_avg = round(sum(p0_waits) / len(p0_waits))
    p2_avg = round(sum(p2_waits) / len(p2_waits)) if p2_waits else 0

    # --- P0 的等待延迟必须**极短** ---
    # 单次模型调用约 6ms —— P0 最多等「正在跑的那一个」，
    # 所以平均等待应当与一次调用同量级，而不是与整个积压同量级。
    assert p0_avg < 200, "P0 平均等待 {}ms —— 太长了".format(p0_avg)
    assert max(p0_waits) < 1000, "P0 最久等了 {}ms".format(max(p0_waits))

    # --- 不浪费请求：丢掉的 P2 每一笔都有降级结果 ---
    dropped = [item for item in p2_outcomes if item.status == STATUS_DROPPED]
    assert len(dropped) == stats["dropped"]
    assert all(item.degraded for item in dropped)

    # --- 账目要对得上 ---
    assert stats["submitted"] == 105
    assert stats["accepted"] + stats["dropped"] == 105
    assert stats["completed"] == stats["accepted"]
    # ⚠️ 最硬的一条：**每一次投递都有结论** ——
    #    要么跑完了，要么明确降级了，**绝不能静默消失**。
    assert len(recorder.outcomes) == 105, (
        "有投递没有结论 —— 静默的失效是最坏的失效"
    )

    print("\n" + "=" * 66)
    print("优先级队列 · 3 Agent 并发压力模拟（100 × P2 + 5 × P0）")
    print("=" * 66)
    print("  总耗时          : {} ms".format(elapsed_ms))
    print("  投递 / 收下 / 丢弃 : {} / {} / {}".format(
        stats["submitted"], stats["accepted"], stats["dropped"]
    ))
    print("  完成 / 抢占 / 失败 : {} / {} / {}".format(
        stats["completed"], stats["preempted"], stats["failed"]
    ))
    print("  P0 平均等待延迟  : {} ms   （最久 {} ms）".format(
        p0_avg, max(p0_waits)
    ))
    print("  P2 平均等待延迟  : {} ms".format(p2_avg))
    print("  P0 等待统计      : {}".format(stats["wait_ms"]["P0_RED_LINE"]))
    print("  P2 等待统计      : {}".format(stats["wait_ms"]["P2_ROUTINE"]))
    print("=" * 66 + "\n")


def test_priority_queue_beats_fifo_baseline_on_p0_wait():
    """对照实验 —— 同一份负载，普通 FIFO 队列会把红线堵成什么样。"""
    recorder = _Recorder()
    queue_ = PriorityTaskQueue(
        handler=_fake_model_call,
        on_result=recorder,
        max_pending=10,
    )
    redline = _spawn_workload(queue_.submit)
    redline.join(timeout=30.0)
    assert queue_.drain(timeout=30.0), "优先级队列没排空"

    p0_waits = [item.wait_ms for item in recorder.of(PRIORITY_P0)]
    priority_avg = round(sum(p0_waits) / len(p0_waits))

    # --- 同一份负载，跑一遍今天的 FIFO 做法 ---
    baseline = _FifoBaseline(_fake_model_call)
    redline = _spawn_workload(baseline.submit)
    redline.join(timeout=30.0)
    assert baseline.drain(timeout=30.0), "FIFO 队列没排空"

    fifo_waits = [
        baseline.wait_ms("p0-{}".format(index)) for index in range(5)
    ]
    fifo_avg = round(sum(fifo_waits) / len(fifo_waits))

    print("\n" + "=" * 66)
    print("P0 等待延迟对照（同一份负载：100 × P2 + 5 × P0）")
    print("=" * 66)
    print("  优先级队列 : 平均 {:>6} ms   最久 {:>6} ms".format(
        priority_avg, max(p0_waits)
    ))
    print("  普通 FIFO  : 平均 {:>6} ms   最久 {:>6} ms".format(
        fifo_avg, max(fifo_waits)
    ))
    print("  改善倍数   : {:.1f}×".format(fifo_avg / max(priority_avg, 1)))
    print("=" * 66 + "\n")

    # 优先级队列必须**明显**更好（留足余量，避免偶发抖动）。
    assert priority_avg * 5 < fifo_avg, (
        "优先级队列（{}ms）没有明显优于 FIFO（{}ms）".format(
            priority_avg, fifo_avg
        )
    )
    assert max(p0_waits) < max(fifo_waits)


def test_no_deadlock_under_continuous_p0_storm():
    """P0 狂风暴雨时，P2 也不该把整个队列卡死。"""
    recorder = _Recorder()
    queue_ = PriorityTaskQueue(
        handler=_fake_model_call,
        on_result=recorder,
        max_pending=10,
    )

    stop = threading.Event()

    def storm() -> None:
        index = 0
        while not stop.is_set():
            queue_.submit(
                "storm-{}".format(index),
                {"name": "storm", "cost": 0.001},
                PRIORITY_P0,
            )
            index += 1
            time.sleep(0.001)

    thread = threading.Thread(target=storm, daemon=True)
    thread.start()
    time.sleep(0.2)
    stop.set()
    thread.join(timeout=5.0)

    assert queue_.drain(timeout=30.0), "P0 风暴把队列卡死了"
    stats = queue_.stats()
    assert stats["completed"] == stats["accepted"]
    assert stats["failed"] == 0
