"""级联模型路由 —— L1 规则 → L2 本地极速过滤 → L3 云端终审（v3.5.0）。

三级各司其职
============

========= ========================== ==================================
层级      做什么                      代价
========= ========================== ==================================
**L1**    正则规则层 —— 命中就拦      **零**（本地内存运算，微秒级）
**L2**    本地小模型（NInfer）——      本地算力，**不花钱、不出网**
          三值判定：还在做吗？          （但慢：单并发，一次几秒）
**L3**    云端强模型 —— 交叉验证      **要钱、要出网**（但最准）
========= ========================== ==================================

为什么要级联
============

单用云端：每一次判定都要钱、都要把日志送出本机。
单用本地：小模型判「危险」时会**误报**，而且它没有云端那种「读得懂上下文」的能力。

所以：**让便宜的去筛，让贵的去判。**

L2 判 ``ON_TRACK`` 的那些事件 —— 也就是**绝大多数**正常操作 ——
**根本不该走到云端**。这就是「省下 Y 次云端调用」的由来。

双盲亮红灯
==========

L2 说有问题 → 叫 L3 来复核 → **两边都同意**才算数。

只有 L2 说有问题、L3 说不 —— 那就不亮红灯。
（本地小模型误报是家常便饭，让它单方面拉响最高级警报，
只会训练用户忽略警报。）

⚠️ 三条硬约束
--------------

1. **L2 不可用时立刻回退 L1** —— 绝不死等、绝不堵队列；
2. **L3 不可用时**只记「单边判定」，**不**亮红灯
   （凑不齐双盲就不响 —— 宁可漏报也不要误报最高级警报）；
3. **一切降级都要记账** —— 「模型判不了」必须**看得见**，
   否则用户以为自己在享受级联，其实在裸奔。
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

from .normalize import build_static_prefix, strip_volatile
from .parsing import parse_cascade_payload
from .prompts import (
    CASCADE_FILTER_TEMPLATE,
    CASCADE_VERIFY_TEMPLATE,
    VERDICT_DANGEROUS,
    VERDICT_DRIFT,
    VERDICT_ON_TRACK,
    build_filter_prompt,
    build_verify_prompt,
)

logger = logging.getLogger(__name__)

__all__ = [
    "CASCADE_BUDGET_NOTICE",
    "CASCADE_FAILOVER_NOTICE",
    "CASCADE_STATUS_BRIDGED",
    "CASCADE_STATUS_CLEARED",
    "CASCADE_STATUS_ESCALATED",
    "CASCADE_STATUS_FAILOVER",
    "CASCADE_STATUS_ON_TRACK",
    "CASCADE_STATUS_SINGLE",
    "CASCADE_STATUS_UNAVAILABLE",
    "CascadeResult",
    "CascadeRouter",
    "CascadeVerdict",
    "CascadeWorker",
    "http_status_of",
    "is_transient_reason",
]

#: L2 判正常 —— **静默丢弃**，省下一次云端调用。
CASCADE_STATUS_ON_TRACK = "on_track"

#: L2 与 L3 **都**说有问题 —— 亮红灯。
CASCADE_STATUS_ESCALATED = "escalated"

#: L2 说有问题、L3 说不 —— 判定被**推翻**。
CASCADE_STATUS_CLEARED = "cleared"

#: 只拿到一边的判定（L3 不可用）—— 不亮红灯，但**记账**。
CASCADE_STATUS_SINGLE = "single"

#: L2 不可用 —— 立刻回退 L1 原始判定。
CASCADE_STATUS_UNAVAILABLE = "unavailable"

#: **单盲**判定（L2 已关闭，只由 L3 复核）—— L3 说有问题。
#:
#: ⚠️ 单盲**永远不亮红灯** —— 「双盲一致才升级红线」这条原则不变。
CASCADE_STATUS_BRIDGED = "bridged"

#: **热切**判定（L2 掉线，原给 L2 的载荷直接转给 L3）—— v3.8.0。
#:
#: ⚠️ 这条路**同样不亮红灯** —— L2 已经没了，只剩 L3 一票，
#: 与「单盲」同等待遇（「双盲才红灯」这条原则**一字未改**）。
CASCADE_STATUS_FAILOVER = "failover"

#: 热切时给用户看的那句话（v3.8.0）。
CASCADE_FAILOVER_NOTICE = "[WARN] L2 掉线，已自动切换至 L3 深度审查"

#: 预算熔断时给用户看的那句话（v3.8.0）。
CASCADE_BUDGET_NOTICE = (
    "[WARN] L3 回退预算已耗尽，已放弃云端调用并回退 L1 纯规则"
)

#: 「有问题」的两种判定。
_PROBLEM_VERDICTS = (VERDICT_DRIFT, VERDICT_DANGEROUS)

#: 临时性故障的标志（429 / 503 / 超时）——
#: 它们与「模型判错了」是两回事，处置方式也不同。
_TRANSIENT_MARKERS = (
    "http 429",
    "http 503",
    "http 502",
    "http 500",
    "http 504",
    "http 408",
    "timeout",
    "timed out",
    "超时",
    "connection",
    "connectionerror",
    "refused",
    # ⚠️ v3.8.0：有些客户端（如 Ollama）抛的是**裸** ``HTTPError``，
    #    错误文本里只有「503 Server Error: Service Unavailable」，
    #    没有「HTTP 503」这种标准形态 —— 补上这两个英文说法。
    "service unavailable",
    "too many requests",
)

#: 明确属于「临时性」的 HTTP 状态码（v3.8.0）。
_TRANSIENT_HTTP_STATUS = frozenset({408, 429, 500, 502, 503, 504})


def http_status_of(exc: Exception) -> int:
    """从异常里挖出 HTTP 状态码（挖不到就返回 ``0``）。

    ⚠️ **不 import requests** —— 各家客户端抛的异常类型不一样
    （``ApiError`` 只带一句文本、``requests.HTTPError`` 带
    ``response.status_code``），只能靠「鸭子类型」去问。

    :param exc: 异常对象。
    :return: 状态码；认不出来返回 ``0``。
    """
    response = getattr(exc, "response", None)
    for holder in (response, exc):
        if holder is None:
            continue
        for name in ("status_code", "status"):
            value = getattr(holder, name, None)
            if isinstance(value, int) and 100 <= value <= 599:
                return value
    return 0


def is_transient_reason(reason: str) -> bool:
    """判断这个失败原因是不是**临时性**的。

    临时性 = 429 / 503 / 超时 / 连不上 ——
    它们的共同点是「过一会儿可能就好了」，
    所以哨兵必须**立刻放弃**（回退 L1 原始判定），
    绝不能把主队列堵在这里等它。

    :param reason: 失败原因文本。
    :return: 是临时性失败返回 ``True``。
    """
    text = str(reason or "").lower()
    return any(marker in text for marker in _TRANSIENT_MARKERS)


@dataclass(frozen=True)
class CascadeVerdict:
    """一次模型判定的结论。

    :param verdict:   三值判定（``ON_TRACK`` / ``DRIFT`` / ``DANGEROUS``）。
    :param reason:    模型给的一句话理由。
    :param available: 模型**有没有给出可用结论**；
        ``False`` 表示降级（不可用 / 超时 / 限流 / 输出无法解析）。
    :param latency_ms: 本次调用耗时（毫秒）。
    :param model:     模型名。
    :param provider:  提供方标识（``l2`` / ``l3``）。
    :param transient: 这次失败是不是**临时性**的
        （429 / 503 / 超时）—— 临时性失败要立刻放弃，
        不能等，也不能重试到把队列堵住。
    """

    verdict: str = VERDICT_ON_TRACK
    reason: str = ""
    available: bool = True
    latency_ms: int = 0
    model: str = ""
    provider: str = ""
    transient: bool = False

    @property
    def problem(self) -> bool:
        """这次判定是不是「有问题」。"""
        return self.verdict in _PROBLEM_VERDICTS

    @property
    def ok(self) -> bool:
        """是否拿到了模型的有效输出。"""
        return self.available

    @property
    def payload(self) -> Dict[str, Any]:
        """这次判定的**结构化载荷**（可 JSON 序列化）。

        不可用时**一律**返回这个形状：

        .. code-block:: json

            {"status": "unavailable", "reason": "HTTP 429：触发限流（稍后重试）"}

        这就是「429 / 503 / 超时 → ``{"status":"unavailable"}``」的
        **统一契约** —— 调用方（级联编排器）据此**立刻回退 L1 原始判定**，
        绝不阻塞主队列。
        """
        if not self.available:
            return {
                "status": CASCADE_STATUS_UNAVAILABLE,
                "reason": self.reason,
                "transient": self.transient,
            }
        return {
            "status": self.verdict,
            "reason": self.reason,
            "model": self.model,
            "provider": self.provider,
            "latency_ms": self.latency_ms,
        }


@dataclass(frozen=True)
class CascadeResult:
    """一次级联审核的**总结果**。

    :param l2: L2 本地模型的判定。
    :param l3: L3 云端模型的判定；没调用就是 ``None``。
    :param status: 总体状态（见 ``CASCADE_STATUS_*``）。
    :param red_light: **双盲亮红灯** —— L2 与 L3 都判定有问题。
    :param saved_cloud_calls: 这次省下了几次云端调用。
    :param reason: 人话理由。
    :param bridged: 本次是不是**单盲**判定（L2 已关闭，只由 L3 复核）。
    :param failover: 本次是不是**热切**判定（L2 掉线，载荷转给 L3）——
        v3.8.0。它与 ``bridged`` 一样**不亮红灯**。
    :param notice: 给界面看的那句话（热切 / 预算熔断时才非空）。
    :param budget_exhausted: 本次是不是**预算熔断**（L3 回退预算用完了，
        已放弃云端调用并回退 L1）—— v3.8.0。
    """

    l2: CascadeVerdict = field(default_factory=CascadeVerdict)
    l3: Optional[CascadeVerdict] = None
    status: str = CASCADE_STATUS_UNAVAILABLE
    red_light: bool = False
    saved_cloud_calls: int = 0
    reason: str = ""
    bridged: bool = False
    failover: bool = False
    notice: str = ""
    budget_exhausted: bool = False

    @property
    def escalated(self) -> bool:
        """是否需要**升级处理**（亮红灯）。"""
        return self.red_light

    def as_dict(self) -> Dict[str, Any]:
        """整次级联审核的**结构化结果**（可 JSON 序列化）。

        给日志行、看板与测试用 —— 里面没有任何秘密，
        只有判定、理由与计数。

        :return: 结果字典。
        """
        return {
            "status": self.status,
            "red_light": self.red_light,
            "saved_cloud_calls": self.saved_cloud_calls,
            "reason": self.reason,
            "bridged": self.bridged,
            "failover": self.failover,
            "notice": self.notice,
            "budget_exhausted": self.budget_exhausted,
            "l2": self.l2.payload,
            "l3": self.l3.payload if self.l3 is not None else None,
        }


class CascadeRouter:
    """级联审核的编排器（L2 → L3）。

    :param l2: L2 本地模型提供方；为 ``None`` 时整个级联**不工作**。
    :param l3: L3 云端模型提供方；为 ``None`` 时只跑 L2
        （没有云端就没有交叉验证，也**不会**亮红灯）。
    :param goal: 本次会话的任务目标（进静态前缀）。
    :param require_agreement: 是否要求「双盲一致」才亮红灯（默认 ``True``）。
    :param breaker_threshold: L2 连续失败多少次后熔断。
    :param breaker_open_seconds: 熔断持续时长。
    :param use_l2: 是否启用 L2（v3.7.0）。
        关掉它 = 本地算力不够，级联通路**直接由 L3 复核**。
    :param use_l3: 是否启用 L3（v3.7.0）。
    :param bridge_l1_to_l3: 是否允许 **L1 未命中**的事件也进入这条通路
        （v3.7.0）。只在 L2 关闭且 L3 开启时才有意义。
    :param failover_to_l3: L2 **掉线**时是否把载荷热切给 L3（v3.8.0）。
        关掉它 = 掉线就老实回退 L1（与 v3.7.0 行为完全一致）。
    :param l3_fallback_limit: 热切预算上限（窗口内的**次数**）。
        ``0`` 表示不限次数。
    :param l3_fallback_limit_seconds: 热切预算的窗口长度（秒）。
        默认一小时。
    :param l3_fallback_cost_limit: 热切预算的**金额**上限（同一个币种）。
        ``0`` 表示不限金额。
    :param l3_fallback_unit_cost: 每次热切的**预估**费用；
        配合 ``l3_fallback_cost_limit`` 算出窗口内的预估花费。
        ⚠️ 这是**估算值**（用户自己填），实际账单以服务商为准。
    """

    def __init__(
        self,
        l2: Optional[Any] = None,
        l3: Optional[Any] = None,
        *,
        goal: str = "",
        require_agreement: bool = True,
        breaker_threshold: int = 3,
        breaker_open_seconds: float = 60.0,
        use_l2: bool = True,
        use_l3: bool = True,
        bridge_l1_to_l3: bool = False,
        failover_to_l3: bool = True,
        l3_fallback_limit: int = 100,
        l3_fallback_limit_seconds: float = 3600.0,
        l3_fallback_cost_limit: float = 0.0,
        l3_fallback_unit_cost: float = 0.0,
    ) -> None:
        self.l2 = l2
        self.l3 = l3
        self.goal = str(goal or "").strip()
        self.require_agreement = bool(require_agreement)
        #: v3.7.0：L2 / L3 的独立开关。
        self.use_l2 = bool(use_l2)
        self.use_l3 = bool(use_l3)
        #: v3.7.0：L1 未命中的事件是否直接桥接给 L3。
        self.bridge_l1_to_l3 = bool(bridge_l1_to_l3)
        #: v3.8.0：L2 掉线是否热切给 L3。
        self.failover_to_l3 = bool(failover_to_l3)
        #: v3.8.0：热切预算（次数 / 窗口 / 金额）。
        self.l3_fallback_limit = max(int(l3_fallback_limit or 0), 0)
        self.l3_fallback_limit_seconds = max(
            float(l3_fallback_limit_seconds or 0), 0.0
        )
        self.l3_fallback_cost_limit = max(float(l3_fallback_cost_limit or 0), 0.0)
        self.l3_fallback_unit_cost = max(float(l3_fallback_unit_cost or 0), 0.0)

        #: L2 的熔断器（与客户端自带的那个**分开** ——
        #: 这里管的是「级联这条路」，不是「这个客户端」）。
        self._failures = 0
        self._breaker_threshold = max(int(breaker_threshold or 0), 1)
        self._breaker_open_seconds = float(breaker_open_seconds)
        self._breaker_opened_at: Optional[float] = None
        self._lock = threading.Lock()

        # --- 计数器（看板顶部的「省下 Y 次云端调用」靠它们） ---
        #: L2 被调用了几次。
        self.l2_calls = 0
        #: L3 被调用了几次。
        self.l3_calls = 0
        #: L2 判正常、因而**省下**的云端调用次数。
        self.saved_cloud_calls = 0
        #: L2 不可用（超时 / 限流 / 熔断）的次数。
        self.unavailable_calls = 0
        #: 亮红灯的次数。
        self.red_lights = 0
        #: v3.7.0：**单盲**（L2 已关闭，只由 L3 复核）的次数。
        self.bridged_calls = 0
        #: 最近一次不可用的原因。
        self.last_unavailable_reason = ""
        #: v3.8.0：**热切**（L2 掉线转给 L3）的次数。
        self.failover_calls = 0
        #: v3.8.0：因**预算熔断**放弃 L3 调用的次数。
        self.budget_exhausted_calls = 0
        #: v3.8.0：最近一次热切的原话（L2 为什么掉线）。
        self.last_failover_reason = ""
        #: v3.8.0：热切时刻队列（用于窗口内计数）。
        self._failover_window: list[float] = []
        #: v3.8.0：热切预算的当前用量（次数与预估费用）。
        self._failover_cost = 0.0

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------
    @property
    def available(self) -> bool:
        """级联有没有在工作。"""
        if self.use_l2 and self.l2 is not None:
            return True
        return bool(self.use_l3 and self.l3 is not None)

    @property
    def bridging(self) -> bool:
        """桥接是否**真的可用**。

        需要同时满足：L2 关闭、L3 开启、``bridge_l1_to_l3`` 打开。

        :return: 可桥接返回 ``True``。
        """
        return bool(
            self.bridge_l1_to_l3
            and not self.use_l2
            and self.use_l3
            and self.l3 is not None
        )

    def filter(
        self,
        *,
        summary: str = "",
        action: str = "",
        l1_verdict: str = "",
        l1_reason: str = "",
    ) -> CascadeResult:
        """跑一次级联审核。

        :param summary:   最近几步的摘要。
        :param action:    本步动作（一句话）。
        :param l1_verdict: L1（规则层）的判定。
        :param l1_reason:  L1 的理由。
        :return: :class:`CascadeResult`；**绝不抛异常**。
        """
        if not self.use_l2:
            # ⚠️ v3.7.0：L2 已关闭 —— **跳过本地过滤，直接问云端 L3**。
            #    这是「单盲」：只有 L3 一票，**永远不亮红灯**。
            return self._filter_without_l2(
                summary=summary,
                action=action,
                l1_verdict=l1_verdict,
                l1_reason=l1_reason,
            )
        if self.l2 is None:
            return CascadeResult(
                status=CASCADE_STATUS_UNAVAILABLE,
                reason="未配置 L2 模型",
            )
        l2 = self._ask_l2(summary=summary, action=action)
        if not l2.available:
            # ⚠️ L2 不可用 —— v3.8.0：先看能不能**热切**给 L3，
            #    切不了才回退 L1 的原始判定，绝不死等、绝不堵队列。
            return self._handle_l2_unavailable(
                l2=l2,
                summary=summary,
                action=action,
                l1_verdict=l1_verdict,
                l1_reason=l1_reason,
            )
        if not l2.problem:
            # ✅ 静默丢弃 —— **省下一次云端调用**。
            self.saved_cloud_calls += 1
            return CascadeResult(
                l2=l2,
                status=CASCADE_STATUS_ON_TRACK,
                saved_cloud_calls=1,
                reason=l2.reason,
            )

        if not self.use_l3 or self.l3 is None:
            # 没有云端就没有交叉验证 —— 凑不齐双盲，**不亮红灯**。
            return CascadeResult(
                l2=l2,
                status=CASCADE_STATUS_SINGLE,
                reason="未配置 L3 模型，无法交叉验证",
            )

        l3 = self._ask_l3(
            summary=summary,
            action=action,
            l2_verdict=l2.verdict,
            l2_reason=l2.reason,
            l1_verdict=l1_verdict,
            l1_reason=l1_reason,
        )
        if not l3.available:
            return CascadeResult(
                l2=l2,
                l3=l3,
                status=CASCADE_STATUS_SINGLE,
                reason=l3.reason or "L3 模型不可用",
            )
        if l3.problem:
            self.red_lights += 1
            return CascadeResult(
                l2=l2,
                l3=l3,
                status=CASCADE_STATUS_ESCALATED,
                red_light=True,
                reason="L2 与 L3 同时判定有问题：{}".format(l3.reason or l2.reason),
            )
        # L3 推翻了 L2 —— 判定被**清除**。
        return CascadeResult(
            l2=l2,
            l3=l3,
            status=CASCADE_STATUS_CLEARED,
            reason="L3 复核未发现问题：{}".format(l3.reason),
        )

    def _filter_without_l2(
        self,
        *,
        summary: str,
        action: str,
        l1_verdict: str,
        l1_reason: str,
    ) -> CascadeResult:
        """L2 已关闭时的通路 —— **单盲**由 L3 复核。

        ⚠️ 单盲**不亮红灯**（「双盲一致才升级红线」这条原则不变），
        但结论照样回传给调用方（引擎据此生成一条**普通级**告警）。

        :param summary:    最近几步的摘要。
        :param action:     本步动作。
        :param l1_verdict: L1 的判定。
        :param l1_reason:  L1 的理由。
        :return: :class:`CascadeResult`。
        """
        if not self.use_l3 or self.l3 is None:
            return CascadeResult(
                status=CASCADE_STATUS_UNAVAILABLE,
                reason="L2 与 L3 均已关闭，级联通路不工作",
                bridged=True,
            )
        l3 = self._ask_l3(
            summary=summary,
            action=action,
            l2_verdict="L2 已关闭（由 L3 直接复核）",
            l2_reason="L2 已关闭",
            l1_verdict=l1_verdict,
            l1_reason=l1_reason,
        )
        if not l3.available:
            return CascadeResult(
                l3=l3,
                status=CASCADE_STATUS_SINGLE,
                bridged=True,
                reason=l3.reason or "L3 模型不可用",
            )
        self.bridged_calls += 1
        if l3.problem:
            return CascadeResult(
                l3=l3,
                status=CASCADE_STATUS_BRIDGED,
                bridged=True,
                reason="L3 单盲判定有问题：{}".format(l3.reason),
            )
        return CascadeResult(
            l3=l3,
            status=CASCADE_STATUS_ON_TRACK,
            bridged=True,
            reason="L3 复核未发现问题：{}".format(l3.reason),
        )

    def _handle_l2_unavailable(
        self,
        *,
        l2: CascadeVerdict,
        summary: str,
        action: str,
        l1_verdict: str,
        l1_reason: str,
    ) -> CascadeResult:
        """L2 掉线时的处置 —— 热切 L3 / 预算熔断 / 安静回退 L1（v3.8.0）。

        按优先级三条路：

        1. **热切** —— L2 是**临时性**掉线（连不上 / 429 / 503 / 超时 /
           熔断中）、L3 已启用、预算没超：
           把**原本给 L2 的载荷**原封不动转给 L3，并挂上
           ``[WARN] L2 掉线，已自动切换至 L3 深度审查``。
           ⚠️ 只有 L3 一票 ⇒ **不亮红灯**（「双盲才红灯」这条原则不变）。
        2. **预算熔断** —— 窗口内的热切次数或预估费用超阈值：
           **放弃**这次云端调用，回退 L1 纯规则，
           并由调用方发一条**严重系统告警**。
        3. **安静回退 L1** —— L3 没开、热切被关、L2 挂得**不是**临时性的，
           或者 L3 也挂了：一律回退 L1，**不报错、不卡死**。

        :param l2: L2 的判定（``available=False``）。
        :param summary:    最近几步的摘要。
        :param action:     本步动作。
        :param l1_verdict: L1 的判定。
        :param l1_reason:  L1 的理由。
        :return: :class:`CascadeResult`；**绝不抛异常**。
        """
        if not self.failover_to_l3:
            # 热切被关 —— 与 v3.7.0 行为**完全一样**。
            return CascadeResult(
                l2=l2,
                status=CASCADE_STATUS_UNAVAILABLE,
                reason=l2.reason or "L2 模型不可用",
            )
        if not self.use_l3 or self.l3 is None:
            return CascadeResult(
                l2=l2,
                status=CASCADE_STATUS_UNAVAILABLE,
                reason=l2.reason or "L2 模型不可用",
            )
        if not l2.transient:
            # ⚠️ L2 还活着，只是这次没给出可用结论（输出无法解析之类）——
            #    那不是「掉线」，不值得为此烧云端的钱，老实回退 L1。
            return CascadeResult(
                l2=l2,
                status=CASCADE_STATUS_UNAVAILABLE,
                reason=l2.reason or "L2 模型不可用",
            )
        budget = self._budget_state()
        if budget["exhausted"]:
            self.budget_exhausted_calls += 1
            logger.warning(
                "L3 回退预算已耗尽（%s），放弃本次云端调用并回退 L1",
                budget["reason"],
            )
            return CascadeResult(
                l2=l2,
                status=CASCADE_STATUS_UNAVAILABLE,
                budget_exhausted=True,
                notice=CASCADE_BUDGET_NOTICE,
                reason="L3 回退预算已耗尽（{}），已回退 L1 纯规则".format(
                    budget["reason"]
                ),
            )
        # ✅ 热切 —— 把**原本给 L2 的载荷**转给 L3。
        l3 = self._ask_l3(
            summary=summary,
            action=action,
            l2_verdict="L2 掉线（{}）".format(l2.reason or "不可用"),
            l2_reason=l2.reason or "L2 模型不可用",
            l1_verdict=l1_verdict,
            l1_reason=l1_reason,
        )
        self._note_failover(l2.reason)
        if not l3.available:
            # ⚠️ L3 也挂了 —— **安静回退 L1**：不报错、不卡死。
            logger.warning(
                "L2 掉线热切 L3 失败（已回退 L1）：%s",
                l3.reason or l2.reason or "未知原因",
            )
            return CascadeResult(
                l2=l2,
                l3=l3,
                status=CASCADE_STATUS_UNAVAILABLE,
                failover=True,
                notice=CASCADE_FAILOVER_NOTICE,
                reason="L2 与 L3 均不可用，已回退 L1 纯规则：{}".format(
                    l3.reason or l2.reason or "未知原因"
                ),
            )
        if l3.problem:
            return CascadeResult(
                l2=l2,
                l3=l3,
                status=CASCADE_STATUS_FAILOVER,
                failover=True,
                bridged=True,
                notice=CASCADE_FAILOVER_NOTICE,
                reason="L2 掉线，L3 单盲复核判定有问题：{}".format(l3.reason),
            )
        return CascadeResult(
            l2=l2,
            l3=l3,
            status=CASCADE_STATUS_FAILOVER,
            failover=True,
            bridged=True,
            notice=CASCADE_FAILOVER_NOTICE,
            reason="L2 掉线，L3 复核未发现问题：{}".format(l3.reason),
        )

    def _budget_state(self) -> Dict[str, Any]:
        """算一下 L3 回退预算还剩多少（v3.8.0）。

        两个维度，**任何一个**超了就算熔断：

        1. **次数** —— 窗口内的热切次数达到 ``l3_fallback_limit``；
        2. **金额** —— 窗口内的预估费用
           （次数 × ``l3_fallback_unit_cost``）达到 ``l3_fallback_cost_limit``。

        ⚠️ 金额是**估算**（用户自己填的单次费用），
        实际账单以服务商为准。

        :return: 预算状态字典（含 ``exhausted`` 标记与原因）。
        """
        window = max(float(self.l3_fallback_limit_seconds or 0), 0.0)
        unit_cost = max(float(self.l3_fallback_unit_cost or 0), 0.0)
        now = time.monotonic()
        with self._lock:
            if window > 0:
                cutoff = now - window
                self._failover_window = [
                    stamp for stamp in self._failover_window if stamp >= cutoff
                ]
            count = len(self._failover_window)
            cost = count * unit_cost
            limit = int(self.l3_fallback_limit or 0)
            cost_limit = float(self.l3_fallback_cost_limit or 0)
            exhausted = False
            reason = ""
            if limit > 0 and count >= limit:
                exhausted = True
                reason = "{} 秒内热切次数已达上限 {} 次".format(
                    int(window) if window > 0 else 0, limit
                )
            elif cost_limit > 0 and unit_cost > 0 and cost >= cost_limit:
                exhausted = True
                reason = "{} 秒内热切预估费用已达上限 {:.4f}".format(
                    int(window) if window > 0 else 0, cost_limit
                )
        return {
            "exhausted": exhausted,
            "reason": reason,
            "count": count,
            "cost": cost,
            "window_seconds": window,
        }

    def _note_failover(self, reason: str) -> None:
        """记一次热切（v3.8.0）。

        :param reason: L2 掉线的原因。
        """
        with self._lock:
            self.failover_calls += 1
            self._failover_window.append(time.monotonic())
            self._failover_cost += max(float(self.l3_fallback_unit_cost or 0), 0.0)
            self.last_failover_reason = str(reason or "")

    def stats(self) -> Dict[str, Any]:
        """级联的统计（看板与晨报用）。

        :return: 统计字典。
        """
        with self._lock:
            breaker_open = self._is_breaker_open_locked()
            budget_count = len(self._failover_window)
            budget_cost = budget_count * max(
                float(self.l3_fallback_unit_cost or 0), 0.0
            )
        return {
            "l2_calls": self.l2_calls,
            "l3_calls": self.l3_calls,
            "saved_cloud_calls": self.saved_cloud_calls,
            "unavailable_calls": self.unavailable_calls,
            "red_lights": self.red_lights,
            "l2_model": str(getattr(self.l2, "model", "") or ""),
            "l3_model": str(getattr(self.l3, "model", "") or ""),
            "breaker_open": breaker_open,
            "last_unavailable_reason": self.last_unavailable_reason,
            "use_l2": self.use_l2,
            "use_l3": self.use_l3,
            "bridge_l1_to_l3": self.bridge_l1_to_l3,
            "bridging": self.bridging,
            "bridged": self.bridged_calls,
            # --- v3.8.0：热切与预算熔断 ---
            "failover_to_l3": self.failover_to_l3,
            "failover": self.failover_calls,
            "budget_exhausted": self.budget_exhausted_calls,
            "last_failover_reason": self.last_failover_reason,
            "l3_fallback_budget": {
                "limit": int(self.l3_fallback_limit or 0),
                "window_seconds": float(self.l3_fallback_limit_seconds or 0),
                "cost_limit": float(self.l3_fallback_cost_limit or 0),
                "unit_cost": float(self.l3_fallback_unit_cost or 0),
                "used": budget_count,
                "used_cost": budget_cost,
            },
        }

    def reset(self) -> None:
        """清空统计（新会话开始时用）。"""
        self.l2_calls = 0
        self.l3_calls = 0
        self.saved_cloud_calls = 0
        self.unavailable_calls = 0
        self.red_lights = 0
        self.bridged_calls = 0
        self.last_unavailable_reason = ""
        # --- v3.8.0 ---
        self.failover_calls = 0
        self.budget_exhausted_calls = 0
        self.last_failover_reason = ""
        with self._lock:
            self._failover_window = []
            self._failover_cost = 0.0

    # ------------------------------------------------------------------
    # 内部实现 —— L2
    # ------------------------------------------------------------------
    def _ask_l2(self, *, summary: str, action: str) -> CascadeVerdict:
        """问一次 L2 本地模型。

        :param summary: 最近几步的摘要。
        :param action:  本步动作。
        :return: :class:`CascadeVerdict`；不可用时 ``available=False``。
        """
        if self._is_breaker_open():
            # ⚠️ 熔断中 = L2 **暂时**不可用 ——
            #    标记成临时性，好让 v3.8.0 的热切能接上（否则
            #    L2 连挂三次之后，热切这条路就永远走不到了）。
            return CascadeVerdict(
                available=False,
                reason="L2 熔断中，暂时不再调用",
                provider="l2",
                transient=True,
            )
        # ⚠️ **静态前缀**（系统提示 + 事实卡片）在前，易变部分在后 ——
        #    这样前缀才能被 Prefix Cache 完整命中。
        system = build_static_prefix(
            CASCADE_FILTER_TEMPLATE,
            goal=self.goal,
            facts={"判定档位": "ON_TRACK / DRIFT / DANGEROUS"},
        )
        user = strip_volatile(
            build_filter_prompt(summary=summary, action=action)
        )
        return self._call(self.l2, "l2", system, user)

    # ------------------------------------------------------------------
    # 内部实现 —— L3
    # ------------------------------------------------------------------
    def _ask_l3(
        self,
        *,
        summary: str,
        action: str,
        l2_verdict: str,
        l2_reason: str,
        l1_verdict: str,
        l1_reason: str,
    ) -> CascadeVerdict:
        """问一次 L3 云端模型（**交叉验证**）。

        :param summary:    最近几步的摘要。
        :param action:     本步动作。
        :param l2_verdict: L2 的判定。
        :param l2_reason:  L2 的理由。
        :param l1_verdict: L1 的判定。
        :param l1_reason:  L1 的理由。
        :return: :class:`CascadeVerdict`；不可用时 ``available=False``。
        """
        system = build_static_prefix(
            CASCADE_VERIFY_TEMPLATE,
            goal=self.goal,
            facts={"判定档位": "ON_TRACK / DRIFT / DANGEROUS"},
        )
        user = strip_volatile(
            build_verify_prompt(
                summary=summary,
                action=action,
                l2_verdict=l2_verdict,
                l2_reason=l2_reason,
                l1_verdict=l1_verdict,
                l1_reason=l1_reason,
            )
        )
        return self._call(self.l3, "l3", system, user)

    # ------------------------------------------------------------------
    # 内部实现 —— 共用
    # ------------------------------------------------------------------
    def _call(
        self,
        provider: Any,
        provider_name: str,
        system: str,
        user: str,
    ) -> CascadeVerdict:
        """调一次模型并解析三值判定。

        ⚠️ **绝不抛异常** —— 任何失败都降级成「不可用」，
        绝不拖垮审查主链路（红线 R5）。

        :param provider:     模型提供方。
        :param provider_name: ``l2`` / ``l3``。
        :param system:       静态前缀。
        :param user:         易变部分。
        :return: :class:`CascadeVerdict`。
        """
        if provider_name == "l2":
            self.l2_calls += 1
        else:
            self.l3_calls += 1
        model = str(getattr(provider, "model", "") or "")
        started = time.monotonic()
        try:
            raw = provider.chat(system, user)
        except Exception as exc:  # noqa: BLE001 —— 失败一律降级
            reason = self._failure_reason(exc)
            # ⚠️ v3.8.0：把 HTTP 状态码补进原因文本 ——
            #    有些客户端（Ollama）抛的是裸 ``HTTPError``，
            #    文本里没有「HTTP 503」这种标准形态，热切就认不出来。
            status = http_status_of(exc)
            if status and "http {}".format(status) not in reason.lower():
                reason = "HTTP {}：{}".format(status, reason)
            transient = is_transient_reason(reason) or (
                status in _TRANSIENT_HTTP_STATUS
            )
            if provider_name == "l2":
                self._note_failure(reason)
            return CascadeVerdict(
                available=False,
                reason=reason,
                transient=transient,
                latency_ms=self._elapsed_ms(started),
                model=model,
                provider=provider_name,
            )
        parsed = parse_cascade_payload(raw)
        if not parsed.ok:
            reason = "模型输出无法解析（{}）".format(parsed.reason)
            if provider_name == "l2":
                self._note_failure(reason)
            return CascadeVerdict(
                available=False,
                reason=reason,
                latency_ms=self._elapsed_ms(started),
                model=model,
                provider=provider_name,
            )
        if provider_name == "l2":
            self._note_success()
        return CascadeVerdict(
            verdict=str(parsed.data.get("verdict") or VERDICT_ON_TRACK),
            reason=str(parsed.data.get("reason") or ""),
            available=True,
            latency_ms=self._elapsed_ms(started),
            model=model,
            provider=provider_name,
        )

    @staticmethod
    def _failure_reason(exc: Exception) -> str:
        """把异常变成一句人能看懂的降级原因。

        :param exc: 异常对象。
        :return: 原因文本。
        """
        detail = " ".join(str(exc).split())[:160]
        if detail:
            return "{}（{}）".format(type(exc).__name__, detail)
        return type(exc).__name__

    @staticmethod
    def _elapsed_ms(started: float) -> int:
        """计算耗时（毫秒）。

        :param started: 起始时刻（``time.monotonic()``）。
        :return: 毫秒数。
        """
        return int((time.monotonic() - started) * 1000)

    def _note_failure(self, reason: str) -> None:
        """记一次 L2 失败 —— 连续失败就熔断。

        :param reason: 失败原因。
        """
        with self._lock:
            self._failures += 1
            self.unavailable_calls += 1
            self.last_unavailable_reason = str(reason or "")
            if (
                self._failures >= self._breaker_threshold
                and self._breaker_opened_at is None
            ):
                self._breaker_opened_at = time.monotonic()
                logger.warning(
                    "L2 连续失败 %s 次，熔断 %.0f 秒",
                    self._failures,
                    self._breaker_open_seconds,
                )

    def _note_success(self) -> None:
        """记一次 L2 成功（重置熔断计数）。"""
        with self._lock:
            self._failures = 0
            self._breaker_opened_at = None

    def _is_breaker_open(self) -> bool:
        """L2 熔断器现在是不是开着。

        :return: 开着返回 ``True``。
        """
        with self._lock:
            return self._is_breaker_open_locked()

    def _is_breaker_open_locked(self) -> bool:
        """熔断器判定（**调用方必须持有锁**）。

        :return: 开着返回 ``True``。
        """
        if self._breaker_opened_at is None:
            return False
        elapsed = time.monotonic() - self._breaker_opened_at
        if elapsed >= self._breaker_open_seconds:
            # 半开 —— 放一次试探。
            self._breaker_opened_at = None
            self._failures = self._breaker_threshold - 1
            return False
        return True


class CascadeWorker:
    """后台线程 —— 跑级联审核，**主循环绝不阻塞**。

    与 :class:`~src.core.engine.ExplanationWorker` 同一套纪律：
    队列满就**丢弃**（背压），绝不把主循环堵住。

    :param router: 级联编排器。
    :param on_result: 结论回调，签名 ``(alert_id, CascadeResult) -> None``。
    :param max_pending: 队列容量。
    """

    def __init__(
        self,
        router: Optional[CascadeRouter] = None,
        on_result: Optional[Callable[[str, CascadeResult], None]] = None,
        *,
        max_pending: int = 8,
    ) -> None:
        self.router = router
        self.on_result = on_result
        self.max_pending = max(int(max_pending or 0), 1)
        self._queue: "queue.Queue[tuple]" = queue.Queue(maxsize=self.max_pending)
        self._worker: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._idle = threading.Event()
        self._idle.set()
        #: 因背压被丢弃的任务数。
        self.dropped = 0

    def submit(self, alert_id: str, payload: Dict[str, Any]) -> bool:
        """投递一次级联审核。

        :param alert_id: 原告警 ID。
        :param payload:  审核载荷（摘要、动作、L1 判定…）。
        :return: 投递成功返回 ``True``。
        """
        if self.router is None:
            return False
        try:
            self._queue.put_nowait((alert_id, dict(payload)))
        except queue.Full:
            self.dropped += 1
            logger.warning(
                "级联审核队列已满（%d），丢弃本次", self.max_pending
            )
            return False
        self._ensure_worker()
        return True

    def drain(self, timeout: float = 30.0) -> bool:
        """等所有**已提交**的任务出结论。

        :param timeout: 最长等多少秒。
        :return: 全部完成返回 ``True``。
        """
        return self._idle.wait(timeout=max(float(timeout), 0.0))

    def stop(self, timeout: float = 5.0) -> None:
        """停掉后台线程。

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
        """懒启动后台线程（**守护线程**）。"""
        if self._worker is not None and self._worker.is_alive():
            return
        self._worker = threading.Thread(
            target=self._worker_loop,
            name="sentinel-cascade",
            daemon=True,
        )
        self._worker.start()

    def _worker_loop(self) -> None:
        """后台线程主体。"""
        while not self._stop.is_set():
            try:
                alert_id, payload = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            if self._queue.empty():
                self._idle.set()
            else:
                self._idle.clear()
            try:
                result = self.router.filter(
                    summary=str(payload.get("summary") or ""),
                    action=str(payload.get("action") or ""),
                    l1_verdict=str(payload.get("l1_verdict") or ""),
                    l1_reason=str(payload.get("l1_reason") or ""),
                )
            except Exception as exc:  # noqa: BLE001 —— 绝不让后台线程死掉
                logger.warning(
                    "级联审核失败（已降级）：%s: %s", type(exc).__name__, exc
                )
                continue
            if self.on_result is None:
                continue
            try:
                self.on_result(alert_id, result)
            except Exception as exc:  # noqa: BLE001 —— 回调失败不影响审查
                logger.warning(
                    "级联结果回调失败：%s: %s", type(exc).__name__, exc
                )
