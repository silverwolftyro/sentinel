"""Token 消耗审计 —— 统计 Agent 烧掉的 Token，并估算「挽回」了多少。

为什么需要它
============
「跑偏的 Agent 会白白烧掉 Token」是共识，但**烧了多少**一直是个黑箱。
用户只看到「它跑了一晚上」，不知道那是几十块还是几百块。

更糟的是：**没人能凭直觉判断「该不该叫醒它」**。
一个烧了 3 万 Token 的漂移 Agent，和一个烧了 300 Token 的，
危险程度天差地别 —— 但在现在的看板上长得一模一样。

这个模块给出两个东西：

1. **已消耗 Token** —— 实时累计，让用户知道钱花到哪了；
2. **挽回 Token** —— 拦截发生时，估算「因为这次拦截而**没有**烧掉的消耗」。

估算规则
========
1. **有 usage 就用 usage** ——
   codex 的 ``turn.completed`` 事件带
   ``{"usage": {"input_tokens": …, "output_tokens": …}}``，那是**真值**，直接采信；
2. **没有 usage 就按字符估** ——
   * **CJK 字符**：1 字 ≈ 1 Token（中文的 tokenize 通常不吃亏，取保守值）；
   * **其它字符**：4 字符 ≈ 1 Token（英文通用经验值）。

估出来的数字会标上 ``estimated=True`` —— 看板和卡片都会如实告诉用户
「这是估算」，绝不让估算值冒充真值。

「挽回 Token」怎么算
====================
一次拦截（守护者挂起 / 连续漂移告警）省下的是**两段**：

::

    已烧掉的   = 本次会话累计消耗
                （这段工作正在走向危险动作，本就是无效消耗）
    本来要烧的 = 每步均摊 Token × 预估剩余步数
                （Agent 若不被拦住，还会继续跑下去）

    挽回 = 已烧掉的 + 本来要烧的

「每步均摊」= 累计 Token ÷ 已处理事件数。
「预估剩余步数」默认 **10** 步（可配）—— 一次典型任务的收尾量级。

⚠️ 这**是估算**，不是账单。所有对外文案都写「预估」。
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional

__all__ = [
    "DEFAULT_REMAINING_STEPS",
    "TokenLedger",
    "estimate_tokens",
    "extract_usage",
    "format_token_count",
]

#: 一次拦截默认能省下的后续步数。
DEFAULT_REMAINING_STEPS = 10

#: CJK 字符（含中文标点与全角符号）—— 1 字 ≈ 1 Token。
_CJK_PATTERN = re.compile(
    r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
    r"\u3000-\u303f\uff00-\uffef]"
)

#: 非 CJK 字符 —— 4 字符 ≈ 1 Token。
_CHARS_PER_TOKEN = 4

#: usage 字段里可能出现的名字（不同 CLI 不一样）。
_USAGE_KEYS = ("input_tokens", "output_tokens", "total_tokens")
_USAGE_ALIASES = {
    "input_tokens": ("input_tokens", "prompt_tokens", "input"),
    "output_tokens": ("output_tokens", "completion_tokens", "output"),
    "cached_input_tokens": ("cached_input_tokens", "cache_read_input_tokens"),
    "cache_write_input_tokens": ("cache_write_input_tokens",),
}


def estimate_tokens(text: str) -> int:
    """按字符数估算一段文本的 Token 数。

    :param text: 文本。
    :return: 估算的 Token 数（至少 0）。
    """
    if not text:
        return 0
    raw = str(text)
    cjk = len(_CJK_PATTERN.findall(raw))
    others = len(raw) - cjk
    return int(cjk + others / _CHARS_PER_TOKEN + 0.5)


def extract_usage(payload: Any) -> Optional[Dict[str, int]]:
    """从原始流式载荷里挖出 usage 统计。

    codex 的 ``turn.completed`` 带 ``usage``；
    OpenAI 兼容 API 带 ``usage``；也有把 usage 平铺在事件根上的。

    :param payload: 原始载荷（字典）。
    :return: 归一化后的 usage 字典；找不到返回 ``None``。
    """
    if not isinstance(payload, dict):
        return None
    raw = payload.get("usage")
    if not isinstance(raw, dict):
        raw = payload if _looks_like_usage(payload) else None
    if not isinstance(raw, dict):
        return None

    result: Dict[str, int] = {}
    for canonical, aliases in _USAGE_ALIASES.items():
        for name in aliases:
            value = raw.get(name)
            if isinstance(value, (int, float)) and value >= 0:
                result[canonical] = int(value)
                break
    for name in _USAGE_KEYS:
        value = raw.get(name)
        if isinstance(value, (int, float)) and value >= 0:
            result[name] = int(value)
    if not result:
        return None
    return result


def _looks_like_usage(payload: Dict[str, Any]) -> bool:
    """判断载荷本身是不是 usage（usage 被平铺在事件根上）。"""
    return any(
        name in payload
        for names in _USAGE_ALIASES.values()
        for name in names
    )


def format_token_count(tokens: Any) -> str:
    """把 Token 数格式化成一句人话。

    ≥ 1 万 → 用「万」做单位（中文语境下「万」比「k」直观）；
    否则直接给数字。

    :param tokens: Token 数。
    :return: 例如 ``3.2 万 Token`` / ``842 Token``。
    """
    try:
        value = float(tokens or 0)
    except (TypeError, ValueError):
        value = 0.0
    value = max(value, 0.0)
    if value >= 10000:
        return f"{value / 10000:.1f} 万 Token"
    return f"{int(value)} Token"


class TokenLedger:
    """一次会话的 Token 台账。

    :param session_id: 会话 ID（仅用于日志可读性）。
    :param remaining_steps: 预估「本来还要跑」的步数。
    """

    def __init__(
        self,
        session_id: str = "",
        remaining_steps: int = DEFAULT_REMAINING_STEPS,
    ) -> None:
        self.session_id = str(session_id or "")
        self.remaining_steps = max(int(remaining_steps or 0), 0)
        #: 真实 usage 累计（输入 / 输出 / 缓存）。
        self.input_tokens = 0
        self.output_tokens = 0
        self.cached_tokens = 0
        #: 上游直接给出的总数（优先于分项）。
        self._exact_total = 0
        #: 估算累计（没有 usage 时按字符估）。
        self.estimated_tokens = 0
        #: 已处理的事件数（用来算「每步均摊」）。
        self.events = 0
        #: 有多少个事件的 Token 是**估算**的。
        self.estimated_events = 0
        #: 最近一次 usage 的原始值（真值优先）。
        self._last_exact: Optional[Dict[str, int]] = None

    # ------------------------------------------------------------------
    # 记账
    # ------------------------------------------------------------------
    def record_usage(self, usage: Optional[Dict[str, int]]) -> int:
        """记一次**真实** usage。

        ⚠️ usage 是**快照**不是增量 ——
        codex 的 ``turn.completed`` 报的是「到目前为止的上下文总量」
        （第二回合的 ``input_tokens`` 已经包含第一回合的内容）。
        把它当成增量累加会重复计数好几倍。

        所以真值来了就**替换**掉此前的估算值 —— 真值覆盖一切。

        :param usage: 归一化的 usage 字典。
        :return: 本次确立的 Token 总数。
        """
        if not usage:
            return 0
        self._last_exact = dict(usage)
        total = usage.get("total_tokens")
        if isinstance(total, (int, float)) and total > 0:
            # 上游给了总数 —— 以它为准（分项别再累加，会重复计数）。
            self._exact_total = int(total)
            self.input_tokens = 0
            self.output_tokens = 0
            self.cached_tokens = int(usage.get("cached_input_tokens") or 0)
            self.estimated_tokens = 0
            return self._exact_total
        self._exact_total = 0
        self.input_tokens = int(usage.get("input_tokens") or 0)
        self.output_tokens = int(usage.get("output_tokens") or 0)
        self.cached_tokens = int(usage.get("cached_input_tokens") or 0)
        # 真值覆盖此前的估算。
        self.estimated_tokens = 0
        return self.input_tokens + self.output_tokens

    def record_text(self, text: str) -> int:
        """按字符估算并记一笔。

        :param text: 事件里的文本 / 命令内容。
        :return: 本次估算出的 Token 数。
        """
        tokens = estimate_tokens(text)
        if tokens <= 0:
            return 0
        self.estimated_tokens += tokens
        self.estimated_events += 1
        return tokens

    def record_event(self, text: str = "", usage: Optional[Dict[str, int]] = None) -> int:
        """记一个事件：有 usage 用 usage，否则按字符估。

        :param text: 事件文本（估算用）。
        :param usage: 真实 usage（有就用）。
        :return: 本次新增的 Token 总数。
        """
        self.events += 1
        if usage:
            return self.record_usage(usage)
        return self.record_text(text)

    # ------------------------------------------------------------------
    # 快照
    # ------------------------------------------------------------------
    @property
    def total_tokens(self) -> int:
        """当前累计消耗的 Token 总数。"""
        if self._exact_total:
            return self._exact_total + self.estimated_tokens
        return (
            self.input_tokens
            + self.output_tokens
            + self.estimated_tokens
        )

    @property
    def estimated(self) -> bool:
        """当前数字里是否含有**估算**成分。"""
        return self.estimated_tokens > 0

    def avg_tokens_per_event(self) -> float:
        """每个事件的平均 Token 消耗。"""
        if self.events <= 0:
            return 0.0
        return self.total_tokens / float(self.events)

    def snapshot(self) -> Dict[str, Any]:
        """返回台账快照（可直接放进告警 / 状态接口）。"""
        return {
            "session_id": self.session_id,
            "input_tokens": int(self.input_tokens),
            "output_tokens": int(self.output_tokens),
            "cached_tokens": int(self.cached_tokens),
            "estimated_tokens": int(self.estimated_tokens),
            "total_tokens": int(self.total_tokens),
            "events": int(self.events),
            "avg_per_event": round(self.avg_tokens_per_event(), 2),
            "estimated": self.estimated,
        }

    def estimate_saved(self) -> Dict[str, Any]:
        """估算「因为这次拦截而没有烧掉的 Token」。

        :return: ``{"tokens": int, "text": str, "steps": int, …}``。
        """
        burned = self.total_tokens
        upcoming = int(self.avg_tokens_per_event() * self.remaining_steps)
        saved = max(burned + upcoming, 0)
        return {
            "tokens": saved,
            "text": (
                f"预估为您挽回 {format_token_count(saved)} 的无效消耗"
            ),
            "burned": burned,
            "upcoming": upcoming,
            "steps": self.remaining_steps,
            "estimated": True,
        }
