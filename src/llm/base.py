"""L2 模型层的统一契约 —— ``LLMProvider`` 抽象基类。

为什么要有这一层
================
v0.6.0 之前，L2 只有 Ollama 一种实现，调用逻辑直接写在
``ollama_client.py`` 里。要接外部大模型（DeepSeek、Kimi、硅基流动…），
若照抄一份，**降级契约、Prompt、解析阶梯、脱敏**就会散落成 N 份 ——
那正是旧项目栽过的地方。

所以把「所有提供方都做的事」抽到这里：

* 脱敏 → 截断 → 前置检查 → 拼 Prompt → 调用 → 容错解析 → 渲染 → 降级；
* 子类**只**需要实现 :meth:`LLMProvider.chat`：把一段对话变成模型输出。

这样无论底层是本地 Ollama 还是外部 API，**返回结构完全一致**：

.. code-block:: text

    Explanation
      ├── text        面向人的一段话（标题/原因/影响/后果拼成）
      ├── detail      结构化字段 {title, why, touch, outcome}
      ├── degraded    是否走了降级路径
      ├── reason      降级原因
      ├── latency_ms  本次调用耗时
      ├── model       使用的模型名
      └── provider    提供方标识（ollama / openai_compatible）

三条硬约束
==========
1. **永不抛异常** —— 模型没开、超时、连不上、输出解析不出来，
   一律返回**降级结果**（``degraded=True``），由调用方落到
   「原始命令 + 规则原因」的兜底文案。
   这条契约抄自旧项目 ``core/llm_review.py:381-435``：
   「**缺失绝不等同于安全**」—— 模型说「安全」却没给理由时，
   连「安全」都要被改写掉。
2. **规则层绝不调用模型** —— L1 永远是确定性的。
3. **不引入重型框架** —— 旧项目用 ``pydantic-ai``，但它自己的实测记录
   （``core/llm_review.py:206-218``，2026-10-22，本地 Ollama + qwen2.5:7b）
   显示 ``output_type=<BaseModel>`` **10 次失败 6 次（60%）**。
   哨兵只用标准 HTTP 调用 + 容错解析阶梯（``src/llm/parsing.py``）。
"""

from __future__ import annotations

import logging
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from .normalize import strip_volatile
from .parsing import ParseResult, parse_drift_payload, parse_explain_payload
from .prompts import (
    DRIFT_EVALUATE_TEMPLATE,
    SENTINEL_EXPLAIN_TEMPLATE,
    build_drift_prompt,
    build_explain_prompt,
)
from .redaction import redact_secrets

logger = logging.getLogger(__name__)

__all__ = [
    "CircuitBreaker",
    "DriftResult",
    "Explanation",
    "LLMProvider",
    "ProviderError",
    "engine_lock_for",
]


#: 每台引擎（按 ``base_url`` 分组）一把**串行锁**。
#:
#: 锁本身很便宜，真正的原因是**排队**：
#: 单并发引擎（``max-concurrency=1``）同时收到两个请求会**直接崩**，
#: 而哨兵天生多线程 —— 后台解释线程、漂移判定线程、晨报线程
#: 都可能同一时刻要模型。
_ENGINE_LOCKS: Dict[str, threading.Lock] = {}

#: 保护上面那张表本身（建表时的竞态）。
_ENGINE_LOCKS_GUARD = threading.Lock()


def engine_lock_for(base_url: str) -> threading.Lock:
    """拿到某台引擎的**串行锁**。

    为什么必须有
    ===========

    v3.2.1 实机踩到：本机 NInfer 引擎是**单并发**
    （``max-concurrency=1``）—— 同时发两个请求，**引擎直接崩**，
    而且崩得很难看（连接断掉，错误信息毫无意义）。

    哨兵天生多线程：后台解释线程、漂移判定线程、晨报线程
    都可能同时要模型。所以排队必须发生在**客户端**。

    锁按 ``base_url`` 分开：本地引擎和云端 API **各排各的队** ——
    升级路由切到云端时，不会被本地引擎的队堵住。

    :param base_url: 引擎根地址（``http://127.0.0.1:8888/v1`` 之类）。
    :return: 该引擎的锁；同一地址永远返回同一把。
    """
    key = str(base_url or "").strip().rstrip("/").lower()
    with _ENGINE_LOCKS_GUARD:
        lock = _ENGINE_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _ENGINE_LOCKS[key] = lock
        return lock


class ProviderError(RuntimeError):
    """模型调用失败（配置问题或服务不可用）。"""


@dataclass(frozen=True)
class Explanation:
    """一次 L2 审查的结论。

    这是 L2 层**唯一**的对外数据结构 —— 无论底层是 Ollama 还是
    外部 API，返回的都是它。

    :param text:     面向人的解释（一段话）。
    :param degraded: 是否走了降级路径。
    :param reason:   降级原因；未降级时为空串。
    :param detail:   结构化字段（``title`` / ``why`` / ``touch`` / ``outcome``）。
    :param latency_ms: 本次调用耗时（毫秒）。
    :param model:    使用的模型名。
    :param provider: 提供方标识（``ollama`` / ``openai_compatible``）。
    """

    text: str
    degraded: bool = False
    reason: str = ""
    detail: Dict[str, Any] = field(default_factory=dict)
    latency_ms: int = 0
    model: str = ""
    provider: str = ""

    @property
    def ok(self) -> bool:
        """是否拿到了模型的有效输出。"""
        return not self.degraded


@dataclass(frozen=True)
class DriftResult:
    """一次「意图漂移」判定的结论。

    :param drift:      是否判定为漂移。
    :param reason:    判定理由（模型给的一句话）。
    :param degraded:   是否走了降级路径。
    :param detail:    降级原因 / 原始输出摘要。
    :param latency_ms: 本次调用耗时（毫秒）。
    :param model:     使用的模型名。
    :param provider:  提供方标识。
    """

    drift: bool = False
    reason: str = ""
    degraded: bool = False
    detail: str = ""
    latency_ms: int = 0
    model: str = ""
    provider: str = ""

    @property
    def ok(self) -> bool:
        """是否拿到了模型的有效判定。"""
        return not self.degraded


class CircuitBreaker:
    """极简熔断器。

    连续失败 ``failure_threshold`` 次后**打开**，持续 ``open_seconds`` 秒，
    期间直接跳过模型调用；时间到自动半开（放一次试探）。

    :param failure_threshold: 触发熔断的连续失败次数。
    :param open_seconds:      熔断持续时长（秒）。
    """

    def __init__(
        self,
        failure_threshold: int = 3,
        open_seconds: float = 300.0,
    ) -> None:
        self.failure_threshold = max(int(failure_threshold or 0), 1)
        self.open_seconds = float(open_seconds)
        self._failures = 0
        self._opened_at: Optional[float] = None

    @property
    def is_open(self) -> bool:
        """当前是否处于熔断状态。"""
        if self._opened_at is None:
            return False
        elapsed = time.monotonic() - self._opened_at
        if elapsed >= self.open_seconds:
            # 半开：放一次试探
            self._opened_at = None
            self._failures = self.failure_threshold - 1
            return False
        return True

    def record_success(self) -> None:
        """记录一次成功，重置计数。"""
        self._failures = 0
        self._opened_at = None

    def record_failure(self) -> None:
        """记录一次失败，达阈值则打开熔断。"""
        self._failures += 1
        if (
            self._failures >= self.failure_threshold
            and self._opened_at is None
        ):
            self._opened_at = time.monotonic()
            logger.warning(
                "模型连续失败 %s 次，熔断 %.0f 秒",
                self._failures,
                self.open_seconds,
            )

    def reset(self) -> None:
        """手动复位。"""
        self._failures = 0
        self._opened_at = None


class LLMProvider(ABC):
    """L2 模型提供方的抽象基类。

    子类**只**需要实现 :meth:`chat`（把对话变成模型输出）；
    其余流程（脱敏、Prompt、解析、渲染、降级）全部由基类统一处理。

    这保证了两件事：

    1. 不同提供方返回的 :class:`Explanation` **结构完全一致**；
    2. 新增一个提供方**不需要**重复实现降级逻辑 ——
       降级契约是全项目最重要的红线之一（红线 R5）。

    :param model:         模型名。
    :param timeout:       单次调用超时（秒）。
    :param enabled:       是否启用；``False`` 时所有调用直接走降级。
    :param redact_before_llm: 是否在送入模型前脱敏。
    :param max_context_chars: 送入模型的上下文最大字符数。
    :param failure_threshold: 熔断阈值。
    :param breaker_open_seconds: 熔断持续时长。
    """

    #: 提供方标识（子类覆盖）。
    name: str = "base"

    def __init__(
        self,
        model: str = "",
        timeout: float = 20.0,
        enabled: bool = True,
        redact_before_llm: bool = True,
        max_context_chars: int = 4000,
        failure_threshold: int = 3,
        breaker_open_seconds: float = 300.0,
    ) -> None:
        self.model = str(model or "")
        self.timeout = float(timeout)
        self.enabled = bool(enabled)
        self.redact_before_llm = bool(redact_before_llm)
        self.max_context_chars = int(max_context_chars)
        self.breaker = CircuitBreaker(
            failure_threshold=int(failure_threshold),
            open_seconds=float(breaker_open_seconds),
        )

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------
    def explain_event(self, event_data: Dict[str, Any]) -> str:
        """把危险事件翻译成人类可读的告警文案（**一段话**）。

        :param event_data: 事件字典。可额外携带：
            ``thinking``（Agent 自述）、``verdict``（L1 规则判定）。
        :return: 面向人的解释；模型不可用时返回兜底文案
            （原始命令 + 规则原因）。
        """
        return self.explain(event_data).text

    def explain(self, event_data: Dict[str, Any]) -> Explanation:
        """结构化版本的解释。

        完整流程：脱敏 → 截断 → 前置检查 → 拼 Prompt → 调用 →
        容错解析 → 渲染。**任何一步失败都降级，绝不抛异常。**

        :param event_data: 事件字典（同 :meth:`explain_event`）。
        :return: :class:`Explanation`。
        """
        event_data = event_data if isinstance(event_data, dict) else {}
        verdict = event_data.get("verdict")
        verdict = verdict if isinstance(verdict, dict) else {}
        tool = event_data.get("tool")
        tool = tool if isinstance(tool, dict) else {}

        command = str(tool.get("command") or "")
        thinking = str(event_data.get("thinking") or "")
        tool_name = str(tool.get("name") or "")

        if self.redact_before_llm:
            command = redact_secrets(command)
            thinking = redact_secrets(thinking)

        if len(command) > self.max_context_chars:
            command = command[: self.max_context_chars]

        # --- 前置检查：任何一条不满足都直接降级 ---
        if not self.enabled:
            return self._degraded(
                event_data, verdict, command, "模型层已关闭"
            )
        blocked = self.blocked_reason()
        if blocked:
            return self._degraded(event_data, verdict, command, blocked)
        if self.breaker.is_open:
            return self._degraded(
                event_data,
                verdict,
                command,
                "模型连续失败已触发熔断，暂时跳过模型解释",
            )

        user_prompt = build_explain_prompt(
            thinking=thinking or None,
            tool_name=tool_name or None,
            command=command or None,
            rule_facts={
                "verdict": verdict.get("verdict"),
                "rule_id": verdict.get("rule_id"),
                "level": verdict.get("level"),
                "reason": verdict.get("reason"),
                "evidence": verdict.get("evidence"),
            },
        )
        # v3.5.0：剥掉**易变内容**（时间戳 / ``[TAIL] #123`` / UUID）——
        #
        # ⚠️ 只动**送给模型的这一份** Prompt，
        #    告警载荷里的原文一个字都不改 ——
        #    证据链里必须留原文，时间戳是排障的关键。
        #
        # 剥完之后，同一件事的 Prompt 前缀**逐字不变**，
        # Prefix Cache 才吃得下。
        user_prompt = strip_volatile(user_prompt)

        started = time.monotonic()
        try:
            raw = self.chat(SENTINEL_EXPLAIN_TEMPLATE, user_prompt)
        except Exception as exc:  # noqa: BLE001 —— 永不把异常抛给调用方
            self.breaker.record_failure()
            logger.warning(
                "模型调用失败（%s）：%s: %s",
                self.name,
                type(exc).__name__,
                exc,
            )
            return self._degraded(
                event_data,
                verdict,
                command,
                self._failure_reason(exc),
                latency_ms=self._elapsed_ms(started),
            )

        latency = self._elapsed_ms(started)
        parsed = parse_explain_payload(raw)
        if not parsed.ok:
            self.breaker.record_failure()
            logger.warning("模型输出无法解析：%s", parsed.reason)
            return self._degraded(
                event_data,
                verdict,
                command,
                f"模型输出无法解析（{parsed.reason}）",
                latency_ms=latency,
            )

        self.breaker.record_success()
        return Explanation(
            text=self.render(parsed, command, verdict),
            degraded=False,
            reason="",
            detail=dict(parsed.data),
            latency_ms=latency,
            model=self.model,
            provider=self.name,
        )

    # ------------------------------------------------------------------
    # 子类需要实现的钩子
    # ------------------------------------------------------------------
    @abstractmethod
    def chat(self, system: str, user: str) -> str:
        """调用底层模型，返回**原始文本**。

        :param system: 系统提示。
        :param user:   用户提示。
        :return: 模型的原始输出（可能是脏 JSON，由基类负责解析）。
        :raises Exception: 任何失败都应抛出，基类会统一降级。
        """

    def evaluate_drift(self, goal: str, summary: str) -> DriftResult:
        """判断 Agent 是否偏离了原始目标。

        与 :meth:`explain` 是**两种不同的任务**，但共用同一套基础设施
        （脱敏、熔断、容错解析、降级）。

        ⚠️ **降级时绝不判定为「漂移」**：模型不可用时返回
        ``drift=False, degraded=True`` —— 因为「缺失绝不等同于有问题」，
        误报一次漂移会让用户对整条告警链路失去信任。

        :param goal:   原始目标。
        :param summary: 最近几步的摘要（调用方负责截断与脱敏）。
        :return: :class:`DriftResult`。
        """
        goal_text = str(goal or "").strip()
        summary_text = str(summary or "").strip()

        if not self.enabled:
            return self._drift_degraded("模型层已关闭")
        blocked = self.blocked_reason()
        if blocked:
            return self._drift_degraded(blocked)
        if self.breaker.is_open:
            return self._drift_degraded("模型连续失败已触发熔断，暂时跳过判定")
        if not goal_text:
            # 没有目标就没有「偏离」可言 —— 直接跳过，不打扰模型。
            return self._drift_degraded("未设置任务目标（task_goal），跳过漂移判定")

        if self.redact_before_llm:
            goal_text = redact_secrets(goal_text)
            summary_text = redact_secrets(summary_text)

        user_prompt = build_drift_prompt(goal=goal_text, summary=summary_text)
        # v3.5.0：同 explain —— 剥掉易变内容，让静态前缀能被缓存。
        user_prompt = strip_volatile(user_prompt)

        started = time.monotonic()
        try:
            raw = self.chat(DRIFT_EVALUATE_TEMPLATE, user_prompt)
        except Exception as exc:  # noqa: BLE001 —— 永不把异常抛给调用方
            self.breaker.record_failure()
            logger.warning(
                "漂移判定调用失败（%s）：%s: %s",
                self.name,
                type(exc).__name__,
                exc,
            )
            return self._drift_degraded(
                self._failure_reason(exc),
                latency_ms=self._elapsed_ms(started),
            )

        latency = self._elapsed_ms(started)
        parsed = parse_drift_payload(raw)
        if not parsed.ok:
            self.breaker.record_failure()
            logger.warning("漂移判定输出无法解析：%s", parsed.reason)
            return self._drift_degraded(
                f"模型输出无法解析（{parsed.reason}）",
                latency_ms=latency,
            )

        self.breaker.record_success()
        return DriftResult(
            drift=bool(parsed.data.get("drift")),
            reason=str(parsed.data.get("reason") or ""),
            degraded=False,
            detail="",
            latency_ms=latency,
            model=self.model,
            provider=self.name,
        )

    def blocked_reason(self) -> str:
        """当前配置是否**禁止**调用模型。

        :return: 禁止原因；允许调用时返回空串。
        """
        return ""

    def render(
        self,
        parsed: ParseResult,
        command: str,
        verdict: Dict[str, Any],
    ) -> str:
        """把结构化结论渲染成一段人话。"""
        parts = []
        title = parsed.get("title")
        if title:
            parts.append(self._strip_tail(title))
        why = parsed.get("why")
        if why:
            parts.append(f"原因：{self._strip_tail(why)}")
        touch = parsed.get("touch")
        if touch:
            parts.append(f"影响：{self._strip_tail(touch)}")
        outcome = parsed.get("outcome")
        if outcome:
            parts.append(f"后果：{self._strip_tail(outcome)}")
        if not parts:
            # 有字段但都没内容 —— 退回规则层原始判定
            return self.fallback_text(command, verdict, "模型未给出有效内容")
        return "。".join(parts)

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    @staticmethod
    def _elapsed_ms(started: float) -> int:
        """计算耗时（毫秒）。"""
        return int((time.monotonic() - started) * 1000)

    @staticmethod
    def _strip_tail(text: str) -> str:
        """去掉末尾的句号，避免拼接时出现「。。」。"""
        cleaned = text.strip()
        while cleaned.endswith(("。", ".", "；", ";")):
            cleaned = cleaned[:-1].rstrip()
        return cleaned

    @staticmethod
    def _failure_reason(exc: Exception) -> str:
        """把异常变成一句人能看懂的降级原因。

        带上异常**消息**（不只是类型名）是有用的：
        「连接被拒绝」说明服务没开，「HTTP 500」说明服务在但报错了，
        「401」说明密钥不对 —— 三者对使用者的处置完全不同。
        抄自旧项目 ``core/llm_review.py:407-414`` 的 ``reason`` 写法。
        """
        detail = " ".join(str(exc).split())[:160]
        if detail:
            return f"调用模型失败（{type(exc).__name__}: {detail}）"
        return f"调用模型失败（{type(exc).__name__}）"

    def _degraded(
        self,
        event_data: Dict[str, Any],
        verdict: Dict[str, Any],
        command: str,
        reason: str,
        latency_ms: int = 0,
    ) -> Explanation:
        """构造降级结果。"""
        return Explanation(
            text=self.fallback_text(command, verdict, reason),
            degraded=True,
            reason=reason,
            detail={},
            latency_ms=latency_ms,
            model=self.model,
            provider=self.name,
        )

    def _drift_degraded(
        self,
        reason: str,
        latency_ms: int = 0,
    ) -> DriftResult:
        """构造漂移判定的降级结果。

        ⚠️ 降级时 ``drift`` 恒为 ``False`` —— 模型不可用**不能**被
        解读成「Agent 跑偏了」。这是「缺失绝不等同于有问题」的落点。
        """
        return DriftResult(
            drift=False,
            reason="",
            degraded=True,
            detail=reason,
            latency_ms=latency_ms,
            model=self.model,
            provider=self.name,
        )

    @staticmethod
    def fallback_text(
        command: str,
        verdict: Dict[str, Any],
        reason: str,
    ) -> str:
        """降级文案：**原始命令 + 规则原因**。

        这是任务书明确要求的兜底 —— 模型不可用时，人依然能看到
        「到底命中了什么、命令长什么样」。
        """
        rule_reason = str(verdict.get("reason") or "").strip()
        if not rule_reason:
            rule_reason = str(verdict.get("verdict") or "未知")
        lines = [f"规则判定：{rule_reason}"]
        if command:
            lines.append(f"原始命令：{command}")
        lines.append(f"（{reason}，以上为规则层原始判定）")
        return "\n".join(lines)
