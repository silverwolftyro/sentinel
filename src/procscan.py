"""进程反查 —— 从「一份日志」反查出「哪个进程正在写它」。

背景
====

v2.3.0 的雷达能**发现**用户正在跑的 Agent 日志并挂载进来实时审查，
但挂载进来的会话是**纯只读**的：哨兵看得见它写了什么，
却**不知道该拦谁** —— 守护者需要一个明确的 PID 才能挂起进程。

本模块负责把这件事做掉：从日志路径出发，反查系统里**哪个进程**
持有这个文件（或在命令行里带着它）。

多策略，而不是一把梭
====================

「写这个文件的进程」在不同平台、不同 Agent、不同启动方式下，
信号完全不一样，所以本模块**并列**几套策略，各给一个置信度：

============  ==========================================  ==========
策略          命中条件                                     置信度
============  ==========================================  ==========
cmdline_path  进程命令行里带着这个日志的路径               0.95
open_files    进程**打开着**这个文件（文件句柄）           0.95
cmdline_cwd   命令行里同时有 Agent 特征与工作目录         0.85
cwd_only      只有工作目录对得上                          0.60
============  ==========================================  ==========

只有置信度达到 :data:`BINDABLE_CONFIDENCE` 的结果才允许
**武装守护者**（见 :attr:`ProcessHit.bindable`）——
因为挂起一个**没确认过**的进程，比不挂起糟糕得多（红线 R7）。

现实限制（如实说明）
====================

* ``mcode ... > session.jsonl`` 这种**重定向**，
  路径**不会**出现在进程的命令行里 —— ``cmdline_path`` 必然落空；
* Windows 上 ``open_files()`` 需要更高权限，
  绝大多数进程会抛 :class:`AccessDenied` —— ``open_files`` 基本指望不上；
* 因此**最稳**的信号其实是「Agent 特征 + 工作目录」。

本模块对结果**如实**：查不到就是查不到，返回空结果，
绝不用「大概是某个进程」糊弄过去。界面据此显示「[未绑定进程]」。

叶子模块
========

本模块**只依赖 Python 标准库**；``psutil`` 是**可选依赖**，
在函数内部惰性导入 —— 没装就整体降级成「反查不可用」，
绝不抛异常挡住启动（架构红线 R5）。

因此它**不 import** 项目里任何其它模块
（包括 :mod:`src.core.guardian` 与 :mod:`src.core.engine`），
界面层、启动层、审查层都能安全地 import 它。
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

logger = logging.getLogger(__name__)

__all__ = [
    "AGENT_CMDLINE_MARKERS",
    "AGENT_HOST_IMAGES",
    "AGENT_PROCESS_MARKERS",
    "BINDABLE_CONFIDENCE",
    "AgentProcess",
    "ProcessHit",
    "build_process_cwd_index",
    "describe_process",
    "detect_agent_process",
    "detect_custom_agent",
    "find_process",
    "match_process_cwd",
    "normalized_path",
    "pid_alive",
    "prompt_from_cmdline_args",
    "protected_pids",
    "psutil_available",
    "scan_agent_processes",
    "scan_agent_prompts",
    "scan_processes",
]


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
#: 命令行里出现这些片段就说明是某个 Agent 的进程。
#:
#: 注意：**只认 Agent 名字，不认「哨兵自己的路径」**——
#: 哨兵自己也会在命令行里带日志路径（``--watch``），
#: 靠排除自身进程来避免误判，而不是靠这些标记。
AGENT_CMDLINE_MARKERS: Dict[str, Tuple[str, ...]] = {
    "codex": ("codex",),
    "mcode": ("mcode",),
    "claude": ("claude",),
}

#: 达到这个置信度才允许「武装守护者」。
#:
#: 低于它的结果只作为**候选**展示：界面会显示「[未绑定进程]」，
#: 并在悬浮提示里说明「只按工作目录匹配到了某个进程，不敢确认」。
#: 宁可少一个能力，也不能挂起一个**没确认过**的进程（红线 R7）。
BINDABLE_CONFIDENCE: float = 0.75

#: 可能是 **Agent CLI 宿主进程**的可执行文件名（小写）（v3.3.2）。
#:
#: 各大 Agent CLI 大多是 Node 应用 —— 进程名就叫 ``node.exe``，
#: 光看进程名**认不出来**，必须再看命令行里有没有 Agent 特征。
AGENT_HOST_IMAGES: Tuple[str, ...] = (
    "node",
    "node.exe",
    "mcode",
    "mcode.exe",
    "codex",
    "codex.exe",
    "claude",
    "claude.exe",
)

#: 命令行里出现这些片段，才算「这是一个 Agent 进程」（v3.3.2）。
#:
#: ⚠️ 这些片段比 :data:`AGENT_CMDLINE_MARKERS` **更具体**：
#: 真实的 mcode 命令行长这样 ——
#: ``node.exe ...\\@minimax-ai\\code\\cli.js --continue``，
#: 里面**根本没有「mcode」这三个字**。
#:
#: 用宽泛的片段会把一堆无关的 Node 进程算进来
#: （本机就有 ``npx rea-agents@6.4.0`` 这种 ——
#: 它跑在 ``.minimax-code\\runtime`` 目录下，但**不是** Agent CLI），
#: 那会让提示变成「狼来了」—— 用户点进来发现是假的，就再也不信了。
#:
#: 所以这里只认**包名**（``@minimax-ai/code``）与**命令名**（``mcode``），
#: 不认运行时目录名。
AGENT_PROCESS_MARKERS: Dict[str, Tuple[str, ...]] = {
    "mcode": ("@minimax-ai/code", "mcode"),
    "codex": ("@openai/codex", "codex"),
    "claude": ("@anthropic-ai/claude-code", "claude-code"),
}

#: 单次「活跃 Agent 感知」最多返回多少个进程。
MAX_AGENT_PROCESSES: int = 20

#: 单次扫描最多检查多少个进程（防止在进程极多的机器上卡住）。
MAX_PROCESS_SCAN: int = 400

#: 命令行里这些形态的**参数**不是 Prompt（v3.5.2）。
#:
#: Agent CLI 的命令行通常是「可执行文件 + 脚本路径 + 子命令 + 用户输入」，
#: 前三样都不是 Prompt，必须先剔干净。
PROMPT_SKIP_ARG_SUFFIXES: Tuple[str, ...] = (
    ".js",
    ".mjs",
    ".cjs",
    ".exe",
    ".cmd",
    ".bat",
    ".ps1",
    ".py",
)

#: Prompt 的最短长度（v3.5.2）。
#:
#: 「exec」「run」这类子命令太短，绝不能被当成用户输入。
PROMPT_MIN_CHARS: int = 6

#: Agent CLI 常见的**子命令**（v3.5.2）——
#: 它们是命令行的一部分，但不是用户说的话。
PROMPT_SKIP_COMMANDS: Tuple[str, ...] = (
    "exec",
    "run",
    "chat",
    "resume",
    "continue",
    "agent",
    "shell",
    "serve",
    "start",
)

#: 命中策略的置信度表（改这里就能整体调整「敢不敢信」）。
_STRATEGY_CONFIDENCE: Dict[str, float] = {
    "cmdline_path": 0.95,
    "open_files": 0.95,
    "cmdline_cwd": 0.85,
    "cwd_only": 0.60,
}


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------
@dataclass
class ProcessHit:
    """一次反查的结果。

    :param pid: 进程 PID。
    :param exe: 可执行文件路径。
    :param cmdline: 完整命令行（拼成一行）。
    :param cwd: 进程当前工作目录。
    :param agent: 识别出的 Agent 类型（``codex`` / ``mcode`` / ``claude``
        / ``unknown``）。
    :param strategy: 命中的策略名。
    :param confidence: 置信度（0.0 ~ 1.0）。
    """

    pid: int
    exe: str = ""
    cmdline: str = ""
    cwd: str = ""
    agent: str = "unknown"
    strategy: str = ""
    confidence: float = 0.0

    @property
    def bindable(self) -> bool:
        """这个结果**敢不敢**拿去武装守护者。

        :return: 置信度达标就返回 ``True``。
        """
        return self.confidence >= BINDABLE_CONFIDENCE

    def to_dict(self) -> Dict[str, Any]:
        """转成普通字典（便于直接 JSON 化）。

        :return: 可 ``json.dumps`` 的字典。
        """
        return {
            "pid": int(self.pid),
            "exe": self.exe,
            "cmdline": self.cmdline,
            "cwd": self.cwd,
            "agent": self.agent,
            "strategy": self.strategy,
            "confidence": float(self.confidence),
            "bindable": bool(self.bindable),
        }


# ---------------------------------------------------------------------------
# 活跃 Agent 进程（v3.3.2）
# ---------------------------------------------------------------------------
@dataclass
class AgentProcess:
    """一个**正在跑的 Agent CLI 进程**（v3.3.2）。

    雷达扫的是「**文件**」；这一条扫的是「**进程**」。
    两者互补：文件找不到的时候，进程往往还在 ——
    那正是用户「终端里跑着任务、看板却抓不到」的那一刻。

    :param pid: 进程 PID。
    :param agent: 识别出的 Agent 类型（``mcode`` / ``codex`` / ``claude``）。
    :param exe: 可执行文件路径。
    :param name: 可执行文件名（如 ``node.exe``）。
    :param cwd: 进程当前工作目录。
    :param cmdline: 完整命令行（拼成一行）。
    """

    pid: int
    agent: str = "unknown"
    exe: str = ""
    name: str = ""
    cwd: str = ""
    cmdline: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """转成普通字典（便于直接 JSON 化）。

        :return: 可 ``json.dumps`` 的字典。
        """
        return {
            "pid": int(self.pid),
            "agent": str(self.agent),
            "exe": str(self.exe),
            "name": str(self.name),
            "cwd": str(self.cwd),
            "cmdline": str(self.cmdline),
        }


def detect_agent_process(cmdline: str) -> str:
    """从命令行里认出这是**哪个 Agent** 的进程。

    :param cmdline: 命令行文本。
    :return: Agent 键（``mcode`` / ``codex`` / ``claude``）；
        认不出来就是空串。
    """
    lowered = str(cmdline or "").lower()
    if not lowered:
        return ""
    # Windows 上路径分隔符可能是 ``\`` 也可能是 ``/`` ——
    # npm 全局装出来的包路径用 ``/``，别的安装方式可能用 ``\``。
    # 统一成 ``/`` 再匹配，免得换个安装方式就认不出来。
    lowered = lowered.replace("\\", "/")
    for key, markers in AGENT_PROCESS_MARKERS.items():
        for marker in markers:
            if marker in lowered:
                return key
    return ""


#: 归一化自定义特征时要**去掉**的分隔符（v3.4.0）。
_FEATURE_SEPARATORS = ".-_\\/: \t"

#: 归一化之后还允许参与「去分隔符匹配」的最短关键词长度。
#:
#: ⚠️ 一两个字母的关键词太模糊 ——
#:    去掉分隔符之后几乎什么路径都能命中，那不是识别，是噪音。
_MIN_COMPACT_LENGTH = 3

#: 自定义特征的**最短长度**（v3.4.0）。
#:
#: ⚠️ 比这更短的特征**直接不参与匹配** ——
#:    一个字母的关键词能命中满机的 Node 进程，
#:    那会让「检测到活跃 Agent 进程」的提示彻底失去意义
#:    （狼来了 —— 用户点进来发现是假的，就再也不信了）。
_MIN_FEATURE_LENGTH = 3


def _compact_keyword(text: str) -> str:
    """去掉分隔符，只留「实义字符」（v3.4.0）。

    为什么需要
    ==========

    用户写的是 ``my-cli``，而项目目录叫 ``MyCLI`` ——
    纯子串匹配对不上（中间隔着三个点），
    那正是这个功能最该管住的那个真实场景，不能让它失效。

    去掉 ``. - _ \\ / :`` 与空白之后再比一次，
    ``my-cli`` 就能认出 ``D:\\项目\\MyCLI``。

    ⚠️ 只去**分隔符**，不去中文 ——
    中文是项目名的一部分（``D:\\项目\\...``），去掉就串味了。

    :param text: 原始文本。
    :return: 归一化后的文本。
    """
    return "".join(
        ch for ch in str(text or "").lower()
        if ch not in _FEATURE_SEPARATORS
    )


def detect_custom_agent(
    cmdline: str,
    cwd: str = "",
    features: Optional[Sequence[str]] = None,
) -> str:
    """按**用户自定义的特征**认出这是哪个 Agent 的进程（v3.4.0）。

    为什么需要它
    ============

    内置特征只认得 ``mcode`` / ``codex`` / ``claude`` 三家。
    可用户自己写的 Agent（或者改了包名的 fork）就**认不出来** ——
    雷达于是「明明有任务在跑，却一个都没提示」。

    所以给用户留一个口子：在配置里写几个关键字
    （例如 ``my-cli``），命令行或工作目录里命中就算。

    ⚠️ 匹配的是**命令行或工作目录** ——
    只看命令行会漏掉「关键字在项目路径里」的情况
    （用户的外部插件任务正是这种）。

    ⚠️ 匹配时会**再去掉分隔符**再比一次 ——
    用户写的是 ``my-cli``，而项目目录叫 ``MyCLI``：
    纯子串匹配对不上（中间隔着三个点），
    那正是这个功能最该管住的那个真实场景，不能让它失效。

    ⚠️ 少于 3 个字符的特征**直接忽略** ——
    一个字母能命中满机的 Node 进程，
    「检测到活跃 Agent 进程」的提示就变成了狼来了。

    :param cmdline: 命令行文本。
    :param cwd: 进程工作目录。
    :param features: 自定义特征清单。
    :return: 命中的那个特征；没命中就是空串。
    """
    haystacks = [
        str(cmdline or "").lower(),
        str(cwd or "").lower(),
    ]
    if not any(haystacks):
        return ""
    for raw in features or ():
        keyword = str(raw or "").strip().lower()
        if len(keyword) < _MIN_FEATURE_LENGTH:
            # 太短的特征会命中一切 —— 直接忽略，不制造噪音。
            continue
        compact = _compact_keyword(keyword)
        for haystack in haystacks:
            if keyword in haystack:
                return keyword
            # ⚠️ 去掉分隔符再比一次 ——
            #    ``my-cli`` 要能认出 ``D:\\项目\\MyCLI``。
            if (
                len(compact) >= _MIN_COMPACT_LENGTH
                and compact in _compact_keyword(haystack)
            ):
                return keyword
    return ""


def scan_agent_processes(
    *,
    limit: int = MAX_AGENT_PROCESSES,
    extra_features: Optional[Sequence[str]] = None,
) -> List[AgentProcess]:
    """枚举系统进程，找出**正在跑的 Agent CLI**（v3.3.2）。

    只看两层信号：

    1. **进程名**在 :data:`AGENT_HOST_IMAGES` 里
       （``node.exe`` / ``mcode.exe`` …）；
    2. **命令行**里有该 Agent 的特征
       （见 :data:`AGENT_PROCESS_MARKERS`）。

    ⚠️ 两层**都要**满足 ——
    只看进程名会把满机的 Node 进程算进来；
    只看命令行又会把哨兵自己、编辑器插件之类的算进来。

    v3.4.0 起多了一条路：配置了 ``radar.custom_process_features``
    时，**自定义特征**命中命令行或工作目录也算一个 Agent 进程
    （见 :func:`detect_custom_agent`）。

    同一个 Agent 在**同一个工作目录**里的多个进程
    （CLI 主进程 + 它的子进程）算**一个** ——
    用户关心的是「有几个任务在跑」，不是「有几个 node 进程」。

    psutil 不可用 / 枚举失败 → 返回空列表（界面据此隐藏提示）。

    :param limit: 最多返回多少个。
    :param extra_features: 自定义特征清单（v3.4.0）。
    :return: 活跃 Agent 进程列表。
    """
    psutil = psutil_module()
    if psutil is None:
        logger.debug("psutil 不可用，活跃 Agent 感知整体降级。")
        return []

    excluded: Set[int] = protected_pids()
    found: List[AgentProcess] = []
    seen: Set[Tuple[str, str]] = set()

    try:
        iterator = psutil.process_iter(["pid", "name", "exe", "cmdline"])
    except Exception as exc:  # noqa: BLE001 —— 扫描失败只降级
        logger.debug("进程枚举失败：%s: %s", type(exc).__name__, exc)
        return []

    checked = 0
    for proc in iterator:
        if checked >= MAX_PROCESS_SCAN:
            break
        checked += 1
        try:
            info = proc.info or {}
            pid = int(info.get("pid") or 0)
        except (TypeError, ValueError):
            continue
        if not pid or pid in excluded:
            continue

        name = str(info.get("name") or "").strip().lower()
        if name not in AGENT_HOST_IMAGES:
            continue

        cmdline_parts = info.get("cmdline") or []
        joined = " ".join(str(part) for part in cmdline_parts)
        cwd = _safe_cwd(proc)
        agent = detect_agent_process(joined)
        if not agent:
            # 内置特征没认出来 —— 试试用户自定义的那些（v3.4.0）。
            agent = detect_custom_agent(joined, cwd, extra_features)
        if not agent:
            continue

        key = (agent, cwd)
        if cwd and key in seen:
            continue
        seen.add(key)

        found.append(
            AgentProcess(
                pid=pid,
                agent=agent,
                exe=str(info.get("exe") or ""),
                name=name,
                cwd=cwd,
                cmdline=joined,
            )
        )
        if len(found) >= max(int(limit), 1):
            break
    return found


# ---------------------------------------------------------------------------
# 命令行里的 Prompt（v3.5.2）
# ---------------------------------------------------------------------------
def prompt_from_cmdline_args(args: Sequence[str]) -> str:
    """从命令行**参数**里认出「用户下的那句 Prompt」（v3.5.2）。

    为什么需要它
    ============

    Agent 的会话日志**不一定**留着用户最初那句话 ——
    旁路挂载进来的会话，哨兵是从文件**尾部**开始跟随的，
    那句 Prompt 可能压根不在日志里。

    但用户**敲的那条命令行**通常还在：``mcode exec "把登录模块重构一下"``。
    那是命令行的最后一个自然语言参数。

    怎么认
    ======

    Agent CLI 的命令行一般是「可执行文件 → 脚本路径 → 子命令 → 用户输入」，
    所以依次剔掉：

    * 以 ``-`` 开头的**开关**（``--continue`` / ``--verbose``）；
    * 路径形态的参数（脚本路径、配置文件）；
    * 常见的**子命令**（``exec`` / ``run`` / ``chat`` …）；
    * 剩下太短的参数（``exec`` 这种两三个字母的词）。

    剩下的第一个「像句话」的参数就是 Prompt。

    ⚠️ **宁可漏报也不误报**（架构红线 R5）——
    认错了会凭空造出一个假的任务目标，
    后面的意图漂移判定全会跟着跑偏。

    :param args: 命令行参数列表（``psutil`` 给的原始形态）。
    :return: 认出的 Prompt；认不出就是空串。
    """
    for raw in list(args or [])[1:]:
        text = str(raw or "").strip().strip('"').strip("'").strip()
        if not text:
            continue
        if text.startswith("-"):
            continue
        lowered = text.lower()
        if lowered.endswith(PROMPT_SKIP_ARG_SUFFIXES):
            continue
        if "/" in text or "\\" in text:
            continue
        if lowered in PROMPT_SKIP_COMMANDS:
            continue
        if len(text) < PROMPT_MIN_CHARS:
            continue
        # 「像句话」的判定：带空格，或者含非 ASCII（中文任务最常见）。
        if " " in text or not text.isascii():
            return text
    return ""


def scan_agent_prompts(
    *,
    extra_features: Optional[Sequence[str]] = None,
) -> List[Tuple[str, str]]:
    """枚举正在跑的 Agent CLI，挑出「命令行里带 Prompt」的进程（v3.5.2）。

    这是 :func:`scan_agent_processes` 的**补充**：它只关心
    「命令行里带着用户输入」的那批进程 ——
    日志里翻不到任务锚点时，这是最后一条自动提取的路子。

    ⚠️ psutil 不可用 / 枚举失败 → 返回空列表（界面据此提示手动输入）。
    ⚠️ 绝不抛异常挡住哨兵启动（架构红线 R5）。

    :param extra_features: 自定义特征清单（v3.4.0）。
    :return: ``[(工作目录, Prompt 原文)]`` 列表；没有就是空列表。
    """
    psutil = psutil_module()
    if psutil is None:
        logger.debug("psutil 不可用，命令行 Prompt 提取整体降级。")
        return []

    excluded: Set[int] = protected_pids()
    found: List[Tuple[str, str]] = []
    seen_cwds: Set[str] = set()

    try:
        iterator = psutil.process_iter(["pid", "name", "exe", "cmdline"])
    except Exception as exc:  # noqa: BLE001 —— 扫描失败只降级
        logger.debug("进程枚举失败：%s: %s", type(exc).__name__, exc)
        return []

    checked = 0
    for proc in iterator:
        if checked >= MAX_PROCESS_SCAN:
            break
        checked += 1
        try:
            info = proc.info or {}
            pid = int(info.get("pid") or 0)
        except (TypeError, ValueError):
            continue
        if not pid or pid in excluded:
            continue

        name = str(info.get("name") or "").strip().lower()
        if name not in AGENT_HOST_IMAGES:
            continue

        parts = [str(part) for part in (info.get("cmdline") or [])]
        if not parts:
            continue
        joined = " ".join(parts)
        cwd = _safe_cwd(proc)
        agent = detect_agent_process(joined)
        if not agent:
            agent = detect_custom_agent(joined, cwd, extra_features)
        if not agent:
            continue

        prompt = prompt_from_cmdline_args(parts)
        if not prompt:
            continue
        key = normalized_path(cwd) if cwd else ""
        if key and key in seen_cwds:
            continue
        if key:
            seen_cwds.add(key)
        found.append((cwd, prompt))
    return found


# ---------------------------------------------------------------------------
# 能力探测
# ---------------------------------------------------------------------------
def psutil_module() -> Optional[Any]:
    """拿到 psutil 模块；没装就返回 ``None``。

    psutil 是**可选依赖** —— 缺了它反查整体降级成「查不到」，
    绝不硬失败（架构红线 R5）。

    :return: psutil 模块；不可用就是 ``None``。
    """
    try:
        import psutil
    except ImportError:
        return None
    return psutil


def psutil_available() -> bool:
    """反查能力当前可不可用。

    :return: psutil 在手就返回 ``True``。
    """
    return psutil_module() is not None


def pid_alive(pid: int) -> Optional[bool]:
    """某个 PID 现在还活着吗（v3.4.0）。

    为什么需要它
    ============

    用户实测：「进程已死，但因日志文件还在，状态依然显示『运行中』。」

    哨兵判断「会话还在跑」靠的是**日志文件还在被写**。
    但 Agent 跑完之后那个文件**仍然躺在磁盘上** ——
    于是看板就一直显示「运行中」，右侧还在刷**不再变化的**旧日志。

    所以要**独立地**看一眼那个进程还在不在。

    :param pid: 进程 ID。
    :return: 活着 ``True``、死了 ``False``；
        **查不了**（没有 psutil、或 PID 非法）返回 ``None`` ——
        ``None`` 的意思是「不知道」，调用方**不能**据此改状态。
    """
    value = int(pid or 0)
    if value <= 0:
        return None
    psutil = psutil_module()
    if psutil is None:
        return None
    try:
        return bool(psutil.pid_exists(value))
    except (OSError, ValueError):
        return None


def normalized_path(path: str) -> str:
    """把路径归一化成「能拿来比大小写」的形式。

    Windows 上路径**不区分大小写**，Linux 上区分 ——
    归一化时按平台决定，免得在 Windows 上漏匹配。

    :param path: 原始路径。
    :return: 归一化后的路径；入参为空就是空串。
    """
    text = str(path or "").strip()
    if not text:
        return ""
    try:
        text = os.path.abspath(os.path.expanduser(text))
    except (OSError, ValueError) as exc:
        # 归一化失败 —— 退回到**原样**的路径（下面照常 normcase）。
        # 不能吞掉：路径没归一化会让后面的路径比较**悄悄失效**。
        logger.warning(
            "归一化路径失败（%s）：%s: %s", text, type(exc).__name__, exc
        )
    return os.path.normcase(text)


def protected_pids() -> Set[int]:
    """「绝对不能当成被监控进程」的那几个 PID。

    至少包括：

    * 哨兵**自己**（把自己当成被监控对象是一种很别致的自杀）；
    * 哨兵的**父进程**；
    * 打包版（PyInstaller）下与哨兵**同名**的所有进程 ——
      单文件模式会再起一个同名子进程，两个都得排除，
      否则哨兵会把自己当成用户那个 Agent。

    :return: 受保护的 PID 集合。
    """
    protected: Set[int] = set()
    try:
        protected.add(os.getpid())
    except OSError as exc:  # pragma: no cover —— 理论上不会发生
        logger.debug("取自身 PID 失败：%s: %s", type(exc).__name__, exc)
    try:
        protected.add(os.getppid())
    except OSError as exc:  # pragma: no cover —— 理论上不会发生
        logger.debug("取父进程 PID 失败：%s: %s", type(exc).__name__, exc)

    own_base = _own_exe_basename()
    if not own_base or not getattr(sys, "frozen", False):
        # 源码运行时 exe 是 python —— 把**所有** python 进程都排除掉
        # 会误伤用户的 Python Agent，所以只排除自身与父进程。
        return protected

    psutil = psutil_module()
    if psutil is None:
        return protected
    try:
        for proc in psutil.process_iter(["pid", "exe"]):
            info = proc.info or {}
            pid = int(info.get("pid") or 0)
            exe = str(info.get("exe") or "")
            if not pid or not exe:
                continue
            if os.path.basename(exe).lower() == own_base:
                protected.add(pid)
    except Exception as exc:  # noqa: BLE001 —— 探测失败只降级
        logger.debug("排除同名进程失败：%s: %s", type(exc).__name__, exc)
    return protected


def _own_exe_basename() -> str:
    """哨兵自己的可执行文件名（小写）。

    :return: 文件名；取不到就是空串。
    """
    try:
        return os.path.basename(str(sys.executable or "")).lower()
    except (OSError, ValueError):  # pragma: no cover —— 理论上不会发生
        return ""


# ---------------------------------------------------------------------------
# 核心：扫描
# ---------------------------------------------------------------------------
def scan_processes(
    *,
    log_path: str = "",
    agent: str = "",
    cwd: str = "",
    exclude_pids: Optional[Iterable[int]] = None,
    limit: int = 5,
) -> List[ProcessHit]:
    """扫描系统里的进程，找出**最可能**在写这份日志的那些。

    :param log_path: 被监控的日志路径（绝对路径或相对路径都行）。
    :param agent: 已知的 Agent 类型；用来在命令行里找特征。
    :param cwd: 已知的工作目录；用来做「进程 cwd 匹配」。
    :param exclude_pids: 额外要排除的 PID（哨兵自身等）。
    :param limit: 最多返回多少条。
    :return: 按置信度**倒序**排列的结果列表；psutil 不可用就是空列表。
    """
    psutil = psutil_module()
    if psutil is None:
        logger.debug("psutil 不可用，进程反查整体降级。")
        return []

    excluded: Set[int] = protected_pids()
    for pid in exclude_pids or ():
        try:
            excluded.add(int(pid))
        except (TypeError, ValueError):
            continue

    needle = normalized_path(log_path)
    target_cwd = normalized_path(cwd)
    wanted_agents = _wanted_markers(agent)
    hits: Dict[int, ProcessHit] = {}

    checked = 0
    try:
        iterator = psutil.process_iter(
            ["pid", "name", "exe", "cmdline"]
        )
    except Exception as exc:  # noqa: BLE001 —— 扫描失败只降级
        logger.debug("进程枚举失败：%s: %s", type(exc).__name__, exc)
        return []

    for proc in iterator:
        if checked >= MAX_PROCESS_SCAN:
            break
        checked += 1
        try:
            info = proc.info or {}
            pid = int(info.get("pid") or 0)
        except (TypeError, ValueError):
            continue
        if not pid or pid in excluded:
            continue

        cmdline_parts = info.get("cmdline") or []
        joined = " ".join(str(part) for part in cmdline_parts)
        proc_cwd = _safe_cwd(proc)

        hit = _match_process(
            pid=pid,
            exe=str(info.get("exe") or ""),
            name=str(info.get("name") or ""),
            cmdline=joined,
            cwd=proc_cwd,
            needle=needle,
            target_cwd=target_cwd,
            wanted_agents=wanted_agents,
        )
        if hit is None:
            continue

        # 文件句柄这条策略**很贵**（Windows 上多半直接被拒），
        # 所以只在命令行没命中时才试。
        if hit.strategy != "cmdline_path" and needle:
            open_hit = _match_open_files(proc, needle, hit)
            if open_hit is not None:
                hit = open_hit

        current = hits.get(pid)
        if current is None or hit.confidence > current.confidence:
            hits[pid] = hit

    ordered = sorted(
        hits.values(),
        key=lambda item: (-item.confidence, item.pid),
    )
    return ordered[: max(int(limit), 0)]


def find_process(
    log_path: str = "",
    *,
    agent: str = "",
    cwd: str = "",
    exclude_pids: Optional[Iterable[int]] = None,
    require_bindable: bool = False,
) -> Optional[ProcessHit]:
    """反查**一个**最可能的进程。

    :param log_path: 被监控的日志路径。
    :param agent: 已知的 Agent 类型。
    :param cwd: 已知的工作目录。
    :param exclude_pids: 额外要排除的 PID。
    :param require_bindable: 为 ``True`` 时**只**接受置信度达标的
        结果 —— 武装守护者时必须走这条。
    :return: 命中的进程；查不到就是 ``None``。
    """
    hits = scan_processes(
        log_path=log_path,
        agent=agent,
        cwd=cwd,
        exclude_pids=exclude_pids,
        limit=5,
    )
    for hit in hits:
        if require_bindable and not hit.bindable:
            continue
        return hit
    return None


def describe_process(pid: int) -> Optional[ProcessHit]:
    """描述**指定**的进程（不看匹配规则，只看它是什么）。

    :param pid: 目标 PID。
    :return: 进程描述；进程不存在就是 ``None``。
    """
    psutil = psutil_module()
    if psutil is None:
        return None
    try:
        value = int(pid)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    try:
        proc = psutil.Process(value)
        with proc.oneshot():
            cmdline = proc.cmdline() or []
            exe = proc.exe() or ""
    except Exception as exc:  # noqa: BLE001 —— 进程可能已经没了
        logger.debug("读取进程 %s 失败：%s: %s", pid, type(exc).__name__, exc)
        return None
    joined = " ".join(str(part) for part in cmdline)
    return ProcessHit(
        pid=value,
        exe=exe,
        cmdline=joined,
        cwd=_safe_cwd(proc),
        agent=detect_agent_in_cmdline(joined),
        strategy="explicit",
        confidence=1.0,
    )


# ---------------------------------------------------------------------------
# 雷达回退链第三步：进程工作目录（v3.0.1）
# ---------------------------------------------------------------------------
def build_process_cwd_index() -> List[Tuple[str, str, str]]:
    """枚举一次系统进程，返回 ``[(归一化命令行, 工作目录, Agent)]``。

    ⚠️ 为什么要有这个「索引」
    ==========================

    雷达要为**每个**候选日志反查一次进程（回退链第三步）。而
    :func:`scan_processes` 每次都要把系统进程**整个枚举一遍** ——
    几十个候选文件就是几十次全量枚举，慢到点开「发现/挂载」要等半天。

    所以改成：**枚举一次**，之后全是字符串匹配。
    这才是能上生产的做法。

    psutil 没装 / 枚举失败 → 返回空列表（雷达自动退到「父目录名」兜底）。

    :return: 进程条目列表；拿不到就是空列表。
    """
    psutil = psutil_module()
    if psutil is None:
        logger.debug("psutil 不可用，进程工作目录反查整体降级。")
        return []

    excluded: Set[int] = protected_pids()
    entries: List[Tuple[str, str, str]] = []

    try:
        iterator = psutil.process_iter(["pid", "name", "exe", "cmdline"])
    except Exception as exc:  # noqa: BLE001 —— 扫描失败只降级
        logger.debug("进程枚举失败：%s: %s", type(exc).__name__, exc)
        return []

    checked = 0
    for proc in iterator:
        if checked >= MAX_PROCESS_SCAN:
            break
        checked += 1
        try:
            info = proc.info or {}
            pid = int(info.get("pid") or 0)
        except (TypeError, ValueError):
            continue
        if not pid or pid in excluded:
            continue

        cmdline_parts = info.get("cmdline") or []
        joined = " ".join(str(part) for part in cmdline_parts)
        cwd = _safe_cwd(proc)
        if not cwd:
            continue
        entries.append(
            (
                _normalize_cmdline(joined),
                cwd,
                detect_agent_in_cmdline(joined),
            )
        )
    return entries


def match_process_cwd(
    index: Sequence[Tuple[str, str, str]],
    log_path: str,
    agent: str = "",
) -> str:
    """在索引里找「可能正在写这份日志的进程」，返回它的工作目录。

    两级策略：

    1. **精确命中** —— 命令行里带这个日志的完整路径（或
       「父目录\\文件名」）→ 直接用它的工作目录；
    2. **保守推断** —— 命令行里带这个 **Agent 的特征**，而且
       **只有一个**候选进程 → 用它的工作目录；
       有两个以上就**不猜**（宁可没有也不给错的）。

    :param index: :func:`build_process_cwd_index` 的结果。
    :param log_path: 日志路径。
    :param agent: 已识别出的 Agent（``codex`` / ``mcode`` / ``claude``）。
    :return: 工作目录；查不到就是空串。
    """
    if not index or not log_path:
        return ""

    needle = normalized_path(log_path)
    parent = os.path.basename(os.path.dirname(str(log_path)))
    short = normalized_path(os.path.join(parent, os.path.basename(str(log_path))))

    fallback_cwds: List[str] = []
    wanted = str(agent or "").strip().lower()

    for cmdline, cwd, cmd_agent in index:
        if needle and needle in cmdline:
            return cwd
        if short and len(short) > 6 and short in cmdline:
            return cwd
        if wanted and cmd_agent == wanted and cwd not in fallback_cwds:
            fallback_cwds.append(cwd)

    # 只有一个候选才敢用 —— 多个候选时给错工作区比不给更糟。
    if len(fallback_cwds) == 1:
        return fallback_cwds[0]
    return ""


def _normalize_cmdline(text: str) -> str:
    """把命令行归一化成「能拿来比大小写」的形式。

    ⚠️ 不能用 :func:`normalized_path` —— 它会做 ``abspath``，
    而命令行里的路径往往是**相对**的。

    :param text: 命令行文本。
    :return: 归一化后的文本。
    """
    return str(text or "").replace("/", "\\").lower()


# ---------------------------------------------------------------------------
# 内部实现
# ---------------------------------------------------------------------------
def _wanted_markers(agent: str) -> List[Tuple[str, Tuple[str, ...]]]:
    """要盯的 Agent 特征。

    指定了 Agent 就只看它；没指定就**全都**看 ——
    「命令行里有个 codex」这件事本身就是很强的信号。

    :param agent: Agent 键。
    :return: ``(agent 键, 特征元组)`` 列表。
    """
    key = str(agent or "").strip().lower()
    if key and key in AGENT_CMDLINE_MARKERS:
        return [(key, AGENT_CMDLINE_MARKERS[key])]
    return list(AGENT_CMDLINE_MARKERS.items())


def _match_process(
    *,
    pid: int,
    exe: str,
    name: str,
    cmdline: str,
    cwd: str,
    needle: str,
    target_cwd: str,
    wanted_agents: Sequence[Tuple[str, Tuple[str, ...]]],
) -> Optional[ProcessHit]:
    """判断一个进程命中了哪条策略。

    :return: 命中就返回 :class:`ProcessHit`；没命中就是 ``None``。
    """
    lowered = cmdline.lower()
    exe_lower = str(exe or "").lower()
    name_lower = str(name or "").lower()

    # --- 策略 1：命令行里带着这个日志路径 ---
    if needle and needle in lowered:
        return ProcessHit(
            pid=pid,
            exe=exe,
            cmdline=cmdline,
            cwd=cwd,
            agent=detect_agent_in_cmdline(cmdline),
            strategy="cmdline_path",
            confidence=_STRATEGY_CONFIDENCE["cmdline_path"],
        )

    agent_key = ""
    for key, markers in wanted_agents:
        if any(marker in lowered for marker in markers) or any(
            marker in name_lower or marker in exe_lower for marker in markers
        ):
            agent_key = key
            break
    if not agent_key:
        return None

    cwd_hit = bool(target_cwd) and cwd == target_cwd

    # --- 策略 3：命令行里有 Agent 特征，工作目录也对得上 ---
    if cwd_hit:
        return ProcessHit(
            pid=pid,
            exe=exe,
            cmdline=cmdline,
            cwd=cwd,
            agent=agent_key,
            strategy="cmdline_cwd",
            confidence=_STRATEGY_CONFIDENCE["cmdline_cwd"],
        )

    # --- 策略 4：只有工作目录对得上（置信度不足，不允许武装）---
    if target_cwd and cwd and _same_dir(cwd, target_cwd):
        return ProcessHit(
            pid=pid,
            exe=exe,
            cmdline=cmdline,
            cwd=cwd,
            agent=agent_key,
            strategy="cwd_only",
            confidence=_STRATEGY_CONFIDENCE["cwd_only"],
        )
    return None


def _match_open_files(
    proc: Any,
    needle: str,
    fallback: ProcessHit,
) -> Optional[ProcessHit]:
    """用「进程打开着这个文件」来认领它。

    Windows 上这一步多半会被权限挡住（:class:`AccessDenied`）——
    那是**正常现象**，不是错误：降级成拿不到句柄即可。

    :param proc: psutil 的进程对象。
    :param needle: 归一化后的日志路径。
    :param fallback: 已经命中的结果（用来继承字段）。
    :return: 命中就返回新的 :class:`ProcessHit`；拿不到就是 ``None``。
    """
    try:
        handles = proc.open_files()
    except Exception as exc:  # noqa: BLE001 —— 权限不足是常态
        logger.debug("读取进程 %s 的文件句柄失败：%s", fallback.pid, exc)
        return None
    for handle in handles or ():
        path = str(getattr(handle, "path", "") or "")
        if not path:
            continue
        if normalized_path(path) == needle:
            return ProcessHit(
                pid=fallback.pid,
                exe=fallback.exe,
                cmdline=fallback.cmdline,
                cwd=fallback.cwd,
                agent=fallback.agent,
                strategy="open_files",
                confidence=_STRATEGY_CONFIDENCE["open_files"],
            )
    return None


def _safe_cwd(proc: Any) -> str:
    """读进程的工作目录；读不到就是空串。

    :param proc: psutil 的进程对象。
    :return: 工作目录；拿不到就是空串。
    """
    try:
        return str(proc.cwd() or "")
    except Exception:  # noqa: BLE001 —— 权限不足是常态
        return ""


def _same_dir(left: str, right: str) -> bool:
    """两个路径是不是同一个目录（Windows 下忽略大小写）。

    :param left: 路径 A。
    :param right: 路径 B。
    :return: 同一个目录就返回 ``True``。
    """
    if not left or not right:
        return False
    if os.name == "nt":
        return left.lower() == right.lower()
    return left == right


def detect_agent_in_cmdline(cmdline: str) -> str:
    """从命令行里认出是哪个 Agent。

    :param cmdline: 命令行文本。
    :return: Agent 键；认不出来就是 ``"unknown"``。
    """
    lowered = str(cmdline or "").lower()
    for key, markers in AGENT_CMDLINE_MARKERS.items():
        for marker in markers:
            if marker in lowered:
                return key
    return "unknown"
