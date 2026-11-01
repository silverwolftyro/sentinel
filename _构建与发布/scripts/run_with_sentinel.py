"""真实 Agent 管道接管 —— 让哨兵**盯住**一个真的在跑的 CLI。

v0.8.0 之前，哨兵一直只在看**已经落盘**的日志（或者我们造的假数据）。
这个脚本把整条管道接上：

.. code-block:: text

    ┌────────────┐   stdout(stream-json)   ┌──────────────────┐
    │ mcode exec │ ───────────────────────▶ │  本脚本（管道）   │
    └────────────┘                          └────────┬─────────┘
             ▲                              分支 A │ 实时打印到控制台
             │                                     │
             │                              分支 B │ 解析成统一事件
             │                                     ▼
             │                            ┌──────────────────┐
             │                            │  SentinelEngine  │
             │                            │  L1 / 上下文 /   │
             │                            │  漂移(异步)       │
             │                            │  L2解释(异步)     │
             │                            └────────┬─────────┘
             │                                     │ 红线级
             │                            ┌────────▼─────────┐
             └──── 挂起 / 恢复 ────────────│     Guardian      │
                                            │  psutil suspend  │
                                            └──────────────────┘

两个分支缺一不可：

* **分支 A** —— 实时打印，让你看得见 Agent 在干嘛；
* **分支 B** —— 喂给哨兵，让它逐事件审查。

红线命中时，守护者会**挂起 Agent 进程**（可逆，不是 kill）；
在看板里点【放行并忽略】即可恢复。

支持哪些 Agent
==============

============  ==========================================  ====================
Agent         启动方式                                    流式格式
============  ==========================================  ====================
``mcode``     ``mcode exec --output-format stream-json``  v0.6 事件溯源协议
``claude``    ``claude --output-format stream-json -p``     Anthropic 消息容器
``codex``     ``codex exec --json``                        ``item.*`` 事件溯源协议
============  ==========================================  ====================

三者**底层往往都是 Node.js**，而且 Windows 上还都可能是 ``.cmd`` 垫片 ——
所以守护者要挂起的是**真正在跑的那个进程**（见 :func:`_resolve_agent_pid`）。

用法::

    python _构建与发布/scripts/run_with_sentinel.py "写一个 hello world"
    python _构建与发布/scripts/run_with_sentinel.py --agent codex "写个 hello world"
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional

#: 脚本所在目录。
_HERE = os.path.dirname(os.path.abspath(__file__))


def _find_project_root() -> str:
    """向上找到项目根目录（含 ``src/core`` 的那一层）。

    脚本在 ``_构建与发布/scripts/`` 下，**不能**靠固定层数往上数
    —— 目录一动就指错地方。按特征找最稳。

    ⚠️ 打包成 exe 之后没有「项目根」这回事（``src/core`` 变成编译后的模块，
    不再是目录）。那时候用 **exe 所在目录**当根 ——
    日志、配置、晨报归档都应该落在用户看得见、删得掉的地方。
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    current = _HERE
    while True:
        if os.path.isdir(os.path.join(current, "src", "core")):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            raise RuntimeError("找不到项目根目录（应含 src/core/）")
        current = parent


def _prepare_stdout() -> None:
    """让重定向 / 管道场景下的中文输出不乱码。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(
                    encoding="utf-8", errors="replace", line_buffering=True
                )
            except (ValueError, OSError, AttributeError):
                pass


def _say(message: str) -> None:
    """打印一条哨兵自己的消息。"""
    print(f"[SENTINEL] {message}", flush=True)


def _version() -> str:
    """哨兵版本号（从 :mod:`src` 取，只有一处真相）。

    :return: 形如 ``v2.1.0`` 的版本串；取不到时返回 ``unknown``。
    """
    try:
        from src import __version__  # type: ignore

        return "v" + str(__version__)
    except Exception:  # noqa: BLE001 —— 取不到版本不该挡住启动
        return "unknown"


def _raw_version() -> str:
    """哨兵**裸**版本号（形如 ``3.0.0``，不带 ``v``）。

    极简启动问候（``Sentinel v3.0.0 is ready.``）要的是这个形态；
    :func:`_version` 给的是带 ``v`` 的展示形态，两者别混。

    :return: 版本号；取不到时返回 ``unknown``。
    """
    try:
        from src import __version__  # type: ignore

        return str(__version__)
    except Exception:  # noqa: BLE001 —— 取不到版本不该挡住启动
        return "unknown"


def _write_event(
    log_path: str,
    kind: str,
    title: str,
    detail: str = "",
    *,
    severity: str = "info",
    safe_point_id: str = "",
    point_id: str = "",
    **extra: Any,
) -> Dict[str, Any]:
    """往告警日志里写一条「操作事件」（v2.2.0）。

    格式与引擎写的**完全一致**（``EVENT_JSON {json}``）——
    看板据此把它拼进「案发时间线」。

    为什么不直接用引擎的 :meth:`SentinelEngine.emit_event`：
    会话**开始**那一刻引擎还没建起来，而「开始目标」恰恰是
    时间线上最该有的第一个节点。

    写失败**绝不抛异常** —— 写事件失败不该影响审查（红线 R5）。

    :param log_path: 告警日志路径。
    :param kind: 事件类型。
    :param title: 一句话标题。
    :param detail: 补充说明。
    :param severity: 严重程度。
    :param safe_point_id: 关联的安全点 ID。
    :param point_id: 算 Diff 用的回滚基线 ID。
    :param extra: 附加信息。
    :return: 事件字典。
    """
    payload: Dict[str, Any] = {
        "kind": str(kind or "event"),
        "title": str(title or ""),
        "detail": str(detail or ""),
        "severity": str(severity or "info"),
        "timestamp": time.time(),
        "safe_point_id": str(safe_point_id or ""),
        "point_id": str(point_id or ""),
        "extra": dict(extra),
    }
    try:
        from src.core.engine import EVENT_MARKER

        target = os.path.abspath(str(log_path or ""))
        directory = os.path.dirname(target)
        if directory:
            os.makedirs(directory, exist_ok=True)
        now = time.time()
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
        msec = int((now - int(now)) * 1000)
        line = "{},{:03d} WARNING {} {}\n".format(
            stamp,
            msec,
            EVENT_MARKER,
            json.dumps(payload, ensure_ascii=False),
        )
        with open(target, "a", encoding="utf-8") as handle:
            handle.write(line)
    except Exception as exc:  # noqa: BLE001 —— 写事件失败只降级
        _say(
            f"写操作事件失败（{type(exc).__name__}: {exc}）—— 不影响审查。"
        )
    return payload


# ---------------------------------------------------------------------------
# Agent 档案
# ---------------------------------------------------------------------------
class AgentProfile:
    """一个 Agent CLI 的启动档案。

    哨兵不该和某个 CLI **绑死** —— 所以每个 CLI 的差异都收在一份档案里：

    * **怎么启动**（``build_args``）；
    * **用什么适配器解析它的流**（``adapter``）；
    * **怎么知道它跑完了**（``done_markers``）。

    :param name: 类型名（``mcode`` / ``claude`` / ``codex``）。
    :param label: 展示名。
    :param exec_names: 可执行文件名候选（按顺序找）。
    :param build_args: 构造参数，签名 ``(opts: dict) -> List[str]``。
    :param adapter: 解析流式输出的适配器名。
    :param done_markers: 判定「本轮结束」的字符串标记。
    :param permission_map: 通用权限档位 → 该 CLI 的权限参数。
    :param process_names: Agent **本体**进程名的候选片段，
        用于在进程树里认出「哪个才是 Agent」——
        ``mcode`` / ``claude`` 是 node，``codex`` 是原生二进制。
    :param supports_stdin: 这个 CLI 会不会**读标准输入**。

        v2.0.0 的「直接发送给 Agent」要往 stdin 写东西，
        但那得对方**真的在读**才有意义：

        * ``codex exec`` / ``mcode exec`` / ``claude -p`` 都是
          **一次性、非交互**模式 —— 任务从命令行参数进去，
          跑完就退出，stdin 没人读；
        * 交互式 REPL 才会一边跑一边读 stdin。

        所以默认是 ``False`` —— **宁可承认送不到，
        也不要写进去以为送到了**。
        真要送时走降级路径（留言箱 + 重启注入）。
    """

    def __init__(
        self,
        name: str,
        label: str,
        exec_names: List[str],
        build_args: Callable[[Dict[str, Any]], List[str]],
        adapter: str,
        done_markers: List[str],
        permission_map: Optional[Dict[str, Any]] = None,
        process_names: Optional[List[str]] = None,
        supports_stdin: bool = False,
        build_full_args: Optional[
            Callable[[Dict[str, Any]], List[str]]
        ] = None,
    ) -> None:
        self.name = name
        self.label = label
        self.exec_names = exec_names
        self.build_args = build_args
        self.adapter = adapter
        self.done_markers = done_markers
        self.permission_map = permission_map or {}
        self.process_names = list(process_names or [])
        #: 这个 CLI 会不会读标准输入（v2.0.0）。
        self.supports_stdin = bool(supports_stdin)
        #: v4.1.0：**完整命令行**构造器（可选）。
        #:
        #: 哨兵自己的三个 CLI 约定把任务正文放**最后**，
        #: 但上游那 14 个 Agent 的正文位置**不固定** ——
        #: ``positional`` 形态紧跟固定参数，``flag`` / ``switch``
        #: 形态跟在开关后面。给了这个钩子就不用硬套「正文最后」，
        #: 而是按数据文件里登记的形态原样拼。
        self.build_full_args = build_full_args

    def resolve_executable(self) -> Optional[str]:
        """找到可执行文件。

        Windows 上 ``mcode`` 之类往往是 npm 装的 **``.cmd`` 垫片**，
        直接 ``Popen(["mcode", ...])`` 解析不到（它只认真正的可执行文件），
        所以先让 :func:`shutil.which` 按 PATHEXT 去找。

        :return: 可执行文件路径；找不到时返回 ``None``。
        """
        import shutil

        for name in self.exec_names:
            found = shutil.which(name)
            if found:
                return found
        return None

    def build_command(
        self,
        executable: str,
        prompt: str,
        opts: Dict[str, Any],
    ) -> List[str]:
        """拼出完整的命令行。

        :param executable: 可执行文件路径。
        :param prompt: 交给 Agent 的任务描述。
        :param opts: 选项字典。
        :return: 命令行参数列表。
        """
        if self.build_full_args is not None:
            # v4.1.0：数据驱动的 Agent —— 命令行形态由数据文件决定，
            # 正文的位置**不固定**，不能套「正文放最后」的约定。
            merged = dict(opts or {})
            merged["prompt"] = prompt
            return [executable] + list(self.build_full_args(merged))
        return [executable] + list(self.build_args(opts)) + [prompt]


#: 通用权限档位。
PERMISSION_MODES = ("smart", "full", "off")


def _build_mcode_args(opts: Dict[str, Any]) -> List[str]:
    """mcode 的启动参数。"""
    args = [
        "exec",
        "--output-format",
        "stream-json",
        "--cwd",
        opts["cwd"],
        "--max-steps",
        str(int(opts.get("max_steps") or 20)),
        "--permission",
        str(opts.get("permission") or "smart"),
    ]
    if opts.get("model"):
        args += ["--model", str(opts["model"])]
    args += list(opts.get("extra_args") or [])
    return args


def _build_claude_args(opts: Dict[str, Any]) -> List[str]:
    """Claude Code 的启动参数。

    Claude Code 的非交互模式是 ``--print``（``-p``）；
    输出格式用 ``--output-format stream-json``。
    工作目录靠**进程级 cwd**（Claude Code 没有通用的 ``--cwd``）。
    """
    args = ["--output-format", "stream-json", "--print"]
    mode = opts.get("permission") or "smart"
    mapped = {
        "smart": "default",
        "full": "bypassPermissions",
        "off": "plan",
    }.get(str(mode), "default")
    args += ["--permission-mode", mapped]
    if opts.get("model"):
        args += ["--model", str(opts["model"])]
    args += list(opts.get("extra_args") or [])
    return args


def _build_codex_args(opts: Dict[str, Any]) -> List[str]:
    """Codex 的启动参数。"""
    mode = opts.get("permission") or "smart"
    sandbox = {
        "smart": "workspace-write",
        "full": "danger-full-access",
        "off": "read-only",
    }.get(str(mode), "workspace-write")
    args = [
        "exec",
        "--json",
        "--cd",
        opts["cwd"],
        "-s",
        sandbox,
    ]
    if str(mode) == "full":
        # 「完全放开」意味着连审批都跳过 —— 危险，但这就是 full 的含义。
        args.append("--dangerously-bypass-approvals-and-sandbox")
    if opts.get("model"):
        args += ["-m", str(opts["model"])]
    args += list(opts.get("extra_args") or [])
    return args


#: Agent 档案表 —— 新接一个 CLI 只要在这里加一条。
AGENT_PROFILES: Dict[str, AgentProfile] = {
    "mcode": AgentProfile(
        name="mcode",
        label="mcode",
        exec_names=["mcode", "mcode.cmd", "mcode.exe", "mcode.bat"],
        build_args=_build_mcode_args,
        adapter="mcode",
        done_markers=['"exec.completed"'],
        process_names=["node"],
    ),
    "claude": AgentProfile(
        name="claude",
        label="Claude Code",
        exec_names=["claude", "claude.cmd", "claude.exe"],
        build_args=_build_claude_args,
        adapter="claude",
        done_markers=['"type":"result"', '"subtype":"success"'],
        process_names=["node"],
    ),
    "codex": AgentProfile(
        name="codex",
        label="Codex CLI",
        exec_names=["codex", "codex.exe", "codex.cmd"],
        build_args=_build_codex_args,
        adapter="codex",
        done_markers=['"turn.completed"'],
        process_names=["codex"],
    ),
}


def _cloud_agent_profiles() -> Dict[str, "AgentProfile"]:
    """从数据文件生成上游那 14 个 Agent 的档案（v4.1.0）。

    为什么走数据文件
    ===============

    哨兵自己的三个 CLI（``mcode`` / ``claude`` / ``codex``）的命令行形态
    是手写的，因为它们各有各的怪癖；但上游那 14 个 CLI 的形态**高度同构**
    —— 全部可以用「可执行名 + 固定参数 + 正文开关 + 模型开关」描述，
    所以做成**数据**（``src/agents/cloud_agents.json``），
    新增一个 Agent 不用改代码。

    ⚠️ 三件事必须如实说明：

    1. **正文位置不固定** —— ``positional`` 形态紧跟固定参数，
       ``flag`` / ``switch`` 形态跟在开关后面。
       所以这里走 ``build_full_args``（完整命令行）而不是
       ``build_args``（正文放最后）。
    2. **一律用通用 text 适配器** —— v4.1.0 明确不做多格式解析器。
       只有 ``json_shape`` 是 ``document`` / ``jsonl`` 的那几个
       会打开机器可读输出开关，让 text 适配器能抠出正文；
       ``stream`` 形态按作者建议**不开**（事件流解不出正文）。
    3. **哨兵已有的档案优先** —— ``claude`` 两边都有，
       哨兵这份用 ``--output-format stream-json``，
       比上游登记的 ``json`` 更适合流式审查，所以保留哨兵这份。

    :return: ``id → AgentProfile`` 的映射。
    """
    from src.agents import build_argv, load_agents

    profiles: Dict[str, "AgentProfile"] = {}
    for agent_id, spec in load_agents(strict=False).items():
        if agent_id in AGENT_PROFILES:
            # 哨兵自己已经有的档案优先（见上面的说明 3）。
            continue

        def _make(spec=spec):
            def _full(opts: Dict[str, Any]) -> List[str]:
                return build_argv(
                    spec,
                    str(opts.get("prompt") or ""),
                    model=opts.get("model") or None,
                    json_output=spec.supports_json_document,
                    extra_args=list(opts.get("extra_args") or []),
                    include_exe=False,
                )

            return _full

        profiles[agent_id] = AgentProfile(
            name=agent_id,
            label=spec.label,
            exec_names=list(spec.exec_names),
            build_args=_make(),
            adapter="text",
            # ⚠️ 没有可靠的「本轮结束」标记 —— 只能靠进程退出。
            done_markers=[],
            process_names=[spec.binary],
            build_full_args=_make(),
        )
    return profiles


#: v4.1.0：把数据驱动的 14 个 Agent 并进档案表。
#:
#: ⚠️ 用 ``setdefault`` 而不是直接覆盖 ——
#: 哨兵自己的 ``claude`` 档案（stream-json）必须保留。
for _agent_id, _agent_profile in _cloud_agent_profiles().items():
    AGENT_PROFILES.setdefault(_agent_id, _agent_profile)


def build_adapter(adapter_name: str) -> Any:
    """按名字造一个适配器。

    :param adapter_name: 适配器名（``mcode`` / ``claude`` / ``codex`` /
        ``text``）。
    :return: 适配器实例。
    :raises ValueError: 适配器名不认识。
    """
    if adapter_name == "mcode":
        from src.adapters.mcode import McodeAdapter

        return McodeAdapter()
    if adapter_name == "claude":
        from src.adapters.claude import ClaudeAdapter

        return ClaudeAdapter()
    if adapter_name == "codex":
        from src.adapters.codex import CodexAdapter

        return CodexAdapter()
    if adapter_name == "text":
        # v4.1.0：通用纯文本适配器 ——
        # 给那 6 个拿不到机器可读输出的 Agent CLI 用。
        from src.adapters.text import TextAdapter

        return TextAdapter()
    raise ValueError(f"未知的适配器：{adapter_name}")


class SentinelPipe:
    """接管 Agent 的 stdout，把事件喂给哨兵引擎。

    :param engine: 哨兵引擎实例。
    :param adapter: 解析流式输出的适配器。
    :param echo: 是否把原始行实时打印到控制台（分支 A）。
    """

    def __init__(self, engine: Any, adapter: Any, echo: bool = True) -> None:
        self.engine = engine
        self.adapter = adapter
        self.echo = bool(echo)
        #: 已经喂给引擎的事件数。
        self.events = 0
        #: 解析失败的行数。
        self.bad_lines = 0
        #: 原始日志落盘句柄（看板可以跟着它看）。
        self._sink = None

    def open_sink(self, path: str) -> None:
        """打开原始日志落盘文件（看板据此显示实时日志）。

        :param path: 文件路径。
        """
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._sink = open(path, "a", encoding="utf-8")

    def close_sink(self) -> None:
        """关闭落盘句柄。"""
        if self._sink is not None:
            try:
                self._sink.close()
            except OSError:
                pass
            self._sink = None

    def feed_line(self, line: str) -> int:
        """处理一行 Agent 输出。

        :param line: 原始行。
        :return: 这一行产生的事件数。
        """
        if self._sink is not None:
            self._sink.write(line + "\n")
            self._sink.flush()
        if self.echo:
            # 分支 A：实时打印，让人看得见 Agent 在干嘛。
            print(line, flush=True)
        stripped = line.strip()
        if not stripped:
            return 0
        if not stripped.startswith("{"):
            # v4.1.0：非 JSON 行**交给适配器** ——
            # 通用 text 适配器的主菜恰恰就是纯文本行。
            # 其余适配器（mcode / claude / codex）继承基类的实现，
            # 对非 JSON 行返回空列表，行为与以前完全一样。
            try:
                events = self.adapter.parse_line(line)
            except Exception as exc:  # noqa: BLE001 —— 采集侧异常不中断接管
                self.bad_lines += 1
                _say(f"解析一行失败，已跳过：{type(exc).__name__}: {exc}")
                return 0
            count = 0
            for event in events:
                if event is None:
                    continue
                self.engine.process_event(event)
                self.events += 1
                count += 1
            return count
        try:
            payload = json.loads(stripped)
        except (json.JSONDecodeError, ValueError):
            self.bad_lines += 1
            return 0
        if not isinstance(payload, dict):
            return 0
        try:
            events = self.adapter.parse_payload(payload)
        except Exception as exc:  # noqa: BLE001 —— 采集侧异常不中断接管
            self.bad_lines += 1
            _say(f"解析一行失败，已跳过：{type(exc).__name__}: {exc}")
            return 0

        count = 0
        for event in events:
            if event is None:
                continue
            # 分支 B：喂给哨兵引擎做审查。
            self.engine.process_event(event)
            self.events += 1
            count += 1
        return count

    def feed_user_message(self, prompt: str) -> None:
        """把用户的原始请求喂给引擎 —— 触发**自动锚定**。

        并非所有 Agent 都会把自己的 Prompt 回显到流里
        （实测 mcode v0.6.12 就不回显），所以这里把它补上：
        引擎从 ``user_message`` 事件里提取任务目标。

        :param prompt: 用户交给 Agent 的那句话。
        """
        if not prompt:
            return
        event = {
            "source": "sentinel",
            "type": "user_message",
            "session_id": None,
            "timestamp": time.time(),
            "tool": None,
            "text": prompt,
            "is_error": False,
            "raw": {},
        }
        self.engine.process_event(event)
        self.events += 1


#: 这些是「跑命令用的壳」而不是 Agent 本体。
#:
#: 挂起它们毫无意义 ——
#: * ``cmd.exe`` / ``conhost.exe``：npm 的 ``.cmd`` 垫片，
#:   真正干活的是它下面的 ``node.exe``；
#: * ``pwsh.exe`` / ``powershell.exe`` / ``bash.exe``：Agent 临时拉起来
#:   执行单条命令的子壳，**命令跑完它就退出了** ——
#:   2026-10 实测就栽在这儿：codex.exe 拉起的 pwsh 被误认成 Agent 本体，
#:   等红线命中要挂起时，那个 pid 已经不存在了。
_SHELL_PROCESS_NAMES = frozenset(
    {
        "cmd.exe",
        "conhost.exe",
        "pwsh.exe",
        "powershell.exe",
        "bash.exe",
        "sh.exe",
        "zsh.exe",
    }
)


def _resolve_agent_pid(
    proc: Any,
    timeout: float = 5.0,
    agent_names: Optional[List[str]] = None,
) -> int:
    """找到**真正在跑 Agent** 的那个进程 PID。

    Agent CLI 的启动形态有**两种**，必须分开处理：

    **形态 A —— 原生可执行文件**（如 ``codex.exe``）
        启动器 PID **就是** Agent 本体，直接用它。
        它的子进程只是临时跑命令的壳，挂起壳没有意义。

    **形态 B —— npm 的 ``.cmd`` 垫片**（如 ``mcode.cmd`` / ``claude.cmd``）
        :mod:`subprocess` 拿到的是 ``cmd.exe`` 的 PID，
        真正干活的是它下面的 ``node.exe``。
        挂起 cmd.exe 毫无意义 —— 必须挂起**真正在跑 Agent 的那个进程**，
        否则 Agent 照样跑。

    所以这里沿着进程树往下找：跳过各类壳进程，挑一个真正的后代；
    如果启动器本身就不是壳，那它自己就是 Agent。

    启动瞬间可能还没生出来，所以轮询一会儿。

    :param proc: :class:`subprocess.Popen` 实例。
    :param timeout: 最长等多久（秒）。
    :param agent_names: Agent 进程名的候选片段（如 ``["node"]``），
        命中时优先选它。
    :return: Agent 进程 PID；找不到时退回父进程 PID。
    """
    deadline = time.monotonic() + max(float(timeout), 0.0)
    wanted = [str(item).lower() for item in (agent_names or []) if item]
    while True:
        try:
            import psutil

            parent = psutil.Process(proc.pid)
            own_name = parent.name().lower()

            # --- 形态 A：启动器自己就是 Agent（原生二进制） ---
            if own_name not in _SHELL_PROCESS_NAMES:
                return proc.pid

            # --- 形态 B：启动器是壳，往下找真正的 Agent ---
            candidates: List[tuple] = []
            for child in parent.children(recursive=True):
                try:
                    name = child.name().lower()
                except Exception as exc:  # noqa: BLE001 —— 进程可能刚好退了
                    # 进程刚好退出是**预期内**的（我们在跟一个活进程赛跑），
                    # 但仍要留痕 —— 否则「为什么没找到 Agent」无从查起。
                    print(
                        "读取子进程名失败（PID {}）：{}: {}".format(
                            child.pid, type(exc).__name__, exc
                        ),
                        file=sys.stderr,
                    )
                    continue
                if name in _SHELL_PROCESS_NAMES:
                    # 垫片进程 / 临时命令壳 —— 挂起它没有意义。
                    continue
                candidates.append((child.pid, name))
            if candidates:
                # 优先挑名字对得上的（mcode / claude 的 node.exe）。
                for pid, name in candidates:
                    if any(fragment in name for fragment in wanted):
                        return pid
                return candidates[0][0]
        except Exception:  # noqa: BLE001 —— psutil 不可用就退回父进程
            return proc.pid
        if time.monotonic() >= deadline:
            return proc.pid
        time.sleep(0.2)


def _sync_watch_target(watch_target: Any, intent_tracker: Any) -> None:
    """把追踪器的目标状态同步到看板的「当前监控目标」快照。

    目标可能在运行中才被**自动锚定**（引擎从流里第一条用户请求里提取），
    所以每次读完一行就同步一次 —— 面板才能立刻显示「自动提取」。

    :param watch_target: 看板快照持有者。
    :param intent_tracker: 意图追踪器。
    """
    if watch_target is None or intent_tracker is None:
        return
    snapshot = watch_target.snapshot()
    if (
        snapshot["goal"] == intent_tracker.goal
        and snapshot["goal_source"] == intent_tracker.goal_source
    ):
        return
    watch_target.set_goal(intent_tracker.goal, intent_tracker.goal_source)


def _drain_stderr(
    stream: Any,
    prefix: str = "[agent] ",
    capture: Optional[Any] = None,
) -> None:
    """把子进程的 stderr 读干净（不然管道会塞满、子进程卡死）。

    v4.1.0：可选地把内容**收进限幅缓冲** ——
    鉴权识别（没登录 / 服务没起）要靠 stderr 的措辞判断，
    光转发到屏幕是不够的。

    :param stream: stderr 文本流。
    :param prefix: 每行前缀。
    :param capture: 限幅缓冲（:class:`src.agents.launcher.BoundedBuffer`）；
        为 ``None`` 时只转发不收集。
    """
    try:
        for line in stream:
            if capture is not None:
                capture.append(line)
            sys.stdout.write(prefix + line)
            sys.stdout.flush()
    except Exception as exc:  # noqa: BLE001 —— 收尾失败不影响主流程
        print(
            "转发子进程输出失败：{}: {}".format(type(exc).__name__, exc),
            file=sys.stderr,
        )


def _read_lines(stream: Any, sink: Any, generation: int = 0) -> None:
    """把子进程 stdout 逐行推进队列。

    为什么要单独一个线程：读取必须**带超时**——
    实测 mcode 在发出 ``exec.completed`` 之后**并不退出**，
    stdout 管道不会关闭，主循环要是干等 EOF 就会永远卡住。

    v2.1.0：加入**代数**（generation）——
    「回滚并重试」会重拉一个新进程，旧进程的 EOF 标记会晚到。
    队列里每条消息都带上它来自哪一代，
    主循环只认**当前这一代**，旧尾巴直接丢掉。

    :param stream: stdout 文本流。
    :param sink: 目标队列（``queue.Queue``）。
    :param generation: 进程代数；默认 0。
    """
    try:
        for line in stream:
            sink.put((generation, line))
    except Exception as exc:  # noqa: BLE001 —— 读取失败只结束读取
        print(
            "读取子进程输出失败：{}: {}".format(type(exc).__name__, exc),
            file=sys.stderr,
        )
    finally:
        sink.put((generation, None))  # EOF 标记


def _terminate_tree(proc: Any) -> None:
    """收尾：结束自己拉起的 Agent 进程**树**。

    ⚠️ 这是**启动器清理自己的子进程**，与哨兵的守护者无关 ——
    守护者那套「绝不 kill」红线针对的是**被监控的 Agent**，
    而这里是父进程回收自己启动的子进程，属于标准做法。

    ⚠️ Windows 上**必须先杀树**：``.cmd`` 垫片被 ``terminate()`` 之后，
    真正的 ``node.exe`` 会变成孤儿继续跑 —— 所以顺序不能反。

    :param proc: :class:`subprocess.Popen` 实例。
    """
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
                check=False,
            )
        except Exception as exc:  # noqa: BLE001 —— 收尾失败不影响退出
            print(
                "taskkill 结束进程树失败（PID {}）：{}: {}".format(
                    proc.pid, type(exc).__name__, exc
                ),
                file=sys.stderr,
            )
    else:
        # POSIX：子进程自成会话（start_new_session=True），整组收掉。
        try:
            os.killpg(os.getpgid(proc.pid), 15)
        except Exception as exc:  # noqa: BLE001 —— 收尾失败不影响退出
            print(
                "killpg 结束进程组失败（PID {}）：{}: {}".format(
                    proc.pid, type(exc).__name__, exc
                ),
                file=sys.stderr,
            )
    try:
        proc.wait(timeout=5)
    except Exception as exc:  # noqa: BLE001 —— 收尾失败不影响退出
        print(
            "等待子进程退出超时（PID {}）：{}: {}".format(
                proc.pid, type(exc).__name__, exc
            ),
            file=sys.stderr,
        )
        try:
            proc.kill()
        except Exception as exc:  # noqa: BLE001 —— 收尾失败不影响退出
            print(
                "强制结束子进程失败（PID {}）：{}: {}".format(
                    proc.pid, type(exc).__name__, exc
                ),
                file=sys.stderr,
            )


def _terminate_agent_trees(sessions: List[Any]) -> None:
    """把启动器**自己拉起**的 Agent 进程树收掉（v3.4.0）。

    为什么需要它
    ============

    用户实测：「点击彻底退出后，任务管理器里找不到任何 sentinel 残留。」
    —— 反过来说，**有残留**是最不能接受的结果。

    哨兵自己启动的 Agent（``--agent`` 模式）如果不收，
    它会变成孤儿进程，在后台继续跑，用户完全不知情。

    ⚠️ **只收自己拉起的** ——
    雷达挂载进来的 Agent 是**用户自己**启动的，
    哨兵**绝不**去杀它（红线 R6）。

    ⚠️ 这里**只**结束进程树，**不**做完整收尾 ——
    完整收尾（关引擎、推晨报）由主流程的 ``finally`` 负责，
    两边各管各的才不会重复推晨报。

    :param sessions: :class:`AgentSession` 列表。
    """
    for item in sessions or []:
        proc = getattr(item, "proc", None)
        if proc is None:
            continue
        try:
            _terminate_tree(proc)
        except Exception as exc:  # noqa: BLE001 —— 收尾失败不影响退出
            print(
                "结束 Agent 进程树失败（{}）：{}: {}".format(
                    getattr(item, "plan", None) and item.plan.id,
                    type(exc).__name__,
                    exc,
                ),
                file=sys.stderr,
            )


class SessionPlan:
    """一路会话的**计划**（从配置文件读出来的描述）。

    :param id: 会话 ID（看板侧边栏用它区分）。
    :param agent: Agent 类型（``mcode`` / ``claude`` / ``codex``）。
    :param task: 交给 Agent 的任务描述。
    :param label: 面板上显示的名字；留空则用 ``id``。
    :param cwd: Agent 的工作目录；留空则用全局 ``--cwd``。
    """

    def __init__(
        self,
        id: str,
        agent: str,
        task: str,
        label: str = "",
        cwd: str = "",
    ) -> None:
        self.id = str(id or "session")
        self.agent = str(agent or "mcode")
        self.task = str(task or "")
        self.label = str(label or self.id)
        self.cwd = str(cwd or "")


def load_session_plans(path: str) -> List[SessionPlan]:
    """从 JSON 文件读会话计划。

    文件格式::

        {
          "sessions": [
            {"id": "codex", "agent": "codex", "task": "写个 README",
             "label": "Codex 写文档", "cwd": "D:/work"},
            {"id": "mcode", "agent": "mcode", "task": "修个 bug"}
          ]
        }

    :param path: JSON 文件路径。
    :return: 会话计划列表。
    :raises ValueError: 文件格式不对或 Agent 类型不认识。
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw_text = fh.read()
    except OSError as exc:
        raise ValueError(f"读不了会话配置文件 {path}：{exc}") from exc
    try:
        data = json.loads(raw_text)
    except ValueError as exc:
        raise ValueError(f"会话配置文件 {path} 不是合法 JSON：{exc}") from exc

    raw = data.get("sessions") if isinstance(data, dict) else data
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"会话配置文件 {path} 里没有 sessions 列表。")

    plans: List[SessionPlan] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise TypeError(f"第 {index + 1} 个会话不是字典。")
        agent = str(item.get("agent") or "mcode")
        if agent not in AGENT_PROFILES:
            raise ValueError(f"第 {index + 1} 个会话的 Agent 类型不认识：{agent}")
        plan = SessionPlan(
            id=str(item.get("id") or f"{agent}-{index + 1}"),
            agent=agent,
            task=str(item.get("task") or ""),
            label=str(item.get("label") or ""),
            cwd=str(item.get("cwd") or ""),
        )
        if not plan.task:
            raise ValueError(f"第 {index + 1} 个会话没有 task。")
        plans.append(plan)
    return plans


class AgentSession:
    """一路被接管的 Agent —— 进程、引擎、守护者、管道、看板会话。

    单路模式下它就是「一个 Agent」；
    多路（中枢）模式下它是「面板侧边栏里的一个条目」。

    :param plan: 会话计划。
    :param config: 哨兵配置。
    :param args: 命令行参数（提供端口、日志路径等）。
    :param raw_log: 该会话的原始日志落盘路径。
    :param alert_log: 该会话的告警日志路径。
    """

    def __init__(
        self,
        plan: SessionPlan,
        *,
        config: Dict[str, Any],
        args: Any,
        raw_log: str,
        alert_log: str,
        notifier: Optional[Any] = None,
    ) -> None:
        self.plan = plan
        self.profile = AGENT_PROFILES[plan.agent]
        self.config = config
        self.args = args
        self.raw_log = os.path.abspath(raw_log)
        self.alert_log = os.path.abspath(alert_log)
        #: 通知通道 —— **所有会话共用一个**。
        #: 每路各建一个的话，同一件事会被推 N 次到手机上 ——
        #: 那不是「多路复用」，是「多路骚扰」。
        self.notifier = notifier
        self.proc: Optional[Any] = None
        self.engine: Optional[Any] = None
        self.guardian: Optional[Any] = None
        self.pipe: Optional[Any] = None
        self.context_tracker: Optional[Any] = None
        self.intent_tracker: Optional[Any] = None
        self.watch_target: Optional[Any] = None
        self.llm_client: Optional[Any] = None
        self.counts: Dict[str, int] = {}
        #: 晨报统计（收尾时生成）。
        self.briefing_stats: Optional[Any] = None
        #: 晨报落盘路径。
        self.briefing_path: str = ""
        #: 是否在会话结束时自动生成晨报（v1.2.0）。
        self.briefing_enabled = True
        #: Agent 的工作目录（安全点与留言箱都落在它旁边）。
        self.workdir = os.path.abspath(self.plan.cwd or args.cwd)
        #: 留言箱（v2.0.0）—— 跨进程的指令通道。
        self.inbox: Optional[Any] = None
        #: 是否允许看板下发指令（配置 ``intervention.enabled``）。
        self.intervention_enabled = True
        #: 安全点服务（v2.0.0）—— 红线拦截前打快照。
        self.safe_point_service: Optional[Any] = None
        #: 留言箱中继线程（v2.0.0）。
        self._relay_thread: Optional[threading.Thread] = None
        #: 已经中继过的留言 ID（不重复投）。
        self._relayed: set = set()
        #: v2.1.0：Agent 进程的**代数**。
        #: 「回滚并重试」会重拉一个新进程 —— 代数用来区分
        #: 「旧进程的尾巴」和「新进程的内容」。
        self._generation = 0
        #: v3.2.0：已经**起过读者**的那一代。
        #:
        #: ⚠️ 「重拉」路径上，``relaunch()`` 会为新进程起一个读者，
        #: 随后 ``run()`` 又会为**同一个**新进程再起一个 ——
        #: 同一行日志会被推进队列**两次**，事件数直接翻倍。
        #: 记下代数就能挡住这个重复。
        self._reader_generation = -1
        #: v2.1.0：stdout 行的共享队列（重拉时复用同一个）。
        self._lines: Any = None
        #: v4.1.0：stdout / stderr 的**限幅缓冲** ——
        #: 鉴权识别要靠这两边的措辞（没登录 / 服务没起）。
        self._stdout_buf: Any = None
        self._stderr_buf: Any = None
        #: v4.1.0：本次运行被判成「未登录」或「运行期失败」。
        self.needs_login = False
        self.runtime_error = False
        #: v2.1.0：拉起 Agent 用的参数（重拉时复用）。
        self._opts: Dict[str, Any] = {}
        #: v2.1.0：复活锁 —— 同一会话的并发点击只有一个能进去。
        self._resume_lock = threading.Lock()

    # -- 准备 -----------------------------------------------------------
    def prepare(self) -> bool:
        """启动 Agent 进程并把审查链路搭好。

        :return: 成功返回 ``True``；失败（例如找不到可执行文件）返回 ``False``。
        """
        args = self.args
        executable = self.profile.resolve_executable()
        if not executable:
            _say(
                f"找不到 {self.profile.label} 的可执行文件 —— "
                f"请确认它已安装并在 PATH 里。"
            )
            return False
        opts: Dict[str, Any] = {
            "cwd": os.path.abspath(self.plan.cwd or args.cwd),
            "model": args.model,
            "max_steps": args.max_steps,
            "permission": args.permission,
            "extra_args": list(args.extra_args),
        }
        self.workdir = os.path.abspath(self.plan.cwd or args.cwd)
        # v2.1.0：拉起参数留着，重拉 Agent 时要复用同一套。
        self._opts = opts
        # 留言箱 + 安全点服务（v2.0.0）——
        # 两者都落在 Agent 的工作目录旁边，用户找得到、删得掉。
        from src.core.injector import InboxStore
        from src.core.safepoint import SafePointService

        intervention_config = self.config.get("intervention") or {}
        self.intervention_enabled = bool(
            intervention_config.get("enabled", True)
        )
        inbox_name = str(
            intervention_config.get("inbox_name") or "sentinel_inbox.json"
        )
        self.inbox = InboxStore(os.path.join(self.workdir, inbox_name))
        safe_config = self.config.get("safe_point") or {}
        if bool(safe_config.get("enabled", True)):
            self.safe_point_service = SafePointService(
                root=str(
                    safe_config.get("root")
                    or os.path.join(self.workdir, ".sentinel", "safepoints")
                ),
                max_bytes=int(
                    safe_config.get("max_bytes") or 64 * 1024 * 1024
                ),
                max_files=int(safe_config.get("max_files") or 20000),
                max_seconds=float(safe_config.get("max_seconds") or 3.0),
                recent_minutes=int(safe_config.get("recent_minutes") or 30),
            )
        else:
            self.safe_point_service = None
        # 留言箱里待投的消息 → 拼进任务提示（重启流程注入上下文）。
        task = self.build_task_with_inbox(self.plan.task)
        cmd = self.profile.build_command(executable, task, opts)

        _say(f"[{self.plan.id}] 正在启动 {self.profile.label} …")
        _say(f"[{self.plan.id}] 命令：" + " ".join(cmd))
        if not self._spawn(cmd, opts):
            return False

        # ---- 守护者：红线级会挂起 Agent 进程 ----
        # 关键：要挂起的是**真正在跑 Agent 的那个进程**，
        # 而不是 cmd.exe 垫片，也不是 Agent 临时拉起来跑命令的 pwsh。
        from src.core.guardian import Guardian

        agent_pid = _resolve_agent_pid(
            self.proc, agent_names=self.profile.process_names
        )
        if not args.no_guardian:
            self.guardian = Guardian(
                self.config,
                target_pid=agent_pid,
                on_event=lambda event: _say(
                    f"[{self.plan.id}] "
                    + (str(event.get("message") or "") if event else "")
                ),
                # 万一原 PID 已经没了（Agent 重启过 / 认错了进程），
                # 让守护者能重新解析一次再拦截，别让红线白溜过去。
                pid_resolver=lambda: _resolve_agent_pid(
                    self.proc, timeout=1.0,
                    agent_names=self.profile.process_names,
                ),
            )
            if self.guardian.enabled:
                _say(
                    f"[{self.plan.id}] 守护者已武装，目标进程 PID={agent_pid}"
                    f"（启动器 PID={self.proc.pid}）"
                )
            else:
                _say(f"[{self.plan.id}] 守护者未启用（intervention_mode 不是 guardian）")

        self._build_engine()
        # 时间线的**第一个节点**（v2.2.0）：开始目标。
        #
        # 为什么由启动器写而不是引擎写：这一刻引擎刚建好，
        # 而「用户交代了什么任务」是启动器才知道的事。
        self.emit_event(
            "session_start",
            "开始目标",
            str(self.plan.task or "")[:200],
            agent=self.profile.name,
            task=str(self.plan.task or "")[:400],
        )
        # 留言箱中继（v2.0.0）—— 把看板写进来的指令转给 Agent。
        self.start_inbox_relay()
        return True

    # ------------------------------------------------------------------
    # 控制中枢（v2.0.0）：把话送给 Agent / 一键回滚
    # ------------------------------------------------------------------
    def emit_event(
        self,
        kind: str,
        title: str,
        detail: str = "",
        *,
        severity: str = "info",
        safe_point_id: str = "",
        point_id: str = "",
        **extra: Any,
    ) -> Dict[str, Any]:
        """写一条「操作事件」（v2.2.0）—— 时间线的原料。

        告警说的是「**发现了什么**」，事件说的是「**我们做了什么**」。
        启动器是**执行动作的那一方**（复活、回滚、注入都在这儿发生），
        所以事件由它写，而不是由引擎写。

        :param kind: 事件类型。
        :param title: 一句话标题。
        :param detail: 补充说明。
        :param severity: 严重程度。
        :param safe_point_id: 关联的安全点 ID。
        :param point_id: 算 Diff 用的回滚基线 ID。
        :param extra: 附加信息。
        :return: 事件字典。
        """
        return _write_event(
            self.alert_log,
            kind,
            title,
            detail,
            severity=severity,
            safe_point_id=safe_point_id,
            point_id=point_id,
            **extra,
        )

    def _spawn(self, cmd: List[str], opts: Dict[str, Any]) -> bool:
        """拉起 Agent 子进程。

        v2.1.0 把它抽出来，是因为「回滚并重试」要**重拉**一次 ——
        第一次启动和重拉必须是同一套参数，
        否则重拉出来的 Agent 跟原不是一个配置。

        :param cmd: 命令行。
        :param opts: 启动参数。
        :return: 成功返回 ``True``。
        """
        try:
            self.proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                # 只有**真的读 stdin** 的 CLI 才给它管道 ——
                # 给不读 stdin 的 CLI 开管道没有意义，
                # 还会让人误以为「写进去就算送到了」。
                #
                # ⚠️ 不读 stdin 的 CLI 给 **DEVNULL**，**不继承**父进程的 stdin
                # （v3.2.0 修的真缺陷）：
                # 「继承」意味着子进程可能从父进程那儿拿到一条**永远不关闭**的管道
                # —— 一次性 exec 模式一旦因为某种原因去读 stdin
                # （比如某个 CLI 想要确认操作），它就会**永久阻塞**。
                # 实测踩到过：后台跑的哨兵把 mcode 拉起来后，
                # mcode 卡在等 stdin 上，stdout 也不吐一个字，进程永远不结束。
                # DEVNULL 给的是「立刻 EOF」—— 读不到就报错退出，
                # 而不是挂在那里等天荒地老。
                stdin=(
                    subprocess.PIPE
                    if self.profile.supports_stdin
                    else subprocess.DEVNULL
                ),
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                cwd=opts["cwd"],
                # POSIX 上让子进程自成会话，收尾时能整组收掉。
                start_new_session=(os.name != "nt"),
            )
        except FileNotFoundError:
            _say(f"找不到 {self.profile.label} 命令 —— 请确认它已安装。")
            return False
        return True

    def agent_state(self) -> Dict[str, Any]:
        """当前 Agent 进程的状态（复活计划要给用户看）。

        :return: ``{"ok": bool, "pid": int, "suspended": bool, ...}``。
        """
        proc = self.proc
        alive = proc is not None and proc.poll() is None
        suspended = False
        pid = proc.pid if proc is not None else 0
        if self.guardian is not None:
            try:
                status = self.guardian.status()
                suspended = bool(pid in (status.get("suspended_pids") or []))
            except Exception:  # noqa: BLE001 —— 查询失败只降级
                suspended = False
        return {
            "ok": True,
            "pid": pid,
            "alive": alive,
            "suspended": suspended,
        }

    def relaunch(self, prompt: str = "") -> Dict[str, Any]:
        """重新拉起 Agent（任务提示里带上纠偏提示）。

        为什么不是「放行」：被挂起的进程正卡在一条**危险命令**上，
        放行它等于放行那条命令。详见 :mod:`src.core.resurrect`。

        :param prompt: 纠偏提示正文（会先落进留言箱，再拼进任务提示）。
        :return: 结果字典。
        """
        if not self._resume_lock.acquire(blocking=False):
            return {
                "ok": False,
                "detail": "这个会话正在复活中，请等它结束。",
            }
        try:
            executable = self.profile.resolve_executable()
            if not executable:
                return {
                    "ok": False,
                    "detail": f"找不到 {self.profile.label} 的可执行文件。",
                }
            # 旧进程：它卡在危险命令上，直接结束。
            old = self.proc
            if old is not None:
                _terminate_tree(old)
                self.proc = None
            # 任务提示里带上留言箱（纠偏提示已经在里面了）。
            task = self.build_task_with_inbox(self.plan.task)
            cmd = self.profile.build_command(
                executable, task, self._opts or {}
            )
            _say(f"[{self.plan.id}] 正在重新拉起 {self.profile.label} …")
            if not self._spawn(cmd, self._opts or {}):
                return {"ok": False, "detail": "重新拉起失败：找不到命令。"}
            # 代数 +1 —— 旧进程的尾巴会被主循环丢掉。
            self._generation += 1
            self._start_reader(self.proc)
            # 新进程的 stderr 也要有人读干净 ——
            # 没人读的话管道会塞满、子进程卡死。
            # v4.1.0：同时**重置**限幅缓冲 ——
            # 新进程的输出不该和旧进程的混在一起判鉴权。
            from src.agents.launcher import BoundedBuffer

            self._stdout_buf = BoundedBuffer()
            self._stderr_buf = BoundedBuffer()
            self.needs_login = False
            self.runtime_error = False
            stderr_thread = threading.Thread(
                target=_drain_stderr,
                args=(self.proc.stderr, "[agent] ", self._stderr_buf),
                name=f"agent-stderr-{self.plan.id}-{self._generation}",
                daemon=True,
            )
            stderr_thread.start()
            # 守护者重新指向新进程。
            if self.guardian is not None:
                agent_pid = _resolve_agent_pid(
                    self.proc, agent_names=self.profile.process_names
                )
                self.guardian.set_target_pid(agent_pid)
                _say(
                    f"[{self.plan.id}] 守护者已重新指向新进程 PID={agent_pid}"
                )
            _say(
                f"[{self.plan.id}] 已重新拉起 "
                f"{self.profile.label}（PID={self.proc.pid}）"
            )
            return {
                "ok": True,
                "pid": self.proc.pid,
                "detail": "已重新拉起 Agent。",
            }
        except Exception as exc:  # noqa: BLE001 —— 重拉失败只降级
            _say(
                f"[{self.plan.id}] 重新拉起失败：{type(exc).__name__}: {exc}"
            )
            return {"ok": False, "detail": f"重新拉起失败：{exc}"}
        finally:
            self._resume_lock.release()

    def build_resurrect_service(self) -> Any:
        """造一个「一键复活」服务。

        ⚠️ 注入器**故意不给 stdin_writer** ——
        旧的 Agent 进程马上就要被结束了，往它的 stdin 写是白写。
        纠偏提示必须落进**留言箱**，新进程起任务时才会带上它。

        :return: :class:`src.core.resurrect.ResurrectService` 实例。
        """
        from src.core.injector import AgentInjector
        from src.core.resurrect import ResurrectService

        if self.inbox is None:
            from src.core.injector import InboxStore

            self.inbox = InboxStore(
                os.path.join(self.workdir, "sentinel_inbox.json")
            )
        return ResurrectService(
            safe_point_service=self.safe_point_service,
            injector=AgentInjector(
                inbox=self.inbox,
                stdin_writer=None,  # ← 只走留言箱
                agent_label=self.profile.name,
            ),
            guardian=self.guardian,
            agent_control=self,
            label=self.plan.id,
        )

    def resume_plan(
        self, point_id: str = "", workdir: str = ""
    ) -> Dict[str, Any]:
        """看板的【复活计划】回调（只算不动）。

        :param point_id: 安全点 ID；留空表示回滚到基线。
        :param workdir: 工作目录。
        :return: 计划字典。
        """
        service = self.build_resurrect_service()
        return service.plan(
            point_id, str(workdir or self.workdir or "")
        )

    def resume(self, prompt: str, meta: Dict[str, Any]) -> Dict[str, Any]:
        """看板的【回滚并重试】回调。

        :param prompt: 纠偏提示正文。
        :param meta: 附加信息。
        :return: 复活结果字典。
        """
        if not getattr(self, "intervention_enabled", True):
            return {
                "ok": False,
                "error": "指令注入功能已关闭（配置 intervention.enabled=false）。",
            }
        service = self.build_resurrect_service()
        result = service.execute(prompt, meta)
        if result.get("ok"):
            _say(f"[{self.plan.id}] {result.get('message')}")
            # 时间线节点（v2.2.0）：复活重试。
            self.emit_event(
                "resurrect",
                "复活重试",
                str(result.get("message") or ""),
                severity="warn",
                point_id=str((meta or {}).get("point_id") or ""),
                alert_id=str((meta or {}).get("alert_id") or ""),
                rule_id=str((meta or {}).get("rule_id") or ""),
            )
        return result

    def build_injector(self) -> Any:
        """造一个注入器（看板的【直接发送给 Agent】用它）。

        :return: :class:`src.core.injector.AgentInjector` 实例。
        """
        from src.core.injector import AgentInjector, InboxStore

        if self.inbox is None:
            self.inbox = InboxStore(
                os.path.join(self.workdir, "sentinel_inbox.json")
            )
        return AgentInjector(
            inbox=self.inbox,
            stdin_writer=(
                self.write_stdin if self.stdin_available() else None
            ),
            agent_label=self.profile.name,
        )

    def stdin_available(self) -> bool:
        """Agent 到底会不会读标准输入。

        :return: ``True`` 表示可以直写 stdin。
        """
        if not self.profile.supports_stdin:
            return False
        if self.proc is None:
            return False
        return getattr(self.proc, "stdin", None) is not None

    def write_stdin(self, text: str) -> bool:
        """往 Agent 的标准输入写一段话。

        **写成功不等于对方读了** —— 所以调用方要如实告诉用户
        「写进去了，但这个 CLI 未必会消费」。

        :param text: 文本。
        :return: 写成功返回 ``True``。
        """
        proc = self.proc
        if proc is None:
            return False
        handle = getattr(proc, "stdin", None)
        if handle is None:
            return False
        try:
            handle.write(str(text or "").rstrip() + "\n")
            handle.flush()
        except (OSError, ValueError) as exc:
            _say(
                f"[{self.plan.id}] 写 Agent 标准输入失败："
                f"{type(exc).__name__}: {exc}"
            )
            return False
        return True

    def inject(self, prompt: str, meta: Dict[str, Any]) -> Dict[str, Any]:
        """看板的【直接发送给 Agent】回调。

        :param prompt: 提示正文。
        :param meta: 附加信息。
        :return: 注入结果字典。
        """
        if not getattr(self, "intervention_enabled", True):
            return {
                "ok": False,
                "error": "指令注入功能已关闭（配置 intervention.enabled=false）。",
            }
        injector = self.build_injector()
        result = injector.inject(
            prompt,
            session_id=self.plan.id,
            verdict=str((meta or {}).get("verdict") or ""),
            rule_id=str((meta or {}).get("rule_id") or ""),
            reason=str((meta or {}).get("reason") or ""),
        )
        data = result.to_dict()
        if data.get("channel") == "stdin":
            _say(f"[{self.plan.id}] 指令已写入 Agent 标准输入。")
        else:
            _say(
                f"[{self.plan.id}] Agent 不读标准输入，指令已写入留言箱："
                f"{data.get('inbox_path')}"
            )
        # 时间线节点（v2.2.0）：注入纠偏提示。
        self.emit_event(
            "inject",
            "注入纠偏提示",
            "通道：{}".format(data.get("channel") or "-"),
            rule_id=str((meta or {}).get("rule_id") or ""),
            alert_id=str((meta or {}).get("alert_id") or ""),
        )
        return data

    def rollback(self, point_id: str = "", workdir: str = "") -> Dict[str, Any]:
        """看板的【一键回滚至拦截点】回调。

        :param point_id: 安全点 ID；留空表示回滚到基线。
        :param workdir: 工作目录；留空用会话自己的。
        :return: 回滚结果字典。
        """
        service = self.safe_point_service
        if service is None:
            return {"ok": False, "error": "这个会话没有开启安全点。"}
        target = os.path.abspath(str(workdir or self.workdir))
        outcome = service.restore(point_id, target)
        if outcome.get("ok"):
            _say(
                f"[{self.plan.id}] 已回滚：恢复 "
                f"{outcome.get('restored_count', 0)} 个文件，删除 "
                f"{outcome.get('deleted_count', 0)} 个新增文件"
                f"（耗时 {outcome.get('elapsed', 0)} 秒）"
            )
            # 时间线节点（v2.2.0）：回滚。
            self.emit_event(
                "rollback",
                "回滚至安全点",
                "恢复 {} 个文件，删除 {} 个新增文件".format(
                    outcome.get("restored_count", 0),
                    outcome.get("deleted_count", 0),
                ),
                severity="warn",
                safe_point_id=str(point_id or ""),
                point_id=str(point_id or ""),
            )
        return outcome

    def start_inbox_relay(self) -> None:
        """起一个后台线程，把留言箱里的新消息**中继**给 Agent。

        为什么需要中继
        =============

        看板和启动器是**两个进程**。看板把指令写进留言箱，
        但真正握着 Agent 管道的是启动器 —— 所以得有人去取。

        取到之后：

        * Agent 读 stdin → 直接写进去，标记已送达；
        * Agent 不读 stdin → **留在箱里**，
          下次启动时由 :meth:`build_task_with_inbox` 注入任务提示。
        """
        if self._relay_thread is not None:
            return
        self._relay_thread = threading.Thread(
            target=self._relay_loop,
            name=f"sentinel-inbox-{self.plan.id}",
            daemon=True,
        )
        self._relay_thread.start()

    def _relay_loop(self) -> None:
        """留言箱中继循环（后台线程）。"""
        while not getattr(self.args, "stop", False):
            try:
                self._relay_once()
            except Exception as exc:  # noqa: BLE001 —— 中继失败不影响审查
                _say(
                    f"[{self.plan.id}] 留言箱中继出错："
                    f"{type(exc).__name__}: {exc}"
                )
            time.sleep(1.0)

    def _relay_once(self) -> None:
        """取一次留言箱，把没投过的消息试着投给 Agent。"""
        if self.inbox is None:
            return
        pending = self.inbox.pending()
        if not pending:
            return
        for message in pending:
            mid = str(message.id)
            if mid in self._relayed:
                continue
            self._relayed.add(mid)
            if not self.stdin_available():
                # 这个 CLI 不读 stdin —— 消息留在箱里，
                # 下次启动时注入任务提示。
                continue
            if self.write_stdin(message.prompt):
                self.inbox.mark_delivered([mid])
                _say(f"[{self.plan.id}] 留言已中继给 Agent。")

    def build_task_with_inbox(self, task: str) -> str:
        """把留言箱里**待投**的消息拼进任务提示。

        这是「重启流程注入上下文」的落点 ——
        哪怕 CLI 完全不支持 stdin，
        下次启动时 Agent 也能一眼看到哨兵之前说了什么。

        :param task: 原始任务描述。
        :return: 拼好的任务描述。
        """
        if self.inbox is None:
            return task
        try:
            block = self.inbox.render_for_prompt()
        except Exception:  # noqa: BLE001 —— 取不到就当没有留言
            return task
        if not block:
            return task
        _say(f"[{self.plan.id}] 留言箱里有待投消息，已注入任务提示。")
        return f"{task}\n\n{block}"

    def _build_engine(self) -> None:
        """装配 L2 客户端、上下文追踪器、漂移追踪器、通知通道与引擎。"""
        from src.core.engine import (
            ContextTracker,
            IntentTracker,
            SentinelEngine,
        )
        from src.core.notifier import Notifier
        from src.gui.server import WatchTarget
        from src.llm.factory import build_routing_provider

        args = self.args
        config = self.config

        llm_client = None
        llm_config = config.get("llm") or {}
        if not args.no_llm and llm_config.get("enabled", True):
            try:
                llm_client = build_routing_provider(config)
            except ValueError as exc:  # noqa: BLE001 —— 配置问题只降级
                _say(f"[{self.plan.id}] 模型层配置有误（{exc}），只做规则层判定。")
            except Exception as exc:  # noqa: BLE001 —— 模型不可用只降级
                _say(f"[{self.plan.id}] 模型不可用（{exc}），只做规则层判定。")
        self.llm_client = llm_client

        # --- 上下文追踪器：STUCK（连续测试失败）/ LEAK（敏感文件后外发）---
        #
        # ⚠️ 刻意**不**复用 CLI 的 ``--no-context`` 开关：
        # 这里是「真实管道接管」路径，不是纯终端审查路径。
        # 之前这条路径**漏接**了上下文追踪器 —— 结果是
        # 「连续跑失败的 pytest」这种最典型的 Agent 卡死场景，
        # 在真实接管时**一次都不会报**。
        # 上下文检测**只读**、**纯状态机**，不写文件也不发请求，接上零风险。
        context_config = config.get("context") or {}
        guardian_config = config.get("guardian") or {}
        context_tracker = ContextTracker(
            window=int(context_config.get("window", 5)),
            stuck_threshold=int(context_config.get("stuck_threshold", 3)),
            enabled=bool(context_config.get("enabled", True)),
            internal_hosts=guardian_config.get("internal_hosts"),
        )

        drift_config = config.get("drift") or {}
        intent_tracker = None
        if llm_client is not None and bool(drift_config.get("enabled", True)):
            intent_tracker = IntentTracker(
                goal=str(config.get("task_goal") or ""),
                window=int(drift_config.get("window", 5)),
                threshold=int(drift_config.get("threshold", 2)),
                enabled=True,
                evaluator=llm_client.evaluate_drift,
            )
        self.intent_tracker = intent_tracker

        # --- 时间盒（v3.3.0）：「一个子任务吃掉整轮」的硬性拦截 ---
        from src.core.timebox import build_timebox

        timebox_detector = build_timebox(config)

        notifier = self.notifier
        if notifier is None and not args.no_notify:
            try:
                notifier = Notifier(config)
                _say(f"告警通道已装配：{len(notifier.channels)} 个")
            except Exception as exc:  # noqa: BLE001 —— 通知层坏了不阻断审查
                _say(f"通知通道不可用（{exc}），仅终端与日志。")

        self.engine = SentinelEngine(
            build_adapter(self.profile.adapter),
            _build_rule_engine(config),
            alert_log_path=self.alert_log,
            console=True,
            llm_client=llm_client,
            context_tracker=context_tracker,
            intent_tracker=intent_tracker,
            notifier=notifier,
            guardian=self.guardian,
            agent_name=self.profile.name,
            # 安全点（v2.0.0）—— 红线拦截前给工作目录打快照。
            safe_point_service=self.safe_point_service,
            workdir=self.workdir,
            # 时间盒（v3.3.0）
            timebox=timebox_detector,
        )

        # --- 引擎心跳看门狗（v3.3.0）---
        #
        # ⚠️ 必须在**引擎建好之后**再起 —— 回调要指向引擎。
        # 看门狗是进程内共享的（同地址只起一个），生命周期归进程管。
        try:
            from src.core.engine_watchdog import start_engine_watchdog

            watchdog = start_engine_watchdog(config)
            if watchdog is not None:
                self.engine.set_engine_watchdog(watchdog)
                _say(
                    "引擎心跳看门狗已启动（每 {} 秒探一次 {}）。".format(
                        int(watchdog.interval), watchdog.base_url
                    )
                )
        except Exception as exc:  # noqa: BLE001 —— 看门狗失败不阻断审查
            _say(f"引擎心跳看门狗启动失败（{exc}），不影响审查。")

        self.watch_target = WatchTarget(
            agent=self.profile.name,
            goal=str(config.get("task_goal") or ""),
            goal_source="config" if config.get("task_goal") else "none",
        )

        self.pipe = SentinelPipe(
            self.engine, build_adapter(self.profile.adapter), echo=True
        )
        self.pipe.open_sink(self.raw_log)

    # -- 运行 -----------------------------------------------------------
    def run(self) -> None:
        """读 Agent 的 stdout，逐事件送审（阻塞到本轮结束）。"""
        proc = self.proc
        pipe = self.pipe
        assert proc is not None and pipe is not None
        # v4.1.0：收 stdout / stderr 的**限幅缓冲** ——
        # 收尾时要靠它们的措辞判断「这个 CLI 是不是没登录」。
        from src.agents.launcher import BoundedBuffer

        self._stdout_buf = BoundedBuffer()
        self._stderr_buf = BoundedBuffer()
        stderr_thread = threading.Thread(
            target=_drain_stderr,
            args=(proc.stderr, "[agent] ", self._stderr_buf),
            name=f"agent-stderr-{self.plan.id}",
            daemon=True,
        )
        stderr_thread.start()

        _say(f"[{self.plan.id}] {self.profile.label} PID={proc.pid}，开始实时审查 …")
        exec_done = False
        last_line_at = time.monotonic()
        grace = 3.0  # 结束标记之后最多再等几秒收尾
        import queue as _queue

        # v2.1.0：队列**挂在会话上**并复用 ——
        # 「回滚并重试」重拉的新进程会把内容推进同一个队列。
        if self._lines is None:
            self._lines = _queue.Queue()
        lines = self._lines
        self._start_reader(proc)
        while True:
            try:
                generation, line = lines.get(timeout=0.5)
            except _queue.Empty:
                current = self.proc
                if exec_done and time.monotonic() - last_line_at > grace:
                    break
                if current is not None and current.poll() is not None:
                    break
                continue
            if generation != self._generation:
                # 旧进程的尾巴（它刚被结束掉）—— 直接丢掉。
                continue
            if line is None:
                break
            last_line_at = time.monotonic()
            text = line.rstrip("\n")
            self._stdout_buf.append(text + "\n")
            pipe.feed_line(text)
            if any(marker in text for marker in self.profile.done_markers):
                # Agent 宣布本轮结束 —— 后面不会再有内容了。
                exec_done = True
            # 目标可能刚被自动锚定 —— 面板要跟着刷新。
            _sync_watch_target(self.watch_target, self.intent_tracker)

        self._judge_auth_failure()

    def _judge_auth_failure(self) -> None:
        """收尾判断「这个 CLI 是不是没登录 / 服务没起」。

        v4.1.0：吸收上游 ``bridge/runner.py`` 的**鉴权识别两层机制**。

        为什么必须单独判
        ===============

        这些 CLI 未登录时**退出码往往是 0**，只往 stderr 打一行
        「Authentication required. Please use /login command to sign in」。
        于是「进程成功退出」会被当成「任务完成」，
        那行登录提示被当成**任务输出**交给上层模型 ——
        模型只能从一段莫名其妙的输出里猜「哦原来要登录」。

        更麻烦的是**退出码不能当门槛**：gemini 未登录是 41、
        command-code 是 3、claude 却是 0。

        判定门槛见 :mod:`src.agents.auth`（stderr 要短、只看开头、
        stdout 不能有实质内容）。
        """
        if self._stdout_buf is None or self._stderr_buf is None:
            return
        from src.agents import load_agents
        from src.agents.auth import (
            detect_auth_failure,
            detect_runtime_failure,
        )

        stdout = self._stdout_buf.value()
        stderr = self._stderr_buf.value()
        spec = load_agents(strict=False).get(self.profile.name)
        extra_auth = list(spec.auth_patterns) if spec else []
        extra_fail = list(spec.fail_patterns) if spec else []

        auth_hit = detect_auth_failure(stdout, stderr, extra_auth)
        if auth_hit:
            self.needs_login = True
            _say(
                f"[{self.plan.id}] ⚠️ {self.profile.label} 疑似**未登录**"
                f"（退出码不代表成功）：{auth_hit}"
            )
            self.emit_event(
                kind="agent_auth",
                title=f"{self.profile.label} 未登录",
                detail=auth_hit,
                severity="warn",
            )
            return

        fail_hit = detect_runtime_failure(stdout, stderr, extra_fail)
        if fail_hit:
            self.runtime_error = True
            _say(
                f"[{self.plan.id}] ⚠️ {self.profile.label} 运行期依赖没就绪："
                f"{fail_hit}"
            )
            self.emit_event(
                kind="agent_runtime_error",
                title=f"{self.profile.label} 运行期失败",
                detail=fail_hit,
                severity="warn",
            )

    def _start_reader(self, proc: Any) -> None:
        """起一个线程读某个 Agent 进程的 stdout，推进共享队列。

        v2.1.0：读线程带**代数** —— 重拉之后，
        旧进程晚到的 EOF 不会把主循环带沟里。

        ⚠️ v3.2.0：**同一代只起一个读者** ——
        「重拉」路径上 ``relaunch()`` 已经为新进程起了读者，
        随后 ``run()`` 又会为**同一个**新进程起一个 ——
        同一行日志会被推进队列**两次**，事件数直接翻倍。
        （生产路径里 ``run()`` 只调一次，所以这是演习脚本逼出来的坑。）

        :param proc: 目标子进程。
        """
        if proc is None or getattr(proc, "stdout", None) is None:
            return
        if self._reader_generation == self._generation:
            # 本代已经有读者了 —— 再起一个就会把每行读两遍。
            return
        self._reader_generation = self._generation
        reader = threading.Thread(
            target=_read_lines,
            args=(proc.stdout, self._lines, self._generation),
            name=f"agent-stdout-{self.plan.id}-{self._generation}",
            daemon=True,
        )
        reader.start()

    def close(self) -> None:
        """收尾：停进程、等后台线程、释放资源。"""
        if self.proc is not None:
            _terminate_tree(self.proc)
        if self.pipe is not None:
            self.pipe.close_sink()
        engine = self.engine
        if engine is not None:
            # 漂移判定与 L2 解释都在后台线程 —— 收尾前等它们回来，
            # 否则告警与解释会丢。
            engine.drain_timeout = 20.0
            if self.intent_tracker is not None:
                self.intent_tracker.drain(timeout=20.0)
            engine.explanation_worker.drain(timeout=20.0)
            # ⚠️ 晨报必须在**通道关闭之前**推出去（v3.2.0 修的真缺陷）：
            #
            # ``engine.close()`` 会把 notifier 关掉（通道的投递线程随之停止）。
            # 如果晨报在那之后才入队，队列里就**没人送了** ——
            # 手机上什么也没收到，而终端还印着「晨报摘要已推送到你的手机」。
            # 那是**说谎**，比不推还糟。
            #
            # 所以顺序是：定结束时间 → 推晨报 → 再关引擎。
            if not engine.ended_at:
                engine.ended_at = time.time()
            self.write_briefing()
            engine.close()
            self.counts = dict(engine.counts)
        else:
            self.write_briefing()
        if self.guardian is not None:
            try:
                self.guardian.close()
            except Exception as exc:  # noqa: BLE001 —— 释放失败不影响退出
                print(
                    "释放守护者失败：{}: {}".format(type(exc).__name__, exc),
                    file=sys.stderr,
                )
        # ⚠️ 这里**不再**调 ``write_briefing()`` ——
        # 上面已经在「通道关闭之前」推过一次了。
        # 推两次的话：手机上会收到**两份**一模一样的晨报，
        # 归档文件里也会多出一节。

    # -- 晨报 -----------------------------------------------------------
    def build_briefing(self) -> Dict[str, Any]:
        """生成晨报内容（**不落盘**）。

        看板的【生成晨报】按钮通过这个回调取内容 ——
        看板层因此**不需要**认识引擎（那条边界不能破）。

        :return: ``{"ok": True, "markdown": …, "terminal": …, "summary": …}``；
            失败时返回 ``{"ok": False, "error": …}``。
        """
        if self.engine is None:
            return {"ok": False, "error": "这个会话还没有启动引擎。"}
        try:
            from src.core.briefing import (
                build_briefing_summary,
                collect_from_engine,
                render_markdown,
                render_terminal,
            )

            stats = collect_from_engine(
                self.engine,
                session_id=self.plan.id,
                agent=self.profile.name,
            )
            self.briefing_stats = stats
            title = f"会话 {self.plan.id}"
            if self.profile.name:
                title += f" · {self.profile.name}"
            return {
                "ok": True,
                # 归档里的每日晨报是**分节**的 ——
                # 多路会话合到同一份文件里，每一路占一节。
                "markdown": render_markdown(
                    stats, title=title, heading_level=2
                ),
                "terminal": render_terminal(stats),
                # v3.2.0：**提炼过的摘要** —— 手机推送用。
                # 完整晨报几 KB，塞进聊天工具会被截断。
                "summary": build_briefing_summary(stats, title=title),
            }
        except Exception as exc:  # noqa: BLE001 —— 晨报失败不外泄
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    def push_briefing(self, summary: Dict[str, Any]) -> bool:
        """把晨报摘要推给用户的手机（Webhook）。

        ⚠️ 推送失败**绝不影响收尾** —— 晨报已经落盘了，
        推不出去只是「手机上没收到」，不是「晨报没了」。

        :param summary: 晨报摘要。
        :return: 成功入队返回 ``True``。
        """
        notifier = self.notifier
        if notifier is None:
            return False
        try:
            delivered = notifier.push_briefing(summary)
        except Exception as exc:  # noqa: BLE001 —— 推送失败不外泄
            _say(f"[{self.plan.id}] 晨报推送失败（{exc}），晨报已落盘。")
            return False
        if delivered:
            _say(f"[{self.plan.id}] 晨报摘要已推送到你的手机。")
        return bool(delivered)

    def briefing_callback(self) -> Dict[str, Any]:
        """看板【生成晨报】按钮的回调 —— 生成**并落盘**。

        :return: ``{"ok": True, "markdown": …, "terminal": …, "path": …}``。
        """
        payload = self.build_briefing()
        if not payload.get("ok"):
            return payload
        try:
            from src.core.briefing import save_briefing

            path = save_briefing(
                str(payload.get("markdown") or ""),
                session_id=self.plan.id,
            )
            self.briefing_path = path
            payload["path"] = path
        except Exception as exc:  # noqa: BLE001 —— 落盘失败不该丢掉内容
            payload["path"] = ""
            payload["error"] = f"晨报已生成，但保存失败：{exc}"
        # v3.2.0：手动点【生成晨报】也顺手推一份到手机。
        summary = payload.get("summary")
        if isinstance(summary, dict) and summary:
            self.push_briefing(summary)
        return payload

    def write_briefing(self) -> str:
        """生成并保存《哨兵值守晨报》（v1.2.0）。

        收尾时自动调用 —— 用户挂了一整夜，早上打开电脑就该有一份总结，
        而不是自己去翻日志。

        v3.2.0：保存之后**顺手推一份摘要到手机** ——
        早上醒来不用去电脑里翻文件。

        :return: 落盘路径；没生成时返回空串。
        """
        if not self.briefing_enabled or self.engine is None:
            return ""
        try:
            from src.core.briefing import save_briefing

            payload = self.build_briefing()
            if not payload.get("ok"):
                _say(
                    f"[{self.plan.id}] 晨报生成失败"
                    f"（{payload.get('error')}），不影响收尾。"
                )
                return ""
            path = save_briefing(
                str(payload.get("markdown") or ""),
                session_id=self.plan.id,
            )
            self.briefing_path = path
            _say(str(payload.get("terminal") or ""))
            _say(f"[{self.plan.id}] 晨报已保存：{path}")
            # v3.2.0：晨报**生成完毕** → 自动推送到用户配置的地址。
            summary = payload.get("summary")
            if isinstance(summary, dict) and summary:
                self.push_briefing(summary)
            return path
        except Exception as exc:  # noqa: BLE001 —— 晨报失败绝不影响收尾
            _say(f"[{self.plan.id}] 晨报生成失败（{exc}），不影响收尾。")
            return ""

    def summary(self) -> str:
        """给终端看的一句收尾小结。"""
        engine = self.engine
        pipe = self.pipe
        counts = self.counts or dict(engine.counts if engine else {})
        return (
            "[{id}] 审查结束：事件 {events} 条（坏行 {bad}）｜ "
            "ON_TRACK={ok}，DANGEROUS={danger}，LOOP={loop}｜ 告警 {alerts} 条".format(
                id=self.plan.id,
                events=pipe.events if pipe else 0,
                bad=pipe.bad_lines if pipe else 0,
                ok=counts.get("ON_TRACK", 0),
                danger=counts.get("DANGEROUS", 0),
                loop=counts.get("LOOP", 0),
                alerts=len(engine.alerts) if engine else 0,
            )
        )


def _build_rule_engine(config: Dict[str, Any]) -> Any:
    """按配置造一个规则引擎。

    :param config: 哨兵配置。
    :return: :class:`~src.rules.basic.RuleEngine` 实例。
    """
    from src.rules.basic import RuleEngine

    return RuleEngine(config)


def _run_dashboard(server: Any, host: str, port: int) -> None:
    """看板线程主体。

    端口被占用这类异常必须在这里**转成人话** ——
    直接往外抛，用户看到的是一屏红色异常栈，
    而他真正需要知道的只有一句「端口被占用，换一个」。

    :param server: uvicorn Server 实例。
    :param host: 监听地址。
    :param port: 监听端口。
    """
    from src.console import print_port_busy

    try:
        server.run()
    except OSError:
        print_port_busy(host, port)
    except Exception as exc:  # noqa: BLE001 —— 看板异常不影响审查
        _say(f"看板异常退出（{type(exc).__name__}: {exc}）。")


def _start_dashboard(
    guardian: Any,
    watch_path: str,
    alert_log_path: str,
    host: str,
    port: int,
    watch_target: Any = None,
    session_specs: Optional[List[Any]] = None,
    briefing_callback: Any = None,
    inject_callback: Any = None,
    rollback_callback: Any = None,
    resume_callback: Any = None,
    plan_callback: Any = None,
    safe_point_service: Any = None,
    workdir: str = "",
) -> Any:
    """在本进程里把看板跑起来（守护者与引擎是**同一个**实例）。

    同进程是关键 —— 看板上的【放行并忽略】按钮恢复的，
    就是引擎挂起的那个 Agent 进程。

    :param guardian: 守护者实例（单路模式用）。
    :param watch_path: 原始日志路径（看板实时显示）。
    :param alert_log_path: 告警日志路径。
    :param host: 监听地址。
    :param port: 监听端口。
    :param watch_target: 「当前监控目标」快照。
    :param session_specs: 多路会话描述；给了就走多路中枢模式。
    :param briefing_callback: 「生成晨报」回调（v1.2.0，单路模式用）。
    :param inject_callback: 「直接发送给 Agent」回调（v2.0.0）。
    :param rollback_callback: 「一键回滚至拦截点」回调（v2.0.0）。
    :param resume_callback: 「回滚并重试」回调（v2.1.0）。
    :param safe_point_service: 安全点服务（v2.0.0）。
    :param workdir: Agent 的工作目录（v2.0.0）。
    :return: uvicorn Server 实例。
    """
    import uvicorn

    from src.gui.server import create_app

    if session_specs:
        app = create_app(
            sessions=session_specs,
            scan_callback=None,
            host=host,
            port=port,
        )
    else:
        app = create_app(
            watch_path,
            alert_log_path,
            scan_callback=None,
            guardian_callback=guardian,
            watch_target=watch_target,
            briefing_callback=briefing_callback,
            # 控制中枢（v2.0.0）
            inject_callback=inject_callback,
            rollback_callback=rollback_callback,
            resume_callback=resume_callback,
            plan_callback=plan_callback,
            safe_point_service=safe_point_service,
            workdir=workdir,
            host=host,
            port=port,
        )
    config = uvicorn.Config(
        app, host=host, port=port, log_level="warning", access_log=False
    )
    return uvicorn.Server(config)


def _resolve_config_path(explicit: str) -> str:
    """找出**实际**在用的配置文件路径。

    热重载要盯的是「用户改得动的那一份」——
    打包运行时那是 exe 旁边的 ``config/config.yaml``，
    源码运行时那是仓库里的 ``config/config.yaml``。

    :param explicit: 用户显式指定的路径；可能为空或不存在。
    :return: 实际使用的配置文件绝对路径；找不到时返回空串。
    """
    try:
        from src.config_loader import default_config_candidates
    except Exception:  # noqa: BLE001 —— 拿不到候选就只用显式路径
        return os.path.abspath(explicit) if explicit else ""
    if explicit and os.path.isfile(explicit):
        return os.path.abspath(explicit)
    for candidate in default_config_candidates():
        if candidate and os.path.isfile(candidate):
            return os.path.abspath(candidate)
    return os.path.abspath(explicit) if explicit else ""


def _run_startup_health(config: Dict[str, Any], workdir: str) -> None:
    """启动时跑一次开箱安全体检（v2.2.0），把报告打到终端。

    用户拿到哨兵的第一件事就该知道「我的环境配好了没」——
    尤其那条「**没配 Webhook = 半夜收不到手机推送**」。

    失败**绝不**抛异常 —— 体检失败不该挡住哨兵启动（红线 R5）。

    :param config: 已加载的配置。
    :param workdir: 工作目录。
    """
    try:
        from src.core.health import run_startup_health_check

        # ⚠️ 走带守卫的那个 —— **一个进程只体检一次**（v2.2.1）。
        # 否则 exe 入口打一份、启动器再打一份，
        # 终端上会出现两份统计。
        run_startup_health_check(config, workdir=workdir)
    except Exception as exc:  # noqa: BLE001 —— 体检失败不阻断启动
        _say(f"开箱安全体检跳过（{type(exc).__name__}: {exc}）。")


def _start_hot_reloader(
    config: Dict[str, Any],
    config_path: str,
    sessions: List["AgentSession"],
) -> Any:
    """开一个后台线程盯 ``config.yaml``，变了就热重载规则（v2.2.0）。

    只换**规则引擎** —— 那正是用户半夜要调的「拦截策略」。
    通知通道、守护者这些**不重建**：重建它们等于把服务重启一遍，
    那就不是「热重载」了。

    :param config: 已加载的配置。
    :param config_path: 配置文件路径。
    :param sessions: 已启动的会话列表。
    :return: 重载器实例；没开或出错时返回 ``None``。
    """
    rules_config = config.get("rules") or {}
    hot_config = rules_config.get("hot_reload") or {}
    if not bool(hot_config.get("enabled", True)):
        _say("规则热重载已关闭（rules.hot_reload.enabled=false）。")
        return None
    if not config_path or not os.path.isfile(config_path):
        _say("没找到配置文件（{}），规则热重载未启用。".format(config_path))
        return None
    try:
        from src.core.hotreload import RuleHotReloader

        engines = [
            item.engine for item in sessions if getattr(item, "engine", None)
        ]
        if not engines:
            return None
        interval = float(hot_config.get("interval") or 1.0)
        reloader = RuleHotReloader(
            config_path=config_path,
            engines=engines,
            interval=interval,
            notify=_say,
            event_sink=lambda payload: _write_event(
                sessions[0].alert_log,
                str(payload.get("kind") or "event"),
                str(payload.get("title") or ""),
                str(payload.get("detail") or ""),
                severity=str(payload.get("severity") or "info"),
            ),
        )
        reloader.start()
        _say(
            "规则热重载已开启：改动 {} 会在后台自动生效（无需重启）。".format(
                os.path.basename(config_path)
            )
        )
        return reloader
    except Exception as exc:  # noqa: BLE001 —— 热重载失败不阻断启动
        _say(f"规则热重载未启用（{type(exc).__name__}: {exc}）。")
        return None


def main(argv: Optional[List[str]] = None) -> int:
    """命令行入口。

    :param argv: 参数列表；``None`` 时取 ``sys.argv[1:]``。
    :return: 进程退出码（有告警时为 1）。
    """
    parser = argparse.ArgumentParser(
        prog="run_with_sentinel",
        description="启动真实的 AI CLI Agent，并用哨兵实时审查它的流式输出",
    )
    parser.add_argument(
        "task",
        nargs="?",
        default=None,
        help="交给 Agent 的任务描述（多路模式请改用 --session-file）",
    )
    parser.add_argument(
        "--session-file",
        default=None,
        help="多路会话配置文件（JSON）；给了就同时接管多个 Agent",
    )
    parser.add_argument(
        "--agent",
        default="mcode",
        choices=sorted(AGENT_PROFILES),
        help="要接管的 Agent 类型，默认 mcode",
    )
    parser.add_argument(
        "--cwd",
        default=os.getcwd(),
        help="Agent 的工作目录，默认当前目录",
    )
    parser.add_argument(
        "--model", default=None, help="覆盖模型（provider/model）"
    )
    parser.add_argument(
        "--max-steps", type=int, default=20, help="最大步数（mcode 专用），默认 20"
    )
    parser.add_argument(
        "--permission",
        default="smart",
        choices=list(PERMISSION_MODES),
        help="权限档位：smart / full / off，默认 smart",
    )
    parser.add_argument(
        "--extra-arg",
        action="append",
        default=[],
        dest="extra_args",
        help="透传给 Agent 的额外参数（可重复）",
    )
    parser.add_argument(
        "--config", default=None, help="哨兵配置文件，默认 config/config.yaml"
    )
    parser.add_argument(
        "--alert-log",
        default=os.path.join("logs", "sentinel_alert.log"),
        help="告警日志路径",
    )
    parser.add_argument(
        "--raw-log",
        default=os.path.join("logs", "agent_live.jsonl"),
        help="Agent 原始输出的落盘路径（看板会跟着它显示）",
    )
    parser.add_argument("--host", default="127.0.0.1", help="看板监听地址")
    parser.add_argument("--port", type=int, default=8000, help="看板监听端口")
    parser.add_argument(
        "--no-gui", action="store_true", help="不启动看板，只在终端审查"
    )
    parser.add_argument(
        "--no-guardian",
        action="store_true",
        help="不启用守护者（只观察，不挂起 Agent 进程）",
    )
    parser.add_argument(
        "--no-notify",
        action="store_true",
        help="不装配任何告警通道（终端与日志仍然照常输出）",
    )
    parser.add_argument(
        "--no-llm",
        action="store_true",
        help="不调用模型层，只做规则层判定（更快，也不需要 Ollama）",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="看板启动后**不**自动打开浏览器（适合远程 / 无桌面环境）",
    )
    parser.add_argument(
        "--keep-alive",
        action="store_true",
        help="Agent 跑完后**不关看板**，一直留着直到 Ctrl+C（适合挂一整夜）",
    )
    parser.add_argument(
        "--no-briefing",
        action="store_true",
        help="会话结束时**不生成晨报**（默认会生成并保存到「晨报归档/」）",
    )
    parser.add_argument(
        "--no-health",
        action="store_true",
        help="启动时**不**跑开箱安全体检（默认会跑一次）",
    )
    args = parser.parse_args(argv)

    _prepare_stdout()
    root = _find_project_root()
    if root not in sys.path:
        sys.path.insert(0, root)
    os.chdir(root)

    from src.config_loader import load_config

    config = load_config(args.config)

    # ---- 会话计划 ----------------------------------------------------
    if args.session_file:
        try:
            plans = load_session_plans(args.session_file)
        except (OSError, ValueError) as exc:
            _say(str(exc))
            return 2
    elif args.task:
        plans = [
            SessionPlan(
                id="default",
                agent=args.agent,
                task=args.task,
                label="默认会话",
            )
        ]
    else:
        _say("要么给任务描述，要么给 --session-file。")
        return 2

    multi = len(plans) > 1 or bool(args.session_file)
    alert_dir = os.path.dirname(os.path.abspath(args.alert_log))
    raw_dir = os.path.dirname(os.path.abspath(args.raw_log))

    # ---- 端口占用：**在启动 Agent 之前**就查清楚 -----------------------
    # 看板起不来不影响审查（终端照样出告警），但用户得**马上**知道
    # 「端口被占了」—— 不然等 Agent 跑完了才发现看板是空的，很难受。
    dashboard_enabled = not args.no_gui
    if dashboard_enabled:
        from src.console import port_is_free, print_port_busy

        if not port_is_free(args.host, args.port):
            print_port_busy(args.host, args.port)
            _say("看板将不启动，本次只在终端审查。")
            _say(f"请改用别的端口，例如：--port {args.port + 1}")
            dashboard_enabled = False

    # ---- v3.8.0：单例拦截（防「双击两次出两个托盘」）--------------------
    #
    # ⚠️ 只有**会出托盘图标**的路径才上锁 ——
    #    ``--no-gui`` 是纯终端审查，用户可能故意并跑多路，
    #    拦它反而是功能倒退。
    #
    # ⚠️ 必须在**启动 Agent 之前**拦 —— 拦晚了Agent 已经跑起来了，
    #    白白浪费一次模型调用。
    if dashboard_enabled:
        try:
            from src.gui.single_instance import (
                ALREADY_RUNNING_EXIT_CODE,
                acquire_single_instance,
                install_process_group,
                report_already_running,
            )

            if acquire_single_instance() is None:
                report_already_running()
                return ALREADY_RUNNING_EXIT_CODE
            group = install_process_group()
            if not group.enabled:
                _say(f"进程树同生共死不可用：{group.reason}")
        except Exception as exc:  # noqa: BLE001 —— 锁不住也不能挡住审查
            _say(
                "单例锁不可用（{}: {}），本次不做双开拦截。".format(
                    type(exc).__name__, exc
                )
            )

    # ---- 通知通道：**全局共用一个** ----
    # 每路会话各建一个的话，同一件事会被推 N 次到手机上 ——
    # 那不是「多路复用」，是「多路骚扰」。
    shared_notifier = None
    if not args.no_notify:
        try:
            from src.core.notifier import Notifier

            shared_notifier = Notifier(config)
            _say(f"告警通道已装配：{len(shared_notifier.channels)} 个")
        except Exception as exc:  # noqa: BLE001 —— 通知层坏了不阻断审查
            _say(f"通知通道不可用（{type(exc).__name__}: {exc}），仅终端与日志。")

    # ---- Webhook 真实连通性自检（v2.1.0）----
    # 配了真实 Webhook 就**真的发一条**出去 ——
    # 告警是偶发的，等出事才发现推不出去就太晚了。
    if not args.no_notify:
        try:
            from src.core.webhook_probe import startup_probe

            probe_results = startup_probe(config, _version())
            for item in probe_results or []:
                _say(
                    "Webhook 自检 → {}：{}".format(
                        item.get("url"), item.get("detail")
                    )
                )
        except Exception as exc:  # noqa: BLE001 —— 自检失败不阻断审查
            _say(f"Webhook 自检跳过（{type(exc).__name__}: {exc}）。")

    # ---- 开箱安全体检（v2.2.0）----
    # 用户拿到哨兵的第一件事就该知道「我的环境配好了没」。
    # 体检失败**绝不**阻断启动（架构红线 R5）。
    if not args.no_health:
        _run_startup_health(config, args.cwd)

    sessions: List[AgentSession] = []
    for plan in plans:
        if multi:
            # 多路模式：每个会话**各写各的**日志 ——
            # 看板要靠分开的日志把各路的告警分开。
            raw_log = os.path.join(raw_dir, f"{plan.id}.jsonl")
            alert_log = os.path.join(alert_dir, f"{plan.id}.log")
        else:
            raw_log = os.path.abspath(args.raw_log)
            alert_log = os.path.abspath(args.alert_log)
        sessions.append(
            AgentSession(
                plan,
                config=config,
                args=args,
                raw_log=raw_log,
                alert_log=alert_log,
                notifier=shared_notifier,
            )
        )
        # 晨报默认开启（v1.2.0）—— 挂一整夜的用户最需要它。
        sessions[-1].briefing_enabled = not args.no_briefing

    # ---- 启动全部 Agent ----------------------------------------------
    started: List[AgentSession] = [
        session for session in sessions if session.prepare()
    ]
    if not started:
        _say("没有一路会话启动成功。")
        return 2

    # ---- 规则热重载（v2.2.0）----
    # 半夜改了 config.yaml 不用重启哨兵 —— 规则引擎会**原地换掉**。
    _start_hot_reloader(config, _resolve_config_path(args.config), started)

    # ---- 看板（守护者与引擎同进程，按钮才能恢复 Agent） ----
    server = None
    if dashboard_enabled:
        from src.console import (
            announce_ready_when_up,
            print_banner,
        )
        try:
            if multi:
                from src.gui.server import SessionSpec

                specs = [
                    SessionSpec(
                        id=item.plan.id,
                        label=item.plan.label,
                        agent=item.profile.name,
                        watch_path=item.raw_log,
                        alert_log_path=item.alert_log,
                        guardian_callback=item.guardian,
                        watch_target=item.watch_target,
                        briefing_callback=item.briefing_callback,
                        # 控制中枢（v2.0.0）——
                        # 启动器握着 Agent 的管道，所以由它来注入；
                        # 看板只做转发。
                        inject_callback=item.inject,
                        rollback_callback=item.rollback,
                        # v2.1.0：一键复活（回滚 + 注入 + 重拉 Agent）——
                        # 同样由启动器执行，因为它手上有 Agent 的进程。
                        resume_callback=item.resume,
                        plan_callback=item.resume_plan,
                        safe_point_service=item.safe_point_service,
                        workdir=item.workdir,
                    )
                    for item in started
                ]
                server = _start_dashboard(
                    None,
                    "",
                    "",
                    args.host,
                    args.port,
                    session_specs=specs,
                )
            else:
                only = started[0]
                server = _start_dashboard(
                    only.guardian,
                    only.raw_log,
                    only.alert_log,
                    args.host,
                    args.port,
                    watch_target=only.watch_target,
                    briefing_callback=only.briefing_callback,
                    # 控制中枢（v2.0.0）
                    inject_callback=only.inject,
                    rollback_callback=only.rollback,
                    # v2.1.0：一键复活
                    resume_callback=only.resume,
                    plan_callback=only.resume_plan,
                    safe_point_service=only.safe_point_service,
                    workdir=only.workdir,
                )
            threading.Thread(
                target=_run_dashboard,
                args=(server, args.host, args.port),
                name="sentinel-dashboard",
                daemon=True,
            ).start()
            # ⚠️ v3.4.0：**托盘图标**（右下角常驻）——
            #
            # 用户实测最痛的一条是「找不到退出的地方」。
            # 托盘给了他们一个**随时能按**的退出键：
            # 右键 →【彻底退出】。
            #
            # ⚠️ 托盘是**可选**的 —— pystray / Pillow 没装就不出图标，
            #    哨兵其它功能一点不受影响（红线 R5）。
            try:
                from src.gui.server import (
                    bind_shutdown_server,
                    install_shutdown_console_handler,
                    register_shutdown_hook,
                    start_tray_icon,
                )

                start_tray_icon(args.host, args.port)
                # ⚠️ v3.8.0：把**自己**建的 uvicorn 绑给退出协调器 ——
                #
                # 托盘的【彻底退出】靠它停机。之前这条路径
                # **没有绑**，所以托盘退出只能等 Agent 线程自己 unwind ——
                # 表现就是「点了退出，窗口半天不关」。
                bind_shutdown_server(server)
                # ⚠️ v3.8.0：接管模式也要装**关窗处理器** ——
                #
                # v3.4.0 只在 ``src.gui.server.main()`` 里装过，
                # 接管模式绕开了那个函数 —— 于是「关掉黑框」
                # 在接管模式下**仍然留下幽灵进程**。
                install_shutdown_console_handler()
                # ⚠️ v3.4.0：退出时把**自己拉起的** Agent 收掉 ——
                #
                # 哨兵自己启动的 Agent 不收的话会变成孤儿，
                # 在后台继续跑而用户完全不知情。
                #
                # ⚠️ 雷达挂载进来的 Agent **不动** ——
                #    那是用户自己的进程（红线 R6）。
                register_shutdown_hook(
                    lambda reason: _terminate_agent_trees(started)
                )
            except Exception as exc:  # noqa: BLE001 —— 托盘失败不影响审查
                _say(
                    "托盘图标不可用（{}: {}），"
                    "可以用看板右上角的【关闭哨兵】退出。".format(
                        type(exc).__name__, exc
                    )
                )
            print_banner()
            # 就绪提示 + 自动开浏览器放在后台线程里做 ——
            # 只有服务**真的**能连上了才敢说「已就绪」。
            announce_ready_when_up(
                args.host,
                args.port,
                version=_raw_version(),
                open_browser=not args.no_browser,
            )
        except Exception as exc:  # noqa: BLE001 —— 看板失败不影响审查
            _say(f"看板启动失败（{type(exc).__name__}: {exc}），继续终端审查。")
    else:
        # 没有看板 —— 审查**已经在跑**了，这就是「就绪」的时刻（v3.0.0）。
        from src.console import print_greeting

        print_greeting(_raw_version())

    # ---- 自动锚定：把用户的原始请求喂进去 ----
    for item in started:
        if item.pipe is not None:
            item.pipe.feed_user_message(item.plan.task)
        _sync_watch_target(item.watch_target, item.intent_tracker)

    # ---- 并行审查 ----------------------------------------------------
    threads = []
    for item in started:
        thread = threading.Thread(
            target=item.run,
            name=f"sentinel-agent-{item.plan.id}",
            daemon=True,
        )
        thread.start()
        threads.append(thread)

    try:
        for thread in threads:
            while thread.is_alive():
                thread.join(timeout=0.5)
    except KeyboardInterrupt:
        _say("收到 Ctrl+C，停止审查。")
    finally:
        for item in started:
            try:
                item.close()
            except Exception as exc:  # noqa: BLE001 —— 收尾失败不影响退出
                _say(f"会话 {item.plan.id} 收尾失败：{exc}")

    # ---- 关掉通知通道 ------------------------------------------------
    # ⚠️ 必须**在进程退出前**关 —— 否则后台投递线程会随进程一起消失，
    # 队列里还没投出去的报文（包括刚推的晨报）就丢了。
    # ``Notifier.close()`` 会先把队列跑完再停线程。
    if shared_notifier is not None:
        try:
            shared_notifier.close()
        except Exception as exc:  # noqa: BLE001 —— 清理失败不影响退出
            _say(f"关闭通知通道失败：{exc}")

    # ---- 挂机模式：Agent 跑完了，但看板留着 ----
    # 「晚上睡觉开着」的真实需求 —— 半夜起来还能翻告警、还能放行。
    if args.keep_alive and server is not None:
        _say(
            f"Agent 已全部结束，看板继续运行：http://{args.host}:{args.port}"
            "（Ctrl+C 退出）"
        )
        try:
            while not server.should_exit:
                time.sleep(0.5)
        except KeyboardInterrupt:
            _say("收到 Ctrl+C，关闭看板。")
    if server is not None:
        server.should_exit = True

    # ⚠️ v3.8.0：托盘图标必须收掉 ——
    #
    # 进程退出时 Explorer 会把图标收走，但那要等进程**真的死透**；
    # 明确收一次更干净 —— 收尾的这几秒里不会留下一个点不动的死图标。
    try:
        from src.gui.server import stop_tray_icon

        stop_tray_icon()
    except Exception as exc:  # noqa: BLE001 —— 收尾失败不影响退出
        _say(f"收尾托盘图标失败：{exc}")

    total_alerts = 0
    for item in started:
        _say(item.summary())
        if item.engine is not None:
            total_alerts += len(item.engine.alerts)
    return 1 if total_alerts else 0


if __name__ == "__main__":
    raise SystemExit(main())
