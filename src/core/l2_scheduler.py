"""L2 同步调度器 —— 让同步阻塞的模型调用也能按优先级排队（v4.0.0）。

它补的是哪块
============

哨兵的 L2 模型调用（漂移判定、告警解释）是**同步阻塞**的：
一次请求要占住调用线程好几秒。

而 :class:`~src.core.priority_queue.PriorityTaskQueue` 是**异步**的 ——
:meth:`~src.core.priority_queue.PriorityTaskQueue.submit` 只给一张
「凭据」（:class:`~src.core.priority_queue.QueueOutcome`），
真正的结论要等 ``on_result`` 回调异步送到。

两头对不上。调用方（引擎里的漂移判定、GUI 里的告警解释）想要的是
「**投递 + 等结论**」的同步语义，却只能自己写一遍
「登记 → 轮询 → 超时 → 拆结果」的样板代码。

本模块把这段样板收进 :class:`L2Scheduler`：

.. code-block:: python

    scheduler = L2Scheduler(max_pending=10)
    verdict = scheduler.call(
        "drift-42",
        lambda: evaluate_drift(...),
        priority=classify_priority(verdict_dict),
    )

三档语义
========

* **P0（红线）插队** —— 队列里压着一堆 P2 时，P0 永远第一个跑；
* **P2 会被背压丢弃** —— 队列满了就丢日常摘要，
  :class:`L2Rejected` 会告诉调用方「这次**根本没跑**」；
* **跑太久的任务会被抢占** —— 让位给更高档位，
  :class:`L2Preempted` 会告诉调用方「这次**被丢了**」。

降级必须**可区分**
==================

调用方拿到结果后通常有三种反应：**用**、**降级**、**重试**。
所以三种结局必须能被**区分开**，不能都糊成一个 ``RuntimeError``：

========================= ================================ ==========
结局                      异常                            含义
========================= ================================ ==========
正常跑完                  ——（返回 ``fn()`` 的值）        用它
``fn`` 自己抛了            **原样重抛**（同一个异常对象）    按它自己的逻辑处理
背压丢弃                  :class:`L2Rejected`             没跑 → 降级
被抢占                    :class:`L2Preempted`            跑了但被丢 → 降级
超时                      :class:`TimeoutError`          还在跑 → 稍后再看
========================= ================================ ==========

⚠️ **已知限制（两条，必须知道）**
==================================

1. **超时之后，任务仍会在后台跑完** ——
   :class:`~src.core.priority_queue.PriorityTaskQueue` **没有取消入口**，
   正在跑的任务拦不下来。所以 :meth:`L2Scheduler.call` 超时抛
   :class:`TimeoutError` 时，那个任务**还在队列里跑着**，
   它的结论会被丢弃（没有调用方认领）。
   也就是说：**超时 ≠ 没跑**。单并发引擎上，超时的任务照样占着位置。
2. **抢占会真实丢掉一次模型调用** ——
   抢占的代价不是「时间」，而是**一次已经发出去的推理请求**
   （可能已经烧掉了大半的推理预算）。所以每一次抢占都被
   :meth:`L2Scheduler.stats` 记账（``preempted`` / ``abandoned``），
   调用方也会拿到 :class:`L2Preempted`。

线程安全
========

:meth:`L2Scheduler.call` **可以多线程同时调**：
每次调用在 :attr:`L2Scheduler._waiters` 里登记一个等待项
（``threading.Event`` + 结论槽位），结论回来时按 ``task_id`` 精确回投。
同一个 ``task_id`` **不能**同时在途（会撞车）—— 直接抛
:class:`L2Rejected`。

怎么用
======

.. code-block:: python

    scheduler = L2Scheduler(max_pending=10, name="sentinel-l2")
    try:
        verdict = scheduler.call("drift-42", fn, priority=PRIORITY_P0)
    except L2Rejected:
        ...   # 队列满了，这次没跑 —— 降级
    except L2Preempted:
        ...   # 被红线插队挤掉了 —— 降级
    except TimeoutError:
        ...   # 超时，任务还在后台跑
    finally:
        scheduler.stop()
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from src.core.priority_queue import (
    PRIORITY_LABELS,
    PRIORITY_P0,
    PRIORITY_P1,
    PRIORITY_P2,
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_DROPPED,
    STATUS_FAILED,
    STATUS_PREEMPTED,
    Priority,
    PriorityTaskQueue,
    QueueOutcome,
)

logger = logging.getLogger(__name__)

__all__ = [
    "PRIORITY_P0",
    "PRIORITY_P1",
    "PRIORITY_P2",
    "L2Preempted",
    "L2Rejected",
    "L2Scheduler",
    "Priority",
    "QueueOutcome",
]


class L2Rejected(RuntimeError):
    """任务被**拒收** —— 压根没跑。

    三种情况会抛它：背压丢弃（队列满了，丢的是 P2）、
    调度器已经 ``stop()``、同一个 ``task_id`` 重复在途。

    这**不是**「模型调用失败」：任务**根本没执行**。
    调用方通常该降级处理（用旧结论、跳过这次摘要），
    而不是把它当成引擎故障上抛。
    """


class L2Preempted(RuntimeError):
    """任务被**抢占** —— 让位给更高档位的任务。

    ⚠️ 抢占意味着**一次模型调用被丢掉**（可能已经烧掉了大半的
    推理预算），所以它必须被记账，也必须让调用方知道。
    """


class _Job:
    """一次调用的内部凭据。

    ⚠️ :attr:`error` 存的是 **原异常对象** —— 队列只会给出
    ``"ValueError: xxx"`` 这样的字符串，调用方要的是**原样重抛**，
    所以得在任务执行时把异常本身留下来。
    """

    def __init__(self, task_id: str, fn: Callable[[], Any]) -> None:
        self.task_id = task_id
        self.fn = fn
        self.error: Optional[BaseException] = None
        self.submitted_at = time.monotonic()


class _Waiter:
    """等待结论的登记项（一次 :meth:`L2Scheduler.call` 一个）。"""

    def __init__(self, job: _Job) -> None:
        self.job = job
        self.event = threading.Event()
        self.outcome: Optional[QueueOutcome] = None


class L2Scheduler:
    """把 :class:`~src.core.priority_queue.PriorityTaskQueue` 包成**同步**接口。

    :param max_pending: 队列容量（待处理任务数上限），满了丢 P2。
    :param preempt_after_seconds: 正在跑的任务超过这个秒数，
        且有更高档位在等，才允许抢占。
    :param name: 后台线程名前缀。
    :param on_result: **可选**的观测钩子，签名
        ``(task_id, QueueOutcome) -> None``。
        每次任务出结论都会调它；它抛异常**只记日志**，
        绝不会影响调度器。⚠️ 它在队列线程上被调用，
        **不许阻塞**。
    """

    def __init__(
        self,
        *,
        max_pending: int = 10,
        preempt_after_seconds: float = 10.0,
        name: str = "sentinel-l2",
        on_result: Optional[Callable[[str, QueueOutcome], None]] = None,
    ) -> None:
        self.max_pending = int(max_pending)
        self.preempt_after_seconds = float(preempt_after_seconds)
        self.name = str(name or "sentinel-l2")
        self._observer = on_result

        #: 登记项锁（只护字典，不护等待过程）。
        self._lock = threading.Lock()

        #: 在途任务的登记项（``task_id`` → 等待项）。
        self._waiters: Dict[str, _Waiter] = {}

        #: 停机标志 —— 停机后不再接受新任务。
        self._stopped = False

        #: 调度器自己的计数（一切降级都要记账）。
        self._counters: Dict[str, int] = {
            "submitted": 0,
            "completed": 0,
            "failed": 0,
            "timed_out": 0,
            "dropped": 0,
            "preempted": 0,
            "cancelled": 0,
        }

        #: 分档位的**排队等待**样本（毫秒，取自队列的 ``wait_ms``）。
        self._waits: Dict[Priority, List[int]] = {
            PRIORITY_P0: [],
            PRIORITY_P1: [],
            PRIORITY_P2: [],
        }

        #: 分档位的**端到端**耗时样本（毫秒，投递 → 拿到结论）。
        self._latencies: Dict[Priority, List[int]] = {
            PRIORITY_P0: [],
            PRIORITY_P1: [],
            PRIORITY_P2: [],
        }

        #: 底层的优先级队列。
        self._queue = PriorityTaskQueue(
            handler=self._handle,
            on_result=self._on_result,
            max_pending=self.max_pending,
            preempt_after_seconds=self.preempt_after_seconds,
            name=self.name,
        )

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------
    def call(
        self,
        task_id: str,
        fn: Callable[[], Any],
        *,
        priority: Priority = PRIORITY_P2,
        timeout: float = 120.0,
    ) -> Any:
        """投递一个任务并**同步等它出结论**。

        :param task_id: 任务 ID（唯一；结论原样带回）。
        :param fn: 无参可调用对象 —— 真正干活的地方。
            它在**队列的工作线程**上执行，不是调用方线程。
        :param priority: 档位（P0 插队）。
        :param timeout: 最长等多少秒。
        :return: ``fn()`` 的返回值。
        :raises L2Rejected: 背压丢弃 / 调度器已停 / ``task_id`` 重复在途。
        :raises L2Preempted: 任务被更高档位抢占。
        :raises TimeoutError: 超时。

        ⚠️ **超时后任务仍会在后台跑完** ——
        队列没有取消入口，正在跑的任务拦不下来。
        超时只是说「调用方不等了」，任务**还在跑**，
        它的结论会被丢弃（没有调用方认领）。
        """
        key = str(task_id)
        label = _priority_text(priority)
        job = _Job(key, fn)
        waiter = _Waiter(job)

        with self._lock:
            if self._stopped:
                raise L2Rejected(
                    "L2 调度器已停止，不再接受新任务：{}（{}）".format(key, label)
                )
            if key in self._waiters:
                raise L2Rejected(
                    "L2 任务 ID 重复（已在队列中）：{}（{}）".format(key, label)
                )
            self._waiters[key] = waiter

        try:
            receipt = self._queue.submit(key, {"job": job}, priority)
        except Exception:  # noqa: BLE001 —— 投递崩了就原样上抛
            with self._lock:
                self._waiters.pop(key, None)
            raise

        prio = receipt.priority
        label = _priority_text(prio)
        self._bump("submitted")

        if receipt.status == STATUS_DROPPED:
            # 背压 —— 队列在 submit 内部就同步回调过 on_result。
            self._settle(key, waiter, receipt)
            raise L2Rejected(
                "L2 任务被背压丢弃：{}（{}）—— {}".format(
                    key, label, receipt.reason
                )
            )

        finished = waiter.event.wait(max(float(timeout), 0.0))
        if not finished:
            # ⚠️ 超时 —— 任务**仍在后台跑**，结论会被丢弃。
            self._settle(key, waiter, None)
            self._bump("timed_out")
            raise TimeoutError(
                "L2 任务超时（{:.1f}s）：{}（{}）—— 任务仍会在后台跑完".format(
                    float(timeout), key, label
                )
            )

        outcome = waiter.outcome
        if outcome is None:
            # 停机打断 —— 没有结论可等。
            self._settle(key, waiter, None)
            raise L2Rejected(
                "L2 调度器已停止，任务未出结论：{}（{}）".format(key, label)
            )

        self._settle(key, waiter, outcome)
        return self._unwrap(key, label, outcome, job)

    def stats(self) -> Dict[str, Any]:
        """统计（看板与验收用）。

        返回**队列统计**（顶层扁平字段，来自
        :meth:`~src.core.priority_queue.PriorityTaskQueue.stats`）
        **加上**本调度器自己的计数（``scheduler`` 子字典）：

        * ``scheduler.submitted`` —— 总投递数；
        * ``scheduler.completed`` —— 正常跑完数；
        * ``scheduler.failed`` —— 失败数（含 ``fn`` 抛异常）；
        * ``scheduler.timed_out`` —— 超时数；
        * ``scheduler.dropped`` —— 背压丢弃数；
        * ``scheduler.preempted`` —— 抢占数；
        * ``scheduler.cancelled`` —— 协作式取消数；
        * ``scheduler.wait_ms`` —— 分档位的**排队等待**延迟；
        * ``scheduler.latency_ms`` —— 分档位的**端到端**耗时。

        :return: 统计字典。
        """
        with self._lock:
            counters = dict(self._counters)
            waits = {
                PRIORITY_LABELS[key]: _summary(values)
                for key, values in self._waits.items()
            }
            latencies = {
                PRIORITY_LABELS[key]: _summary(values)
                for key, values in self._latencies.items()
            }
            inflight = len(self._waiters)
            stopped = self._stopped

        result = dict(self._queue.stats())
        result["name"] = self.name
        result["stopped"] = stopped
        result["inflight"] = inflight
        result["scheduler"] = {
            "submitted": counters["submitted"],
            "completed": counters["completed"],
            "failed": counters["failed"],
            "timed_out": counters["timed_out"],
            "dropped": counters["dropped"],
            "preempted": counters["preempted"],
            "cancelled": counters["cancelled"],
            "wait_ms": waits,
            "latency_ms": latencies,
        }
        return result

    def drain(self, timeout: float = 30.0) -> bool:
        """等所有**已收下**的任务出结论。

        :param timeout: 最长等多少秒。
        :return: 全部完成返回 ``True``；超时返回 ``False``。
        """
        return self._queue.drain(timeout)

    def stop(self, timeout: float = 5.0) -> None:
        """停掉后台线程。

        在途任务**立刻**以「被拒收」收场（调用方拿到
        :class:`L2Rejected`），因为停机后不会再有结论送回来。

        :param timeout: 等线程退出的秒数。
        """
        with self._lock:
            self._stopped = True
            waiters = list(self._waiters.values())
            self._waiters.clear()
        for waiter in waiters:
            waiter.event.set()
        self._queue.stop(timeout)

    def pending(self) -> int:
        """队列里**待处理**的任务数。

        :return: 待处理任务数。
        """
        return self._queue.pending

    def idle(self) -> bool:
        """队列是不是**彻底空了**。

        :return: 没待处理、没在跑、没线程时返回 ``True``。
        """
        return self._queue.idle

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------
    def _handle(self, payload: Dict[str, Any]) -> Any:
        """真正执行任务（队列的工作线程上跑）。

        ⚠️ 异常必须在这里收住**再原样上抛** ——
        队列会把异常变成 ``failed`` 结局（而不是让后台线程崩掉），
        同时把**原异常对象**留在 :attr:`_Job.error` 上，
        好让 :meth:`call` 能把它**原样**抛回调用方。
        """
        job = payload.get("job")
        if not isinstance(job, _Job):
            raise TypeError("L2 任务载荷里没有 job 凭据")
        try:
            return job.fn()
        except BaseException as exc:  # noqa: BLE001 —— 原样上抛给队列
            job.error = exc
            raise

    def _on_result(self, task_id: str, outcome: QueueOutcome) -> None:
        """回收结论 —— **绝不能**因为异常而影响调度器。

        :param task_id: 任务 ID。
        :param outcome: 队列给出的结论。
        """
        waiter: Optional[_Waiter] = None
        try:
            with self._lock:
                waiter = self._waiters.get(task_id)
                if waiter is not None:
                    waiter.outcome = outcome
            if waiter is not None:
                waiter.event.set()
            else:
                # 超时之后才回来的结论 —— 没人认领，只能丢掉。
                logger.debug("L2 任务 %s 的结论无人认领（可能已超时）", task_id)
        except Exception as exc:  # noqa: BLE001 —— 回收失败不许影响审查
            logger.warning(
                "L2 结果回收失败（已隔离）：%s: %s", type(exc).__name__, exc
            )
        self._notify_observer(task_id, outcome)

    def _notify_observer(self, task_id: str, outcome: QueueOutcome) -> None:
        """用户观测钩子 —— 失败只记日志，绝不影响调度器。"""
        observer = self._observer
        if observer is None:
            return
        try:
            observer(task_id, outcome)
        except Exception as exc:  # noqa: BLE001 —— 观测失败不许影响审查
            logger.warning(
                "L2 观测回调失败（已隔离）：%s: %s", type(exc).__name__, exc
            )

    def _settle(
        self,
        key: str,
        waiter: _Waiter,
        outcome: Optional[QueueOutcome],
    ) -> None:
        """摘掉登记项并记账。

        :param key: 任务 ID。
        :param waiter: 等待项。
        :param outcome: 结论；``None`` 表示「没结论」（超时 / 停机）。
        """
        with self._lock:
            self._waiters.pop(key, None)
            if outcome is None:
                return
            status = outcome.status
            if status == STATUS_COMPLETED:
                self._counters["completed"] += 1
            elif status == STATUS_FAILED:
                self._counters["failed"] += 1
            elif status == STATUS_DROPPED:
                self._counters["dropped"] += 1
            elif status == STATUS_PREEMPTED:
                self._counters["preempted"] += 1
            elif status == STATUS_CANCELLED:
                self._counters["cancelled"] += 1
            self._waits[outcome.priority].append(outcome.wait_ms)
            elapsed = time.monotonic() - waiter.job.submitted_at
            self._latencies[outcome.priority].append(int(elapsed * 1000))

    def _bump(self, counter: str) -> None:
        """计数 +1（线程安全）。

        :param counter: 计数器名字。
        """
        with self._lock:
            self._counters[counter] += 1

    def _unwrap(
        self,
        key: str,
        label: str,
        outcome: QueueOutcome,
        job: _Job,
    ) -> Any:
        """把结论翻译成「返回值」或「异常」。

        :param key: 任务 ID。
        :param label: 档位标签。
        :param outcome: 队列给出的结论。
        :param job: 任务凭据（带着原异常对象）。
        :return: ``fn()`` 的返回值。
        :raises L2Rejected: 背压丢弃。
        :raises L2Preempted: 被抢占 / 被取消。
        :raises Exception: ``fn`` 自己抛的异常（**原样重抛**）。
        """
        status = outcome.status
        if status == STATUS_COMPLETED:
            return outcome.value
        if status == STATUS_DROPPED:
            raise L2Rejected(
                "L2 任务被背压丢弃：{}（{}）—— {}".format(
                    key, label, outcome.reason
                )
            )
        if status == STATUS_PREEMPTED:
            raise L2Preempted(
                "L2 任务被抢占：{}（{}）—— {}".format(
                    key, label, outcome.reason
                )
            )
        if status == STATUS_CANCELLED:
            if job.error is not None:
                # 任务自己收的手（抛了 TaskCancelled）—— 原样重抛。
                raise job.error
            raise L2Preempted(
                "L2 任务被取消：{}（{}）—— {}".format(
                    key, label, outcome.reason
                )
            )
        # 失败 —— 原样重新抛出 fn 抛的那个异常。
        if job.error is not None:
            raise job.error
        raise RuntimeError(
            "L2 任务失败：{}（{}）—— {}".format(key, label, outcome.reason)
        )


# ----------------------------------------------------------------------
# 辅助
# ----------------------------------------------------------------------
def _priority_text(priority: Any) -> str:
    """把档位写成**人话标签**（``P0_RED_LINE`` 这种）。

    :param priority: 档位。
    :return: 标签文本。
    """
    if isinstance(priority, Priority):
        return PRIORITY_LABELS.get(priority, priority.name)
    return str(priority)


def _summary(values: List[int]) -> Dict[str, Any]:
    """把一组毫秒样本压成统计。

    :param values: 样本（毫秒）。
    :return: ``{"count", "avg", "max", "min", "p50"}``。
    """
    if not values:
        return {"count": 0, "avg": 0, "max": 0, "min": 0, "p50": 0}
    ordered = sorted(values)
    count = len(ordered)
    return {
        "count": count,
        "avg": round(sum(ordered) / count),
        "max": ordered[-1],
        "min": ordered[0],
        "p50": ordered[count // 2],
    }
