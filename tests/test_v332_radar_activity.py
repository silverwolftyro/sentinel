"""v3.3.2 回归：雷达的活跃进程感知 · 挂载管理页的快捷重定向命令。

背景
====

用户在自己的终端里跑着一个**真实的** MCode 任务，看板却抓不到它。
根因有两个：

1. **雷达扫的是文件** —— Agent 没把输出重定向成文件，雷达自然看不见；
2. **用户不知道该去哪儿找日志路径** —— 看板和终端彻底脱节。

v3.3.2 用两条路回应：

* **任务一**：雷达除了扫文件，还扫**进程** —— 发现「本机有活跃 Agent
  进程，却对不上正在监控的日志」时，在看板左侧给出高亮提示与引导；
* **任务二**：挂载管理页给出**针对当前这台机器**的「快捷重定向命令」，
  让用户下一次能直接用 ``> log.jsonl`` 的方式启动任务，从而被雷达捕获。

本文件钉住：

1. **进程识别** —— 必须「进程名 + 命令行特征」两层同时满足，
   否则满机的 Node 进程都会被算进来；
2. **「有没有被监控」的判定** —— 三条信号，从确定到推断；
3. **接口契约** —— ``/api/process_activity`` 的字段与降级行为；
4. **界面文案** —— 提示长什么样、点了之后去哪儿；
5. **快捷重定向命令** —— 生成的命令必须带 JSONL 开关与重定向。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.core import session_server as server
from src.procscan import (
    AgentProcess,
)

#: 仓库根目录（tests/ 的上一级）。
ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = ROOT / "src" / "gui" / "static"
TEMPLATE_DIR = ROOT / "src" / "gui" / "templates"


@pytest.fixture(autouse=True)
def _清活跃缓存():
    """每个用例都从「没缓存」开始。

    「活跃进程感知」带 8 秒缓存 —— 不清的话前一个用例的结果
    会直接被下一个用例读走，用例之间互相干扰。
    """

    server._activity_cache["at"] = 0.0
    server._activity_cache["data"] = {}
    yield
    server._activity_cache["at"] = 0.0
    server._activity_cache["data"] = {}


def _read(*parts) -> str:
    """读仓库里的一个文本文件。

    :param parts: 相对仓库根的路径片段。
    :return: 文件文本。
    """
    path = ROOT.joinpath(*parts)
    return path.read_text(encoding="utf-8")


















# ---------------------------------------------------------------------------
# 二、「有没有被监控」的三条信号
# ---------------------------------------------------------------------------
def _proc(**kwargs) -> AgentProcess:
    """造一个 :class:`AgentProcess` 测试替身。

    :param kwargs: 覆盖默认字段。
    :return: 进程对象。
    """
    base = {
        "pid": 1234,
        "agent": "mcode",
        "exe": "C:\\node.exe",
        "name": "node.exe",
        "cwd": "D:\\项目\\demo",
        "cmdline": "node.exe cli.js --continue",
    }
    base.update(kwargs)
    return AgentProcess(**base)
