"""会话上下文：按指纹完全隔离的多路中枢（v5.0.0）。

为什么要有它
============

v4.x 的引擎已经能「按线程指纹分路」（``_intent_trackers``），
但那是**引擎内部**的一个字典 —— 目标、追踪器、记忆都挂在引擎上，
外部（面板、雷达、告警）拿到的仍是**引擎级**的单一目标。

v5.0.0 把这个概念**升格成一等公民**：一个会话 = 一个
:class:`SessionContext`，它**自己**拥有：

* ``goal`` —— 只属于这一路的目标（绝不串台）；
* ``rules`` —— 只属于这一路的 L1 规则引擎（``LoopDetector`` 是**有状态**的，
  共享就等于把 A 路的「读同一个文件三次」算到 B 路头上）；
* ``tailer`` —— 只跟这一路自己的日志文件；
* ``pid`` / ``cwd`` —— 进程身份。

**没有目标就休眠**
==================

``l2_sleeping`` 为真时，这一路的 **L2 意图漂移判定进入休眠** ——
不调本地模型、不排队、不占额度，只保留 **L1 纯规则拦截**。
理由：没有目标就没有「偏离」可言，去打扰模型纯属浪费。

⚠️ 这一条**不是** v5.0.0 新造的 —— ``src/llm/base.py`` 早就
「没有目标就没有『偏离』可言 —— 直接跳过，不打扰模型」。
v5.0.0 做的是把它**变成可观测的状态**（``/api/state`` 里能看见），
并把「目标从哪来」从「引擎的全局变量」改成「**这一路自己的**」。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

#: 指纹的最大长度 —— 太长的路径会把日志撑爆。
FINGERPRINT_MAX_CHARS = 200


def build_fingerprint(
    *,
    cwd: str = "",
    session_id: str = "",
    thread_id: str = "",
    pid: int = 0,
) -> str:
    """拼出会话指纹。

    指纹是**会话的唯一身份**：同一路的所有后续事件都靠它对上号。

    优先级（从强到弱）：

    1. ``thread_id`` —— 同一进程内的一路线程，最精确；
    2. ``session_id`` —— Agent 自己给的会话 ID；
    3. ``cwd`` —— 工作目录（不同项目天然隔离）；
    4. ``pid`` —— 最后的兜底。

    ⚠️ 指纹**只用于分区**，不是安全边界 —— 但它必须**稳定**：
    同一路的事件算出来的指纹必须一致，否则会分裂成两路、
    各自拿到半个滑窗，判定结果自然错乱。

    :param cwd: 工作目录。
    :param session_id: Agent 会话 ID。
    :param thread_id: 线程 ID。
    :param pid: 进程 ID。
    :return: 指纹字符串；一个都没有时返回空串。
    """
    parts = [
        str(thread_id or "").strip(),
        str(session_id or "").strip(),
        str(cwd or "").strip(),
        str(pid or "").strip(),
    ]
    parts = [part for part in parts if part]
    if not parts:
        return ""
    return "|".join(parts)[:FINGERPRINT_MAX_CHARS]


@dataclass
class SessionContext:
    """一个会话的**完全隔离**上下文。

    会话之间不共享任何可变状态 ——
    目标、规则引擎、跟读器、计数，全都各有一份。
    """

    #: 会话指纹（分区键）。
    fingerprint: str
    #: Agent 会话 ID（如果 Agent 给了）。
    session_id: str = ""
    #: 工作目录。
    cwd: str = ""
    #: 进程 ID。
    pid: int = 0
    #: Agent 类型。
    agent: str = ""
    #: **这一路自己的**目标 —— 空串就是「没目标」。
    goal: str = ""
    #: 被跟读的日志路径。
    log_path: str = ""
    #: 这一路自己的 L1 规则引擎（有状态，不能共享）。
    rules: Any = None
    #: 这一路自己的跟读器。
    tailer: Any = None
    #: 这一路自己的意图追踪器（可为 ``None`` —— 没启用 L2）。
    tracker: Any = None
    #: 事件计数。
    events: int = 0
    #: 命中红线次数。
    alerts: int = 0
    #: 最近一次判定结果。
    last_verdict: str = ""
    #: 最近一次判定理由。
    last_reason: str = ""
    #: 创建时刻。
    created_at: float = 0.0
    #: 最近一次活动时刻。
    updated_at: float = 0.0
    #: 这一路的附加数据。
    extra: Dict[str, Any] = field(default_factory=dict)

    @property
    def l2_sleeping(self) -> bool:
        """这一路的 L2 是不是**休眠**中。

        没有目标 → 休眠（不调本地模型），只留 L1 规则拦截。

        :return: 休眠中返回 ``True``。
        """
        return not str(self.goal or "").strip()

    def bind_goal(self, goal: str) -> bool:
        """给这一路绑定目标。

        ⚠️ 只改**自己**的目标 —— 别的路不受影响。

        :param goal: 目标文本；空串等于解绑（回到休眠）。
        :return: 目标是否发生了变化。
        """
        text = str(goal or "").strip()
        if text == str(self.goal or "").strip():
            return False
        self.goal = text
        return True

    def to_dict(self) -> Dict[str, Any]:
        """转成可 JSON 化的快照。

        :return: 快照字典。
        """
        return {
            "fingerprint": self.fingerprint,
            "session_id": self.session_id,
            "cwd": self.cwd,
            "pid": self.pid,
            "agent": self.agent,
            "goal": self.goal,
            "log_path": self.log_path,
            "l2_sleeping": self.l2_sleeping,
            "events": self.events,
            "alerts": self.alerts,
            "last_verdict": self.last_verdict,
            "last_reason": self.last_reason,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class SessionRegistry:
    """会话登记表 —— 按指纹维护**完全隔离**的上下文。

    线程安全：多路 Agent 同时进来时，
    建会话、读会话、删会话都要串行化。
    """

    def __init__(self) -> None:
        """建一个空登记表。"""
        self._sessions: Dict[str, SessionContext] = {}
        self._lock = threading.Lock()

    def get_or_create(
        self,
        fingerprint: str,
        **fields: Any,
    ) -> SessionContext:
        """按指纹取会话；没有就建一个。

        同一指纹**重复调用不会重建** —— 那会把已有的滑窗清空，
        等于把这一路的历史抹掉。

        :param fingerprint: 会话指纹。
        :param fields: 建会话时的初始字段（已存在时忽略）。
        :return: 会话上下文。
        """
        key = str(fingerprint or "")
        with self._lock:
            existing = self._sessions.get(key)
            if existing is not None:
                return existing
            context = SessionContext(fingerprint=key, **fields)
            self._sessions[key] = context
            return context

    def get(self, fingerprint: str) -> Optional[SessionContext]:
        """按指纹取会话。

        :param fingerprint: 会话指纹。
        :return: 会话；没有就是 ``None``。
        """
        with self._lock:
            return self._sessions.get(str(fingerprint or ""))

    def list(self) -> List[SessionContext]:
        """列出全部会话（按指纹排序，保证输出稳定）。

        :return: 会话列表。
        """
        with self._lock:
            return [
                self._sessions[key] for key in sorted(self._sessions)
            ]

    def remove(self, fingerprint: str) -> bool:
        """删掉一路会话。

        :param fingerprint: 会话指纹。
        :return: 真的删掉了返回 ``True``。
        """
        with self._lock:
            return self._sessions.pop(str(fingerprint or ""), None) is not None

    def snapshot(self) -> List[Dict[str, Any]]:
        """全部会话的快照。

        :return: 快照列表。
        """
        return [context.to_dict() for context in self.list()]
