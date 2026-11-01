"""v4.2.0 回归：雷达「慢/待验证 · 疑似挂起」状态与派发层不退化。

背景
====
《Agent Bridge 资产盘点》报告建议先落地「阶段 A（数据层）」与
「阶段 B（派发层）」。v4.1.0 已经把这两步做完并发布。

v4.2.0 是它的**后续**：把雷达面板的「慢/待验证」判据按任务要求
收紧 —— 阈值 60 秒 → **10 秒**，措辞改成「疑似挂起」，
色系由琥珀（警示）改为**灰**（中性提醒）。

为什么 10 秒这一档必须是灰色
============================
10 秒只能证明「这个进程还没吐出第一行」，**不能断言它挂了**：

* 可能是 CLI 未登录时静默挂起（实测有的 CLI 会挂 40 秒以上）；
* 可能是它还在初始化（CLI 启动本来就慢）；
* 也可能它压根不往 cwd 写日志文件，输出只在 stdout 上。

所以：提示词写「疑似挂起」（留余地），颜色用灰（不是警示色），
tooltip 把三种可能都列出来。

本文件钉住以上全部行为，外加 v4.1.0 成果的**防回退**断言。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Dict

import pytest

from src import proc_tree
from src.agents import build_argv, load_agents
from src.agents.launcher import DEFAULT_OUTPUT_LIMIT, run_agent_process

#: 静态资源目录（用来断言前端源码里的关键约定）。
STATIC_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "src",
    "gui",
    "static",
)

#: 期望的 14 个 Agent id。
EXPECTED_IDS = {
    "atomcode",
    "claude",
    "codebuddy",
    "command-code",
    "dsh",
    "evox",
    "gemini",
    "hermes",
    "omp",
    "openclaw",
    "opencode",
    "pi",
    "qwenpaw",
    "step",
}

#: v4.1.0 补齐的 8 条别名 —— 这次改动不能把它们弄丢。
NEW_ALIASES = {
    "cmdc": "command-code",
    "commandcode": "command-code",
    "gcli": "gemini",
    "geminicli": "gemini",
    "hermes-agent": "hermes",
    "pi-coding-agent": "pi",
    "earendil-pi": "pi",
    "stepcode": "step",
}

#: v4.2.0 要求的慢状态阈值（秒）。
EXPECTED_SLOW_SECONDS = 10


@pytest.fixture(scope="module")
def agents() -> Dict[str, Any]:
    """加载 Agent 清单。"""
    return load_agents()


def _dashboard_js() -> str:
    """读前端主脚本。"""
    return Path(STATIC_DIR, "dashboard.js").read_text(encoding="utf-8")


def _dashboard_css() -> str:
    """读前端样式表。"""
    return Path(STATIC_DIR, "dashboard.css").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 任务三：慢/待验证 → 疑似挂起
# ---------------------------------------------------------------------------
def test_慢状态阈值是10秒() -> None:
    """任务要求：启动后 10 秒没有输出就提示疑似挂起。"""
    source = _dashboard_js()
    assert "var SLOW_PROCESS_SECONDS = 10;" in source
    assert "var SLOW_PROCESS_SECONDS = 60;" not in source


def test_慢状态徽章提示疑似挂起() -> None:
    """提示语必须是「疑似挂起」—— 断言，不是下结论。"""
    source = _dashboard_js()
    assert "疑似挂起" in source
    assert "慢/待验证" in source


def test_慢状态不再显示运行中() -> None:
    """这条路径**不能**再显示「运行中」—— 那会让人一直等。"""
    source = _dashboard_js()
    marker = "} else if (isSlowProcess(item)) {"
    assert marker in source
    block = source.split(marker)[1].split("} else {")[0]
    assert "疑似挂起" in block
    assert "运行中" not in block


def test_慢状态用灰色而不是警示色() -> None:
    """10 秒只能说明「还没动静」—— 用中性灰，不制造恐慌。"""
    css = _dashboard_css()
    block = css.split(".attach__badge--slow {")[1].split("}")[0]
    assert "var(--text-dim)" in block
    # 琥珀警示色不能留在这一档上。
    assert "#f0b45a" not in block
    assert "240, 180, 90" not in block


def test_慢状态提示列出三种可能() -> None:
    """tooltip 要把「未登录挂起 / 还在初始化 / 不写日志」都说到。"""
    source = _dashboard_js()
    assert "静默挂起" in source
    assert "初始化" in source
    assert "stdout" in source


def test_慢状态判据仍然是活着且没有日志() -> None:
    """判据本身不能被改坏：进程死了就不算「慢」。"""
    source = _dashboard_js()
    assert "item.attachable || item.attached" in source
    assert "alive >= SLOW_PROCESS_SECONDS" in source


# ---------------------------------------------------------------------------
# 防回退：v4.1.0 的数据层成果
# ---------------------------------------------------------------------------
def test_清单里还是14个Agent(agents: Dict[str, Any]) -> None:
    """这次是前端改动，数据层不该被动过。"""
    assert set(agents) == EXPECTED_IDS
    assert len(agents) == 14


def test_雷达别名表仍是16条() -> None:
    """补齐的 8 条别名不能被这次改动弄丢。"""
    assert len(proc_tree.CLOUD_AGENT_ALIASES) == 16
    for alias, expected in NEW_ALIASES.items():
        assert proc_tree.CLOUD_AGENT_ALIASES.get(alias) == expected, alias


def test_雷达仍能认出每个Agent的可执行名(
    agents: Dict[str, Any],
) -> None:
    """验收项：14 个 Agent 配置都能被正确识别。"""
    for agent_id, profile in agents.items():
        found, label, _ = proc_tree.match_agent(profile.binary)
        assert found == agent_id, agent_id
        assert label == profile.label, agent_id


def test_每个Agent都能拼出命令行(agents: Dict[str, Any]) -> None:
    """验收项：14 个 Agent 的启动模板都成立。"""
    for agent_id, profile in agents.items():
        argv = build_argv(profile, "干活")
        assert argv, agent_id
        assert argv[0] == profile.binary, agent_id
        assert "干活" in argv, agent_id


# ---------------------------------------------------------------------------
# 派发层不退化：输出管道不许被写满
# ---------------------------------------------------------------------------
def test_超大输出不会把子进程堵死() -> None:
    """验收项：拉起逻辑不会死锁。

    管道缓冲区只有 64 KB 左右。子进程往 stdout 写 2 MB，
    如果没人持续读，**它会永久阻塞** —— 这就是外部插件
    ``bridge/runner.py`` 里「输出管道必须持续读取」那条坑。

    这里用 2 MB 输出验证：后台线程确实在一直读。
    """
    payload = "x" * 2_000_000
    argv = [
        sys.executable,
        "-c",
        "import sys; sys.stdout.write('x' * 2000000)",
    ]
    outcome = run_agent_process(argv, timeout=60)
    assert not outcome.timed_out, "子进程被输出管道堵死了"
    assert outcome.exit_code == 0
    assert payload[:64] in outcome.stdout


def test_超大输出会被限幅() -> None:
    """限幅：不能把宿主的内存吃光。"""
    argv = [
        sys.executable,
        "-c",
        "import sys; sys.stdout.write('x' * 2000000)",
    ]
    outcome = run_agent_process(argv, timeout=60)
    assert len(outcome.stdout) <= DEFAULT_OUTPUT_LIMIT


def test_输出限幅后保留的是尾部() -> None:
    """留尾部：Agent 输出里最有价值的是最后那几句。"""
    argv = [
        sys.executable,
        "-c",
        (
            "import sys; "
            "sys.stdout.write('A' * 1000000); "
            "sys.stdout.write('TAIL-MARKER')"
        ),
    ]
    outcome = run_agent_process(argv, timeout=60)
    assert "TAIL-MARKER" in outcome.stdout
    # 头部被丢掉了 —— 保留的 A 远少于写出去的 100 万。
    assert outcome.stdout.count("A") < 300_000
    # 尾部标记确实落在最后。
    assert outcome.stdout.rstrip().endswith("TAIL-MARKER")


def test_超时后按时收手() -> None:
    """防回退：超时机制不能被数据层改动碰坏。"""
    import time

    if sys.platform == "win32":
        argv = ["cmd", "/d", "/s", "/c", "ping -n 30 127.0.0.1 > nul"]
    else:
        argv = ["sh", "-c", "sleep 30"]
    started = time.monotonic()
    outcome = run_agent_process(argv, timeout=2)
    cost = time.monotonic() - started
    assert outcome.timed_out
    assert cost < 20, "超时没按时收手：%.1f 秒" % cost
    assert not outcome.ok
