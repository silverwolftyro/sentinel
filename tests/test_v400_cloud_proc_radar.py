"""v4.0.0 回归：外部插件派发**进程树**雷达在看板侧的接线。

背景
====

v3.x 的雷达只扫**日志文件**。v4.0.0 加了一路：扫**进程** ——
找出外部插件的桥接宿主 → 枚举它派发出来的子孙 → 按命令行 / 工作目录
认出是哪个 Agent CLI → 关联工作目录下的日志。

本文件钉住三件事：

1. **发现** —— ``/api/discover`` 多返回 ``processes`` / ``process_count``；
2. **状态** —— 已经接入过的进程要标成 ``attached``（别让人重复接入）；
3. **一键挂载** —— 拿发现的 ``log_path`` 去 ``POST /api/attach``，
   要真的能挂上，挂完再扫一次要显示「已接入」。

⚠️ 端到端那几条会**真起进程树**（父进程拉起带特征的子进程），
   这是本任务要证明的核心行为。
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import pytest

from src.core import session_server as gui_server
from src.proc_tree import scan_process_tree

logger = logging.getLogger(__name__)

#: 静态资源目录（用来断言前端源码里的关键约定）。
STATIC_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "src",
    "gui",
    "static",
)

#: 假「桥接宿主」—— 拉起一个 Agent CLI 子进程，然后盯着它。
#:
#: ⚠️ 文件名里的 ``webui.server`` 就是**宿主特征** ——
#:    雷达靠它认出「这是外部插件的桥接宿主」。
_HOST_SCRIPT = '''"""假桥接宿主 —— 拉起一个 Agent CLI 子进程。"""
import subprocess
import sys
import time

child = subprocess.Popen(
    [sys.executable, sys.argv[1]],
    cwd=sys.argv[2],
)
try:
    while child.poll() is None:
        time.sleep(0.2)
finally:
    child.terminate()
'''

#: 假「Agent CLI」—— 什么也不干，就活着。
_AGENT_SCRIPT = '''"""假 Agent CLI —— 什么也不干，就活着。"""
import time

time.sleep(300)
'''


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------
def _kill_tree(proc: subprocess.Popen) -> None:
    """把一棵进程树拆干净（连子进程一起）。

    :param proc: 顶层进程。
    """
    try:
        import psutil
    except ImportError:  # pragma: no cover —— 没装 psutil 就退化成杀一个
        killer = getattr(proc, "kill", None)
        if killer is not None:
            killer()
        return
    try:
        parent = psutil.Process(proc.pid)
    except Exception as exc:  # noqa: BLE001 —— 进程早没了就没事了
        logger.debug("取宿主进程 %s 失败：%s", proc.pid, exc)
        return
    victims: List[Any] = []
    try:
        victims = parent.children(recursive=True)
    except Exception as exc:  # noqa: BLE001 —— 读不到子进程就只杀自己
        logger.debug("读子进程失败：%s", exc)
        victims = []
    for victim in victims:
        try:
            victim.terminate()
        except Exception as exc:  # noqa: BLE001 —— 已经死了就跳过
            logger.debug("终止子进程 %s 失败：%s", victim.pid, exc)
            continue
    try:
        parent.terminate()
    except Exception as exc:  # noqa: BLE001 —— 已经死了就跳过
        logger.debug("终止宿主 %s 失败：%s", parent.pid, exc)
        return
    _gone, alive = psutil.wait_procs(victims + [parent], timeout=3.0)
    for survivor in alive:
        try:
            survivor.kill()
        except Exception as exc:  # noqa: BLE001 —— 杀不掉就算了
            logger.debug("强杀 %s 失败：%s", survivor.pid, exc)
            continue


def _hermetic_config(tmp_path: Path) -> str:
    """造一份「只扫临时目录、什么都不排除」的配置文件。

    ⚠️ 两个目的：

    1. **快** —— 不去扫用户真实的 ``~/.codex`` / ``~/.claude``；
    2. **稳** —— 不依赖本机到底装了哪些 Agent。

    :param tmp_path: pytest 的临时目录。
    :return: 配置文件路径。
    """
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




@pytest.fixture(autouse=True)
def _reset_scan_cache():
    """每个用例前把进程树扫描缓存清空。

    ⚠️ 缓存是**模块级**的（5 秒 TTL），不清的话上一个用例塞进去的
    假数据会被下一个用例读走 —— 那样测的就不是真东西了。
    """
    gui_server._cloud_proc_cache["ready"] = False
    gui_server._cloud_proc_cache["at"] = 0.0
    gui_server._cloud_proc_cache["items"] = []
    yield
    gui_server._cloud_proc_cache["ready"] = False
    gui_server._cloud_proc_cache["at"] = 0.0
    gui_server._cloud_proc_cache["items"] = []


def _fake_process(pid: int = 4242, log_path: str = "") -> Dict[str, Any]:
    """造一条假的「外部插件派发进程」发现结果。"""
    return {
        "pid": pid,
        "parent_pid": 42,
        "host_pid": 42,
        "depth": 1,
        "agent": "claude",
        "agent_label": "Claude Code",
        "exe": "C:/x/claude.exe",
        "cmdline": "claude.exe",
        "cwd": "C:/x",
        "confidence": 0.95,
        "log_path": log_path,
        "attachable": bool(log_path),
        "state": "discovered",
        "matched_by": "cmdline_name",
    }










# ---------------------------------------------------------------------------
# 端到端：真进程树 → 发现 → 一键挂载
# ---------------------------------------------------------------------------
def _wait_for_process(
    predicate: Callable[[Any], bool],
    *,
    timeout: float = 40.0,
    interval: float = 0.4,
) -> Optional[Any]:
    """反复扫进程树，直到找到满足条件的那个（或超时）。

    :param predicate: 判定函数。
    :param timeout: 总超时（秒）。
    :param interval: 两次扫描之间的间隔（秒）。
    :return: 命中的进程；没命中是 ``None``。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for item in scan_process_tree():
            if predicate(item):
                return item
        time.sleep(interval)
    return None


@pytest.fixture()
def tree(tmp_path: Path):
    """拉起「宿主 → Agent CLI」进程树。

    :yield: ``(顶层进程, Agent 日志路径)``。
    """
    host_script = tmp_path / "webui.server.py"
    agent_script = tmp_path / "claude.py"
    host_script.write_text(_HOST_SCRIPT, encoding="utf-8")
    agent_script.write_text(_AGENT_SCRIPT, encoding="utf-8")
    log = tmp_path / "session.jsonl"
    log.write_text('{"type": "text", "text": "hi"}\n', encoding="utf-8")

    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.Popen(  # noqa: S603 —— 测试必须起真进程
        [
            sys.executable,
            str(host_script),
            str(agent_script),
            str(tmp_path),
        ],
        cwd=str(tmp_path),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=flags,
    )
    try:
        yield proc, log
    finally:
        _kill_tree(proc)
