"""规则层（L1）—— 确定性检测。

**本模块绝对不调用任何模型**（架构红线：规则层不允许 import requests /
ollama）。所有判定都是「纯正则 + 纯逻辑」：相同输入必然得到相同输出。

本层当前实现三类检测：

============ ====================================================
判定          触发条件
============ ====================================================
``DANGEROUS`` 工具调用中匹配到高危命令（``rm -rf`` / ``sudo`` /
              ``chmod 777`` / ``git push -f`` 等）
``LOOP``      连续 N 次读取同一个文件，疑似简易死循环
============ ====================================================

其中 ``DANGEROUS`` 又分两个**严重度**：

* ``standard`` —— 危险，但通常可恢复（``chmod 777``、``git push -f``）；
* ``red_line`` —— **红线级**：一旦执行就回不来（``rm -rf /``、整盘格式化、
  ``find / -delete``、``curl | sh``）。红线条目是守护者模式
  （:class:`~src.core.guardian.Guardian`）的**唯一触发源**。

其余一律返回 ``ON_TRACK``。

扩展方式
========
* 新增高危命令：往 ``config/config.yaml`` 的 ``rules.dangerous_commands``
  里追加正则即可，**不需要改代码**；
* 新增红线类别：往 ``rules.catastrophic.categories`` 里加类别名，
  或照抄 :class:`CatastrophicDetector` 实现新检测器；
* 新增死循环模式：照抄 :class:`LoopDetector` 的
  ``observe(event) -> Optional[str]`` 契约实现一个新检测器，
  在 :meth:`RuleEngine.check_event` 中调用即可，上层无需改动。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional, Pattern, Tuple

from ..config_loader import load_config
from .registry import (
    ACTION_BLOCK,
    ACTION_LOG,
    BUILTIN_DANGEROUS_RULES,
    KIND_DANGEROUS,
    RuleSelection,
)

logger = logging.getLogger(__name__)

__all__ = [
    "CATEGORY_DISK_WIPE",
    "CATEGORY_LABELS",
    "CATEGORY_MASS_DELETE",
    "CATEGORY_REMOTE_EXEC",
    "CATEGORY_ROOT_DELETE",
    "DEFAULT_DANGEROUS_COMMANDS",
    "SEVERITY_NONE",
    "SEVERITY_RED_LINE",
    "SEVERITY_STANDARD",
    "VERDICT_DANGEROUS",
    "VERDICT_LOOP",
    "VERDICT_ON_TRACK",
    "CatastrophicDetector",
    "LoopDetector",
    "RuleEngine",
]

# ---------------------------------------------------------------------------
# 判定常量
# ---------------------------------------------------------------------------

#: 未发现异常。
VERDICT_ON_TRACK = "ON_TRACK"
#: 命中高危命令。
VERDICT_DANGEROUS = "DANGEROUS"
#: 疑似死循环。
VERDICT_LOOP = "LOOP"

# ---------------------------------------------------------------------------
# 严重度常量
# ---------------------------------------------------------------------------

#: 红线级：一经执行不可逆 —— 守护者模式唯一的触发源。
SEVERITY_RED_LINE = "red_line"
#: 普通级：危险，但通常可恢复。
SEVERITY_STANDARD = "standard"
#: 无异常。
SEVERITY_NONE = "none"


# ---------------------------------------------------------------------------
# 内置高危命令
# ---------------------------------------------------------------------------

#: 内置高危命令正则（配置里的 ``rules.dangerous_commands`` 是在此基础上**增量追加**）。
#:
#: v3.7.0 起这份表改由 :mod:`src.rules.registry` 的
#: ``BUILTIN_DANGEROUS_RULES`` 派生 —— 清单条目额外带稳定 id 与中文名，
#: 设置页要按条开关就靠它。**正则内容与顺序一字未改**，
#: 与 v3.6.0 的字面量表完全一致。
DEFAULT_DANGEROUS_COMMANDS: Tuple[str, ...] = tuple(
    entry.pattern for entry in BUILTIN_DANGEROUS_RULES
)


# ---------------------------------------------------------------------------
# 红线级（catastrophic）
# ---------------------------------------------------------------------------

#: 递归删除根目录 / 家目录 / 整盘。
CATEGORY_ROOT_DELETE = "root_delete"
#: 批量删除大量文件。
CATEGORY_MASS_DELETE = "mass_delete"
#: 整盘抹除或覆写块设备。
CATEGORY_DISK_WIPE = "disk_wipe"
#: 下载远程内容直接执行。
CATEGORY_REMOTE_EXEC = "remote_exec"

#: 各类别的中文说明（用于告警文案）。
CATEGORY_LABELS: Dict[str, str] = {
    CATEGORY_ROOT_DELETE: "递归删除根目录 / 家目录 / 整盘",
    CATEGORY_MASS_DELETE: "批量删除大量文件",
    CATEGORY_DISK_WIPE: "整盘抹除或覆写块设备",
    CATEGORY_REMOTE_EXEC: "下载远程内容直接执行",
}


class CatastrophicDetector:
    """红线级检测器 —— 识别「一旦执行就回不来」的毁灭性操作。

    与普通高危命令的差别
    ====================

    ``DANGEROUS / standard``
        「这条命令有风险」。比如 ``chmod 777``、``git push -f`` ——
        它们通常可恢复，告警即可。

    ``DANGEROUS / red_line``
        「这条命令一旦跑完就不可逆」。比如 ``rm -rf /``、``format c:``、
        ``find / -delete``、``curl … | sh``。**红线是守护者模式的唯一
        触发源** —— 只有红线才会让哨兵去挂起被监控的进程。

    设计取舍
    ========

    * **宁可漏报也不误报**：误报会让守护者挂起用户正在跑的正事，
      那比不拦更糟。因此判定全部基于**可解释的结构化特征**
      （根路径、通配符、块设备、管道执行），而不是「看起来很危险」；
    * **单步判定**：只看一条命令，不依赖历史。跨事件的判定
      （「读了 ``.env`` 紧接着往外发」）属于上下文层，
      见 :class:`~src.core.engine.ContextTracker`；
    * **绝不调用模型**（架构红线）。

    :param enabled: 是否启用红线判定。
    :param categories: 启用的红线类别；``None`` 表示全部启用。
    """

    #: 直接抹盘 / 覆写块设备的命令（正则 → 说明）。
    _DISK_PATTERNS: Tuple[Tuple[str, str], ...] = (
        (r"\bformat\b\s+[a-zA-Z]:", "格式化磁盘分区"),
        (r"\bmkfs(?:\.[a-z0-9]+)?\b", "重建文件系统（抹掉整盘数据）"),
        (r"\bdd\b[^|;&\n]*\bof=/dev/", "直接覆写块设备"),
        (r"\bshred\b[^|;&\n]*/dev/", "粉碎块设备"),
    )

    #: 下载即执行（正则 → 说明）。
    _REMOTE_EXEC_PATTERNS: Tuple[Tuple[str, str], ...] = (
        (r"\b(?:curl|wget)\b[^|;&\n]*\|\s*(?:ba|z|k)?sh\b",
         "下载内容直接管道给 shell 执行"),
        (r"\b(?:curl|wget)\b[^|;&\n]*\|\s*(?:ba|z|k)?python\b",
         "下载内容直接管道给 python 执行"),
    )

    #: 批量删除（正则 → 说明）。
    _MASS_PATTERNS: Tuple[Tuple[str, str], ...] = (
        (r"\bfind\b[^|;&\n]*\s-(?:delete\b|exec\s+(?:rm|del)\b)",
         "用 find 批量删除文件"),
        (r"\bgit\s+clean\b[^|;&\n]*\s-[a-zA-Z]*[fdx][a-zA-Z]*\b",
         "清空 Git 工作区的未跟踪文件"),
    )

    #: 删除类命令名（取路径最后一段后比较）。
    _DELETE_COMMANDS = frozenset({
        "rm", "rmdir", "rd", "del", "erase", "remove-item", "unlink", "shred",
    })

    #: 允许出现在删除命令之前的包装器（``sudo rm -rf /``）。
    _WRAPPERS = frozenset({
        "sudo", "doas", "env", "command", "nohup", "nice", "ionice", "time",
    })

    #: 视为「根目录」的路径（归一化：小写、反斜杠转正斜杠、去引号）。
    _ROOT_TARGETS = frozenset({
        "/", "/*", "//",
        "~", "~/", "~/*",
        "$home", "$home/", "$home/*",
        "${home}", "${home}/", "${home}/*",
        "/etc", "/usr", "/var", "/bin", "/sbin", "/lib", "/lib64",
        "/boot", "/home", "/root", "/opt", "/srv", "/sys", "/proc",
        "/dev", "/data", "/workspace",
    })

    #: 命令分段（按 shell 的命令分隔符切开）。
    _SEGMENT_SPLIT_RE = re.compile(r"(?:&&|\|\||;|\||\n)")

    #: 词法切分（保留引号，便于识别 ``"C:\Program Files"``）。
    _TOKEN_RE = re.compile(r"""("[^"]*"|'[^']*'|\S+)""")

    #: POSIX 风格选项（``-rf`` / ``--recursive``）。
    _POSIX_FLAG_RE = re.compile(r"^--?[A-Za-z][A-Za-z0-9-]*$")

    #: Windows 风格选项（``/s`` / ``/q``）。
    _WIN_FLAG_RE = re.compile(r"^/[A-Za-z]$")

    #: 环境变量赋值前缀（``FOO=bar rm -rf /``）。
    _ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

    #: Windows 盘符根目录（``C:`` / ``C:/`` / ``C:/*``）。
    _DRIVE_ROOT_RE = re.compile(r"^[a-z]:/?$|^[a-z]:/\*$")

    def __init__(
        self,
        enabled: bool = True,
        categories: Optional[List[str]] = None,
    ) -> None:
        self.enabled = bool(enabled)
        if categories is None:
            self.categories = frozenset(CATEGORY_LABELS.keys())
        else:
            known = {
                str(item).strip().lower()
                for item in categories
                if str(item).strip()
            }
            self.categories = frozenset(
                item for item in known if item in CATEGORY_LABELS
            )

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------
    def check(
        self,
        command: str,
        arguments: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        """判断一条命令是否属于红线级。

        :param command: 命令文本。
        :param arguments: 工具参数 —— 命令常常藏在参数里而不是
            ``tool.command`` 字段里。
        :return: 命中时返回判定字典（含 ``rule_id`` / ``category`` /
            ``reason`` / ``evidence``），否则返回 ``None``。
        """
        if not self.enabled:
            return None
        for text in self._candidates(command, arguments):
            hit = self._check_one(text)
            if hit is not None:
                return hit
        return None

    def category_enabled(self, category: str) -> bool:
        """某个红线类别当前是否启用。"""
        return category in self.categories

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------
    @staticmethod
    def _candidates(
        command: str,
        arguments: Optional[Dict[str, Any]],
    ) -> List[str]:
        """收集待检查的候选文本。

        除了命令本身，还要看工具参数 —— 很多 CLI 把命令放在
        ``arguments["command"]`` 而不是 ``tool["command"]`` 里。
        """
        result: List[str] = []
        if command and str(command).strip():
            result.append(str(command))
        if isinstance(arguments, dict):
            for value in arguments.values():
                if isinstance(value, str) and value.strip():
                    result.append(value)
                elif isinstance(value, (list, tuple)):
                    result.extend(
                        item
                        for item in value
                        if isinstance(item, str) and item.strip()
                    )
        return result

    def _check_one(self, text: str) -> Optional[Dict[str, Any]]:
        """检查一段文本。"""
        lowered = text.lower()

        if self.category_enabled(CATEGORY_DISK_WIPE):
            for pattern, label in self._DISK_PATTERNS:
                match = re.search(pattern, lowered)
                if match:
                    return self._hit(
                        CATEGORY_DISK_WIPE, label, match.group(0), pattern
                    )

        if self.category_enabled(CATEGORY_REMOTE_EXEC):
            for pattern, label in self._REMOTE_EXEC_PATTERNS:
                match = re.search(pattern, lowered)
                if match:
                    return self._hit(
                        CATEGORY_REMOTE_EXEC, label, match.group(0), pattern
                    )

        if self.category_enabled(CATEGORY_MASS_DELETE):
            for pattern, label in self._MASS_PATTERNS:
                match = re.search(pattern, lowered)
                if match:
                    return self._hit(
                        CATEGORY_MASS_DELETE, label, match.group(0), pattern
                    )

        return self._check_delete_command(text)

    def _check_delete_command(self, text: str) -> Optional[Dict[str, Any]]:
        """检查删除类命令的目标是否触到红线。

        只在命令**带递归语义**时才继续往下看 —— ``rm file.txt`` 是
        普通删除，``rm -rf dir/`` 才有资格讨论红线。
        """
        for segment in self._SEGMENT_SPLIT_RE.split(text):
            parsed = self._parse_segment(segment)
            if parsed is None:
                continue
            _name, flags, targets = parsed
            if not self._is_recursive(flags):
                continue
            for target in targets:
                kind = self._classify_target(target)
                if kind == "root" and self.category_enabled(
                    CATEGORY_ROOT_DELETE
                ):
                    return self._hit(
                        CATEGORY_ROOT_DELETE,
                        "递归删除根目录 / 家目录 / 整盘",
                        target,
                        None,
                    )
                if kind == "mass" and self.category_enabled(
                    CATEGORY_MASS_DELETE
                ):
                    return self._hit(
                        CATEGORY_MASS_DELETE,
                        "递归删除通配符匹配的目录树",
                        target,
                        None,
                    )
        return None

    @classmethod
    def _parse_segment(
        cls,
        segment: str,
    ) -> Optional[Tuple[str, List[str], List[str]]]:
        """把一段命令解析成 ``(命令名, 选项, 目标)``。

        :return: 段里没有删除类命令时返回 ``None``。
        """
        tokens = cls._TOKEN_RE.findall(segment)
        if not tokens:
            return None

        index = -1
        for position, token in enumerate(tokens[:4]):
            cleaned = token.strip("\"'")
            base = cleaned.replace("\\", "/").rsplit("/", 1)[-1].lower()
            if base in cls._DELETE_COMMANDS:
                index = position
                break
            if base in cls._WRAPPERS:
                continue
            if cls._ENV_ASSIGN_RE.match(cleaned):
                continue
            break
        if index < 0:
            return None

        flags: List[str] = []
        targets: List[str] = []
        for token in tokens[index + 1:]:
            cleaned = token.strip("\"'")
            if not cleaned:
                continue
            if cleaned == "--":
                continue
            if (
                cls._POSIX_FLAG_RE.match(cleaned)
                or cls._WIN_FLAG_RE.match(cleaned)
            ):
                flags.append(cleaned.lower())
            else:
                targets.append(cleaned)
        if not targets:
            return None
        return tokens[index].strip("\"'").lower(), flags, targets

    @staticmethod
    def _is_recursive(flags: List[str]) -> bool:
        """判断选项里有没有递归语义。"""
        for flag in flags:
            if flag == "--recursive":
                return True
            if flag.startswith("--"):
                continue
            if flag.startswith("/"):
                if flag == "/s":
                    return True
                continue
            if "r" in flag[1:].lower():
                return True
        return False

    @staticmethod
    def _is_force(flags: List[str]) -> bool:
        """判断选项里有没有强制语义。"""
        for flag in flags:
            if flag == "--force":
                return True
            if flag.startswith("--"):
                continue
            if flag.startswith("/"):
                if flag == "/f":
                    return True
                continue
            if "f" in flag[1:].lower():
                return True
        return False

    @classmethod
    def _classify_target(cls, target: str) -> Optional[str]:
        """把删除目标分类成 ``root`` / ``mass`` / ``None``。"""
        value = str(target).strip().strip("\"'").replace("\\", "/")
        if not value:
            return None
        lowered = value.lower()
        if cls._DRIVE_ROOT_RE.match(lowered):
            return "root"
        if lowered in cls._ROOT_TARGETS:
            return "root"
        if "*" in value or "?" in value:
            return "mass"
        return None

    @staticmethod
    def _hit(
        category: str,
        label: str,
        evidence: str,
        pattern: Optional[str],
    ) -> Dict[str, Any]:
        """构造红线判定结果。"""
        text = str(evidence or "").strip()
        return {
            "rule_id": f"catastrophic_{category}",
            "category": category,
            "reason": f"红线级命中：{label}（{text}）",
            "evidence": text[:_MAX_EVIDENCE],
            "pattern": pattern,
            "severity": SEVERITY_RED_LINE,
        }


# ---------------------------------------------------------------------------
# 死循环检测器
# ---------------------------------------------------------------------------

#: 读操作工具名特征（工具名包含其中之一即视为「读」）。
DEFAULT_READ_TOOL_HINTS: Tuple[str, ...] = (
    "read",
    "cat",
    "view",
    "open",
    "get-content",
    "getcontent",
    "head",
    "tail",
    "type",
    "inspect",
    "glob",
    "grep",
)

#: 写 / 执行类工具名特征（**优先于读特征判断**，避免 "read_and_write" 误判）。
DEFAULT_WRITE_TOOL_HINTS: Tuple[str, ...] = (
    "write",
    "edit",
    "create",
    "delete",
    "remove",
    "move",
    "copy",
    "mkdir",
    "rmdir",
    "apply",
    "patch",
    "insert",
    "update",
    "replace",
    "execute",
    "bash",
    "shell",
    "run",
    "command",
    "spawn",
)

#: 可能承载「读取目标路径」的参数名。
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


class LoopDetector:
    """连续读取同一文件的死循环检测器（状态机）。

    状态迁移::

        IDLE ──读文件 A──▶ READING(A, 1)
        READING(A, n) ──再读 A──▶ READING(A, n+1)
        READING(A, n) ──读别的文件 / 做别的事──▶ IDLE
        READING(A, count >= threshold) ──▶ 触发 LOOP

    扩展接口：``observe()`` 只负责「同一目标重复」这一种模式。后续要加
    「相同命令重复」「相同参数重复」等模式时，照抄同样的
    ``observe(event) -> Optional[str]`` 契约实现新检测器即可，
    上层 :class:`RuleEngine` 不需要改动。

    :param threshold: 连续读取同一文件达到该次数即判定为疑似死循环。
    :param enabled: 是否启用。
    :param read_tools: 读操作工具名特征，覆盖默认值。
    :param write_tools: 写 / 执行类工具名特征，覆盖默认值。
    """

    def __init__(
        self,
        threshold: int = 3,
        enabled: bool = True,
        read_tools: Optional[List[str]] = None,
        write_tools: Optional[List[str]] = None,
    ) -> None:
        self.threshold = max(int(threshold or 0), 1)
        self.enabled = bool(enabled)
        self.read_hints = tuple(
            str(item).lower() for item in (read_tools or DEFAULT_READ_TOOL_HINTS)
        )
        self.write_hints = tuple(
            str(item).lower() for item in (write_tools or DEFAULT_WRITE_TOOL_HINTS)
        )
        self._current_key: Optional[str] = None
        self._current_target: str = ""
        self._count: int = 0

    # -- 状态机 ---------------------------------------------------------
    def observe(self, event: Dict[str, Any]) -> Optional[str]:
        """喂入一个事件，判断是否构成死循环。

        非「读取同一文件」的事件会打断连续性（状态回到 IDLE）。

        :param event: 统一事件字典。
        :return: 命中时返回人类可读的 reason，否则返回 ``None``。
        """
        if not self.enabled:
            return None
        if not isinstance(event, dict) or str(event.get("type") or "") != "tool_call":
            self.reset()
            return None

        tool = event.get("tool")
        tool = tool if isinstance(tool, dict) else {}
        name = str(tool.get("name") or "").lower()
        arguments = tool.get("arguments")
        arguments = arguments if isinstance(arguments, dict) else {}

        if self._classify(name) != "read":
            self.reset()
            return None

        target = self._extract_target(arguments)
        if not target:
            self.reset()
            return None

        key = self._normalize(target)
        if key == self._current_key:
            self._count += 1
        else:
            self._current_key = key
            self._current_target = target
            self._count = 1

        if self._count >= self.threshold:
            return (
                f"连续 {self._count} 次读取同一个文件，疑似死循环: {target}"
            )
        return None

    def reset(self) -> None:
        """把状态机复位到 IDLE。"""
        self._current_key = None
        self._current_target = ""
        self._count = 0

    @property
    def state(self) -> str:
        """当前状态机的可读状态（用于日志与调试）。"""
        if self._current_key is None:
            return "IDLE"
        return f"READING({self._current_target}, {self._count})"

    @property
    def current_target(self) -> str:
        """当前正在重复读取的目标（无则为空串）。"""
        return self._current_target

    # -- 内部工具 -------------------------------------------------------
    def _classify(self, tool_name: str) -> str:
        """把工具名分类为 ``read`` / ``write`` / ``other``。"""
        if not tool_name:
            return "other"
        if any(hint in tool_name for hint in self.write_hints):
            return "write"
        if any(hint in tool_name for hint in self.read_hints):
            return "read"
        return "other"

    @staticmethod
    def _extract_target(arguments: Dict[str, Any]) -> str:
        """从工具参数中取出读取目标路径。"""
        for key in _PATH_KEYS:
            value = arguments.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    @staticmethod
    def _normalize(target: str) -> str:
        """归一化比较键：统一分隔符、去空白、转小写。

        转小写是因为哨兵主要运行在 Windows 上，同一文件的路径大小写
        可能不一致；代价是 Linux 上会偶尔把两个不同文件当成同一个，
        这个方向上的误报只会多一条告警，不会造成漏报。
        """
        return target.strip().replace("\\", "/").rstrip("/").lower()


# ---------------------------------------------------------------------------
# 规则引擎
# ---------------------------------------------------------------------------

#: 证据片段的最大长度。
_MAX_EVIDENCE = 500


class RuleEngine:
    """L1 规则引擎。

    :param config: 已加载的配置字典；为 ``None`` 时自行从
        ``config/config.yaml`` 读取。
    :param config_path: 配置文件路径（``config`` 为 ``None`` 时生效）。
    """

    def __init__(
        self,
        config: Optional[Dict[str, Any]] = None,
        *,
        config_path: Optional[str] = None,
    ) -> None:
        if config is None:
            config = load_config(config_path)
        self.config: Dict[str, Any] = config or {}
        self.rules_config: Dict[str, Any] = self.config.get("rules") or {}
        self.enabled = bool(self.rules_config.get("enabled", True))
        #: v3.7.0：用户在清单上做出的选择（关闭 / 降级 / 自定义）。
        self.selection = RuleSelection.from_config(self.rules_config)
        self._dangerous: List[
            Tuple[str, Pattern[str], Optional[str]]
        ] = self._compile_dangerous()
        catastrophic_config = self.rules_config.get("catastrophic") or {}
        self._catastrophic = CatastrophicDetector(
            enabled=bool(catastrophic_config.get("enabled", True)),
            categories=self._effective_categories(catastrophic_config),
        )
        loop_config = self.rules_config.get("loop") or {}
        self._loop = LoopDetector(
            threshold=int(loop_config.get("same_file_read_threshold", 3) or 3),
            enabled=bool(loop_config.get("enabled", True)),
            read_tools=loop_config.get("read_tools"),
            write_tools=loop_config.get("write_tools"),
        )

    # -- 对外接口 -------------------------------------------------------
    def check_event(self, event: Dict[str, Any]) -> Dict[str, Any]:
        """检查单个事件。

        :param event: 统一事件字典（来自适配器）。
        :return: 判定字典，必定包含 ``verdict`` 与 ``reason`` 两个键。
            ``verdict`` 取值：``ON_TRACK`` / ``DANGEROUS`` / ``LOOP``。
        """
        if not self.enabled:
            return self._on_track()
        if not isinstance(event, dict):
            return self._on_track()

        event_type = str(event.get("type") or "")
        tool = event.get("tool")
        tool = tool if isinstance(tool, dict) else {}
        command = str(tool.get("command") or "")
        arguments = tool.get("arguments")
        arguments = arguments if isinstance(arguments, dict) else {}

        # 1) 红线级 —— 优先级最高，守护者模式只看这一类
        if event_type == "tool_call":
            red_line = self._catastrophic.check(command, arguments)
            if red_line is not None:
                entry_id = "catastrophic.{}".format(red_line["category"])
                action = self.selection.action_of(entry_id)
                log_only = action == ACTION_LOG
                return {
                    "verdict": VERDICT_DANGEROUS,
                    "reason": red_line["reason"],
                    "rule_id": red_line["rule_id"],
                    "evidence": red_line["evidence"],
                    "level": "critical",
                    "confidence": 1.0,
                    "pattern": red_line["pattern"],
                    # 「仅记录」= 降为普通级 —— 于是它**永远不会被挂起**。
                    "severity": (
                        SEVERITY_STANDARD if log_only else SEVERITY_RED_LINE
                    ),
                    "category": red_line["category"],
                    "rule_entry_id": entry_id,
                    "rule_action": action,
                    "log_only": log_only,
                }

        # 2) 高危命令 —— 优先级高于死循环
        if event_type == "tool_call":
            hit = self._match_dangerous(command, arguments)
            if hit is not None:
                pattern, matched, entry_id = hit
                action = self.selection.action_of(entry_id) if entry_id else ACTION_BLOCK
                return {
                    "verdict": VERDICT_DANGEROUS,
                    "reason": f"匹配到高危命令: {matched}",
                    "rule_id": "dangerous_command",
                    "evidence": matched[:_MAX_EVIDENCE],
                    "level": "critical",
                    "confidence": 1.0,
                    "pattern": pattern,
                    "severity": SEVERITY_STANDARD,
                    "category": None,
                    "rule_entry_id": entry_id,
                    "rule_action": action,
                    "log_only": action == ACTION_LOG,
                }

        # 3) 死循环 —— 状态机跨事件累积
        #
        # v3.7.0：清单里关掉「重复读取同一文件」后，
        # 连**状态都不再累积**（不然关了规则还在偷偷记状态）。
        loop_reason = ""
        if self.selection.is_enabled("loop.repeat_read"):
            loop_reason = self._loop.observe(event)
        if loop_reason:
            entry_id = "loop.repeat_read"
            action = self.selection.action_of(entry_id)
            return {
                "verdict": VERDICT_LOOP,
                "reason": loop_reason,
                "rule_id": "repeat_read_loop",
                "evidence": self._loop.current_target[:_MAX_EVIDENCE],
                "level": "warn",
                "confidence": 0.7,
                "pattern": None,
                "severity": SEVERITY_STANDARD,
                "category": None,
                "rule_entry_id": entry_id,
                "rule_action": action,
                "log_only": action == ACTION_LOG,
            }

        return self._on_track()

    def reset(self) -> None:
        """复位规则引擎的状态机（换日志文件 / 换会话时调用）。"""
        self._loop.reset()

    # -- 内部实现 -------------------------------------------------------
    def _effective_categories(self, catastrophic_config: Dict[str, Any]) -> List[str]:
        """算出**实际生效**的红线类别。

        两个来源取交集：

        1. ``rules.catastrophic.categories``（v3.6.x 的白名单）；
        2. v3.7.0 的规则清单 —— 被关闭的 ``catastrophic.<类别>``
           规则**不再参与判定**。

        :param catastrophic_config: ``rules.catastrophic`` 配置块。
        :return: 生效的类别名列表。
        """
        configured = catastrophic_config.get("categories")
        if configured is None:
            candidates = list(CATEGORY_LABELS.keys())
        else:
            candidates = [
                str(item).strip().lower()
                for item in configured
                if str(item).strip()
            ]
        disabled = self.selection.disabled_categories()
        return [
            item
            for item in candidates
            if item in CATEGORY_LABELS and item not in disabled
        ]

    def _compile_dangerous(
        self,
    ) -> List[Tuple[str, Pattern[str], Optional[str]]]:
        """编译高危命令正则表。

        配置里的 ``rules.dangerous_commands`` 默认是**增量追加**到内置表；
        置 ``rules.dangerous_commands_replace: true`` 可改为整体替换。

        v3.7.0 起，内置条目**逐条**受规则清单控制 ——
        被关闭的规则**不参与编译**；
        用户在清单里新增的自定义规则也会一并编译进来。

        :return: ``(正则原文, 编译结果, 规则 id)`` 列表；
            第三项为 ``None`` 表示这条来自旧的
            ``rules.dangerous_commands`` 列表（没有清单 id）。
        """
        custom_raw = self.rules_config.get("dangerous_commands") or []
        legacy = [
            str(item)
            for item in custom_raw
            if isinstance(item, (str, int, float)) and str(item).strip()
        ]
        replace = bool(self.rules_config.get("dangerous_commands_replace", False))
        pairs: List[Tuple[str, Optional[str]]] = []
        if not replace:
            pairs.extend(
                (entry.pattern, entry.id) for entry in BUILTIN_DANGEROUS_RULES
            )
        pairs.extend((raw, None) for raw in legacy)

        compiled: List[Tuple[str, Pattern[str], Optional[str]]] = []
        for raw, entry_id in pairs:
            if entry_id is not None and not self.selection.is_enabled(entry_id):
                continue
            regex = self._compile_one(raw, entry_id)
            if regex is not None:
                compiled.append((raw, regex, entry_id))

        # --- v3.7.0：清单里的自定义规则 ---
        for entry in self.selection.custom:
            if entry.kind != KIND_DANGEROUS:
                continue
            if not self.selection.is_enabled(entry.id):
                continue
            regex = self._compile_one(entry.pattern, entry.id)
            if regex is not None:
                compiled.append((entry.pattern, regex, entry.id))
        return compiled

    @staticmethod
    def _compile_one(raw: str, entry_id: Optional[str]) -> Optional[Pattern[str]]:
        """编译一条正则；失败只记日志，不抛异常。

        :param raw: 正则原文。
        :param entry_id: 规则 id（用于日志）。
        :return: 编译结果；失败返回 ``None``。
        """
        try:
            return re.compile(raw, re.IGNORECASE)
        except re.error as exc:
            logger.warning(
                "高危命令正则编译失败，已跳过：%r（%s，规则 %s）",
                raw,
                exc,
                entry_id or "-",
            )
            return None

    def _match_dangerous(
        self,
        command: str,
        arguments: Dict[str, Any],
    ) -> Optional[Tuple[str, str, Optional[str]]]:
        """在命令与参数中匹配高危命令。

        :return: ``(正则原文, 命中的文本, 规则 id)``；
            未命中返回 ``None``；规则 id 为 ``None`` 表示这条来自旧的
            ``rules.dangerous_commands`` 列表。
        """
        parts: List[str] = []
        if command.strip():
            parts.append(command)
        if arguments:
            try:
                parts.append(json.dumps(arguments, ensure_ascii=False, sort_keys=True))
            except (TypeError, ValueError):
                parts.append(str(arguments))
        haystack = "\n".join(parts)
        if not haystack.strip():
            return None

        for pattern, regex, entry_id in self._dangerous:
            match = regex.search(haystack)
            if match is None:
                continue
            matched = match.group(0).strip()
            if matched:
                return pattern, matched, entry_id
        return None

    @staticmethod
    def _on_track() -> Dict[str, Any]:
        """构造「一切正常」的判定结果。"""
        return {
            "verdict": VERDICT_ON_TRACK,
            "reason": "",
            "rule_id": None,
            "evidence": "",
            "level": "info",
            "confidence": 0.0,
            "pattern": None,
            "severity": SEVERITY_NONE,
            "category": None,
            "rule_entry_id": None,
            "rule_action": ACTION_BLOCK,
            "log_only": False,
        }
