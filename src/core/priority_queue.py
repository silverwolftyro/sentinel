"""单并发引擎上的优先级队列 —— 让红线插队，让日常别挡路（v3.8.1）。

为什么要它
==========

本机 NInfer 是 **单并发** 引擎（``max-concurrency=1``）——
同时发两个请求，引擎**直接崩**（v3.2.1 实机记录）。
所以哨兵从一开始就用 :func:`~src.llm.base.engine_lock_for`
在**客户端**把请求排成队：一把 ``threading.Lock``，谁先抢到谁先跑。

但「排队」不等于「排队就够」
==============================

一把普通的 FIFO 锁有个致命问题：**先到先得**。

红线级事件（``rm -rf /``、整盘格式化……）如果不幸排在
一串 ``ON_TRACK`` 摘要提取后面，它要**等前面那些日常任务全部跑完**
才能拿到模型 —— 而单并发引擎上，每一次判定都要好几秒。

十次日常摘要 = 几十秒。红线事件就在队列里**干等着**。
这与哨兵存在的意义正好相反：
**最该被立刻看见的事，反而是最晚被看见的。**

三级优先级
==========

===================== ==================================== ===============
档位                  谁                                    抢在谁前面
===================== ==================================== ===============
``P0_RED_LINE``       L1 命中的**红线**事件                 **所有人**
``P1_DRIFT``          连续 ``DRIFT`` 漂移判定              P2
``P2_ROUTINE``        日常 ``ON_TRACK`` 摘要 / 人话解释      谁都不抢
===================== ==================================== ===============

⚠️ 这三个字符串与 :mod:`src.rules.basic` 里的
``SEVERITY_RED_LINE`` / ``VERDICT_DANGEROUS`` 一致，
但这里**故意不 import** —— 规则层会连带读配置文件，
而本模块要保持成**零依赖叶子模块**（和
:mod:`src.core.timebox`、:mod:`src.core.engine_watchdog` 一个待遇）。
一致性由 ``tests/test_priority_queue.py`` 守着。

四条硬约束
==========

1. **P0 绝不被普通事件阻塞** —— P0 永远排在队首，
   哪怕队里压着一百个 P2；
2. **不浪费任何一次请求** —— 被丢弃的 P2 也要**交出降级结果**
   （绝不能悄悄消失：沉默的失效是最坏的失效），
   被抢占的任务也必须**如实标记**（``preempted``），不能装作跑完了；
3. **抢占必须留痕** —— 抢占掉的不是「时间」，是**一次模型调用**；
   每一次抢占都计数、都有原因，都能在 :meth:`PriorityTaskQueue.stats` 里看见；
4. **绝不抛异常** —— 任务失败、回调失败、取消失败，
   一律降级成结果，**绝不让后台线程死掉**（红线 R5）。

抢占的两级台阶
==============

正在跑的任务不是**说杀就杀**（Python 杀不掉线程），
所以抢占分两级：

* **协作式取消** —— 给任务发一个 :class:`CancelToken`，
  任务自己轮询它、发现被取消就**自己收手**（``TaskCancelled``）；
* **强制放弃** —— 任务不配合时，等过 ``cancel_grace_seconds``
  之后**放弃它**：结果丢弃、状态标记为 ``preempted``，
  队列**立刻**腾出位置给更高优先级。

⚠️ 被强制放弃的线程会在后台自行了断（Python 无法杀线程），
它的返回值**绝不会被当成结论使用** —— 这一点在
:meth:`PriorityTaskQueue.stats` 里以 ``abandoned`` 计数公开。

背压与熔断
==========

* 队列里**待处理**的任务数达到 ``max_pending``（默认 10）时，
  新来的 **P2 直接丢弃**并返回降级结果 ——
  日常摘要不值得占着显存；
* **P0 / P1 永不因背压被丢** —— 宁可排队也不能把红线弄丢；
* 队列**清空**后，后台线程**自动退出**，
  线程与内存**全部释放**（下次投递再懒启动）——
  空闲时不该占着资源。

⚠️ 这里**故意不做「老化提升」**（不让等了太久的 P2 反超 P0）：
「高危事件绝对不被普通事件阻塞」是本模块的**第一原则**。
持续高压下 P2 会被背压丢弃 —— 但每一次丢弃都有据可查。

怎么用
======

.. code-block:: python

    queue = PriorityTaskQueue(
        handler=lambda payload, token: run_cascade(payload),
        on_result=lambda task_id, outcome: publish(task_id, outcome),
        max_pending=10,
        preempt_after_seconds=10.0,
    )

    queue.submit("alert-42", payload, classify_priority(verdict))

:class:`PriorityTaskQueue` **不碰业务路由** —— 它只是一个通用列。
本版**刻意不接线**到 :class:`~src.core.engine.SentinelEngine`，
接线属于另一次改动（且必须一并改掉
``engine_lock_for`` 那把 FIFO 锁的排队顺序）。

统计数据
========

:meth:`PriorityTaskQueue.stats` 给出**分档位**的等待延迟
（``avg_wait_ms`` / ``max_wait_ms``）—— 验收要看的
「P0 平均等待延迟」就在这里。
"""

from __future__ import annotations

import inspect
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass
from enum import IntEnum
from typing import Any, Callable, Deque, Dict, List, Optional

logger = logging.getLogger(__name__)

__all__ = [
    "CANCEL_REASON_PREEMPTED",
    "PRIORITY_LABELS",
    "PRIORITY_P0",
    "PRIORITY_P1",
    "PRIORITY_P2",
    "STATUS_CANCELLED",
    "STATUS_COMPLETED",
    "STATUS_DROPPED",
    "STATUS_FAILED",
    "STATUS_PREEMPTED",
    "STATUS_QUEUED",
    "CancelToken",
    "Priority",
    "PriorityTaskQueue",
    "QueueOutcome",
    "TaskCancelled",
    "classify_priority",
]


class Priority(IntEnum):
    """三级优先级 —— **数字越小越优先**。

    用 :class:`~enum.IntEnum` 而不是字符串，是为了让比较直接可用
    （``p0 < p1``），少一次字符串比较就少一处能写错的地方。
    """

    #: P0 —— L1 命中的**红线**事件。必须插队，谁都不许挡它。
    P0_RED_LINE = 0

    #: P1 —— 连续 ``DRIFT`` 漂移判定，以及非红线的危险判定。
    P1_DRIFT = 1

    #: P2 —— 日常的 ``ON_TRACK`` 摘要提取 / 人话解释。
    P2_ROUTINE = 2


#: P0 的短别名（写起来顺口）。
PRIORITY_P0 = Priority.P0_RED_LINE

#: P1 的短别名。
PRIORITY_P1 = Priority.P1_DRIFT

#: P2 的短别名。
PRIORITY_P2 = Priority.P2_ROUTINE

#: 已入队（凭据状态，还不是最终结论）。
STATUS_QUEUED = "queued"

#: 正常跑完。
STATUS_COMPLETED = "completed"

#: 因背压被丢弃。
STATUS_DROPPED = "dropped"

#: 协作式取消 —— 任务自己收手了。
STATUS_CANCELLED = "cancelled"

#: 强制抢占 —— 任务没配合，被放弃。
STATUS_PREEMPTED = "preempted"

#: 任务抛了异常。
STATUS_FAILED = "failed"

#: 抢占时写进降级原因里的那句话。
CANCEL_REASON_PREEMPTED = "被更高优先级任务抢占"

#: 红线严重度（与 :mod:`src.rules.basic` 的 ``SEVERITY_RED_LINE`` 一致）。
_SEVERITY_RED_LINE = "red_line"

#: 危险判定名（与 :mod:`src.rules.basic` 的 ``VERDICT_DANGEROUS`` 一致）。
_VERDICT_DANGEROUS = "DANGEROUS"

#: 档位的人话标签 —— 统计字典的**键名**（字符串，方便直接 JSON 化）。
PRIORITY_LABELS: Dict[Priority, str] = {
    PRIORITY_P0: "P0_RED_LINE",
    PRIORITY_P1: "P1_DRIFT",
    PRIORITY_P2: "P2_ROUTINE",
}


class TaskCancelled(RuntimeError):
    """任务主动响应取消 —— 抛出它表示「我知道该收手了」。

    任务在协作式取消时抛出本异常，队列会把状态记成
    :data:`STATUS_CANCELLED` 而不是 :data:`STATUS_FAILED`。
    """


class CancelToken:
    """取消令牌 —— 正在跑的任务自己轮询它。

    为什么要有它
    ===========

    Python **杀不掉线程**。所以「取消一个正在跑的模型调用」
    只有两条路：

    1. **协作式** —— 任务自己发现「该收手了」，主动抛
       :class:`TaskCancelled`（本类提供）；
    2. **强制放弃** —— 队列不等了，把结果丢掉（见模块文档）。

    本类负责第 1 条路：把「要取消了」这件事**可靠地**送到任务手里。
    """

    def __init__(self) -> None:
        self._event = threading.Event()
        self._reason = ""
        self._lock = threading.Lock()

    def cancel(self, reason: str = "") -> bool:
        """请求取消。

        :param reason: 取消原因（人话）。
        :return: 这次调用**真的**把取消信号发出去了返回 ``True``；
            之前已经取消过返回 ``False``。
        """
        with self._lock:
            if self._event.is_set():
                return False
            self._reason = str(reason or "")
            self._event.set()
            return True

    @property
    def cancelled(self) -> bool:
        """是不是已经被要求取消了。"""
        return self._event.is_set()

    @property
    def reason(self) -> str:
        """取消原因（人话）。"""
        with self._lock:
            return self._reason

    def wait(self, timeout: Optional[float] = None) -> bool:
        """等取消信号（任务可以用它来睡到被取消为止）。

        :param timeout: 最长等多少秒；``None`` 表示一直等。
        :return: 收到取消信号返回 ``True``；超时返回 ``False``。
        """
        return self._event.wait(timeout)

    def raise_if_cancelled(self) -> None:
        """被取消了就抛 :class:`TaskCancelled` —— 任务自查用。"""
        if self._event.is_set():
            raise TaskCancelled(self.reason or "任务已被取消")


@dataclass(frozen=True)
class QueueOutcome:
    """一次任务的**结果凭据**。

    投递时它表示「收下了还是丢了」，跑完后它表示「结论是什么」。

    :param task_id: 任务 ID。
    :param priority: 档位。
    :param status: 状态（``queued`` / ``completed`` / ``dropped`` /
        ``cancelled`` / ``preempted`` / ``failed``）。
    :param value: 任务的返回值（只有 ``completed`` 时有意义）。
    :param error: 失败原因（异常类型 + 文本）。
    :param reason: 人话原因（降级、丢弃、抢占都用它）。
    :param degraded: 是不是走了降级路径。
    :param wait_ms: **等待延迟**（入队 → 开跑，毫秒）——
        验收要看的「P0 平均等待延迟」就是它。
    :param run_ms: 执行耗时（开跑 → 结束，毫秒）。
    """

    task_id: str
    priority: Priority
    status: str
    value: Any = None
    error: str = ""
    reason: str = ""
    degraded: bool = False
    wait_ms: int = 0
    run_ms: int = 0

    @property
    def accepted(self) -> bool:
        """这个任务是不是**被收下了**（没被背压丢弃）。"""
        return self.status != STATUS_DROPPED

    @property
    def ok(self) -> bool:
        """是不是正常跑完了。"""
        return self.status == STATUS_COMPLETED

    def __bool__(self) -> bool:
        """``if queue.submit(...)`` 直接读「收下了没有」。"""
        return self.accepted


@dataclass
class _QueueTask:
    """队列里的一个任务（内部用）。"""

    task_id: str
    priority: Priority
    payload: Dict[str, Any]
    submitted_at: float
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    wait_ms: int = 0
    run_ms: int = 0
    token: Optional[CancelToken] = None


def classify_priority(
    verdict: Optional[Dict[str, Any]] = None,
    *,
    drift_streak: int = 0,
    drift_threshold: int = 1,
) -> Priority:
    """把一次判定折算成队列档位。

    判定口径（与哨兵现有语义一致）：

    * **红线**（``severity == "red_line"``）→ :data:`PRIORITY_P0`；
    * **非红线的危险判定**（``verdict == "DANGEROUS"``）→ :data:`PRIORITY_P1`
      —— 它是「有问题但不是红线」，所以比红线低一档；
    * **连续漂移**（连续次数达到阈值）→ :data:`PRIORITY_P1`；
    * 其余（``ON_TRACK``、摘要提取、人话解释）→ :data:`PRIORITY_P2`。

    :param verdict: L1 判定字典（或漂移结论字典）。
    :param drift_streak: 当前连续漂移次数。
    :param drift_threshold: 连续几次漂移算「要处理了」。
    :return: 档位。
    """
    data = verdict if isinstance(verdict, dict) else {}
    severity = str(data.get("severity") or "").strip().lower()
    name = str(data.get("verdict") or "").strip().upper()
    streak = int(data.get("drift_streak") or drift_streak or 0)
    threshold = int(drift_threshold or 0)

    if severity == _SEVERITY_RED_LINE:
        return PRIORITY_P0
    if name == _VERDICT_DANGEROUS:
        return PRIORITY_P1
    if threshold > 0 and streak >= threshold:
        return PRIORITY_P1
    if bool(data.get("drift")):
        return PRIORITY_P1
    return PRIORITY_P2


class PriorityTaskQueue:
    """单并发引擎上的优先级队列 —— **同档内**先来先服务，**跨档**严格插队。

    :param handler: 处理函数。两种签名都认：

        * ``handler(payload)`` —— 简单形式，拿不到取消令牌；
        * ``handler(payload, token)`` —— 协作式形式，
          任务可以轮询 :class:`CancelToken` 及时收手。

    :param on_result: 结论回调，签名 ``(task_id, QueueOutcome) -> None``。
        **每一次任务都有回调** —— 包括被丢弃的（降级结果）、
        被抢占的（``preempted``）、失败的（``failed``）。
    :param max_pending: 队列容量（待处理任务数上限）。
        满了就丢 P2。
    :param preempt_after_seconds: 正在跑的任务**超过**这个秒数，
        且有更高档位在等，才允许抢占。默认 10 秒。
    :param cancel_grace_seconds: 发出取消信号后，等任务自己收手的秒数。
        超时就**强制放弃**。
    :param poll_interval: 抢占检查的轮询间隔（秒）。
    :param name: 后台线程名前缀。
    """

    def __init__(
        self,
        handler: Optional[Callable[..., Any]] = None,
        on_result: Optional[Callable[[str, QueueOutcome], None]] = None,
        *,
        max_pending: int = 10,
        preempt_after_seconds: float = 10.0,
        cancel_grace_seconds: float = 0.5,
        poll_interval: float = 0.02,
        name: str = "sentinel-priority",
    ) -> None:
        self.handler = handler
        self.on_result = on_result
        self.max_pending = max(int(max_pending or 0), 1)
        self.preempt_after_seconds = max(float(preempt_after_seconds or 0), 0.0)
        self.cancel_grace_seconds = max(float(cancel_grace_seconds or 0), 0.0)
        self.poll_interval = max(float(poll_interval or 0), 0.001)
        self.name = str(name or "sentinel-priority")

        #: 处理函数是否接受取消令牌。
        self._handler_wants_token = _accepts_token(handler)

        #: 三条待处理队列（每档一条，同档 FIFO）。
        self._queues: Dict[Priority, Deque[_QueueTask]] = {
            PRIORITY_P0: deque(),
            PRIORITY_P1: deque(),
            PRIORITY_P2: deque(),
        }

        #: 队列状态锁（``Condition`` 自带 ``RLock``，可重入）。
        self._cond = threading.Condition(threading.RLock())

        #: 后台线程（空闲时**自动退出** —— 释放资源）。
        self._worker: Optional[threading.Thread] = None

        #: 正在跑的任务。
        self._current: Optional[_QueueTask] = None

        #: 还没出结论的任务数（``drain`` 靠它）。
        self._outstanding = 0

        #: 停止标志。
        self._stop = threading.Event()

        # --- 计数器（一切降级都要记账） ---
        self._submitted = 0
        self._accepted = 0
        self._dropped = 0
        self._completed = 0
        self._cancelled = 0
        self._preempted = 0
        self._failed = 0
        self._abandoned = 0

        #: 分档位的等待延迟样本（毫秒）。
        self._waits: Dict[Priority, List[int]] = {
            PRIORITY_P0: [],
            PRIORITY_P1: [],
            PRIORITY_P2: [],
        }

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------
    def submit(
        self,
        task_id: str,
        payload: Optional[Dict[str, Any]] = None,
        priority: Priority = PRIORITY_P2,
    ) -> QueueOutcome:
        """投递一个任务。

        **背压**：队列里待处理的任务数达到 ``max_pending`` 时，
        新来的 **P2 直接丢弃**，并把降级结果回调出去 ——
        日常摘要不值得占着显存。

        **P0 / P1 永不因背压被丢** —— 宁可排队也不能把红线弄丢。

        :param task_id: 任务 ID（回调时原样带回）。
        :param payload: 任务载荷。
        :param priority: 档位。
        :return: 投递凭据 —— 入队返回 :data:`STATUS_QUEUED`，
            丢弃返回 :data:`STATUS_DROPPED`（``degraded=True``）。
        """
        prio = _coerce_priority(priority)
        body = dict(payload) if isinstance(payload, dict) else {}
        now = time.monotonic()
        outcome: QueueOutcome
        dropped = False

        with self._cond:
            self._submitted += 1
            if prio == PRIORITY_P2 and self._pending_locked() >= self.max_pending:
                # ⚠️ 背压 —— 只丢 P2，红线与漂移一律照收。
                self._dropped += 1
                dropped = True
                outcome = QueueOutcome(
                    task_id=str(task_id),
                    priority=prio,
                    status=STATUS_DROPPED,
                    reason=(
                        "队列已满（{}/{}），低优先级任务被丢弃以保显存".format(
                            self._pending_locked(), self.max_pending
                        )
                    ),
                    degraded=True,
                )
            else:
                task = _QueueTask(
                    task_id=str(task_id),
                    priority=prio,
                    payload=body,
                    submitted_at=now,
                )
                self._queues[prio].append(task)
                self._accepted += 1
                self._outstanding += 1
                outcome = QueueOutcome(
                    task_id=str(task_id),
                    priority=prio,
                    status=STATUS_QUEUED,
                )
                self._cond.notify_all()

        if dropped:
            # ⚠️ 丢弃**不是沉默** —— 降级结果照样给出去。
            self._deliver(str(task_id), outcome)
            return outcome

        self._ensure_worker()
        return outcome

    def drain(self, timeout: float = 30.0) -> bool:
        """等所有**已收下**的任务出结论。

        :param timeout: 最长等多少秒。
        :return: 全部完成返回 ``True``；超时返回 ``False``。
        """
        deadline = time.monotonic() + max(float(timeout), 0.0)
        with self._cond:
            while self._outstanding > 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._cond.wait(min(remaining, 0.05))
            return True

    def stop(self, timeout: float = 5.0) -> None:
        """停掉后台线程（并取消正在跑的任务）。

        :param timeout: 等线程退出的秒数。
        """
        self._stop.set()
        with self._cond:
            current = self._current
            self._cond.notify_all()
        if current is not None and current.token is not None:
            current.token.cancel("队列正在停止")
        thread = self._worker
        if thread is not None:
            thread.join(timeout=max(float(timeout), 0.0))
        with self._cond:
            if self._worker is thread:
                self._worker = None

    def release(self) -> bool:
        """释放空闲资源（后台线程）。

        队列清空后后台线程**本来就会自动退出**；
        这个方法是给「想提前收回线程」用的。

        :return: 确实释放掉了（有线程被停掉）返回 ``True``。
        """
        with self._cond:
            if self._outstanding > 0 or self._current is not None:
                return False
            thread = self._worker
            self._worker = None
        if thread is not None:
            thread.join(timeout=1.0)
            return True
        return False

    @property
    def pending(self) -> int:
        """队列里**待处理**的任务数。"""
        with self._cond:
            return self._pending_locked()

    @property
    def running(self) -> bool:
        """现在是不是有任务在跑。"""
        with self._cond:
            return self._current is not None

    @property
    def idle(self) -> bool:
        """队列是不是**彻底空了**（没待处理、没在跑、没线程）。"""
        with self._cond:
            return (
                self._pending_locked() == 0
                and self._current is None
                and (self._worker is None or not self._worker.is_alive())
            )

    def stats(self) -> Dict[str, Any]:
        """统计（看板与验收用）。

        重点是 ``wait_ms`` —— **分档位**的等待延迟统计：
        ``avg`` 是平均等待毫秒，``max`` 是最久的一次。
        「P0 的平均等待延迟」就是 ``wait_ms["P0_RED_LINE"]["avg"]``。
        键名是**字符串**（见 :data:`PRIORITY_LABELS`），方便直接 JSON 化。

        :return: 统计字典。
        """
        with self._cond:
            waits = {
                PRIORITY_LABELS[key]: _latency_summary(values)
                for key, values in self._waits.items()
            }
            return {
                "submitted": self._submitted,
                "accepted": self._accepted,
                "dropped": self._dropped,
                "completed": self._completed,
                "cancelled": self._cancelled,
                "preempted": self._preempted,
                "failed": self._failed,
                "abandoned": self._abandoned,
                "pending": self._pending_locked(),
                "outstanding": self._outstanding,
                "max_pending": self.max_pending,
                "preempt_after_seconds": self.preempt_after_seconds,
                "wait_ms": waits,
                "running": self._current is not None,
                "worker_alive": bool(self._worker and self._worker.is_alive()),
            }

    def __enter__(self) -> "PriorityTaskQueue":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.stop()

    # ------------------------------------------------------------------
    # 内部实现 —— 后台线程
    # ------------------------------------------------------------------
    def _ensure_worker(self) -> None:
        """懒启动后台线程（**守护线程**）。"""
        with self._cond:
            if self._worker is not None and self._worker.is_alive():
                return
            self._worker = threading.Thread(
                target=self._worker_loop,
                name=self.name,
                daemon=True,
            )
            self._worker.start()

    def _worker_loop(self) -> None:
        """后台线程主体 —— 一次只跑一个任务（单并发的忠实模拟）。"""
        while not self._stop.is_set():
            task = self._take_next()
            if task is None:
                # 队列空了 —— **释放资源**，线程退出。
                # 下次投递时再懒启动。
                return
            self._execute(task)

    def _take_next(self) -> Optional[_QueueTask]:
        """取下一个任务 —— **永远取最高档位**（P0 插队就在这里实现）。

        :return: 取到返回任务；队列空了返回 ``None``。
        """
        with self._cond:
            while True:
                if self._stop.is_set():
                    return None
                task = self._pop_locked()
                if task is not None:
                    return task
                # 队列空了 —— 释放资源并退出（下次投递再懒启动）。
                self._worker = None
                self._cond.notify_all()
                return None

    def _pop_locked(self) -> Optional[_QueueTask]:
        """弹出最高档位的任务（**调用方必须持有锁**）。"""
        for prio in (PRIORITY_P0, PRIORITY_P1, PRIORITY_P2):
            queue = self._queues[prio]
            if queue:
                return queue.popleft()
        return None

    def _execute(self, task: _QueueTask) -> None:
        """跑一个任务 —— 可被更高档位抢占。"""
        token = CancelToken()
        task.token = token
        done = threading.Event()
        box: Dict[str, Any] = {}

        def _runner() -> None:
            try:
                box["value"] = self._invoke(task.payload, token)
            except BaseException as exc:  # noqa: BLE001 —— 失败也要记账
                box["error"] = exc
            finally:
                done.set()

        started = time.monotonic()
        task.started_at = started
        thread = threading.Thread(
            target=_runner,
            name="{}:{}".format(self.name, task.task_id),
            daemon=True,
        )
        with self._cond:
            self._current = task
        thread.start()

        abandoned = False
        while not done.wait(self.poll_interval):
            if self._stop.is_set():
                # 队列正在停止 —— 也要给任务一个收手的机会。
                token.cancel("队列正在停止")
                if done.wait(self.cancel_grace_seconds):
                    break
                abandoned = True
                break
            if self._should_preempt(task):
                # ⚠️ 先礼：给任务一个自己收手的机会。
                token.cancel(CANCEL_REASON_PREEMPTED)
                if done.wait(self.cancel_grace_seconds):
                    break
                # 后兵：它不配合 —— **强制放弃**，把位置腾给更高档位。
                abandoned = True
                logger.warning(
                    "任务 %s（%s）超时未响应取消，已强制抢占",
                    task.task_id,
                    task.priority.name,
                )
                break

        finished = time.monotonic()
        task.finished_at = finished
        task.wait_ms = int((started - task.submitted_at) * 1000)
        task.run_ms = int((finished - started) * 1000)

        with self._cond:
            self._current = None

        error = box.get("error")
        if abandoned:
            status = STATUS_PREEMPTED
            value: Any = None
            reason = CANCEL_REASON_PREEMPTED
            degraded = True
        elif isinstance(error, TaskCancelled):
            status = STATUS_CANCELLED
            value = None
            reason = str(error) or "任务响应取消并自行收手"
            degraded = True
        elif error is not None:
            status = STATUS_FAILED
            value = None
            reason = "{}: {}".format(type(error).__name__, error)
            degraded = True
        elif token.cancelled:
            status = STATUS_CANCELLED
            value = box.get("value")
            reason = token.reason or "任务被取消"
            degraded = True
        else:
            status = STATUS_COMPLETED
            value = box.get("value")
            reason = ""
            degraded = False

        self._finish(task, status, value, reason, degraded)

    def _should_preempt(self, task: _QueueTask) -> bool:
        """这个正在跑的任务，现在该不该被抢走。

        条件**两个都要满足**：

        1. 已经有**更高档位**的任务在排队（没人等，抢它没有意义）；
        2. 它自己已经跑了**超过** ``preempt_after_seconds``
           （跑得不久的任务就别抢了 —— 抢了反而更亏）。

        :param task: 正在跑的任务。
        :return: 该抢返回 ``True``。
        """
        if task.started_at is None:
            return False
        elapsed = time.monotonic() - task.started_at
        if elapsed < self.preempt_after_seconds:
            return False
        with self._cond:
            for prio, queue in self._queues.items():
                if prio < task.priority and queue:
                    return True
        return False

    def _invoke(self, payload: Dict[str, Any], token: CancelToken) -> Any:
        """调用处理函数（认两种签名）。"""
        if self.handler is None:
            raise RuntimeError("队列没有绑定处理函数")
        if self._handler_wants_token:
            return self.handler(payload, token)
        return self.handler(payload)

    def _finish(
        self,
        task: _QueueTask,
        status: str,
        value: Any,
        reason: str,
        degraded: bool,
    ) -> None:
        """记账 + 交出结论。"""
        with self._cond:
            self._outstanding -= 1
            if status == STATUS_COMPLETED:
                self._completed += 1
            elif status == STATUS_DROPPED:
                self._dropped += 1
            elif status == STATUS_CANCELLED:
                self._cancelled += 1
            elif status == STATUS_PREEMPTED:
                self._preempted += 1
                self._abandoned += 1
            elif status == STATUS_FAILED:
                self._failed += 1
            self._waits[task.priority].append(task.wait_ms)
            self._cond.notify_all()

        self._deliver(
            task.task_id,
            QueueOutcome(
                task_id=task.task_id,
                priority=task.priority,
                status=status,
                value=value,
                error=reason if status == STATUS_FAILED else "",
                reason=reason,
                degraded=degraded,
                wait_ms=task.wait_ms,
                run_ms=task.run_ms,
            ),
        )

    def _deliver(self, task_id: str, outcome: QueueOutcome) -> None:
        """把结论交给回调（**回调失败绝不影响队列**）。"""
        if self.on_result is None:
            return
        try:
            self.on_result(task_id, outcome)
        except Exception as exc:  # noqa: BLE001 —— 回调失败不影响审查
            logger.warning(
                "队列结果回调失败（已隔离）：%s: %s", type(exc).__name__, exc
            )

    def _pending_locked(self) -> int:
        """待处理任务总数（**调用方必须持有锁**）。"""
        return sum(len(queue) for queue in self._queues.values())


# ----------------------------------------------------------------------
# 辅助
# ----------------------------------------------------------------------
def _coerce_priority(value: Any) -> Priority:
    """把各种写法折算成 :class:`Priority`。

    认 ``Priority`` 成员、整数（``0`` / ``1`` / ``2``），
    以及 ``"P0"`` / ``"p0_red_line"`` / ``"red_line"`` 这类字符串。

    :param value: 待折算的值。
    :return: 档位；认不出来按 :data:`PRIORITY_P2` 处理（最保守）。
    """
    if isinstance(value, Priority):
        return value
    if isinstance(value, int):
        try:
            return Priority(value)
        except ValueError:
            return PRIORITY_P2
    text = str(value or "").strip().lower()
    if not text:
        return PRIORITY_P2
    if text in ("0", "p0", "p0_red_line", "red_line", "redline"):
        return PRIORITY_P0
    if text in ("1", "p1", "p1_drift", "drift"):
        return PRIORITY_P1
    return PRIORITY_P2


def _accepts_token(handler: Optional[Callable[..., Any]]) -> bool:
    """判断处理函数是不是接受取消令牌。

    :param handler: 处理函数。
    :return: 接受两个位置参数返回 ``True``。
    """
    if handler is None:
        return False
    try:
        signature = inspect.signature(handler)
    except (TypeError, ValueError):  # 某些内置函数签名取不到
        return False
    positional = 0
    for parameter in signature.parameters.values():
        if parameter.kind is inspect.Parameter.VAR_POSITIONAL:
            return True
        if parameter.kind in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        ):
            positional += 1
    return positional >= 2


def _latency_summary(values: List[int]) -> Dict[str, Any]:
    """把一组延迟样本压成统计。

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
