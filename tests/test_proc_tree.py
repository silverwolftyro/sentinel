"""外部插件派发进程树雷达的测试（v4.0.0）。

分两层：

1. **纯函数层** —— 特征匹配、日志关联、摘要渲染；
2. **端到端层** —— 真的拉起一棵进程树（桥接宿主 → Agent CLI），
   让雷达自己把它扫出来。

⚠️ 端到端那几条会**真起进程**（这是本任务的重点：
   「父进程派生子进程」必须被扫到）。
   所以收尾一定把进程树拆干净，别给 CI 留孤儿进程。
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Optional

import pytest

from src.proc_tree import (
    CONFIDENCE_CMDLINE,
    CONFIDENCE_CWD,
    STATE_DISCOVERED,
    DiscoveredAgentProcess,
    find_log_for_process,
    match_agent,
    scan_process_tree,
    summarize_tree,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 端到端用的假脚本
# ---------------------------------------------------------------------------

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


def _kill_tree(proc: subprocess.Popen) -> None:
    """把一棵进程树拆干净（连子进程一起）。

    :param proc: 顶层进程。
    """
    try:
        import psutil
    except ImportError:  # pragma: no cover —— 没装 psutil 就退化成杀一个
        with_suppress = getattr(proc, "kill", None)
        if with_suppress is not None:
            with_suppress()
        return
    try:
        parent = psutil.Process(proc.pid)
    except Exception:  # noqa: BLE001 —— 进程早没了就没事了
        return
    victims = []
    try:
        victims = parent.children(recursive=True)
    except Exception:  # noqa: BLE001 —— 读不到子进程就只杀自己
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


# ---------------------------------------------------------------------------
# 特征匹配
# ---------------------------------------------------------------------------
class TestMatchAgent:
    """命令行 / 工作目录的 Agent 身份识别。"""

    def test_可执行名要完全相等(self) -> None:
        """``claude.exe`` 认成 Claude Code。"""
        agent, label, confidence = match_agent("claude.exe --resume")
        assert agent == "claude"
        assert "Claude" in label
        assert confidence == CONFIDENCE_CMDLINE

    def test_脚本名去掉扩展名也算(self) -> None:
        """``node.exe pi.js`` 认成 pi（插件是这么起的）。"""
        agent, _, confidence = match_agent(
            "node.exe C:/agents/opencode/bin/pi.js"
        )
        assert agent == "pi"
        assert confidence == CONFIDENCE_CMDLINE

    def test_别名要归一到规范id(self) -> None:
        """``oc`` 是 opencode 的别名，面板要的是规范 id。"""
        agent, _, _ = match_agent("oc run")
        assert agent == "opencode"

    def test_prompt里的别名不算命中(self) -> None:
        """用户下的一句 Prompt 不能把身份带偏。

        ``opencode run "把 oh-my-pi 修一下"`` —— 引号里的自然语言
        会被剔除，否则 ``oh-my-pi`` 会把它认成 pi。
        """
        agent, _, _ = match_agent('opencode run "把 oh-my-pi 修一下"')
        assert agent == "opencode"

    def test_工作目录是弱关联(self) -> None:
        """只有工作目录命中时，置信度要**低**。"""
        agent, _, confidence = match_agent("", "D:/neko/opencode/bin")
        assert agent == "opencode"
        assert confidence == CONFIDENCE_CWD

    def test_认不出就返回空(self) -> None:
        """什么都没有就老实说没有。"""
        agent, label, confidence = match_agent("notepad.exe", "C:/tmp")
        assert agent == ""
        assert label == ""
        assert confidence == 0.0


# ---------------------------------------------------------------------------
# 日志关联
# ---------------------------------------------------------------------------
class TestFindLogForProcess:
    """进程工作目录 → 日志文件。"""

    def test_挑最近改过的日志(self, tmp_path: Path) -> None:
        """同一个目录里多个日志，要挑**最近**改过的那个。"""
        old = tmp_path / "old.jsonl"
        new = tmp_path / "new.jsonl"
        old.write_text("{}\n", encoding="utf-8")
        new.write_text("{}\n", encoding="utf-8")
        stale = time.time() - 7200
        os.utime(old, (stale, stale))
        found = find_log_for_process(str(tmp_path))
        assert os.path.normcase(found) == os.path.normcase(str(new))

    def test_只认真的存在的文件(self, tmp_path: Path) -> None:
        """目录不算日志 —— 找不到就返回空串，绝不猜。"""
        (tmp_path / "sub").mkdir()
        assert find_log_for_process(str(tmp_path)) == ""

    def test_超出时间窗就没有(self, tmp_path: Path) -> None:
        """很久没动过的日志不算「正在跑的那个」。"""
        log = tmp_path / "old.jsonl"
        log.write_text("{}\n", encoding="utf-8")
        stale = time.time() - 7200
        os.utime(log, (stale, stale))
        assert find_log_for_process(str(tmp_path), max_age_seconds=60.0) == ""

    def test_目录不存在也不炸(self, tmp_path: Path) -> None:
        """路径不存在只能返回空串，不能抛异常。"""
        missing = str(tmp_path / "nope")
        assert find_log_for_process(missing) == ""


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------
class TestDiscoveredAgentProcess:
    """发现结果的数据结构。"""

    def test_字段齐全(self) -> None:
        """前端要拿这些字段渲染。"""
        item = DiscoveredAgentProcess(
            pid=11,
            parent_pid=1,
            host_pid=2,
            depth=1,
            agent="claude",
            agent_label="Claude Code（Anthropic）",
            exe="C:/x/claude.exe",
            cmdline="claude.exe",
            cwd="C:/x",
            confidence=0.95,
            log_path="C:/x/a.jsonl",
            attachable=True,
            state=STATE_DISCOVERED,
            matched_by="cmdline_name",
        )
        data = item.to_dict()
        assert data["pid"] == 11
        assert data["agent"] == "claude"
        assert data["attachable"] is True
        assert data["state"] == STATE_DISCOVERED
        assert data["matched_by"] == "cmdline_name"

    def test_摘要含关键字段(self) -> None:
        """给人看的摘要要说清「谁派的、什么 Agent、能不能挂载」。"""
        item = DiscoveredAgentProcess(
            pid=11,
            host_pid=2,
            depth=1,
            agent="claude",
            agent_label="Claude Code",
            log_path="C:/x/a.jsonl",
            attachable=True,
        )
        text = summarize_tree([item])
        assert "claude" in text
        assert "可挂载" in text
        assert "a.jsonl" in text


# ---------------------------------------------------------------------------
# 端到端
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


class TestProcessTreeEndToEnd:
    """真起一棵进程树，让雷达自己扫出来。"""

    @pytest.fixture()
    def tree(self, tmp_path: Path):
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

    def test_宿主派生的agent进程要被扫出来(self, tree) -> None:
        """**父进程派生子进程** —— 这是本功能的核心断言。

        宿主（命令行带 ``webui.server``）派生出 Agent CLI
        （可执行名 ``claude``），雷达必须：
        认出宿主 → 枚举子孙 → 认出 Agent → 关联到工作目录下的日志。
        """
        proc, _log = tree
        found = _wait_for_process(
            lambda item: item.agent == "claude"
            and item.host_pid == proc.pid
        )
        assert found is not None, "雷达没有扫出宿主派生的 Agent 进程"
        assert found.pid != proc.pid
        assert found.depth >= 1
        assert found.parent_pid == proc.pid
        assert found.matched_by.startswith("cmdline")

    def test_扫出来之后要能挂载(self, tree) -> None:
        """关联到**真实存在**的日志 → 才允许一键挂载（红线 R7）。"""
        proc, log = tree
        found = _wait_for_process(
            lambda item: item.agent == "claude"
            and item.host_pid == proc.pid
        )
        assert found is not None
        assert found.attachable is True
        assert os.path.normcase(found.log_path) == os.path.normcase(
            str(log)
        )


# ---------------------------------------------------------------------------
# 降级
# ---------------------------------------------------------------------------
class TestDegradation:
    """psutil 没装 / 扫不到时的降级。"""

    def test_扫不到就是空列表(self) -> None:
        """扫不到不能抛异常（红线 R5）。"""
        assert isinstance(scan_process_tree(), list)
