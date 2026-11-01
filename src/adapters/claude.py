"""Claude 适配器 —— 解析 Claude Code 的 ``--output-format stream-json`` 输出。

输出格式
========

Claude Code 的 ``stream-json`` 模式用 Anthropic 风格的**消息容器**：

.. code-block:: json

    {"type": "system",    "subtype": "init", "session_id": "…"}
    {"type": "assistant", "message": {"content": [
        {"type": "text", "text": "我先看一下配置。"}
    ]}}
    {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "toolu_01", "name": "Bash",
         "input": {"command": "ls -la"}}
    ]}}
    {"type": "user",      "message": {"content": [
        {"type": "tool_result", "tool_use_id": "toolu_01",
         "content": "…", "is_error": false}
    ]}}
    {"type": "result",    "subtype": "success", "result": "…"}

与 mcode 的关系
==============

消息容器的解析（``assistant`` / ``user`` 里的 ``content`` 数组）
在 :class:`~src.adapters.base.BaseAdapter` 里已经实现好了 ——
Claude Code 与 mcode 旧协议用的是**同一套**结构。
本适配器只补 Claude Code **特有**的那几件事：

* ``system`` 事件（会话初始化）—— 记下会话 ID，不产出可审查事件；
* ``user`` 消息里的**字符串**正文 —— 那是**用户提的需求**，
  产出 ``user_message`` 事件，让引擎能**自动锚定任务目标**；
* ``result`` 事件 —— 本轮结束，不产出事件（启动器拿它当终止信号）。
"""

from __future__ import annotations

from typing import Any, Dict, List

from .base import BaseAdapter, Event

__all__ = ["ClaudeAdapter"]


class ClaudeAdapter(BaseAdapter):
    """解析 Claude Code 的 ``--output-format stream-json`` 事件流。"""

    name = "claude"

    def parse_payload(self, payload: Dict[str, Any]) -> List[Event]:
        """把一行 JSON 拆成 0..N 个统一事件。

        :param payload: 单行 JSON 解析后的字典。
        :return: 事件列表。
        """
        payload_type = str(payload.get("type") or "").strip().lower()

        # --- 会话初始化：只记会话 ID，不产出事件 ---
        if payload_type == "system":
            return []

        # --- 本轮结束：不产出事件（启动器拿它当终止信号）---
        if payload_type == "result":
            return []

        # --- 用户消息 ---
        if payload_type in ("user", "human"):
            user_event = self._parse_user_message(payload)
            events: List[Event] = []
            if user_event is not None:
                events.append(user_event)
            # ⚠️ 只有**数组形态**的 content 才可能是工具回执。
            # 字符串形态的正文已经被上面的 ``user_message`` 吃掉了，
            # 再走一遍基类解析会**重复**产出一条 ``text`` 事件 ——
            # 那等于把用户自己的需求混进「Agent 输出」里污染审查。
            if isinstance(self._message_content(payload), list):
                events.extend(self._parse_message_container(payload))
            return events

        # --- assistant 消息：内容块在基类里已实现 ---
        return self._parse_message_container(payload)

    @staticmethod
    def _message_content(payload: Dict[str, Any]) -> Any:
        """取出 ``message.content``（可能压根没有 ``message`` 字段）。"""
        message = payload.get("message")
        if isinstance(message, dict):
            return message.get("content")
        return None

    def _parse_user_message(self, payload: Dict[str, Any]) -> Event:
        """解析用户消息（**任务目标**的来源）。

        Claude Code 会把用户的原始请求作为第一条 ``user`` 消息发出来 ——
        引擎据此**自动锚定**任务目标。

        注意：``user`` 消息的 ``content`` 若是**数组**，那多半是工具回执
        （``tool_result`` 块），不是用户说的话 —— 那就不算「用户请求」。

        :param payload: 整行载荷。
        :return: ``user_message`` 事件；不是用户请求时返回 ``None``。
        """
        message = payload.get("message")
        if isinstance(message, dict):
            content = message.get("content")
            if isinstance(content, str) and content.strip():
                event = self._base_event(payload)
                event["type"] = "user_message"
                event["text"] = content.strip()
                return event
            # 数组形态：只有**不含**工具回执时才算用户请求。
            if isinstance(content, list):
                kinds = {
                    str(block.get("type") or "")
                    for block in content
                    if isinstance(block, dict)
                }
                if kinds and not (kinds & {"tool_result", "tool-result"}):
                    text = self._first_text(content[0] if content else {}, ("text", "content"))
                    if text.strip():
                        event = self._base_event(payload)
                        event["type"] = "user_message"
                        event["text"] = text.strip()
                        return event
        text = payload.get("text")
        if isinstance(text, str) and text.strip():
            event = self._base_event(payload)
            event["type"] = "user_message"
            event["text"] = text.strip()
            return event
        return None
