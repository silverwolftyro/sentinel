"""真实管道接管脚本的测试。

被测对象：``_构建与发布/scripts/run_with_sentinel.py`` 里的
:class:`SentinelPipe` —— 它是「分支 A（打印）+ 分支 B（喂引擎）」的汇合点。

脚本不在包内，所以按路径加载它。
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from src.adapters.mcode import McodeAdapter

#: 仓库根目录。
ROOT = Path(__file__).resolve().parents[1]

#: 脚本路径。
SCRIPT_PATH = ROOT / "_构建与发布" / "scripts" / "run_with_sentinel.py"


def _load_script() -> Any:
    """按路径加载包装脚本（它不在包内）。"""
    spec = importlib.util.spec_from_file_location(
        "run_with_sentinel_under_test", SCRIPT_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


script = _load_script()


class _RecordingEngine:
    """记录收到的事件的假引擎。

    :param goal: 自动锚定后应记录的目标。
    """

    def __init__(self) -> None:
        self.events: List[Dict[str, Any]] = []
        self.goal = ""

    def process_event(self, event: Dict[str, Any]) -> None:
        """记录事件；遇到用户请求就锚定目标。"""
        self.events.append(event)
        if event.get("type") == "user_message":
            self.goal = str(event.get("text") or "")


class _FakePipe:
    """测试用的替身：记录写入的行。

    :param path: 落盘路径。
    """

    def __init__(self, path: str) -> None:
        self.path = path
        self.lines: List[str] = []

    def open_sink(self, path: str) -> None:
        """记录落盘路径。"""
        self.path = path

    def close_sink(self) -> None:
        """无操作。"""

    def feed_line(self, line: str) -> int:
        """记录一行并返回事件数。"""
        self.lines.append(line)
        return 0

    def feed_user_message(self, prompt: str) -> None:
        """记录用户请求。"""
        self.lines.append(prompt)


# ---------------------------------------------------------------------------
# 管道双分支
# ---------------------------------------------------------------------------
class TestSentinelPipe:
    """``SentinelPipe`` 的行为约定。"""

    def test_should_feed_parsed_events_to_engine(self) -> None:
        """分支 B：一行 JSON 要变成事件喂进引擎。"""
        engine = _RecordingEngine()
        pipe = script.SentinelPipe(engine, McodeAdapter(), echo=False)

        payload = {
            "schemaVersion": 1, "runId": "r", "type": "item.updated",
            "item": {
                "id": "call_1", "type": "tool_call",
                "toolCall": {
                    "id": "call_1", "name": "bash", "status": 1,
                    "input": {"command": "ls -la"},
                },
            },
        }
        count = pipe.feed_line(json.dumps(payload))

        assert count == 1
        assert engine.events[0]["type"] == "tool_call"
        assert engine.events[0]["tool"]["command"] == "ls -la"

    def test_should_echo_raw_line_to_console(self, capsys) -> None:
        """分支 A：原始行要实时打印到控制台。"""
        engine = _RecordingEngine()
        pipe = script.SentinelPipe(engine, McodeAdapter(), echo=True)

        pipe.feed_line('{"hello": "world"}')

        assert "hello" in capsys.readouterr().out

    def test_should_skip_non_json_lines(self) -> None:
        """mcode 会把日志行混进 stdout —— 不是 JSON 就跳过，不能崩。"""
        engine = _RecordingEngine()
        pipe = script.SentinelPipe(engine, McodeAdapter(), echo=False)

        count = pipe.feed_line("[00:56:38] ERROR: MCP 连接失败")

        assert count == 0
        assert engine.events == []
        assert pipe.bad_lines == 0  # 不是 JSON 不算「坏行」，只是日志

    def test_should_count_malformed_json_as_bad_line(self) -> None:
        """坏 JSON 要计入坏行，但**不能中断**接管。"""
        engine = _RecordingEngine()
        pipe = script.SentinelPipe(engine, McodeAdapter(), echo=False)

        count = pipe.feed_line('{"broken": ')

        assert count == 0
        assert pipe.bad_lines == 1
        assert engine.events == []

    def test_should_write_raw_log_to_sink(self, tmp_path) -> None:
        """原始输出要落盘（看板据此显示实时日志）。"""
        engine = _RecordingEngine()
        pipe = script.SentinelPipe(engine, McodeAdapter(), echo=False)
        target = tmp_path / "raw" / "mcode.jsonl"
        pipe.open_sink(str(target))

        pipe.feed_line('{"a": 1}')
        pipe.close_sink()

        assert target.read_text(encoding="utf-8").strip() == '{"a": 1}'

    def test_user_message_should_trigger_auto_anchor(self) -> None:
        """用户请求要被喂给引擎 —— 引擎据此自动锚定任务目标。"""
        engine = _RecordingEngine()
        pipe = script.SentinelPipe(engine, McodeAdapter(), echo=False)

        pipe.feed_user_message("重构 auth 模块并修复登录 bug")

        assert engine.goal == "重构 auth 模块并修复登录 bug"
        assert engine.events[0]["type"] == "user_message"


# ---------------------------------------------------------------------------
# 可执行文件解析
# ---------------------------------------------------------------------------
class TestResolveMcode:
    """``mcode`` 可执行文件的定位。"""

    def test_should_resolve_mcode_executable(self) -> None:
        """必须能定位到 mcode —— Windows 上它是 ``.cmd`` 垫片。"""
        found = script.AGENT_PROFILES["mcode"].resolve_executable()

        assert found is not None
        assert os.path.basename(found).lower().startswith("mcode")
        assert os.path.exists(found)


# ---------------------------------------------------------------------------
# 多路 Agent 适配
# ---------------------------------------------------------------------------
class TestAgentProfiles:
    """不同 CLI 的启动档案。"""

    def test_should_known_agents(self) -> None:
        """哨兵自有的三个 + v4.1.0 并进来的 14 个外部插件 Agent。"""
        known = set(script.AGENT_PROFILES)
        # 哨兵自有的三个必须还在。
        assert {"mcode", "claude", "codex"} <= known
        # v4.1.0：数据驱动的 14 个外部插件 Agent 并进来了
        # （claude 两边都有，哨兵这份优先 —— 它用 stream-json）。
        assert {
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
        } <= known
        assert len(known) == 16

    def test_should_build_mcode_command(self) -> None:
        """mcode 用 ``exec --output-format stream-json``。"""
        profile = script.AGENT_PROFILES["mcode"]
        cmd = profile.build_command(
            "mcode.cmd",
            "写个 hello world",
            {
                "cwd": "/tmp",
                "model": None,
                "max_steps": 12,
                "permission": "smart",
                "extra_args": [],
            },
        )

        assert cmd[0] == "mcode.cmd"
        assert "exec" in cmd
        assert "--output-format" in cmd
        assert "stream-json" in cmd
        assert "--cwd" in cmd and "/tmp" in cmd
        assert "--max-steps" in cmd and "12" in cmd
        # prompt 放在最后 —— 免得被当成选项。
        assert cmd[-1] == "写个 hello world"

    def test_should_build_claude_command(self) -> None:
        """Claude Code 用 ``--output-format stream-json --print``。"""
        profile = script.AGENT_PROFILES["claude"]
        cmd = profile.build_command(
            "claude",
            "写个 hello world",
            {
                "cwd": "/tmp",
                "model": None,
                "max_steps": 12,
                "permission": "smart",
                "extra_args": [],
            },
        )

        assert "--output-format" in cmd
        assert "stream-json" in cmd
        assert "--print" in cmd
        assert cmd[-1] == "写个 hello world"

    def test_should_build_codex_command(self) -> None:
        """Codex 用 ``exec --json``。"""
        profile = script.AGENT_PROFILES["codex"]
        cmd = profile.build_command(
            "codex",
            "写个 hello world",
            {
                "cwd": "/tmp",
                "model": None,
                "max_steps": 12,
                "permission": "smart",
                "extra_args": [],
            },
        )

        assert "exec" in cmd
        assert "--json" in cmd
        assert "--cd" in cmd and "/tmp" in cmd
        assert cmd[-1] == "写个 hello world"

    def test_should_map_permission_per_agent(self) -> None:
        """同一个权限档位，不同 CLI 的说法不一样。"""
        opts = {
            "cwd": "/tmp",
            "model": None,
            "max_steps": 12,
            "permission": "full",
            "extra_args": [],
        }
        mcode_cmd = script.AGENT_PROFILES["mcode"].build_command(
            "mcode", "p", opts
        )
        claude_cmd = script.AGENT_PROFILES["claude"].build_command(
            "claude", "p", opts
        )
        codex_cmd = script.AGENT_PROFILES["codex"].build_command(
            "codex", "p", opts
        )

        assert mcode_cmd[mcode_cmd.index("--permission") + 1] == "full"
        assert claude_cmd[claude_cmd.index("--permission-mode") + 1] == (
            "bypassPermissions"
        )
        assert codex_cmd[codex_cmd.index("-s") + 1] == "danger-full-access"

    def test_should_pass_extra_args_and_model(self) -> None:
        """额外参数与模型要能透传。"""
        cmd = script.AGENT_PROFILES["codex"].build_command(
            "codex",
            "p",
            {
                "cwd": "/tmp",
                "model": "gpt-5",
                "max_steps": 5,
                "permission": "smart",
                "extra_args": ["-c", 'foo="bar"'],
            },
        )

        assert "-m" in cmd and "gpt-5" in cmd
        assert "-c" in cmd and 'foo="bar"' in cmd

    def test_should_have_done_markers_per_agent(self) -> None:
        """每个 Agent 都要有「本轮结束」的标记。"""
        assert '"exec.completed"' in script.AGENT_PROFILES["mcode"].done_markers
        assert script.AGENT_PROFILES["claude"].done_markers
        assert '"turn.completed"' in script.AGENT_PROFILES["codex"].done_markers

    def test_should_build_adapters(self) -> None:
        """三种适配器都要能造出来。"""
        assert script.build_adapter("mcode").name == "mcode"
        assert script.build_adapter("claude").name == "claude"
        assert script.build_adapter("codex").name == "codex"

    def test_should_reject_unknown_adapter(self) -> None:
        """不认识的适配器要**报错**，不静默回落。"""
        import pytest

        with pytest.raises(ValueError):
            script.build_adapter("nope")


# ---------------------------------------------------------------------------
# Agent 进程 PID 定位
# ---------------------------------------------------------------------------
class _FakeProc:
    """psutil.Process 的替身。

    :param pid: 进程号。
    :param name: 进程名。
    :param children: 子进程列表。
    """

    def __init__(
        self,
        pid: int,
        name: str,
        children: Optional[List["_FakeProc"]] = None,
    ) -> None:
        self.pid = pid
        self._name = name
        self._children = children or []

    def name(self) -> str:
        """返回进程名。"""
        return self._name

    def children(self, recursive: bool = False) -> List["_FakeProc"]:
        """返回子进程列表。"""
        return list(self._children)


def _install_fake_psutil(monkeypatch, tree: _FakeProc) -> None:
    """把 ``psutil`` 换成假模块。"""
    import types

    module = types.ModuleType("psutil")
    module.Process = lambda pid: tree  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "psutil", module)


class _FakePopen:
    """subprocess.Popen 的替身。

    :param pid: 进程号。
    """

    def __init__(self, pid: int) -> None:
        self.pid = pid


class TestResolveAgentPid:
    """「到底哪个进程才是 Agent」的定位逻辑。

    这段逻辑是**实机踩坑**逼出来的：
    2026-10 实测 codex（原生二进制）时，
    沿进程树取「第一个非 cmd 后代」抓到的是 Agent 临时拉起来的
    ``pwsh.exe`` —— 命令跑完它就没了，红线命中时挂起必然失败。
    """

    def test_native_binary_launcher_is_agent_itself(self, monkeypatch) -> None:
        """★ 原生二进制（codex.exe）：启动器自己就是 Agent 本体。"""
        tree = _FakeProc(
            1000,
            "codex.exe",
            [_FakeProc(1001, "pwsh.exe"), _FakeProc(1002, "conhost.exe")],
        )
        _install_fake_psutil(monkeypatch, tree)

        pid = script._resolve_agent_pid(_FakePopen(1000), timeout=0.1)

        assert pid == 1000

    def test_cmd_shim_resolves_to_node(self, monkeypatch) -> None:
        """npm 的 ``.cmd`` 垫片：真正干活的是下面的 node.exe。"""
        tree = _FakeProc(2000, "cmd.exe", [_FakeProc(2001, "node.exe")])
        _install_fake_psutil(monkeypatch, tree)

        pid = script._resolve_agent_pid(
            _FakePopen(2000), timeout=0.1, agent_names=["node"]
        )

        assert pid == 2001

    def test_should_skip_transient_shell_children(self, monkeypatch) -> None:
        """★ 临时跑命令的壳（pwsh）不能被当成 Agent 本体。"""
        tree = _FakeProc(
            3000,
            "cmd.exe",
            [
                _FakeProc(3001, "pwsh.exe"),
                _FakeProc(3002, "node.exe"),
            ],
        )
        _install_fake_psutil(monkeypatch, tree)

        pid = script._resolve_agent_pid(
            _FakePopen(3000), timeout=0.1, agent_names=["node"]
        )

        assert pid == 3002

    def test_should_fall_back_when_only_shells_exist(self, monkeypatch) -> None:
        """整棵树里只有壳时，退回启动器 PID（而不是随便挑一个壳）。"""
        tree = _FakeProc(4000, "cmd.exe", [_FakeProc(4001, "pwsh.exe")])
        _install_fake_psutil(monkeypatch, tree)

        pid = script._resolve_agent_pid(_FakePopen(4000), timeout=0.1)

        assert pid == 4000

    def test_should_prefer_name_matched_candidate(self, monkeypatch) -> None:
        """名字对得上的候选优先。"""
        tree = _FakeProc(
            5000,
            "cmd.exe",
            [_FakeProc(5001, "other.exe"), _FakeProc(5002, "node.exe")],
        )
        _install_fake_psutil(monkeypatch, tree)

        pid = script._resolve_agent_pid(
            _FakePopen(5000), timeout=0.1, agent_names=["node"]
        )

        assert pid == 5002

    def test_should_fall_back_when_psutil_unavailable(self, monkeypatch) -> None:
        """psutil 不可用时退回启动器 PID，绝不抛异常。"""
        import types

        broken = types.ModuleType("psutil")
        broken.Process = lambda pid: (_ for _ in ()).throw(  # type: ignore[attr-defined]
            RuntimeError("boom")
        )
        monkeypatch.setitem(sys.modules, "psutil", broken)

        pid = script._resolve_agent_pid(_FakePopen(6000), timeout=0.1)

        assert pid == 6000


# ---------------------------------------------------------------------------
# 多路会话（v1.0.0）
# ---------------------------------------------------------------------------
class TestSessionPlans:
    """多路会话配置文件的解析。"""

    def _write(self, tmp_path, data: Any) -> str:
        """写一个会话配置文件。"""
        path = tmp_path / "sessions.json"
        path.write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8"
        )
        return str(path)

    def test_loads_multiple_sessions(self, tmp_path) -> None:
        """★ 一次读出多路会话。"""
        path = self._write(
            tmp_path,
            {
                "sessions": [
                    {"id": "a", "agent": "codex", "task": "写个 hello"},
                    {"id": "b", "agent": "mcode", "task": "修个 bug"},
                ]
            },
        )
        plans = script.load_session_plans(path)

        assert len(plans) == 2
        assert plans[0].id == "a"
        assert plans[0].agent == "codex"
        assert plans[0].task == "写个 hello"
        assert plans[1].agent == "mcode"

    def test_label_defaults_to_id(self, tmp_path) -> None:
        """没写 label 就用 id。"""
        path = self._write(
            tmp_path,
            {"sessions": [{"id": "a", "agent": "codex", "task": "x"}]},
        )
        assert script.load_session_plans(path)[0].label == "a"

    def test_accepts_bare_list(self, tmp_path) -> None:
        """顶层直接是数组也行。"""
        path = self._write(
            tmp_path, [{"id": "a", "agent": "codex", "task": "x"}]
        )
        assert len(script.load_session_plans(path)) == 1

    def test_rejects_unknown_agent(self, tmp_path) -> None:
        """Agent 类型不认识要报错，不能静默回落。"""
        path = self._write(
            tmp_path,
            {"sessions": [{"id": "a", "agent": "火星人", "task": "x"}]},
        )
        with pytest.raises(ValueError):
            script.load_session_plans(path)

    def test_rejects_missing_task(self, tmp_path) -> None:
        """没有任务描述要报错。"""
        path = self._write(
            tmp_path, {"sessions": [{"id": "a", "agent": "codex"}]}
        )
        with pytest.raises(ValueError):
            script.load_session_plans(path)

    def test_rejects_empty_sessions(self, tmp_path) -> None:
        """空列表要报错。"""
        path = self._write(tmp_path, {"sessions": []})
        with pytest.raises(ValueError):
            script.load_session_plans(path)

    def test_rejects_missing_file(self, tmp_path) -> None:
        """文件不存在要报错。"""
        with pytest.raises(ValueError):
            script.load_session_plans(str(tmp_path / "没有这个文件.json"))

    def test_rejects_broken_json(self, tmp_path) -> None:
        """坏 JSON 要报错。"""
        path = tmp_path / "坏.json"
        path.write_text("{不是 JSON", encoding="utf-8")
        with pytest.raises(ValueError):
            script.load_session_plans(str(path))


# ---------------------------------------------------------------------------
# 收尾顺序与 stdin（v3.2.0 修的真缺陷）
# ---------------------------------------------------------------------------
class _FakeWorker:
    """L2 解释线程替身。"""

    def drain(self, timeout: float = 0.0) -> bool:
        """永远收敛。"""
        return True


class TestCloseOrder:
    """★ 晨报必须在**通道关闭之前**推出去。

    真实顺序是「关引擎 → 推晨报」，而 ``engine.close()`` 会把 notifier 关掉
    （投递线程随之停止）。如果晨报在那之后才入队，队列里就**没人送了** ——
    手机上什么也没收到，而终端还印着「晨报摘要已推送到你的手机」。
    那是**说谎**，比不推还糟。
    """

    @staticmethod
    def _session(order: List[str]) -> Any:
        """造一个只记录顺序的会话。"""

        class _FakeEngine:
            """引擎替身 —— 只记录顺序。"""

            def __init__(self) -> None:
                self.ended_at = 0.0
                self.counts: Dict[str, int] = {}
                self.explanation_worker = _FakeWorker()

            def close(self) -> None:
                """记录关闭时刻。"""
                order.append("engine.close")

        class _FakeNotifier:
            """通知器替身 —— 记录它被关掉的时刻。"""

            def close(self) -> None:
                """记录关闭时刻。"""
                order.append("notifier.close")

            def push_briefing(self, summary: Dict[str, Any]) -> bool:
                """记录推送时刻。"""
                order.append("push_briefing")
                return True

        session = script.AgentSession.__new__(script.AgentSession)
        session.proc = None
        session.pipe = None
        session.engine = _FakeEngine()
        session.guardian = None
        session.intent_tracker = None
        session.notifier = _FakeNotifier()
        session.briefing_enabled = True
        session.briefing_path = ""
        session.counts = {}
        session.plan = script.SessionPlan(
            id="s1", agent="mcode", task="t", label="L", cwd="."
        )
        return session

    def test_briefing_is_pushed_before_engine_closes(self) -> None:
        """推晨报的时机必须早于 ``engine.close()``。"""
        order: List[str] = []
        session = self._session(order)

        def _fake_write() -> str:
            """记录推晨报的时刻。"""
            order.append("write_briefing")
            return "晨报路径"

        session.write_briefing = _fake_write
        session.close()

        assert order.index("write_briefing") < order.index("engine.close")

    def test_briefing_is_pushed_only_once(self) -> None:
        """★ 推**一次**就够 —— 推两次手机上会收到两份一样的晨报。"""
        order: List[str] = []
        session = self._session(order)
        calls: List[int] = []

        def _fake_write() -> str:
            """记录调用次数。"""
            calls.append(1)
            return "晨报路径"

        session.write_briefing = _fake_write
        session.close()
        assert len(calls) == 1


class TestStdinIsNotInherited:
    """★ 不读 stdin 的 CLI **不能继承**父进程的 stdin。

    继承意味着子进程可能拿到一条**永远不关闭**的管道 ——
    一次性 exec 模式一旦因为某种原因去读 stdin（比如想确认操作），
    它就会**永久阻塞**。实测踩到过：后台跑的哨兵把 mcode 拉起来后，
    mcode 卡在等 stdin 上，stdout 一个字都不吐，进程永远不结束。
    """

    @staticmethod
    def _capture_popen(monkeypatch) -> Dict[str, Any]:
        """把 ``Popen`` 换成只记参数的假货。"""
        captured: Dict[str, Any] = {}

        class _FakePopen:
            """Popen 替身 —— 只记参数。"""

            def __init__(self, cmd: List[str], **kwargs: Any) -> None:
                captured["cmd"] = cmd
                captured.update(kwargs)
                self.pid = 4242
                self.stdout = None
                self.stderr = None
                self.stdin = None

            def poll(self) -> None:
                """假装还活着。"""
                return

        monkeypatch.setattr(script.subprocess, "Popen", _FakePopen)
        return captured

    @staticmethod
    def _opts() -> Dict[str, Any]:
        """一份最小的启动参数。"""
        return {
            "cwd": ".",
            "model": None,
            "max_steps": 1,
            "permission": "smart",
            "extra_args": [],
        }

    def test_non_stdin_cli_gets_devnull(self, monkeypatch) -> None:
        """mcode 这类 CLI 拿到的应该是 DEVNULL，不是继承。"""
        captured = self._capture_popen(monkeypatch)

        session = script.AgentSession.__new__(script.AgentSession)
        session.profile = script.AGENT_PROFILES["mcode"]
        session.proc = None
        session._opts = {}
        assert session.profile.supports_stdin is False

        session._spawn(["mcode", "exec", "x"], self._opts())
        assert captured["stdin"] is script.subprocess.DEVNULL

    def test_stdin_cli_still_gets_a_pipe(self, monkeypatch) -> None:
        """会读 stdin 的 CLI 仍然拿管道（v2.0.0 的直写通道）。"""
        captured = self._capture_popen(monkeypatch)

        profile = script.AgentProfile(
            name="fake",
            label="Fake",
            exec_names=["fake"],
            build_args=lambda opts: ["fake"],
            adapter="mcode",
            done_markers=[],
            supports_stdin=True,
        )
        session = script.AgentSession.__new__(script.AgentSession)
        session.profile = profile
        session.proc = None
        session._opts = {}

        session._spawn(["fake"], self._opts())
        assert captured["stdin"] is script.subprocess.PIPE
