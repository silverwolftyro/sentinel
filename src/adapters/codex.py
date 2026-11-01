"""Codex 适配器 —— 解析 ``codex exec --json`` 的输出。

输出格式（实测 codex-cli 0.162.1）
==================================

``--json`` 模式下每行一个 JSON 事件。它用的是和 mcode v0.6 **同一套**
``item.*`` 事件溯源协议，只是 **item 的字段名不一样**：

.. code-block:: json

    {"type": "thread.started", "thread_id": "01a17af0-…"}
    {"type": "turn.started"}
    {"type": "item.completed", "item": {"id": "item_0", "type": "agent_message",
                                        "text": "已经在当前目录创建了 hi.py"}}
    {"type": "item.started",   "item": {"id": "item_1", "type": "file_change",
                                        "changes": [{"path": "…/hi.py", "kind": "add"}],
                                        "status": "in_progress"}}
    {"type": "item.started",   "item": {"id": "item_2", "type": "command_execution",
                                        "command": "…/pwsh.exe -Command 'python hi.py'",
                                        "aggregated_output": "", "exit_code": null,
                                        "status": "in_progress"}}
    {"type": "item.completed", "item": {"id": "item_2", "type": "command_execution",
                                        "command": "…", "aggregated_output": "hi\\r\\n",
                                        "exit_code": 0, "status": "completed"}}
    {"type": "turn.completed", "usage": {"input_tokens": 73546, …}}

和 mcode 的关键差异
==================

======================  ==========================  ==========================
字段                    mcode v0.6                  codex 0.16x
======================  ==========================  ==========================
工具调用藏在哪          ``item.toolCall``           **直接在 ``item`` 上**
工具名                  ``item.toolCall.name``      由 ``item.type`` 推断
工具参数                ``item.toolCall.input``     ``item.command`` /
                                                    ``item.changes[]``
工具输出                ``item.toolCall.output``    ``item.aggregated_output``
Agent 消息正文          ``item.content``            ``item.text``
======================  ==========================  ==========================

**好处**：codex 的 ``command_execution`` 在 ``item.started`` 时
**命令就已经完整了** —— 守护者可以在命令执行前就动手，
比 mcode 还要早一步。
"""

from __future__ import annotations

from typing import Any, Dict, List

from .base import BaseAdapter, Event

__all__ = ["CodexAdapter"]


class CodexAdapter(BaseAdapter):
    """解析 Codex CLI 的 ``--json`` 事件流。"""

    name = "codex"

    def __init__(self) -> None:
        #: 已经产出过 ``tool_call`` 事件的 item ID（去重）。
        self._seen_items: set = set()

    def parse_payload(self, payload: Dict[str, Any]) -> List[Event]:
        """把一行 JSON 拆成 0..N 个统一事件。

        :param payload: 单行 JSON 解析后的字典。
        :return: 事件列表。
        """
        payload_type = str(payload.get("type") or "").strip().lower()

        # 会话级事件（thread.* / turn.* / error）不含可审查的动作。
        if payload_type.startswith(("thread.", "turn.")):
            # 但 ``turn.completed`` 带着 **usage**（真实 Token 账单）——
            # 那不是「可审查的动作」，可**是钱**。单独产出一个 usage 事件，
            # 让引擎的 Token 台账能采信真值（而不是靠字符估算）。
            if payload_type == "turn.completed" and isinstance(
                payload.get("usage"), dict
            ):
                event = self._base_event(payload)
                event["type"] = "usage"
                event["usage"] = dict(payload["usage"])
                event["text"] = ""
                return [event]
            return []
        if payload_type == "error":
            # codex 的错误事件只带一段消息文本。
            event = self._base_event(payload)
            event["type"] = "text"
            event["text"] = str(payload.get("message") or "")
            event["is_error"] = True
            return [event] if event["text"] else []

        item = payload.get("item")
        if not isinstance(item, dict):
            # --- 扁平形态：用户请求（自动锚定任务目标用，v3.0.0）---
            if payload_type in ("user_message", "user", "prompt", "human"):
                event = self._base_event(payload)
                event["type"] = "user_message"
                event["text"] = self._first_text(
                    payload, ("content", "text", "prompt", "message")
                )
                return [event] if event["text"] else []
            return []
        return self._parse_item(payload, item, payload_type)

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------
    def _parse_item(
        self,
        payload: Dict[str, Any],
        item: Dict[str, Any],
        payload_type: str,
    ) -> List[Event]:
        """解析一个 ``item.*`` 事件。

        :param payload: 整行载荷。
        :param item: ``item`` 对象。
        :param payload_type: 事件类型（``item.started`` / ``item.completed``）。
        :return: 事件列表。
        """
        item_type = str(item.get("type") or "").strip().lower()
        item_id = str(item.get("id") or "")
        completed = payload_type.endswith("completed")

        # --- 用户请求：自动锚定任务目标靠它（v3.0.0）---
        #
        # ⚠️ Codex 以前**没有**这个分支 ——
        # 意味着 codex 会话里用户最初那句话根本不被识别，
        # 引擎也就没法从 codex 的日志里自动锚定目标。
        # 现在补上：与 mcode / claude 保持一致。
        if item_type in ("user_message", "user", "human"):
            event = self._base_event(payload)
            event["type"] = "user_message"
            event["text"] = self._first_text(
                item, ("content", "text", "prompt", "message")
            )
            return [event] if event["text"] else []

        # --- Agent 消息：完成时才有全文 ---
        if item_type in ("agent_message", "assistant_message", "message"):
            if not completed:
                return []
            event = self._base_event(payload)
            event["type"] = "text"
            event["text"] = self._first_text(item, ("text", "content", "message"))
            return [event] if event["text"] else []

        # --- 模型的思考过程 ---
        if item_type in ("reasoning", "thinking"):
            if not completed:
                return []
            event = self._base_event(payload)
            event["type"] = "thinking"
            event["text"] = self._first_text(item, ("text", "content", "summary"))
            return [event] if event["text"] else []

        # --- 命令执行 ---
        if item_type in ("command_execution", "exec_command", "shell"):
            return self._parse_command_execution(
                payload, item, item_id, completed
            )

        # --- 文件改动 ---
        if item_type in ("file_change", "patch_apply"):
            return self._parse_file_change(payload, item, item_id, completed)

        # --- 其它工具（MCP / 搜索等）：当作工具调用 ---
        if item_type and item_id and item_id not in self._seen_items:
            self._seen_items.add(item_id)
            event = self._base_event(payload)
            event["type"] = "tool_call"
            arguments = item.get("input")
            if not isinstance(arguments, dict):
                arguments = {
                    key: value
                    for key, value in item.items()
                    if key not in ("id", "type", "status")
                }
            event["tool"] = {
                "name": item_type,
                "arguments": arguments,
                "command": self._extract_command(arguments),
            }
            return [event]
        return []

    def _parse_command_execution(
        self,
        payload: Dict[str, Any],
        item: Dict[str, Any],
        item_id: str,
        completed: bool,
    ) -> List[Event]:
        """解析 ``command_execution``（codex 的命令行执行）。

        ⚠️ **命令在 ``item.started`` 时就是完整的** ——
        所以在这里就产出 ``tool_call``，守护者能在命令执行前动手。

        :param payload: 整行载荷。
        :param item: ``item`` 对象。
        :param item_id: item ID（用于去重）。
        :param completed: 是不是完成事件。
        :return: 事件列表。
        """
        command = str(item.get("command") or "")
        events: List[Event] = []

        if command and item_id not in self._seen_items:
            self._seen_items.add(item_id)
            event = self._base_event(payload)
            event["type"] = "tool_call"
            event["tool"] = {
                "name": "shell",
                "arguments": {"command": command},
                "command": command,
            }
            events.append(event)

        if completed:
            result = self._base_event(payload)
            result["type"] = "tool_result"
            result["text"] = str(item.get("aggregated_output") or "")
            exit_code = item.get("exit_code")
            # ⚠️ 只认**明确的**非零退出码 —— 缺了就是「不知道」，
            # 不能当成失败（宁可漏报不误报）。
            result["is_error"] = (
                isinstance(exit_code, int) and not isinstance(exit_code, bool)
                and exit_code != 0
            )
            if item_id:
                result["tool_use_id"] = item_id
            events.append(result)
        return events

    def _parse_file_change(
        self,
        payload: Dict[str, Any],
        item: Dict[str, Any],
        item_id: str,
        completed: bool,
    ) -> List[Event]:
        """解析 ``file_change``（codex 的文件改动）。

        :param payload: 整行载荷。
        :param item: ``item`` 对象。
        :param item_id: item ID（用于去重）。
        :param completed: 是不是完成事件。
        :return: 事件列表。
        """
        changes = item.get("changes")
        paths: List[str] = []
        if isinstance(changes, list):
            for change in changes:
                if isinstance(change, dict):
                    path = change.get("path")
                    if isinstance(path, str) and path:
                        paths.append(path)
                elif isinstance(change, str) and change:
                    paths.append(change)

        events: List[Event] = []
        if paths and item_id not in self._seen_items:
            self._seen_items.add(item_id)
            event = self._base_event(payload)
            event["type"] = "tool_call"
            event["tool"] = {
                "name": "write",
                "arguments": {"path": paths[0], "paths": paths},
                # 文件改动没有「命令文本」，但路径本身就是信号 ——
                # 意图漂移的窗口摘要会把它显示成 ``path=…``。
                "command": "",
            }
            events.append(event)

        if completed:
            result = self._base_event(payload)
            result["type"] = "tool_result"
            result["text"] = "、".join(paths)
            result["is_error"] = False
            if item_id:
                result["tool_use_id"] = item_id
            events.append(result)
        return events
