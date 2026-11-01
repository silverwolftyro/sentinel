"""主流程控制 —— 哨兵调度器。

:class:`SentinelEngine` 把采集层与规则层串起来：

    Adapter.read_stream(文件) ──▶ RuleEngine.check_event(事件)
                                        │
                                   DANGEROUS / LOOP
                                        │
                             终端红色告警 + logs/sentinel_alert.log

哨兵在这一层**只做观察与告警**，不会修改被监控 Agent 的任何东西，
也不会终止任何进程（架构红线 R1 / R2 / R5）。
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import logging
import os
import queue
import re
import sys
import threading
import time
import uuid
from collections import deque
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

from ..adapters.base import BaseAdapter, Event
from ..adapters.fingerprint import fingerprint_label, fingerprint_stream_key
from ..llm.cascade import CascadeWorker
from ..rules.basic import (
    SEVERITY_RED_LINE,
    SEVERITY_STANDARD,
    VERDICT_DANGEROUS,
    VERDICT_LOOP,
    VERDICT_ON_TRACK,
    RuleEngine,
)
from ..rules.registry import (
    L1_ACTION_ALERT,
    L1_ACTION_ESCALATE,
    L1_ACTION_RED_LINE_ONLY,
    L1_ACTION_SUSPEND,
    VALID_L1_ACTIONS,
)
from .channels import (
    TRIGGER_CASCADE_BUDGET,
    TRIGGER_CONTEXT_ANOMALY,
    TRIGGER_ENGINE_OFFLINE,
    TRIGGER_GUARDIAN_INTERCEPT,
    TRIGGER_INTENT_DRIFT,
    TRIGGER_MICRO_STATS,
    TRIGGER_TIMEBOX,
)
from .engine_watchdog import (
    VERDICT_ENGINE_OFFLINE,
    VERDICT_ENGINE_RECOVERED,
)
from .l2_scheduler import L2Scheduler
from .live_audit import extract_snippet, summarize_event
from .log_compressor import (
    CompressionOptions,
    compress_events,
    extract_intent,
    fallback_intent,
)
from .memory import StreamMemory
from .micro_stats import MicroStatFinding, finding_to_verdict
from .priority_queue import PRIORITY_P0, PRIORITY_P1, PRIORITY_P2
from .timebox import VERDICT_TIMEBOX
from .tokens import TokenLedger, extract_usage

__all__ = ["DEFAULT_ALERT_LOG", "SentinelEngine"]

logger = logging.getLogger(__name__)

#: 告警日志的默认落盘位置。
DEFAULT_ALERT_LOG = os.path.join("logs", "sentinel_alert.log")

#: ANSI 颜色。
_RED = "\033[91m"
_BOLD_RED = "\033[1;91m"
_RESET = "\033[0m"


def _supports_ansi(stream: Any = None) -> bool:
    """判断当前终端是否支持 ANSI 颜色。"""
    if os.environ.get("NO_COLOR"):
        return False
    target = stream if stream is not None else sys.stdout
    isatty = getattr(target, "isatty", None)
    try:
        return bool(isatty and isatty())
    except (ValueError, OSError):
        return False


def _prepare_stdout() -> None:
    """让「重定向 / 管道」场景下的终端输出也是 UTF-8。

    Windows 控制台本身走 UTF-16 通道，中文天然正常；但一旦 stdout 被
    重定向到文件或管道，Python 会退回系统 ANSI 代码页，中文就变成乱码。
    这里只在**非交互**场景切换编码，不影响真实终端。

    同时打开**行缓冲** —— 流式监听（``--tail``）的价值就在于「看得见它在动」，
    要是输出被重定向到文件后还攒着 8KB 才落盘，那实时性就没了。

    stderr 同样要处理 —— 哨兵的告警与降级提示有一部分走 logger（stderr）。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            if not stream.isatty():
                reconfigure(
                    encoding="utf-8",
                    errors="replace",
                    line_buffering=True,
                )
        except (ValueError, OSError, AttributeError) as exc:
            # 改不了控制台编码 —— 中文可能显示成乱码，但**审查照跑**。
            logger.debug(
                "调整控制台编码失败：%s: %s", type(exc).__name__, exc
            )


def _console_safe(message: str, stream: Any = None) -> str:
    """确保待打印的文本能被当前终端编码表示。

    终端编码装不下中文时，退化为 ASCII 摘要：保留最关键的
    「等级 / 判定 / 规则 / 命令」，丢掉中文说明。
    """
    target = stream if stream is not None else sys.stdout
    encoding = getattr(target, "encoding", None) or "ascii"
    try:
        message.encode(encoding)
    except (LookupError, UnicodeEncodeError):
        return _ascii_summary(message)
    return message


def _ascii_summary(message: str) -> str:
    """把中文告警降级成 ASCII 摘要（保留判定与命令）。"""
    ascii_message = message.encode("ascii", errors="replace").decode("ascii")
    return ascii_message.replace("?", "")


#: 上下文追踪器识别出的两类模式判定。
VERDICT_STUCK = "STUCK"
VERDICT_LEAK = "LEAK"

#: 意图漂移判定（v0.7.0）。
VERDICT_DRIFT = "DRIFT"

#: 意图漂移告警的固定文案。
DRIFT_ALERT_REASON = "[WARNING] 意图漂移：Agent 可能偏离了原始任务"

#: 参与意图漂移窗口的事件类型。
#:
#: 只有「Agent 想什么」与「Agent 做了什么」才携带意图信息；
#: ``text`` / ``tool_result`` 事件是**结果**，不参与判定 ——
#: 把结果也算进去只会稀释窗口的信号密度。
DRIFT_WINDOW_EVENTS = ("tool_call", "thinking")

#: 用户请求事件类型 —— 引擎据此**自动锚定**任务目标（v0.8.0）。
EVENT_USER_MESSAGE = "user_message"

#: Token 账单事件类型（v1.1.0）——
#:
#: codex 的 ``turn.completed`` 带着真实 usage。它**不是**一次可审查的动作，
#: 所以不进规则引擎、也不计入 ``ON_TRACK`` 计数 —— 它只是「账单」。
EVENT_USAGE = "usage"

#: 自动锚定的目标文本最大字符数（再长对模型也没意义）。
GOAL_MAX_CHARS = 200

#: 告警日志里「解释补充」行的标记。
#:
#: 告警日志有三类机器行（这是**日志格式契约**，两侧各持一份常量）：
#:
#: * ``ALERT_JSON {…}``       —— 一条**新**告警（可能还没有解释）；
#: * ``ALERT_UPDATE_JSON {…}`` —— 对**已有**告警的**追加更新**（L2 解释回来了）；
#: * ``TOKEN_JSON {…}``       —— 本次会话的 **Token 累计**（v1.1.0）。
#:
#: 看板据此把更新**合并进已有卡片**，而不是新开一张；
#: 也据此实时刷新顶部的「已消耗 Token」计数器。
ALERT_UPDATE_MARKER = "ALERT_UPDATE_JSON"

#: Token 累计行的标记（与看板各持一份常量 —— 日志格式契约）。
TOKEN_MARKER = "TOKEN_JSON"  # noqa: S105 —— 这是**格式标记**，不是密钥

#: 「操作事件」行的标记（v2.2.0）—— 同样是一对**日志格式契约**。
#:
#: 告警说的是「**发现了什么**」，事件说的是「**我们做了什么**」：
#: 开始目标、复活重试、回滚、注入、热重载……
#:
#: 它**不是**告警 —— 所以看板单独解析，不混进告警列表，
#: 而是拿去拼「案发时间线」。
EVENT_MARKER = "EVENT_JSON"

#: 「实时审核流水」行的标记（v3.3.1）—— 同样是一对**日志格式契约**。
#:
#: L1 判定为 ``ON_TRACK`` 的事件会被压成**一行极简摘要**写进告警日志，
#: 看板据此渲染「实时审核流水」——
#: 让用户看见「哨兵一直在看」，而不是盯着一块空白面板猜它死没死。
#:
#: 它**不是**告警 —— 所以单独解析，不混进告警列表。
ONTRACK_MARKER = "ONTRACK_JSON"

#: 级联审核统计行的标记（v3.5.0）—— 同样是一对**日志格式契约**。
#:
#: 看板顶部那个「L2 已过滤 X 次冗余，为您节省 Y 次云端调用」
#: 就是靠这一行刷新的。
CASCADE_MARKER = "CASCADE_JSON"

#: 「L3 回退预算熔断」的判定名（v3.8.0）。
#:
#: ⚠️ 这是一条**系统级**告警 —— 它不指向 Agent 的任何行为，
#: 指向的是「云端预算被 L2 掉线吃掉了」。
VERDICT_CASCADE_BUDGET = "CASCADE_BUDGET_EXHAUSTED"

#: Token 累计行的**节流阈值**（Token）。
#:
#: 没有 usage 时每个事件都会让估算值变化，逐事件写会把告警日志撑爆。
#: 所以只在累计值**至少增加这么多**时才写一行。
TOKEN_EMIT_STEP = 100

#: 单步摘要的最大字符数。
#:
#: 窗口内每一步都要**硬截断**：5 步 × 180 字 ≈ 900 字，
#: 这样 7B 模型的上下文负担可控，判定质量稳定。
#: 刻意**不**让模型先做摘要再判断 —— 一个模型同时做「摘要」和「判断」
#: 两件事，输出格式会明显不稳定（旧项目「8 字段 + 多任务」失败率 44.9%）。
SUMMARY_STEP_LIMIT = 180

#: 判定「这是一次测试运行」的命令特征。
#:
#: 刻意**不**用裸的 ``test`` 子串 —— 那会把 ``latest`` / ``contest`` 之类
#: 一股脑当成测试。这里列的是确切的测试入口。
_TEST_COMMAND_RE = re.compile(
    r"(?i)(?:^|[\\/\s\"'])"
    r"(?:python[0-9.]*\s+-m\s+)?(?:pytest|unittest|nose2|tox)\b"
    r"|(?:npm|yarn|pnpm)\s+(?:run\s+)?test\b"
    r"|\b(?:jest|vitest|mocha|rspec|phpunit)\b"
    r"|\bgo\s+test\b"
    r"|\bcargo\s+test\b"
    r"|\b(?:mvn|gradle)\s+(?:-\S+\s+)*test\b"
)

#: 判定「这是一个敏感文件」的文件名特征。
#:
#: 前缀允许「路径分隔符**或空白**」—— 只认路径分隔符的话，
#: ``cat .env`` 这种最常见的读法就漏掉了，而那恰恰是外泄的前置动作。
_SENSITIVE_FILE_RE = re.compile(
    r"(?i)(?:^|[\s\\/])"
    r"(?:\.env(?:\.[\w.-]+)?"
    r"|credentials"
    r"|id_rsa|id_dsa|id_ecdsa|id_ed25519"
    r"|\.npmrc|\.pypirc|\.netrc"
    r"|\.htpasswd"
    r"|secrets?\.ya?ml|secrets?\.json"
    r")"
)

#: 判定「这是一次外发」的特征（命令或工具名）。
_EXFIL_RE = re.compile(
    r"(?i)(?:^|[\\/\s\"'])(?:curl|wget|httpie|aria2c|fetch)\b"
    r"|\b(?:requests\.(?:post|put|patch)|axios|fetch)\s*\("
    r"|\b(?:nc|netcat)\s+"
    r"|\b(?:scp|rsync|sftp)\s+"
    r"|\bInvoke-(?:WebRequest|RestMethod)\b"
    r"|\bsendmail\b|\bmail\s+-s\b"
)

#: 工具名本身就说明了「外发」意图的特征。
#:
#: 单看工具名比看参数更稳 —— 很多 CLI 的外发工具并不经过 shell，
#: 参数里根本不会出现 ``curl`` 这类字样。
_EXFIL_TOOL_MARKERS = ("fetch", "http", "webhook", "upload", "sendmail")

#: 写类工具名 —— 命中即认为不是「读敏感文件」。
_WRITE_TOOL_RE = re.compile(
    r"(?i)(write|edit|create|delete|remove|move|copy|mkdir|rmdir|apply|patch|insert|update|replace|append)"
)

#: 从命令里提取 URL 的目标主机。
_URL_RE = re.compile(
    r"(?i)\b(?:https?|ftp|ftps|wss?)://"
    r"(?P<host>\[[0-9a-f:.]+\]|[^\s/:?#\"'@\[\]]+)"
)

#: 从命令里提取裸 IP（``1.2.3.4:8080`` 形式）。
_BARE_IP_RE = re.compile(
    r"(?<![\w.])(?P<ip>\d{1,3}(?:\.\d{1,3}){3})(?::(?P<port>\d+))?"
)

#: 默认视为「内部」的主机 —— 数据没有离开本机。
#:
#: 内网地址（10.x / 172.16.x / 192.168.x）**不算**内部：它们同样
#: 把密钥送出了本机。可通过 ``guardian.internal_hosts`` 追加豁免。
DEFAULT_INTERNAL_HOSTS = frozenset({
    "localhost",
    "127.0.0.1",
    "0.0.0.0",  # noqa: S104 —— 这是**可信主机名单**，不是监听地址
    "::1",
    "[::1]",
    "host.docker.internal",
})


class ContextTracker:
    """上下文感知追踪器 —— 检测**跨事件**的模式，而不只是单步。

    L1 规则只看单个事件（一条命令危不危险）。
    本追踪器看的是**一串事件**（Agent 是不是卡住了？是不是在偷密钥？）。

    两种模式
    =========

    ``STUCK``
        连续 N 次尝试运行测试，且**全部失败**（对应的 ``tool_result``
        标记为 error）—— Agent 很可能陷入了「改代码 → 跑测试 → 还是失败」
        的死循环。

    ``LEAK``
        读取敏感文件（``.env`` / ``credentials`` / ``id_rsa`` …）之后
        **紧接着**执行外发操作（``curl`` / ``fetch`` / ``scp`` …）——
        这是典型的「读密钥然后发出去」特征。

    设计约束
    =========
    * **只读**：本类不写任何文件、不发起任何网络请求；
    * **纯状态机**：只依赖喂进来的事件流，可随时 :meth:`reset`；
    * **有窗口**：只保留最近 N 个工具事件，避免无限增长。
    """

    def __init__(
        self,
        window: int = 5,
        stuck_threshold: int = 3,
        enabled: bool = True,
        internal_hosts: Optional[List[str]] = None,
    ) -> None:
        self.window = max(int(window or 0), 1)
        self.stuck_threshold = max(int(stuck_threshold or 0), 1)
        self.enabled = bool(enabled)
        #: 视为「内部」的主机名单（数据没有离开本机）。
        self.internal_hosts = frozenset(
            {str(item).strip().lower() for item in internal_hosts}
            if internal_hosts is not None
            else DEFAULT_INTERNAL_HOSTS
        )
        #: 最近 N 个工具相关事件（供告警上下文与调试使用）。
        self._history: Deque[Dict[str, Any]] = deque(maxlen=self.window)
        #: 连续失败的测试次数。
        self._failed_tests = 0
        #: 是否有一次「测试调用」正在等待它的回执。
        self._pending_test = False
        #: 上一次工具调用是否读了敏感文件（等待下一个调用来判定 LEAK）。
        self._read_sensitive = False
        #: 最近一次 LEAK 的外发目标是否指向外部地址。
        self.last_leak_external: bool = False
        #: 最近一次 LEAK 的外发目标地址。
        self.last_leak_target: str = ""

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------
    def observe(self, event: Dict[str, Any]) -> Optional[str]:
        """喂入一个事件，检测跨事件模式。

        :param event: 统一事件字典。
        :return: 命中时返回告警文案（含 ``[STUCK]`` / ``[LEAK]`` 前缀），
            否则返回 ``None``。
        """
        if not self.enabled or not isinstance(event, dict):
            return None

        event_type = str(event.get("type") or "")
        if event_type == "tool_call":
            return self._on_tool_call(event)
        if event_type == "tool_result":
            return self._on_tool_result(event)
        return None

    def reset(self) -> None:
        """复位全部状态。"""
        self._history.clear()
        self._failed_tests = 0
        self._pending_test = False
        self._read_sensitive = False
        self.last_leak_external = False
        self.last_leak_target = ""

    @property
    def history(self) -> List[Dict[str, Any]]:
        """最近 N 个工具事件的快照（副本）。"""
        return list(self._history)

    @property
    def state(self) -> str:
        """当前状态机的可读状态（用于日志与调试）。"""
        if not self.enabled:
            return "DISABLED"
        return (
            f"failed_tests={self._failed_tests}, "
            f"pending_test={self._pending_test}, "
            f"read_sensitive={self._read_sensitive}"
        )

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------
    def _on_tool_call(self, event: Dict[str, Any]) -> Optional[str]:
        """处理一次工具调用。"""
        tool = event.get("tool")
        tool = tool if isinstance(tool, dict) else {}
        arguments = tool.get("arguments")
        arguments = arguments if isinstance(arguments, dict) else {}

        name = str(tool.get("name") or "").lower()
        command = str(tool.get("command") or "").lower()
        path = str(
            arguments.get("file_path")
            or arguments.get("path")
            or arguments.get("filename")
            or ""
        ).lower()

        self._remember(event, "tool_call")

        # --- STUCK：这是一次测试运行吗？---
        is_test = bool(_TEST_COMMAND_RE.search(command)) or bool(
            _TEST_COMMAND_RE.search(name)
        )
        if is_test:
            self._pending_test = True
        else:
            # 非测试动作打断「连续失败测试」的累积。
            self._pending_test = False
            self._failed_tests = 0

        # --- LEAK：读敏感文件后紧接着外发 ---
        sensitive_read = self._is_sensitive_read(name, command, path)
        exfil = self._is_exfil(name, command)
        external, target = self._exfil_target(name, command)

        leak: Optional[str] = None
        if self._read_sensitive and exfil:
            leak = (
                "[LEAK] 疑似敏感信息外发：刚读取过敏感文件，"
                "紧接着执行了外发操作"
            )
            if external and target:
                leak = leak[:-1] + f"（目标：{target}）"
            # 「明确外发到外部地址」是红线级判定的触发条件之一。
            self.last_leak_external = bool(external)
            self.last_leak_target = target
        # 标记被「下一个工具调用」消费 —— 「紧接着」就是紧邻的那一个。
        self._read_sensitive = sensitive_read and not exfil

        return leak

    def _on_tool_result(self, event: Dict[str, Any]) -> Optional[str]:
        """处理一次工具回执。"""
        is_error = bool(event.get("is_error"))
        self._remember(event, "tool_result")

        if not self._pending_test:
            return None
        self._pending_test = False

        if not is_error:
            # 测试跑通了 —— 「连续失败」的累积清零。
            self._failed_tests = 0
            return None

        self._failed_tests += 1
        if self._failed_tests >= self.stuck_threshold:
            count = self._failed_tests
            # 复位，避免同一次卡死刷屏。
            self._failed_tests = 0
            return f"[STUCK] 测试连续失败{count}次，Agent 可能卡住了"
        return None

    def _remember(self, event: Dict[str, Any], kind: str) -> None:
        """把事件存进滑动窗口。

        同时记下 ``target``（读写的文件路径）—— 对「读了 .env」这类判定，
        光看命令是看不出端倪的，路径才是关键证据。
        """
        tool = event.get("tool")
        tool = tool if isinstance(tool, dict) else {}
        arguments = tool.get("arguments")
        arguments = arguments if isinstance(arguments, dict) else {}
        target = str(
            arguments.get("file_path")
            or arguments.get("path")
            or arguments.get("filename")
            or ""
        )
        self._history.append(
            {
                "kind": kind,
                "tool": tool.get("name"),
                "command": str(tool.get("command") or "")[:200],
                "target": target[:200],
                "is_error": bool(event.get("is_error")),
            }
        )

    @staticmethod
    def _is_sensitive_read(name: str, command: str, path: str) -> bool:
        """判断这次调用是不是「读了一个敏感文件」。"""
        haystack = f"{path} {command}".lower()
        if not _SENSITIVE_FILE_RE.search(haystack):
            return False
        # 写敏感文件不算「读」—— 那不是外泄的前置动作。
        return not _WRITE_TOOL_RE.search(name)

    @staticmethod
    def _is_exfil(tool_name: str, command: str) -> bool:
        """判断这次调用是不是「往外发东西」。"""
        haystack = f"{tool_name} {command}".lower()
        if _EXFIL_RE.search(haystack):
            return True
        # 工具名本身就说明了外发意图（很多 CLI 的外发工具不经过 shell，
        # 参数里根本不会出现 curl 这类字样）。
        return any(marker in tool_name for marker in _EXFIL_TOOL_MARKERS)

    def _exfil_target(
        self,
        tool_name: str,
        command: str,
    ) -> Tuple[bool, str]:
        """找出外发操作的目标地址，并判断它算不算「外部」。

        :param tool_name: 工具名。
        :param command: 命令文本。
        :return: ``(是否外部, 目标地址)``；识别不出地址时返回
            ``(False, "")``。
        """
        match = _URL_RE.search(f"{tool_name} {command}")
        if match:
            host = match.group("host")
            return self._is_external_host(host), host
        match = _BARE_IP_RE.search(command)
        if match:
            host = match.group("ip")
            return self._is_external_host(host), host
        return False, ""

    def _is_external_host(self, host: str) -> bool:
        """判断目标地址算不算「外部」。

        判据是「数据有没有离开这台机器」—— 回环地址是内部，其余
        （**包括内网 IP**）都算外部：内网地址同样会把密钥送出本机。

        域名在解析前无法证明是本机，保守起见按外部处理。
        """
        value = str(host or "").strip().lower()
        if not value:
            return False
        if value in self.internal_hosts:
            return False
        bare = value.strip("[]")
        try:
            address = ipaddress.ip_address(bare)
        except ValueError:
            return True
        return not address.is_loopback


# ---------------------------------------------------------------------------
# 意图漂移检测（v0.7.0）
# ---------------------------------------------------------------------------
def _truncate_step(text: str, limit: int = SUMMARY_STEP_LIMIT) -> str:
    """把单步文本压到给定长度以内。

    换行与连续空白会被压成单个空格 —— 摘要要给模型看，
    排版噪声只会占掉模型的注意力。
    """
    flat = " ".join(str(text or "").split())
    if len(flat) <= limit:
        return flat
    return flat[: max(limit - 1, 1)] + "…"


#: 工具参数里「最能说明这一步在干什么」的键（按优先级）。
#:
#: Read / Edit / Write / Grep 这类工具**没有命令文本**，真正能说明问题的
#: 是文件路径或搜索模式 —— 判断漂移恰恰靠这个信号
#: （「一直在读同一个文件」= 在推进目标；「突然去读完全无关的目录」= 跑偏了）。
_SUMMARY_ARG_KEYS: Tuple[str, ...] = (
    "file_path",
    "notebook_path",
    "path",
    "pattern",
    "query",
    "url",
    "description",
    "prompt",
)


def _summarize_arguments(arguments: Any) -> str:
    """从工具参数里挑出**最能说明这一步在干什么**的一项。

    :param arguments: 工具参数字典。
    :return: ``key=value`` 形式的摘要；挑不出时返回空串。
    """
    if not isinstance(arguments, dict):
        return ""
    for key in _SUMMARY_ARG_KEYS:
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            return f"{key}={value.strip()}"
    return ""


def build_window_summary(
    events: List[Dict[str, Any]],
    limit: int = SUMMARY_STEP_LIMIT,
) -> str:
    """把窗口内的事件压成一份**紧凑摘要**。

    摘要长什么样
    ===========

    ::

        第1步 [思考] 用户让我把项目里的旧构建产物清理掉，我打算用一条命令直接删掉
        第2步 [Bash] rm -rf /tmp/build
        第3步 [Read] file_path=D:/proj/package.json
        第4步 [Bash] git status --short

    为什么是「结构化摘要」而不是「模型摘要」
    =======================================

    常见做法是「先把 5 步喂给模型让它总结，再拿总结去判断漂移」。
    本项目**不这么做**，理由有二：

    1. **一个模型同时做两件事，输出格式会明显不稳定** ——
       旧项目 ``dsh-plain-approval`` 的 98 行 prompt + 8 个 JSON 字段
       失败率 44.9%，根因就在「既让模型判断、又让模型排版」。
       这里让模型**只做判断**，格式契约只有 2 个字段；
    2. **确定性压缩不丢信息** —— 每一步都保留了「类型 + 工具名 + 命令」，
       这些恰恰是判断漂移最关键的信号（同一个工具、同一个文件反复出现
       = 在推进目标；工具类型突然变了 = 可能跑偏）。
       摘要化反而可能把这类信号抹掉。

    每步**硬截断**到 ``limit`` 字：5 步 ≈ 900 字，7B 模型吃得下，
    判定质量稳定。

    :param events: 窗口内的事件列表。
    :param limit: 单步最大字符数。
    :return: 摘要文本；没有可用事件时返回空串。
    """
    lines: List[str] = []
    index = 0
    for event in events:
        kind = str(event.get("type") or "")
        if kind == "thinking":
            index += 1
            text = _truncate_step(event.get("text") or "", limit)
            lines.append(
                f"第{index}步 [思考] {text or '（空）'}"
            )
        elif kind == "tool_call":
            index += 1
            tool = event.get("tool")
            tool = tool if isinstance(tool, dict) else {}
            name = str(tool.get("name") or "工具")
            command = _truncate_step(tool.get("command") or "", limit)
            if not command:
                # 没有命令文本的工具（Read / Edit / Grep）改用关键参数 ——
                # 少了这一步，摘要会变成一串「（无命令文本）」，模型无从判断。
                command = _truncate_step(
                    _summarize_arguments(tool.get("arguments")), limit
                )
            lines.append(
                f"第{index}步 [{name}] {command or '（无命令文本）'}"
            )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# v4.0.0：日志压缩器 —— L2 判定前的「高信噪比演进流」
# ---------------------------------------------------------------------------
#
# 为什么换掉「粗暴截断」
# =====================
#
# v3.9.0 之前，L2 判定吃的是 :func:`build_window_summary` 的产物：
# 每步**硬截断**到 180 字，第 N 步前缀 + 工具名 + 命令。
# 问题有三个：
#
# 1. **信噪比低** —— 「同一个文件被读了 5 次」在旧格式里是 5 行几乎
#    一样的文本，模型的注意力被平摊到重复内容上；
# 2. **单行上限吃掉关键信息** —— 一条命令被截到 180 字后，
#    真正决定「在干什么」的参数（路径、目标）可能正好在尾部被切掉；
# 3. **同质动作淹没异质动作** —— 连续 12 次读文件会把中间那 1 次
#    「写了点东西」挤到窗口边缘，甚至被预算裁掉。
#
# 压缩器（:mod:`src.core.log_compressor`）的做法是
# **先把同质动作合并成一条**（「连续读同一文件 ×5」），
# 再把每条动作的意图抽成一句话，最后渲染成一条演进流：
#
#     [1] [思考] 我要先把旧构建产物清掉
#     [2] [Read] build/old.js
#     [3] [Bash] rm -rf build （连续 3 次同类操作）
#     [Current Action]: [Bash] rm -rf build
#
# ⚠️ **格式变了** —— L2 判定的输入不再是「第 N 步 [...]」，
# 而是「[N] [...]」。这是一次**有意的**输入契约变更，
# v3.9.0 的 A/B 证据（REAL-FP-01/02）是基于旧格式测的，
# 换格式后必须重新验证（见 v4.0.0 版本文档「已知边界」）。
#
# ⚠️ **红线一字未改** —— 本函数只影响 **L2 软判定**的输入文本，
# L1 规则引擎、阈值、告警口径全部原样。

#: 压缩后摘要的字符预算。
#:
#: 8000 字 ≈ 4K token，给 7B 本地模型留足余量；
#: 旧格式 5 步 ≈ 900 字，压缩后通常**更长**但**信息密度更高**。
COMPRESSED_SUMMARY_MAX_CHARS = 8000

#: 压缩器输出的目标锚定行前缀（与 prompt 的【原始目标】段重复）。
_COMPRESSED_GOAL_PREFIX = "[Task Goal]:"

#: 压缩器输出的「当前动作」行前缀。
_CURRENT_ACTION_PREFIX = "[Current Action]"

#: 兜底补充块最多补几条被压缩器丢掉的思考。
FALLBACK_THINKING_MAX_LINES = 3

#: 兜底补充块里每条的最大字符数。
FALLBACK_THINKING_MAX_CHARS = 120

#: 兜底补充块的小标题。
_FALLBACK_BLOCK_TITLE = "[思考补充]"


def _strip_compressed_goal(text: str) -> str:
    """剥掉压缩结果里的目标锚定行。

    压缩器的输出**自带**一行 ``[Task Goal]: …``，
    而 L2 prompt 里已经有【原始目标】段（:func:`src.llm.prompts.build_drift_prompt`）
    —— 同一句话说两遍会挤占模型的注意力。

    :param text: 压缩器产出的文本。
    :return: 去掉目标锚定行之后的正文。
    """
    lines = str(text or "").splitlines()
    while lines and lines[0].startswith(_COMPRESSED_GOAL_PREFIX):
        lines.pop(0)
    return "\n".join(lines).strip()


def _fallback_thinking_lines(
    events: List[Dict[str, Any]],
    *,
    max_lines: int = FALLBACK_THINKING_MAX_LINES,
    max_chars: int = FALLBACK_THINKING_MAX_CHARS,
) -> List[str]:
    """捞回「被压缩器整条丢掉」的思考与自述。

    为什么需要这一层
    ===============

    压缩器的意图提取只认「意图动词表」里的动词
    （修改 / 修复 / 测试 / 集成 ……），
    所以「我先把旧构建产物清掉」这种**没命中动词表**、
    却**确实说明了 Agent 想干什么**的话会被整条丢掉。

    对**日志压缩**来说丢掉是合理的（它要的是高信噪比）；
    但对 **L2 漂移判定**来说这是**真实的信息损失** ——
    模型看不到「Agent 说它要清旧构建产物」这句话。

    所以这里用压缩器的**兜底口径**（:func:`src.core.log_compressor.fallback_intent`）
    把这些句子捞回来，作为**补充块**附在演进流后面。

    ⚠️ **不改变压缩器语义** —— 压缩器该丢的仍然丢，
       只是引擎在**摘要**层面把丢掉的话补回来。

    :param events: 窗口内的事件列表。
    :param max_lines: 最多补几条。
    :param max_chars: 每条最多多少字符。
    :return: 兜底文本列表；没有可补内容时返回空列表。
    """
    lines: List[str] = []
    seen: set = set()
    for event in events or ():
        if not isinstance(event, dict):
            continue
        kind = str(event.get("type") or "").strip().lower()
        if kind not in ("thinking", "text"):
            continue
        text = str(event.get("text") or "").strip()
        if not text:
            continue
        if extract_intent(text, max_chars=max_chars):
            # 压缩器已经留住了 —— 不重复补。
            continue
        recovered = fallback_intent(text, max_chars=max_chars)
        if not recovered or recovered in seen:
            continue
        seen.add(recovered)
        lines.append(recovered)
        if len(lines) >= max_lines:
            break
    return lines


def _merge_fallback_block(text: str, lines: List[str]) -> str:
    """把兜底补充块插到 ``[Current Action]`` 行之前。

    ⚠️ 补充块**不参与编号** —— 它是「被压缩器丢掉的原话」，
       不属于演进流里的第几步；给编号反而会谎报时序。

    :param text: 演进流正文。
    :param lines: 兜底文本列表。
    :return: 合并后的文本。
    """
    if not lines:
        return text
    block = [_FALLBACK_BLOCK_TITLE]
    block.extend("· {}".format(item) for item in lines)
    body = str(text or "").splitlines()
    for index, line in enumerate(body):
        if line.startswith(_CURRENT_ACTION_PREFIX):
            return "\n".join(body[:index] + block + body[index:])
    return "\n".join(body + block)


def build_compressed_summary(
    events: List[Dict[str, Any]],
    goal: str = "",
    max_chars: int = COMPRESSED_SUMMARY_MAX_CHARS,
) -> str:
    """把窗口内的事件压成**高信噪比演进流**（v4.0.0）。

    这是 L2 漂移判定的新输入；:func:`build_window_summary` 保留为
    **降级路径** —— 压缩器出错或产出为空时立刻回落，绝不让
    一次异常把漂移检测整个打挂（红线 R5）。

    :param events: 窗口内的事件列表。
    :param goal: 任务目标；用于压缩器的目标锚定（会随正文一起剥掉）。
    :param max_chars: 输出字符预算。
    :return: 演进流文本；没有可用事件时返回空串。
    """
    if not events:
        return ""
    try:
        options = CompressionOptions(goal=goal, max_chars=int(max_chars))
        compressed = compress_events(events, options=options)
    except Exception as exc:  # noqa: BLE001 —— 压缩失败不影响审查
        logger.warning(
            "日志压缩失败，已降级为旧摘要：%s: %s", type(exc).__name__, exc
        )
        return build_window_summary(events)

    text = _strip_compressed_goal(compressed.text)
    if not text.strip():
        # 压缩器产出为空 —— 回落旧摘要，绝不把空摘要喂给模型。
        return build_window_summary(events)
    fallback = _fallback_thinking_lines(events)
    return _merge_fallback_block(text, fallback)


# ---------------------------------------------------------------------------
# v3.9.0：子阶段宽容度 —— 确定性白名单
# ---------------------------------------------------------------------------
#
# 为什么要有这一层
# =================
#
# 漂移判定问的是「这些步骤还在服务于原始目标吗」。但真实的开发任务
# **天然包含多个子阶段**：
#
#     修代码 → 写测试 → 改 CHANGELOG → 版本号 bump → 跑验证 → 打包
#
# 每一个子阶段单独看，都「不是」最初那句目标原文。
# 模型在窗口里只看到「第 3 步改了 CHANGELOG.md」，
# 就很容易判成「跑去做别的了」—— 这正是 v3.8.0 之前
# 「正常改 CHANGELOG 却被误报 DRIFT」的根因。
#
# 为什么这一层是**确定性**的（不靠模型）
# ======================================
#
# 「改 CHANGELOG」和「跑 pytest」是**字面可判定**的事实，
# 不需要推理。交给模型推理有两个坏处：
#
#   ① **会判错** —— 同一个窗口，模型有时判 ON_TRACK、有时判 DRIFT；
#   ② **要花钱** —— 每 5 步就要问一次模型。
#
# 所以这一层在**发请求之前**就短路：命中 → 根本不发 L2 请求，
# 直接判 ON_TRACK。省下算力，也省下误报。
#
# ⚠️ **这一层不碰红线**（R1–R9 一字未改）：
#    L1 规则引擎的拦截与本白名单**完全并行、互不影响** ——
#    白名单只作用于「漂移」这层**软告警**。
#    真正的危险动作（删库、发密钥、破坏数据）照样会被 L1 拦下。

#: 项目演进的**常规文件**（只看文件名，不看目录）。
#:
#: - ``changelog.md`` / ``changelog``：变更日志（``changelog.zh-CN.md``
#:   这类带后缀的也算）；
#: - ``package.json``：依赖与版本；
#: - ``version.txt``：版本号。
ROUTINE_MAINTENANCE_FILES: Tuple[str, ...] = (
    "changelog",
    "package.json",
    "version.txt",
)

#: 工具参数里可能装文件路径的键（与 :data:`_SUMMARY_ARG_KEYS` 同源）。
_ROUTINE_PATH_KEYS: Tuple[str, ...] = (
    "file_path",
    "filePath",
    "notebook_path",
    "path",
    "target_file",
    "filename",
)

#: **标准验证命令**的可执行体（命令段的头一个词）。
_ROUTINE_VERIFY_EXECUTABLES: frozenset = frozenset(
    {
        "pytest",
        "py.test",
        "tox",
        "nox",
        "ruff",
        "mypy",
        "flake8",
        "black",
        "tsc",
        "eslint",
    }
)

#: ``npm`` / ``pnpm`` / ``yarn`` 之后允许出现的验证子命令。
_ROUTINE_VERIFY_SUBCOMMANDS: frozenset = frozenset(
    {
        "build",
        "test",
        "lint",
        "check",
        "typecheck",
        "verify",
    }
)

#: 命令段的分隔符 —— 按它们把一条命令拆成若干「独立命令」。
_COMMAND_SPLIT_PATTERN = re.compile(r"&&|\|\||[;|\n]")

#: 自由文本里识别常规演进文件的正则。
#:
#: 用「前后不能紧邻单词字符」的断言，避免把 ``myCHANGELOG.md``
#: 这类**别的**文件误认成变更日志；``CHANGELOG.md`` /
#: ``CHANGELOG`` / ``package.json`` / ``version.txt`` 都能命中。
_ROUTINE_TEXT_PATTERN = re.compile(
    r"(?<![0-9A-Za-z_])(?:"
    r"changelog(?:\.[0-9A-Za-z_-]+)*"
    r"|package\.json"
    r"|version\.txt"
    r")(?![0-9A-Za-z_])",
    re.IGNORECASE,
)


def _routine_basename(path_text: str) -> str:
    """取路径的**文件名**部分（兼容 Windows 与 POSIX 分隔符）。

    :param path_text: 原始路径文本。
    :return: 小写后的文件名；取不到时返回空串。
    """
    text = str(path_text or "").strip().strip("\"'")
    if not text:
        return ""
    normalized = text.replace("\\", "/")
    name = normalized.rsplit("/", 1)[-1].strip().lower()
    return name


def _is_routine_maintenance_file(path_text: str) -> bool:
    """判断一个路径是不是**项目演进的常规文件**。

    :param path_text: 路径文本。
    :return: 命中白名单返回 ``True``。
    """
    name = _routine_basename(path_text)
    if not name:
        return False
    for target in ROUTINE_MAINTENANCE_FILES:
        if name == target:
            return True
        # ``changelog.zh-CN.md`` 这类带后缀的变更日志也算。
        if target == "changelog" and name.startswith("changelog."):
            return True
    return False


def _is_routine_verify_command(command: str) -> bool:
    """判断一条命令是不是**标准验证命令**（测试 / 编译 / 检查）。

    ⚠️ 刻意**按命令段**判定（用 ``&&`` / ``;`` / ``|`` 拆开），
    而不是全文正则 —— 否则 ``echo "npm test"`` 这种
    「提到」验证命令的普通命令也会被误判成验证命令。

    :param command: 命令文本。
    :return: 命中返回 ``True``。
    """
    if not command:
        return False
    for segment in _COMMAND_SPLIT_PATTERN.split(command):
        tokens = segment.strip().split()
        if not tokens:
            continue
        head = tokens[0].strip("\"'")
        rest = tokens[1:]
        # 允许 ``python -m pytest`` / ``python3 -m pytest``。
        if head.startswith("python") and len(tokens) >= 3 and tokens[1] == "-m":
            head = tokens[2].strip("\"'")
            rest = tokens[3:]
        if head in _ROUTINE_VERIFY_EXECUTABLES:
            return True
        if head in ("npm", "pnpm", "yarn") and rest:
            verb = rest[0]
            if verb == "run" and len(rest) >= 2:
                verb = rest[1]
            if verb in _ROUTINE_VERIFY_SUBCOMMANDS:
                return True
        if head == "cargo" and rest and rest[0] in (
            "build",
            "test",
            "clippy",
            "check",
        ):
            return True
        if head == "go" and rest and rest[0] in ("build", "test", "vet"):
            return True
        if head in ("mvn", "gradle") and rest and rest[0] in (
            "test",
            "verify",
            "build",
            "check",
            "package",
        ):
            return True
        if head == "make" and rest and rest[0] in ("test", "build", "check"):
            return True
    return False


def _routine_text_token(text: str) -> Optional[str]:
    """在**自由文本**里找出常规演进文件的文件名。

    ⚠️ 只认「最具体」的那个 —— 文本里同时出现 ``CHANGELOG`` 和
    ``CHANGELOG.md`` 时，报告 ``changelog.md``，理由更贴近实际动作。

    :param text: 自由文本（典型是 Agent 的思考内容）。
    :return: 命中的文件名（小写）；没命中返回 ``None``。
    """
    matches = _ROUTINE_TEXT_PATTERN.findall(text or "")
    if not matches:
        return None
    best = max(matches, key=len)
    return best.lower()


def detect_routine_maintenance(
    events: List[Dict[str, Any]],
) -> Optional[str]:
    """扫描一个窗口，找出**项目演进的常规铺垫**。

    看两类事件
    ==========

    1. ``tool_call`` —— **动作**。路径参数命中常规文件、
       命令行里出现常规文件、或者跑的是标准验证命令，都算；
    2. ``thinking`` —— **声明**。Agent 在思考里点名要改
       ``CHANGELOG.md`` / ``package.json`` / ``version.txt``，
       同样算（它下一步就是那个动作）。

    为什么要看思考
    ===============

    真实生产日志（``2026-10-31.txt``，61 条真实推理记录）里，
    **所有**触发误报的漂移窗口都是**纯思考窗口** ——
    Agent 连着几步在思考「现在做版本 bump + CHANGELOG + 版本文档」，
    工具调用还没落地就被送去了 L2。只看 ``tool_call``
    的白名单在真实场景里**根本接不住**。

    ⚠️ 命中条件是「窗口里**至少有一个**常规步骤」。
    这是**有意放宽**的取舍：漂移本来就是软告警，
    L1 红线不受影响；而「读到 package.json」也算铺垫，
    比误报一次的代价小得多。

    :param events: 窗口内的事件列表。
    :return: 命中时返回一句中文说明（可直接当判定理由）；
        没命中返回 ``None``。
    """
    for event in events or []:
        if not isinstance(event, dict):
            continue
        kind = str(event.get("type") or "")
        if kind == "thinking":
            # 思考里声明「要改哪个文件」。
            token = _routine_text_token(str(event.get("text") or ""))
            if token:
                return f"常规演进文件：{token}（思考声明）"
            continue
        if kind != "tool_call":
            continue
        tool = event.get("tool")
        if not isinstance(tool, dict):
            continue
        arguments = tool.get("arguments")
        if not isinstance(arguments, dict):
            arguments = {}

        # ① 常规文件：路径参数命中。
        for key in _ROUTINE_PATH_KEYS:
            value = arguments.get(key)
            if isinstance(value, str) and _is_routine_maintenance_file(
                value
            ):
                return f"常规演进文件：{_routine_basename(value)}"
        # ② 常规文件：命令行里出现（``git add CHANGELOG.md``）。
        command = tool.get("command")
        if not isinstance(command, str):
            command = arguments.get("command")
        if isinstance(command, str) and command:
            for token in re.findall(r"[\w./\\-]+", command):
                if _is_routine_maintenance_file(token):
                    return f"常规演进文件：{_routine_basename(token)}"
            # ③ 标准验证命令。
            if _is_routine_verify_command(command):
                head = command.strip().split()[0].strip("\"'")
                return f"标准验证命令：{head}"
    return None


class IntentTracker:
    """意图锚定与漂移检测。

    哨兵之前只能回答「这一步危不危险」。本追踪器回答的是另一个问题：
    **Agent 还在做用户交代的事吗？**

    工作方式
    ========

    1. 以 ``task_goal``（用户给的原始目标）为**锚点**；
    2. 每收集 ``window`` 个 ``tool_call`` / ``thinking`` 事件，
       压成一份摘要（见 :func:`build_window_summary`）；
    3. 交给 L2 模型判定「有没有偏离」，得到 ``drift`` 与 ``reason``；
    4. **连续** ``threshold`` 次判定为漂移 → 触发一次告警。

    窗口是**不重叠**的：5 步一窗，两窗即 10 步 —— 与「连续 2 次
    （10 步）」的要求一致。

    异步模型（v0.8.0）
    ==================

    判定要问模型，而模型**慢**。v0.7.0 的同步写法会让主事件循环
    卡在一次 HTTP 请求上 —— 日志读得慢，Agent 也被拖住。

    所以判定搬到了**后台线程**：

    ::

        主线程                          后台线程
        ------                          --------
        observe(event)  ──攒够 5 步──▶  队列（有界）
        投递窗口后立刻返回               取窗口 → 问模型
        继续读下一行日志                 结论 → 回调 → 引擎产告警

    队列**有界**（默认 3 个窗口）。模型太慢、队列满了，
    就**丢弃**新窗口并记一条 ``[WARN] 漂移检测超载`` ——
    宁可漏判，不可把主循环堵死。

    三条重要约定
    ===========

    * **漂移不是红线** —— 判定结果不会触发守护者挂起进程，
      只弹窗警告。跑偏是「可能做错事」，不是「马上要毁掉东西」；
    * **降级不等于漂移** —— 模型不可用时判定为「没漂移」，
      连续计数归零。缺失绝不等同于有问题；
    * **必须有目标** —— 没设 ``task_goal`` 就完全不做判定，
      不去打扰模型。

    :param goal: 原始目标（``task_goal``）。
    :param window: 窗口大小（多少个事件判一次）。
    :param threshold: 连续几次判定为漂移才告警。
    :param enabled: 是否启用。
    :param evaluator: 判定回调，签名 ``(goal, summary) -> DriftResult``；
        为 ``None`` 时本追踪器不工作。
    :param max_pending: **背压上限** —— 队列里最多缓存几个待判定的窗口；
        满了就**丢弃**新窗口（而不是把主循环堵住）。
    :param wait_for_result: 为 ``True``（默认）时 :meth:`observe` 会
        **等**这一窗的判定结论再返回（同步模式，便于测试与脚本化调用）；
        为 ``False`` 时立即返回，结论走 :meth:`attach_async` 挂上的回调。
    :param result_timeout: 同步模式下等待结论的超时秒数。
    :param scheduler: L2 共享调度器（v4.0.0）；为 ``None`` 时
        **直连**判定回调（等于没排队）。
    """

    def __init__(
        self,
        goal: str = "",
        window: int = 5,
        threshold: int = 2,
        enabled: bool = True,
        evaluator: Optional[Any] = None,
        max_pending: int = 3,
        wait_for_result: bool = True,
        result_timeout: float = 120.0,
        scheduler: Optional[Any] = None,
    ) -> None:
        self.goal = str(goal or "").strip()
        self.window = max(int(window or 0), 1)
        self.threshold = max(int(threshold or 0), 1)
        self.enabled = bool(enabled)
        self.evaluator = evaluator
        if self.goal:
            # 构造时给的目标来自配置 —— 它只是**默认值**。
            self.goal_source = "config"
        self.max_pending = max(int(max_pending or 0), 1)
        self.wait_for_result = bool(wait_for_result)
        self.result_timeout = float(result_timeout)
        #: L2 共享调度器（v4.0.0）—— 为 ``None`` 时直连判定回调。
        self.scheduler = scheduler
        self._buffer: Deque[Dict[str, Any]] = deque()
        #: 连续判定为漂移的次数。
        self.consecutive_drift = 0
        #: 已完成的判定次数。
        self.evaluations = 0
        #: 最近一次的判定结果。
        self.last_result: Optional[Dict[str, Any]] = None
        #: 因背压被**丢弃**的窗口数。
        self.dropped = 0
        #: 被**确定性白名单**放行（没发模型请求）的窗口数（v3.9.0）。
        self.bypassed = 0
        #: 异步模式下的结果回调。
        self.on_result: Optional[Callable[[Dict[str, Any]], None]] = None
        #: 异步模式开关。
        self.async_enabled = False
        #: 目标是否由**人工显式**指定（显式目标不被自动锚定覆盖）。
        self.goal_explicit = False
        #: 目标来源：``none`` / ``config`` / ``explicit`` / ``auto``。
        #: 看板的「当前监控目标」面板要显示它。
        self.goal_source = "none"
        #: 待判定窗口队列（背压上限 = ``max_pending``）。
        self._queue: "queue.Queue[_DriftJob]" = queue.Queue(
            maxsize=self.max_pending
        )
        self._worker: Optional[threading.Thread] = None
        self._stop = threading.Event()
        #: 保护连续计数等跨线程状态。
        self._state_lock = threading.Lock()
        #: 已提交但尚未出结论的窗口数。
        self._pending = 0
        #: 队列空且无在途任务时置位。
        self._idle = threading.Event()
        self._idle.set()

    # ------------------------------------------------------------------
    # 异步模式
    # ------------------------------------------------------------------
    def sibling(self, goal: str = "") -> "IntentTracker":
        """造一个**同配置**的新追踪器（v3.5.0 —— 多路隔离）。

        为什么需要
        ==========

        多个 Agent 并行写**同一个**日志文件时，每一路都要有**自己的**滑动窗口 ——
        共享一个窗口就会「串台」：
        A 路读了 ``a.py``、B 路要删库，两件事混在一个窗口里，
        漂移检测就会把 A 的正常动作判成「突然要删库」。

        ⚠️ 刻意**不**用 ``copy.deepcopy``：
        追踪器持有队列、事件与线程，深拷贝会把**线程**也拷一份
        （更糟的是拷出一个**死锁**的锁）。
        这里显式重建 —— **只带配置，不带状态**。

        ⚠️ 但 L2 调度器（v4.0.0）**要共享** ——
        红线解释插队是**跨路径**的能力：
        A 路在排队等漂移判定时，B 路撞了红线，
        红线的解释必须能越过 A 路的队列先跑完。
        所以调度器是**共享**的，滑窗才是**隔离**的。

        :param goal: 新追踪器的目标；留空则沿用当前的。
        :return: 新的追踪器实例（**尚未**挂异步回调）。
        """
        return IntentTracker(
            goal=goal or self.goal,
            window=self.window,
            threshold=self.threshold,
            enabled=self.enabled,
            evaluator=self.evaluator,
            max_pending=self.max_pending,
            wait_for_result=self.wait_for_result,
            result_timeout=self.result_timeout,
            scheduler=self.scheduler,
        )

    def set_scheduler(self, scheduler: Optional[Any]) -> None:
        """换装 L2 调度器（**运行中**改用 / 停用都用它）。

        :param scheduler: 调度器；``None`` 表示退回直连判定回调。
        :return: 无返回值。
        """
        self.scheduler = scheduler

    def attach_async(
        self,
        on_result: Callable[[Dict[str, Any]], None],
    ) -> None:
        """切到**异步模式**并挂上结果回调。

        异步模式下 :meth:`observe` **绝不阻塞**：窗口满就把摘要丢进队列，
        立刻返回；后台线程取出窗口去问模型，结论通过 ``on_result``
        回调交还给引擎。

        :param on_result: 结论回调，签名 ``(verdict: dict) -> None``。
        """
        self.on_result = on_result
        self.async_enabled = True
        self.wait_for_result = False

    def observe(self, event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """喂一个事件进来；窗口满时**投递**一次判定。

        ⚠️ **不阻塞**（异步模式）：窗口满就把摘要丢进队列立刻返回。
        模型慢的时候主循环照常读日志 —— 这是 v0.8.0 的核心改动。

        :param event: 统一事件字典。
        :return: 判定结果字典（``drift`` / ``reason`` / ``summary`` /
            ``alert``）；同步模式下返回结论，异步模式下返回 ``None``
            （结论走回调）。
        """
        if not self.enabled or self.evaluator is None:
            return None
        kind = str(event.get("type") or "")
        if kind not in DRIFT_WINDOW_EVENTS:
            return None

        self._buffer.append(event)
        if len(self._buffer) < self.window:
            return None
        if not self.goal:
            # 没有锚点就没有「偏离」可言 —— 但缓冲**留着**，
            # 等目标到位（比如自动锚定）后再判这一窗。
            return None

        window_events = list(self._buffer)[: self.window]
        rest = list(self._buffer)[self.window:]
        self._buffer.clear()
        self._buffer.extend(rest)
        summary = build_compressed_summary(window_events, self.goal)

        # v3.9.0：确定性白名单 —— 命中就**根本不发 L2 请求**（省算力）。
        bypass_reason = detect_routine_maintenance(window_events)
        if bypass_reason is not None:
            return self._bypass_drift(bypass_reason, summary, len(window_events))

        job = _DriftJob(
            goal=self.goal,
            summary=summary,
            steps=len(window_events),
            task_id=f"drift-{uuid.uuid4().hex[:12]}",
        )
        if not self._submit(job):
            # 背压：队列满了 → 丢弃这一窗，**不拖慢主循环**。
            return None
        if not self.wait_for_result:
            return None
        return self._await(job)

    def _submit(self, job: "_DriftJob") -> bool:
        """把窗口投递到队列。

        :param job: 待判定的窗口。
        :return: 投递成功返回 ``True``；因背压丢弃返回 ``False``。
        """
        self._ensure_worker()
        with self._state_lock:
            self._pending += 1
            self._idle.clear()
        try:
            self._queue.put_nowait(job)
        except queue.Full:
            with self._state_lock:
                self._pending = max(self._pending - 1, 0)
                if self._pending == 0:
                    self._idle.set()
            self.dropped += 1
            logger.warning(
                "[WARN] 漂移检测超载：待判定窗口已达 %d 个，丢弃当前窗口"
                "（累计丢弃 %d 个）",
                self.max_pending,
                self.dropped,
            )
            return False
        return True

    def _await(self, job: "_DriftJob") -> Optional[Dict[str, Any]]:
        """同步模式下等一个窗口的结论。

        :param job: 待判定的窗口。
        :return: 判定结果字典；超时或失败时返回 ``None``。
        """
        if not job.done.wait(timeout=self.result_timeout):
            logger.warning("意图漂移判定超时（%.0f 秒），已跳过", self.result_timeout)
            return None
        return job.verdict

    def _bypass_drift(
        self,
        reason: str,
        summary: str,
        steps: int,
    ) -> Optional[Dict[str, Any]]:
        """白名单放行：**不问模型**，直接判 ``ON_TRACK``。

        为什么可以这样做
        ===============

        「改 CHANGELOG」「跑 pytest」是**字面事实**，不是需要推理的事。
        既然事实是确定的，就没必要付出一次模型调用的代价去
        问一个可能答错的问题。

        与模型判定的差别
        ===============

        * 模型判定：连续计数照常参与，``drift=True`` 时会累加；
        * 白名单放行：``consecutive_drift`` **直接归零** ——
          常规铺垫把之前的漂移嫌疑**洗掉**了（一次常规验证
          说明 Agent 还在按流程做事）。

        :param reason: 放行理由（中文一句话）。
        :param summary: 这一窗的摘要（照样带进结论，便于看板显示）。
        :param steps: 这一窗的事件数。
        :return: 同步模式下返回结论字典；异步模式下返回 ``None``
            （结论走 :meth:`attach_async` 挂上的回调，与模型判定同构）。
        """
        with self._state_lock:
            self.evaluations += 1
            self.bypassed += 1
            # 常规铺垫 → 连续漂移计数归零。
            self.consecutive_drift = 0
            verdict = {
                "drift": False,
                "reason": reason,
                "degraded": False,
                "summary": summary,
                "steps": steps,
                "alert": False,
                "consecutive": 0,
                "bypassed": True,
            }
            self.last_result = verdict
        logger.debug("漂移判定白名单放行：%s", reason)
        if self.async_enabled and self.on_result is not None:
            try:
                self.on_result(verdict)
            except Exception as exc:  # noqa: BLE001 —— 回调失败不外泄
                logger.warning(
                    "漂移结论回调失败：%s: %s", type(exc).__name__, exc
                )
            return None
        return verdict

    # ------------------------------------------------------------------
    # 后台线程
    # ------------------------------------------------------------------
    def _ensure_worker(self) -> None:
        """确保后台判定线程已启动（**懒启动**）。"""
        with self._state_lock:
            if self._worker is not None and self._worker.is_alive():
                return
            self._worker = threading.Thread(
                target=self._worker_loop,
                name="sentinel-drift",
                daemon=True,
            )
            self._worker.start()

    def _worker_loop(self) -> None:
        """后台线程主体：从队列取窗口 → 问模型 → 回调。"""
        while not self._stop.is_set():
            try:
                job = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                self._process(job)
            finally:
                self._queue.task_done()
                with self._state_lock:
                    self._pending = max(self._pending - 1, 0)
                    if self._pending == 0:
                        self._idle.set()
                    else:
                        self._idle.clear()

    def _process(self, job: "_DriftJob") -> None:
        """判定一个窗口，并把结论交还出去。

        :param job: 待判定的窗口。
        """
        try:
            result = self._evaluate(job)
        except Exception as exc:  # noqa: BLE001 —— 审查侧异常绝不外泄
            logger.warning(
                "意图漂移判定失败，已跳过：%s: %s", type(exc).__name__, exc
            )
            job.finish(None)
            return

        verdict = self._apply_result(job, result)
        job.finish(verdict)
        if self.async_enabled and self.on_result is not None and verdict:
            try:
                self.on_result(verdict)
            except Exception as exc:  # noqa: BLE001 —— 回调失败不外泄
                logger.warning(
                    "漂移结论回调失败：%s: %s", type(exc).__name__, exc
                )

    def _evaluate(self, job: "_DriftJob") -> Any:
        """问一次模型 —— 挂了调度器就**排队**，没挂就直连。

        漂移判定固定是 **P1** 档：
        它比红线解释（P0）低一级，所以红线插队时它会让路；
        但它比日常摘要（P2）高一级，不会被日常请求挤掉。

        ⚠️ 没挂调度器时**直连** —— 行为与 v3.9.0 完全一致，
        既有测试与脚本化调用不受影响。

        :param job: 待判定的窗口。
        :return: 判定回调的原始返回值。
        """
        evaluator = self.evaluator
        if evaluator is None:
            return None
        scheduler = self.scheduler
        if scheduler is None:
            return evaluator(job.goal, job.summary)
        task_id = job.task_id or f"drift-{uuid.uuid4().hex[:12]}"
        return scheduler.call(
            task_id,
            lambda: evaluator(job.goal, job.summary),
            priority=PRIORITY_P1,
            timeout=self.result_timeout,
        )

    def _apply_result(
        self,
        job: "_DriftJob",
        result: Any,
    ) -> Optional[Dict[str, Any]]:
        """把一次判定结果折算成结论（含连续计数）。

        换过目标之后，**在途**的旧窗口结论一律作废 ——
        换了目标，之前的判定就不作数了。

        :param job: 待判定的窗口。
        :param result: 判定回调的返回值。
        :return: 判定结果字典；结果已作废时返回 ``None``。
        """
        drift = bool(getattr(result, "drift", False))
        reason = str(getattr(result, "reason", "") or "")
        degraded = bool(getattr(result, "degraded", False))

        with self._state_lock:
            if job.goal != self.goal:
                # 目标换过了 —— 这条结论针对的是旧目标，丢弃。
                logger.debug("漂移结论已作废（目标已变更）")
                return None
            self.evaluations += 1
            if drift:
                self.consecutive_drift += 1
            else:
                # 没漂移（含降级）→ 连续计数归零。
                # 降级也归零：模型不可用时我们**不知道**，
                # 不能把「不知道」当成「没漂移」，更不能当成「漂移」。
                self.consecutive_drift = 0

            should_alert = self.consecutive_drift >= self.threshold
            if should_alert:
                # 告警后归零，避免此后每 5 步就报一次。
                self.consecutive_drift = 0

            verdict = {
                "drift": drift,
                "reason": reason,
                "degraded": degraded,
                "summary": job.summary,
                "steps": job.steps,
                "alert": should_alert,
                "consecutive": self.consecutive_drift,
                "bypassed": False,
            }
            self.last_result = verdict
            return verdict

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def drain(self, timeout: float = 30.0) -> bool:
        """等所有**已提交**的窗口出结论。

        批量模式收尾时必须调它 —— 否则引擎会在漂移判定还没回来时就退出，
        告警就丢了。

        :param timeout: 最长等待秒数。
        :return: 全部出结论返回 ``True``；超时返回 ``False``。
        """
        return self._idle.wait(timeout=max(float(timeout), 0.0))

    def stop(self, timeout: float = 5.0) -> None:
        """停掉后台判定线程。

        :param timeout: 等待线程退出的秒数。
        """
        self._stop.set()
        worker = self._worker
        if worker is not None and worker.is_alive():
            worker.join(timeout=max(float(timeout), 0.0))
        self._worker = None

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------
    def set_goal(self, goal: str, explicit: bool = False) -> bool:
        """运行时更新原始目标（看板的输入框 / 自动锚定用）。

        改目标会把连续计数清零 —— 换了目标，之前的漂移判定就不作数了。

        ⚠️ **显式目标优先**：命令行 ``--task-goal`` 与看板输入框里
        人工设定的目标属于「明确指示」，**不会被**自动锚定覆盖；
        而配置文件里的 ``task_goal`` 只是默认值，
        会被流里自动提取到的真实用户请求取代。

        :param goal: 新目标。
        :param explicit: 是否为**显式**指定（人工决定）。
        :return: 目标是否真的变了。
        """
        new_goal = str(goal or "").strip()
        if not new_goal:
            return False
        if self.goal_explicit and not explicit:
            # 人已经明确交代过了 —— 自动锚定不许覆盖。
            return False
        if new_goal == self.goal:
            if explicit:
                self.goal_explicit = True
            return False
        with self._state_lock:
            self.goal = new_goal
            self.goal_explicit = self.goal_explicit or bool(explicit)
            self.goal_source = "explicit" if explicit else "auto"
            self.consecutive_drift = 0
            self.last_result = None
        self._buffer.clear()
        return True

    def reset(self) -> None:
        """复位全部状态。"""
        self._buffer.clear()
        with self._state_lock:
            self.consecutive_drift = 0
            self.evaluations = 0
            self.last_result = None
            self.dropped = 0
            self.bypassed = 0

    @property
    def pending(self) -> int:
        """已提交但还没出结论的窗口数。"""
        with self._state_lock:
            return self._pending

    def state(self) -> str:
        """返回一句人类可读的状态描述。"""
        if not self.enabled:
            return "漂移检测已关闭"
        if not self.goal:
            return "未设置任务目标（task_goal），漂移检测待命"
        extra = ""
        if self.dropped:
            extra = f" | 超载丢弃 {self.dropped} 窗"
        if self.bypassed:
            extra = f"{extra} | 白名单放行 {self.bypassed} 窗"
        return (
            f"目标：{self.goal} | 窗口 {self.window} 步 | "
            f"连续判定 {self.consecutive_drift}/{self.threshold}"
            f"{extra}"
        )


class _DriftJob:
    """一个待判定的漂移窗口。

    既是队列里的货，也是同步模式下「等结论」的把手。

    :param goal: 投递时的目标快照。
    :param summary: 窗口摘要。
    :param steps: 窗口内的事件数。
    :param task_id: 调度器里的唯一任务号（v4.0.0）。
    """

    __slots__ = ("done", "goal", "steps", "summary", "task_id", "verdict")

    def __init__(
        self,
        goal: str,
        summary: str,
        steps: int,
        task_id: str = "",
    ) -> None:
        self.goal = goal
        self.summary = summary
        self.steps = int(steps)
        #: 调度器任务号（v4.0.0）—— 排队时用它定位与统计。
        self.task_id = str(task_id or "")
        #: 结论就绪信号。
        self.done = threading.Event()
        #: 判定结果字典。
        self.verdict: Optional[Dict[str, Any]] = None

    def finish(self, verdict: Optional[Dict[str, Any]]) -> None:
        """落结论并唤醒等待者。

        :param verdict: 判定结果字典。
        """
        self.verdict = verdict
        self.done.set()


class _ExplanationJob:
    """一条待补解释的告警。

    :param alert_id: 原告警的 ID（更新事件靠它对上号）。
    :param payload: 送给模型的载荷（事件 + 判定 + 最近自述）。
    """

    __slots__ = ("alert_id", "payload")

    def __init__(self, alert_id: str, payload: Dict[str, Any]) -> None:
        self.alert_id = alert_id
        self.payload = payload


def _l2_priority_for_verdict(verdict: Any) -> Any:
    """按判定内容定 L2 请求的**优先级档位**（v4.0.0）。

    档位口径
    ========

    * **P0** —— 红线（``severity == red_line``）：
      红线的解释必须**最先**跑完，不能排在漂移判定后面；
    * **P1** —— 危险动作（``DANGEROUS``）：比红线低一级；
    * **P2** —— 其余（日常解释、漂移说明）。

    ⚠️ 只有**同一条解释链路**上的请求才能互相插队 ——
    所以漂移判定（P1）与红线解释（P0）必须共用**同一个**调度器，
    否则两个队列各排各的，插队价值归零。

    :param verdict: 判定字典。
    :return: 优先级常量（:data:`~src.core.priority_queue.PRIORITY_P0` 等）。
    """
    if not isinstance(verdict, dict):
        return PRIORITY_P2
    if str(verdict.get("severity") or "") == SEVERITY_RED_LINE:
        return PRIORITY_P0
    if str(verdict.get("verdict") or "") == VERDICT_DANGEROUS:
        return PRIORITY_P1
    return PRIORITY_P2


class ExplanationWorker:
    """后台 L2 解释队列（v0.9.0）。

    为什么要有它
    ============

    L1 命中之后要问一次模型才能说清「这一步到底危不危险」。
    但那次调用**慢**，而且**守护者不能等它** ——
    红线事件必须**立刻**挂起目标进程。

    所以拆成两段：

    ::

        L1 命中
          ├─► 立刻发出原始告警（没有解释）→ 守护者同时动手
          └─► 解释请求丢进这个有界队列 ──► 后台线程问模型
                                          │
                                          └─► 回调 on_explanation(alert_id, …)
                                              → 追加更新事件 → 前端**淡入**同一张卡片

    队列是**有界**的：模型太慢、队列满了就**丢弃**解释请求
    （告警本身已经发出去了，丢一条解释不致命），
    但绝不把主循环堵住 —— 审查侧的卡顿不能变成被监控方的卡顿（红线 R5）。

    :param explainer: 解释回调，签名 ``(payload) -> Explanation``。
    :param on_explanation: 结论回调，签名 ``(alert_id, fields) -> None``。
    :param max_pending: 队列最多缓存几个待解释的告警。
    :param scheduler: L2 共享调度器（v4.0.0）；为 ``None`` 时直连解释回调。
    """

    def __init__(
        self,
        explainer: Optional[Any] = None,
        on_explanation: Optional[Any] = None,
        max_pending: int = 4,
        scheduler: Optional[Any] = None,
        result_timeout: float = 120.0,
    ) -> None:
        self._explainer = explainer
        self._on_explanation = on_explanation
        self.max_pending = max(int(max_pending or 0), 1)
        #: L2 共享调度器（v4.0.0）—— 为 ``None`` 时直连解释回调。
        self.scheduler = scheduler
        #: 走调度器时等待结论的最长秒数。
        self.result_timeout = float(result_timeout)
        self._queue: "queue.Queue[_ExplanationJob]" = queue.Queue(
            maxsize=self.max_pending
        )
        self._worker: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._pending = 0
        self._idle = threading.Event()
        self._idle.set()
        self._lock = threading.Lock()
        #: 因背压被丢弃的解释请求数。
        self.dropped = 0

    def submit(self, alert_id: str, payload: Dict[str, Any]) -> bool:
        """投递一个解释请求。

        :param alert_id: 原告警 ID。
        :param payload: 送给模型的载荷。
        :return: 投递成功返回 ``True``；因背压丢弃返回 ``False``。
        """
        if self._explainer is None:
            return False
        self._ensure_worker()
        job = _ExplanationJob(alert_id, payload)
        with self._lock:
            self._pending += 1
            self._idle.clear()
        try:
            self._queue.put_nowait(job)
        except queue.Full:
            with self._lock:
                self._pending = max(self._pending - 1, 0)
                if self._pending == 0:
                    self._idle.set()
            self.dropped += 1
            logger.warning(
                "[WARN] 解释队列已满（%d），丢弃本次 L2 解释",
                self.max_pending,
            )
            return False
        return True

    def _ensure_worker(self) -> None:
        """确保后台解释线程已启动（**懒启动**）。"""
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                return
            self._worker = threading.Thread(
                target=self._worker_loop,
                name="sentinel-explain",
                daemon=True,
            )
            self._worker.start()

    def _worker_loop(self) -> None:
        """后台线程主体：取请求 → 问模型 → 回调。"""
        while not self._stop.is_set():
            try:
                job = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                self._process(job)
            finally:
                self._queue.task_done()
                with self._lock:
                    self._pending = max(self._pending - 1, 0)
                    if self._pending == 0:
                        self._idle.set()
                    else:
                        self._idle.clear()

    def _explain(self, job: _ExplanationJob) -> Any:
        """问一次模型 —— 挂了调度器就**按档位排队**。

        红线解释是 **P0**，危险动作是 **P1**，其余是 **P2**。
        队列里如果正卡着一个 P1 漂移判定，红线解释会**插到它前面**。

        ⚠️ 调度器**没有取消入口** —— 超时只会让本调用抛
        ``TimeoutError``，已经排进队列的那个任务**仍会跑完**
        （那次模型调用照常计费）。这是已知取舍，见 v4.0.0 版本文档。

        :param job: 待解释的告警。
        :return: 解释对象。
        """
        explainer = self._explainer
        if explainer is None:
            return None
        scheduler = self.scheduler
        if scheduler is None:
            return explainer(job.payload)
        payload = job.payload if isinstance(job.payload, dict) else {}
        priority = _l2_priority_for_verdict(payload.get("verdict"))
        return scheduler.call(
            f"explain-{job.alert_id}",
            lambda: explainer(job.payload),
            priority=priority,
            timeout=self.result_timeout,
        )

    def _process(self, job: _ExplanationJob) -> None:
        """问一次模型，并把结论交回去。

        :param job: 待解释的告警。
        """
        try:
            explanation = self._explain(job)
        except Exception as exc:  # noqa: BLE001 —— L2 绝不影响主流程
            logger.warning(
                "L2 解释失败，已降级：%s: %s", type(exc).__name__, exc
            )
            fields = {
                "explanation": "",
                "explanation_degraded": True,
                "explanation_reason": f"L2 调用异常（{type(exc).__name__}）",
            }
        else:
            fields = {
                "explanation": getattr(explanation, "text", ""),
                "explanation_degraded": bool(
                    getattr(explanation, "degraded", False)
                ),
            }
            reason = str(getattr(explanation, "reason", "") or "")
            if reason:
                fields["explanation_reason"] = reason

        if self._on_explanation is None:
            return
        try:
            self._on_explanation(job.alert_id, fields)
        except Exception as exc:  # noqa: BLE001 —— 回调失败不外泄
            logger.warning(
                "L2 解释回调失败：%s: %s", type(exc).__name__, exc
            )

    def drain(self, timeout: float = 30.0) -> bool:
        """等所有**已提交**的解释请求出结论。

        :param timeout: 最长等待秒数。
        :return: 全部完成返回 ``True``；超时返回 ``False``。
        """
        return self._idle.wait(timeout=max(float(timeout), 0.0))

    def stop(self, timeout: float = 5.0) -> None:
        """停掉后台解释线程。

        :param timeout: 等待线程退出的秒数。
        """
        self._stop.set()
        worker = self._worker
        if worker is not None and worker.is_alive():
            worker.join(timeout=max(float(timeout), 0.0))
        self._worker = None

    @property
    def pending(self) -> int:
        """已提交但还没出结论的解释请求数。"""
        with self._lock:
            return self._pending


def format_alert_message(alert: Dict[str, Any]) -> str:
    """把一条告警渲染成**人读**的文本（终端与日志共用）。

    :param alert: 告警字典。
    :return: 多行文本。
    """
    verdict = str(alert.get("verdict") or "")
    if verdict in (
        VERDICT_STUCK,
        VERDICT_LEAK,
        VERDICT_DRIFT,
        VERDICT_TIMEBOX,
        VERDICT_ENGINE_OFFLINE,
        VERDICT_ENGINE_RECOVERED,
        VERDICT_CASCADE_BUDGET,
    ):
        # 上下文模式判定用「[STUCK] …」「[WARNING] 意图漂移：…」这种
        # 一眼可辨的标签形式。
        message = str(alert.get("reason") or verdict)
    else:
        message = (
            "[{level}] {verdict}: {reason} | 工具={tool} | 命令={command}"
        ).format(
            level=str(alert.get("level", "warn")).upper(),
            verdict=verdict,
            reason=alert.get("reason", ""),
            tool=alert.get("tool") or "-",
            command=alert.get("command") or "-",
        )

    # L2 人话解释：模型不可用时客户端已降级成「原始命令 + 规则原因」。
    explanation = str(alert.get("explanation") or "")
    if explanation:
        message = f"{message}\n  人话解释：{explanation}"

    # 守护者拦截提示（只有真的动手时才会有）。
    guardian_result = alert.get("guardian")
    if isinstance(guardian_result, dict):
        guardian_message = str(guardian_result.get("message") or "")
        if guardian_message:
            message = f"{message}\n  {guardian_message}"
    return message


def publish_system_alert(
    alert: Dict[str, Any],
    *,
    alert_log_path: str,
    notifier: Optional[Any] = None,
    console: bool = True,
) -> None:
    """把一条**系统级**告警（引擎掉线之类）发出去。

    走的是和引擎告警**完全一样**的三件事：

    1. 写告警日志（看板靠它显示卡片）；
    2. 终端红色提示；
    3. 通知通道（手机推送）。

    ⚠️ 为什么单独有这个函数
    ----------------------
    守护 / 雷达模式下可能**一个会话都没接入**，
    那时没有 :class:`SentinelEngine` 实例，
    但「本地引擎掉线」这件事**仍然必须**出现在告警日志和看板上。

    :param alert: 告警字典。
    :param alert_log_path: 告警日志路径。
    :param notifier: 通知器；``None`` 时只输出到终端与日志。
    :param console: 是否在终端打印。
    :return: 无。
    """
    if not isinstance(alert, dict):
        return
    message = format_alert_message(alert)
    try:
        path = os.path.abspath(alert_log_path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("{}\n".format(message))
            handle.write(
                "ALERT_JSON {}\n".format(
                    json.dumps(alert, ensure_ascii=False)
                )
            )
    except OSError as exc:
        logger.warning("写系统告警日志失败：%s: %s", type(exc).__name__, exc)
    if console:
        safe = _console_safe(message)
        color = _BOLD_RED if alert.get("level") == "critical" else _RED
        if _supports_ansi():
            print(f"{color}{safe}{_RESET}")
        else:
            print(safe)
    if notifier is not None:
        try:
            notifier.notify(alert)
        except Exception as exc:  # noqa: BLE001 —— 通知失败不影响审查
            logger.warning("系统告警分发失败：%s: %s", type(exc).__name__, exc)


class SentinelEngine:
    """哨兵调度器。

    :param adapter: 采集层适配器实例。
    :param rule_engine: 规则层引擎实例。
    :param alert_log_path: 告警日志路径，默认 ``logs/sentinel_alert.log``。
    :param console: 是否在终端打印告警。
    :param alert_logger: 写入告警日志的 logger，默认 ``sentinel.alert``。
    :param llm_client: L2 本地模型客户端；为 ``None`` 时只输出规则层原始判定。
    :param context_tracker: 上下文追踪器；为 ``None`` 时不检测 STUCK / LEAK。
    :param intent_tracker: 意图追踪器；为 ``None`` 时不做意图漂移检测。
    :param notifier: 多通道通知器；为 ``None`` 时只输出到终端与日志。
    :param guardian: 守护者；为 ``None`` 时不执行任何主动干预。
    :param timebox: 时间盒检测器（v3.3.0）；为 ``None`` 时不做时间盒判定。
    :param engine_watchdog: 本地引擎心跳看门狗（v3.3.0）；
        为 ``None`` 时不监控引擎存活。
    :param micro_stats: 微观统计检测器（v4.0.0）；
        为 ``None`` 时不做微观统计判定。
    :param l2_scheduler: L2 共享调度器（v4.0.0）；
        为 ``None`` 时**自建**一个 —— 保证红线插队始终生效。
    """

    def __init__(
        self,
        adapter: BaseAdapter,
        rule_engine: RuleEngine,
        *,
        alert_log_path: Optional[str] = None,
        console: bool = True,
        alert_logger: Optional[logging.Logger] = None,
        llm_client: Optional[Any] = None,
        context_tracker: Optional[ContextTracker] = None,
        intent_tracker: Optional[IntentTracker] = None,
        notifier: Optional[Any] = None,
        guardian: Optional[Any] = None,
        agent_name: str = "",
        on_explanation: Optional[Callable[[str, Dict[str, Any]], None]] = None,
        safe_point_service: Optional[Any] = None,
        workdir: str = "",
        timebox: Optional[Any] = None,
        engine_watchdog: Optional[Any] = None,
        micro_stats: Optional[Any] = None,
        l2_scheduler: Optional[Any] = None,
    ) -> None:
        self.adapter = adapter
        self.rule_engine = rule_engine
        self.console = bool(console)
        self.alert_log_path = os.path.abspath(
            alert_log_path or DEFAULT_ALERT_LOG
        )
        self.logger = alert_logger or logging.getLogger(
            f"sentinel.alert.{uuid.uuid4().hex[:8]}"
        )
        self.llm_client = llm_client
        self.context_tracker = context_tracker
        self.intent_tracker = intent_tracker
        self.notifier = notifier
        self.guardian = guardian
        #: 时间盒检测器（v3.3.0）—— 「一个子任务吃掉整轮」的硬性拦截。
        self.timebox = timebox
        #: 本地引擎心跳看门狗（v3.3.0）—— 引擎掉线时立刻红色告警。
        self.engine_watchdog = engine_watchdog
        #: 微观统计检测器（v4.0.0）—— 「同一个文件改了十遍」这类
        #: **原地打转**的硬证据。为 ``None`` 时不做这项判定。
        self.micro_stats = micro_stats
        #: L2 共享调度器（v4.0.0）—— 红线解释（P0）插队到
        #: 漂移判定（P1）前面。为 ``None`` 时**自建**一个。
        self.l2_scheduler = (
            l2_scheduler if l2_scheduler is not None else L2Scheduler()
        )
        #: 调度器是不是**自己**建的 —— 决定 close() 时要不要停它。
        self._owns_l2_scheduler = l2_scheduler is None
        #: 被监控的 Agent 名称（如 ``codex``）——
        #: 远程通知里必须写清楚「是哪个 Agent 出事了」。
        self.agent_name = str(agent_name or "").strip()
        #: 解释补充的**外部观察者**（事故演练 / 集成方用）。
        #:
        #: 为什么不直接暴露 ``_on_explanation``：
        #: 解释工作线程在构造时就抓住了那个绑定方法，事后再替换引擎上的
        #: 属性是**没用的**。这里给一个正经的公开钩子。
        self.on_explanation = on_explanation
        #: 安全点服务（v2.0.0）—— 红线拦截前给工作目录打快照。
        #:
        #: 为 ``None`` 时**不**打快照 —— 审查侧不该强行往用户的目录里
        #: 写东西，配置了才做。
        self.safe_point_service = safe_point_service
        #: 被监控 Agent 的工作目录（安全点与留言箱都落在它旁边）。
        self.workdir = os.path.abspath(str(workdir or ""))
        #: 流式监听时的停止信号（看板用它优雅收尾）。
        self.stop_event = threading.Event()
        #: 会话开始时间戳（晨报用它算执行时长）。
        self.started_at = time.time()
        #: 会话结束时间戳；``close()`` 时打上。
        self.ended_at = 0.0
        #: 最近一次 thinking 事件的正文 —— L2 解释时作为「Agent 自述」证据。
        self._last_thinking = ""
        #: 已处理的事件数（供外部脚本读进度）。
        self.processed_events = 0
        # --- 自检（v3.2.0）-------------------------------------------------
        #: 哨兵自身的资源采样器（CPU / 内存 / 运行时长）。
        #:
        #: ⚠️ 「监控工具本身不能变成资源消耗大户」——
        #: 所以采样走 :mod:`src.selfmon`（纯 Win32 API，**不读文件**、
        #: **不起线程**、**不做网络 I/O**），而且**默认 5 分钟才采一次**。
        self._monitor: Optional[Any] = None
        #: 最近一次采样结果（给看板的【关于系统】区块用）。
        #:
        #: ⚠️ 属性名**不能**叫 ``selfcheck`` —— 那会覆盖同名的
        #: :meth:`selfcheck` 方法，实例属性优先级高于类属性，
        #: 引擎主循环第二次自检就会 ``TypeError: 'dict' object is not callable``。
        self.selfcheck_data: Dict[str, Any] = {}
        #: 两次采样之间的间隔（秒）。
        self.selfcheck_interval = 300.0
        #: 上次采样的时刻。
        self._selfcheck_at = 0.0
        #: 「L2 模型判不了」已经提醒过的原因 —— 同一个原因只提醒一次，
        #: 免得模型长时间不可用时把终端刷爆。
        self._llm_degraded_notice = ""
        #: 引擎产出的告警（后台线程回调时也要往里追加）。
        self.alerts: List[Dict[str, Any]] = []
        #: 各判定的计数。
        self.counts: Dict[str, int] = {
            VERDICT_ON_TRACK: 0,
            VERDICT_DANGEROUS: 0,
            VERDICT_LOOP: 0,
        }
        #: Token 台账（v1.1.0）—— 实时统计这次会话烧了多少 Token。
        self.token_ledger = TokenLedger(session_id=self.agent_name)
        #: 上次写出 Token 累计行时的数值（用于节流）。
        self._token_emit_total = 0
        #: 收尾时等漂移判定收敛的最长秒数。
        self.drain_timeout = 30.0
        self._alert_lock = threading.Lock()
        #: 告警自增序号（配合时间戳构成 ``alert_id``）。
        self._alert_seq = 0
        #: 后台 L2 解释队列 —— 主循环**绝不**等模型。
        self.explanation_worker = ExplanationWorker(
            explainer=(self.llm_client.explain if self.llm_client else None),
            on_explanation=self._on_explanation,
            scheduler=self.l2_scheduler,
        )
        if self.intent_tracker is not None:
            # 漂移判定搬去后台线程 —— 主事件循环不能被模型调用堵住。
            self.intent_tracker.attach_async(self._on_drift_result)
            # v4.0.0：漂移判定也走**同一个**调度器 ——
            # 否则红线解释排在漂移队列后面，插队价值归零。
            self.intent_tracker.set_scheduler(self.l2_scheduler)
        #: v3.5.0：按**线程指纹**分路的意图追踪器。
        #:
        #: 多个 Agent 并行写同一个日志文件时，每一路都要有自己的滑窗 ——
        #: 共享一个窗口就会串台（A 读文件、B 删库被当成同一个 Agent 的连续行为）。
        #:
        #: 键是线程指纹；空串（认不出来）那一路用 ``self.intent_tracker``。
        self._intent_trackers: Dict[str, IntentTracker] = {}
        #: 保护上面那张表（多线程可能同时建新的一路）。
        self._tracker_lock = threading.Lock()
        #: v3.5.0：按**线程指纹**分路的记忆（短期滑窗 + 长期背景）。
        self._memories: Dict[str, Any] = {}
        #: 短期记忆保留几步（v3.5.0）。
        self.memory_steps = 4
        #: 长期记忆多少次触发一次摘要（v3.5.0）。
        self.memory_summary_threshold = 10
        #: v3.5.0：级联审核编排器（L2 本地过滤 → L3 云端终审）。
        #: 为 ``None`` 时**不跑**级联，行为与 v3.4.0 完全一样。
        self.cascade: Optional[Any] = None
        #: 上次写级联统计行时的计数签名（用来节流）。
        self._cascade_emit_signature: Tuple[Any, ...] = ()
        #: 级联审核的后台线程 —— 主循环**绝不**等模型。
        self.cascade_worker = CascadeWorker(
            None, self._on_cascade_result
        )
        # --- v3.7.0：L1→L3 桥接 ---
        #: 正在等 L3 结论的桥接事件（键是合成的告警 ID）。
        self._bridge_pending: Dict[str, Dict[str, Any]] = {}
        self._bridge_lock = threading.Lock()
        self._setup_alert_log()

    # ------------------------------------------------------------------
    # 意图追踪器（运行中换装）
    # ------------------------------------------------------------------
    def set_cascade(self, router: Optional[Any]) -> None:
        """装上级联审核编排器（**运行中**换装用）。

        ⚠️ 传 ``None`` = 关掉级联 ——
        行为回到 v3.4.0（只有 L1 + 原来的解释链路）。

        :param router: :class:`~src.llm.cascade.CascadeRouter`；
            ``None`` 表示停用级联。
        """
        self.cascade = router
        self.cascade_worker.router = router

    def set_intent_tracker(self, tracker: Optional[Any]) -> None:
        """装上一个意图追踪器（**运行中**补建 / 更换都用它）。

        v2.4.0：挂载进来的会话**没有**最初的 Prompt，
        用户手动设定目标之后才补建追踪器 ——
        这时候**必须**把异步回调重新挂上：
        流式模式下 :meth:`IntentTracker.observe` 的返回值没人看，
        漂移结论全靠 ``attach_async`` 的回调回到引擎。
        漏了这一步，追踪器照样在判，DRIFT 告警却**永远发不出来**。

        :param tracker: 追踪器；``None`` 表示停用意图漂移检测。
        :return: 无返回值。
        """
        old = self.intent_tracker
        if old is not None and old is not tracker:
            try:
                old.stop()
            except Exception as exc:  # noqa: BLE001 —— 收尾失败不影响
                logger.warning(
                    "停止旧意图追踪器失败（已降级）：%s: %s",
                    type(exc).__name__,
                    exc,
                )
        self.intent_tracker = tracker
        if tracker is not None:
            tracker.attach_async(self._on_drift_result)
            # v4.0.0：新追踪器也要接上共享调度器（否则它绕过排队）。
            tracker.set_scheduler(self.l2_scheduler)
        # ⚠️ v3.5.0：换了追踪器，旧的分路追踪器必须**一起换掉** ——
        # 它们是照着旧配置造出来的，留着会拿旧配置判新会话。
        with self._tracker_lock:
            for stale in self._intent_trackers.values():
                try:
                    stale.stop()
                except Exception as exc:  # noqa: BLE001 —— 收尾失败不影响
                    logger.warning(
                        "停止旧分路追踪器失败（已降级）：%s: %s",
                        type(exc).__name__,
                        exc,
                    )
            self._intent_trackers.clear()

    def set_micro_stats(self, detector: Optional[Any]) -> None:
        """装上微观统计检测器（**运行中**补建 / 停用都用它）。

        :param detector: 检测器；``None`` 表示停用微观统计判定。
        :return: 无返回值。
        """
        self.micro_stats = detector

    def set_l2_scheduler(self, scheduler: Optional[Any]) -> None:
        """换装 L2 共享调度器（**运行中**改用 / 停用都用它）。

        ⚠️ 换装后**所有**已有解释请求与漂移判定都会改走新调度器 ——
        已经排进旧队列的任务**不会被搬走**（队列没有迁移入口）。

        :param scheduler: 调度器；``None`` 表示退回直连模型调用
            （等于取消排队，红线不再享有插队特权）。
        :return: 无返回值。
        """
        self.l2_scheduler = scheduler
        self.explanation_worker.scheduler = scheduler
        if self.intent_tracker is not None:
            self.intent_tracker.set_scheduler(scheduler)
        with self._tracker_lock:
            for tracker in self._intent_trackers.values():
                tracker.set_scheduler(scheduler)

    # ------------------------------------------------------------------
    # 告警日志
    # ------------------------------------------------------------------
    def _setup_alert_log(self) -> None:
        """准备告警日志文件处理器。

        日志文件属于哨兵自身的运行产物，落在 ``logs/`` 下，与被监控数据分区存放。

        ⚠️ **每个引擎必须有自己的 logger**
        ================================
        共用一个 logger 时，第二个引擎建 handler 会把第一个的**顶掉**
        （``removeHandler`` + ``addHandler``）——
        于是**两路会话的告警全都写进最后一个会话的文件**。

        这不是理论问题：v1.1.0 的并发压测实测到了它
        （A 路告警落进了 B 路的日志文件，A 路日志是空的）。
        """
        os.makedirs(os.path.dirname(self.alert_log_path), exist_ok=True)

        # 只清掉**自己**挂的处理器 —— 不能动别人的。
        for existing in list(self.logger.handlers):
            if getattr(existing, "_sentinel_owner", None) == id(self):
                self.logger.removeHandler(existing)
                existing.close()

        handler = logging.FileHandler(self.alert_log_path, encoding="utf-8")
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(message)s")
        )
        handler._sentinel_handler = True  # type: ignore[attr-defined]
        handler._sentinel_owner = id(self)  # type: ignore[attr-defined]
        self.logger.addHandler(handler)
        self.logger.setLevel(logging.WARNING)
        self.logger.propagate = False

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------
    def run(
        self,
        log_file_path: Any,
        tail: bool = False,
    ) -> Dict[str, Any]:
        """读取一份日志，逐事件检查，汇总告警。

        :param log_file_path: 日志文件路径，或已打开的文本流。
        :param tail: 为 ``True`` 时进入**实时流式模式**：
            不读完就退出，而是一直阻塞着监听文件变化，
            新事件到达即刻审查（``tail -f`` 语义）。
        :return: 运行摘要字典，包含 ``total_events`` / ``counts`` / ``alerts``。
        """
        summary: Dict[str, Any] = {
            "log_file": str(log_file_path),
            "adapter": getattr(self.adapter, "name", "unknown"),
            "mode": "tail" if tail else "batch",
            "total_events": 0,
            "counts": {
                VERDICT_ON_TRACK: 0,
                VERDICT_DANGEROUS: 0,
                VERDICT_LOOP: 0,
            },
            "alerts": self.alerts,
        }

        self._resolve_guardian_target(log_file_path)
        self._ensure_baseline_safe_point()

        events = (
            self.adapter.tail_stream(
                log_file_path,
                interval=0.5,
                from_end=True,
                stop_event=self.stop_event,
            )
            if tail
            else self.adapter.read_stream(log_file_path)
        )

        for event in events:
            if self.stop_event.is_set():
                break
            summary["total_events"] += 1
            if tail:
                self._trace_event(event, summary["total_events"])
            self.process_event(event)
            # v3.2.0：每过一段时间看一眼**哨兵自己**的负载。
            # 挂在主循环里是刻意的 —— 引擎空闲时（半夜 Agent 也没动静）
            # 采样频率自然跟着事件频率走，不会空转。
            if (time.time() - self._selfcheck_at) >= self.selfcheck_interval:
                snapshot = self.selfcheck(force=True)
                if snapshot:
                    logger.info(
                        "哨兵自检：CPU %s%% · 内存 %s MB · 运行时长 %s",
                        snapshot.get("cpu_percent"),
                        snapshot.get("memory_mb"),
                        snapshot.get("uptime_text"),
                    )

        # 漂移判定跑在后台线程 —— 收尾前必须等它出结论，
        # 否则引擎会在告警还没生成时就退出，告警就丢了。
        if (
            self.intent_tracker is not None
            and not self.intent_tracker.drain(timeout=self.drain_timeout)
        ):
            logger.warning(
                "漂移判定未在 %.0f 秒内收敛，已放弃剩余窗口",
                self.drain_timeout,
            )
        # ⚠️ v3.5.0：分路追踪器**各自**也要收敛 ——
        # 漏掉任何一路，这一路的漂移告警就会随进程退出一起消失。
        with self._tracker_lock:
            stream_trackers = list(self._intent_trackers.values())
        for tracker in stream_trackers:
            try:
                tracker.drain(timeout=self.drain_timeout)
            except Exception as exc:  # noqa: BLE001 —— 收尾失败不影响
                logger.warning(
                    "分路漂移判定未收敛（已降级）：%s: %s",
                    type(exc).__name__,
                    exc,
                )
        # L2 解释同样在后台 —— 也要等它回来，否则解释会丢。
        if not self.explanation_worker.drain(timeout=self.drain_timeout):
            logger.warning(
                "L2 解释未在 %.0f 秒内收敛，已放弃剩余解释",
                self.drain_timeout,
            )
        # ⚠️ v3.5.0：级联审核也在后台 —— 双盲结论要赶在退出前回来，
        # 否则「双盲命中」这条升级信息就随进程一起消失了。
        if not self.cascade_worker.drain(timeout=self.drain_timeout):
            logger.warning(
                "级联审核未在 %.0f 秒内收敛，已放弃剩余审核",
                self.drain_timeout,
            )
        summary["counts"] = dict(self.counts)
        return summary

    # ------------------------------------------------------------------
    # 自检（v3.2.0）
    # ------------------------------------------------------------------
    def selfcheck(self, *, force: bool = False) -> Dict[str, Any]:
        """采一次哨兵自身的资源快照。

        采的是**哨兵这个进程**的 CPU 占用、内存占用与运行时长 ——
        不是被监控的 Agent 的。

        ⚠️ **采样本身必须便宜**：走 :mod:`src.selfmon`（纯 Win32 API），
        不读文件、不起线程、不做网络 I/O。
        而且默认**5 分钟才采一次**（:attr:`selfcheck_interval`），
        免得监控器自己变成负载。

        :param force: 为 ``True`` 时无视时间间隔，强制采一次。
        :return: 采样结果字典；采不到时是空字典。
        """
        now = time.time()
        if (
            not force
            and self._selfcheck_at
            and (now - self._selfcheck_at) < self.selfcheck_interval
        ):
            return self.selfcheck_data
        try:
            if self._monitor is None:
                from ..selfmon import ProcessMonitor

                self._monitor = ProcessMonitor()
            self.selfcheck_data = self._monitor.snapshot()
            self._selfcheck_at = now
        except Exception as exc:  # noqa: BLE001 —— 自检失败绝不影响审查
            logger.warning("哨兵自检失败：%s: %s", type(exc).__name__, exc)
            self._selfcheck_at = now
            self.selfcheck_data = {}
        return self.selfcheck_data

    # ------------------------------------------------------------------
    # 单事件处理（引擎的「单步入口」）
    # ------------------------------------------------------------------
    def process_event(self, event: Event) -> Optional[Dict[str, Any]]:
        """处理**单个**事件：规则层 → 上下文模式 → 意图漂移。

        这是引擎对外的「单步入口」——
        :meth:`run` 内部也走它，而真实 Agent 管道接管脚本
        （``_构建与发布/scripts/run_with_sentinel.py``）直接调它，
        不必先把日志落盘。

        ⚠️ **意图漂移不阻塞**：窗口满了只是把摘要丢进队列，
        结论由后台线程回调（:meth:`_on_drift_result`）。

        :param event: 统一事件字典。
        :return: 本事件产生的告警（没有则返回 ``None``）。
        """
        self.processed_events += 1

        # --- Token 记账（v1.1.0） ---
        # 有 usage 就用真值，没有就按字符估 —— 两者都记进台账。
        # 这一步**必须极快**：它只是算术，不能拖慢主循环。
        self._record_tokens(event)

        # usage 事件只是「账单」，不是一次可审查的动作 ——
        # 不进规则引擎，也不计入 ON_TRACK 计数。
        if str(event.get("type") or "") == EVENT_USAGE:
            return None

        # --- 自动锚定：首个用户请求就是本次任务的目标 ---
        self._maybe_anchor_goal(event)

        # 记住最近的 Agent 自述 —— L2 解释时作为证据优先级最高的材料。
        if str(event.get("type") or "") == "thinking":
            text = str(event.get("text") or "")
            if text:
                self._last_thinking = text

        # --- 意图漂移：窗口**最先**推进 ---
        # 放在最前面是有讲究的：下面的 L2 解释是一次阻塞的模型调用，
        # 要是把漂移观察排在它后面，一次慢解释就会把滑窗拖住好几秒。
        # 漂移观察本身是**非阻塞**的（窗口满了就丢进队列），所以先走它。
        self._observe_intent(event)

        # --- 记忆管理（v3.5.0）---
        # ⚠️ 按**线程指纹**分路 ——
        # A 路的事件只进 A 路的短期滑窗，
        # 挤出去的事件交给 A 路的长期记忆（后台滚动摘要）。
        try:
            self._memory_for(event).observe(event)
        except Exception as exc:  # noqa: BLE001 —— 记忆失败不影响审查
            logger.warning(
                "写入短期记忆失败（已跳过）：%s: %s", type(exc).__name__, exc
            )

        # --- 微观统计（v4.0.0）：独立**前置**检测器 ---
        #
        # ⚠️ **先喂数据、后发告警** —— 这样 L1 红线**照常先出**，
        # 微统计不会把红线告警挤掉（红线 R1–R9 一字未改）。
        micro_findings = self._observe_micro_stats(event)

        verdict = self.rule_engine.check_event(event)
        name = str(verdict.get("verdict") or VERDICT_ON_TRACK)
        with self._alert_lock:
            self.counts[name] = self.counts.get(name, 0) + 1

        produced: Optional[Dict[str, Any]] = None
        if name in (VERDICT_DANGEROUS, VERDICT_LOOP):
            alert = self._build_alert(event, verdict)
            self._publish_alert(alert, event, verdict)
            produced = alert
        elif name == VERDICT_ON_TRACK:
            # v3.3.1：ON_TRACK 也留痕 —— 一行极简摘要。
            #
            # 以前 ON_TRACK 只被**计数**，用户看到的永远是一块空面板；
            # 现在它会变成看板底部的一行灰字，
            # 让人一眼看出「它一直在看，只是没出事」。
            self._emit_on_track(event)
            # --- v3.7.0：L2 关闭 + L3 开启时，L1 未命中的事件直打云端 ---
            self._bridge_to_l3_async(event, verdict)

        # --- 微观统计告警：命中就**打断**后续判定 ---
        #
        # 「打断」的意思是：不再往下跑上下文 / 时间盒判定。
        # 一次事件同时命中三类判定时，卡片会刷屏 ——
        # 用户真正要看的应该只有「它在原地打转」这一条。
        if micro_findings:
            for finding in micro_findings:
                micro_alert, micro_verdict = self._build_micro_alert(
                    event, finding
                )
                self._publish_alert(
                    micro_alert,
                    event,
                    micro_verdict,
                    cascade=False,
                    explain=False,
                )
                if produced is None:
                    produced = micro_alert
            return produced

        # --- 上下文模式：跨事件的 STUCK / LEAK ---
        context_hit = self._check_context(event)
        if context_hit is not None:
            context_alert, context_verdict = context_hit
            self._publish_alert(context_alert, event, context_verdict)
            produced = context_alert

        # --- 时间盒（v3.3.0）：同一件事磨太久 = 严重跑偏 ---
        #
        # 放在最后：前面的判定更「硬」（删文件、读同一个文件），
        # 时间盒是**兜底维度**，一条事件命中多个判定时以更硬的那个为准。
        timebox_hit = self._check_timebox(event)
        if timebox_hit is not None:
            timebox_alert, timebox_verdict = timebox_hit
            self._publish_alert(timebox_alert, event, timebox_verdict)
            produced = timebox_alert

        return produced

    # ------------------------------------------------------------------
    # Token 审计（v1.1.0）
    # ------------------------------------------------------------------
    def _record_tokens(self, event: Dict[str, Any]) -> int:
        """给台账记一个事件的 Token 消耗。

        有 ``usage`` 就采信它（真值）；没有就按字符估算。
        估算值会带上 ``estimated=True`` 标记 —— 绝不让估算冒充真值。

        :param event: 统一事件字典。
        :return: 本次新增的 Token 数。
        """
        if not isinstance(event, dict):
            return 0
        usage = extract_usage(event.get("raw"))
        if not usage:
            # 适配器也可能把 usage 直接挂在事件上（codex 的 turn.completed）。
            usage = extract_usage(event.get("usage"))
        text = str(event.get("text") or "")
        if not text:
            tool = event.get("tool")
            if isinstance(tool, dict):
                text = str(tool.get("command") or "")
        added = self.token_ledger.record_event(text=text, usage=usage)
        if added:
            self._maybe_emit_tokens()
        return added

    def _maybe_emit_tokens(self) -> None:
        """必要时往告警日志写一行 Token 累计。

        **节流**：没有 usage 时每个事件都会让估算值变化，
        逐事件写会把日志撑爆。所以累计值至少涨了
        :data:`TOKEN_EMIT_STEP` 才写一行 —— 看板上的计数器
        最多滞后 100 个 Token，肉眼看不出差别。
        """
        total = self.token_ledger.total_tokens
        if total - self._token_emit_total < TOKEN_EMIT_STEP:
            return
        self._token_emit_total = total
        payload = self.token_ledger.snapshot()
        payload["timestamp"] = time.time()
        try:
            self.logger.warning("%s %s", TOKEN_MARKER, json.dumps(payload, ensure_ascii=False))
        except Exception as exc:  # noqa: BLE001 —— 记账失败不影响审查
            logger.warning("写 Token 累计行失败：%s: %s", type(exc).__name__, exc)

    def flush_tokens(self) -> Dict[str, Any]:
        """把当前 Token 台账**强制**写一行，并返回快照。

        收尾时调用 —— 保证「会话结束时的那一次累计」一定落到盘上。

        :return: Token 快照。
        """
        self._token_emit_total = 0
        self._maybe_emit_tokens()
        return self.token_ledger.snapshot()

    def _emit_on_track(self, event: Dict[str, Any]) -> None:
        """把一条 ON_TRACK 判定写成「实时审核流水」行（v3.3.1）。

        这一行**不是告警** —— 它只是告诉用户「刚才那一下我看过了，
        没事」。看板把它渲染成面板底部的灰字流水。

        失败**绝不抛异常** —— 流水是锦上添花，
        绝不能拖垮审查主链路（架构红线 R5）。

        :param event: 统一事件字典。
        """
        event_type = str(event.get("type") or "")
        # tool_result 只是动作的**回执**（动作本身已经记过一行），
        # 再记一次会让同一次操作在流水里出现两遍。
        if event_type == "tool_result":
            return
        try:
            payload = {
                "summary": summarize_event(event),
                # ⚠️ v3.5.1：流水区要说的是**人话** ——
                #    ``snippet`` 是从 thinking / tool_call 里截出来的原文，
                #    前端拿它拼成「Agent 正在思考：……」这种句子。
                #    刻意**不**把原始 JSON 丢给前端。
                "snippet": extract_snippet(event),
                "timestamp": time.time(),
                "event_type": event_type,
                # ⚠️ v3.5.0：流水也要带指纹 ——
                #    否则看板分不清这一行是哪一路的。
                "fingerprint": str(event.get("fingerprint") or ""),
            }
            self.logger.warning(
                "%s %s", ONTRACK_MARKER, json.dumps(payload, ensure_ascii=False)
            )
        except Exception as exc:  # noqa: BLE001 —— 流水失败不影响审查
            logger.warning("写实时审核流水失败：%s: %s", type(exc).__name__, exc)

    def _publish_alert(
        self,
        alert: Dict[str, Any],
        event: Event,
        verdict: Dict[str, Any],
        *,
        cascade: bool = True,
        explain: bool = True,
    ) -> None:
        """告警的**统一出口**：挂起 → 标记 → 发出 → 排队解释。

        顺序是有讲究的：

        1. **先挂起** —— 红线必须立刻动手，不能等模型；
        2. **标记「解释在路」** —— 前端据此显示「模型解释生成中…」，
           而不是「本次事件没有附带解释」；
        3. **立刻发出原始告警** —— 没有解释，但**已经可读**（含命令与规则原因）；
        4. **最后才排队解释** —— 主循环到这一步就返回了。

        ⚠️ 第 3 步一定要在第 4 步**之前**：
        解释线程可能瞬间就返回了，更新事件先于原告警落盘的话，
        看板就找不到对应的卡片了。

        :param alert: 告警字典（会被回填解释相关字段）。
        :param event: 触发事件。
        :param verdict: 判定字典。
        :param cascade: 是否再丢给级联审核（桥接来的告警**不**再丢 ——
            否则就是无限回环）。
        :param explain: 是否排队生成解释（桥接告警自带 L3 的理由，
            不用再解释一遍）。
        """
        self._maybe_intervene(alert)
        if not explain:
            alert["explanation"] = str(alert.get("reason") or "")
            alert["explanation_degraded"] = False
            alert["explanation_pending"] = False
        elif self.llm_client is None:
            alert["explanation"] = ""
            alert["explanation_degraded"] = False
            alert["explanation_pending"] = False
        else:
            alert["explanation_pending"] = True
        self._record_alert(alert)
        if explain:
            self._explain_async(alert, event, verdict)
        # ⚠️ v3.5.0：级联审核（L2 本地过滤 → L3 云端终审）——
        #    丢给**后台线程**，主循环绝不等模型。
        if cascade:
            self._cascade_async(alert, event, verdict)

    def _cascade_payload(
        self,
        event: Event,
        verdict: Dict[str, Any],
    ) -> Dict[str, Any]:
        """拼装级联审核的载荷。

        :param event:  触发事件。
        :param verdict: 判定字典。
        :return: 载荷字典。
        """
        try:
            memory = self._memory_for(event)
            summary = memory.context()
        except Exception as exc:  # noqa: BLE001 —— 取上下文失败不影响审查
            logger.warning(
                "取级联上下文失败（已跳过）：%s: %s", type(exc).__name__, exc
            )
            summary = ""
        tool = event.get("tool")
        tool = tool if isinstance(tool, dict) else {}
        action = str(tool.get("command") or "").strip()
        if not action:
            action = summarize_event(event)
        return {
            "summary": summary,
            "action": action,
            "l1_verdict": str(verdict.get("verdict") or ""),
            "l1_reason": str(verdict.get("reason") or ""),
            "fingerprint": str(event.get("fingerprint") or ""),
        }

    def _cascade_async(
        self,
        alert: Dict[str, Any],
        event: Event,
        verdict: Dict[str, Any],
    ) -> None:
        """把这次告警丢给**级联审核**（后台线程，绝不阻塞）。

        ⚠️ 没装级联就直接返回 —— 行为与 v3.4.0 **完全一样**。

        :param alert:  告警字典。
        :param event:  触发事件。
        :param verdict: 判定字典。
        """
        if self.cascade is None:
            return
        payload = self._cascade_payload(event, verdict)
        self.cascade_worker.submit(str(alert.get("alert_id") or ""), payload)

    def _bridge_to_l3_async(
        self,
        event: Dict[str, Any],
        verdict: Dict[str, Any],
    ) -> None:
        """把 **L1 未命中**的事件桥接给云端 L3（v3.7.0）。

        触发条件（三个都要满足）：

        1. 级联已装配，且 ``bridge_l1_to_l3`` 打开；
        2. L2 已关闭、L3 已开启（否则没必要桥接）；
        3. 事件是**工具调用** —— 思考、文本、usage 不值得往云端送。

        ⚠️ **桥接是单盲** —— 只有 L3 一票，**永远不亮红灯**；
        L3 说有问题时生成一条**普通级**告警（照常记录，但不挂起）。

        :param event:  触发事件。
        :param verdict: 判定字典（ON_TRACK）。
        """
        router = self.cascade
        if router is None:
            return
        if not getattr(router, "bridging", False):
            return
        if str(event.get("type") or "") != "tool_call":
            return
        bridge_id = "bridge-{}".format(uuid.uuid4().hex[:12])
        payload = self._cascade_payload(event, verdict)
        payload["bridge"] = True
        with self._bridge_lock:
            self._bridge_pending[bridge_id] = event
        if not self.cascade_worker.submit(bridge_id, payload):
            with self._bridge_lock:
                self._bridge_pending.pop(bridge_id, None)

    def _on_bridge_result(
        self,
        event: Dict[str, Any],
        result: Any,
    ) -> None:
        """处理**桥接**事件的级联结论。

        L3 说没问题 → 什么都不做（本来就没事）。
        L3 说有问题 → 生成一条**普通级**告警（**不挂起**，因为是单盲）。

        :param event:  原事件。
        :param result: :class:`~src.llm.cascade.CascadeResult`。
        """
        l3 = getattr(result, "l3", None)
        if l3 is None or not getattr(l3, "available", False):
            logger.info(
                "桥接复核未拿到可用结论（已忽略）：%s",
                str(getattr(result, "reason", "") or ""),
            )
            return
        if not getattr(l3, "problem", False):
            return
        reason = str(getattr(l3, "reason", "") or "L3 单盲复核判定有问题")
        verdict = {
            "verdict": VERDICT_DANGEROUS,
            "reason": reason,
            "rule_id": "cascade_bridge_l3",
            "evidence": "",
            "level": "warn",
            "confidence": 0.5,
            "pattern": None,
            "severity": SEVERITY_STANDARD,
            "category": None,
            "rule_entry_id": None,
            "rule_action": "block",
            "log_only": False,
        }
        alert = self._build_alert(event, verdict)
        alert["bridged"] = True
        # ⚠️ 单盲结论**永不挂起** —— 「双盲才红灯、红线才挂起」不变。
        alert["single_blind"] = True
        alert["cascade"] = {
            "status": str(getattr(result, "status", "") or ""),
            "l2": "",
            "l3": str(getattr(l3, "verdict", "") or ""),
            "reason": reason,
        }
        logger.warning(
            "桥接复核命中：L3 单盲判定有问题（不挂起）—— %s", reason
        )
        self._publish_alert(alert, event, verdict, cascade=False, explain=False)

    def _attach_cascade_note(self, alert_id: str, result: Any) -> None:
        """把**单盲**级联结论记到原告警上（**不升级等级**）。

        L2 关闭时级联只剩 L3 一票 —— 结论照样要让用户看见，
        但**绝不**升级成红线（「双盲才红灯」不变）。

        :param alert_id: 原告警 ID。
        :param result:   :class:`~src.llm.cascade.CascadeResult`。
        """
        if not getattr(result, "bridged", False):
            return
        l3 = getattr(result, "l3", None)
        with self._alert_lock:
            for item in self.alerts:
                if str(item.get("alert_id") or "") != alert_id:
                    continue
                item["cascade"] = {
                    "status": str(getattr(result, "status", "") or ""),
                    "l2": "",
                    "l3": str(getattr(l3, "verdict", "") or ""),
                    "reason": str(getattr(result, "reason", "") or ""),
                }
                break

    def _find_alert(self, alert_id: str) -> Optional[Dict[str, Any]]:
        """按 ID 找出原告警。

        :param alert_id: 告警 ID。
        :return: 告警字典；找不到返回 ``None``。
        """
        if not alert_id:
            return None
        with self._alert_lock:
            for item in self.alerts:
                if str(item.get("alert_id") or "") == alert_id:
                    return item
        return None

    def _emit_cascade_notice(self, alert_id: str, result: Any) -> None:
        """把级联的**热切 / 熔断**提示写进实时审核流水（v3.8.0）。

        ⚠️ 这类提示**不是告警** —— 它不改变任何判定口径，
        只是让用户知道「L2 掉了，已经自动切到 L3」。

        :param alert_id: 原告警 ID。
        :param result:   :class:`~src.llm.cascade.CascadeResult`。
        """
        notice = str(getattr(result, "notice", "") or "")
        if not notice:
            return
        alert = self._find_alert(alert_id)
        fingerprint = ""
        if alert is not None:
            fingerprint = str(alert.get("fingerprint") or "")
        level = "danger" if getattr(result, "budget_exhausted", False) else "warn"
        payload = {
            # ⚠️ 服务端的流水解析要求 ``summary`` 非空 ——
            #    这里用提示语本身当摘要（它就是这一行要说的话）。
            "summary": notice,
            "snippet": "",
            "timestamp": time.time(),
            "event_type": "cascade_notice",
            "fingerprint": fingerprint,
            # ⚠️ v3.8.0：``audit_text`` / ``audit_level`` 是前端
            #    「这一行直接显示成什么、什么颜色」的约定。
            "audit_text": notice,
            "audit_level": level,
        }
        try:
            self.logger.warning(
                "%s %s", ONTRACK_MARKER, json.dumps(payload, ensure_ascii=False)
            )
        except Exception as exc:  # noqa: BLE001 —— 流水失败不影响审查
            logger.warning("写级联提示流水失败：%s: %s", type(exc).__name__, exc)

    def _emit_cascade_budget_alert(self, result: Any) -> None:
        """L3 回退预算熔断 —— 发一条**严重系统告警**（v3.8.0）。

        ⚠️ 这条告警**不走 L2 解释** ——
        预算熔断恰恰是因为 L2 掉线、L3 又被预算挡住了，
        再去问模型只会更糟。所以直接记录 + 输出 + 分发。

        ⚠️ 它也**不挂起** Agent —— 它说的是「预算用完了」，
        不是「Agent 干了坏事」。

        :param result: :class:`~src.llm.cascade.CascadeResult`。
        """
        if not getattr(result, "budget_exhausted", False):
            return
        reason = str(getattr(result, "reason", "") or "L3 回退预算已耗尽")
        alert = {
            "alert_id": "cascade-budget-{}".format(int(time.time() * 1000)),
            "timestamp": time.time(),
            "verdict": VERDICT_CASCADE_BUDGET,
            "level": "critical",
            "rule_id": "cascade_l3_budget_exhausted",
            "reason": reason,
            "evidence": str(getattr(result, "reason", "") or ""),
            "severity": SEVERITY_STANDARD,
            "category": "cascade_budget",
            "source": "cascade_router",
            "event_type": "cascade_budget",
            "tool": "",
            "command": "",
            "arguments": {},
            "explanation": reason,
            "explanation_degraded": False,
            "explanation_pending": False,
            "guardian": {},
            "agent": self.agent_name,
            "task_goal": self._current_goal(),
            "triggers": [TRIGGER_CASCADE_BUDGET],
            "token_usage": self.token_ledger.snapshot(),
        }
        logger.warning("L3 回退预算熔断，已放弃云端调用并回退 L1：%s", reason)
        try:
            self._record_alert(alert)
        except Exception as exc:  # noqa: BLE001 —— 输出失败不影响审查
            logger.warning(
                "记录预算熔断告警失败：%s: %s", type(exc).__name__, exc
            )

    def _on_cascade_result(self, alert_id: str, result: Any) -> None:
        """级联审核的结论回来了。

        ⚠️ **双盲亮红灯** ——
        只有 L2 与 L3 **都**判定有问题，才升级成最高级告警
        并触发守护者挂起。
        凑不齐双盲（只有一边、或者模型不可用）**一律不亮红灯**。

        :param alert_id: 原告警 ID。
        :param result:   :class:`~src.llm.cascade.CascadeResult`。
        """
        with self._bridge_lock:
            bridged_event = self._bridge_pending.pop(alert_id, None)
        if bridged_event is not None:
            # v3.7.0：这是**桥接**来的事件 —— 结论走另一条路。
            self._on_bridge_result(bridged_event, result)
            self._emit_cascade_stats()
            return
        if not getattr(result, "red_light", False):
            self._attach_cascade_note(alert_id, result)
            # ⚠️ v3.8.0：L2 掉线热切 L3 / 预算熔断 —— 这两件事**必须让人看见**，
            #    但它们**不改变**任何判定口径（热切是单盲，绝不亮红灯）。
            self._emit_cascade_budget_alert(result)
            self._emit_cascade_notice(alert_id, result)
            self._emit_cascade_stats()
            return
        if self.cascade is None:
            # ⚠️ **没装级联就没有双盲** ——
            #    凑不出「L2 与 L3 同时判定有问题」这个前提，
            #    就不许凭空升级成最高级告警。
            logger.warning(
                "未启用级联审核，忽略本次升级请求（%s）", alert_id
            )
            return
        alert = None
        with self._alert_lock:
            for item in self.alerts:
                if str(item.get("alert_id") or "") == alert_id:
                    alert = item
                    break
        if alert is None:
            return
        alert["severity"] = SEVERITY_RED_LINE
        alert["cascade"] = {
            "status": getattr(result, "status", ""),
            "l2": getattr(getattr(result, "l2", None), "verdict", ""),
            "l3": getattr(getattr(result, "l3", None), "verdict", ""),
            "reason": getattr(result, "reason", ""),
        }
        alert["escalated"] = True
        logger.warning(
            "双盲命中：L2 与 L3 同时判定有问题，告警升级为红线（%s）",
            alert_id,
        )
        # ⚠️ **先升级再通知** ——
        # 守护者挂起与最高级 Webhook 都读 ``severity``，
        # 顺序反了就会用旧等级发出去。
        self._maybe_intervene(alert)
        # 追加更新事件 —— 看板据此**更新同一张卡片**，而不是新开一张。
        payload = {
            "alert_id": alert_id,
            "timestamp": time.time(),
            "update": True,
            "severity": SEVERITY_RED_LINE,
            "escalated": True,
            "cascade": alert["cascade"],
        }
        try:
            self.logger.warning(
                "%s %s",
                ALERT_UPDATE_MARKER,
                json.dumps(payload, ensure_ascii=False),
            )
        except Exception as exc:  # noqa: BLE001 —— 升级失败不影响审查
            logger.warning(
                "写级联升级行失败：%s: %s", type(exc).__name__, exc
            )
        # 最高级预警 —— 发一次**升级**通知。
        self._dispatch(alert)
        self._emit_cascade_stats()

    def _emit_cascade_stats(self) -> None:
        """把级联审核的统计写成一行 ``CASCADE_JSON``（v3.5.0）。

        看板顶部那个「L2 已过滤 X 次冗余，为您节省 Y 次云端调用」
        就是靠这一行刷新的。

        ⚠️ **节流**：统计没变就不写 ——
        否则每次告警都多一行，看板要重新解析。

        :return: 无返回值。
        """
        if self.cascade is None:
            return
        try:
            stats = self.cascade.stats()
        except Exception as exc:  # noqa: BLE001 —— 统计失败不影响审查
            logger.warning(
                "读级联统计失败（已跳过）：%s: %s", type(exc).__name__, exc
            )
            return
        payload = dict(stats)
        payload["timestamp"] = time.time()
        signature = (
            payload.get("l2_calls"),
            payload.get("l3_calls"),
            payload.get("saved_cloud_calls"),
            payload.get("unavailable_calls"),
            payload.get("red_lights"),
            # ⚠️ v3.8.0：热切与预算熔断也要能刷新统计行 ——
            #    否则用户看不到「刚才热切了几次、熔断了几次」。
            payload.get("failover"),
            payload.get("budget_exhausted"),
        )
        if signature == self._cascade_emit_signature:
            return
        self._cascade_emit_signature = signature
        try:
            self.logger.warning(
                "%s %s", CASCADE_MARKER, json.dumps(payload, ensure_ascii=False)
            )
        except Exception as exc:  # noqa: BLE001 —— 统计失败不影响审查
            logger.warning(
                "写级联统计行失败：%s: %s", type(exc).__name__, exc
            )

    def _record_alert(self, alert: Dict[str, Any]) -> None:
        """登记一条告警并输出。

        :param alert: 告警字典。
        """
        with self._alert_lock:
            self.alerts.append(alert)
        self._emit_alert(alert)

    def _observe_intent(self, event: Event) -> None:
        """把事件喂给**它那一路**的意图追踪器（**非阻塞**）。

        ⚠️ v3.5.0：按**线程指纹**分路 ——
        A 路的日志只能进 A 路的滑窗，B 路的进 B 路的。
        认不出指纹的事件（空串）走默认那一路，
        行为与 v3.4.0 **完全一样**。

        :param event: 当前事件。
        """
        tracker = self._tracker_for(event)
        if tracker is None:
            return
        try:
            tracker.observe(event)
        except Exception as exc:  # noqa: BLE001 —— 审查侧异常绝不外泄
            logger.warning(
                "意图漂移检测失败，已跳过：%s: %s", type(exc).__name__, exc
            )

    def _tracker_for(self, event: Event) -> Optional[Any]:
        """取出某个事件**该用**的那个追踪器。

        没有指纹 → 默认那一路；
        有指纹但还没建过 → 现造一个（同配置），并挂上异步回调。

        :param event: 统一事件字典。
        :return: 追踪器实例；漂移检测关闭时返回 ``None``。
        """
        if self.intent_tracker is None:
            return None
        fingerprint = str(event.get("fingerprint") or "") if isinstance(
            event, dict
        ) else ""
        if not fingerprint:
            return self.intent_tracker
        with self._tracker_lock:
            tracker = self._intent_trackers.get(fingerprint)
            if tracker is None:
                try:
                    tracker = self.intent_tracker.sibling()
                except Exception as exc:  # noqa: BLE001 —— 退化到默认那一路
                    logger.warning(
                        "新建分路追踪器失败，退回默认路：%s: %s",
                        type(exc).__name__,
                        exc,
                    )
                    return self.intent_tracker
                try:
                    tracker.attach_async(self._on_drift_result)
                except Exception as exc:  # noqa: BLE001 —— 退化到默认那一路
                    logger.warning(
                        "分路追踪器挂回调失败，退回默认路：%s: %s",
                        type(exc).__name__,
                        exc,
                    )
                    return self.intent_tracker
                self._intent_trackers[fingerprint] = tracker
            return tracker

    def stream_trackers(self) -> Dict[str, Any]:
        """当前所有**分路**追踪器的快照（给看板显示「几路在跑」）。

        :return: ``{指纹: 追踪器}`` 字典（**不含**默认那一路）。
        """
        with self._tracker_lock:
            return dict(self._intent_trackers)

    # ------------------------------------------------------------------
    # 记忆管理（v3.5.0）
    # ------------------------------------------------------------------
    def _memory_for(self, event: Event) -> Any:
        """取出某个事件**那一路**的记忆（短期滑窗 + 长期背景）。

        ⚠️ **按指纹分路** —— A 路的历史**绝不**进 B 路的窗口。

        :param event: 统一事件字典。
        :return: :class:`~src.core.memory.StreamMemory` 实例。
        """
        fingerprint = str(event.get("fingerprint") or "") if isinstance(
            event, dict
        ) else ""
        key = fingerprint_stream_key(fingerprint)
        with self._tracker_lock:
            memory = self._memories.get(key)
            if memory is None:
                memory = StreamMemory(
                    capacity=self.memory_steps,
                    threshold=self.memory_summary_threshold,
                    summarizer=self._memory_summarizer,
                )
                self._memories[key] = memory
            return memory

    def _memory_summarizer(
        self,
        old_summary: str,
        events: List[Dict[str, Any]],
    ) -> str:
        """把被挤出窗口的事件汇总成一段「历史背景」（v3.5.0）。

        ⚠️ 这一步**跑在后台线程**里（由
        :class:`~src.core.memory.LongTermMemory` 调用）——
        慢也不影响主循环。

        :param old_summary: 旧摘要（可能为空）。
        :param events:       被挤出的事件。
        :return: 新摘要文本；没有模型可用就返回空串。
        """
        provider = self._summary_provider()
        if provider is None:
            return ""
        lines = []
        for index, event in enumerate(events, start=1):
            tool = event.get("tool")
            tool = tool if isinstance(tool, dict) else {}
            name = str(tool.get("name") or "unknown")
            command = str(tool.get("command") or "").strip()
            lines.append("{}.[{}] {}".format(index, name, command[:120]))
        body = "\n".join(lines)
        system = (
            "你在把一个 AI Agent 的历史动作压缩成一段简短的背景说明。"
            "只写事实，不要评价，不要加解释。不超过120字。"
        )
        user = "【已有背景】\n{}\n\n【新增动作】\n{}".format(
            old_summary or "（无）", body
        )
        try:
            raw = provider.chat(system, user)
        except Exception as exc:  # noqa: BLE001 —— 摘要失败不影响审查
            logger.warning(
                "背景摘要失败（已降级）：%s: %s", type(exc).__name__, exc
            )
            return ""
        return " ".join(str(raw or "").split())[:600]

    def _summary_provider(self) -> Optional[Any]:
        """拿来做背景摘要的模型（优先用**本地**那个）。

        :return: 提供方；没有就返回 ``None``。
        """
        if self.cascade is not None:
            provider = getattr(self.cascade, "l2", None)
            if provider is not None:
                return provider
        return None

    def stream_summary(self) -> List[Dict[str, Any]]:
        """当前所有**分路**的记忆概况（给看板显示）。

        :return: ``[{"key":…, "label":…, "steps":…}]`` 列表。
        """
        with self._tracker_lock:
            items = list(self._memories.items())
        result = []
        for key, memory in items:
            result.append({
                "key": key,
                "label": fingerprint_label(
                    "" if key == "default" else key
                ),
                "steps": memory.short.steps(),
                "summaries": memory.long.summaries,
            })
        return result

    # ------------------------------------------------------------------
    # 流式追踪
    # ------------------------------------------------------------------
    def _trace_event(self, event: Event, index: int) -> None:
        """在流式模式下打印一行「收到事件」。

        批量模式是「读完再汇总」，逐条打印没有意义；
        流式模式的价值恰恰是**看得见它在动** —— 每条新事件都留一行痕迹，
        用户才能确认监听真的接上了，而不是卡住不动。

        :param event: 当前事件。
        :param index: 第几条事件（从 1 开始）。
        """
        if not self.console:
            return
        kind = str(event.get("type") or "unknown")
        tool = event.get("tool") or {}
        if isinstance(tool, dict):
            name = str(tool.get("name") or "")
            command = str(tool.get("command") or "")
            arguments = tool.get("arguments")
        else:
            name = ""
            command = ""
            arguments = None
        detail = command or _summarize_arguments(arguments)
        detail = detail or str(event.get("text") or "")
        detail = " ".join(detail.split())
        if len(detail) > 90:
            detail = detail[:90] + "…"
        label = f"{kind}/{name}" if name else kind
        print(_console_safe(f"[TAIL] #{index} {label} | {detail}"))

    # ------------------------------------------------------------------
    # 意图漂移（语义级，仅警告）
    # ------------------------------------------------------------------
    def _on_drift_result(self, verdict: Dict[str, Any]) -> None:
        """后台线程的漂移结论回调（**异步模式的落点**）。

        模型判完了，结论在这儿变成告警 ——
        主事件循环此刻早就在读下一行日志了。

        :param verdict: 判定结果字典。
        """
        try:
            if not verdict:
                return
            if verdict.get("degraded"):
                # 模型判不了 → 这一窗按「未漂移」处理（红线 R4）。
                # 但**必须说出来** —— 静悄悄地失效最坏。
                self._warn_llm_degraded(str(verdict.get("reason") or ""))
                return
            if not verdict.get("alert"):
                return
            alert = self._build_drift_alert(verdict)
            if alert is not None:
                # 走统一出口 —— 漂移的 severity 是 standard，
                # 所以 _maybe_intervene 不会碰它（漂移只警告不干预）。
                self._publish_alert(
                    alert,
                    {
                        "source": "sentinel",
                        "type": "tool_call",
                        "tool": {
                            "name": "IntentTracker",
                            "command": str(verdict.get("summary") or ""),
                            "arguments": {},
                        },
                        "text": "",
                        # 漂移的解释里，**目标**才是关键证据 ——
                        # 所以这里显式给自述，不让引擎用「最近一次自述」覆盖。
                        "thinking": (
                            "用户给的原始目标是："
                            f"{self.intent_tracker.goal if self.intent_tracker else ''}。"
                        ),
                    },
                    {
                        "verdict": VERDICT_DRIFT,
                        "rule_id": "intent_drift",
                        "level": "warn",
                        "reason": DRIFT_ALERT_REASON,
                        "evidence": str(verdict.get("reason") or ""),
                        "severity": SEVERITY_STANDARD,
                        "category": "intent_drift",
                    },
                )
        except Exception as exc:  # noqa: BLE001 —— 审查侧异常绝不外泄
            logger.warning(
                "漂移告警生成失败，已跳过：%s: %s", type(exc).__name__, exc
            )

    def _warn_llm_degraded(self, reason: str) -> None:
        """L2 模型判不了时**明确说一声** —— 别让失效静悄悄发生。

        v3.2.0 实机演习撞上的真问题：本机 Ollama 服务在跑，但**一个模型都没装**，
        于是每次漂移判定都降级；而降级在口径上等于「没漂移」，
        漂移检测就此**静悄悄地死掉**，用户从头到尾什么都不知道。

        这里只提醒、**不改判定口径**（红线 R4）：
        降级依然算「未漂移」，绝不把「不知道」当成「有问题」。

        ⚠️ 同一个原因**只提醒一次** —— 模型长时间不可用时，
        每 5 步刷一行会把终端刷爆。

        :param reason: 降级原因（人话）。
        :return: 无返回值。
        """
        text = reason or "模型不可用"
        if text == self._llm_degraded_notice:
            return
        self._llm_degraded_notice = text
        print(
            _console_safe(
                "[SENTINEL] L2 模型判不了，本次意图漂移判定按「未漂移」处理"
                f"（{text}）。这段时间漂移检测等于没开 —— "
                "把主力模型装好，或改 llm.provider 指向可用的模型。"
            )
        )

    def _build_drift_alert(
        self,
        verdict: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """把一次漂移判定折算成告警字典。

        ⚠️ **漂移不是红线**：这条告警的 ``severity`` 是 ``standard``，
        所以 :meth:`_maybe_intervene` 不会碰它 —— 守护者不挂起进程，
        只弹窗警告。跑偏是「可能做错事」，不是「马上要毁掉东西」。

        :param verdict: 判定结果字典。
        :return: 告警字典；不该告警时返回 ``None``。
        """
        if self.intent_tracker is None:
            return None
        goal = self.intent_tracker.goal
        summary_text = str(verdict.get("summary") or "")
        detail = str(verdict.get("reason") or "")

        # 解释交给后台（v0.9.0）—— 这里不碰模型。
        alert: Dict[str, Any] = {
            "alert_id": self._next_alert_id(),
            "timestamp": time.time(),
            "verdict": VERDICT_DRIFT,
            "level": "warn",
            "rule_id": "intent_drift",
            "reason": DRIFT_ALERT_REASON,
            "evidence": detail,
            "severity": SEVERITY_STANDARD,
            "category": "intent_drift",
            "source": "sentinel",
            "event_type": "intent_drift",
            "tool": "IntentTracker",
            "command": summary_text[:500],
            "arguments": {},
            "drift": {
                "goal": goal,
                "reason": detail,
                "steps": int(verdict.get("steps") or 0),
                "summary": summary_text,
            },
            # --- 远程推送需要的上下文 ---
            "agent": self.agent_name,
            "task_goal": goal,
            # 连续意图漂移是「叫醒服务」的两类触发之一。
            "triggers": [TRIGGER_INTENT_DRIFT],
            # --- Token 审计（v1.1.0）---
            "token_usage": self.token_ledger.snapshot(),
        }
        # 连续漂移 = 这次异常**被拦住了**（不再继续跑偏）——
        # 同样告诉用户「省下了多少无效 Token 消耗」。
        self._attach_token_saved(alert)
        return alert

    def _next_alert_id(self) -> str:
        """生成下一个告警 ID（时间戳 + 自增序号）。"""
        with self._alert_lock:
            self._alert_seq += 1
            seq = self._alert_seq
        return f"{int(time.time() * 1000)}-{seq:06d}"

    # ------------------------------------------------------------------
    # 自动锚定任务目标
    # ------------------------------------------------------------------
    def _maybe_anchor_goal(self, event: Event) -> None:
        """首个用户请求 → 自动锚定任务目标。

        用户交代的那句话**就是**原始目标。引擎在监听流的初始阶段
        找第一条 :data:`EVENT_USER_MESSAGE` 事件，把它作为 ``task_goal``。

        优先级：

        1. 命令行 ``--task-goal`` / 看板输入框（**显式**）—— 谁都不许覆盖；
        2. 流里自动提取到的用户请求 —— 覆盖配置文件里的默认值；
        3. 配置文件里的 ``task_goal`` —— 兜底。

        ⚠️ v3.5.0：目标**按路锚定** ——
        多个 Agent 并行时，A 路的任务目标不该套到 B 路上。
        所以这里锚的是「**这一条事件所属的那一路**」。

        :param event: 当前事件。
        """
        tracker = self._tracker_for(event)
        if tracker is None:
            return
        if str(event.get("type") or "") != EVENT_USER_MESSAGE:
            return
        text = " ".join(str(event.get("text") or "").split())
        if not text:
            return
        goal = text[:GOAL_MAX_CHARS]
        if not tracker.set_goal(goal):
            return
        print(_console_safe(f"[SENTINEL] 已自动锚定任务目标: {goal}"))

    # ------------------------------------------------------------------
    # 守护者（主动干预）
    # ------------------------------------------------------------------
    def _l1_action(self) -> str:
        """当前生效的 **L1 拦截策略**。

        ⚠️ 每次都**现读**规则引擎 ——
        热重载换掉规则引擎时，策略跟着换，引擎不用重启。

        :return: ``red_line_only`` / ``suspend`` / ``alert`` / ``escalate``；
            认不出来时退回 ``red_line_only``（等价现状）。
        """
        selection = getattr(self.rule_engine, "selection", None)
        action = str(getattr(selection, "l1_action", "") or "")
        if action in VALID_L1_ACTIONS:
            return action
        return L1_ACTION_RED_LINE_ONLY

    def _maybe_intervene(self, alert: Dict[str, Any]) -> None:
        """红线级事件交给守护者处理。

        默认（``rules.l1_action = "red_line_only"``）只有
        ``severity == "red_line"`` 的告警才有资格触发干预 ——
        守护者**不是**「更吵的告警器」，普通高危命令它一眼都不看。

        v3.7.0 起，用户可以在设置页改这个策略：

        * ``red_line_only``（默认）—— 只有红线挂起（**等同现状**）；
        * ``suspend`` —— L1 判为「危险」就挂起（不再只认红线）；
        * ``alert`` —— 只告警，**绝不挂起**；
        * ``escalate`` —— 先不挂起，交给 L2 二次判断。

        另外，被降级为「仅记录」的规则（``log_only``）**永远不挂起**。

        干预失败（没有 PID、psutil 缺失、权限不足）**绝不抛异常**：
        审查侧的异常绝不能影响被监控的 Agent（架构红线 R5）。

        :param alert: 已构造好的告警字典（会被就地补上 ``guardian`` 字段）。
        """
        if self.guardian is None:
            return
        strategy = self._l1_action()
        if strategy == L1_ACTION_ALERT:
            # 「只告警不挂起」—— 用户明确要求，绝不挂起。
            return
        if strategy == L1_ACTION_ESCALATE:
            # 「放行给 L2 二次判断」—— 先不挂起，等级联的双盲结论。
            return
        if alert.get("log_only"):
            # 该规则被降级成「仅记录」—— 照常告警，但不挂起。
            return
        if alert.get("single_blind"):
            # 单盲结论（L3 一票）—— **永不挂起**。
            return
        severity = str(alert.get("severity") or "")
        if strategy == L1_ACTION_SUSPEND:
            # 「命中即挂起」—— 危险判定（红线 / 普通级）都动手；
            # 死循环判定（LOOP）不挂起 —— 它不是「不可逆操作」。
            if severity not in (SEVERITY_RED_LINE, SEVERITY_STANDARD):
                return
            if str(alert.get("verdict") or "") != VERDICT_DANGEROUS:
                return
        elif severity != SEVERITY_RED_LINE:
            return
        # --- 安全点（v2.0.0）：挂起**之前**先把工作目录拍个快照 ---
        #
        # 顺序很关键：守护者挂起进程要花几十毫秒，
        # 那段时间里 Agent 还活着、还可能继续改文件。
        # 所以快照必须在**动手之前**做完。
        safe_point = self._capture_safe_point(alert)
        if safe_point:
            alert["safe_point"] = safe_point
        try:
            result = self.guardian.intervene(
                {
                    "rule_id": alert.get("rule_id"),
                    "reason": alert.get("reason"),
                    "command": alert.get("command"),
                    "source": "engine",
                }
            )
        except Exception as exc:  # noqa: BLE001 —— 干预失败不影响审查
            logger.warning(
                "守护者干预失败，已跳过：%s: %s", type(exc).__name__, exc
            )
            return
        alert["guardian"] = result
        # 真的动手拦下来了 —— 这才值得**叫醒**用户。
        # 拦截失败不标记：手机被一条「拦不住」的告警吵醒更烦人。
        if isinstance(result, dict) and result.get("ok"):
            triggers = alert.setdefault("triggers", [])
            if isinstance(triggers, list) and TRIGGER_GUARDIAN_INTERCEPT not in triggers:
                triggers.append(TRIGGER_GUARDIAN_INTERCEPT)
            # 挂起成功 = 这次异常**真的被拦住了** ——
            # 告诉用户「省下了多少无效 Token 消耗」。
            self._attach_token_saved(alert)

    def _ensure_baseline_safe_point(self) -> None:
        """会话开始时打一个**基线安全点**。

        基线 = 工作目录的「起点」快照 —— 它是**回滚的目标**。
        Agent 在这次任务里改乱的东西，回滚到基线就都没了。

        已经打过就不重复打（同一个工作目录可能跑多路会话）。

        失败只记一条日志 —— 审查侧不该因为打个快照就中断。
        """
        service = self.safe_point_service
        if service is None or not self.workdir:
            return
        try:
            if service.baseline(workdir=self.workdir):
                return
            service.capture(
                self.workdir,
                label="基线安全点 · {time}".format(
                    time=time.strftime("%Y-%m-%d %H:%M:%S")
                ),
            )
        except Exception as exc:  # noqa: BLE001 —— 快照失败不影响审查
            logger.warning(
                "打基线安全点失败（已降级）：%s: %s", type(exc).__name__, exc
            )

    def _capture_safe_point(self, alert: Dict[str, Any]) -> Dict[str, Any]:
        """红线拦截前打一个安全点快照。

        快照失败**绝不抛异常** —— 审查侧的异常一律降级
        （架构红线 R5）：打不了快照也要照常挂起进程。

        :param alert: 告警字典（只读）。
        :return: 安全点信息字典；没打成功返回空字典。
        """
        service = self.safe_point_service
        if service is None or not self.workdir:
            return {}
        try:
            point = service.capture(
                self.workdir,
                label=(
                    "红线拦截快照 · {rule} · {time}".format(
                        rule=str(alert.get("rule_id") or "-"),
                        time=time.strftime("%H:%M:%S"),
                    )
                ),
            )
        except Exception as exc:  # noqa: BLE001 —— 快照失败不影响审查
            logger.warning(
                "打安全点快照失败（已降级，继续拦截）：%s: %s",
                type(exc).__name__,
                exc,
            )
            return {}
        data = point.to_dict() if hasattr(point, "to_dict") else dict(point)
        if not data.get("id"):
            return {}
        # 回滚目标 = 拦截发生时**生效**的安全点（默认是会话起点的基线）。
        try:
            baseline = service.baseline(workdir=self.workdir)
        except Exception:  # noqa: BLE001 —— 取不到基线不影响审查
            baseline = None
        if baseline:
            data["rollback_point"] = baseline
            data["rollback_label"] = str(baseline.get("label") or "")
        return data

    def _attach_token_saved(self, alert: Dict[str, Any]) -> None:
        """给告警挂上「挽回 Token」的估算。

        只在**真的拦住了**时才有意义 ——
        拦不住的话，「省下了多少」就是一句空话。

        :param alert: 告警字典（会被就地补上 ``token_saved`` 字段）。
        """
        try:
            saved = self.token_ledger.estimate_saved()
        except Exception as exc:  # noqa: BLE001 —— 估算失败不影响告警
            logger.warning("估算挽回 Token 失败：%s: %s", type(exc).__name__, exc)
            return
        alert["token_saved"] = saved
        alert["token_saved_text"] = str(saved.get("text") or "")

    def _resolve_guardian_target(self, log_file_path: Any) -> None:
        """尽力定位被监控进程，并交给守护者记住。

        哨兵读的是 Agent 写的日志，而那个 Agent 的命令行里通常就带着
        这个日志路径 —— 这是最稳的定位方式。定位不到也不影响审查，
        只是干预会退化成「报告但不动手」。

        :param log_file_path: 正在审查的日志路径。
        """
        if self.guardian is None:
            return
        try:
            if self.guardian.resolve_pid() is not None:
                return
            pid = self.guardian.detect_pid(str(log_file_path))
            if pid:
                self.guardian.set_target_pid(pid)
        except Exception as exc:  # noqa: BLE001 —— 定位失败不影响审查
            logger.warning(
                "定位被监控进程失败：%s: %s", type(exc).__name__, exc
            )

    # ------------------------------------------------------------------
    # 微观统计（v4.0.0）—— 独立前置检测器
    # ------------------------------------------------------------------
    def _observe_micro_stats(
        self,
        event: Event,
    ) -> List[MicroStatFinding]:
        """把事件喂给微观统计检测器。

        ⚠️ 这里**只喂数据、不发告警** ——
        发告警在 :meth:`process_event` 里、L1 判定**之后**，
        这样红线告警永远先出（红线 R1–R9 一字未改）。

        :param event: 当前事件。
        :return: 本次命中的结论列表（没有命中就是空列表）。
        """
        detector = self.micro_stats
        if detector is None:
            return []
        try:
            findings = detector.observe(event)
        except Exception as exc:  # noqa: BLE001 —— 检测器异常不影响审查
            logger.warning(
                "微观统计检测失败，已跳过：%s: %s", type(exc).__name__, exc
            )
            return []
        if not findings:
            return []
        return [item for item in findings if isinstance(item, MicroStatFinding)]

    def _build_micro_alert(
        self,
        event: Event,
        finding: MicroStatFinding,
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """把一条微观统计结论组装成告警。

        ⚠️ **不解释、不上云** ——
        微观统计的结论本身就是**确定性的事实**
        （「同一文件在窗口内被改了 6 次」），
        再让模型解释一遍是纯浪费；推给 L3 终审同理。
        它的价值是「立刻看见」，不是「再确认一次」。

        :param event: 触发事件。
        :param finding: 结论。
        :return: ``(告警字典, 判定字典)``。
        """
        verdict = finding_to_verdict(finding)
        alert = self._build_alert(event, verdict)
        # 触发类型标记 —— 远程通道据此决定要不要推。
        alert.setdefault("triggers", []).append(TRIGGER_MICRO_STATS)
        # 结论原文（次数、窗口、字符数……）—— 用户要看到「凭什么是它」。
        alert["micro_stats"] = finding.to_dict()
        return alert, verdict

    def _check_context(
        self,
        event: Event,
    ) -> Optional[Tuple[Dict[str, Any], Dict[str, Any]]]:
        """用上下文追踪器检查跨事件模式。

        :param event: 当前事件。
        :return: 命中时返回 ``(告警字典, 判定字典)``，否则返回 ``None``。
        """
        if self.context_tracker is None:
            return None
        try:
            reason = self.context_tracker.observe(event)
        except Exception as exc:  # noqa: BLE001 —— 追踪器异常不影响主流程
            logger.warning("上下文追踪失败，已跳过：%s: %s", type(exc).__name__, exc)
            return None
        if not reason:
            return None

        # 形如 "[STUCK] 测试连续失败三次，Agent 可能卡住了"
        tag = ""
        if reason.startswith("["):
            end = reason.find("]")
            if end > 0:
                tag = reason[1:end]
        verdict_name = tag or VERDICT_STUCK
        level = "critical" if verdict_name == VERDICT_LEAK else "warn"

        # 「读了敏感文件 + 紧接着外发到外部地址」= 红线级，
        # 这是守护者模式除破坏性命令之外的第二个触发源。
        severity = SEVERITY_STANDARD
        category: Optional[str] = None
        leak_external = bool(
            getattr(self.context_tracker, "last_leak_external", False)
        )
        if verdict_name == VERDICT_LEAK and leak_external:
            severity = SEVERITY_RED_LINE
            category = "leak_external"

        verdict = {
            "verdict": verdict_name,
            "reason": reason,
            "rule_id": f"context_{verdict_name.lower()}",
            "evidence": "",
            "level": level,
            "confidence": 0.8,
            "severity": severity,
            "category": category,
        }
        alert = self._build_alert(event, verdict)
        # 上下文告警要带上最近几步，否则用户不知道为什么被判定。
        history = getattr(self.context_tracker, "history", [])
        alert["context_history"] = list(history)
        # 死循环 / 敏感数据外泄 —— 归入「上下文异常」触发类型。
        alert.setdefault("triggers", []).append(TRIGGER_CONTEXT_ANOMALY)
        # ⚠️ 这里**不**附解释 —— 解释由调用方丢后台（v0.9.0）。
        return alert, verdict

    # ------------------------------------------------------------------
    # 时间盒（v3.3.0）
    # ------------------------------------------------------------------
    def _check_timebox(
        self,
        event: Event,
    ) -> Optional[Tuple[Dict[str, Any], Dict[str, Any]]]:
        """用时间盒检测器检查「一个子任务吃掉整轮」。

        :param event: 当前事件。
        :return: 命中时返回 ``(告警字典, 判定字典)``，否则返回 ``None``。
        """
        detector = self.timebox
        if detector is None:
            return None
        try:
            verdict = detector.observe(event)
        except Exception as exc:  # noqa: BLE001 —— 检测器异常不影响主流程
            logger.warning("时间盒检测失败，已跳过：%s: %s", type(exc).__name__, exc)
            return None
        if not verdict:
            return None
        alert = self._build_alert(event, verdict)
        alert.setdefault("triggers", []).append(TRIGGER_TIMEBOX)
        # 守护者：只有用户显式打开「超时死磕就挂起」才会动手。
        self._maybe_suspend_for_timebox(alert)
        return alert, verdict

    def _maybe_suspend_for_timebox(self, alert: Dict[str, Any]) -> None:
        """时间盒撞破时，按配置决定要不要**挂起**死磕的进程。

        ⚠️ 默认**跟着守护者的武装状态走**
        --------------------------------

        用户把 ``alert.intervention_mode`` 设成 ``guardian``，
        就等于说了「该拦的给我拦」——
        「超时死磕」也是一种该拦的情况。

        想改成「永远只告警不挂起」，
        把 ``rules.timebox.guardian_suspend`` 显式设成 ``false``。

        挂起是**可逆**的（看板上一键恢复），
        且**只针对**被监控的那个进程。

        :param alert: 已构造好的告警字典（会被就地补上 ``guardian`` 字段）。
        :return: 无。
        """
        detector = self.timebox
        if detector is None:
            return
        if self.guardian is None:
            return
        allowed = False
        try:
            allowed = bool(
                detector.guardian_suspend_allowed(self.guardian)
            )
        except Exception as exc:  # noqa: BLE001 —— 判断失败不干预
            logger.warning(
                "时间盒挂起判定失败，已跳过：%s: %s", type(exc).__name__, exc
            )
            return
        if not allowed:
            return
        try:
            result = self.guardian.intervene(
                {
                    "rule_id": str(alert.get("rule_id") or ""),
                    "reason": str(alert.get("reason") or ""),
                    "command": str(alert.get("command") or ""),
                    "source": "timebox",
                }
            )
        except Exception as exc:  # noqa: BLE001 —— 干预失败不影响审查
            logger.warning(
                "时间盒挂起失败，已跳过：%s: %s", type(exc).__name__, exc
            )
            return
        alert["guardian"] = result
        if isinstance(result, dict) and result.get("ok"):
            triggers = alert.setdefault("triggers", [])
            if isinstance(triggers, list) and (
                TRIGGER_GUARDIAN_INTERCEPT not in triggers
            ):
                triggers.append(TRIGGER_GUARDIAN_INTERCEPT)

    # ------------------------------------------------------------------
    # L2 本地模型解释
    # ------------------------------------------------------------------
    def _explain_async(
        self,
        alert: Dict[str, Any],
        event: Event,
        verdict: Dict[str, Any],
    ) -> None:
        """把 L2 解释请求丢进**后台队列**（主循环绝不等待）。

        L1 命中时原始告警已经发出去了（守护者也动手了），
        这里只负责「回头补一句人话」。

        :param alert: 已经发出的告警字典（会被回填解释字段）。
        :param event: 触发事件。
        :param verdict: 判定字典。
        """
        if self.llm_client is None:
            return

        payload = dict(event)
        if not payload.get("thinking"):
            payload["thinking"] = self._last_thinking
        payload["verdict"] = verdict

        alert_id = str(alert.get("alert_id") or "")
        if not alert_id:
            return
        self.explanation_worker.submit(alert_id, payload)

    def _on_explanation(
        self,
        alert_id: str,
        fields: Dict[str, Any],
    ) -> None:
        """后台解释回来了 —— 回填原告警，并发出一条**追加更新**事件。

        追加更新走告警日志（``ALERT_UPDATE_JSON``）→ 看板的 tailer →
        SSE → 前端把解释**淡入已有卡片**，而不是新开一张。

        :param alert_id: 原告警 ID。
        :param fields: 解释字段（``explanation`` 等）。
        """
        # --- 回填内存里的原告警 ---
        with self._alert_lock:
            for alert in self.alerts:
                if str(alert.get("alert_id") or "") == alert_id:
                    alert.update(fields)
                    break

        payload = {
            "alert_id": alert_id,
            "timestamp": time.time(),
            "update": True,
            "explanation_pending": False,
        }
        payload.update(fields)

        # --- 人读的一行（给 tail -f 的用户看）---
        text = str(fields.get("explanation") or "")
        if text:
            self.logger.warning("人话解释（补充）：%s", text)
        else:
            self.logger.warning(
                "人话解释（补充）：模型未提供解释（本次为规则原始判定）"
            )

        # --- 追加更新事件：给看板 / 机器消费 ---
        self.logger.warning(
            "%s %s", ALERT_UPDATE_MARKER, json.dumps(payload, ensure_ascii=False)
        )

        # --- 终端补充 ---
        if self.console:
            if text:
                print(_console_safe(f"  ↳ 人话解释（补充）：{text}"))
            else:
                print(
                    _console_safe(
                        "  ↳ 人话解释（补充）：模型未提供解释"
                        "（本次为规则原始判定）"
                    )
                )

        # --- 外部观察者（演练 / 集成方）---
        # 观察者失败**只记日志** —— 审查侧的异常绝不能外泄（红线 R5）。
        if self.on_explanation is not None:
            try:
                self.on_explanation(alert_id, dict(fields))
            except Exception as exc:  # noqa: BLE001 —— 观察者失败不影响审查
                logger.warning(
                    "解释观察者回调失败：%s: %s", type(exc).__name__, exc
                )

    # ------------------------------------------------------------------
    # 告警构造与输出
    # ------------------------------------------------------------------
    def _build_alert(self, event: Event, verdict: Dict[str, Any]) -> Dict[str, Any]:
        """把事件与判定拼装成一条结构化告警。

        告警带一个稳定的 ``alert_id`` —— L2 解释异步回来时靠它
        把补充内容**对上号**（追加进同一张卡片，而不是新开一张）。
        """
        tool = event.get("tool")
        tool = tool if isinstance(tool, dict) else {}
        arguments = tool.get("arguments")
        arguments = arguments if isinstance(arguments, dict) else {}
        with self._alert_lock:
            self._alert_seq += 1
            seq = self._alert_seq
        return {
            "alert_id": f"{int(time.time() * 1000)}-{seq:06d}",
            "timestamp": time.time(),
            "verdict": verdict.get("verdict"),
            "level": verdict.get("level", "warn"),
            "rule_id": verdict.get("rule_id"),
            "reason": verdict.get("reason", ""),
            "evidence": verdict.get("evidence", ""),
            "severity": verdict.get("severity", SEVERITY_STANDARD),
            "category": verdict.get("category"),
            # --- v3.7.0：规则清单的三个新字段 ---
            #: 规则清单 id（如 ``dangerous.rm_rf``）。
            "rule_entry_id": verdict.get("rule_entry_id"),
            #: 命中后的动作（``block`` / ``log``）。
            "rule_action": verdict.get("rule_action"),
            #: 是否被降级成「仅记录」（告警但不挂起）。
            "log_only": bool(verdict.get("log_only", False)),
            "source": event.get("source"),
            "event_type": event.get("type"),
            # ⚠️ v3.5.0：线程指纹 —— 看板据此把同一份日志里的
            #    多路 Agent **拆成多张虚拟卡片**。
            "fingerprint": str(event.get("fingerprint") or ""),
            "tool": tool.get("name"),
            "command": str(tool.get("command") or "")[:500],
            "arguments": arguments,
            # --- 远程推送（手机叫醒服务）需要的上下文 ---
            # 「是哪个 Agent」「它本来该干什么」—— 没有这两条，
            # 手机上那条通知就只是「某处出事了」，没法处理。
            "agent": self.agent_name,
            "task_goal": self._current_goal(),
            #: 触发类型标记（如守护者拦截、连续漂移）——
            #: 远程通道据此决定要不要推。
            "triggers": [],
            # --- Token 审计（v1.1.0）---
            #: 本次会话到此刻为止的 Token 累计。
            "token_usage": self.token_ledger.snapshot(),
        }

    def _current_goal(self) -> str:
        """取当前的任务目标（给远程通知当上下文）。

        :return: 任务目标文本；没设定时返回空串。
        """
        tracker = self.intent_tracker
        if tracker is None:
            return ""
        try:
            return str(getattr(tracker, "goal", "") or "")
        except Exception:  # noqa: BLE001 —— 取不到不影响审查
            return ""

    def _emit_alert(self, alert: Dict[str, Any]) -> None:
        """输出告警：写入告警日志 + 终端红色提示 + 分发到通知通道。"""
        message = format_alert_message(alert)

        # 告警日志里额外写一份结构化 JSON，方便后续复盘与机器消费。
        self.logger.warning("%s", message)
        self.logger.warning("ALERT_JSON %s", json.dumps(alert, ensure_ascii=False))

        if self.console:
            safe = _console_safe(message)
            color = _BOLD_RED if alert.get("level") == "critical" else _RED
            if _supports_ansi():
                print(f"{color}{safe}{_RESET}")
            else:
                print(safe)

        self._dispatch(alert)

    def emit_event(
        self,
        kind: str,
        title: str,
        detail: str = "",
        *,
        severity: str = "info",
        safe_point_id: str = "",
        point_id: str = "",
        **extra: Any,
    ) -> Dict[str, Any]:
        """写一条「操作事件」到告警日志（v2.2.0）。

        告警说的是「**发现了什么**」，事件说的是「**我们做了什么**」——
        开始目标、复活重试、回滚、注入、热重载。
        看板拿这些事件去拼「案发时间线」。

        失败**绝不抛异常** —— 写事件失败不该影响审查（架构红线 R5）。

        :param kind: 事件类型（如 ``session_start`` / ``resurrect``）。
        :param title: 一句话标题。
        :param detail: 补充说明。
        :param severity: 严重程度（``info`` / ``warn`` / ``critical``）。
        :param safe_point_id: 关联的安全点 ID。
        :param point_id: 算 Diff 用的回滚基线 ID。
        :param extra: 附加信息。
        :return: 事件字典（写失败也照样返回，内容如实标记）。
        """
        payload: Dict[str, Any] = {
            "kind": str(kind or "event"),
            "title": str(title or ""),
            "detail": str(detail or ""),
            "severity": str(severity or "info"),
            "timestamp": time.time(),
            "safe_point_id": str(safe_point_id or ""),
            "point_id": str(point_id or ""),
            "extra": dict(extra),
        }
        try:
            self.logger.warning(
                "%s %s", EVENT_MARKER, json.dumps(payload, ensure_ascii=False)
            )
        except Exception as exc:  # noqa: BLE001 —— 写事件失败只记日志
            logger.warning("写操作事件失败：%s: %s", type(exc).__name__, exc)
            payload["write_error"] = "{}: {}".format(type(exc).__name__, exc)
        return payload

    def _dispatch(self, alert: Dict[str, Any]) -> None:
        """把告警分发给多通道通知器。

        通知失败**绝不影响**哨兵主流程 —— 桌面弹窗挂了、webhook 超时了，
        都不该让审查中断。
        """
        if self.notifier is None:
            return
        try:
            self.notifier.notify(alert)
        except Exception as exc:  # noqa: BLE001 —— 通知失败只记日志
            logger.warning("告警通道分发失败：%s: %s", type(exc).__name__, exc)

    # ------------------------------------------------------------------
    # 系统级告警（v3.3.0：本地引擎心跳看门狗）
    # ------------------------------------------------------------------
    def set_engine_watchdog(self, watchdog: Optional[Any]) -> None:
        """装上引擎心跳看门狗（v3.3.0）。

        看门狗是**进程内共享**的（同一台引擎只起一个），
        所以它**不**随某一个会话的结束而停 ——
        它的生命周期归进程管（守护进程一直挂着，就一直探活）。

        :param watchdog: 看门狗实例；``None`` 表示卸掉。
        :return: 无。
        """
        self.engine_watchdog = watchdog
        if watchdog is None:
            return
        add_listener = getattr(watchdog, "add_listener", None)
        if callable(add_listener):
            try:
                add_listener(self.on_watchdog_alert)
            except Exception as exc:  # noqa: BLE001 —— 注册失败不影响审查
                logger.warning(
                    "注册引擎看门狗回调失败：%s: %s", type(exc).__name__, exc
                )

    def on_watchdog_alert(self, alert: Dict[str, Any]) -> None:
        """接住看门狗发来的告警（引擎掉线 / 恢复）。

        这是**公开回调** —— :mod:`src.core.engine_watchdog` 会调它。

        ⚠️ 这类告警**不走 L2 解释** —— 引擎都掉线了，
        解释它的模型**自己就是掉线的那个**。
        所以直接记录 + 输出 + 分发。

        :param alert: 看门狗给的告警字典。
        :return: 无。
        """
        if not isinstance(alert, dict):
            return
        data = dict(alert)
        data.setdefault("agent", self.agent_name)
        if not data.get("task_goal"):
            data["task_goal"] = self._current_goal()
        data.setdefault("token_usage", self.token_ledger.snapshot())
        data.setdefault("explanation", "")
        data.setdefault("explanation_degraded", False)
        data.setdefault("explanation_pending", False)
        triggers = data.setdefault("triggers", [])
        if isinstance(triggers, list) and TRIGGER_ENGINE_OFFLINE not in triggers:
            triggers.append(TRIGGER_ENGINE_OFFLINE)
        try:
            self._record_alert(data)
        except Exception as exc:  # noqa: BLE001 —— 输出失败不影响审查
            logger.warning(
                "记录引擎看门狗告警失败：%s: %s", type(exc).__name__, exc
            )

    # ------------------------------------------------------------------
    # 资源
    # ------------------------------------------------------------------
    def close(self) -> None:
        """关闭告警日志文件句柄，并释放通知通道持有的资源。"""
        # 记下结束时间 —— 晨报要靠它算执行时长。
        if not self.ended_at:
            self.ended_at = time.time()
        # 先把 Token 台账的最终数字落盘 —— 会话结束时的累计不能丢。
        try:
            self.flush_tokens()
        except Exception as exc:  # noqa: BLE001 —— 收尾失败不影响退出
            logger.warning("写 Token 收尾行失败：%s: %s", type(exc).__name__, exc)
        # 先停后台线程 —— 它们可能正准备往告警日志里写东西。
        try:
            self.explanation_worker.stop()
        except Exception as exc:  # noqa: BLE001 —— 释放失败不影响退出
            logger.warning(
                "停止 L2 解释线程失败：%s: %s", type(exc).__name__, exc
            )
        # ⚠️ v3.5.0：级联审核线程也要停 ——
        # 漏停就会变成新的「后台幽灵」。
        try:
            self.cascade_worker.stop()
        except Exception as exc:  # noqa: BLE001 —— 释放失败不影响退出
            logger.warning(
                "停止级联审核线程失败：%s: %s", type(exc).__name__, exc
            )
        if self.intent_tracker is not None:
            try:
                self.intent_tracker.stop()
            except Exception as exc:  # noqa: BLE001 —— 释放失败不影响退出
                logger.warning(
                    "停止漂移判定线程失败：%s: %s", type(exc).__name__, exc
                )
        # ⚠️ v3.5.0：分路追踪器也要停 ——
        # 它们各自起了一条后台线程，漏停就会变成新的「后台幽灵」。
        with self._tracker_lock:
            stream_trackers = list(self._intent_trackers.values())
            self._intent_trackers.clear()
        for tracker in stream_trackers:
            try:
                tracker.stop()
            except Exception as exc:  # noqa: BLE001 —— 释放失败不影响退出
                logger.warning(
                    "停止分路漂移判定线程失败：%s: %s", type(exc).__name__, exc
                )
        # ⚠️ v4.0.0：L2 调度器也要停 —— 它内部有一条工作线程。
        # 只停**自己建**的那个：外部传进来的调度器归调用方管。
        if self._owns_l2_scheduler:
            try:
                self.l2_scheduler.stop()
            except Exception as exc:  # noqa: BLE001 —— 释放失败不影响退出
                logger.warning(
                    "停止 L2 调度器失败：%s: %s", type(exc).__name__, exc
                )
        for handler in list(self.logger.handlers):
            # 只摘**自己**挂的 —— 摘别人的会毁掉另一路的告警日志。
            if getattr(handler, "_sentinel_owner", None) == id(self):
                self.logger.removeHandler(handler)
                handler.close()
        if self.notifier is not None:
            try:
                self.notifier.close()
            except Exception as exc:  # noqa: BLE001 —— 释放失败不影响退出
                logger.warning("关闭通知通道失败：%s: %s", type(exc).__name__, exc)
        # ⚠️ 看门狗**不**在这里停 ——
        # 它是进程内共享的（同地址只起一个），
        # 停它得由进程收尾时统一做（见 engine_watchdog.stop_engine_watchdog）。

    def __enter__(self) -> "SentinelEngine":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()


# ---------------------------------------------------------------------------
# 命令行入口
# ---------------------------------------------------------------------------
def _print_guardian_event(event: Dict[str, Any]) -> None:
    """把守护者事件打印到终端。

    终端呈现属于 CLI 层的职责 —— 守护者只负责**产生**事件，
    怎么显示是调用方的事。这样 GUI 就能用同一个回调做自己的呈现。
    """
    message = str(event.get("message") or "")
    if not message:
        return
    print(_console_safe(message))


def _print_tail_banner(
    log_file: str,
    intent_tracker: Optional[IntentTracker],
) -> None:
    """打印实时监听的开场提示。"""
    print(_console_safe(f"哨兵实时监听中：{log_file}"))
    print(_console_safe("  · 新写入的内容会被立刻审查（Ctrl+C 退出）"))
    if intent_tracker is not None:
        print(_console_safe(f"  · 意图漂移检测已开启：{intent_tracker.state()}"))
    else:
        print(
            _console_safe(
                "  · 意图漂移检测未开启"
                "（用 --task-goal 指定目标，或在配置里填 task_goal）"
            )
        )


def main(argv: Optional[List[str]] = None) -> int:
    """命令行入口：``python -m src.core.engine <日志文件>``。

    :param argv: 参数列表；为 ``None`` 时取 ``sys.argv[1:]``。
    :return: 进程退出码。
    """
    parser = argparse.ArgumentParser(
        prog="sentinel",
        description="哨兵 Sentinel —— 读取 Agent 流式日志并做 L1 规则审查",
    )
    parser.add_argument("log_file", help="要审查的日志文件（JSONL）")
    parser.add_argument(
        "--config", default=None, help="配置文件路径，默认 config/config.yaml"
    )
    parser.add_argument(
        "--alert-log",
        default=DEFAULT_ALERT_LOG,
        help="告警日志路径，默认 logs/sentinel_alert.log",
    )
    parser.add_argument(
        "--quiet", action="store_true", help="不在终端打印告警"
    )
    parser.add_argument(
        "--agent",
        default="",
        help="被监控的 Agent 名称（如 codex）；会写进告警与远程通知",
    )
    parser.add_argument(
        "--no-llm",
        action="store_true",
        help="不调用本地模型，只输出规则层原始判定",
    )
    parser.add_argument(
        "--no-context",
        action="store_true",
        help="关闭上下文模式检测（STUCK / LEAK）",
    )
    parser.add_argument(
        "--no-notify",
        action="store_true",
        help="关闭桌面 / 声音 / Webhook 通知通道",
    )
    parser.add_argument(
        "--guardian",
        action="store_true",
        help="启用守护者模式（覆盖配置里的 intervention_mode）",
    )
    parser.add_argument(
        "--no-guardian",
        action="store_true",
        help="关闭守护者模式（即使配置里开着）",
    )
    parser.add_argument(
        "--target-pid",
        type=int,
        default=None,
        help="被监控进程的 PID；不给则尝试自动探测",
    )
    parser.add_argument(
        "--tail",
        action="store_true",
        help=(
            "实时流式监听：不读完就退出，持续阻塞跟随文件变化，"
            "新事件到达即刻审查（Ctrl+C 退出）"
        ),
    )
    parser.add_argument(
        "--task-goal",
        default=None,
        help=(
            "本次任务的原始目标（意图锚点）；"
            "给了才启用意图漂移检测。默认取配置里的 task_goal"
        ),
    )
    parser.add_argument(
        "--no-drift",
        action="store_true",
        help="关闭意图漂移检测（即使配置里开着）",
    )
    args = parser.parse_args(argv)

    _prepare_stdout()

    from ..adapters.mcode import McodeAdapter
    from ..config_loader import load_config
    from ..llm.factory import build_routing_provider

    config = load_config(args.config)
    rule_engine = RuleEngine(config)

    llm_client = None
    if not args.no_llm:
        try:
            llm_client = build_routing_provider(config)
        except ValueError as exc:  # noqa: BLE001 —— 配置问题只降级
            print(f"模型层配置有误（{exc}），只输出规则层判定。")
        else:
            if not llm_client.enabled:
                print(
                    "模型层已关闭（config: llm.enabled=false），"
                    "只输出规则层判定。"
                )

    context_tracker = None
    if not args.no_context:
        context_config = config.get("context") or {}
        guardian_config = config.get("guardian") or {}
        context_tracker = ContextTracker(
            window=int(context_config.get("window", 5)),
            stuck_threshold=int(context_config.get("stuck_threshold", 3)),
            enabled=bool(context_config.get("enabled", True)),
            internal_hosts=guardian_config.get("internal_hosts"),
        )

    intent_tracker = None
    if not args.no_drift:
        drift_config = config.get("drift") or {}
        goal = args.task_goal
        if goal is None:
            goal = str(config.get("task_goal") or "")
        if llm_client is not None and goal:
            intent_tracker = IntentTracker(
                goal=goal,
                window=int(drift_config.get("window", 5)),
                threshold=int(drift_config.get("threshold", 2)),
                enabled=bool(drift_config.get("enabled", True)),
                evaluator=llm_client.evaluate_drift,
            )
            if args.task_goal:
                # 命令行显式指定的目标 —— **不许**被自动锚定覆盖。
                intent_tracker.set_goal(goal, explicit=True)

    notifier = None
    if not args.no_notify:
        try:
            from .notifier import Notifier

            notifier = Notifier(config)
        except Exception as exc:  # noqa: BLE001 —— 通知层不可用不应阻断审查
            print(f"通知通道不可用（{type(exc).__name__}: {exc}），仅输出到终端与日志。")

    # --- v3.5.0：级联审核（L2 本地过滤 → L3 云端终审） ---
    cascade = None
    if not args.no_llm:
        try:
            from ..llm.factory import build_cascade_provider

            cascade = build_cascade_provider(config, goal=goal or "")
        except Exception as exc:  # noqa: BLE001 —— 配错只降级
            print(
                f"级联审核装配失败（{type(exc).__name__}: {exc}），"
                "仍按规则层判定。"
            )
            cascade = None

    guardian = None
    if not args.no_guardian:
        try:
            from .guardian import Guardian

            guardian = Guardian(
                config,
                target_pid=args.target_pid,
                on_event=_print_guardian_event,
            )
            if args.guardian:
                guardian.force_enable()
            if guardian.enabled:
                guardian.watch_stdin()
        except Exception as exc:  # noqa: BLE001 —— 守护者不可用不应阻断审查
            print(f"守护者不可用（{type(exc).__name__}: {exc}），仅观察不干预。")

    engine = SentinelEngine(
        McodeAdapter(),
        rule_engine,
        alert_log_path=args.alert_log,
        console=not args.quiet,
        llm_client=llm_client,
        context_tracker=context_tracker,
        intent_tracker=intent_tracker,
        notifier=notifier,
        guardian=guardian,
        agent_name=str(args.agent or ""),
    )
    # --- v3.5.0：记忆参数与级联 ---
    memory_config = config.get("memory") or {}
    engine.memory_steps = int(memory_config.get("short_term_steps", 4))
    engine.memory_summary_threshold = int(
        memory_config.get("long_term_threshold", 10)
    )
    engine.set_cascade(cascade)
    try:
        if args.tail:
            _print_tail_banner(args.log_file, intent_tracker)
            try:
                summary = engine.run(args.log_file, tail=True)
            except KeyboardInterrupt:
                print("\n收到 Ctrl+C，停止监听。")
                summary = {
                    "total_events": 0,
                    "counts": {},
                    "alerts": [],
                }
        else:
            summary = engine.run(args.log_file)
    finally:
        if guardian is not None:
            try:
                guardian.close()
            except Exception as exc:  # noqa: BLE001 —— 释放失败不影响退出
                logger.warning("关闭守护者失败：%s: %s", type(exc).__name__, exc)
        engine.close()

    if args.tail:
        print(
            _console_safe(
                "监听结束：共处理 {total} 个事件，"
                "DANGEROUS={danger}，LOOP={loop}，漂移告警={drift}".format(
                    total=summary.get("total_events", 0),
                    danger=summary.get("counts", {}).get(
                        VERDICT_DANGEROUS, 0
                    ),
                    loop=summary.get("counts", {}).get(VERDICT_LOOP, 0),
                    drift=sum(
                        1
                        for item in summary.get("alerts", [])
                        if item.get("verdict") == VERDICT_DRIFT
                    ),
                )
            )
        )
    else:
        print(
            _console_safe(
                "扫描完成：共 {total} 个事件，"
                "ON_TRACK={ok}，DANGEROUS={danger}，LOOP={loop}".format(
                    total=summary["total_events"],
                    ok=summary["counts"].get(VERDICT_ON_TRACK, 0),
                    danger=summary["counts"].get(VERDICT_DANGEROUS, 0),
                    loop=summary["counts"].get(VERDICT_LOOP, 0),
                )
            )
        )
    return 1 if summary["alerts"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
