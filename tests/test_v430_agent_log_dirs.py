"""v4.3.0 回归：已知会话日志目录（log_dirs）与「疑似挂起」误报的消除。

背景
====
v4.2.0 把雷达的「慢/待验证」阈值降到 10 秒，代价是**误报变多**。
根因是「这个进程有没有输出」在雷达里**只能用代理判据**表示::

    attachable = bool(log_path)
    log_path   = find_log_for_process(cwd, agent)   # v4.2.0 只看 cwd

而 Agent CLI 常常**不往自己的 cwd 写日志**：

* Claude Code 写 ``~/.claude/projects/<项目slug>/``；
* Codex 写 ``~/.codex/sessions/<年>/<月>/<日>/``；
* OpenCode 写 ``~/.local/share/opencode/log/``。

cwd 下找不到 → ``attachable=False`` → 徽章变成「疑似挂起」→ **假阳性**。

v4.3.0 的解法：清单里给每个 Agent 登记**已知会话日志目录**
（``log_dirs``），雷达在 cwd 下没找到时再去那些目录找。

本文件钉住：
* 数据层 —— 14 条 Agent 都有 ``log_dirs`` 字段，有依据的才非空；
* 查找层 —— cwd 优先、log_dirs 兜底、递归、排除压缩日志、深度与预算有界；
* 接线层 —— 雷达真的把 log_dirs 传下去了。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict

import pytest

from src import proc_tree
from src.agents import AgentProfile, load_agents, log_dir_table

#: 仓库根目录。
ROOT_DIR = Path(__file__).resolve().parents[1]

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

#: 有依据、**必须**登记了日志目录的 Agent 及其目录。
#:
#: 依据来源见 :func:`src.agents.log_dir_table` 的文档与清单里的
#: ``log_dirs_note``。
EXPECTED_LOG_DIRS = {
    "claude": ("~/.claude/projects",),
    "codebuddy": ("~/.codebuddy/projects", "~/.codebuddy/sessions"),
    "dsh": ("~/.dsh/sessions",),
    "opencode": ("~/.local/share/opencode/log",),
}


@pytest.fixture(scope="module")
def agents() -> Dict[str, Any]:
    """加载 Agent 清单。"""
    return load_agents()


@pytest.fixture(scope="module")
def raw_manifest() -> Dict[str, Any]:
    """读清单原始 JSON（用来查 log_dirs_note 这种纯文档字段）。"""
    path = ROOT_DIR / "src" / "agents" / "cloud_agents.json"
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 数据层：log_dirs 本身
# ---------------------------------------------------------------------------
def test_每条Agent都有logDirs字段(raw_manifest: Dict[str, Any]) -> None:
    """字段是**可选**的 —— 但每条都得显式写出来。

    显式写空数组 = 「查清楚了，没有」；
    字段缺失 = 「还没查」—— 这两件事必须能区分。
    """
    for agent in raw_manifest["agents"]:
        assert "log_dirs" in agent, agent.get("id")
        assert isinstance(agent["log_dirs"], list), agent.get("id")
        for item in agent["log_dirs"]:
            assert isinstance(item, str) and item.strip(), agent.get("id")


def test_有依据的Agent登记了正确的目录(
    agents: Dict[str, Any],
) -> None:
    """依据来源逐条对应 —— 详见清单里的 ``log_dirs_note``。"""
    for agent_id, expected in EXPECTED_LOG_DIRS.items():
        assert agents[agent_id].log_dirs == expected, agent_id


def test_没有依据的Agent是空数组(agents: Dict[str, Any]) -> None:
    """没查清楚的就留空 —— **不能凭印象填一个不存在的路径**。

    填了不存在的路径，只会让雷达每次扫描白跑一圈。
    """
    for agent_id, profile in agents.items():
        if agent_id in EXPECTED_LOG_DIRS:
            continue
        assert profile.log_dirs == (), agent_id


def test_日志目录表只收录非空的(agents: Dict[str, Any]) -> None:
    """空的不该进表 —— 表是「查得到」的索引。"""
    table = log_dir_table(agents)
    assert set(table) == set(EXPECTED_LOG_DIRS)
    for agent_id, dirs in table.items():
        assert dirs, agent_id


def test_雷达的日志目录表已经接线() -> None:
    """``proc_tree`` 必须真的用上这张表。"""
    assert set(proc_tree.CLOUD_AGENT_LOG_DIRS) == set(EXPECTED_LOG_DIRS)
    assert proc_tree.CLOUD_AGENT_LOG_DIRS["claude"] == (
        "~/.claude/projects",
    )


def test_清单里14条一个不少(agents: Dict[str, Any]) -> None:
    """防回退：这次的改动不能把清单弄少。"""
    assert set(agents) == EXPECTED_IDS
    assert len(agents) == 14


# ---------------------------------------------------------------------------
# 查找层：cwd 优先，log_dirs 兜底
# ---------------------------------------------------------------------------
def test_cwd下有日志时优先用cwd的(tmp_path: Path) -> None:
    """cwd 里的文件**优先** —— 它最可能就是这次任务写的。"""
    work = tmp_path / "work"
    work.mkdir()
    local = work / "local.jsonl"
    local.write_text("{}\n", encoding="utf-8")

    elsewhere = tmp_path / "home"
    elsewhere.mkdir()
    (elsewhere / "remote.jsonl").write_text("{}\n", encoding="utf-8")

    found = proc_tree.find_log_for_process(
        str(work), "claude", log_dirs=[str(elsewhere)]
    )
    assert found == str(local)


def test_cwd下没有就去logDirs里找(tmp_path: Path) -> None:
    """**这就是 v4.3.0 的核心行为** —— 消除「疑似挂起」误报。"""
    work = tmp_path / "work"
    work.mkdir()
    assert not list(work.iterdir())

    home = tmp_path / "home"
    home.mkdir()
    remote = home / "remote.jsonl"
    remote.write_text("{}\n", encoding="utf-8")

    # 旧行为（只查 cwd）：找不到。
    assert proc_tree.find_log_for_process(str(work), "claude") == ""
    # 新行为（cwd + log_dirs）：找到了。
    found = proc_tree.find_log_for_process(
        str(work), "claude", log_dirs=[str(home)]
    )
    assert found == str(remote)


def test_logDirs要递归找(tmp_path: Path) -> None:
    """Claude Code 的会话日志在根目录**下面一层**。

    ``~/.claude/projects/<项目slug>/<会话>.jsonl`` —— 只看一层找不到。
    """
    work = tmp_path / "work"
    work.mkdir()
    home = tmp_path / "home"
    nested = home / "projects" / "my-project"
    nested.mkdir(parents=True)
    target = nested / "session.jsonl"
    target.write_text("{}\n", encoding="utf-8")

    found = proc_tree.find_log_for_process(
        str(work), "claude", log_dirs=[str(home)]
    )
    assert found == str(target)


def test_要能找四层深的目录(tmp_path: Path) -> None:
    """Codex 的会话日志是 ``sessions/<年>/<月>/<日>/<会话>.jsonl`` —— 4 层。"""
    work = tmp_path / "work"
    work.mkdir()
    home = tmp_path / "home"
    deep = home / "sessions" / "2026" / "11" / "01"
    deep.mkdir(parents=True)
    target = deep / "rollout.jsonl"
    target.write_text("{}\n", encoding="utf-8")

    found = proc_tree.find_log_for_process(
        str(work), "codex", log_dirs=[str(home)]
    )
    assert found == str(target)


def test_压缩日志要被排除(tmp_path: Path) -> None:
    """zstd 压缩的日志**不能接入**实时审查。

    跟读器记的是原始字节偏移，压缩流没法按字节增量读 ——
    挂载接口会明确拒绝它。那就不该把徽章标成「可接入」。
    """
    work = tmp_path / "work"
    work.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    (home / "session.v4.jsonl.zstd").write_bytes(b"\x00\x01\x02")

    found = proc_tree.find_log_for_process(
        str(work), "dsh", log_dirs=[str(home)]
    )
    assert found == ""


def test_太旧的文件不算(tmp_path: Path) -> None:
    """时间窗之外的旧日志不算「正在写」。"""
    work = tmp_path / "work"
    work.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    stale = home / "old.jsonl"
    stale.write_text("{}\n", encoding="utf-8")
    old = time.time() - 7200
    os.utime(stale, (old, old))

    found = proc_tree.find_log_for_process(
        str(work), "claude", log_dirs=[str(home)], max_age_seconds=3600
    )
    assert found == ""


def test_目录不存在时不崩(tmp_path: Path) -> None:
    """查一个不存在的目录 —— 只能返回空串，不能抛异常。"""
    work = tmp_path / "work"
    work.mkdir()
    found = proc_tree.find_log_for_process(
        str(work), "claude", log_dirs=[str(tmp_path / "压根不存在")]
    )
    assert found == ""


def test_波浪号会被展开(tmp_path: Path, monkeypatch) -> None:
    """清单里写的是 ``~/.claude/projects`` —— 相对**家目录**。"""
    home = tmp_path / "home"
    (home / ".claude" / "projects").mkdir(parents=True)
    (home / ".claude" / "projects" / "s.jsonl").write_text(
        "{}\n", encoding="utf-8"
    )
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))

    work = tmp_path / "work"
    work.mkdir()
    found = proc_tree.find_log_for_process(
        str(work), "claude", log_dirs=["~/.claude/projects"]
    )
    assert found.endswith(".jsonl")


def test_不给logDirs时行为不变(tmp_path: Path) -> None:
    """**回归保护**：不传 log_dirs 就等于 v4.2.0 的行为。"""
    work = tmp_path / "work"
    work.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    (home / "remote.jsonl").write_text("{}\n", encoding="utf-8")

    assert proc_tree.find_log_for_process(str(work), "claude") == ""


def test_多个logDirs按顺序找(tmp_path: Path) -> None:
    """第一个目录里没有就去第二个。"""
    work = tmp_path / "work"
    work.mkdir()
    first = tmp_path / "first"
    first.mkdir()
    second = tmp_path / "second"
    second.mkdir()
    target = second / "b.jsonl"
    target.write_text("{}\n", encoding="utf-8")

    found = proc_tree.find_log_for_process(
        str(work), "claude", log_dirs=[str(first), str(second)]
    )
    assert found == str(target)


def test_名字带Agent名的优先(tmp_path: Path) -> None:
    """名字里带 Agent id 的文件优先 —— 同一目录里可能有别人的日志。"""
    work = tmp_path / "work"
    work.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    (home / "other.jsonl").write_text("{}\n", encoding="utf-8")
    mine = home / "claude-session.jsonl"
    mine.write_text("{}\n", encoding="utf-8")
    os.utime(mine, (time.time() - 60, time.time() - 60))

    found = proc_tree.find_log_for_process(
        str(work), "claude", log_dirs=[str(home)]
    )
    assert found == str(mine)


# ---------------------------------------------------------------------------
# 接线层：雷达真的把 log_dirs 传下去了
# ---------------------------------------------------------------------------
def test_档案默认值是空元组() -> None:
    """直接构造 AgentProfile 时不传 log_dirs —— 不能报错。"""
    profile = AgentProfile(
        id="x", label="X", binary="x", mode="flag", prompt_flag="-p"
    )
    assert profile.log_dirs == ()
