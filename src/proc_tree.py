"""外部插件派发进程树 —— 找出「桥接宿主」派发出来的 Agent CLI 进程。

背景
====

第三方插件（上游 ``n.e.k.o_plugin_agent_bridge``）用一个**桥接宿主进程**派发各种
AI Agent CLI。宿主有两种形态：

* webui 形态 —— 命令行是 ``python.exe -m webui.server --port 8787``，
  工作目录落在插件根目录；
* 桌面端插件形态 —— 命令行里**没有** ``webui.server``，
  但工作目录落在插件的数据目录里。

派发出来的 Agent CLI 是宿主的**直接子进程**（插件用
``subprocess.Popen`` 起它们）。

哨兵此前只能从**文件**这一侧发现会话；本模块补上**进程**这一侧：
把宿主进程树整棵走一遍，认出里面哪些是 Agent CLI、它们的工作目录
在哪、有没有一份**真的存在**的日志可以对上。

三层关联，逐层降级
====================

1. **宿主** —— 命令行命中 :data:`BRIDGE_HOST_CMDLINE_MARKERS`
   （``webui.server`` …），**或**工作目录命中
   :data:`BRIDGE_HOST_DIR_MARKERS`（``N.E.K.O`` / ``agent_bridge`` …）；
2. **Agent 身份** —— 命令行里出现 14 个外部插件 CLI 的可执行名 / 别名
   （含 ``node.exe + 脚本`` 形态），认不出才退到**工作目录**弱匹配；
3. **日志** —— 在进程工作目录里找**最近修改过**的 ``.jsonl`` / ``.log``。

⚠️ 红线 R7：**只有真的关联到一个存在的日志文件**，
:class:`DiscoveredAgentProcess` 的 ``attachable`` 才是 ``True`` ——
不够确信就不许用户去武装守护者（挂起一个没确认过的进程，
比不挂起糟糕得多）。

⚠️ 派发审计 ``dispatch.jsonl`` **没有 pid / exe / cmdline**，
只有 ``agent`` / ``cwd`` / 时间窗 —— 所以本模块的一切关联
**一律是弱关联**，绝不当成确定关联。

叶子模块
========

本模块只依赖标准库与**可选**的 ``psutil``；``psutil`` 缺了就整体
降级成「扫不到」，绝不抛异常挡住审查（红线 R5）。

对 :mod:`src.procscan` 的复用
=============================

* :func:`src.procscan.protected_pids` —— 排除哨兵自己与父进程；
* :func:`src.procscan.normalized_path` —— 路径归一化；
* :func:`src.procscan.detect_custom_agent` —— 特征匹配（去分隔符比对）。

⚠️ **进程树枚举**（全仓此前**没有**任何父子进程遍历代码）
在本模块**自己**实现，不动 :mod:`src.procscan`。
"""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Optional,
    Sequence,
    Tuple,
)

try:
    import psutil
except ImportError:  # pragma: no cover —— psutil 是可选依赖
    psutil = None  # type: ignore[assignment]

from src import procscan
from src.agents import (
    alias_table,
    binary_table,
    label_table,
    load_agents,
    log_dir_table,
)

logger = logging.getLogger(__name__)

__all__ = [
    "BRIDGE_HOST_CMDLINE_MARKERS",
    "BRIDGE_HOST_DIR_MARKERS",
    "CLOUD_AGENT_ALIASES",
    "CLOUD_AGENT_BINARIES",
    "CLOUD_AGENT_LABELS",
    "CONFIDENCE_CMDLINE",
    "CONFIDENCE_CWD",
    "MAX_HOSTS",
    "MAX_TREE_DEPTH",
    "STATE_ATTACHED",
    "STATE_DISCOVERED",
    "STATE_UNAVAILABLE",
    "DiscoveredAgentProcess",
    "ProcessInfo",
    "enumerate_descendants",
    "find_bridge_hosts",
    "find_log_for_process",
    "match_agent",
    "scan_process_tree",
    "summarize_tree",
]


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: 桥接宿主的**命令行**特征（小写；比对时不分大小写）。
#:
#: ``webui.server`` 是 webui 形态的命门 —— 插件自己的启动命令就是
#: ``python.exe -m webui.server --port 8787``。
BRIDGE_HOST_CMDLINE_MARKERS: Tuple[str, ...] = (
    "webui.server",
    "webui/server",
    "n.e.k.o_plugin_agent_bridge",
    "agent_bridge",
)

#: 桥接宿主的**工作目录**特征（N.E.K.O 桌面端插件形态）。
#:
#: ⚠️ 这一形态的命令行里**没有** ``webui.server``，只能靠工作目录认 ——
#: 插件数据目录与 N.E.K.O 安装目录里都带这些片段。
BRIDGE_HOST_DIR_MARKERS: Tuple[str, ...] = (
    "n.e.k.o",
    "neko",
    "agent_bridge",
    "agent-bridge",
)

#: 外部插件派发的 Agent CLI 清单（数据驱动）。
#:
#: v4.1.0：这三张表改由 :mod:`src.agents` 从
#: ``src/agents/cloud_agents.json`` **动态加载**，
#: 不再在本文件里硬编码 ——
#: 新增一个 Agent 只改数据文件，**不需要动本模块一行代码**。
#:
#: 数据来源：插件 ``bridge/registry.py`` 的 ``AGENT_SPECS``
#: 与 ``providers/*/provider.toml`` 的 ``binary`` 字段。
#:
#: ⚠️ 清单缺失或损坏时**降级成空表**而不是抛异常 ——
#: 本模块是叶子模块，绝不能因为一个数据文件出问题就挡住审查（红线 R5）。
_CLOUD_AGENTS = load_agents(strict=False)

#: 外部插件派发的 Agent CLI 的可执行名。
CLOUD_AGENT_BINARIES: Tuple[str, ...] = binary_table(_CLOUD_AGENTS)

#: 规范 id → 中文展示名（id 与 :data:`CLOUD_AGENT_BINARIES` 一一对应）。
CLOUD_AGENT_LABELS: Dict[str, str] = label_table(_CLOUD_AGENTS)

#: 别名 → 规范 id。
#:
#: ⚠️ 别名**必须**归一到规范 id —— 面板与派发审计里用的是 id，
#: 拿别名去比对永远对不上。
#:
#: v4.1.0 补齐了数据化 5 个 Agent 的别名（``cmdc`` / ``gcli`` /
#: ``stepcode`` / ``pi-coding-agent`` 等共 8 条）——
#: 此前这里只有内置 9 个 Agent 的别名，新 Agent 一律认不出来。
CLOUD_AGENT_ALIASES: Dict[str, str] = alias_table(_CLOUD_AGENTS)

#: 规范 id → 已知会话日志目录（v4.3.0）。
#:
#: ⚠️ 为什么需要它 —— Agent CLI 常常**不往自己的 cwd 写日志**：
#: Claude Code 写 ``~/.claude/projects/<项目slug>/``，
#: Codex 写 ``~/.codex/sessions/<年>/<月>/<日>/``。
#: 雷达原先只在 cwd 下找 ``.jsonl`` / ``.log``，找不到就把进程
#: 标成「疑似挂起」—— **假阳性**。v4.2.0 把阈值降到 10 秒后
#: 这个假阳性变得很显眼，必须解决。
#:
#: 只有**有依据**的目录才登记（见清单里的 ``log_dirs_note``）。
CLOUD_AGENT_LOG_DIRS: Dict[str, Tuple[str, ...]] = log_dir_table(
    _CLOUD_AGENTS
)

#: 命令行命中时的置信度。
CONFIDENCE_CMDLINE: float = 0.95

#: 只有工作目录命中时的置信度（**弱**关联）。
CONFIDENCE_CWD: float = 0.60

#: 单次扫描最多认几个宿主。
MAX_HOSTS: int = 8

#: 单棵进程树最多往下走几层。
MAX_TREE_DEPTH: int = 6

#: 进程的三个状态。
STATE_DISCOVERED: str = "discovered"
"""已发现、还没挂载。"""

STATE_ATTACHED: str = "attached"
"""已经挂载进哨兵的会话审查。"""

STATE_UNAVAILABLE: str = "unavailable"
"""进程已经退出（还在，但 PID 已经不在了）。"""

#: 判定「可执行名」时要剥掉的扩展名。
_EXE_SUFFIXES: Tuple[str, ...] = (
    ".exe",
    ".cmd",
    ".bat",
    ".com",
    ".ps1",
    ".js",
    ".mjs",
    ".cjs",
    ".py",
    ".sh",
    ".jar",
    ".vbs",
)

#: 命令行分词（保留引号内的空格）。
_TOKEN_RE = re.compile(r"\"[^\"]*\"|'[^']*'|\S+")

#: 从文本里切出的「词」（只认 ASCII 字母数字）。
_WORD_RE = re.compile(r"[a-z0-9]+")

#: 路径片段匹配允许的**最短**名字长度。
#:
#: ⚠️ ``pi`` / ``omp`` / ``dsh`` 太短 —— 拿它们做子串匹配会命中
#: ``pipe`` / ``compile`` / ``stepping`` 一类的路径，纯属噪音。
#: 这几个只认「可执行名完全相等」与「单词完全相等」。
_MIN_PATH_MATCH_LEN: int = 4

#: 父链 / 深度计算的防环上限。
_CHAIN_GUARD: int = 64


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------
@dataclass
class ProcessInfo:
    """一个进程的基本身份。

    :param pid: 进程 PID。
    :param parent_pid: 父进程 PID（读不到就是 0）。
    :param name: 可执行文件名（如 ``node.exe``）。
    :param exe: 可执行文件完整路径。
    :param cmdline: 完整命令行（拼成一行）。
    :param cwd: 进程当前工作目录。
    :param depth: 相对宿主进程的层数（直接子进程是 1）。
    """

    pid: int = 0
    parent_pid: int = 0
    name: str = ""
    exe: str = ""
    cmdline: str = ""
    cwd: str = ""
    depth: int = 0

    def to_dict(self) -> Dict[str, Any]:
        """转成普通字典（便于直接 JSON 化）。

        :return: 可 ``json.dumps`` 的字典。
        """
        return {
            "pid": int(self.pid),
            "parent_pid": int(self.parent_pid),
            "name": str(self.name),
            "exe": str(self.exe),
            "cmdline": str(self.cmdline),
            "cwd": str(self.cwd),
            "depth": int(self.depth),
        }


@dataclass
class DiscoveredAgentProcess:
    """一个被发现的外部插件 Agent CLI 进程。

    :param pid: 进程 PID。
    :param parent_pid: 父进程 PID。
    :param host_pid: 派发它的桥接宿主 PID。
    :param depth: 相对宿主的层数（直接子进程是 1）。
    :param agent: 规范 Agent id（``opencode`` …）；认不出是空串。
    :param agent_label: 中文展示名。
    :param exe: 可执行文件路径。
    :param cmdline: 完整命令行。
    :param cwd: 进程当前工作目录。
    :param confidence: 关联置信度（0.0 ~ 1.0）。
    :param log_path: 关联到的日志文件路径；关联不上是空串。
    :param attachable: 敢不敢让用户去武装守护者（红线 R7）。
    :param state: 进程状态（:data:`STATE_DISCOVERED` 等三态）。
    :param matched_by: 靠什么认出来的（``cmdline_name`` / ``cwd_path`` …）。
    :param started_at: 进程创建时刻（UNIX 时间戳）；读不到是 0.0。
    """

    pid: int = 0
    parent_pid: int = 0
    host_pid: int = 0
    depth: int = 0
    agent: str = ""
    agent_label: str = ""
    exe: str = ""
    cmdline: str = ""
    cwd: str = ""
    confidence: float = 0.0
    log_path: str = ""
    attachable: bool = False
    state: str = STATE_DISCOVERED
    matched_by: str = ""
    started_at: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        """转成普通字典（便于直接 JSON 化）。

        :return: 可 ``json.dumps`` 的字典。
        """
        return {
            "pid": int(self.pid),
            "parent_pid": int(self.parent_pid),
            "host_pid": int(self.host_pid),
            "depth": int(self.depth),
            "agent": str(self.agent),
            "agent_label": str(self.agent_label),
            "exe": str(self.exe),
            "cmdline": str(self.cmdline),
            "cwd": str(self.cwd),
            "confidence": float(self.confidence),
            "log_path": str(self.log_path),
            "attachable": bool(self.attachable),
            "state": str(self.state),
            "matched_by": str(self.matched_by),
            "started_at": float(self.started_at),
        }


# ---------------------------------------------------------------------------
# 宿主识别
# ---------------------------------------------------------------------------
def find_bridge_hosts(
    deadline: Optional[float] = None,
) -> List[ProcessInfo]:
    """找出系统里正在跑的**外部插件桥接宿主**进程。

    判定条件（满足其一即可）：

    1. 命令行命中 :data:`BRIDGE_HOST_CMDLINE_MARKERS`；
    2. 工作目录命中 :data:`BRIDGE_HOST_DIR_MARKERS`。

    ⚠️ 哨兵自己与它的父进程会被排除（:func:`src.procscan.protected_pids`）
    —— 把宿主认成自己，就会去挂起哨兵自己。

    ⚠️ **嵌套宿主会被丢弃** —— 宿主自己派出来的子进程如果命令行里
    带了插件路径，也会被特征命中；那是宿主的后代，不是宿主。

    psutil 不可用 / 枚举失败 → 返回空列表（绝不抛异常）。

    :param deadline: :func:`time.monotonic` 时间戳形式的**截止时刻**；
        枚举过程中一旦超过它就**立刻收手**，返回已经认出的那部分宿主。
        为 ``None`` 表示不限时（扫到完为止）。
    :return: 宿主进程列表；一个都没有就是空列表。
    """
    ps = _psutil_module()
    if ps is None:
        logger.debug("psutil 不可用，外部插件宿主探测整体降级。")
        return []

    excluded = procscan.protected_pids()
    hosts: List[ProcessInfo] = []
    parents: Dict[int, int] = {}

    try:
        iterator = ps.process_iter(
            ["pid", "ppid", "name", "exe", "cmdline"]
        )
    except Exception as exc:  # noqa: BLE001 —— 枚举失败只降级
        logger.debug("进程枚举失败：%s: %s", type(exc).__name__, exc)
        return []

    for proc in iterator:
        # ⚠️ v4.0.1：**超时熔断** —— Windows 上第一次枚举几百个进程
        #    要 2~3 秒（逐个开句柄）。超过截止时刻就收手，
        #    把已经认出的宿主交出去，绝不把调用方一起拖死。
        if deadline is not None and time.monotonic() > deadline:
            logger.info(
                "外部插件宿主枚举超时收手：已认出 %s 个宿主。", len(hosts)
            )
            break
        # ⚠️ **先不读 cwd** —— Windows 上读每个进程的工作目录要
        #    3~4 秒（全量枚举实测），而绝大多数进程根本不是宿主。
        #    只有命令行认不出来时，才**单独**去读那一个进程的 cwd。
        info = _describe_process(proc, with_cwd=False)
        if not info.pid:
            continue
        parents[info.pid] = info.parent_pid
        if info.pid in excluded:
            continue
        if not _host_by_cmdline(info.cmdline):
            info.cwd = _safe_cwd(proc)
            if not _host_by_cwd(info.cwd):
                continue
        hosts.append(info)

    return _drop_nested_hosts(hosts, parents)


def _host_by_cmdline(cmdline: str) -> bool:
    """命令行像不像外部插件桥接宿主。

    :param cmdline: 进程命令行。
    :return: 像就是 ``True``。
    """
    return bool(
        procscan.detect_custom_agent(
            cmdline, "", BRIDGE_HOST_CMDLINE_MARKERS
        )
    )


def _host_by_cwd(cwd: str) -> bool:
    """工作目录像不像外部插件桥接宿主。

    :param cwd: 进程工作目录。
    :return: 像就是 ``True``。
    """
    return bool(
        procscan.detect_custom_agent(
            "", cwd, BRIDGE_HOST_DIR_MARKERS
        )
    )


def _is_bridge_host(cmdline: str, cwd: str) -> bool:
    """这个进程像不像外部插件桥接宿主。

    :param cmdline: 进程命令行。
    :param cwd: 进程工作目录。
    :return: 像就是 ``True``。
    """
    return bool(_host_by_cmdline(cmdline) or _host_by_cwd(cwd))


def _drop_nested_hosts(
    hosts: Sequence[ProcessInfo],
    parents: Dict[int, int],
) -> List[ProcessInfo]:
    """丢掉「另一个宿主的子孙」这种伪宿主。

    :param hosts: 候选宿主。
    :param parents: ``pid → 父 pid`` 映射。
    :return: 去掉嵌套之后的宿主列表。
    """
    pid_set = {host.pid for host in hosts}
    kept: List[ProcessInfo] = []
    for host in hosts:
        if _has_ancestor_in(host.pid, parents, pid_set):
            logger.debug(
                "进程 %s 是另一个宿主的子孙，不当作宿主。", host.pid
            )
            continue
        kept.append(host)
    return kept


def _has_ancestor_in(
    pid: int,
    parents: Dict[int, int],
    pid_set: set,
) -> bool:
    """这个进程的祖先里有没有另一个宿主。

    :param pid: 起点 PID。
    :param parents: ``pid → 父 pid`` 映射。
    :param pid_set: 宿主 PID 集合。
    :return: 有就是 ``True``。
    """
    current = pid
    for _ in range(_CHAIN_GUARD):
        parent = parents.get(current, 0)
        if not parent or parent == current:
            return False
        if parent in pid_set:
            return True
        current = parent
    return False


# ---------------------------------------------------------------------------
# 进程树枚举
# ---------------------------------------------------------------------------
def enumerate_descendants(
    pid: int,
    *,
    max_depth: int = MAX_TREE_DEPTH,
) -> List[ProcessInfo]:
    """递归枚举某个进程的**全部子孙**进程。

    ⚠️ psutil 的 ``children(recursive=True)`` 在 Windows 上会抛
    :class:`AccessDenied`（权限不足）或 :class:`NoSuchProcess`
    （进程刚退出）。这两种情况**都不能让整棵树失败**：

    * 递归枚举整体失败 → 降级成**逐层**枚举（每层用非递归
      ``children()``），**逐个**降级 —— 一个分支被拒，其余分支照走；
    * 某一层的 ``children()`` 失败 → 只丢这一支，不影响别的分支。

    psutil 不可用 / 进程不存在 → 返回空列表。

    :param pid: 根进程 PID。
    :param max_depth: 最多往下走几层（直接子进程是第 1 层）。
    :return: 子孙进程列表（按层数从小到大）。
    """
    ps = _psutil_module()
    if ps is None:
        return []
    try:
        root = ps.Process(int(pid))
    except Exception as exc:  # noqa: BLE001 —— 进程可能已经没了
        logger.debug(
            "取进程 %s 失败：%s: %s", pid, type(exc).__name__, exc
        )
        return []

    limit = max(int(max_depth), 1)
    raw = _recursive_children(root)
    if raw is not None:
        described = _describe_children(root, raw, limit)
        if described:
            return described
    return _layered_descendants(root, limit)


def _recursive_children(root: Any) -> Optional[List[Any]]:
    """一次拿到全部子孙；失败返回 ``None``（交给降级路径）。

    :param root: 根进程对象。
    :return: 子孙进程对象列表；失败就是 ``None``。
    """
    try:
        children = root.children(recursive=True)
    except Exception as exc:  # noqa: BLE001 —— 权限不足 / 进程刚退出
        logger.debug(
            "递归枚举子进程失败，降级为逐层枚举：%s: %s",
            type(exc).__name__,
            exc,
        )
        return None
    return list(children or [])


def _describe_children(
    root: Any,
    raw: Sequence[Any],
    limit: int,
) -> List[ProcessInfo]:
    """把递归枚举的结果算上层数。

    :param root: 根进程对象。
    :param raw: 子孙进程对象列表。
    :param limit: 最大层数。
    :return: 带 ``depth`` 的进程列表。
    """
    root_pid = _pid_of(root)
    parents: Dict[int, int] = {root_pid: root_pid}
    infos: List[ProcessInfo] = []
    for child in raw:
        info = _describe_process(child)
        if not info.pid:
            continue
        parents[info.pid] = info.parent_pid
        infos.append(info)
    for info in infos:
        info.depth = _depth_of(info.pid, parents, root_pid)
    return [info for info in infos if 0 < info.depth <= limit]


def _layered_descendants(root: Any, limit: int) -> List[ProcessInfo]:
    """逐层枚举子孙 —— 每一层、每一个节点**单独**降级。

    :param root: 根进程对象。
    :param limit: 最大层数。
    :return: 带 ``depth`` 的进程列表。
    """
    result: List[ProcessInfo] = []
    root_pid = _pid_of(root)
    seen = {root_pid}
    queue: List[Tuple[Any, int]] = [(root, 0)]
    while queue:
        proc, depth = queue.pop(0)
        if depth >= limit:
            continue
        for child in _safe_children(proc):
            info = _describe_process(child)
            if not info.pid or info.pid in seen:
                continue
            seen.add(info.pid)
            info.depth = depth + 1
            result.append(info)
            queue.append((child, depth + 1))
    return result


def _safe_children(proc: Any) -> List[Any]:
    """取直接子进程；读不到就当这一支没有子进程。

    :param proc: 父进程对象。
    :return: 直接子进程对象列表；读不到就是空列表。
    """
    try:
        children = proc.children()
    except Exception as exc:  # noqa: BLE001 —— 权限不足是常态
        logger.debug(
            "枚举进程 %s 的子进程失败，只丢这一支：%s: %s",
            _pid_of(proc),
            type(exc).__name__,
            exc,
        )
        return []
    return list(children or [])


def _depth_of(
    pid: int,
    parents: Dict[int, int],
    root_pid: int,
) -> int:
    """算某个进程相对根进程的层数。

    :param pid: 目标 PID。
    :param parents: ``pid → 父 pid`` 映射。
    :param root_pid: 根进程 PID。
    :return: 层数（直接子进程是 1）；算不出来就是 1。
    """
    chain = [pid]
    current = pid
    for _ in range(_CHAIN_GUARD):
        parent = parents.get(current, 0)
        if not parent or parent == current:
            break
        if parent == root_pid:
            chain.append(parent)
            break
        if parent not in parents:
            break
        chain.append(parent)
        current = parent
    return max(len(chain) - 1, 1)


# ---------------------------------------------------------------------------
# 特征匹配
# ---------------------------------------------------------------------------
def match_agent(cmdline: str, cwd: str = "") -> Tuple[str, str, float]:
    """从命令行 / 工作目录认出这是**哪个**外部插件 Agent CLI。

    判定顺序（先强后弱）：

    1. **别名**（``oc`` → ``opencode``）—— 命令行里出现即认；
    2. **可执行名** —— 去掉目录与扩展名后**完全相等**
       （``opencode.cmd`` / ``pi.js`` / ``command-code.cmd`` …）；
    3. **单词** —— 命令行里出现一个**独立**的单词（``omp`` …）；
    4. **路径片段** —— 只认长度 ≥ 4 的名字，避免 ``pi`` 命中 ``pipe``。

    ⚠️ ``node.exe + 脚本`` 形态由第 2 / 4 条接住 ——
    脚本名 ``pi.js`` 认成 ``pi``，路径 ``.../opencode/bin/cli.js``
    认成 ``opencode``。

    ⚠️ 命令行里**带引号的自然语言参数**（用户下的那句 Prompt）
    会被剔除再匹配 —— 否则 ``opencode run "把 oh-my-pi 修一下"``
    会被里面的别名带偏。

    :param cmdline: 进程命令行。
    :param cwd: 进程工作目录（只在命令行认不出时才用）。
    :return: ``(规范 id, 中文展示名, 置信度)``；认不出是
        ``("", "", 0.0)``。
    """
    agent, label, confidence, _by = _match_agent_detail(cmdline, cwd)
    return (agent, label, confidence)


def _match_agent_detail(
    cmdline: str,
    cwd: str,
) -> Tuple[str, str, float, str]:
    """匹配 Agent 身份，并额外给出「靠什么认出来的」。

    :param cmdline: 进程命令行。
    :param cwd: 进程工作目录。
    :return: ``(规范 id, 中文展示名, 置信度, 命中方式)``。
    """
    tokens = _tokenize(cmdline)
    effective = [item for item in tokens if not _is_prompt_token(item)]
    hit = _match_tokens(effective)
    if hit[0]:
        return (hit[0], hit[1], CONFIDENCE_CMDLINE, f"cmdline_{hit[2]}")
    hit = _match_tokens(_tokenize(cwd))
    if hit[0]:
        return (hit[0], hit[1], CONFIDENCE_CWD, f"cwd_{hit[2]}")
    return ("", "", 0.0, "")


def _match_tokens(tokens: Sequence[str]) -> Tuple[str, str, str]:
    """在一组 token 里找 Agent 身份。

    :param tokens: 命令行 / 路径切出来的 token。
    :return: ``(规范 id, 中文展示名, 命中方式)``；认不出是
        ``("", "", "")``。
    """
    lowered = " ".join(tokens).lower()
    if not lowered.strip():
        return ("", "", "")

    # --- 1) 别名（最长优先 —— "oh-my-pi" 比 "pi" 具体）---
    best_alias = ""
    best_pos = -1
    for alias, canonical in CLOUD_AGENT_ALIASES.items():
        pos = _boundary_find(lowered, alias)
        if pos < 0:
            continue
        longer = len(alias) > len(best_alias)
        earlier = len(alias) == len(best_alias) and 0 <= pos < best_pos
        if not best_alias or longer or earlier:
            best_alias = alias
            best_pos = pos
    if best_alias:
        canonical = CLOUD_AGENT_ALIASES[best_alias]
        return (
            canonical,
            CLOUD_AGENT_LABELS.get(canonical, canonical),
            "alias",
        )

    # --- 2) 可执行名（去目录、去扩展名后完全相等）---
    for token in tokens:
        stem = _executable_stem(token)
        if stem in CLOUD_AGENT_BINARIES:
            return (stem, CLOUD_AGENT_LABELS.get(stem, stem), "name")

    # --- 3) 独立单词 ---
    words = set(_WORD_RE.findall(lowered))
    for binary in CLOUD_AGENT_BINARIES:
        if binary in words:
            return (binary, CLOUD_AGENT_LABELS.get(binary, binary), "word")

    # --- 4) 路径片段（只认足够长的名字）---
    for binary in sorted(CLOUD_AGENT_BINARIES, key=len, reverse=True):
        if len(binary) < _MIN_PATH_MATCH_LEN:
            continue
        if _boundary_find(lowered, binary) >= 0:
            return (
                binary,
                CLOUD_AGENT_LABELS.get(binary, binary),
                "path",
            )
    return ("", "", "")


def _tokenize(text: str) -> List[str]:
    """把一段文本切成 token（引号内的空格不拆）。

    :param text: 原始文本。
    :return: token 列表。
    """
    return _TOKEN_RE.findall(str(text or ""))


def _is_prompt_token(token: str) -> bool:
    """这个 token 是不是用户下的一句 Prompt。

    :param token: 单个 token。
    :return: 是 Prompt 就是 ``True``。
    """
    text = str(token or "").strip()
    if len(text) < 2:
        return False
    if (text[0] == '"' and text[-1] == '"') or (
        text[0] == "'" and text[-1] == "'"
    ):
        inner = text[1:-1]
    else:
        return False
    if not inner.strip():
        return True
    if "/" in inner or "\\" in inner:
        return False
    if inner.lower().endswith(_EXE_SUFFIXES):
        return False
    return " " in inner or not inner.isascii()


def _executable_stem(token: str) -> str:
    """取 token 的「可执行名」（去目录、去扩展名）。

    :param token: 单个 token。
    :return: 小写的可执行名；取不到是空串。
    """
    text = str(token or "").strip().strip('"').strip("'")
    if not text:
        return ""
    base = re.split(r"[\\/]", text)[-1]
    stem, ext = os.path.splitext(base)
    if ext.lower() in _EXE_SUFFIXES:
        return stem.lower()
    return base.lower()


def _boundary_find(text: str, needle: str) -> int:
    """在文本里找 ``needle``，且左右必须是「非字母数字」。

    ⚠️ 这一条边界检查是防噪音的关键：``oc`` 不能命中 ``opencode``，
    ``atom`` 不能命中 ``atomcode``。

    :param text: 被搜索的文本（小写）。
    :param needle: 要找的片段（小写）。
    :return: 命中的起始下标；没命中是 ``-1``。
    """
    if not text or not needle:
        return -1
    start = 0
    while True:
        pos = text.find(needle, start)
        if pos < 0:
            return -1
        before = text[pos - 1] if pos > 0 else ""
        after_at = pos + len(needle)
        after = text[after_at] if after_at < len(text) else ""
        if not _is_word_char(before) and not _is_word_char(after):
            return pos
        start = pos + 1


def _is_word_char(char: str) -> bool:
    """这个字符算不算「词内字符」。

    :param char: 单个字符（可能为空串）。
    :return: 算就是 ``True``。
    """
    return bool(char) and char.isalnum()


# ---------------------------------------------------------------------------
# 日志关联
# ---------------------------------------------------------------------------
#: 日志目录兜底查找的**最大递归深度**（v4.3.0）。
#:
#: Codex 的会话日志在 ``~/.codex/sessions/<年>/<月>/<日>/<会话>.jsonl``
#: —— 相对登记的根目录是 **4 层**；Claude Code 是 2 层
#: （``~/.claude/projects/<项目slug>/<会话>.jsonl``）。
LOG_DIR_MAX_DEPTH: int = 4

#: 单个日志目录**最多看多少个目录项**（v4.3.0）。
#:
#: 递归扫用户家目录是有代价的 —— 装了几个月 Agent 的机器上，
#: ``~/.claude/projects`` 下可能有几百个项目目录。
#: 超出预算就收手：宁可少找到一个，也不能把雷达拖慢。
LOG_DIR_MAX_ENTRIES: int = 4000

#: 递归查找时**跳过**的目录名（v4.3.0）。
#:
#: 隐藏目录（``.git`` 等）与 ``node_modules`` 里没有会话日志，
#: 扫进去纯属浪费预算。
LOG_DIR_SKIP_DIRS: Tuple[str, ...] = ("node_modules",)


def _compressed_suffixes() -> Tuple[str, ...]:
    """压缩日志的后缀（从 :mod:`src.radar` 取，**单一事实来源**）。

    :return: 压缩后缀元组。
    """
    from src.radar import COMPRESSED_SUFFIXES

    return COMPRESSED_SUFFIXES


def _scan_log_dir(
    root: str,
    wanted: str,
    window: float,
    reference: float,
) -> str:
    """在**一个**目录里找最近修改过的日志文件（**只看一层**）。

    :param root: 目录。
    :param wanted: 优先挑名字里带它的关键词（小写）。
    :param window: 时间窗（秒）。
    :param reference: 「现在」的 UNIX 时间戳。
    :return: 日志文件路径；找不到是空串。
    """
    if not root:
        return ""
    try:
        scanner = os.scandir(root)
    except (OSError, ValueError) as exc:
        logger.debug(
            "扫描目录 %s 失败：%s: %s", root, type(exc).__name__, exc
        )
        return ""

    best_path = ""
    best_key = (0, -1.0)
    compressed = _compressed_suffixes()
    with scanner:
        for entry in scanner:
            name = entry.name.lower()
            if not name.endswith(".jsonl") and not name.endswith(".log"):
                continue
            # 压缩日志**不能接入**实时审查（跟读器记的是原始字节偏移，
            # 压缩流没法按字节增量读）。挂载接口会明确拒绝它们 ——
            # 这里就不该把徽章标成「可接入」。
            if name.endswith(compressed):
                continue
            try:
                if not entry.is_file():
                    continue
                mtime = entry.stat().st_mtime
            except OSError as exc:
                logger.debug("读取 %s 的属性失败：%s", entry.path, exc)
                continue
            if reference - mtime > window:
                continue
            named = 1 if wanted and wanted in name else 0
            key = (named, mtime)
            if key > best_key:
                best_key = key
                best_path = entry.path
    return best_path


def _scan_log_tree(
    root: str,
    wanted: str,
    window: float,
    reference: float,
) -> str:
    """在**已知日志目录**里递归找最近修改过的日志文件（v4.3.0）。

    为什么要递归：Claude Code 的会话日志是
    ``~/.claude/projects/<项目slug>/<会话>.jsonl`` ——
    文件在**根目录下面一层**，只看一层是找不到的。

    :param root: 根目录。
    :param wanted: 优先挑名字里带它的关键词（小写）。
    :param window: 时间窗（秒）。
    :param reference: 「现在」的 UNIX 时间戳。
    :return: 日志文件路径；找不到是空串。
    """
    if not root or not os.path.isdir(root):
        return ""
    compressed = _compressed_suffixes()
    best_path = ""
    best_key = (0, -1.0)
    budget = LOG_DIR_MAX_ENTRIES

    for dirpath, dirnames, filenames in os.walk(root, topdown=True):
        # 原地裁剪子目录清单 —— ``os.walk`` 认这个。
        dirnames[:] = [
            name
            for name in dirnames
            if not name.startswith(".") and name not in LOG_DIR_SKIP_DIRS
        ]
        depth = dirpath[len(root):].count(os.sep)
        if depth >= LOG_DIR_MAX_DEPTH:
            dirnames[:] = []

        for filename in filenames:
            budget -= 1
            if budget <= 0:
                logger.debug(
                    "日志目录 %s 扫到条目预算上限，收手", root
                )
                return best_path
            name = filename.lower()
            if not name.endswith(".jsonl") and not name.endswith(".log"):
                continue
            if name.endswith(compressed):
                continue
            path = os.path.join(dirpath, filename)
            try:
                mtime = os.path.getmtime(path)
            except OSError as exc:
                logger.debug("读取 %s 的修改时间失败：%s", path, exc)
                continue
            if reference - mtime > window:
                continue
            named = 1 if wanted and wanted in name else 0
            key = (named, mtime)
            if key > best_key:
                best_key = key
                best_path = path
    return best_path


def find_log_for_process(
    cwd: str,
    agent: str = "",
    *,
    max_age_seconds: float = 3600.0,
    now: Optional[float] = None,
    log_dirs: Sequence[str] = (),
) -> str:
    """找这个进程**正在写的**日志文件。

    查找顺序（v4.3.0）：

    1. **先看 cwd** —— Agent 直接在自己工作目录里写日志的情况最常见；
    2. **cwd 下没有，再看该 Agent 登记的 ``log_dirs``** ——
       Claude Code 写 ``~/.claude/projects/``、Codex 写
       ``~/.codex/sessions/``，**都不在 cwd 里**。
       不查这一步，雷达会把它们**永远**标成「疑似挂起」（假阳性）。

    候选条件：

    * 扩展名是 ``.jsonl`` 或 ``.log``；
    * **不是压缩日志**（``.zstd``）—— 压缩流没法按字节增量跟读，
      挂载接口会明确拒绝，徽章就不该标「可接入」；
    * **真的存在**，而且是**普通文件**（目录不算）；
    * 修改时间落在 ``max_age_seconds`` 的时间窗内。

    ⚠️ 找不到就是空串 —— **绝不**返回猜测出来的路径。
    返回空串的进程 :attr:`DiscoveredAgentProcess.attachable` 就是
    ``False``，用户也就没机会去武装一个没确认过的守护者（红线 R7）。

    :param cwd: 进程工作目录。
    :param agent: 已识别出的 Agent id；只用来**优先**挑名字里带它的文件。
    :param max_age_seconds: 时间窗（秒）。
    :param now: 「现在」的 UNIX 时间戳；不给就用系统时间。
    :param log_dirs: 该 Agent 的**已知会话日志目录**（兜底查找用）。
    :return: 日志文件路径；找不到是空串。
    """
    try:
        reference = time.time() if now is None else float(now)
    except (TypeError, ValueError):
        reference = time.time()
    window = max(float(max_age_seconds), 0.0)
    wanted = str(agent or "").strip().lower()

    root = str(cwd or "").strip()
    if root:
        found = _scan_log_dir(root, wanted, window, reference)
        if found:
            return found

    for raw_dir in log_dirs or ():
        text = str(raw_dir or "").strip()
        if not text:
            continue
        try:
            expanded = os.path.expanduser(text)
        except (OSError, ValueError) as exc:
            logger.debug("展开日志目录 %s 失败：%s", text, exc)
            continue
        found = _scan_log_tree(expanded, wanted, window, reference)
        if found:
            logger.debug(
                "在已知日志目录 %s 里找到日志：%s", expanded, found
            )
            return found
    return ""


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def scan_process_tree(
    *,
    max_hosts: int = MAX_HOSTS,
    max_depth: int = MAX_TREE_DEPTH,
    now: Optional[float] = None,
    deadline: Optional[float] = None,
    on_progress: Optional[Callable[[List[DiscoveredAgentProcess]], None]] = None,
) -> List[DiscoveredAgentProcess]:
    """扫描「外部插件派发进程树」，找出宿主派发出来的 Agent CLI 进程。

    流程：找宿主 → 枚举子孙 → 逐个认 Agent → 关联日志 → 组装结果。

    ⚠️ **绝不抛异常** —— 审查侧绝不因为探测失败而中断（红线 R5）。
    psutil 不可用时返回空列表。

    :param max_hosts: 最多认几个宿主。
    :param max_depth: 每棵进程树最多往下走几层。
    :param now: 「现在」的 UNIX 时间戳（给日志时间窗用）。
    :param deadline: :func:`time.monotonic` 时间戳形式的**截止时刻**；
        超过它就**立刻收手**，返回已经认出的那部分结果。
        为 ``None`` 表示不限时。
    :param on_progress: 进度回调 —— 每认出一个新进程就调用一次，
        参数是**到目前为止的全部结果**（拷贝）。为 ``None`` 表示不回调。
    :return: 发现结果列表；一个都没有就是空列表。
    """
    ps = _psutil_module()
    if ps is None:
        logger.debug("psutil 不可用，外部插件派发进程树探测整体降级。")
        return []

    limit_hosts = max(int(max_hosts), 1)
    limit_depth = max(int(max_depth), 1)

    results: List[DiscoveredAgentProcess] = []
    try:
        hosts = find_bridge_hosts(deadline=deadline)
    except Exception as exc:  # noqa: BLE001 —— 审查侧绝不中断
        logger.debug("宿主探测失败：%s: %s", type(exc).__name__, exc)
        return []

    for host in hosts[:limit_hosts]:
        # ⚠️ v4.0.1：**超时熔断** —— 宿主认完了但时间不够了，
        #    就把已经有的部分交出去，别把调用方一起拖死。
        if deadline is not None and time.monotonic() > deadline:
            logger.info(
                "进程树扫描超时收手：已认出 %s 个 Agent 进程。", len(results)
            )
            break
        try:
            descendants = enumerate_descendants(
                host.pid, max_depth=limit_depth
            )
        except Exception as exc:  # noqa: BLE001 —— 审查侧绝不中断
            logger.debug(
                "枚举宿主 %s 的子孙失败：%s: %s",
                host.pid,
                type(exc).__name__,
                exc,
            )
            continue
        for info in descendants:
            item = _build_discovered(host, info, now=now)
            if item is not None:
                results.append(item)
                if on_progress is not None:
                    # ⚠️ 回调里可能抛异常 —— 进度通知失败**绝不能**
                    #    把整次扫描带崩（红线 R5）。
                    try:
                        on_progress(list(results))
                    except Exception as exc:  # noqa: BLE001
                        logger.debug(
                            "进程树扫描进度回调失败：%s: %s",
                            type(exc).__name__,
                            exc,
                        )
    return results


def _build_discovered(
    host: ProcessInfo,
    info: ProcessInfo,
    *,
    now: Optional[float] = None,
) -> Optional[DiscoveredAgentProcess]:
    """把一个子进程组装成发现结果。

    :param host: 派发它的宿主。
    :param info: 子进程信息。
    :param now: 「现在」的 UNIX 时间戳（给日志时间窗用）。
    :return: 发现结果；认不出 Agent 就是 ``None``。
    """
    agent, label, confidence, matched_by = _match_agent_detail(
        info.cmdline, info.cwd
    )
    if not agent:
        return None
    log_path = find_log_for_process(
        info.cwd,
        agent,
        now=now,
        # v4.3.0：cwd 下找不到就去该 Agent 登记的已知会话日志目录里找。
        # Claude Code 写 ~/.claude/projects/、Codex 写 ~/.codex/sessions/，
        # 都不在 cwd 里 —— 不查这一步，它们会被永远标成「疑似挂起」。
        log_dirs=CLOUD_AGENT_LOG_DIRS.get(agent, ()),
    )
    alive = _pid_alive(info.pid)
    return DiscoveredAgentProcess(
        pid=info.pid,
        parent_pid=info.parent_pid,
        host_pid=host.pid,
        depth=info.depth,
        agent=agent,
        agent_label=label,
        exe=info.exe,
        cmdline=info.cmdline,
        cwd=info.cwd,
        confidence=confidence,
        log_path=log_path,
        attachable=bool(log_path),
        state=STATE_DISCOVERED if alive else STATE_UNAVAILABLE,
        matched_by=matched_by,
        started_at=_process_start_time(info.pid),
    )


# ---------------------------------------------------------------------------
# 导出辅助
# ---------------------------------------------------------------------------
def summarize_tree(processes: Sequence[DiscoveredAgentProcess]) -> str:
    """把发现结果渲染成给人看的摘要。

    :param processes: 发现结果列表。
    :return: 每行一个进程的多行文本；没有结果就是空串。
    """
    lines: List[str] = []
    for item in processes or ():
        agent = item.agent or "-"
        label = item.agent_label or ""
        mark = "可挂载" if item.attachable else "不可挂载"
        log_path = item.log_path or "-"
        lines.append(
            f"[host {item.host_pid}] pid={item.pid} "
            f"depth={item.depth} agent={agent}（{label}） "
            f"置信度={item.confidence:.2f} {mark} 日志={log_path}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 内部实现
# ---------------------------------------------------------------------------
def _psutil_module() -> Optional[Any]:
    """拿到 psutil 模块；没装就返回 ``None``。

    :return: psutil 模块；不可用就是 ``None``。
    """
    return psutil


def _process_start_time(pid: int) -> float:
    """取进程的创建时刻（UNIX 时间戳）。

    v4.1.0：用来判「进程活了多久却一直没输出」——
    这是外部插件面板「慢/待验证」状态的数据来源。

    ⚠️ 只在**已认出的 Agent 进程**上调用（数量很少），
    绝不对全量进程枚举调用 —— Windows 上逐个打开进程句柄极贵
    （v4.0.1 实测 345 个进程要 2.8 秒）。

    :param pid: 进程 PID。
    :return: 创建时刻；读不到就是 ``0.0``。
    """
    ps = _psutil_module()
    if ps is None:
        return 0.0
    try:
        return float(ps.Process(int(pid)).create_time())
    except Exception as exc:  # noqa: BLE001 —— 权限不足 / 进程刚退出
        logger.debug(
            "读进程 %s 的创建时刻失败：%s: %s",
            pid,
            type(exc).__name__,
            exc,
        )
        return 0.0


def _pid_alive(pid: int) -> bool:
    """某个 PID 现在还活着吗。

    ⚠️ 这里**不用** :func:`src.procscan.pid_alive` —— 它内部自己
    ``import psutil``，本模块的测试就没法把 psutil 换掉。
    读不到就当它没了（保守：宁可说它不在，也不假装它还在）。

    :param pid: 进程 PID。
    :return: 活着就是 ``True``。
    """
    ps = _psutil_module()
    if ps is None:
        return False
    try:
        return bool(ps.pid_exists(int(pid)))
    except Exception as exc:  # noqa: BLE001 —— 读不到就当它没了
        logger.debug(
            "检查进程 %s 是否存活失败：%s: %s",
            pid,
            type(exc).__name__,
            exc,
        )
        return False


def _pid_of(proc: Any) -> int:
    """取进程对象的 PID。

    :param proc: psutil 进程对象。
    :return: PID；取不到就是 0。
    """
    try:
        return int(getattr(proc, "pid", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _describe_process(
    proc: Any,
    *,
    depth: int = 0,
    with_cwd: bool = True,
) -> ProcessInfo:
    """把一个 psutil 进程对象读成 :class:`ProcessInfo`。

    ⚠️ ``process_iter`` 给的进程带 ``info`` 缓存；``children()`` 给的
    进程**没有** —— 两种来源都要能读。

    :param proc: psutil 进程对象。
    :param depth: 预设层数。
    :param with_cwd: 是否读工作目录（Windows 上很贵，按需再读）。
    :return: 进程信息。
    """
    raw_info = getattr(proc, "info", None)
    info = raw_info if isinstance(raw_info, dict) else {}

    pid = info.get("pid")
    if pid is None:
        pid = getattr(proc, "pid", 0)
    parent_pid = info.get("ppid")
    if parent_pid is None:
        parent_pid = _call_proc(proc, "ppid", 0)

    cmdline_raw = info.get("cmdline")
    if cmdline_raw is None:
        cmdline_raw = _call_proc(proc, "cmdline", [])
    parts = [str(part) for part in (cmdline_raw or [])]

    name = info.get("name")
    if name is None:
        name = _call_proc(proc, "name", "")
    exe = info.get("exe")
    if exe is None:
        exe = _call_proc(proc, "exe", "")

    return ProcessInfo(
        pid=_as_int(pid),
        parent_pid=_as_int(parent_pid),
        name=str(name or ""),
        exe=str(exe or ""),
        cmdline=" ".join(parts),
        cwd=_safe_cwd(proc) if with_cwd else "",
        depth=int(depth),
    )


def _call_proc(proc: Any, method: str, default: Any) -> Any:
    """调进程对象的某个方法；失败返回默认值。

    :param proc: psutil 进程对象。
    :param method: 方法名。
    :param default: 失败时的返回值。
    :return: 方法的返回值；失败就是 ``default``。
    """
    getter = getattr(proc, method, None)
    if getter is None:
        return default
    try:
        value = getter()
    except Exception as exc:  # noqa: BLE001 —— 权限不足 / 进程刚退出
        logger.debug(
            "读取进程 %s 的 %s 失败：%s: %s",
            _pid_of(proc),
            method,
            type(exc).__name__,
            exc,
        )
        return default
    return default if value is None else value


def _safe_cwd(proc: Any) -> str:
    """读进程的工作目录；读不到就是空串。

    :param proc: psutil 进程对象。
    :return: 工作目录；读不到就是空串。
    """
    getter = getattr(proc, "cwd", None)
    if getter is None:
        return ""
    try:
        return str(getter() or "")
    except Exception as exc:  # noqa: BLE001 —— 权限不足是常态
        logger.debug(
            "读取进程 %s 的工作目录失败：%s: %s",
            _pid_of(proc),
            type(exc).__name__,
            exc,
        )
        return ""


def _as_int(value: Any) -> int:
    """把任意值转成 int；转不了就是 0。

    :param value: 原始值。
    :return: 整数；转不了就是 0。
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
