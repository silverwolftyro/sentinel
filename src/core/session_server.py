"""哨兵 Sentinel —— 会话运行时核心（v5.0.0）。

v5.0.0 之前，这个文件叫 ``src/gui/server.py``，是 **8062 行**的
「本地只读监控面板」（FastAPI + Jinja2 + SSE）。

这一版把**面板整体剥离**，并把这个文件**搬到它该在的地方** ——
``src/core/session_server.py``。

为什么搬
========

旧文件里混着两种**完全不同的东西**：

* **前端胶水**（已剥离）：路由、模板、静态资源、托盘、
  ``create_app``（2839 行）、``main``（521 行）；
* **核心逻辑**（留下来）：告警仓库 ``AlertStore``、
  事件总线 ``EventHub``、跟读器 ``Watcher``、
  各日志行的解析器、**会话注册表**（``Session`` / ``SessionRegistry``）、
  目标锚点（``TaskGoal`` / ``WatchTarget``）。

后者**跟界面一点关系都没有** —— 放在 ``gui/`` 里本来就是放错了地方。
剥离前端正好是搬家的好时机。

⚠️ 搬的是**整个文件**，不是逐个符号 ——
因为这些类互相引用（``Session`` 用 ``AlertStore``、
``Watcher`` 用 ``EventHub``……），逐个搬要把依赖关系重新理一遍，
那是**另一个数量级**的工程。

边界（架构红线）
================

* **只读**：本模块只以 ``"rb"`` 打开被监控日志，绝不写入它；
* **与引擎解耦**：应用层**不 import** ``src.core.engine``。
  扫描能力由 ``scan_callback`` 注入 —— 回调跑完引擎、把告警写进告警日志，
  本模块的 tailer 再把新告警取出来。

两个 tailer 都是「只读新增字节」的增量跟随器，能识别**同名新文件**
（轮转）与**原地截断**。
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import io
import ipaddress
import json
import logging
import os
import sys
import threading
import time
from collections import deque
from typing import (
    Any,
    Callable,
    ClassVar,
    Deque,
    Dict,
    Iterable,
    List,
    Optional,
    Sequence,
    Set,
    Tuple,
)

from ..adapters.fingerprint import fingerprint_label, fingerprint_stream_key

#: 版本号再导出 —— 测试与调用方习惯从本模块取它。
from ..gui.shutdown import ShutdownCoordinator
from ..tailer import FileTailer as _FileTailer

__all__ = [
    "AlertStore",
    "EventHub",
    "FileTailer",
    "Watcher",
    "normalize_alert",
    "parse_alert_line",
]

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: 告警日志里的机器可读标记（其后紧跟一段 JSON）。
ALERT_JSON_MARKER = "ALERT_JSON"

#: 「解释补充」行的标记 —— 与 :mod:`src.core.engine` 的同名常量是一对
#: **日志格式契约**（引擎写、看板读），两边各持一份。
#:
#: L2 解释是**异步**的：原始告警先发，解释回来后另起一行做**追加更新**，
#: 看板据此把解释**合并进已有卡片**，而不是新开一张。
ALERT_UPDATE_MARKER = "ALERT_UPDATE_JSON"

#: 「Token 累计」行的标记（v1.1.0）—— 同样是一对**日志格式契约**。
#:
#: 引擎每累计够一定量就写一行，看板据此刷新顶部的「已消耗 Token」计数器。
#: 它**不是**告警 —— 所以单独解析，不混进告警列表。
TOKEN_MARKER = "TOKEN_JSON"  # noqa: S105 —— 这是**格式标记**，不是密钥

#: 「操作事件」行的标记（v2.2.0）—— 同样是一对**日志格式契约**。
#:
#: 告警说的是「**发现了什么**」，事件说的是「**我们做了什么**」：
#: 开始目标、复活重试、回滚、注入、热重载。
#: 它**不是**告警 —— 所以单独解析，不混进告警列表，
#: 而是拿去拼「案发时间线」。
EVENT_MARKER = "EVENT_JSON"

#: 「实时审核流水」行的标记（v3.3.1）—— 同样是一对**日志格式契约**。
#:
#: L1 判定为 ``ON_TRACK`` 的事件会被引擎压成一行极简摘要写进告警日志，
#: 看板据此渲染「实时审核流水」——
#: 让用户看见「哨兵一直在看」，而不是盯着一块空面板猜它死没死。
#:
#: 它**不是**告警 —— 所以单独解析，不混进告警列表。
ONTRACK_MARKER = "ONTRACK_JSON"

#: 「级联审核统计」行的标记（v3.5.0）—— 同样是一对**日志格式契约**。
#:
#: L2 本地模型每过滤掉一次冗余、或每次级联判定收尾，
#: 引擎就写一行 ``CASCADE_JSON {json}``；
#: 看板顶部那个「L2 已过滤 X 次冗余，为您节省 Y 次云端调用」
#: 就是靠它刷新的。
#:
#: 它**不是**告警 —— 所以单独解析，不混进告警列表。
CASCADE_MARKER = "CASCADE_JSON"

#: 默认监听地址（只允许本机）。
DEFAULT_HOST = "127.0.0.1"

#: 默认监听端口。
DEFAULT_PORT = 8000

#: 本 GUI 所在目录。
GUI_DIR = os.path.dirname(os.path.abspath(__file__))

#: 项目根目录（``src/`` 的上一级）。
#:
#: ⚠️ 打包成 exe 之后，「项目根」必须改成 **exe 所在目录** ——
#: 否则日志、演示文件会落进 PyInstaller 的临时解包目录，
#: 程序一关就全没了，用户什么都留不下。
PROJECT_ROOT = os.path.dirname(os.path.dirname(GUI_DIR))
if getattr(sys, "frozen", False):
    PROJECT_ROOT = os.path.dirname(os.path.abspath(sys.executable))

#: 打包成 exe 后的资源根目录（PyInstaller 把数据文件解到 ``sys._MEIPASS``）。
BUNDLE_DIR = getattr(sys, "_MEIPASS", "") or ""


def _capture_path() -> str:
    """「快捷重定向命令」默认把 Agent 输出落到哪个文件（v3.3.2）。

    用户在终端里跑 ``mcode exec "任务" > <这个路径>``，
    哨兵就能顺着这个文件找到日志 —— 这正是雷达看不见日志时
    最省事的解法。

    :return: 捕获文件的绝对路径。
    """
    return os.path.join(PROJECT_ROOT, "logs", "capture.jsonl")


def _ensure_capture_dir() -> str:
    """把捕获文件所在的目录建出来（v3.3.2）。

    重定向是 shell 干的活 —— 目录不存在的话 ``>`` 会直接报错，
    命令根本跑不起来。所以这里先把目录备好。

    :return: 捕获文件的绝对路径；建目录失败时照样返回路径。
    """
    path = _capture_path()
    folder = os.path.dirname(path)
    try:
        os.makedirs(folder, exist_ok=True)
    except OSError as exc:
        logger.warning("创建捕获目录失败：%s: %s", type(exc).__name__, exc)
    return path


def _resource_dir(*parts: str) -> str:
    """定位随包资源目录（源码运行与打包运行都能找到）。

    打包后资源有两个可能的位置：

    1. ``<_MEIPASS>/src/gui/<parts>`` —— 保持源码目录结构打包时的布局；
    2. ``<_MEIPASS>/<parts>`` —— 扁平打包时的布局。

    两个都试一遍，找到哪个用哪个；都没有就退回源码目录
    （源码运行时它本来就是对的）。

    :param parts: 相对路径片段。
    :return: 目录绝对路径。
    """
    if BUNDLE_DIR:
        for candidate in (
            os.path.join(BUNDLE_DIR, "src", "gui", *parts),
            os.path.join(BUNDLE_DIR, *parts),
        ):
            if os.path.isdir(candidate):
                return candidate
    return os.path.join(GUI_DIR, *parts)


#: Jinja2 模板目录。
TEMPLATES_DIR = _resource_dir("templates")

#: 静态资源目录。
STATIC_DIR = _resource_dir("static")

#: 默认告警日志路径（相对项目根，避免受当前工作目录影响）。
DEFAULT_ALERT_LOG = os.path.join(PROJECT_ROOT, "logs", "sentinel_alert.log")

#: 回环主机名白名单。
_LOOPBACK_NAMES = frozenset(
    {"localhost", "localhost.localdomain", "testserver"}
)

#: 允许出现在 Host 头里的端口（测试客户端用 ``testserver``）。
_IGNORED_HOSTS = frozenset({"testserver"})

#: 「监听所有网卡」的地址（v3.4.0）。
#:
#: ⚠️ 它**不是**能打开浏览器的地址 ——
#: 所以托盘的【打开看板】要把它换成 ``127.0.0.1``。
_ANY_INTERFACE = "0.0.0.0"  # noqa: S104 —— 这是要**识别**的地址，不是要监听的

#: 「活跃 Agent 感知」结果的缓存时间（秒）（v3.3.2）。
#:
#: 枚举系统进程不便宜（几十到几百毫秒），
#: 而看板每几秒就会问一次 —— 所以缓存一小会儿。
AGENT_ACTIVITY_TTL = 8.0

#: 「同类型 Agent 的日志最近被写过」算「被监控」的窗口（秒）（v3.3.2）。
#:
#: ⚠️ 这一条是**推断**，不是证据 —— 所以窗口不能太长：
#: 十分钟前的日志跟现在跑着的进程多半没关系。
AGENT_RECENT_SECONDS = 600.0

#: 「活跃 Agent 感知」的结果缓存。
_activity_cache: Dict[str, Any] = {"at": 0.0, "data": {}}

#: 缓存的锁（后台线程与事件循环都会碰它）。
_activity_lock = threading.Lock()


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _match_monitored(
    proc: Any,
    monitored: List[Dict[str, Any]],
) -> str:
    """判断一个活跃 Agent 进程**是不是已经被监控**（v3.3.2）。

    三条信号，从**确定**到**推断**：

    1. ``cmdline`` —— 进程命令行里带着某个正在监控的日志路径
       （重定向场景，**确定**是它）；
    2. ``cwd`` —— 会话的工作目录就是进程的工作目录
       （同一个项目，**确定**是它）；
    3. ``recent`` —— 同类型 Agent 的日志**落在雷达的扫描目录里**
       且**刚被写过**（**推断**）。

    ⚠️ 第 3 条是**推断**，所以条件卡得很死 ——
    宁可多提示一句，也不能让用户以为「被监控了」其实没有。

    :param proc: :class:`src.procscan.AgentProcess` 实例。
    :param monitored: 正在监控的会话列表。
    :return: 命中方式（``cmdline`` / ``cwd`` / ``recent``）；没命中就是空串。
    """
    cmdline = os.path.normcase(str(getattr(proc, "cmdline", "") or ""))
    cwd = os.path.normcase(str(getattr(proc, "cwd", "") or ""))
    agent = str(getattr(proc, "agent", "") or "").lower()

    for row in monitored:
        path = str(row.get("path") or "")
        if path and cmdline and os.path.normcase(path) in cmdline:
            return "cmdline"
    for row in monitored:
        workspace = str(row.get("workspace") or "")
        if workspace and cwd and os.path.normcase(workspace) == cwd:
            return "cwd"
    for row in monitored:
        if not agent:
            continue
        if not row.get("recent") or not row.get("under_scan_root"):
            continue
        if str(row.get("agent") or "") == agent:
            return "recent"
    return ""


def _pid_alive(pid: int) -> Optional[bool]:
    """某个 PID 现在还活着吗（v3.4.0）。

    ⚠️ ``psutil`` 缺失时返回 ``None``（**不知道**）——
    调用方**不能**据此把状态改成「已退出」：
    「查不到」与「死了」是两回事，混起来会误报。

    :param pid: 进程 ID。
    :return: 活着 ``True``、死了 ``False``、不知道 ``None``。
    """
    from ..procscan import pid_alive

    return pid_alive(pid)


def _is_loopback_host(host: str) -> bool:
    """判断监听地址是否是回环地址。

    「回环」是哨兵的第一道安全边界：绑定到回环意味着别的机器连不上。

    :param host: 监听地址，例如 ``127.0.0.1`` / ``localhost``。
    :return: 是回环地址返回 ``True``。
    """
    value = str(host or "").strip().lower()
    if not value:
        return False
    if value in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def _host_of(host_header: str) -> str:
    """从 Host 头里取出主机名（兼容 IPv6 的 ``[::1]:8000`` 写法）。"""
    value = str(host_header or "").strip()
    if not value:
        return ""
    if value.startswith("["):
        return value[1:].split("]", 1)[0].lower()
    if ":" in value:
        return value.rsplit(":", 1)[0].lower()
    return value.lower()


def _allowed_origins(
    host: str,
    port: int,
    allow_remote: bool,
) -> Set[str]:
    """构造跨站来源白名单。

    永远包含本机的两个写法（``127.0.0.1`` 与 ``localhost``）；
    只有显式允许远程监听时，才把实际监听地址加进来。

    :param host: 监听地址。
    :param port: 监听端口。
    :param allow_remote: 是否允许绑定非回环地址。
    :return: 允许的 Origin 集合。
    """
    origins = {
        f"http://127.0.0.1:{port}",
        f"http://localhost:{port}",
    }
    if allow_remote:
        origins.add(f"http://{host}:{port}")
    return origins


# ---------------------------------------------------------------------------
# 文件跟随
# ---------------------------------------------------------------------------
#: 文件增量跟随器下沉到 :mod:`src.tailer` —— 采集层的 ``tail_stream()``
#: 与看板都需要它（轮转 / 截断 / 半行等待的逻辑完全相同），
#: 放在一处才不会分叉。这里保留这个名字是为了兼容既有导入。
FileTailer = _FileTailer


# ---------------------------------------------------------------------------
# 告警解析
# ---------------------------------------------------------------------------
def parse_alert_line(line: str) -> Optional[Dict[str, Any]]:
    """从一行告警日志里解析出结构化告警。

    告警日志里有两类机器行：

    * ``ALERT_JSON {json}``       —— 一条**新**告警（可能还没有解释）；
    * ``ALERT_UPDATE_JSON {json}`` —— 对**已有**告警的**追加更新**
      （L2 解释异步回来了）。

    只有机器行会被解析，人读的那行直接忽略。
    更新行会被打上 ``update=True``，看板据此**合并**而不是新开一张卡片。

    :param line: 告警日志的一行。
    :return: 结构化告警字典；不是机器行或 JSON 非法时返回 ``None``。
    """
    for marker, is_update in (
        (ALERT_UPDATE_MARKER, True),
        (ALERT_JSON_MARKER, False),
    ):
        index = line.find(marker)
        if index < 0:
            continue
        payload = line[index + len(marker):].strip()
        if not payload.startswith("{"):
            return None
        try:
            data = json.loads(payload)
        except (json.JSONDecodeError, ValueError) as exc:
            logger.debug("告警 JSON 解析失败：%s", exc)
            return None
        if not isinstance(data, dict):
            return None
        if is_update:
            data["update"] = True
        return data
    return None


def parse_token_line(line: str) -> Optional[Dict[str, Any]]:
    """从一行告警日志里解析出 Token 累计。

    ``TOKEN_JSON {json}`` 是引擎写的「账单行」（v1.1.0）——
    它**不是**告警，所以单独解析，不混进告警列表。

    :param line: 告警日志的一行。
    :return: Token 快照字典；不是账单行或 JSON 非法时返回 ``None``。
    """
    index = line.find(TOKEN_MARKER)
    if index < 0:
        return None
    payload = line[index + len(TOKEN_MARKER):].strip()
    if not payload.startswith("{"):
        return None
    try:
        data = json.loads(payload)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.debug("Token JSON 解析失败：%s", exc)
        return None
    if not isinstance(data, dict):
        return None
    return data


def parse_ontrack_line(line: str) -> Optional[Dict[str, Any]]:
    """从一行告警日志里解析出「实时审核流水」条目（v3.3.1）。

    ``ONTRACK_JSON {json}`` 是引擎写的「流水行」——
    L1 判定为 ``ON_TRACK`` 的事件被压成一句摘要（``正常读取: auth.py``）。

    它**不是**告警，所以单独解析，不混进告警列表。

    :param line: 告警日志的一行。
    :return: 流水条目字典；不是流水行或 JSON 非法时返回 ``None``。
    """
    index = line.find(ONTRACK_MARKER)
    if index < 0:
        return None
    payload = line[index + len(ONTRACK_MARKER):].strip()
    if not payload.startswith("{"):
        return None
    try:
        data = json.loads(payload)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.debug("实时审核流水 JSON 解析失败：%s", exc)
        return None
    if not isinstance(data, dict):
        return None
    if not str(data.get("summary") or "").strip():
        return None
    return data


def _safe_float(value: Any) -> float:
    """把任意值安全地转成浮点数（转不了就是 0.0）。

    刻意**不**用 ``try/except: pass`` —— 红线不允许：
    裸吞异常会让「时间戳坏了」这件事**完全看不见**。

    :param value: 任意值。
    :return: 浮点数；转不了返回 ``0.0``。
    """
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def parse_cascade_line(line: str) -> Optional[Dict[str, Any]]:
    """从一行告警日志里解析出「级联审核统计」（v3.5.0）。

    ``CASCADE_JSON {json}`` 是引擎写的统计行 ——
    L2 判了几次、L3 判了几次、**省下了几次云端调用**。

    :param line: 告警日志的一行。
    :return: 统计字典；不是统计行或 JSON 非法时返回 ``None``。
    """
    index = line.find(CASCADE_MARKER)
    if index < 0:
        return None
    payload = line[index + len(CASCADE_MARKER):].strip()
    if not payload.startswith("{"):
        return None
    try:
        data = json.loads(payload)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.debug("级联统计 JSON 解析失败：%s", exc)
        return None
    if not isinstance(data, dict):
        return None
    return data


def _ontrack_clock(timestamp: Any) -> str:
    """把流水条目的时间戳格式化成 ``HH:MM:SS``（v3.3.1）。

    刻意放在**后端**做 —— 前端只负责渲染。
    这样时间格式只有一个解释者（时区、精度都不会各写各的）。

    :param timestamp: Unix 时间戳（秒）。
    :return: 形如 ``12:01:05`` 的时间文本。
    """
    # 惰性导入 —— 看板层不静态依赖 core 包。
    from ..core.live_audit import format_clock

    return format_clock(timestamp)


def parse_event_line(line: str) -> Optional[Dict[str, Any]]:
    """从一行告警日志里解析出「操作事件」（v2.2.0）。

    ``EVENT_JSON {json}`` 是引擎写的「操作行」——
    开始目标、复活重试、回滚、注入、热重载……
    它**不是**告警，所以单独解析，不混进告警列表。

    :param line: 告警日志的一行。
    :return: 事件字典；不是事件行或 JSON 非法时返回 ``None``。
    """
    index = line.find(EVENT_MARKER)
    if index < 0:
        return None
    payload = line[index + len(EVENT_MARKER):].strip()
    if not payload.startswith("{"):
        return None
    try:
        data = json.loads(payload)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.debug("事件 JSON 解析失败：%s", exc)
        return None
    if not isinstance(data, dict):
        return None
    if not str(data.get("kind") or "").strip():
        return None
    return data


def _synthesize_alert_id(
    timestamp: float, rule_id: str, reason: str
) -> str:
    """给**没有** ``alert_id`` 的告警行合成一个稳定的 ID。

    为什么需要
    =========

    早几版的告警日志行里**没有** ``alert_id`` 字段。
    看板要用这个 ID 做两件事：

    * 把 L2 解释淡进**同一张**卡片；
    * 按 ID 找回告警，生成【复制纠偏提示】。

    没有 ID 的卡片点「复制纠偏提示」会**静默失败** ——
    用户看到按钮，按了，什么也没发生。

    合成的 ID 必须**稳定**：同一行日志，每次读都要得到同一个值，
    否则「按 ID 找回」就找不到了。这里用
    「毫秒时间戳 + 规则名 + 原因摘要」拼出来。

    :param timestamp: 告警时间戳。
    :param rule_id:   规则 ID。
    :param reason:    规则原因。
    :return: 形如 ``1793026126391-a1b2c3`` 的 ID。
    """
    # sha1 只用来生成**去重 ID**，不涉及任何安全用途。
    digest = hashlib.sha1(  # noqa: S324
        f"{rule_id}|{reason}".encode("utf-8", "ignore")
    ).hexdigest()[:6]
    return f"{int(timestamp * 1000)}-{digest}"


def normalize_alert(raw: Dict[str, Any]) -> Dict[str, Any]:
    """补齐结构化告警的缺省字段，让前端可以无脑渲染。

    :param raw: 告警日志里解析出来的原始字典。
    :return: 字段齐全的新字典（不修改入参）。
    """
    arguments = raw.get("arguments")
    if not isinstance(arguments, dict):
        arguments = {}
    try:
        timestamp = float(raw.get("timestamp") or 0.0)
    except (TypeError, ValueError):
        timestamp = 0.0
    rule_id = str(raw.get("rule_id") or "-")
    reason = str(raw.get("reason") or "")
    alert_id = str(raw.get("alert_id") or "").strip()
    if not alert_id:
        alert_id = _synthesize_alert_id(timestamp, rule_id, reason)
    return {
        "alert_id": alert_id,
        "update": bool(raw.get("update")),
        "timestamp": timestamp,
        "verdict": str(raw.get("verdict") or "UNKNOWN"),
        "level": str(raw.get("level") or "warn"),
        "rule_id": rule_id,
        "reason": reason,
        "evidence": str(raw.get("evidence") or ""),
        "severity": str(raw.get("severity") or "standard"),
        "category": str(raw.get("category") or ""),
        "source": str(raw.get("source") or "-"),
        "event_type": str(raw.get("event_type") or "-"),
        "tool": str(raw.get("tool") or "-"),
        "command": str(raw.get("command") or ""),
        "arguments": arguments,
        "explanation": str(raw.get("explanation") or ""),
        "explanation_degraded": bool(raw.get("explanation_degraded")),
        "explanation_reason": str(raw.get("explanation_reason") or ""),
        "explanation_pending": bool(raw.get("explanation_pending")),
        "guardian": raw.get("guardian") if isinstance(
            raw.get("guardian"), dict
        ) else {},
        # --- Token 审计（v1.1.0）---
        "token_usage": raw.get("token_usage") if isinstance(
            raw.get("token_usage"), dict
        ) else {},
        "token_saved": raw.get("token_saved") if isinstance(
            raw.get("token_saved"), dict
        ) else {},
        "token_saved_text": str(raw.get("token_saved_text") or ""),
        # --- 安全点（v2.0.0）---
        # 告警卡片上的【一键回滚至拦截点】靠它判断
        # 「这次拦截到底有没有留下可回滚的快照」。
        "safe_point": raw.get("safe_point") if isinstance(
            raw.get("safe_point"), dict
        ) else {},
    }


def _audit_stream_entry(normalized: Dict[str, Any]) -> Dict[str, Any]:
    """把一条告警变成「实时审核流水」条目（v3.5.1）。

    流水区以前只有「一切正常」的灰字 —— 真正出事的那一刻
    反而**看不见**。v3.5.1 把告警也放进同一条流水里，
    按严重程度分成橘黄 / 红两色。

    ⚠️ 为什么必须由**服务端**进快照，而不是前端临时追加：
    看板每 15 秒会用服务端快照**整段重画**流水区，
    前端追加的行会被那次重画整个抹掉。
    告警一旦由服务端权威地进快照，
    刷新页面、切会话都还在。

    :param normalized: 归一化后的告警字典。
    :return: 流水条目字典（副本）。
    """
    entry = dict(normalized)
    # stream_kind 是**流水区专用**标记 ——
    # 告警自己还有个 event_type（触发它的那次事件类型），
    # 拿它当判据会把「tool_call 触发的告警」认错。
    entry["stream_kind"] = "alert"
    entry["time"] = _ontrack_clock(entry.get("timestamp"))
    return entry


# ---------------------------------------------------------------------------
# 状态存储
# ---------------------------------------------------------------------------
class WatchTarget:
    """「当前监控目标」的可变快照。

    面板顶上要显示三件事：

    * **正在监控的 Agent 类型**（mcode / claude / codex …）；
    * **任务目标**（意图锚点）；
    * **这个目标是哪儿来的** —— 人工指定 / 自动提取 / 配置文件 / 没有。

    三者都会在**运行中**变（Agent 类型不会，目标会），
    所以这里是一个带锁的持有者，外部随时更新，看板随时拉取。

    v2.3.0 又加了一项：**工作区**（被监控 Agent 正在哪个目录里干活）——
    「雷达」挂载进来的会话尤其需要它：
    光知道「有个 Codex 在跑」没用，得知道**它跑的是哪个项目**。

    :param agent: 被监控的 Agent 类型。
    :param goal: 当前任务目标。
    :param goal_source: 目标来源（``none`` / ``config`` / ``explicit`` / ``auto``）。
    :param workspace: 被监控 Agent 的工作目录。
    """

    #: 目标来源的中文说明（给面板直接显示）。
    #:
    #: v3.3.1：``explicit`` 的说法从「人工指定」改成「手动锚定」——
    #: 顶部要显示的是**结果**（漂移监控已经锚上了），
    #: 而不是「这个值是你手填的」这种**过程**描述。
    GOAL_SOURCE_LABELS: ClassVar[Dict[str, str]] = {
        "none": "未设置",
        "config": "配置文件",
        "explicit": "手动锚定",
        "auto": "自动提取",
    }

    def __init__(
        self,
        agent: str = "",
        goal: str = "",
        goal_source: str = "none",
        workspace: str = "",
    ) -> None:
        self._agent = str(agent or "")
        self._goal = str(goal or "")
        self._goal_source = str(goal_source or "none")
        self._workspace = str(workspace or "")
        self._lock = threading.Lock()

    def get(self) -> str:
        """取 Agent 类型。"""
        with self._lock:
            return self._agent

    def set_agent(self, agent: str) -> None:
        """设定被监控的 Agent 类型。

        :param agent: 类型名（``mcode`` / ``claude`` / ``codex`` …）。
        """
        with self._lock:
            self._agent = str(agent or "")

    def set_goal(self, goal: str, source: str = "explicit") -> None:
        """设定任务目标及其来源。

        :param goal: 目标文本。
        :param source: 来源标记。
        """
        with self._lock:
            self._goal = str(goal or "")
            self._goal_source = str(source or "none")

    def set_workspace(self, workspace: str) -> None:
        """设定被监控 Agent 的工作目录（v2.3.0）。

        :param workspace: 工作目录；空串表示「未知工作区」。
        """
        with self._lock:
            self._workspace = str(workspace or "")

    def snapshot(self) -> Dict[str, Any]:
        """取一份快照（给 API / SSE 用）。

        :return: 含 ``agent`` / ``goal`` / ``goal_source`` /
            ``goal_source_label`` / ``auto_anchored`` / ``workspace`` 的字典。
        """
        with self._lock:
            source = self._goal_source
            goal = self._goal
            agent = self._agent
            workspace = self._workspace
        return {
            "agent": agent,
            "goal": goal,
            "goal_source": source,
            "goal_source_label": self.GOAL_SOURCE_LABELS.get(
                source, source
            ),
            "auto_anchored": source == "auto",
            # 工作区（v2.3.0）—— 提取不到时前端显示「未知工作区」。
            "workspace": workspace,
        }


class TaskGoal:
    """运行时可改的「任务目标」（意图锚点）。

    哨兵靠它判断 Agent 是否还在做用户交代的事。目标可以在**运行中**改 ——
    看板顶栏的输入框就是干这个的。

    监听器（:meth:`on_change`）用于把变化**推到**正在运行的引擎：
    改了目标，引擎里的 :class:`~src.core.engine.IntentTracker`
    也要跟着换锚点，并把连续漂移计数清零。

    :param goal: 初始目标。
    """

    def __init__(self, goal: str = "") -> None:
        self._goal = str(goal or "")
        self._lock = threading.Lock()
        self._listeners: List[Callable[[str], None]] = []

    def get(self) -> str:
        """取当前目标。"""
        with self._lock:
            return self._goal

    def set(self, goal: str) -> str:
        """设定新目标，并通知所有监听器。

        :param goal: 新目标（空串表示停用意图漂移检测）。
        :return: 设定后的目标。
        """
        value = str(goal or "").strip()
        with self._lock:
            changed = value != self._goal
            self._goal = value
            listeners = list(self._listeners)
        if changed:
            for listener in listeners:
                try:
                    listener(value)
                except Exception as exc:  # noqa: BLE001 —— 监听器失败不影响
                    logger.warning("任务目标监听器失败：%s: %s", type(exc).__name__, exc)
        return value

    def on_change(self, callback: Callable[[str], None]) -> None:
        """注册一个「目标变了」的回调。"""
        with self._lock:
            self._listeners.append(callback)


class AlertStore:
    """内存中的告警与日志行存储（线程安全）。

    只存摘要，不落盘 —— 哨兵的持久化职责在告警日志那边。

    :param max_alerts: 最多保留多少条告警。
    :param max_lines: 最多保留多少行被监控日志。
    """

    def __init__(
        self,
        *,
        max_alerts: int = 500,
        max_lines: int = 200,
    ) -> None:
        self._lock = threading.Lock()
        self._alerts: Deque[Dict[str, Any]] = deque(maxlen=max(1, max_alerts))
        self._lines: Deque[str] = deque(maxlen=max(1, max_lines))
        #: 「操作事件」（v2.2.0）—— 开始目标、复活重试、热重载……
        #:
        #: 它**不是**告警，所以单独存一条线：混进告警列表的话，
        #: 看板会凭空多出「告警卡片」，用户会以为出了事。
        self._events: Deque[Dict[str, Any]] = deque(maxlen=max(1, max_alerts))
        #: 「实时审核流水」（v3.3.1）—— ON_TRACK 判定的极简摘要。
        #:
        #: 同样**不是**告警：它是「一切正常」的证据，
        #: 混进告警列表的话，用户会以为出了事。
        self._ontrack: Deque[Dict[str, Any]] = deque(maxlen=max(1, max_alerts))
        self._counts: Dict[str, int] = {}
        self._scan_counts: Dict[str, int] = {}
        #: 本次会话的 Token 累计（v1.1.0）。
        self._tokens: Dict[str, Any] = {}
        #: 级联审核统计（v3.5.0）—— L2 过滤次数与省下的云端调用。
        self._cascade: Dict[str, Any] = {}
        self._running = False
        self._paused = False

    # -- 写入 -----------------------------------------------------------
    def add_alert(self, alert: Dict[str, Any]) -> None:
        """追加一条告警并累加判定计数。"""
        with self._lock:
            self._alerts.append(alert)
            verdict = str(alert.get("verdict") or "UNKNOWN")
            self._counts[verdict] = self._counts.get(verdict, 0) + 1

    def update_alert(self, alert_id: str, fields: Dict[str, Any]) -> bool:
        """把**补充内容**合并进一条**已有**告警。

        L2 解释是异步的：原始告警先进列表，解释回来后再合并进来 ——
        列表里始终是「一条告警一张卡片」，不会因为解释晚到而多出一张。

        :param alert_id: 目标告警的 ID。
        :param fields: 要合并的字段（``explanation`` 等）。
        :return: 找到并合并了返回 ``True``；没找到返回 ``False``。
        """
        if not alert_id:
            return False
        with self._lock:
            for alert in self._alerts:
                if str(alert.get("alert_id") or "") == alert_id:
                    alert.update(fields)
                    return True
        return False

    def add_line(self, text: str) -> None:
        """追加一行被监控日志。"""
        with self._lock:
            self._lines.append(text)

    def add_event(self, event: Dict[str, Any]) -> None:
        """追加一条「操作事件」（v2.2.0）。

        :param event: 事件字典（``EVENT_JSON`` 解析结果）。
        """
        if not isinstance(event, dict):
            return
        with self._lock:
            self._events.append(dict(event))

    def add_ontrack(self, entry: Dict[str, Any]) -> None:
        """追加一条「实时审核流水」条目（v3.3.1）。

        :param entry: 流水条目字典（``ONTRACK_JSON`` 解析结果）。
        """
        if not isinstance(entry, dict):
            return
        with self._lock:
            self._ontrack.append(dict(entry))

    def set_running(self, running: bool) -> None:
        """标记 tailer 线程是否在跑。"""
        with self._lock:
            self._running = bool(running)

    def set_paused(self, paused: bool) -> None:
        """标记是否暂停推送（tailer 仍然继续读，数据不丢）。"""
        with self._lock:
            self._paused = bool(paused)

    def set_scan_counts(self, counts: Dict[str, Any]) -> None:
        """记录最近一次扫描引擎给出的判定计数（含 ON_TRACK）。"""
        cleaned: Dict[str, int] = {}
        for key, value in counts.items():
            try:
                cleaned[str(key)] = int(value)
            except (TypeError, ValueError):
                continue
        with self._lock:
            self._scan_counts = cleaned

    def set_tokens(self, snapshot: Dict[str, Any]) -> None:
        """记录本次会话的 Token 累计（v1.1.0）。

        引擎每累计够一定量就写一行 ``TOKEN_JSON``，看板据此刷新
        顶部的「已消耗 Token」计数器。

        :param snapshot: Token 快照（来自引擎的台账）。
        """
        if not isinstance(snapshot, dict):
            return
        cleaned: Dict[str, Any] = {}
        for key in (
            "input_tokens",
            "output_tokens",
            "cached_tokens",
            "estimated_tokens",
            "total_tokens",
            "events",
        ):
            try:
                cleaned[key] = int(snapshot.get(key) or 0)
            except (TypeError, ValueError):
                cleaned[key] = 0
        cleaned["estimated"] = bool(snapshot.get("estimated"))
        try:
            cleaned["avg_per_event"] = float(snapshot.get("avg_per_event") or 0.0)
        except (TypeError, ValueError):
            cleaned["avg_per_event"] = 0.0
        with self._lock:
            self._tokens = cleaned

    def tokens(self) -> Dict[str, Any]:
        """返回本次会话的 Token 累计（副本）。"""
        with self._lock:
            return dict(self._tokens)

    def set_cascade_stats(self, stats: Dict[str, Any]) -> None:
        """记录级联审核统计（v3.5.0）。

        引擎每跑完一次级联审核就写一行 ``CASCADE_JSON``，
        看板顶部那个「L2 已过滤 X 次冗余，为您节省 Y 次云端调用」
        就是靠它刷新的。

        :param stats: 统计字典（来自引擎的级联编排器）。
        """
        if not isinstance(stats, dict):
            return
        cleaned: Dict[str, Any] = {}
        for key in (
            "l2_calls",
            "l3_calls",
            "saved_cloud_calls",
            "unavailable_calls",
            "red_lights",
        ):
            try:
                cleaned[key] = int(stats.get(key) or 0)
            except (TypeError, ValueError):
                cleaned[key] = 0
        for key in ("l2_model", "l3_model", "last_unavailable_reason"):
            cleaned[key] = str(stats.get(key) or "")
        cleaned["breaker_open"] = bool(stats.get("breaker_open"))
        with self._lock:
            self._cascade = cleaned

    def cascade_stats(self) -> Dict[str, Any]:
        """返回级联审核统计（副本）。"""
        with self._lock:
            return dict(self._cascade)

    def streams(self) -> List[Dict[str, Any]]:
        """把告警与流水按**线程指纹**分组（v3.5.0）。

        多个 Agent 并行写**同一个**日志文件时，
        告警流其实是**交织**的 —— 看板据此把一份日志
        拆成多个**虚拟会话卡片**，各路互不干扰。

        :return: ``[{"key", "label", "alerts", "ontrack", "last_seen"}]``。
        """
        buckets: Dict[str, Dict[str, Any]] = {}
        with self._lock:
            alerts = list(self._alerts)
            ontrack = list(self._ontrack)
        for alert in alerts:
            key = fingerprint_stream_key(alert.get("fingerprint"))
            bucket = buckets.setdefault(
                key,
                {
                    "key": key,
                    "label": fingerprint_label(
                        "" if key == "default" else key
                    ),
                    "alerts": 0,
                    "ontrack": 0,
                    "last_seen": 0.0,
                },
            )
            bucket["alerts"] += 1
            bucket["last_seen"] = max(
                float(bucket["last_seen"]),
                _safe_float(alert.get("timestamp")),
            )
        for entry in ontrack:
            key = fingerprint_stream_key(entry.get("fingerprint"))
            bucket = buckets.setdefault(
                key,
                {
                    "key": key,
                    "label": fingerprint_label(
                        "" if key == "default" else key
                    ),
                    "alerts": 0,
                    "ontrack": 0,
                    "last_seen": 0.0,
                },
            )
            # v3.5.1：告警流水条目**不算**「正常」——
            # 它已经在 alerts 那栏里数过了，不能再数一遍。
            if str(entry.get("stream_kind") or "") != "alert":
                bucket["ontrack"] += 1
            bucket["last_seen"] = max(
                float(bucket["last_seen"]),
                _safe_float(entry.get("timestamp")),
            )
        return sorted(
            buckets.values(),
            key=lambda item: (-int(item["alerts"]), str(item["key"])),
        )

    # -- 读取 -----------------------------------------------------------
    def alerts(self) -> List[Dict[str, Any]]:
        """返回当前保留的告警列表（副本）。"""
        with self._lock:
            return list(self._alerts)

    def events(self) -> List[Dict[str, Any]]:
        """返回当前保留的「操作事件」列表（副本，v2.2.0）。

        :return: 事件字典列表。
        """
        with self._lock:
            return list(self._events)

    def ontrack(self) -> List[Dict[str, Any]]:
        """返回当前保留的「实时审核流水」列表（副本，v3.3.1）。

        :return: 流水条目字典列表。
        """
        with self._lock:
            return list(self._ontrack)

    def find_alert(self, alert_id: str) -> Optional[Dict[str, Any]]:
        """按 ``alert_id`` 找一条告警。

        :param alert_id: 告警 ID。
        :return: 告警字典（副本）；找不到返回 ``None``。
        """
        wanted = str(alert_id or "")
        if not wanted:
            return None
        with self._lock:
            for alert in self._alerts:
                if str(alert.get("alert_id") or "") == wanted:
                    return dict(alert)
        return None

    def recent_lines(self) -> List[str]:
        """返回当前保留的最近日志行（副本）。"""
        with self._lock:
            return list(self._lines)

    def counts(self) -> Dict[str, int]:
        """返回按判定聚合的告警计数（副本）。"""
        with self._lock:
            return dict(self._counts)

    def is_paused(self) -> bool:
        """当前是否处于暂停推送状态。"""
        with self._lock:
            return self._paused

    def is_running(self) -> bool:
        """当前是否有跟随线程在跑。"""
        with self._lock:
            return self._running

    def status(self) -> Dict[str, Any]:
        """返回运行状态摘要。"""
        with self._lock:
            if self._paused:
                status = "paused"
            elif self._running:
                status = "running"
            else:
                status = "idle"
            return {
                "status": status,
                "running": status == "running",
                "paused": self._paused,
                "counts": dict(self._counts),
                "tokens": dict(self._tokens),
            }

    def snapshot(
        self,
        watch_path: str,
        alert_log_path: str,
    ) -> Dict[str, Any]:
        """返回 ``/api/state`` 用的完整快照。"""
        # ⚠️ 必须**在加锁之前**取：``streams()`` / ``cascade_stats()``
        # 自己也要拿同一把锁，而它**不是**可重入锁 ——
        # 在 ``with`` 里面调就是自己等自己（死锁）。
        streams = self.streams()
        cascade = self.cascade_stats()
        with self._lock:
            if self._paused:
                status = "paused"
            elif self._running:
                status = "running"
            else:
                status = "idle"
            return {
                "status": status,
                "watch_path": watch_path,
                "alert_log_path": alert_log_path,
                "counts": dict(self._counts),
                "alerts": list(self._alerts),
                "recent_lines": list(self._lines),
                "running": status == "running",
                "paused": self._paused,
                "scan_counts": dict(self._scan_counts),
                # Token 累计（v1.1.0）——
                # ⚠️ 必须在这里带上：前端**切换会话时**走的就是这个接口
                # （``/api/sessions/{id}/state`` 与 ``/api/state``）。
                # 少这一行，计数器在切换会话后就会归零 ——
                # 哪怕 SSE 已经把正确的值推过来过。
                "tokens": dict(self._tokens),
                # 实时审核流水（v3.3.1）——
                # 同样必须在这里带上：前端切换会话 / 刷新页面时
                # 靠它把「实时审核流水」补回来，
                # 不然用户会看到一块**空白的**面板，
                # 误以为哨兵没在工作。
                "ontrack": list(self._ontrack),
                # 级联审核统计（v3.5.0）——
                # 顶部那个「L2 已过滤 X 次冗余，为您节省 Y 次云端调用」
                # 的数据源。切换会话 / 刷新页面时也靠它补回来。
                "cascade": cascade,
                # 线程指纹分路（v3.5.0）——
                # 同一份日志混着多个 Agent 时，前端据此拆成多张
                # **虚拟会话卡片**，各路互不干扰。
                "streams": streams,
            }


# ---------------------------------------------------------------------------
# SSE 分发
# ---------------------------------------------------------------------------
class EventHub:
    """SSE 事件分发中心。

    每个订阅者一个 ``asyncio.Queue``；后台线程用
    ``loop.call_soon_threadsafe`` 把事件塞进对应队列 ——
    这是从「线程」安全进入「事件循环」的官方做法，
    等价于 ``asyncio.run_coroutine_threadsafe``，但不会阻塞线程。

    :param max_queue: 单个订阅者的队列长度上限（超出就丢最旧的）。
    """

    def __init__(self, *, max_queue: int = 500) -> None:
        self._lock = threading.Lock()
        self._subscribers: Set["asyncio.Queue[Dict[str, Any]]"] = set()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._max_queue = max(int(max_queue), 1)

    # -- 订阅 -----------------------------------------------------------
    def subscribe(self) -> "asyncio.Queue[Dict[str, Any]]":
        """注册一个订阅者，返回它的事件队列。"""
        queue: "asyncio.Queue[Dict[str, Any]]" = asyncio.Queue(
            maxsize=self._max_queue
        )
        with self._lock:
            self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: "asyncio.Queue[Dict[str, Any]]") -> None:
        """注销一个订阅者。"""
        with self._lock:
            self._subscribers.discard(queue)

    def subscriber_count(self) -> int:
        """当前订阅者数量。"""
        with self._lock:
            return len(self._subscribers)

    # -- 分发 -----------------------------------------------------------
    def attach_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """绑定事件循环（应用启动时调用一次）。"""
        self._loop = loop

    def publish(self, event: Dict[str, Any]) -> None:
        """把事件广播给所有订阅者。

        可从任意线程调用。投递失败只记录日志，绝不影响生产者。

        :param event: 事件字典，必须含 ``type`` 字段。
        """
        with self._lock:
            targets = list(self._subscribers)
        for queue in targets:
            self._deliver(queue, event)

    def _deliver(
        self,
        queue: "asyncio.Queue[Dict[str, Any]]",
        event: Dict[str, Any],
    ) -> None:
        """把事件投递给一个订阅者。"""
        loop = self._loop
        if loop is not None and loop.is_running():
            try:
                loop.call_soon_threadsafe(queue.put_nowait, event)
                return
            except RuntimeError as exc:
                logger.debug("事件循环已关闭，丢弃事件：%s", exc)
                return
        # 没有事件循环（例如单元测试里直接调用）时直接入队。
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            logger.warning("订阅队列已满，丢弃事件：%s", event.get("type"))








# ---------------------------------------------------------------------------
# 后台跟随
# ---------------------------------------------------------------------------
class Watcher:
    """后台线程：同时跟随告警日志与被监控日志。

    两个 tailer 各读各的，**任何一次失败都只会记录日志**，
    绝不会让线程静默退出 —— 一个坏文件不该让整个面板失聪。

    :param watch_path: 被监控的 Agent 日志路径（只读）。
    :param alert_log_path: 哨兵告警日志路径（只读）。
    :param hub: 事件分发中心。
    :param store: 状态存储。
    :param session_id: 所属会话 ID —— 推给前端时带上，前端才知道
        「这条事件来自哪个 Agent」。
    :param poll_interval: 轮询间隔（秒）。
    :param recent_line_limit: 保留多少行被监控日志。
    :param prime_bytes: 首次加载历史内容时最多读多少字节。
    :param line_batch_size: 一批最多打包多少行日志（v2.4.0）。
    """

    def __init__(
        self,
        watch_path: str,
        alert_log_path: str,
        hub: EventHub,
        store: AlertStore,
        *,
        session_id: str = "default",
        poll_interval: float = 0.5,
        recent_line_limit: int = 200,
        prime_bytes: int = 256 * 1024,
        line_batch_size: int = 200,
    ) -> None:
        self.watch_path = os.path.abspath(watch_path)
        self.alert_log_path = os.path.abspath(alert_log_path)
        self.hub = hub
        self.store = store
        self.session_id = str(session_id or "default")
        self.poll_interval = max(float(poll_interval), 0.05)
        self.recent_line_limit = max(int(recent_line_limit), 1)
        self.prime_bytes = max(int(prime_bytes), 0)
        self.line_batch_size = max(int(line_batch_size), 1)
        self._alert_tailer = FileTailer(self.alert_log_path)
        self._watch_tailer = FileTailer(self.watch_path)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._finished = False
        # 待推送的日志行（v2.4.0）——
        #
        # Agent 跑得飞起时一秒能写几百行。逐行发 SSE = 一秒几百个
        # 事件，订阅队列（500 条）会被瞬间灌满、开始丢事件，
        # 前端也被 DOM 操作淹没。这里先攒着，攒够一批再发。
        self._pending_lines: List[str] = []
        self._pending_lock = threading.Lock()

    # -- 生命周期 -------------------------------------------------------
    def start(self) -> None:
        """启动后台跟随线程（幂等）。"""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._finished = False
        self._thread = threading.Thread(
            target=self._run,
            name=f"sentinel-watcher-{self.session_id}",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        """停止后台跟随线程。"""
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        self._thread = None
        # 别把最后攒下的几行丢掉（v2.4.0）。
        self._flush_lines()
        self.store.set_running(False)

    def is_finished(self) -> bool:
        """这个会话是不是已经**结束**了（跟随线程已退出）。"""
        return bool(self._finished)

    # -- 内部实现 -------------------------------------------------------
    def _run(self) -> None:
        """线程主循环：异常只记日志，永不退出。"""
        self.store.set_running(True)
        try:
            self._prime()
        except Exception as exc:  # noqa: BLE001 —— 预热失败不影响后续
            logger.warning(
                "预加载历史内容失败：%s: %s", type(exc).__name__, exc
            )
        self.hub.publish(self._event({"type": "status", **self.store.status()}))
        while not self._stop.is_set():
            try:
                self._poll_once()
            except Exception as exc:  # noqa: BLE001 —— 单轮失败不致命
                logger.exception(
                    "监控轮询失败：%s: %s", type(exc).__name__, exc
                )
            self._stop.wait(self.poll_interval)
        self.store.set_running(False)
        self._finished = True
        # 会话结束了 —— 告诉前端，让它把这一路标成「已结束」。
        self.hub.publish(self._event({"type": "session_state", "state": "finished"}))

    def _event(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """给事件打上会话 ID。

        多路会话下，前端必须知道「这条事件来自哪个 Agent」——
        否则两个 Agent 的告警会混在同一个列表里。

        ⚠️ 这里必须**强制覆盖**，不能用 ``setdefault``。
        引擎写进来的载荷里**可能已经带** ``session_id``，
        但那是**引擎侧的会话名**（比如 ``codex``）——
        跟看板的会话 ID（``stress-b``）是两套**不同的命名空间**。
        一旦被载荷里的旧值抢走，前端就会认为「这条不属于当前会话」
        而直接丢掉 —— 多路模式下 **Token 计数器会永远停在 0**。
        看板的会话 ID 是路由的唯一权威，监听器只属于一个会话，
        所以这里说了算。

        :param payload: 事件内容。
        :return: 带 ``session_id`` 的事件。
        """
        data = dict(payload)
        data["session_id"] = self.session_id
        return data

    def _prime(self) -> None:
        """首次加载历史内容，让面板一打开就有东西看。"""
        try:
            for line in self._alert_tailer.prime(self.prime_bytes):
                # 实时审核流水（v3.3.1）——
                # 历史里的流水也要收进来 ——
                # 看板**刚打开**时就得看到「它一直在看」，
                # 而不是从这一刻才开始记。
                ontrack = parse_ontrack_line(line)
                if ontrack is not None:
                    entry = dict(ontrack)
                    entry["time"] = _ontrack_clock(entry.get("timestamp"))
                    self.store.add_ontrack(entry)
                    continue
                # 级联审核统计（v3.5.0）—— 不是告警，单独收。
                cascade = parse_cascade_line(line)
                if cascade is not None:
                    self.store.set_cascade_stats(cascade)
                    continue
                # 操作事件（v2.2.0）—— 不是告警，单独收。
                event = parse_event_line(line)
                if event is not None:
                    self.store.add_event(event)
                    continue
                alert = parse_alert_line(line)
                if alert is None:
                    continue
                normalized = normalize_alert(alert)
                if normalized.get("update"):
                    # 历史里的解释补充 —— 合并进前面那条告警。
                    self.store.update_alert(
                        normalized.get("alert_id") or "",
                        {
                            "explanation": normalized.get("explanation") or "",
                            "explanation_degraded": normalized.get(
                                "explanation_degraded"
                            ),
                            "explanation_reason": (
                                normalized.get("explanation_reason") or ""
                            ),
                            "explanation_pending": False,
                        },
                    )
                    continue
                self.store.add_alert(normalized)
                # v3.5.1：历史告警也要进「实时审核流水」。
                self.store.add_ontrack(_audit_stream_entry(normalized))
        except Exception as exc:  # noqa: BLE001
            logger.warning("加载告警日志失败：%s: %s", type(exc).__name__, exc)
        try:
            lines = self._watch_tailer.prime(self.prime_bytes)
            for line in lines[-self.recent_line_limit:]:
                self.store.add_line(line)
        except Exception as exc:  # noqa: BLE001
            logger.warning("加载被监控日志失败：%s: %s", type(exc).__name__, exc)

    def _poll_once(self) -> None:
        """读取两个文件的新内容并分发事件。"""
        paused = self.store.is_paused()
        for line in self._alert_tailer.poll():
            # 实时审核流水（v3.3.1）—— ON_TRACK 的极简摘要。
            # 不是告警，单独处理：它只往「实时审核流水」区里加一行灰字。
            ontrack = parse_ontrack_line(line)
            if ontrack is not None:
                entry = dict(ontrack)
                entry["time"] = _ontrack_clock(entry.get("timestamp"))
                self.store.add_ontrack(entry)
                if not paused:
                    self.hub.publish(
                        self._event({"type": "on_track", **entry})
                    )
                continue
            # 级联审核统计（v3.5.0）—— 不是告警，单独处理。
            cascade = parse_cascade_line(line)
            if cascade is not None:
                self.store.set_cascade_stats(cascade)
                if not paused:
                    self.hub.publish(
                        self._event({"type": "cascade", **cascade})
                    )
                continue
            # 操作事件（v2.2.0）—— 不是告警，单独处理。
            event = parse_event_line(line)
            if event is not None:
                self.store.add_event(event)
                if not paused:
                    self.hub.publish(
                        self._event({"type": "timeline_event", **event})
                    )
                continue
            # Token 账单行（v1.1.0）—— 不是告警，单独处理。
            tokens = parse_token_line(line)
            if tokens is not None:
                self.store.set_tokens(tokens)
                if not paused:
                    self.hub.publish(
                        self._event({"type": "tokens", **tokens})
                    )
                continue
            alert = parse_alert_line(line)
            if alert is None:
                continue
            normalized = normalize_alert(alert)
            if normalized.get("update"):
                # 追加更新（L2 解释回来了）—— 合并进**已有**告警，
                # 前端据此把解释淡进同一张卡片，而不是新开一张。
                alert_id = normalized.get("alert_id") or ""
                fields = {
                    "explanation": normalized.get("explanation") or "",
                    "explanation_degraded": normalized.get(
                        "explanation_degraded"
                    ),
                    "explanation_reason": normalized.get("explanation_reason") or "",
                    "explanation_pending": False,
                }
                self.store.update_alert(alert_id, fields)
                if not paused:
                    self.hub.publish(
                        self._event({
                            "type": "alert_update",
                            "alert_id": alert_id,
                            **fields,
                        })
                    )
                continue
            self.store.add_alert(normalized)
            # v3.5.1：告警也要进「实时审核流水」——
            # 进了快照，刷新页面 / 切会话都还在。
            self.store.add_ontrack(_audit_stream_entry(normalized))
            if not paused:
                self.hub.publish(
                    self._event({
                        "type": "alert",
                        "stream_kind": "alert",
                        **normalized,
                    })
                )
        for line in self._watch_tailer.poll():
            self.store.add_line(line)
            if not paused:
                self._queue_line(line)
        self._flush_lines()

    def _queue_line(self, line: str) -> None:
        """把一行日志**攒**起来，等下一批一起发（v2.4.0）。

        :param line: 日志行文本。
        """
        with self._pending_lock:
            self._pending_lines.append(str(line))

    def _flush_lines(self) -> None:
        """把攒下来的日志行**批量**推给前端（v2.4.0）。

        为什么要有这一步：Agent 跑得飞起时，一秒能写几百行。
        逐行发 SSE 意味着一秒几百个事件 —— 订阅队列（默认 500 条）
        会被瞬间灌满、开始丢事件，前端也被几百次 DOM 操作淹没。
        攒成一批发，一个事件带几百行：网络帧数降一个数量级，
        前端也只需要一次批量插入。

        ⚠️ 旧的 ``{"type": "line"}`` 单行事件**不再**由本方法发出，
        但前端仍然认它（兼容旧页面 / 旧脚本）。

        :return: 无返回值。
        """
        with self._pending_lock:
            if not self._pending_lines:
                return
            batch = self._pending_lines
            self._pending_lines = []
        size = max(int(self.line_batch_size), 1)
        for index in range(0, len(batch), size):
            chunk = batch[index:index + size]
            self.hub.publish(self._event({"type": "lines", "texts": chunk}))


# ---------------------------------------------------------------------------
# 多路会话（v1.0.0）
# ---------------------------------------------------------------------------

#: 会话状态取值。
SESSION_STATES: Sequence[str] = (
    "starting",
    "running",
    "paused",
    "idle",
    "finished",
    # v3.4.0：进程死了，但日志文件还在 ——
    # 「运行中」是**假的**，得有个专门的状态说清楚「它已经静默了」。
    "exited",
)

#: 会话状态的中文说明（面板直接显示）。
SESSION_STATE_LABELS = {
    "starting": "启动中",
    "running": "运行中",
    "paused": "已挂起",
    "idle": "空闲",
    "finished": "已结束",
    "exited": "已退出（静默）",
}

#: 演示模式下会话名的前缀（v2.2.2）。
#:
#: ⚠️ 演示模式**必须**显式暴露在界面里：
#: 用户看到的告警全是编出来的，如果会话名还叫「默认会话」，
#: 他完全有理由以为哨兵在监控自己正在独立运行的那个 Agent。
DEMO_SESSION_PREFIX = "[演示] "

#: 演示模式下默认会话的名字（v2.2.2）。
DEMO_DEFAULT_SESSION_LABEL = DEMO_SESSION_PREFIX + "默认会话"

#: 雷达模式下占位会话的名字（v3.0.2）。
#:
#: ⚠️ 雷达模式**没有**任何被监控的会话 —— 它只是个「雷达」，
#: 扫到东西以后由用户点【接入】。
#: 名字必须说清楚这一点，否则用户会以为「默认会话」就是他的 Agent。
RADAR_SESSION_LABEL = "[雷达] 尚未接入任何 Agent"

#: 雷达模式下那个**空的占位日志**的文件名（v3.0.2）。
#:
#: ⚠️ 看板至少要有一个会话才能把三栏渲染出来，
#: 而雷达模式**不监控任何东西** —— 所以给它一个永远不会有事件的空文件。
RADAR_IDLE_LOG_NAME = "radar_idle.jsonl"

#: 「退出演示」用的 uvicorn 服务器句柄（v3.0.1）——
#:
#: 演示模式下看板上有个大按钮【退出演示，开始真实扫描】。
#: 点它要**真的**把演示进程停下来 —— 光在页面上写一句话没用，
#: 用户看着黑框终端还开着，照样以为「没反应」。
#:
#: uvicorn 的 ``Server.should_exit = True`` 是它自己文档里的正规做法：
#: 主循环下一轮就会干净地退出（不是强杀）。
_DEMO_SHUTDOWN: Dict[str, Any] = {"server": None}

#: **彻底退出**的统一出口（v3.4.0）。
#:
#: 托盘的【彻底退出】、看板的【关闭哨兵】按钮、
#: 以及「用户直接关掉黑框」—— 三条路最终都调用
#: :meth:`ShutdownCoordinator.request`，所以只有**一套**收尾逻辑。
#:
#: ⚠️ 演示模式的【退出演示】是**另一回事** ——
#: 它只停服务、不退程序（用户还要接着用真的哨兵），
#: 所以它仍然走 ``_DEMO_SHUTDOWN`` 那条老路。
_SHUTDOWN = ShutdownCoordinator()

#: 托盘图标句柄（v3.4.0）—— 收尾时要把它收掉。
_TRAY: Dict[str, Any] = {"icon": None}

#: 「关掉黑框」时 Windows 只给大约 5 秒（v3.4.0）。
#:
#: 超时的话系统会**直接杀掉进程** —— 那属于「强杀」，
#: 收尾做不完。所以收尾动作必须快。
CONSOLE_CLOSE_GRACE_SECONDS = 5.0

#: 进程存活巡检的间隔（秒）（v3.4.0）。
#:
#: ⚠️ 10 秒是个折中：再快没有意义（进程不会秒死），
#: 再慢用户会觉得「状态卡住了」。
PROCESS_LIVENESS_INTERVAL = 10.0

#: 演示进程退出前，页面要留给用户的「接下来怎么办」提示（v3.0.1）。
#:
#: ⚠️ 这些行会被前端用 ``textContent`` **原样**显示 ——
#: 所以这里写纯文本，别写 Markdown 标记。
DEMO_EXIT_INSTRUCTIONS = (
    "演示到此结束。下面这些命令才是真的监控：",
    "",
    "  # ① 接管一个 Agent（边跑边审）",
    '  sentinel --agent codex "把这个接口的鉴权改成 JWT"',
    "",
    "  # ② 审一份已有的日志",
    "  sentinel --scan path/to/agent.log",
    "",
    "  # ③ 只看终端，不开看板",
    '  sentinel --agent mcode --no-gui "你的任务"',
    "",
    "Windows 上双击 启动哨兵.bat 也能带同样的参数。",
    "想看扫描结果，在看板里点【＋ 发现/挂载进程】。",
)

#: 密钥类字段的**打码值**（v2.3.0）——
#: 设置页拿到的是打码后的值；保存时打码值会被**跳过**，
#: 绝不会把用户真正的密钥用 ``********`` 覆盖掉。
SECRET_MASK = "********"  # noqa: S105 —— 这是**打码符**，不是密钥本身

#: 哪些字段算「密钥」—— 名字命中就一律打码。
SECRET_FIELD_NAMES = frozenset(
    {
        "api_key",
        "secret",
        "token",
        "password",
        "webhook_secret",
        "access_token",
        "bark_key",
    }
)


class SessionSpec:
    """一个被监控会话的**描述**（纯配置，不持有运行时状态）。

    一个会话 = 一个 Agent + 一份被监控日志 + 一份告警日志 +
    一个守护者。多个会话并行跑，就是「多路中枢」。

    :param id: 会话 ID（全局唯一，用于 API 路径）。
    :param label: 面板上显示的名字。
    :param agent: Agent 类型（``codex`` / ``mcode`` / ``claude`` …）。
    :param watch_path: 被监控的 Agent 日志路径（只读）。
    :param alert_log_path: 该会话的哨兵告警日志路径。
    :param guardian_callback: 守护者（鸭子类型）。
    :param scan_callback: 「立即扫描」回调。
    :param watch_target: 「当前监控目标」快照。
    :param task_goal: 任务目标（意图锚点）。
    :param briefing_callback: 「生成晨报」回调。
    :param inject_callback: 「直接发送给 Agent」回调（v2.0.0）。
    :param rollback_callback: 「一键回滚至拦截点」回调（v2.0.0）。
    :param attached: 这个会话是不是**雷达挂载**进来的（v2.4.0）。
    :param process_info: 进程反查结果（v2.4.0），形如
        ``{"pid": 123, "strategy": "cmdline_path", …}``；
        反查不到就是空字典。
    :param arm_callback: 「武装守护者」回调（v2.4.0）——
        签名 ``(armed: bool) -> dict``。**只有用户手动打开开关时**
        它才会真的把守护者接到那个 PID 上。
    :param placeholder: 这是不是个**占位**会话（v3.4.0）——
        雷达模式下那个「尚未接入任何 Agent」的空壳。
        占位会话**不给**卸载按钮：它不是用户挂上来的东西。
    """

    def __init__(
        self,
        id: str,
        *,
        label: str = "",
        agent: str = "",
        watch_path: str = "",
        alert_log_path: str = "",
        guardian_callback: Optional[Any] = None,
        scan_callback: Optional[Callable[[str], Any]] = None,
        watch_target: Optional["WatchTarget"] = None,
        task_goal: Optional["TaskGoal"] = None,
        briefing_callback: Optional[Callable[[], Dict[str, Any]]] = None,
        inject_callback: Optional[Callable[..., Dict[str, Any]]] = None,
        rollback_callback: Optional[Callable[..., Dict[str, Any]]] = None,
        resume_callback: Optional[Callable[..., Dict[str, Any]]] = None,
        plan_callback: Optional[Callable[..., Dict[str, Any]]] = None,
        safe_point_service: Optional[Any] = None,
        workdir: str = "",
        tail_runner: Optional[Any] = None,
        attached: bool = False,
        process_info: Optional[Dict[str, Any]] = None,
        arm_callback: Optional[Callable[[bool], Dict[str, Any]]] = None,
        placeholder: bool = False,
    ) -> None:
        self.id = str(id or "default")
        self.label = str(label or self.id)
        self.agent = str(agent or "")
        self.watch_path = os.path.abspath(watch_path or "")
        self.alert_log_path = os.path.abspath(
            alert_log_path or DEFAULT_ALERT_LOG
        )
        self.guardian_callback = guardian_callback
        self.scan_callback = scan_callback
        self.watch_target = watch_target
        self.task_goal = task_goal
        # 「生成晨报」回调（v1.2.0）——
        #
        # 刻意用**回调**而不是把引擎塞进来：看板层不认识
        # ``src.core.engine``，这条边界不能因为一个新功能就破掉。
        # 回调返回 ``{"markdown": …, "terminal": …, "path": …}``。
        self.briefing_callback = briefing_callback
        # 「直接发送给 Agent」回调（v2.0.0）——
        #
        # 同样用**回调**：看板不认识 Agent 的进程管道，
        # 真正能往 stdin 写东西的是启动器。
        # 回调签名为 ``(prompt: str, meta: dict) -> dict``。
        self.inject_callback = inject_callback
        # 「一键回滚至拦截点」回调（v2.0.0）——
        # 签名 ``(point_id: str, workdir: str) -> dict``。
        self.rollback_callback = rollback_callback
        # 「回滚并重试」回调（v2.1.0）——
        # 签名 ``(prompt: str, meta: dict) -> dict``。
        self.resume_callback = resume_callback
        # 「复活计划」回调（v2.1.0）——
        # 只有启动器知道 Agent 当前是不是挂起状态。
        self.plan_callback = plan_callback
        # 安全点服务（v2.0.0）—— 用来列出可回滚的安全点。
        self.safe_point_service = safe_point_service
        # 被监控 Agent 的工作目录（留言箱与安全点都落在它旁边）。
        self.workdir = os.path.abspath(workdir) if workdir else ""
        # 常驻的实时监听运行器（v2.3.0）——
        #
        # 「雷达」挂载进来的会话靠它：引擎一直阻塞在 ``tail -f`` 语义上，
        # 用户**已经在跑**的 Agent 每写一行就立刻被审一次。
        # 单会话启动模式（``--watch``）用的是主会话自己的那一套。
        self.tail_runner = tail_runner
        # 是不是雷达挂载进来的（v2.4.0）——
        #
        # 挂载会话与主会话的差别不只是「多一路」：
        # 主会话是哨兵自己拉起来的，守护者按配置工作；
        # 挂载会话是**别人**在跑的进程，默认**不带**守护者，
        # 必须由用户手动「武装」才接管（红线 R7）。
        self.attached = bool(attached)
        # 进程反查结果（v2.4.0）—— 反查不到就是空字典。
        self.process_info: Dict[str, Any] = dict(process_info or {})
        # 「武装守护者」回调（v2.4.0）。
        self.arm_callback = arm_callback
        #: 这是不是**占位**会话（v3.4.0）——
        #: 雷达模式下那个「尚未接入任何 Agent」的空壳。
        self.placeholder = bool(placeholder)

class Session:
    """运行中的会话：自己的 store / watcher / 目标 / 守护者。

    会话之间**完全隔离** —— 各自的告警、计数、目标、守护者互不干扰。
    但它们共享同一个事件中心（:class:`EventHub`），事件里带着
    ``session_id``，前端据此分流。

    :param spec: 会话描述。
    :param hub: 事件分发中心。
    :param poll_interval: tailer 轮询间隔（秒）。
    :param recent_line_limit: 保留多少行被监控日志。
    :param max_alerts: 保留多少条告警。
    """

    def __init__(
        self,
        spec: SessionSpec,
        hub: EventHub,
        *,
        poll_interval: float = 0.5,
        recent_line_limit: int = 200,
        max_alerts: int = 500,
    ) -> None:
        self.spec = spec
        self.id = spec.id
        self.label = spec.label
        self.agent = spec.agent
        self.watch_path = spec.watch_path
        self.alert_log_path = spec.alert_log_path
        self.guardian = spec.guardian_callback
        self.scan_callback = spec.scan_callback
        self.watch_target = spec.watch_target or WatchTarget(
            agent=spec.agent
        )
        self.task_goal = spec.task_goal or TaskGoal()
        self.briefing_callback = spec.briefing_callback
        self.inject_callback = spec.inject_callback
        self.rollback_callback = spec.rollback_callback
        self.resume_callback = spec.resume_callback
        self.plan_callback = spec.plan_callback
        self.safe_point_service = spec.safe_point_service
        self.workdir = spec.workdir
        self.tail_runner = spec.tail_runner
        # v2.4.0：挂载会话的「进程绑定」与「武装守护者」状态。
        self.attached = bool(spec.attached)
        self.process_info: Dict[str, Any] = dict(spec.process_info or {})
        self.arm_callback = spec.arm_callback
        #: 这是不是**占位**会话（v3.4.0）—— 雷达模式下的那个空壳。
        self.placeholder = bool(spec.placeholder)
        #: 用户是否**手动**打开了「武装守护者」开关（v2.4.0）。
        self.armed = False
        #: 这个会话的进程是不是**已经死了**（v3.4.0）——
        #: 反查到了 PID、而那个 PID 没了，就置上。
        self._exited = False
        self.store = AlertStore(
            max_alerts=max_alerts, max_lines=recent_line_limit
        )
        self.watcher = Watcher(
            self.watch_path,
            self.alert_log_path,
            hub,
            self.store,
            session_id=self.id,
            poll_interval=poll_interval,
            recent_line_limit=recent_line_limit,
        )

    # -- 状态 -----------------------------------------------------------
    def bound_pid(self) -> int:
        """这个会话**确定绑定**到的进程 PID（v3.4.0）。

        ⚠️ 只有「敢认」的绑定才算 ——
        ``bindable`` 为假（只是「按工作目录猜到了个什么」）的，
        **不能**拿它的 PID 去判断死活。

        :return: PID；没有可靠绑定就是 ``0``。
        """
        info = self.process_info or {}
        if not info.get("bindable"):
            return 0
        return int(info.get("pid") or 0)

    def process_alive(self) -> Optional[bool]:
        """绑定的那个进程现在还活着吗（v3.4.0）。

        :return: 活着 ``True``、死了 ``False``；
            **不知道**（没绑定 / 查不了）返回 ``None``。
        """
        pid = self.bound_pid()
        if pid <= 0:
            return None
        return _pid_alive(pid)

    def mark_exited(self) -> None:
        """标记「进程已经死了」，并**停止**对那份日志的跟随（v3.4.0）。

        为什么必须停
        ============

        Agent 跑完之后，日志文件**还躺在磁盘上**，
        但它再也不会被写了。

        哨兵原来只看「文件还在不在」→ 一直显示「运行中」，
        右侧还在**一遍遍重放**同样的旧行 —— 用户看着像卡住了。

        所以进程一死就把跟随线程收掉：
        状态变成「已退出（静默）」，右侧**不再刷**。

        :return: 无。
        """
        if self._exited:
            return
        self._exited = True
        try:
            self.watcher.stop()
        except Exception as exc:  # noqa: BLE001 —— 停止失败不影响状态
            logger.warning(
                "停止会话 %s 的跟随线程失败：%s: %s",
                self.id,
                type(exc).__name__,
                exc,
            )

    def state(self) -> str:
        """当前会话状态。

        :return: ``starting`` / ``running`` / ``paused`` / ``idle`` /
            ``finished`` / ``exited`` 之一。
        """
        if self._exited:
            return "exited"
        if self.watcher.is_finished():
            return "finished"
        if self.store.is_paused():
            return "paused"
        if self.store.is_running():
            return "running"
        return "idle"

    def state_label(self) -> str:
        """会话状态的中文说明。"""
        return SESSION_STATE_LABELS.get(self.state(), self.state())

    def snapshot(self) -> Dict[str, Any]:
        """会话的完整快照（给 ``/api/sessions`` 用）。

        :return: 含 ID / 名字 / Agent / 状态 / 计数 / 目标的字典。
        """
        target = self.watch_target.snapshot()
        counts = self.store.counts()
        total = 0
        for value in counts.values():
            total += int(value or 0)
        # 进程绑定情况（v2.4.0）——
        #
        # 反查到**敢认**的进程才给 PID；只是「按工作目录猜到了个
        # 什么」不算 —— 宁可显示「未绑定进程」，也不能让用户以为
        # 哨兵盯上了一个确定的进程。
        info = self.process_info or {}
        bindable = bool(info.get("bindable")) and int(info.get("pid") or 0) > 0
        pid = int(info.get("pid") or 0) if bindable else 0
        return {
            "id": self.id,
            "label": self.label,
            "agent": self.agent or target.get("agent") or "",
            "state": self.state(),
            "state_label": self.state_label(),
            "counts": counts,
            "alert_total": total,
            "watch_path": self.watch_path,
            "alert_log_path": self.alert_log_path,
            # 工作区（v2.3.0）—— 雷达挂载进来的会话靠它显示
            # 「[Codex] D:\work\project_alpha」这种带项目路径的名字。
            "workspace": self.workdir or target.get("workspace") or "",
            "goal": target.get("goal") or "",
            "goal_source": target.get("goal_source") or "none",
            "goal_source_label": target.get("goal_source_label") or "未设置",
            "auto_anchored": bool(target.get("auto_anchored")),
            "guardian": bool(self.guardian is not None),
            # --- 进程绑定与武装（v2.4.0）---
            "attached": bool(self.attached),
            "pid": pid,
            "process_bound": bindable,
            "process_info": dict(info),
            "armed": bool(self.armed),
            # v3.4.0：占位会话（雷达模式下的空壳）——
            # 界面据此**不给**它卸载按钮。
            "placeholder": bool(self.placeholder),
            # v3.4.0：绑定的进程是不是**已经死了**。
            "process_alive": self.process_alive(),
            # 能不能武装：既要有回调（说明这个会话支持武装），
            # 也要真的绑定到了进程。
            "arm_available": bool(self.arm_callback is not None and bindable),
            # Token 累计（v1.1.0）—— 顶部计数器与左侧会话列表都用它。
            "tokens": self.store.tokens(),
            # v3.5.0：按线程指纹分出来的**虚拟会话**——
            # 一份日志里混着多个 Agent 时，左侧据此拆成多张卡片，
            # 各路互不干扰（防串台）。
            "streams": self.store.streams(),
            # v3.5.0：级联审核统计（L2 过滤数 / 省下的云端调用数）。
            "cascade": self.store.cascade_stats(),
        }

    def build_briefing(self) -> Dict[str, Any]:
        """生成《哨兵值守晨报》（v1.2.0）。

        看板只负责**转发** —— 真正干活的是外部注入的回调
        （见 :class:`SessionSpec` 的说明）。所以这里不 import
        任何引擎相关的模块。

        :return: ``{"ok": True, "markdown": …, "terminal": …, "path": …}``；
            不可用时返回 ``{"ok": False, "error": …}``。
        """
        if self.briefing_callback is None:
            return {
                "ok": False,
                "error": "这个会话没有配置晨报功能（只有「真实管道接管」模式才有）。",
            }
        try:
            payload = self.briefing_callback()
        except Exception as exc:  # noqa: BLE001 —— 晨报失败不外泄
            logger.warning(
                "生成晨报失败（%s）：%s", type(exc).__name__, exc
            )
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        if not isinstance(payload, dict):
            return {"ok": False, "error": "晨报回调返回了非法的内容。"}
        result = dict(payload)
        result.setdefault("ok", True)
        return result

    def inject(self, prompt: str, meta: Dict[str, Any]) -> Dict[str, Any]:
        """把纠偏提示**直接发送给 Agent**（v2.0.0）。

        优先走外部注入的回调（启动器才有 Agent 的 stdin 管道）；
        没有回调时降级到**留言箱** —— 指令照样留底，
        用户能自己粘，哨兵下次启动也能补投。

        :param prompt: 提示正文。
        :param meta: 附加信息（会话 ID / 判定 / 规则…）。
        :return: 注入结果字典。
        """
        if self.inject_callback is not None:
            try:
                payload = self.inject_callback(prompt, dict(meta or {}))
            except Exception as exc:  # noqa: BLE001 —— 注入失败不外泄
                logger.warning(
                    "注入 Agent 失败（%s）：%s", type(exc).__name__, exc
                )
                return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            if isinstance(payload, dict):
                result = dict(payload)
                result.setdefault("ok", True)
                return result
            return {"ok": False, "error": "注入回调返回了非法的内容。"}
        return self._inject_via_inbox(prompt, meta)

    def _inject_via_inbox(
        self, prompt: str, meta: Dict[str, Any]
    ) -> Dict[str, Any]:
        """没有回调时的降级路径：写留言箱。

        :param prompt: 提示正文。
        :param meta: 附加信息。
        :return: 注入结果字典。
        """
        # 惰性导入 —— 看板层不静态依赖 core 包。
        from ..core.injector import AgentInjector, InboxStore

        workdir = (
            self.workdir or os.path.dirname(self.watch_path) or os.getcwd()
        )
        inbox = InboxStore(os.path.join(workdir, "sentinel_inbox.json"))
        injector = AgentInjector(inbox=inbox, agent_label=self.agent)
        try:
            result = injector.inject(
                prompt,
                session_id=self.id,
                verdict=str((meta or {}).get("verdict") or ""),
                rule_id=str((meta or {}).get("rule_id") or ""),
                reason=str((meta or {}).get("reason") or ""),
            )
        except Exception as exc:  # noqa: BLE001 —— 注入失败不外泄
            logger.warning(
                "写留言箱失败（%s）：%s", type(exc).__name__, exc
            )
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        return result.to_dict()

    def rollback(
        self, point_id: str = "", workdir: str = ""
    ) -> Dict[str, Any]:
        """【一键回滚至拦截点】（v2.0.0）。

        :param point_id: 安全点 ID；留空表示回滚到基线。
        :param workdir: 工作目录；留空用会话自己的。
        :return: 回滚结果字典。
        """
        if self.rollback_callback is None:
            return {
                "ok": False,
                "error": (
                    "这个会话没有配置回滚功能"
                    "（只有「真实管道接管」模式才有安全点）。"
                ),
            }
        try:
            payload = self.rollback_callback(
                str(point_id or ""), str(workdir or self.workdir or "")
            )
        except Exception as exc:  # noqa: BLE001 —— 回滚失败不外泄
            logger.warning(
                "回滚失败（%s）：%s", type(exc).__name__, exc
            )
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        if not isinstance(payload, dict):
            return {"ok": False, "error": "回滚回调返回了非法的内容。"}
        result = dict(payload)
        result.setdefault("ok", True)
        return result

    def resume(self, prompt: str, meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """【回滚并重试】（v2.1.0）—— 一键复活被挂起的会话。

        这一步会做三件**不可逆**的事，所以必须由用户明确点：
        回滚工作目录、结束挂起的进程、重新拉起 Agent。

        :param prompt: 纠偏提示正文。
        :param meta: 附加信息（告警 ID、规则 ID…）。
        :return: 复活结果字典。
        """
        if self.resume_callback is None:
            return {
                "ok": False,
                "error": (
                    "这个会话没有配置复活功能"
                    "（只有「真实管道接管」模式才能重拉 Agent）。"
                ),
            }
        try:
            payload = self.resume_callback(str(prompt or ""), dict(meta or {}))
        except Exception as exc:  # noqa: BLE001 —— 复活失败不外泄
            logger.warning(
                "复活失败（%s）：%s", type(exc).__name__, exc
            )
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        if not isinstance(payload, dict):
            return {"ok": False, "error": "复活回调返回了非法的内容。"}
        result = dict(payload)
        result.setdefault("ok", True)
        return result

    def resume_plan(
        self, point_id: str = "", workdir: str = ""
    ) -> Dict[str, Any]:
        """算「回滚并重试」**将要**做什么（只算不动）。

        :param point_id: 安全点 ID；留空表示用基线。
        :param workdir: 工作目录。
        :return: 计划字典。
        """
        if self.plan_callback is not None:
            try:
                payload = self.plan_callback(
                    str(point_id or ""), str(workdir or self.workdir or "")
                )
            except Exception as exc:  # noqa: BLE001 —— 计划失败不外泄
                logger.warning(
                    "算复活计划失败（%s）：%s", type(exc).__name__, exc
                )
                return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            if isinstance(payload, dict):
                return payload
            return {"ok": False, "error": "复活计划回调返回了非法的内容。"}
        # 没有启动器就退化成「只算回滚」——
        # 至少让用户知道会恢复几个、删除几个。
        service = self.safe_point_service
        if service is None:
            return {
                "ok": False,
                "error": "这个会话没有安全点，没法算复活计划。",
            }
        try:
            plan = service.plan(
                str(point_id or ""), str(workdir or self.workdir or "")
            )
        except Exception as exc:  # noqa: BLE001 —— 计划失败不外泄
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        plan = dict(plan)
        plan["steps"] = [
            "把工作目录回滚到安全点"
            f"（恢复 {plan.get('restored_count', 0)} 个、"
            f"删除 {plan.get('deleted_count', 0)} 个）",
            "把拦截原因和纠偏提示写进留言箱",
            "重新拉起 Agent（任务提示里带上纠偏提示）",
        ]
        return plan

    def diff(
        self, point_id: str = "", workdir: str = ""
    ) -> Dict[str, Any]:
        """算出「安全点 vs 现在」的文件差异（v2.1.0）。

        给看板的【查看改动】用 —— 让用户**直观看到** Agent 改了哪些文件、
        加了什么、删了什么，再决定放行还是回滚。

        :param point_id: 安全点 ID；留空表示用基线。
        :param workdir: 工作目录；留空用会话自己的。
        :return: 差异字典。
        """
        service = self.safe_point_service
        if service is None:
            return {
                "ok": False,
                "error": "这个会话没有安全点，没法算差异。",
                "files": [],
            }
        try:
            payload = service.diff(
                str(point_id or ""), str(workdir or self.workdir or "")
            )
        except Exception as exc:  # noqa: BLE001 —— 算差异失败不外泄
            logger.warning(
                "算文件差异失败（%s）：%s", type(exc).__name__, exc
            )
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        if not isinstance(payload, dict):
            return {"ok": False, "error": "安全点服务返回了非法的内容。"}
        return payload

    def safe_points(self) -> List[Dict[str, Any]]:
        """列出这个会话可用的安全点（新的在前）。

        :return: 安全点字典列表；没有服务时返回空列表。
        """
        service = self.safe_point_service
        if service is None:
            return []
        try:
            points = service.list_points(workdir=self.workdir)
        except Exception as exc:  # noqa: BLE001 —— 列表失败不外泄
            logger.warning(
                "列安全点失败（%s）：%s", type(exc).__name__, exc
            )
            return []
        return list(points or [])

    def _baseline_point_id(self, points: List[Dict[str, Any]]) -> str:
        """找出**基线安全点**的 ID（时间线的 Diff 兜底）。

        守护者缺席时，红线告警**不会**带安全点快照 ——
        但基线总在。用户照样要能回看「Agent 到底改了什么」，
        所以没有自带快照的告警一律挂到基线上。

        :param points: 安全点列表。
        :return: 基线安全点 ID；找不到返回空串。
        """
        for item in points or ():
            if str(item.get("label") or "").startswith("基线"):
                return str(item.get("id") or "")
        return ""

    def timeline(self) -> Dict[str, Any]:
        """【案发时间线】（v2.2.0）。

        把这个会话散落在四处的事实（告警、操作事件、安全点）
        拼成**一条带时间戳的线** —— 让用户能回看
        「在触发拦截的 10 分钟前，Agent 到底在干什么」。

        看板只负责**呈现**；节点怎么分类、怎么关联安全点，
        是 :mod:`src.core.timeline` 的事。所以这里**惰性导入**，
        看板应用层依旧不认识 ``src.core.engine``。

        :return: ``{"ok": True, "nodes": [...], "summary": {...}}``。
        """
        try:
            # 惰性导入 —— 看板层不静态依赖 core 包。
            from ..core.timeline import build_timeline, summarize

            points = self.safe_points()
            nodes = build_timeline(
                self.store.alerts(),
                self.store.events(),
                safe_points=points,
                fallback_point_id=self._baseline_point_id(points),
            )
            return {
                "ok": True,
                "nodes": nodes,
                "summary": summarize(nodes),
            }
        except Exception as exc:  # noqa: BLE001 —— 复盘失败不外泄
            logger.warning(
                "生成时间线失败（%s）：%s", type(exc).__name__, exc
            )
            return {
                "ok": False,
                "error": "{}: {}".format(type(exc).__name__, exc),
                "nodes": [],
                "summary": {},
            }

    def health(self, *, skip_network: bool = False) -> Dict[str, Any]:
        """【开箱安全体检】（v2.2.0）。

        检查四项：本地模型通不通、云端密钥有没有效、
        Webhook 配没配、工作目录有没有排除规则。

        :param skip_network: 跳过要发网络请求的两项（快速模式）。
        :return: ``{"ok": True, "items": [...], "markdown": …}``。
        """
        try:
            # 惰性导入 —— 看板层不静态依赖 core 包。
            from ..config_loader import load_config
            from ..core.health import render_markdown, run_health_check

            config = load_config(None)
            items = run_health_check(
                config,
                workdir=self.workdir,
                skip_network=bool(skip_network),
            )
            return {
                "ok": True,
                "items": [item.to_dict() for item in items],
                "markdown": render_markdown(items),
            }
        except Exception as exc:  # noqa: BLE001 —— 体检失败不外泄
            logger.warning(
                "安全体检失败（%s）：%s", type(exc).__name__, exc
            )
            return {
                "ok": False,
                "error": "{}: {}".format(type(exc).__name__, exc),
                "items": [],
                "markdown": "",
            }

    def start(self) -> None:
        """启动这个会话的跟随线程。"""
        self.watcher.start()

    def stop(self, timeout: float = 2.0) -> None:
        """停止这个会话的跟随线程。"""
        self.watcher.stop(timeout=timeout)
        # v2.3.0：雷达挂载进来的会话还带着一个常驻的实时监听引擎 ——
        # 不停掉它，那个线程会一直占着被监控文件的句柄。
        runner = getattr(self, "tail_runner", None)
        if runner is not None:
            try:
                runner.stop()
            except Exception as exc:  # noqa: BLE001 —— 停止失败不影响退出
                logger.warning(
                    "停止会话 %s 的实时监听失败：%s: %s",
                    self.id,
                    type(exc).__name__,
                    exc,
                )


class SessionRegistry:
    """会话登记表 —— 多路中枢的大脑。

    负责：登记会话、按 ID 查找、列出全部、统一起停。
    线程安全 —— 会话可能在运行中被动态加入。

    :param sessions: 初始会话列表。
    """

    def __init__(self, sessions: Optional[Sequence[Session]] = None) -> None:
        self._lock = threading.Lock()
        self._sessions: Dict[str, Session] = {}
        for session in sessions or ():
            self.add(session)

    def add(self, session: Session) -> Session:
        """登记一个会话（同 ID 会覆盖旧的）。

        :param session: 会话实例。
        :return: 登记后的会话。
        """
        with self._lock:
            self._sessions[session.id] = session
        return session

    def remove(self, session_id: str) -> Optional[Session]:
        """移除一个会话（先停掉它的跟随线程）。

        :param session_id: 会话 ID。
        :return: 被移除的会话；没找到返回 ``None``。
        """
        with self._lock:
            session = self._sessions.pop(str(session_id), None)
        if session is not None:
            try:
                session.stop()
            except Exception:  # noqa: BLE001 —— 移除失败不影响服务
                logger.warning("停止会话 %s 失败。", session_id)
        return session

    def get(self, session_id: Optional[str]) -> Optional[Session]:
        """按 ID 找会话。

        找不到（或没给 ID）时返回**主会话** ——
        这样单会话的老用法不需要改任何前端代码。

        ⚠️ 但**会话级接口**要用 :meth:`resolve` ——
        那里必须「找不到就报 404」，不能悄悄返回别的会话。

        :param session_id: 会话 ID。
        :return: 会话实例；没有则返回 ``None``。
        """
        with self._lock:
            if session_id:
                session = self._sessions.get(str(session_id))
                if session is not None:
                    return session
            if not self._sessions:
                return None
            return next(iter(self._sessions.values()))

    def resolve(self, session_id: Optional[str]) -> Optional[Session]:
        """严格按 ID 找会话 —— 找不到就返回 ``None``。

        会话级 API 用这个：用户点名要 ``/api/sessions/foo``，
        却没有 ``foo``，那就该报 404 ——
        悄悄返回主会话的数据属于**串台**，比报错更糟。

        :param session_id: 会话 ID。
        :return: 会话实例；没有则返回 ``None``。
        """
        if not session_id:
            return None
        with self._lock:
            return self._sessions.get(str(session_id))

    def list(self) -> List[Session]:
        """列出全部会话（按登记顺序）。"""
        with self._lock:
            return list(self._sessions.values())

    def primary(self) -> Optional[Session]:
        """主会话（第一个登记的）。"""
        return self.get(None)

    def snapshot(self) -> List[Dict[str, Any]]:
        """全部会话的快照列表。"""
        return [session.snapshot() for session in self.list()]

    def start_all(self) -> None:
        """启动全部会话的跟随线程。"""
        for session in self.list():
            try:
                session.start()
            except Exception as exc:  # noqa: BLE001 —— 单个会话失败不影响其他
                logger.warning(
                    "启动会话 %s 失败：%s: %s",
                    session.id,
                    type(exc).__name__,
                    exc,
                )

    def stop_all(self, timeout: float = 2.0) -> None:
        """停止全部会话的跟随线程。"""
        for session in self.list():
            try:
                session.stop(timeout=timeout)
            except Exception as exc:  # noqa: BLE001 —— 停止失败不影响退出
                logger.warning(
                    "停止会话 %s 失败：%s: %s",
                    session.id,
                    type(exc).__name__,
                    exc,
                )


# ---------------------------------------------------------------------------
# 应用
# ---------------------------------------------------------------------------
def _with_demo_label(spec: SessionSpec) -> SessionSpec:
    """给会话名加上「[演示]」前缀（v2.2.2）。

    **不修改调用方传进来的对象** —— 复制一份再改，
    免得把别人的 ``SessionSpec`` 改坏了。

    :param spec: 原始会话描述。
    :return: 带「[演示]」前缀的会话描述（幂等：已有前缀就不再加）。
    """
    label = str(getattr(spec, "label", "") or "")
    if label.startswith(DEMO_SESSION_PREFIX):
        return spec
    clone = copy.copy(spec)
    clone.label = DEMO_SESSION_PREFIX + label
    return clone


def _resolve_runtime_config_path(config_path: Optional[str] = None) -> str:
    """运行期真正在用的配置文件路径（v2.3.0）。

    设置页要读写的是**同一个**文件 ——
    读错了文件，用户改了半天不生效，那比没有设置页还糟。

    :param config_path: 命令行给的 ``--config``；为空时按默认候选顺序找。
    :return: 绝对路径；一个候选都没有时返回空串。
    """
    if config_path:
        return os.path.abspath(config_path)
    from ..config_loader import default_config_candidates

    candidates = default_config_candidates()
    return os.path.abspath(candidates[0]) if candidates else ""


#: 外部插件进程树扫描结果的缓存有效期（秒）。
#:
#: ⚠️ 为什么要缓存 —— Windows 上**第一次**全量枚举进程属性要 3~4 秒
#:    （逐个打开进程句柄，很贵）；
#:    psutil 会把句柄缓存住，之后每次只要几十毫秒。
#:    雷达面板刷新得挺频繁，不缓存就是每次都让用户等。
CLOUD_PROC_SCAN_TTL = 5.0

#: 进程树扫描的**超时熔断**阈值（秒）。
#:
#: ⚠️ v4.0.1：用户点开雷达面板时，扫描**最多只让他等这么久**。
#:    超时就先把手头已经扫到的结果交出去，界面上提示
#:    「正在深度检索进程…」，后台继续扫、前端轮询取后续结果。
#:    —— 绝不让「首次要 4 秒」去堵住主线程。
CLOUD_PROC_SCAN_TIMEOUT = 1.5

#: 外部插件进程树扫描缓存。
#:
#: 结构：``{"ready": bool, "at": float, "items": list}``。
_cloud_proc_cache: Dict[str, Any] = {
    "ready": False,
    "at": 0.0,
    "items": [],
}

#: 进程树扫描的**运行状态**（v4.0.1）。
#:
#: 结构：``{"status": str, "started_at": float, "finished_at": float,
#: "items": list, "error": str}``。
#:
#: ``status`` 取值：
#:
#: * ``idle`` —— 还没扫过；
#: * ``scanning`` —— 正在扫（**已有部分结果**）；
#: * ``ready`` —— 扫完了；
#: * ``error`` —— 炸了（但审查侧仍能继续，红线 R5）。
_cloud_proc_state: Dict[str, Any] = {
    "status": "idle",
    "started_at": 0.0,
    "finished_at": 0.0,
    "items": [],
    "error": "",
}

#: 保护 :data:`_cloud_proc_state` 与 :data:`_cloud_proc_cache` 的锁。
_cloud_proc_lock = threading.Lock()

#: 扫描状态变化时置位，让等待方立刻醒来（不必空等轮询周期）。
_cloud_proc_event = threading.Event()


def _cloud_proc_scan_state() -> Dict[str, Any]:
    """取一份进程树扫描的状态快照。

    :return: ``{"status": str, "partial": bool, "elapsed_ms": float}``。
        ``status`` 是 ``idle`` / ``scanning`` / ``ready`` / ``error``；
        ``partial`` 表示「这份结果是**部分结果**，还在补」。
    """
    with _cloud_proc_lock:
        status = str(_cloud_proc_state.get("status") or "idle")
        started = float(_cloud_proc_state.get("started_at") or 0.0)
        finished = float(_cloud_proc_state.get("finished_at") or 0.0)
    if status == "scanning":
        elapsed = (time.monotonic() - started) * 1000.0
    elif started and finished:
        elapsed = (finished - started) * 1000.0
    else:
        elapsed = 0.0
    return {
        "status": status,
        "partial": status == "scanning",
        "elapsed_ms": round(elapsed, 1),
    }


def _publish_cloud_process_scan(
    rows: List[Any],
) -> None:
    """把「已经扫到的部分结果」发布出去（供超时熔断时取用）。

    :param rows: :class:`src.proc_tree.DiscoveredAgentProcess` 列表。
    """
    with _cloud_proc_lock:
        _cloud_proc_state["items"] = [item.to_dict() for item in rows]
    _cloud_proc_event.set()


def _run_cloud_process_scan() -> None:
    """真正干活的进程树扫描（**跑在后台线程里**）。

    ⚠️ 这一段可能跑好几秒（冷启动时 Windows 要逐个开进程句柄），
    所以它**绝不**在请求线程里跑。
    """
    started = time.monotonic()
    items: List[Dict[str, Any]] = []
    error = ""
    try:
        from ..proc_tree import scan_process_tree

        found = scan_process_tree(on_progress=_publish_cloud_process_scan)
        items = [item.to_dict() for item in found]
    except Exception as exc:  # noqa: BLE001 —— 探测失败只降级
        error = "{}: {}".format(type(exc).__name__, exc)
        logger.warning(
            "外部插件进程树扫描失败：%s", error
        )
    finished = time.monotonic()
    with _cloud_proc_lock:
        _cloud_proc_state["status"] = "error" if error else "ready"
        _cloud_proc_state["started_at"] = started
        _cloud_proc_state["finished_at"] = finished
        _cloud_proc_state["items"] = items
        _cloud_proc_state["error"] = error
        _cloud_proc_cache["ready"] = True
        _cloud_proc_cache["at"] = finished
        _cloud_proc_cache["items"] = list(items)
    _cloud_proc_event.set()


def _start_cloud_process_scan(force: bool = False) -> None:
    """在后台线程里起一次进程树扫描（**不阻塞**调用方）。

    ⚠️ 已经在扫就直接返回 —— 绝不并发起第二次全量枚举
    （那只会把机器搞得更慢）。

    :param force: 是否无视「已经在扫」强行起一次
        （预热收尾时用 —— 那一轮预热已经把状态占住了）。
    """
    with _cloud_proc_lock:
        if (
            not force
            and str(_cloud_proc_state.get("status")) == "scanning"
        ):
            return
        _cloud_proc_state["status"] = "scanning"
        _cloud_proc_state["started_at"] = time.monotonic()
        _cloud_proc_state["finished_at"] = 0.0
        _cloud_proc_state["error"] = ""
        _cloud_proc_state["items"] = []
    thread = threading.Thread(
        target=_run_cloud_process_scan,
        name="sentinel-proc-scan",
        daemon=True,
    )
    thread.start()


def _scan_cloud_processes(force: bool = False) -> List[Dict[str, Any]]:
    """扫「外部插件派发进程树」，返回正在跑的 Agent CLI 进程。

    流程（见 :mod:`src.proc_tree`）：找桥接宿主 → 枚举子孙 →
    按命令行 / 工作目录认 Agent → 关联工作目录下的日志。

    ⚠️ **绝不抛异常** —— 审查侧绝不因为探测失败而中断（红线 R5）。
    psutil 没装 / 枚举失败 → 返回空列表。

    ⚠️ v4.0.1：**最多只等** :data:`CLOUD_PROC_SCAN_TIMEOUT` 秒。
    超时就返回**已经扫到的那部分**（状态是 ``scanning``），
    后台线程继续扫，前端轮询 ``/api/discover`` 取后续结果。

    :param force: 是否忽略缓存强制重扫。
    :return: 每项一个进程字典（``pid`` / ``agent`` / ``cwd`` /
        ``log_path`` / ``attachable`` / ``state`` …）。
    """
    now = time.monotonic()
    with _cloud_proc_lock:
        cached = _cloud_proc_cache
        fresh = (
            bool(cached.get("ready"))
            and (now - float(cached.get("at") or 0.0)) < CLOUD_PROC_SCAN_TTL
        )
        scanning = str(_cloud_proc_state.get("status")) == "scanning"
        if fresh and not force:
            return list(cached.get("items") or [])

    # 需要扫 —— 但**最多只等 1.5 秒**（v4.0.1 的超时熔断）。
    if not scanning:
        _start_cloud_process_scan()
    deadline = time.monotonic() + CLOUD_PROC_SCAN_TIMEOUT
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        _cloud_proc_event.wait(timeout=min(remaining, 0.05))
        _cloud_proc_event.clear()
        with _cloud_proc_lock:
            if str(_cloud_proc_state.get("status")) != "scanning":
                break
    with _cloud_proc_lock:
        return list(_cloud_proc_state.get("items") or [])


def _warm_cloud_process_scan() -> None:
    """后台预热进程树扫描（v4.0.1）。

    ⚠️ 为什么要**两段**：

    1. 先静默调一次 :func:`src.proc_tree.find_bridge_hosts` ——
       Windows 上第一次枚举几百个进程要 2~3 秒（逐个打开句柄），
       这笔开销在**启动期**付掉；用户点开雷达面板时就只剩几百毫秒；
    2. 再跑一次完整扫描，把结果填进缓存 —— 用户第一次点开面板
       直接就有数据，不用等。

    ⚠️ 失败只记日志 —— 预热不是功能，扫不到也不该影响哨兵启动。
    """
    started = time.monotonic()
    hosts: List[Any] = []
    # ⚠️ **先**把状态标成「扫描中」——
    #    用户此刻点开雷达面板会**等**这一轮，
    #    而不是另起一次全量枚举（那只会更慢、更卡）。
    with _cloud_proc_lock:
        _cloud_proc_state["status"] = "scanning"
        _cloud_proc_state["started_at"] = started
        _cloud_proc_state["finished_at"] = 0.0
        _cloud_proc_state["error"] = ""
        _cloud_proc_state["items"] = []
    try:
        from ..proc_tree import find_bridge_hosts

        hosts = find_bridge_hosts()
    except Exception as exc:  # noqa: BLE001 —— 预热失败只降级
        logger.debug(
            "预热外部插件宿主枚举失败：%s: %s", type(exc).__name__, exc
        )
    warm_ms = (time.monotonic() - started) * 1000.0
    logger.info(
        "预热外部插件进程树枚举完成：宿主 %s 个，耗时 %.0f ms",
        len(hosts),
        warm_ms,
    )
    # 句柄已经热了 —— 接着让后台线程跑一次**完整**扫描，
    # 状态自然从「扫描中」走到「就绪」，缓存也一并填上。
    try:
        _start_cloud_process_scan(force=True)
    except Exception as exc:  # noqa: BLE001 —— 预热失败只降级
        logger.debug(
            "预热外部插件进程树扫描失败：%s: %s", type(exc).__name__, exc
        )
        with _cloud_proc_lock:
            _cloud_proc_state["status"] = "error"
            _cloud_proc_state["error"] = "{}: {}".format(
                type(exc).__name__, exc
            )
            _cloud_proc_state["finished_at"] = time.monotonic()


def _radar_scan_roots(config_path: Optional[str] = None) -> Optional[List[str]]:
    """从配置里读雷达的扫描根目录（``radar.scan_roots``）。

    配置里没写这一项 → 返回 ``None``，雷达用代码内置的默认清单
    （``.codex`` / ``.mcode`` / ``.claude`` / ``.minimax/v2/sessions``）。

    写了一个空列表也当「没写」—— 那是手滑，不该让雷达一条都扫不到。

    :param config_path: 配置文件路径；为空时按默认候选顺序找。
    :return: 路径清单；用默认清单就是 ``None``。
    """
    from ..config_loader import load_config

    target = _resolve_runtime_config_path(config_path)
    if not target:
        return None
    try:
        raw = load_config(target)
    except Exception as exc:  # noqa: BLE001 —— 配置坏了只降级
        logger.warning(
            "读取雷达扫描根目录失败（%s）：%s: %s",
            target,
            type(exc).__name__,
            exc,
        )
        return None

    radar = raw.get("radar") if isinstance(raw, dict) else None
    if not isinstance(radar, dict):
        return None
    roots = radar.get("scan_roots")
    if not isinstance(roots, (list, tuple)):
        return None
    cleaned = [str(item).strip() for item in roots if str(item).strip()]
    return cleaned or None


def _radar_exclude_keywords(
    config_path: Optional[str] = None,
) -> Optional[List[str]]:
    """从配置里读雷达的**排除关键词**（``radar.exclude_keywords``）。

    用户实测：「哨兵去抓自己压测产生的残留日志，
    却漏掉了用户真实在跑的外部插件任务。」

    配置里没写这一项 → 返回 ``None``，
    雷达用代码内置的默认清单（``sentinel`` / ``stress`` / ``test``）。

    ⚠️ 写了一个**空列表**是「我什么都不想排除」——
    与「没写」是两回事，所以这里空列表返回 ``[]`` 而不是 ``None``。

    :param config_path: 配置文件路径；为空时按默认候选顺序找。
    :return: 关键词清单；用默认清单就是 ``None``。
    """
    from ..config_loader import load_config

    target = _resolve_runtime_config_path(config_path)
    if not target:
        return None
    try:
        raw = load_config(target)
    except Exception as exc:  # noqa: BLE001 —— 配置坏了只降级
        logger.warning(
            "读取雷达排除关键词失败（%s）：%s: %s",
            target,
            type(exc).__name__,
            exc,
        )
        return None

    radar = raw.get("radar") if isinstance(raw, dict) else None
    if not isinstance(radar, dict):
        return None
    keywords = radar.get("exclude_keywords")
    if not isinstance(keywords, (list, tuple)):
        return None
    cleaned = [str(item).strip() for item in keywords if str(item).strip()]
    return cleaned


def _radar_custom_features(
    config_path: Optional[str] = None,
) -> List[str]:
    """从配置里读雷达的**自定义进程特征**（v3.4.0）。

    配置项是 ``radar.custom_process_features`` —— 一串自定义关键字
    （例如 ``my-cli``）。命令行或工作目录里命中它们的进程，
    雷达也会当成 Agent 进程提示出来。

    :param config_path: 配置文件路径；为空时按默认候选顺序找。
    :return: 特征清单；没配就是空列表。
    """
    from ..config_loader import load_config

    target = _resolve_runtime_config_path(config_path)
    if not target:
        return []
    try:
        raw = load_config(target)
    except Exception as exc:  # noqa: BLE001 —— 配置坏了只降级
        logger.warning(
            "读取雷达自定义特征失败（%s）：%s: %s",
            target,
            type(exc).__name__,
            exc,
        )
        return []

    radar = raw.get("radar") if isinstance(raw, dict) else None
    if not isinstance(radar, dict):
        return []
    features = radar.get("custom_process_features")
    if not isinstance(features, (list, tuple)):
        return []
    return [str(item).strip() for item in features if str(item).strip()]


def _effective_exclude_keywords(
    config_path: Optional[str] = None,
) -> List[str]:
    """雷达**当前实际生效**的排除关键词（v3.4.0）。

    配置里写了就用配置的；没写就用内置默认清单
    （``sentinel`` / ``stress`` / ``test``）。

    :param config_path: 配置文件路径；为空时按默认候选顺序找。
    :return: 关键词清单。
    """
    from ..radar import DEFAULT_EXCLUDE_KEYWORDS

    configured = _radar_exclude_keywords(config_path)
    if configured is None:
        return list(DEFAULT_EXCLUDE_KEYWORDS)
    return configured


def _current_workspace() -> str:
    """哨兵**当前**所在的工作目录（v3.0.2）。

    ⚠️ 为什么单独返回
    ****************
    任务书要求雷达「明确列出当前的 cwd 工作区」——
    用户得一眼看出「我现在站在哪个目录里」，
    而不是从一堆会话里猜。

    :return: 当前工作目录；取不到就是空串。
    """
    try:
        return os.path.abspath(os.getcwd())
    except (OSError, ValueError) as exc:
        logger.warning(
            "读取当前工作目录失败：%s: %s", type(exc).__name__, exc
        )
        return ""


def _radar_roots_for_display(config_path: Optional[str] = None) -> List[Any]:
    """雷达**实际**要扫的目录清单（给设置页显示用）。

    :param config_path: 配置文件路径。
    :return: ``{"agent", "label", "path"}`` 字典列表。
    """
    from ..radar import agent_roots

    try:
        return list(agent_roots(scan_roots=_radar_scan_roots(config_path)))
    except Exception as exc:  # noqa: BLE001 —— 只降级，不挡设置页
        logger.warning(
            "列出雷达扫描目录失败：%s: %s", type(exc).__name__, exc
        )
        return []


#: v3.7.0 新增的配置键及其默认值。
#:
#: 这些键在用户的**旧** ``config.yaml`` 里根本不存在，而配置写回器
#: 「找不到路径就跳过」—— 所以保存前必须先把它们**补齐**
#: （:func:`~src.config_loader.ensure_config_defaults`）。
#:
#: ⚠️ 形态只能是**标量或标量列表** —— 配置写回器只支持这两种，
#:    映射和「字典列表」写不回去。
V370_CONFIG_DEFAULTS: Dict[str, Any] = {
    "rules": {
        # L1 规则层总开关（老配置里就有这一项，缺了就补上）。
        "enabled": True,
        # L1 命中后的拦截策略：
        # red_line_only / suspend / alert / escalate。
        "l1_action": "red_line_only",
        # 被**关闭**的规则 id。
        "disabled_rules": [],
        # 被降级成「仅记录」的规则 id。
        "log_only_rules": [],
        # 用户自定义规则；每项是**一段 JSON 文本**（写回器只能写标量）。
        "custom_rules": [],
        # 红线类别：总开关 + 白名单。
        # ⚠️ 白名单里的四个类别就是 ``src/rules/registry.py`` 里真实登记的
        #    四个红线类别 —— 不多不少（老配置里这两个键可能没有，
        #    补上之后规则页的红线开关才是**真的**能写回去）。
        "catastrophic": {
            "enabled": True,
            "categories": [
                "root_delete",
                "mass_delete",
                "disk_wipe",
                "remote_exec",
            ],
        },
    },
    "llm": {
        "cascade": {
            # L2（本地过滤）/ L3（云端终审）各自的独立开关。
            "l2": {"enabled": True},
            "l3": {"enabled": True},
            # L2 关 + L3 开时，L1 未命中的事件直接桥接给云端 L3。
            "bridge_l1_to_l3": False,
            # --- v3.8.0：L2 掉线热切 L3 + 回退预算熔断 ---
            # L2 临时性掉线时，是否把载荷自动转给 L3。
            "failover_to_l3": True,
            # 预算熔断：窗口（秒）内最多热切多少次；0 = 不限。
            "l3_fallback_limit": 100,
            "l3_fallback_limit_seconds": 3600,
            # 金额上限与单次预估费用；两者都为 0 时金额维度不生效。
            "l3_fallback_cost_limit": 0,
            "l3_fallback_unit_cost": 0,
        },
    },
    "presentation": {
        # 右侧「被监控日志」的极限压缩档位（0-100）。
        "log_compression": 40,
        # 中间「实时审核流水」的极限压缩档位（0-100）。
        "audit_compression": 60,
    },
}


def _rules_snapshot(config: Dict[str, Any]) -> Dict[str, Any]:
    """L1 规则清单快照（给 ``/api/rules`` 用）。

    :param config: 已加载的配置字典。
    :return: 含 ``rules`` / ``disabled`` / ``log_only`` / ``custom`` /
        ``l1_action`` / ``l1_action_labels`` 的字典。
    """
    from ..rules.registry import (
        BUILTIN_CATASTROPHIC_RULES,
        KIND_CATASTROPHIC,
        L1_ACTION_LABELS,
        RuleSelection,
        export_rules,
    )

    rules_config = config.get("rules") or {}
    selection = RuleSelection.from_config(rules_config)
    catastrophic = rules_config.get("catastrophic") or {}
    master = bool(catastrophic.get("enabled", True))
    known = {
        entry.id.split(".", 1)[1]
        for entry in BUILTIN_CATASTROPHIC_RULES
    }
    configured = catastrophic.get("categories")
    if configured is None:
        allowed = set(known)
    else:
        allowed = {
            str(item).strip().lower()
            for item in configured
            if str(item).strip()
        } & known

    rows = export_rules(selection)
    for row in rows:
        if row.get("kind") != KIND_CATASTROPHIC:
            continue
        # 红线规则**三重生效**：总开关开 + 没被关闭 + 在白名单里。
        # ⚠️ 三者缺一，页面上那个「开」就是**假开关** —— 必须如实反映。
        category = str(row.get("id") or "").split(".", 1)[-1]
        row["enabled"] = bool(
            master and row.get("enabled") and category in allowed
        )
    return {
        "rules": rows,
        "disabled": sorted(selection.disabled),
        "log_only": sorted(selection.log_only),
        "custom": [entry.as_dict() for entry in selection.custom],
        "l1_action": selection.l1_action,
        "l1_action_labels": dict(L1_ACTION_LABELS),
        "catastrophic_enabled": master,
        "catastrophic_categories": sorted(allowed),
    }


def _dump_custom_rules(raw: Any) -> List[str]:
    """把自定义规则压成**可写回**的形态（每项一段 JSON 文本）。

    配置写回器只支持标量与标量列表 —— 映射写不回去，
    所以落盘时每条规则压成一段 JSON 文本。

    :param raw: 规则列表（每项可以是字典或 JSON 文本）。
    :return: JSON 文本列表；不合法的项被丢弃并记日志。
    """
    from ..rules.registry import dump_custom_rules, parse_custom_rules

    entries, errors = parse_custom_rules(raw)
    for message in errors:
        logger.warning("自定义规则被跳过：%s", message)
    return dump_custom_rules(entries)


def _normalize_categories(raw: Any) -> List[str]:
    """把红线类别列表收敛成**真实存在**的类别（v3.7.0）。

    ⚠️ v3.6.x 的设置页里有四个「死控件」
    （提权 / 网络变更 / 清历史 / 读凭据）——
    它们从来**没有**对应的判定逻辑，勾上也不生效。
    这里只认 ``src/rules/registry.py`` 里真实登记的类别，
    免得把不存在的类别写回配置文件。

    :param raw: 类别名列表。
    :return: 去重、排序后的真实类别名列表。
    """
    known = set(_catastrophic_category_names())
    picked = {
        str(item).strip().lower()
        for item in (raw or [])
        if str(item).strip()
    }
    return sorted(picked & known)


def _catastrophic_category_names() -> List[str]:
    """真实存在的红线类别名（v3.7.0）。

    :return: 类别名列表（顺序与内置清单一致）。
    """
    from ..rules.registry import BUILTIN_CATASTROPHIC_RULES

    return [
        entry.id.split(".", 1)[1]
        for entry in BUILTIN_CATASTROPHIC_RULES
    ]


def _clamp_level(raw: Any, fallback: int) -> int:
    """把压缩档位夹到 0-100（v3.7.0）。

    :param raw: 原始值（可能是字符串）。
    :param fallback: 读不出来时用的默认值。
    :return: 0-100 的整数档位。
    """
    try:
        level = int(raw)
    except (TypeError, ValueError):
        level = int(fallback)
    return max(0, min(100, level))


def _collect_config_updates(
    payload: Dict[str, Any]
) -> List[Tuple[str, Any]]:
    """把设置页的表单翻译成「(yaml 路径, 新值)」列表（v2.3.0）。

    设置页给的是**结构化**的 JSON（按界面分区），
    而配置写回需要的是**扁平**的 yaml 路径 ——
    这里就是那层翻译。

    ⚠️ 打码值（``********``）会被**跳过**：
    用户没改密钥时，界面回传的就是打码值，
    拿它去写会把用户真正的密钥**抹掉**。

    :param payload: 设置页提交的 JSON。
    :return: ``(yaml 路径, 新值)`` 列表。
    """
    updates: List[Tuple[str, Any]] = []

    def _put(path: str, value: Any) -> None:
        """记一条更新；打码值直接跳过。"""
        if value == SECRET_MASK:
            return
        updates.append((path, value))

    llm = payload.get("llm")
    if isinstance(llm, dict):
        for key in (
            "provider",
            "base_url",
            "model",
            "timeout",
            "max_context_chars",
            "risk_threshold",
            "redact_before_llm",
            "keep_alive",
            "failure_threshold",
            "breaker_open_seconds",
            "api_key",
            "temperature",
            "max_tokens",
            "json_mode",
        ):
            if key in llm:
                _put("llm.{}".format(key), llm[key])

    routing = payload.get("routing")
    if isinstance(routing, dict):
        for key in (
            "enabled",
            "escalate_on_red_line",
            "drift_escalate_threshold",
        ):
            if key in routing:
                _put("llm.routing.{}".format(key), routing[key])
        fallback = routing.get("fallback")
        if isinstance(fallback, dict):
            for key in ("provider", "base_url", "model", "api_key"):
                if key in fallback:
                    _put("llm.routing.fallback.{}".format(key), fallback[key])

    # --- v3.5.0：级联审核（L2 本地过滤 / L3 云端终审） ---
    cascade = payload.get("cascade")
    if isinstance(cascade, dict):
        for key in (
            "enabled",
            "require_agreement",
            "breaker_threshold",
            "breaker_open_seconds",
            # v3.7.0：L2 关 + L3 开时，L1 未命中的事件直接桥接给云端 L3。
            "bridge_l1_to_l3",
            # --- v3.8.0：L2 掉线热切 L3 + 回退预算熔断 ---
            "failover_to_l3",
            "l3_fallback_limit",
            "l3_fallback_limit_seconds",
            "l3_fallback_cost_limit",
            "l3_fallback_unit_cost",
        ):
            if key in cascade:
                _put("llm.cascade.{}".format(key), cascade[key])
        # L2 与 L3 **各自**一份完整配置 —— 刻意不复用顶层 llm 的字段：
        # 两者的 base_url / model / api_key / timeout 完全不是一回事。
        for tier in ("l2", "l3"):
            block = cascade.get(tier)
            if not isinstance(block, dict):
                continue
            for key in (
                # v3.7.0：L2 / L3 各自独立开关（关掉一个不影响另一个）。
                "enabled",
                "provider",
                "base_url",
                "model",
                "api_key",
                "api_key_env",
                "timeout",
                "timeout_floor",
                "temperature",
                "max_tokens",
                "json_mode",
                "reasoning_effort",
            ):
                if key in block:
                    _put(
                        "llm.cascade.{}.{}".format(tier, key),
                        block[key],
                    )

    channels = payload.get("channels")
    if isinstance(channels, list):
        for index, channel in enumerate(channels):
            if not isinstance(channel, dict):
                continue
            for key, value in channel.items():
                _put("alert.channels[{}].{}".format(index, key), value)

    rules = payload.get("rules")
    if isinstance(rules, dict):
        if "enabled" in rules:
            _put("rules.enabled", rules.get("enabled"))
        if "dangerous_commands" in rules:
            commands = rules.get("dangerous_commands")
            if isinstance(commands, list):
                _put("rules.dangerous_commands", commands)
        if "hot_reload_enabled" in rules:
            _put("rules.hot_reload.enabled", rules.get("hot_reload_enabled"))
        categories = rules.get("catastrophic_categories")
        if isinstance(categories, list):
            _put("rules.catastrophic.categories", categories)
        if "catastrophic_enabled" in rules:
            _put(
                "rules.catastrophic.enabled",
                rules.get("catastrophic_enabled"),
            )
        # --- v3.7.0：规则清单的单条开关 / 降级 / 自定义规则 ---
        #
        # ⚠️ 三者都是**标量列表** —— 配置写回器写不了映射。
        l1_action = rules.get("l1_action")
        if isinstance(l1_action, str) and l1_action.strip():
            _put("rules.l1_action", l1_action.strip())
        disabled = rules.get("disabled_rules")
        if isinstance(disabled, list):
            _put(
                "rules.disabled_rules",
                [
                    str(item).strip()
                    for item in disabled
                    if str(item).strip()
                ],
            )
        log_only = rules.get("log_only_rules")
        if isinstance(log_only, list):
            _put(
                "rules.log_only_rules",
                [
                    str(item).strip()
                    for item in log_only
                    if str(item).strip()
                ],
            )
        custom = rules.get("custom_rules")
        if isinstance(custom, list):
            _put("rules.custom_rules", _dump_custom_rules(custom))

    # --- v3.7.0：呈现与摘要（两个极限压缩档位） ---
    presentation = payload.get("presentation")
    if isinstance(presentation, dict):
        for key in ("log_compression", "audit_compression"):
            if key not in presentation:
                continue
            value = presentation.get(key)
            try:
                level = int(value)
            except (TypeError, ValueError):
                continue
            _put(
                "presentation.{}".format(key),
                max(0, min(100, level)),
            )

    # 雷达扫描根目录（v3.0.1）——
    # 一行一个路径；相对路径按家目录解析，绝对路径原样使用。
    radar = payload.get("radar")
    if isinstance(radar, dict) and "scan_roots" in radar:
        roots = radar.get("scan_roots")
        if isinstance(roots, list):
            cleaned = [
                str(item).strip() for item in roots if str(item).strip()
            ]
            _put("radar.scan_roots", cleaned)

    # 雷达排除关键词（v3.4.0）——
    # 路径里命中这些词的日志**一律不显示**。
    #
    # ⚠️ **空列表是有意义的** —— 它表示「我什么都不想排除」，
    #    与「没配，用默认清单」是两回事，所以照样写回去。
    if isinstance(radar, dict) and "exclude_keywords" in radar:
        keywords = radar.get("exclude_keywords")
        if isinstance(keywords, list):
            cleaned = [
                str(item).strip()
                for item in keywords
                if str(item).strip()
            ]
            _put("radar.exclude_keywords", cleaned)

    # 雷达自定义进程特征（v3.4.0）——
    # 用户自己写的 Agent（改了包名的 fork 之类）靠它认。
    if isinstance(radar, dict) and "custom_process_features" in radar:
        features = radar.get("custom_process_features")
        if isinstance(features, list):
            cleaned = [
                str(item).strip()
                for item in features
                if str(item).strip()
            ]
            _put("radar.custom_process_features", cleaned)

    return updates




def _is_allowed_host(
    host_header: str,
    host: str,
    allow_remote: bool,
) -> bool:
    """判断 ``Host`` 头是否指向本机。

    :param host_header: 请求的 Host 头。
    :param host: 应用监听地址。
    :param allow_remote: 是否允许远程监听。
    :return: 允许返回 ``True``。
    """
    if not host_header:
        return True
    name = _host_of(host_header)
    if not name:
        return True
    if name in _IGNORED_HOSTS:
        return True
    if _is_loopback_host(name):
        return True
    if name == str(host or "").strip().lower():
        return True
    return bool(allow_remote)


def _is_allowed_origin(
    origin: str,
    host_header: str,
    origins: Iterable[str],
    allow_remote: bool,
) -> bool:
    """判断 ``Origin`` 是否被允许。

    :param origin: 请求的 Origin 头。
    :param host_header: 请求的 Host 头（用于判断是否同源）。
    :param origins: 显式白名单。
    :param allow_remote: 是否允许远程监听（放宽到「同源即可」）。
    :return: 允许返回 ``True``。
    """
    value = str(origin or "").strip()
    if value in set(origins):
        return True
    if allow_remote:
        # 远程模式下允许「来源与访问地址一致」的情况。
        origin_host = _host_of(value.split("://", 1)[-1])
        request_host = _host_of(host_header)
        if origin_host and origin_host == request_host:
            return True
    return False


# ---------------------------------------------------------------------------
# 命令行入口
# ---------------------------------------------------------------------------
def _prepare_streams() -> None:
    """让重定向 / 管道场景下的中文日志不乱码。

    Windows 控制台本身走 UTF-16 通道，中文天然正常；但 stdout / stderr
    一旦被重定向到文件或管道，Python 会退回系统 ANSI 代码页，
    看板那些中文提示就会变成一串「ʵʱ��ʽ��」。

    只在**非交互**场景切换编码与缓冲方式，不影响真实终端。
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


def _tail_scan_stub(_target: str) -> Dict[str, Any]:
    """tail 模式下的「立即扫描」占位回调。

    流式模式下事件是**来一条审一条**的，再把整份历史日志扫一遍只会
    产生重复告警。所以这里不回扫描结果，只回一句「已经在流式审查」。

    :param _target: 被监控日志路径（本回调用不到）。
    :return: 一个不含 ``counts`` 的说明字典。
    """
    return {
        "ok": True,
        "mode": "tail",
        "note": "实时监听模式：事件已在流式审查，无需重复扫描",
    }


def _detect_agent_of(watch_path: str) -> str:
    """按日志**内容**猜它是谁（v2.3.0）。

    哨兵是雷达 —— 用户指着哪个日志说「盯这个」，
    哨兵就该**自己看出来**那是 Codex 还是 Claude，
    而不是逼用户先回答「这文件是什么格式」。

    猜不出来就返回 ``"unknown``（调用方会退回最宽容的 mcode 适配器）。

    :param watch_path: 被监控日志路径。
    :return: ``codex`` / ``mcode`` / ``claude`` / ``unknown``。
    """
    if not watch_path:
        return "unknown"
    try:
        from ..radar import describe_session

        described = describe_session(watch_path)
    except Exception as exc:  # noqa: BLE001 —— 识别失败只降级
        logger.warning("识别日志类型失败（%s）：%s", type(exc).__name__, exc)
        return "unknown"
    if described is None:
        return "unknown"
    return described.agent


def _build_adapter(adapter_name: str, watch_path: str = "") -> Any:
    """按 Agent 类型挑一个适配器（v2.3.0）。

    雷达挂载进来的会话必须按**识别出来的**类型选适配器 ——
    codex 与 mcode 的事件长得像但字段名不同，选错就会整路漏审。

    ``adapter_name`` 传 ``"auto"``（或空串）时按日志**内容**自动识别；
    认不出来就退回 mcode —— 它是三者里最宽容的那个。

    :param adapter_name: ``mcode`` / ``codex`` / ``claude`` / ``auto``。
    :param watch_path: 被监控日志路径（自动识别时用）。
    :return: 适配器实例。
    """
    name = str(adapter_name or "").strip().lower()
    if name in ("", "auto"):
        name = _detect_agent_of(watch_path)
    if name == "codex":
        from ..adapters.codex import CodexAdapter

        return CodexAdapter()
    if name == "claude":
        from ..adapters.claude import ClaudeAdapter

        return ClaudeAdapter()
    from ..adapters.mcode import McodeAdapter

    return McodeAdapter()


def _build_scan_callback(
    alert_log_path: str,
    config_path: Optional[str] = None,
    guardian: Optional[Any] = None,
    task_goal: Optional["TaskGoal"] = None,
    adapter_name: str = "auto",
) -> Callable[[str], Dict[str, Any]]:
    """构造「立即扫描」回调。

    引擎相关模块在这里**惰性导入** —— FastAPI 应用层因此完全不依赖
    ``src.core.engine``，测试 GUI 时也不需要把引擎拖进来。

    :param alert_log_path: 告警日志路径。
    :param config_path: 配置文件路径。
    :param guardian: 守护者实例；扫描时挂给引擎，红线事件才会真的
        被拦截。
    :param task_goal: 任务目标持有者；扫描时读它来构造意图追踪器。
    :param adapter_name: 用哪个适配器解析（v2.3.0）——
        ``mcode`` / ``codex`` / ``claude``。雷达挂载进来的会话
        必须按**识别出来的** Agent 类型选适配器，
        否则 codex 的事件会被 mcode 适配器漏掉。
    :return: 接收日志路径、返回扫描摘要的回调。
    """
    from ..config_loader import load_config
    from ..core.engine import IntentTracker, SentinelEngine
    from ..core.timebox import build_timebox
    from ..llm.factory import build_routing_provider
    from ..rules.basic import RuleEngine

    config = load_config(config_path)
    llm_config = config.get("llm") or {}
    llm_client = None
    if llm_config.get("enabled", True):
        try:
            llm_client = build_routing_provider(config)
        except ValueError as exc:  # noqa: BLE001 —— 配置问题只降级
            logger.warning("模型层配置有误（%s），只输出规则层判定。", exc)
    # 模型客户端的缓存（v2.3.0）——
    # 配置没动就复用，动了才重建（省掉每次扫描的握手开销）。
    llm_cache: Dict[str, Any] = {"config": llm_config, "client": llm_client}

    def _current_llm_client(current: Dict[str, Any]) -> Any:
        """按当前配置拿模型客户端（配置没变就复用旧的）。"""
        llm_cfg = current.get("llm") or {}
        if llm_cache["config"] == llm_cfg:
            return llm_cache["client"]
        client = None
        if llm_cfg.get("enabled", True):
            try:
                client = build_routing_provider(current)
            except ValueError as exc:  # noqa: BLE001 —— 配置问题只降级
                logger.warning("模型层配置有误（%s），只输出规则层判定。", exc)
        llm_cache["config"] = llm_cfg
        llm_cache["client"] = client
        return client

    def _make_intent_tracker(
        current: Dict[str, Any],
        client: Any,
    ) -> Optional[Any]:
        """按当前任务目标造一个意图追踪器。"""
        if client is None:
            return None
        goal = ""
        if task_goal is not None:
            goal = task_goal.get()
        if not goal:
            goal = str(current.get("task_goal") or "")
        if not goal:
            return None
        drift = current.get("drift") or {}
        return IntentTracker(
            goal=goal,
            window=int(drift.get("window", 5)),
            threshold=int(drift.get("threshold", 2)),
            enabled=bool(drift.get("enabled", True)),
            evaluator=client.evaluate_drift,
        )

    def scan(target: str) -> Dict[str, Any]:
        """跑一次哨兵引擎，把告警写进告警日志。"""
        # ⚠️ 每次扫描都**重新读一次配置**（v2.3.0）：
        # 设置页改完规则点「保存并应用」，下一次扫描就该用新规则，
        # 不该逼用户重启哨兵。
        current = load_config(config_path)
        try:
            rules = RuleEngine(current)
        except Exception as exc:  # noqa: BLE001 —— 坏配置只降级
            logger.warning("规则编译失败（%s），本次扫描沿用旧规则。", exc)
            rules = RuleEngine(config)
        client = _current_llm_client(current)
        # 适配器在**扫描时**才定 —— 因为这时候才知道要审哪份日志。
        # 用户指着哪个文件说「盯这个」，哨兵就该自己看出那是哪种格式。
        engine = SentinelEngine(
            _build_adapter(adapter_name, target),
            rules,
            alert_log_path=alert_log_path,
            console=False,
            llm_client=client,
            guardian=guardian,
            intent_tracker=_make_intent_tracker(current, client),
            timebox=build_timebox(current),
        )
        # --- v3.5.0：级联审核 ---
        engine.set_cascade(
            _build_cascade(current, str(current.get("task_goal") or ""))
        )
        try:
            return engine.run(target)
        finally:
            engine.close()

    return scan


def _build_guardian(
    config_path: Optional[str] = None,
    watch_path: Optional[str] = None,
    target_pid: Optional[int] = None,
) -> Optional[Any]:
    """构造守护者实例（守护者没启用时返回 ``None``）。

    同样走**惰性导入** —— GUI 应用层依旧不认识 ``src.core.guardian``。

    :param config_path: 配置文件路径。
    :param watch_path: 被监控日志路径（用于自动定位被监控进程）。
    :param target_pid: 已知的被监控进程 PID（v2.4.0）——
        给了就用它，**不再**自己去猜。
    :return: 守护者实例；未启用时返回 ``None``。
    """
    from ..config_loader import load_config
    from ..core.guardian import Guardian

    config = load_config(config_path)
    guardian = Guardian(config)
    if not guardian.enabled:
        return None
    if target_pid:
        # 用户已经**手动武装**了 —— PID 是我们自己反查出来的，
        # 比按命令行特征瞎猜可靠得多。
        guardian.set_target_pid(int(target_pid))
        return guardian
    if watch_path:
        try:
            pid = guardian.detect_pid(watch_path)
            if pid:
                guardian.set_target_pid(pid)
        except Exception as exc:  # noqa: BLE001 —— 探测失败只降级
            logger.warning("定位被监控进程失败：%s: %s", type(exc).__name__, exc)
    return guardian


class TailRunner:
    """常驻的「实时监听」引擎（看板 ``--tail`` 模式用）。

    SSE 链路是这样串起来的::

        引擎(tail 常驻) → 告警日志 → 本模块的 FileTailer → SSE → 浏览器

    本类只负责把引擎放进后台线程跑起来，并让**正在运行**的意图追踪器
    能跟着看板顶栏的输入框换锚点 —— 换了目标，之前的漂移判定就不作数。

    :param engine: 已经造好的哨兵引擎。
    :param watch_path: 被监控日志路径（只读）。
    :param task_goal: 任务目标持有者；``None`` 时不支持运行时换锚点。
    :param drift_config: 漂移检测配置（窗口大小 / 阈值 / 开关）。
    :param llm_client: 模型客户端；之前没目标、后来才设目标时用它补追踪器。
    """

    def __init__(
        self,
        engine: Any,
        watch_path: str,
        task_goal: Optional["TaskGoal"] = None,
        drift_config: Optional[Dict[str, Any]] = None,
        llm_client: Optional[Any] = None,
    ) -> None:
        self.engine = engine
        self.watch_path = watch_path
        self.task_goal = task_goal
        self.drift_config = drift_config or {}
        self.llm_client = llm_client
        self._thread: Optional[threading.Thread] = None
        self._error = ""
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # 目标变更
    # ------------------------------------------------------------------
    def apply_goal(self, goal: str) -> None:
        """把新的任务目标推给正在运行的引擎。

        * 引擎已有追踪器 → 换锚点并清零连续计数；
        * 引擎还没有追踪器（启动时没设目标）→ 现补一个。

        ⚠️ 补建追踪器时**必须**走引擎的
        :meth:`~src.core.engine.SentinelEngine.set_intent_tracker` ——
        只有它会把漂移结论的回调接上，
        否则追踪器在判、告警发不出来（v2.4.0）。

        :param goal: 新目标；空串表示停用意图漂移检测。
        """
        value = str(goal or "").strip()
        tracker = getattr(self.engine, "intent_tracker", None)
        setter = getattr(self.engine, "set_intent_tracker", None)
        if tracker is not None:
            if not value:
                # 目标被**清空** → 停用意图漂移检测。
                #
                # ⚠️ 追踪器的 ``set_goal("")`` 是个**空操作** ——
                # 它不会清掉旧锚点。所以这里直接把追踪器摘掉，
                # 不然界面说「已停用」，引擎却还在拿旧目标判漂移。
                if callable(setter):
                    setter(None)
                else:  # pragma: no cover —— 假引擎（测试用）
                    self.engine.intent_tracker = None
                return
            # 看板里人工设定的目标 = **显式**目标，不被自动锚定覆盖。
            tracker.set_goal(value, explicit=True)
            return
        if not value or self.llm_client is None:
            return
        from ..core.engine import IntentTracker

        tracker = IntentTracker(
            goal=value,
            window=int(self.drift_config.get("window", 5)),
            threshold=int(self.drift_config.get("threshold", 2)),
            enabled=bool(self.drift_config.get("enabled", True)),
            evaluator=self.llm_client.evaluate_drift,
        )
        setter = getattr(self.engine, "set_intent_tracker", None)
        if callable(setter):
            setter(tracker)
        else:  # pragma: no cover —— 假引擎（测试用）没有这个口子
            self.engine.intent_tracker = tracker

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def start(self) -> None:
        """拉起后台线程，开始实时监听。"""
        with self._lock:
            if self._thread is not None:
                return
            self._thread = threading.Thread(
                target=self._run,
                name="sentinel-tail",
                daemon=True,
            )
            self._thread.start()

    def _run(self) -> None:
        """线程主体：阻塞式监听，直到收到停止信号。"""
        try:
            self.engine.run(self.watch_path, tail=True)
        except Exception as exc:  # noqa: BLE001 —— 审查侧异常不外泄
            self._error = f"{type(exc).__name__}: {exc}"
            logger.exception("实时监听线程异常退出：%s", exc)
        finally:
            try:
                self.engine.close()
            except Exception as exc:  # noqa: BLE001 —— 收尾失败只记录
                logger.warning("引擎收尾失败：%s: %s", type(exc).__name__, exc)

    def stop(self) -> None:
        """停止监听。

        **不杀任何进程** —— 只是把引擎的停止信号置位，
        让它自己从监听循环里退出来（红线 R5：绝不 kill）。
        """
        stop_event = getattr(self.engine, "stop_event", None)
        if stop_event is not None:
            stop_event.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=5)

    @property
    def running(self) -> bool:
        """监听线程是否还活着。"""
        thread = self._thread
        return bool(thread is not None and thread.is_alive())

    @property
    def error(self) -> str:
        """线程里捕获到的异常摘要（空串表示没出错）。"""
        return self._error


def _build_cascade(
    config: Dict[str, Any],
    goal: str = "",
) -> Optional[Any]:
    """按配置构造**级联审核**编排器（v3.5.0）。

    装配失败**只降级** —— 级联是增强，不是审查的前提。

    :param config: 已加载的配置字典。
    :param goal:   任务目标（进静态前缀）。
    :return: :class:`~src.llm.cascade.CascadeRouter`；没开就返回 ``None``。
    """
    try:
        from ..llm.factory import build_cascade_provider

        return build_cascade_provider(config, goal=goal)
    except Exception as exc:  # noqa: BLE001 —— 配错只降级
        logger.warning("级联审核装配失败（已降级）：%s: %s", type(exc).__name__, exc)
        return None


def _build_tail_runner(
    watch_path: str,
    alert_log_path: str,
    config_path: Optional[str] = None,
    guardian: Optional[Any] = None,
    task_goal: Optional["TaskGoal"] = None,
    adapter_name: str = "auto",
    inherit_config_goal: bool = True,
) -> TailRunner:
    """构造常驻的实时监听运行器。

    和 :func:`_build_scan_callback` 一样，引擎相关模块在这里**惰性导入**
    —— GUI 应用层依旧不认识 ``src.core.engine``。

    :param watch_path: 被监控日志路径。
    :param alert_log_path: 告警日志路径。
    :param config_path: 配置文件路径。
    :param guardian: 守护者实例。
    :param task_goal: 任务目标持有者。
    :param adapter_name: 用哪个适配器解析（v2.3.0）——
        雷达挂载进来的会话必须按**识别出来的** Agent 类型选适配器。
    :param inherit_config_goal: 启动时没有目标时，要不要拿配置文件里的
        ``task_goal`` 当默认值（v2.4.0）。
        挂载进来的会话传 ``False`` —— 它干活的是**另一件事**，
        拿全局配置里的目标去套它，只会凭空造出一堆假的漂移告警。
    :return: 未启动的 :class:`TailRunner`（由调用方决定何时 ``start``）。
    """
    from ..config_loader import load_config
    from ..core.engine import IntentTracker, SentinelEngine
    from ..core.timebox import build_timebox
    from ..llm.factory import build_routing_provider
    from ..rules.basic import RuleEngine

    config = load_config(config_path)
    rule_engine = RuleEngine(config)
    llm_config = config.get("llm") or {}
    llm_client = None
    if llm_config.get("enabled", True):
        try:
            llm_client = build_routing_provider(config)
        except ValueError as exc:  # noqa: BLE001 —— 配置问题只降级
            logger.warning("模型层配置有误（%s），只输出规则层判定。", exc)
    adapter = _build_adapter(adapter_name, watch_path)
    drift_config = config.get("drift") or {}
    # 时间盒（v3.3.0）—— 「一个子任务吃掉整轮」的硬性拦截。
    timebox_detector = build_timebox(config)

    goal = ""
    if task_goal is not None:
        goal = task_goal.get()
    if not goal and inherit_config_goal:
        goal = str(config.get("task_goal") or "")

    tracker = None
    if goal and llm_client is not None:
        tracker = IntentTracker(
            goal=goal,
            window=int(drift_config.get("window", 5)),
            threshold=int(drift_config.get("threshold", 2)),
            enabled=bool(drift_config.get("enabled", True)),
            evaluator=llm_client.evaluate_drift,
        )

    engine = SentinelEngine(
        adapter,
        rule_engine,
        alert_log_path=alert_log_path,
        console=True,
        llm_client=llm_client,
        guardian=guardian,
        intent_tracker=tracker,
        timebox=timebox_detector,
    )
    # --- v3.5.0：级联审核 + 记忆参数 ---
    memory_config = config.get("memory") or {}
    engine.memory_steps = int(memory_config.get("short_term_steps", 4))
    engine.memory_summary_threshold = int(
        memory_config.get("long_term_threshold", 10)
    )
    engine.set_cascade(_build_cascade(config, goal))
    # 引擎心跳看门狗（v3.3.0）—— 引擎掉线时立刻红色告警。
    try:
        from ..core.engine_watchdog import start_engine_watchdog

        watchdog = start_engine_watchdog(config)
        if watchdog is not None:
            engine.set_engine_watchdog(watchdog)
    except Exception as exc:  # noqa: BLE001 —— 看门狗失败不阻断服务
        logger.warning("引擎看门狗启动失败：%s: %s", type(exc).__name__, exc)
    return TailRunner(
        engine,
        watch_path,
        task_goal=task_goal,
        drift_config=drift_config,
        llm_client=llm_client,
    )


# ---------------------------------------------------------------------------
# 雷达挂载（v2.3.0）
# ---------------------------------------------------------------------------
def _attach_alert_log_path(watch_path: str) -> str:
    """给被挂载的日志算一个哨兵自己的告警日志路径。

    ⚠️ **绝对不能**写到用户被监控的项目目录里 ——
    哨兵对被监控对象是**只读**的（红线 R1）。
    告警日志一律落在哨兵自己的 ``logs/attached/`` 下面。

    :param watch_path: 被挂载的日志路径。
    :return: 告警日志绝对路径。
    """
    stem = os.path.splitext(os.path.basename(watch_path))[0] or "session"
    safe = "".join(ch for ch in stem if ch.isalnum() or ch in "-_.")[:48]
    # sha1 只用来生成**目录名后缀**，不涉及任何安全用途。
    digest = hashlib.sha1(  # noqa: S324
        watch_path.encode("utf-8")
    ).hexdigest()[:8]
    folder = os.path.join(PROJECT_ROOT, "logs", "attached")
    return os.path.join(folder, "{}-{}.alerts.jsonl".format(safe, digest))


#: 自动提取初始目标时，最多往前看多少行（v3.0.0）。
#:
#: 用户的第一句话一定在会话日志的**开头**，
#: 扫整个文件既慢又没必要 —— 而且日志可能有几十万行。
INITIAL_GOAL_SCAN_LINES = 200

#: 自动提取出来的目标最多留多少字（v3.0.0）。
#:
#: 目标只是**锚点**，不是全文存档 —— 太长会挤爆输入框，
#: 也会让漂移判定变贵。
INITIAL_GOAL_MAX_CHARS = 200

#: 「建议武装」的置信度门槛（v3.0.0）。
#:
#: 置信度 >= 这个值时，界面把【武装守护者】开关**高亮**出来，
#: 并提示「高置信度匹配，建议开启守护者」。
#:
#: ⚠️ 只是**建议**，绝不自动武装 ——
#: 挂起别人的进程必须用户自己点头（红线 R7）。
#:
#: ⚠️ 前端 ``dashboard.js`` 里也存了一份同名常量 —— 两侧必须一致。
ARM_SUGGEST_CONFIDENCE = 0.95


def _extract_initial_goal(watch_path: str, adapter_name: str = "auto") -> str:
    """从日志**开头**挖出用户最初那句 Prompt（v3.0.0）。

    为什么需要它
    ============

    旁路挂载进来的会话，哨兵是从文件**尾部**开始跟随的 ——
    用户那句「我要重构 auth 模块」很可能**根本不在**日志里
    （Agent 的会话日志有时只记工具调用，不记最初的对话）。

    没有锚点就没有滑窗、没有意图漂移检测。
    以前只能让用户**手动**在顶栏输入 —— 多一步手活，
    而且大部分人根本不知道有这一步。

    所以这里主动去日志的前几行里**找**那条用户消息。

    怎么找
    =====

    **复用适配器** —— Codex / mcode / Claude 三种格式的
    「用户消息」长得完全不一样，但适配器本来就懂它们
    （``user_message`` 事件就是它们为「自动锚定」准备的）。
    自己再写一套正则解析只会多一处会过期的知识。

    只读**前几百行** —— 用户的第一句话一定在开头，
    整个文件扫一遍既慢又没必要。

    :param watch_path: 被监控的日志路径。
    :param adapter_name: 用哪个适配器解析（``auto`` = 自动识别）。
    :return: 用户最初那句 Prompt（截断到 200 字）；找不到就是空串。
    """
    from ..adapters.base import BaseAdapter

    try:
        adapter = _build_adapter(adapter_name, watch_path)
    except Exception as exc:  # noqa: BLE001 —— 适配器不可用只降级
        logger.warning("自动提取目标时适配器不可用：%s: %s", type(exc).__name__, exc)
        return ""
    if not isinstance(adapter, BaseAdapter):
        return ""

    try:
        with io.open(watch_path, "r", encoding="utf-8", errors="replace") as handle:
            for index, line in enumerate(handle):
                if index >= INITIAL_GOAL_SCAN_LINES:
                    break
                if not line.strip():
                    continue
                for event in adapter.parse_line(line):
                    if str(event.get("type") or "") != "user_message":
                        continue
                    text = " ".join(str(event.get("text") or "").split())
                    if text:
                        return text[:INITIAL_GOAL_MAX_CHARS]
    except OSError as exc:  # noqa: BLE001 —— 读不到就当没提取到
        logger.info("自动提取目标时读不了 %s：%s", watch_path, exc)
    except Exception as exc:  # noqa: BLE001 —— 解析失败只降级
        logger.warning(
            "自动提取目标失败（%s）：%s: %s",
            watch_path,
            type(exc).__name__,
            exc,
        )
    return ""


def _resolve_attached_goal(
    watch_path: str,
    adapter_name: str = "mcode",
    cwd: str = "",
) -> str:
    """解析挂载会话的**任务锚点**（v3.5.2）。

    三级回退，**从最可靠的开始**：

    1. **日志头部**（:func:`src.radar.extract_task_goal`）——
       直接读会话日志的前几行，找用户最初那句 Prompt。
       这一级覆盖了 mcode 的 ``## Goal`` 压缩摘要、codex 的
       ``response_item``、claude 的 ``user`` 消息，
       还**过滤掉**系统注入的噪声（``<environment_context>`` 之类）。
    2. **命令行**（:func:`src.radar.resolve_task_goal` 的第二级）——
       日志里没有时，去正在跑的 Agent 进程的命令行里找
       （``mcode exec "..."`` 那种一次性启动）。
    3. **适配器解析**（:func:`_extract_initial_goal`）——
       老路子的兜底，走适配器的 ``user_message`` 事件。

    ⚠️ 一条都找不到就返回空串 ——
    界面据此保留「请手动输入」的提示（**绝不编造**）。

    :param watch_path: 被监控的日志路径。
    :param adapter_name: 用哪个适配器解析（第三级兜底）。
    :param cwd: 会话的工作目录（命令行索引的对号入座依据）。
    :return: 任务锚点；找不到就是空串。
    """
    from ..radar import build_prompt_index, resolve_task_goal

    try:
        prompt_index = build_prompt_index()
    except Exception as exc:  # noqa: BLE001 —— 索引失败只降级
        logger.warning("构建命令行 Prompt 索引失败：%s: %s", type(exc).__name__, exc)
        prompt_index = {}

    try:
        goal = resolve_task_goal(
            watch_path,
            agent=adapter_name,
            cwd=cwd,
            prompt_index=prompt_index,
        )
    except Exception as exc:  # noqa: BLE001 —— 提取失败只降级
        logger.warning(
            "自动提取任务锚点失败（%s）：%s: %s",
            watch_path,
            type(exc).__name__,
            exc,
        )
        goal = ""

    if goal:
        return goal

    # 第三级：老路子（适配器解析 ``user_message`` 事件）。
    return _extract_initial_goal(watch_path, adapter_name)


def _lookup_process(
    watch_path: str,
    agent: str = "",
    cwd: str = "",
) -> Dict[str, Any]:
    """反查「正在写这份日志」的进程（v2.4.0）。

    ⚠️ **失败就是失败**：查不到就返回空字典，
    界面据此显示「[未绑定进程]」并禁用武装开关 ——
    绝不拿一个「大概是某个进程」去糊弄用户（红线 R7）。

    :param watch_path: 被挂载的日志路径。
    :param agent: 已识别出的 Agent 类型。
    :param cwd: 已识别出的工作目录。
    :return: 命中字典（见 :meth:`ProcessHit.to_dict`）；查不到就是空字典。
    """
    try:
        from ..procscan import find_process
    except Exception as exc:  # noqa: BLE001 —— 模块不可用只降级
        logger.warning("进程反查模块不可用：%s: %s", type(exc).__name__, exc)
        return {}
    try:
        hit = find_process(watch_path, agent=agent, cwd=cwd)
    except Exception as exc:  # noqa: BLE001 —— 反查失败只降级
        logger.warning("反查进程失败：%s: %s", type(exc).__name__, exc)
        return {}
    if hit is None:
        logger.info("未能反查到写入 %s 的进程。", watch_path)
        return {}
    logger.info(
        "反查到进程 PID %s（策略 %s，置信度 %.2f）：%s",
        hit.pid,
        hit.strategy,
        hit.confidence,
        hit.cmdline[:120],
    )
    return hit.to_dict()


def _arm_attached_guardian(
    engine: Any,
    config_path: Optional[str],
    pid: int,
    armed: bool,
    label: str = "",
) -> Dict[str, Any]:
    """给挂载进来的会话装上 / 卸下守护者（v2.4.0）。

    ⚠️ **只有用户手动打开开关时才会真的接管**（红线 R7）：
    在那之前，挂载进来的会话是**纯只读**的 —— 只报告，不动手。
    哨兵对**别人**启动的进程连碰都不碰，直到用户亲自说「可以管它」。

    :param engine: 挂载会话的常驻引擎。
    :param config_path: 配置文件路径。
    :param pid: 反查到的被监控进程 PID。
    :param armed: ``True`` = 武装，``False`` = 解除武装。
    :param label: 会话展示名（用于提示语）。
    :return: ``{"ok": bool, "armed": bool, "message": str}``；
        失败时多一个 ``error`` 字段。
    """
    if not armed:
        # 解除武装：把守护者从引擎上摘掉。
        try:
            engine.guardian = None
        except Exception as exc:  # noqa: BLE001 —— 摘失败只降级
            logger.warning("解除武装失败：%s: %s", type(exc).__name__, exc)
        return {
            "ok": True,
            "armed": False,
            "message": "已解除武装：{} 恢复为只读监控。".format(label or "该会话"),
        }

    if not pid or int(pid) <= 0:
        return {
            "ok": False,
            "armed": False,
            "error": "尚未绑定进程，无法武装守护者。",
        }

    guardian = _build_guardian(config_path, None, target_pid=int(pid))
    if guardian is None:
        return {
            "ok": False,
            "armed": False,
            "error": (
                "配置里守护者是关闭的（guardian.enabled=false 且 "
                "intervention_mode 不是 guardian）—— 请先到设置页开启。"
            ),
        }
    try:
        engine.guardian = guardian
    except Exception as exc:  # noqa: BLE001 —— 装不上只降级
        logger.warning("武装失败：%s: %s", type(exc).__name__, exc)
        return {
            "ok": False,
            "armed": False,
            "error": "挂载守护者失败：{}".format(exc),
        }
    return {
        "ok": True,
        "armed": True,
        "message": "已武装：红线级告警会挂起 PID {}。".format(int(pid)),
        # 内部字段：调用方取走后塞进 ``session.guardian``，
        # 绝不让它进 JSON（守护者对象不可序列化）。
        "_guardian": guardian,
    }


def _build_attach_factory(
    config_path: Optional[str] = None,
    engine_registry: Optional[List[Any]] = None,
) -> Callable[[Dict[str, Any]], Dict[str, Any]]:
    """构造「挂载一个外部日志」的工厂（v2.3.0）。

    哨兵的新定位是**雷达**：用户**已经在终端里**跑着 Agent，
    哨兵不该要求「必须从我这儿启动」——
    它应该能发现那些正在跑的会话，并把它们**接进来**实时审查。

    挂进来的会话跑的是**常驻实时监听**（``tail -f`` 语义）：
    用户那个 Agent 每写一行，哨兵就立刻审一次。

    ⚠️ 守护者**默认不跟着过去**：
    守护者会真的挂起被监控进程，而挂载进来的会话我们并不清楚
    那个进程到底是不是用户的 Agent —— 宁可少一个能力，
    也不能在**没弄清对象**的时候动手（红线 R7：留痕、可否决）。

    v2.4.0 补上了这条链：反查 PID → 用户**手动**武装 → 才接管。

    :param config_path: 配置文件路径。
    :param engine_registry: 常驻引擎登记表（v2.3.0）——
        挂进来的实时监听引擎会登记进去，
        设置页「保存并应用」时热重载器才能把新规则换到它身上。
    :return: 工厂函数 ``(info) -> dict``。
    """

    def factory(info: Dict[str, Any]) -> Dict[str, Any]:
        """按被挂载日志的路径与 Agent 类型，造一套监听器。

        :param info: ``{"path": …, "agent": …, "cwd": …, "id": …}``。
        :return: ``{"ok": True, "alert_log_path": …, "tail_runner": …,
            "scan_callback": …, "workdir": …, "task_goal": …,
            "process_info": …, "arm_callback": …}``。
        """
        path = str((info or {}).get("path") or "")
        agent = str((info or {}).get("agent") or "mcode")
        cwd = str((info or {}).get("cwd") or "")
        # v3.0.1：**工作区标识** —— 提取不到真工作目录时的兜底
        # （父目录名 + 文件名）。绝不用「未知工作区」——
        # 那句话会让用户以为哨兵坏了。
        workspace = str((info or {}).get("workspace") or "")
        if not path:
            return {"ok": False, "error": "缺少日志路径。"}

        alert_log_path = _attach_alert_log_path(path)
        try:
            os.makedirs(os.path.dirname(alert_log_path), exist_ok=True)
        except OSError as exc:  # noqa: BLE001 —— 目录建不出来只降级
            logger.warning("建告警日志目录失败：%s", exc)

        # 反查「谁在写这个文件」（v2.4.0）——
        #
        # 查不到就是查不到，界面会显示「[未绑定进程]」并禁用武装开关。
        process_info = _lookup_process(path, agent=agent, cwd=cwd)

        # 每个挂载会话**一份自己的**任务目标（v2.4.0）——
        #
        # 旁路日志往往没有最初的 Prompt，用户只能手动注入目标。
        # 有了目标 → 立刻启用滑窗机制，开启意图漂移（DRIFT）监控。
        goal = TaskGoal()
        # 常驻实时监听：用户那个 Agent 每写一行就审一次。
        runner = _build_tail_runner(
            path,
            alert_log_path,
            config_path,
            None,
            goal,
            adapter_name=agent,
            # 挂载会话**不继承**配置里的全局目标（v2.4.0）——
            # 它干的活跟哨兵自己那份任务多半不是一回事，
            # 硬套只会凭空造出一堆假的漂移告警。
            inherit_config_goal=False,
        )
        # 目标一变，引擎里的意图追踪器立刻换锚点并清零连续计数。
        goal.on_change(runner.apply_goal)

        # v3.0.0：**自动提取**用户最初那句 Prompt ——
        #
        # 旁路日志往往没有最初的对话，没有锚点就做不了漂移检测。
        # 与其让用户自己去顶栏翻出那句话再抄一遍，
        # 不如哨兵自己从日志开头把它挖出来。
        #
        # ⚠️ v3.5.2：改走**三级回退**（日志头部 → 命令行 → 适配器）——
        # 以前只有适配器那一级，而 mcode / codex 的真实日志格式
        # 适配器根本认不出「用户消息」，所以永远提取不到。
        extracted = _resolve_attached_goal(path, agent, cwd)
        if extracted:
            # ⚠️ 必须在 ``on_change`` **之后**设定 ——
            # 监听器这时才挂上，设定的那一刻追踪器就被建起来，
            # 漂移监控**立刻**开始工作。
            goal.set(extracted)
            logger.info("已从 %s 自动提取任务目标：%s", path, extracted[:60])

        runner.start()
        # 登记进热重载器 —— 设置页改完规则，这个常驻引擎也要换规则。
        if engine_registry is not None:
            engine_registry.append(runner.engine)
        # 「立即扫描」也要能按同一个 Agent 类型解析。
        scan = _build_scan_callback(
            alert_log_path,
            config_path,
            None,
            None,
            adapter_name=agent,
        )

        def _arm(armed: bool) -> Dict[str, Any]:
            """武装 / 解除武装这个挂载会话的守护者（v2.4.0）。"""
            return _arm_attached_guardian(
                runner.engine,
                config_path,
                int((process_info or {}).get("pid") or 0),
                bool(armed),
                label="[{}] {}".format(
                    agent, cwd or workspace or "未知工作区"
                ),
            )

        return {
            "ok": True,
            "alert_log_path": alert_log_path,
            "tail_runner": runner,
            "scan_callback": scan,
            "workdir": cwd,
            "task_goal": goal,
            # v3.0.0：目标来源 —— ``auto`` 表示「从日志里自动提取的」。
            "goal_source": "auto" if extracted else "none",
            "process_info": process_info,
            "arm_callback": _arm,
        }

    return factory


#: 看板崩溃日志的路径（哨兵自己的 ``logs/`` 目录）。
CRASH_LOG: str = os.path.join(
    PROJECT_ROOT, "logs", "sentinel_crash.log"
)


def _write_crash_log(exc: BaseException) -> str:
    """把一次崩溃写进日志（v3.3.0）。

    ⚠️ v3.1.0 时这里叫 ``_write_daemon_crash_log``，
    记的是「守护模式第几次重启」。
    v3.3.0 删掉了守护模式 —— 崩了就是崩了，
    记一份日志给用户看，哨兵**自己退出**，不复活。

    :param exc: 抛出来的异常。
    :return: 日志文件路径；写不进去就是空串。
    """
    import traceback

    try:
        os.makedirs(os.path.dirname(CRASH_LOG), exist_ok=True)
        with open(CRASH_LOG, "a", encoding="utf-8") as handle:
            handle.write(
                "===== 崩溃 · {} =====\n".format(
                    time.strftime("%Y-%m-%d %H:%M:%S"),
                )
            )
            handle.write("".join(
                traceback.format_exception(type(exc), exc, exc.__traceback__)
            ))
            handle.write("\n")
        return CRASH_LOG
    except OSError:
        return ""


def _watch_process_liveness(
    registry: Any,
    stop: threading.Event,
    interval: float = PROCESS_LIVENESS_INTERVAL,
) -> None:
    """每隔几秒看一眼：绑定的进程**还活着吗**（v3.4.0）。

    用户实测的原话：「进程已死，但因日志文件还在，状态依然显示运行中。」

    哨兵判断「会话还在跑」本来只看**日志文件还在被写**，
    而 Agent 跑完之后那个文件仍然躺在磁盘上 ——
    于是状态永远停在「运行中」，右侧还在**一遍遍重放**同样的旧行。

    这里独立地看**进程**：反查到了 PID、而那个 PID 没了，
    就把会话标成「已退出（静默）」并**停掉**对那份日志的跟随。

    ⚠️ 只有**确实绑定**了进程的会话才判 ——
    查不到 PID 的会话**永远**不会被误判成「已退出」
    （那正是 v3.3.1 刚放宽过的宽容度，不能又收回去）。

    ⚠️ psutil 缺失 → 一律判「不知道」→ 什么都不改（红线 R5）。

    :param registry: 会话登记表。
    :param stop: 停止信号。
    :param interval: 巡检间隔（秒）。
    """
    while not stop.wait(interval):
        try:
            sessions = registry.list()
        except Exception as exc:  # noqa: BLE001 —— 巡检失败只跳过这一轮
            logger.warning(
                "进程存活巡检失败：%s: %s", type(exc).__name__, exc
            )
            continue
        for session in sessions:
            try:
                alive = session.process_alive()
            except Exception as exc:  # noqa: BLE001 —— 单个会话失败不影响其他
                logger.warning(
                    "检查会话 %s 的进程失败：%s: %s",
                    session.id,
                    type(exc).__name__,
                    exc,
                )
                continue
            if alive is not False:
                # ``None`` = 不知道（没绑定 / 查不了）——
                # 「不知道」**不等于**「死了」，不能改状态。
                continue
            logger.info(
                "会话 %s 的进程已退出 —— 标记为「已退出（静默）」并停止跟随。",
                session.id,
            )
            try:
                session.mark_exited()
            except Exception as exc:  # noqa: BLE001 —— 标记失败只记日志
                logger.warning(
                    "标记会话 %s 退出失败：%s: %s",
                    session.id,
                    type(exc).__name__,
                    exc,
                )


def _force_exit(code: int = 0) -> None:
    """**确定地**结束进程（v3.4.0）。

    为什么需要它
    ============

    用户实测的原话是「关掉黑框后后台变成无法杀死的幽灵」。
    正常的 ``return`` 会等所有**非守护线程**结束 ——
    而托盘、尾巴线程都可能还在那儿，于是进程就赖着不走。

    所以「彻底退出」这条路在收尾做完之后**直接走 ``os._exit``**：
    缓冲先刷出去，然后立刻消失，不给任何线程留机会。

    ⚠️ ``os._exit`` **不跑** ``atexit``、**不跑** ``finally`` ——
    所以它必须放在**所有收尾都做完之后**调用。

    ⚠️ v3.8.0：托盘图标必须**在这里**收掉 ——
    ``finally`` 里那一次收尾在 ``os._exit`` 路径上**根本不会跑**，
    托盘图标就会留在系统里（点不动的那种残留）。
    所以这里**先收图标，再刷缓冲，最后才退**。

    :param code: 退出码。
    """
    # ⚠️ 顺序很重要：缓冲 → 退出。
    # （v5.0.0：托盘已整体剥离，不再有图标要收。）
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except (AttributeError, ValueError, OSError) as exc:
            # 刷不出去也要继续退 —— 宁可少一句日志，
            # 也不能因为「日志没刷出去」而把进程留着。
            logger.debug("刷新输出流失败：%s: %s", type(exc).__name__, exc)
    os._exit(code)
