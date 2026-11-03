"""v4.1.0 回归：Agent 扩展的**数据层**与**派发层**加固。

背景
====

《Agent Bridge 资产盘点》报告建议：先做成本最低、收益最高的
「阶段 A（数据层）」与「阶段 B（派发层）」，把哨兵的 Agent 扩展机制
彻底**数据化**，暂不做高成本的多格式解析器。

本次落地三件事：

1. **数据层** —— 新增 :mod:`src.agents` 包，
   14 个外部插件 Agent 的配置从 ``src/agents/cloud_agents.json`` 加载；
   ``src/proc_tree.py`` 的三张硬编码表改成动态加载，
   **补齐此前缺失的 8 条别名**。
2. **派发层** —— 吸收上游 ``bridge/runner.py`` 的四条避坑经验
   （stdin DEVNULL / 超时连进程树一起清 / 输出管道持续读取但限幅 /
   不靠退出码判成败），并引入**鉴权识别两层机制**。
3. **前端** —— 雷达面板新增「慢/待验证」状态；
   会话列表与实时流水区改成「签名没变就不重建」。

本文件钉住以上全部行为，外加前端契约。
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict

import pytest

from src import proc_tree
from src.agents import (
    AgentConfigError,
    build_argv,
    canonical_agent,
    label_table,
    load_agents,
    resolve_executable,
)
from src.agents.auth import (
    detect_auth_failure,
    detect_runtime_failure,
    has_substantive_output,
)
from src.agents.launcher import BoundedBuffer, run_agent_process

logger = logging.getLogger(__name__)

#: 静态资源目录（用来断言前端源码里的关键约定）。
STATIC_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "src",
    "gui",
    "static",
)

#: 仓库根目录。
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

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

#: 此前哨兵**没有**、本次必须补齐的 8 条别名。
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

_IS_WINDOWS = sys.platform == "win32"


@pytest.fixture(scope="module")
def agents() -> Dict[str, Any]:
    """加载 Agent 清单。"""
    return load_agents()


# ---------------------------------------------------------------------------
# 数据层：清单本身
# ---------------------------------------------------------------------------
def test_清单能加载出14个Agent(agents: Dict[str, Any]) -> None:
    """数据文件里必须正好是 14 个 Agent。"""
    assert set(agents) == EXPECTED_IDS
    assert len(agents) == 14


def test_每个Agent的mode与promptFlag自洽(agents: Dict[str, Any]) -> None:
    """``flag`` / ``switch`` 必须有开关，``positional`` 必须没有。"""
    for agent_id, profile in agents.items():
        if profile.mode in ("flag", "switch"):
            assert profile.prompt_flag, f"{agent_id} 缺 prompt_flag"
        else:
            assert profile.mode == "positional"
            assert not profile.prompt_flag, f"{agent_id} 不该有 prompt_flag"


def test_每个Agent都有可执行名与展示名(agents: Dict[str, Any]) -> None:
    """可执行名与展示名是雷达显示的最低要求。"""
    for agent_id, profile in agents.items():
        assert profile.binary, f"{agent_id} 缺 binary"
        assert profile.label, f"{agent_id} 缺 label"
        assert profile.exec_names[0] == profile.binary


def test_jsonArgs与jsonShape必须同时声明(agents: Dict[str, Any]) -> None:
    """``json_args`` 与 ``json_shape`` 要么都有，要么都没有。"""
    for agent_id, profile in agents.items():
        if profile.json_args:
            assert profile.json_shape != "none", agent_id
        else:
            assert profile.json_shape == "none", agent_id


def test_清单Schema不对时必须报错(tmp_path: Path) -> None:
    """坏清单要**立刻报错**，不能悄悄按空表跑。"""
    bad = tmp_path / "provider.json"
    bad.write_text(
        json.dumps({"schema": 99, "agents": []}), encoding="utf-8"
    )
    with pytest.raises(AgentConfigError):
        load_agents(bad)


def test_清单缺失时降级成空表不抛异常(tmp_path: Path) -> None:
    """``strict=False`` 时清单缺失只降级 —— 打包漏文件不能起不来。"""
    assert load_agents(tmp_path / "nope.json", strict=False) == {}


def test_别名冲突必须报错(tmp_path: Path) -> None:
    """同一个别名挂在两个 Agent 上是数据错误，必须拦下。"""
    bad = tmp_path / "provider.json"
    bad.write_text(
        json.dumps(
            {
                "schema": 1,
                "agents": [
                    {
                        "id": "aaa",
                        "binary": "aaa",
                        "mode": "flag",
                        "prompt_flag": "-p",
                        "aliases": ["same"],
                    },
                    {
                        "id": "bbb",
                        "binary": "bbb",
                        "mode": "flag",
                        "prompt_flag": "-p",
                        "aliases": ["same"],
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(AgentConfigError):
        load_agents(bad)


# ---------------------------------------------------------------------------
# 数据层：别名归一
# ---------------------------------------------------------------------------
def test_雷达能认出新增的8个别名(agents: Dict[str, Any]) -> None:
    """此前这 8 条别名哨兵一概认不出 —— 本次必须全部归一。"""
    for alias, expected in NEW_ALIASES.items():
        assert canonical_agent(alias, agents) == expected, alias


def test_雷达别名表已经变成动态加载() -> None:
    """``proc_tree`` 的三张表必须来自数据文件，而不是硬编码。"""
    assert len(proc_tree.CLOUD_AGENT_ALIASES) == 16
    for alias, expected in NEW_ALIASES.items():
        assert proc_tree.CLOUD_AGENT_ALIASES.get(alias) == expected, alias


def test_雷达可执行名表覆盖全部14个Agent(
    agents: Dict[str, Any],
) -> None:
    """可执行名表要覆盖 14 个 Agent。"""
    assert set(proc_tree.CLOUD_AGENT_BINARIES) == {
        profile.binary for profile in agents.values()
    }


def test_旧有的8个别名没有丢() -> None:
    """补齐新别名不能把老的弄丢。"""
    for alias, expected in {
        "cbc": "codebuddy",
        "codebuddy-code": "codebuddy",
        "oh-my-pi": "omp",
        "ompi": "omp",
        "oc": "opencode",
        "claw": "openclaw",
        "atom": "atomcode",
        "qwen": "qwenpaw",
    }.items():
        assert proc_tree.CLOUD_AGENT_ALIASES.get(alias) == expected, alias


def test_按可执行名也能归一(agents: Dict[str, Any]) -> None:
    """可执行名本身就是一种写法。"""
    assert canonical_agent("gemini", agents) == "gemini"
    assert canonical_agent("command-code", agents) == "command-code"


def test_认不出来的名字返回空(agents: Dict[str, Any]) -> None:
    """认不出就说认不出 —— 不能瞎猜一个。"""
    assert canonical_agent("完全不存在的cli", agents) is None
    assert canonical_agent("", agents) is None


def test_雷达能从未知命令认出新Agent() -> None:
    """``match_agent`` 走的是别名路径，新别名必须能认出。"""
    agent_id, label, confidence = proc_tree.match_agent("cmdc -p 干活")
    assert agent_id == "command-code"
    assert label
    assert confidence > 0

    agent_id, label, _ = proc_tree.match_agent("gcli -p 干活")
    assert agent_id == "gemini"


# ---------------------------------------------------------------------------
# 数据层：argv 构造
# ---------------------------------------------------------------------------
def test_flag形态的argv(agents: Dict[str, Any]) -> None:
    """``flag`` 形态：可执行名 + 开关 + 正文。"""
    argv = build_argv(agents["claude"], "干活")
    assert argv == ["claude", "-p", "干活"]


def test_positional形态的argv(agents: Dict[str, Any]) -> None:
    """``positional`` 形态：正文紧跟固定参数。"""
    argv = build_argv(agents["dsh"], "干活")
    assert argv == ["dsh", "--profile", "headless", "干活"]

    argv = build_argv(agents["opencode"], "干活")
    assert argv == ["opencode", "run", "干活"]


def test_switch形态的argv(agents: Dict[str, Any]) -> None:
    """``switch`` 形态与 ``flag`` 拼出来一样（开关不吃值）。"""
    argv = build_argv(agents["command-code"], "干活")
    assert argv == ["command-code", "-p", "干活"]


def test_模型参数插在正文之后(agents: Dict[str, Any]) -> None:
    """模型参数的位置要跟外部插件一致。"""
    argv = build_argv(agents["gemini"], "干活", model="gpt-5")
    assert argv == ["gemini", "-p", "干活", "-m", "gpt-5"]


def test_json开关默认不生效(agents: Dict[str, Any]) -> None:
    """默认**不开**机器可读输出 —— 那是显式选择。"""
    argv = build_argv(agents["gemini"], "干活")
    assert "-o" not in argv

    argv = build_argv(agents["gemini"], "干活", json_output=True)
    assert argv[-2:] == ["-o", "json"]


def test_额外参数追加在最后(agents: Dict[str, Any]) -> None:
    """``extra_args`` 永远在最后。"""
    argv = build_argv(agents["pi"], "干活", extra_args=["--no-skills"])
    assert argv[-1] == "--no-skills"


def test_include_exe_false时不带可执行名(agents: Dict[str, Any]) -> None:
    """哨兵自己拼可执行名时用这个形态。"""
    argv = build_argv(agents["pi"], "干活", include_exe=False)
    assert argv == ["-p", "干活"]


def test_展示名表覆盖全部Agent(agents: Dict[str, Any]) -> None:
    """展示名表要能给雷达用。"""
    table = label_table(agents)
    assert len(table) == 14
    assert table["step"] == "StepCode CLI（阶跃星辰）"


# ---------------------------------------------------------------------------
# 鉴权识别（两层机制）
# ---------------------------------------------------------------------------
def test_全局表认出未登录() -> None:
    """全局兜底表要覆盖通用措辞。"""
    hit = detect_auth_failure(
        "", "Authentication required. Please use /login command to sign in"
    )
    assert "Authentication required" in hit


def test_中文未登录措辞也认得出() -> None:
    """中文措辞同样要认出来。"""
    hit = detect_auth_failure("", "错误：未登录，请先登录")
    assert hit


def test_每Agent的专属措辞能认出来(agents: Dict[str, Any]) -> None:
    """第二层：每 Agent 自带的措辞。"""
    profile = agents["command-code"]
    hit = detect_auth_failure(
        "",
        'Error: Authentication failed. Please run "cmdc login" first.',
        profile.auth_patterns,
    )
    assert "Authentication failed" in hit


def test_gemini的专属措辞能认出来(agents: Dict[str, Any]) -> None:
    """gemini 的措辞与全局表不重叠，靠第二层兜住。"""
    profile = agents["gemini"]
    hit = detect_auth_failure(
        "",
        "Please set an Auth method in your settings.json",
        profile.auth_patterns,
    )
    assert "Auth method" in hit


def test_stderr太长时不判未登录() -> None:
    """真跑完任务的 Agent stderr 会长 —— 长输出不是登录提示。"""
    stderr = "Authentication required\n" + ("x" * 2000)
    assert detect_auth_failure("", stderr) == ""


def test_stdout有正文时不判未登录() -> None:
    """stdout 有正文说明它**真的在干活**，不该判成未登录。"""
    stderr = "Authentication required. Please use /login command to sign in"
    stdout = "我已经完成了任务，详见下面的报告。"
    assert detect_auth_failure(stdout, stderr) == ""


def test_实质输出判定会滤掉噪声行() -> None:
    """警告行不算实质输出。"""
    assert not has_substantive_output("warning: something\n(node:123) abc\n")
    assert has_substantive_output("这是一段足够长的正文内容。")


def test_运行期失败与未登录要分开(agents: Dict[str, Any]) -> None:
    """hermes 的 gateway 措辞属于**运行期失败**，不是没登录。"""
    profile = agents["hermes"]
    stderr = "Error: Gateway inaccessible: service_manager_unavailable"
    assert detect_runtime_failure("", stderr, profile.fail_patterns)
    assert not detect_auth_failure("", stderr, profile.auth_patterns)


# ---------------------------------------------------------------------------
# 派发层加固
# ---------------------------------------------------------------------------
def test_限幅缓冲只留尾部() -> None:
    """超限要从**头部**丢 —— 最后那几句才是有用的。"""
    buf = BoundedBuffer(100)
    buf.append("x" * 10000)
    assert len(buf.value()) == 100
    assert buf.truncated
    assert buf.dropped == 9900


def test_限幅缓冲多块追加() -> None:
    """分多次追加也要限住。"""
    buf = BoundedBuffer(50)
    for _ in range(20):
        buf.append("y" * 30)
    assert len(buf.value()) <= 50
    assert buf.truncated


def test_能拉起最简单的echo类命令() -> None:
    """验收项：派发器要能真的把进程拉起来并拿到输出。"""
    if _IS_WINDOWS:
        argv = ["cmd", "/d", "/s", "/c", "echo hello-sentinel"]
    else:
        argv = ["sh", "-c", "echo hello-sentinel"]
    outcome = run_agent_process(argv, timeout=30)
    assert outcome.exit_code == 0
    assert "hello-sentinel" in outcome.stdout
    assert outcome.ok
    assert not outcome.timed_out


def test_stdin必须是DEVNULL() -> None:
    """子进程不能继承宿主 stdin —— 否则会跟宿主抢管道。"""
    from src.agents import launcher

    if _IS_WINDOWS:
        argv = ["cmd", "/d", "/s", "/c", "more"]
    else:
        argv = ["sh", "-c", "cat"]
    proc = launcher.spawn_agent(argv)
    try:
        assert proc.stdin is None
    finally:
        try:
            proc.kill()
        except OSError:
            pass
        proc.wait(timeout=10)


def test_超时会按时返回而不是干等() -> None:
    """超时要**按时**收手，并连带子进程树一起清。"""
    if _IS_WINDOWS:
        argv = ["cmd", "/d", "/s", "/c", "ping -n 30 127.0.0.1 > nul"]
    else:
        argv = ["sh", "-c", "sleep 30"]
    started = time.monotonic()
    outcome = run_agent_process(argv, timeout=2)
    cost = time.monotonic() - started
    assert outcome.timed_out
    assert cost < 20, "超时没按时收手：%.1f 秒" % cost
    assert not outcome.ok


def test_环境变量清洗掉Python系列() -> None:
    """哨兵打包成 exe 后不能把 PYTHONHOME 传给 Agent 自己的解释器。"""
    from src.agents.launcher import ENV_STRIP, sanitize_env

    env = sanitize_env({"PYTHONHOME": "x", "FOO": "bar"})
    assert "PYTHONHOME" not in env
    assert env["FOO"] == "bar"
    assert "PYTHONPATH" in ENV_STRIP


def test_拉起失败不算崩() -> None:
    """命令不存在时只降级，不抛异常。"""
    outcome = run_agent_process(["哨兵-不存在的命令-xyz"], timeout=10)
    assert outcome.exit_code is None
    assert not outcome.ok
    assert "拉起失败" in outcome.stderr


# ---------------------------------------------------------------------------
# 通用 text 适配器
# ---------------------------------------------------------------------------
def test_text适配器把纯文本行转成事件() -> None:
    """纯文本行必须能进审查流。"""
    from src.adapters.text import TextAdapter

    adapter = TextAdapter()
    events = adapter.parse_line("我完成了一件事")
    assert len(events) == 1
    assert events[0]["type"] == "text"
    assert events[0]["text"] == "我完成了一件事"


def test_text适配器跳过空行() -> None:
    """空行不是事件。"""
    from src.adapters.text import TextAdapter

    adapter = TextAdapter()
    assert adapter.parse_line("   ") == []


def test_text适配器能从JSON对象里抠出正文() -> None:
    """gemini 的 ``-o json`` 把正文放在 ``response`` 字段。"""
    from src.adapters.text import TextAdapter

    adapter = TextAdapter()
    events = adapter.parse_payload({"response": "正文在这里"})
    assert len(events) == 1
    assert events[0]["text"] == "正文在这里"


# ---------------------------------------------------------------------------
# 可执行文件查找
# ---------------------------------------------------------------------------
def test_找不到可执行文件返回空() -> None:
    """认不出就说认不出。"""
    assert resolve_executable("哨兵-不存在的-cli-xyz") is None


def test_能通过兜底目录找到可执行文件(tmp_path: Path) -> None:
    """PATH 之外也要能找到 —— bun / npm 装的位置不在 PATH 里。"""
    target = tmp_path / "mytool"
    target.write_text("#!/bin/sh\n", encoding="utf-8")
    found = resolve_executable("mytool", extra_dirs=(str(tmp_path),))
    assert found == str(target)










# ---------------------------------------------------------------------------
# 派发档案表
# ---------------------------------------------------------------------------
def test_派发档案表包含14个外部插件Agent() -> None:
    """``run_with_sentinel.py`` 的档案表要并进 14 个外部插件 Agent。"""
    module = _load_runner_module()
    profiles = module.AGENT_PROFILES
    for agent_id in EXPECTED_IDS:
        assert agent_id in profiles, agent_id
    assert len(profiles) == 16  # 14 个外部插件 + mcode + codex（claude 重合）


def test_哨兵自有的claude档案没被覆盖() -> None:
    """哨兵的 claude 用 ``stream-json``，比外部插件登记的 ``json`` 更好。"""
    module = _load_runner_module()
    profile = module.AGENT_PROFILES["claude"]
    assert profile.adapter == "claude"
    argv = profile.build_command("claude", "干活", {"cwd": "C:/x"})
    assert "--output-format" in argv
    assert "stream-json" in argv


def test_外部插件Agent的正文位置按数据登记() -> None:
    """``positional`` 形态的 Agent 正文不能被挪到最后。"""
    module = _load_runner_module()
    profile = module.AGENT_PROFILES["dsh"]
    argv = profile.build_command("dsh", "干活", {"cwd": "C:/x"})
    assert argv == ["dsh", "--profile", "headless", "干活"]


def _load_runner_module() -> Any:
    """把派发脚本当模块加载进来。

    :return: 模块对象。
    """
    import importlib.util

    script = Path(
        ROOT_DIR, "_构建与发布", "scripts", "run_with_sentinel.py"
    )
    spec = importlib.util.spec_from_file_location("rws_v410", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_构建脚本要打包Agent清单() -> None:
    """``cloud_agents.json`` 必须进 exe —— 否则打包后清单为空。"""
    source = Path(
        ROOT_DIR, "_构建与发布", "scripts", "build_exe.py"
    ).read_text(encoding="utf-8")
    assert "cloud_agents.json" in source


def test_仓库里没有残留的临时脚本() -> None:
    """诊断脚本不能进版本库。"""
    tracked = subprocess.run(
        ["git", "ls-files", "logs/agentbridge-audit"],
        cwd=ROOT_DIR,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert not tracked.stdout.strip()


def test_版本号不低于410() -> None:
    """版本号必须不低于 4.1.0 —— 数据层是 4.1.0 引入的。

    ⚠️ 这里**不钉死具体版本号** —— 精确一致性由
    ``tests/test_v300_release.py`` 的四处通用检查负责
    （CHANGELOG 条目 / 版本文档 / 打包入口回退值 / Git 标签）。
    把版本号钉在特性测试文件里，会让每次发版都得改一个
    与该特性无关的文件 —— 那是维护陷阱，不是保护。
    """
    from src import __version__

    def _parts(text: str) -> tuple:
        """把 ``"4.2.0"`` 拆成可比较的整数元组。"""
        return tuple(int(item) for item in text.split("."))

    assert _parts(__version__) >= (4, 1, 0), __version__


def test_清单文件本身是合法JSON() -> None:
    """清单文件必须能直接被解析。"""
    path = Path(ROOT_DIR, "src", "agents", "cloud_agents.json")
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["schema"] == 1
    assert len(raw["agents"]) == 14
