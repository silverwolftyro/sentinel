"""通用**纯文本**适配器 —— 给没有机器可读输出格式的 Agent CLI 用。

为什么需要它
============

14 个外部插件 Agent CLI 里，有 **6 个拿不到可直接解析的机器可读输出**：

* ``dsh`` / ``evox`` / ``hermes`` / ``qwenpaw`` / ``step``
  —— 压根没登记 ``json_args``，stdout 只有纯文本；
* ``pi`` —— 虽然登记了 ``--mode json``，但那是**事件流**，
  不是单个 JSON 文档，要写状态机才能解。

v4.1.0 明确**不做**这 6 个的多格式解析器（成本过高），
但也不能因此完全接不上它们 —— 于是有了这个「最省」的适配器：
**把 stdout 的每一行原样当成一条文本事件**。

能力边界（必须如实告知用户）
============================

* ✅ 规则层能对**正文**做正则匹配（危险命令、密钥泄漏等）；
* ✅ 引擎能统计行数与事件数；
* ❌ **做不了 L2 漂移判定** —— 漂移判定依赖结构化的事件语义
  （工具调用、思考块、结果事件），纯文本没有这些标记；
* ❌ **做不了「本轮结束」判定** —— 没有 ``done_markers``，
  只能靠进程退出。

所以这 6 个 Agent 在哨兵里属于「**能看、不能审**」的一档。
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from .base import BaseAdapter, Event

__all__ = ["TextAdapter"]


class TextAdapter(BaseAdapter):
    """把纯文本行原样转成文本事件。

    与其他适配器最大的不同：它**不解析 JSON** ——
    非 JSON 行才是它的主菜。
    """

    #: 适配器名称，同时作为事件的 ``source`` 字段。
    name = "text"

    def parse_payload(self, payload: Dict[str, Any]) -> List[Event]:
        """把一个 JSON 对象当成一段文本。

        有些 CLI 的输出是「一个 JSON 对象里装着正文」
        （比如 gemini 的 ``-o json``，正文在 ``response`` 字段），
        这种情况下按文本处理比按结构处理更稳。

        :param payload: 单行 JSON 解析后的字典。
        :return: 事件列表（0 或 1 条）。
        """
        text = ""
        for key in ("response", "text", "result", "content", "message"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                text = value
                break
        if not text:
            return []
        return [self._make_event(text, raw=payload)]

    def parse_line(self, line: str) -> List[Event]:
        """把一行原始文本转成事件。

        ⚠️ 这里**覆盖**了基类的实现 ——
        基类遇到非 JSON 行会直接跳过，而纯文本适配器的主菜
        恰恰就是非 JSON 行。

        :param line: 一行原始文本（不带换行符）。
        :return: 事件列表（0 或 1 条）。
        """
        stripped = line.strip()
        if not stripped:
            return []
        return [self._make_event(stripped)]

    def _make_event(
        self,
        text: str,
        *,
        raw: Optional[Dict[str, Any]] = None,
    ) -> Event:
        """造一条文本事件。

        :param text: 事件正文。
        :param raw: 原始载荷。
        :return: 事件字典。
        """
        payload: Dict[str, Any] = dict(raw or {})
        return {
            "source": self.name,
            "type": "text",
            "session_id": None,
            "fingerprint": "",
            "timestamp": time.time(),
            "tool": None,
            "text": text,
            "is_error": False,
            "raw": payload,
        }
