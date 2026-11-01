"""哨兵 Sentinel —— 本地只读监控面板（FastAPI + Jinja2 + SSE）。

技术栈刻意选得很轻：FastAPI + Jinja2 + Server-Sent Events，纯 Python，
没有 Node.js 构建链、没有打包器、没有 npm。哨兵是「一个文件跑起来」的
本地工具，GUI 不应该给部署增加任何额外负担。

边界（架构红线）
================
* **只读**：本模块只以 ``"rb"`` 打开被监控日志，绝不写入它；
* **只在本机**：默认绑定 ``127.0.0.1``，非回环地址需要显式
  ``--allow-remote``；跨站请求（Origin 不在白名单）一律 403；
* **与引擎解耦**：FastAPI 应用层**不 import** ``src.core.engine``。
  扫描能力由 ``scan_callback`` 注入 —— 回调跑完引擎、把告警写进告警
  日志，本模块的 tailer 再把新告警推给浏览器。

数据流
======

.. code-block:: text

    告警日志 ──tail──▶ AlertStore ──▶ SSE ──▶ 浏览器
    被监控日志 ─tail─▶ 最近 N 行 ──┘

两个 tailer 都是「只读新增字节」的增量跟随器，能识别**同名新文件**
（轮转）与**原地截断**。
"""

from __future__ import annotations

import argparse
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
from contextlib import asynccontextmanager
from typing import (
    Any,
    AsyncIterator,
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

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape
from starlette.concurrency import run_in_threadpool

from .. import __version__
from ..adapters.fingerprint import fingerprint_label, fingerprint_stream_key
from ..attachments import AttachmentStore, default_attached_path
from ..console import (
    announce_demo_keep_open,
    announce_radar_keep_open,
    announce_ready_when_up,
    port_is_free,
    print_banner,
    print_demo_notice,
    print_keep_open_notice,
    print_port_busy,
    print_radar_keep_open_notice,
)
from ..selfmon import ProcessMonitor, format_uptime
from ..tailer import FileTailer as _FileTailer
from .shutdown import ShutdownCoordinator, install_console_close_handler
from .tray import build_tray_icon, start_tray, stop_tray, tray_available

__all__ = [
    "AlertStore",
    "EventHub",
    "FileTailer",
    "Watcher",
    "create_app",
    "format_sse",
    "main",
    "normalize_alert",
    "parse_alert_line",
    "stream_events",
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


def format_sse(event: Dict[str, Any]) -> str:
    """把事件字典格式化成 SSE 帧。

    :param event: 事件字典。
    :return: ``"data: {json}\\n\\n"`` 形式的文本帧。
    """
    return "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"


def _build_resume_prompt(alert: Dict[str, Any]) -> str:
    """给「回滚并重试」拼一份**能直接开工**的纠偏提示。

    和【复制纠偏提示】用的是同一套底料（:func:`src.core.correction.build_prompt`），
    但开头多了一段「上一次为什么被拦」的交代 ——
    重拉起来的 Agent 是**全新的**，它不知道上一轮发生过什么，
    所以提示里必须把上下文说全。

    :param alert: 告警字典。
    :return: 提示正文。
    """
    verdict = str(alert.get("verdict") or "")
    reason = str(alert.get("reason") or "")
    command = str(alert.get("command") or "")
    head = [
        "【哨兵纠偏提示 · {}】".format(verdict or "拦截"),
        "",
        "你刚才的任务被哨兵**拦下并挂起**了，原因如下：",
        "  拦截原因：{}".format(reason or "（未提供）"),
    ]
    if command:
        head.append("  被拦的命令：{}".format(command))
    head.extend(
        [
            "",
            "工作目录已经**回滚到拦截前的状态**（你之前的改动已被撤销）。",
            "请按下面的建议改用安全的方式继续任务，"
            "**不要再执行上面那条被拦的命令**。",
            "",
            "────────────────────────────",
        ]
    )
    body = ""
    try:
        # 惰性导入 —— 看板层不静态依赖 core 包。
        from ..core.correction import build_prompt

        body = str(build_prompt(alert) or "")
    except Exception:  # noqa: BLE001 —— 取不到正文也不该挡住复活
        body = ""
    return "\n".join(head) + "\n" + body


async def stream_events(
    request: Request,
    queue: "asyncio.Queue[Dict[str, Any]]",
    hub: EventHub,
    store: AlertStore,
) -> AsyncIterator[str]:
    """SSE 事件流的生成器。

    先推一帧状态快照，随后不断把队列里的事件推出去；15 秒没有新事件
    时发一个注释帧保活（避免代理把连接掐掉）。

    :param request: 当前请求（用于判断客户端是否断开）。
    :param queue: 本订阅者的事件队列。
    :param hub: 事件分发中心（断开时用来注销）。
    :param store: 状态存储（用来生成首帧状态）。
    """
    try:
        yield format_sse({"type": "status", **store.status()})
        while True:
            if await request.is_disconnected():
                break
            try:
                event = await asyncio.wait_for(queue.get(), timeout=15.0)
            except asyncio.TimeoutError:
                yield ": keep-alive\n\n"
                continue
            yield format_sse(event)
    finally:
        hub.unsubscribe(queue)


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


def create_app(
    watch_path: Optional[str] = None,
    alert_log_path: Optional[str] = None,
    *,
    sessions: Optional[Sequence[SessionSpec]] = None,
    scan_callback: Optional[Callable[[str], Any]] = None,
    guardian_callback: Optional[Any] = None,
    task_goal: Optional["TaskGoal"] = None,
    watch_target: Optional["WatchTarget"] = None,
    briefing_callback: Optional[Callable[[], Dict[str, Any]]] = None,
    inject_callback: Optional[Callable[..., Dict[str, Any]]] = None,
    rollback_callback: Optional[Callable[..., Dict[str, Any]]] = None,
    resume_callback: Optional[Callable[..., Dict[str, Any]]] = None,
    plan_callback: Optional[Callable[..., Dict[str, Any]]] = None,
    safe_point_service: Optional[Any] = None,
    workdir: str = "",
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    allow_remote: bool = False,
    poll_interval: float = 0.5,
    recent_line_limit: int = 200,
    max_alerts: int = 500,
    start_watcher: bool = True,
    demo_mode: bool = False,
    radar_mode: bool = False,
    attach_factory: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
    config_path: Optional[str] = None,
    reload_callback: Optional[Callable[[], Dict[str, Any]]] = None,
    attach_store: Optional[Any] = None,
) -> FastAPI:
    """构造监控面板的 FastAPI 应用。

    应用层**不认识**哨兵引擎：扫描能力由 ``scan_callback`` 注入。
    回调跑完引擎、把告警写进告警日志，随后的 tailer 会把新告警推给
    浏览器 —— GUI 与引擎之间只有「一个告警日志文件」这一条耦合。

    守护者能力同理由 ``guardian_callback`` 注入 —— 它是一个**鸭子类型**
    对象，只需实现 ``status()`` / ``intervene(payload)`` /
    ``override(payload)`` 三个方法。这样 GUI 层依然不需要 import
    ``src.core.guardian``，测试时也可以塞个假对象进去。

    **多路会话（v1.0.0）**：传 ``sessions`` 可以同时盯多个 Agent；
    不传就退化成 v0.x 的单会话模式，接口与前端行为完全不变。

    :param watch_path: 被监控的 Agent 日志路径（只读）。单会话模式必填。
    :param alert_log_path: 哨兵告警日志路径；``None`` 时用默认位置。
    :param sessions: 多会话描述列表；给了就用它，不再看 ``watch_path``。
    :param scan_callback: 「立即扫描」回调，签名 ``(watch_path) -> Any``；
        返回值若是带 ``counts`` 的字典，会被记进 ``scan_counts``。
    :param guardian_callback: 守护者（鸭子类型），提供 ``status`` /
        ``intervene`` / ``override``；``None`` 时干预按钮禁用。
    :param task_goal: 任务目标（意图锚点）；为 ``None`` 时自建一个空的。
    :param watch_target: 「当前监控目标」快照（Agent 类型 / 目标 / 锚定来源）；
        为 ``None`` 时自建一个空的。
    :param briefing_callback: 「生成晨报」回调（v1.2.0），无参调用，
        返回 ``{"ok": True, "markdown": …, "terminal": …, "path": …}``；
        为 ``None`` 时该功能不可用（前端按钮会给出人话提示）。
    :param host: 监听地址；非回环地址必须同时传 ``allow_remote``。
    :param port: 监听端口。
    :param allow_remote: 是否允许绑定非回环地址。
    :param poll_interval: tailer 轮询间隔（秒）。
    :param recent_line_limit: 保留多少行被监控日志。
    :param max_alerts: 保留多少条告警。
    :param start_watcher: 是否在应用启动时拉起后台跟随线程。
    :param demo_mode: 是否处于**演示模式**（v2.2.2）。
        为 ``True`` 时看板会挂上红色横幅、给会话名加「[演示]」前缀 ——
        演示里的一切都是模拟数据，界面必须**说破**，
        否则用户会误以为哨兵在监控自己正在跑的 Agent。
    :param radar_mode: 是否处于**雷达模式**（v3.0.2）。
        与演示模式相反：这里**没有**模拟数据，雷达扫的是用户**真实**的
        Agent 会话日志。看板会自动打开【发现/挂载进程】面板，
        让用户第一眼就看到「我正在跑的那些任务」。
    :param attach_factory: 「挂载一个外部日志」的工厂（v2.3.0）。
        签名 ``(info) -> dict``，``info`` 含 ``path`` / ``agent`` / ``cwd``；
        返回 ``{"ok": True, "alert_log_path": …, "scan_callback": …,
        "workdir": …}``。GUI 层只认这个字典，**不认识**引擎 ——
        与 ``scan_callback`` 同一套注入哲学。
    :param config_path: 配置文件路径（v2.3.0）——
        独立设置页要读它、改它、再触发热重载。
    :param reload_callback: 「立刻热重载规则」的回调（v2.3.0）。
        签名 ``() -> dict``；为 ``None`` 时设置页只保存不热重载。
    :param attach_store: 挂载会话的**持久化仓库**（v3.1.0）。
        是一个鸭子类型对象，要有 ``load()`` / ``save_entry(entry)`` /
        ``remove(path)`` / ``update(path, **changes)`` 四个方法；
        为 ``None`` 时挂载**不持久化**（挂完就忘，重启要重新点）。
    :return: FastAPI 应用实例。
    :raises ValueError: 监听非回环地址却没有显式 ``allow_remote``。
    """
    if not allow_remote and not _is_loopback_host(host):
        raise ValueError(
            f"拒绝监听非回环地址 {host}（确需远程访问请显式传 "
            "allow_remote=True / --allow-remote）"
        )

    origins = _allowed_origins(host, port, allow_remote)
    hub = EventHub()

    # ---- 组装会话 ----------------------------------------------------
    specs: List[SessionSpec] = list(sessions or ())
    if demo_mode:
        # 演示模式：会话名**统一**加「[演示]」前缀（v2.2.2）。
        specs = [_with_demo_label(spec) for spec in specs]
    if not specs:
        if not watch_path:
            raise ValueError("要么给 watch_path，要么给 sessions。")
        # v2.3.0：认出被监控的是**谁**、在**哪儿**干活。
        #
        # 用户指着某个日志说「盯这个」，哨兵就该自己看出那是 Codex
        # 还是 Claude、跑的是哪个项目 —— 而不是让他先回答一串问题。
        detected_agent = ""
        detected_cwd = ""
        if not demo_mode:
            try:
                from ..radar import describe_session

                described = describe_session(watch_path)
                if described is not None:
                    detected_agent = described.agent
                    detected_cwd = described.cwd
            except Exception as exc:  # noqa: BLE001 —— 识别失败只降级
                logger.warning("识别被监控日志失败：%s", exc)
        if demo_mode:
            label = DEMO_DEFAULT_SESSION_LABEL
        elif radar_mode:
            # 雷达模式：还没接入任何 Agent —— 名字要说实话，
            # 别让用户以为「默认会话」就是他的 Agent。
            label = RADAR_SESSION_LABEL
        elif detected_agent and detected_agent != "unknown":
            from ..radar import format_label

            label = format_label(detected_agent, detected_cwd)
        else:
            label = "默认会话"

        target = watch_target
        if target is None:
            target = WatchTarget(
                agent=detected_agent, workspace=detected_cwd
            )
        else:
            snapshot = target.snapshot()
            if detected_agent and not snapshot.get("agent"):
                target.set_agent(detected_agent)
            if detected_cwd and not snapshot.get("workspace"):
                target.set_workspace(detected_cwd)
        agent_name = target.snapshot().get("agent") or ""
        specs = [
            SessionSpec(
                id="default",
                label=label,
                agent=agent_name,
                watch_path=watch_path,
                alert_log_path=alert_log_path or DEFAULT_ALERT_LOG,
                guardian_callback=guardian_callback,
                scan_callback=scan_callback,
                watch_target=target,
                task_goal=task_goal,
                briefing_callback=briefing_callback,
                # 控制中枢（v2.0.0）
                inject_callback=inject_callback,
                rollback_callback=rollback_callback,
                resume_callback=resume_callback,
                plan_callback=plan_callback,
                safe_point_service=safe_point_service,
                workdir=workdir or detected_cwd,
                # v3.4.0：雷达模式下的那个**占位**会话 ——
                # 它不是「挂载进来的会话」，所以**不给**卸载按钮。
                placeholder=bool(radar_mode and not demo_mode),
            )
        ]

    registry = SessionRegistry()
    for spec in specs:
        registry.add(
            Session(
                spec,
                hub,
                poll_interval=poll_interval,
                recent_line_limit=recent_line_limit,
                max_alerts=max_alerts,
            )
        )

    primary = registry.primary()
    if primary is None:  # pragma: no cover —— specs 为空时上面的守卫已拦下
        raise ValueError("至少需要一个会话。")
    # 老接口继续指向主会话 —— 单会话时行为与 v0.x 完全一致。
    store = primary.store
    goal_holder = primary.task_goal
    target_holder = primary.watch_target
    watch_abs = primary.watch_path
    alert_abs = primary.alert_log_path

    templates = Environment(
        loader=FileSystemLoader(TEMPLATES_DIR),
        autoescape=select_autoescape(["html"]),
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        """应用生命周期：绑定事件循环并拉起/收起 tailer。"""
        hub.attach_loop(asyncio.get_running_loop())
        # v3.1.0：把持久化的挂载**接回来** ——
        #
        # 哨兵重启（崩溃 / 更新 / 机器重启）之后，
        # 用户之前挂上的会话不该凭空消失。
        #
        # ⚠️ 恢复放在 ``start_all()`` **之前** —— 恢复出来的会话
        #    跟着 ``start_all()`` 一起启动，不会漏掉。
        # ⚠️ 恢复失败只是少接一个会话，**绝不让哨兵起不来**（红线 R5）。
        try:
            await run_in_threadpool(_restore_attachments)
        except Exception as exc:  # noqa: BLE001 —— 恢复失败只降级
            logger.warning(
                "恢复挂载会话失败：%s: %s", type(exc).__name__, exc
            )
        if start_watcher:
            registry.start_all()
        # v3.4.0：起「进程存活巡检」线程 ——
        #
        # Agent 跑完之后，日志文件还躺在磁盘上，
        # 于是看板一直显示「运行中」、右侧一直重放旧行。
        # 这条线程每 10 秒看一眼**进程**还在不在。
        liveness_stop = threading.Event()
        liveness_thread = threading.Thread(
            target=_watch_process_liveness,
            args=(registry, liveness_stop),
            name="sentinel-liveness",
            daemon=True,
        )
        liveness_thread.start()
        # v4.0.0：后台预热「外部插件派发进程树」扫描 ——
        #
        # 第一次全量枚举进程属性在 Windows 上要 3~4 秒，
        # 放在启动期用一条后台线程付掉，
        # 用户点开雷达面板时就不用等。
        warm_thread = threading.Thread(
            target=_warm_cloud_process_scan,
            name="sentinel-proc-warm",
            daemon=True,
        )
        warm_thread.start()
        try:
            yield
        finally:
            liveness_stop.set()
            liveness_thread.join(timeout=2.0)
            warm_thread.join(timeout=1.0)
            registry.stop_all()

    app = FastAPI(
        title="哨兵 Sentinel 监控面板",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
    )
    app.state.store = store
    app.state.hub = hub
    app.state.watcher = primary.watcher
    app.state.sessions = registry
    # v3.2.0：看板服务的启动时刻 —— 【关于系统】要显示「运行时长」。
    app.state.started_at = time.time()
    # v3.2.0：进程级采样器（CPU / 内存 / 线程数）。
    # ⚠️ 采样器**必须复用同一个实例** ——
    # 每次都新建的话，CPU 百分比永远算不出来
    # （百分比要靠两次采样的差值）。
    app.state.monitor = ProcessMonitor()

    @app.middleware("http")
    async def origin_guard(
        request: Request,
        call_next: Callable[[Request], Any],
    ) -> Any:
        """跨站请求防护（CSRF / DNS rebinding）。

        规则：

        1. ``Host`` 头必须指向本机（远程监听时才放宽）；
        2. ``Origin`` 头存在时必须在白名单里，或者与 ``Host`` 同源；
        3. 没有 ``Origin`` 头（同源导航、curl 等）一律放行。
        """
        host_header = request.headers.get("host", "")
        if not _is_allowed_host(host_header, host, allow_remote):
            return JSONResponse(
                {"ok": False, "error": "Host 头不被允许"},
                status_code=403,
            )
        origin = request.headers.get("origin")
        if origin and not _is_allowed_origin(
            origin, host_header, origins, allow_remote
        ):
            logger.warning("拒绝跨站请求：Origin=%s", origin)
            return JSONResponse(
                {"ok": False, "error": f"来源不被允许：{origin}"},
                status_code=403,
            )
        return await call_next(request)

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        """返回监控面板页面。"""
        template = templates.get_template("dashboard.html")
        html = template.render(
            watch_path=watch_abs,
            alert_log_path=alert_abs,
            bind=f"{host}:{port}",
            origins=sorted(origins),
            sessions=registry.snapshot(),
            # 演示模式标志（v2.2.2）——
            # 模板据此挂上**红色横幅**与「[演示]」徽章。
            # 这一层刻意是「服务端渲染」而不是前端 JS 判断：
            # 横幅必须在**第一帧**就在，绝不能等 JS 跑完才出现。
            demo_mode=demo_mode,
            # 雷达模式标志（v3.0.2）——
            # 模板据此**自动打开**【发现/挂载进程】面板，
            # 让用户第一眼就看到「我正在跑的那些任务」。
            radar_mode=radar_mode,
        )
        return HTMLResponse(html)

    # ------------------------------------------------------------------
    # 退出演示（v3.0.1）
    # ------------------------------------------------------------------
    @app.post("/api/demo/exit")
    async def api_demo_exit() -> Dict[str, Any]:
        """**真的**把演示进程停下来，并告诉用户接下来怎么用。

        ⚠️ 为什么要有这个
        ================

        v3.0.0 的实测反馈里有一条很致命：
        「双击 exe 直接进了演示模式，导致我以为雷达扫不到真实的进程。」

        用户看到一屏**模拟数据**，却以为这是「雷达没扫到东西」——
        演示模式**成功地把用户带偏了**。
        光在横幅上写「这是模拟数据」是不够的：
        他要的是**一条能立刻走通的真路径**。

        所以这里做两件事：

        1. 返回**能直接复制粘贴**的命令行；
        2. 真的把 uvicorn 停下来 —— 终端窗口随之关闭，
           用户看到「窗口没了」就知道演示**结束了**，而不是「卡住了」。
        """
        if not demo_mode:
            return {
                "ok": False,
                "error": "当前不是演示模式 —— 没有需要退出的东西。",
            }

        server = _DEMO_SHUTDOWN.get("server")
        if server is None:
            # 没有句柄（比如测试里直接 create_app）——
            # 至少把**话**说清楚，别让页面干等着。
            return {
                "ok": True,
                "instructions": list(DEMO_EXIT_INSTRUCTIONS),
                "stopped": False,
                "message": "演示已结束（没有可关闭的看板进程）。",
            }

        def _stop_later() -> None:
            """稍等一下再停 —— 让响应先发出去。

            ⚠️ 立刻停的话，浏览器会先收到「连接被重置」而不是那句引导，
            用户看到的就是一个**报错页** —— 那比演示模式还糟糕。
            """
            import time as _time

            _time.sleep(1.2)
            server.should_exit = True

        threading.Thread(
            target=_stop_later, name="sentinel-demo-exit", daemon=True
        ).start()
        return {
            "ok": True,
            "instructions": list(DEMO_EXIT_INSTRUCTIONS),
            "stopped": True,
            "message": "演示已结束，终端窗口马上会关闭。",
        }

    # ------------------------------------------------------------------
    # 彻底退出（v3.4.0）
    # ------------------------------------------------------------------
    @app.post("/api/shutdown")
    async def api_shutdown() -> Dict[str, Any]:
        """让哨兵**彻底退出**（v3.4.0）。

        这是用户实测里最痛的一条：
        「找不到退出系统的按钮，关掉黑框后后台变成无法杀死的幽灵。」

        所以看板上必须有这个按钮 —— 网页不能自己关掉自己，
        但它**可以**让后台那个进程把自己收干净。

        收尾动作：

        1. uvicorn 停机（``should_exit = True``，不是强杀）；
        2. 结束哨兵**自己拉起**的子进程（雷达挂载进来的 Agent
           **不动** —— 那是用户自己的进程，红线 R6）；
        3. 收掉托盘图标。

        :return: ``{"ok": True, "message": …}``。
        """
        def _stop_later() -> None:
            """稍等一下再停 —— 让响应先发出去。

            ⚠️ 立刻停的话，浏览器会先收到「连接被重置」而不是那句
            「正在退出」，用户看到的就是一个**报错页**。
            """
            import time as _time

            _time.sleep(0.8)
            _SHUTDOWN.request("api")

        threading.Thread(
            target=_stop_later, name="sentinel-shutdown", daemon=True
        ).start()
        return {
            "ok": True,
            "stopped": True,
            "message": "哨兵正在退出……托盘图标会一起消失。",
        }

    # ------------------------------------------------------------------
    # 雷达挂载（v2.3.0）
    # ------------------------------------------------------------------
    def _attached_paths() -> Dict[str, str]:
        """已经在监控的日志路径（归一化后 → 会话 ID）。"""
        result: Dict[str, str] = {}
        for session in registry.list():
            if session.watch_path:
                result[os.path.normcase(session.watch_path)] = session.id
        return result

    def _monitored_agents() -> List[Dict[str, Any]]:
        """当前**正在被监控**的会话（用来判断某个进程有没有被接住）。

        :return: 每项含 ``path`` / ``agent`` / ``workspace`` / ``mtime`` /
            ``recent`` / ``under_scan_root``。
        """
        from ..radar import agent_roots

        rows: List[Dict[str, Any]] = []
        now = time.time()
        roots = [
            os.path.normcase(str(item["path"]))
            for item in agent_roots(
                scan_roots=_radar_scan_roots(config_path)
            )
        ]
        for session in registry.list():
            path = str(session.watch_path or "")
            mtime = 0.0
            if path and os.path.isfile(path):
                try:
                    mtime = os.path.getmtime(path)
                except OSError:
                    mtime = 0.0
            normalized = os.path.normcase(path) if path else ""
            under_root = any(
                normalized.startswith(root) for root in roots if root
            )
            rows.append({
                "path": path,
                "agent": str(session.agent or "").lower(),
                "workspace": str(session.workdir or ""),
                "mtime": mtime,
                "recent": bool(mtime and (now - mtime) <= AGENT_RECENT_SECONDS),
                "under_scan_root": under_root,
            })
        return rows

    def _agent_activity() -> Dict[str, Any]:
        """算一次「活跃 Agent 进程」状态（v3.3.2）。

        回答用户最常问的那句话：
        **「我明明在终端跑着 Agent，看板为什么抓不到？」**

        ⚠️ 判「有没有被监控」用三条信号，从**确定**到**推断**：

        1. **命令行带这个日志路径** —— 一定是它（重定向场景）；
        2. **工作目录对得上** —— 同一个项目，一定是它；
        3. **同类型 Agent 的日志刚在雷达目录里被写过** —— 推断。

        第 3 条是**推断**，所以拿不准时宁可提示用户 ——
        多问一句的代价，远小于「用户以为被监控了其实没有」。
        """
        from ..procscan import (
            psutil_available,
            scan_agent_processes,
        )

        capture = _capture_path()
        if not psutil_available():
            return {
                "ok": True,
                "available": False,
                "active": [],
                "active_count": 0,
                "unmonitored": [],
                "unmonitored_count": 0,
                "by_agent": {},
                "capture_path": capture,
                # v3.6.0：空态引导要知道这个文件**在不在** ——
                # 不在的话，给用户的命令就不能假装它已经就绪。
                "capture_exists": os.path.isfile(capture),
            }

        try:
            processes = scan_agent_processes(
                # v3.4.0：用户自定义的进程特征
                # （``radar.custom_process_features``）——
                # 内置认不出来的 Agent，靠它认。
                extra_features=_radar_custom_features(config_path),
            )
        except Exception as exc:  # noqa: BLE001 —— 探测失败只降级
            logger.warning("活跃 Agent 探测失败：%s: %s", type(exc).__name__, exc)
            processes = []

        monitored = _monitored_agents()
        unmonitored: List[Dict[str, Any]] = []
        for proc in processes:
            hit = _match_monitored(proc, monitored)
            if hit:
                continue
            unmonitored.append(proc.to_dict())

        by_agent: Dict[str, int] = {}
        for item in unmonitored:
            key = str(item.get("agent") or "unknown")
            by_agent[key] = by_agent.get(key, 0) + 1

        return {
            "ok": True,
            "available": True,
            "active": [item.to_dict() for item in processes],
            "active_count": len(processes),
            "unmonitored": unmonitored,
            "unmonitored_count": len(unmonitored),
            "by_agent": by_agent,
            "capture_path": capture,
            "capture_exists": os.path.isfile(capture),
        }

    @app.get("/api/process_activity")
    async def api_process_activity() -> Dict[str, Any]:
        """雷达的「活跃进程感知」：本机正在跑哪些 Agent，它们被监控了没有。

        雷达扫的是**文件**；这一路扫的是**进程**。
        文件找不到的时候进程往往还在 ——
        那正是用户「终端里跑着任务、看板却抓不到」的那一刻。

        :return: 活跃 Agent 进程列表与「哪些没被监控」。
        """
        with _activity_lock:
            now = time.time()
            if (
                _activity_cache["at"]
                and (now - _activity_cache["at"]) < AGENT_ACTIVITY_TTL
            ):
                return dict(_activity_cache["data"])
            data = _agent_activity()
            _activity_cache["at"] = now
            _activity_cache["data"] = data
        return dict(data)

    def _persist_attach(
        path: str,
        agent: str,
        cwd: str,
        armed: bool = False,
        session_id: str = "",
    ) -> None:
        """把一次挂载写进持久化列表（v3.1.0）。

        ⚠️ 存不下来**不能**让挂载失败 ——
        用户最关心的是「现在正在盯着」，不是「重启后还在不在」。
        存失败只记一条日志，挂载照常成功。

        :param path: 日志路径。
        :param agent: Agent 类型。
        :param cwd: 工作目录。
        :param armed: 是否已武装守护者。
        :param session_id: 会话 ID。
        """
        if attach_store is None:
            return
        try:
            attach_store.save_entry(
                {
                    "path": path,
                    "agent": agent,
                    "cwd": cwd,
                    "armed": bool(armed),
                    "session_id": session_id,
                    "attached_at": time.time(),
                }
            )
        except Exception as exc:  # noqa: BLE001 —— 持久化失败只降级
            logger.warning(
                "保存挂载历史失败：%s: %s", type(exc).__name__, exc
            )

    def _forget_attach(path: str) -> None:
        """把一个挂载从持久化列表里删掉（v3.1.0）。

        :param path: 日志路径。
        """
        if attach_store is None:
            return
        try:
            attach_store.remove(path)
        except Exception as exc:  # noqa: BLE001 —— 持久化失败只降级
            logger.warning(
                "删除挂载历史失败：%s: %s", type(exc).__name__, exc
            )

    def _attach_one(
        path: str,
        agent_hint: str = "",
        cwd_hint: str = "",
    ) -> Dict[str, Any]:
        """挂载**一个**外部日志，返回结果字典（v3.1.0 抽出来的）。

        雷达面板的「接入」按钮、启动时的**自动恢复**，
        走的是同一条路 —— 不然恢复出来的会话就会缺一堆设置。

        :param path: 日志路径（已绝对化）。
        :param agent_hint: 调用方指定的 Agent 类型；为空则自动识别。
        :param cwd_hint: 调用方指定的工作目录；为空则自动识别。
        :return: ``{"ok": bool, "session": …, "message": str, …}``。
        """
        if attach_factory is None:
            return {
                "ok": False,
                "error": "当前模式不支持挂载外部日志。",
                "status": 503,
            }

        # 识别它是谁、在哪儿干活（v2.3.0 任务二）。
        from ..radar import describe_session, format_label

        described = describe_session(path)
        agent = str(
            agent_hint
            or (described.agent if described is not None else "")
            or "unknown"
        )
        cwd = str(
            cwd_hint
            or (described.cwd if described is not None else "")
            or ""
        )
        # v3.0.1：工作区**标识** —— 真工作目录提不到时，
        # 退到「父目录名 + 文件名」（雷达那一侧算好的）。
        # 绝不再显示「未知工作区」。
        workspace = str(
            cwd_hint
            or (described.cwd if described is not None else "")
            or (described.workspace if described is not None else "")
            or ""
        )
        label = format_label(agent, cwd, fallback=workspace)

        session_id = "attach-{}".format(
            # sha1 只用来生成**会话 ID**，不涉及任何安全用途。
            hashlib.sha1(path.encode("utf-8")).hexdigest()[:12]  # noqa: S324
        )
        try:
            built = attach_factory(
                {
                    "path": path,
                    "agent": agent,
                    "cwd": cwd,
                    "workspace": workspace,
                    "id": session_id,
                }
            )
        except Exception as exc:  # noqa: BLE001 —— 挂载失败只降级
            logger.exception("挂载 %s 失败", path)
            return {
                "ok": False,
                "error": "挂载失败：{}".format(exc),
                "status": 500,
            }
        if not isinstance(built, dict) or not built.get("ok"):
            error = (
                built.get("error") if isinstance(built, dict) else "未知错误"
            )
            return {
                "ok": False,
                "error": str(error or "挂载失败"),
                "status": 500,
            }

        watch_target = WatchTarget(agent=agent, workspace=workspace)
        # v3.0.0：自动提取出来的目标要**立刻**出现在「当前监控目标」面板 ——
        # 用户得看得见「它自己认出了我的目标」，否则会以为还得自己填。
        task_goal = built.get("task_goal")
        goal_source = str(built.get("goal_source") or "none")
        if task_goal is not None and goal_source == "auto":
            try:
                watch_target.set_goal(task_goal.get(), "auto")
            except Exception as exc:  # noqa: BLE001 —— 面板同步失败不影响
                logger.warning(
                    "同步自动提取的目标失败：%s: %s", type(exc).__name__, exc
                )

        spec = SessionSpec(
            id=session_id,
            label=label,
            agent=agent,
            watch_path=path,
            alert_log_path=str(built.get("alert_log_path") or ""),
            scan_callback=built.get("scan_callback"),
            workdir=str(built.get("workdir") or cwd),
            tail_runner=built.get("tail_runner"),
            watch_target=watch_target,
            # v2.4.0：挂载会话的进程绑定、任务目标与武装开关。
            attached=True,
            process_info=built.get("process_info") or {},
            arm_callback=built.get("arm_callback"),
            task_goal=task_goal,
        )
        session = Session(
            spec,
            hub,
            poll_interval=poll_interval,
            recent_line_limit=recent_line_limit,
            max_alerts=max_alerts,
        )
        registry.add(session)
        if start_watcher:
            session.start()
        logger.info("已挂载会话：%s（%s）", label, path)
        # 如实告诉用户进程绑定结果（v2.4.0）——
        # 绑定上了才谈得上「武装守护者」，没绑上就说没绑上。
        if session.process_info.get("bindable"):
            message = "已挂载 {}（PID {}）".format(
                label, int(session.process_info.get("pid") or 0)
            )
        else:
            message = "已挂载 {}（未绑定进程，武装守护者不可用）".format(label)
        return {
            "ok": True,
            "session": session.snapshot(),
            "message": message,
            "agent": agent,
            "cwd": cwd,
            "session_id": session_id,
        }

    def _restore_attachments() -> List[Dict[str, Any]]:
        """把持久化的挂载**接回来**（v3.1.0）。

        流程：

        1. 读持久化列表；
        2. 清掉**失效**的（文件没了 / 静默超过 1 小时），
           每清一个打印一行 ``[SENTINEL] 清理失效的挂载历史: xxx``；
        3. 把还活着的重新接上（走的是和面板「接入」完全一样的路径）；
        4. 之前**武装**过的，恢复武装状态 —— 用户当初亲手开的开关，
           不该因为哨兵重启一次就没了。

        ⚠️ 全程**不抛异常**：恢复失败只是少接一个会话，
        绝不能让哨兵起不来（红线 R5）。

        :return: 恢复成功的挂载列表。
        """
        if attach_store is None:
            return []

        restored: List[Dict[str, Any]] = []
        try:
            kept, dropped = attach_store.prune()
        except Exception as exc:  # noqa: BLE001 —— 持久化坏了只降级
            logger.warning(
                "清理挂载历史失败：%s: %s", type(exc).__name__, exc
            )
            return []

        for entry in dropped:
            path = str(entry.get("path") or "")
            print("[SENTINEL] 清理失效的挂载历史: {}".format(path))
            logger.info("清理失效的挂载历史：%s", path)

        for entry in kept:
            path = str(entry.get("path") or "")
            if not os.path.isfile(path):
                continue
            result = _attach_one(
                path,
                str(entry.get("agent") or ""),
                str(entry.get("cwd") or ""),
            )
            if not result.get("ok"):
                logger.warning(
                    "恢复挂载失败（%s）：%s", path, result.get("error")
                )
                continue
            restored.append(result)

            # 恢复武装状态（v3.1.0）——
            #
            # ⚠️ 只在**进程确实绑上了**的时候才恢复 ——
            # 绑不上的话武装按钮本来就是禁用的，
            # 硬调只会得到一句「没有 PID」。
            if entry.get("armed"):
                session = registry.resolve(str(result.get("session_id") or ""))
                if session is not None and session.arm_callback is not None:
                    try:
                        armed_result = session.arm_callback(True)
                        if isinstance(armed_result, dict) and armed_result.get(
                            "ok"
                        ):
                            session.armed = bool(
                                armed_result.get("armed", True)
                            )
                            print(
                                "[SENTINEL] 已恢复武装状态：{}".format(
                                    session.label
                                )
                            )
                    except Exception as exc:  # noqa: BLE001 —— 武装失败只降级
                        logger.warning(
                            "恢复武装状态失败（%s）：%s: %s",
                            path,
                            type(exc).__name__,
                            exc,
                        )

        if restored:
            print(
                "[SENTINEL] 已恢复 {} 个挂载会话。".format(len(restored))
            )
        return restored

    @app.get("/api/discover")
    async def api_discover(active_only: int = 1) -> Dict[str, Any]:
        """雷达发现：列出**正在活跃写入**的 Agent 日志。

        哨兵的新定位是**雷达**，不是「必须从我这儿启动」——
        用户早就在自己的终端里跑着 Agent 了，
        哨兵应该能**发现**它们，而不是逼用户改习惯。

        扫描哪些目录由配置 ``radar.scan_roots`` 决定
        （默认 ``.minimax/v2/sessions`` / ``.dsh/sessions`` /
        ``.mcode`` / ``.claude`` / ``.codex``，顺序即优先级，
        ``.codex`` 排最后 —— 里面全是历史死文件）。
        用户也可以填 ``D:\\项目`` 这种自定义路径。

        每条结果都带**工作区**、**会话 ID** 与**首条消息时间** ——
        让用户一眼认出「这是我刚才跑的那个任务」。

        ``active_only=1``（**默认**）只返回「正在写入的会话主日志」——
        这正是任务书要的「默认只显示活跃（正在写入）的会话」。
        传 ``active_only=0`` 可以拿到完整列表（界面上的
        「显示其余 N 个」就是靠它）。

        v4.0.0 追加 ``processes`` —— **外部插件派发进程树**的发现结果：
        正在跑的 Agent CLI 进程（谁派发的、命令行、工作目录、
        关联到的日志、能不能挂载）。

        :param active_only: ``1`` = 只返回正在写入的；``0`` = 全量。
        :return: 发现结果列表（每项带 agent / cwd / label / active）。
        """
        from ..radar import (
            agent_roots,
            build_prompt_index,
            scan_agent_sessions,
        )

        attached = _attached_paths()
        scan_roots = _radar_scan_roots(config_path)
        cwd_index: List[Any] = []
        prompt_index: Dict[str, str] = {}

        def _resolve_process_cwd(path: str, agent: str) -> str:
            """回退链第三步：反查写这个日志的进程的工作目录。

            ⚠️ 进程索引**只枚举一次**（lazily）——
            同一个请求里几十个候选文件共用一份，
            否则每查一个文件就把系统进程整个扫一遍。
            """
            from ..procscan import build_process_cwd_index, match_process_cwd

            if not cwd_index:
                cwd_index.extend(build_process_cwd_index())
            return match_process_cwd(cwd_index, path, agent)

        def _resolve_prompt_index() -> Dict[str, str]:
            """v3.5.2：扫一遍正在跑的 Agent CLI，建「工作目录 → Prompt」索引。

            ⚠️ 同样**只建一次**（lazily）——
            这是「日志里翻不到任务锚点」时的第二条自动提取路子。
            """
            nonlocal prompt_index
            if not prompt_index:
                try:
                    prompt_index = build_prompt_index()
                except Exception as exc:  # noqa: BLE001 —— 索引失败只降级
                    logger.warning(
                        "构建命令行 Prompt 索引失败：%s: %s",
                        type(exc).__name__,
                        exc,
                    )
                    prompt_index = {}
            return prompt_index

        try:
            # 已经被挂载的文件**跳过**（v2.4.0）——
            # 雷达扫描是纯 I/O 密集操作，同一份日志不必读两遍。
            found = scan_agent_sessions(
                exclude_paths=list(attached.keys()),
                scan_roots=scan_roots,
                process_cwd_resolver=_resolve_process_cwd,
                active_only=bool(active_only),
                # v3.4.0：排除关键词命中的路径**一律不显示** ——
                # 哨兵自己压测留下的残留日志不该出现在用户列表里。
                #
                # ⚠️ 传的是「**当前实际生效**」的那份清单：
                #    配置没写就用内置默认（sentinel / stress / test），
                #    配了空列表就真的什么都不排除。
                exclude_keywords=_effective_exclude_keywords(config_path),
                # v3.5.2：把**任务锚点**一起带出来 ——
                # 界面直接显示「自动找到的任务目标」，用户不用手敲。
                prompt_index=_resolve_prompt_index(),
            )
        except Exception as exc:  # noqa: BLE001 —— 扫描失败只降级
            logger.warning("雷达扫描失败：%s: %s", type(exc).__name__, exc)
            scan_state = _cloud_proc_scan_state()
            return {
                "ok": False,
                "error": "扫描失败：{}".format(exc),
                "sessions": [],
                "count": 0,
                "active_count": 0,
                "processes": [],
                "process_count": 0,
                "process_scan_status": scan_state["status"],
                "process_scan_partial": scan_state["partial"],
                "process_scan_elapsed_ms": scan_state["elapsed_ms"],
                "cwd": _current_workspace(),
                "roots": [],
            }

        items: List[Dict[str, Any]] = []
        active_count = 0
        for item in found:
            data = item.to_dict()
            data["attached"] = os.path.normcase(item.path) in attached
            if item.active and item.primary:
                active_count += 1
            items.append(data)

        # v4.0.0：外部插件派发**进程树** ——
        #
        # 雷达原来只扫**日志文件**；这一路扫的是**进程**：
        # 找出外部插件的桥接宿主 → 枚举它派发出来的子孙 →
        # 按命令行 / 工作目录认出是哪个 Agent CLI →
        # 关联工作目录下的日志文件。
        #
        # ⚠️ 放线程池里跑 —— 第一次全量枚举要 3~4 秒，
        #    放事件循环里会把整个看板卡住。
        # ⚠️ v4.0.1：``_scan_cloud_processes`` 内部**最多只等 1.5 秒**，
        #    超时就返回已经扫到的那部分（``process_scan_status`` 会是
        #    ``scanning``），剩下的交给后台线程 —— 绝不堵住主线程。
        scan_state = _cloud_proc_scan_state()
        try:
            raw_processes = await run_in_threadpool(_scan_cloud_processes)
        except Exception as exc:  # noqa: BLE001 —— 探测失败只降级
            logger.warning(
                "外部插件进程树扫描失败：%s: %s", type(exc).__name__, exc
            )
            raw_processes = []
        # ⚠️ 扫描状态要在**等完之后**再取一次 ——
        #    等的过程中后台可能刚好扫完了，状态会从 ``scanning``
        #    变成 ``ready``，那时候就不该再提示「正在深度检索」。
        scan_state = _cloud_proc_scan_state()
        processes: List[Dict[str, Any]] = []
        for row in raw_processes:
            item = dict(row)
            log_path = str(item.get("log_path") or "")
            is_attached = (
                bool(log_path) and os.path.normcase(log_path) in attached
            )
            item["attached"] = is_attached
            if is_attached:
                item["state"] = "attached"
            processes.append(item)

        return {
            "ok": True,
            "sessions": items,
            "count": len(items),
            "active_count": active_count,
            "processes": processes,
            "process_count": len(processes),
            # v4.0.1：进程树扫描的状态 ——
            # ``scanning`` 表示「这份结果**还在补**」，
            # 前端要显示「正在深度检索进程…」并轮询本接口。
            "process_scan_status": scan_state["status"],
            "process_scan_partial": scan_state["partial"],
            "process_scan_elapsed_ms": scan_state["elapsed_ms"],
            # 当前工作区 —— 任务书要求「明确列出当前的 cwd 工作区」。
            "cwd": _current_workspace(),
            "roots": [
                item["path"]
                for item in agent_roots(scan_roots=scan_roots)
            ],
        }

    @app.get("/api/path_exists")
    async def api_path_exists(path: str = "") -> Dict[str, Any]:
        """确认某个日志文件**在不在**（v3.6.0）。

        为什么需要它
        ============
        用户在雷达弹窗里手填一个路径点【接入】，路径写错时
        服务端会回 400「文件不存在：…」—— 但那是**挂了才知道**。
        更好的做法是前端**先问一句**：文件不在就直接飘红字，
        不必让用户对着一个 500 一样的报错猜半天。

        ⚠️ 这个端点**只读**，**不打开**也不**读取**文件内容 ——
        它只做一次 ``os.path`` 判断，**不碰**任何字节。

        :param path: 待确认的路径（查询参数）。
        :return: ``{"ok": True, "path": …, "exists": bool,
            "is_file": bool}``。
        """
        raw = str(path or "").strip()
        if not raw:
            return {
                "ok": True,
                "path": "",
                "exists": False,
                "is_file": False,
            }
        target = os.path.abspath(os.path.expanduser(raw))
        return {
            "ok": True,
            "path": target,
            "exists": os.path.exists(target),
            "is_file": os.path.isfile(target),
        }

    @app.post("/api/attach")
    async def api_attach(request: Request) -> Any:
        """把一个**已经在跑**的 Agent 日志挂进来实时监控。

        这是「旁路监听」方案：操作系统层面没法拦截已运行进程的 stdout，
        所以哨兵退一步 —— 直接盯着那个 Agent 正在写的日志文件。

        :param request: JSON 体 ``{"path": "…"}``（可带 ``agent`` / ``cwd``）。
        :return: 新会话的快照。
        """
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001 —— 坏 JSON 当空请求
            payload = {}
        if not isinstance(payload, dict):
            payload = {}

        raw_path = str(payload.get("path") or "").strip()
        if not raw_path:
            return JSONResponse(
                {"ok": False, "error": "请给出要挂载的日志文件路径。"},
                status_code=400,
            )
        path = os.path.abspath(os.path.expanduser(raw_path))
        if not os.path.isfile(path):
            return JSONResponse(
                {"ok": False, "error": "文件不存在：{}".format(path)},
                status_code=400,
            )

        # v3.0.2：压缩日志（DeepSeek 的 ``.zstd``）**不能接入**。
        # 跟读器记的是「原始字节偏移」，压缩流没法按字节增量读 ——
        # 硬接进去只会得到一堆乱码。与其给一个半成品的按钮，
        # 不如在这里说清楚。
        from ..radar import is_compressed_log

        if is_compressed_log(path):
            return JSONResponse(
                {
                    "ok": False,
                    "error": (
                        "这是压缩日志（zstd），只能被发现，"
                        "暂不支持接入实时审查。"
                    ),
                },
                status_code=400,
            )

        # 已经挂过了就别重复挂 —— 同一个文件挂两次会产生两份告警。
        attached = _attached_paths()
        key = os.path.normcase(path)
        if key in attached:
            existing = registry.resolve(attached[key])
            return {
                "ok": True,
                "session": existing.snapshot() if existing else {},
                "message": "这个日志已经在监控里了。",
                "duplicate": True,
            }

        # v3.1.0：挂载**持久化** ——
        #
        # 哨兵重启后自动接回来，用户不用重新点一遍。
        result = _attach_one(
            path,
            str(payload.get("agent") or ""),
            str(payload.get("cwd") or ""),
        )
        if not result.get("ok"):
            status = int(result.get("status") or 500)
            return JSONResponse(
                {"ok": False, "error": str(result.get("error") or "挂载失败")},
                status_code=status,
            )

        _persist_attach(
            path,
            str(result.get("agent") or ""),
            str(result.get("cwd") or ""),
            armed=False,
            session_id=str(result.get("session_id") or ""),
        )
        return {
            "ok": True,
            "session": result.get("session") or {},
            "message": str(result.get("message") or ""),
        }

    @app.delete("/api/sessions/{session_id}")
    async def api_detach(session_id: str) -> Any:
        """卸载一个会话（停止它的跟随线程与实时监听引擎）。"""
        session = registry.resolve(session_id)
        if session is None:
            return JSONResponse(
                {"ok": False, "error": "会话不存在：{}".format(session_id)},
                status_code=404,
            )
        watch_path = session.watch_path
        registry.remove(session_id)
        # v3.1.0：卸载了就不再持久化 ——
        # 用户主动卸载 = 「我不想盯这个了」，
        # 下次启动不该又把它接回来。
        if watch_path:
            _forget_attach(watch_path)
        return {"ok": True, "detached": session_id}

    # ------------------------------------------------------------------
    # 独立设置页（v2.3.0）
    # ------------------------------------------------------------------
    def _resolve_config_path() -> str:
        """要读写的是哪个配置文件。"""
        return _resolve_runtime_config_path(config_path)

    def _mask_secrets(value: Any) -> Any:
        """把密钥类的值**打码**再发给前端。

        看板是本地页面，但浏览器里出现明文 API Key 仍然不是好主意 ——
        更别说页面可能被截图发到群里。

        :param value: 任意配置值。
        :return: 打码后的值（密钥类字段变成 ``SECRET_MASK``）。
        """
        if isinstance(value, dict):
            return {
                key: (
                    SECRET_MASK
                    if str(key).lower() in SECRET_FIELD_NAMES
                    else _mask_secrets(item)
                )
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [_mask_secrets(item) for item in value]
        return value

    def _lookup_channel_secret(url: str) -> str:
        """按地址去配置里找这个通道的**真**密钥。

        设置页回传的是打码值 ``********`` —— 测试时要用真密钥去签名，
        绝不能把 ``********`` 当签名密钥发出去。

        :param url: 通道地址。
        :return: 真密钥；找不到时返回空串。
        """
        from ..config_loader import load_config

        target = _resolve_config_path()
        raw = load_config(target) if target else {}
        channels = (raw.get("alert") or {}).get("channels") or []
        for entry in channels:
            if not isinstance(entry, dict):
                continue
            if str(entry.get("url") or "").strip() != url:
                continue
            return str(entry.get("secret") or "")
        return ""

    def _render_settings(
        template_name: str, active_key: str
    ) -> HTMLResponse:
        """渲染一个设置页（MPA —— 每个功能一个独立页面）。

        :param template_name: 模板名。
        :param active_key: 左侧导航里要高亮的那一项。
        :return: 页面 HTML。
        """
        template = templates.get_template(template_name)
        html = template.render(
            bind=f"{host}:{port}",
            config_path=_resolve_config_path(),
            demo_mode=demo_mode,
            radar_mode=radar_mode,
            active_key=active_key,
            radar_workdir=PROJECT_ROOT,
            radar_capture_path=_ensure_capture_dir(),
            radar_roots=[
                item["path"]
                for item in _radar_roots_for_display(config_path)
            ],
        )
        return HTMLResponse(html)

    @app.get("/settings", response_class=HTMLResponse)
    async def settings_page() -> HTMLResponse:
        """设置**主页**：一张干净的菜单列表。

        v2.3.0 之前所有功能都堆在一个页面上；
        v3.0.1 有了侧边栏，但还是一个页面里切来切去 —— URL 永远不变。

        v3.0.2 改成真正的**多页面应用**：这里只负责「选一个功能」，
        选完就跳到那个功能**自己的页面**。
        """
        return _render_settings("settings.html", "")

    @app.get("/settings/llm", response_class=HTMLResponse)
    async def settings_llm_page() -> HTMLResponse:
        """设置子页面：模型路由（L2）。"""
        return _render_settings("settings_llm.html", "llm")

    @app.get("/settings/webhook", response_class=HTMLResponse)
    async def settings_webhook_page() -> HTMLResponse:
        """设置子页面：通知通道。"""
        return _render_settings("settings_webhook.html", "channels")

    @app.get("/settings/rules", response_class=HTMLResponse)
    async def settings_rules_page() -> HTMLResponse:
        """设置子页面：L1 规则。"""
        return _render_settings("settings_rules.html", "rules")

    @app.get("/settings/pipeline", response_class=HTMLResponse)
    async def settings_pipeline_page() -> HTMLResponse:
        """设置子页面：审查流水线控制 + 呈现与摘要（v3.7.0）。

        刻意**独立成页**而不是塞进 ``/settings/llm`` ——
        流水线开关是「要不要花钱、要不要占显存」的决定，
        跟模型路由不是一回事，混在一起会让人以为它们绑在一起。
        """
        return _render_settings("settings_pipeline.html", "pipeline")

    @app.get("/settings/sessions", response_class=HTMLResponse)
    async def settings_sessions_page() -> HTMLResponse:
        """设置子页面：挂载管理。"""
        return _render_settings("settings_sessions.html", "sessions")

    @app.get("/settings/system", response_class=HTMLResponse)
    async def settings_system_page() -> HTMLResponse:
        """设置子页面：关于系统（v3.2.0）。

        展示哨兵自身的**运行时长、内存占用、版本号** ——
        「监控工具本身不能变成资源消耗大户」，
        所以用户得能**看见**它吃了多少。
        """
        return _render_settings("settings_system.html", "system")

    @app.get("/api/system")
    async def api_system() -> Dict[str, Any]:
        """哨兵自身的运行状况（v3.2.0）。

        给设置页的【关于系统】区块用。

        :return: ``{"ok": True, "version": …, "process": {…}, …}``。
        """
        monitor = getattr(app.state, "monitor", None)
        process: Dict[str, Any] = {}
        if monitor is not None:
            try:
                process = monitor.snapshot()
            except Exception as exc:  # noqa: BLE001 —— 自检失败只降级
                logger.warning(
                    "哨兵自检失败：%s: %s", type(exc).__name__, exc
                )
                process = {}
        started_at = float(getattr(app.state, "started_at", 0.0) or 0.0)
        server_uptime = max(time.time() - started_at, 0.0) if started_at else 0.0
        return {
            "ok": True,
            "version": __version__,
            "process": process,
            "server_uptime_seconds": int(server_uptime),
            "server_uptime_text": format_uptime(server_uptime),
            "sessions": len(registry.list()),
            "config_path": _resolve_config_path(),
            "bind": f"{host}:{port}",
            "demo_mode": bool(demo_mode),
            "radar_mode": bool(radar_mode),
        }

    @app.get("/api/config")
    async def api_config_get() -> Dict[str, Any]:
        """读出当前配置（密钥已打码），给设置页渲染用。"""
        from ..config_loader import load_config

        target = _resolve_config_path()
        raw = load_config(target) if target else {}
        config = _mask_secrets(raw)
        return {
            "ok": True,
            "path": target,
            "exists": bool(target) and os.path.isfile(target),
            "config": config,
            "llm": config.get("llm") or {},
            "routing": (config.get("llm") or {}).get("routing") or {},
            # v3.5.0：级联审核（L2 本地过滤 / L3 云端终审）。
            # 单独拎出来 —— 设置页要把这两层**分成两个区**显示。
            "cascade": (config.get("llm") or {}).get("cascade") or {},
            "memory": config.get("memory") or {},
            "channels": (config.get("alert") or {}).get("channels") or [],
            "rules": config.get("rules") or {},
            # v3.7.0：呈现与摘要（两个极限压缩档位）。
            "presentation": config.get("presentation") or {},
            # 雷达：扫描根目录 + 实际展开后的目录清单（v3.0.1）。
            "radar": config.get("radar") or {},
            # v3.4.0：排除关键词与自定义进程特征 ——
            # 这里给的是**当前实际生效**的值（配置没写就给内置默认）。
            "radar_exclude_keywords": _effective_exclude_keywords(
                config_path
            ),
            "radar_custom_features": _radar_custom_features(config_path),
            "radar_roots": [
                item["path"]
                for item in _radar_roots_for_display(config_path)
            ],
            # 挂载管理：当前正在监控的文件路径列表。
            "sessions": [
                {
                    "id": item.get("id"),
                    "label": item.get("label"),
                    "agent": item.get("agent"),
                    "workspace": item.get("workspace"),
                    "watch_path": item.get("watch_path"),
                    "alert_log_path": item.get("alert_log_path"),
                    "state": item.get("state"),
                }
                for item in registry.snapshot()
            ],
        }

    @app.post("/api/webhook/test")
    async def api_webhook_test(request: Request) -> Dict[str, Any]:
        """真的发一条测试消息，验证这个 Webhook 能不能收到。

        ⚠️ 这是**真发** —— 用户点【测试】就是想确认「手机真的能收到」，
        不是想看一个「看起来成功」的假结果。

        :param request: JSON 体 ``{"url": …, "format": …, "secret": …}``。
        :return: 投递结果。
        """
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001 —— 坏 JSON 当空请求
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        url = str(payload.get("url") or "").strip()
        if not url:
            return JSONResponse(
                {"ok": False, "error": "请先填 Webhook 地址。"},
                status_code=400,
            )

        secret = str(payload.get("secret") or "")
        if secret == SECRET_MASK:
            # 用户没改密钥 —— 拿配置里的**真**密钥去签，
            # 绝不能拿 "********" 当签名密钥发出去。
            secret = _lookup_channel_secret(url)

        channel = {
            "type": "webhook",
            "enabled": True,
            "url": url,
            "format": str(payload.get("format") or "generic"),
            "secret": secret,
            "timeout": float(payload.get("timeout") or 10),
        }
        from ..core.webhook_probe import probe_webhooks

        try:
            results = probe_webhooks(
                {"alert": {"channels": [channel]}}, __version__
            )
        except Exception as exc:  # noqa: BLE001 —— 自检失败只降级
            return {
                "ok": False,
                "error": "测试失败：{}".format(exc),
                "results": [],
            }
        if not results:
            return {"ok": False, "error": "没能发出测试消息。", "results": []}
        first = results[0]
        return {
            "ok": bool(first.get("ok")),
            "detail": str(first.get("detail") or ""),
            "results": results,
        }

    @app.post("/api/config")
    async def api_config_set(request: Request) -> Any:
        """把设置页的改动**写回**配置文件，并触发热重载。

        ⚠️ 写回是**外科手术式的**（只改目标键的值，注释全部保留）——
        ``config.yaml`` 里有大段中文注释，
        整体重写会把它们全部抹掉。

        :param request: JSON 体（结构见 ``settings.js``）。
        :return: 应用了哪些、跳过了哪些、有没有触发热重载。
        """
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001 —— 坏 JSON 当空请求
            payload = {}
        if not isinstance(payload, dict):
            payload = {}

        target = _resolve_config_path()
        if not target:
            return JSONResponse(
                {"ok": False, "error": "找不到配置文件路径。"},
                status_code=400,
            )

        updates = _collect_config_updates(payload)
        if not updates:
            return {
                "ok": True,
                "applied": [],
                "skipped": [],
                "changed": False,
                "message": "没有要保存的改动。",
                "reloaded": False,
            }

        from ..config_loader import save_config

        try:
            result = save_config(
                target,
                updates,
                defaults=V370_CONFIG_DEFAULTS,
            )
        except Exception as exc:  # noqa: BLE001 —— 写配置失败只降级
            logger.exception("保存配置失败")
            return JSONResponse(
                {"ok": False, "error": "保存失败：{}".format(exc)},
                status_code=500,
            )
        if not result.get("ok"):
            return JSONResponse(
                {
                    "ok": False,
                    "error": result.get("error") or "保存失败",
                    "applied": result.get("applied") or [],
                    "skipped": result.get("skipped") or [],
                },
                status_code=500,
            )

        reloaded = False
        reload_note = ""
        if reload_callback is not None:
            try:
                outcome = reload_callback()
                reloaded = bool(outcome.get("ok")) if isinstance(
                    outcome, dict
                ) else bool(outcome)
                reload_note = (
                    str(outcome.get("note") or "") if isinstance(outcome, dict)
                    else ""
                )
            except Exception as exc:  # noqa: BLE001 —— 热重载失败只降级
                logger.warning("热重载失败：%s: %s", type(exc).__name__, exc)
                reload_note = "热重载失败：{}".format(exc)

        applied = result.get("applied") or []
        skipped = result.get("skipped") or []
        return {
            "ok": True,
            "path": target,
            "applied": applied,
            "skipped": skipped,
            "backup": result.get("backup") or "",
            "changed": bool(result.get("changed")),
            "reloaded": reloaded,
            "message": "已保存 {} 项{}。".format(
                len(applied),
                "，{} 项没找到（未修改）".format(len(skipped)) if skipped else "",
            ),
            "reload_note": reload_note,
        }

    # ------------------------------------------------------------------
    # L1 规则清单（v3.7.0）
    # ------------------------------------------------------------------
    def _reload_rules_now() -> Dict[str, Any]:
        """触发热重载，返回 ``{"ok": bool, "note": str}``。"""
        if reload_callback is None:
            return {
                "ok": False,
                "note": "未配置热重载回调，配置已保存。",
            }
        try:
            outcome = reload_callback()
        except Exception as exc:  # noqa: BLE001 —— 热重载失败只降级
            logger.warning("热重载失败：%s: %s", type(exc).__name__, exc)
            return {"ok": False, "note": "热重载失败：{}".format(exc)}
        if isinstance(outcome, dict):
            note = str(
                outcome.get("note") or outcome.get("message") or ""
            )
            return {"ok": bool(outcome.get("ok")), "note": note}
        return {"ok": bool(outcome), "note": ""}

    def _save_rules(target: str, updates: List[Tuple[str, Any]]) -> Any:
        """把规则清单写回配置文件（失败返回错误 JSON）。"""
        from ..config_loader import save_config

        try:
            result = save_config(
                target, updates, defaults=V370_CONFIG_DEFAULTS
            )
        except Exception as exc:  # noqa: BLE001 —— 写配置失败只降级
            logger.exception("保存规则清单失败")
            return JSONResponse(
                {"ok": False, "error": "保存失败：{}".format(exc)},
                status_code=500,
            )
        if not result.get("ok"):
            return JSONResponse(
                {
                    "ok": False,
                    "error": result.get("error") or "保存失败",
                    "applied": result.get("applied") or [],
                    "skipped": result.get("skipped") or [],
                },
                status_code=500,
            )
        return result

    @app.get("/api/rules")
    async def api_rules_get() -> Dict[str, Any]:
        """读出 L1 规则清单（含开关与动作状态）。

        :return: ``{"ok": True, "rules": [...], "disabled": [...], …}``。
        """
        from ..config_loader import load_config

        target = _resolve_config_path()
        raw = load_config(target) if target else {}
        snapshot = _rules_snapshot(raw)
        snapshot["ok"] = True
        snapshot["path"] = target
        return snapshot

    @app.post("/api/rules")
    async def api_rules_set(request: Request) -> Any:
        """保存 L1 规则清单（开关 / 降级 / 自定义规则 / 拦截策略）。

        :param request: JSON 体 —— ``disabled`` / ``log_only`` /
            ``custom`` / ``l1_action``。
        :return: 保存后的规则清单快照。
        """
        from ..config_loader import load_config
        from ..rules.registry import (
            VALID_L1_ACTIONS,
            dump_custom_rules,
            parse_custom_rules,
        )

        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001 —— 坏 JSON 当空请求
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        target = _resolve_config_path()
        if not target:
            return JSONResponse(
                {"ok": False, "error": "找不到配置文件路径。"},
                status_code=400,
            )
        updates: List[Tuple[str, Any]] = []
        for key, path in (
            ("disabled", "rules.disabled_rules"),
            ("log_only", "rules.log_only_rules"),
        ):
            values = payload.get(key)
            if not isinstance(values, list):
                continue
            updates.append(
                (
                    path,
                    [
                        str(item).strip()
                        for item in values
                        if str(item).strip()
                    ],
                )
            )
        custom = payload.get("custom")
        if isinstance(custom, list):
            entries, errors = parse_custom_rules(custom)
            if errors:
                return JSONResponse(
                    {
                        "ok": False,
                        "error": "自定义规则不合法。",
                        "errors": errors,
                    },
                    status_code=400,
                )
            updates.append(("rules.custom_rules", dump_custom_rules(entries)))
        # --- v3.7.0：红线类别白名单与总开关 ---
        #
        # ⚠️ 规则表是红线类别的**唯一权威 UI** ——
        #    「把红线规则打开」必须真的生效，
        #    所以这里把页面上开着的类别回写成白名单。
        categories = payload.get("categories")
        if isinstance(categories, list):
            updates.append(
                (
                    "rules.catastrophic.categories",
                    _normalize_categories(categories),
                )
            )
        if "catastrophic_enabled" in payload:
            updates.append(
                (
                    "rules.catastrophic.enabled",
                    bool(payload.get("catastrophic_enabled")),
                )
            )
        l1_action = str(payload.get("l1_action") or "").strip().lower()
        if l1_action:
            if l1_action not in VALID_L1_ACTIONS:
                return JSONResponse(
                    {
                        "ok": False,
                        "error": "拦截策略不合法：{}".format(l1_action),
                    },
                    status_code=400,
                )
            updates.append(("rules.l1_action", l1_action))
        if not updates:
            return {
                "ok": True,
                "changed": False,
                "message": "没有要保存的改动。",
                "reloaded": False,
            }
        result = _save_rules(target, updates)
        if isinstance(result, JSONResponse):
            return result
        outcome = _reload_rules_now()
        raw = load_config(target) if target else {}
        snapshot = _rules_snapshot(raw)
        applied = result.get("applied") or []
        snapshot.update(
            {
                "ok": True,
                "path": target,
                "applied": applied,
                "skipped": result.get("skipped") or [],
                "changed": bool(result.get("changed")),
                "reloaded": bool(outcome.get("ok")),
                "reload_note": outcome.get("note") or "",
                "message": "已保存 {} 项。".format(len(applied)),
            }
        )
        return snapshot

    @app.get("/api/rules/export")
    async def api_rules_export(format: str = "json") -> Dict[str, Any]:
        """导出全部规则（JSON 或 YAML 文本）。

        ⚠️ 导出的是**纯规则数组**（一条规则一个对象）——
        这样导出的文件可以直接改、可以直接贴回导入框，
        而且「导出 → 导入」是**无损**的：
        每条规则自带 ``enabled``（开/关）与 ``action``（拦截/仅记录），
        所以开关状态也一并带走。

        ⚠️ ``l1_action``（L1 命中后的拦截策略）**不在**这个文件里 ——
        它是「流水线控制」页上的一个总开关，不是单条规则的属性。

        :param format: ``json`` 或 ``yaml``。
        :return: ``{"ok": True, "format": …, "text": …, "filename": …}``。
        """
        import yaml as _yaml

        from ..config_loader import load_config

        target = _resolve_config_path()
        raw = load_config(target) if target else {}
        snapshot = _rules_snapshot(raw)
        payload = snapshot["rules"]
        fmt = str(format or "json").strip().lower()
        if fmt == "yaml":
            text = _yaml.safe_dump(
                payload, allow_unicode=True, sort_keys=False
            )
            filename = "sentinel-rules.yaml"
        else:
            text = json.dumps(payload, ensure_ascii=False, indent=2)
            filename = "sentinel-rules.json"
        return {
            "ok": True,
            "format": "yaml" if fmt == "yaml" else "json",
            "text": text,
            "filename": filename,
            "count": len(payload),
        }

    @app.post("/api/rules/import")
    async def api_rules_import(request: Request) -> Any:
        """从 JSON / YAML 文本导入规则并写回配置。

        :param request: JSON 体 —— ``{"text": "<JSON 或 YAML 文本>"}``。
        :return: 导入后的规则清单快照。
        """
        from ..config_loader import load_config
        from ..rules.registry import (
            dump_custom_rules,
            import_rules,
            parse_custom_rules,
        )

        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001 —— 坏 JSON 当空请求
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        text = str(payload.get("text") or "")
        outcome = import_rules(text)
        if not outcome.get("ok"):
            return JSONResponse(
                {
                    "ok": False,
                    "error": "导入失败。",
                    "errors": outcome.get("errors") or [],
                },
                status_code=400,
            )
        entries, errors = parse_custom_rules(outcome.get("custom") or [])
        if errors:
            return JSONResponse(
                {"ok": False, "error": "导入失败。", "errors": errors},
                status_code=400,
            )
        target = _resolve_config_path()
        if not target:
            return JSONResponse(
                {"ok": False, "error": "找不到配置文件路径。"},
                status_code=400,
            )
        updates: List[Tuple[str, Any]] = [
            (
                "rules.disabled_rules",
                [str(item) for item in (outcome.get("disabled") or [])],
            ),
            (
                "rules.log_only_rules",
                [str(item) for item in (outcome.get("log_only") or [])],
            ),
            ("rules.custom_rules", dump_custom_rules(entries)),
            (
                "rules.catastrophic.categories",
                _normalize_categories(
                    category
                    for category in _catastrophic_category_names()
                    if "catastrophic.{}".format(category)
                    not in set(outcome.get("disabled") or [])
                ),
            ),
        ]
        result = _save_rules(target, updates)
        if isinstance(result, JSONResponse):
            return result
        reload_outcome = _reload_rules_now()
        raw = load_config(target) if target else {}
        snapshot = _rules_snapshot(raw)
        snapshot.update(
            {
                "ok": True,
                "path": target,
                "applied": result.get("applied") or [],
                "skipped": result.get("skipped") or [],
                "changed": bool(result.get("changed")),
                "reloaded": bool(reload_outcome.get("ok")),
                "reload_note": reload_outcome.get("note") or "",
                "errors": outcome.get("errors") or [],
                "message": "导入完成：{} 条规则，{} 条关闭。".format(
                    len(snapshot["rules"]),
                    len(snapshot["disabled"]),
                ),
            }
        )
        return snapshot

    @app.get("/api/presentation")
    async def api_presentation() -> Dict[str, Any]:
        """返回「呈现与摘要」的两个极限压缩档位（v3.7.0）。

        看板用它决定右侧日志与中间流水的呈现密度 ——
        档位在**设置页**配（``/settings/pipeline``），
        这里只是把配置读出来给看板用。

        :return: ``{"ok": True, "log_compression": …,
            "audit_compression": …}``。
        """
        from ..config_loader import load_config

        target = _resolve_config_path()
        raw = load_config(target) if target else {}
        presentation = raw.get("presentation") or {}
        return {
            "ok": True,
            "log_compression": _clamp_level(
                presentation.get("log_compression"), 40
            ),
            "audit_compression": _clamp_level(
                presentation.get("audit_compression"), 60
            ),
        }

    # ------------------------------------------------------------------
    # 多路会话（v1.0.0）
    # ------------------------------------------------------------------
    @app.get("/api/sessions")
    async def api_sessions() -> Dict[str, Any]:
        """列出**全部**会话及其状态。

        这是「多路中枢」的总览接口 —— 面板顶部的「全局状态」条与
        左侧的会话列表都靠它渲染。

        每一项包含：ID / 名字 / Agent 类型 / 状态 / 告警计数 /
        任务目标 / 目标来源。
        """
        return {
            "ok": True,
            "sessions": registry.snapshot(),
            "active": primary.id,
        }

    @app.get("/api/sessions/{session_id}/state")
    async def api_session_state(session_id: str) -> Dict[str, Any]:
        """返回某个会话的状态快照。"""
        session = registry.resolve(session_id)
        if session is None:
            return JSONResponse(
                {"ok": False, "error": f"会话不存在：{session_id}"},
                status_code=404,
            )
        return session.store.snapshot(
            session.watch_path, session.alert_log_path
        )

    @app.get("/api/sessions/{session_id}/alerts")
    async def api_session_alerts(
        session_id: str, limit: int = 100
    ) -> Dict[str, Any]:
        """返回某个会话最近的告警列表。"""
        session = registry.resolve(session_id)
        if session is None:
            return JSONResponse(
                {"ok": False, "error": f"会话不存在：{session_id}"},
                status_code=404,
            )
        count = max(int(limit), 1)
        alerts = session.store.alerts()[-count:]
        return {
            "ok": True,
            "session_id": session.id,
            "alerts": alerts,
            "counts": session.store.counts(),
            "total": len(alerts),
        }

    @app.get("/api/sessions/{session_id}/watch_target")
    async def api_session_watch_target(session_id: str) -> Dict[str, Any]:
        """返回某个会话的「当前监控目标」快照。"""
        session = registry.resolve(session_id)
        if session is None:
            return JSONResponse(
                {"ok": False, "error": f"会话不存在：{session_id}"},
                status_code=404,
            )
        snapshot = session.watch_target.snapshot()
        snapshot["ok"] = True
        snapshot["session_id"] = session.id
        return snapshot

    @app.post("/api/sessions/{session_id}/pause")
    async def api_session_pause(
        session_id: str, payload: Dict[str, Any]
    ) -> JSONResponse:
        """暂停 / 恢复某个会话的实时推送。"""
        session = registry.resolve(session_id)
        if session is None:
            return JSONResponse(
                {"ok": False, "error": f"会话不存在：{session_id}"},
                status_code=404,
            )
        paused = bool(payload.get("paused", True))
        session.store.set_paused(paused)
        status = session.store.status()
        hub.publish({
            "type": "status",
            "session_id": session.id,
            **status,
        })
        return JSONResponse({"ok": True, **status})

    @app.post("/api/sessions/{session_id}/scan")
    async def api_session_scan(session_id: str) -> JSONResponse:
        """触发某个会话的「立即扫描」。"""
        session = registry.resolve(session_id)
        if session is None:
            return JSONResponse(
                {"ok": False, "error": f"会话不存在：{session_id}"},
                status_code=404,
            )
        if session.scan_callback is None:
            return JSONResponse(
                {"ok": False, "error": "扫描未启用（--no-scan）"},
                status_code=503,
            )
        try:
            result = await run_in_threadpool(
                session.scan_callback, session.watch_path
            )
        except Exception as exc:  # noqa: BLE001 —— 扫描失败不影响服务
            logger.exception("扫描失败：%s: %s", type(exc).__name__, exc)
            return JSONResponse(
                {"ok": False, "error": f"{type(exc).__name__}: {exc}"},
                status_code=500,
            )
        if isinstance(result, dict):
            counts = result.get("counts")
            if isinstance(counts, dict):
                session.store.set_scan_counts(counts)
        return JSONResponse({"ok": True})

    @app.get("/api/sessions/{session_id}/task_goal")
    async def api_session_task_goal_get(session_id: str) -> Dict[str, Any]:
        """返回某个会话的任务目标（意图锚点）。"""
        session = registry.resolve(session_id)
        if session is None:
            return JSONResponse(
                {"ok": False, "error": f"会话不存在：{session_id}"},
                status_code=404,
            )
        return {"ok": True, "goal": session.task_goal.get()}

    @app.post("/api/sessions/{session_id}/task_goal")
    async def api_session_task_goal_set(
        session_id: str, payload: Dict[str, Any]
    ) -> Dict[str, Any]:
        """设定 / 更换某个会话的任务目标。

        目标一改，正在运行的引擎里的意图追踪器会立刻换锚点，
        并把「连续漂移」计数清零。

        v2.4.0：挂载进来的会话**没有**最初的 Prompt，
        用户在这里手动注入目标之后，
        该会话的滑窗机制**立刻**启用、开始盯意图漂移（DRIFT）。
        """
        session = registry.resolve(session_id)
        if session is None:
            return JSONResponse(
                {"ok": False, "error": f"会话不存在：{session_id}"},
                status_code=404,
            )
        goal = str(payload.get("goal") or "").strip()
        value = session.task_goal.set(goal)
        # 「当前监控目标」面板要跟上 —— 不然用户设了目标，
        # 面板上还写着「（尚未锚定任务目标）」，会以为没生效。
        try:
            session.watch_target.set_goal(value, "explicit" if value else "none")
        except Exception as exc:  # noqa: BLE001 —— 面板同步失败不影响
            logger.warning("同步监控目标失败：%s: %s", type(exc).__name__, exc)
        hub.publish({
            "type": "task_goal",
            "session_id": session.id,
            "goal": value,
        })
        return {"ok": True, "goal": value}

    @app.post("/api/briefing")
    async def api_briefing() -> Dict[str, Any]:
        """生成并保存《哨兵值守晨报》（v1.2.0，单路模式）。

        :return: ``{"ok": True, "markdown": …, "terminal": …, "path": …}``。
        """
        result = primary.build_briefing()
        if not result.get("ok"):
            return JSONResponse(result, status_code=501)
        hub.publish({
            "type": "briefing",
            "session_id": primary.id,
            "path": str(result.get("path") or ""),
        })
        return result

    @app.post("/api/sessions/{session_id}/briefing")
    async def api_session_briefing(session_id: str) -> Dict[str, Any]:
        """生成并保存《哨兵值守晨报》（v1.2.0）。

        看板上的【生成晨报】按钮打这个接口。

        :param session_id: 会话 ID。
        :return: ``{"ok": True, "markdown": …, "terminal": …, "path": …}``。
        """
        session = registry.resolve(session_id)
        if session is None:
            return JSONResponse(
                {"ok": False, "error": f"会话不存在：{session_id}"},
                status_code=404,
            )
        result = session.build_briefing()
        if not result.get("ok"):
            return JSONResponse(result, status_code=501)
        hub.publish({
            "type": "briefing",
            "session_id": session.id,
            "path": str(result.get("path") or ""),
        })
        return result

    @app.post("/api/sessions/{session_id}/alerts/{alert_id}/correction")
    async def api_session_alert_correction(
        session_id: str, alert_id: str
    ) -> Dict[str, Any]:
        """生成「纠偏提示」/「拦截原因」（v1.2.0）。

        看板告警卡片上的【复制纠偏提示】按钮打这个接口。

        :param session_id: 会话 ID。
        :param alert_id:  告警 ID。
        :return: ``{"ok": True, "prompt": …, "label": …}``。
        """
        session = registry.resolve(session_id)
        if session is None:
            return JSONResponse(
                {"ok": False, "error": f"会话不存在：{session_id}"},
                status_code=404,
            )
        alert = session.store.find_alert(alert_id)
        if alert is None:
            return JSONResponse(
                {"ok": False, "error": f"告警不存在或已被挤出列表：{alert_id}"},
                status_code=404,
            )
        # 惰性导入 —— 看板层不静态依赖 core 包。
        from ..core.correction import build_prompt, copy_label

        goal = ""
        try:
            goal = session.task_goal.get()
        except Exception:  # noqa: BLE001 —— 取不到目标不该挡住复制
            goal = ""
        return {
            "ok": True,
            "prompt": build_prompt(alert, task_goal=goal),
            "label": copy_label(alert),
        }

    @app.post("/api/alerts/{alert_id}/correction")
    async def api_alert_correction(alert_id: str) -> Dict[str, Any]:
        """生成「纠偏提示」/「拦截原因」（**不带**会话 ID 的那条路）。

        为什么需要它
        ============

        前端只有在**拿得到会话 ID** 时才会打
        ``/api/sessions/{id}/alerts/{alert_id}/correction``；
        拿不到（比如单会话模式下会话 ID 是空的）
        就退回来打这个**无会话**路由。

        早先只实现了会话级的那条 —— 无会话时前端拿到 404，
        【复制纠偏提示】**静默失败**（按钮点了没反应，
        剪贴板里还是旧内容）。
        这里补上，让两条路都能用。

        :param alert_id: 告警 ID。
        :return: ``{"ok": True, "prompt": …, "label": …}``。
        """
        alert = store.find_alert(alert_id)
        if alert is None:
            return JSONResponse(
                {"ok": False, "error": f"告警不存在或已被挤出列表：{alert_id}"},
                status_code=404,
            )
        # 惰性导入 —— 看板层不静态依赖 core 包。
        from ..core.correction import build_prompt, copy_label

        goal = ""
        try:
            goal = goal_holder.get()
        except Exception:  # noqa: BLE001 —— 取不到目标不该挡住复制
            goal = ""
        return {
            "ok": True,
            "prompt": build_prompt(alert, task_goal=goal),
            "label": copy_label(alert),
        }

    # ------------------------------------------------------------------
    # 控制中枢（v2.0.0）：直接发指令 + 一键回滚
    # ------------------------------------------------------------------
    @app.post("/api/sessions/{session_id}/alerts/{alert_id}/inject")
    async def api_session_alert_inject(
        session_id: str, alert_id: str
    ) -> Dict[str, Any]:
        """把纠偏提示**直接发送给 Agent**（v2.0.0）。

        看板告警卡片上的【直接发送给 Agent】按钮打这个接口。

        :param session_id: 会话 ID。
        :param alert_id:  告警 ID。
        :return: 注入结果（含 ``channel`` / ``detail`` / ``next_step``）。
        """
        session = registry.resolve(session_id)
        if session is None:
            return JSONResponse(
                {"ok": False, "error": f"会话不存在：{session_id}"},
                status_code=404,
            )
        alert = session.store.find_alert(alert_id)
        if alert is None:
            return JSONResponse(
                {"ok": False, "error": f"告警不存在或已被挤出列表：{alert_id}"},
                status_code=404,
            )
        # 惰性导入 —— 看板层不静态依赖 core 包。
        from ..core.correction import build_prompt, copy_label

        goal = ""
        try:
            goal = session.task_goal.get()
        except Exception:  # noqa: BLE001 —— 取不到目标不该挡住发送
            goal = ""
        prompt = build_prompt(alert, task_goal=goal)
        result = session.inject(
            prompt,
            {
                "verdict": str(alert.get("verdict") or ""),
                "rule_id": str(alert.get("rule_id") or ""),
                "reason": str(alert.get("reason") or ""),
                "label": copy_label(alert),
            },
        )
        hub.publish({
            "type": "injection",
            "session_id": session.id,
            "alert_id": alert_id,
            "channel": str(result.get("channel") or ""),
            "ok": bool(result.get("ok")),
        })
        return result

    @app.post("/api/alerts/{alert_id}/inject")
    async def api_alert_inject(alert_id: str) -> Dict[str, Any]:
        """把纠偏提示直接发送给 Agent（**无会话**的那条路）。

        :param alert_id: 告警 ID。
        :return: 注入结果。
        """
        alert = store.find_alert(alert_id)
        if alert is None:
            return JSONResponse(
                {"ok": False, "error": f"告警不存在或已被挤出列表：{alert_id}"},
                status_code=404,
            )
        # 惰性导入 —— 看板层不静态依赖 core 包。
        from ..core.correction import build_prompt, copy_label

        goal = ""
        try:
            goal = goal_holder.get()
        except Exception:  # noqa: BLE001 —— 取不到目标不该挡住发送
            goal = ""
        prompt = build_prompt(alert, task_goal=goal)
        primary = registry.primary()
        if primary is not None:
            result = primary.inject(
                prompt,
                {
                    "verdict": str(alert.get("verdict") or ""),
                    "rule_id": str(alert.get("rule_id") or ""),
                    "reason": str(alert.get("reason") or ""),
                    "label": copy_label(alert),
                },
            )
        else:
            from ..core.injector import AgentInjector, InboxStore

            workdir = os.path.dirname(watch_abs) or os.getcwd()
            injector = AgentInjector(
                inbox=InboxStore(
                    os.path.join(workdir, "sentinel_inbox.json")
                )
            )
            outcome = injector.inject(
                prompt,
                verdict=str(alert.get("verdict") or ""),
                rule_id=str(alert.get("rule_id") or ""),
                reason=str(alert.get("reason") or ""),
            )
            result = outcome.to_dict()
        hub.publish({
            "type": "injection",
            "session_id": primary.id if primary else "",
            "alert_id": alert_id,
            "channel": str(result.get("channel") or ""),
            "ok": bool(result.get("ok")),
        })
        return result

    @app.post("/api/sessions/{session_id}/alerts/{alert_id}/rollback")
    async def api_session_alert_rollback(
        session_id: str, alert_id: str
    ) -> Dict[str, Any]:
        """【一键回滚至拦截点】（v2.0.0）。

        :param session_id: 会话 ID。
        :param alert_id:  告警 ID（用来定位那次拦截的安全点）。
        :return: 回滚结果（含恢复/删除的文件数与耗时）。
        """
        session = registry.resolve(session_id)
        if session is None:
            return JSONResponse(
                {"ok": False, "error": f"会话不存在：{session_id}"},
                status_code=404,
            )
        alert = session.store.find_alert(alert_id)
        if alert is None:
            return JSONResponse(
                {"ok": False, "error": f"告警不存在或已被挤出列表：{alert_id}"},
                status_code=404,
            )
        point_id = ""
        try:
            safe_point = alert.get("safe_point") or {}
            if isinstance(safe_point, dict):
                rollback_point = safe_point.get("rollback_point") or {}
                if isinstance(rollback_point, dict):
                    point_id = str(rollback_point.get("id") or "")
        except Exception:  # noqa: BLE001 —— 取不到就回滚到基线
            point_id = ""
        result = session.rollback(point_id)
        hub.publish({
            "type": "rollback",
            "session_id": session.id,
            "alert_id": alert_id,
            "ok": bool(result.get("ok")),
        })
        return result

    @app.post("/api/sessions/{session_id}/rollback")
    async def api_session_rollback(
        session_id: str, point_id: str = ""
    ) -> Dict[str, Any]:
        """回滚到指定安全点（留空 = 基线）。

        :param session_id: 会话 ID。
        :param point_id:  安全点 ID。
        :return: 回滚结果。
        """
        session = registry.resolve(session_id)
        if session is None:
            return JSONResponse(
                {"ok": False, "error": f"会话不存在：{session_id}"},
                status_code=404,
            )
        return session.rollback(point_id)

    @app.get("/api/sessions/{session_id}/safe-points")
    async def api_session_safe_points(session_id: str) -> Dict[str, Any]:
        """列出这个会话的安全点。

        :param session_id: 会话 ID。
        :return: ``{"ok": True, "points": [...]}``。
        """
        session = registry.resolve(session_id)
        if session is None:
            return JSONResponse(
                {"ok": False, "error": f"会话不存在：{session_id}"},
                status_code=404,
            )
        return {
            "ok": True,
            "points": session.safe_points(),
        }

    @app.post("/api/sessions/{session_id}/alerts/{alert_id}/resume")
    async def api_session_alert_resume(
        session_id: str, alert_id: str
    ) -> Dict[str, Any]:
        """【回滚并重试】（v2.1.0）—— 一键复活被挂起的会话。

        做三件不可逆的事：回滚工作目录、结束挂起的进程、重拉 Agent。
        所以端点只负责**执行** —— 计划由前端先弹给用户看。

        :param session_id: 会话 ID。
        :param alert_id:  告警 ID（用来定位那次拦截的安全点）。
        :return: 复活结果。
        """
        session = registry.resolve(session_id)
        if session is None:
            return JSONResponse(
                {"ok": False, "error": f"会话不存在：{session_id}"},
                status_code=404,
            )
        alert = session.store.find_alert(alert_id)
        if alert is None:
            return JSONResponse(
                {"ok": False, "error": f"告警不存在或已被挤出列表：{alert_id}"},
                status_code=404,
            )
        # 用那条告警的**生效安全点**（拦截时生效的那个，通常是基线）。
        point_id = ""
        try:
            safe_point = alert.get("safe_point") or {}
            if isinstance(safe_point, dict):
                rollback_point = safe_point.get("rollback_point") or {}
                if isinstance(rollback_point, dict):
                    point_id = str(rollback_point.get("id") or "")
        except Exception:  # noqa: BLE001 —— 取不到就用基线
            point_id = ""
        prompt = _build_resume_prompt(alert)
        result = session.resume(
            prompt,
            {
                "alert_id": alert_id,
                "verdict": str(alert.get("verdict") or ""),
                "rule_id": str(alert.get("rule_id") or ""),
                "reason": str(alert.get("reason") or ""),
                "point_id": point_id,
            },
        )
        hub.publish({
            "type": "resume",
            "session_id": session.id,
            "alert_id": alert_id,
            "ok": bool(result.get("ok")),
            "message": str(result.get("message") or ""),
        })
        return result

    @app.get("/api/sessions/{session_id}/alerts/{alert_id}/diff")
    async def api_session_alert_diff(
        session_id: str, alert_id: str, point_id: str = ""
    ) -> Dict[str, Any]:
        """【查看改动】（v2.1.0）—— 安全点 vs 现在的文件差异。

        :param session_id: 会话 ID。
        :param alert_id:  告警 ID。
        :param point_id:  安全点 ID；留空表示用告警里的生效安全点。
        :return: 差异字典（含每个文件的 unified diff）。
        """
        session = registry.resolve(session_id)
        if session is None:
            return JSONResponse(
                {"ok": False, "error": f"会话不存在：{session_id}"},
                status_code=404,
            )
        alert = session.store.find_alert(alert_id)
        if alert is None:
            return JSONResponse(
                {"ok": False, "error": f"告警不存在或已被挤出列表：{alert_id}"},
                status_code=404,
            )
        if not point_id:
            try:
                safe_point = alert.get("safe_point") or {}
                if isinstance(safe_point, dict):
                    rollback_point = safe_point.get("rollback_point") or {}
                    if isinstance(rollback_point, dict):
                        point_id = str(rollback_point.get("id") or "")
            except Exception:  # noqa: BLE001 —— 取不到就用基线
                point_id = ""
        return session.diff(point_id)

    @app.get("/api/sessions/{session_id}/resume-plan")
    async def api_session_resume_plan(
        session_id: str, point_id: str = ""
    ) -> Dict[str, Any]:
        """复活**计划**（只算不动）—— 看板先弹给用户看。

        :param session_id: 会话 ID。
        :param point_id:  安全点 ID；留空表示用基线。
        :return: 计划字典。
        """
        session = registry.resolve(session_id)
        if session is None:
            return JSONResponse(
                {"ok": False, "error": f"会话不存在：{session_id}"},
                status_code=404,
            )
        plan = session.resume_plan(point_id)
        return {"ok": bool(plan.get("ok")), "plan": plan}

    @app.get("/api/sessions/{session_id}/diff")
    async def api_session_diff(
        session_id: str, point_id: str = ""
    ) -> Dict[str, Any]:
        """按**安全点**算文件差异（v2.2.0）。

        时间线上的节点不一定来自告警（复活、回滚、热重载都是
        「操作节点」），但它们照样关联着某个安全点 ——
        所以这里提供一个**不依赖告警 ID** 的差异入口。

        :param session_id: 会话 ID。
        :param point_id: 安全点 ID；留空表示用基线。
        :return: 差异字典（含每个文件的 unified diff）。
        """
        session = registry.resolve(session_id)
        if session is None:
            return JSONResponse(
                {"ok": False, "error": f"会话不存在：{session_id}"},
                status_code=404,
            )
        return session.diff(point_id)

    # ------------------------------------------------------------------
    # 案发时间线 & 开箱体检（v2.2.0）
    # ------------------------------------------------------------------
    @app.get("/api/sessions/{session_id}/timeline")
    async def api_session_timeline(session_id: str) -> Dict[str, Any]:
        """【案发时间线】（v2.2.0）—— 把这一路发生过的事排成一条线。

        :param session_id: 会话 ID。
        :return: ``{"ok": True, "nodes": [...], "summary": {...}}``。
        """
        session = registry.resolve(session_id)
        if session is None:
            return JSONResponse(
                {"ok": False, "error": f"会话不存在：{session_id}"},
                status_code=404,
            )
        return session.timeline()

    @app.post("/api/sessions/{session_id}/health")
    async def api_session_health(
        session_id: str, payload: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """【开箱安全体检】（v2.2.0）—— 跑一次体检并返回报告。

        :param session_id: 会话 ID。
        :param payload: 可选请求体；``{"skip_network": true}`` 跳过联网两项。
        :return: 体检结果。
        """
        session = registry.resolve(session_id)
        if session is None:
            return JSONResponse(
                {"ok": False, "error": f"会话不存在：{session_id}"},
                status_code=404,
            )
        skip_network = bool((payload or {}).get("skip_network"))
        return session.health(skip_network=skip_network)

    @app.post("/api/health")
    async def api_health(
        payload: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """【开箱安全体检】（v2.2.0）—— 对**主会话**跑一次体检。

        顶栏的【体检】按钮走这个端点。

        :param payload: 可选请求体；``{"skip_network": true}`` 跳过联网两项。
        :return: 体检结果。
        """
        skip_network = bool((payload or {}).get("skip_network"))
        session = registry.primary()
        if session is None:
            # 一个会话都没有时也要能给体检结果 —— 用当前目录凑合。
            try:
                from ..config_loader import load_config
                from ..core.health import render_markdown, run_health_check

                config = load_config(None)
                items = run_health_check(
                    config,
                    workdir=os.getcwd(),
                    skip_network=skip_network,
                )
                return {
                    "ok": True,
                    "items": [item.to_dict() for item in items],
                    "markdown": render_markdown(items),
                }
            except Exception as exc:  # noqa: BLE001 —— 体检失败不外泄
                return {
                    "ok": False,
                    "error": "{}: {}".format(type(exc).__name__, exc),
                    "items": [],
                    "markdown": "",
                }
        return session.health(skip_network=skip_network)


    @app.get("/api/state")
    async def api_state() -> Dict[str, Any]:
        """返回当前状态快照。"""
        return store.snapshot(watch_abs, alert_abs)

    @app.get("/api/alerts")
    async def api_alerts(limit: int = 100) -> Dict[str, Any]:
        """返回最近的告警列表。

        :param limit: 最多返回多少条（默认 100）。
        """
        count = max(int(limit), 1)
        alerts = store.alerts()[-count:]
        return {
            "alerts": alerts,
            "counts": store.counts(),
            "total": len(alerts),
        }

    @app.get("/api/events")
    async def api_events(request: Request) -> StreamingResponse:
        """SSE 事件流（告警 / 日志行 / 状态）。

        多路会话下，事件里带 ``session_id`` —— 前端据此把事件
        分流到对应的会话，而不是全都堆在一个列表里。
        """
        queue = hub.subscribe()

        async def generator() -> AsyncIterator[str]:
            async for frame in stream_events(request, queue, hub, store):
                yield frame

        return StreamingResponse(
            generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @app.post("/api/scan")
    async def api_scan() -> JSONResponse:
        """触发一次扫描（由注入的回调执行真正的引擎）。"""
        if scan_callback is None:
            return JSONResponse(
                {"ok": False, "error": "扫描未启用（--no-scan）"},
                status_code=503,
            )
        try:
            result = await run_in_threadpool(scan_callback, watch_abs)
        except Exception as exc:  # noqa: BLE001 —— 扫描失败不影响服务
            logger.exception("扫描失败：%s: %s", type(exc).__name__, exc)
            return JSONResponse(
                {"ok": False, "error": f"{type(exc).__name__}: {exc}"},
                status_code=500,
            )
        if isinstance(result, dict):
            counts = result.get("counts")
            if isinstance(counts, dict):
                store.set_scan_counts(counts)
        return JSONResponse({"ok": True})

    @app.post("/api/pause")
    async def api_pause(payload: Dict[str, Any]) -> Dict[str, Any]:
        """暂停 / 恢复实时推送。

        暂停只是**不往浏览器推**，tailer 仍然继续读文件，
        数据不会丢；恢复后前端会重新拉一次 ``/api/state`` 对齐。
        """
        paused = bool(payload.get("paused", True))
        store.set_paused(paused)
        status = store.status()
        hub.publish({"type": "status", **status})
        return status

    # ------------------------------------------------------------------
    # 任务目标（意图锚点）
    # ------------------------------------------------------------------
    @app.get("/api/task_goal")
    async def api_task_goal_get() -> Dict[str, Any]:
        """返回当前的任务目标（意图锚点）。"""
        return {"ok": True, "goal": goal_holder.get()}

    @app.post("/api/task_goal")
    async def api_task_goal_set(payload: Dict[str, Any]) -> Dict[str, Any]:
        """设定 / 更换任务目标。

        目标一改，正在运行的引擎里的意图追踪器会立刻换锚点，
        并把「连续漂移」计数清零 —— 换了目标，之前的判定就不作数了。
        """
        goal = str(payload.get("goal") or "").strip()
        value = goal_holder.set(goal)
        hub.publish({"type": "task_goal", "goal": value})
        return {"ok": True, "goal": value}

    # ------------------------------------------------------------------
    # 当前监控目标（面板顶部）
    # ------------------------------------------------------------------
    @app.get("/api/watch_target")
    async def api_watch_target() -> Dict[str, Any]:
        """返回「当前监控目标」快照。

        面板顶部用它显示三件事：被监控的 Agent 类型、任务目标、
        以及这个目标是**自动提取**的还是人工/配置给的。
        """
        snapshot = target_holder.snapshot()
        snapshot["ok"] = True
        return snapshot

    # ------------------------------------------------------------------
    # 守护者（实时干预）
    # ------------------------------------------------------------------
    async def _guardian_status_of(session: Optional[Session]) -> Dict[str, Any]:
        """读取某个会话的守护者状态。

        :param session: 会话；``None`` 或没接守护者时返回不可用。
        :return: 守护者状态字典。
        """
        if session is None or session.guardian is None:
            return {"ok": False, "enabled": False, "available": False}
        try:
            state = await run_in_threadpool(session.guardian.status)
        except Exception as exc:  # noqa: BLE001 —— 守护者异常不影响服务
            logger.exception("读取守护者状态失败：%s: %s", type(exc).__name__, exc)
            return JSONResponse(
                {"ok": False, "error": f"{type(exc).__name__}: {exc}"},
                status_code=500,
            )
        if not isinstance(state, dict):
            state = {}
        state.setdefault("ok", True)
        state.setdefault("available", True)
        state.setdefault("enabled", False)
        return state

    async def _guardian_action(
        session: Optional[Session],
        action: str,
        payload: Dict[str, Any],
    ) -> JSONResponse:
        """对某个会话执行一次守护者动作（干预 / 放行）。

        :param session: 会话。
        :param action: ``intervene`` 或 ``override``。
        :param payload: 动作参数。
        :return: JSON 响应。
        """
        if session is None or session.guardian is None:
            return JSONResponse(
                {"ok": False, "error": "守护者未接入（--no-guardian）"},
                status_code=503,
            )
        try:
            result = await run_in_threadpool(
                session.guardian.intervene if action == "intervene"
                else session.guardian.override,
                payload,
            )
        except Exception as exc:  # noqa: BLE001 —— 动作失败只回报
            logger.exception(
                "%s失败：%s: %s", action, type(exc).__name__, exc
            )
            return JSONResponse(
                {"ok": False, "error": f"{type(exc).__name__}: {exc}"},
                status_code=500,
            )
        result = result if isinstance(result, dict) else {"ok": False}
        result.setdefault("ok", False)
        hub.publish({
            "type": "guardian",
            "session_id": session.id,
            **result,
        })
        return JSONResponse(result)

    @app.get("/api/guardian/status")
    async def api_guardian_status() -> Dict[str, Any]:
        """返回守护者状态（是否启用、目标 PID、被挂起的进程等）。"""
        return await _guardian_status_of(primary)

    @app.post("/api/guardian/intervene")
    async def api_guardian_intervene(
        payload: Dict[str, Any],
    ) -> JSONResponse:
        """【强制中断】—— 对指定规则执行一次主动干预（挂起 / 中断）。

        失败不抛异常，只是把失败原因原样回给前端 ——
        审查侧的异常绝不能让看板跟着挂掉。
        """
        return await _guardian_action(primary, "intervene", payload)

    @app.post("/api/guardian/override")
    async def api_guardian_override(
        payload: Dict[str, Any],
    ) -> JSONResponse:
        """【放行并忽略】—— 用户否决：恢复进程并把该规则拉黑。

        「拉黑」只在**本次任务**内有效 —— 哨兵的权限必须小于用户，
        下次重启它还得重新问一遍。
        """
        return await _guardian_action(primary, "override", payload)

    # ------------------------------------------------------------------
    # 会话级守护者操作
    # ------------------------------------------------------------------
    @app.get("/api/sessions/{session_id}/guardian/status")
    async def api_session_guardian_status(
        session_id: str,
    ) -> Dict[str, Any]:
        """返回某个会话的守护者状态。"""
        return await _guardian_status_of(registry.resolve(session_id))

    @app.post("/api/sessions/{session_id}/guardian/intervene")
    async def api_session_guardian_intervene(
        session_id: str, payload: Dict[str, Any]
    ) -> JSONResponse:
        """对某个会话执行【强制中断】。"""
        return await _guardian_action(
            registry.resolve(session_id), "intervene", payload
        )

    @app.post("/api/sessions/{session_id}/guardian/override")
    async def api_session_guardian_override(
        session_id: str, payload: Dict[str, Any]
    ) -> JSONResponse:
        """对某个会话执行【放行并忽略】。"""
        return await _guardian_action(
            registry.resolve(session_id), "override", payload
        )

    # ------------------------------------------------------------------
    # 武装守护者（v2.4.0）
    # ------------------------------------------------------------------
    @app.post("/api/sessions/{session_id}/arm")
    async def api_session_arm(
        session_id: str, payload: Dict[str, Any]
    ) -> Any:
        """打开 / 关闭某个挂载会话的【武装守护者】开关。

        ⚠️ 这是**唯一**能让守护者接管挂载会话的入口：
        在用户手动打开之前，挂载进来的会话**只读、只报告**，
        哨兵对用户自己跑的进程连碰都不碰（红线 R7）。

        :param session_id: 会话 ID。
        :param payload: ``{"armed": true}`` 或 ``{"armed": false}``。
        :return: ``{"ok": bool, "armed": bool, "message": str}``。
        """
        session = registry.resolve(session_id)
        if session is None:
            return JSONResponse(
                {"ok": False, "error": f"会话不存在：{session_id}"},
                status_code=404,
            )
        if session.arm_callback is None:
            return JSONResponse(
                {
                    "ok": False,
                    "error": "这个会话不支持武装守护者（它不是挂载进来的会话）。",
                },
                status_code=400,
            )
        armed = bool(payload.get("armed"))
        try:
            result = await run_in_threadpool(session.arm_callback, armed)
        except Exception as exc:  # noqa: BLE001 —— 武装失败只回报
            logger.exception("武装失败：%s: %s", type(exc).__name__, exc)
            return JSONResponse(
                {"ok": False, "error": f"{type(exc).__name__}: {exc}"},
                status_code=500,
            )
        if not isinstance(result, dict):
            result = {"ok": False, "error": "武装失败：未知错误"}
        # 守护者对象不可 JSON 化 —— 取出来塞进会话，绝不放进响应。
        guardian = result.pop("_guardian", None)
        if result.get("ok"):
            session.armed = bool(result.get("armed"))
            if guardian is not None:
                session.guardian = guardian
            elif not session.armed:
                session.guardian = None
            # v3.1.0：武装状态也**持久化** ——
            # 用户亲手开的开关，不该因为哨兵重启一次就白开。
            if session.attached and session.watch_path:
                try:
                    if attach_store is not None:
                        attach_store.update(
                            session.watch_path, armed=session.armed
                        )
                except Exception as exc:  # noqa: BLE001 —— 持久化失败只降级
                    logger.warning(
                        "保存武装状态失败：%s: %s", type(exc).__name__, exc
                    )
            hub.publish({
                "type": "arm",
                "session_id": session.id,
                "armed": session.armed,
                "pid": int((session.process_info or {}).get("pid") or 0),
            })
        result.setdefault("ok", False)
        result.setdefault("armed", session.armed)
        return result

    if os.path.isdir(STATIC_DIR):
        app.mount(
            "/static",
            StaticFiles(directory=STATIC_DIR),
            name="static",
        )
    return app


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
    # ⚠️ 顺序很重要：图标 → 缓冲 → 退出。
    stop_tray_icon()
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except (AttributeError, ValueError, OSError) as exc:
            # 刷不出去也要继续退 —— 宁可少一句日志，
            # 也不能因为「日志没刷出去」而把进程留着。
            logger.debug("刷新输出流失败：%s: %s", type(exc).__name__, exc)
    os._exit(code)


def stop_tray_icon() -> None:
    """收掉托盘图标（并把句柄清空）（v3.8.0）。

    ⚠️ 句柄必须**清空** —— 否则退出钩子会把同一个图标收两遍，
    而 ``finally`` 里那次也会再收一遍。
    """
    icon = _TRAY.get("icon")
    _TRAY["icon"] = None
    stop_tray(icon)


def start_tray_icon(host: str, port: int) -> None:
    """在系统托盘里放一个哨兵图标（v3.4.0）。

    托盘不可用（没装 pystray / Pillow、非 Windows、
    或者建图标失败）时**什么都不做** —— 哨兵照常跑，
    只是没有那个图标（红线 R5：旁路能力缺失不许拖垮主功能）。

    ⚠️ 托盘的收尾（收掉图标）会**自动**登记成退出钩子 ——
    任何调用这条路的入口都自带一致的收尾行为，
    不用各自记得清理。

    :param host: 监听地址。
    :param port: 监听端口。
    """
    if not tray_available():
        logger.info("托盘不可用（缺少 pystray / Pillow）—— 不出图标。")
        return
    # ⚠️ 监听 0.0.0.0 时，「打开看板」要指向 127.0.0.1 ——
    #    0.0.0.0 不是能打开的地址。
    browser_host = (
        "127.0.0.1" if host in (_ANY_INTERFACE, "::", "") else host
    )
    url = "http://{}:{}/".format(browser_host, port)
    try:
        icon = build_tray_icon(url, lambda: _SHUTDOWN.request("tray"))
    except Exception as exc:  # noqa: BLE001 —— 托盘失败不该影响主功能
        logger.warning(
            "创建托盘图标失败：%s: %s", type(exc).__name__, exc
        )
        return
    if icon is None:
        return
    thread = start_tray(icon)
    if thread is None:
        return
    _TRAY["icon"] = icon
    # 收尾钩子：退出时把图标收掉（不然图标会留在系统里）。
    _SHUTDOWN.add_hook(lambda reason: stop_tray_icon())
    logger.info("托盘图标已启动（右键菜单：打开看板 / 彻底退出）")


def register_shutdown_hook(hook: Callable[[str], None]) -> None:
    """登记一个**退出钩子**（v3.4.0）。

    给那些**不在看板层**的代码用 ——
    比如「接管真实 Agent」的启动器：它自己拉起了 Agent 子进程，
    退出时必须把那些进程收掉
    （红线 R6：只收**自己**拉起的，不碰用户的进程）。

    :param hook: 形如 ``hook(reason: str) -> None`` 的可调用对象。
    """
    _SHUTDOWN.add_hook(hook)


def bind_shutdown_server(server: Optional[object]) -> None:
    """把 uvicorn 服务器对象绑给退出协调器（v3.8.0）。

    「接管真实 Agent」的启动器**自己**建 uvicorn，
    绕开了 :func:`_serve` —— 结果托盘的【彻底退出】找不到服务器，
    只能等 Agent 线程自己 unwind 才停。
    这里给它一个**公开入口**，让启动器也能绑上。

    :param server: uvicorn 的 :class:`uvicorn.Server` 实例；``None`` 表示解绑。
    """
    _SHUTDOWN.bind_server(server)


def install_shutdown_console_handler() -> bool:
    """让「关掉黑框」也走同一条退出路径（v3.8.0）。

    ⚠️ v3.4.0 只在 :func:`main` 里装过这个处理器，
    所以「接管模式」关掉控制台窗口**仍然会留下幽灵进程**。
    这里把它提成公开入口，启动器也装上。

    :return: 装上了返回 ``True``；非 Windows 或失败返回 ``False``。
    """
    return install_console_close_handler(_SHUTDOWN)


def _serve(
    app: FastAPI,
    config: Any,
    *,
    demo_mode: bool = False,
) -> int:
    """跑 uvicorn（v3.3.0 起**不再**自动重启）。

    ⚠️ v3.3.0 的理念：**用完即走，想开才开**
    ==========================================

    v3.1.0 ~ v3.2.1 这里有个「守护模式」：看板服务崩了**自己爬起来**，
    配合开机自启，构成一条「开机就在后台静默驻留」的链路。

    v3.3.0 起这条链路被**彻底删除**：

    * 崩了 → **老老实实退出**，把崩溃日志留给用户看；
    * 不后台静默、不自己复活、不开机自启。

    :param app: FastAPI 应用。
    :param config: uvicorn 配置对象。
    :param demo_mode: 是否是演示模式（页面上的按钮要能找到服务器对象）。
    :return: 进程退出码。
    """
    import uvicorn

    server = uvicorn.Server(config)
    if demo_mode:
        # 演示模式：页面上的【退出演示】按钮靠它找到服务器对象。
        _DEMO_SHUTDOWN["server"] = server
    # ⚠️ v3.4.0：**彻底退出**也要能找到服务器对象 ——
    #    托盘与看板按钮都靠它让 uvicorn 干净停机。
    _SHUTDOWN.bind_server(server)
    try:
        server.run()
    except Exception as exc:  # noqa: BLE001 —— 崩了就退出，不自己复活
        crash_log = _write_crash_log(exc)
        logger.exception(
            "看板服务异常退出：%s: %s%s",
            type(exc).__name__,
            exc,
            "，详见 {}".format(crash_log) if crash_log else "",
        )
        print(
            "[SENTINEL] 看板服务崩溃（{}），哨兵**就此退出**。{}".format(
                type(exc).__name__,
                "详见 {}".format(crash_log) if crash_log else "",
            ),
            flush=True,
        )
        return 1
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    """命令行入口：``python -m src.gui.server --watch <日志>``。

    :param argv: 参数列表；``None`` 时取 ``sys.argv[1:]``。
    :return: 进程退出码。
    """
    parser = argparse.ArgumentParser(
        prog="sentinel-gui",
        description="哨兵 Sentinel —— 本地只读监控面板",
    )
    parser.add_argument(
        "--watch",
        default=None,
        help="被监控的 Agent 日志路径（JSONL）；--demo 模式可省略",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="演示模式：自动生成一段模拟日志并审查它 —— 不接任何真实 Agent",
    )
    parser.add_argument(
        "--radar",
        action="store_true",
        help=(
            "雷达模式：只扫描你机器上已有的 Agent 会话日志 —— "
            "不生成任何模拟数据，也不监控任何进程"
        ),
    )
    parser.add_argument(
        "--alert-log",
        default=DEFAULT_ALERT_LOG,
        help="告警日志路径，默认 logs/sentinel_alert.log",
    )
    parser.add_argument(
        "--host", default=DEFAULT_HOST, help="监听地址，默认 127.0.0.1"
    )
    parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT, help="监听端口，默认 8000"
    )
    parser.add_argument(
        "--config", default=None, help="配置文件路径，默认 config/config.yaml"
    )
    parser.add_argument(
        "--no-scan",
        action="store_true",
        help="禁用内置扫描器（引擎单独运行时用）",
    )
    parser.add_argument(
        "--no-guardian",
        action="store_true",
        help="禁用守护者干预按钮（即使配置里开着）",
    )
    parser.add_argument(
        "--tail",
        action="store_true",
        help="实时流式监听模式：常驻阻塞监听日志，新事件即刻审查",
    )
    parser.add_argument(
        "--task-goal",
        default=None,
        help="任务目标（意图锚点），例如「重构 auth 模块并修复 bug」",
    )
    parser.add_argument(
        "--allow-remote",
        action="store_true",
        help="允许绑定非回环地址（危险，请确认网络环境）",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="启动后**不**自动打开浏览器（适合远程 / 无桌面环境）",
    )
    parser.add_argument(
        "--no-health",
        action="store_true",
        help="启动时**不**跑开箱安全体检（默认会跑一次）",
    )
    parser.add_argument(
        "--log-level", default="info", help="日志级别，默认 info"
    )
    args = parser.parse_args(argv)

    if not args.demo and not args.watch and not args.radar:
        parser.error(
            "必须给 --watch <日志路径>，或用 --demo 进演示模式，"
            "或用 --radar 开雷达。"
        )

    _prepare_streams()

    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if not _is_loopback_host(args.host) and not args.allow_remote:
        logger.error(
            "拒绝监听非回环地址 %s（如确有需要请显式加 --allow-remote）",
            args.host,
        )
        return 2

    # ---- 端口占用：给一句人话，而不是一屏红字异常栈 --------------------
    # 放在**扫描之前** —— 端口已经被占了就没必要再白等一轮审查。
    if not port_is_free(args.host, args.port):
        print_port_busy(args.host, args.port)
        print(f"[SENTINEL] 请改用别的端口，例如：--port {args.port + 1}")
        return 2

    # ---- 演示模式：造一段模拟日志 -------------------------------------
    # 用户第一次双击启动器时，多半还不知道哨兵长什么样。
    # 与其让人对着帮助文档猜，不如直接演给他看。
    demo_mode = bool(args.demo)
    radar_mode = bool(args.radar)
    watch_target = WatchTarget()
    if demo_mode:
        # --- 开箱安全体检（v2.2.1）---
        # ⚠️ 走 ``run_startup_health_check`` —— **一个进程只跑一次**。
        # exe 入口已经打过一份了；这里再打一次的话，
        # 终端上会出现两份统计，用户会以为哨兵出了两次问题。
        #
        # 这一段是为了让 ``启动哨兵.bat``（``python -m src.gui.server --demo``）
        # 这条路**也有**体检 —— 两边行为完全一致。
        try:
            if not args.no_health:
                from ..config_loader import load_config
                from ..core.health import run_startup_health_check

                run_startup_health_check(
                    load_config(args.config),
                    workdir=os.getcwd(),
                )
        except Exception as exc:  # noqa: BLE001 —— 体检失败不阻断启动
            logger.warning("开箱安全体检跳过：%s: %s", type(exc).__name__, exc)

        from ..demo import (
            DEMO_ALERT_LOG_NAME,
            DEMO_TASK_GOAL,
            generate_demo_log,
        )

        watch_path = generate_demo_log(os.path.join(PROJECT_ROOT, "logs"))
        alert_log_path = os.path.join(
            os.path.dirname(watch_path), DEMO_ALERT_LOG_NAME
        )
        args.task_goal = args.task_goal or DEMO_TASK_GOAL
        # 清空上一次的演示告警 —— 演示必须从干净状态开始，
        # 否则看板上堆着旧告警，用户会以为哨兵一直在报警。
        try:
            with open(alert_log_path, "w", encoding="utf-8"):
                pass
        except OSError as exc:
            logger.warning("清空演示告警日志失败：%s: %s", type(exc).__name__, exc)
        # 「当前监控目标」面板要显示得出东西，否则用户看到的又是一片空白。
        watch_target.set_agent("mcode")
        watch_target.set_goal(DEMO_TASK_GOAL, "auto")
        print("[SENTINEL] 演示模式：已生成模拟日志，开始审查……")
        print(f"[SENTINEL] 模拟日志：{watch_path}")
    elif radar_mode:
        # ---- 雷达模式（v3.0.2）----
        #
        # ⚠️ 雷达模式**不监控任何东西** ——
        # 它只负责「发现」你机器上已经在跑的 Agent 会话日志，
        # 让你能一眼看到「我正在跑的那些任务」，然后由你点【接入】。
        #
        # 所以这里给它一个**空的占位日志**：看板需要至少一个会话
        # 才能把三栏渲染出来，但那个会话里**永远不会有事件**。
        radar_log_dir = os.path.join(PROJECT_ROOT, "logs")
        try:
            os.makedirs(radar_log_dir, exist_ok=True)
        except OSError as exc:
            logger.warning("创建日志目录失败：%s: %s", type(exc).__name__, exc)
        watch_path = os.path.join(radar_log_dir, RADAR_IDLE_LOG_NAME)
        try:
            with open(watch_path, "a", encoding="utf-8"):
                pass
        except OSError as exc:
            logger.warning("创建雷达占位日志失败：%s: %s", type(exc).__name__, exc)
        alert_log_path = os.path.abspath(args.alert_log)
        print(
            "[SENTINEL] 雷达模式：只扫描你机器上已有的 Agent 会话日志，"
            "不监控任何进程。"
        )
    else:
        watch_path = os.path.abspath(args.watch)
        alert_log_path = os.path.abspath(args.alert_log)

    # 任务目标（意图锚点）：命令行优先于配置文件，看板里还能随时改。
    goal_holder = TaskGoal()
    try:
        from ..config_loader import load_config

        initial_goal = str(args.task_goal or load_config(args.config).get("task_goal") or "")
    except Exception as exc:  # noqa: BLE001 —— 配置读不到不影响启动
        logger.warning("读取配置中的 task_goal 失败：%s: %s", type(exc).__name__, exc)
        initial_goal = str(args.task_goal or "")
    goal_holder.set(initial_goal)

    scan_callback: Optional[Callable[[str], Any]] = None
    guardian: Optional[Any] = None
    if not args.no_guardian:
        try:
            guardian = _build_guardian(args.config, watch_path)
        except Exception as exc:  # noqa: BLE001 —— 守护者不可用不该阻断服务
            logger.warning("守护者不可用：%s: %s", type(exc).__name__, exc)
            guardian = None

    tail_runner: Optional[TailRunner] = None
    if demo_mode:
        # 演示日志是**一次性写好**的，所以走批处理扫描 ——
        # 看板一打开就已经有红线卡片了，不用等。
        scan_callback = _build_scan_callback(
            alert_log_path, args.config, guardian, goal_holder
        )
    elif args.tail:
        try:
            tail_runner = _build_tail_runner(
                watch_path, alert_log_path, args.config, guardian, goal_holder
            )
        except Exception as exc:  # noqa: BLE001 —— 监听不可用不该阻断服务
            logger.error("实时监听启动失败：%s: %s", type(exc).__name__, exc)
            return 2
        goal_holder.on_change(tail_runner.apply_goal)
        tail_runner.start()
        logger.info("实时流式监听已启动：%s", watch_path)
        logger.info("任务目标：%s", goal_holder.get() or "（未设置）")
        # tail 模式下一条事件只审一次 —— 「立即扫描」按钮改成提示，
        # 免得把整份历史日志再过一遍、导致告警重复。
        scan_callback = _tail_scan_stub
    elif not args.no_scan:
        scan_callback = _build_scan_callback(
            alert_log_path, args.config, guardian, goal_holder
        )

    # ---- 引擎心跳看门狗（v3.3.0）------------------------------------
    #
    # 守护 / 雷达模式下可能**一个会话都没接入**，
    # 那时没有引擎实例，但「本地引擎掉线」这件事
    # **仍然必须**出现在告警日志（看板靠它显示）与手机上。
    #
    # ⚠️ 已经有会话引擎注册过看门狗时**不要再加一个监听** ——
    #    同一个告警会写两遍（引擎一遍、系统一遍）。
    try:
        from ..config_loader import load_config as _load_config
        from ..core.engine import publish_system_alert
        from ..core.engine_watchdog import start_engine_watchdog

        _watchdog_config = _load_config(args.config)
        _watchdog = start_engine_watchdog(_watchdog_config)
        if _watchdog is not None:
            if tail_runner is None:
                _system_notifier = None
                try:
                    from ..core.notifier import Notifier

                    _system_notifier = Notifier(_watchdog_config)
                except Exception as exc:  # noqa: BLE001 —— 通知层坏了不阻断
                    logger.warning(
                        "系统告警通知层不可用：%s: %s", type(exc).__name__, exc
                    )

                def _publish_system_alert(alert: Dict[str, Any]) -> None:
                    """把引擎掉线告警发到日志 / 终端 / 手机。"""
                    publish_system_alert(
                        alert,
                        alert_log_path=alert_log_path,
                        notifier=_system_notifier,
                    )

                _watchdog.add_listener(_publish_system_alert)
            print(
                "[SENTINEL] 引擎心跳看门狗已启动"
                "（每 {} 秒探一次 {}）。".format(
                    int(_watchdog.interval), _watchdog.base_url
                ),
                flush=True,
            )
    except Exception as exc:  # noqa: BLE001 —— 看门狗失败不阻断服务
        logger.warning("引擎看门狗启动失败：%s: %s", type(exc).__name__, exc)

    # ---- 规则热重载（v2.3.0）-----------------------------------------
    #
    # 设置页的「保存并应用」要能**立刻**换掉规则，
    # 不该逼用户重启哨兵。常驻的实时监听引擎持有旧规则，
    # 所以要用热重载器把它的 ``rule_engine`` 原子换掉。
    hot_reloader: Optional[Any] = None
    runtime_config = _resolve_runtime_config_path(args.config)
    if runtime_config and os.path.isfile(runtime_config):
        try:
            from ..core.hotreload import RuleHotReloader

            hot_reloader = RuleHotReloader(
                config_path=runtime_config,
                engines=[],
                notify=lambda message: logger.info("%s", message),
            )
            hot_reloader.start()
        except Exception as exc:  # noqa: BLE001 —— 热重载不可用不该阻断服务
            logger.warning(
                "规则热重载器启动失败：%s: %s", type(exc).__name__, exc
            )
            hot_reloader = None
    if hot_reloader is not None and tail_runner is not None:
        hot_reloader.engines.append(tail_runner.engine)

    def _rebuild_cascades() -> str:
        """按最新配置**重建**级联编排器（v3.7.0）。

        L2 / L3 / 桥接开关改完，光换规则引擎是不够的 ——
        旧的编排器还攥着上一轮的开关，照旧跑等于没改。

        :return: 一句说明（没重建就返回空串）。
        """
        if tail_runner is None:
            return ""
        from ..config_loader import load_config

        config = load_config(runtime_config) if runtime_config else {}
        goal = ""
        try:
            goal = str(goal_holder.get() or "")
        except Exception as exc:  # noqa: BLE001 —— 目标读不出来只降级
            logger.warning("读取任务目标失败：%s: %s", type(exc).__name__, exc)
        try:
            tail_runner.engine.set_cascade(_build_cascade(config, goal))
        except Exception as exc:  # noqa: BLE001 —— 重建失败只降级
            logger.warning(
                "重建级联审核失败（已保留原编排器）：%s: %s",
                type(exc).__name__,
                exc,
            )
            return "级联编排器重建失败（已保留原设置）。"
        return "级联编排器已重建。"

    def _reload_config() -> Dict[str, Any]:
        """设置页「保存并应用」后触发热重载。

        v3.7.0 起**级联编排器也一起重建** ——
        L2 / L3 / 桥接开关变了，光换规则引擎是不够的。

        :return: ``{"ok": bool, "message": str}``。
        """
        cascade_note = _rebuild_cascades()
        if hot_reloader is not None:
            try:
                outcome = hot_reloader.reload_now()
            except Exception as exc:  # noqa: BLE001 —— 热重载失败只降级
                return {"ok": False, "message": "热重载失败：{}".format(exc)}
            if isinstance(outcome, dict):
                if cascade_note:
                    merged = dict(outcome)
                    merged["message"] = "{} {}".format(
                        merged.get("message") or "", cascade_note
                    ).strip()
                    return merged
                return outcome
            return {"ok": bool(outcome), "message": cascade_note}
        # 没有常驻引擎时：扫描回调每次都会**重新读配置**，
        # 所以「下一次扫描就会用新规则」—— 这话说清楚，别让用户猜。
        return {
            "ok": True,
            "message": "配置已保存，下一次扫描即用新规则。",
        }

    # ---- 雷达挂载（v2.3.0）------------------------------------------
    #
    # 哨兵不只是「启动器」，也是**雷达** ——
    # 用户早就在自己的终端里跑着 Agent 了，
    # 看板应该能发现它们、把日志接进来实时审查，
    # 而不是要求用户改习惯（必须从哨兵这儿启动）。
    attach_factory = _build_attach_factory(
        args.config,
        hot_reloader.engines if hot_reloader is not None else None,
    )

    # 演示模式：**先审一遍**，把告警写进告警日志 ——
    # 看板启动时会把历史告警预热出来，用户一打开就看得见。
    if demo_mode and scan_callback is not None:
        try:
            summary = scan_callback(watch_path)
            counts = summary.get("counts") or {}
            alerts = summary.get("alerts") or []
            print(
                f"[SENTINEL] 演示审查完成：事件 {summary.get('total_events', 0)} 条，"
                f"告警 {len(alerts)} 条（ON_TRACK={counts.get('ON_TRACK', 0)}，"
                f"DANGEROUS={counts.get('DANGEROUS', 0)}）。"
            )
        except Exception as exc:  # noqa: BLE001 —— 演示失败不该阻断看板
            logger.warning("演示审查失败：%s: %s", type(exc).__name__, exc)

    # ---- 挂载会话的持久化（v3.1.0）-----------------------------------
    #
    # 哨兵重启后自动把之前挂上的会话接回来。
    # 文件放在**哨兵自己的**配置目录里（``config/attached_sessions.json``）——
    # 绝不写进用户被监控的项目目录（红线 R1）。
    attach_store = AttachmentStore(
        default_attached_path(
            os.path.dirname(runtime_config) if runtime_config else ""
        )
    )
    logger.info("挂载历史文件：%s", attach_store.path)

    try:
        app = create_app(
            watch_path,
            alert_log_path,
            scan_callback=scan_callback,
            guardian_callback=guardian,
            task_goal=goal_holder,
            watch_target=watch_target,
            host=args.host,
            port=args.port,
            allow_remote=args.allow_remote,
            # 演示模式标志（v2.2.2）——
            # 看板据此挂红色横幅、把会话名改成「[演示] 默认会话」。
            demo_mode=demo_mode,
            # 雷达模式标志（v3.0.2）——
            # 看板据此**自动打开**【发现/挂载进程】面板。
            radar_mode=radar_mode,
            # 雷达挂载（v2.3.0）——
            # 「发现 / 挂载进程」按钮靠它把外部日志接进来。
            attach_factory=attach_factory,
            # 设置页（v2.3.0）—— 要读写的正是这份配置。
            config_path=args.config,
            reload_callback=_reload_config,
            # 挂载持久化（v3.1.0）—— 重启后自动接回来。
            attach_store=attach_store,
        )
    except ValueError as exc:
        logger.error("%s", exc)
        return 2

    import uvicorn

    logger.info("哨兵面板启动：http://%s:%s", args.host, args.port)
    logger.info("被监控日志：%s", watch_path)
    print_banner()
    if demo_mode:
        # ⚠️ 演示模式必须在终端**说破**（v2.2.2）——
        #
        # 看板上的红色横幅管的是「盯着网页看」的人，
        # 而双击 exe 的用户第一眼看的其实是**黑框终端**。
        # 两条路都得给同样的提示，否则总有人会被误导。
        print_demo_notice()
        # v3.0.1：再补一段「别关窗口」+ **反复**提醒 ——
        # 浏览器要等一会儿才打开，那个「一会儿」正是用户关窗口的时机。
        print_keep_open_notice()
        announce_demo_keep_open()
    elif radar_mode:
        # v3.0.2：雷达模式同样要「别关窗口」——
        # 浏览器要等一会儿才打开，终端一关雷达就断了。
        print_radar_keep_open_notice()
        announce_radar_keep_open()
    # 就绪提示与自动开浏览器都放到**后台线程**里做 ——
    # uvicorn.run() 是阻塞的，只有另起线程才能在服务真正起来之后再说「就绪」。
    announce_ready_when_up(
        args.host,
        args.port,
        version=__version__,
        open_browser=not args.no_browser,
    )

    try:
        # ⚠️ v3.0.1：不用 ``uvicorn.run()``，改用 ``Server`` 对象 ——
        #
        # 因为演示模式要能被**页面上的按钮**停下来：
        # ``Server.should_exit = True`` 是 uvicorn 文档里的正规做法，
        # 主循环下一轮就干净退出（不是强杀）。
        uvicorn_config = uvicorn.Config(
            app,
            host=args.host,
            port=args.port,
            log_level=str(args.log_level).lower(),
        )
        if demo_mode:
            _DEMO_SHUTDOWN["server"] = None
        # ⚠️ v3.4.0：**托盘图标**（右下角常驻）——
        #
        # 用户实测最痛的一条是「找不到退出的地方」。
        # 托盘给了他们一个**随时能按**的退出键：
        # 右键 →【彻底退出】。
        #
        # ⚠️ 托盘是**可选**的 —— pystray / Pillow 没装就不出图标，
        #    哨兵其它功能一点不受影响（红线 R5）。
        start_tray_icon(args.host, args.port)
        # ⚠️ v3.4.0：「关掉黑框」也要能退出 ——
        #
        # Windows 上关控制台窗口**默认什么都不做**，进程会变成
        # 用户说的「幽灵」。装上这个处理器之后，
        # 关窗 = 走同一条彻底退出的路。
        install_console_close_handler(_SHUTDOWN)
        # ⚠️ v3.3.0：**没有守护模式了** ——
        #    崩了就退出（用完即走），不后台静默、不自己复活。
        _serve(
            app,
            uvicorn_config,
            demo_mode=demo_mode,
        )
    except OSError as exc:
        # 端口在启动的那一瞬间被别人抢走了 —— 照样给一句人话。
        print_port_busy(args.host, args.port)
        logger.warning("看板启动失败（%s: %s）", type(exc).__name__, exc)
        return 2
    finally:
        _DEMO_SHUTDOWN["server"] = None
        if tail_runner is not None:
            tail_runner.stop()
        # ⚠️ v3.4.0：收尾 —— 托盘图标必须收掉。
        #
        # 不收的话托盘会**留在系统里**（图标还在，但点不动），
        # 那比「程序没退出」还怪。
        stop_tray_icon()
        _SHUTDOWN.bind_server(None)
    # ⚠️ v3.4.0：用户点了【彻底退出】之后，进程必须**真的没了**。
    #
    # 正常路径靠 Python 自己退出就行；
    # 但「彻底退出」这条路要的是**确定性** ——
    # 宁可强一点，也不能留一个杀不掉的残留（那正是用户最恨的）。
    if _SHUTDOWN.requested:
        _force_exit(0)
    return 0


if __name__ == "__main__":
    # ⚠️ 包一层 ``fatal_guard``（v2.2.1）——
    # 参数写错 / 端口被占这类致命错误，**不能让窗口闪退**。
    from ..console import fatal_guard

    raise SystemExit(fatal_guard(main))
