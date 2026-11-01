"""mcode 适配器 —— 解析 ``mcode exec --output-format stream-json`` 的输出。

输出格式
========
``stream-json`` 模式下每行一个 JSON 对象。哨兵已知的行形态：

.. code-block:: json

    {"type": "thinking",  "thinking": "我要先看看配置文件……"}
    {"type": "tool_call", "tool_call": {"name": "Bash",
                                       "arguments": {"command": "ls -la"}}}
    {"type": "text",      "text": "好的，已经列出了目录内容。"}
    {"type": "result",    "result": {"status": "ok"}}

同时兼容 Claude Code 风格的 ``assistant`` 消息（一行里可能同时带
``thinking`` 与 ``tool_use`` 两个内容块）：

.. code-block:: json

    {"type": "assistant", "message": {"content": [
        {"type": "thinking", "thinking": "先读文件"},
        {"type": "tool_use", "name": "Read", "input": {"file_path": "a.py"}}
    ]}}

为什么要写得这么「宽容」
======================
不同版本的 mcode 在 ``tool_call`` 的字段命名上并不统一（``tool_call``
对象 / 平铺的 ``name`` + ``arguments`` / 平铺的 ``name`` + ``args``）。
适配器在这里做**归一化**，把这些差异收敛成统一事件结构，避免规则层去猜
字段名 —— 规则层只认「工具名 + 参数」这一个契约。

抽取重点
========
* ``thinking`` → 事件 ``type="thinking"``，正文放在 ``text``；
* ``tool_call`` → 事件 ``type="tool_call"``，工具名与参数放在 ``tool``，
  并从参数中尽力提取出命令文本 ``tool["command"]``（供规则层做正则匹配）。
"""

from __future__ import annotations

from typing import Any, Dict, List

from .base import BaseAdapter, Event

__all__ = ["McodeAdapter"]


class McodeAdapter(BaseAdapter):
    """解析 mcode 的 ``stream-json`` 输出。

    支持两代协议
    ============

    **旧协议**（v0.5 及以前）—— 一行一个动作::

        {"type": "thinking",  "thinking": "我要先看看配置文件……"}
        {"type": "tool_call", "tool_call": {"name": "Bash",
                                           "arguments": {"command": "ls -la"}}}

    **v0.6 事件溯源协议** —— 一行一个**事件**，工具调用藏在 ``item`` 里::

        {"type": "item.started",   "item": {"type": "tool_call",
                                          "toolCall": {"name": "bash"}}}
        {"type": "item.updated",   "item": {"type": "tool_call",
                                          "toolCall": {"name": "bash",
                                                       "input": {"command": "ls"}}}}
        {"type": "item.completed", "item": {"type": "tool_call",
                                          "toolCall": {"name": "bash",
                                                       "output": {...}}}}

    同一次调用会被**反复推送**（``started`` → 多个 ``updated`` → ``completed``），
    所以靠 ``toolCall.id`` 去重：只在**参数第一次出现**时产出 ``tool_call`` 事件 ——
    那正是工具**执行前**的瞬间，守护者还来得及挂起进程。

    为什么要写得这么「宽容」
    =======================
    不同版本的 mcode 在 ``tool_call`` 的字段命名上并不统一（``tool_call``
    对象 / 平铺的 ``name`` + ``arguments`` / 平铺的 ``name`` + ``args``）。
    适配器在这里做**归一化**，把这些差异收敛成统一事件结构，避免规则层去猜
    字段名 —— 规则层只认「工具名 + 参数」这一个契约。

    抽取重点
    ==========
    * ``thinking`` → 事件 ``type="thinking"``，正文放在 ``text``；
    * ``tool_call`` → 事件 ``type="tool_call"``，工具名与参数放在 ``tool``，
      并从参数中尽力提取出命令文本 ``tool["command"]``（供规则层做正则匹配）。
    * ``user_message`` → 事件 ``type="user_message"`` ——
      引擎据此**自动锚定**任务目标（v0.8.0）。
    """

    name = "mcode"

    def __init__(self) -> None:
        #: 已经产出过 ``tool_call`` 事件的调用 ID（v0.6 协议去重）。
        self._seen_calls: set = set()

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------
    def parse_payload(self, payload: Dict[str, Any]) -> List[Event]:
        """把一行 JSON 拆成 0..N 个统一事件。"""
        payload_type = str(payload.get("type") or "").strip().lower()

        # 0) mcode v0.6 的事件溯源协议（item.* 事件）
        if payload_type.startswith("item.") or isinstance(
            payload.get("item"), dict
        ):
            return self._parse_item_event(payload)

        # 0.5) v0.6 协议的**会话级**事件（exec.* / session.* / turn.*）
        # —— 它们只是生命周期标记，不含可审查的动作，直接忽略，
        # 否则会凭空多出一堆 "unknown" 事件。
        if "schemaVersion" in payload and "runId" in payload:
            return []

        # 1) Claude 风格：assistant 消息里带 content 数组
        events = self._parse_message_container(payload)
        if events:
            return events

        # 2) 扁平形态：thinking
        if payload_type == "thinking":
            event = self._base_event(payload)
            event["type"] = "thinking"
            event["text"] = self._first_text(
                payload, ("thinking", "text", "content", "reasoning")
            )
            return [event]

        # 2.5) 扁平形态：用户请求（自动锚定任务目标用）
        if payload_type in ("user_message", "user", "prompt", "human"):
            event = self._base_event(payload)
            event["type"] = "user_message"
            event["text"] = self._first_text(
                payload, ("content", "text", "prompt", "message", "user_message")
            )
            return [event]

        # 3) 扁平形态：tool_call
        if payload_type in ("tool_call", "tool_use", "tool-call"):
            event = self._parse_tool_call(payload)
            return [event] if event else []

        # 4) 扁平形态：tool_result（工具调用的回执）
        if payload_type in ("tool_result", "tool-result", "tool_result_chunk"):
            event = self._parse_tool_result(payload)
            return [event] if event else []

        # 5) 扁平形态：text / result
        if payload_type in ("text", "message", "assistant_text"):
            event = self._base_event(payload)
            event["type"] = "text"
            event["text"] = self._first_text(payload, ("text", "content", "message"))
            return [event]

        if payload_type == "result":
            event = self._base_event(payload)
            event["type"] = "result"
            event["text"] = self._first_text(payload, ("result", "text", "content"))
            event["is_error"] = self._detect_error(payload)
            return [event]

        # 5) 兜底：类型未知，但载荷里塞了 tool_call / thinking
        event = self._parse_tool_call(payload)
        if event is not None:
            return [event]
        if payload_type:
            event = self._base_event(payload)
            event["type"] = "unknown"
            event["text"] = self._first_text(payload, ("text", "content", "message"))
            return [event]
        return []

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------
    def _parse_item_event(self, payload: Dict[str, Any]) -> List[Event]:
        """解析 mcode v0.6 的 ``item.*`` 事件。

        :param payload: 一行 JSON。
        :return: 事件列表。
        """
        item = payload.get("item")
        if not isinstance(item, dict):
            return []
        item_type = str(item.get("type") or "").strip().lower()
        phase = str(payload.get("type") or "").split(".")[-1].lower()

        # --- 用户请求：自动锚定任务目标靠它 ---
        if item_type == "user_message":
            event = self._base_event(payload)
            event["type"] = "user_message"
            event["text"] = self._first_text(
                item, ("content", "text", "prompt", "message")
            )
            return [event] if event["text"] else []

        # --- Agent 消息：只在完成时取全文（中途是碎片） ---
        if item_type in ("agent_message", "assistant_message", "message"):
            if phase != "completed":
                return []
            event = self._base_event(payload)
            event["type"] = "text"
            event["text"] = self._first_text(item, ("content", "text", "message"))
            return [event] if event["text"] else []

        # --- 思考 ---
        # mcode v0.6 把「推理 / 自述」放在 ``reasoning`` item 里
        # （实测：``{"id": "...:reasoning", "type": "reasoning",
        # "content": "Verified. Let me also confirm ..."}``）。
        # 它就是「Agent 自己怎么想的」—— 意图漂移判定最看重的信号，
        # 所以映射成统一的 ``thinking`` 事件。
        if item_type in ("thinking", "reasoning", "thought"):
            event = self._base_event(payload)
            event["type"] = "thinking"
            event["text"] = self._first_text(
                item, ("thinking", "content", "text", "reasoning")
            )
            return [event] if event["text"] else []

        # --- 工具调用 ---
        if item_type in ("tool_call", "tool_use"):
            return self._parse_v6_tool_call(payload, item, phase)

        # --- 工具回执 ---
        if item_type in ("tool_result", "tool-result"):
            return self._parse_tool_result(item, parent=payload)

        return []

    def _parse_v6_tool_call(
        self,
        payload: Dict[str, Any],
        item: Dict[str, Any],
        phase: str,
    ) -> List[Event]:
        """解析 v0.6 协议里的工具调用。

        :param payload: 整行载荷。
        :param item: ``item`` 对象。
        :param phase: 事件阶段（``started`` / ``updated`` / ``completed``）。
        :return: 事件列表。
        """
        call = item.get("toolCall")
        call = call if isinstance(call, dict) else {}
        name = str(call.get("name") or "")
        arguments = call.get("input")
        arguments = arguments if isinstance(arguments, dict) else {}
        call_id = str(call.get("id") or item.get("id") or "")

        events: List[Event] = []

        # 参数第一次出现 = 工具即将执行 —— 此刻最有价值（守护者还来得及动手）。
        if arguments and call_id not in self._seen_calls:
            self._seen_calls.add(call_id)
            event = self._base_event(payload)
            event["type"] = "tool_call"
            event["tool"] = {
                "name": name or "unknown",
                "arguments": arguments,
                "command": self._extract_command(arguments),
            }
            events.append(event)

        # 完成 —— 产出回执（上下文追踪器靠它判断这次调用成功没有）。
        if phase == "completed" and call_id:
            output = call.get("output")
            result = self._base_event(payload)
            result["type"] = "tool_result"
            result["is_error"] = self._v6_output_failed(output)
            result["text"] = self._v6_output_text(output)
            result["tool_use_id"] = call_id
            events.append(result)
        return events

    @staticmethod
    def _v6_output_text(output: Any) -> str:
        """从 v0.6 的工具输出里取出正文。

        :param output: ``toolCall.output`` 对象。
        :return: 正文文本；取不到时返回空串。
        """
        if not isinstance(output, dict):
            return ""
        content = output.get("content")
        if isinstance(content, list):
            parts: List[str] = []
            for block in content:
                if isinstance(block, dict):
                    text = block.get("text")
                    if isinstance(text, str) and text:
                        parts.append(text)
                elif isinstance(block, str) and block:
                    parts.append(block)
            return "\n".join(parts)
        if isinstance(content, str):
            return content
        return ""

    @staticmethod
    def _v6_output_failed(output: Any) -> bool:
        """判断 v0.6 的工具输出表示「失败」。

        ⚠️ **只认明确的失败标记**，绝不从 ``status`` 之类的数字去猜 ——
        宁可漏报也不误报（误报会让上下文追踪器误判成「死循环」）。

        :param output: ``toolCall.output`` 对象。
        :return: 失败返回 ``True``。
        """
        if not isinstance(output, dict):
            return False
        details = output.get("details")
        holders = [output]
        if isinstance(details, dict):
            holders.append(details)
        for holder in holders:
            flag = holder.get("is_error", holder.get("isError"))
            if isinstance(flag, bool) and flag:
                return True
            error = holder.get("error")
            if isinstance(error, str) and error.strip():
                return True
            if isinstance(error, dict) and error:
                return True
            success = holder.get("success")
            if isinstance(success, bool) and not success:
                return True
        return False
