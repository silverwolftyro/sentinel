"""v4.0.1 回归：进程树扫描的**首发卡顿**治理。

背景
====

v4.0.0 实测：Windows 上**第一次**扫进程树要 4.2 秒 ——
冷启动时枚举几百个进程、逐个打开句柄，非常贵。
用户点开雷达面板就得盯着转圈等 4 秒 —— 这是实打实的卡顿。

v4.0.1 的三招：

1. **异步预热** —— 服务启动的后台线程里立刻静默调一次
   :func:`src.proc_tree.find_bridge_hosts`，把 psutil 的句柄缓存热起来；
2. **超时熔断** —— 扫描接口最多只等 1.5 秒，超时就返回**已经扫到的
   那部分**，状态标 ``scanning``，界面提示「正在深度检索进程…」；
3. **不阻塞主线程** —— 扫描永远跑在后台线程里，请求线程只等那 1.5 秒。

本文件钉住这三件事，外加前端契约。
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Any

import pytest

from src.core import session_server as gui_server
from src.proc_tree import DiscoveredAgentProcess

logger = logging.getLogger(__name__)

#: 静态资源目录（用来断言前端源码里的关键约定）。
STATIC_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "src",
    "gui",
    "static",
)


def _fake_agent(pid: int = 777) -> DiscoveredAgentProcess:
    """造一个「已发现的 Agent 进程」。"""
    return DiscoveredAgentProcess(
        pid=pid,
        parent_pid=42,
        host_pid=42,
        depth=1,
        agent="claude",
        agent_label="Claude Code",
        exe="C:/x/claude.exe",
        cmdline="claude.exe",
        cwd="C:/x",
        confidence=0.95,
        log_path="",
        attachable=False,
        state="discovered",
        matched_by="cmdline_name",
    )


def _fake_psutil() -> Any:
    """假的 psutil 模块（只够让扫描流程跑起来）。"""
    return object()


@pytest.fixture(autouse=True)
def _reset_scan_state():
    """每个用例前把进程树扫描的**状态机**清干净。

    ⚠️ 状态与缓存都是**模块级**的（跨用例共享），
    不清的话上一个用例塞进去的假结果会被下一个用例读走。
    """
    with gui_server._cloud_proc_lock:
        gui_server._cloud_proc_state.update(
            {
                "status": "idle",
                "started_at": 0.0,
                "finished_at": 0.0,
                "items": [],
                "error": "",
            }
        )
        gui_server._cloud_proc_cache.update(
            {"ready": False, "at": 0.0, "items": []}
        )
    gui_server._cloud_proc_event.clear()
    yield
    with gui_server._cloud_proc_lock:
        gui_server._cloud_proc_state["status"] = "idle"
        gui_server._cloud_proc_state["items"] = []
        gui_server._cloud_proc_cache["ready"] = False
        gui_server._cloud_proc_cache["items"] = []
    gui_server._cloud_proc_event.clear()


@pytest.fixture()
def release_scan():
    """给「慢扫描」一个放行开关（用例结束时别把线程吊死）。"""
    gate = threading.Event()
    yield gate
    gate.set()


def _hermetic_config(tmp_path: Path) -> str:
    """造一份「只扫临时目录、什么都不排除」的配置文件。"""
    empty_root = tmp_path / "empty-root"
    empty_root.mkdir(exist_ok=True)
    config = tmp_path / "radar.yaml"
    config.write_text(
        "radar:\n"
        "  scan_roots:\n"
        "    - {}\n"
        "  exclude_keywords: []\n".format(empty_root),
        encoding="utf-8",
    )
    return str(config)
