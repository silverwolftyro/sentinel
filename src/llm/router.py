"""L2 多级模型路由 —— 「平时省钱，出事用强模型」。

为什么要有它
============

半夜挂机的时候，单一模型总会卡在两头：

- **只用本地 7B**：省钱、不出本机，但解释「为什么这条命令危险」时，
  经常说得又慢又糙 —— 而这恰恰是**最需要说清楚**的时刻；
- **只用云端强模型**：解释质量好，但每一次调用都要钱、
  要把日志送出本机，而且网络一抖整条链路就哑了。

真实需求是**分级**的：

> 平时用本地模型省钱；
> **一旦红线级拦截、或连续漂移**，立刻换云端强模型去写解释和晨报。

这就是本模块的职责。

路由规则
========

===================== ================== ==============================
触发条件              用哪个模型          为什么
===================== ================== ==============================
普通告警 / 正常审查    ``primary``        默认路径，省钱
**红线级**拦截        ``fallback``       这一刻的解释最要紧，
                                          值得用更强的模型，也更值得
                                          多花那点钱
**连续漂移**          ``fallback``       连续漂移说明 Agent 已经跑偏，
                                          判定本身要准，不能将就
===================== ================== ==============================

两条硬约束
==========

1. **云端挂了不能没有解释。**
   ``fallback`` 降级（超时 / 鉴权失败 / 熔断）时，自动退回 ``primary``。
   —— 红线事件**绝不能**因为「云端抽风」而没有解释；
2. **降级绝不等同于有问题。**
   漂移判定失败一律算「**没漂移**」，连续计数归零。
   （与引擎里 :class:`~src.core.engine.IntentTracker` 的口径一致。）

用了哪些模型
============

路由器会记录本次会话**实际调用过**的模型（``models_used``）和
每一次**升级**的原因（``escalations``）——
晨报要靠它们告诉用户「昨晚是谁在干活」。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from .base import DriftResult, Explanation, LLMProvider

logger = logging.getLogger(__name__)

__all__ = ["ESCALATE_DRIFT", "ESCALATE_RED_LINE", "ModelRouter"]

#: 红线级告警触发升级。
ESCALATE_RED_LINE = "red_line"

#: 连续漂移触发升级。
ESCALATE_DRIFT = "drift_streak"


class ModelRouter(LLMProvider):
    """多级模型路由（本身也是 :class:`LLMProvider`，可到处替换）。

    用法上它就是**一个普通的 provider**：

    .. code-block:: python

        router = ModelRouter(
            primary=OllamaClient(model="qwen2.5:7b"),
            fallback=OpenAICompatibleClient(
                base_url="https://api.deepseek.com", model="deepseek-chat"
            ),
        )
        client = build_provider(config)   # 内部按配置决定要不要包一层

    :param primary:   主力模型（通常是本地 Ollama）。
    :param fallback:  升级用的更强模型（通常是云端 API）；留空则不升级。
    :param escalate_on_red_line: 红线级是否升级。
    :param drift_escalate_threshold: 连续判漂移几次后升级。
        ``1`` 表示「已经连续判了 1 次漂移，下一次判定就用强模型」——
        也就是第二次连续漂移（触发告警的那一次）由强模型来判。
    :param max_context_chars: 上下文长度上限（透传给子提供方时用）。
    """

    #: 提供方标识。
    name = "router"

    def __init__(
        self,
        primary: LLMProvider,
        fallback: Optional[LLMProvider] = None,
        *,
        escalate_on_red_line: bool = True,
        drift_escalate_threshold: int = 1,
        max_context_chars: int = 4000,
        timeout: float = 60.0,
        enabled: bool = True,
    ) -> None:
        super().__init__(
            model=str(getattr(primary, "model", "") or ""),
            timeout=float(timeout),
            enabled=bool(enabled),
            max_context_chars=int(max_context_chars),
        )
        self.primary = primary
        self.fallback = fallback
        self.escalate_on_red_line = bool(escalate_on_red_line)
        self.drift_escalate_threshold = max(0, int(drift_escalate_threshold))

        #: 本次会话**实际调用过**的模型 → 调用次数。
        self.models_used: Dict[str, int] = {}
        #: 每一次升级的记录：``{"reason": …, "model": …, "at": …}``。
        self.escalations: List[Dict[str, Any]] = []
        #: 连续判为漂移的次数。
        self._drift_streak = 0
        #: 判定**降级**（模型不可用）的次数。
        #:
        #: 只**记账**，不改判定口径 —— 降级依然算「没漂移」
        #: （红线 R4：缺失绝不等同于有问题）。记账的用处是让终端与晨报
        #: 都能**如实告诉用户**「那段时间漂移检测等于没开」，
        #: 而不是让它静悄悄地死掉。
        self.degraded_calls: int = 0
        #: 最近一次降级的原因（人话）。
        self.last_degraded_reason: str = ""

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------
    def chat(self, system: str, user: str) -> str:
        """裸对话（基类契约要求实现）。

        路由器本身**不参与** Prompt 的组织 —— 它只在
        :meth:`explain` / :meth:`evaluate_drift` 里做「选谁」的决策。
        所以这里直接委派给**主力模型**。

        :param system: 系统提示。
        :param user:   用户提示。
        :return: 模型原始输出。
        """
        return self.primary.chat(system, user)

    def explain(self, event_data: Dict[str, Any]) -> Explanation:
        """生成告警解释（红线级会升级到强模型）。

        :param event_data: 事件字典（含 ``verdict``）。
        :return: :class:`Explanation`；**绝不抛异常**。
        """
        payload = event_data if isinstance(event_data, dict) else {}
        verdict = payload.get("verdict")
        verdict = verdict if isinstance(verdict, dict) else {}
        severity = str(verdict.get("severity") or "")

        want_fallback = (
            self.fallback is not None
            and self.escalate_on_red_line
            and severity == ESCALATE_RED_LINE
        )
        provider = self.fallback if want_fallback else self.primary
        try:
            result = provider.explain(payload)
        except Exception as exc:  # noqa: BLE001 —— 子提供方不该抛，但不能假设
            logger.warning(
                "模型路由调用失败，已隔离：%s: %s", type(exc).__name__, exc
            )
            result = self._fallback_explanation(payload, str(exc))

        if want_fallback and result.degraded and self.primary is not provider:
            # 云端这次没顶住 —— 但红线事件的解释**不能没有**。
            logger.info("升级模型降级，回退主力模型补一次解释。")
            try:
                result = self.primary.explain(payload)
                provider = self.primary
            except Exception as exc:  # noqa: BLE001 —— 兜底兜底
                logger.warning(
                    "主力模型补解释也失败：%s: %s", type(exc).__name__, exc
                )

        self._record(provider, result, ESCALATE_RED_LINE if want_fallback else "")
        if result.degraded:
            self._note_degraded(result)
        return result

    def evaluate_drift(self, goal: str, summary: str) -> DriftResult:
        """判定意图漂移（连续漂移会升级到强模型）。

        ⚠️ 模型判不了的时候（降级），这里**保守算「没漂移」**（红线 R4），
        但会把降级**记一笔**（``degraded_calls`` / ``last_degraded_reason``）——
        静悄悄的失效最坏，终端与晨报要靠它把失效说出来。

        :param goal:    任务目标。
        :param summary: Agent 最近行为的摘要。
        :return: :class:`DriftResult`；**绝不抛异常**。
        """
        want_fallback = (
            self.fallback is not None
            and self._drift_streak >= self.drift_escalate_threshold
            and self.drift_escalate_threshold > 0
        )
        provider = self.fallback if want_fallback else self.primary
        try:
            result = provider.evaluate_drift(goal, summary)
        except Exception as exc:  # noqa: BLE001 —— 子提供方不该抛，但不能假设
            logger.warning(
                "漂移判定调用失败，已隔离：%s: %s", type(exc).__name__, exc
            )
            result = DriftResult(
                drift=False,
                reason="模型不可用，保守判定为未漂移",
                degraded=True,
                detail=str(exc),
            )

        if result.degraded:
            # 降级绝不等同于「有问题」—— 保守算没漂移，计数归零。
            self._drift_streak = 0
            self._note_degraded(result)
            self._record(provider, result, ESCALATE_DRIFT if want_fallback else "")
            return result

        if result.drift:
            self._drift_streak += 1
        else:
            self._drift_streak = 0
        self._record(provider, result, ESCALATE_DRIFT if want_fallback else "")
        return result

    def _note_degraded(self, result: Any) -> None:
        """记一笔「模型判不了」—— **沉默的失效是最坏的失效**。

        v3.2.0 实机演习撞上的真问题：主力模型没装（本机 Ollama 一个模型
        都没有）时，每次漂移判定都降级；而降级在口径上等于「没漂移」，
        于是**漂移检测静悄悄地死掉，用户什么都不知道**。

        这里只**记账**，不动判定口径（红线 R4）：
        降级依然算「没漂移」，绝不把「不知道」当成「有问题」。
        记账的用处是让终端与晨报都能**如实告诉用户**：
        「你那段时间的漂移检测其实是关着的」。

        :param result: 判定结果（:class:`DriftResult` 或
            :class:`Explanation`）。
        :return: 无返回值。
        """
        self.degraded_calls += 1
        reason = str(getattr(result, "reason", "") or "")
        if reason:
            self.last_degraded_reason = reason

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------
    @property
    def drift_streak(self) -> int:
        """当前连续判为漂移的次数。"""
        return self._drift_streak

    def models_used_text(self) -> str:
        """把「用过哪些模型」渲染成一行人话。

        :return: 形如 ``ollama:qwen2.5:7b ×12、openai_compatible:deepseek-chat ×3``。
        """
        if not self.models_used:
            return "（本次会话没有调用任何模型）"
        parts = [
            f"{name} ×{count}"
            for name, count in sorted(
                self.models_used.items(), key=lambda kv: (-kv[1], kv[0])
            )
        ]
        return "、".join(parts)

    def reset(self) -> None:
        """清空统计（新一轮会话开始时用）。"""
        self.models_used.clear()
        self.escalations.clear()
        self._drift_streak = 0
        self.degraded_calls = 0
        self.last_degraded_reason = ""

    def _record(
        self,
        provider: LLMProvider,
        result: Any,
        reason: str,
    ) -> None:
        """记账：这次用了哪个模型、有没有升级。

        :param provider: 实际干活的提供方。
        :param result:   调用结果（:class:`Explanation` 或 :class:`DriftResult`）。
        :param reason:   升级原因；空串表示没升级。
        """
        name = str(getattr(provider, "name", "unknown") or "unknown")
        model = str(getattr(result, "model", "") or getattr(provider, "model", ""))
        label = f"{name}:{model}" if model else name
        self.models_used[label] = self.models_used.get(label, 0) + 1
        if reason:
            self.escalations.append(
                {
                    "reason": reason,
                    "model": label,
                    "at": time.time(),
                }
            )
            logger.info("L2 升级到强模型：%s（%s）", label, reason)

    @staticmethod
    def _fallback_explanation(
        event_data: Dict[str, Any], reason: str
    ) -> Explanation:
        """子提供方抛异常时的兜底解释。

        :param event_data: 事件字典。
        :param reason:    失败原因。
        :return: 降级的 :class:`Explanation`。
        """
        verdict = event_data.get("verdict")
        verdict = verdict if isinstance(verdict, dict) else {}
        tool = event_data.get("tool")
        tool = tool if isinstance(tool, dict) else {}
        command = str(tool.get("command") or "")
        rule_reason = str(verdict.get("reason") or "（无规则说明）")
        return Explanation(
            text=f"{command} —— {rule_reason}",
            degraded=True,
            reason=reason,
            detail={
                "title": "模型不可用",
                "why": rule_reason,
                "touch": command,
                "outcome": "（模型不可用，仅展示原始命令与规则判定）",
            },
            model="",
            provider="router",
        )
