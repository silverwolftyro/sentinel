"""动态特征剥离 —— 让 L2 的 Prompt **吃满 Prefix Cache**（v3.5.0）。

问题
====

本地小模型（NInfer）跑在 12G 显存的物理极限上，
**每一次 Prompt 都要重新算**。

而 Prefix Cache 的原理很简单：**前缀完全相同 → 直接复用**。
所以只要 Prompt 里混进一个会变的东西，
整段前缀的缓存就**全部作废**。

会变的东西
----------

| 内容 | 例子 | 为什么它毁缓存 |
| --- | --- | --- |
| 时间戳 | ``2026-10-29 23:41:05`` | 每次请求都不一样 |
| 尾部行号 | ``[TAIL] #123`` | 每读一行就变一次 |
| 随机 UUID | ``01a17af0-3b2c-…`` | 天生每次都不同 |
| 长随机十六进制 | ``a3f9c2…``（32 位以上） | 同上 |
| Epoch 毫秒 | ``1793294465000`` | 同上 |

怎么办
======

把会变的内容**换成固定的占位符**：

.. code-block:: text

    改前：2026-10-29 23:41:05 读 a3f9c2e1b0d4... 文件
    改后：<时间戳> 读 <随机ID> 文件

⚠️ 三条纪律
-----------

1. **只动送给模型的 Prompt**，**不动**告警载荷 ——
   证据链里必须留原文，时间戳与 UUID 是排障的关键。
2. **占位符是固定字符串** —— 换成一个常量，
   同样的输入永远得到同样的输出，缓存才可能命中。
3. **只剥「纯易变」的东西** ——
   文件路径、命令、工具名**一个字都不动**，
   那些是判定依据，剥了模型就没法判了。

前缀怎么排
==========

Prefix Cache 吃的是**最长公共前缀**，所以顺序很重要：

.. code-block:: text

    ┌──────────────────────────────┐  ← 静态部分：逐字不变 → 命中缓存
    │ System Prompt（模块级常量）    │
    │ + 事实卡片（本次会话内不变）   │
    ├──────────────────────────────┤  ← 易变部分：每次都不同
    │ User Prompt（最近窗口 + 本步） │
    └──────────────────────────────┘

所以本模块提供 :func:`build_static_prefix` ——
把「静态系统提示 + 事实卡片」拼成**一段**，
交给调用方放进**单条 system 消息**里。
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

__all__ = [
    "PLACEHOLDER_RANDOM_ID",
    "PLACEHOLDER_TIMESTAMP",
    "PLACEHOLDER_UUID",
    "build_static_prefix",
    "strip_volatile",
    "volatile_fingerprint",
]

#: 时间戳的占位符。
PLACEHOLDER_TIMESTAMP = "<时间戳>"

#: UUID 的占位符。
PLACEHOLDER_UUID = "<UUID>"

#: 长随机十六进制的占位符。
PLACEHOLDER_RANDOM_ID = "<随机ID>"

#: ``[TAIL] #123`` 里的行号占位符。
PLACEHOLDER_LINE = "[TAIL]"

#: 易变内容的替换规则（按顺序应用）。
#:
#: ⚠️ 顺序有讲究：UUID 要在「长十六进制」**之前**处理 ——
#:    否则 UUID 会被拆成两截。
_VOLATILE_RULES: Tuple[Tuple["re.Pattern[str]", str], ...] = (
    # ISO 8601 / ``YYYY-MM-DD HH:MM:SS``
    (
        re.compile(
            r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?"
            r"(?:Z|[+-]\d{2}:?\d{2})?"
        ),
        PLACEHOLDER_TIMESTAMP,
    ),
    # 纯日期 ``YYYY-MM-DD``
    (re.compile(r"\b\d{4}-\d{2}-\d{2}\b"), PLACEHOLDER_TIMESTAMP),
    # ``HH:MM:SS``
    (re.compile(r"\b\d{2}:\d{2}:\d{2}\b"), PLACEHOLDER_TIMESTAMP),
    # Epoch 毫秒（13 位）
    (re.compile(r"\b1[6-9]\d{11}\b"), PLACEHOLDER_TIMESTAMP),
    # 尾部行号 ``[TAIL] #123``
    (re.compile(r"\[TAIL\]\s*#\d+"), PLACEHOLDER_LINE),
    # UUID v4
    (
        re.compile(
            r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
            r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
        ),
        PLACEHOLDER_UUID,
    ),
    # 长的随机十六进制（32 位以上）
    (re.compile(r"\b[0-9a-fA-F]{32,}\b"), PLACEHOLDER_RANDOM_ID),
)


def strip_volatile(text: Any) -> str:
    """把文本里的**易变内容**换成固定占位符。

    :param text: 原始文本。
    :return: 归一化后的文本。
    """
    result = str(text or "")
    if not result:
        return ""
    for pattern, replacement in _VOLATILE_RULES:
        result = pattern.sub(replacement, result)
    return result


def volatile_fingerprint(text: Any) -> str:
    """算一段文本的**易变指纹**（归一化之后的哈希）。

    用途：**测试**里断言「两次请求的静态前缀完全一致」。
    指纹一致 → 缓存能命中。

    :param text: 原始文本。
    :return: 16 位十六进制哈希。
    """
    normalized = strip_volatile(text)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def build_static_prefix(
    system_prompt: str,
    *,
    goal: str = "",
    facts: Optional[Dict[str, Any]] = None,
    extra_lines: Optional[Iterable[str]] = None,
) -> str:
    """拼出**静态前缀** —— 系统提示 + 事实卡片。

    这一整段在**一次会话内不变**，所以它能被 Prefix Cache 完整命中。

    :param system_prompt: 模块级常量形式的系统提示。
    :param goal: 本次会话的任务目标（会话内不变）。
    :param facts: 事实卡片（键值对，**顺序固定**才稳）。
    :param extra_lines: 追加的静态行。
    :return: 拼好的静态前缀。
    """
    lines: List[str] = [str(system_prompt or "").strip()]
    if goal:
        lines.append("【任务目标】")
        lines.append(str(goal).strip())
    if facts:
        fact_lines: List[str] = []
        # ⚠️ 按**键名排序** —— dict 的插入顺序不保证，
        #    排序之后同样的事实永远得到同样的字节。
        for key in sorted(facts.keys()):
            value = facts[key]
            if value in (None, "", [], {}):
                continue
            fact_lines.append("- {}：{}".format(key, value))
        # 事实全是空的 → **连标题都不给** ——
        # 一个空标题白白占掉 Prompt 的前缀位置，缓存也就白白少一截。
        if fact_lines:
            lines.append("【事实卡片】")
            lines.extend(fact_lines)
    if extra_lines:
        lines.extend(str(line) for line in extra_lines if str(line).strip())
    return "\n".join(lines).strip()
