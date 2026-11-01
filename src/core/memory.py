"""动态记忆管理 —— 短期滑窗 + 后台滚动摘要（v3.5.0）。

为什么需要
==========

128K 上下文**不是垃圾桶**。

把整段会话原样塞进 Prompt，后果是：

* **又慢又贵**：每一次判定都要重新读一遍越来越长的历史；
* **重点被淹没**：真正该看的那三步，淹没在两百步里；
* **前缀缓存失效**：Prompt 一变，缓存就作废 ——
  本地模型那点算力全浪费在「重新读一遍」上。

所以要**主动管理记忆**：

短期记忆
========

每个线程只保留**最近 3-5 步**的完整原文。

⚠️ 「Tool Call + Result 作为**原子对**，不可拆分」
------------------------------------------------

这一步是硬要求，理由很实在：

``tool_call``（要执行 ``rm -rf build``）与随后的 ``tool_result``
（``deleted 3 files``）是**同一件事的两半**。

如果窗口在它们中间截断，模型看到的就是：

* 只有 ``rm -rf build``，没有回执 → 误判成「命令失败了，Agent 在重试」；
* 只有 ``deleted 3 files``，没有命令 → 根本不知道发生了什么。

所以窗口的容量以「**步**」计，而**一步**是「一个调用（连同它的回执）」，
挤出去的时候**整步一起走**。

长期记忆
========

被挤出窗口的事件不是直接丢掉 —— 它们累积到一定数量（比如 10 条）时，
在**后台异步**触发一次摘要总结：

.. code-block:: text

    新摘要 = LLM(旧摘要 + 新挤出来的事件)

更新后的摘要作为「**历史背景**」放在 Prompt 的**开头**。

⚠️ 三个刻意的设计
----------------

1. **后台异步** —— 摘要绝不在主循环里同步做。
   模型慢的时候主循环照常读日志（与 v0.8.0 的漂移判定同一条纪律）。
2. **摘要失败不丢历史** —— 失败的摘要**不会**把旧摘要清掉，
   累积的事件也留着，下次再试。
3. **不引依赖** —— 本模块**只用标准库**。
   摘要用哪个模型由调用方用「一个可调用对象」注入。
"""

from __future__ import annotations

import logging
import queue
import threading
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_LONG_TERM_THRESHOLD",
    "DEFAULT_SHORT_TERM_STEPS",
    "LongTermMemory",
    "ShortTermMemory",
    "StreamMemory",
]

#: 短期记忆保留的**步**数（默认 4 步，落在用户要的 3-5 区间中间）。
DEFAULT_SHORT_TERM_STEPS = 4

#: 长期记忆触发摘要的**累计事件数**阈值。
DEFAULT_LONG_TERM_THRESHOLD = 10

#: 事件类型：工具调用。
EVENT_TOOL_CALL = "tool_call"

#: 事件类型：工具回执。
EVENT_TOOL_RESULT = "tool_result"


class _Unit:
    """窗口里的「一步」—— 一个调用连同它的回执（**不可拆分**）。"""

    __slots__ = ("closed", "events")

    def __init__(self, event: Dict[str, Any]) -> None:
        self.events: List[Dict[str, Any]] = [event]
        #: 已经配上了回执（配上了就不该再往里塞别的）。
        self.closed = False

    @property
    def kind(self) -> str:
        """这一步的事件类型（取第一个事件的）。"""
        if not self.events:
            return ""
        return str(self.events[0].get("type") or "")

    def __len__(self) -> int:
        """这一步包含几个事件。"""
        return len(self.events)


class ShortTermMemory:
    """短期记忆 —— 最近 N 步的完整原文。

    :param capacity: 保留多少**步**（默认 4）。
    """

    def __init__(self, capacity: int = DEFAULT_SHORT_TERM_STEPS) -> None:
        self.capacity = max(int(capacity or 0), 1)
        self._units: List[_Unit] = []
        self._lock = threading.Lock()
        #: 被挤出窗口的事件（交给长期记忆）。
        self._evicted: List[Dict[str, Any]] = []
        #: 总共挤出去过多少事件。
        self.evicted_count = 0

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------
    def observe(self, event: Dict[str, Any]) -> List[Dict[str, Any]]:
        """吃进一个事件，返回**这次被挤出去**的事件。

        ⚠️ 「原子对」的判定：紧跟在 ``tool_call`` 后面的
        ``tool_result`` 会被**合并进同一步**，
        挤出去的时候也是**一起**走 —— 绝不会只挤走一半。

        :param event: 统一事件字典。
        :return: 被挤出窗口的事件列表（没有就是空列表）。
        """
        if not isinstance(event, dict):
            return []
        with self._lock:
            event_type = str(event.get("type") or "")
            if (
                event_type == EVENT_TOOL_RESULT
                and self._units
                and self._units[-1].kind == EVENT_TOOL_CALL
                and not self._units[-1].closed
            ):
                # 同一个动作的回执 —— 合并进同一步。
                self._units[-1].events.append(event)
                self._units[-1].closed = True
                return []

            self._units.append(_Unit(event))
            evicted: List[Dict[str, Any]] = []
            while len(self._units) > self.capacity:
                old = self._units.pop(0)
                evicted.extend(old.events)
            if evicted:
                self._evicted.extend(evicted)
                self.evicted_count += len(evicted)
            return evicted

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------
    def steps(self) -> int:
        """当前窗口里有几**步**。"""
        with self._lock:
            return len(self._units)

    def events(self) -> List[Dict[str, Any]]:
        """当前窗口里的**完整原文**（按时间顺序）。

        :return: 事件列表。
        """
        with self._lock:
            return [
                event
                for unit in self._units
                for event in unit.events
            ]

    def summary(self) -> str:
        """把窗口压成一份**紧凑摘要**（给 Prompt 用）。

        :return: 多行文本；窗口为空就是空串。
        """
        with self._lock:
            units = list(self._units)
        lines = []
        for index, unit in enumerate(units, start=1):
            tool = unit.events[0].get("tool") if unit.events else None
            tool = tool if isinstance(tool, dict) else {}
            name = str(tool.get("name") or "unknown")
            command = str(tool.get("command") or "").strip()
            line = "第{}步 [{}] {}".format(
                index, name, command or "（无命令文本）"
            )
            extra = []
            for event in unit.events[1:]:
                result = str(event.get("text") or "").strip()
                if result:
                    extra.append("→ " + result[:120])
            if extra:
                line = line + " " + " ".join(extra)
            lines.append(line)
        return "\n".join(lines)

    def drain_evicted(self) -> List[Dict[str, Any]]:
        """取走并清空「被挤出去的事件」。

        :return: 事件列表。
        """
        with self._lock:
            evicted = list(self._evicted)
            self._evicted.clear()
            return evicted

    def reset(self) -> None:
        """清空窗口与待汇总事件。"""
        with self._lock:
            self._units.clear()
            self._evicted.clear()


class LongTermMemory:
    """长期记忆 —— 后台滚动摘要。

    摘要用**注入**的函数完成（本模块不引任何 LLM 依赖）：

    .. code-block:: python

        def summarize(old: str, events: list) -> str:
            ...

    :param summarizer: 摘要函数 ``(旧摘要, 新事件) -> 新摘要``；
        为 ``None`` 时**只累积不摘要**。
    :param threshold: 累积多少条事件触发一次摘要（默认 10）。
    """

    def __init__(
        self,
        summarizer: Optional[Callable[[str, List[Dict[str, Any]]], str]] = None,
        threshold: int = DEFAULT_LONG_TERM_THRESHOLD,
    ) -> None:
        self.summarizer = summarizer
        self.threshold = max(int(threshold or 0), 1)
        self._pending: List[Dict[str, Any]] = []
        self._lock = threading.Lock()
        #: 当前摘要（放在 Prompt 的**开头**当「历史背景」）。
        self._summary = ""
        self._queue: "queue.Queue[List[Dict[str, Any]]]" = queue.Queue()
        self._worker: Optional[threading.Thread] = None
        self._stop = threading.Event()
        #: 摘要成功了几次。
        self.summaries = 0
        #: 摘要失败了几次（失败**不丢历史**）。
        self.failures = 0
        #: 最近一次失败原因。
        self.last_error = ""

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------
    @property
    def summary(self) -> str:
        """当前的历史背景摘要。"""
        with self._lock:
            return self._summary

    @property
    def pending_count(self) -> int:
        """还没被摘要掉的事件数。"""
        with self._lock:
            return len(self._pending)

    def push(self, events: List[Dict[str, Any]]) -> bool:
        """把被挤出的事件交给长期记忆。

        累积到阈值就**异步**触发一次摘要。

        :param events: 事件列表。
        :return: 触发了摘要返回 ``True``；
            没有摘要函数（只累积不摘要）时返回 ``False``。
        """
        if not events:
            return False
        if self.summarizer is None:
            # ⚠️ 没有摘要函数就**只累积** —— 绝不假装触发了摘要。
            with self._lock:
                self._pending.extend(events)
            return False
        with self._lock:
            self._pending.extend(events)
            should = len(self._pending) >= self.threshold
        if not should:
            return False
        self._ensure_worker()
        try:
            self._queue.put_nowait(events)
        except queue.Full:  # pragma: no cover —— 队列无界，走不到这里
            return False
        return True

    def stop(self, timeout: float = 5.0) -> None:
        """停掉后台摘要线程。

        :param timeout: 等待线程退出的秒数。
        """
        self._stop.set()
        thread = self._worker
        if thread is not None:
            thread.join(timeout=max(float(timeout), 0.0))
            self._worker = None

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------
    def _ensure_worker(self) -> None:
        """懒启动后台摘要线程（**守护线程**）。"""
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                return
            self._worker = threading.Thread(
                target=self._worker_loop,
                name="sentinel-memory-summary",
                daemon=True,
            )
            self._worker.start()

    def _worker_loop(self) -> None:
        """后台线程主体：取事件 → 调摘要函数 → 更新摘要。"""
        while not self._stop.is_set():
            try:
                batch = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                self._summarize(batch)
            except Exception as exc:  # noqa: BLE001 —— 摘要失败不影响审查
                self.failures += 1
                self.last_error = "{}: {}".format(type(exc).__name__, exc)
                logger.warning(
                    "后台滚动摘要失败（已降级，历史保留）：%s: %s",
                    type(exc).__name__,
                    exc,
                )

    def _summarize(self, batch: List[Dict[str, Any]]) -> None:
        """做一次摘要并更新。

        ⚠️ 失败时**不清空**待汇总事件 ——
        摘要是锦上添花，历史丢了才是真丢。

        :param batch: 这一批新事件。
        """
        if self.summarizer is None:
            return
        with self._lock:
            old = self._summary
            pending = list(self._pending)
        text = str(self.summarizer(old, pending) or "").strip()
        if not text:
            self.failures += 1
            self.last_error = "摘要函数返回了空串"
            return
        with self._lock:
            self._summary = text
            self._pending.clear()
            self.summaries += 1
        logger.info("长期记忆摘要已更新（第 %s 次）。", self.summaries)


class StreamMemory:
    """**一路** Agent 的记忆 —— 短期滑窗 + 长期背景。

    每个线程指纹一路一份（v3.5.0 的多路隔离），
    所以 A 路的历史**绝不会**进 B 路的窗口。

    :param capacity:  短期保留多少步。
    :param threshold: 长期摘要的触发阈值。
    :param summarizer: 摘要函数；为 ``None`` 时只累积不摘要。
    """

    def __init__(
        self,
        *,
        capacity: int = DEFAULT_SHORT_TERM_STEPS,
        threshold: int = DEFAULT_LONG_TERM_THRESHOLD,
        summarizer: Optional[Callable[[str, List[Dict[str, Any]]], str]] = None,
    ) -> None:
        self.short = ShortTermMemory(capacity)
        self.long = LongTermMemory(summarizer, threshold)

    def observe(self, event: Dict[str, Any]) -> None:
        """吃进一个事件。

        被挤出短期窗口的事件会**自动**交给长期记忆。

        :param event: 统一事件字典。
        """
        evicted = self.short.observe(event)
        if evicted:
            self.long.push(evicted)

    def window(self) -> str:
        """短期窗口的紧凑摘要。

        :return: 多行文本。
        """
        return self.short.summary()

    def background(self) -> str:
        """长期记忆的「历史背景」。

        :return: 摘要文本；还没有就是空串。
        """
        return self.long.summary

    def context(self) -> str:
        """拼出给 Prompt 用的上下文（**背景在前，窗口在后**）。

        ⚠️ 顺序有讲究：历史背景放**开头**，
        静态前缀放最前面 —— 这样前缀才能被缓存命中。

        :return: 上下文文本；什么都没有就是空串。
        """
        parts = []
        background = self.background()
        if background:
            parts.extend(["【历史背景】", background, ""])
        window = self.window()
        if window:
            parts.extend(["【最近步骤】", window])
        return "\n".join(parts).strip()

    def reset(self) -> None:
        """清空这一路的全部记忆。"""
        self.short.reset()
        self.long.stop()
