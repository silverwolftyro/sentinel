"""微观统计学检测器 —— 把「微观频次」从 L2 的负担里摘出来。

为什么要有这个
==============

L2（大模型）擅长的是**语义**判断：「这个删除操作该不该做」。
它不擅长、也**不该**擅长**数数** —— 「这个文件你今天读了几遍？」
「这条命令你重跑了几次？」「这次编辑到底改没改？」

但这几件事恰恰是 L2 最容易误判的地方：

* 第 6 次读同一个文件，L2 看着像「在确认上下文」，于是放过；
* 同一条 ``pytest`` 跑了 4 遍，L2 觉得「重跑一次很正常」；
* 一次「只改了个空格」的编辑，L2 判成「有实质改动」。

这些判断的**正确答案**是死的 —— 它们是**计数**问题，不是语义问题。
让 L2 去数数，等于把一把精确的尺子换成一把肉眼。

所以本模块**在模型之前**把这些数清楚：

===================== ===========================================
判定                  触发条件
===================== ===========================================
``STATS_LOOP``        10 分钟内同一文件被读 / 改**超过 5 次**；
                      或同一条命令被执行**超过 3 次**
``STATS_NOOP``        编辑前后内容**完全没变**，或**只差空白字符**
``STATS_STALL``       连续两次思考 / 工具调用间隔**超过 15 分钟**
===================== ===========================================

三条硬约束
==========

1. **绝不调用任何模型** —— 本模块只做算术与哈希；
2. **绝不抛异常** —— 统计器挂了不该拖垮审查主链路（红线 R5）；
3. **绝不猜** —— 拿不到「修改前 / 修改后」的文本就**不下结论**，
   宁可漏报也**不误报**（误报会让用户对整套告警脱敏）。

与既有检测器的分工
==================

============================ ==========================================
检测器                      管的**是什么**
============================ ==========================================
``LoopDetector``（L1 规则）  **连续**读同一个文件（中间不能有别的动作）
``TimeBoxDetector``          同一件事**磨太久**（要够久才报警）
**本模块**                   **窗口内的频次**（不管连不连续、够不够久）
============================ ==========================================

三者是**正交**的：连续 3 次读同一文件（老检测器命中，但只读 3 次不算
死循环）、隔 20 分钟读 6 次（本模块命中）、一条命令磨 40 分钟
（时间盒命中）。谁也不替代谁。

看板呈现
========

报警以「**橘黄色警告**」写进看板的**实时审核流水** ——
复用 ``ONTRACK_JSON`` 这条日志格式契约（看板据此渲染颜色），
并带上 ``audit_text``，让这一行在**任何档位**都逐条可见
（看板把带 ``audit_text`` 的条目当作「系统提示」，不参与压缩）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_SAME_COMMAND_THRESHOLD",
    "DEFAULT_SAME_FILE_THRESHOLD",
    "DEFAULT_STALL_MINUTES",
    "DEFAULT_WINDOW_MINUTES",
    "MICRO_STATS_LEVEL",
    "ONTRACK_MARKER",
    "TAG_INVALID_EDIT",
    "TAG_STALL",
    "TAG_STATS_LOOP",
    "VERDICT_STATS_LOOP",
    "VERDICT_STATS_NOOP",
    "VERDICT_STATS_STALL",
    "MicroStatFinding",
    "MicroStatsDetector",
    "MicroStatsReporter",
    "build_micro_stats",
    "finding_to_verdict",
]

# ---------------------------------------------------------------------------
# 判定常量
# ---------------------------------------------------------------------------

#: 高频重复（疑似死循环）判定名。
VERDICT_STATS_LOOP = "STATS_LOOP"

#: 无效编辑判定名。
VERDICT_STATS_NOOP = "STATS_NOOP"

#: 疑似卡死判定名。
VERDICT_STATS_STALL = "STATS_STALL"

#: 高频重复的展示标签。
TAG_STATS_LOOP = "[STATS_LOOP]"

#: 无效编辑的展示标签。
TAG_INVALID_EDIT = "[无效编辑]"

#: 疑似卡死的展示标签。
TAG_STALL = "[疑似卡死]"

#: 微观统计告警的等级 —— 全部是「橘黄」（看板流水区）。
MICRO_STATS_LEVEL = "warn"

#: 「实时审核流水」行的标记（与看板各持一份常量 —— 日志格式契约）。
#:
#: ⚠️ 本模块**复用**引擎写流水的那条契约，而不是另立一套 ——
#:    另立一套就得改看板与后端，而任务要求「不修改核心」。
#:    看板对「带 ``audit_text`` 的条目」有专门待遇：
#:    任何档位都**逐条显示**，不参与极限档的计数行合并。
ONTRACK_MARKER = "ONTRACK_JSON"

# ---------------------------------------------------------------------------
# 默认参数
# ---------------------------------------------------------------------------

#: 默认统计窗口（分钟）。
DEFAULT_WINDOW_MINUTES = 10.0

#: 同一文件的读 / 改次数**超过**该值即判定为高频重复。
DEFAULT_SAME_FILE_THRESHOLD = 5

#: 同一条命令的执行次数**超过**该值即判定为高频重复。
DEFAULT_SAME_COMMAND_THRESHOLD = 3

#: 连续两次思考 / 工具调用的间隔**超过**该值即判定为疑似卡死（分钟）。
DEFAULT_STALL_MINUTES = 15.0

#: 单个文件最多缓存多少字节的正文（超过就只留摘要哈希）。
MAX_TRACKED_CONTENT_BYTES = 256 * 1024

#: 最多同时跟踪多少个文件的指纹。
MAX_TRACKED_FILES = 64

#: 告警文案的最大字符数（看板流水区一行放不下太长的话）。
MAX_REASON_LENGTH = 160

# ---------------------------------------------------------------------------
# 工具名分类
# ---------------------------------------------------------------------------

#: 读类工具名（精确匹配，转小写）。
_READ_TOOL_NAMES = frozenset({
    "read",
    "read_file",
    "readfile",
    "cat",
    "view",
    "view_file",
    "open",
    "open_file",
    "get_file",
    "notebookread",
    "notebook_read",
    "readnotebook",
})

#: 编辑类工具名（精确匹配，转小写）。
_EDIT_TOOL_NAMES = frozenset({
    "edit",
    "edit_file",
    "editfile",
    "multiedit",
    "multi_edit",
    "str_replace_editor",
    "str_replace_based_edit_tool",
    "apply_patch",
    "applypatch",
    "patch",
    "replace",
    "replace_in_file",
    "search_replace",
    "update_file",
})

#: 写类工具名（整文件覆盖，精确匹配）。
_WRITE_TOOL_NAMES = frozenset({
    "write",
    "write_file",
    "writefile",
    "create_file",
    "createfile",
})

#: 命令执行类工具名（精确匹配）。
_COMMAND_TOOL_NAMES = frozenset({
    "bash",
    "shell",
    "sh",
    "zsh",
    "exec",
    "execute",
    "run",
    "run_command",
    "runcommand",
    "terminal",
    "cmd",
    "command",
    "powershell",
    "pwsh",
    "wsl",
})

#: 可能承载「目标文件路径」的参数名（按优先级排列）。
_PATH_KEYS: Tuple[str, ...] = (
    "file_path",
    "filepath",
    "path",
    "filename",
    "file",
    "abs_path",
    "absolute_path",
    "target_file",
    "notebook_path",
)

#: 可能承载「命令文本」的参数名（按优先级排列）。
_COMMAND_KEYS: Tuple[str, ...] = (
    "command",
    "cmd",
    "commandLine",
    "command_line",
    "shell",
    "script",
)

#: 可能承载「编辑前文本 / 编辑后文本」的参数名对（按优先级排列）。
_EDIT_TEXT_PAIRS: Tuple[Tuple[str, str], ...] = (
    ("old_string", "new_string"),
    ("old_text", "new_text"),
    ("old_content", "new_content"),
    ("before", "after"),
    ("original", "replacement"),
)

#: 可能承载「整份新内容」的参数名（写类工具）。
_FULL_CONTENT_KEYS: Tuple[str, ...] = (
    "content",
    "new_content",
    "file_content",
    "contents",
)

_WHITESPACE_RE = re.compile(r"\s+")


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def _classify_tool(name: str) -> str:
    """把工具名分类成 ``read`` / ``edit`` / ``write`` / ``command`` / ``other``。

    先按**精确名单**判，再退回子串兜底 —— 纯子串匹配会把
    ``spreadsheet`` 里的 ``read`` 也当成读操作。

    :param name: 工具名。
    :return: 类别名。
    """
    key = str(name or "").strip().lower()
    if not key:
        return "other"
    if key in _READ_TOOL_NAMES:
        return "read"
    if key in _EDIT_TOOL_NAMES:
        return "edit"
    if key in _WRITE_TOOL_NAMES:
        return "write"
    if key in _COMMAND_TOOL_NAMES:
        return "command"
    # 兜底：子串匹配（覆盖 ``my_edit_tool`` 这类自定义名）。
    if "edit" in key or "patch" in key or "replace" in key:
        return "edit"
    if "read" in key and "spread" not in key:
        return "read"
    if "write" in key:
        return "write"
    if "bash" in key or "shell" in key or "exec" in key:
        return "command"
    return "other"


def _extract_path(arguments: Dict[str, Any]) -> str:
    """从工具参数里取出目标文件路径。

    :param arguments: 工具参数。
    :return: 路径；取不到返回空串。
    """
    for key in _PATH_KEYS:
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _extract_command(
    tool: Dict[str, Any],
    arguments: Dict[str, Any],
) -> str:
    """从工具里取出命令文本。

    适配器已经把命令**提取**到 ``tool["command"]`` 了，
    这里是最后一道兜底 —— 直接从参数里翻。

    :param tool: 工具字典。
    :param arguments: 工具参数。
    :return: 命令文本；取不到返回空串。
    """
    command = str(tool.get("command") or "").strip()
    if command:
        return command
    for key in _COMMAND_KEYS:
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, list):
            parts = [str(item) for item in value if isinstance(item, str)]
            if parts:
                return "\n".join(parts).strip()
    return ""


def _extract_edit_pair(
    arguments: Dict[str, Any],
) -> Tuple[Optional[str], Optional[str]]:
    """取出「编辑前文本」与「编辑后文本」。

    :param arguments: 工具参数。
    :return: ``(前, 后)``；任一取不到即为 ``None``（**不猜**）。
    """
    for old_key, new_key in _EDIT_TEXT_PAIRS:
        old_value = arguments.get(old_key)
        new_value = arguments.get(new_key)
        if isinstance(old_value, str) and isinstance(new_value, str):
            return old_value, new_value
    return None, None


def _extract_full_content(arguments: Dict[str, Any]) -> Optional[str]:
    """取出「整份新内容」（写类工具）。

    :param arguments: 工具参数。
    :return: 正文；取不到返回 ``None``。
    """
    for key in _FULL_CONTENT_KEYS:
        value = arguments.get(key)
        if isinstance(value, str):
            return value
    return None


def _extract_call_id(event: Dict[str, Any]) -> str:
    """取出事件的调用 ID。

    调用 ID 用来把「回执」和「调用」配上对 ——
    没有它就退回**先进先出**（Agent 一次只干一件事，够用了）。

    :param event: 统一事件字典。
    :return: 调用 ID；取不到返回空串。
    """
    for key in ("tool_use_id", "toolUseId", "call_id", "callId"):
        value = event.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    raw = event.get("raw")
    if isinstance(raw, dict):
        for key in ("tool_use_id", "toolUseId", "call_id", "callId"):
            value = raw.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def _digest(text: str) -> str:
    """算一段文本的 SHA-256 摘要。

    :param text: 文本。
    :return: 十六进制摘要。
    """
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def _strip_whitespace(text: str) -> str:
    """去掉**全部**空白字符（空格 / 制表 / 换行）。

    :param text: 文本。
    :return: 去空白后的文本。
    """
    return _WHITESPACE_RE.sub("", str(text or ""))


def _normalize_path(path: str) -> str:
    """把路径归一化成比较键。

    与 :meth:`src.rules.basic.LoopDetector._normalize` 同一口径：
    统一分隔符、去空白、转小写。转小写是因为哨兵主要跑在 Windows 上，
    同一文件的路径大小写可能不一致。

    :param path: 原始路径。
    :return: 归一化后的路径。
    """
    return (
        str(path or "")
        .strip()
        .replace("\\", "/")
        .rstrip("/")
        .lower()
    )


def _normalize_command(command: str) -> str:
    """把命令归一化成比较键。

    统一小写、压缩空白 —— ``pytest  -q`` 与 ``pytest -q``
    应当算同一条命令。

    :param command: 原始命令。
    :return: 归一化后的命令。
    """
    return re.sub(r"\s+", " ", str(command or "").strip().lower())


def _clip(text: str, limit: int = MAX_REASON_LENGTH) -> str:
    """截断文案。

    :param text: 原文。
    :param limit: 最大字符数。
    :return: 截断后的文本。
    """
    value = str(text or "")
    if len(value) <= limit:
        return value
    return value[: max(limit - 1, 1)] + "…"


# ---------------------------------------------------------------------------
# 结论
# ---------------------------------------------------------------------------

@dataclass
class MicroStatFinding:
    """一条微观统计结论。

    :param verdict: 判定名（``STATS_LOOP`` / ``STATS_NOOP`` / ``STATS_STALL``）。
    :param tag: 展示标签（如 ``[无效编辑]``）。
    :param reason: 人类可读的完整文案（**以标签开头**）。
    :param rule_id: 规则 ID。
    :param kind: 细分类别（``file_repeat`` / ``command_repeat`` / …）。
    :param target: 判定对象（文件路径或命令）。
    :param level: 告警等级（恒为 ``warn`` —— 橘黄）。
    :param severity: 严重程度。
    :param confidence: 置信度。
    :param detail: 补充证据（次数、窗口、字符数……）。
    """

    verdict: str
    tag: str
    reason: str
    rule_id: str = ""
    kind: str = ""
    target: str = ""
    level: str = MICRO_STATS_LEVEL
    severity: str = "standard"
    confidence: float = 0.9
    detail: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """转成字典（给看板 / 日志 / L1 用）。

        :return: 结论字典。
        """
        return {
            "verdict": self.verdict,
            "tag": self.tag,
            "reason": self.reason,
            "rule_id": self.rule_id,
            "kind": self.kind,
            "target": self.target,
            "level": self.level,
            "severity": self.severity,
            "confidence": self.confidence,
            "detail": dict(self.detail),
        }


def finding_to_verdict(finding: MicroStatFinding) -> Dict[str, Any]:
    """把结论转成引擎认识的**判定字典**。

    形状与 :meth:`src.core.timebox.TimeBoxDetector.observe` 的返回值一致，
    这样它能直接塞进 ``engine.process_event`` 的判定通道。

    :param finding: 微观统计结论。
    :return: 判定字典。
    """
    return {
        "verdict": finding.verdict,
        "reason": finding.reason,
        "rule_id": finding.rule_id,
        "evidence": str(finding.detail.get("evidence") or ""),
        "level": finding.level,
        "confidence": finding.confidence,
        "severity": finding.severity,
        "category": finding.kind,
    }


# ---------------------------------------------------------------------------
# 内部结构
# ---------------------------------------------------------------------------

@dataclass
class _Bucket:
    """一个滑动窗口桶。

    :param key: 桶的键。
    :param label: 给人看的标签（原始路径 / 命令）。
    :param hits: 窗口内的命中记录（时间戳 + 类别）。
    :param fired: 已经报过警（同一波不重复刷屏）。
    """

    key: str
    label: str
    hits: Deque[Tuple[float, str]] = field(default_factory=deque)
    fired: bool = False


@dataclass
class _FileState:
    """一个文件的内容指纹。

    :param digest: 正文摘要。
    :param size: 正文字符数。
    :param content: 正文（超长时为 ``None`` —— 只留摘要）。
    """

    digest: str
    size: int
    content: Optional[str] = None


@dataclass
class _PendingCall:
    """一次还没回来的工具调用。

    :param call_id: 调用 ID（没有就空串）。
    :param kind: 工具类别（``read`` / ``edit`` / ``write`` / …）。
    :param path: 目标文件路径（没有就空串）。
    """

    call_id: str
    kind: str
    path: str = ""


# ---------------------------------------------------------------------------
# 检测器
# ---------------------------------------------------------------------------

class MicroStatsDetector:
    """微观统计学检测器 —— 纯统计，**不碰任何模型**。

    :param window_minutes: 统计窗口（分钟）。
    :param same_file_threshold: 同一文件的读 / 改次数**超过**该值即报警。
    :param same_command_threshold: 同一条命令的执行次数**超过**该值即报警。
    :param stall_minutes: 连续两次思考 / 工具调用的间隔**超过**该值即报警。
    :param enabled: 是否启用。
    :param clock: 时钟（测试用）。
    """

    def __init__(
        self,
        *,
        window_minutes: float = DEFAULT_WINDOW_MINUTES,
        same_file_threshold: int = DEFAULT_SAME_FILE_THRESHOLD,
        same_command_threshold: int = DEFAULT_SAME_COMMAND_THRESHOLD,
        stall_minutes: float = DEFAULT_STALL_MINUTES,
        enabled: bool = True,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.window_minutes = max(float(window_minutes or 0), 0.01)
        self.same_file_threshold = max(int(same_file_threshold or 0), 0)
        self.same_command_threshold = max(
            int(same_command_threshold or 0), 0
        )
        self.stall_minutes = max(float(stall_minutes or 0), 0.0)
        self.enabled = bool(enabled)
        self._clock = clock
        #: 文件频次桶。
        self._file_buckets: Dict[str, _Bucket] = {}
        #: 命令频次桶。
        self._command_buckets: Dict[str, _Bucket] = {}
        #: 文件内容指纹（LRU）。
        self._file_state: "OrderedDict[str, _FileState]" = OrderedDict()
        #: 还没回来的工具调用（按先后排队，等回执认领）。
        self._pending_calls: Deque[_PendingCall] = deque()
        #: 上一次思考 / 工具调用的时间戳。
        self._last_activity_ts: float = 0.0
        #: 上一次思考 / 工具调用的类型。
        self._last_activity_kind: str = ""

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------
    @property
    def window_seconds(self) -> float:
        """统计窗口（秒）。"""
        return self.window_minutes * 60.0

    @property
    def stall_seconds(self) -> float:
        """疑似卡死的间隔阈值（秒）。"""
        return self.stall_minutes * 60.0

    def observe(self, event: Dict[str, Any]) -> List[MicroStatFinding]:
        """喂入一个事件，返回本次事件命中的**全部**结论。

        ⚠️ 绝不让异常冒出去 —— 统计器是「锦上添花」，
        它的失败不该影响审查主链路（红线 R5）。

        :param event: 统一事件字典。
        :return: 结论列表（没有命中就是空列表）。
        """
        if not self.enabled or not isinstance(event, dict):
            return []
        try:
            return self._observe(event)
        except Exception as exc:  # noqa: BLE001 —— 统计失败不影响审查
            logger.warning(
                "微观统计失败，已跳过：%s: %s", type(exc).__name__, exc
            )
            return []

    def observe_verdict(
        self,
        event: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """喂入一个事件，返回**第一条**结论的判定字典。

        给「只要一个结论」的调用方用（比如引擎的单步入口）。

        :param event: 统一事件字典。
        :return: 判定字典；没有命中返回 ``None``。
        """
        findings = self.observe(event)
        if not findings:
            return None
        return finding_to_verdict(findings[0])

    def reset(self) -> None:
        """清空全部状态（会话结束 / 重新锚定目标时用）。"""
        self._file_buckets.clear()
        self._command_buckets.clear()
        self._file_state.clear()
        self._pending_calls.clear()
        self._last_activity_ts = 0.0
        self._last_activity_kind = ""

    def state(self) -> Dict[str, Any]:
        """返回当前状态的快照（给看板 / 调试用）。

        :return: 快照字典。
        """
        return {
            "enabled": self.enabled,
            "window_minutes": self.window_minutes,
            "same_file_threshold": self.same_file_threshold,
            "same_command_threshold": self.same_command_threshold,
            "stall_minutes": self.stall_minutes,
            "files": [
                {
                    "path": bucket.label,
                    "count": len(bucket.hits),
                    "fired": bucket.fired,
                }
                for bucket in self._file_buckets.values()
            ],
            "commands": [
                {
                    "command": bucket.label,
                    "count": len(bucket.hits),
                    "fired": bucket.fired,
                }
                for bucket in self._command_buckets.values()
            ],
            "tracked_files": len(self._file_state),
            "pending_calls": len(self._pending_calls),
            "last_activity_ts": self._last_activity_ts,
            "last_activity_kind": self._last_activity_kind,
        }

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------
    def _observe(self, event: Dict[str, Any]) -> List[MicroStatFinding]:
        """真正的观察逻辑（异常由 :meth:`observe` 兜住）。"""
        findings: List[MicroStatFinding] = []
        event_type = str(event.get("type") or "")
        now = self._resolve_now(event)

        # --- 疑似卡死：只看 thinking / tool_call 的相邻间隔 ---
        if event_type in ("thinking", "tool_call"):
            findings.extend(self._check_stall(now))
            if now > self._last_activity_ts:
                self._last_activity_ts = now
                self._last_activity_kind = event_type

        if event_type == "tool_call":
            findings.extend(self._on_tool_call(event, now))
        elif event_type == "tool_result":
            self._on_tool_result(event)

        return findings

    def _resolve_now(self, event: Dict[str, Any]) -> float:
        """取出这次事件的「当前时间」。

        事件自带的时间戳优先 —— 它是**日志里的真实时刻**；
        没有才退回墙上时钟。

        :param event: 统一事件字典。
        :return: epoch 秒。
        """
        stamp = event.get("timestamp")
        if isinstance(stamp, (int, float)) and not isinstance(stamp, bool):
            value = float(stamp)
            if value > 0:
                return value
        return float(self._clock())

    def _check_stall(self, now: float) -> List[MicroStatFinding]:
        """检查「两次动作之间隔太久」。

        :param now: 当前时间戳。
        :return: 结论列表。
        """
        if not self._last_activity_ts:
            return []
        gap = now - self._last_activity_ts
        if gap <= self.stall_seconds:
            return []
        minutes = gap / 60.0
        previous = (
            "思考" if self._last_activity_kind == "thinking" else "工具调用"
        )
        reason = (
            "[疑似卡死] 距上一次{}隔了 {:.1f} 分钟"
            "（阈值 {:.0f} 分钟）—— Agent 可能卡住或长时间摸鱼"
        ).format(previous, minutes, self.stall_minutes)
        return [
            MicroStatFinding(
                verdict=VERDICT_STATS_STALL,
                tag=TAG_STALL,
                reason=_clip(reason),
                rule_id="micro_stats.stall",
                kind="stall",
                target=previous,
                detail={
                    "gap_seconds": round(gap, 3),
                    "gap_minutes": round(minutes, 2),
                    "threshold_minutes": self.stall_minutes,
                    "previous_kind": self._last_activity_kind,
                    "evidence": reason,
                },
            )
        ]

    def _on_tool_call(
        self,
        event: Dict[str, Any],
        now: float,
    ) -> List[MicroStatFinding]:
        """处理一次工具调用。

        :param event: 统一事件字典。
        :param now: 当前时间戳。
        :return: 结论列表。
        """
        findings: List[MicroStatFinding] = []
        tool = event.get("tool")
        tool = tool if isinstance(tool, dict) else {}
        arguments = tool.get("arguments")
        arguments = arguments if isinstance(arguments, dict) else {}

        name = str(tool.get("name") or "")
        kind = _classify_tool(name)
        path = _extract_path(arguments)
        command = _extract_command(tool, arguments)

        # --- 高频重复：同一文件被反复读 / 改 ---
        if kind in ("read", "edit") and path:
            hit = self._touch_file(path, kind, now)
            if hit is not None:
                findings.append(hit)

        # --- 高频重复：同一条命令被反复执行 ---
        if kind == "command" and command:
            hit = self._touch_command(command, now)
            if hit is not None:
                findings.append(hit)

        # --- 无效编辑：改了等于没改 ---
        if kind in ("edit", "write"):
            noop = self._check_invalid_edit(kind, path, arguments)
            if noop is not None:
                findings.append(noop)

        # --- 记住这次调用，等回执来认领（读回来的正文就是文件内容）---
        self._pending_calls.append(
            _PendingCall(
                call_id=_extract_call_id(event),
                kind=kind,
                path=path,
            )
        )
        while len(self._pending_calls) > 64:
            self._pending_calls.popleft()

        self._purge_stale(now)
        return findings

    def _on_tool_result(self, event: Dict[str, Any]) -> None:
        """处理一次工具回执 —— 认领「读回来的正文」。

        ⚠️ 回执与调用是**按顺序**配对的（Agent 一次只干一件事），
        所以这里按先进先出认领；只有认领到**读类**调用时
        才把回执正文当成文件内容 —— 绝不把命令输出当成文件。

        :param event: 统一事件字典。
        """
        if event.get("is_error"):
            return
        text = event.get("text")
        if not isinstance(text, str) or not text:
            return
        pending = self._claim_pending(event)
        if pending is None or pending.kind != "read":
            return
        if not pending.path:
            return
        self._remember_content(pending.path, text)

    def _claim_pending(
        self,
        event: Dict[str, Any],
    ) -> Optional[_PendingCall]:
        """认领一次工具回执对应的调用。

        :param event: 统一事件字典。
        :return: 对应的调用；认领不到返回 ``None``。
        """
        call_id = _extract_call_id(event)
        if call_id:
            for pending in self._pending_calls:
                if pending.call_id and pending.call_id == call_id:
                    self._pending_calls.remove(pending)
                    return pending
        if not self._pending_calls:
            return None
        return self._pending_calls.popleft()

    def _touch_file(
        self,
        path: str,
        kind: str,
        now: float,
    ) -> Optional[MicroStatFinding]:
        """把一次「读 / 改某文件」记进窗口，必要时报警。

        :param path: 文件路径。
        :param kind: 动作类别（``read`` / ``edit``）。
        :param now: 当前时间戳。
        :return: 命中的结论；没命中返回 ``None``。
        """
        key = _normalize_path(path)
        if not key:
            return None
        bucket = self._file_buckets.get(key)
        if bucket is None:
            bucket = _Bucket(key=key, label=path)
            self._file_buckets[key] = bucket
        bucket.hits.append((now, kind))
        self._evict(bucket, now)

        count = len(bucket.hits)
        if count <= self.same_file_threshold:
            # 掉回阈值以下 → 重新武装（下一波还能报）。
            bucket.fired = False
            return None
        if bucket.fired:
            return None
        bucket.fired = True

        reads = sum(1 for _, kind in bucket.hits if kind == "read")
        edits = len(bucket.hits) - reads
        reason = (
            "[STATS_LOOP] {:.0f} 分钟内对同一个文件发起了 {} 次读 / 改"
            "（读 {} 次、改 {} 次）—— 疑似死循环：{}"
        ).format(
            self.window_minutes,
            count,
            reads,
            edits,
            path,
        )
        return MicroStatFinding(
            verdict=VERDICT_STATS_LOOP,
            tag=TAG_STATS_LOOP,
            reason=_clip(reason),
            rule_id="micro_stats.file_repeat",
            kind="file_repeat",
            target=path,
            detail={
                "count": count,
                "reads": reads,
                "edits": edits,
                "threshold": self.same_file_threshold,
                "window_minutes": self.window_minutes,
                "path": path,
                "evidence": path,
            },
        )

    def _touch_command(
        self,
        command: str,
        now: float,
    ) -> Optional[MicroStatFinding]:
        """把一次「执行某命令」记进窗口，必要时报警。

        :param command: 命令文本。
        :param now: 当前时间戳。
        :return: 命中的结论；没命中返回 ``None``。
        """
        key = _normalize_command(command)
        if not key:
            return None
        bucket = self._command_buckets.get(key)
        if bucket is None:
            bucket = _Bucket(key=key, label=command)
            self._command_buckets[key] = bucket
        bucket.hits.append((now, "command"))
        self._evict(bucket, now)

        count = len(bucket.hits)
        if count <= self.same_command_threshold:
            bucket.fired = False
            return None
        if bucket.fired:
            return None
        bucket.fired = True

        reason = (
            "[STATS_LOOP] {:.0f} 分钟内同一条命令被执行了 {} 次"
            "（阈值 {} 次）—— 疑似死循环：{}"
        ).format(
            self.window_minutes,
            count,
            self.same_command_threshold,
            command,
        )
        return MicroStatFinding(
            verdict=VERDICT_STATS_LOOP,
            tag=TAG_STATS_LOOP,
            reason=_clip(reason),
            rule_id="micro_stats.command_repeat",
            kind="command_repeat",
            target=command,
            detail={
                "count": count,
                "threshold": self.same_command_threshold,
                "window_minutes": self.window_minutes,
                "command": command,
                "evidence": command,
            },
        )

    def _evict(self, bucket: _Bucket, now: float) -> None:
        """把窗口外的记录挤出桶。

        :param bucket: 桶。
        :param now: 当前时间戳。
        """
        cutoff = now - self.window_seconds
        while bucket.hits and bucket.hits[0][0] < cutoff:
            bucket.hits.popleft()

    def _purge_stale(self, now: float) -> None:
        """清掉**很久没碰过**的桶（防止字典无限增长）。

        :param now: 当前时间戳。
        """
        cutoff = now - self.window_seconds
        for buckets in (self._file_buckets, self._command_buckets):
            stale = [
                key
                for key, bucket in buckets.items()
                if not bucket.hits or bucket.hits[-1][0] < cutoff
            ]
            for key in stale:
                buckets.pop(key, None)

    def _check_invalid_edit(
        self,
        kind: str,
        path: str,
        arguments: Dict[str, Any],
    ) -> Optional[MicroStatFinding]:
        """判断这次编辑是不是「改了等于没改」。

        两条判据：

        1. **自带前后文本** —— 直接比哈希（最准）；
        2. **只带整份新内容** —— 与上一次观察到的文件指纹比。

        两条都拿不到证据就**不下结论**（宁可漏报，不误报）。

        :param kind: 工具类别（``edit`` / ``write``）。
        :param path: 目标文件路径。
        :param arguments: 工具参数。
        :return: 命中的结论；没命中返回 ``None``。
        """
        old_text, new_text = _extract_edit_pair(arguments)
        if old_text is not None and new_text is not None:
            change = _classify_text_change(old_text, new_text)
            if change:
                return self._build_noop_finding(
                    change,
                    path,
                    old_text,
                    new_text,
                    source="diff",
                )

        # 指纹兜底：整份新内容与上次看到的**完全一样**。
        full_content = _extract_full_content(arguments)
        if full_content is not None and path:
            state = self._file_state.get(_normalize_path(path))
            if state is not None and state.digest == _digest(full_content):
                return self._build_noop_finding(
                    "identical",
                    path,
                    "",
                    full_content,
                    source="fingerprint",
                )
        return None

    def _remember_content(self, path: str, text: str) -> None:
        """记住一个文件的内容指纹。

        :param path: 文件路径。
        :param text: 正文。
        """
        key = _normalize_path(path)
        if not key:
            return
        encoded = text.encode("utf-8", errors="replace")
        content: Optional[str] = text
        if len(encoded) > MAX_TRACKED_CONTENT_BYTES:
            content = None
        self._file_state[key] = _FileState(
            digest=_digest(text),
            size=len(text),
            content=content,
        )
        self._file_state.move_to_end(key)
        while len(self._file_state) > MAX_TRACKED_FILES:
            self._file_state.popitem(last=False)

    @staticmethod
    def _build_noop_finding(
        change: str,
        path: str,
        old_text: str,
        new_text: str,
        *,
        source: str,
    ) -> MicroStatFinding:
        """造一条「无效编辑」结论。

        :param change: 变化类型（``identical`` / ``whitespace_only``）。
        :param path: 目标文件路径。
        :param old_text: 编辑前文本。
        :param new_text: 编辑后文本。
        :param source: 判据来源（``diff`` / ``fingerprint``）。
        :return: 结论。
        """
        wording = (
            "完全没变" if change == "identical" else "只差空格 / 换行"
        )
        target = path or "（未指明文件）"
        reason = (
            "[无效编辑] {}：修改前后{}（{} 字符 → {} 字符）"
            "—— 这次编辑没有任何实质变化"
        ).format(target, wording, len(old_text), len(new_text))
        return MicroStatFinding(
            verdict=VERDICT_STATS_NOOP,
            tag=TAG_INVALID_EDIT,
            reason=_clip(reason),
            rule_id="micro_stats.invalid_edit",
            kind="invalid_edit",
            target=target,
            detail={
                "change": change,
                "source": source,
                "path": path,
                "old_length": len(old_text),
                "new_length": len(new_text),
                "old_digest": _digest(old_text),
                "new_digest": _digest(new_text),
                "evidence": reason,
            },
        )


def _classify_text_change(old_text: str, new_text: str) -> str:
    """判断一段「编辑前 → 编辑后」到底变了没有。

    :param old_text: 编辑前文本。
    :param new_text: 编辑后文本。
    :return: ``identical`` / ``whitespace_only`` / 空串（有实质变化）。
    """
    if old_text == new_text:
        return "identical"
    if _strip_whitespace(old_text) == _strip_whitespace(new_text):
        return "whitespace_only"
    return ""


# ---------------------------------------------------------------------------
# 引擎接口
# ---------------------------------------------------------------------------

class MicroStatsReporter:
    """把微观统计接到引擎上（**不改引擎核心**）。

    用法::

        from src.core.micro_stats import MicroStatsReporter

        reporter = MicroStatsReporter()
        reporter.attach(engine)      # 之后 engine.process_event 自带微观统计

    报警以「**橘黄色警告**」写进看板的**实时审核流水** ——
    复用 ``ONTRACK_JSON`` 契约，并带上 ``audit_text``，
    让这一行在任何档位都**逐条可见**（看板把带 ``audit_text`` 的条目
    当作「系统提示」，不参与极限档的计数行合并）。

    :param detector: 检测器；不传就按默认参数造一个。
    :param log: 写日志用的 logger；不传就用模块自己的。
    """

    def __init__(
        self,
        detector: Optional[MicroStatsDetector] = None,
        *,
        log: Optional[logging.Logger] = None,
    ) -> None:
        self.detector = detector or MicroStatsDetector()
        self._log = log or logger

    def attach(self, engine: Any) -> None:
        """把检测器挂到引擎的 ``process_event`` 上。

        ⚠️ 这是**运行期包装**，不改引擎源码 ——
        引擎的职责边界一个字都不动（红线 R1 / R7）。

        :param engine: :class:`~src.core.engine.SentinelEngine` 实例。
        """
        if getattr(engine, "_micro_stats_attached", False):
            return
        original = getattr(engine, "process_event", None)
        if not callable(original):
            logger.warning("引擎没有 process_event，微观统计未挂载")
            return
        reporter = self

        def _process_event(event: Dict[str, Any]) -> Any:
            findings = reporter.detector.observe(event)
            if findings:
                reporter.emit(findings, event, engine=engine)
            return original(event)

        engine.process_event = _process_event
        try:
            engine._micro_stats_attached = True
        except Exception as exc:  # noqa: BLE001 —— 标记失败不影响挂载
            logger.debug(
                "标记引擎已挂载微观统计失败：%s: %s", type(exc).__name__, exc
            )

    def observe(
        self,
        event: Dict[str, Any],
    ) -> List[MicroStatFinding]:
        """喂入一个事件，返回命中的结论。

        :param event: 统一事件字典。
        :return: 结论列表。
        """
        return self.detector.observe(event)

    def emit(
        self,
        findings: List[MicroStatFinding],
        event: Optional[Dict[str, Any]] = None,
        *,
        engine: Any = None,
    ) -> int:
        """把结论写成「橘黄流水行」。

        :param findings: 结论列表。
        :param event: 触发事件（用来带上线程指纹）。
        :param engine: 引擎实例（用它自己的 logger，保证落进告警日志）。
        :return: 写出的行数。
        """
        if not findings:
            return 0
        target = self._log
        if engine is not None:
            engine_log = getattr(engine, "logger", None)
            if engine_log is not None:
                target = engine_log
        written = 0
        fingerprint = ""
        if isinstance(event, dict):
            fingerprint = str(event.get("fingerprint") or "")
        for finding in findings:
            text = "警告：" + finding.reason
            payload = {
                # ⚠️ ``summary`` 必须非空 —— 看板的流水解析器靠它认行。
                "summary": finding.reason,
                "snippet": finding.reason,
                # ⚠️ ``audit_level = "warn"`` → 看板渲染成**橘黄**。
                "audit_level": MICRO_STATS_LEVEL,
                # ⚠️ ``audit_text`` 让这一行成为「系统提示」——
                #    任何档位都逐条显示，不会被极限档合并掉。
                "audit_text": text,
                "timestamp": time.time(),
                "event_type": "micro_stats",
                "fingerprint": fingerprint,
                "micro_stats": finding.to_dict(),
            }
            try:
                target.warning(
                    "%s %s",
                    ONTRACK_MARKER,
                    json.dumps(payload, ensure_ascii=False),
                )
                written += 1
            except Exception as exc:  # noqa: BLE001 —— 写流水不影响审查
                self._log.warning(
                    "写微观统计流水失败：%s: %s", type(exc).__name__, exc
                )
        return written


def build_micro_stats(
    config: Optional[Dict[str, Any]],
) -> Optional[MicroStatsDetector]:
    """按配置造一个微观统计检测器。

    配置形态（``config.yaml`` 的 ``rules.micro_stats``）::

        rules:
          micro_stats:
            enabled: true
            window_minutes: 10        # 统计窗口
            same_file_threshold: 5    # 同一文件超过 5 次即报警
            same_command_threshold: 3 # 同一条命令超过 3 次即报警
            stall_minutes: 15         # 间隔超过 15 分钟即报警

    :param config: 哨兵配置。
    :return: 检测器；配置里明确关掉时返回 ``None``。
    """
    rules = (config or {}).get("rules") or {}
    section = rules.get("micro_stats") or {}
    if not isinstance(section, dict):
        section = {}
    if not section.get("enabled", True):
        return None
    return MicroStatsDetector(
        window_minutes=float(
            section.get("window_minutes", DEFAULT_WINDOW_MINUTES)
        ),
        same_file_threshold=int(
            section.get("same_file_threshold", DEFAULT_SAME_FILE_THRESHOLD)
        ),
        same_command_threshold=int(
            section.get(
                "same_command_threshold", DEFAULT_SAME_COMMAND_THRESHOLD
            )
        ),
        stall_minutes=float(
            section.get("stall_minutes", DEFAULT_STALL_MINUTES)
        ),
        enabled=True,
    )
