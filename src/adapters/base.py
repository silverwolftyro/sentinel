"""采集层 —— 适配器抽象基类。

哨兵以**只读**方式读取被监控 Agent 的日志：只打开、只读取、绝不写入、
绝不截断、绝不删除。本模块定义所有适配器共享的抽象与工具方法。

统一事件（Event）结构
=====================
适配器产出的每个事件都是一个普通 ``dict``（保持 JSON 友好，便于直接落盘）::

    {
      "source":     "mcode",       # 归一化来源标识
      "type":       "tool_call",   # thinking / tool_call / text / result / unknown
      "session_id": None,          # 会话 ID（可能缺失）
      "timestamp":  1750000000.0,  # 事件自带时间戳（可能为 None）
      "tool":       {              # 仅 tool_call 事件有值
                       "name":      "Bash",
                       "arguments": {"command": "rm -rf /"},
                       "command":   "rm -rf /",   # 从参数中提取的命令文本
                     },
      "text":       "",            # thinking / text 事件的正文
      "is_error":   False,         # tool_result 事件：这次调用是否失败
      "raw":        {},            # 原始 JSON 载荷
    }

设计约束
========
* 解析失败**不得**中断监听 —— 采集侧异常一律跳过该行并记录日志（红线 R5）；
* 适配器不做任何内容判断，不决定告警等级，不调用模型；
* **只读** —— 无论 ``read_stream`` 还是 ``tail_stream``，
  都以只读方式打开文件，绝不写入、截断、删除（红线 R1）。
"""

from __future__ import annotations

import abc
import json
import logging
import os
import threading
import time
from typing import IO, Any, Dict, Iterator, List, Optional, Union

from ..tailer import FileTailer
from .fingerprint import extract_fingerprint

logger = logging.getLogger(__name__)

__all__ = ["BaseAdapter", "Event", "Source"]

#: 统一事件类型别名。
Event = Dict[str, Any]

#: ``read_stream`` 接受的输入：文件路径，或已经打开的文本流（管道 / stdin）。
Source = Union[str, "os.PathLike[str]", IO[str]]

#: 可能承载命令文本的参数名（按优先级排列）。
_COMMAND_KEYS = (
    "command",
    "cmd",
    "commandLine",
    "command_line",
    "shell",
    "script",
    "code",
    "text",
)

#: 可能承载工具名的参数名（按优先级排列）。
_TOOL_NAME_KEYS = ("name", "tool", "tool_name", "toolName", "function")

#: 可能承载工具参数的参数名（按优先级排列）。
_TOOL_ARGS_KEYS = ("arguments", "args", "input", "parameters", "params")

#: 可能**嵌套**着工具调用的键（按优先级排列，v3.8.0 补齐）。
#:
#: ⚠️ 这一组必须与前端看板的解析口径**完全一致** ——
#:    与 :data:`src.gui.static.dashboard.js` 里的 ``logToolName`` /
#:    ``logToolArguments`` 用的是同一组键。
#:    两边只要有一边不认识某个形态，就会出现
#:    「日志里有工具名，看板解析不出来」或者
#:    「看板能解析，后端却当没这回事」的错位。
_NESTED_TOOL_KEYS = (
    "tool",
    "tool_call",
    "toolCall",
    "toolName",
    "function",
    "function_call",
    "functionCall",
    "call",
)


class BaseAdapter(abc.ABC):
    """所有 CLI 工具流式日志适配器的抽象基类。

    子类只需实现 :meth:`parse_payload`，即可获得逐行读取、跳过坏行、
    空行过滤等通用能力。
    """

    #: 适配器名称，同时作为事件的 ``source`` 字段。
    name: str = "base"

    # ------------------------------------------------------------------
    # 子类必须实现
    # ------------------------------------------------------------------
    @abc.abstractmethod
    def parse_payload(self, payload: Dict[str, Any]) -> List[Event]:
        """把一个**已解析**的 JSON 对象拆成 0..N 个统一事件。

        :param payload: 单行 JSON 解析后的字典。
        :return: 事件列表；无法识别时返回空列表。
        """

    # ------------------------------------------------------------------
    # 通用实现
    # ------------------------------------------------------------------
    def parse_line(self, line: str) -> List[Event]:
        """解析一行原始文本。

        非法的 JSON、不是对象的 JSON 一律返回空列表（跳过该行）。

        :param line: 一行原始文本（不带换行符）。
        :return: 事件列表，通常 0 或 1 个，assistant 消息可能拆出多个。
        """
        stripped = line.strip()
        if not stripped:
            return []
        try:
            payload = json.loads(stripped)
        except (json.JSONDecodeError, ValueError):
            return []
        if not isinstance(payload, dict):
            return []
        return self.parse_payload(payload)

    def read_stream(self, file_path_or_pipe: Source) -> Iterator[Event]:
        """逐行读取并 yield 解析后的事件。

        这是采集层的**唯一对外入口**。参数既可以是文件路径，也可以是已经
        打开的文本流（``sys.stdin``、子进程管道等）。本方法**不关闭**调用方
        传入的流。

        :param file_path_or_pipe: 日志文件路径，或已打开的文本流。
        :yield: 统一事件字典。
        """
        for raw in self.iter_raw_lines(file_path_or_pipe):
            if not raw.strip():
                continue
            try:
                events = self.parse_line(raw)
            except Exception as exc:  # noqa: BLE001
                # 采集侧异常绝不能中断监听（红线 R5：旁路失败）。
                logger.warning("[%s] 解析一行失败，已跳过：%s", self.name, exc)
                continue
            for event in events:
                if event is not None:
                    yield event

    def iter_raw_lines(self, file_path_or_pipe: Source) -> Iterator[str]:
        """把「路径」或「已打开的流」统一成逐行文本迭代器。

        :param file_path_or_pipe: 日志文件路径，或已打开的文本流。
        :yield: 原始文本行。
        """
        if hasattr(file_path_or_pipe, "read") or hasattr(file_path_or_pipe, "readline"):
            stream = file_path_or_pipe
            for raw in stream:  # type: ignore[union-attr]
                yield self._decode(raw)
            return

        path = os.fspath(file_path_or_pipe)  # type: ignore[arg-type]
        # 只读模式打开，绝不写入被监控文件（红线 R1）。
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for raw in handle:
                yield raw

    # ------------------------------------------------------------------
    # 实时跟随
    # ------------------------------------------------------------------
    def tail_stream(
        self,
        file_path: Source,
        *,
        interval: float = 0.5,
        from_end: bool = True,
        stop_event: Optional[threading.Event] = None,
    ) -> Iterator[Event]:
        """``tail -f`` 式地**持续**产出新增事件。

        与 :meth:`read_stream` 的区别
        ===========================

        ``read_stream``  读**已存在**的全部内容然后结束 —— 事后验尸官。
        ``tail_stream``  一直阻塞着，文件被写入时立刻产出新事件 —— 实时审计。

        ⚠️ **起点在调用时锁定**
        ========================
        本方法**不是**生成器函数，而是「普通函数返回生成器」。
        因为「从文件末尾开始」这个起点**必须在调用那一刻**确定 ——
        如果写成生成器函数，函数体要等到第一次迭代才执行，
        那一刻文件里已经可能写进了新内容，**它们会被静默跳过**。
        这个坑是实测出来的（见 ``tests/test_tail_stream.py``）。

        边界情况
        ========

        | 情况 | 处理 |
        | --- | --- |
        | 文件还不存在 | 等到它出现（不抛异常、不退出）；此时它的全部内容都算「新增」 |
        | 文件被**轮转**（同名换新文件） | 识别文件身份变化，偏移重置为 0，**新文件从头读** |
        | 文件被**截断**（清空重写） | 识别大小回退，偏移重置为 0 |
        | 写入只写了一半行 | 半行留在缓冲区，等补齐后才产出（不会截半行） |
        | 文件被删掉又建回来 | 同轮转处理 |
        | 读取过程中出错 | 记录日志后继续等，**绝不中断监听** |

        :param file_path: 日志文件路径。
        :param interval: 轮询间隔（秒）。
        :param from_end: 为 ``True``（默认）时只产出**新增**内容，
            这是 ``tail -f`` 的语义；为 ``False`` 时先读完历史内容再跟随。
        :param stop_event: 停止信号；置位后本方法尽快返回。
        :return: 产出事件的迭代器。
        """
        path = os.fspath(file_path)  # type: ignore[arg-type]
        tailer = FileTailer(path)
        if from_end and os.path.exists(path):
            # 关键：在**调用时**就记住起点，不能等到首次迭代。
            tailer.skip_to_end()
        return self._follow(
            path, tailer, interval, from_end, stop_event
        )

    def _follow(
        self,
        path: str,
        tailer: FileTailer,
        interval: float,
        from_end: bool,
        stop_event: Optional[threading.Event],
    ) -> Iterator[Event]:
        """真正的跟随循环（由 :meth:`tail_stream` 返回）。"""
        # 文件可能还不存在 —— 等它出现，但绝不因此退出。
        while not os.path.exists(path):
            if stop_event is not None and stop_event.is_set():
                return
            logger.info("等待日志文件出现：%s", path)
            if stop_event is not None:
                if stop_event.wait(max(float(interval), 0.05)):
                    return
            else:
                time.sleep(max(float(interval), 0.05))

        if not from_end:
            for raw in tailer.prime():
                for event in self._events_from_line(raw):
                    yield event

        while True:
            if stop_event is not None and stop_event.is_set():
                return
            try:
                lines = tailer.poll()
            except Exception as exc:  # noqa: BLE001 —— 采集侧永不中断
                logger.warning("[%s] 跟随 %s 失败，已跳过：%s", self.name, path, exc)
                lines = []
            for raw in lines:
                for event in self._events_from_line(raw):
                    yield event
            if not lines:
                # 没有新内容时让出 CPU；stop_event 也能立刻唤醒。
                if stop_event is not None:
                    if stop_event.wait(max(float(interval), 0.05)):
                        return
                else:
                    time.sleep(max(float(interval), 0.05))

    def _events_from_line(self, raw: str) -> List[Event]:
        """把一行原始文本解析成事件列表（解析失败就跳过，绝不抛异常）。"""
        if not raw or not raw.strip():
            return []
        try:
            events = self.parse_line(raw)
        except Exception as exc:  # noqa: BLE001
            # 采集侧异常绝不能中断监听（红线 R5：旁路失败）。
            logger.warning("[%s] 解析一行失败，已跳过：%s", self.name, exc)
            return []
        return [event for event in events if event is not None]

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    @staticmethod
    def _decode(raw: Any) -> str:
        """把管道里可能出现的 ``bytes`` 解码成文本。"""
        if isinstance(raw, bytes):
            return raw.decode("utf-8", errors="replace")
        return raw

    # ------------------------------------------------------------------
    # 消息容器解析（Anthropic / Claude 风格）
    # ------------------------------------------------------------------
    # 这部分与**具体产品无关** —— Claude Code、mcode 旧协议都用这套
    # ``{"type":"assistant","message":{"content":[...]}}`` 结构。
    # 放在基类里，各产品适配器只写自己**特有**的那部分，
    # 免得同一套归一化逻辑被复制成 N 份、改一处漏一处。
    def _parse_message_container(self, payload: Dict[str, Any]) -> List[Event]:
        """解析 ``{"type":"assistant","message":{"content":[...]}}`` 形态。"""
        message = payload.get("message")
        if not isinstance(message, dict):
            return []
        content = message.get("content")
        if isinstance(content, str):
            event = self._base_event(payload)
            event["type"] = "text"
            event["text"] = content
            return [event]
        if not isinstance(content, list):
            return []

        events: List[Event] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            event = self._parse_block(payload, block)
            if event is not None:
                events.append(event)
        return events

    def _parse_block(
        self,
        payload: Dict[str, Any],
        block: Dict[str, Any],
    ) -> Optional[Event]:
        """解析 ``content`` 数组里的单个内容块。"""
        block_type = str(block.get("type") or "").strip().lower()

        if block_type == "thinking":
            event = self._base_event(payload)
            event["type"] = "thinking"
            event["text"] = self._first_text(block, ("thinking", "text", "content"))
            return event

        if block_type in ("tool_use", "tool_call", "tool-call"):
            return self._parse_tool_call(block, parent=payload)

        if block_type in ("tool_result", "tool-result"):
            return self._parse_tool_result(block, parent=payload)

        if block_type == "text":
            event = self._base_event(payload)
            event["type"] = "text"
            event["text"] = self._first_text(block, ("text", "content"))
            return event

        return None

    def _parse_tool_call(
        self,
        payload: Dict[str, Any],
        parent: Optional[Dict[str, Any]] = None,
    ) -> Optional[Event]:
        """解析工具调用，归一化为 ``{"name", "arguments", "command"}``。"""
        # 可能嵌套在 tool / tool_call / toolCall / function 之类的键里
        inner: Dict[str, Any] = payload
        for key in _NESTED_TOOL_KEYS:
            nested = payload.get(key)
            if isinstance(nested, dict):
                inner = nested
                break

        name = self._first_str(inner, _TOOL_NAME_KEYS)
        if not name:
            name = self._first_str(payload, _TOOL_NAME_KEYS)

        arguments = self._first_dict(inner, _TOOL_ARGS_KEYS)
        if not arguments:
            arguments = self._first_dict(payload, _TOOL_ARGS_KEYS)

        if not name and not arguments:
            return None

        event = self._base_event(parent if parent is not None else payload)
        event["type"] = "tool_call"
        event["tool"] = {
            "name": name or "unknown",
            "arguments": arguments,
            "command": self._extract_command(arguments),
        }
        return event

    @staticmethod
    def _detect_error(payload: Dict[str, Any]) -> bool:
        """判断载荷是否表示一次**失败**。

        不同工具的失败标记并不统一，常见的有：
        ``is_error``、``error`` 非空、``success=False``、
        ``status`` ∈ {error, failed, failure}、``subtype`` 含 error。
        """
        flag = payload.get("is_error")
        if isinstance(flag, bool):
            return flag
        if isinstance(flag, str) and flag.lower() in ("true", "1", "yes"):
            return True

        error_value = payload.get("error")
        if isinstance(error_value, str) and error_value.strip():
            return True
        if isinstance(error_value, dict) and error_value:
            return True

        success = payload.get("success")
        if isinstance(success, bool):
            return not success

        for key in ("status", "subtype", "state"):
            value = payload.get(key)
            if isinstance(value, str) and value.lower() in (
                "error",
                "failed",
                "failure",
                "timeout",
                "cancelled",
                "canceled",
            ):
                return True
        return False

    def _parse_tool_result(
        self,
        payload: Dict[str, Any],
        parent: Optional[Dict[str, Any]] = None,
    ) -> Optional[Event]:
        """解析工具回执。

        回执里最关键的信息是「这次调用**失败了没有**」——
        上下文追踪器的死循环检测（连续测试失败）依赖它。

        ⚠️ 扁平形态的回执会把内容**嵌在** ``tool_result`` 里::

            {"type": "tool_result",
             "tool_result": {"status": "error", "output": "FAILED ..."}}

        这里必须像 :meth:`_parse_tool_call` 那样**拆开这一层**——
        否则「失败了没有」和回执正文会一起丢掉，
        死循环检测（``[STUCK]``）与外泄检测（``[LEAK]``）
        对这种格式**永远不会触发**。
        """
        inner: Dict[str, Any] = payload
        for key in ("tool_result", "toolResult"):
            nested = payload.get(key)
            if isinstance(nested, dict):
                inner = nested
                break

        event = self._base_event(parent if parent is not None else payload)
        event["type"] = "tool_result"
        event["is_error"] = self._detect_error(inner) or self._detect_error(
            payload
        )
        text_keys = ("content", "text", "output", "result", "error")
        event["text"] = self._first_text(inner, text_keys) or self._first_text(
            payload, text_keys
        )
        tool_use_id = payload.get("tool_use_id") or payload.get("toolUseId")
        if not isinstance(tool_use_id, str):
            tool_use_id = inner.get("tool_use_id") or inner.get("toolUseId")
        if isinstance(tool_use_id, str):
            event["tool_use_id"] = tool_use_id
        return event

    @staticmethod
    def _extract_command(arguments: Dict[str, Any]) -> str:
        """从工具参数中尽力提取命令文本。

        参数值可能是字符串，也可能是字符串列表（多行脚本）。
        """
        for key in _COMMAND_KEYS:
            value = arguments.get(key)
            if isinstance(value, str) and value.strip():
                return value
            if isinstance(value, list):
                parts = [str(item) for item in value if isinstance(item, str)]
                if parts:
                    return "\n".join(parts)
        return ""

    @staticmethod
    def _first_text(payload: Dict[str, Any], keys: Any) -> str:
        """按优先级取第一个非空文本字段。"""
        for key in keys:
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
            if isinstance(value, list):
                parts = [str(item) for item in value if isinstance(item, str)]
                if parts:
                    return "\n".join(parts)
        return ""

    @staticmethod
    def _first_str(payload: Dict[str, Any], keys: Any) -> str:
        """按优先级取第一个非空字符串字段。"""
        for key in keys:
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value
        return ""

    @staticmethod
    def _first_dict(payload: Dict[str, Any], keys: Any) -> Dict[str, Any]:
        """按优先级取第一个字典字段。"""
        for key in keys:
            value = payload.get(key)
            if isinstance(value, dict):
                return value
            if isinstance(value, str) and value.strip().startswith("{"):
                try:
                    parsed = json.loads(value)
                except (json.JSONDecodeError, ValueError):
                    continue
                if isinstance(parsed, dict):
                    return parsed
        return {}

    def _base_event(self, payload: Dict[str, Any]) -> Event:
        """构造带公共字段的事件骨架。"""
        return {
            "source": self.name,
            "type": "unknown",
            "session_id": self._extract_session_id(payload),
            # ⚠️ v3.5.0：线程指纹 ——
            #    多个 Agent 并行写同一个日志文件时，
            #    这一行是「我是哪一路」的凭据。
            #    认不出来就是空串（= 归到默认那一路，
            #    与 v3.4.0 之前的行为完全一样）。
            "fingerprint": extract_fingerprint(payload),
            "timestamp": self._extract_timestamp(payload),
            "tool": None,
            "text": "",
            "is_error": False,
            "raw": payload,
        }

    @staticmethod
    def _extract_session_id(payload: Dict[str, Any]) -> Optional[str]:
        """从载荷中提取会话 ID（兼容多种命名）。"""
        for key in ("session_id", "sessionId", "session"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
        return None

    @staticmethod
    def _extract_timestamp(payload: Dict[str, Any]) -> Optional[float]:
        """从载荷中提取时间戳；没有就用 ``None``，绝不伪造时间。"""
        for key in ("timestamp", "ts", "time", "created_at"):
            value = payload.get(key)
            if isinstance(value, bool):
                continue
            if isinstance(value, (int, float)):
                return float(value)
            if isinstance(value, str):
                try:
                    return float(value)
                except ValueError:
                    continue
        return None
