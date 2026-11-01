"""雷达 —— 发现用户**已经在终端里跑着**的 AI CLI Agent 会话日志。

背景
====

哨兵最初的架构要求「用户必须通过哨兵来启动 Agent」：
哨兵自己拉起进程、自己接管 stdout、自己解析事件流。
这条路很干净，但门槛太高 —— 用户在别的终端里已经开了
``codex`` / ``mcode`` / ``claude``，哨兵就**完全看不见**。

为什么不直接去接管已运行进程的 stdout
======================================

在操作系统层面**做不到**：一个进程的 stdout 在启动那一刻就已经
绑定到某个管道 / 终端 / 文件描述符上，外部进程无法在「不重启
该进程」的前提下把它的输出引流到别处。用 ``ptrace`` 或调试器
注入属于侵入式手段，代价与风险都远超收益。

于是走**日志发现**这条路
========================

各大 Agent CLI 都会把会话过程落成**本地日志文件**（Codex 的
``~/.codex/sessions/*.jsonl``、mcode 的 ``~/.mcode/``、
Claude Code 的 ``~/.claude/projects/``）。这些日志是**只读**
可读的：哨兵扫描它们，找出**正在被活跃写入**的那些，就知道
「用户此刻正在用哪个 Agent、干活的工作区在哪」。

本模块因此是一个纯粹的**只读探测器**
====================================

* 从不写入、删除或修改任何被监控的日志；
* 任何单文件读取都有字节上限，任何异常都**降级成 ``None`` /
  空列表**（架构红线 R5：审查侧异常一律降级，绝不抛出去
  挡住哨兵启动）；
* 任何输出都走 ``logging.debug``，本模块**不 print**。

叶子模块
========

本模块**只依赖 Python 标准库**，不 import 项目里任何其它模块
（包括 :mod:`src.adapters` 与 :mod:`src.core.*`）——
它要能被界面层、启动层、审查层任意一方安全地 import，
因此不产生层级倒置。
"""

from __future__ import annotations

import json
import logging
import os
import re
import stat as stat_module
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import (
    Any,
    Callable,
    Dict,
    Iterator,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)

logger = logging.getLogger(__name__)

__all__ = [
    "ACTIVE_AGE_SECONDS",
    "AGENT_KEYS",
    "AGENT_LABELS",
    "COMPRESSED_SUFFIXES",
    "DEFAULT_EXCLUDE_KEYWORDS",
    "DEFAULT_SCAN_ROOTS",
    "WORKSPACE_SOURCES",
    "DiscoveredSession",
    "agent_roots",
    "describe_session",
    "detect_agent",
    "exclude_keywords",
    "extract_cwd",
    "extract_first_time",
    "extract_session_id",
    "extract_task_goal",
    "extract_task_goal_from_cmdline",
    "extract_workspace",
    "format_label",
    "is_compressed_log",
    "is_primary_log",
    "path_matches_keywords",
    "resolve_scan_roots",
    "resolve_task_goal",
    "scan_agent_sessions",
]


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
#: 各 Agent 的展示名。``unknown`` 覆盖「认不出来」的兜底场景。
AGENT_LABELS: Dict[str, str] = {
    "codex": "Codex",
    "mcode": "MCode",
    "claude": "Claude",
    "dsh": "DeepSeek",
    "unknown": "未知",
}

#: 已知的 Agent 键，顺序即展示顺序。
AGENT_KEYS: Tuple[str, ...] = ("codex", "mcode", "claude", "dsh")

#: 活跃判定阈值：距今这么多秒内还有写入，就认为 Agent「正在跑」。
ACTIVE_AGE_SECONDS: float = 300.0

#: 单次扫描最多检查多少个候选文件（防止扫到全盘卡住）。
MAX_CANDIDATE_FILES: int = 300

#: **每个**扫描根最多深读多少个候选文件（v3.0.2）。
#:
#: ⚠️ 为什么要有「每个根一份」而不是全局一份
#: **************************************
#: 用户实测 v3.0.1 时，``.codex`` 里有成百上千个历史文件，**一个根就把
#: 全局预算吃光了** —— 结果 ``.minimax`` / ``.dsh`` 一个都没轮到，
#: 雷达「扫不到正在跑的那个会话」。
#:
#: 现在每个根各拿一份预算，谁也挤不掉谁。
MAX_CANDIDATE_FILES_PER_ROOT: int = 40

#: 扫描的最大目录深度（相对 root 记 0 层）。
#:
#: ⚠️ v3.0.2 从 4 提到 5 —— DeepSeek harness 的会话日志在
#:    ``<项目目录>/session-<uuid>/session.v4.jsonl.zstd``，
#:    扫描根指到 ``~/.dsh`` 上时需要 5 层才够。
MAX_SCAN_DEPTH: int = 5

#: 超过这个体积的文件直接跳过（多半不是会话日志）。
MAX_FILE_BYTES: int = 64 * 1024 * 1024

#: 描述单个会话时，最多读文件**头部**多少字节（只读头不整读）。
DEFAULT_MAX_READ_BYTES: int = 256 * 1024

#: 取样行数上限（文件头部的前 N 行）。
SAMPLE_LINE_LIMIT: int = 40

#: 自动拔取任务锚点时，最多往前看多少行（v3.5.2）。
#:
#: 任务书的要求是「文件前 10 行」——
#: 实测 codex 的会话日志里，用户那句话就在第 7 行，
#: 10 行足够覆盖；再多就是浪费 I/O。
TASK_GOAL_SCAN_LINES: int = 10

#: 自动拔取出来的任务锚点最多留多少字（v3.5.2）。
#:
#: 锚点只是**锚点** —— 太长会挤爆输入框，也会让漂移判定变贵。
TASK_GOAL_MAX_CHARS: int = 200

#: 任务锚点的最短长度（v3.5.2）。
#:
#: 一两个字符的「锚点」没有意义，宁可当作没找到。
TASK_GOAL_MIN_CHARS: int = 4

#: 拔取任务锚点时最多读文件**头部**多少字节（v3.5.2）。
#:
#: mcode 的 ``compactionSummary`` 单行能有 3 万字，
#: 所以这个上限必须给得比「一行」宽裕得多。
TASK_GOAL_MAX_READ_BYTES: int = 256 * 1024

#: 递归找用户消息时最多往下钻几层（v3.5.2）。
#:
#: codex 的格式是 ``{"type":"response_item","payload":{"role":"user"}}`` ——
#: 两层就到底了；再深就是数据里恰好有个叫 ``user`` 的字段，纯属巧合。
TASK_GOAL_MAX_DEPTH: int = 6

#: 这些是**系统注入**的标记（v3.5.2）。
#:
#: ⚠️ 绝不能把系统噪声当成任务目标 ——
#:    否则会凭空造出一个假锚点，后面的意图漂移判定全会跟着跑偏。
#:
#: 实测样本：codex 会把 ``<environment_context>`` 塞进**第一条** user 消息；
#: mcode 会塞 ``<system-reminder>`` / ``<background-task-finished>`` /
#: ``<agent-context>``。
_TASK_GOAL_NOISE_TAGS: Tuple[str, ...] = (
    "<system-reminder>",
    "<background-task-finished>",
    "<agent-context>",
    "<environment_context>",
    "<skills_instructions>",
    "<permissions instructions>",
    "<user_instructions>",
    "<system-instructions>",
    "<command-name>",
    "<command-message>",
    "<local-command-stdout>",
    "<user-memory>",
    "<session_identity_reminder>",
)

#: ``role`` 取这些值就是「用户说的话」（v3.5.2）。
_TASK_GOAL_USER_ROLES: Tuple[str, ...] = ("user", "human")

#: ``type`` 取这些值就是「用户发的那条消息」（v3.5.2）。
_TASK_GOAL_USER_TYPES: Tuple[str, ...] = (
    "user_message",
    "user",
    "prompt",
    "human",
)

#: 压缩摘要里的「任务目标」标题（v3.5.2）。
_GOAL_SECTION_RE = re.compile(r"(?mi)^##\s*goal\s*$")

#: 压缩摘要里的任意二级标题（v3.5.2）——
#: 用来判断 ``## Goal`` 那一段**到哪儿为止**。
_ANY_SECTION_RE = re.compile(r"(?m)^##\s")

#: 会被当作候选的日志扩展名。
LOG_SUFFIXES: Tuple[str, ...] = (".jsonl", ".json", ".log", ".zstd")

#: 压缩日志的后缀（v3.0.2：DeepSeek harness 用 zstd）。
COMPRESSED_SUFFIXES: Tuple[str, ...] = (".zstd",)

#: 路径里出现这些片段就说明是哪个 Agent 的家目录。
_PATH_MARKERS: Tuple[Tuple[str, str], ...] = (
    (".codex", "codex"),
    (".mcode", "mcode"),
    (".minimax", "mcode"),
    (".claude", "claude"),
    (".dsh", "dsh"),
)

#: 隐藏目录的「豁免名单」——Agent 的家目录本身是隐藏目录，
#: 但那正是雷达要找的地方，所以不能一刀切地跳过。
_HIDDEN_DIR_EXEMPT: Tuple[str, ...] = (
    ".codex",
    ".mcode",
    ".minimax",
    ".claude",
    ".dsh",
)

#: 默认扫描根目录（相对家目录；绝对路径原样使用）。
#:
#: v3.0.1 新增配置项 ``radar.scan_roots`` —— 用户可以在
#: ``config.yaml`` 里换成自己的路径（例如 ``D:\\项目``）。
#:
#: ⚠️ **顺序就是优先级**（v3.0.2 改的）
#:    ----------------------------
#:    用户实测 v3.0.1 时，雷达扫出来**全是** ``.codex`` 下的历史死
#:    文件，把真正在跑的 MiniMax / DeepSeek 会话全挤掉了。所以：
#:
#:    * ``.minimax/v2/sessions`` —— MiniMax Code（mcode）**真正**
#:      落会话日志的地方（``~/.minimax/v2/sessions/<年/月/日>/<会话>/``）；
#:    * ``.dsh/sessions`` —— DeepSeek harness 的会话目录
#:      （``~/.dsh/sessions/<项目>/session-<uuid>/session.v4.jsonl.zstd``）；
#:    * ``.mcode`` / ``.claude`` —— 社区习惯叫法与 Claude Code；
#:    * ``.codex`` —— **排到最后**：里面绝大多数是早就跑完的历史
#:      记录，只有「正在写入」的那一份才值得关注。
DEFAULT_SCAN_ROOTS: Tuple[str, ...] = (
    ".minimax/v2/sessions",
    ".dsh/sessions",
    ".mcode",
    ".claude",
    ".codex",
)

#: 默认**排除**的路径关键词（v3.4.0）。
#:
#: 用户实测的原话：「哨兵去抓自己压测产生的残留日志，
#: 却漏掉了用户真实在跑的外部插件任务。」
#:
#: 也就是说，雷达把哨兵**自己造出来的测试日志**当成了真会话 ——
#: 列表里全是噪音，用户真正在跑的那个反而被挤掉了。
#:
#: ⚠️ 匹配方式是**子串**匹配（不区分大小写）——
#:    所以 ``contest`` 这种目录也会被 ``test`` 命中。
#:    真被误伤了就把 ``test`` 从配置清单里删掉。
DEFAULT_EXCLUDE_KEYWORDS: Tuple[str, ...] = (
    "sentinel",
    "stress",
    "test",
)

#: 工作区的来源标记（写进 JSON，前端据此说明「这个路径是怎么来的」）。
WORKSPACE_SOURCES: Dict[str, str] = {
    "cwd": "日志字段",
    "args": "启动参数",
    "process": "进程工作目录",
    "file": "日志文件名",
}

#: 永远跳过的目录名。
_SKIPPED_DIR_NAMES: Tuple[str, ...] = ("node_modules",)

#: 工作目录候选键（顶层与 ``item`` / ``payload`` 内都会找）。
_CWD_KEYS: Tuple[str, ...] = (
    "cwd",
    "workspace",
    "workspaceRoot",
    "project_root",
    "projectRoot",
    "workingDirectory",
)

#: 「工作区根目录列表」候选键（codex 的 ``runtime_workspace_roots``）。
_WORKSPACE_ROOT_KEYS: Tuple[str, ...] = (
    "runtime_workspace_roots",
    "workspace_roots",
    "workspaceRoots",
)

#: 命令行 / 启动参数候选键（回退链的第二步）。
_ARGS_KEYS: Tuple[str, ...] = (
    "args",
    "argv",
    "command",
    "cmd",
    "commandLine",
    "command_line",
    "commandline",
)

#: 会话 ID 候选键（回退链之外的「这是哪个会话」）。
_SESSION_ID_KEYS: Tuple[str, ...] = (
    "session_id",
    "sessionId",
    "sessionID",
    "thread_id",
    "threadId",
    "conversation_id",
    "conversationId",
    "conversationID",
)

#: 时间戳候选键。
_TIME_KEYS: Tuple[str, ...] = (
    "timestamp",
    "time",
    "ts",
    "created_at",
    "createdAt",
    "createdAtMs",
    "started_at",
    "startedAt",
    "date",
)

#: 路径里出现 ``-session_`` 就说明后面那截是会话 ID（mcode 的目录名）。
_SESSION_PATH_MARKER: str = "-session_"

#: DeepSeek harness 的会话 ID 前缀（``session-<uuid>``）。
_DSH_SESSION_PREFIX: str = "session-"

#: 文件名结尾的 UUID 形态（codex 的 ``rollout-…-<uuid>.jsonl``）。
_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
    r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)

#: 毫秒时间戳的判定下限（1e11 秒 ≈ 公元 5138 年，正常秒级时间戳
#: 远小于它；超过就是毫秒）。
_MILLISECOND_THRESHOLD: float = 1e11

#: 「会话主日志」的文件名（精确匹配，小写）。
#:
#: 一个 mcode 会话目录下躺着 4 个文件（``messages.jsonl`` /
#: ``llm-call.json`` / ``history-catalog.json`` /
#: ``user-message-locators.jsonl``）—— 全列出来就是「满屏垃圾」，
#: 只有 ``messages.jsonl`` 才是真正的对话日志。
_PRIMARY_FILE_NAMES: Tuple[str, ...] = (
    "messages.jsonl",
    "history.jsonl",
)

#: 「会话主日志」的文件名前缀（codex 的 rollout、DeepSeek 的 session）。
#:
#: ⚠️ v3.0.2 加了 ``session.`` —— DeepSeek harness 的会话日志
#:    叫 ``session.v4.jsonl.zstd``（旧版本是 ``session.v3.jsonl.zstd``），
#:    不认它的话整个 DeepSeek 会话目录都会被当成「附属文件」折叠掉。
_PRIMARY_FILE_PREFIXES: Tuple[str, ...] = ("rollout-", "session.")

#: 额外扫描目录的环境变量（分号分隔）。
ROOTS_ENV_VAR: str = "SENTINEL_RADAR_ROOTS"

#: 明确知道是**附属文件**的名字（读它们纯属浪费 I/O，v3.0.2）。
#:
#: ⚠️ 这是黑名单，不是白名单 —— 白名单会把自定义目录里
#:    名字不规范的会话日志全部漏掉（v3.0.2 的教训）。
_AUXILIARY_FILE_NAMES: Tuple[str, ...] = (
    "history-catalog.json",
    "llm-call.json",
    "user-message-locators.jsonl",
    "display.jsonl",
    "ledger.jsonl",
    "snapshot.json",
    "manifest.json",
)

#: mcode 的快照文件名（``g000000000031--ctx_<uuid>.jsonl`` /
#: ``env-g000000000031--ctx_<uuid>.json``）—— 也是附属文件。
_SNAPSHOT_FILE_RE = re.compile(r"^(env-)?g\d+--ctx_")


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------
@dataclass
class DiscoveredSession:
    """一个被雷达发现的 Agent 会话日志。

    :param path: 被发现的日志**绝对路径**。
    :param agent: ``"codex"`` / ``"mcode"`` / ``"claude"`` / ``"unknown"``。
    :param agent_label: Agent 的展示名。
    :param cwd: 工作目录；提取不到就是空字符串。
    :param label: 展示名，形如 ``"[Codex] D:\\work\\project"``。
    :param size: 文件字节数。
    :param mtime: 最后修改时间（epoch 秒）。
    :param age: 距今多少秒没有更新。
    :param active: 是否正在被活跃写入。
    :param workspace: **用于展示**的工作区标识（v3.0.1）——
        优先是真实工作目录；实在提取不到就是「父目录名\\文件名」。
    :param workspace_source: 工作区是从哪儿来的
        （``cwd`` / ``args`` / ``process`` / ``file``）。
    :param session_id: 会话 ID（线程 ID）；提取不到就是空字符串。
    :param first_time: 首条消息时间（epoch 秒）；提取不到就是 0。
    :param primary: 是不是「会话主日志」（v3.0.1）——
        附属文件（索引 / 目录 / 快照）默认折叠起来。
    :param priority: 所在扫描根的**优先级**（v3.0.2）——
        数值越大越优先；``.codex`` 被排到最后，所以它的优先级最低。
    :param compressed: 日志是不是**压缩**的（v3.0.2，DeepSeek 的 zstd）。
    :param attachable: 能不能**接入实时审查**（v3.0.2）——
        压缩日志目前只能「发现」，不能实时跟读，所以是 ``False``。
    :param task_goal: **自动拔出来的任务锚点**（v3.5.2）——
        从日志头部（或命令行）里找到的用户最初那句 Prompt；
        找不到就是空字符串。
    """

    path: str
    agent: str
    agent_label: str
    cwd: str
    label: str
    size: int
    mtime: float
    age: float
    active: bool
    workspace: str = ""
    workspace_source: str = ""
    session_id: str = ""
    first_time: float = 0.0
    primary: bool = True
    priority: int = 0
    compressed: bool = False
    attachable: bool = True
    task_goal: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """转成普通字典（字段原样输出，便于直接 JSON 化）。

        :return: 可 ``json.dumps`` 的字典。
        """
        return {
            "path": self.path,
            "agent": self.agent,
            "agent_label": self.agent_label,
            "cwd": self.cwd,
            "label": self.label,
            "size": self.size,
            "mtime": self.mtime,
            "age": self.age,
            "active": self.active,
            "workspace": self.workspace,
            "workspace_source": self.workspace_source,
            "session_id": self.session_id,
            "first_time": self.first_time,
            "primary": self.primary,
            "priority": self.priority,
            "compressed": self.compressed,
            "attachable": self.attachable,
            "task_goal": self.task_goal,
        }


# ---------------------------------------------------------------------------
# 函数 1：扫描目录清单
# ---------------------------------------------------------------------------
def agent_roots(
    home: Optional[str] = None,
    *,
    scan_roots: Optional[Sequence[str]] = None,
) -> List[Dict[str, Any]]:
    """列出要扫描的 Agent 会话目录。

    **不给** ``scan_roots`` 时（v3.0.2 起）：

    用内置默认清单 :data:`DEFAULT_SCAN_ROOTS` —— **顺序即优先级**
    （``.minimax/v2/sessions`` 最前，``.codex`` 最后），**只返回
    实际存在**的那些（家目录可能被用户删掉或者没装）。

    **给了** ``scan_roots`` 时（v3.0.1）：

    以配置清单为准 —— ``radar.scan_roots`` **替换**默认清单，
    相对路径（``.codex``）按家目录解析，绝对路径（``D:\\项目``）原样使用。
    这时**不再要求目录存在**：不存在的目录扫不出东西，但要在界面上
    让用户看见「你配的这条路径我们认了」。

    两种情况下，环境变量 :data:`ROOTS_ENV_VAR` 里的额外目录都会被追加。

    :param home: 家目录；默认取当前用户家目录。
    :param scan_roots: 配置里的扫描根目录清单；``None`` 表示用默认清单。
    :return: ``{"agent", "label", "path", "priority"}`` 字典列表。
    """
    base = os.path.expanduser(str(home)) if home else os.path.expanduser("~")

    if scan_roots is not None:
        roots = resolve_scan_roots(scan_roots, home=base)
        roots.extend(_extra_roots())
        return roots

    # 默认清单：只留**实际存在**的目录（用户可能压根没装某个 Agent）。
    roots = []
    for entry in resolve_scan_roots(DEFAULT_SCAN_ROOTS, home=base):
        try:
            exists = os.path.isdir(entry["path"])
        except OSError as exc:  # 权限异常不能挡住启动
            logger.debug("检查目录 %s 失败：%s", entry["path"], exc)
            exists = False
        if exists:
            roots.append(entry)

    roots.extend(_extra_roots())
    return roots


def resolve_scan_roots(
    scan_roots: Sequence[str],
    home: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """把配置里的 ``radar.scan_roots`` 解析成实际要扫的目录清单。

    解析规则：

    * 空串 / 纯空白 → 跳过；
    * ``~`` 开头 → 展开成家目录；
    * **相对路径**（``.codex`` / ``.minimax/v2/sessions``）→ 按家目录解析；
    * **绝对路径**（``D:\\项目``）→ 原样使用；
    * Agent 名字按**路径分段名**猜（``.codex`` → codex、``.minimax`` → mcode、
      ``.dsh`` → dsh）。

    重复的路径只留第一条（按平台规则忽略大小写比较）。

    ⚠️ **顺序即优先级**（v3.0.2）
    ************************
    每一条都会带上 ``priority``（数值**越大越优先**）：清单里排第一的
    优先级最高。用户实测 v3.0.1 时，``.codex`` 里的历史死文件把真正
    在跑的会话全挤掉了 —— 现在把 ``.codex`` 排到最后就能解决。

    :param scan_roots: 配置里的路径清单。
    :param home: 家目录；默认取当前用户家目录。
    :return: ``{"agent", "label", "path", "priority"}`` 字典列表。
    """
    base = os.path.expanduser(str(home)) if home else os.path.expanduser("~")
    resolved: List[Dict[str, Any]] = []
    seen: set = set()

    for entry in scan_roots or ():
        text = str(entry or "").strip()
        if not text:
            continue
        try:
            expanded = os.path.expanduser(text)
            path = (
                expanded
                if os.path.isabs(expanded)
                else os.path.join(base, expanded)
            )
            path = os.path.normpath(path)
        except (OSError, ValueError) as exc:
            # 路径解析不了就跳过这一条 —— 不能因为一条脏配置让整个雷达罢工。
            logger.warning(
                "解析扫描根目录失败（%s）：%s: %s",
                text,
                type(exc).__name__,
                exc,
            )
            continue

        key = os.path.normcase(path)
        if key in seen:
            continue
        seen.add(key)

        agent = _infer_agent_from_path(path)
        resolved.append(
            {
                "agent": agent,
                "label": AGENT_LABELS.get(agent, AGENT_LABELS["unknown"]),
                "path": path,
                "priority": 0,
            }
        )

    # 优先级：清单里越靠前，数值越大。
    total = len(resolved)
    for index, item in enumerate(resolved):
        item["priority"] = total - index
    return resolved


def _extra_roots() -> List[Dict[str, Any]]:
    """解析环境变量里的额外扫描目录。

    :return: ``{"agent", "label", "path", "priority"}`` 字典列表。
    """
    raw = os.environ.get(ROOTS_ENV_VAR, "") or ""
    roots: List[Dict[str, Any]] = []

    for chunk in raw.split(";"):
        text = chunk.strip()
        if not text:
            continue

        left, sep, right = text.rpartition("=")
        if sep and right.strip().lower() in AGENT_KEYS:
            # 明确的 ``路径=agent`` 形态。
            path_text = left.strip()
            agent = right.strip().lower()
        else:
            # 没有可识别的 agent 名 —— 整段当路径，再猜。
            path_text = text
            agent = _infer_agent_from_path(path_text)

        path = os.path.expanduser(path_text)
        if not path:
            continue
        roots.append(
            {
                "agent": agent,
                "label": AGENT_LABELS.get(agent, AGENT_LABELS["unknown"]),
                "path": path,
                # 环境变量追加的目录优先级最低（0）。
                "priority": 0,
            }
        )
    return roots


def _infer_agent_from_path(path: str) -> str:
    """按**路径分段名**猜测这个目录属于哪个 Agent。

    分段依次检查（大小写不敏感，去掉开头的 ``.``），命中第一个
    已知 Agent 名就算它 —— 例如 ``C:\\Users\\x\\.codex`` 推出
    ``codex``。``.minimax`` 也算 mcode（MiniMax Code 的真实家目录）。
    都猜不中就是 ``unknown``。

    :param path: 目录路径。
    :return: Agent 键或 ``"unknown"``。
    """
    text = os.path.expanduser(str(path or "")).replace("\\", "/")
    for segment in text.split("/"):
        name = segment.strip().lower()
        if not name:
            continue
        if name.startswith("."):
            name = name[1:]
        if name in AGENT_KEYS:
            return name
        if name == "minimax":
            # MiniMax Code 的家目录 —— 它就是 mcode，只是换了目录名。
            return "mcode"
    return "unknown"


# ---------------------------------------------------------------------------
# 函数 2：识别 Agent 种类
# ---------------------------------------------------------------------------
def detect_agent(path: str, sample_lines: Sequence[str]) -> str:
    """判断这份日志属于哪个 Agent。

    判定顺序是**先路径后内容**（路径优先）：

    1. **路径特征** —— 路径任意一段命中 ``.codex`` / ``.mcode`` /
       ``.claude`` 即可（大小写不敏感）；
    2. **内容特征** —— 逐行尝试 ``json.loads``，按事件类型判断。

    ⚠️ codex 与 mcode 的 v0.6 协议**都用** ``item.*`` 事件，
    唯一可靠的区别是 ``item.toolCall`` 这个嵌套包装**在不在**：
    mcode 有、codex 没有。

    :param path: 日志路径（仅用于路径特征判定）。
    :param sample_lines: 样本行（调用方保证单行长度可控）。
    :return: ``"codex"`` / ``"mcode"`` / ``"claude"`` / ``"unknown"``。
    """
    by_path = _detect_agent_by_path(path)
    if by_path:
        return by_path

    for line in sample_lines or ():
        payload = _loads(line)
        if not isinstance(payload, dict):
            continue
        verdict = _detect_agent_by_payload(payload)
        if verdict:
            return verdict
    return "unknown"


def _detect_agent_by_path(path: str) -> Optional[str]:
    """按路径特征判定 Agent（大小写不敏感，任意一段命中即可）。

    :param path: 日志路径。
    :return: Agent 键；看不出来就是 ``None``。
    """
    text = str(path or "").lower()
    if not text:
        return None
    for marker, agent in _PATH_MARKERS:
        if marker in text:
            return agent
    return None


def _detect_agent_by_payload(payload: Dict[str, Any]) -> Optional[str]:
    """按单行 JSON 的事件类型判定 Agent。

    :param payload: 解析后的字典。
    :return: Agent 键；这一行看不出来就是 ``None``。
    """
    payload_type = str(payload.get("type") or "").strip().lower()

    # --- codex：会话级事件 ---
    if payload_type in (
        "thread.started",
        "thread.completed",
        "turn.started",
        "turn.completed",
    ):
        return "codex"

    # --- codex：新版 rollout 协议（v0.16x，v3.0.1 实地核对过）---
    #     ``session_meta``  里带 ``payload.cwd`` / ``payload.session_id``；
    #     ``response_item`` 是消息与工具调用条目。
    #
    # ⚠️ mcode 用的是 v0.6 的 ``item.*`` 协议（工具调用藏在
    #    ``item.toolCall`` 里），**不用** ``response_item`` ——
    #    所以这里不必再为它做区分。
    if payload_type in ("session_meta", "event_msg", "turn_context"):
        return "codex"

    if payload_type == "response_item":
        return "codex"

    # --- DeepSeek harness：会话头（``{"type":"session", …}``）---
    #
    # ⚠️ v3.0.2 实地核对：DeepSeek 的会话日志首行就是
    #    ``{"type":"session","version":4,"id":"session-<uuid>",
    #      "createdAt":…,"cwd":"D:\\项目",…}``。
    if payload_type == "session":
        return "dsh"

    # --- codex / mcode：item.* 事件（靠 toolCall 包装区分）---
    if payload_type in ("item.started", "item.completed", "item.updated"):
        item = payload.get("item")
        if not isinstance(item, dict):
            return None
        return "mcode" if "toolCall" in item else "codex"

    # --- mcode 旧协议：tool_call ---
    if payload_type == "tool_call":
        if isinstance(payload.get("tool_call"), dict):
            return "mcode"
        return None

    # --- claude：消息容器（assistant / user）---
    if payload_type in ("assistant", "user"):
        content = _message_content(payload)
        if isinstance(content, list):
            return "claude"
        return None

    # --- claude：会话初始化 ---
    if payload_type == "system":
        subtype = str(payload.get("subtype") or "").strip().lower()
        if subtype == "init" and payload.get("session_id"):
            return "claude"
        return None

    # --- mcode 旧协议：思考 / 正文 ---
    if payload_type in ("thinking", "text"):
        return "mcode"

    return None


def _message_content(payload: Dict[str, Any]) -> Optional[List[Any]]:
    """取出消息容器里的 ``content`` 块数组。

    既认 ``message.content``（claude / mcode 的标准写法），也认
    顶层 ``content``（个别版本会拍平）。

    :param payload: 解析后的字典。
    :return: 内容块数组；不是数组就是 ``None``。
    """
    message = payload.get("message")
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, list):
            return content
    content = payload.get("content")
    if isinstance(content, list):
        return content
    return None


# ---------------------------------------------------------------------------
# 函数 3：提取工作目录 / 工作区 / 会话 ID / 时间
# ---------------------------------------------------------------------------
def _containers(payload: Dict[str, Any]) -> Iterator[Dict[str, Any]]:
    """产出「要搜键」的字典容器。

    ⚠️ Agent 的协议层级很乱（v3.0.1 实地核对过真实日志）：

    * 老 codex / mcode：``{"type": "turn.started", "cwd": "D:\\work"}``
      —— ``cwd`` 在**顶层**；
    * 新 codex（v0.16x）：``{"type": "session_meta", "payload": {…"cwd":…}}``
      —— ``cwd`` 在 **payload 里**；
    * claude：``{"type": "assistant", "message": {…}}``；
    * mcode：``{"message": {"timestamp": …}}``。

    挨个找一遍最省事 —— 少找一个层级，用户看到的就又是「未知工作区」。

    :param payload: 解析后的一行。
    :yield: 值得搜的字典容器。
    """
    yield payload
    for key in ("item", "payload", "message", "data"):
        nested = payload.get(key)
        if not isinstance(nested, dict):
            continue
        yield nested
        inner = nested.get("payload")
        if isinstance(inner, dict):
            yield inner


def extract_cwd(path: str, sample_lines: Sequence[str]) -> str:
    """从日志内容里提取 Agent 干活时的工作目录。

    逐行 ``json.loads``（解析失败就跳过），在**顶层**字典和
    ``item`` / ``payload`` / ``message`` 等嵌套字典里依次找这些键：
    ``cwd``、``workspace``、``workspaceRoot``、``project_root``、
    ``projectRoot``、``workingDirectory``。

    取值必须**看起来像路径**：非空字符串、长度 ≥ 3、含 ``/`` 或
    ``\\``、且不是 URL（不以 ``http://`` / ``https://`` 开头）。

    找不到 ``cwd`` 字段时，还会看 ``runtime_workspace_roots`` 这类
    **工作区根目录列表**（新 codex 会写），取第一个像路径的。

    :param path: 日志路径（保留是为了与 :func:`detect_agent`
        签名对称，内容判定用不到）。
    :param sample_lines: 样本行。
    :return: 工作目录；提取不到就是空字符串。
    """
    for line in sample_lines or ():
        payload = _loads(line)
        if not isinstance(payload, dict):
            continue
        for container in _containers(payload):
            for key in _CWD_KEYS:
                value = container.get(key)
                if _looks_like_path(value):
                    return value.strip()
            for key in _WORKSPACE_ROOT_KEYS:
                roots = container.get(key)
                if not isinstance(roots, (list, tuple)):
                    continue
                for item in roots:
                    if _looks_like_path(item):
                        return str(item).strip()
    return ""


def extract_workspace(
    path: str,
    sample_lines: Sequence[str],
    process_cwd_resolver: Optional[Callable[[str, str], str]] = None,
    agent: str = "",
) -> Tuple[str, str]:
    """按**回退链**提取「这是哪个工作区」。

    顺序（v3.0.1 —— 用户实测发现全是「未知工作区」）：

    1. **日志字段** —— ``cwd`` / ``workspace`` / ``runtime_workspace_roots``；
    2. **启动参数** —— ``args`` / ``command`` 里像路径的参数
       （且该路径**确实是个目录**）；
    3. **进程工作目录** —— 由调用方注入的 ``process_cwd_resolver``
       现场查（查不到就是空串）；
    4. **兜底** —— 日志文件所在的**父目录名 + 文件名**，
       例如 ``27\\rollout-xxx.jsonl``。

    ⚠️ 第 4 步是**硬性兜底**：宁可显示「哪个文件」，
    也不显示「未知工作区」—— 后者会让用户以为雷达坏了。

    ⚠️ 第 3 步**只在前面都失败时**才调用 —— 反查进程要枚举系统进程，
    不能对每个候选文件都做一遍。

    :param path: 日志路径。
    :param sample_lines: 样本行。
    :param process_cwd_resolver: 进程工作目录查询回调，
        签名 ``(path, agent) -> str``；为 ``None`` 表示不查。
    :param agent: 已识别出的 Agent（传给回调用）。
    :return: ``(工作区, 来源)``；来源取值见 :data:`WORKSPACE_SOURCES`。
    """
    cwd = extract_cwd(path, sample_lines)
    if cwd:
        return cwd, "cwd"

    from_args = _extract_workspace_from_args(sample_lines)
    if from_args:
        return from_args, "args"

    if process_cwd_resolver is not None:
        try:
            process_text = str(process_cwd_resolver(path, agent) or "").strip()
        except Exception as exc:  # noqa: BLE001 —— 反查失败只降级
            logger.debug(
                "反查进程工作目录失败 %s：%s: %s",
                path,
                type(exc).__name__,
                exc,
            )
            process_text = ""
        if process_text:
            return process_text, "process"

    return _file_workspace(path), "file"


def _extract_workspace_from_args(sample_lines: Sequence[str]) -> str:
    """从 ``args`` / ``command`` 里挑出一个**确实是目录**的路径。

    ⚠️ 为什么这么挑：命令行里到处是路径 —— 可执行文件、配置文件、
    目标文件。只有**真的是目录**的那个才可能是工作区。

    :param sample_lines: 样本行。
    :return: 工作目录；挑不出来就是空字符串。
    """
    for line in sample_lines or ():
        payload = _loads(line)
        if not isinstance(payload, dict):
            continue
        for container in _containers(payload):
            for key in _ARGS_KEYS:
                for token in _iter_command_tokens(container.get(key)):
                    candidate = _directory_from_token(token)
                    if candidate:
                        return candidate
    return ""


def _iter_command_tokens(value: Any) -> Iterator[str]:
    """把命令行（字符串或数组）拆成一个个 token。

    :param value: 命令行原文或 argv 数组。
    :yield: token 文本。
    """
    if isinstance(value, str):
        for token in _split_command(value):
            yield token
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            if isinstance(item, (str, list, tuple)):
                yield from _iter_command_tokens(item)


def _split_command(text: str) -> List[str]:
    """按空白切命令行，但**尊重引号**。

    Windows 上路径常带空格（``D:\\My Project\\x``），
    不认引号就会把路径切成两半。

    :param text: 命令行原文。
    :return: token 列表。
    """
    tokens: List[str] = []
    current: List[str] = []
    quote = ""

    for char in text:
        if quote:
            if char == quote:
                quote = ""
            else:
                current.append(char)
            continue
        if char in ('"', "'"):
            quote = char
            continue
        if char.isspace():
            if current:
                tokens.append("".join(current))
                current = []
            continue
        current.append(char)

    if current:
        tokens.append("".join(current))
    return tokens


def _directory_from_token(token: str) -> str:
    """判断一个命令行 token 是不是「确实是目录的绝对路径」。

    :param token: 命令行 token。
    :return: 是目录就返回规范化后的路径；否则返回空串。
    """
    text = str(token or "").strip().strip('"').strip("'")
    if not text:
        return ""

    # ``--cwd=D:\\项目`` 这种写法：取等号右边。
    if "=" in text:
        tail = text.rsplit("=", 1)[-1].strip()
        if tail and tail != text:
            text = tail

    # 去掉结尾常见的标点（命令行里常带逗号、括号、引号）。
    text = text.rstrip(",;)]}\"'")
    if not _looks_like_path(text):
        return ""
    try:
        if not os.path.isabs(text):
            return ""
        if not os.path.isdir(text):
            return ""
    except (OSError, ValueError):
        return ""
    return text


def _file_workspace(path: str) -> str:
    """兜底标识：**父目录名 + 文件名**。

    例：``C:\\Users\\x\\.codex\\sessions\\2026\\10\\26\\27\\rollout-a.jsonl``
    → ``27\\rollout-a.jsonl``。

    ⚠️ 这一步**必须存在**：少了它，用户看到的就只有「未知工作区」，
    根本不知道雷达到底扫到了哪个文件。

    :param path: 日志路径。
    :return: 兜底标识；路径为空就是空串。
    """
    text = str(path or "")
    if not text:
        return ""
    parent = os.path.basename(os.path.dirname(text))
    name = os.path.basename(text)
    if parent and name:
        return parent + os.sep + name
    return name or parent


def extract_session_id(path: str, sample_lines: Sequence[str]) -> str:
    """提取会话 ID（线程 ID）。

    内容里找不到就从**路径**里找：

    * mcode 的会话目录名形如 ``…-session_<id>`` → 取 ``<id>``；
    * codex 的文件名形如 ``rollout-…-<uuid>.jsonl`` → 取那个 UUID；
    * DeepSeek 的会话目录名形如 ``session-<uuid>`` → 取那个 UUID
      （v3.0.2：日志在 ``<项目>/session-<uuid>/session.v4.jsonl.zstd``）。

    ⚠️ v3.0.2：DeepSeek 会话头的 ``id`` 字段写成 ``session-<uuid>``，
    这里会**去掉前缀**只留 UUID —— 这样内容里取到的和路径里取到的
    是同一个字符串，雷达才能认出「这两个文件是同一个会话」。

    :param path: 日志路径。
    :param sample_lines: 样本行。
    :return: 会话 ID；找不到就是空字符串。
    """
    for line in sample_lines or ():
        payload = _loads(line)
        if not isinstance(payload, dict):
            continue
        for container in _containers(payload):
            # DeepSeek 会话头：``{"type":"session","id":"session-<uuid>"}``。
            if str(container.get("type") or "").lower() == "session":
                value = container.get("id")
                if isinstance(value, str) and value.strip():
                    return _strip_session_prefix(value.strip())
            for key in _SESSION_ID_KEYS:
                value = container.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
    return _session_id_from_path(path)


def _strip_session_prefix(text: str) -> str:
    """去掉会话 ID 的 ``session-`` 前缀（只留 UUID）。

    :param text: 原始会话 ID。
    :return: 去掉前缀后的 ID；没有前缀就原样返回。
    """
    value = str(text or "").strip()
    if value.lower().startswith(_DSH_SESSION_PREFIX):
        return value[len(_DSH_SESSION_PREFIX) :].strip()
    return value


def _session_id_from_path(path: str) -> str:
    """从**路径**里猜会话 ID。

    :param path: 日志路径。
    :return: 会话 ID；猜不出来就是空串。
    """
    text = str(path or "")
    if not text:
        return ""

    marker = text.rfind(_SESSION_PATH_MARKER)
    if marker >= 0:
        tail = text[marker + len(_SESSION_PATH_MARKER) :]
        tail = tail.replace("\\", "/").split("/")[0].strip()
        if tail:
            return tail

    # DeepSeek：``<项目>/session-<uuid>/session.v4.jsonl.zstd``
    # —— 会话 ID 在**父目录名**里，不在文件名里。
    parent = os.path.basename(os.path.dirname(text))
    if parent.lower().startswith(_DSH_SESSION_PREFIX):
        found = _UUID_RE.findall(parent)
        if found:
            return found[0]

    found = _UUID_RE.findall(os.path.basename(text))
    if found:
        return found[-1]
    return ""


def extract_first_time(path: str, sample_lines: Sequence[str]) -> float:
    """提取**首条消息**的时间（epoch 秒）。

    这样用户一眼就能确认「这是我刚才跑的那个任务」。

    :param path: 日志路径（内容里没有时间时，用文件 mtime 兜底）。
    :param sample_lines: 样本行。
    :return: epoch 秒；提取不到就是 0。
    """
    for line in sample_lines or ():
        payload = _loads(line)
        if not isinstance(payload, dict):
            continue
        for container in _containers(payload):
            for key in _TIME_KEYS:
                stamp = _parse_timestamp(container.get(key))
                if stamp:
                    return stamp
    return 0.0


def _parse_timestamp(value: Any) -> float:
    """把各种时间写法解析成 epoch 秒。

    支持：epoch 秒 / epoch 毫秒 / ISO 8601 字符串
    （``2026-10-27T08:49:47.571Z``）。

    :param value: 原始值。
    :return: epoch 秒；解析不了就是 0。
    """
    if isinstance(value, bool):
        return 0.0

    if isinstance(value, (int, float)):
        number = float(value)
        if number <= 0:
            return 0.0
        if number >= _MILLISECOND_THRESHOLD:
            return number / 1000.0
        return number

    if not isinstance(value, str):
        return 0.0

    text = value.strip()
    if not text:
        return 0.0

    # 纯数字字符串 → 当作 epoch；位数太短的不然（年份 2026 会被当时间戳）。
    digits = text.replace(".", "", 1)
    if digits.isdigit() and len(digits) >= 9:
        try:
            return _parse_timestamp(float(text))
        except ValueError:
            return 0.0

    candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        moment = datetime.fromisoformat(candidate)
    except ValueError:
        return 0.0
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp()


def is_primary_log(name: str, agent: str = "") -> bool:
    """判断一个文件是不是「会话主日志」。

    附属文件（索引、目录、单次调用记录、快照）默认折叠 ——
    一个 mcode 会话目录下躺着 4 个文件，全列出来就是「满屏垃圾」。

    判定规则：

    * 文件名命中 :data:`_PRIMARY_FILE_NAMES`（``messages.jsonl`` /
      ``history.jsonl``）→ 是主日志；
    * 文件名以 :data:`_PRIMARY_FILE_PREFIXES` 开头
      （codex 的 ``rollout-``）→ 是主日志；
    * claude 的 ``*.jsonl``（项目目录下的会话文件）→ 是主日志；
    * 其余 → 附属文件。

    :param name: 文件名（或完整路径）。
    :param agent: 已识别出的 Agent。
    :return: 是主日志就返回 ``True``。
    """
    base = os.path.basename(str(name or "")).strip().lower()
    if not base:
        return False
    if base in _PRIMARY_FILE_NAMES:
        return True
    if any(base.startswith(prefix) for prefix in _PRIMARY_FILE_PREFIXES):
        return True
    return bool(
        str(agent or "").lower() == "claude" and base.endswith(".jsonl")
    )


def is_compressed_log(name: str) -> bool:
    """判断一个日志文件是不是**压缩**的（v3.0.2）。

    DeepSeek harness 的会话日志是 ``session.v4.jsonl.zstd`` ——
    zstd 压缩的 JSONL。哨兵**能读它的头部**（拿工作区 / 会话 ID /
    时间），但**不能实时跟读**：跟读器记的是「原始字节偏移」，
    压缩流根本没法按字节增量读。

    所以压缩日志**只能被发现**，不能被「接入」——
    界面会明确标出来，不做半成品的按钮。

    :param name: 文件名（或完整路径）。
    :return: 是压缩日志就返回 ``True``。
    """
    return str(name or "").lower().endswith(COMPRESSED_SUFFIXES)


def _looks_like_path(value: Any) -> bool:
    """判断一个值是否**看起来像本地路径**。

    :param value: 候选值。
    :return: 像路径就返回 ``True``。
    """
    if not isinstance(value, str):
        return False
    text = value.strip()
    if len(text) < 3:
        return False
    lowered = text.lower()
    if lowered.startswith(("http://", "https://")):
        return False
    return "/" in text or "\\" in text


# ---------------------------------------------------------------------------
# 函数 4：展示名
# ---------------------------------------------------------------------------
def format_label(
    agent: str,
    cwd: str,
    fallback: str = "",
) -> str:
    """拼出会话展示名。

    * 工作目录非空 → ``"[Codex] D:\\work\\project"``；
    * 工作目录为空但有**兜底标识** → ``"[Codex] 27\\rollout-a.jsonl"``；
    * 两者都为空 → ``"[Codex] 未知工作区"``（最后这个兜底，
      :func:`describe_session` 实际上永远不会触发）；
    * Agent 认不出来 → 前缀用 ``"未知"``。

    :param agent: Agent 键。
    :param cwd: 工作目录。
    :param fallback: 提取不到工作目录时的兜底标识。
    :return: 展示名。
    """
    label = AGENT_LABELS.get(str(agent or ""), AGENT_LABELS["unknown"])
    text = str(cwd or "").strip()
    if text:
        return f"[{label}] {text}"
    alt = str(fallback or "").strip()
    if alt:
        return f"[{label}] {alt}"
    return f"[{label}] 未知工作区"


# ---------------------------------------------------------------------------
# 函数 4.5：自动拔取任务锚点（v3.5.2）
# ---------------------------------------------------------------------------
def extract_task_goal(
    path: str,
    *,
    agent: str = "",
    max_lines: int = TASK_GOAL_SCAN_LINES,
) -> str:
    """从会话日志**头部**自动拔出任务锚点（v3.5.2）。

    为什么需要它
    ============

    旁路挂载进来的会话，哨兵是从文件**尾部**开始跟随的 ——
    用户那句「我要重构 auth 模块」很可能**根本不在**日志里。
    没有锚点就没有滑窗、没有意图漂移检测，
    用户只能自己跑到顶栏手动敲一遍（而且大多数人不知道有这一步）。

    所以这里主动去日志的**前几行**里找那条用户消息。

    支持的形态（都是实测来的，不是猜的）
    ====================================

    * **mcode 续接会话** —— 首行是
      ``{"message":{"role":"compactionSummary","summary":"## Goal\\n\\n..."}}``，
      真正的任务描述在 ``## Goal`` 那一段里（实测单段两万多字）；
    * **mcode 新会话** —— ``{"message":{"role":"user","content":[...]}}``；
    * **codex** —— ``{"type":"response_item","payload":{"role":"user",...}}``；
    * **claude** —— ``{"type":"user","message":{"role":"user","content":"..."}}``；
    * **v0.6 事件** —— ``{"item":{"type":"user_message",...}}``；
    * **扁平形态** —— ``{"type":"user_message","content":"..."}``。

    ⚠️ **系统噪声必须过滤**（v3.5.2）
    ---------------------------------
    codex 会把 ``<environment_context>`` 塞进**第一条** user 消息；
    mcode 会塞 ``<system-reminder>`` / ``<agent-context>``。
    那些不是用户说的话 —— 当成锚点就会凭空造出假目标，
    后面的意图漂移判定全会跟着跑偏（宁可找不到，也不能找错）。

    :param path: 会话日志路径。
    :param agent: 已识别出的 Agent（目前只用于日志，不影响判定）。
    :param max_lines: 最多看前多少行。
    :return: 任务锚点（截断到 200 字）；找不到就是空串。
    """
    for line in _read_head_lines(path, max_lines):
        payload = _parse_json_line(line)
        if payload is None:
            continue
        goal = _goal_from_payload(payload)
        if goal:
            return goal[:TASK_GOAL_MAX_CHARS]
    return ""


def extract_task_goal_from_cmdline(args: Sequence[str]) -> str:
    """从 Agent 命令行里认出任务锚点（v3.5.2）。

    这是日志之外的最后一条自动提取路子：
    ``mcode exec "把登录模块重构一下"`` —— 用户敲的那句话还在命令行里。

    ⚠️ 进程扫描的知识在 :mod:`src.procscan` 里 ——
    这里只做**转发**，不重复实现（免得两处规则各自漂移）。
    ⚠️ psutil 没装 / 导入失败 → 返回空串（整体降级，绝不抛异常）。

    :param args: 命令行参数列表。
    :return: 认出的 Prompt；认不出就是空串。
    """
    parser = _load_cmdline_prompt_parser()
    if parser is None:
        return ""
    try:
        text = str(parser(args) or "").strip()
    except Exception as exc:  # noqa: BLE001 —— 解析失败只降级
        logger.debug("命令行锚点提取失败：%s: %s", type(exc).__name__, exc)
        return ""
    if len(text) < TASK_GOAL_MIN_CHARS:
        return ""
    return text[:TASK_GOAL_MAX_CHARS]


def build_prompt_index(
    *,
    extra_features: Optional[Sequence[str]] = None,
) -> Dict[str, str]:
    """扫一遍正在跑的 Agent CLI，返回「工作目录 → Prompt」的索引（v3.5.2）。

    这个索引**整轮扫描只建一次** ——
    几十个候选文件共用一份，否则每查一个文件就把系统进程整个扫一遍。

    ⚠️ psutil 没装 / 枚举失败 → 返回空字典（界面据此提示手动输入）。

    :param extra_features: 自定义特征清单（v3.4.0）。
    :return: ``{归一化工作目录: Prompt}``；没有就是空字典。
    """
    try:
        from .procscan import normalized_path, scan_agent_prompts
    except ImportError:
        try:
            from src.procscan import (  # type: ignore[no-redef]
                normalized_path,
                scan_agent_prompts,
            )
        except ImportError as exc:
            logger.debug("命令行 Prompt 索引不可用：%s", exc)
            return {}
    index: Dict[str, str] = {}
    try:
        pairs = scan_agent_prompts(extra_features=extra_features)
    except Exception as exc:  # noqa: BLE001 —— 扫描失败只降级
        logger.debug("命令行 Prompt 索引失败：%s: %s", type(exc).__name__, exc)
        return {}
    for cwd, prompt in pairs:
        key = normalized_path(cwd)
        if key and key not in index:
            index[key] = str(prompt or "")
    return index


def resolve_task_goal(
    path: str,
    *,
    agent: str = "",
    cwd: str = "",
    prompt_index: Optional[Mapping[str, str]] = None,
) -> str:
    """三级回退地解析任务锚点（v3.5.2）。

    顺序：

    1. **日志头部** —— 最可靠，用户真的说过那句话；
    2. **命令行** —— 日志里没有时的第二条路
       （``mcode exec "..."`` 那种一次性启动）；
    3. **空串** —— 界面据此保留「请手动输入」的提示。

    :param path: 会话日志路径。
    :param agent: 已识别出的 Agent。
    :param cwd: 会话的工作目录（用来在命令行索引里对号入座）。
    :param prompt_index: ``{归一化工作目录: Prompt}`` 索引；
        ``None`` 表示不查命令行。
    :return: 任务锚点；找不到就是空串。
    """
    goal = extract_task_goal(path, agent=agent)
    if goal:
        return goal
    if not prompt_index:
        return ""
    key = _normalize_cwd_key(cwd)
    if not key:
        return ""
    return str(prompt_index.get(key) or "").strip()[:TASK_GOAL_MAX_CHARS]


def _load_cmdline_prompt_parser() -> Optional[Callable[[Sequence[str]], str]]:
    """拿到「命令行参数 → Prompt」的解析器；拿不到就是 ``None``。

    ⚠️ :mod:`src.procscan` 是**惰性**导入的 ——
    :mod:`src.radar` 本身只依赖标准库（叶子模块的规矩），
    psutil 更是可选依赖，缺了就整体降级。
    """
    try:
        from .procscan import prompt_from_cmdline_args
    except ImportError:
        try:
            from src.procscan import (  # type: ignore[no-redef]
                prompt_from_cmdline_args,
            )
        except ImportError as exc:
            logger.debug("命令行锚点解析器不可用：%s", exc)
            return None
    return prompt_from_cmdline_args


def _normalize_cwd_key(cwd: str) -> str:
    """把工作目录归一化成「能拿来当索引键」的形式。"""
    text = str(cwd or "").strip()
    if not text:
        return ""
    try:
        return os.path.normcase(os.path.abspath(text))
    except (OSError, ValueError):
        return os.path.normcase(text)


def _read_head_lines(path: str, max_lines: int) -> List[str]:
    """读文件头部的若干行（只读前 N 行，绝不整读）。

    :param path: 日志路径。
    :param max_lines: 最多读多少行。
    :return: 行列表（读不到就是空列表）。
    """
    limit = max(int(max_lines), 0)
    if limit <= 0:
        return []
    if is_compressed_log(path):
        return _read_sample_lines(path, TASK_GOAL_MAX_READ_BYTES)[:limit]
    try:
        with open(path, "rb") as handle:
            data = handle.read(TASK_GOAL_MAX_READ_BYTES)
    except (OSError, ValueError) as exc:
        logger.debug("读取任务锚点失败 %s：%s", path, exc)
        return []
    lines: List[str] = []
    for raw in data.decode("utf-8", errors="replace").split("\n"):
        text = raw.strip()
        if text:
            lines.append(text)
        if len(lines) >= limit:
            break
    return lines


def _parse_json_line(line: str) -> Optional[Dict[str, Any]]:
    """把一行文本解析成 JSON 对象；解析不了就是 ``None``。"""
    try:
        payload = json.loads(line)
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _goal_from_payload(payload: Dict[str, Any]) -> str:
    """在一行 JSON 里找出任务锚点（v3.5.2）。"""
    return _walk_for_goal(payload, 0)


def _walk_for_goal(node: Any, depth: int) -> str:
    """递归地在 JSON 树里找「用户说的话」。

    ⚠️ 为什么要递归
    ---------------
    各家 Agent 的嵌套深度不一样：codex 藏在 ``payload`` 里，
    mcode 藏在 ``message`` 里，v0.6 藏在 ``item`` 里。
    写死一条路径就会漏掉另外两家。

    ⚠️ 为什么要限制深度
    -----------------
    再深就是数据里**恰好**有个叫 ``user`` 的字段 —— 纯巧合，
    当成锚点就是凭空造目标。

    :param node: 当前节点。
    :param depth: 当前深度。
    :return: 找到的锚点；没找到就是空串。
    """
    if depth > TASK_GOAL_MAX_DEPTH:
        return ""
    if isinstance(node, dict):
        role = str(node.get("role") or "").strip().lower()
        if role == "compactionsummary":
            text = _clean_goal_text(
                _extract_goal_section(str(node.get("summary") or ""))
            )
            if text:
                return text
        if role in _TASK_GOAL_USER_ROLES:
            text = _clean_goal_text(
                _text_from_content(node.get("content"))
                or _first_text_field(node, ("text", "prompt"))
            )
            if text:
                return text
        event_type = str(node.get("type") or "").strip().lower()
        if event_type in _TASK_GOAL_USER_TYPES:
            text = _clean_goal_text(
                _text_from_content(node.get("content"))
                or _first_text_field(node, ("text", "prompt", "message"))
            )
            if text:
                return text
        for value in node.values():
            if isinstance(value, (dict, list)):
                found = _walk_for_goal(value, depth + 1)
                if found:
                    return found
        return ""
    if isinstance(node, list):
        for item in node:
            if isinstance(item, (dict, list)):
                found = _walk_for_goal(item, depth + 1)
                if found:
                    return found
    return ""


def _text_from_content(content: Any) -> str:
    """把 ``content`` 字段（字符串或内容块数组）还原成正文。

    :param content: ``content`` 字段的值。
    :return: 正文文本；取不到就是空串。
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
                continue
            if not isinstance(block, dict):
                continue
            text = block.get("text")
            if isinstance(text, str) and text:
                parts.append(text)
                continue
            inner = block.get("content")
            if isinstance(inner, str) and inner:
                parts.append(inner)
        return "\n".join(parts)
    return ""


def _first_text_field(node: Dict[str, Any], keys: Sequence[str]) -> str:
    """按顺序取出第一个非空的文本字段。"""
    for key in keys:
        value = node.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _clean_goal_text(text: str) -> str:
    """去掉系统注入噪声；剩下的太短就当作没找到。

    :param text: 原始正文。
    :return: 清洗后的正文；不值得当锚点就是空串。
    """
    cleaned = _strip_noise(str(text or "").strip())
    if len(cleaned) < TASK_GOAL_MIN_CHARS:
        return ""
    return cleaned


def _strip_noise(text: str) -> str:
    """剥掉系统注入块（v3.5.2）。

    先剥**开头**的噪声块（系统提示往往整块放在最前面），
    再把正文里残留的噪声块**截断**掉 ——
    用户真正的诉求总是在噪声之前。

    :param text: 原始正文。
    :return: 剥完噪声的正文。
    """
    current = text
    for _ in range(len(_TASK_GOAL_NOISE_TAGS) + 1):
        lowered = current.lstrip().lower()
        tag = ""
        for candidate in _TASK_GOAL_NOISE_TAGS:
            if lowered.startswith(candidate.lower()):
                tag = candidate
                break
        if not tag:
            break
        closing = "</" + tag[1:]
        index = current.lower().find(closing)
        if index < 0:
            # 没有闭合标签 —— 整段都是噪声。
            return ""
        current = current[index + len(closing):]
    current = current.strip()
    for candidate in _TASK_GOAL_NOISE_TAGS:
        index = current.lower().find(candidate.lower())
        if index >= 0:
            current = current[:index]
    return current.strip()


def _extract_goal_section(summary: str) -> str:
    """从压缩摘要里取出 ``## Goal`` 那一段（v3.5.2）。

    mcode 续接会话的首行是一份**压缩摘要**，
    真正的任务描述在 ``## Goal`` 标题下 —— 直到下一个 ``##`` 标题为止。

    :param summary: 压缩摘要全文。
    :return: ``## Goal`` 段的内容；没有这一段就是空串。
    """
    text = str(summary or "")
    match = _GOAL_SECTION_RE.search(text)
    if match is None:
        return ""
    rest = text[match.end():]
    following = _ANY_SECTION_RE.search(rest)
    if following is not None:
        rest = rest[:following.start()]
    return rest.strip()


# ---------------------------------------------------------------------------
# 函数 5：描述单个会话
# ---------------------------------------------------------------------------
def describe_session(
    path: str,
    *,
    max_read_bytes: int = DEFAULT_MAX_READ_BYTES,
    now: Optional[float] = None,
    process_cwd_resolver: Optional[Callable[[str, str], str]] = None,
    priority: int = 0,
    prompt_index: Optional[Mapping[str, str]] = None,
) -> Optional[DiscoveredSession]:
    """描述**单个**日志文件。

    只读文件**头部**最多 ``max_read_bytes`` 字节（前 40 行做样本），
    绝不整读一个可能几百 MB 的日志。压缩日志（``.zstd``）会先解压
    再切行 —— 解压量同样受这个上限约束（v3.0.2）。

    文件不存在、不是普通文件或者大小为 0 → 返回 ``None``；
    任何 ``OSError`` / ``ValueError`` 也返回 ``None`` ——
    审查侧异常一律降级，绝不抛出去挡住哨兵启动。

    :param path: 日志路径。
    :param max_read_bytes: 最多读多少字节的头部。
    :param now: 「现在」的 epoch 秒；默认取系统当前时间。
    :param process_cwd_resolver: 进程工作目录查询回调
        （回退链第三步）；签名 ``(path, agent) -> str``。
    :param priority: 所在扫描根的优先级（数值越大越优先）。
    :param prompt_index: ``{归一化工作目录: Prompt}`` 索引（v3.5.2）——
        日志里翻不到任务锚点时，用它按工作目录对号入座。
    :return: 会话描述；不可用就是 ``None``。
    """
    try:
        if not os.path.isfile(path):
            return None
        stat_result = os.stat(path)
        size = int(stat_result.st_size)
        if size <= 0:
            return None
        mtime = float(stat_result.st_mtime)
    except (OSError, ValueError) as exc:
        logger.debug("读取文件属性失败 %s：%s", path, exc)
        return None

    lines = _read_sample_lines(path, max_read_bytes)
    agent = detect_agent(path, lines)
    workspace, source = extract_workspace(
        path, lines, process_cwd_resolver, agent
    )
    # 「工作区根目录 + 文件名」只是**标识**，不是真的工作目录 ——
    # 别把它写进 cwd，否则挂载会话时会被当成真工作区用。
    cwd = "" if source == "file" else workspace

    current = time.time() if now is None else float(now)
    age = max(0.0, current - mtime)
    compressed = is_compressed_log(path)

    # v3.5.2：顺手把**任务锚点**也拔出来 ——
    # 以前要用户自己敲，现在从日志头部（退而求其次从命令行）自动找。
    task_goal = resolve_task_goal(
        path,
        agent=agent,
        cwd=cwd,
        prompt_index=prompt_index,
    )

    return DiscoveredSession(
        path=os.path.abspath(str(path)),
        agent=agent,
        agent_label=AGENT_LABELS.get(agent, AGENT_LABELS["unknown"]),
        cwd=cwd,
        label=format_label(agent, cwd, fallback=workspace),
        size=size,
        mtime=mtime,
        age=age,
        active=age <= ACTIVE_AGE_SECONDS,
        workspace=workspace,
        workspace_source=source,
        session_id=extract_session_id(path, lines),
        first_time=extract_first_time(path, lines),
        primary=is_primary_log(path, agent),
        priority=int(priority),
        compressed=compressed,
        # 压缩日志只能「发现」，不能实时跟读（跟读器按字节偏移走）。
        attachable=not compressed,
        task_goal=task_goal,
    )


def _read_sample_lines(path: str, max_read_bytes: int) -> List[str]:
    """读取文件头部的若干行作为样本。

    压缩日志（``.zstd``）走 :func:`_read_zstd_sample_lines` ——
    先解压再切行（v3.0.2：DeepSeek harness 的会话日志）。

    :param path: 日志路径。
    :param max_read_bytes: 最多读多少字节。
    :return: 样本行列表（读不到就是空列表）。
    """
    if is_compressed_log(path):
        return _read_zstd_sample_lines(path, max_read_bytes)

    try:
        limit = max(int(max_read_bytes), 0)
        with open(path, "rb") as handle:
            data = handle.read(limit)
    except (OSError, ValueError) as exc:
        logger.debug("读取日志头部失败 %s：%s", path, exc)
        return []

    return _split_sample_text(data.decode("utf-8", errors="replace"))


def _read_zstd_sample_lines(path: str, max_read_bytes: int) -> List[str]:
    """读取 **zstd 压缩**日志的头部若干行（v3.0.2）。

    用 Python 3.14 内置的 :mod:`compression.zstd`（PEP 784）流式
    解压 —— 只解压到够用的字节数，不整读（会话日志能有好几 MB）。

    ⚠️ 拿不到 zstd 实现（老 Python）或者文件损坏 → 返回空列表。
    审查侧异常一律降级（红线 R5），绝不抛出去挡住哨兵启动。

    :param path: 日志路径。
    :param max_read_bytes: 最多读多少**解压后**的字节。
    :return: 样本行列表（读不到就是空列表）。
    """
    limit = max(int(max_read_bytes), 0)
    if limit <= 0:
        return []

    try:
        from compression import zstd as _zstd
    except ImportError:
        logger.debug(
            "当前 Python 没有 compression.zstd，跳过压缩日志 %s", path
        )
        return []

    try:
        with _zstd.ZstdFile(path, "rb") as handle:
            data = handle.read(limit)
    except Exception as exc:  # noqa: BLE001 —— zstd 解压失败只降级
        logger.debug(
            "读取 zstd 日志失败 %s：%s: %s",
            path,
            type(exc).__name__,
            exc,
        )
        return []

    return _split_sample_text(data.decode("utf-8", errors="replace"))


def _split_sample_text(text: str) -> List[str]:
    """把整段文本切成样本行（最多 :data:`SAMPLE_LINE_LIMIT` 行）。

    :param text: 解码后的文本。
    :return: 样本行列表。
    """
    lines: List[str] = []
    for raw in text.split("\n"):
        stripped = raw.strip()
        if stripped:
            lines.append(stripped)
        if len(lines) >= SAMPLE_LINE_LIMIT:
            break
    return lines


# ---------------------------------------------------------------------------
# 函数 6：主扫描入口
# ---------------------------------------------------------------------------
def _normalize_exclude_keywords(
    keywords: Optional[Sequence[str]] = None,
) -> List[str]:
    """把排除关键词整理成一份干净清单（**实现**）。

    ⚠️ 这个私有函数存在的唯一理由：
    :func:`scan_agent_sessions` 的参数也叫 ``exclude_keywords``，
    在那个函数体里它会把同名的**公开函数**遮蔽掉 ——
    所以那里只能调这个带下划线的版本。

    :param keywords: 配置里的 ``radar.exclude_keywords``；
        ``None`` 表示用内置默认清单。
    :return: 小写、去空、去重后的关键词列表。
    """
    source = (
        DEFAULT_EXCLUDE_KEYWORDS if keywords is None else list(keywords or [])
    )
    cleaned: List[str] = []
    seen: set = set()
    for item in source:
        text = str(item or "").strip().lower()
        if not text or text in seen:
            continue
        seen.add(text)
        cleaned.append(text)
    return cleaned


def exclude_keywords(
    keywords: Optional[Sequence[str]] = None,
) -> List[str]:
    """把配置里的排除关键词整理成一份干净清单（v3.4.0）。

    :param keywords: 配置里的 ``radar.exclude_keywords``；
        ``None`` 表示用内置默认清单。
    :return: 小写、去空、去重后的关键词列表。
    """
    return _normalize_exclude_keywords(keywords)


def path_matches_keywords(
    path: str,
    keywords: Optional[Sequence[str]] = None,
) -> bool:
    """这个路径是不是**该被排除**（v3.4.0）。

    匹配方式是**子串**匹配（不区分大小写）——
    关键词命中路径的任意一部分就算。

    ⚠️ 既然是子串匹配，``test`` 会顺带命中 ``contest`` 这类目录。
    :param path: 日志路径。
    :param keywords: 关键词清单；``None`` 用内置默认清单。
    :return: 该排除返回 ``True``。
    """
    text = str(path or "").lower()
    if not text:
        return False
    return any(keyword in text for keyword in exclude_keywords(keywords))


def scan_agent_sessions(
    *,
    roots: Optional[Sequence[Any]] = None,
    max_age: float = 300.0,
    limit: int = 50,
    home: Optional[str] = None,
    now: Optional[float] = None,
    exclude_paths: Optional[Sequence[str]] = None,
    scan_roots: Optional[Sequence[str]] = None,
    process_cwd_resolver: Optional[Callable[[str, str], str]] = None,
    active_only: bool = False,
    exclude_keywords: Optional[Sequence[str]] = None,
    prompt_index: Optional[Mapping[str, str]] = None,
) -> List[DiscoveredSession]:
    """扫描各 Agent 的会话目录，找出**正在被活跃写入**的日志。

    * 每个 root 递归遍历，最多下探 :data:`MAX_SCAN_DEPTH` 层；
    * 只收集 ``.jsonl`` / ``.json`` / ``.log`` / ``.zstd`` 的普通文件，
      超过 :data:`MAX_FILE_BYTES` 的直接跳过；
    * 隐藏目录会被跳过 —— 但 Agent 的家目录（``.codex`` 之类）
      是豁免的，那正是要找的地方；
    * **已经被挂载**的文件直接跳过（v2.4.0）—— 同一份日志不必
      读两遍，省下的都是实打实的 I/O；
    * 单次运行最多检查 :data:`MAX_CANDIDATE_FILES` 个候选文件；
    * 同一个**会话**只保留一条（v3.0.2）—— DeepSeek 的同一个会话
      目录里可能同时躺着 ``session.v3`` 与 ``session.v4`` 两个文件，
      全列出来又是「满屏垃圾」；
    * 结果按 ``(活跃主日志, 根优先级, mtime)`` 排序，
      截断到 ``limit``。

    整个函数**不抛异常**：目录读不了就跳过那个目录。

    :param roots: ``{"agent", "label", "path"}`` 字典列表；
        ``None`` 表示用 :func:`agent_roots` 的默认清单。
    :param max_age: 活跃判定阈值（秒）。
    :param limit: 最多返回多少条。
    :param home: 家目录（仅在 ``roots`` 为 ``None`` 时用到）。
    :param now: 「现在」的 epoch 秒。
    :param exclude_paths: 要**跳过**的日志路径（通常是已经挂载进
        哨兵的那些）；比较时按平台规则归一化大小写。
    :param scan_roots: 配置里的扫描根目录（``radar.scan_roots``）；
        仅在 ``roots`` 为 ``None`` 时用到。
    :param process_cwd_resolver: 进程工作目录查询回调
        （回退链第三步）；签名 ``(path, agent) -> str``。
    :param active_only: 只返回「正在写入的会话主日志」（v3.0.2）。
    :param exclude_keywords: 路径里命中这些词的**一律不返回**
        （v3.4.0）—— 用来挡掉哨兵自己压测留下的残留日志。
        ⚠️ ``None`` 表示「**不过滤**」——
        默认清单（``sentinel`` / ``stress`` / ``test``）由**调用方**
        （看板服务）决定并显式传进来，
        这个库函数自己不替用户做主。
    :param prompt_index: ``{归一化工作目录: Prompt}`` 索引（v3.5.2）——
        日志里翻不到任务锚点时，按工作目录去命令行索引里找。
        ⚠️ 调用方**整轮只建一次**再传进来 ——
        否则每个候选文件都扫一遍系统进程，慢到点一下「发现」要等半天。
    :return: 会话描述列表（最值得关注的在最前）。
    """
    root_list = (
        agent_roots(home, scan_roots=scan_roots)
        if roots is None
        else _normalize_roots(roots)
    )

    current = time.time() if now is None else float(now)
    age_limit = float(max_age)
    excluded = _normalize_exclusions(exclude_paths)
    # ⚠️ 这里的 ``None`` 表示「**不过滤**」——
    #
    #    排除清单是**产品决定**的，不是这个库函数决定的：
    #    看板服务会把当前生效的清单（默认 ``sentinel`` / ``stress`` /
    #    ``test``）明确传进来。
    #
    #    如果库函数自己就默认排除 ``test``，
    #    路径里带 ``test`` 的**正常**目录会被误伤
    #    （例如 ``D:\\projects\\test-suite``、pytest 的临时目录）——
    #    那是「替用户做主」，必须留给上层决定。
    blocked = (
        _normalize_exclude_keywords(exclude_keywords)
        if exclude_keywords
        else []
    )

    found: List[DiscoveredSession] = []
    checked = 0

    for root in root_list:
        if checked >= MAX_CANDIDATE_FILES:
            break
        root_path = str(root.get("path") or "")
        if not root_path:
            continue
        try:
            if not os.path.isdir(root_path):
                continue
        except OSError as exc:
            logger.debug("跳过不可读目录 %s：%s", root_path, exc)
            continue

        root_priority = int(root.get("priority") or 0)
        # 每个根各拿一份预算（v3.0.2）——
        # 否则 ``.codex`` 那种满是历史文件的根会把预算吃光，
        # 真正在跑的 ``.minimax`` / ``.dsh`` 一个都轮不上。
        candidates = _collect_candidates(root_path)

        if active_only:
            # 只保留「可能正在写入」的**会话主日志**——
            # 静默文件**连读都不用读**，附属文件也不是「会话」。
            # 这一步把绝大部分 I/O 直接省掉了。
            candidates = [
                item
                for item in candidates
                if current - item[1] <= age_limit and not item[2]
            ]

        for candidate, mtime, is_aux in candidates[:MAX_CANDIDATE_FILES_PER_ROOT]:
            if checked >= MAX_CANDIDATE_FILES:
                break
            checked += 1
            # 已经被挂载的日志不必再读一遍（v2.4.0）——
            # 雷达扫描是纯 I/O 密集操作，跳过它最省事。
            if _normalize_path(candidate) in excluded:
                continue
            # ⚠️ v3.4.0：排除**关键词**命中的路径 ——
            # 哨兵自己压测留下的残留日志不该出现在用户的列表里。
            #
            # 这一步放在**读文件之前** —— 不合格的候选连读都不用读，
            # 省下的都是实打实的 I/O。
            if path_matches_keywords(candidate, blocked):
                continue
            session = describe_session(
                candidate,
                now=current,
                process_cwd_resolver=process_cwd_resolver,
                priority=root_priority,
                prompt_index=prompt_index,
            )
            if session is None:
                continue
            # 扫描层的活跃判定用自己的阈值（可能比模块常量更宽）。
            session.active = session.age <= age_limit
            found.append(session)

    # 同一个会话只留**最近写**的那一份（v3.0.2）。
    found = _dedupe_by_session(found)

    if active_only:
        # 只要「正在写入」的文件 —— 这正是任务书要的
        # 「默认只显示活跃（正在写入）的会话」。
        #
        # ⚠️ 这里只按 ``active`` 过滤，**不**按 ``primary``
        #    —— 「主日志」是**展示层**的讲究（附属文件折叠），
        #    而「正在写入」是用户真正要的筛选。
        found = [item for item in found if item.active]

    # 排序：**活跃的会话主日志优先**，其次是**扫描根的优先级**
    # （``.codex`` 排最后 —— 里面全是历史死文件），
    # 最后按 mtime 倒序。
    #
    # ⚠️ 为什么不能只按 mtime 排
    # --------------------------
    # 一次最多返回 ``limit`` 条，而用户目录里可能堆着几百个历史文件。
    # 只按 mtime 排的话，**正在写入**的那个会被挤出列表 ——
    # 界面上于是「一个活跃会话都没有」，用户又要骂雷达扫不到东西。
    found.sort(
        key=lambda item: (
            item.active and item.primary,
            item.priority,
            item.mtime,
        ),
        reverse=True,
    )
    return _apply_root_quota(found, max(int(limit), 0))


def _round_robin(
    sessions: List[DiscoveredSession],
    limit: int,
) -> List[DiscoveredSession]:
    """在**各个扫描根之间轮流取**，直到取满 ``limit`` 条。

    ⚠️ 为什么需要（v3.0.2）
    *********************
    用户实测 v3.0.1 时，雷达列表**全是** ``.codex`` 的历史文件 ——
    因为那个根的历史会话最多，排序一压就把别的根全挤掉了。
    现在每个根轮流出场，MiniMax / DeepSeek 的会话才露得出来。

    :param sessions: 已排好序的会话列表。
    :param limit: 最多取多少条。
    :return: 轮流取出来的会话列表。
    """
    if limit <= 0:
        return []

    groups: Dict[int, List[DiscoveredSession]] = {}
    for item in sessions:
        groups.setdefault(item.priority, []).append(item)

    picked: List[DiscoveredSession] = []
    round_index = 0
    while len(picked) < limit:
        added = False
        # 优先级高的根先出场（``priority`` 大的排前面）。
        for priority in sorted(groups, reverse=True):
            bucket = groups[priority]
            if round_index < len(bucket):
                picked.append(bucket[round_index])
                added = True
                if len(picked) >= limit:
                    break
        if not added:
            break
        round_index += 1
    return picked


def _apply_root_quota(
    sessions: List[DiscoveredSession],
    limit: int,
) -> List[DiscoveredSession]:
    """在「各根轮流出场」的前提下，**先给正在写入的**。

    分两层处理：

    1. 「正在写入的会话主日志」—— 轮流取，用户一眼就能看到
       此刻真正在跑的那些；
    2. 剩下的名额再给「已静默 / 附属」的条目。

    :param sessions: 已排好序的会话列表。
    :param limit: 最多返回多少条。
    :return: 挑好的会话列表。
    """
    if limit <= 0:
        return []

    active: List[DiscoveredSession] = []
    quiet: List[DiscoveredSession] = []
    for item in sessions:
        if item.active:
            active.append(item)
        else:
            quiet.append(item)

    picked = _round_robin(active, limit)
    remaining = limit - len(picked)
    if remaining > 0:
        picked.extend(_round_robin(quiet, remaining))
    return picked


def _dedupe_by_session(
    sessions: List[DiscoveredSession],
) -> List[DiscoveredSession]:
    """同一个**会话**只保留最近写的那一份（v3.0.2）。

    ⚠️ 为什么需要
    ************
    DeepSeek harness 的一个会话目录里可能同时躺着
    ``session.v3.jsonl.zstd`` 与 ``session.v4.jsonl.zstd`` ——
    它们是**同一个会话**的两种格式版本。全列出来用户就看到
    「同一个任务出现两次」，又是一件说不清的事。

    判定依据是「同一个 Agent + 同一个会话 ID」。取不到会话 ID 的
    就不合并（宁可可重复，也不误杀）。

    :param sessions: 扫描结果。
    :return: 去重后的列表（保持原有相对顺序）。
    """
    best: Dict[Tuple[str, str], DiscoveredSession] = {}
    order: List[Tuple[str, str]] = []
    passthrough: List[DiscoveredSession] = []

    for item in sessions:
        key = (item.agent, item.session_id) if item.session_id else ""
        if not key:
            passthrough.append(item)
            continue
        existing = best.get(key)
        if existing is None:
            best[key] = item
            order.append(key)
            continue
        if item.mtime > existing.mtime:
            best[key] = item

    merged = [best[key] for key in order]
    return merged + passthrough


def _normalize_exclusions(paths: Optional[Sequence[str]]) -> set:
    """把要排除的路径归一化成集合。

    :param paths: 路径列表。
    :return: 归一化后的路径集合。
    """
    result = set()
    for path in paths or ():
        key = _normalize_path(path)
        if key:
            result.add(key)
    return result


def _normalize_path(path: str) -> str:
    """按平台规则归一化路径（Windows 下忽略大小写）。

    :param path: 原始路径。
    :return: 归一化后的路径；入参为空就是空串。
    """
    text = str(path or "").strip()
    if not text:
        return ""
    try:
        text = os.path.abspath(os.path.expanduser(text))
    except (OSError, ValueError) as exc:
        # 归一化失败 —— 退回原样路径（下面照常 normcase）。
        # 要留痕：路径没归一化，雷达的「跳过已挂载文件」会**悄悄失效**。
        logger.warning(
            "归一化路径失败（%s）：%s: %s", text, type(exc).__name__, exc
        )
    return os.path.normcase(text)


def _normalize_roots(roots: Sequence[Any]) -> List[Dict[str, Any]]:
    """把调用方给的 roots 规整成统一形态。

    :param roots: 字典列表（或直接给路径字符串）。
    :return: ``{"agent", "label", "path", "priority"}`` 字典列表。
    """
    normalized: List[Dict[str, Any]] = []
    for entry in roots or ():
        if isinstance(entry, str):
            normalized.append(
                {
                    "agent": "unknown",
                    "label": AGENT_LABELS["unknown"],
                    "path": entry,
                    "priority": 0,
                }
            )
            continue
        if not isinstance(entry, dict):
            continue
        path = str(entry.get("path") or "")
        if not path:
            continue
        agent = str(entry.get("agent") or "unknown")
        label = str(entry.get("label") or AGENT_LABELS.get(agent, ""))
        if not label:
            label = AGENT_LABELS["unknown"]
        try:
            priority = int(entry.get("priority") or 0)
        except (TypeError, ValueError):
            priority = 0
        normalized.append(
            {
                "agent": agent,
                "label": label,
                "path": path,
                "priority": priority,
            }
        )
    return normalized


def _collect_candidates(root_path: str) -> List[Tuple[str, float, bool]]:
    """遍历一个 root，收集候选日志文件。

    不可读的目录直接跳过（``os.walk`` 的 ``onerror`` 吞掉），
    整个过程不会抛异常。

    ⚠️ 排序规则（v3.0.2）
    ****************
    两级排序：

    1. **会话主日志优先**，其次才是附属文件 ——
       深读一个文件的头部要几毫秒，一个根下动辄几千个文件，
       预算必须花在真正有用的文件上；
    2. 同一级内按**最后修改时间**倒序 —— 正在写入的那个一定排在
       最前面，不会被挤出列表。

    :param root_path: 根目录。
    :return: ``(路径, mtime, 是否附属文件)`` 列表。
    """
    def _on_error(exc: OSError) -> None:
        logger.debug("遍历目录出错 %s：%s", root_path, exc)

    main: List[Tuple[str, float, bool]] = []
    aux: List[Tuple[str, float, bool]] = []

    for dirpath, dirnames, filenames in os.walk(
        root_path, topdown=True, onerror=_on_error
    ):
        depth = _depth_of(dirpath, root_path)

        kept: List[str] = []
        for name in sorted(dirnames):
            if depth + 1 > MAX_SCAN_DEPTH:
                break
            if _should_skip_dir(name):
                continue
            kept.append(name)
        dirnames[:] = kept

        for name in sorted(filenames):
            full_path = os.path.join(dirpath, name)
            mtime = _candidate_mtime(full_path, name)
            if mtime is None:
                continue
            if _prefilter_primary(full_path):
                aux.append((full_path, mtime, True))
            else:
                main.append((full_path, mtime, False))

    main.sort(key=lambda item: item[1], reverse=True)
    aux.sort(key=lambda item: item[1], reverse=True)
    return main + aux


def _candidate_mtime(full_path: str, name: str) -> Optional[float]:
    """判断一个文件是不是候选会话日志，并返回它的 mtime。

    :param full_path: 文件完整路径。
    :param name: 文件名。
    :return: 是候选就返回 mtime（epoch 秒）；否则返回 ``None``。
    """
    if not name.lower().endswith(LOG_SUFFIXES):
        return None
    try:
        stat_result = os.stat(full_path)
    except (OSError, ValueError) as exc:
        logger.debug("跳过不可读文件 %s：%s", full_path, exc)
        return None
    if not stat_module.S_ISREG(stat_result.st_mode):
        return None
    size = int(stat_result.st_size)
    if size <= 0 or size > MAX_FILE_BYTES:
        return None
    return float(stat_result.st_mtime)


def _prefilter_primary(full_path: str) -> bool:
    """判断一个文件是不是「明确知道是附属文件」的垃圾。

    ⚠️ 为什么是**黑名单**而不是白名单（v3.0.2）
    ******************************************
    一开始这里用的是「文件名必须命中主日志规则」的白名单 ——
    结果自定义目录里那些名字不规范的会话日志（``rollout.jsonl``、
    ``old.jsonl``）被**全部漏掉**，雷达直接扫不到东西。
    白名单太狠了。

    现在改成**黑名单**：只标出「明确知道是附属文件」的那几个名字
    （mcode 的索引 / 目录 / 快照），其余一律当作候选。
    附件目录（``.codex`` 的 ``auth.json`` 之类）不在黑名单里 ——
    多读一个几十 KB 的文件，比漏掉用户的会话划算得多。

    ⚠️ 注意：附属文件**照样会被扫到**（v3.0.1 的契约不变），
    只是**排在后面读** —— 预算先花在真正的会话日志上。

    :param full_path: 文件完整路径。
    :return: 是已知附属文件就返回 ``True``。
    """
    name = os.path.basename(full_path).lower()
    if name in _AUXILIARY_FILE_NAMES:
        return True
    return bool(_SNAPSHOT_FILE_RE.match(name))


def _depth_of(dirpath: str, root_path: str) -> int:
    """算目录相对 root 的深度（root 本身是 0 层）。

    :param dirpath: 当前目录。
    :param root_path: 根目录。
    :return: 深度（负数表示算不出来，按 0 处理）。
    """
    try:
        relative = os.path.relpath(dirpath, root_path)
    except (OSError, ValueError):
        return 0
    if relative in (".", ""):
        return 0
    return len(relative.split(os.sep))


def _should_skip_dir(name: str) -> bool:
    """判断一个子目录要不要跳过。

    * ``node_modules`` 一律跳过；
    * 隐藏目录（以 ``.`` 开头）跳过 —— 但 Agent 的家目录
      （``.codex`` / ``.mcode`` / ``.claude``）豁免。

    :param name: 目录名。
    :return: 跳过就返回 ``True``。
    """
    if name in _SKIPPED_DIR_NAMES:
        return True
    if not name.startswith("."):
        return False
    lowered = name.lower()
    return not any(lowered.startswith(item) for item in _HIDDEN_DIR_EXEMPT)


def _is_candidate(full_path: str, name: str) -> bool:
    """判断一个文件是不是候选会话日志。

    ⚠️ v3.0.2 起这个函数只保留给**外部调用方**判断用 ——
    扫描流程走 :func:`_candidate_mtime`（一次 ``stat`` 拿到
    mtime 与大小，不用重复 stat）。

    :param full_path: 文件完整路径。
    :param name: 文件名。
    :return: 是候选就返回 ``True``。
    """
    if not name.lower().endswith(LOG_SUFFIXES):
        return False
    try:
        if not os.path.isfile(full_path):
            return False
        if os.path.getsize(full_path) > MAX_FILE_BYTES:
            return False
    except (OSError, ValueError) as exc:
        logger.debug("跳过不可读文件 %s：%s", full_path, exc)
        return False
    return True


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------
def _loads(line: str) -> Any:
    """安全地解析一行 JSON（失败返回 ``None``）。

    :param line: 文本行。
    :return: 解析结果；失败就是 ``None``。
    """
    try:
        return json.loads(line)
    except (TypeError, ValueError):
        return None
